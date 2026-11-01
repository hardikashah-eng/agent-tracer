import copy
import json
import subprocess
import sys
import pytest
from fastapi.testclient import TestClient
from backend.storage import SQLiteStore
from backend.tracer import Tracer, tracer
from backend.comparison import compare
from backend.replay import records, evaluate, intervention, RecordedTools, replay_workflow, controlled_trials
from backend.infrastructure import correlate, experiment_plan
from backend.trials import summarize
from backend.main import app


def recorded(t, success=False, metadata=None):
    s = t.create(metadata)
    before = t.emit(s, 'tool.before', 'simulation', {'arguments': {'scenario': 's1'}})
    after = t.emit(s, 'tool.after', 'simulation', {'success': success, 'result': {'passed': success}})
    assert before['call_id'] == after['call_id']
    s.status = 'completed' if success else 'failed'; t.persist(s)
    return s


def fixture(s):
    calls = records(s.events)
    return {'schema_version': 1, 'calls': calls, 'assertions': [{'call_id': calls[0]['call_id'], 'path': ['success'], 'equals': True}]}


def test_durable_restore_and_interruption(tmp_path):
    path = tmp_path/'traces.db'
    store = SQLiteStore(path); t = Tracer(store)
    good = recorded(t, True, {'node_id': 'a'})
    active = t.create(); active.status = 'running'; t.emit(active, 'agent.start', 'x')
    store.close()
    reopened = SQLiteStore(path); restored = Tracer(reopened)
    assert restored.sessions[good.id].events == good.events
    assert restored.sessions[good.id].metadata == {'node_id': 'a'}
    assert restored.sessions[active.id].status == 'interrupted'
    assert restored.sessions[good.id].restored
    reopened.close()


def test_persistence_failure_does_not_publish_event():
    class Broken:
        def load(self): return []
        def save(self, s):
            if s.events: raise OSError('disk full')
    t = Tracer(Broken()); s = t.create()
    with pytest.raises(OSError): t.emit(s, 'tool.before', 'x')
    assert s.events == [] and s.seq == 0 and s.pending_calls == {}


def test_comparison_normalizes_ids_but_preserves_material_changes():
    t = Tracer(); a=recorded(t, True); b=recorded(t, True)
    assert compare(a,b)['first_divergence'] is None
    b.events[-1]['payload']['result']['passed'] = False
    report = compare(a,b)
    assert report['first_divergence']['baseline_seq'] == 2
    b.events.insert(0, {'seq':0, 'kind':'agent.start', 'name':'extra', 'payload':{}})
    assert any(d['type']=='added' for d in compare(a,b)['differences'])


def test_replay_intervention_and_no_live_fallback():
    t=Tracer(); s=recorded(t); f=fixture(s); original=copy.deepcopy(f)
    assert not evaluate(f)['passed']
    cid=f['calls'][0]['call_id']
    report=intervention(f,cid,{'success':True})
    assert report['outcome_changed'] and report['variant']['external_calls']==0
    assert f == original
    with pytest.raises(ValueError): intervention(f,'unknown',{})
    with pytest.raises(ValueError): RecordedTools(f['calls']).call('simulation', {'scenario':'other'})
    with pytest.raises(ValueError): records(s.events[:1])
    bad=copy.deepcopy(s.events); bad[1]['call_id']='different'
    with pytest.raises(ValueError): records(bad)


def test_trusted_application_replay_detects_regression():
    f=fixture(recorded(Tracer(),True))
    def application(tools):
        return tools.call('simulation', {'scenario':'s1'})['result']['passed']
    assert replay_workflow(f, application) is True
    assert replay_workflow(f, application, {f['calls'][0]['call_id']: {'result':{'passed':False}}}) is False
    with pytest.raises(ValueError): replay_workflow(f, lambda tools: True)


def test_cli_returns_nonzero_for_failed_contract(tmp_path):
    p=tmp_path/'fixture.json';p.write_text(json.dumps(fixture(recorded(Tracer()))))
    result=subprocess.run([sys.executable,'-m','backend.replay',str(p)],capture_output=True,text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)['passed'] is False


