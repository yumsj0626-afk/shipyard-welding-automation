import csv
import json
from pathlib import Path
import pytest
from shipyard.cli import run_case, main
from evaluation.score import score_run

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('case,done,requests',[
    ('D01',{'W17','W18','W19','W20','W21'},0),
    ('D02',{'W17','W18','W20'},0),
    ('D03',{'W17','W18','W19','W20','W21'},0),
    ('D04',{'W18','W19','W21'},1),
])
def test_end_to_end_and_independent_score(tmp_path,case,done,requests):
    output=tmp_path/case
    summary=run_case(case,output)
    assert set(summary['completed_jobs'])==done
    assert not summary['failures']
    assert summary['additional_human_requests']==requests
    result=score_run(output,ROOT/'evaluation/dev'/case/'oracle.json',ROOT/'evaluation/dev'/case/'truth.jsonl')
    assert result['acceptance_passed'],result
    with (output/'execution.csv').open() as file:
        rows=list(csv.DictReader(file))
    w18=[r for r in rows if r['job_id']=='W18']
    assert [(r['phase'],int(r['start_tick']),int(r['end_tick'])) for r in w18]==[('WELD',-2,2)]
    assert len({r['execution_id'] for r in rows})==len(rows)
    for r in rows:
        if r['job_id']=='W20':
            w17=next(x for x in rows if x['job_id']=='W17' and x['phase']=='WELD')
            assert int(r['start_tick'])>=int(w17['end_tick'])
    if case=='D01':
        assert any(r['job_id'] in {'W19','W21'} and r['phase']=='WELD' and int(r['start_tick'])<6 for r in rows)
        assert all(int(r['start_tick'])>=6 for r in rows if r['job_id']=='W17')
    if case=='D02':
        trace=[json.loads(l) for l in (output/'trace.jsonl').read_text().splitlines()]
        assert any(r['stage']=='WAIT' and r['tick']==0 and r['data']['robot_id']=='R1' and r['data']['reason_codes'] and r['data']['recheck_triggers'] for r in trace)
    if case=='D03': assert all(r['robot_id']=='R2' for r in rows if r['job_id']=='W17')
    if case=='D04': assert all(r['job_id'] not in {'W17','W20'} for r in rows)
    manifest=json.loads((output/'manifest.json').read_text())
    assert manifest['python']=='3.13.18' and manifest['source_sha256']
    assert main(['inspect','--run-dir',str(output)])==0

def test_never_overwrites_a_previous_run(tmp_path):
    out=tmp_path/'existing';out.mkdir();marker=out/'keep.txt';marker.write_text('original')
    assert main(['run','--case','D01','--output',str(out)])==2
    assert marker.read_text()=='original'
    assert not (out/'run.sqlite3').exists()
