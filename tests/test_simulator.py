from pathlib import Path
import pytest
from shipyard.cli import ROOT,load_public_case
from shipyard.contracts import ArrivalBatch,Delivery,Policy,parse_json
from shipyard.controller import Controller
from shipyard.repository import Repository
from shipyard.simulator import Simulator


def make_hold(snapshot,job,tick):
    delivery=Delivery.model_validate(parse_json((ROOT/'simulation/dev/D01/events.jsonl').read_text().splitlines()[0]))
    delivery.event.run_id=snapshot.run_id
    delivery.event.event_id='E-hold-'+job
    delivery.event.incident_id='INC-'+job
    delivery.event.target.job_id=job
    delivery.event.received_tick=tick;delivery.event.observed_tick=tick;delivery.delivery_tick=tick
    delivery.event.evidence_ids=['EV-hold-'+job]
    evidence=delivery.published_evidence[0]
    evidence.evidence_id='EV-hold-'+job;evidence.incident_id='INC-'+job;evidence.target.job_id=job
    evidence.available_tick=tick;evidence.observed_tick=tick
    return delivery


@pytest.mark.parametrize('when',['during_move','after_move','during_setup'])
def test_new_hold_preserves_current_phase_then_cancels_preparation(tmp_path,when):
    repo=Repository(tmp_path/'run.sqlite3');repo.initialize(load_public_case(ROOT,'D01','phase-run'))
    ctrl=Controller(repo,Policy());sim=Simulator([])
    first=make_hold(repo.read_snapshot(),'W17',0)
    ctrl.handle_batch(ArrivalBatch(tick=0,deliveries=[first]))
    move=next(i for i in repo.read_snapshot().current_instructions if i.job_id=='W19' and i.action=='MOVE')
    start=ctrl.authorize_phase_start(move.instruction_id);assert start
    sim.register_completion(start,move.expected_end_tick)
    old_setup=next(i.instruction_id for i in repo.read_snapshot().current_instructions if i.job_id=='W19' and i.action=='SETUP')
    if when=='during_move':
        ctrl.handle_batch(ArrivalBatch(tick=0,deliveries=[make_hold(repo.read_snapshot(),'W19',0)]))
        assert next(r for r in repo.read_snapshot().robots if r.robot_id=='R1').phase=='MOVING'
        ctrl.handle_batch(sim.due_events(1))
    elif when=='after_move':
        batch=sim.due_events(1);batch.deliveries=[make_hold(repo.read_snapshot(),'W19',1)]
        ctrl.handle_batch(batch)
    else:
        ctrl.handle_batch(sim.due_events(1))
        setup=next(i for i in repo.read_snapshot().current_instructions if i.job_id=='W19' and i.action=='SETUP' and i.status=='PENDING')
        setup_start=ctrl.authorize_phase_start(setup.instruction_id);assert setup_start
        sim.register_completion(setup_start,setup.expected_end_tick)
        ctrl.handle_batch(ArrivalBatch(tick=1,deliveries=[make_hold(repo.read_snapshot(),'W19',1)]))
        assert next(r for r in repo.read_snapshot().robots if r.robot_id=='R1').phase=='SETTING_UP'
        ctrl.handle_batch(sim.due_events(2))
    state=repo.read_snapshot()
    job=next(j for j in state.jobs if j.job_id=='W19')
    robot=next(r for r in state.robots if r.robot_id=='R1')
    assert job.lifecycle=='NOT_STARTED' and job.actual_weld_start is None
    assert robot.location_zone_id=='C' and robot.phase=='IDLE'
    assert next(z for z in state.zones if z.zone_id=='C').active_execution_ids==[]
    assert ctrl.authorize_phase_start(old_setup) is None
    assert not any(r['stage']=='PHASE_START' and r['data']['observation']['job_id']=='W19' and r['data']['observation']['phase']=='WELD' for r in repo.audit_records())
    repo.close()


def test_future_queue_returns_only_arrived_deliveries():
    sim=Simulator.load(ROOT/'simulation/dev/D01/events.jsonl','queue-run')
    first=sim.due_events(0)
    assert len(first.deliveries)==1
    assert not sim.due_events(5).deliveries
    due=sim.due_events(6)
    assert {d.event.kind for d in due.deliveries}=={'REPAIR_REPORTED','RECHECK_REPORTED'}
    assert not sim.due_events(6).deliveries
