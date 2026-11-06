from pathlib import Path
import pytest
from shipyard.contracts import InitialData, EvidenceData, Policy, PlanningSnapshot, parse_json

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def initial():
    return InitialData.model_validate(parse_json((ROOT/'data/dev/D01/initial.json').read_text()))

@pytest.fixture
def policy():
    return Policy()

@pytest.fixture
def snapshot(initial):
    evidence = EvidenceData.model_validate(parse_json((ROOT/'data/dev/D01/evidence.json').read_text()))
    return PlanningSnapshot(run_id='test-run',now_tick=initial.now_tick,jobs=initial.jobs,robots=initial.robots,zones=initial.zones,travel_times=initial.travel_times,baseline_plan=initial.baseline_plan,execution_progress=initial.execution_progress,sources=evidence.sources,evidence=evidence.evidence)
