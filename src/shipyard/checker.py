"""Independent execution checks against public facts, never solver expressions.

This module deliberately imports neither constraints nor scheduler. It
reconstructs obligations and uses ordinary interval arithmetic rather than
trusting the solver's constraint list or its claimed feasibility.
"""
from __future__ import annotations

from .contracts import CheckResult, Instruction, Plan, PlanningSnapshot, Policy, digest


def _restrictions_for(snapshot, job, robot_id=None):
    matches = []
    for restriction in snapshot.active_restrictions:
        if restriction.status != "ACTIVE":
            continue
        target = restriction.target
        applies = {"JOB": target.job_id == job.job_id,
                   "ZONE": target.zone_id == job.zone_id,
                   "ROBOT": robot_id is not None and target.robot_id == robot_id,
                   "JOB_ROBOT": robot_id is not None and target.job_id == job.job_id and target.robot_id == robot_id,
                   "UNRESOLVED": False}[target.scope]
        if applies:
            matches.append(restriction)
    return matches


def _raw_obligations(snapshot):
    """Independent fixed-point closure (not the translator's DAG traversal)."""
    jobs = {j.job_id: j for j in snapshot.jobs}
    zones = {z.zone_id: z for z in snapshot.zones}
    progresses = {p.job_id: p for p in snapshot.execution_progress}
    allowed = {}
    required = {j.job_id for j in snapshot.jobs if j.lifecycle == "RUNNING"}
    for job in snapshot.jobs:
        if job.lifecycle in ("COMPLETED", "RUNNING"):
            continue
        if (job.material_ready != "TRUE" or job.zone_id not in zones
                or zones[job.zone_id].availability != "AVAILABLE"
                or _restrictions_for(snapshot, job)):
            continue
        options = {r.robot_id for r in snapshot.robots
            if r.availability == "AVAILABLE" and job.job_id in r.capable_job_ids
            and job.zone_id in r.allowed_zone_ids and not _restrictions_for(snapshot, job, r.robot_id)}
        if job.lifecycle == "COMMITTED":
            p = progresses.get(job.job_id)
            options &= {p.robot_id} if p else set()
        if options:
            allowed[job.job_id] = options
    candidates = set(allowed)
    while True:
        additions = {j for j in candidates - required
            if all(p in required or p in jobs and jobs[p].lifecycle == "COMPLETED"
                   for p in jobs[j].predecessor_ids)}
        if not additions:
            break
        required.update(additions)
    deferred = {j.job_id for j in snapshot.jobs if j.lifecycle != "COMPLETED"} - required
    return required, deferred, allowed


def _interval_overlap(intervals):
    """Return every conflicting pair; adjacent and zero-length intervals fit."""
    result = []
    for index, (a, b, label) in enumerate(intervals):
        for c, d, other in intervals[index + 1:]:
            if a < b and c < d and max(a, c) < min(b, d):
                result.append(f"{label}/{other}")
    return result


