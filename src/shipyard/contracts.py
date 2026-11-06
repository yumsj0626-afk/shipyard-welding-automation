"""Strict public DTOs shared across independent implementation boundaries."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Tick = Annotated[int, Field(ge=0)]
Phase = Literal["MOVE", "SETUP", "WELD"]
Availability = Literal["AVAILABLE", "UNAVAILABLE", "UNKNOWN"]
ObservationKind = Literal["FITUP_RESULT", "REPAIR_COMPLETED", "RECHECK_RESULT", "PAIR_REACHABILITY", "MATERIAL_READY", "ROBOT_AVAILABILITY", "ZONE_AVAILABILITY", "IDENTITY_MATCH"]


def new_id(prefix: str) -> str:
    return f"{prefix}:{uuid4()}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parse_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


class DTO(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_assignment=True)


class Provenance(DTO):
    basis: Literal["CONFIRMED_SOURCE", "INFERRED", "DEV_ASSUMPTION"]
    source_ref: str
    section_or_record: str
    rationale: str
    review_status: str


class Target(DTO):
    scope: Literal["JOB", "JOB_ROBOT", "ROBOT", "ZONE", "UNRESOLVED"]
    job_id: ID | None = None
    robot_id: ID | None = None
    zone_id: ID | None = None

    @model_validator(mode="after")
    def scope_fields(self):
        expected = {"JOB": {"job_id"}, "JOB_ROBOT": {"job_id", "robot_id"}, "ROBOT": {"robot_id"}, "ZONE": {"zone_id"}, "UNRESOLVED": set()}[self.scope]
        actual = {x for x in ("job_id", "robot_id", "zone_id") if getattr(self, x) is not None}
        if actual != expected:
            raise ValueError("target fields do not match scope")
        return self


class Job(DTO):
    job_id: ID
    zone_id: ID
    duration_ticks: Annotated[int, Field(gt=0)]
    setup_ticks: Tick
    due_tick: Tick
    weight: Annotated[int, Field(gt=0)]
    predecessor_ids: list[ID]
    material_ready: Literal["TRUE", "FALSE", "UNKNOWN"]
    lifecycle: Literal["NOT_STARTED", "COMMITTED", "RUNNING", "COMPLETED"]
    assigned_robot_id: ID | None = None
    actual_weld_start: int | None = None
    actual_weld_end: Tick | None = None
    active_execution_id: ID | None = None
    fact_refs: dict[str, list[ID]] = Field(default_factory=dict)


class Robot(DTO):
    robot_id: ID
    capable_job_ids: list[ID]
    allowed_zone_ids: list[ID]
    availability: Availability
    location_zone_id: ID
    phase: Literal["IDLE", "MOVING", "WAITING_FOR_ZONE", "SETTING_UP", "WELDING"]
    phase_job_id: ID | None = None
    phase_execution_id: ID | None = None
    phase_start_tick: int | None = None
    phase_end_tick: Tick | None = None
    destination_zone_id: ID | None = None
    fact_refs: dict[str, list[ID]] = Field(default_factory=dict)


class Zone(DTO):
    zone_id: ID
    capacity: Literal[1] = 1
    availability: Availability
    active_execution_ids: list[ID] = Field(default_factory=list)
    fact_refs: dict[str, list[ID]] = Field(default_factory=dict)


class TravelTime(DTO):
    from_zone_id: ID
    to_zone_id: ID
    duration_ticks: Tick
    provenance: Provenance


class Source(DTO):
    source_id: ID
    source_kind: Literal["DOCUMENT", "EXTERNAL_INSPECTION", "SIMULATED_EXTERNAL", "EXECUTION_OBSERVER"]
    allowed_observation_kinds: list[str]
    allowed_target_scopes: list[str]
    provenance: Provenance


class Evidence(DTO):
    evidence_id: ID
    incident_id: ID | None = None
    target: Target
    observation_kind: ObservationKind
    value: Literal["PASS", "FAIL", "UNKNOWN", "TRUE", "FALSE", "AVAILABLE", "UNAVAILABLE"]
    source_id: ID
    observed_tick: Tick
    available_tick: Tick
    revision: Annotated[int, Field(ge=1)]
    supersedes_evidence_id: ID | None = None
    restriction_id: ID | None = None
    related_evidence_ids: list[ID] = Field(default_factory=list)
    provenance: Provenance

    @model_validator(mode="after")
    def time_order(self):
        if self.observed_tick > self.available_tick:
            raise ValueError("observation after availability")
        return self


class EventEnvelope(DTO):
    schema_version: Literal["1.0"] = "1.0"
    run_id: ID
    event_id: ID
    incident_id: ID
    source_id: ID
    kind: Literal["FITUP_FAILED", "PAIR_UNREACHABLE", "PAIR_REACHABILITY_CHANGED", "REPAIR_REPORTED", "RECHECK_REPORTED", "MATERIAL_READINESS_CHANGED", "ROBOT_AVAILABILITY_CHANGED", "ZONE_AVAILABILITY_CHANGED", "IDENTITY_UNCERTAIN"]
    target: Target
    observed_tick: Tick
    received_tick: Tick
    observed_state_version: Tick | None = None
    evidence_ids: list[ID] = Field(default_factory=list)
    restriction_ids: list[ID] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)
    raw_report: str | None = None
    supersedes_event_id: ID | None = None

    @model_validator(mode="after")
    def time_order(self):
        if self.observed_tick > self.received_tick:
            raise ValueError("future event")
        return self


class Delivery(DTO):
    delivery_tick: Tick
    event: EventEnvelope
    published_evidence: list[Evidence] = Field(default_factory=list)


class Restriction(DTO):
    restriction_id: ID
    incident_id: ID
    kind: Literal["FITUP_HOLD", "PAIR_BAN", "ROBOT_BAN", "ZONE_BAN", "EVIDENCE_PENDING"]
    target: Target
    status: Literal["ACTIVE", "RELEASED"] = "ACTIVE"
    opened_event_id: ID
    opened_evidence_ids: list[ID]
    opened_tick: Tick
    rule_id: str
    release_rule_id: str
    released_event_id: ID | None = None
    released_evidence_ids: list[ID] = Field(default_factory=list)
    released_tick: Tick | None = None


class PhaseRecord(DTO):
    phase: Phase
    execution_id: ID
    start_tick: int
    end_tick: Tick
    from_zone_id: ID
    to_zone_id: ID


class ExecutionProgress(DTO):
    job_id: ID
    attempt_id: ID
    robot_id: ID
    completed_phases: list[PhaseRecord] = Field(default_factory=list)
    current_phase: PhaseRecord | None = None
    next_phase: Phase | None = None
    setup_valid: bool = False


class FixedSegment(DTO):
    execution_id: ID
    attempt_id: ID
    job_id: ID
    robot_id: ID
    phase: Phase
    zone_id: ID | None
    start_tick: int
    end_tick: Tick


class BaselineAssignment(DTO):
    job_id: ID
    robot_id: ID
    weld_start: Tick | None = None


class BaselinePlan(DTO):
    plan_id: ID
    assignments: list[BaselineAssignment]


class Instruction(DTO):
    instruction_id: ID
    plan_id: ID
    accepted_state_version: Tick
    accepted_plan_version: Tick = 0
    job_id: ID | None = None
    robot_id: ID
    action: Literal["MOVE", "SETUP", "WELD", "WAIT"]
    not_before_tick: Tick
    expected_end_tick: Tick | None = None
    precondition_refs: list[str] = Field(default_factory=list)
    status: Literal["PENDING", "RUNNING", "COMPLETED", "CANCELLED"] = "PENDING"
    superseded_by: ID | None = None
    reason_codes: list[str] = Field(default_factory=list)
    recheck_triggers: list[str] = Field(default_factory=list)
    execution_id: ID | None = None
    attempt_id: ID | None = None
    from_zone_id: ID | None = None
    to_zone_id: ID | None = None


class Observation(DTO):
    observation_id: ID
    execution_id: ID
    instruction_id: ID
    job_id: ID
    robot_id: ID
    phase: Phase
    kind: Literal["PHASE_STARTED", "PHASE_ENDED"]
    tick: int
    source_id: ID
    from_zone_id: ID
    to_zone_id: ID
    expected_state_version: Tick | None = None


class ArrivalBatch(DTO):
    tick: Tick
    observations: list[Observation] = Field(default_factory=list)
    deliveries: list[Delivery] = Field(default_factory=list)


class Policy(DTO):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["dev-v1"] = "dev-v1"
    rule_version: Literal["rules-v1"] = "rules-v1"
    solver_seconds: Annotated[float, Field(gt=0)] = 5.0
    num_search_workers: Literal[1] = 1
    random_seed: Literal[0] = 0
    stale_retries: Literal[2] = 2
    evidence_lookup_passes: Literal[1] = 1
    db_busy_ms: Literal[1000] = 1000
    work_conserving: Literal[True] = True


class PlanningSnapshot(DTO):
    schema_version: Literal["1.0"] = "1.0"
    run_id: ID
    state_version: Tick = 0
    active_plan_version: Tick = 0
    now_tick: Tick
    jobs: list[Job]
    robots: list[Robot]
    zones: list[Zone]
    travel_times: list[TravelTime]
    active_restrictions: list[Restriction] = Field(default_factory=list)
    visible_evidence_refs: list[ID] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    policy_version: str = "dev-v1"
    rule_version: str = "rules-v1"
    current_instructions: list[Instruction] = Field(default_factory=list)
    execution_progress: list[ExecutionProgress] = Field(default_factory=list)
    baseline_plan: BaselinePlan | None = None


class DeferredJob(DTO):
    job_id: ID
    reason_codes: list[str]
    restriction_ids: list[ID] = Field(default_factory=list)
    evidence_ids: list[ID] = Field(default_factory=list)
    recheck_triggers: list[str]


class SnapshotRef(DTO):
    run_id: ID
    state_version: Tick
    active_plan_version: Tick
    now_tick: Tick


class ConstraintSet(DTO):
    snapshot_ref: SnapshotRef
    required_job_ids: list[ID]
    deferred_jobs: list[DeferredJob]
    allowed_assignments: dict[str, list[str]]
    precedence_edges: list[list[str]]
    fixed_executions: list[FixedSegment]
    travel_setup: list[TravelTime]
    zone_capacities: dict[str, int]
    immediate_start_pairs: list[list[str]]
    horizon_end: Tick
    rule_version: str
    constraint_hash: str = ""


class Assignment(DTO):
    mode: Literal["NEW", "CONTINUATION"]
    attempt_id: ID
    resume_phase: Phase
    job_id: ID
    robot_id: ID
    zone_id: ID
    move_start: Tick | None = None
    move_end: Tick | None = None
    setup_start: Tick | None = None
    setup_end: Tick | None = None
    weld_start: Tick
    weld_end: Tick
    predecessor_ids: list[ID]
    source_position: ID
    is_fixed: bool = False
    execution_id: ID | None = None


SolverStatus = Literal["OPTIMAL", "FEASIBLE_NOT_PROVEN", "INFEASIBLE", "NO_SOLUTION_WITHIN_LIMIT", "MODEL_INVALID", "INTERNAL_ERROR", "NO_ELIGIBLE_WORK"]


class SolverResult(DTO):
    raw_status: str = ""
    stages: list[dict] = Field(default_factory=list)
    solver_version: str = "9.15.6755"
    termination_reason: str = ""
    status: SolverStatus
    objective_values: list[int] = Field(default_factory=list)
    proven_stages: int = 0
    elapsed_ms: float = 0.0
    reason_codes: list[str] = Field(default_factory=list)
    num_search_workers: int = 1
    random_seed: int = 0
    time_limit_seconds: float = 5.0


class Plan(DTO):
    schema_version: Literal["1.0"] = "1.0"
    run_id: ID
    plan_id: ID
    based_on_state_version: Tick
    based_on_tick: Tick
    based_on_active_plan_version: Tick
    policy_version: str
    rule_version: str
    constraint_hash: str
    required_job_ids: list[ID]
    deferred_jobs: list[DeferredJob]
    fixed_segments: list[FixedSegment]
    assignments: list[Assignment]
    solver_result: SolverResult
    created_at: str = Field(default_factory=utc_now)
    payload_hash: str = ""

    def seal(self):
        self.payload_hash = digest(self.model_dump(mode="json", exclude={"payload_hash"}))
        return self


class CheckResult(DTO):
    check_id: ID = Field(default_factory=lambda: new_id("check"))
    plan_id: ID
    plan_payload_hash: str
    checked_state_version: Tick
    checked_tick: Tick
    checked_active_plan_version: Tick
    policy_version: str
    checker_version: str = "checker-v1"
    passed: bool
    violations: list[str]


class InitialData(DTO):
    schema_version: Literal["1.0"]
    case_id: ID
    data_revision: str
    now_tick: Tick
    jobs: list[Job]
    robots: list[Robot]
    zones: list[Zone]
    travel_times: list[TravelTime]
    baseline_plan: BaselinePlan | None
    execution_progress: list[ExecutionProgress]
    provenance: Provenance

    @model_validator(mode="after")
    def graph_and_resources(self):
        jobs = {j.job_id: j for j in self.jobs}
        robots = {r.robot_id: r for r in self.robots}
        zones = {z.zone_id for z in self.zones}
        if len(jobs) != len(self.jobs) or len(robots) != len(self.robots) or len(zones) != len(self.zones):
            raise ValueError("duplicate entity ID")
        edges = {(t.from_zone_id, t.to_zone_id) for t in self.travel_times}
        if edges != {(a, b) for a in zones for b in zones} or len(edges) != len(self.travel_times):
            raise ValueError("travel matrix must be complete and unique")
        visited, visiting = set(), set()
        def visit(job_id):
            if job_id in visiting:
                raise ValueError("precedence cycle")
            if job_id not in jobs:
                raise ValueError("unknown predecessor")
            if job_id in visited:
                return
            visiting.add(job_id)
            for pred in jobs[job_id].predecessor_ids:
                visit(pred)
            visiting.remove(job_id)
            visited.add(job_id)
        for j in self.jobs:
            visit(j.job_id)
            if j.zone_id not in zones or (j.assigned_robot_id is not None and j.assigned_robot_id not in robots):
                raise ValueError("unknown job resource")
            if j.actual_weld_start is not None and j.actual_weld_start < 0 and (j.job_id != "W18" or j.actual_weld_start != -2):
                raise ValueError("unsupported negative history")
        for r in self.robots:
            if not set(r.capable_job_ids) <= jobs.keys() or not set(r.allowed_zone_ids) <= zones or r.location_zone_id not in zones:
                raise ValueError("unknown robot resource")
        progress_by_job = {p.job_id: p for p in self.execution_progress}
        if len(progress_by_job) != len(self.execution_progress):
            raise ValueError("duplicate execution progress")
        live_robots, execution_ids, occupied = set(), set(), {z: set() for z in zones}
        for progress in self.execution_progress:
            if progress.job_id not in jobs or progress.robot_id not in robots:
                raise ValueError("unknown progress job or robot")
            job, robot = jobs[progress.job_id], robots[progress.robot_id]
            if job.lifecycle == "NOT_STARTED" or job.assigned_robot_id != robot.robot_id:
                raise ValueError("progress does not match job assignment")
            phases = progress.completed_phases + ([progress.current_phase] if progress.current_phase else [])
            for phase in phases:
                if phase.execution_id in execution_ids or phase.end_tick < phase.start_tick:
                    raise ValueError("duplicate execution ID or invalid interval")
                execution_ids.add(phase.execution_id)
                if phase.from_zone_id not in zones or phase.to_zone_id != job.zone_id:
                    raise ValueError("phase zone does not match job")
                duration = (next(t.duration_ticks for t in self.travel_times if (t.from_zone_id,t.to_zone_id)==(phase.from_zone_id,phase.to_zone_id))
                    if phase.phase == "MOVE" else job.setup_ticks if phase.phase == "SETUP" else job.duration_ticks)
                if phase.end_tick - phase.start_tick != duration:
                    raise ValueError("phase interval does not match duration")
                if phase.start_tick < 0 and not (job.job_id == "W18" and phase.phase == "WELD" and phase.start_tick == -2):
                    raise ValueError("unsupported negative phase history")
            if any(p.end_tick > self.now_tick for p in progress.completed_phases):
                raise ValueError("completed phase is in the future")
            if job.lifecycle == "COMPLETED":
                if progress.current_phase is not None:
                    raise ValueError("completed job has a current phase")
                continue
            if progress.robot_id in live_robots:
                raise ValueError("robot has multiple active jobs")
            live_robots.add(progress.robot_id)
            phase = progress.current_phase
            if robot.phase_job_id != job.job_id:
                raise ValueError("robot phase job mismatch")
            if phase:
                expected_phase = {"MOVE":"MOVING", "SETUP":"SETTING_UP", "WELD":"WELDING"}[phase.phase]
                if not phase.start_tick <= self.now_tick < phase.end_tick:
                    raise ValueError("current phase is not active at initial tick")
                if (robot.phase != expected_phase or robot.phase_execution_id != phase.execution_id
                    or job.active_execution_id != phase.execution_id or robot.phase_start_tick != phase.start_tick
                    or robot.phase_end_tick != phase.end_tick or robot.destination_zone_id != phase.to_zone_id):
                    raise ValueError("job/robot/current phase disagreement")
                expected_location = phase.from_zone_id if phase.phase == "MOVE" else phase.to_zone_id
                if robot.location_zone_id != expected_location:
                    raise ValueError("current robot location disagreement")
                if phase.phase in {"SETUP", "WELD"}:
                    occupied[job.zone_id].add(phase.execution_id)
                if (phase.phase == "WELD") != (job.lifecycle == "RUNNING"):
                    raise ValueError("lifecycle and phase disagreement")
                if phase.phase == "WELD" and job.actual_weld_start != phase.start_tick:
                    raise ValueError("actual weld start disagreement")
            else:
                if job.lifecycle != "COMMITTED" or robot.phase != "WAITING_FOR_ZONE" or robot.phase_execution_id is not None or job.active_execution_id is not None:
                    raise ValueError("reserved preparation state disagreement")
                if progress.setup_valid:
                    setup = next((p for p in progress.completed_phases if p.phase == "SETUP"), None)
                    if not setup:
                        raise ValueError("setup validity lacks observation")
                    occupied[job.zone_id].add(setup.execution_id)
        for job in self.jobs:
            if job.lifecycle == "NOT_STARTED":
                if any(v is not None for v in (job.assigned_robot_id,job.active_execution_id,job.actual_weld_start,job.actual_weld_end)):
                    raise ValueError("not-started job contains execution facts")
            elif job.lifecycle in {"RUNNING", "COMMITTED"} and job.job_id not in progress_by_job:
                raise ValueError("active job lacks progress")
            if job.lifecycle == "COMPLETED":
                if job.actual_weld_start is None or job.actual_weld_end is None or job.actual_weld_end > self.now_tick or job.actual_weld_end-job.actual_weld_start != job.duration_ticks:
                    raise ValueError("invalid actual completion")
            elif job.actual_weld_end is not None:
                raise ValueError("unfinished job has actual completion")
        for robot in self.robots:
            if robot.phase == "IDLE":
                if any(v is not None for v in (robot.phase_job_id,robot.phase_execution_id,robot.phase_start_tick,robot.phase_end_tick,robot.destination_zone_id)):
                    raise ValueError("idle robot contains phase reservation")
            elif robot.robot_id not in live_robots:
                raise ValueError("busy robot lacks progress")
        for zone in self.zones:
            if set(zone.active_execution_ids) != occupied[zone.zone_id] or len(zone.active_execution_ids) > zone.capacity:
                raise ValueError("zone actual occupancy disagreement")
        return self


class EvidenceData(DTO):
    schema_version: Literal["1.0"]
    case_id: ID
    data_revision: str
    sources: list[Source]
    evidence: list[Evidence]
