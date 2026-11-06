"""Independent oracle tests use hand-written schedules, never solver outputs."""
from __future__ import annotations

import ast
import copy
import csv
import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("independent_score", ROOT / "evaluation" / "score.py")
score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score)


def scenario(case):
    folder = ROOT / "evaluation" / "dev" / case
    oracle = json.loads((folder / "oracle.json").read_text())
    truth = [json.loads(line) for line in (folder / "truth.jsonl").read_text().splitlines()]
    return oracle, truth


def fixtures(case):
    """Feasible examples calculated from documented ticks, not a fixed oracle schedule."""
    oracle, truth = scenario(case)
    rows = [{"execution_id": "initial-weld", "instruction_id": "initial-instruction",
             "plan_id": "initial", "attempt_id": "initial-attempt", "job_id": "W18",
             "robot_id": "R2", "phase": "WELD", "start_tick": -2, "end_tick": 2,
             "from_zone_id": "B", "to_zone_id": "B", "zone_id": "B"}]

    def work(job, robot, origin, destination, start, move, setup, weld):
        cursor = start
        for phase, duration in (("MOVE", move), ("SETUP", setup), ("WELD", weld)):
            rows.append({"execution_id": f"{job}-{phase}", "instruction_id": f"I-{job}-{phase}",
                         "plan_id": "valid-plan", "attempt_id": f"attempt-{job}",
                         "job_id": job, "robot_id": robot, "phase": phase,
                         "start_tick": cursor, "end_tick": cursor + duration,
                         "from_zone_id": origin if phase == "MOVE" else destination,
                         "to_zone_id": destination, "zone_id": destination})
            cursor += duration

    if case in ("D01", "D04"):
        work("W19", "R1", "A", "C", 0, 1, 1, 3)
        work("W21", "R2", "B", "B", 2, 0, 1, 2)
        if case == "D01":
            work("W17", "R1", "C", "A", 6, 1, 1, 4)
            work("W20", "R1", "A", "A", 12, 0, 1, 2)
    elif case == "D02":
        work("W17", "R1", "A", "A", 6, 0, 1, 4)
        work("W20", "R1", "A", "A", 11, 0, 1, 2)
    elif case == "D03":
        work("W19", "R1", "A", "C", 0, 1, 1, 3)
        work("W17", "R2", "B", "A", 2, 1, 1, 4)
        work("W21", "R1", "C", "B", 5, 1, 1, 2)
        work("W20", "R1", "B", "A", 9, 1, 1, 2)
    trace = []

    def log(stage, tick, data, outcome="OK"):
        trace.append({"seq": len(trace) + 1, "run_id": "unit-run", "tick": tick,
                      "received_at": "2026-11-06T00:00:00Z", "stage": stage,
                      "actor": "test", "outcome": outcome,
                      "state_version_before": 0, "state_version_after": 1, "data": data})

    for index, expected in enumerate(oracle["expected_events"]):
        log("EVENT", expected["tick"], {"event": {
            "event_id": f"event-{index}", "kind": expected["kind"],
            "received_tick": expected["tick"],
            "incident_id": "incident-1",
            "restriction_ids": ["fitup-1"] if expected["tick"] == 6 else [],
            "evidence_ids": [{"FITUP_FAILED": "fitup-fail", "REPAIR_REPORTED": "repair", "RECHECK_REPORTED": "recheck", "PAIR_UNREACHABLE": "pair-fail"}[expected["kind"]]],
            "target": {"scope": "JOB_ROBOT" if expected["robot_id"] else "JOB",
                       "job_id": expected["job_id"], "robot_id": expected["robot_id"]}},
            "result": {"status": "ACCEPTED"}})
    if oracle["required_hold"]:
        restriction = {"restriction_id": "fitup-1", "incident_id": "incident-1", "kind": "FITUP_HOLD",
                       "target": {"scope": "JOB", "job_id": "W17", "robot_id": None},
                       "status": "ACTIVE", "opened_tick": 0,
                       "opened_evidence_ids": ["fitup-fail"], "released_tick": None,
                       "released_evidence_ids": []}
        log("RESTRICTION", 0, {"restriction": copy.deepcopy(restriction)})
        if case in ("D01", "D02"):
            restriction.update(status="RELEASED", released_tick=6,
                               released_evidence_ids=["repair", "recheck"])
            log("RESTRICTION", 6, {"restriction": restriction})
    else:
        log("RESTRICTION", 0, {"restriction": {"restriction_id": "pair-1", "kind": "PAIR_BAN",
            "status": "ACTIVE", "opened_tick": 0,
            "target": {"scope": "JOB_ROBOT", "job_id": "W17", "robot_id": "R1"}}})
    if case == "D02":
        log("WAIT", 0, {"robot_id": "R1", "reason_codes": ["FITUP_HOLD", "MATERIAL_NOT_READY"],
                         "recheck_triggers": ["EVIDENCE_ARRIVAL"]})
    if case == "D04":
        log("HUMAN_REQUEST", 6, {"event_id": "event-1", "reason_codes": ["RECHECK_RESULT_REQUIRED"]})
    for row in rows:
        for stage, tick_key in (("PHASE_START", "start_tick"), ("PHASE_END", "end_tick")):
            if stage == "PHASE_START" and row["start_tick"] == -2:
                continue
            log(stage, row[tick_key], {"observation": {
                "execution_id": row["execution_id"], "instruction_id": row["instruction_id"],
                "job_id": row["job_id"], "robot_id": row["robot_id"], "phase": row["phase"],
                "tick": row[tick_key], "from_zone_id": row["from_zone_id"], "to_zone_id": row["to_zone_id"]}})
    return rows, trace, oracle, truth