def test_cohorts_confounders_and_paired_trials():
    t=Tracer();a=recorded(t,False,{'node_id':'a','scenario_id':'s1'});b=recorded(t,True,{'node_id':'b','scenario_id':'s2'})
    report=correlate([a,b])
    assert next(g for g in report['groups'] if g['field']=='node_id' and g['value']=='a')['failure_rate']==1
    assert experiment_plan(a,b)['controlled'] is False
    trials=controlled_trials(lambda i: False, lambda i: True, 10)
    assert trials['report']['paired_delta']==1
    assert trials['report']['delta_interval_95'][1]==1
    with pytest.raises(ValueError): summarize([{'trial_id':'same','baseline':True,'variant':False}]*2)
    with pytest.raises(ValueError): controlled_trials(lambda i: 'yes', lambda i: True, 2)


def test_workbench_api():
    a=recorded(tracer,False,{'node_id':'a'});b=recorded(tracer,True,{'node_id':'b'})
    f=fixture(a);body={'assertions':f['assertions']}
    try:
        with TestClient(app) as c:
            assert c.get(f'/api/compare?baseline={a.id}&candidate={b.id}').json()['first_divergence']
            assert c.get('/api/infrastructure').status_code==200
            assert c.get(f'/api/experiment-plan?baseline={a.id}&candidate={b.id}').status_code==200
            assert c.post(f'/api/sessions/{a.id}/replay',json=body).json()['passed'] is False
            export=c.post(f'/api/sessions/{a.id}/regression',json=body)
            assert export.json()['schema_version']==1 and 'attachment' in export.headers['content-disposition']
            response=c.post(f'/api/sessions/{a.id}/intervention',json={**body,'call_id':f['calls'][0]['call_id'],'replacement':{'success':True}})
            assert response.json()['outcome_changed']
            assert c.post(f'/api/sessions/{a.id}/intervention',json={**body,'call_id':'bad','replacement':{}}).status_code==422
            a.restored=True
            assert c.post(f'/api/sessions/{a.id}/command',json={'action':'resume'}).status_code==409
    finally:
        tracer.sessions.pop(a.id);tracer.sessions.pop(b.id)


def test_explicit_ids_handle_concurrent_same_name_calls():
    t=Tracer();s=t.create()
    t.emit(s,'tool.before','x',{'arguments':{'x':1},'call_id':'a'})
    t.emit(s,'tool.before','x',{'arguments':{'x':2},'call_id':'b'})
    t.emit(s,'tool.after','x',{'call_id':'b','success':True})
    t.emit(s,'tool.error','x',{'call_id':'a','error':'timeout','error_type':'TimeoutError'})
    calls=records(s.events)
    assert calls[0]['arguments']=={'x':2}
    assert s.events[-1]['error']['type']=='TimeoutError'


def test_reordered_steps_are_reported():
    t=Tracer();a=t.create();b=t.create()
    for name in ('one','two'):t.emit(a,'tool.after',name,{'success':True})
    for name in ('two','one'):t.emit(b,'tool.after',name,{'success':True})
    assert compare(a,b)['differences']


def test_trial_api_persists_outcomes_and_rejects_string_boolean():
    a=recorded(tracer);b=recorded(tracer,True)
    body={'baseline_id':a.id,'candidate_id':b.id,'pairs':[{'trial_id':str(i),'baseline':False,'variant':True} for i in range(2)]}
    with TestClient(app) as c:
        response=c.post('/api/trials',json=body)
        assert response.status_code==200
        sid=response.json()['analysis_session_id']
        assert c.get(f'/api/sessions/{sid}').json()['events'][0]['payload']['pairs']==body['pairs']
        body['pairs'][0]['baseline']='false'
        assert c.post('/api/trials',json=body).status_code==422


def test_timing_metrics_exclude_invalid_values():
    from backend.infrastructure import timings
    t=Tracer();s=t.create()
    t.emit(s,'infra.metrics','node',{'queue_wait_ms':20})
    t.emit(s,'infra.metrics','node',{'queue_wait_ms':40,'execution_ms':-3})
    assert timings([s])['queue_wait_ms']=={'samples':2,'mean':30,'p95_nearest_rank':40}
    assert 'execution_ms' not in timings([s])
