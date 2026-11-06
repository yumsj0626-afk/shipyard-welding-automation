from pathlib import Path

from shipyard.contracts import Delivery, parse_json
from shipyard.events import apply_deliveries

ROOT = Path(__file__).resolve().parents[1]


def deliveries(snapshot, case="D01"):
    result = [Delivery.model_validate(parse_json(line)) for line in
              (ROOT / f"simulation/dev/{case}/events.jsonl").read_text().splitlines()]
    for item in result:
        item.event.run_id = snapshot.run_id
    return result


def test_fitup_hold_is_job_wide_without_mutating_input(snapshot):
    updated, results = apply_deliveries(snapshot, deliveries(snapshot)[:1])
    assert not snapshot.active_restrictions and not snapshot.evidence
    assert updated.state_version == snapshot.state_version
    assert [(r.kind, r.target.scope, r.target.job_id) for r in updated.active_restrictions] == [
        ("FITUP_HOLD", "JOB", "W17")]
    assert next(j for j in updated.jobs if j.job_id == "W18").lifecycle == "RUNNING"
    assert results[0]["outcome"] == "APPLIED"


def test_pair_ban_does_not_become_job_or_robot_ban(snapshot):
    updated, _ = apply_deliveries(snapshot, deliveries(snapshot, "D03"))
    restriction, = updated.active_restrictions
    assert (restriction.kind, restriction.target.scope, restriction.target.job_id, restriction.target.robot_id) == (
        "PAIR_BAN", "JOB_ROBOT", "W17", "R1")


def test_repair_alone_cannot_release_and_linked_recheck_can(snapshot):
    inputs = deliveries(snapshot)
    held, _ = apply_deliveries(snapshot, inputs[:1])
    held.now_tick = 6
    repaired, results = apply_deliveries(held, inputs[1:2])
    assert repaired.active_restrictions[0].kind == "FITUP_HOLD"
    assert results[0]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert results[0]["intervention_kind"] == "ADDITIONAL_HUMAN_REQUEST"
    recovered, results = apply_deliveries(repaired, inputs[2:])
    assert not recovered.active_restrictions
    assert next(j for j in recovered.jobs if j.job_id == "W17").lifecycle == "NOT_STARTED"
    assert results[0]["released_restriction_ids"] == ["restriction:E-fitup-W17:FITUP_HOLD"]


def test_recovery_releases_only_its_own_restriction(snapshot):
    inputs = deliveries(snapshot)
    held, _ = apply_deliveries(snapshot, inputs[:1] + deliveries(snapshot, "D03"))
    held.now_tick = 6
    recovered, results = apply_deliveries(held, inputs[1:])
    assert [r.kind for r in recovered.active_restrictions] == ["PAIR_BAN"]
    assert all(r["intervention_kind"] != "ADDITIONAL_HUMAN_REQUEST" for r in results)


def test_wrong_incident_or_recheck_before_repair_cannot_release(snapshot):
    for defect in ("incident", "time"):
        inputs = deliveries(snapshot)
        held, _ = apply_deliveries(snapshot, inputs[:1])
        held.now_tick = 6
        check = inputs[2]
        if defect == "incident":
            check.event.incident_id = "OLD-INCIDENT"
            check.published_evidence[0].incident_id = "OLD-INCIDENT"
        else:
            check.published_evidence[0].observed_tick = 5
            check.event.observed_tick = 5
        updated, _ = apply_deliveries(held, inputs[1:])
        assert any(r.kind == "FITUP_HOLD" for r in updated.active_restrictions)


def test_unknown_source_and_future_evidence_are_not_published(snapshot):
    for defect in ("source", "future"):
        item = deliveries(snapshot)[0]
        if defect == "source":
            item.published_evidence[0].source_id = "unregistered"
        else:
            item.published_evidence[0].available_tick = 10
        updated, results = apply_deliveries(snapshot, [item])
        assert not updated.evidence and not updated.active_restrictions
        assert results[0]["outcome"] == "INVALID_INPUT"


def test_conflicting_sources_do_not_depend_on_arrival_order(snapshot):
    first = deliveries(snapshot)[0]
    second = first.model_copy(deep=True)
    source = snapshot.sources[0].model_copy(deep=True)
    source.source_id = "second-inspection"
    snapshot.sources.append(source)
    second.event.event_id = "E-conflicting"
    second.event.source_id = source.source_id
    second.event.evidence_ids = ["EV-conflicting"]
    second.published_evidence[0].evidence_id = "EV-conflicting"
    second.published_evidence[0].source_id = source.source_id
    second.published_evidence[0].value = "PASS"
    for order in ([first, second], [second, first]):
        updated, results = apply_deliveries(snapshot, order)
        assert all(r["outcome"] == "EVIDENCE_CONFLICT" for r in results)
        assert [r.kind for r in updated.active_restrictions] == ["EVIDENCE_PENDING"]


def test_missing_reference_is_filled_from_current_public_evidence(snapshot):
    item = deliveries(snapshot)[0]
    snapshot.evidence = [item.published_evidence[0].model_copy(deep=True)]
    snapshot.visible_evidence_refs = [snapshot.evidence[0].evidence_id]
    item.published_evidence = []
    item.event.evidence_ids = ["not-in-store"]
    updated, results = apply_deliveries(snapshot, [item])
    assert results[0]["outcome"] == "APPLIED"
    assert results[0]["evidence_ids"] == ["EV-fitup-W17"]
    assert updated.active_restrictions[0].kind == "FITUP_HOLD"


