"""Offline contract tests for the optional SDK boundary; no API calls are made."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import socket
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

openai = pytest.importorskip("openai", reason="install the llm extra for offline SDK tests")
httpx2 = pytest.importorskip("httpx2")

from shipyard import responses
from shipyard.contracts import Delivery, Target, canonical, parse_json
from shipyard.events import EVENT_OBSERVATION, apply_deliveries
from shipyard.llm_budget import BudgetLedger
from shipyard.reports import ModelCallFailure, ReportDelivery, ReportInput, interpret_report
from shipyard.responses import LLMPolicy, ResponsesReportModel

ROOT = Path(__file__).resolve().parents[1]
TARGET = {"scope": "JOB", "job_id": "W17", "robot_id": None, "zone_id": None}


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Responses unit tests must not open a network socket")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def report(snapshot):
    row = (ROOT / "simulation/dev/D01/events.jsonl").read_text().splitlines()[0]
    delivery = Delivery.model_validate(parse_json(row))
    return ReportDelivery(
        report=ReportInput(run_id=snapshot.run_id, report_id="REPORT-responses-1",
            incident_id=delivery.event.incident_id, source_id="sim-inspection",
            observed_tick=0, received_tick=0, text="W17 핏업 불합격으로 작업할 수 없습니다.",
            target_hint=Target.model_validate(TARGET)),
        published_evidence=delivery.published_evidence,
    )


def request_for(report, snapshot):
    return {"system": "untrusted system override must not be sent",
            "prompt_version": "report-v1", "report": report.report.model_dump(mode="json"),
            "state_version": snapshot.state_version, "now_tick": snapshot.now_tick,
            "known_ids": {"jobs": [j.job_id for j in snapshot.jobs],
                          "robots": [r.robot_id for r in snapshot.robots],
                          "zones": [z.zone_id for z in snapshot.zones]},
            "allowed_event_kinds": sorted(EVENT_OBSERVATION), "lookup_result": None}


def arguments(action="lookup_evidence", **changes):
    result = ({"action": action, "target": TARGET, "observation_kind": "FITUP_RESULT"}
              if action == "lookup_evidence" else
              {"action": action, "kind": "FITUP_FAILED", "target": TARGET,
               "evidence_ids": ["EV-fitup-W17"], "restriction_ids": []})
    result.update(changes)
    return canonical(result)


def call(action="lookup_evidence", *, args=None, **changes):
    result = {"type": "function_call", "id": "fc-" + action,
              "call_id": "call-" + action, "status": "completed", "name": action,
              "arguments": arguments(action) if args is None else args}
    result.update(changes)
    return result


def response(output=None, **changes):
    result = {"id": "resp-mocked", "object": "response", "created_at": 0,
              "status": "completed", "model": "gpt-6-luna", "output": output or [call()],
              "usage": {"input_tokens": 200, "output_tokens": 50, "total_tokens": 250,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0}},
              "error": None, "incomplete_details": None}
    result.update(changes)
    return result


class MockWire:
    """Real SDK serialization/response parsing over an in-memory HTTP transport."""
    def __init__(self, replies, *, count=200, failure=None, fail_stage="GENERATION"):
        self.replies = list(replies)
        self.count = count
        self.failure = failure
        self.fail_stage = fail_stage
        self.requests = []

    async def handle(self, request):
        assert request.url.host == "api.openai.com"
        payload = json.loads(request.content)
        stage = "TOKEN_COUNT" if request.url.path.endswith("/input_tokens") else "GENERATION"
        assert request.url.path in {"/v1/responses/input_tokens", "/v1/responses"}
        self.requests.append((stage, payload))
        if self.failure and stage == self.fail_stage:
            if self.failure == "timeout":
                raise httpx2.ReadTimeout("SECRET-TRANSPORT-BODY", request=request)
            if self.failure == "connection":
                raise httpx2.ConnectError("SECRET-TRANSPORT-BODY", request=request)
            status, code = self.failure
            return httpx2.Response(status, json={"error": {"message": "SECRET-TRANSPORT-BODY",
                                   "type": "invalid_request_error", "code": code}},
                                   headers={"x-request-id": "req-mocked-error"})
        body = {"object": "response.input_tokens", "input_tokens": self.count} if stage == "TOKEN_COUNT" else self.replies.pop(0)
        return httpx2.Response(200, json=body, headers={"x-request-id": "req-mocked"})

    def client(self):
        return openai.AsyncOpenAI(api_key="test-key-never-sent", project="project-test",
            base_url=responses.API_BASE, max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle), trust_env=False))


@pytest.fixture
def create_model(tmp_path):
    opened = []

    def create(*, replies=None, policy=None, count=200, failure=None, fail_stage="GENERATION",
               client_factory=None, clock=time.monotonic, ledger_limits=None):
        policy = policy or LLMPolicy(project_id="project-test", approved_for_paid_calls=True)
        limits = ledger_limits or (policy.report_budget_micro_usd, policy.run_budget_micro_usd,
                                   policy.project_budget_micro_usd)
        ledger = BudgetLedger(tmp_path / f"budget-{len(opened)}.sqlite3", policy.project_id, *limits)
        wire = MockWire(replies or [response()], count=count, failure=failure, fail_stage=fail_stage)
        model = ResponsesReportModel(policy, ledger, allow_paid=True,
                    client_factory=client_factory or wire.client, clock=clock)
        opened.append((model, ledger))
        return model, wire, ledger

    yield create
    for model, ledger in reversed(opened):
        model.close()
        ledger.close()


def test_sdk_lookup_and_proposal_share_call_history_and_common_event_checks(snapshot, report, create_model):
    reasoning = {"type": "reasoning", "id": "rs-mocked", "summary": []}
    model, wire, ledger = create_model(replies=[response([reasoning, call()]), response([call("propose")])])
    before = canonical(snapshot), canonical(report)
    result = interpret_report(report, snapshot, model)

    assert result.status == "READY" and (result.model_calls, result.lookup_count) == (2, 1)
    assert [stage for stage, _ in wire.requests] == ["TOKEN_COUNT", "GENERATION"] * 2
    first, second = [payload for stage, payload in wire.requests if stage == "GENERATION"]
    assert first["instructions"] == responses.SYSTEM_INSTRUCTION
    assert second["input"][0] == first["input"][0]
    assert second["input"][1:3] == [reasoning, call()]
    tool_result = second["input"][3]
    assert tool_result["type"] == "function_call_output" and tool_result["call_id"] == "call-lookup_evidence"
    assert json.loads(tool_result["output"])["evidence"][0]["evidence_id"] == "EV-fitup-W17"
    assert {t["name"] for t in first["tools"]} == {"lookup_evidence", "propose", "clarify"}
    assert {t["name"] for t in second["tools"]} == {"propose", "clarify"}
    for payload in (first, second):
        assert payload["store"] is False and payload["background"] is False and payload["stream"] is False
        assert payload["parallel_tool_calls"] is False
        assert payload["max_output_tokens"] == 1000 and payload["truncation"] == "disabled"
        assert "previous_response_id" not in payload and "conversation" not in payload
    assert (canonical(snapshot), canonical(report)) == before
    assert ledger.summary() == {"reserved_micro_usd": 0, "settled_micro_usd": 90,
                                 "committed_micro_usd": 90, "request_count": 2}
    provider = result.trace[-1]["provider"]
    assert provider["request_id"] == "req-mocked" and provider["usage_status"] == "GENERATION_ESTIMATE_SETTLED"

    updated, outcomes = apply_deliveries(snapshot, [result.delivery])
    assert outcomes[0]["outcome"] == "APPLIED"
    assert [r.kind for r in updated.active_restrictions] == ["FITUP_HOLD"]
    assert next(j for j in updated.jobs if j.job_id == "W18").lifecycle == "RUNNING"


def test_wire_builder_uses_allowlist_and_fixed_system_instructions(snapshot, report, create_model):
    model, wire, _ = create_model()
    request = request_for(report, snapshot)
    request.update(oracle="ORACLE_PRIVATE_SENTINEL", future_events="FUTURE_PRIVATE_SENTINEL",
                   evaluator_truth="TRUTH_PRIVATE_SENTINEL", api_key="KEY_PRIVATE_SENTINEL")
    model.respond(request)
    encoded = canonical(wire.requests)
    for value in ("ORACLE_PRIVATE_SENTINEL", "FUTURE_PRIVATE_SENTINEL", "TRUTH_PRIVATE_SENTINEL",
                  "KEY_PRIVATE_SENTINEL", "untrusted system override", report.report.report_id,
                  report.report.run_id, report.report.incident_id, report.report.source_id):
        assert value not in encoded
    sent_report = json.loads(wire.requests[1][1]["input"][0]["content"])["report"]
    assert set(sent_report) == {"text", "target_hint"}


def test_function_schemas_are_strict_at_every_object_boundary():
    def visit(value):
        if isinstance(value, dict):
            if "properties" in value:
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            assert "default" not in value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for tool in responses.function_tools():
        assert tool["type"] == "function" and tool["strict"] is True
        visit(tool["parameters"])


@pytest.mark.parametrize("failure,expected", [
    ((401, "invalid_api_key"), "MODEL_AUTH_ERROR"),
    ((403, "permission_denied"), "MODEL_PERMISSION_ERROR"),
    ((404, "model_not_found"), "MODEL_NOT_FOUND"),
    ((429, "insufficient_quota"), "MODEL_QUOTA_ERROR"),
    ((429, "billing_hard_limit_reached"), "MODEL_QUOTA_ERROR"),
    ((429, "project_spend_limit_exceeded"), "MODEL_QUOTA_ERROR"),
    ((429, "organization_spend_limit_exceeded"), "MODEL_QUOTA_ERROR"),
    ((429, "organization_usage_limit_exceeded"), "MODEL_QUOTA_ERROR"),
    ((429, "credit_balance_exhausted"), "MODEL_QUOTA_ERROR"),
    ((429, "rate_limit_exceeded"), "MODEL_RATE_LIMIT"),
    ((500, "server_error"), "MODEL_SERVICE_ERROR"),
    ((400, "invalid_request"), "MODEL_REQUEST_ERROR"),
    ("timeout", "MODEL_TIMEOUT"),
    ("connection", "MODEL_CONNECTION_ERROR"),
])
def test_sdk_failures_are_distinct_safe_and_not_retried(snapshot, report, create_model, failure, expected):
    model, wire, ledger = create_model(failure=failure)
    result = interpret_report(report, snapshot, model)
    assert result.status == expected
    assert [stage for stage, _ in wire.requests] == ["TOKEN_COUNT", "GENERATION"]
    assert ledger.summary()["reserved_micro_usd"] == 1700
    assert ledger.summary()["settled_micro_usd"] == 0
    assert "SECRET-TRANSPORT-BODY" not in canonical(result)
    assert "test-key-never-sent" not in canonical(result)
    updated, outcomes = apply_deliveries(snapshot, [result.delivery])
    assert outcomes[0]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert [r.kind for r in updated.active_restrictions] == ["EVIDENCE_PENDING"]

    another = report.model_copy(deep=True)
    another.report.report_id = "REPORT-after-failure"
    stopped = interpret_report(another, snapshot, model)
    assert stopped.status == "MODEL_SESSION_STOPPED" and len(wire.requests) == 2


def test_token_count_failure_does_not_start_generation(snapshot, report, create_model):
    model, wire, ledger = create_model(failure=(403, "permission_denied"), fail_stage="TOKEN_COUNT")
    result = interpret_report(report, snapshot, model)
    assert result.status == "MODEL_PERMISSION_ERROR"
    assert [stage for stage, _ in wire.requests] == ["TOKEN_COUNT"]
    assert ledger.summary()["reserved_micro_usd"] == 1700


@pytest.mark.parametrize("status,expected", [("incomplete", "MODEL_INCOMPLETE"), ("failed", "MODEL_SERVICE_ERROR")])
def test_unfinished_response_does_not_accept_embedded_valid_function(snapshot, report, create_model, status, expected):
    model, _, ledger = create_model(replies=[response(status=status)])
    result = interpret_report(report, snapshot, model)
    assert result.status == expected and result.lookup_count == 0
    assert ledger.summary()["settled_micro_usd"] == 45
    assert result.delivery.event.kind == "IDENTITY_UNCERTAIN"


def test_provider_refusal_is_not_a_schema_or_transport_error(snapshot, report, create_model):
    refusal = {"type": "message", "id": "msg-refusal", "role": "assistant", "status": "completed",
               "content": [{"type": "refusal", "refusal": "I cannot interpret this report."}]}
    model, _, ledger = create_model(replies=[response([refusal])])
    result = interpret_report(report, snapshot, model)
    assert result.status == "MODEL_REFUSAL" and result.model_calls == 1
    assert ledger.summary()["settled_micro_usd"] == 45


@pytest.mark.parametrize("output", [
    [call(), call("propose")],
    [call("unknown_function", args='{"action":"refuse"}')],
    [{"type": "message", "id": "msg-text", "role": "assistant", "status": "completed",
      "content": [{"type": "output_text", "text": "{}", "annotations": []}]}],
    [call(status="in_progress")],
])
def test_only_one_completed_allowlisted_function_can_reach_adapter(snapshot, report, create_model, output):
    model, _, _ = create_model(replies=[response(output)])
    result = interpret_report(report, snapshot, model)
    assert result.status == "INVALID_MODEL_RESPONSE"
    assert result.reason_codes == ["EXPECTED_ONE_ALLOWED_FUNCTION_CALL"]
    assert result.lookup_count == 0


@pytest.mark.parametrize("raw,reason", [
    ("not-json", "INVALID_FUNCTION_ARGUMENTS"),
    ('{"action":"refuse","action":"lookup_evidence"}', "INVALID_FUNCTION_ARGUMENTS"),
    (arguments(code="__import__('os').system('echo forbidden')"), "INVALID_FUNCTION_ARGUMENTS"),
    (arguments(source_id="invented-source", state_version=10), "INVALID_FUNCTION_ARGUMENTS"),
    (arguments("propose"), "FUNCTION_ACTION_MISMATCH"),
])
def test_malformed_or_malicious_arguments_remain_data(snapshot, report, create_model, raw, reason):
    model, _, _ = create_model(replies=[response([call(args=raw)])])
    before = canonical(snapshot)
    result = interpret_report(report, snapshot, model)
    assert result.status == "INVALID_MODEL_RESPONSE" and result.reason_codes == [reason]
    assert canonical(snapshot) == before and result.lookup_count == 0


@pytest.mark.parametrize("count", [12001, -1, True])
def test_input_limit_prevents_generation_and_keeps_reservation(snapshot, report, create_model, count):
    model, wire, ledger = create_model(count=count)
    result = interpret_report(report, snapshot, model)
    assert result.status == "MODEL_INPUT_LIMIT"
    assert [stage for stage, _ in wire.requests] == ["TOKEN_COUNT"]
    assert ledger.summary()["reserved_micro_usd"] == 1700


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 200},
    {"input_tokens": -1, "output_tokens": 1}, {"input_tokens": True, "output_tokens": 1}])
def test_unknown_or_invalid_usage_keeps_reservation_and_stops_session(snapshot, report, create_model, usage):
    model, _, ledger = create_model(replies=[response(usage=usage)])
    result = interpret_report(report, snapshot, model)
    assert result.status == "MODEL_USAGE_UNKNOWN"
    assert ledger.summary()["reserved_micro_usd"] == 1700
    assert ledger.summary()["settled_micro_usd"] == 0


@pytest.mark.parametrize("changes", [
    {"usage": {"input_tokens": 12001, "output_tokens": 1}},
    {"usage": {"input_tokens": 200, "output_tokens": 1001}},
    {"model": "unexpected-model-alias"},
])
def test_usage_or_returned_model_outside_approved_policy_is_not_settled(snapshot, report, create_model, changes):
    model, _, ledger = create_model(replies=[response(**changes)])
    result = interpret_report(report, snapshot, model)
    assert result.status == "MODEL_USAGE_MISMATCH"
    assert ledger.summary()["reserved_micro_usd"] == 1700


@pytest.mark.parametrize("limits,reason", [
    ((1000, 50000, 100000), "REPORT_BUDGET_EXCEEDED"),
    ((5000, 1000, 100000), "RUN_BUDGET_EXCEEDED"),
    ((5000, 50000, 1000), "PROJECT_BUDGET_EXCEEDED"),
])
def test_budget_rejection_happens_before_token_count_or_generation(snapshot, report, create_model, limits, reason):
    model, wire, ledger = create_model(ledger_limits=limits)
    result = interpret_report(report, snapshot, model)
    assert result.status == "MODEL_BUDGET_EXCEEDED"
    assert result.reason_codes == [reason]
    assert not wire.requests and ledger.summary()["request_count"] == 0


def test_run_deadline_is_checked_before_reserving_or_sending(snapshot, report, create_model):
    now = [0.0]
    model, wire, ledger = create_model(clock=lambda: now[0])
    now[0] = 181.0
    result = interpret_report(report, snapshot, model)
    assert result.status == "MODEL_TIMEOUT"
    assert result.reason_codes == ["REPORT_OR_RUN_DEADLINE"]
    assert not wire.requests and ledger.summary()["request_count"] == 0


def test_expired_deadline_after_slow_reservation_prevents_network(snapshot, report, create_model, monkeypatch):
    now = [0.0]
    model, wire, ledger = create_model(clock=lambda: now[0])
    reserve = ledger.reserve

    def slow_reserve(*args, **kwargs):
        reservation = reserve(*args, **kwargs)
        now[0] = 46.0
        return reservation

    monkeypatch.setattr(ledger, "reserve", slow_reserve)
    result = interpret_report(report, snapshot, model)
    assert result.status == "MODEL_TIMEOUT"
    assert not wire.requests
    assert ledger.summary()["reserved_micro_usd"] == 1700


@pytest.mark.parametrize("stage", ["TOKEN_COUNT", "GENERATION"])
def test_actual_async_timeout_cancels_pending_request(snapshot, report, create_model, stage):
    cancelled = []
    closed = []

    async def never_returns(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async def close():
        closed.append(True)

    async def count(**kwargs):
        return SimpleNamespace(input_tokens=200)

    count_method = never_returns if stage == "TOKEN_COUNT" else count
    client = SimpleNamespace(responses=SimpleNamespace(
        input_tokens=SimpleNamespace(count=count_method), create=never_returns), close=close)
    policy = LLMPolicy(project_id="project-test", approved_for_paid_calls=True, request_timeout_seconds=1)
    model, _, ledger = create_model(policy=policy, client_factory=lambda: client)
    started = time.monotonic()
    result = interpret_report(report, snapshot, model)
    elapsed = time.monotonic() - started
    assert result.status == "MODEL_TIMEOUT" and cancelled == [True]
    assert elapsed < 3.0
    assert ledger.summary()["reserved_micro_usd"] == 1700
    assert model.last_call_metadata["network_stage"] == stage


def test_paid_flags_and_project_are_required_before_client_creation(tmp_path):
    ledger = BudgetLedger(tmp_path / "flags.sqlite3", "project-test", 5000, 50000, 100000)
    try:
        for policy, allow in [(LLMPolicy(project_id="project-test"), True),
                              (LLMPolicy(project_id="project-test", approved_for_paid_calls=True), False),
                              (LLMPolicy(approved_for_paid_calls=True), True)]:
            with pytest.raises(ValueError):
                ResponsesReportModel(policy, ledger, allow_paid=allow,
                    client_factory=lambda: pytest.fail("unapproved client must not be constructed"))
        assert ledger.summary()["request_count"] == 0
    finally:
        ledger.close()


def test_policy_project_cannot_charge_another_projects_ledger(tmp_path):
    ledger = BudgetLedger(tmp_path / "project.sqlite3", "other-project", 5000, 50000, 100000)
    try:
        with pytest.raises(ValueError, match="LLM_LEDGER_PROJECT_MISMATCH"):
            ResponsesReportModel(LLMPolicy(project_id="project-test", approved_for_paid_calls=True),
                                 ledger, allow_paid=True,
                                 client_factory=lambda: pytest.fail("mismatched project must not connect"))
        assert ledger.summary()["request_count"] == 0
    finally:
        ledger.close()


def test_generation_attempt_cap_survives_provider_and_ledger_recreation(snapshot, report, tmp_path):
    policy = LLMPolicy(project_id="project-test", approved_for_paid_calls=True)
    ledger_path = tmp_path / "persistent-attempts.sqlite3"
    request = request_for(report, snapshot)
    for attempt in range(3):
        ledger = BudgetLedger(ledger_path, policy.project_id, 5000, 50000, 100000)
        wire = MockWire([response([call("propose", args=arguments("propose", evidence_ids=[]))])])
        model = ResponsesReportModel(policy, ledger, allow_paid=True, client_factory=wire.client)
        try:
            if attempt < 2:
                assert json.loads(model.respond(request))["action"] == "propose"
                assert len(wire.requests) == 2
            else:
                with pytest.raises(ModelCallFailure) as error:
                    model.respond(request)
                assert error.value.status == "MODEL_CALL_LIMIT"
                assert not wire.requests
            assert ledger.summary()["request_count"] == min(attempt + 1, 2)
            assert ledger.summary()["committed_micro_usd"] == 45 * min(attempt + 1, 2)
        finally:
            model.close()
            ledger.close()


def test_policy_rejects_budget_and_timeout_escape_hatches():
    for values in ({"max_input_tokens": 12001}, {"max_output_tokens": 1001},
                   {"request_timeout_seconds": 16}, {"report_deadline_seconds": 46},
                   {"run_deadline_seconds": 181}, {"max_input_tokens": True},
                   {"report_budget_micro_usd": 0}, {"model": "unapproved-model"},
                   {"base_url": "https://example.invalid"}):
        with pytest.raises(ValidationError):
            LLMPolicy.model_validate(values)


def test_report_context_changes_and_third_generation_are_blocked(snapshot, report, create_model):
    model, wire, _ = create_model(replies=[response(), response([call("propose")])])
    request = request_for(report, snapshot)
    model.respond(request)
    changed = copy.deepcopy(request)
    changed["state_version"] += 1
    changed["lookup_result"] = {"evidence": []}
    with pytest.raises(ModelCallFailure) as error:
        model.respond(changed)
    assert error.value.reason == "REPORT_CONTEXT_CHANGED"
    assert len(wire.requests) == 2

    request["lookup_result"] = {"evidence": []}
    model.respond(request)
    with pytest.raises(ModelCallFailure) as error:
        model.respond(request)
    assert error.value.reason == "GENERATION_CALL_LIMIT"
    assert len(wire.requests) == 4


def test_sdk_factory_ignores_host_and_proxy_environment(monkeypatch):
    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "fake-local-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://must-not-route.example")
    monkeypatch.setenv("HTTPS_PROXY", "https://must-not-proxy.example")

    def fake_async_client(**kwargs):
        captured["http"] = kwargs
        return object()

    def fake_sdk(**kwargs):
        captured["sdk"] = kwargs
        return object()

    monkeypatch.setattr(httpx2, "AsyncClient", fake_async_client)
    monkeypatch.setattr(openai, "AsyncOpenAI", fake_sdk)
    responses.sdk_client(LLMPolicy(project_id="project-test"))
    assert captured["sdk"]["base_url"] == responses.API_BASE
    assert captured["sdk"]["max_retries"] == 0 and captured["sdk"]["timeout"] == 15
    assert captured["sdk"]["project"] == "project-test"
    assert captured["http"] == {"trust_env": False, "follow_redirects": False}


def test_preflight_without_access_check_reports_presence_without_key_or_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE-KEY-SENTINEL")
    monkeypatch.setattr(responses, "sdk_client", lambda policy: pytest.fail("default preflight must not connect"))
    result = responses.preflight(LLMPolicy(project_id="project-test"))
    assert result["api_key_present"] is True and result["generation_requests"] == 0
    assert result["model_access"] == "NOT_CHECKED"
    assert result["responses_permission"] == "NOT_CHECKED"
    assert result["billing_quota"] == "NOT_CHECKED"
    assert "PRIVATE-KEY-SENTINEL" not in canonical(result)


def test_metadata_visibility_does_not_claim_generation_or_billing_access():
    calls = []

    async def retrieve(name):
        calls.append(name)
        return SimpleNamespace(id=name)

    client = SimpleNamespace(models=SimpleNamespace(retrieve=retrieve))
    result = asyncio.run(responses.check_model_access(LLMPolicy(), client))
    assert calls == ["gpt-6-luna"]
    assert result == {"model_access": "METADATA_VISIBLE", "model": "gpt-6-luna",
                      "responses_permission": "NOT_PROVEN", "billing_quota": "NOT_PROVEN"}


def test_live_input_gate_requires_exact_approved_synthetic_bundle(tmp_path):
    hashes = {}
    for name in responses.DEV_INPUTS:
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = (ROOT / name).read_bytes()
        destination.write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    policy = LLMPolicy(input_sha256=hashes)
    responses.validate_live_inputs(tmp_path, policy, "D01")
    with pytest.raises(ValueError, match="DATA_POLICY_REJECTED"):
        responses.validate_live_inputs(tmp_path, policy, "D02")
    missing = policy.model_copy(deep=True)
    missing.input_sha256.pop(responses.DEV_INPUTS[0])
    with pytest.raises(ValueError, match="DATA_POLICY_REJECTED"):
        responses.validate_live_inputs(tmp_path, missing, "D01")
    (tmp_path / responses.DEV_INPUTS[-1]).write_text("changed synthetic input")
    with pytest.raises(ValueError, match="DATA_POLICY_REJECTED"):
        responses.validate_live_inputs(tmp_path, policy, "D01")
