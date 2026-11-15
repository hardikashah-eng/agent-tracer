import copy
import json

from fastapi.testclient import TestClient

from backend.advisor import AdvisorConfig, analyze_sessions
from backend.advisor.__main__ import main as cli_main
from backend.advisor.otel import spans_to_sessions
from backend.main import app
from backend.tracer import tracer


class Builder:
    """Small helper that emits tracer-shaped events with timestamps."""

    def __init__(self, sid='s1'):
        self.sid, self.events, self.seq, self.ts, self.n = sid, [], 0, 1000.0, 0

    def ev(self, kind, name, payload=None, call_id=None, dt=0.0):
        self.ts += dt
        self.seq += 1
        self.events.append({'seq': self.seq, 'ts': self.ts, 'kind': kind, 'name': name,
                            'call_id': call_id, 'payload': payload or {}})

    def llm(self, input_tokens=1000, cached=0, output='ok', dt=1.0, snapshot=None):
        if snapshot is not None:
            self.ev('context.snapshot', 'prompt', snapshot)
        self.n += 1
        cid = f'l{self.n}'
        self.ev('llm.before', 'model', {}, cid)
        self.ev('llm.after', 'model', {'output': output, 'usage': {'input_tokens': input_tokens, 'output_tokens': 50,
                                                                   'cache_read_input_tokens': cached}}, cid, dt)

    def tool(self, name, args, result=None, ok=True, error=None, dt=0.5, upstream_ms=None, **extra):
        self.n += 1
        cid = f't{self.n}'
        self.ev('tool.before', name, {'arguments': args, **extra}, cid)
        payload = {'result': result} if ok else {'error': error}
        if upstream_ms is not None:
            payload['upstream_ms'] = upstream_ms
        self.ev('tool.after' if ok else 'tool.error', name, payload, cid, dt)

    def session(self):
        return {'id': self.sid, 'events': self.events}


def rec(report, detector):
    return [r for r in report['recommendations'] if r['detector'] == detector]


def test_decomposition_splits_latency_and_apportions_budget():
    b = Builder()
    snap = {'messages': [{'id': 'sys', 'role': 'system', 'estimated_tokens': 100},
                         {'id': 'u', 'role': 'user', 'category': 'conversation', 'estimated_tokens': 100},
                         {'id': 't', 'role': 'tool', 'category': 'tool', 'source': 'search', 'estimated_tokens': 200}]}
    b.llm(input_tokens=800, dt=2.0, snapshot=snap)
    b.tool('search', {'q': 'x'}, {'hits': 1}, dt=1.0, upstream_ms=600)
    report = analyze_sessions([b.session()])
    d = report['sessions'][0]
    assert d['latency']['llm_ms'] == 2000.0
    assert d['latency']['tool_upstream_ms'] == 600.0 and d['latency']['tool_overhead_ms'] == 400.0
    turn = d['token_budget']['turns'][0]
    assert turn['basis'] == 'apportioned'
    assert turn['composition']['tool_results'] == 400 and turn['composition']['system'] == 200
    assert d['token_budget']['tool_results_by_tool'] == {'search': 400}


def test_duplicate_fetch_first_party_includes_server_levers():
    b = Builder()
    for _ in range(2):
        b.tool('search_code', {'query': 'margin'}, {'path': 'a.cc'})
        b.llm()
    catalog = {'tools': {'search_code': {'ownership': 'first_party', 'kind': 'read', 'server': 'dev'}}}
    report = analyze_sessions([b.session()], catalog)
    r = rec(report, 'duplicate_fetch')[0]
    assert r['tool'] == 'search_code' and r['ownership'] == 'first_party'
    owners = {lever['owner'] for lever in r['levers']}
    assert 'server_owner' in owners and 'vendor_request' not in owners


def test_failure_loop_on_third_party_only_offers_levers_you_control():
    b = Builder()
    for _ in range(3):
        b.tool('get_issue', {'issueKey': 'A-1'}, ok=False, error='Bad request 400')
        b.llm()
    catalog = {'tools': {'get_issue': {'ownership': 'third_party', 'server': 'jira'}}}
    report = analyze_sessions([b.session()], catalog)
    r = rec(report, 'failure_loop')[0]
    assert r['severity'] == 'high' and r['estimated_savings_per_session']['turns'] == 2
    owners = {lever['owner'] for lever in r['levers']}
    assert 'server_owner' not in owners and {'harness_owner', 'vendor_request'} <= owners
    assert 'Third-party' in r['note']
    # Different arguments are not a loop.
    b2 = Builder()
    for i in range(3):
        b2.tool('get_issue', {'issueKey': f'A-{i}'}, ok=False, error='Bad request')
    assert not rec(analyze_sessions([b2.session()]), 'failure_loop')


