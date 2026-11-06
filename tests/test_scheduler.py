from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

from shipyard import scheduler
from shipyard.checker import check_plan
from shipyard.constraints import build_constraints
from shipyard.contracts import ExecutionProgress, PhaseRecord, Restriction, Target, digest


def _hold(snapshot):
    snapshot.active_restrictions = [Restriction(restriction_id="hold", incident_id="inc",
        kind="FITUP_HOLD", target=Target(scope="JOB", job_id="W17"),
        opened_event_id="ev", opened_evidence_ids=[], opened_tick=0,
        rule_id="rules-v1", release_rule_id="repair-recheck")]


def test_fitup_replanning_is_feasible_and_preserves_w18(snapshot, policy):
    _hold(snapshot)
    plan = scheduler.solve(snapshot, build_constraints(snapshot, policy), policy)
    assert plan.solver_result.status == "OPTIMAL"
    assert plan.solver_result.proven_stages == 4
    assert {a.job_id for a in plan.assignments} == {"W19", "W21"}
    assert plan.fixed_segments[0].start_tick == -2
    assert plan.fixed_segments[0].end_tick == 2
    assert check_plan(snapshot, plan, policy).passed
    assert any(a.move_start == 0 for a in plan.assignments)
    assert all(stage["best_bound"] is not None for stage in plan.solver_result.stages)


def test_all_eligible_jobs_are_required_even_when_every_job_is_late(snapshot, policy):
    for job in snapshot.jobs:
        job.due_tick = 0
    plan = scheduler.solve(snapshot, build_constraints(snapshot, policy), policy)
    assert set(plan.required_job_ids) == {"W17", "W18", "W19", "W20", "W21"}
    assert len(plan.assignments) == 4
    assert check_plan(snapshot, plan, policy).passed
    assignments = {a.job_id: a for a in plan.assignments}
    assert assignments["W20"].move_start >= assignments["W17"].weld_end


def test_no_eligible_work_is_not_infeasibility(snapshot, policy):
    _hold(snapshot)
    for job in snapshot.jobs:
        if job.job_id in ("W19", "W21"):
            job.material_ready = "FALSE"
    plan = scheduler.solve(snapshot, build_constraints(snapshot, policy), policy)
    assert plan.solver_result.status == "NO_ELIGIBLE_WORK"
    assert plan.assignments == []
    assert check_plan(snapshot, plan, policy).passed


@pytest.mark.parametrize("phase,now", [("MOVE", 0), ("SETUP", 1), (None, 2)])
def test_continuation_does_not_repeat_prefix_or_overlap_itself(snapshot, policy, phase, now):
    snapshot.now_tick = now
    job = next(j for j in snapshot.jobs if j.job_id == "W19")
    job.lifecycle = "COMMITTED"
    job.assigned_robot_id = "R1"
    move = PhaseRecord(phase="MOVE", execution_id="move-exec", start_tick=0,
        end_tick=1, from_zone_id="A", to_zone_id="C")
    setup = PhaseRecord(phase="SETUP", execution_id="setup-exec", start_tick=1,
        end_tick=2, from_zone_id="C", to_zone_id="C")
    p = ExecutionProgress(job_id="W19", attempt_id="attempt-W19", robot_id="R1",
        completed_phases=[] if phase == "MOVE" else [move] if phase == "SETUP" else [move, setup],
        current_phase=move if phase == "MOVE" else setup if phase == "SETUP" else None,
        next_phase="SETUP" if phase == "MOVE" else "WELD", setup_valid=phase is None)
    snapshot.execution_progress.append(p)
    robot = next(r for r in snapshot.robots if r.robot_id == "R1")
    robot.phase = "MOVING" if phase == "MOVE" else "SETTING_UP" if phase == "SETUP" else "WAITING_FOR_ZONE"
    robot.location_zone_id = "A" if phase == "MOVE" else "C"
    robot.phase_job_id = "W19"
    robot.phase_execution_id = p.current_phase.execution_id if p.current_phase else None
    robot.phase_start_tick = p.current_phase.start_tick if p.current_phase else None
    robot.phase_end_tick = p.current_phase.end_tick if p.current_phase else None
    robot.destination_zone_id = "C" if phase == "MOVE" else None
    plan = scheduler.solve(snapshot, build_constraints(snapshot, policy), policy)
    assert plan.solver_result.status == "OPTIMAL"
    continuation = next(a for a in plan.assignments if a.job_id == "W19")
    assert continuation.mode == "CONTINUATION"
    assert continuation.attempt_id == "attempt-W19"
    assert continuation.robot_id == "R1"
    assert continuation.move_start is None
    assert continuation.move_end is None
    assert continuation.weld_start == 2
    assert continuation.weld_end == 5
    if phase != "MOVE":
        assert continuation.setup_start is None
    assert check_plan(snapshot, plan, policy).passed


def test_real_infeasibility_and_invalid_model_are_distinct(snapshot, policy):
    constraints = build_constraints(snapshot, policy)
    constraints.horizon_end = 1
    constraints.constraint_hash = digest(constraints.model_dump(mode="json", exclude={"constraint_hash"}))
    assert scheduler.solve(snapshot, constraints, policy).solver_result.status == "INFEASIBLE"
    constraints.constraint_hash = "wrong"
    assert scheduler.solve(snapshot, constraints, policy).solver_result.status == "MODEL_INVALID"


@pytest.mark.parametrize("keep_first", [False, True])
def test_unknown_preserves_an_incumbent_from_an_earlier_objective(snapshot, policy, monkeypatch, keep_first):
    real_run = scheduler._run_solver
    calls = 0

    def forced_unknown(model, policy, remaining):
        nonlocal calls
        calls += 1
        if keep_first and calls == 1:
            return real_run(model, policy, remaining)
        fake = SimpleNamespace(status_name=lambda status: "UNKNOWN", best_objective_bound=0.0, wall_time=0.0)
        return fake, cp_model.UNKNOWN

    monkeypatch.setattr(scheduler, "_run_solver", forced_unknown)
    plan = scheduler.solve(snapshot, build_constraints(snapshot, policy), policy)
    assert plan.solver_result.status == ("FEASIBLE_NOT_PROVEN" if keep_first else "NO_SOLUTION_WITHIN_LIMIT")
    assert bool(plan.assignments) is keep_first
    assert plan.solver_result.proven_stages == (1 if keep_first else 0)
    if keep_first:
        assert check_plan(snapshot, plan, policy).passed
