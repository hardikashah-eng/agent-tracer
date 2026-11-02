import hashlib
from backend.context import snapshot_context
from backend.tracer import Tracer

def test_snapshot_preserves_versioned_provenance_and_call_link():
    tracer = Tracer()
    session = tracer.create()
    message = {'id': 'm1', 'role': 'tool', 'content': 'test failed', 'source_id': 'tests',
               'source_type': 'tool', 'source_event_seq': 4, 'goal_ids': ['g1']}
    event = snapshot_context(tracer, session, 'decision', [message], call_id='c1',
                             goal={'id': 'g1', 'text': 'Fix tests'})
    source = event['payload']['messages'][0]
    assert event['payload']['call_id'] == 'c1'
    assert source['content_hash'] == hashlib.sha256(b'test failed').hexdigest()
    assert source['source_id'] == 'tests'
    assert source['source_event_seq'] == 4
    message['goal_ids'].append('mutated')
    assert source['goal_ids'] == ['g1']

def test_legacy_snapshot_signature_still_works():
    tracer = Tracer()
    session = tracer.create()
    event = snapshot_context(tracer, session, 'legacy', [{'role': 'user', 'content': 'hello'}])
    assert event['payload']['call_id'] is None
    assert event['payload']['messages'][0]['estimated_tokens'] == 2