def test_chatty_tools_are_merged_per_server_into_code_mode_recommendation():
    b = Builder()
    for page in range(1, 4):
        b.tool('list_prs', {'page': page}, {'values': [page]}, server='bitbucket')
        b.tool('get_diff', {'id': page}, {'patch': 'x'}, server='bitbucket')
        b.llm()
    report = analyze_sessions([b.session()])
    chatty = rec(report, 'chatty_tool')
    assert len(chatty) == 1 and chatty[0]['tool'] == 'bitbucket'
    assert 'code mode' in chatty[0]['title']
    assert chatty[0]['estimated_savings_per_session']['turns'] == 2
    # Same-tool fan-out already covered by chatty_tool must not double count as serial calls.
    assert not rec(report, 'serial_independent_calls')


def test_large_low_use_result_with_reported_ratio_and_lexical_inference():
    b = Builder()
    b.tool('get_diff', {'id': 1}, {'patch': 'x' * 10}, result_tokens=None)
    b.events[-1]['payload'].update({'result_tokens': 5000, 'used_ratio': 0.05})
    b.llm()
    report = analyze_sessions([b.session()])
    r = rec(report, 'large_low_use_result')[0]
    assert r['estimated_savings_per_session']['tokens'] == 4000
    b2 = Builder()
    words = ' '.join(f'token{i}' for i in range(400))
    b2.tool('fetch_doc', {'id': 1}, {'body': words * 2})
    b2.llm(output='Answer mentions token1 and token2 only.')
    assert rec(analyze_sessions([b2.session()]), 'large_low_use_result')


def test_dead_schemas_grouped_per_server_from_catalog():
    sessions = []
    for i in range(5):
        b = Builder(f's{i}')
        b.llm()
        b.llm()
        b.tool('used_tool', {'i': i}, {'ok': True})
        sessions.append(b.session())
    catalog = {'tools': {'used_tool': {'server': 'jira', 'definition_tokens': 300},
                         'unused_a': {'server': 'jira', 'definition_tokens': 500, 'ownership': 'third_party'},
                         'unused_b': {'server': 'jira', 'definition_tokens': 700, 'ownership': 'third_party'},
                         'deferred': {'server': 'jira', 'definition_tokens': 900, 'loaded': False}}}
    report = analyze_sessions(sessions, catalog)
    dead = rec(report, 'dead_schema')
    assert len(dead) == 1 and dead[0]['tool'] == 'jira'
    assert dead[0]['estimated_savings_per_session']['tokens'] == 2400  # (500 + 700) * 2 turns
    assert 'unused_a' in dead[0]['evidence'][0]['detail'] and 'deferred' not in dead[0]['evidence'][0]['detail']
    budget = report['sessions'][0]['token_budget']['turns'][0]
    assert budget['basis'] == 'catalog+reported' and budget['composition']['tool_definitions'] == 1000


def test_cache_prefix_context_dominance_and_protocol_overhead():
    b = Builder()
    for i in range(8):
        snap = {'tool_definitions_hash': f'v{i % 2}',
                'messages': [{'id': 'sys', 'role': 'system', 'estimated_tokens': 50, 'content_hash': 'h'},
                             {'id': 'c', 'role': 'user', 'category': 'conversation', 'estimated_tokens': 100 * (i + 1) ** 2}]}
        b.llm(input_tokens=150 + 100 * (i + 1) ** 2, cached=10, snapshot=snap)
        b.tool('slow', {'i': i}, {'ok': i}, dt=0.6, upstream_ms=100, server='remote')
    report = analyze_sessions([b.session()])
    detectors = {r['detector'] for r in report['recommendations']}
    assert {'cache_prefix_unstable', 'context_dominance', 'protocol_overhead'} <= detectors
    assert rec(report, 'protocol_overhead')[0]['tool'] == 'remote'


