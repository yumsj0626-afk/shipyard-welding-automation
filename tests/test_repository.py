from pathlib import Path

import pytest

from shipyard.contracts import CheckResult, Delivery, Instruction, Observation, Plan, SolverResult, parse_json
from shipyard.events import apply_deliveries
from shipyard.repository import IdentityConflictError, Repository, StaleStateError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repository(tmp_path, snapshot):
    repo = Repository(tmp_path / "run.sqlite3")
    repo.initialize(snapshot)
    yield repo
    repo.close()


def first_delivery(snapshot):
    line = (ROOT / "simulation/dev/D01/events.jsonl").read_text().splitlines()[0]
    delivery = Delivery.model_validate(parse_json(line))
    delivery.event.run_id = snapshot.run_id
    return delivery


def process(repo, values):
    before = repo.read_snapshot()
    fresh, prior = repo.filter_deliveries(values)
    updated, results = apply_deliveries(before, fresh)
    return repo.commit_event_batch(updated, values, prior + results, before.state_version)


def plan_and_check(snapshot):
    plan = Plan(run_id=snapshot.run_id, plan_id="plan:test", based_on_state_version=snapshot.state_version,
                based_on_tick=snapshot.now_tick, based_on_active_plan_version=snapshot.active_plan_version,
                policy_version=snapshot.policy_version, rule_version=snapshot.rule_version, constraint_hash="test",
                required_job_ids=[], deferred_jobs=[], fixed_segments=[], assignments=[],
                solver_result=SolverResult(status="NO_ELIGIBLE_WORK")).seal()
    check = CheckResult(plan_id=plan.plan_id, plan_payload_hash=plan.payload_hash,
                        checked_state_version=snapshot.state_version, checked_tick=snapshot.now_tick,
                        checked_active_plan_version=snapshot.active_plan_version, policy_version=snapshot.policy_version,
                        passed=True, violations=[])
    return plan, check


def test_snapshot_is_detached_and_audit_does_not_change_version(repository):
    snapshot = repository.read_snapshot()
    snapshot.now_tick = 12
    assert repository.read_snapshot().now_tick == 0
    repository.audit("QUERY", "FOUND", {"evidence_ids": []})
    assert repository.read_snapshot().state_version == 0
    assert repository.audit_records()[-1]["stage"] == "QUERY"


def test_compare_and_swap_rejects_second_writer_without_partial_log(repository):
    second = Repository(repository.path)
    try:
        old = second.read_snapshot()
        first = repository.read_snapshot()
        first.now_tick = 1
        repository.commit_snapshot(first, first.state_version, "CLOCK", "ADVANCED")
        count = len(repository.audit_records())
        old.now_tick = 2
        with pytest.raises(StaleStateError):
            second.commit_snapshot(old, old.state_version, "CLOCK", "ADVANCED")
        assert second.read_snapshot().now_tick == 1
        assert second.read_snapshot().state_version == 1
        assert len(repository.audit_records()) == count
    finally:
        second.close()


def test_same_event_is_noop_and_conflicting_payload_is_not_applied(repository):
    item = first_delivery(repository.read_snapshot())
    first = process(repository, [item])
    duplicate = process(repository, [item.model_copy(deep=True)])
    assert duplicate.state_version == first.state_version
    assert len(duplicate.active_restrictions) == 1
    assert repository.audit_records()[-1]["outcome"] == "DUPLICATE"
    conflict = item.model_copy(deep=True)
    conflict.event.raw_report = "different contents"
    final = process(repository, [conflict])
    assert final.state_version == first.state_version
    assert [r.kind for r in final.active_restrictions] == ["FITUP_HOLD"]
    assert any(r["outcome"] == "ID_CONFLICT" for r in repository.audit_records())


def test_received_tick_does_not_change_event_identity(repository):
    item = first_delivery(repository.read_snapshot())
    process(repository, [item])
    changed = repository.read_snapshot()
    changed.now_tick = 1
    repository.commit_snapshot(changed, changed.state_version, "CLOCK", "ADVANCED")
    item.delivery_tick = 1
    item.event.received_tick = 1
    fresh, prior = repository.filter_deliveries([item])
    assert not fresh and prior[0]["outcome"] == "DUPLICATE"


