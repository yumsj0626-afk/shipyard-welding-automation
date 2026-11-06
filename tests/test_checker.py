import ast
from pathlib import Path

import pytest

from shipyard.checker import check_phase_start, check_plan
from shipyard.constraints import build_constraints
from shipyard.contracts import ExecutionProgress, Instruction, PhaseRecord, Restriction, Target
from shipyard.scheduler import solve


@pytest.fixture
def plan(snapshot, policy):
    result = solve(snapshot, build_constraints(snapshot, policy), policy)
    assert result.solver_result.status == "OPTIMAL"
    assert check_plan(snapshot, result, policy).passed
    return result


@pytest.mark.parametrize("mutation,code", [
    ("weld", "WELD_DURATION"), ("travel", "TRAVEL_DURATION"),
    ("setup", "SETUP_DURATION_OR_WELD_GAP"), ("capability", "FORBIDDEN_ASSIGNMENT"),
    ("fixed", "FIXED_EXECUTION_CHANGED"), ("precedence", "PRECEDENCE"),
    ("overlap", "ROBOT_OVERLAP"), ("missing", "COMPLETION_PATH_SET"),
    ("idle", "UNJUSTIFIED_IDLE"),
])
def test_independent_checker_rejects_corrupted_plans(snapshot, plan, policy, mutation, code):
    assignments = {a.job_id: a for a in plan.assignments}
    a = assignments["W19"]
    if mutation == "weld":
        a.weld_end += 1
    elif mutation == "travel":
        a.move_end = a.move_start
    elif mutation == "setup":
        a.setup_end = a.setup_start
    elif mutation == "capability":
        assignments["W20"].robot_id = "R2"
    elif mutation == "fixed":
        plan.fixed_segments[0].end_tick += 1
    elif mutation == "precedence":
        assignments["W20"].move_start = 0
    elif mutation == "overlap":
        other = assignments["W21"]
        other.robot_id = a.robot_id
        other.move_start, other.move_end = a.move_start, a.move_end
        other.setup_start, other.setup_end = a.setup_start, a.setup_end
        other.weld_start, other.weld_end = a.weld_start, a.weld_end
    elif mutation == "missing":
        plan.assignments = [x for x in plan.assignments if x.job_id != "W19"]
    elif mutation == "idle":
        for item in plan.assignments:
            for name in ("move_start", "move_end", "setup_start", "setup_end", "weld_start", "weld_end"):
                setattr(item, name, getattr(item, name) + 10)
    plan.seal()
    result = check_plan(snapshot, plan, policy)
    assert not result.passed
    assert any(v.startswith(code) for v in result.violations), result.violations


def test_new_hold_invalidates_previously_valid_plan_without_using_constraint_builder(snapshot, plan, policy):
    snapshot.active_restrictions = [Restriction(restriction_id="hold", incident_id="inc",
        kind="FITUP_HOLD", target=Target(scope="JOB", job_id="W17"),
        opened_event_id="ev", opened_evidence_ids=[], opened_tick=0,
        rule_id="rules-v1", release_rule_id="repair-recheck")]
    result = check_plan(snapshot, plan, policy)
    assert not result.passed
    assert any(v.startswith("FORBIDDEN_ASSIGNMENT:W17") for v in result.violations)


def test_plan_checked_before_state_change_is_stale(snapshot, plan, policy):
    snapshot.state_version += 1
    assert "STALE_PLAN" in check_plan(snapshot, plan, policy).violations


def test_zone_overlap_is_rejected_even_when_robot_intervals_do_not_overlap(snapshot, policy):
    jobs = {j.job_id: j for j in snapshot.jobs}
    robots = {r.robot_id: r for r in snapshot.robots}
    jobs["W17"].zone_id = "C"
    robots["R1"].capable_job_ids = ["W17"]
    robots["R2"].capable_job_ids = ["W18", "W19"]
    plan = solve(snapshot, build_constraints(snapshot, policy), policy)
    assert check_plan(snapshot, plan, policy).passed
    assignments = {a.job_id: a for a in plan.assignments}
    assert assignments["W17"].robot_id == "R1"
    assert assignments["W19"].robot_id == "R2"
    for job_id, move_start in [("W17", 0), ("W19", 2)]:
        a = assignments[job_id]
        a.move_start, a.move_end = move_start, move_start + 1
        a.setup_start, a.setup_end = 10, 11
        a.weld_start, a.weld_end = 11, 11 + jobs[job_id].duration_ticks
    plan.seal()
    result = check_plan(snapshot, plan, policy)
    assert any(v.startswith("ZONE_OVERLAP") for v in result.violations)
    assert not any(v.startswith("ROBOT_OVERLAP") for v in result.violations)


