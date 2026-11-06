"""Independent development-case evaluator; never imports the operational checker.

Expected behavior comes from a manually authored oracle and evaluator-only facts.
The implementation deliberately reads raw phase records and audit payloads rather
than operational summary success flags. This is not the main research experiment.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import platform
import shlex
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _parse_json(text, path):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key in {path}: {key}")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError(f"non-finite number in {path}: {value}")

    return json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)


def _parse_jsonl(text, path):
    return [_parse_json(line, f"{path}:{line_number}")
            for line_number, line in enumerate(text.splitlines(), start=1) if line.strip()]


def _number(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer tick")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    raise ValueError(f"invalid integer tick: {value!r}")


def _payload(row, name):
    data = row.get("data", {})
    return data.get(name, data)


def _issue(code, **details):
    return {"code": code, **details}


def _normalize_executions(rows):
    result = []
    fields = ("execution_id", "instruction_id", "job_id", "robot_id", "phase",
              "start_tick", "end_tick", "from_zone_id", "to_zone_id", "zone_id")
    for raw in rows:
        missing = [key for key in fields if key not in raw]
        if missing:
            raise ValueError(f"execution record missing fields: {missing}")
        row = dict(raw)
        row["start_tick"] = _number(row["start_tick"])
        row["end_tick"] = _number(row["end_tick"])
        if row["phase"] not in ("MOVE", "SETUP", "WELD"):
            raise ValueError(f"unknown execution phase: {row['phase']}")
        result.append(row)
    return result


def _blocked_at(truth, tick):
    held, pair_bans = set(), set()
    for fact in sorted(truth, key=lambda f: f["tick"]):
        if fact["tick"] > tick:
            continue
        if fact["fact_kind"] == "FITUP_BLOCKED":
            held.add(fact["job_id"])
        elif fact["fact_kind"] == "RECHECK_PASSED":
            repairs = [r for r in truth if r["fact_kind"] == "REPAIR_COMPLETED"
                       and r["job_id"] == fact["job_id"]
                       and r["tick"] == fact.get("linked_repair_tick")
                       and r["tick"] <= fact["tick"]]
            if repairs:
                held.discard(fact["job_id"])
        elif fact["fact_kind"] == "PAIR_UNREACHABLE":
            pair_bans.add((fact["job_id"], fact["robot_id"]))
    return held, pair_bans


def _overlaps(intervals, resource, violations):
    for i, first in enumerate(intervals):
        for second in intervals[i + 1:]:
            if first["end_tick"] <= first["start_tick"] or second["end_tick"] <= second["start_tick"]:
                continue
            if first["end_tick"] > second["start_tick"] and second["end_tick"] > first["start_tick"]:
                violations.append(_issue(resource + "_OVERLAP", execution_ids=[first["execution_id"], second["execution_id"]]))


def _execution_violations(rows, trace, environment, truth):
    jobs = environment["jobs"]
    failures = []
    by_robot, by_zone, by_attempt = defaultdict(list), defaultdict(list), defaultdict(list)
    welds = defaultdict(list)
    seen = set()
    for row in rows:
        key = row["execution_id"]
        if key in seen:
            failures.append(_issue("DUPLICATE_EXECUTION", execution_id=key))
        seen.add(key)
        if row["job_id"] not in jobs:
            failures.append(_issue("UNKNOWN_JOB", execution_id=key))
            continue
        job = jobs[row["job_id"]]
        if row["end_tick"] < row["start_tick"]:
            failures.append(_issue("NEGATIVE_DURATION", execution_id=key))
        if row["robot_id"] not in job["capable_robot_ids"]:
            failures.append(_issue("ROBOT_CAPABILITY", execution_id=key))
        held, pairs = _blocked_at(truth, max(0, row["start_tick"]))
        if row["job_id"] in held:
            failures.append(_issue("HELD_JOB_STARTED", execution_id=key, job_id=row["job_id"]))
        if (row["job_id"], row["robot_id"]) in pairs:
            failures.append(_issue("FORBIDDEN_PAIR_STARTED", execution_id=key))
        if row["job_id"] in environment["material_unready_job_ids"]:
            failures.append(_issue("UNREADY_JOB_STARTED", execution_id=key))
        duration = row["end_tick"] - row["start_tick"]
        if row["phase"] == "MOVE":
            expected = environment["same_zone_travel_ticks"] if row["from_zone_id"] == row["to_zone_id"] else environment["different_zone_travel_ticks"]
            if duration != expected or row["to_zone_id"] != job["zone_id"]:
                failures.append(_issue("MOVE_TIME_OR_DESTINATION", execution_id=key))
        elif duration != job["setup_ticks" if row["phase"] == "SETUP" else "duration_ticks"]:
            failures.append(_issue("PHASE_DURATION", execution_id=key))
        if row["phase"] in ("SETUP", "WELD"):
            if row["zone_id"] != job["zone_id"]:
                failures.append(_issue("WRONG_ZONE", execution_id=key))
            by_zone[row["zone_id"]].append(row)
        by_robot[row["robot_id"]].append(row)
        by_attempt[(row["job_id"], row.get("attempt_id") or row["job_id"])].append(row)
        if row["phase"] == "WELD":
            welds[row["job_id"]].append(row)
    for resource, items in by_robot.items():
        _overlaps(items, "ROBOT", failures)
        position = environment["initial_robot_locations"].get(resource)
        for row in sorted(items, key=lambda r: (r["start_tick"], {"MOVE": 0, "SETUP": 1, "WELD": 2}[r["phase"]])):
            if row["phase"] == "MOVE":
                if row["from_zone_id"] != position:
                    failures.append(_issue("ROBOT_TELEPORT", execution_id=row["execution_id"]))
                position = row["to_zone_id"]
            elif row["to_zone_id"] != position:
                failures.append(_issue("ROBOT_POSITION", execution_id=row["execution_id"]))
    for items in by_zone.values():
        _overlaps(items, "ZONE", failures)
    for job_id, items in welds.items():
        if len(items) > 1:
            failures.append(_issue("DUPLICATE_WELD", job_id=job_id))
    # Check starts against actual predecessor END, never a planned end.
    for row in rows:
        for pred in jobs.get(row["job_id"], {}).get("predecessor_ids", []):
            if not any(p["end_tick"] <= row["start_tick"] for p in welds[pred]):
                failures.append(_issue("ACTUAL_PREDECESSOR_INCOMPLETE", execution_id=row["execution_id"], predecessor_id=pred))
    for (job_id, attempt), items in by_attempt.items():
        for weld in [r for r in items if r["phase"] == "WELD"]:
            if job_id == "W18" and weld["start_tick"] == -2:
                continue
            setups = [r for r in items if r["phase"] == "SETUP" and r["end_tick"] == weld["start_tick"] and r["robot_id"] == weld["robot_id"]]
            if len(setups) != 1:
                failures.append(_issue("SETUP_MISSING_OR_NOT_CONTIGUOUS", execution_id=weld["execution_id"]))
            else:
                moves = [r for r in items if r["phase"] == "MOVE" and r["end_tick"] <= setups[0]["start_tick"] and r["robot_id"] == weld["robot_id"]]
                if len(moves) != 1:
                    failures.append(_issue("MOVE_MISSING_OR_DUPLICATE", execution_id=weld["execution_id"]))
        # Committed robot is occupied while waiting between MOVE and SETUP.
        moves, ends = [r for r in items if r["phase"] == "MOVE"], [r for r in items if r["phase"] == "WELD"]
        if len(moves) == 1 and len(ends) == 1:
            for other in rows:
                if other["robot_id"] == moves[0]["robot_id"] and (other["job_id"], other.get("attempt_id") or other["job_id"]) != (job_id, attempt):
                    if other["start_tick"] < ends[0]["end_tick"] and other["end_tick"] > moves[0]["start_tick"]:
                        if other["end_tick"] <= other["start_tick"]:
                            continue
                        failures.append(_issue("COMMITTED_ROBOT_OVERLAP", execution_id=other["execution_id"]))
    phase_observations = defaultdict(list)
    for log in trace:
        if log.get("stage") in ("PHASE_START", "PHASE_END"):
            obs = _payload(log, "observation")
            phase_observations[(log["stage"], obs.get("execution_id"))].append(obs)
    for row in rows:
        for stage, time_field in (("PHASE_START", "start_tick"), ("PHASE_END", "end_tick")):
            if stage == "PHASE_START" and row["job_id"] == "W18" and row["start_tick"] == -2:
                continue  # Explicit pre-run history is the one allowed exception.
            observations = phase_observations[(stage, row["execution_id"])]
            if not any(obs.get("tick") == row[time_field] and all(obs.get(k) == row[k] for k in ("job_id", "robot_id", "phase", "instruction_id")) for obs in observations):
                failures.append(_issue("CSV_TRACE_MISMATCH", execution_id=row["execution_id"], missing_stage=stage))
    # A forbidden or unfinished start must not disappear just because CSV exports ENDs.
    for log in trace:
        if log.get("stage") != "PHASE_START":
            continue
        obs = _payload(log, "observation")
        if obs.get("execution_id") in seen:
            continue
        failures.append(_issue("START_WITHOUT_END", execution_id=obs.get("execution_id"), trace_seq=log.get("seq")))
    return failures, welds


def _proposal_violations(plan, tick, completed_rows, environment, truth):
    """A separate arithmetic audit of candidate schedules, not the live checker."""
    failures = []
    jobs = environment["jobs"]
    held, banned = _blocked_at(truth, tick)
    assignments = plan.get("assignments", [])
    scheduled = {a["job_id"]: a for a in assignments}
    if len(scheduled) != len(assignments):
        failures.append("DUPLICATE_ASSIGNMENT")
    robot_spans, zone_spans = defaultdict(list), defaultdict(list)
    actual_ends = {r["job_id"]: r["end_tick"] for r in completed_rows if r["phase"] == "WELD" and r["end_tick"] <= tick}
    fixed = plan.get("fixed_segments", [])
    fixed_weld_ends = {f["job_id"]: f["end_tick"] for f in fixed if f["phase"] == "WELD"}
    for a in assignments:
        jid, rid = a["job_id"], a["robot_id"]
        if jid not in jobs:
            failures.append("UNKNOWN_JOB")
            continue
        job = jobs[jid]
        if jid in held or any(pred in held for pred in job["predecessor_ids"]):
            failures.append("HELD_JOB_PLANNED")
        if (jid, rid) in banned:
            failures.append("FORBIDDEN_PAIR_PLANNED")
        if jid in environment["material_unready_job_ids"]:
            failures.append("UNREADY_JOB_PLANNED")
        if rid not in job["capable_robot_ids"]:
            failures.append("ROBOT_CAPABILITY")
        if a["weld_end"] - a["weld_start"] != job["duration_ticks"]:
            failures.append("WELD_DURATION")
        start = next((a.get(k) for k in ("move_start", "setup_start", "weld_start") if a.get(k) is not None), tick)
        if start < tick:
            failures.append("PLAN_START_IN_PAST")
        if a.get("setup_start") is not None and (a["setup_end"] - a["setup_start"] != job["setup_ticks"] or a["setup_end"] != a["weld_start"]):
            failures.append("SETUP_DURATION_OR_ORDER")
        if a.get("move_start") is not None:
            travel = environment["same_zone_travel_ticks"] if a["source_position"] == job["zone_id"] else environment["different_zone_travel_ticks"]
            if a["move_end"] - a["move_start"] != travel or a["setup_start"] < a["move_end"]:
                failures.append("MOVE_DURATION_OR_ORDER")
        for pred in job["predecessor_ids"]:
            pred_end = scheduled[pred]["weld_end"] if pred in scheduled else actual_ends.get(pred, fixed_weld_ends.get(pred))
            if pred_end is None or start < pred_end:
                failures.append("PLANNED_PRECEDENCE")
        robot_spans[rid].append({"execution_id": jid, "start_tick": start, "end_tick": a["weld_end"]})
        zone_start = a.get("setup_start") if a.get("setup_start") is not None else a["weld_start"]
        zone_spans[job["zone_id"]].append({"execution_id": jid, "start_tick": zone_start, "end_tick": a["weld_end"]})
    for f in fixed:
        if f["job_id"] in scheduled:
            continue  # The continuation's committed span already covers this job.
        span = {"execution_id": f["execution_id"], "start_tick": max(tick, f["start_tick"]), "end_tick": f["end_tick"]}
        robot_spans[f["robot_id"]].append(span)
        if f["phase"] in ("SETUP", "WELD"):
            zone_spans[f["zone_id"]].append(span)
    for resources, code in ((robot_spans, "ROBOT"), (zone_spans, "ZONE")):
        for spans in resources.values():
            overlap = []
            _overlaps(spans, code, overlap)
            failures.extend(v["code"] for v in overlap)
    return sorted(set(failures))


def _release_references_match(restriction, trace):
    """Require repair and recheck receipts for this restriction, not two arbitrary IDs."""
    released = set(restriction.get("released_evidence_ids", []))
    matches = {}
    evidence = {}
    for log in trace:
        if log.get("stage") != "EVENT":
            continue
        event = _payload(log, "event")
        if event.get("target", {}).get("job_id") != restriction.get("target", {}).get("job_id"):
            continue
        if event.get("incident_id") != restriction.get("incident_id"):
            continue
        if restriction.get("restriction_id") not in event.get("restriction_ids", []):
            continue
        if event.get("received_tick", float("inf")) > restriction.get("released_tick", -1):
            continue
        matches.setdefault(event.get("kind"), set()).update(released.intersection(event.get("evidence_ids", [])))
        for item in log.get("data", {}).get("published_evidence", []):
            evidence[item["evidence_id"]] = item
    repair_ids = matches.get("REPAIR_REPORTED", set())
    recheck_ids = matches.get("RECHECK_REPORTED", set())
    if not repair_ids or not recheck_ids:
        return False
    # When raw published evidence is present, verify its cross-reference as well.
    if evidence:
        return any(evidence.get(recheck_id, {}).get("value") == "PASS"
                   and repair_ids.intersection(evidence.get(recheck_id, {}).get("related_evidence_ids", []))
                   for recheck_id in recheck_ids)
    return True


def score_records(executions, trace, oracle, truth):
    """Return independently checked development acceptance and event-level records."""
    if any(f.get("case_id") != oracle["case_id"] for f in truth):
        raise ValueError("oracle/truth case mismatch")
    initial = [f for f in truth if f["fact_kind"] == "INITIAL_ENVIRONMENT"]
    if len(initial) != 1:
        raise ValueError("exactly one INITIAL_ENVIRONMENT fact required")
    environment = initial[0]
    rows = _normalize_executions(executions)
    violations, welds = _execution_violations(rows, trace, environment, truth)
    issues = list(violations)
    required_initial = oracle["required_initial_execution"]
    keys = ("job_id", "robot_id", "phase", "start_tick", "end_tick")
    if not any(all(r[k] == required_initial[k] for k in keys) for r in rows):
        issues.append(_issue("INITIAL_W18_NOT_PRESERVED"))
    for jid in oracle["expected_completed_job_ids"]:
        if len(welds[jid]) != 1:
            issues.append(_issue("REQUIRED_COMPLETION_MISSING", job_id=jid))
    for jid in oracle["expected_unstarted_job_ids"]:
        if any(r["job_id"] == jid for r in rows):
            issues.append(_issue("EXPECTED_UNSTARTED_JOB_EXECUTED", job_id=jid))

    restrictions = [_payload(t, "restriction") for t in trace if t.get("stage") == "RESTRICTION"]
    hold_correct = True
    held = oracle.get("required_hold")
    if held:
        matches = [r for r in restrictions if r.get("kind") == held["kind"] and r.get("target", {}).get("job_id") == held["job_id"]]
        opens = [r for r in matches if r.get("status") == "ACTIVE" and r.get("opened_tick") == held["opened_tick"] and r.get("opened_evidence_ids") and r.get("target", {}).get("scope") == "JOB" and r.get("target", {}).get("robot_id") is None]
        releases = [r for r in matches if r.get("status") == "RELEASED"]
        hold_correct = bool(opens)
        if held["release_tick"] is None:
            hold_correct = hold_correct and not releases
        else:
            hold_correct = hold_correct and bool(releases) and all(r.get("released_tick") == held["release_tick"] and _release_references_match(r, trace) for r in releases)
        if not hold_correct:
            issues.append(_issue("HOLD_OR_RELEASE_INCORRECT"))
    pair = oracle.get("required_pair_ban")
    if pair and not any(r.get("kind") == "PAIR_BAN" and r.get("status") == "ACTIVE" and r.get("target", {}).get("scope") == "JOB_ROBOT" and r.get("target", {}).get("job_id") == pair["job_id"] and r.get("target", {}).get("robot_id") == pair["robot_id"] for r in restrictions):
        issues.append(_issue("REQUIRED_PAIR_BAN_MISSING"))
    for jid in oracle.get("forbidden_job_wide_hold_ids", []):
        if any(r.get("target", {}).get("job_id") == jid and r.get("target", {}).get("scope") == "JOB" and r.get("status") == "ACTIVE" for r in restrictions):
            issues.append(_issue("OVERBROAD_JOB_HOLD", job_id=jid))
    alt = oracle.get("required_alternative")
    if alt and not any(r["job_id"] in alt["job_ids"] and r["phase"] == "WELD" and r["start_tick"] < alt["weld_start_before_tick"] for r in rows):
        issues.append(_issue("ALTERNATIVE_NOT_EXECUTED"))
    wait = oracle.get("required_wait")
    if wait and not any(t.get("stage") == "WAIT" and t["tick"] < wait["before_tick"] and t.get("data", {}).get("robot_id") == wait["robot_id"] and t["data"].get("reason_codes") and t["data"].get("recheck_triggers") and not any(x in {"NO_SOLUTION_WITHIN_LIMIT", "INFEASIBLE", "MODEL_INVALID", "INTERNAL_ERROR"} for x in t["data"]["reason_codes"]) for t in trace):
        issues.append(_issue("JUSTIFIED_WAIT_MISSING"))
    humans = [t for t in trace if t.get("stage") == "HUMAN_REQUEST"]
    request = oracle.get("required_human_request")
    if request and not any(t["tick"] >= request["at_or_after_tick"] and any("RECHECK" in str(reason).upper() for reason in t.get("data", {}).get("reason_codes", [])) for t in humans):
        issues.append(_issue("NECESSARY_RECHECK_REQUEST_MISSING"))
    if humans and not oracle["additional_human_request_allowed"]:
        issues.append(_issue("UNNECESSARY_HUMAN_REQUEST"))
    reentry_correct = None
    if oracle["requires_reentry"]:
        reentry_correct = hold_correct and len(welds["W17"]) == 1 and welds["W17"][0]["start_tick"] >= held["release_tick"]
        if not reentry_correct:
            issues.append(_issue("RECOVERY_REENTRY_MISSING"))

    plan_audits = []
    for t in trace:
        if t.get("stage") != "PLAN":
            continue
        plan = _payload(t, "plan")
        bad = _proposal_violations(plan, t["tick"], rows, environment, truth)
        checks = [_payload(c, "check") for c in trace if c.get("stage") == "CHECK" and _payload(c, "check").get("plan_id") == plan["plan_id"]]
        applied = any(a.get("stage") == "APPLY" and a.get("data", {}).get("plan_id") == plan["plan_id"] for a in trace)
        plan_audits.append({"plan_id": plan["plan_id"], "trace_seq": t.get("seq"), "violations": bad, "blocked": bool(bad) and any(c.get("passed") is False for c in checks) and not applied})
        if bad and applied:
            issues.append(_issue("INVALID_PLAN_APPLIED", plan_id=plan["plan_id"], violations=bad))

    events = {}
    for t in trace:
        if t.get("stage") == "EVENT":
            event = _payload(t, "event")
            if event.get("event_id"):
                events.setdefault(event["event_id"], (event, t.get("seq")))
    matched = set()
    for expected in oracle["expected_events"]:
        match = [eid for eid, (e, _) in events.items() if e.get("kind") == expected["kind"] and e.get("received_tick") == expected["tick"] and e.get("target", {}).get("job_id") == expected["job_id"] and e.get("target", {}).get("robot_id") == expected.get("robot_id")]
        if not match:
            issues.append(_issue("EXPECTED_EVENT_NOT_RECORDED", kind=expected["kind"], tick=expected["tick"]))
        matched.update(match)
    for eid in events.keys() - matched:
        issues.append(_issue("UNEXPECTED_EVENT", event_id=eid))
    correct = not issues
    no_human = correct and not humans
    event_scores = [{"event_id": eid, "kind": e["kind"], "trace_seq": seq, "correct_action": correct, "completed_without_additional_human": no_human, "hold_correct": hold_correct, "reentry_correct": reentry_correct, "reason_codes": sorted({i["code"] for i in issues})} for eid, (e, seq) in events.items()]
    # Missing inputs cannot shrink the denominator and inflate success.
    denominator = max(len(oracle["expected_events"]), len(events))
    numerator = sum(e["completed_without_additional_human"] for e in event_scores)
    return {"schema_version": "1.0", "case_id": oracle["case_id"], "evaluation_scope": "development_acceptance", "main_experiment": False,
            "deadline_applied": False, "oracle_review_status": oracle["provenance"]["review_status"],
            "acceptance_passed": correct, "correct_action": correct, "completed_without_additional_human": no_human,
            "hold_correct": hold_correct, "reentry_correct": reentry_correct, "events": event_scores,
            "evaluated_event_count": denominator, "autonomous_correct_count": numerator,
            "development_autonomous_correct_rate": numerator / denominator if denominator else None,
            "additional_human_requests": len(humans), "proposed_violation_count": sum(bool(p["violations"]) for p in plan_audits),
            "blocked_violation_count": sum(p["blocked"] for p in plan_audits), "executed_violation_count": len(violations),
            "plan_audits": plan_audits, "issues": issues,
            "completed_job_ids": sorted(jid for jid, items in welds.items() if len(items) == 1),
            "total_weighted_tardiness_completed_jobs": sum(environment["jobs"][jid]["weight"] * max(0, items[0]["end_tick"] - environment["jobs"][jid]["due_tick"]) for jid, items in welds.items() if len(items) == 1),
            "scoring_note": "Event closure uses whole-case rule obligations, including required recovery; no operating checker or summary success flags are used."}


def score_run(run_dir, oracle_path, truth_path, *, command=None):
    run_dir = Path(run_dir)
    paths = {"execution": run_dir / "execution.csv", "trace": run_dir / "trace.jsonl",
             "oracle": Path(oracle_path), "truth": Path(truth_path)}
    # Score and hash the same bytes; a later file edit cannot change input identity.
    contents = {name: path.read_bytes() for name, path in paths.items()}
    texts = {name: content.decode("utf-8") for name, content in contents.items()}
    executions = list(csv.DictReader(io.StringIO(texts["execution"], newline="")))
    oracle = _parse_json(texts["oracle"], paths["oracle"])
    truth = _parse_jsonl(texts["truth"], paths["truth"])
    report = score_records(executions, _parse_jsonl(texts["trace"], paths["trace"]), oracle, truth)
    evaluator = Path(__file__).resolve()
    reproduction = [sys.executable, str(evaluator), "--run-dir", str(run_dir.resolve()),
                    "--oracle", str(paths["oracle"].resolve()), "--truth", str(paths["truth"].resolve())]
    report["evaluation_provenance"] = {
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "invocation": "CLI" if command is not None else "API",
        "command": list(command) if command is not None else None,
        "reproduction_command": shlex.join(reproduction),
        "python": platform.python_version(),
        "evaluator_path": str(evaluator),
        "evaluator_sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        "inputs": {name: {"path": str(paths[name].resolve()),
                          "sha256": hashlib.sha256(content).hexdigest()}
                   for name, content in contents.items()},
        "oracle_provenance": oracle.get("provenance"),
        "truth_provenance": [fact["provenance"] for fact in truth if "provenance" in fact],
        "note": "Hashes identify evaluator and exact scored bytes; they do not establish human review or tamper-proof storage.",
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    args = parser.parse_args(argv)
    command = [sys.executable, str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)]
    report = score_run(args.run_dir, args.oracle, args.truth, command=command)
    (args.run_dir / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.run_dir / "evaluation.csv").open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=("event_id", "kind", "correct_action", "completed_without_additional_human", "hold_correct", "reentry_correct", "reason_codes"), extrasaction="ignore")
        writer.writeheader()
        for event in report["events"]:
            writer.writerow({**event, "reason_codes": ";".join(event["reason_codes"])})
    print(json.dumps({"case_id": report["case_id"], "acceptance_passed": report["acceptance_passed"], "issue_count": len(report["issues"]), "evaluation": str(args.run_dir / "evaluation.json")}, ensure_ascii=False))
    return 0 if report["acceptance_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
