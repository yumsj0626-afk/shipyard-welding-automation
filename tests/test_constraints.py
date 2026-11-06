import pytest

from shipyard.constraints import build_constraints
from shipyard.contracts import Restriction, Target


def hold(job="W17", kind="FITUP_HOLD", robot=None):
    return Restriction(restriction_id="hold", incident_id="incident", kind=kind,
        target=Target(scope="JOB_ROBOT" if robot else "JOB", job_id=job, robot_id=robot),
        opened_event_id="event", opened_evidence_ids=["ev"], opened_tick=0,
        rule_id="rules-v1", release_rule_id="repair-and-recheck")


def test_fitup_is_global_and_predecessor_closure_preserves_unrelated_work(snapshot, policy):
    snapshot.active_restrictions = [hold()]
    result = build_constraints(snapshot, policy)
    assert result.required_job_ids == ["W18", "W19", "W21"]
    assert {d.job_id for d in result.deferred_jobs} == {"W17", "W20"}
    assert next(d for d in result.deferred_jobs if d.job_id == "W20").reason_codes == ["PREDECESSOR_DEFERRED"]
    assert [f.model_dump() for f in result.fixed_executions] == [{
        "execution_id": "exec:initial-W18", "attempt_id": "attempt:initial-W18",
        "job_id": "W18", "robot_id": "R2", "phase": "WELD", "zone_id": "B",
        "start_tick": -2, "end_tick": 2,
    }]
    assert result.immediate_start_pairs == [["W19", "R1"]]


def test_pair_ban_does_not_ban_job_or_robot(snapshot, policy):
    snapshot.active_restrictions = [hold(kind="PAIR_BAN", robot="R1")]
    result = build_constraints(snapshot, policy)
    assert result.allowed_assignments["W17"] == ["R2"]
    assert result.allowed_assignments["W19"] == ["R1", "R2"]
    assert "W20" in result.required_job_ids


def test_no_alternatives_keeps_running_weld_and_explains_each_deferral(snapshot, policy):
    snapshot.active_restrictions = [hold()]
    for job in snapshot.jobs:
        if job.job_id in ("W19", "W21"):
            job.material_ready = "FALSE"
    result = build_constraints(snapshot, policy)
    assert result.required_job_ids == ["W18"]
    assert len(result.deferred_jobs) == 4
    assert all(d.reason_codes and d.recheck_triggers for d in result.deferred_jobs)
    assert result.immediate_start_pairs == []


def test_missing_travel_is_not_silently_zero(snapshot, policy):
    snapshot.travel_times.pop()
    with pytest.raises(ValueError, match="travel"):
        build_constraints(snapshot, policy)


def test_cyclic_precedence_is_rejected(snapshot, policy):
    next(j for j in snapshot.jobs if j.job_id == "W17").predecessor_ids = ["W20"]
    with pytest.raises(ValueError, match="cycle"):
        build_constraints(snapshot, policy)