@pytest.mark.parametrize("case", ["D01", "D02", "D03", "D04"])
def test_manual_feasible_examples_and_human_distinction(case):
    result = score.score_records(*fixtures(case))
    assert result["acceptance_passed"], result["issues"]
    assert result["completed_without_additional_human"] is (case != "D04")
    assert result["main_experiment"] is False
    assert result["deadline_applied"] is False
    assert result["oracle_review_status"] == "INDEPENDENT_AGENT_DRAFT_HUMAN_TEAM_REVIEW_PENDING"


def test_w17_repair_is_not_actual_predecessor_completion():
    rows, trace, oracle, truth = fixtures("D01")
    for row in rows:
        if row["job_id"] == "W20":
            row["start_tick"] -= 6
            row["end_tick"] -= 6
    result = score.score_records(rows, trace, oracle, truth)
    assert not result["acceptance_passed"]
    assert "ACTUAL_PREDECESSOR_INCOMPLETE" in {x["code"] for x in result["issues"]}


def test_checker_pass_does_not_hide_execution_violation():
    rows, trace, oracle, truth = fixtures("D03")
    for row in rows:
        if row["job_id"] == "W17":
            row["robot_id"] = "R1"
    trace.append({"stage": "CHECK", "tick": 0, "data": {"check": {"plan_id": "p", "passed": True}}})
    result = score.score_records(rows, trace, oracle, truth)
    assert result["executed_violation_count"] > 0
    assert "FORBIDDEN_PAIR_STARTED" in {x["code"] for x in result["issues"]}


def test_starts_without_end_are_not_hidden_by_end_only_export():
    rows, trace, oracle, truth = fixtures("D04")
    trace.append({"seq": 99, "stage": "PHASE_START", "tick": 7,
                  "data": {"observation": {"execution_id": "bad-start", "job_id": "W17"}}})
    result = score.score_records(rows, trace, oracle, truth)
    assert not result["acceptance_passed"]
    assert any(i["code"] == "START_WITHOUT_END" for i in result["issues"])


def test_repair_without_recheck_does_not_release_hold():
    rows, trace, oracle, truth = fixtures("D04")
    trace.append({"seq": 98, "tick": 6, "stage": "RESTRICTION", "data": {"restriction": {
        "restriction_id": "fitup-1", "kind": "FITUP_HOLD", "target": {"scope": "JOB", "job_id": "W17"},
        "status": "RELEASED", "opened_tick": 0, "released_tick": 6,
        "opened_evidence_ids": ["fail"], "released_evidence_ids": ["repair"]}}})
    result = score.score_records(rows, trace, oracle, truth)
    assert result["hold_correct"] is False


def test_fabricated_release_evidence_cannot_pass_two_id_count():
    rows, trace, oracle, truth = fixtures("D01")
    for log in trace:
        if log["stage"] == "RESTRICTION" and log["data"]["restriction"]["status"] == "RELEASED":
            log["data"]["restriction"]["released_evidence_ids"] = ["invented-repair", "invented-recheck"]
    result = score.score_records(rows, trace, oracle, truth)
    assert not result["hold_correct"]


def test_fitup_hold_scope_must_cover_every_robot():
    rows, trace, oracle, truth = fixtures("D01")
    for log in trace:
        if log["stage"] == "RESTRICTION":
            log["data"]["restriction"]["target"].update(scope="JOB_ROBOT", robot_id="R1")
    result = score.score_records(rows, trace, oracle, truth)
    assert not result["hold_correct"]