def test_conflicting_same_batch_event_is_not_partly_applied(repository):
    item = first_delivery(repository.read_snapshot())
    conflict = item.model_copy(deep=True)
    conflict.event.raw_report = "different"
    final = process(repository, [item, conflict])
    assert final.state_version == 0
    assert not final.active_restrictions and not final.evidence
    still_conflicted = process(repository, [item])
    assert still_conflicted.state_version == 0
    assert not still_conflicted.active_restrictions


def test_evidence_identity_failure_rolls_back_state_and_audit(repository):
    process(repository, [first_delivery(repository.read_snapshot())])
    old = repository.read_snapshot()
    proposed = old.model_copy(deep=True)
    proposed.now_tick = 9
    proposed.evidence[0].value = "PASS"
    count = len(repository.audit_records())
    with pytest.raises(IdentityConflictError):
        repository.commit_snapshot(proposed, old.state_version, "BAD_UPDATE")
    assert repository.read_snapshot() == old
    assert len(repository.audit_records()) == count


def test_plan_apply_is_atomic_and_rejects_check_then_state_change(repository):
    original = repository.read_snapshot()
    plan, check = plan_and_check(original)
    changed = original.model_copy(deep=True)
    changed.now_tick = 1
    repository.commit_snapshot(changed, original.state_version, "CLOCK", "ADVANCED")
    count = len(repository.audit_records())
    with pytest.raises(StaleStateError):
        repository.apply_plan(plan, check, [])
    assert repository.read_snapshot().active_plan_version == 0
    assert len(repository.audit_records()) == count


def test_plan_apply_sets_versions_and_does_not_change_running_job(repository):
    before = repository.read_snapshot()
    plan, check = plan_and_check(before)
    instruction = Instruction(instruction_id="wait:test", plan_id=plan.plan_id, accepted_state_version=0,
                              robot_id="R1", action="WAIT", not_before_tick=0,
                              reason_codes=["NO_ELIGIBLE_WORK"], recheck_triggers=["NEW_EVIDENCE"])
    after = repository.apply_plan(plan, check, [instruction])
    assert (after.state_version, after.active_plan_version) == (1, 1)
    assert after.current_instructions[-1].accepted_state_version == 1
    assert after.current_instructions[-1].accepted_plan_version == 1
    assert next(j for j in after.jobs if j.job_id == "W18") == next(j for j in before.jobs if j.job_id == "W18")


def test_tampered_plan_cannot_reuse_check(repository):
    plan, check = plan_and_check(repository.read_snapshot())
    plan.constraint_hash = "tampered"
    with pytest.raises(ValueError, match="payload changed"):
        repository.apply_plan(plan, check, [])
    assert repository.read_snapshot().state_version == 0


def test_observation_duplicate_is_checked_before_any_proposed_state_change(repository):
    original = repository.read_snapshot()
    observation = Observation(observation_id="obs:one", execution_id="exec:initial-W18", instruction_id="initial:W18",
                              job_id="W18", robot_id="R2", phase="WELD", kind="PHASE_ENDED", tick=2,
                              source_id="execution-observer", from_zone_id="B", to_zone_id="B")
    proposed = original.model_copy(deep=True)
    proposed.now_tick = 2
    stored = repository.commit_observation(proposed, observation, original.state_version)
    stale_proposal = original.model_copy(deep=True)
    stale_proposal.now_tick = 500
    repeated = repository.commit_observation(stale_proposal, observation, original.state_version)
    assert repeated == stored
    conflict = observation.model_copy(deep=True)
    conflict.tick = 3
    with pytest.raises(IdentityConflictError):
        repository.observation_receipt(conflict)


def test_event_trace_records_one_public_lookup_and_published_evidence(repository):
    item = first_delivery(repository.read_snapshot())
    process(repository, [item])
    process(repository, [item])
    queries = [r for r in repository.audit_records() if r["stage"] == "QUERY"]
    assert len(queries) == 1
    assert queries[0]["data"]["lookup_passes"] == 1
    assert queries[0]["data"]["query_kind"] == "PUBLIC_SNAPSHOT"
    event = next(r for r in repository.audit_records() if r["stage"] == "EVENT")
    assert event["data"]["published_evidence"][0]["evidence_id"] == "EV-fitup-W17"
