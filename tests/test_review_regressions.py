"""Independent integration review regressions: actual state transitions and bad inputs."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from shipyard.cli import load_public_case
from shipyard.contracts import (
    ArrivalBatch, Delivery, Evidence, EventEnvelope, InitialData, Observation,
    Policy, Provenance, SolverResult, Target,
)
from shipyard.controller import Controller
from shipyard.repository import Repository
from shipyard.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mutation", ["truncated_running_weld", "unknown_progress_robot"])
def test_initial_state_rejects_inconsistent_execution_history(mutation):
    data = json.loads((ROOT / "data/dev/D01/initial.json").read_text())
    if mutation == "truncated_running_weld":
        data["execution_progress"][0]["current_phase"]["end_tick"] = 0
        data["robots"][1]["phase_end_tick"] = 0
    else:
        data["execution_progress"][0]["robot_id"] = "ghost"
    with pytest.raises(ValidationError):
        InitialData.model_validate(data)


def material_unready(job_id, tick):
    target = Target(scope="JOB", job_id=job_id)
    evidence = Evidence(evidence_id=f"review-evidence-{job_id}", incident_id=f"review-{job_id}",
        target=target, observation_kind="MATERIAL_READY", value="FALSE", source_id="sim-inspection",
        observed_tick=tick, available_tick=tick, revision=1,
        provenance=Provenance(basis="DEV_ASSUMPTION", source_ref="review regression",
            section_or_record="pre-weld cancellation", rationale="failure injection", review_status="TEST_ONLY"))
    event = EventEnvelope(run_id="review-run", event_id=f"review-event-{job_id}",
        incident_id=f"review-{job_id}", source_id="sim-inspection", kind="MATERIAL_READINESS_CHANGED",
        target=target, observed_tick=tick, received_tick=tick, evidence_ids=[evidence.evidence_id])
    return Delivery(delivery_tick=tick, event=event, published_evidence=[evidence])


def make_controller(tmp_path):
    repo = Repository(tmp_path / "run.sqlite3")
    repo.initialize(load_public_case(ROOT, "D01", "review-run"))
    controller = Controller(repo, Policy())
    simulator = Simulator.load(ROOT / "simulation/dev/D01/events.jsonl", "review-run")
    controller.handle_batch(simulator.due_events(0))
    return repo, controller, simulator


def test_move_cancellation_preserves_arrival_location_and_w18(tmp_path):
    repo, controller, simulator = make_controller(tmp_path)
    try:
        instruction = next(i for i in repo.read_snapshot().current_instructions
                           if i.action == "MOVE" and i.job_id == "W19" and i.not_before_tick == 0)
        observation = controller.authorize_phase_start(instruction.instruction_id)
        assert observation is not None
        simulator.register_completion(observation, instruction.expected_end_tick)
        # A new pre-weld restriction arrives while the MOVE is already in flight.
        controller.handle_batch(ArrivalBatch(tick=0, deliveries=[material_unready("W19", 0)]))
        during = repo.read_snapshot()
        assert next(p for p in during.execution_progress if p.job_id == "W19").current_phase.execution_id == observation.execution_id
        controller.handle_batch(simulator.due_events(1))
        after = repo.read_snapshot()
        robot = next(r for r in after.robots if r.robot_id == instruction.robot_id)
        assert robot.location_zone_id == "C"
        assert next(j for j in after.jobs if j.job_id == "W19").lifecycle == "NOT_STARTED"
        assert not any(i.job_id == "W19" and i.status == "PENDING" for i in after.current_instructions)
        w18 = next(p for p in after.execution_progress if p.job_id == "W18").current_phase
        assert (w18.start_tick, w18.end_tick, w18.phase) == (-2, 2, "WELD")
    finally:
        repo.close()


def test_solver_failure_does_not_authorize_newly_blocked_old_pending(tmp_path, monkeypatch):
    repo, controller, _ = make_controller(tmp_path)
    try:
        old = next(i for i in repo.read_snapshot().current_instructions if i.action == "MOVE" and i.job_id == "W19")
        from shipyard import scheduler
        original = scheduler.solve

        def no_incumbent(snapshot, constraints, policy):
            result = original(snapshot, constraints, policy)
            result.solver_result = SolverResult(status="NO_SOLUTION_WITHIN_LIMIT", reason_codes=["INJECTED_TIMEOUT"])
            return result.seal()

        monkeypatch.setattr(scheduler, "solve", no_incumbent)
        assert controller.handle_batch(ArrivalBatch(tick=0, deliveries=[material_unready("W19", 0)])) == "NO_SOLUTION_WITHIN_LIMIT"
        assert controller.authorize_phase_start(old.instruction_id) is None
        state = repo.read_snapshot()
        current = next(p for p in state.execution_progress if p.job_id == "W18").current_phase
        end = Observation(observation_id="review-w18-end", execution_id=current.execution_id,
            instruction_id="initial:W18", job_id="W18", robot_id="R2", phase="WELD",
            kind="PHASE_ENDED", tick=2, source_id="execution-observer", from_zone_id="B", to_zone_id="B")
        controller.handle_batch(ArrivalBatch(tick=2, observations=[end]))
        w18 = next(j for j in repo.read_snapshot().jobs if j.job_id == "W18")
        assert (w18.lifecycle, w18.actual_weld_end) == ("COMPLETED", 2)
    finally:
        repo.close()


def test_application_runs_without_optional_pytest_distribution(tmp_path, monkeypatch):
    """A runtime-only installation must not fail while creating its manifest."""
    import importlib.metadata
    from shipyard.cli import run_case

    original_version = importlib.metadata.version

    def runtime_only_version(distribution):
        if distribution == "pytest":
            raise importlib.metadata.PackageNotFoundError(distribution)
        return original_version(distribution)

    monkeypatch.setattr(importlib.metadata, "version", runtime_only_version)
    output = tmp_path / "without-dev-dependencies"
    assert run_case("D01", output)["termination"] == "COMPLETED"
    assert (output / "manifest.json").exists()
