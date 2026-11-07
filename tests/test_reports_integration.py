"""Fake report interpretation joins the existing public execution and scoring path."""
import ast
import csv
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evaluation.score import score_run
from shipyard import checker, cli, scheduler
from shipyard.contracts import Delivery, Policy, canonical
from shipyard.controller import Controller
from shipyard.reports import FakeReportModel, ReportDelivery, ReportInput
from shipyard.repository import Repository


ROOT = Path(__file__).resolve().parents[1]
EXECUTION_FIELDS = (
    "job_id", "robot_id", "phase", "start_tick", "end_tick",
    "from_zone_id", "to_zone_id", "zone_id",
)


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def executions(path):
    with (path / "execution.csv").open(newline="") as source:
        rows = list(csv.DictReader(source))
    assert len({row["execution_id"] for row in rows}) == len(rows)
    return sorted(tuple(row[key] for key in EXECUTION_FIELDS) for row in rows)


def arrived_report(run_id, case="D01", report_id="E-fitup-W17"):
    delivery = next(Delivery.model_validate(item) for item in
                    read_jsonl(ROOT / "simulation/dev" / case / "events.jsonl")
                    if item["event"]["event_id"] == report_id)
    record = next(item for item in read_jsonl(ROOT / "simulation/reports" / f"{case}.jsonl")
                  if item["report_id"] == report_id)
    event = delivery.event
    report = ReportDelivery(
        report=ReportInput(
            run_id=run_id, report_id=report_id, incident_id=event.incident_id,
            source_id=event.source_id, observed_tick=event.observed_tick,
            received_tick=delivery.delivery_tick, text=record["text"],
            target_hint=record["target_hint"],
        ),
        published_evidence=delivery.published_evidence,
    )
    return report, record["responses"]


@pytest.fixture
def report_controller(tmp_path):
    repository = Repository(tmp_path / "report-run.sqlite3")
    repository.initialize(cli.load_public_case(ROOT, "D01", "report-integration-run"))
    yield Controller(repository, Policy())
    repository.close()


@pytest.mark.parametrize("case", ["D01", "D02", "D03", "D04"])
def test_fake_reports_preserve_structured_execution_and_independent_score(tmp_path, monkeypatch, case):
    calls = {mode: {"solver": 0, "checker": 0} for mode in ("structured", "fake")}
    mode = "structured"
    original_solve, original_check = scheduler.solve, checker.check_plan

    def solve(snapshot, constraints, policy):
        calls[mode]["solver"] += 1
        return original_solve(snapshot, constraints, policy)

    def check(snapshot, plan, policy):
        calls[mode]["checker"] += 1
        return original_check(snapshot, plan, policy)

    monkeypatch.setattr(scheduler, "solve", solve)
    monkeypatch.setattr(checker, "check_plan", check)
    structured = tmp_path / "structured"
    expected = cli.run_case(case, structured)
    mode = "fake"
    fake = tmp_path / "fake"
    observed = cli.run_case(case, fake, report_mode="fake")

    assert executions(fake) == executions(structured)
    assert calls["fake"] == calls["structured"]
    assert calls["fake"]["solver"] > 0 and calls["fake"]["checker"] > 0
    for key in ("termination", "now_tick", "completed_jobs", "additional_human_requests", "failures"):
        assert observed[key] == expected[key], key
    oracle = ROOT / "evaluation/dev" / case / "oracle.json"
    truth = ROOT / "evaluation/dev" / case / "truth.jsonl"
    scores = [score_run(path, oracle, truth) for path in (structured, fake)]
    assert all(score["acceptance_passed"] for score in scores), scores
    for key in ("correct_action", "completed_without_additional_human", "hold_correct",
                "reentry_correct", "evaluated_event_count", "autonomous_correct_count",
                "additional_human_requests", "proposed_violation_count", "blocked_violation_count",
                "executed_violation_count", "completed_job_ids", "issues",
                "total_weighted_tardiness_completed_jobs"):
        assert scores[0][key] == scores[1][key], key

    trace = read_jsonl(fake / "trace.jsonl")
    results = [item for item in trace if item["stage"] == "REPORT_RESULT"]
    records = read_jsonl(ROOT / "simulation/reports" / f"{case}.jsonl")
    assert {item["data"]["report_id"] for item in results} == {item["report_id"] for item in records}
    assert len(results) == len(records)
    assert all(item["outcome"] == "READY" for item in results)
    assert all(item["data"]["model_calls"] == 2 and item["data"]["lookup_count"] == 1 for item in results)
    assert observed["reports"] == {
        "count": len(records), "statuses": {"READY": len(records)},
        "model_calls": len(records) * 2, "lookups": len(records),
    }
    assert json.loads((fake / "summary.json").read_text())["reports"] == observed["reports"]
    assert "reports" not in expected
    assert "reports" not in json.loads((structured / "summary.json").read_text())
    for result in results:
        report_id = result["data"]["report_id"]
        event = next(item for item in trace if item["stage"] == "EVENT"
                     and item["data"]["event"]["event_id"] == report_id)
        assert event["data"]["event"]["raw_report"] == next(
            record["text"] for record in records if record["report_id"] == report_id)
        assert event["state_version_after"] == result["state_version_after"]
    if case in {"D01", "D02"}:
        recovery_results = [item for item in results if item["tick"] == 6]
        assert len(recovery_results) == 2
        assert len({item["state_version_after"] for item in recovery_results}) == 1
        assert not any(item["stage"] == "HUMAN_REQUEST" for item in trace)


