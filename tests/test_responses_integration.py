"""Responses CLI integration uses only an in-memory HTTP transport and dummy key."""
import csv
import hashlib
import json
from pathlib import Path
import shutil
import shlex

import pytest

from evaluation.score import score_run
from shipyard import cli, responses


ROOT = Path(__file__).resolve().parents[1]
DUMMY_KEY = "sk-test-mocked-transport-never-a-real-key"


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def execution_rows(path):
    fields = ("job_id", "robot_id", "phase", "start_tick", "end_tick", "from_zone_id", "to_zone_id")
    with (path / "execution.csv").open(newline="") as source:
        return sorted(tuple(row[key] for key in fields) for row in csv.DictReader(source))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for folder in ("src", "config", "data", "simulation", "evaluation"):
        shutil.copytree(ROOT / folder, root / folder, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, root / name)
    return root


def policy_file(root, **changes):
    policy = json.loads((root / "config/llm-policy.json").read_text())
    policy.update(project_id="proj_mock_integration", approved_for_paid_calls=True)
    policy.update(changes)
    path = root / "test llm-policy.json"
    path.write_text(json.dumps(policy))
    return path


def forbid_sdk(monkeypatch):
    calls = []

    def forbidden(policy):
        calls.append(policy.model)
        raise AssertionError("a rejected or offline path attempted to construct an API client")

    monkeypatch.setattr(responses, "sdk_client", forbidden)
    return calls


def mock_sdk(monkeypatch, handler):
    httpx2 = pytest.importorskip("httpx2")
    openai = pytest.importorskip("openai")
    clients = []

    def factory(policy):
        transport = httpx2.MockTransport(handler)
        http = httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
        clients.append(http)
        return openai.AsyncOpenAI(
            api_key=DUMMY_KEY, project=policy.project_id, base_url=responses.API_BASE,
            max_retries=0, timeout=policy.request_timeout_seconds, http_client=http,
        )

    monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
    monkeypatch.setattr(responses, "sdk_client", factory)
    return httpx2, clients


