"""Deterministic translation from public state to scheduling requirements.

No solver expressions or user supplied code cross this boundary. A deferred
job remains a visible obligation with a reason; it is never silently dropped.
"""
from __future__ import annotations

from .contracts import (
    ConstraintSet, DeferredJob, FixedSegment, PlanningSnapshot, Policy,
    SnapshotRef, digest,
)


def build_constraints(snapshot: PlanningSnapshot, policy: Policy) -> ConstraintSet:
    jobs = {j.job_id: j for j in snapshot.jobs}
    robots = {r.robot_id: r for r in snapshot.robots}
    zones = {z.zone_id: z for z in snapshot.zones}
    progress = {p.job_id: p for p in snapshot.execution_progress}
    restrictions = [r for r in snapshot.active_restrictions if r.status == "ACTIVE"]
    fixed = []
    for p in snapshot.execution_progress:
        phase = p.current_phase
        if phase is not None:
            fixed.append(FixedSegment(
                execution_id=phase.execution_id, attempt_id=p.attempt_id,
                job_id=p.job_id, robot_id=p.robot_id, phase=phase.phase,
                zone_id=phase.to_zone_id if phase.phase != "MOVE" else None,
                start_tick=phase.start_tick, end_tick=phase.end_tick,
            ))
    fixed.sort(key=lambda f: (f.robot_id, f.start_tick, f.execution_id))
    fixed_weld = {f.job_id for f in fixed if f.phase == "WELD"}

    def blocking(job, robot_id=None):
        result = []
        for restriction in restrictions:
            t = restriction.target
            if (t.scope == "JOB" and t.job_id == job.job_id
                    or t.scope == "ZONE" and t.zone_id == job.zone_id
                    or robot_id is not None and t.scope == "ROBOT" and t.robot_id == robot_id
                    or robot_id is not None and t.scope == "JOB_ROBOT"
                    and t.job_id == job.job_id and t.robot_id == robot_id):
                result.append(restriction)
        return result

    allowed: dict[str, list[str]] = {}
    deferred: dict[str, DeferredJob] = {}
    required: set[str] = set()
    visiting, visited = set(), set()

    def visit(job_id):
        if job_id in visited:
            return
        if job_id in visiting:
            raise ValueError("precedence cycle")
        if job_id not in jobs:
            raise ValueError("unknown predecessor")
        visiting.add(job_id)
        job = jobs[job_id]
        for pred in job.predecessor_ids:
            visit(pred)
        visiting.remove(job_id)
        visited.add(job_id)
        if job.lifecycle == "COMPLETED":
            if job.actual_weld_end is None:
                raise ValueError("completed job lacks actual completion")
            return
        if job.lifecycle == "RUNNING":
            if job_id not in fixed_weld:
                raise ValueError("running job lacks fixed weld")
            required.add(job_id)
            allowed[job_id] = [job.assigned_robot_id]
            return
        reason_codes = []
        implicated = blocking(job)
        reason_codes.extend(r.kind for r in implicated)
        if job.material_ready != "TRUE":
            reason_codes.append("MATERIAL_NOT_READY" if job.material_ready == "FALSE" else "MATERIAL_UNKNOWN")
        if job.zone_id not in zones or zones[job.zone_id].availability != "AVAILABLE":
            reason_codes.append("ZONE_UNAVAILABLE")
        options = []
        for robot_id, robot in sorted(robots.items()):
            if (job_id in robot.capable_job_ids and job.zone_id in robot.allowed_zone_ids
                    and robot.availability == "AVAILABLE" and not blocking(job, robot_id)):
                options.append(robot_id)
        if job.lifecycle == "COMMITTED":
            if job_id not in progress:
                raise ValueError("committed job lacks execution progress")
            options = [r for r in options if r == progress[job_id].robot_id]
        if not options:
            reason_codes.append("NO_AVAILABLE_ROBOT")
            implicated.extend(r for robot_id in robots for r in blocking(job, robot_id))
        if any(jobs[p].lifecycle != "COMPLETED" and p not in required for p in job.predecessor_ids):
            reason_codes.append("PREDECESSOR_DEFERRED")
        if reason_codes:
            unique = {r.restriction_id: r for r in implicated}
            deferred[job_id] = DeferredJob(
                job_id=job_id, reason_codes=sorted(set(reason_codes)),
                restriction_ids=sorted(unique),
                evidence_ids=sorted({e for r in unique.values() for e in r.opened_evidence_ids}),
                recheck_triggers=["NEW_VALID_EVIDENCE", "RESOURCE_OR_PHASE_COMPLETED", "EXTERNAL_EVENT"],
            )
        else:
            required.add(job_id)
            allowed[job_id] = options

    for job_id in sorted(jobs):
        visit(job_id)

    occupied_zones = {f.zone_id for f in fixed if f.zone_id and f.start_tick <= snapshot.now_tick < f.end_tick}
    immediate = []
    for job_id in sorted(required - fixed_weld):
        job = jobs[job_id]
        if job.lifecycle != "NOT_STARTED" or job.zone_id in occupied_zones:
            continue
        if any(jobs[p].lifecycle != "COMPLETED" for p in job.predecessor_ids):
            continue
        for robot_id in allowed[job_id]:
            if robots[robot_id].phase == "IDLE":
                immediate.append([job_id, robot_id])
    travel_keys = {(t.from_zone_id, t.to_zone_id) for t in snapshot.travel_times}
    if travel_keys != {(a, b) for a in zones for b in zones}:
        raise ValueError("missing travel matrix entry")
    max_travel = max((t.duration_ticks for t in snapshot.travel_times), default=0)
    remaining = max([0] + [f.end_tick - snapshot.now_tick for f in fixed])
    horizon = snapshot.now_tick + remaining + sum(
        j.duration_ticks + j.setup_ticks + max_travel
        for j in snapshot.jobs if j.lifecycle != "COMPLETED"
    )
    result = ConstraintSet(
        snapshot_ref=SnapshotRef(run_id=snapshot.run_id, state_version=snapshot.state_version,
            active_plan_version=snapshot.active_plan_version, now_tick=snapshot.now_tick),
        required_job_ids=sorted(required), deferred_jobs=[deferred[k] for k in sorted(deferred)],
        allowed_assignments=allowed,
        precedence_edges=[[p, j.job_id] for j in sorted(snapshot.jobs, key=lambda j: j.job_id) for p in sorted(j.predecessor_ids)],
        fixed_executions=fixed, travel_setup=snapshot.travel_times,
        zone_capacities={z.zone_id: z.capacity for z in snapshot.zones},
        immediate_start_pairs=immediate, horizon_end=max(horizon, snapshot.now_tick + 1),
        rule_version=policy.rule_version,
    )
    result.constraint_hash = digest(result.model_dump(mode="json", exclude={"constraint_hash"}))
    return result
