"""Filesystem composition root: public data and private simulator load separately."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import sqlite3
import subprocess
import sys
import shlex
from pathlib import Path
from uuid import uuid4

from .case_files import case_input_files, validate_case_id
from .contracts import (
    EvidenceData, InitialData, Instruction, Observation, PlanningSnapshot, Policy,
    canonical, parse_json, utc_now,
)
from .controller import Controller
from .repository import Repository
from .reporting import export_run, inspect_run
from .history import DEFAULT_TOP_K, HistoryRetriever, OutcomeStore
from .report_retrieval import DEFAULT_CHAT_MODEL, DEFAULT_EMBED_MODEL, OpenAIReportClient, analyze_report
from .rule_knowledge import RuleKnowledge
from .simulator import Simulator
from .reports import (FakeReportModel, FakeReportRecord, ReportDelivery, ReportInput,
                      MAX_MODEL_CALLS, MAX_LOOKUPS, PROMPT_VERSION)

ROOT = Path(__file__).resolve().parents[2]
# Every development case is synthetic; stored outcomes carry this marker and
# retrieval only returns rows recorded under the caller's data policy.
DEV_DATA_POLICY = "SYNTHETIC_DEV_ONLY"


def default_history_path(root: Path) -> Path:
    return root / "artifacts" / "report-history.sqlite3"


def default_rule_topics(root: Path) -> Path | None:
    path = root / "config" / "rule-topics.json"
    return path if path.is_file() else None


def load_public_case(root: Path, case: str, run_id: str) -> PlanningSnapshot:
    validate_case_id(case)
    folder=root/'data'/'dev'/case
    initial=InitialData.model_validate(parse_json((folder/'initial.json').read_text(encoding='utf-8')))
    evidence=EvidenceData.model_validate(parse_json((folder/'evidence.json').read_text(encoding='utf-8')))
    if initial.case_id != case or evidence.case_id != case or initial.data_revision != evidence.data_revision:
        raise ValueError('case/revision mismatch')
    source_ids={s.source_id for s in evidence.sources}
    if len(source_ids)!=len(evidence.sources) or len({e.evidence_id for e in evidence.evidence})!=len(evidence.evidence):
        raise ValueError('duplicate evidence/source ID')
    for ev in evidence.evidence:
        source=next((s for s in evidence.sources if s.source_id==ev.source_id),None)
        if ev.available_tick>initial.now_tick or not source or ev.observation_kind not in source.allowed_observation_kinds or ev.target.scope not in source.allowed_target_scopes:
            raise ValueError('unavailable or unauthorized initial evidence')
    snapshot=PlanningSnapshot(run_id=run_id,now_tick=initial.now_tick,jobs=initial.jobs,robots=initial.robots,
        zones=initial.zones,travel_times=initial.travel_times,baseline_plan=initial.baseline_plan,
        execution_progress=initial.execution_progress,sources=evidence.sources,evidence=evidence.evidence,
        visible_evidence_refs=[e.evidence_id for e in evidence.evidence])
    for progress in snapshot.execution_progress:
        phase=progress.current_phase
        if phase:
            snapshot.current_instructions.append(Instruction(instruction_id='initial:'+progress.job_id,
                plan_id='initial-plan',accepted_state_version=0,job_id=progress.job_id,robot_id=progress.robot_id,
                action=phase.phase,not_before_tick=0,expected_end_tick=phase.end_tick,status='RUNNING',
                execution_id=phase.execution_id,attempt_id=progress.attempt_id,
                from_zone_id=phase.from_zone_id,to_zone_id=phase.to_zone_id))
    return snapshot


def load_report_records(path: Path, *, require_script: bool = False) -> dict[str, FakeReportRecord]:
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = FakeReportRecord.model_validate(parse_json(line))
        if record.report_id in records:
            raise ValueError("Duplicate report ID")
        if require_script and "responses" not in record.model_fields_set:
            raise ValueError("Fake mode requires scripted responses for each report")
        records[record.report_id] = record
    return records


def source_hash(root: Path) -> str:
    files=sorted((root/'src'/'shipyard').glob('*.py'))+sorted((root/'src'/'shipyard').glob('*.sql'))
    files += [root/'pyproject.toml',root/'uv.lock']
    value=hashlib.sha256()
    for file in files:
        value.update(str(file.relative_to(root)).encode());value.update(file.read_bytes())
    return value.hexdigest()


def run_case(case: str, output: Path, root: Path=ROOT, *, report_mode: str="structured",
             llm_policy_path: Path | None=None, allow_paid: bool=False,
             rules_path: Path | None = None, history_path: Path | None = None,
             include_same_case: bool = False, rule_topics_path: Path | None = None,
             embed_model: str = DEFAULT_EMBED_MODEL, top_k: int = DEFAULT_TOP_K) -> dict:
    if report_mode not in {"structured", "fake", "responses"}:
        raise ValueError("unknown report mode")
    input_files = case_input_files(case, include_reports=report_mode != "structured")
    if rules_path is not None and report_mode not in {"fake", "responses"}:
        raise ValueError("--rules is available with --report-mode fake or responses")
    if rules_path is not None and rule_topics_path is None:
        rule_topics_path = default_rule_topics(root)
    rule_knowledge = RuleKnowledge(rules_path, rule_topics_path) if rules_path is not None else None
    if history_path is not None and report_mode == "structured":
        raise ValueError("--history is available with --report-mode fake or responses")
    embedding_client = None
    llm_policy = None
    live_model = ledger = None
    if report_mode == "responses":
        from .responses import load_policy, credential_status, validate_live_inputs, ResponsesReportModel
        from .llm_budget import BudgetLedger
        if llm_policy_path is None:
            raise ValueError("LLM_POLICY_REQUIRED")
        llm_policy = load_policy(llm_policy_path, dotenv_path=root / ".env")
        if not allow_paid or not llm_policy.approved_for_paid_calls:
            raise ValueError("PAID_CALLS_NOT_APPROVED")
        validate_live_inputs(root,llm_policy,case)
        if rule_knowledge is not None and rule_knowledge.sha256 != llm_policy.rule_knowledge_sha256:
            raise ValueError("DATA_POLICY_REJECTED: Korean Register rule excerpts hash is not approved")
        credentials = credential_status(llm_policy, dotenv_path=root / ".env")
        if not credentials['api_key_present'] or not credentials['project_configured']:
            raise ValueError("LLM_CREDENTIALS_MISSING")
        try:
            importlib.metadata.version('openai')
        except importlib.metadata.PackageNotFoundError:
            raise ValueError("LLM_SDK_MISSING: run uv sync --locked --extra llm") from None
        if history_path is not None:
            if not llm_policy.history_retrieval_approved or llm_policy.data_policy != DEV_DATA_POLICY:
                raise ValueError("DATA_POLICY_REJECTED: sending past report outcomes is not approved")
            embedding_client = OpenAIReportClient(allow_paid=allow_paid, project_id=llm_policy.project_id,
                                                  timeout=llm_policy.request_timeout_seconds, dotenv_path=root / ".env")
    run_id=str(uuid4())
    policy=Policy.model_validate(parse_json((root/'config/dev-policy.json').read_text(encoding='utf-8')))
    snapshot=load_public_case(root,case,run_id)
    simulator=Simulator.load(root/'simulation'/'dev'/case/'events.jsonl',run_id)
    report_records = {}
    if report_mode != "structured":
        report_path = root / "simulation" / "reports" / f"{case}.jsonl"
        report_records = load_report_records(report_path, require_script=report_mode == "fake")
        simulator.validate_report_schedule({key: record.delivery_tick for key, record in report_records.items()})
        if llm_policy is not None and len(report_records) > llm_policy.max_reports_per_run:
            raise ValueError("DATA_POLICY_REJECTED: case contains more reports than the policy permits")
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    repository=Repository(output/'run.sqlite3')
    repository.initialize(snapshot)
    controller=Controller(repository,policy)
    started=utc_now()
    store = retriever = None
    try:
        if history_path is not None:
            store = OutcomeStore(history_path)
            if embedding_client is not None:
                # Only outcomes of runs that finished before this one started.
                retriever = HistoryRetriever(store, embedding_client, embed_model, before=started,
                    data_policy=DEV_DATA_POLICY, exclude_run_id=run_id,
                    exclude_case_id=None if include_same_case else case, top_k=top_k)
        if llm_policy:
            ledger = BudgetLedger(root/'artifacts/llm-budget.sqlite3',llm_policy.project_id,
                llm_policy.report_budget_micro_usd,llm_policy.run_budget_micro_usd,llm_policy.project_budget_micro_usd)
            live_model = ResponsesReportModel(llm_policy,ledger,allow_paid=allow_paid,dotenv_path=root / ".env")
    except Exception:
        repository.close()
        if ledger is not None:
            ledger.close()
        if store is not None:
            store.close()
        raise

    def handle_arrivals(batch):
        if report_mode == "structured":
            return controller.handle_batch(batch)
        reports, scripts = [], {}
        for delivery in batch.deliveries:
            event = delivery.event
            record = report_records[event.event_id]
            reports.append(ReportDelivery(report=ReportInput(run_id=run_id,report_id=record.report_id,
                incident_id=event.incident_id,source_id=event.source_id,observed_tick=event.observed_tick,
                received_tick=batch.tick,text=record.text,target_hint=record.target_hint),
                published_evidence=delivery.published_evidence))
            if live_model is None:
                scripts[record.report_id] = record.responses
        return controller.handle_report_batch(batch.tick,batch.observations,reports,
                                             live_model or FakeReportModel(scripts),
                                             rule_knowledge=rule_knowledge, history=retriever)
    for progress in snapshot.execution_progress:
        phase=progress.current_phase
        if phase:
            simulator.register_completion(Observation(observation_id='initial-observation:'+progress.job_id,
                execution_id=phase.execution_id,instruction_id='initial:'+progress.job_id,job_id=progress.job_id,
                robot_id=progress.robot_id,phase=phase.phase,kind='PHASE_STARTED',tick=phase.start_tick,
                source_id='execution-observer',from_zone_id=phase.from_zone_id,to_zone_id=phase.to_zone_id),phase.end_tick)
    termination='FAILED'
    finished_normally=False
    tick=snapshot.now_tick
    try:
        for _ in range(1000):
            batch=simulator.due_events(tick)
            status=handle_arrivals(batch)
            for _microstep in range(100):
                current=repository.read_snapshot()
                due=[i for i in current.current_instructions if i.status=='PENDING' and i.action!='WAIT' and i.not_before_tick<=tick]
                for instruction in sorted(due,key=lambda i:({'MOVE':0,'SETUP':1,'WELD':2}[i.action],i.robot_id)):
                    observation=controller.authorize_phase_start(instruction.instruction_id)
                    if observation:
                        simulator.register_completion(observation,instruction.expected_end_tick)
                if simulator.next_tick()!=tick:
                    break
                status=handle_arrivals(simulator.due_events(tick))
            else:
                raise RuntimeError('zero-duration phase loop exceeded finite microstep bound')
            current=repository.read_snapshot()
            ticks=[i.not_before_tick for i in current.current_instructions if i.status=='PENDING' and i.action!='WAIT' and i.not_before_tick>tick]
            if simulator.next_tick() is not None:
                ticks.append(simulator.next_tick())
            if not ticks:
                failed=status not in {'OPTIMAL','FEASIBLE_NOT_PROVEN','NO_ELIGIBLE_WORK'}
                all_done=all(j.lifecycle=='COMPLETED' for j in current.jobs)
                termination='FAILED' if failed else 'COMPLETED' if all_done else 'WAITING'
                if termination == 'WAITING':
                    from .constraints import build_constraints
                    remaining = build_constraints(current, policy)
                    if remaining.required_job_ids:
                        repository.audit('FAILURE','NO_PROGRESS',{'reason_codes':['REQUIRED_WORK_WITHOUT_PENDING_TRANSITION']})
                        termination='FAILED'
                break
            tick=min(ticks)
        else:
            raise RuntimeError('development event limit exceeded')
        finished_normally=True
    except Exception as error:
        repository.audit('FAILURE','INTERNAL_ERROR',{'reason_codes':[type(error).__name__+': '+str(error)]})
        raise
    finally:
        llm_usage = None
        if live_model is not None:
            live_model.close()
            llm_usage = {'run':ledger.summary(run_id), 'project':ledger.summary()}
            ledger.close()
        repository.close()
        summary=export_run(output,termination)
        finished_at=utc_now()
        recorded=None
        recording_error=None
        if store is not None:
            try:
                if finished_normally:
                    recorded=store.record_run(output, case_id=case, data_policy=DEV_DATA_POLICY,
                        started_at=started, finished_at=finished_at, termination=termination)
            except (OSError, ValueError, sqlite3.Error) as error:
                # The run itself is finished; a failed outcome write must not cost us the manifest.
                recording_error=f'{type(error).__name__}: {error}'
                print(f'WARNING: run finished but outcomes were not recorded: {recording_error}', file=sys.stderr)
            finally:
                store.close()
        command=['uv','run','--locked',*(['--extra','llm'] if llm_policy else []),'python','-m','shipyard',
                 'run','--case',case,'--output',str(output),'--report-mode',report_mode]
        if llm_policy:
            command += ['--llm-policy',str(llm_policy_path),'--allow-paid']
        if rules_path is not None:
            command += ['--rules', str(rules_path)]
        if rule_topics_path is not None and rule_knowledge is not None:
            command += ['--rule-topics', str(rule_topics_path)]
        if history_path is not None:
            command += ['--history', str(history_path), '--top-k', str(top_k), '--embed-model', embed_model]
            if include_same_case:
                command.append('--history-include-same-case')
        manifest={'schema_version':'1.0','run_id':run_id,'case_id':case,'started_at':started,'finished_at':finished_at,
            'source_sha256':source_hash(root),'python':platform.python_version(),'platform':platform.platform(),'cpu':platform.machine(),
            'sqlite':sqlite3.sqlite_version,'packages':{p:importlib.metadata.version(p) for p in ['ortools','pydantic']},
            'policy':policy.model_dump(),'inputs':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in input_files},
            'report_mode':report_mode,'report_adapter':{'prompt_version':PROMPT_VERSION,'max_model_calls':MAX_MODEL_CALLS,'max_lookups':MAX_LOOKUPS} if report_mode!='structured' else None,
            'command':shlex.join(command),'argv':sys.argv,'unit':'abstract_tick'}
        if rule_knowledge is not None:
            manifest['rule_knowledge'] = {'file': rules_path.name, 'topics_file': rule_knowledge.topics_file,
                'sha256': rule_knowledge.sha256}
        if history_path is not None:
            manifest['history'] = {'store': history_path.name, 'recorded_outcomes': recorded,
                'recording_error': recording_error,
                'retrieval': dict(retriever.describe(), chat_model=llm_policy.model) if retriever else None}
        if llm_policy:
            manifest['llm'] = {'policy':llm_policy.model_dump(exclude={'project_id'}),
                'policy_sha256':hashlib.sha256(llm_policy_path.read_bytes()).hexdigest(),
                'sdk_version':importlib.metadata.version('openai'), 'generation_budget':llm_usage,
                'pricing_scope':'estimated text generation only; token-count billing unverified'}
        (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary


def case_policy_path(root: Path, case: str) -> Path:
    validate_case_id(case)
    return root / "config" / f"llm-policy.{case}.local.json"


def prepare_case_policy(case: str, root: Path, *, template: Path, output: Path,
                        rules_path: Path, approved: bool, rule_topics_path: Path | None = None) -> dict:
    """Freeze the selected synthetic inputs without contacting the provider."""
    from .responses import load_policy

    inputs = case_input_files(case, include_reports=True)
    load_public_case(root, case, "policy-preparation")
    simulator = Simulator.load(root / "simulation" / "dev" / case / "events.jsonl", "policy-preparation")
    reports = load_report_records(root / "simulation" / "reports" / f"{case}.jsonl")
    simulator.validate_report_schedule({key: record.delivery_tick for key, record in reports.items()})
    policy = load_policy(template)
    policy.approved_for_paid_calls = approved
    policy.max_reports_per_run = max(1, len(reports))
    policy.input_sha256 = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in inputs}
    # Development runs only store synthetic outcomes, so approval covers reusing them.
    policy.history_retrieval_approved = approved
    policy.rule_knowledge_sha256 = RuleKnowledge(rules_path, rule_topics_path or default_rule_topics(root)).sha256
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as file:
        file.write(policy.model_dump_json(indent=2) + "\n")
    return {"case_id": case, "policy": str(output), "report_count": len(reports), "approved": approved}


def run_live_case(args, root: Path) -> int:
    case = args.case
    local_policy = args.llm_policy or case_policy_path(root, case)
    if not local_policy.is_file():
        raise ValueError(f"LLM_POLICY_REQUIRED: run prepare-case-policy --case {case} --approve first")
    output = args.output or root / "outputs" / f"{case}-live-{uuid4().hex[:12]}"
    summary = run_case(
        case, output, root, report_mode="responses", allow_paid=args.allow_paid,
        llm_policy_path=local_policy, top_k=args.top_k, embed_model=args.embed_model,
        history_path=args.history or default_history_path(root),
        include_same_case=args.history_include_same_case, rule_topics_path=args.rule_topics,
        rules_path=args.rules or root / "references-local" / "kr-2026-welding-rules.jsonl",
    )
    print(json.dumps({"run_dir": str(output), **summary}, ensure_ascii=False, indent=2), flush=True)
    result = 1 if summary.get("termination") == "FAILED" else 0
    evaluation_dir = root / "evaluation" / "dev" / case
    oracle, truth = evaluation_dir / "oracle.json", evaluation_dir / "truth.jsonl"
    if not (oracle.is_file() and truth.is_file()):
        print("Evaluation skipped: the case needs oracle.json and truth.jsonl.")
        return result
    if parse_json(oracle.read_text(encoding="utf-8")).get("case_id") != case:
        raise ValueError("Evaluation oracle case ID does not match the run")
    scored = subprocess.run(
        [sys.executable, str(root / "evaluation" / "score.py"),
         "--run-dir", str(output), "--oracle", str(oracle), "--truth", str(truth)],
        check=False,
    )
    return result or scored.returncode


def main(argv=None):
    parser=argparse.ArgumentParser(description='용접 예외 처리 시뮬레이션 및 텍스트 리포트 검색')
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run')
    live=commands.add_parser('run-live', help='run and optionally score an approved synthetic case')
    for case_parser in (run, live):
        case_parser.add_argument('--case', required=True)
        case_parser.add_argument('--output', type=Path, required=case_parser is run)
        case_parser.add_argument('--llm-policy', type=Path)
        case_parser.add_argument('--allow-paid', action='store_true')
        case_parser.add_argument('--rules', type=Path, help='local KR rule excerpts JSONL')
        case_parser.add_argument('--rule-topics', type=Path, help='observation kind to rule query map')
        case_parser.add_argument('--history', type=Path,
                                 help='shared report-outcome store; recorded after the run, searched in responses mode')
        case_parser.add_argument('--history-include-same-case', action='store_true',
                                 help='also retrieve earlier outcomes of the same case ID')
        case_parser.add_argument('--embed-model', default=DEFAULT_EMBED_MODEL)
        case_parser.add_argument('--top-k', type=int, default=DEFAULT_TOP_K)
    run.add_argument('--report-mode',choices=['structured','fake','responses'],default='structured')
    prepare=commands.add_parser('prepare-case-policy', help='freeze a synthetic case without any API calls')
    prepare.add_argument('--case', required=True)
    prepare.add_argument('--output', type=Path)
    prepare.add_argument('--template', type=Path)
    prepare.add_argument('--rules', type=Path)
    prepare.add_argument('--rule-topics', type=Path)
    prepare.add_argument('--approve', action='store_true', help='approve sending these reviewed synthetic inputs')
    preflight=commands.add_parser('llm-preflight')
    preflight.add_argument('--llm-policy',type=Path,default=ROOT/'config/llm-policy.json')
    preflight.add_argument('--check-access',action='store_true')
    inspect=commands.add_parser('inspect');inspect.add_argument('--run-dir',type=Path,required=True)
    search = commands.add_parser('search-rules')
    search.add_argument('--rules', type=Path, required=True)
    search.add_argument('query')
    for name in ('search-reports', 'analyze-report'):
        reports_command = commands.add_parser(name)
        reports_command.add_argument('--history', type=Path, default=default_history_path(ROOT))
        reports_command.add_argument('--embed-model', default=DEFAULT_EMBED_MODEL)
        reports_command.add_argument('--exclude-case', help='leave out outcomes recorded for this case ID')
        query = reports_command.add_mutually_exclusive_group(required=True)
        query.add_argument('--text')
        query.add_argument('--report-file', type=Path)
        reports_command.add_argument('--report-id')
        reports_command.add_argument('--top-k', type=int, default=DEFAULT_TOP_K)
        reports_command.add_argument('--project-id')
        reports_command.add_argument('--allow-paid', action='store_true')
        if name == 'analyze-report':
            reports_command.add_argument('--chat-model', default=DEFAULT_CHAT_MODEL)
            reports_command.add_argument('--rules', type=Path,
                                         default=ROOT / 'references-local/kr-2026-welding-rules.jsonl')
            reports_command.add_argument('--rule-topics', type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command == 'prepare-case-policy':
            result = prepare_case_policy(
                args.case, ROOT, template=args.template or ROOT / "config" / "llm-policy.json",
                output=args.output or case_policy_path(ROOT, args.case), approved=args.approve,
                rules_path=args.rules or ROOT / "references-local" / "kr-2026-welding-rules.jsonl",
                rule_topics_path=args.rule_topics,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == 'run-live':
            return run_live_case(args, ROOT)
        if args.command in {'search-reports', 'analyze-report'}:
            text = args.report_file.read_text(encoding="utf-8-sig") if args.report_file else args.text
            if not args.history.is_file():
                raise ValueError(f"No outcome store at {args.history}. Run a case with --history first.")
            client = OpenAIReportClient(allow_paid=args.allow_paid, project_id=args.project_id, dotenv_path=ROOT / ".env")
            store = OutcomeStore(args.history)
            try:
                index = HistoryRetriever(store, client, args.embed_model, before=utc_now(),
                                         data_policy=DEV_DATA_POLICY, exclude_case_id=args.exclude_case,
                                         top_k=args.top_k)
                if args.command == 'analyze-report':
                    rules = RuleKnowledge(args.rules, args.rule_topics or default_rule_topics(ROOT))
                    result = analyze_report(index, client, args.chat_model, text, args.report_id,
                                            rule_knowledge=rules)
                else:
                    result = index.search(text, args.report_id)
            finally:
                store.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'search-rules':
            print(json.dumps(RuleKnowledge(args.rules).search(args.query), ensure_ascii=False, indent=2))
            return 0
        if args.command=='llm-preflight':
            from .responses import load_policy, preflight as inspect_llm
            dotenv_path = ROOT / ".env"
            report = inspect_llm(load_policy(args.llm_policy, dotenv_path=dotenv_path),
                                 check_access=args.check_access, dotenv_path=dotenv_path)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        summary=run_case(args.case,args.output,report_mode=args.report_mode,
                         llm_policy_path=args.llm_policy,allow_paid=args.allow_paid,
                         rules_path=args.rules, history_path=args.history,
                         include_same_case=args.history_include_same_case, rule_topics_path=args.rule_topics,
                         embed_model=args.embed_model, top_k=args.top_k) if args.command=='run' else inspect_run(args.run_dir)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        return 1 if summary.get('termination')=='FAILED' else 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as error:
        print(f'{type(error).__name__}: {error}',file=sys.stderr)
        return 2