def function_response(reply, serial):
    return {
        "id": f"resp_mock_{serial}", "created_at": 0, "model": "gpt-6-luna",
        "object": "response", "status": "completed", "error": None,
        "incomplete_details": None, "instructions": None, "metadata": {},
        "output": [{"type": "function_call", "id": f"fc_mock_{serial}",
                    "call_id": f"call_mock_{serial}", "name": reply["action"],
                    "arguments": json.dumps(reply), "status": "completed"}],
        "parallel_tool_calls": False, "temperature": 1, "tool_choice": "required",
        "tools": [], "top_p": 1,
        "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }


def test_mocked_responses_d01_matches_fake_execution_and_never_sends_private_inputs(project, tmp_path, monkeypatch):
    fake = tmp_path / "fake"
    baseline = cli.run_case("D01", fake, project, report_mode="fake")

    # The driver may own these files; neither the model nor its HTTP payload may.
    reports_path = project / "simulation/reports/D01.jsonl"
    records = jsonl(reports_path)
    for record in records:
        record["responses"] = ["PRIVATE_FAKE_ANSWER_SENTINEL_92e4"]
        if record["delivery_tick"] == 6:
            record["text"] += " FUTURE_REPORT_SENTINEL_5c80"
    reports_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    for filename, marker in (("oracle.json", "ORACLE_SENTINEL_5d18"), ("truth.jsonl", "TRUTH_SENTINEL_27a6")):
        path = project / "evaluation/dev/D01" / filename
        if filename.endswith(".jsonl"):
            facts = jsonl(path)
            facts[0]["private_test_marker"] = marker
            path.write_text("\n".join(json.dumps(fact) for fact in facts) + "\n")
        else:
            oracle = json.loads(path.read_text())
            oracle["private_test_marker"] = marker
            path.write_text(json.dumps(oracle))
    approved = policy_file(project, input_sha256={
        name: hashlib.sha256((project / name).read_bytes()).hexdigest() for name in responses.DEV_INPUTS
    })
    packets, generations = [], []

    def handler(request):
        assert request.url.host == "api.openai.com"
        assert request.headers["Authorization"] == f"Bearer {DUMMY_KEY}"
        payload = json.loads(request.content)
        packets.append((request.url.path, payload))
        encoded = json.dumps(payload)
        for forbidden in ("PRIVATE_FAKE_ANSWER_SENTINEL", "ORACLE_SENTINEL", "TRUTH_SENTINEL"):
            assert forbidden not in encoded
        public = json.loads(payload["input"][0]["content"])
        assert set(public) == {"report", "known_ids", "allowed_event_kinds", "now_tick"}
        assert set(public["report"]) == {"text", "target_hint"}
        if public["now_tick"] < 6:
            assert "FUTURE_REPORT_SENTINEL" not in encoded
            assert "EV-repair-W17" not in encoded and "EV-recheck-W17" not in encoded
        if request.url.path == "/v1/responses/input_tokens":
            return httpx2.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})
        assert request.url.path == "/v1/responses" and request.method == "POST"
        assert payload["store"] is False and payload["background"] is False and payload["stream"] is False
        assert payload["parallel_tool_calls"] is False and payload["truncation"] == "disabled"
        assert "previous_response_id" not in payload
        outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
        target = {"scope": "JOB", "job_id": "W17", "robot_id": None, "zone_id": None}
        if outputs:
            lookup = json.loads(outputs[0]["output"])
            kind, evidence_id = {
                "FITUP_RESULT": ("FITUP_FAILED", "EV-fitup-W17"),
                "REPAIR_COMPLETED": ("REPAIR_REPORTED", "EV-repair-W17"),
                "RECHECK_RESULT": ("RECHECK_REPORTED", "EV-recheck-W17"),
            }[lookup["observation_kind"]]
            assert evidence_id in {item["evidence_id"] for item in lookup["evidence"]}
            reply = {"action": "propose", "kind": kind, "target": target,
                     "evidence_ids": [evidence_id], "restriction_ids": [] if kind == "FITUP_FAILED"
                     else ["restriction:E-fitup-W17:FITUP_HOLD"]}
        else:
            text = public["report"]["text"]
            observation = "FITUP_RESULT" if "불합격" in text else "RECHECK_RESULT" if "재검사에 합격" in text else "REPAIR_COMPLETED"
            reply = {"action": "lookup_evidence", "target": target, "observation_kind": observation}
        generations.append(reply)
        return httpx2.Response(200, json=function_response(reply, len(generations)),
                              headers={"x-request-id": f"req_mock_{len(generations)}"})

    httpx2, clients = mock_sdk(monkeypatch, handler)
    original_open = Path.open

    def guard(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if "r" in mode and "evaluation" in path.parts:
            raise AssertionError(f"runtime attempted to read evaluator data: {path}")
        return original_open(path, *args, **kwargs)

    live = tmp_path / "mocked-responses"
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guard)
        summary = cli.run_case("D01", live, project, report_mode="responses",
                               llm_policy_path=approved, allow_paid=True)
    assert summary["termination"] == "COMPLETED" and not summary["failures"]
    assert summary["completed_jobs"] == baseline["completed_jobs"]
    assert summary["additional_human_requests"] == 0
    assert execution_rows(live) == execution_rows(fake)
    assert len(generations) == 6 and len(packets) == 12
    assert all(client.is_closed for client in clients)
    assert any("FUTURE_REPORT_SENTINEL" in json.dumps(body) for _, body in packets)
    for output in (fake, live):
        score = score_run(output, project / "evaluation/dev/D01/oracle.json", project / "evaluation/dev/D01/truth.jsonl")
        assert score["acceptance_passed"] and score["completed_without_additional_human"]
        assert score["executed_violation_count"] == 0
    manifest = json.loads((live / "manifest.json").read_text())
    assert manifest["report_mode"] == "responses"
    assert manifest["llm"]["generation_budget"]["run"] == {
        "reserved_micro_usd": 0, "settled_micro_usd": 150,
        "committed_micro_usd": 150, "request_count": 6,
    }
    assert "project_id" not in manifest["llm"]["policy"]
    assert "--allow-paid" in manifest["command"] and "--llm-policy" in manifest["command"]
    command = shlex.split(manifest["command"])
    assert command[:5] == ["uv", "run", "--locked", "--extra", "llm"]
    assert command[command.index("--llm-policy") + 1] == str(approved)
    for filename in ("manifest.json", "trace.jsonl", "summary.json"):
        assert DUMMY_KEY not in (live / filename).read_text()