def check_plan(snapshot: PlanningSnapshot, plan: Plan, policy: Policy) -> CheckResult:
    violations = []

    def fail(code, detail=""):
        violations.append(f"{code}:{detail}" if detail else code)

    if plan.run_id != snapshot.run_id:
        fail("RUN_MISMATCH")
    if (plan.based_on_state_version, plan.based_on_active_plan_version, plan.based_on_tick) != (
            snapshot.state_version, snapshot.active_plan_version, snapshot.now_tick):
        fail("STALE_PLAN")
    if (plan.policy_version, plan.rule_version) != (policy.policy_version, policy.rule_version):
        fail("POLICY_VERSION_MISMATCH")
    if plan.payload_hash != digest(plan.model_dump(mode="json", exclude={"payload_hash"})):
        fail("PLAN_HASH_MISMATCH")
    if plan.solver_result.status not in ("OPTIMAL", "FEASIBLE_NOT_PROVEN", "NO_ELIGIBLE_WORK"):
        fail("NO_FEASIBLE_CANDIDATE", plan.solver_result.status)

    jobs = {j.job_id: j for j in snapshot.jobs}
    robots = {r.robot_id: r for r in snapshot.robots}
    zones = {z.zone_id: z for z in snapshot.zones}
    travel = {(t.from_zone_id, t.to_zone_id): t.duration_ticks for t in snapshot.travel_times}
    progresses = {p.job_id: p for p in snapshot.execution_progress}
    required, deferred, allowed = _raw_obligations(snapshot)
    if len(plan.required_job_ids) != len(set(plan.required_job_ids)) or set(plan.required_job_ids) != required:
        fail("REQUIRED_JOB_SET")
    deferred_ids = [d.job_id for d in plan.deferred_jobs]
    if len(deferred_ids) != len(set(deferred_ids)) or set(deferred_ids) != deferred:
        fail("DEFERRED_JOB_SET")
    for item in plan.deferred_jobs:
        if not item.reason_codes or not item.recheck_triggers:
            fail("UNEXPLAINED_DEFERRAL", item.job_id)
    expected_fixed = {}
    for p in snapshot.execution_progress:
        phase = p.current_phase
        if phase:
            expected_fixed[phase.execution_id] = dict(execution_id=phase.execution_id,
                attempt_id=p.attempt_id, job_id=p.job_id, robot_id=p.robot_id,
                phase=phase.phase, zone_id=phase.to_zone_id if phase.phase != "MOVE" else None,
                start_tick=phase.start_tick, end_tick=phase.end_tick)
    actual_fixed = {f.execution_id: f.model_dump() for f in plan.fixed_segments}
    if len(actual_fixed) != len(plan.fixed_segments) or actual_fixed != expected_fixed:
        fail("FIXED_EXECUTION_CHANGED")
    fixed_weld = {x["job_id"]: x for x in expected_fixed.values() if x["phase"] == "WELD"}
    assignment_map = {a.job_id: a for a in plan.assignments}
    if len(assignment_map) != len(plan.assignments):
        fail("DUPLICATE_JOB_ASSIGNMENT")
    if set(assignment_map) != required - fixed_weld.keys():
        fail("COMPLETION_PATH_SET")
    if plan.solver_result.status == "NO_ELIGIBLE_WORK" and required - fixed_weld.keys():
        fail("FALSE_NO_ELIGIBLE_WORK")

    robot_intervals = {r: [] for r in robots}
    zone_intervals = {z: [] for z in zones}
    for fixed in expected_fixed.values():
        interval = (fixed["start_tick"], fixed["end_tick"], fixed["execution_id"])
        robot_intervals[fixed["robot_id"]].append(interval)
        if fixed["zone_id"] is not None:
            zone_intervals[fixed["zone_id"]].append(interval)
    suffixes = {r: [] for r in robots}
    for a in plan.assignments:
        if a.job_id not in jobs or a.robot_id not in robots or a.zone_id not in zones:
            fail("UNKNOWN_ENTITY", a.job_id)
            continue
        job = jobs[a.job_id]
        if a.job_id not in required or a.robot_id not in allowed.get(a.job_id, set()):
            fail("FORBIDDEN_ASSIGNMENT", a.job_id)
        if a.zone_id != job.zone_id or a.predecessor_ids != job.predecessor_ids:
            fail("JOB_METADATA_CHANGED", a.job_id)
        if a.is_fixed:
            fail("NEW_ASSIGNMENT_MARKED_FIXED", a.job_id)
        if a.weld_end - a.weld_start != job.duration_ticks:
            fail("WELD_DURATION", a.job_id)
        if a.weld_start < snapshot.now_tick:
            fail("PAST_WELD", a.job_id)
        begin = None
        zone_begin = a.setup_start
        if a.mode == "NEW":
            if job.lifecycle != "NOT_STARTED" or a.resume_phase != "MOVE":
                fail("INVALID_NEW_ATTEMPT", a.job_id)
            if None in (a.move_start, a.move_end, a.setup_start, a.setup_end):
                fail("MISSING_PHASE_TIME", a.job_id)
                continue
            begin = a.move_start
            if begin < snapshot.now_tick:
                fail("PAST_MOVE", a.job_id)
            if a.setup_start < a.move_end or a.move_end < a.move_start:
                fail("PHASE_ORDER", a.job_id)
        else:
            p = progresses.get(a.job_id)
            if job.lifecycle != "COMMITTED" or not p or a.attempt_id != p.attempt_id or a.robot_id != p.robot_id:
                fail("CONTINUATION_IDENTITY", a.job_id)
                continue
            if a.move_start is not None or a.move_end is not None:
                fail("REPEATED_MOVE", a.job_id)
            begin = max(snapshot.now_tick, p.current_phase.end_tick) if p.current_phase else snapshot.now_tick
            expected_resume = ({"MOVE": "SETUP", "SETUP": "WELD"}.get(p.current_phase.phase)
                if p.current_phase else "WELD" if p.setup_valid else "SETUP")
            if a.resume_phase != expected_resume:
                fail("WRONG_RESUME_PHASE", a.job_id)
            if a.resume_phase == "SETUP":
                if a.setup_start is None or a.setup_end is None or a.setup_start < begin:
                    fail("INVALID_SETUP_CONTINUATION", a.job_id)
                    continue
            else:
                zone_begin = a.weld_start
                if a.setup_start is not None or a.setup_end is not None or a.weld_start != begin:
                    fail("REPEATED_OR_DELAYED_SETUP", a.job_id)
            if any(jobs[pred].lifecycle != "COMPLETED" for pred in job.predecessor_ids):
                fail("COMMITTED_PREDECESSOR_NOT_COMPLETED", a.job_id)
            if a.source_position != job.zone_id:
                fail("CONTINUATION_POSITION", a.job_id)
        if a.setup_start is not None:
            if a.setup_end is None or a.setup_end - a.setup_start != job.setup_ticks or a.weld_start != a.setup_end:
                fail("SETUP_DURATION_OR_WELD_GAP", a.job_id)
        if begin is not None and zone_begin is not None:
            robot_intervals[a.robot_id].append((begin, a.weld_end, a.job_id))
            zone_intervals[a.zone_id].append((zone_begin, a.weld_end, a.job_id))
            suffixes[a.robot_id].append((begin, a))
        for pred_id in job.predecessor_ids:
            pred = jobs[pred_id]
            if pred.lifecycle == "COMPLETED":
                pred_end = pred.actual_weld_end
            elif pred_id in fixed_weld:
                pred_end = fixed_weld[pred_id]["end_tick"]
            elif pred_id in assignment_map:
                pred_end = assignment_map[pred_id].weld_end
            else:
                pred_end = None
            if pred_end is None or begin is None or begin < pred_end:
                fail("PRECEDENCE", f"{pred_id}->{a.job_id}")

    for robot_id, intervals in robot_intervals.items():
        for pair in _interval_overlap(intervals):
            fail("ROBOT_OVERLAP", f"{robot_id}:{pair}")
        position = robots[robot_id].location_zone_id
        release = snapshot.now_tick
        for p in snapshot.execution_progress:
            if p.robot_id == robot_id and p.current_phase:
                position = p.current_phase.to_zone_id
                release = max(release, p.current_phase.end_tick)
        for begin, assignment in sorted(suffixes[robot_id], key=lambda item: (item[0], item[1].job_id)):
            if begin < release:
                fail("ROBOT_NOT_RELEASED", assignment.job_id)
            if assignment.mode == "NEW":
                expected_travel = travel.get((position, assignment.zone_id))
                if assignment.source_position != position or expected_travel is None:
                    fail("TRAVEL_SOURCE", assignment.job_id)
                elif assignment.move_end - assignment.move_start != expected_travel:
                    fail("TRAVEL_DURATION", assignment.job_id)
            position, release = assignment.zone_id, assignment.weld_end
    for zone_id, intervals in zone_intervals.items():
        for pair in _interval_overlap(intervals):
            fail("ZONE_OVERLAP", f"{zone_id}:{pair}")

    occupied_now = {x["zone_id"] for x in expected_fixed.values()
        if x["zone_id"] is not None and x["start_tick"] <= snapshot.now_tick < x["end_tick"]}
    possible_now = set()
    for job_id in required - fixed_weld.keys():
        job = jobs[job_id]
        if job.lifecycle == "NOT_STARTED" and job.zone_id not in occupied_now and all(
                jobs[p].lifecycle == "COMPLETED" for p in job.predecessor_ids):
            for robot_id in allowed.get(job_id, set()):
                if robots[robot_id].phase == "IDLE":
                    possible_now.add((job_id, robot_id))
    if possible_now and not any(a.mode == "NEW" and a.move_start == snapshot.now_tick
            and (a.job_id, a.robot_id) in possible_now for a in plan.assignments):
        fail("UNJUSTIFIED_IDLE")

    # Public evidence may be insufficient, but future data must never become
    # a justification for an otherwise valid-looking plan.
    visible = set(snapshot.visible_evidence_refs)
    for evidence in snapshot.evidence:
        if evidence.evidence_id in visible and evidence.available_tick > snapshot.now_tick:
            fail("FUTURE_EVIDENCE", evidence.evidence_id)
    return CheckResult(plan_id=plan.plan_id, plan_payload_hash=plan.payload_hash,
        checked_state_version=snapshot.state_version, checked_tick=snapshot.now_tick,
        checked_active_plan_version=snapshot.active_plan_version, policy_version=policy.policy_version,
        passed=not violations, violations=sorted(set(violations)))


