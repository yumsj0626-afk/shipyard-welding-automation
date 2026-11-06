import copy
import pytest
from pydantic import ValidationError
from shipyard.contracts import InitialData, PlanningSnapshot, parse_json, Target, Evidence, Delivery

@pytest.mark.parametrize('text', ['{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'])
def test_json_rejects_ambiguous_values(text):
    with pytest.raises(ValueError): parse_json(text)

@pytest.mark.parametrize('value', ['4', True, -1, 1.5])
def test_strict_integer(initial,value):
    data=initial.model_dump();data['jobs'][0]['duration_ticks']=value
    with pytest.raises(ValidationError): InitialData.model_validate(data)

@pytest.mark.parametrize('field', ['future_recovery_tick','oracle','truth','simulator'])
def test_snapshot_forbids_private_fields(snapshot,field):
    data=snapshot.model_dump();data[field]='private-marker'
    with pytest.raises(ValidationError): PlanningSnapshot.model_validate(data)

@pytest.mark.parametrize('mutation', ['unknown_pred','cycle','missing_travel','unknown_robot','duplicate_id','unicode_id'])
def test_initial_references_and_graph(initial,mutation):
    data=initial.model_dump()
    if mutation=='unknown_pred': data['jobs'][0]['predecessor_ids']=['absent']
    if mutation=='cycle': data['jobs'][0]['predecessor_ids']=['W20']
    if mutation=='missing_travel': data['travel_times'].pop()
    if mutation=='unknown_robot': data['robots'][0]['capable_job_ids'].append('absent')
    if mutation=='duplicate_id': data['jobs'].append(copy.deepcopy(data['jobs'][0]))
    if mutation=='unicode_id': data['jobs'][0]['job_id']='로봇'
    with pytest.raises(ValidationError): InitialData.model_validate(data)

def test_target_does_not_silently_expand_scope():
    with pytest.raises(ValidationError): Target(scope='JOB',job_id='W17',robot_id='R1')
