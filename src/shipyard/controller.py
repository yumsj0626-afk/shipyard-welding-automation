"""Public workflow; the controller never receives a simulator or private path."""
from . import checker, constraints, events, scheduler
from .contracts import (
    ArrivalBatch, BaselineAssignment, BaselinePlan, ExecutionProgress, Instruction,
    Observation, PhaseRecord, Policy, new_id,
)
from .repository import Repository
from .reports import (AdapterResult, ReportDelivery, ReportModel, fallback_delivery,
                      interpret_report, report_fingerprint)


class Controller:
    def __init__(self, repository: Repository, policy: Policy):
        self.repository = repository
        self.policy = policy

    def _accept_observations(self, batch: ArrivalBatch):
        snapshot = self.repository.read_snapshot()
        if batch.tick < snapshot.now_tick:
            raise ValueError("INVALID_INPUT: clock cannot move backwards")
        if batch.tick != snapshot.now_tick:
            snapshot.now_tick = batch.tick
            self.repository.commit_snapshot(snapshot, snapshot.state_version, "CLOCK", "ADVANCED")
        for observation in batch.observations:
            self.complete_phase(observation)

    def handle_batch(self, batch: ArrivalBatch):
        self._accept_observations(batch)
        if batch.deliveries:
            snapshot = self.repository.read_snapshot()
            fresh, prior = self.repository.filter_deliveries(batch.deliveries)
            updated, results = events.apply_deliveries(snapshot, fresh)
            self.repository.commit_event_batch(updated, batch.deliveries, prior + results, snapshot.state_version)
        self._cancel_blocked_preparation()
        return self.plan_and_apply()

    def handle_report_batch(self, tick: int, observations: list[Observation],
                            reports: list[ReportDelivery], model: ReportModel):
        """Interpret all arrivals before one shared event update and replan."""
        before = self.repository.read_snapshot()
        self._accept_observations(ArrivalBatch(tick=tick, observations=observations))
        snapshot = self.repository.read_snapshot()
        receipts = self.repository.report_receipts()
        grouped = {}
        for item in reports:
            grouped.setdefault(item.report.report_id, []).append(item)
        results = []
        accepted = {}
        rejected = False
        for report_id, group in grouped.items():
            fingerprints = {report_fingerprint(item) for item in group}
            old = receipts.get(report_id)
            if old:
                duplicate = fingerprints == {old["report_fingerprint"]}
                self.repository.audit("REPORT_RECEIPT", "DUPLICATE" if duplicate else "ID_CONFLICT",
                                      {"report_id": report_id, "original_status": old["status"],
                                       "fingerprints": sorted(fingerprints)})
                if not duplicate:
                    rejected = True
                    self.repository.audit("HUMAN_REQUEST", "REQUESTED",
                                          {"report_id": report_id, "reason_codes": ["REPORT_ID_REUSED"]})
                continue
            item = group[0]
            if len(fingerprints) > 1:
                result = AdapterResult(status="ID_CONFLICT", reason_codes=["REPORT_ID_REUSED"],
                    report_id=report_id, report_fingerprint=report_fingerprint(item),
                    based_on_state_version=snapshot.state_version, received_report=item.model_copy(deep=True),
                    trace=[{"step": "CONFLICT", "fingerprints": sorted(fingerprints)}])
            else:
                result = interpret_report(item, snapshot, model)
            results.append(result)
            accepted[report_id] = item
        current = self.repository.read_snapshot()
        for result in results:
            if result.delivery and current.state_version != result.based_on_state_version:
                result.status = "STALE_CONTEXT"
                result.reason_codes = ["STATE_CHANGED_DURING_INTERPRETATION"]
                result.delivery = fallback_delivery(accepted[result.report_id], current)
        deliveries = [result.delivery for result in results if result.delivery]
        if results:
            fresh, prior = self.repository.filter_deliveries(deliveries)
            updated, event_results = events.apply_deliveries(current, fresh)
            self.repository.commit_event_batch(updated, deliveries, prior + event_results,
                                               current.state_version, report_results=results)
        if reports and not deliveries and before.state_version == current.state_version:
            return "REPORT_REJECTED" if results or rejected else "DUPLICATE"
        self._cancel_blocked_preparation()
        return self.plan_and_apply()

    def _cancel_blocked_preparation(self):
        snapshot = self.repository.read_snapshot()
        constraint_set = constraints.build_constraints(snapshot, self.policy)
        deferred = {d.job_id for d in constraint_set.deferred_jobs}
        changed = False
        for progress in list(snapshot.execution_progress):
            job = next(j for j in snapshot.jobs if j.job_id == progress.job_id)
            if job.lifecycle != "COMMITTED" or job.job_id not in deferred or progress.current_phase:
                continue
            robot = next(r for r in snapshot.robots if r.robot_id == progress.robot_id)
            job.lifecycle = "NOT_STARTED"
            job.assigned_robot_id = None
            job.active_execution_id = None
            robot.phase = "IDLE"
            robot.phase_job_id = None
            robot.destination_zone_id = None
            own_ids = {p.execution_id for p in progress.completed_phases}
            for zone in snapshot.zones:
                zone.active_execution_ids = [e for e in zone.active_execution_ids if e not in own_ids]
            snapshot.execution_progress.remove(progress)
            for instruction in snapshot.current_instructions:
                if instruction.job_id == job.job_id and instruction.status == "PENDING":
                    instruction.status = "CANCELLED"
            changed = True
        if changed:
            self.repository.commit_snapshot(snapshot, snapshot.state_version, "CANCEL", "PREPARATION_CANCELLED", {"deferred_job_ids": sorted(deferred)})

    def plan_and_apply(self):
        for retry in range(self.policy.stale_retries + 1):
            snapshot = self.repository.read_snapshot()
            constraint_set = constraints.build_constraints(snapshot, self.policy)
            plan = scheduler.solve(snapshot, constraint_set, self.policy)
            self.repository.record_plan(plan)
            if plan.solver_result.status not in {"OPTIMAL", "FEASIBLE_NOT_PROVEN", "NO_ELIGIBLE_WORK"}:
                self.repository.audit("FAILURE", plan.solver_result.status, {"reason_codes": plan.solver_result.reason_codes, "recheck_triggers": ["NEW_EVIDENCE", "PHASE_ENDED", "MODEL_DIAGNOSIS"]})
                return plan.solver_result.status
            result = checker.check_plan(snapshot, plan, self.policy)
            self.repository.record_plan(plan, result)
            if not result.passed:
                return "CHECK_REJECTED"
            instructions = self._instructions(snapshot, plan)
            try:
                self.repository.apply_plan(plan, result, instructions)
            except (ValueError, RuntimeError) as error:
                if "STALE" not in str(error):
                    raise
                self.repository.audit("APPLY", "STALE_PLAN", {"plan_id": plan.plan_id, "retry": retry})
                continue
            for instruction in instructions:
                if instruction.action == "WAIT":
                    self.repository.audit("WAIT", "NO_ELIGIBLE_WORK" if not plan.assignments else "WAIT_FOR_PLAN", instruction.model_dump(mode="json"))
            return plan.solver_result.status
        self.repository.audit("FAILURE", "RETRY_EXHAUSTED", {"reason_codes": ["STATE_CHANGED"]})
        return "RETRY_EXHAUSTED"

    def _instructions(self, snapshot, plan):
        result = []
        for a in plan.assignments:
            for action, start, end in [("MOVE", a.move_start, a.move_end), ("SETUP", a.setup_start, a.setup_end), ("WELD", a.weld_start, a.weld_end)]:
                if start is None:
                    continue
                result.append(Instruction(instruction_id=new_id("instruction"), plan_id=plan.plan_id,
                    accepted_state_version=snapshot.state_version + 1, job_id=a.job_id,
                    robot_id=a.robot_id, action=action, not_before_tick=start, expected_end_tick=end,
                    precondition_refs=a.predecessor_ids, attempt_id=a.attempt_id,
                    from_zone_id=a.source_position if action == "MOVE" else a.zone_id, to_zone_id=a.zone_id))
        for robot in snapshot.robots:
            if robot.phase != "IDLE":
                continue
            future = [i for i in result if i.robot_id == robot.robot_id]
            if future and min(i.not_before_tick for i in future) <= snapshot.now_tick:
                continue
            reasons = sorted({r for d in plan.deferred_jobs for r in d.reason_codes}) or ["WAIT_FOR_RESOURCE_OR_PREDECESSOR"]
            triggers = sorted({t for d in plan.deferred_jobs for t in d.recheck_triggers}) or ["PHASE_ENDED"]
            if all(j.lifecycle == "COMPLETED" for j in snapshot.jobs):
                continue
            result.append(Instruction(instruction_id=new_id("wait"),plan_id=plan.plan_id,
                accepted_state_version=snapshot.state_version+1,robot_id=robot.robot_id,action="WAIT",
                not_before_tick=snapshot.now_tick,reason_codes=reasons,recheck_triggers=triggers))
        return result

    def authorize_phase_start(self, instruction_id: str) -> Observation | None:
        snapshot = self.repository.read_snapshot()
        instruction = next((i for i in snapshot.current_instructions if i.instruction_id == instruction_id), None)
        if instruction is None or instruction.status != "PENDING" or instruction.action == "WAIT":
            return None
        violations = checker.check_phase_start(snapshot, instruction, self.policy)
        if violations:
            self.repository.audit("START_GATE", "CHECK_REJECTED", {"instruction_id": instruction_id, "reason_codes": violations})
            return None
        before = snapshot.state_version
        job = next(j for j in snapshot.jobs if j.job_id == instruction.job_id)
        robot = next(r for r in snapshot.robots if r.robot_id == instruction.robot_id)
        progress = next((p for p in snapshot.execution_progress if p.job_id == job.job_id), None)
        if progress is None:
            progress = ExecutionProgress(job_id=job.job_id, robot_id=robot.robot_id, attempt_id=instruction.attempt_id)
            snapshot.execution_progress.append(progress)
        execution_id = new_id("execution")
        record = PhaseRecord(phase=instruction.action, execution_id=execution_id, start_tick=snapshot.now_tick,
            end_tick=instruction.expected_end_tick, from_zone_id=instruction.from_zone_id, to_zone_id=instruction.to_zone_id)
        progress.current_phase = record
        progress.next_phase = None
        instruction.status = "RUNNING"
        instruction.execution_id = execution_id
        job.lifecycle = "RUNNING" if instruction.action == "WELD" else "COMMITTED"
        job.assigned_robot_id = robot.robot_id
        job.active_execution_id = execution_id
        if instruction.action == "WELD":
            job.actual_weld_start = snapshot.now_tick
        robot.phase = {"MOVE":"MOVING", "SETUP":"SETTING_UP", "WELD":"WELDING"}[instruction.action]
        robot.phase_job_id = job.job_id
        robot.phase_execution_id = execution_id
        robot.phase_start_tick = snapshot.now_tick
        robot.phase_end_tick = instruction.expected_end_tick
        robot.destination_zone_id = instruction.to_zone_id
        if instruction.action in {"SETUP", "WELD"}:
            zone = next(z for z in snapshot.zones if z.zone_id == job.zone_id)
            zone.active_execution_ids = [execution_id]
        observation = Observation(observation_id=new_id("observation"), execution_id=execution_id,
            instruction_id=instruction_id,job_id=job.job_id,robot_id=robot.robot_id,phase=instruction.action,
            kind="PHASE_STARTED",tick=snapshot.now_tick,source_id="execution-observer",
            from_zone_id=instruction.from_zone_id,to_zone_id=instruction.to_zone_id,expected_state_version=before)
        self.repository.commit_observation(snapshot, observation, before)
        return observation

    def complete_phase(self, observation: Observation):
        if self.repository.observation_receipt(observation):
            return
        snapshot = self.repository.read_snapshot()
        source = next((s for s in snapshot.sources if s.source_id == observation.source_id), None)
        if not source or source.source_kind != "EXECUTION_OBSERVER" or "PHASE_ENDED" not in source.allowed_observation_kinds:
            raise ValueError("INVALID_INPUT: unauthorized observation source")
        progress = next((p for p in snapshot.execution_progress if p.current_phase and p.current_phase.execution_id == observation.execution_id), None)
        instruction = next((i for i in snapshot.current_instructions if i.instruction_id == observation.instruction_id), None)
        if not progress or not instruction:
            raise ValueError("INVALID_INPUT: unknown execution")
        phase = progress.current_phase
        if (observation.kind != "PHASE_ENDED" or observation.tick != snapshot.now_tick
            or observation.tick != phase.end_tick or observation.job_id != progress.job_id
            or observation.robot_id != progress.robot_id or observation.phase != phase.phase
            or observation.from_zone_id != phase.from_zone_id or observation.to_zone_id != phase.to_zone_id
            or instruction.execution_id != observation.execution_id or instruction.status != "RUNNING"):
            raise ValueError("INVALID_INPUT: observation does not match execution")
        before = snapshot.state_version
        job = next(j for j in snapshot.jobs if j.job_id == progress.job_id)
        robot = next(r for r in snapshot.robots if r.robot_id == progress.robot_id)
        instruction.status = "COMPLETED"
        progress.completed_phases.append(phase)
        progress.current_phase = None
        progress.next_phase = {"MOVE":"SETUP", "SETUP":"WELD", "WELD":None}[phase.phase]
        progress.setup_valid = phase.phase == "SETUP"
        robot.location_zone_id = phase.to_zone_id
        robot.phase_execution_id = None
        robot.phase_start_tick = None
        robot.phase_end_tick = None
        job.active_execution_id = None
        if phase.phase == "WELD":
            job.lifecycle = "COMPLETED"
            job.actual_weld_end = observation.tick
            robot.phase = "IDLE"
            robot.phase_job_id = None
            robot.destination_zone_id = None
            zone = next(z for z in snapshot.zones if z.zone_id == job.zone_id)
            zone.active_execution_ids = []
        else:
            robot.phase = "WAITING_FOR_ZONE"
        self.repository.commit_observation(snapshot, observation, before)
