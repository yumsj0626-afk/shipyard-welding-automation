"""Optional Responses API boundary. No operational state or future inputs here."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import os
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from .contracts import DTO, canonical, digest, parse_json
from .reports import (ClarifyReply, LookupReply, ProposalReply, REPLY, ReportInput,
                      ModelCallFailure, SYSTEM_INSTRUCTION, MAX_RESPONSE_BYTES)
from .llm_budget import BudgetLedger, BudgetExceeded

SDK_VERSION = "3.46.0"
API_BASE = "https://api.openai.com/v1"
WIRE_VERSION = "responses-v1"


class LLMPolicy(DTO):
    schema_version: Literal["1.0"] = "1.0"
    model: Literal["gpt-6-luna"] = "gpt-6-luna"
    project_id: str | None = None
    approved_for_paid_calls: bool = False
    data_policy: Literal["SYNTHETIC_DEV_ONLY"] = "SYNTHETIC_DEV_ONLY"
    max_input_tokens: Annotated[int, Field(ge=1, le=12000)] = 12000
    max_output_tokens: Annotated[int, Field(ge=64, le=1000)] = 1000
    request_timeout_seconds: Annotated[int, Field(ge=1, le=15)] = 15
    report_deadline_seconds: Annotated[int, Field(ge=1, le=45)] = 45
    run_deadline_seconds: Annotated[int, Field(ge=1, le=180)] = 180
    report_budget_micro_usd: Annotated[int, Field(ge=1)] = 5000
    run_budget_micro_usd: Annotated[int, Field(ge=1)] = 50000
    project_budget_micro_usd: Annotated[int, Field(ge=1)] = 100000
    input_sha256: dict[str, str] = Field(default_factory=dict)


def load_policy(path: Path) -> LLMPolicy:
    return LLMPolicy.model_validate(parse_json(path.read_text(encoding="utf-8")))


DEV_INPUTS = ("data/dev/D01/initial.json", "data/dev/D01/evidence.json",
              "simulation/dev/D01/events.jsonl", "simulation/reports/D01.jsonl", "config/dev-policy.json")


def validate_live_inputs(root: Path, policy: LLMPolicy, case: str):
    if case != "D01" or set(policy.input_sha256) != set(DEV_INPUTS):
        raise ValueError("DATA_POLICY_REJECTED: only the approved synthetic D01 bundle is supported")
    for name in DEV_INPUTS:
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != policy.input_sha256[name]:
            raise ValueError("DATA_POLICY_REJECTED: approved input hash changed")


def generation_cost_micro_usd(input_tokens: int, output_tokens: int) -> int:
    # Standard Luna, no cache writes, text only; round upward to a micro-dollar.
    return (input_tokens + 5 * output_tokens + 9) // 10


def function_tools(after_lookup: bool = False) -> list[dict]:
    def strict(node):
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            if "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)
    models = [("lookup_evidence", LookupReply), ("propose", ProposalReply), ("clarify", ClarifyReply)]
    result = []
    for name, model in models:
        if after_lookup and name == "lookup_evidence":
            continue
        schema = model.model_json_schema()
        strict(schema)
        result.append({"type":"function", "name":name, "strict":True, "parameters":schema,
                       "description":{"lookup_evidence":"Read current evidence for the reported target.",
                       "propose":"Propose an event for deterministic validation; never execute it.",
                       "clarify":"Request clarification when identity or interpretation is uncertain."}[name]})
    return result


def credential_status(policy: LLMPolicy) -> dict:
    return {"api_key_present":bool(os.environ.get("OPENAI_API_KEY")),
            "project_configured":bool(policy.project_id and policy.project_id.strip()),
            "model":policy.model, "paid_calls_approved":policy.approved_for_paid_calls,
            "data_policy":policy.data_policy, "model_access":"NOT_CHECKED",
            "responses_permission":"NOT_CHECKED", "billing_quota":"NOT_CHECKED"}


def preflight(policy: LLMPolicy, *, check_access: bool = False) -> dict:
    result = credential_status(policy)
    try:
        result["sdk_version"] = importlib.metadata.version("openai")
    except importlib.metadata.PackageNotFoundError:
        result["sdk_version"] = None
    result["generation_requests"] = 0
    result["generation_limits"] = policy.model_dump(exclude={"input_sha256","project_id"})
    if check_access:
        if not result["api_key_present"] or not result["project_configured"] or not result["sdk_version"]:
            result["model_access"] = "CREDENTIALS_OR_SDK_MISSING"
        else:
            async def check():
                client = sdk_client(policy)
                try:
                    return await check_model_access(policy,client)
                finally:
                    await client.close()
            result.update(asyncio.run(check()))
    return result


def sdk_client(policy: LLMPolicy):
    from openai import AsyncOpenAI
    import httpx2
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not policy.project_id or not policy.project_id.strip():
        raise ValueError("LLM_CREDENTIALS_MISSING: set API key in environment and project_id in local policy")
    # Explicit host and disabled environment proxies avoid accidental credential routing.
    return AsyncOpenAI(api_key=key,project=policy.project_id,base_url=API_BASE,
        max_retries=0,timeout=policy.request_timeout_seconds,
        http_client=httpx2.AsyncClient(trust_env=False,follow_redirects=False))


def classify_error(error: Exception) -> tuple[str, str]:
    name = type(error).__name__
    code = getattr(error,"status_code",None)
    if isinstance(error,TimeoutError) or name == "APITimeoutError":
        return "MODEL_TIMEOUT", "API_OR_LOCAL_DEADLINE"
    if code == 401: return "MODEL_AUTH_ERROR", "API_AUTHENTICATION_FAILED"
    if code == 403: return "MODEL_PERMISSION_ERROR", "API_PERMISSION_DENIED"
    if code == 404: return "MODEL_NOT_FOUND", "MODEL_OR_ENDPOINT_UNAVAILABLE"
    if code == 429:
        body = getattr(error,"body",{})
        detail = body.get("error",body) if isinstance(body,dict) else {}
        if isinstance(detail,dict) and detail.get("code") in {"insufficient_quota","billing_hard_limit_reached",
            "project_spend_limit_exceeded","organization_spend_limit_exceeded",
            "organization_usage_limit_exceeded","credit_balance_exhausted"}:
            return "MODEL_QUOTA_ERROR", "API_QUOTA_EXHAUSTED"
        return "MODEL_RATE_LIMIT", "API_RATE_LIMITED"
    if name == "APIConnectionError": return "MODEL_CONNECTION_ERROR", "API_CONNECTION_FAILED"
    if code and code >= 500: return "MODEL_SERVICE_ERROR", "API_SERVICE_FAILURE"
    if code: return "MODEL_REQUEST_ERROR", "API_REQUEST_REJECTED"
    return "MODEL_ERROR", "PROVIDER_PROTOCOL_ERROR"


async def check_model_access(policy: LLMPolicy, client) -> dict:
    """Read-only metadata check, not a generation or a billing approval."""
    try:
        async with asyncio.timeout(policy.request_timeout_seconds):
            result = await client.models.retrieve(policy.model)
        return {"model_access":"METADATA_VISIBLE", "model":result.id,
                "responses_permission":"NOT_PROVEN", "billing_quota":"NOT_PROVEN"}
    except Exception as error:
        status, reason = classify_error(error)
        return {"model_access":status,"reason_code":reason,
                "responses_permission":"NOT_PROVEN","billing_quota":"NOT_PROVEN"}


class ResponsesReportModel:
    def __init__(self, policy: LLMPolicy, ledger: BudgetLedger, *, allow_paid: bool = False,
                 client_factory=None, clock=time.monotonic):
        self.policy = policy.model_copy(deep=True)
        if not allow_paid or not policy.approved_for_paid_calls:
            raise ValueError("PAID_CALLS_NOT_APPROVED")
        if not policy.project_id or not policy.project_id.strip():
            raise ValueError("LLM_PROJECT_REQUIRED")
        if policy.project_id != ledger.project_id:
            raise ValueError("LLM_LEDGER_PROJECT_MISMATCH")
        self.ledger, self.clock = ledger, clock
        self._client_factory = client_factory or (lambda: sdk_client(self.policy))
        self._client = None
        self._runner = asyncio.Runner()
        self._started = clock()
        self._reports = {}
        self._run_id = None
        self._halted = False
        self.last_call_metadata = {}

    def _fail(self, status, reason):
        raise ModelCallFailure(status,reason,dict(self.last_call_metadata))

    def respond(self, request: dict) -> str:
        self.last_call_metadata = {"provider":"openai", "wire_version":WIRE_VERSION,
                                   "requested_model":self.policy.model, "sdk_version":SDK_VERSION}
        if self._halted:
            self._fail("MODEL_SESSION_STOPPED","PREVIOUS_PROVIDER_FAILURE")
        report = ReportInput.model_validate(request["report"])
        if self._run_id is not None and self._run_id != report.run_id:
            self._fail("INVALID_MODEL_REQUEST","MODEL_INSTANCE_RUN_MISMATCH")
        self._run_id = report.run_id
        state = self._reports.get(report.report_id)
        if state is None:
            if request.get("lookup_result") is not None or len(self._reports) >= 3:
                self._fail("MODEL_CALL_LIMIT","REPORT_SESSION_LIMIT")
            state = {"started":self.clock(), "calls":0, "history":None,
                     "report_hash":digest(report),"context_hash":digest({k:request[k] for k in ("known_ids","allowed_event_kinds","state_version","now_tick")})}
            self._reports[report.report_id] = state
        if (state["report_hash"] != digest(report) or state["context_hash"] != digest({k:request[k] for k in ("known_ids","allowed_event_kinds","state_version","now_tick")})):
            self._fail("INVALID_MODEL_REQUEST","REPORT_CONTEXT_CHANGED")
        if state["calls"] >= 2:
            self._fail("MODEL_CALL_LIMIT","GENERATION_CALL_LIMIT")
        after_lookup = state["calls"] > 0
        if after_lookup and (not state["history"] or request.get("lookup_result") is None):
            self._fail("INVALID_MODEL_REQUEST","MISSING_TOOL_RESULT")
        if not after_lookup:
            public = {"report":{"text":report.text,"target_hint":report.target_hint.model_dump() if report.target_hint else None},
                      "known_ids":request["known_ids"],"allowed_event_kinds":request["allowed_event_kinds"],"now_tick":request["now_tick"]}
            inputs = [{"role":"user","content":canonical(public)}]
        else:
            inputs = [*state["history"], {"type":"function_call_output", "call_id":state["call_id"],
                                         "output":canonical(request["lookup_result"])}]
        payload = {"model":self.policy.model, "instructions":SYSTEM_INSTRUCTION,
                   "input":inputs,"tools":function_tools(after_lookup), "tool_choice":"required",
                   "parallel_tool_calls":False,"reasoning":{"effort":"none"},"truncation":"disabled"}
        state["calls"] += 1
        self.last_call_metadata["generation_attempt"] = state["calls"]
        try:
            return self._runner.run(self._respond(report,state,payload))
        except ModelCallFailure:
            raise
        except Exception as error:
            self._halted = True
            status,reason = classify_error(error)
            self._fail(status,reason)

    async def _respond(self, report, state, payload):
        def remaining_time():
            remaining = min(self.policy.report_deadline_seconds-(self.clock()-state["started"]),
                            self.policy.run_deadline_seconds-(self.clock()-self._started))
            if remaining <= 0:
                self._fail("MODEL_TIMEOUT","REPORT_OR_RUN_DEADLINE")
            return remaining
        remaining_time()
        amount = generation_cost_micro_usd(self.policy.max_input_tokens,self.policy.max_output_tokens)
        try:
            reservation = self.ledger.reserve(report.run_id,report.report_id,amount,max_requests_per_report=2)
        except BudgetExceeded as error:
            self._halted = True
            self._fail("MODEL_CALL_LIMIT" if error.code=="REPORT_REQUEST_LIMIT_EXCEEDED" else "MODEL_BUDGET_EXCEEDED",error.code)
        self.last_call_metadata.update(reservation_id=reservation,reserved_micro_usd=amount,
                                       usage_status="UNKNOWN_RESERVATION_RETAINED",network_stage="TOKEN_COUNT")
        async with asyncio.timeout(remaining_time()):
            if self._client is None:
                self._client = self._client_factory()
            remaining_time()
            async with asyncio.timeout(self.policy.request_timeout_seconds):
                count = await self._client.responses.input_tokens.count(**payload)
            n = count.input_tokens
            if type(n) is not int or n < 0 or n > self.policy.max_input_tokens:
                self._fail("MODEL_INPUT_LIMIT","INPUT_TOKEN_LIMIT_OR_INVALID_COUNT")
            self.last_call_metadata.update(counted_input_tokens=n,network_stage="GENERATION")
            async with asyncio.timeout(self.policy.request_timeout_seconds):
                response = await self._client.responses.create(**payload,
                    max_output_tokens=self.policy.max_output_tokens,store=False,background=False,stream=False,
                    service_tier="default",prompt_cache_options={"mode":"explicit"})
        if (self.clock()-state["started"]>self.policy.report_deadline_seconds
            or self.clock()-self._started>self.policy.run_deadline_seconds):
            self._fail("MODEL_TIMEOUT","REPORT_OR_RUN_DEADLINE")
        self.last_call_metadata.update(response_id=response.id,returned_model=response.model,
            request_id=getattr(response,"_request_id",None),response_status=response.status)
        usage = getattr(response,"usage",None)
        if usage is None or any(type(v) is not int or v<0 for v in (
            getattr(usage,"input_tokens",None),getattr(usage,"output_tokens",None))):
            self._halted=True
            self._fail("MODEL_USAGE_UNKNOWN","MISSING_OR_INVALID_USAGE")
        cost = generation_cost_micro_usd(usage.input_tokens,usage.output_tokens)
        self.last_call_metadata.update(input_tokens=usage.input_tokens,output_tokens=usage.output_tokens,
            estimated_generation_micro_usd=cost)
        if (usage.input_tokens>self.policy.max_input_tokens or usage.output_tokens>self.policy.max_output_tokens
            or cost>amount or response.model!=self.policy.model):
            self._halted=True
            self._fail("MODEL_USAGE_MISMATCH","USAGE_OR_MODEL_OUTSIDE_APPROVED_POLICY")
        self.ledger.settle(reservation,cost)
        self.last_call_metadata["usage_status"]="GENERATION_ESTIMATE_SETTLED"
        if response.status != "completed":
            self._fail("MODEL_INCOMPLETE" if response.status=="incomplete" else "MODEL_SERVICE_ERROR","RESPONSE_NOT_COMPLETED")
        output=[item.model_dump(mode="json",exclude_none=True) for item in response.output]
        if any(c.get("type")=="refusal" for item in output for c in item.get("content",[])):
            return '{"action":"refuse"}'
        calls=[item for item in output if item.get("type")=="function_call"]
        allowed={tool["name"] for tool in payload["tools"]}
        if (len(calls)!=1 or any(item.get("type") not in {"function_call","reasoning"} for item in output)
            or calls[0].get("name") not in allowed or calls[0].get("status")!="completed"):
            self._fail("INVALID_MODEL_RESPONSE","EXPECTED_ONE_ALLOWED_FUNCTION_CALL")
        call=calls[0]
        raw=call["arguments"]
        try:
            if len(raw.encode("utf-8"))>MAX_RESPONSE_BYTES:
                self._fail("RESPONSE_LIMIT","RESPONSE_BYTES_EXCEEDED")
            reply=REPLY.validate_python(parse_json(raw))
        except (ValueError,UnicodeError,RecursionError):
            self._fail("INVALID_MODEL_RESPONSE","INVALID_FUNCTION_ARGUMENTS")
        if reply.action!=call["name"]:
            self._fail("INVALID_MODEL_RESPONSE","FUNCTION_ACTION_MISMATCH")
        if reply.action=="lookup_evidence":
            state["history"]=[*payload["input"],*output]
            state["call_id"]=call["call_id"]
        return canonical(reply)

    def close(self):
        try:
            if self._client is not None:
                self._runner.run(self._client.close())
        finally:
            self._runner.close()