def test_serial_independent_same_tool_lookups():
    b = Builder()
    for key in ('alpha-one', 'bravo-two', 'charlie-three'):
        b.tool('lookup', {'key': key}, {'value': 'unrelated'})
        b.llm()
    report = analyze_sessions([b.session()], config=AdvisorConfig(chatty_min_calls=10))
    assert rec(report, 'serial_independent_calls')[0]['estimated_savings_per_session']['turns'] == 2


def test_high_error_rate_is_cross_session():
    sessions = []
    for i in range(3):
        b = Builder(f's{i}')
        b.tool('flaky', {'i': i}, ok=False, error='timeout')
        b.tool('flaky', {'i': i + 10}, {'ok': True})
        sessions.append(b.session())
    r = rec(analyze_sessions(sessions), 'high_error_rate')[0]
    assert r['sessions_affected'] == 3 and r['occurrences'] == 1


def test_no_mutation_empty_and_clean_sessions():
    b = Builder()
    b.llm()
    b.tool('read_file', {'path': 'a'}, {'text': 'hello'})
    b.llm(output='hello')
    original = copy.deepcopy(b.events)
    report = analyze_sessions([b.session()])
    assert b.events == original
    assert report['recommendations'] == []
    empty = analyze_sessions([])
    assert empty['summary']['sessions'] == 0 and empty['recommendations'] == []


def test_otel_spans_convert_and_analyze():
    def span(sid, name, start, end, attrs, status=None):
        return {'traceId': 'trace1', 'spanId': sid, 'name': name, 'startTimeUnixNano': str(int(start * 1e9)),
                'endTimeUnixNano': str(int(end * 1e9)), 'status': status or {},
                'attributes': [{'key': k, 'value': {'intValue' if isinstance(v, int) else 'stringValue': v}} for k, v in attrs.items()]}
    spans = [span('a', 'chat model', 0, 2, {'gen_ai.operation.name': 'chat', 'gen_ai.usage.input_tokens': 900,
                                             'gen_ai.usage.output_tokens': 40, 'gen_ai.usage.cache_read.input_tokens': 300})]
    for i in range(2):
        spans.append(span(f't{i}', 'tools/call get_issue', 3 + i, 3.5 + i,
                          {'mcp.method.name': 'tools/call', 'mcp.server.name': 'jira',
                           'gen_ai.tool.call.arguments': '{"issueKey": "A-1"}', 'error.type': 'invalid_params'},
                          {'code': 2}))
    sessions = spans_to_sessions({'resourceSpans': [{'scopeSpans': [{'spans': spans}]}]})
    assert len(sessions) == 1 and len(sessions[0]['events']) == 6
    report = analyze_sessions(sessions)
    assert report['summary']['cache_read_ratio'] == 0.333
    assert rec(report, 'failure_loop')[0]['tool'] == 'get_issue'


def test_advisor_api_single_and_multi_session():
    s1, s2 = tracer.create(), tracer.create()
    try:
        for s in (s1, s2):
            for _ in range(2):
                tracer.emit(s, 'tool.before', 'search', {'arguments': {'q': 'same'}})
                tracer.emit(s, 'tool.after', 'search', {'result': {'hits': 1}})
        with TestClient(app) as client:
            one = client.get(f'/api/sessions/{s1.id}/advisor')
            assert one.status_code == 200 and one.json()['summary']['sessions'] == 1
            assert rec(one.json(), 'duplicate_fetch')
            both = client.get('/api/advisor', params=[('session_id', s1.id), ('session_id', s2.id)])
            assert both.status_code == 200 and both.json()['summary']['sessions'] == 2
            assert client.get('/api/sessions/missing/advisor').status_code == 404
            assert client.get('/api/advisor', params={'session_id': 'missing'}).status_code == 404
    finally:
        tracer.sessions.pop(s1.id)
        tracer.sessions.pop(s2.id)


def test_cli_writes_all_formats(tmp_path):
    b = Builder()
    for _ in range(2):
        b.tool('get_issue', {'k': 1}, ok=False, error='bad')
    path = tmp_path / 's.json'
    path.write_text(json.dumps(b.session()))
    for fmt in ('json', 'md', 'html'):
        out = tmp_path / f'r.{fmt}'
        assert cli_main([str(path), '--format', fmt, '-o', str(out)]) == 0
        text = out.read_text()
        assert 'get_issue' in text
    assert json.loads((tmp_path / 'r.json').read_text())['engine'] == 'harness-advisor-v1'
    assert '<script type="application/json" id="advisor-data">' in (tmp_path / 'r.html').read_text()