@pytest.mark.parametrize("approved,allow_paid", [(False, False), (False, True), (True, False)])
def test_cli_requires_both_policy_approval_and_explicit_paid_flag(project, tmp_path, monkeypatch, capsys, approved, allow_paid):
    calls = forbid_sdk(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
    policy = policy_file(project, approved_for_paid_calls=approved)
    output = tmp_path / "rejected"
    arguments = ["run", "--case", "D01", "--output", str(output), "--report-mode", "responses",
                 "--llm-policy", str(policy)]
    if allow_paid:
        arguments.append("--allow-paid")
    assert cli.main(arguments) == 2
    assert "PAID_CALLS_NOT_APPROVED" in capsys.readouterr().err
    assert calls == [] and not output.exists()
    assert not (project / "artifacts/llm-budget.sqlite3").exists()


@pytest.mark.parametrize("missing", ["policy", "key", "project"])
def test_missing_policy_or_credentials_prevent_network_and_output(project, tmp_path, monkeypatch, missing):
    calls = forbid_sdk(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if missing != "key":
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
    path = policy_file(project, project_id=None if missing == "project" else "proj_mock_integration")
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="LLM_POLICY_REQUIRED" if missing == "policy" else "LLM_CREDENTIALS_MISSING"):
        cli.run_case("D01", output, project, report_mode="responses", allow_paid=True,
                     llm_policy_path=None if missing == "policy" else path)
    assert calls == [] and not output.exists()
    assert not (project / "artifacts/llm-budget.sqlite3").exists()


@pytest.mark.parametrize("change", ["case", "input_hash", "incomplete_allowlist"])
def test_only_frozen_approved_d01_inputs_can_reach_responses(project, tmp_path, monkeypatch, change):
    calls = forbid_sdk(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
    path = policy_file(project)
    if change == "input_hash":
        with (project / "simulation/reports/D01.jsonl").open("a") as file:
            file.write("\n")
    elif change == "incomplete_allowlist":
        policy = json.loads(path.read_text())
        policy["input_sha256"].pop("config/dev-policy.json")
        path.write_text(json.dumps(policy))
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="DATA_POLICY_REJECTED"):
        cli.run_case("D02" if change == "case" else "D01", output, project,
                     report_mode="responses", llm_policy_path=path, allow_paid=True)
    assert calls == [] and not output.exists()
    assert not (project / "artifacts/llm-budget.sqlite3").exists()


def test_local_preflight_needs_no_key_and_makes_no_requests(project, monkeypatch, capsys):
    calls = forbid_sdk(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    policy = policy_file(project, project_id=None, approved_for_paid_calls=False)
    assert cli.main(["llm-preflight", "--llm-policy", str(policy)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["api_key_present"] is False and result["project_configured"] is False
    assert result["generation_requests"] == 0
    assert result["model_access"] == result["responses_permission"] == result["billing_quota"] == "NOT_CHECKED"
    assert calls == []


def test_metadata_preflight_is_get_only_and_does_not_approve_generation_or_quota(project, tmp_path, monkeypatch, capsys):
    packets = []

    def handler(request):
        packets.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == "/v1/models/gpt-6-luna"
        return httpx2.Response(200, json={"id": "gpt-6-luna", "object": "model", "created": 0, "owned_by": "openai"})

    httpx2, clients = mock_sdk(monkeypatch, handler)
    policy = policy_file(project, approved_for_paid_calls=False)
    assert cli.main(["llm-preflight", "--llm-policy", str(policy), "--check-access"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["model_access"] == "METADATA_VISIBLE"
    assert result["responses_permission"] == result["billing_quota"] == "NOT_PROVEN"
    assert result["paid_calls_approved"] is False and result["generation_requests"] == 0
    assert all(client.is_closed for client in clients)
    output = tmp_path / "not-authorized-by-preflight"
    with pytest.raises(ValueError, match="PAID_CALLS_NOT_APPROVED"):
        cli.run_case("D01", output, project, report_mode="responses", llm_policy_path=policy, allow_paid=True)
    assert not output.exists()
    assert packets == [("GET", "/v1/models/gpt-6-luna")]


def test_default_structured_and_fake_modes_remain_offline_without_credentials(project, tmp_path, monkeypatch):
    calls = forbid_sdk(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    structured, fake = tmp_path / "default", tmp_path / "fake"
    original = cli.run_case("D01", structured, project)
    adapted = cli.run_case("D01", fake, project, report_mode="fake")
    assert original["termination"] == adapted["termination"] == "COMPLETED"
    assert execution_rows(structured) == execution_rows(fake)
    assert calls == [] and not (project / "artifacts/llm-budget.sqlite3").exists()
    for directory, expected in ((structured, "structured"), (fake, "fake")):
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["report_mode"] == expected and "llm" not in manifest
