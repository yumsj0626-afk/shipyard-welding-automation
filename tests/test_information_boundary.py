import ast
import json
import shutil
from pathlib import Path

from shipyard import scheduler
from shipyard.cli import ROOT, load_public_case, run_case
from shipyard.contracts import canonical


def test_dependency_boundaries_are_executable_rules():
    forbidden={
        'controller':{'simulator','evaluation','cli'},
        'scheduler':{'simulator','evaluation','repository','cli'},
        'constraints':{'simulator','evaluation','repository','cli'},
        'checker':{'constraints','scheduler','simulator','evaluation','repository','cli'},
        'repository':{'scheduler','simulator','evaluation','controller','cli'},
    }
    for module,blocked in forbidden.items():
        tree=ast.parse((ROOT/'src/shipyard'/f'{module}.py').read_text())
        imported=set()
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                imported.update(n.name.split('.')[-1] for n in node.names)
            elif isinstance(node,ast.ImportFrom):
                imported.update((node.module or '').split('.'))
                imported.update(n.name for n in node.names)
        assert not imported&blocked,(module,imported&blocked)
    evaluator=ast.parse((ROOT/'evaluation/score.py').read_text())
    assert not any(isinstance(n,ast.ImportFrom) and (n.module or '').startswith('shipyard') for n in ast.walk(evaluator))


def test_public_loader_reads_only_the_two_public_files(monkeypatch):
    opened=[];original=Path.open
    allowed={(ROOT/'data/dev/D01/initial.json').resolve(),(ROOT/'data/dev/D01/evidence.json').resolve()}
    def guarded(path,*args,**kwargs):
        assert path.resolve() in allowed,f'forbidden public loader read: {path}'
        opened.append(path.resolve());return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',guarded)
    snapshot=load_public_case(ROOT,'D01','boundary-run')
    assert set(opened)==allowed
    assert not snapshot.evidence


def test_future_and_oracle_sentinels_cannot_reach_planning_before_arrival(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir()
    for name in ('data','simulation','config','src','evaluation'):
        shutil.copytree(ROOT/name,root/name,ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('pyproject.toml','uv.lock'): shutil.copy2(ROOT/name,root/name)
    events=root/'simulation/dev/D01/events.jsonl'
    deliveries=[json.loads(line) for line in events.read_text().splitlines()]
    for delivery in deliveries:
        if delivery['delivery_tick']==6:
            delivery['event']['raw_report']='FUTURE_SENTINEL_7ba8'
            for evidence in delivery['published_evidence']:
                evidence['provenance']['rationale']='FUTURE_SENTINEL_7ba8'
    events.write_text('\n'.join(json.dumps(x) for x in deliveries)+'\n')
    (root/'evaluation/dev/D01/oracle.json').write_text('{"secret":"ORACLE_SENTINEL_3a91"}')
    (root/'evaluation/dev/D01/truth.jsonl').write_text('{"secret":"TRUTH_SENTINEL_f81a"}\n')
    seen=[];original_solve=scheduler.solve
    def observed_solve(snapshot,constraints,policy):
        encoded=canonical(snapshot)+canonical(constraints)
        assert 'ORACLE_SENTINEL' not in encoded and 'TRUTH_SENTINEL' not in encoded
        assert all(e.available_tick<=snapshot.now_tick for e in snapshot.evidence)
        if snapshot.now_tick<6:
            assert 'FUTURE_SENTINEL' not in encoded
            assert 'EV-repair-W17' not in encoded and 'EV-recheck-W17' not in encoded
        seen.append((snapshot.now_tick,'FUTURE_SENTINEL' in encoded))
        return original_solve(snapshot,constraints,policy)
    original_open=Path.open
    def guard(path,*args,**kwargs):
        mode=args[0] if args else kwargs.get('mode','r')
        if 'r' in mode and 'evaluation' in path.parts:
            raise AssertionError(f'runtime attempted evaluator read: {path}')
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(scheduler,'solve',observed_solve)
    monkeypatch.setattr(Path,'open',guard)
    result=run_case('D01',tmp_path/'output',root)
    assert len(result['completed_jobs'])==5
    assert any(t<6 for t,_ in seen)
    assert any(t>=6 and visible for t,visible in seen)