def test_invalid_evidence_identity_is_quarantined(snapshot):
    first = deliveries(snapshot)[0]
    second = first.model_copy(deep=True)
    second.event.event_id = "E-second"
    second.published_evidence[0].value = "PASS"
    updated, results = apply_deliveries(snapshot, [first, second])
    assert not updated.evidence
    assert all(r["outcome"] == "ID_CONFLICT" for r in results)


def test_running_weld_is_not_stopped_by_out_of_scope_event(snapshot):
    item = deliveries(snapshot)[0]
    item.event.target.job_id = "W18"
    item.published_evidence[0].target.job_id = "W18"
    updated, results = apply_deliveries(snapshot, [item])
    assert results[0]["outcome"] == "OUT_OF_SCOPE_RUNNING"
    assert not updated.active_restrictions
    assert next(j for j in updated.jobs if j.job_id == "W18").lifecycle == "RUNNING"


def test_superseded_repair_true_cannot_release_with_old_link(snapshot):
    inputs = deliveries(snapshot)
    held, _ = apply_deliveries(snapshot, inputs[:1])
    held.now_tick = 6
    repaired, _ = apply_deliveries(held, inputs[1:2])
    correction = inputs[1].model_copy(deep=True)
    correction.event.event_id = "E-repair-correction"
    correction.event.evidence_ids = ["EV-repair-correction"]
    evidence = correction.published_evidence[0]
    evidence.evidence_id = "EV-repair-correction"
    evidence.revision = 2
    evidence.value = "FALSE"
    evidence.supersedes_evidence_id = "EV-repair-W17"
    corrected, _ = apply_deliveries(repaired, [correction])
    updated, results = apply_deliveries(corrected, inputs[2:])
    assert [r.kind for r in updated.active_restrictions] == ["FITUP_HOLD"]
    assert results[0]["outcome"] == "INSUFFICIENT_EVIDENCE"


def test_supersedes_cannot_replace_another_source_fact(snapshot):
    item = deliveries(snapshot)[0]
    held, _ = apply_deliveries(snapshot, [item])
    second_source = held.sources[0].model_copy(deep=True)
    second_source.source_id = "other-inspector"
    held.sources.append(second_source)
    update = item.model_copy(deep=True)
    update.event.event_id = "E-other-source"
    update.event.source_id = second_source.source_id
    update.event.evidence_ids = ["EV-other-source"]
    evidence = update.published_evidence[0]
    evidence.evidence_id = "EV-other-source"
    evidence.source_id = second_source.source_id
    evidence.revision = 2
    evidence.supersedes_evidence_id = "EV-fitup-W17"
    updated, results = apply_deliveries(held, [update])
    assert results[0]["reason_codes"] == ["INVALID_SUPERSEDES_LINK"]
    assert [e.evidence_id for e in updated.evidence] == ["EV-fitup-W17"]


def test_pair_recovery_needs_newer_linked_evidence_for_matching_target(snapshot):
    item = deliveries(snapshot, "D03")[0]
    held, _ = apply_deliveries(snapshot, [item])
    restriction_id = held.active_restrictions[0].restriction_id
    recovery = item.model_copy(deep=True)
    recovery.delivery_tick = recovery.event.received_tick = recovery.event.observed_tick = 1
    recovery.event.kind = "PAIR_REACHABILITY_CHANGED"
    recovery.event.event_id = "E-pair-recovery"
    recovery.event.evidence_ids = ["EV-pair-recovery"]
    recovery.event.restriction_ids = [restriction_id]
    evidence = recovery.published_evidence[0]
    evidence.evidence_id = "EV-pair-recovery"
    evidence.value = "PASS"
    evidence.revision = 2
    evidence.available_tick = evidence.observed_tick = 1
    evidence.restriction_id = restriction_id
    held.now_tick = 1
    wrong = recovery.model_copy(deep=True)
    wrong.event.target.robot_id = "R2"
    wrong.published_evidence[0].target.robot_id = "R2"
    still_held, _ = apply_deliveries(held, [wrong])
    assert len(still_held.active_restrictions) == 1
    old = recovery.model_copy(deep=True)
    old.published_evidence[0].revision = 1
    still_held, results = apply_deliveries(held, [old])
    assert results[0]["reason_codes"] == ["STALE_EVIDENCE_REVISION"]
    assert len(still_held.active_restrictions) == 1
    released, results = apply_deliveries(held, [recovery])
    assert not released.active_restrictions
    assert results[0]["released_restriction_ids"] == [restriction_id]


def test_current_evidence_resolves_only_matching_information_hold(snapshot):
    missing = deliveries(snapshot)[0]
    missing.published_evidence = []
    held, result = apply_deliveries(snapshot, [missing])
    assert result[0]["lookup_passes"] == 1
    assert [r.kind for r in held.active_restrictions] == ["EVIDENCE_PENDING"]
    confirmed = deliveries(snapshot)[0]
    confirmed.event.event_id = "E-confirmed-fitup"
    updated, results = apply_deliveries(held, [confirmed])
    assert [r.kind for r in updated.active_restrictions] == ["FITUP_HOLD"]
    assert results[0]["released_restriction_ids"] == ["restriction:E-fitup-W17:EVIDENCE_PENDING"]


def test_maximum_length_external_id_still_produces_a_valid_restriction(snapshot):
    item=deliveries(snapshot)[0]
    item.event.event_id='e'*128
    updated,results=apply_deliveries(snapshot,[item])
    assert results[0]['outcome']=='APPLIED'
    assert len(updated.active_restrictions[0].restriction_id)<=128
