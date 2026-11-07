"""Filesystem composition root: public data and private simulator load separately."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import sqlite3
import sys
import shlex
from pathlib import Path
from uuid import uuid4

from .contracts import (
    EvidenceData, InitialData, Instruction, Observation, PlanningSnapshot, Policy,
    canonical, parse_json, utc_now,
)
from .controller import Controller
from .repository import Repository
from .reporting import export_run, inspect_run
from .simulator import Simulator
from .reports import (FakeReportModel, FakeReportRecord, ReportDelivery, ReportInput,
                      MAX_MODEL_CALLS, MAX_LOOKUPS, PROMPT_VERSION)

ROOT = Path(__file__).resolve().parents[2]


def load_public_case(root: Path, case: str, run_id: str) -> PlanningSnapshot:
    if case not in {'D01','D02','D03','D04'}:
        raise ValueError('unknown development case')
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


def source_hash(root: Path) -> str:
    files=sorted((root/'src'/'shipyard').glob('*.py'))+sorted((root/'src'/'shipyard').glob('*.sql'))
    files += [root/'pyproject.toml',root/'uv.lock']
    value=hashlib.sha256()
    for file in files:
        value.update(str(file.relative_to(root)).encode());value.update(file.read_bytes())
    return value.hexdigest()


def run_case(case: str, output: Path, root: Path=ROOT, *, report_mode: str="structured",
             llm_policy_path: Path | None=None, allow_paid: bool=False) -> dict:
    if report_mode not in {"structured", "fake", "responses"}:
        raise ValueError("unknown report mode")
    llm_policy = None
    live_model = ledger = None
    if report_mode == "responses":
        from .responses import load_policy, credential_status, validate_live_inputs, ResponsesReportModel
        from .llm_budget import BudgetLedger
        if llm_policy_path is None:
            raise ValueError("LLM_POLICY_REQUIRED")
        llm_policy = load_policy(llm_policy_path)
        if not allow_paid or not llm_policy.approved_for_paid_calls:
            raise ValueError("PAID_CALLS_NOT_APPROVED")
        validate_live_inputs(root,llm_policy,case)
        credentials = credential_status(llm_policy)
        if not credentials['api_key_present'] or not credentials['project_configured']:
            raise ValueError("LLM_CREDENTIALS_MISSING")
        try:
            importlib.metadata.version('openai')
        except importlib.metadata.PackageNotFoundError:
            raise ValueError("LLM_SDK_MISSING: run uv sync --locked --extra llm") from None
    report_path = root/'simulation'/'reports'/f'{case}.jsonl'
    report_records = {}
    if report_mode != "structured":
        for line in report_path.read_text(encoding='utf-8').splitlines():
            record = FakeReportRecord.model_validate(parse_json(line))
            if record.report_id in report_records:
                raise ValueError("duplicate fake report ID")
            report_records[record.report_id] = record
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    run_id=str(uuid4())
    policy=Policy.model_validate(parse_json((root/'config/dev-policy.json').read_text(encoding='utf-8')))
    snapshot=load_public_case(root,case,run_id)
    simulator=Simulator.load(root/'simulation'/'dev'/case/'events.jsonl',run_id)
    repository=Repository(output/'run.sqlite3')
    repository.initialize(snapshot)
    controller=Controller(repository,policy)
    try:
        if llm_policy:
            ledger = BudgetLedger(root/'artifacts/llm-budget.sqlite3',llm_policy.project_id,
                llm_policy.report_budget_micro_usd,llm_policy.run_budget_micro_usd,llm_policy.project_budget_micro_usd)
            live_model = ResponsesReportModel(llm_policy,ledger,allow_paid=allow_paid)
    except Exception:
        repository.close()
        if ledger is not None:
            ledger.close()
        raise

    def handle_arrivals(batch):
        if report_mode == "structured":
            return controller.handle_batch(batch)
        reports, scripts = [], {}
        for delivery in batch.deliveries:
            event = delivery.event
            record = report_records.get(event.event_id)
            if record is None or record.delivery_tick != batch.tick:
                raise ValueError("missing fake report or delivery tick mismatch")
            reports.append(ReportDelivery(report=ReportInput(run_id=run_id,report_id=record.report_id,
                incident_id=event.incident_id,source_id=event.source_id,observed_tick=event.observed_tick,
                received_tick=batch.tick,text=record.text,target_hint=record.target_hint),
                published_evidence=delivery.published_evidence))
            scripts[record.report_id] = record.responses
        return controller.handle_report_batch(batch.tick,batch.observations,reports,
                                             live_model if live_model is not None else FakeReportModel(scripts))
    for progress in snapshot.execution_progress:
        phase=progress.current_phase
        if phase:
            simulator.register_completion(Observation(observation_id='initial-observation:'+progress.job_id,
                execution_id=phase.execution_id,instruction_id='initial:'+progress.job_id,job_id=progress.job_id,
                robot_id=progress.robot_id,phase=phase.phase,kind='PHASE_STARTED',tick=phase.start_tick,
                source_id='execution-observer',from_zone_id=phase.from_zone_id,to_zone_id=phase.to_zone_id),phase.end_tick)
    started=utc_now()
    termination='FAILED'
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
        input_paths=[root/'config/dev-policy.json',root/'data/dev'/case/'initial.json',root/'data/dev'/case/'evidence.json',root/'simulation/dev'/case/'events.jsonl']
        if report_mode != 'structured':
            input_paths.append(report_path)
        command=['uv','run','--locked',*(['--extra','llm'] if llm_policy else []),'python','-m','shipyard',
                 'run','--case',case,'--output',str(output),'--report-mode',report_mode]
        if llm_policy:
            command += ['--llm-policy',str(llm_policy_path),'--allow-paid']
        manifest={'schema_version':'1.0','run_id':run_id,'case_id':case,'started_at':started,'finished_at':utc_now(),
            'source_sha256':source_hash(root),'python':platform.python_version(),'platform':platform.platform(),'cpu':platform.machine(),
            'sqlite':sqlite3.sqlite_version,'packages':{p:importlib.metadata.version(p) for p in ['ortools','pydantic']},
            'policy':policy.model_dump(),'inputs':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in input_paths},
            'report_mode':report_mode,'report_adapter':{'prompt_version':PROMPT_VERSION,'max_model_calls':MAX_MODEL_CALLS,'max_lookups':MAX_LOOKUPS} if report_mode!='structured' else None,
            'command':shlex.join(command),'argv':sys.argv,'unit':'abstract_tick'}
        if llm_policy:
            manifest['llm'] = {'policy':llm_policy.model_dump(exclude={'project_id'}),
                'policy_sha256':hashlib.sha256(llm_policy_path.read_bytes()).hexdigest(),
                'sdk_version':importlib.metadata.version('openai'), 'generation_budget':llm_usage,
                'pricing_scope':'estimated text generation only; token-count billing unverified'}
        (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description='용접 예외 처리 시뮬레이션 (정형 사건 / fake / Responses 문장 보고)')
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run');run.add_argument('--case',choices=['D01','D02','D03','D04'],required=True);run.add_argument('--output',type=Path,required=True)
    run.add_argument('--report-mode',choices=['structured','fake','responses'],default='structured')
    run.add_argument('--llm-policy',type=Path)
    run.add_argument('--allow-paid',action='store_true')
    preflight=commands.add_parser('llm-preflight')
    preflight.add_argument('--llm-policy',type=Path,default=ROOT/'config/llm-policy.json')
    preflight.add_argument('--check-access',action='store_true')
    inspect=commands.add_parser('inspect');inspect.add_argument('--run-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='llm-preflight':
            from .responses import load_policy, preflight as inspect_llm
            print(json.dumps(inspect_llm(load_policy(args.llm_policy),check_access=args.check_access),ensure_ascii=False,indent=2))
            return 0
        summary=run_case(args.case,args.output,report_mode=args.report_mode,
                         llm_policy_path=args.llm_policy,allow_paid=args.allow_paid) if args.command=='run' else inspect_run(args.run_dir)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        return 1 if summary.get('termination')=='FAILED' else 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as error:
        print(f'{type(error).__name__}: {error}',file=sys.stderr)
        return 2