def test_report_adapter_cannot_import_state_writers_private_driver_or_evaluator():
    tree = ast.parse((ROOT / "src/shipyard/reports.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                imported.update(name.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            imported.update((node.module or "").split("."))
            imported.update(name.name for name in node.names)
    forbidden = {"simulator", "evaluation", "repository", "controller", "cli"}
    assert not imported & forbidden, imported & forbidden


def test_future_reports_scripts_and_evaluation_files_do_not_reach_model_or_solver(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    for folder in ("data", "simulation", "config", "src", "evaluation"):
        shutil.copytree(ROOT / folder, root / folder, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, root / name)
    path = root / "simulation/reports/D01.jsonl"
    records = read_jsonl(path)
    for record in records:
        if record["delivery_tick"] == 6:
            record["text"] += " FUTURE_REPORT_SENTINEL_13c7"
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n")
    (root / "evaluation/dev/D01/oracle.json").write_text('{"secret":"ORACLE_SENTINEL_8e32"}')
    (root / "evaluation/dev/D01/truth.jsonl").write_text('{"secret":"TRUTH_SENTINEL_6a11"}\n')
    arrivals = {item["report_id"]: item["delivery_tick"] for item in records}
    model_requests, solver_requests = [], []
    original_open, original_solve = Path.open, scheduler.solve

    class ObservedFakeModel(FakeReportModel):
        def respond(self, request):
            encoded = canonical(request)
            assert "ORACLE_SENTINEL" not in encoded and "TRUTH_SENTINEL" not in encoded
            assert all(arrivals[report_id] <= request["now_tick"] for report_id in self._scripts)
            assert set(request) == {"system", "prompt_version", "report", "state_version",
                                    "now_tick", "known_ids", "allowed_event_kinds", "lookup_result"}
            if request["now_tick"] < 6:
                assert "FUTURE_REPORT_SENTINEL" not in encoded
                assert "EV-repair-W17" not in encoded and "EV-recheck-W17" not in encoded
                assert "E-repair-W17" not in self._scripts and "E-recheck-W17" not in self._scripts
            model_requests.append(json.loads(encoded))
            return super().respond(request)

    def solve(snapshot, constraints, policy):
        encoded = canonical(snapshot) + canonical(constraints)
        assert "ORACLE_SENTINEL" not in encoded and "TRUTH_SENTINEL" not in encoded
        assert all(e.available_tick <= snapshot.now_tick for e in snapshot.evidence)
        if snapshot.now_tick < 6:
            assert "FUTURE_REPORT_SENTINEL" not in encoded
            assert "EV-repair-W17" not in encoded and "EV-recheck-W17" not in encoded
        solver_requests.append(snapshot.now_tick)
        return original_solve(snapshot, constraints, policy)

    def guarded_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if "r" in mode and "evaluation" in path.parts:
            raise AssertionError(f"runtime read evaluator-only data: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(cli, "FakeReportModel", ObservedFakeModel)
    monkeypatch.setattr(scheduler, "solve", solve)
    monkeypatch.setattr(Path, "open", guarded_open)
    summary = cli.run_case("D01", tmp_path / "output", root, report_mode="fake")
    assert len(summary["completed_jobs"]) == 5
    assert any(request["now_tick"] < 6 for request in model_requests)
    assert any(request["now_tick"] >= 6 and "FUTURE_REPORT_SENTINEL" in canonical(request)
               for request in model_requests)
    assert any(tick < 6 for tick in solver_requests) and any(tick >= 6 for tick in solver_requests)


def test_duplicate_report_reuses_receipt_without_model_solver_or_state_change(report_controller, monkeypatch):
    controller = report_controller
    repository = controller.repository
    item, responses = arrived_report(repository.read_snapshot().run_id)
    model = FakeReportModel({item.report.report_id: responses})
    assert controller.handle_report_batch(0, [], [item, item.model_copy(deep=True)], model) == "OPTIMAL"
    assert len(model.requests) == 2
    before = repository.read_snapshot()
    receipts = repository.report_receipts()
    event_count = repository._conn.execute("SELECT count(*) FROM events").fetchone()[0]

    def forbidden(*args, **kwargs):
        raise AssertionError("duplicate report repeated interpretation or replanning")

    monkeypatch.setattr(model, "respond", forbidden)
    monkeypatch.setattr(scheduler, "solve", forbidden)
    assert controller.handle_report_batch(0, [], [item], model) == "DUPLICATE"
    assert repository.read_snapshot() == before
    assert repository.report_receipts() == receipts
    assert repository._conn.execute("SELECT count(*) FROM events").fetchone()[0] == event_count == 1
    assert len(before.active_restrictions) == 1
    assert repository.audit_records()[-1]["outcome"] == "DUPLICATE"


@pytest.mark.parametrize("previously_committed", [False, True])
def test_conflicting_report_id_is_quarantined_without_new_event_or_state(report_controller, monkeypatch, previously_committed):
    controller = report_controller
    repository = controller.repository
    item, responses = arrived_report(repository.read_snapshot().run_id)
    model = FakeReportModel({item.report.report_id: responses})
    if previously_committed:
        controller.handle_report_batch(0, [], [item], model)
    before = repository.read_snapshot()
    original_receipts = repository.report_receipts()
    original_events = repository._conn.execute("SELECT count(*) FROM events").fetchone()[0]
    conflict = item.model_copy(deep=True)
    conflict.report.text = "W17 핏업이 정상입니다. 기존 보고와 다른 내용입니다."

    def forbidden(*args, **kwargs):
        raise AssertionError("conflicting report must not invoke the model or solver")

    monkeypatch.setattr(model, "respond", forbidden)
    monkeypatch.setattr(scheduler, "solve", forbidden)
    assert controller.handle_report_batch(
        0, [], [conflict] if previously_committed else [item, conflict], model
    ) == "REPORT_REJECTED"
    assert repository.read_snapshot() == before
    assert repository._conn.execute("SELECT count(*) FROM events").fetchone()[0] == original_events
    if previously_committed:
        assert repository.report_receipts() == original_receipts
    else:
        assert repository.report_receipts()[item.report.report_id]["status"] == "ID_CONFLICT"
    assert any(row["outcome"] == "ID_CONFLICT" for row in repository.audit_records())
    assert any(row["stage"] == "HUMAN_REQUEST" and row["data"]["reason_codes"] == ["REPORT_ID_REUSED"]
               for row in repository.audit_records())


def test_state_change_during_interpretation_discards_proposal_and_uses_public_fallback(report_controller):
    controller = report_controller
    repository = controller.repository
    item, responses = arrived_report(repository.read_snapshot().run_id)

    class RacingModel(FakeReportModel):
        def respond(self, request):
            if not self.requests:
                changed = repository.read_snapshot()
                changed.jobs[2].due_tick += 1
                repository.commit_snapshot(changed, changed.state_version, "TEST_CONCURRENT_CHANGE")
            return super().respond(request)

    model = RacingModel({item.report.report_id: responses})
    assert controller.handle_report_batch(0, [], [item], model) == "OPTIMAL"
    receipt = repository.report_receipts()[item.report.report_id]
    assert receipt["status"] == "STALE_CONTEXT"
    assert receipt["based_on_state_version"] == 0
    assert receipt["reason_codes"] == ["STATE_CHANGED_DURING_INTERPRETATION"]
    fallback = receipt["delivery"]
    assert fallback["event"]["kind"] == "IDENTITY_UNCERTAIN"
    assert fallback["event"]["observed_state_version"] == 1
    assert fallback["event"]["target"] == item.report.target_hint.model_dump()
    assert fallback["published_evidence"] == []
    snapshot = repository.read_snapshot()
    assert [(r.kind, r.target.job_id) for r in snapshot.active_restrictions] == [("EVIDENCE_PENDING", "W17")]
    assert not snapshot.evidence
    assert not any(i.job_id in {"W17", "W20"} and i.status == "PENDING" for i in snapshot.current_instructions)
    assert any(row["stage"] == "HUMAN_REQUEST" for row in repository.audit_records())


def test_report_result_event_and_state_rollback_together_and_retry_is_possible(report_controller, monkeypatch):
    controller = report_controller
    repository = controller.repository
    item, responses = arrived_report(repository.read_snapshot().run_id)
    before, audit_before = repository.read_snapshot(), repository.audit_records()
    original_audit = repository._audit

    def fail_after_report_result(snapshot, stage, *args, **kwargs):
        original_audit(snapshot, stage, *args, **kwargs)
        if stage == "REPORT_RESULT":
            raise RuntimeError("injected failure after report and event rows")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "_audit", fail_after_report_result)
        with pytest.raises(RuntimeError, match="injected failure"):
            controller.handle_report_batch(0, [], [item], FakeReportModel({item.report.report_id: responses}))
    assert repository.read_snapshot() == before
    assert repository.audit_records() == audit_before
    assert repository.report_receipts() == {}
    for table in ("events", "event_receipts", "evidence", "plans", "checks"):
        assert repository._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert controller.handle_report_batch(0, [], [item], FakeReportModel({item.report.report_id: responses})) == "OPTIMAL"
    assert repository.report_receipts()[item.report.report_id]["status"] == "READY"
    assert len(repository.read_snapshot().active_restrictions) == 1


def test_cli_fake_mode_records_reproduction_inputs_and_default_remains_structured(tmp_path, capsys):
    fake = tmp_path / "fake-cli"
    assert cli.main(["run", "--case", "D01", "--output", str(fake), "--report-mode", "fake"]) == 0
    assert json.loads(capsys.readouterr().out)["termination"] == "COMPLETED"
    manifest = json.loads((fake / "manifest.json").read_text())
    assert manifest["report_mode"] == "fake"
    assert manifest["report_adapter"] == {"prompt_version": "report-v1", "max_model_calls": 2, "max_lookups": 1}
    relative = "simulation/reports/D01.jsonl"
    assert manifest["inputs"][relative] == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    assert "--report-mode fake" in manifest["command"]
    default = tmp_path / "default-cli"
    assert cli.main(["run", "--case", "D01", "--output", str(default)]) == 0
    capsys.readouterr()
    default_manifest = json.loads((default / "manifest.json").read_text())
    assert default_manifest["report_mode"] == "structured" and default_manifest["report_adapter"] is None
    assert relative not in default_manifest["inputs"]
    assert not any(row["stage"].startswith("REPORT") for row in read_jsonl(default / "trace.jsonl"))
    assert executions(fake) == executions(default)
