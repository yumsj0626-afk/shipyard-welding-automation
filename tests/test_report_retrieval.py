"""The shared outcome store: runs record results, later runs retrieve them."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shipyard import cli, responses
from shipyard.history import HistoryRetriever, OutcomeStore
from shipyard.report_retrieval import ReportAnalysis

ROOT = Path(__file__).resolve().parents[1]
INPUT = "접합부 조립 치수가 맞지 않아 본작업을 멈췄습니다."
FITUP_IDS = ("KR-2026-2-2-304-BODY", "KR-2026-2-2-402-BODY", "KR-2026-2-2-407-JOINT", "KR-2026-2-2-GUIDE-407-BODY")
CONSUMABLE_ID = "KR-2026-2-2-303-BODY"
# The fixtures mimic the 18-row excerpt, so they use its topics, not the full-Part-2 default.
CURATED_TOPICS = ROOT / "config/rule-topics.curated.json"
POLICY = "SYNTHETIC_DEV_ONLY"


def fake_sdk(monkeypatch):
    openai = pytest.importorskip("openai", reason="install the llm extra for offline SDK tests")
    sdk = MagicMock()
    client = sdk.return_value.__enter__.return_value
    monkeypatch.setattr(openai, "OpenAI", sdk)
    return sdk, client


def embedding_result(vectors):
    return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=vector)
                                 for i, vector in reversed(list(enumerate(vectors)))])


class Embeddings:
    """Deterministic vectors keyed by text; counts how many texts were embedded."""
    def __init__(self, table, default=(0.0, 1.0)):
        self.table, self.default, self.texts = table, default, []

    def embed(self, model, texts):
        self.texts.extend(texts)
        return [list(self.table.get(text, self.default)) for text in texts]


@pytest.fixture
def rule_file(tmp_path):
    path = tmp_path / "rules.jsonl"
    rows = [
        {"id": ref, "clause": clause, "title": title, "keywords": title, "text": title,
         "source": "한국선급 테스트 발췌", "edition": 2026,
         "pdf_pages": [number], "printed_pages": [number - 5]}
        for ref, clause, title, number in zip(
            (*FITUP_IDS, CONSUMABLE_ID), ("304", "402", "407", "407", "303"),
            ("용접준비", "용접절차 시방서", "루트 간격", "관련 적용지침 루트", "용접용재료"), (130, 133, 152, 271, 128),
            strict=True,
        )
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return path


def outcome(report_id, text, kind="FITUP_FAILED"):
    return {"report_id": report_id, "incident_id": "INC-1", "observed_tick": 0, "received_tick": 0, "text": text,
            "target": {"scope": "JOB", "job_id": "W17"}, "adapter_status": "READY", "reason_codes": [],
            "event_kind": kind, "event_outcome": "APPLIED", "evidence_ids": ["EV-1"], "rules_retrieved": [],
            "rule_refs": [], "job_id": "W17", "job_completed": True, "job_weld_end_tick": 12,
            "restrictions": [{"restriction_id": "r1", "kind": "FITUP_HOLD", "opened_by_this_report": True,
                              "released_by_this_report": False, "opened_tick": 0, "released_tick": 6,
                              "status_at_run_end": "RELEASED", "hold_ticks": 6}]}


def seed(store, run_id, case_id, finished, rows, *, data_policy=POLICY):
    store.add_outcomes(rows, run_id=run_id, case_id=case_id, data_policy=data_policy,
                       started_at=finished, finished_at=finished, termination="COMPLETED")


def test_fake_run_records_report_outcomes(tmp_path, rule_file):
    history = tmp_path / "history.sqlite3"
    run = tmp_path / "run"
    summary = cli.run_case("D01", run, ROOT, report_mode="fake", rules_path=rule_file, history_path=history,
                           rule_topics_path=CURATED_TOPICS)
    assert summary["termination"] == "COMPLETED"
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["history"] == {"store": "history.sqlite3", "recorded_outcomes": 3, "recording_error": None,
                                   "retrieval": None}
    assert manifest["rule_knowledge"]["topics_file"] == "rule-topics.curated.json"
    store = OutcomeStore(history)
    try:
        rows = {row["outcome"]["event_kind"]: row
                for row in store.eligible(before="9999", data_policy=POLICY)}
        raw = store._conn.execute(
            "SELECT event_kind, rules_retrieved_json, restrictions_json, job_completed, job_weld_end_tick "
            "FROM report_outcomes ORDER BY received_tick, report_id").fetchall()
    finally:
        store.close()
    assert set(rows) == {"FITUP_FAILED", "REPAIR_REPORTED", "RECHECK_REPORTED"}
    hold = rows["FITUP_FAILED"]["outcome"]["restrictions"]
    assert hold == [{"kind": "FITUP_HOLD", "opened_tick": 0, "released_tick": 6, "hold_ticks": 6,
                     "status_at_run_end": "RELEASED"}]
    assert rows["RECHECK_REPORTED"]["outcome"]["restrictions"][0]["hold_ticks"] == 6
    assert all(row["outcome"]["job_completed"] for row in rows.values())
    assert all(row["case_id"] == "D01" for row in rows.values())
    # The agent's FITUP_RESULT lookup selected the fit-up rule topic.
    assert sorted(json.loads(raw[0][1])) == sorted(FITUP_IDS)
    assert raw[0][3:] == (1, 12)


def test_retrieval_only_sees_earlier_runs_and_other_cases(tmp_path):
    store = OutcomeStore(tmp_path / "history.sqlite3")
    try:
        seed(store, "run-other", "D02", "2026-01-01T00:00:00+00:00", [outcome("A", "판 간격 불량")])
        seed(store, "run-same", "D01", "2026-01-01T00:00:00+00:00", [outcome("B", "핏업 불합격")])
        seed(store, "run-later", "D03", "2026-12-31T00:00:00+00:00", [outcome("C", "미래 결과")])
        seed(store, "run-real", "D04", "2026-01-01T00:00:00+00:00", [outcome("D", "실데이터")],
             data_policy="PRODUCTION")
        seed(store, "run-now", "D05", "2026-01-01T00:00:00+00:00", [outcome("E", "현재 실행")])
        client = Embeddings({"판 간격 불량": (1, 0), "핏업 불합격": (1, 0.1), "질의": (1, 0)})
        cutoff = "2026-06-01T00:00:00+00:00"
        retriever = HistoryRetriever(store, client, "embed-test", before=cutoff, data_policy=POLICY,
                                     exclude_run_id="run-now", exclude_case_id="D01", top_k=5)
        found = retriever.search("질의")
        assert [row["id"] for row in found] == ["run-other:A"]
        assert found[0]["outcome"]["event_kind"] == "FITUP_FAILED"
        assert found[0]["outcome"]["restrictions"][0]["hold_ticks"] == 6
        assert retriever.describe()["eligible_outcomes"] == 1
        assert client.texts == ["질의", "판 간격 불량"]

        # Cached vectors are reused; only the query is embedded again.
        both = HistoryRetriever(store, client, "embed-test", before=cutoff, data_policy=POLICY,
                                exclude_run_id="run-now", top_k=5)
        client.texts.clear()
        assert [row["id"] for row in both.search("질의")] == ["run-other:A", "run-same:B"]
        assert client.texts == ["질의", "핏업 불합격"]
        empty = HistoryRetriever(store, client, "embed-test", before="2000-01-01", data_policy=POLICY)
        client.texts.clear()
        assert empty.search("질의") == [] and client.texts == []
    finally:
        store.close()


def test_analyze_report_reads_the_outcome_store(tmp_path, monkeypatch, capsys, rule_file):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_PROJECT_ID", raising=False)
    (tmp_path / ".env").write_text(
        'OPENAI_API_KEY="sk-test-file"\nOPENAI_PROJECT_ID=proj-history-test\n', encoding="utf-8-sig",
    )
    history = tmp_path / "history.sqlite3"
    report = tmp_path / "incoming.txt"
    report.write_text(INPUT, encoding="utf-8")
    analyze = ["analyze-report", "--history", str(history), "--report-file", str(report), "--top-k", "1",
               "--allow-paid", "--project-id", "proj-explicit", "--rules", str(rule_file),
               "--rule-topics", str(CURATED_TOPICS), "--rule-search", "keyword"]
    assert cli.main(analyze) == 2
    assert "No outcome store" in capsys.readouterr().err

    store = OutcomeStore(history)
    seed(store, "run-1", "D02", "2026-01-01T00:00:00+00:00",
         [outcome("CASE-1", "판 사이 간격이 허용값을 넘어서 용접을 보류했다."),
          outcome("CASE-2", "장비 메인 전원이 차단됐다.", kind="ROBOT_AVAILABILITY_CHANGED")])
    store.close()
    vectors = {"판 사이 간격이 허용값을 넘어서 용접을 보류했다.": [4, 0], "장비 메인 전원이 차단됐다.": [0, 1],
               INPUT: [1, 0], "UNRELATEDXYZ": [1, 0]}
    sdk, client = fake_sdk(monkeypatch)
    client.embeddings.create.side_effect = lambda **kwargs: embedding_result(
        [vectors[text] for text in kwargs["input"]])
    cited = {"outcome": "run-1:CASE-1", "rule": FITUP_IDS[2], "expect": ["run-1:CASE-1"]}

    def generate(**kwargs):
        context = json.loads(kwargs["input"])
        assert context["report_text"] == INPUT
        assert [row["id"] for row in context["similar_reports"]] == cited["expect"]
        assert all(row["outcome"]["restrictions"][0]["hold_ticks"] == 6 for row in context["similar_reports"])
        assert sorted(row["id"] for row in context["rule_excerpts"]) == sorted(FITUP_IDS)
        assert kwargs["text_format"] is ReportAnalysis and kwargs["store"] is False
        return SimpleNamespace(status="completed", output_parsed=ReportAnalysis(
            decision="INSUFFICIENT_BASIS", assessment="조립 치수 이상이 보고됐습니다.",
            checks_needed=["현재 이음부 치수 확인"], similar_report_ids=[cited["outcome"]],
            rule_basis=[{"rule_id": cited["rule"], "application": "해당 이음의 허용오차 기준을 확인해야 합니다."}],
        ))

    client.responses.parse.side_effect = generate
    assert cli.main([arg for arg in analyze if arg != "--allow-paid"]) == 2
    assert "allow-paid" in capsys.readouterr().err
    sdk.assert_not_called()
    assert cli.main(analyze) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["analysis"]["similar_report_ids"] == ["run-1:CASE-1"]
    assert result["similar_reports"][0]["score"] == 1.0
    assert {call.kwargs["project"] for call in sdk.call_args_list} == {"proj-explicit"}

    cited["outcome"] = "run-1:CASE-2"
    assert cli.main(analyze) == 2
    assert "unretrieved past outcome" in capsys.readouterr().err
    cited.update(outcome="run-1:CASE-1", rule=CONSUMABLE_ID)
    assert cli.main(analyze) == 2
    assert "unretrieved Korean Register rule" in capsys.readouterr().err
    cited["expect"] = []
    assert cli.main([*analyze, "--exclude-case", "D02"]) == 2
    assert "unretrieved past outcome" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["D01", "QA-EXTRA"])
def test_responses_run_receives_earlier_outcomes_and_records_its_own(tmp_path, monkeypatch, rule_file, case):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_PROJECT_ID", raising=False)
    (project / ".env").write_text(
        "OPENAI_API_KEY=sk-test-placeholder\nOPENAI_PROJECT_ID=proj-approved\n", encoding="utf-8",
    )
    for name in ("config", "data", "simulation", "src"):
        shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, project / name)
    source_reports = project / "simulation/reports/D01.jsonl"
    records = [json.loads(line) for line in source_reports.read_text(encoding="utf-8").splitlines()]
    templates = {row["report_id"]: row["responses"] for row in records}
    if case != "D01":
        initial = project / "data/dev" / case
        initial.mkdir()
        for name in ("initial.json", "evidence.json"):
            payload = json.loads((project / "data/dev/D01" / name).read_text(encoding="utf-8"))
            payload["case_id"] = case
            (initial / name).write_text(json.dumps(payload), encoding="utf-8")
        shutil.copytree(project / "simulation/dev/D01", project / "simulation/dev" / case)
        for record in records:
            del record["responses"]
        (source_reports.parent / f"{case}.jsonl").write_text(
            "\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8",
        )
        evaluation = project / "evaluation"
        answer_dir = evaluation / "dev" / case
        answer_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "evaluation/score.py", evaluation / "score.py")
        for name in ("oracle.json", "truth.jsonl"):
            contents = (ROOT / "evaluation/dev/D01" / name).read_text(encoding="utf-8")
            (answer_dir / name).write_text(contents.replace('"D01"', json.dumps(case)), encoding="utf-8")

    history = cli.default_history_path(project)
    store = OutcomeStore(history)
    seed(store, "earlier-other", "D02", "2026-01-01T00:00:00+00:00",
         [outcome("P1", "판 간격 불량으로 보류"), outcome("P2", "와이어 미입고", kind="MATERIAL_READINESS_CHANGED")])
    seed(store, "earlier-same", case, "2026-01-01T00:00:00+00:00", [outcome("S1", "같은 사례의 이전 결과")])
    store.close()
    _, client = fake_sdk(monkeypatch)
    client.embeddings.create.side_effect = lambda **kwargs: embedding_result(
        [[1, i + 1] for i, _ in enumerate(kwargs["input"])])

    topics = ["--rules", str(rule_file), "--rule-topics", str(project / "config/rule-topics.curated.json")]
    assert cli.main(["prepare-case-policy", "--case", case, *topics]) == 0
    policy_path = cli.case_policy_path(project, case)
    assert not responses.load_policy(policy_path).history_retrieval_approved
    run = tmp_path / "run"
    with pytest.raises(ValueError, match="PAID_CALLS_NOT_APPROVED"):
        cli.run_case(case, run, project, report_mode="responses", history_path=history, rules_path=rule_file,
                     llm_policy_path=policy_path, allow_paid=True, rule_topics_path=project / "config/rule-topics.curated.json")
    policy = responses.load_policy(policy_path)
    policy.approved_for_paid_calls = True
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="past report outcomes is not approved"):
        cli.run_case(case, run, project, report_mode="responses", history_path=history, rules_path=rule_file,
                     llm_policy_path=policy_path, allow_paid=True, rule_topics_path=project / "config/rule-topics.curated.json")
    assert not run.exists()
    policy_path.unlink()
    assert cli.main(["prepare-case-policy", "--case", case, "--approve", *topics]) == 0
    prepared = responses.load_policy(policy_path)
    assert prepared.history_retrieval_approved and prepared.max_reports_per_run == len(records)
    assert client.embeddings.create.call_count == 0
    seen = []

    class MockResponses(responses.ResponsesReportModel):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            assert self.policy.project_id == "proj-approved"
            self._runner = SimpleNamespace(run=lambda value: value, close=lambda: None)

        def _respond(self, report, state, payload):
            context = json.loads(payload["input"][0]["content"])
            assert {row["id"] for row in context["similar_reports"]} == {"earlier-other:P1", "earlier-other:P2"}
            assert all("event_kind" in row["outcome"] for row in context["similar_reports"])
            assert responses.HISTORY_INSTRUCTIONS in payload["instructions"]
            assert responses.RULE_INSTRUCTIONS in payload["instructions"]
            after_lookup = state["calls"] > 1
            if after_lookup:
                lookup = json.loads(payload["input"][-1]["output"])
                assert sorted(row["id"] for row in lookup["rule_excerpts"]) == sorted(FITUP_IDS)
                proposal = next(tool for tool in payload["tools"] if tool["name"] == "propose")
                assert "rule_refs" in proposal["parameters"]["required"]
            else:
                state["history"] = payload["input"]
                state["call_id"] = "mock-call"
            seen.append((report.report_id, after_lookup))
            reply = json.loads(templates[report.report_id][int(after_lookup)])
            if after_lookup:
                reply["rule_refs"] = [FITUP_IDS[0]]
            return json.dumps(reply)

    monkeypatch.setattr(responses, "ResponsesReportModel", MockResponses)
    assert cli.main(["run-live", "--case", case, "--allow-paid", "--output", str(run),
                     *topics, "--rule-search", "keyword", "--top-k", "2"]) == 0
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["termination"] == "COMPLETED"
    assert len(seen) == 6
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    retrieval = manifest["history"]["retrieval"]
    assert (retrieval["top_k"], retrieval["eligible_outcomes"], retrieval["excluded_case_id"]) == (2, 2, case)
    assert manifest["history"]["recorded_outcomes"] == 3
    assert manifest["rule_knowledge"]["sha256"] == prepared.rule_knowledge_sha256
    assert manifest["rule_knowledge"]["retrieval"]["mode"] == "keyword"
    if case != "D01":
        score = json.loads((run / "evaluation.json").read_text(encoding="utf-8"))
        assert score["case_id"] == case and score["acceptance_passed"]
    trace = (run / "trace.jsonl").read_text(encoding="utf-8")
    assert "RULE_CITATIONS" in trace and "sk-test-placeholder" not in trace

    store = OutcomeStore(history)
    try:
        mine = [row for row in store.eligible(before="9999", data_policy=POLICY) if row["case_id"] == case]
        cited = store._conn.execute(
            "SELECT rule_refs_json FROM report_outcomes WHERE run_id = ? ORDER BY report_id",
            (manifest["run_id"],)).fetchall()
    finally:
        store.close()
    assert len(mine) == 4
    assert {tuple(json.loads(refs)) for (refs,) in cited} == {(FITUP_IDS[0],)}
