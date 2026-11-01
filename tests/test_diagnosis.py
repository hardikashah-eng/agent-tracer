import copy
from backend.diagnosis import diagnose
from backend.main import app
from backend.tracer import tracer
from fastapi.testclient import TestClient


def event(seq, kind, payload=None, name='simulation'):
    return dict(seq=seq, kind=kind, name=name, payload=payload or {})


def test_no_signal_and_no_false_timeout_from_quoted_content():
    report = diagnose([event(1, 'tool.after', {'result': 'documentation says timeout'})])
    assert report['hypotheses'] == []
    assert 'Insufficient' in report['summary']
    assert diagnose([])['analyzed_through_seq'] == 0


def test_ranked_errors_retries_and_evidence_without_mutation():
    events = [event(i, 'tool.before', {'arguments': {'branch': 'x'}}) for i in range(1, 4)]
    events += [event(4, 'tool.error', {'error': 'request timed out'}), event(5, 'agent.failed', {'error': 'validation'})]
    original = copy.deepcopy(events)
    report = diagnose(events, 'paused', True)
    assert report['hypotheses'][0]['id'] == 'timeout'
    assert {h['id'] for h in report['hypotheses']} >= {'execution_error', 'repeated_call_1'}
    assert all(not h['confirmed'] and h['confidence']=='heuristic' for h in report['hypotheses'])
    assert all(set(h['evidence_seqs']) <= set(range(1, 6)) for h in report['hypotheses'])
    assert events == original


def test_different_arguments_are_not_retry_loop():
    assert not diagnose([event(i, 'tool.before', {'arguments': {'x': i}}) for i in range(4)])['hypotheses']


def test_context_and_attribution_are_tentative():
    events = [event(1, 'context.snapshot', {'messages': [{'id': 'rules', 'role': 'system'}]}),
              event(2, 'tool.after'), event(3, 'context.snapshot', {'messages': []})]
    findings = diagnose(events)['hypotheses']
    assert {h['id'] for h in findings} == {'instruction_removed_3', 'unattributed_result_2'}
    events[2]['payload']['messages'] = [{'id': 'rules', 'role': 'system'}, {'id': 't', 'source': 'simulation'}]
    assert diagnose(events)['hypotheses'] == []


def test_diagnosis_api_is_read_only_and_missing_session():
    s = tracer.create()
    try:
        tracer.emit(s, 'tool.error', 'simulation', {'error': 'timeout'})
        original = copy.deepcopy(s.events)
        with TestClient(app) as client:
            for status in ('running', 'paused', 'completed', 'failed'):
                s.status = status
                report = client.get(f'/api/sessions/{s.id}/diagnosis')
                assert report.status_code == 200
                assert report.json()['status'] == status
                assert report.json()['analyzed_through_seq'] == 1
            assert client.get('/api/sessions/missing/diagnosis').status_code == 404
        assert s.events == original
    finally:
        tracer.sessions.pop(s.id)