def check_phase_start(snapshot: PlanningSnapshot, instruction: Instruction, policy: Policy) -> list[str]:
    """Just-in-time checks use actual predecessor completion, never prediction."""
    if instruction.action == "WAIT":
        return [] if instruction.reason_codes and instruction.recheck_triggers else ["UNEXPLAINED_WAIT"]
    errors = []
    jobs = {j.job_id: j for j in snapshot.jobs}
    robots = {r.robot_id: r for r in snapshot.robots}
    zones = {z.zone_id: z for z in snapshot.zones}
    job, robot = jobs.get(instruction.job_id), robots.get(instruction.robot_id)
    if job is None or robot is None:
        return ["UNKNOWN_ENTITY"]
    registered = next((i for i in snapshot.current_instructions if i.instruction_id == instruction.instruction_id), None)
    if registered is None or registered.status != "PENDING" or registered.model_dump() != instruction.model_dump():
        errors.append("INACTIVE_INSTRUCTION")
    if instruction.status != "PENDING" or instruction.not_before_tick > snapshot.now_tick:
        errors.append("NOT_DUE")
    if job.material_ready != "TRUE" or robot.availability != "AVAILABLE" or zones[job.zone_id].availability != "AVAILABLE":
        errors.append("READINESS_OR_AVAILABILITY")
    if job.job_id not in robot.capable_job_ids or job.zone_id not in robot.allowed_zone_ids:
        errors.append("CAPABILITY")
    if _restrictions_for(snapshot, job, robot.robot_id):
        errors.append("ACTIVE_RESTRICTION")
    if any(jobs[p].lifecycle != "COMPLETED" or jobs[p].actual_weld_end is None
            or jobs[p].actual_weld_end > snapshot.now_tick for p in job.predecessor_ids):
        errors.append("PREDECESSOR_NOT_ACTUALLY_COMPLETED")
    if robot.phase_execution_id is not None or robot.phase in ("MOVING", "SETTING_UP", "WELDING"):
        errors.append("ROBOT_BUSY")
    p = next((p for p in snapshot.execution_progress if p.job_id == job.job_id), None)
    if instruction.action == "MOVE":
        if job.lifecycle != "NOT_STARTED" or robot.phase != "IDLE":
            errors.append("INVALID_MOVE_STATE")
        if instruction.from_zone_id != robot.location_zone_id or instruction.to_zone_id != job.zone_id:
            errors.append("MOVE_POSITION")
        travel = next((t.duration_ticks for t in snapshot.travel_times
            if (t.from_zone_id, t.to_zone_id) == (robot.location_zone_id, job.zone_id)), None)
        expected_duration = travel
    else:
        if job.lifecycle != "COMMITTED" or p is None or p.robot_id != robot.robot_id or p.attempt_id != instruction.attempt_id:
            errors.append("INVALID_CONTINUATION_STATE")
        if robot.location_zone_id != job.zone_id:
            errors.append("WRONG_POSITION")
        if instruction.action == "SETUP":
            if p is None or p.setup_valid or not any(phase.phase == "MOVE" for phase in p.completed_phases):
                errors.append("MOVE_PREFIX_MISSING_OR_SETUP_REPEATED")
            expected_duration = job.setup_ticks
        else:
            if p is None or not p.setup_valid or not any(phase.phase == "SETUP" for phase in p.completed_phases):
                errors.append("SETUP_NOT_COMPLETED")
            expected_duration = job.duration_ticks
        for other in snapshot.robots:
            if other.robot_id != robot.robot_id and other.phase in ("SETTING_UP", "WELDING") and other.phase_job_id in jobs:
                if jobs[other.phase_job_id].zone_id == job.zone_id:
                    errors.append("ZONE_BUSY")
        # A finished SETUP keeps the zone until its same-attempt WELD starts.
        # During that boundary the robot has no current execution, so checking
        # robot.phase alone would let another robot steal the reservation.
        for execution_id in zones[job.zone_id].active_execution_ids:
            own_completed_setup = (
                instruction.action == "WELD" and p is not None and p.setup_valid
                and p.job_id == job.job_id and p.robot_id == robot.robot_id
                and p.attempt_id == instruction.attempt_id
                and any(record.execution_id == execution_id and record.phase == "SETUP"
                        and record.end_tick <= snapshot.now_tick for record in p.completed_phases)
            )
            if not own_completed_setup:
                errors.append("ZONE_BUSY")
    if expected_duration is None or instruction.expected_end_tick != snapshot.now_tick + expected_duration:
        errors.append("PHASE_DURATION_OR_LATE_START")
    return sorted(set(errors))