def test_finished_setup_retains_zone_for_own_weld_only(snapshot, policy):
    snapshot.now_tick = 2
    jobs = {j.job_id: j for j in snapshot.jobs}
    robots = {r.robot_id: r for r in snapshot.robots}
    zones = {z.zone_id: z for z in snapshot.zones}
    jobs["W18"].lifecycle = "COMPLETED"
    jobs["W18"].actual_weld_end = 2
    snapshot.execution_progress = []
    for robot_id, job_id in [("R1", "W17"), ("R2", "W19")]:
        robot, job = robots[robot_id], jobs[job_id]
        job.zone_id, job.lifecycle, job.assigned_robot_id = "A", "COMMITTED", robot_id
        robot.phase, robot.phase_job_id, robot.location_zone_id = "WAITING_FOR_ZONE", job_id, "A"
        robot.phase_execution_id = robot.phase_start_tick = robot.phase_end_tick = None
        move = PhaseRecord(phase="MOVE", execution_id=f"move-{robot_id}", start_tick=1,
            end_tick=1 if robot_id == "R1" else 2, from_zone_id="A" if robot_id == "R1" else "B", to_zone_id="A")
        phases = [move]
        if robot_id == "R1":
            phases.append(PhaseRecord(phase="SETUP", execution_id="reserved-setup", start_tick=1,
                end_tick=2, from_zone_id="A", to_zone_id="A"))
        snapshot.execution_progress.append(ExecutionProgress(job_id=job_id, robot_id=robot_id,
            attempt_id=f"attempt-{robot_id}", completed_phases=phases, setup_valid=robot_id == "R1"))
    zones["A"].active_execution_ids = ["reserved-setup"]
    zones["B"].active_execution_ids = []
    other_setup = Instruction(instruction_id="other-setup", plan_id="plan", accepted_state_version=0,
        job_id="W19", robot_id="R2", action="SETUP", not_before_tick=2, expected_end_tick=3,
        attempt_id="attempt-R2", from_zone_id="A", to_zone_id="A")
    own_weld = Instruction(instruction_id="own-weld", plan_id="plan", accepted_state_version=0,
        job_id="W17", robot_id="R1", action="WELD", not_before_tick=2, expected_end_tick=6,
        attempt_id="attempt-R1", from_zone_id="A", to_zone_id="A")
    snapshot.current_instructions = [other_setup, own_weld]
    assert "ZONE_BUSY" in check_phase_start(snapshot, other_setup, policy)
    assert check_phase_start(snapshot, own_weld, policy) == []


def test_runtime_gate_requires_actual_predecessor_completion(snapshot, policy):
    instruction = Instruction(instruction_id="move-W20", plan_id="plan", accepted_state_version=0,
        job_id="W20", robot_id="R1", action="MOVE", not_before_tick=0,
        expected_end_tick=0, attempt_id="attempt-W20", from_zone_id="A", to_zone_id="A")
    snapshot.current_instructions.append(instruction)
    assert "PREDECESSOR_NOT_ACTUALLY_COMPLETED" in check_phase_start(snapshot, instruction, policy)
    predecessor = next(j for j in snapshot.jobs if j.job_id == "W17")
    predecessor.lifecycle = "COMPLETED"
    predecessor.actual_weld_start = 0
    predecessor.actual_weld_end = 0
    assert check_phase_start(snapshot, instruction, policy) == []


def test_execution_checker_does_not_import_model_builder_or_solver():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/shipyard/checker.py").read_text())
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert not any(any(word in module for word in ("constraints", "scheduler", "ortools")) for module in imports)
