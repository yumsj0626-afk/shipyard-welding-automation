"""Pure evidence and restriction reduction for already arrived external events."""
from __future__ import annotations

from .contracts import Delivery, Evidence, EventEnvelope, PlanningSnapshot, Restriction, canonical, digest


EVENT_OBSERVATION = {
    "FITUP_FAILED": "FITUP_RESULT",
    "PAIR_UNREACHABLE": "PAIR_REACHABILITY",
    "PAIR_REACHABILITY_CHANGED": "PAIR_REACHABILITY",
    "REPAIR_REPORTED": "REPAIR_COMPLETED",
    "RECHECK_REPORTED": "RECHECK_RESULT",
    "MATERIAL_READINESS_CHANGED": "MATERIAL_READY",
    "ROBOT_AVAILABILITY_CHANGED": "ROBOT_AVAILABILITY",
    "ZONE_AVAILABILITY_CHANGED": "ZONE_AVAILABILITY",
    "IDENTITY_UNCERTAIN": "IDENTITY_MATCH",
}
EVENT_SCOPE = {
    "FITUP_FAILED": "JOB", "REPAIR_REPORTED": "JOB", "RECHECK_REPORTED": "JOB",
    "MATERIAL_READINESS_CHANGED": "JOB", "PAIR_UNREACHABLE": "JOB_ROBOT",
    "PAIR_REACHABILITY_CHANGED": "JOB_ROBOT", "ROBOT_AVAILABILITY_CHANGED": "ROBOT",
    "ZONE_AVAILABILITY_CHANGED": "ZONE",
}
VALUES = {
    "FITUP_RESULT": {"PASS", "FAIL", "UNKNOWN"},
    "REPAIR_COMPLETED": {"TRUE", "FALSE", "UNKNOWN"},
    "RECHECK_RESULT": {"PASS", "FAIL", "UNKNOWN"},
    "PAIR_REACHABILITY": {"PASS", "FAIL", "UNKNOWN"},
    "MATERIAL_READY": {"TRUE", "FALSE", "UNKNOWN"},
    "ROBOT_AVAILABILITY": {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"},
    "ZONE_AVAILABILITY": {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"},
    "IDENTITY_MATCH": {"PASS", "FAIL", "UNKNOWN"},
}


def _target_exists(target, snapshot):
    return (
        (target.job_id is None or any(j.job_id == target.job_id for j in snapshot.jobs))
        and (target.robot_id is None or any(r.robot_id == target.robot_id for r in snapshot.robots))
        and (target.zone_id is None or any(z.zone_id == target.zone_id for z in snapshot.zones))
    )


def _result(event, outcome="APPLIED", reasons=None):
    return {"event_id": event.event_id, "outcome": outcome, "reason_codes": reasons or [],
            "evidence_ids": [], "restriction_ids": [], "released_restriction_ids": [],
            "restrictions": [], "intervention_kind": "PREDEFINED_EXTERNAL",
            "lookup_passes": 0, "lookup_evidence_ids": []}


def _add_restriction(snapshot, event, result, kind, evidence):
    rule_id = f"rules-v1:{kind}" + (f":{EVENT_OBSERVATION[event.kind]}" if kind == "EVIDENCE_PENDING" else "")
    existing = next((r for r in snapshot.active_restrictions if r.kind == kind
                     and r.incident_id == event.incident_id and r.target == event.target
                     and r.rule_id == rule_id), None)
    if existing:
        result["restriction_ids"].append(existing.restriction_id)
        return
    restriction_id = f"restriction:{event.event_id}:{kind}"
    if len(restriction_id) > 128:
        restriction_id = "restriction:" + digest({"event_id": event.event_id, "kind": kind})
    restriction = Restriction(
        restriction_id=restriction_id, incident_id=event.incident_id,
        kind=kind, target=event.target.model_copy(deep=True), opened_event_id=event.event_id,
        opened_evidence_ids=[e.evidence_id for e in evidence], opened_tick=snapshot.now_tick,
        rule_id=rule_id, release_rule_id=f"rules-v1:release:{kind}",
    )
    snapshot.active_restrictions.append(restriction)
    result["restriction_ids"].append(restriction.restriction_id)
    result["restrictions"].append(restriction.model_dump(mode="json"))


def _request(snapshot, event, result, reason, *, conflict=False, add_pending=True):
    result["outcome"] = "EVIDENCE_CONFLICT" if conflict else "INSUFFICIENT_EVIDENCE"
    result["reason_codes"].append(reason)
    result["intervention_kind"] = "ADDITIONAL_HUMAN_REQUEST"
    if add_pending and event.target.scope != "UNRESOLVED" and _target_exists(event.target, snapshot):
        _add_restriction(snapshot, event, result, "EVIDENCE_PENDING", [])


def _latest(evidence: list[Evidence], event: EventEnvelope, kind: str):
    matching = [e for e in evidence if e.target == event.target and e.observation_kind == kind
                and e.incident_id in {None, event.incident_id}]
    by_source: dict[str, list[Evidence]] = {}
    for item in matching:
        by_source.setdefault(item.source_id, []).append(item)
    selected = []
    for group in by_source.values():
        latest = max((e.observed_tick, e.revision) for e in group)
        selected.extend(e for e in group if (e.observed_tick, e.revision) == latest)
    return selected


def _release(snapshot, event, result, restriction, evidence):
    released = restriction.model_copy(deep=True)
    released.status = "RELEASED"
    released.released_event_id = event.event_id
    released.released_evidence_ids = [e.evidence_id for e in evidence]
    released.released_tick = snapshot.now_tick
    snapshot.active_restrictions = [r for r in snapshot.active_restrictions
                                    if r.restriction_id != restriction.restriction_id]
    result["released_restriction_ids"].append(restriction.restriction_id)
    result["restrictions"].append(released.model_dump(mode="json"))


def _linked_recovery(evidence, restriction, event):
    return (evidence.restriction_id == restriction.restriction_id
            and evidence.incident_id == restriction.incident_id == event.incident_id
            and evidence.target == restriction.target == event.target
            and evidence.observed_tick >= restriction.opened_tick)


def _fitup_recovery(snapshot, event, result):
    targets = [r for r in snapshot.active_restrictions
               if r.restriction_id in event.restriction_ids and r.kind == "FITUP_HOLD"]
    if not targets:
        _request(snapshot, event, result, "RECOVERY_TARGET_NOT_ACTIVE", add_pending=False)
        return
    released_any = False
    for restriction in targets:
        current_repairs = [e for e in _latest(snapshot.evidence, event, "REPAIR_COMPLETED")
                           if _linked_recovery(e, restriction, event)]
        if {e.value for e in current_repairs} != {"TRUE"}:
            _request(snapshot, event, result, "CURRENT_REPAIR_CONFIRMATION_REQUIRED",
                     conflict=len({e.value for e in current_repairs} - {"UNKNOWN"}) > 1,
                     add_pending=False)
            continue
        checks = [e for e in _latest(snapshot.evidence, event, "RECHECK_RESULT")
                  if _linked_recovery(e, restriction, event)]
        if {e.value for e in checks} - {"UNKNOWN"} == {"PASS", "FAIL"}:
            _request(snapshot, event, result, "CONFLICTING_RECHECK", conflict=True, add_pending=False)
            continue
        for check in checks:
            if check.value != "PASS":
                continue
            repairs = [e for e in current_repairs
                       if e.evidence_id in check.related_evidence_ids
                       and e.observation_kind == "REPAIR_COMPLETED" and e.value == "TRUE"
                       and _linked_recovery(e, restriction, event)
                       and e.observed_tick <= check.observed_tick]
            if repairs:
                _release(snapshot, event, result, restriction, repairs + [check])
                result["evidence_ids"].extend(e.evidence_id for e in repairs + [check])
                released_any = True
                break
    if not released_any and not result["reason_codes"]:
        _request(snapshot, event, result, "REPAIR_AND_LINKED_RECHECK_REQUIRED", add_pending=False)


def apply_deliveries(snapshot: PlanningSnapshot, deliveries: list[Delivery]):
    """Return a new public snapshot and per-event results; no I/O or version change."""
    updated = snapshot.model_copy(deep=True)
    sources = {s.source_id: s for s in snapshot.sources}
    original = {e.evidence_id: e for e in snapshot.evidence}
    published: dict[str, Evidence] = {}
    rejected: dict[str, tuple[str, str]] = {}

    for delivery in deliveries:
        event = delivery.event
        source = sources.get(event.source_id)
        if event.run_id != snapshot.run_id or delivery.delivery_tick != event.received_tick or delivery.delivery_tick > snapshot.now_tick:
            rejected[event.event_id] = ("INVALID_INPUT", "RUN_OR_DELIVERY_TIME_MISMATCH")
        elif not source or event.target.scope not in source.allowed_target_scopes:
            rejected[event.event_id] = ("INVALID_INPUT", "UNAUTHORIZED_SOURCE")
        elif event.kind in EVENT_SCOPE and event.target.scope != EVENT_SCOPE[event.kind]:
            rejected[event.event_id] = ("INVALID_INPUT", "EVENT_SCOPE_MISMATCH")
        elif not _target_exists(event.target, snapshot):
            rejected[event.event_id] = ("INVALID_INPUT", "UNKNOWN_TARGET")
        elif EVENT_OBSERVATION[event.kind] not in source.allowed_observation_kinds:
            rejected[event.event_id] = ("INVALID_INPUT", "UNAUTHORIZED_OBSERVATION_KIND")
        if event.event_id in rejected:
            continue
        for evidence in delivery.published_evidence:
            authority = sources.get(evidence.source_id)
            if (not authority or evidence.observation_kind not in authority.allowed_observation_kinds
                    or evidence.target.scope not in authority.allowed_target_scopes
                    or not _target_exists(evidence.target, snapshot)
                    or evidence.available_tick > snapshot.now_tick
                    or evidence.value not in VALUES[evidence.observation_kind]
                    or evidence.target != event.target
                    or evidence.incident_id not in {None, event.incident_id}):
                rejected[event.event_id] = ("INVALID_INPUT", "INVALID_PUBLISHED_EVIDENCE")
                break
            old = original.get(evidence.evidence_id) or published.get(evidence.evidence_id)
            if old is not None and canonical(old) != canonical(evidence):
                rejected[event.event_id] = ("ID_CONFLICT", "EVIDENCE_ID_REUSED")
                for other in deliveries:
                    if any(e.evidence_id == evidence.evidence_id for e in other.published_evidence):
                        rejected[other.event.event_id] = ("ID_CONFLICT", "EVIDENCE_ID_REUSED")
                break
            prior = [e for e in snapshot.evidence if e.source_id == evidence.source_id
                     and e.target == evidence.target and e.incident_id == evidence.incident_id
                     and e.observation_kind == evidence.observation_kind and e.evidence_id != evidence.evidence_id]
            if prior and evidence.revision <= max(e.revision for e in prior):
                rejected[event.event_id] = ("INVALID_INPUT", "STALE_EVIDENCE_REVISION")
                break
            published[evidence.evidence_id] = evidence

    accepted_ids = {e.evidence_id for d in deliveries if d.event.event_id not in rejected
                    for e in d.published_evidence}
    while True:
        combined = {**original, **{key: value for key, value in published.items() if key in accepted_ids}}
        count = len(rejected)
        for delivery in deliveries:
            if delivery.event.event_id in rejected:
                continue
            for evidence in delivery.published_evidence:
                refs = evidence.related_evidence_ids + ([evidence.supersedes_evidence_id] if evidence.supersedes_evidence_id else [])
                reason = None
                if any(ref not in combined for ref in refs):
                    reason = "UNKNOWN_EVIDENCE_REFERENCE"
                elif evidence.supersedes_evidence_id:
                    previous = combined[evidence.supersedes_evidence_id]
                    if (previous.source_id != evidence.source_id or previous.target != evidence.target
                            or previous.incident_id != evidence.incident_id
                            or previous.observation_kind != evidence.observation_kind
                            or previous.revision >= evidence.revision
                            or previous.observed_tick > evidence.observed_tick):
                        reason = "INVALID_SUPERSEDES_LINK"
                if reason is None and evidence.observation_kind == "RECHECK_RESULT":
                    if any(combined[ref].observation_kind != "REPAIR_COMPLETED"
                           or combined[ref].target != evidence.target
                           or combined[ref].incident_id != evidence.incident_id
                           or combined[ref].restriction_id != evidence.restriction_id
                           for ref in evidence.related_evidence_ids):
                        reason = "INVALID_RECHECK_LINK"
                if reason:
                    rejected[delivery.event.event_id] = ("INVALID_INPUT", reason)
                    break
        accepted_ids = {e.evidence_id for d in deliveries if d.event.event_id not in rejected
                        for e in d.published_evidence}
        if count == len(rejected):
            break
    accepted_ids = {e.evidence_id for d in deliveries if d.event.event_id not in rejected
                    for e in d.published_evidence}
    updated.evidence = list(original.values()) + [published[key].model_copy(deep=True)
                                                for key in sorted(accepted_ids - original.keys())]
    updated.visible_evidence_refs = [e.evidence_id for e in updated.evidence]

    results = []
    for delivery in sorted(deliveries, key=lambda d: (d.event.observed_tick, d.event.event_id)):
        event = delivery.event
        result = _result(event)
        results.append(result)
        if event.event_id in rejected:
            result["outcome"], reason = rejected[event.event_id]
            result["reason_codes"].append(reason)
            result["intervention_kind"] = "ADDITIONAL_HUMAN_REQUEST"
            continue
        if event.target.scope == "UNRESOLVED":
            _request(updated, event, result, "IDENTIFY_TARGET_REQUIRED", add_pending=False)
            continue
        evidence = _latest(updated.evidence, event, EVENT_OBSERVATION[event.kind])
        result["lookup_passes"] = 1
        result["lookup_evidence_ids"] = [e.evidence_id for e in evidence]
        result["evidence_ids"] = [e.evidence_id for e in evidence]
        values = {e.value for e in evidence}
        if len(values - {"UNKNOWN"}) > 1:
            _request(updated, event, result, "CONFLICTING_CURRENT_EVIDENCE", conflict=True)
            continue
        if not evidence or "UNKNOWN" in values or event.unknown_fields:
            _request(updated, event, result, "CURRENT_EVIDENCE_REQUIRED")
            continue
        value = evidence[0].value
        affected_running = any(
            j.lifecycle == "RUNNING" and (
                event.target.job_id == j.job_id or event.target.robot_id == j.assigned_robot_id
                or event.target.zone_id == j.zone_id
            ) for j in updated.jobs
        )
        if affected_running and value in {"FAIL", "FALSE", "UNAVAILABLE"}:
            result["outcome"] = "OUT_OF_SCOPE_RUNNING"
            result["reason_codes"] = ["RUNNING_WELD_IS_NONPREEMPTIVE"]
            continue
        if event.kind == "FITUP_FAILED":
            if value != "FAIL":
                result.update(outcome="INVALID_INPUT", reason_codes=["FITUP_FAILED_REQUIRES_FAIL"])
                continue
            _add_restriction(updated, event, result, "FITUP_HOLD", evidence)
        elif event.kind in {"PAIR_UNREACHABLE", "PAIR_REACHABILITY_CHANGED"}:
            if value == "FAIL":
                _add_restriction(updated, event, result, "PAIR_BAN", evidence)
            elif value == "PASS" and event.kind == "PAIR_REACHABILITY_CHANGED":
                _release_explicit(updated, event, result, "PAIR_BAN", evidence)
            else:
                result.update(outcome="INVALID_INPUT", reason_codes=["PAIR_UNREACHABLE_REQUIRES_FAIL"])
                continue
        elif event.kind == "REPAIR_REPORTED":
            if value != "TRUE":
                _request(updated, event, result, "REPAIR_NOT_CONFIRMED", add_pending=False)
                continue
            checks = _latest(updated.evidence, event, "RECHECK_RESULT")
            if not any(c.value == "PASS" and any(e.evidence_id in c.related_evidence_ids for e in evidence) for c in checks):
                _request(updated, event, result, "RECHECK_REQUIRED", add_pending=False)
            else:
                result["outcome"] = "REPAIR_RECORDED"
        elif event.kind == "RECHECK_REPORTED":
            _fitup_recovery(updated, event, result)
        elif event.kind == "MATERIAL_READINESS_CHANGED":
            job = next(j for j in updated.jobs if j.job_id == event.target.job_id)
            job.material_ready = value
            job.fact_refs["material_ready"] = result["evidence_ids"]
        elif event.kind in {"ROBOT_AVAILABILITY_CHANGED", "ZONE_AVAILABILITY_CHANGED"}:
            is_robot = event.kind == "ROBOT_AVAILABILITY_CHANGED"
            item = (next(r for r in updated.robots if r.robot_id == event.target.robot_id) if is_robot
                    else next(z for z in updated.zones if z.zone_id == event.target.zone_id))
            restriction_kind = "ROBOT_BAN" if is_robot else "ZONE_BAN"
            if value == "UNAVAILABLE":
                item.availability = value
                _add_restriction(updated, event, result, restriction_kind, evidence)
            else:
                active = [r for r in updated.active_restrictions if r.kind == restriction_kind and r.target == event.target]
                if active:
                    _release_explicit(updated, event, result, restriction_kind, evidence)
                    remaining = [r for r in updated.active_restrictions if r.kind == restriction_kind and r.target == event.target]
                    if not remaining and result["released_restriction_ids"]:
                        item.availability = "AVAILABLE"
                else:
                    item.availability = "AVAILABLE"
            item.fact_refs["availability"] = result["evidence_ids"]
        elif event.kind == "IDENTITY_UNCERTAIN" and value != "PASS":
            _request(updated, event, result, "IDENTITY_CONFIRMATION_REQUIRED")
        if result["outcome"] in {"APPLIED", "REPAIR_RECORDED"}:
            # A matching confirmed fact resolves only that incident's information hold.
            for pending in list(updated.active_restrictions):
                if (pending.kind == "EVIDENCE_PENDING" and pending.target == event.target
                        and pending.incident_id == event.incident_id
                        and pending.rule_id == f"rules-v1:EVIDENCE_PENDING:{EVENT_OBSERVATION[event.kind]}"):
                    _release(updated, event, result, pending, evidence)
    for result in results:
        result["evidence_ids"] = sorted(set(result["evidence_ids"]))
    return updated, results


def _release_explicit(snapshot, event, result, kind, evidence):
    matching = [r for r in snapshot.active_restrictions
                if r.kind == kind and r.restriction_id in event.restriction_ids]
    released = False
    for restriction in matching:
        valid = [e for e in evidence if _linked_recovery(e, restriction, event)]
        if valid:
            _release(snapshot, event, result, restriction, valid)
            released = True
    if not released:
        _request(snapshot, event, result, "RECOVERY_LINK_MISMATCH", add_pending=False)