def test_d02_timeout_wait_does_not_count_as_justified_wait():
    rows, trace, oracle, truth = fixtures("D02")
    for entry in trace:
        if entry["stage"] == "WAIT":
            entry["data"]["reason_codes"] = ["NO_SOLUTION_WITHIN_LIMIT"]
    result = score.score_records(rows, trace, oracle, truth)
    assert "JUSTIFIED_WAIT_MISSING" in {x["code"] for x in result["issues"]}


def test_candidate_violation_and_checker_block_are_separate_from_execution():
    rows, trace, oracle, truth = fixtures("D01")
    bad = {"plan_id": "bad-candidate", "assignments": [{"job_id": "W17", "robot_id": "R1", "source_position": "A",
        "move_start": 0, "move_end": 0, "setup_start": 0, "setup_end": 1, "weld_start": 1, "weld_end": 5}], "fixed_segments": []}
    trace.extend([
        {"stage": "PLAN", "seq": 80, "tick": 0, "data": {"plan": bad}},
        {"stage": "CHECK", "seq": 81, "tick": 0, "data": {"check": {"plan_id": "bad-candidate", "passed": False}}},
    ])
    result = score.score_records(rows, trace, oracle, truth)
    assert result["proposed_violation_count"] == 1
    assert result["blocked_violation_count"] == 1
    assert result["executed_violation_count"] == 0
    assert result["acceptance_passed"]


def test_missing_event_does_not_shrink_denominator():
    rows, trace, oracle, truth = fixtures("D01")
    trace = [t for t in trace if t["stage"] != "EVENT"]
    result = score.score_records(rows, trace, oracle, truth)
    assert result["evaluated_event_count"] == 3
    assert result["autonomous_correct_count"] == 0
    assert not result["acceptance_passed"]


def test_evaluator_reads_raw_exports_not_summary(tmp_path):
    rows, trace, oracle, truth = fixtures("D02")
    with (tmp_path / "execution.csv").open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (tmp_path / "trace.jsonl").write_text("".join(json.dumps(t) + "\n" for t in trace))
    (tmp_path / "summary.json").write_text('{"acceptance_passed": false, "completed_job_ids": []}')
    path = ROOT / "evaluation" / "dev" / "D02"
    result = score.score_run(tmp_path, path / "oracle.json", path / "truth.jsonl")
    assert result["acceptance_passed"]
    provenance = result["evaluation_provenance"]
    assert provenance["invocation"] == "API"
    assert provenance["command"] is None
    assert datetime.fromisoformat(provenance["evaluated_at_utc"]).utcoffset().total_seconds() == 0
    assert provenance["evaluator_sha256"] == hashlib.sha256((ROOT / "evaluation/score.py").read_bytes()).hexdigest()
    inputs = {"execution": tmp_path / "execution.csv", "trace": tmp_path / "trace.jsonl",
              "oracle": path / "oracle.json", "truth": path / "truth.jsonl"}
    for name, input_path in inputs.items():
        assert provenance["inputs"][name]["sha256"] == hashlib.sha256(input_path.read_bytes()).hexdigest()
    assert provenance["oracle_provenance"]["review_status"] == "INDEPENDENT_AGENT_DRAFT_HUMAN_TEAM_REVIEW_PENDING"
    assert score.main(["--run-dir", str(tmp_path), "--oracle", str(path / "oracle.json"), "--truth", str(path / "truth.jsonl")]) == 0
    assert (tmp_path / "evaluation.json").exists()
    assert (tmp_path / "evaluation.csv").exists()
    cli_result = json.loads((tmp_path / "evaluation.json").read_text())
    assert cli_result["evaluation_provenance"]["invocation"] == "CLI"
    assert "--run-dir" in cli_result["evaluation_provenance"]["command"]
    assert cli_result["evaluation_provenance"]["inputs"] == provenance["inputs"]
    # A semantically equivalent byte change is still a different exported input.
    with (tmp_path / "trace.jsonl").open("a") as target:
        target.write("\n")
    changed = score.score_run(tmp_path, path / "oracle.json", path / "truth.jsonl")
    assert changed["acceptance_passed"]
    assert changed["evaluation_provenance"]["inputs"]["trace"]["sha256"] != provenance["inputs"]["trace"]["sha256"]


def test_fabricated_csv_completion_requires_actual_trace():
    rows, trace, oracle, truth = fixtures("D01")
    trace = [t for t in trace if not (t["stage"] == "PHASE_END" and t["data"]["observation"]["job_id"] == "W17")]
    result = score.score_records(rows, trace, oracle, truth)
    assert not result["acceptance_passed"]
    assert any(i["code"] == "CSV_TRACE_MISMATCH" for i in result["issues"])


def test_evaluator_does_not_import_operational_modules():
    tree = ast.parse((ROOT / "evaluation" / "score.py").read_text())
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    assert not any(m.startswith("shipyard") for m in modules)
