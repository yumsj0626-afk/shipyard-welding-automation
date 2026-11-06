from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest

from shipyard.cli import ROOT, load_public_case
from shipyard.contracts import ArrivalBatch, Policy
from shipyard.controller import Controller
from shipyard.repository import Repository, StaleStateError

@pytest.fixture
def controller(tmp_path):
    repo=Repository(tmp_path/'run.sqlite3');repo.initialize(load_public_case(ROOT,'D01','apply-run'))
    yield Controller(repo,Policy())
    repo.close()


def test_change_between_check_and_apply_replans_from_new_snapshot(controller,monkeypatch):
    repo=controller.repository;actual_apply=repo.apply_plan;calls=[]
    def race(plan,check,instructions):
        calls.append(plan.based_on_state_version)
        if len(calls)==1:
            state=repo.read_snapshot();state.jobs[2].due_tick+=1
            repo.commit_snapshot(state,state.state_version,'TEST_EXTERNAL_CHANGE')
        return actual_apply(plan,check,instructions)
    monkeypatch.setattr(repo,'apply_plan',race)
    assert controller.plan_and_apply()=='OPTIMAL'
    assert calls==[0,1]
    assert repo.read_snapshot().active_plan_version==1
    assert sum(r['outcome']=='STALE_PLAN' for r in repo.audit_records())==1


def test_stale_retries_are_bounded_without_partial_activation(controller,monkeypatch):
    repo=controller.repository;actual_apply=repo.apply_plan
    def race(plan,check,instructions):
        state=repo.read_snapshot();state.jobs[2].due_tick+=1
        repo.commit_snapshot(state,state.state_version,'TEST_EXTERNAL_CHANGE')
        return actual_apply(plan,check,instructions)
    monkeypatch.setattr(repo,'apply_plan',race)
    assert controller.plan_and_apply()=='RETRY_EXHAUSTED'
    assert repo.read_snapshot().active_plan_version==0
    assert len(repo.read_snapshot().current_instructions)==1
    assert sum(r['outcome']=='STALE_PLAN' for r in repo.audit_records())==3


def test_fault_after_instruction_write_rolls_back_activation(controller,monkeypatch):
    repo=controller.repository;before=repo.read_snapshot();actual=repo._sync_entities
    def fault(state):
        actual(state)
        if state.active_plan_version:
            raise RuntimeError('injected storage fault after instruction rows')
    monkeypatch.setattr(repo,'_sync_entities',fault)
    with pytest.raises(RuntimeError,match='injected'):
        controller.plan_and_apply()
    assert repo.read_snapshot()==before
    assert repo._conn.execute('SELECT count(*) FROM instructions').fetchone()[0]==1
    assert not any(r['stage']=='APPLY' for r in repo.audit_records())


def test_simultaneous_writers_only_one_can_commit(controller):
    repo=controller.repository;barrier=Barrier(2)
    def write():
        other=Repository(repo.path)
        try:
            state=other.read_snapshot();state.now_tick=1;barrier.wait(timeout=3)
            try:
                other.commit_snapshot(state,state.state_version,'RACE')
                return 'COMMITTED'
            except StaleStateError:
                return 'STALE'
        finally: other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:write(),range(2)))
    assert sorted(results)==['COMMITTED','STALE']
    assert repo.read_snapshot().state_version==1
    assert len([r for r in repo.audit_records() if r['stage']=='RACE'])==1


def test_duplicate_start_and_inactive_instruction_never_execute_twice(controller):
    controller.handle_batch(ArrivalBatch(tick=0))
    repo=controller.repository
    first=next(i for i in repo.read_snapshot().current_instructions if i.action=='MOVE' and i.not_before_tick==0)
    observation=controller.authorize_phase_start(first.instruction_id)
    assert observation is not None
    version=repo.read_snapshot().state_version
    assert controller.authorize_phase_start(first.instruction_id) is None
    assert repo.read_snapshot().state_version==version
    controller.plan_and_apply()
    assert controller.authorize_phase_start(first.instruction_id) is None


@pytest.mark.parametrize('status',['INFEASIBLE','NO_SOLUTION_WITHIN_LIMIT','MODEL_INVALID','INTERNAL_ERROR'])
def test_failure_never_activates_new_plan_or_claims_normal_wait(controller,monkeypatch,status):
    from shipyard import scheduler
    actual=scheduler.solve
    def fail(snapshot,constraints,policy):
        plan=actual(snapshot,constraints,policy)
        plan.solver_result.status=status;plan.assignments=[];return plan.seal()
    monkeypatch.setattr(scheduler,'solve',fail)
    assert controller.plan_and_apply()==status
    current=controller.repository.read_snapshot()
    assert current.active_plan_version==0
    assert current.jobs[1].lifecycle=='RUNNING'
    assert not any(r['stage']=='WAIT' for r in controller.repository.audit_records())


def test_feasible_unproven_candidate_may_apply_but_keeps_status(controller,monkeypatch):
    from shipyard import scheduler
    actual=scheduler.solve
    def unproven(snapshot,constraints,policy):
        plan=actual(snapshot,constraints,policy)
        plan.solver_result.status='FEASIBLE_NOT_PROVEN';plan.solver_result.proven_stages=1
        return plan.seal()
    monkeypatch.setattr(scheduler,'solve',unproven)
    assert controller.plan_and_apply()=='FEASIBLE_NOT_PROVEN'
    assert controller.repository.read_snapshot().active_plan_version==1
    assert any(r['stage']=='PLAN' and r['outcome']=='FEASIBLE_NOT_PROVEN' for r in controller.repository.audit_records())
