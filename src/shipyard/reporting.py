"""Read-only exports; the database remains authoritative."""
import csv
import json
import sqlite3
from pathlib import Path
from .contracts import PlanningSnapshot


def inspect_run(run_dir: Path) -> dict:
    return json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))


def export_run(run_dir: Path, termination: str) -> dict:
    db = sqlite3.connect(f"{(run_dir/'run.sqlite3').resolve().as_uri()}?mode=ro", uri=True)
    try:
        snapshot = PlanningSnapshot.model_validate_json(db.execute("SELECT snapshot_json FROM runs").fetchone()[0])
        trace = [dict(json.loads(row[1]),seq=row[0]) for row in db.execute("SELECT log_seq,payload_json FROM audit_log ORDER BY log_seq")]
        instructions = {row[0]:json.loads(row[1]) for row in db.execute("SELECT instruction_id,payload_json FROM instructions")}
    finally:
        db.close()
    (run_dir/'trace.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n' for row in trace),encoding='utf-8')
    completed = {}
    for progress in snapshot.execution_progress:
        for phase in progress.completed_phases:
            completed[phase.execution_id] = (progress,phase)
    starts = {}
    for row in trace:
        if row['stage'] == 'PHASE_START':
            obs=row['data']['observation'];starts[obs['execution_id']]=obs
    executions = []
    for row in trace:
        if row['stage'] != 'PHASE_END':
            continue
        obs = row['data']['observation']
        instruction = instructions[obs['instruction_id']]
        if obs['execution_id'] in starts:
            start=starts[obs['execution_id']]['tick']
        elif obs['execution_id'] in completed:
            start=completed[obs['execution_id']][1].start_tick
        else:
            raise ValueError('missing actual start for completed phase')
        executions.append(dict(execution_id=obs['execution_id'],instruction_id=obs['instruction_id'],plan_id=instruction['plan_id'],attempt_id=instruction['attempt_id'],job_id=obs['job_id'],robot_id=obs['robot_id'],phase=obs['phase'],start_tick=start,end_tick=obs['tick'],from_zone_id=obs['from_zone_id'],to_zone_id=obs['to_zone_id'],zone_id=obs['to_zone_id'] if obs['phase']!='MOVE' else ''))
    fields=['execution_id','instruction_id','plan_id','attempt_id','job_id','robot_id','phase','start_tick','end_tick','from_zone_id','to_zone_id','zone_id']
    with (run_dir/'execution.csv').open('w',newline='',encoding='utf-8') as file:
        writer=csv.DictWriter(file,fieldnames=fields);writer.writeheader();writer.writerows(executions)
    last_plan = next((row['data']['plan'] for row in reversed(trace) if row['stage']=='PLAN'),None)
    summary={
        'schema_version':'1.0','run_id':snapshot.run_id,'termination':termination,'now_tick':snapshot.now_tick,
        'state_version':snapshot.state_version,'active_plan_version':snapshot.active_plan_version,
        'completed_jobs':[j.job_id for j in snapshot.jobs if j.lifecycle=='COMPLETED'],
        'unfinished_jobs':[j.model_dump(mode='json') for j in snapshot.jobs if j.lifecycle!='COMPLETED'],
        'active_restrictions':[r.model_dump(mode='json') for r in snapshot.active_restrictions],
        'deferred_jobs':last_plan['deferred_jobs'] if last_plan else [],
        'additional_human_requests':sum(r['stage']=='HUMAN_REQUEST' for r in trace),
        'failures':[{'seq':r['seq'],'outcome':r['outcome']} for r in trace if r['stage']=='FAILURE' or (r['stage']=='CHECK' and r['outcome'] not in {'PASS','PASSED'})],
        'completed_phases':len(executions),'evaluation':'NOT_SCORED_BY_RUNTIME',
    }
    (run_dir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary
