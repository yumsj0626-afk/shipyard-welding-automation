"""Filesystem composition root: public data and private simulator load separately."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import sqlite3
import sys
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


def run_case(case: str, output: Path, root: Path=ROOT) -> dict:
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    run_id=str(uuid4())
    policy=Policy.model_validate(parse_json((root/'config/dev-policy.json').read_text(encoding='utf-8')))
    snapshot=load_public_case(root,case,run_id)
    simulator=Simulator.load(root/'simulation'/'dev'/case/'events.jsonl',run_id)
    repository=Repository(output/'run.sqlite3')
    repository.initialize(snapshot)
    controller=Controller(repository,policy)
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
            status=controller.handle_batch(batch)
            for _microstep in range(100):
                current=repository.read_snapshot()
                due=[i for i in current.current_instructions if i.status=='PENDING' and i.action!='WAIT' and i.not_before_tick<=tick]
                for instruction in sorted(due,key=lambda i:({'MOVE':0,'SETUP':1,'WELD':2}[i.action],i.robot_id)):
                    observation=controller.authorize_phase_start(instruction.instruction_id)
                    if observation:
                        simulator.register_completion(observation,instruction.expected_end_tick)
                if simulator.next_tick()!=tick:
                    break
                status=controller.handle_batch(simulator.due_events(tick))
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
        repository.close()
        summary=export_run(output,termination)
        input_paths=[root/'config/dev-policy.json',root/'data/dev'/case/'initial.json',root/'data/dev'/case/'evidence.json',root/'simulation/dev'/case/'events.jsonl']
        manifest={'schema_version':'1.0','run_id':run_id,'case_id':case,'started_at':started,'finished_at':utc_now(),
            'source_sha256':source_hash(root),'python':platform.python_version(),'platform':platform.platform(),'cpu':platform.machine(),
            'sqlite':sqlite3.sqlite_version,'packages':{p:importlib.metadata.version(p) for p in ['ortools','pydantic']},
            'policy':policy.model_dump(),'inputs':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in input_paths},
            'command':f'uv run --locked python -m shipyard run --case {case} --output {output}','argv':sys.argv,'unit':'abstract_tick'}
        (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description='정형 사건 기반 용접 예외 처리 시뮬레이션')
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run');run.add_argument('--case',choices=['D01','D02','D03','D04'],required=True);run.add_argument('--output',type=Path,required=True)
    inspect=commands.add_parser('inspect');inspect.add_argument('--run-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        summary=run_case(args.case,args.output) if args.command=='run' else inspect_run(args.run_dir)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        return 1 if summary.get('termination')=='FAILED' else 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as error:
        print(f'{type(error).__name__}: {error}',file=sys.stderr)
        return 2
