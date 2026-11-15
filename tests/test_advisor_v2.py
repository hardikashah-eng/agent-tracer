"""Optional LLM judge, verification plans and the savings ledger."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.advisor import JudgeOptions, analyze_sessions
from backend.advisor.judge import CallableJudge, judge_from_env, judge_status, parse_json_object, redact
from backend.advisor.ledger import JSONLedger, SQLiteLedger, evaluate_ledger, make_entry
from backend.advisor.verify import evaluate_plan, verification_plans
from backend.advisor.__main__ import main as cli_main
from backend.main import app, ledger_store
from backend.tracer import tracer
from tests.test_advisor import Builder


def retry_session(sid, start, attempts=3, metadata=None, extra_tokens=0):
    b = Builder(sid)
    b.ts = start
    b.ev('agent.start', 'agent', {'task': 'Link Jira issue'})
    b.llm(input_tokens=2000 + extra_tokens)
    for _ in range(attempts):
        b.tool('get_issue', {'issueKey': 'A-1'}, ok=False, error='Bad request')
        b.llm(input_tokens=2000 + extra_tokens)
    b.tool('get_issue', {'issueKey': 'A-1', 'cloudId': 'c'}, {'status': 'done'})
    b.llm(input_tokens=2000 + extra_tokens)
    s = b.session()
    s['status'] = 'completed'
    s['metadata'] = {'agent': 'jira-bot', **(metadata or {})}
    return s


# ---------- LLM judge ----------

def test_redaction_and_json_parsing():
    text = redact('Authorization: Bearer abcdefghijklmnop12345 api_key="sk-live-abcdefghijkl" mail bob@example.com')
    assert 'abcdefghijklmnop12345' not in text and 'sk-live' not in text and 'bob@example.com' not in text
    assert parse_json_object('```json\n{"a": 1}\n```') == {'a': 1}
    with pytest.raises(ValueError):
        parse_json_object('no json here')


def test_judge_env_configuration():
    assert judge_from_env({}) is None and judge_status({})['configured'] is False
    j = judge_from_env({'AGENT_TRACER_JUDGE': 'vllm', 'AGENT_TRACER_JUDGE_URL': 'http://gpu:8000/v1', 'AGENT_TRACER_JUDGE_MODEL': 'qwen'})
    assert j.name == 'openai-compatible' and j.model == 'qwen'
    assert judge_status({'AGENT_TRACER_JUDGE': 'anthropic'})['configured'] is False


def test_judge_flags_tool_choice_and_overrides_lexical_usage():
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        if 'TOOL SELECTION' in prompt:
            return json.dumps({'appropriate': 'Chosen tool: grep_repo' not in prompt, 'better_tool': 'search_code',
                               'reason': 'grep over the whole repo when an index exists', 'confidence': 0.9})
        return json.dumps({'used_ratio': 0.9, 'used_fields': ['body'], 'unused_fields': [], 'reason': 'summarised', 'confidence': 0.8})

    b = Builder()
    b.ev('agent.start', 'agent', {'goal': {'id': 'g', 'text': 'Find callers of compute_margin'}})
    b.llm(output='I will grep.')
    b.tool('grep_repo', {'pattern': 'compute_margin'}, {'hits': 3})
    big = ' '.join(f'word{i}' for i in range(600))
    b.tool('fetch_doc', {'id': 1}, {'body': big})
    b.llm(output='Paraphrased summary with no verbatim words.')
    cache = {}
    report = analyze_sessions([b.session()], judge=CallableJudge(fake), judge_cache=cache)
    j = report['judge']
    assert j['tool_selection_flagged'] == 1 and j['result_usage_judged'] == 1
    sel = [r for r in report['recommendations'] if r['detector'] == 'tool_selection']
    assert sel and sel[0]['tool'] == 'grep_repo' and sel[0]['confidence'] == 'llm_judge'
    # Lexical overlap would flag fetch_doc as unused; the confident judge says 90% used, so no finding.
    assert not [r for r in report['recommendations'] if r['detector'] == 'large_low_use_result']
    assert 'Find callers of compute_margin' in prompts[0]
    # Cached on re-run: no new judge calls.
    calls = len(prompts)
    again = analyze_sessions([b.session()], judge=CallableJudge(fake), judge_cache=cache)
    assert len(prompts) == calls and again['judge']['cache_hits'] == calls


def test_judge_errors_do_not_break_report():
    b = Builder()
    b.tool('anything', {'x': 1}, {'ok': True})
    report = analyze_sessions([b.session()], judge=CallableJudge(lambda p: 'not json'),
                              judge_options=JudgeOptions(max_tool_selection=5))
    assert report['judge']['errors'] and report['summary']['tool_calls'] == 1


# ---------- Verification plans ----------

def test_plans_are_sized_tagged_and_evaluated():
    base = [retry_session(f'b{i}', 1000 + i * 100) for i in range(4)]
    report = analyze_sessions(base)
    plan = next(p for p in report['verification_plans'] if p['recommendation_id'] == 'failure_loop:get_issue')
    assert plan['primary_metric']['name'] == 'tool_failures'
    assert plan['tagging']['experiment_id'] == plan['experiment_id'] and plan['design']['pairs_required'] >= 5
    assert 'result' not in plan
    trial = []
    for k in range(4):
        for arm, attempts in (('baseline', 3), ('variant', 1)):
            meta = {'experiment_id': plan['experiment_id'], 'arm': arm, 'task_id': f't{k}'}
            trial.append(retry_session(f'{arm}{k}', 5000 + k * 100 + (50 if arm == 'variant' else 0), attempts, meta))
    rows = analyze_sessions(trial, plans=0)['session_metrics']
    result = evaluate_plan(plan, rows)
    assert result['verdict'] in ('confirmed', 'inconclusive') and result['pairs_observed'] == 4
    assert result['mean_delta'] == -2 and result['success']['non_inferior']
    # Auto-evaluation when tagged sessions are part of the analysed set.
    combined = analyze_sessions(base + trial)
    auto = next(p for p in combined['verification_plans'] if p['experiment_id'] == plan['experiment_id'])
    assert auto['result']['pairs_observed'] == 4
    assert evaluate_plan(plan, [])['verdict'] == 'insufficient_data'


def test_regression_verdict():
    plan = {'experiment_id': 'e', 'primary_metric': {'name': 'input_tokens', 'expected_delta_per_task': 100},
            'design': {'pairs_required': 3}}
    rows = []
    for k in range(3):
        for arm, tokens in (('baseline', 1000), ('variant', 1500 + k)):
            rows.append({'session_id': f'{arm}{k}', 'metadata': {'experiment_id': 'e', 'arm': arm, 'task_id': str(k)},
                         'task_key': str(k), 'input_tokens': tokens, 'success': True})
    assert evaluate_plan(plan, rows)['verdict'] == 'regressed'


# ---------- Savings ledger ----------

def test_ledger_tracks_realized_savings_and_awaiting_data(tmp_path):
    before = [retry_session(f'b{i}', 1000 + i * 100) for i in range(4)]
    report = analyze_sessions(before)
    entry = make_entry(report, 'failure_loop:get_issue', applied_at=2000, scope={'agent': 'jira-bot'})
    assert entry['baseline']['sessions'] == 4 and entry['baseline']['finding_share'] == 1.0
    pending = evaluate_ledger([entry], report)['entries'][0]
    assert pending['status'] == 'awaiting_data'
    after = [retry_session(f'a{i}', 3000 + i * 100, attempts=1) for i in range(3)]
    trial_noise = [retry_session('t', 3500, attempts=1, metadata={'experiment_id': 'x', 'arm': 'variant'})]
    out = evaluate_ledger([entry], analyze_sessions(before + after + trial_noise))
    e = out['entries'][0]
    assert e['status'] == 'realized' and e['after']['sessions'] == 3
    assert e['realized']['primary_metric_change'] == -2 and out['cumulative_tokens_saved'] > 0
    with pytest.raises(KeyError):
        make_entry(report, 'nope:x')
    # Out-of-scope sessions are ignored; regressions are detected.
    worse = [retry_session(f'w{i}', 3000 + i * 100, attempts=4, extra_tokens=900) for i in range(3)]
    assert evaluate_ledger([entry], analyze_sessions(before + worse))['entries'][0]['status'] == 'regressed'
    other = make_entry(report, 'failure_loop:get_issue', applied_at=2000, scope={'agent': 'other'})
    assert evaluate_ledger([other], analyze_sessions(before + after))['entries'][0]['status'] == 'awaiting_data'
    # Storage backends.
    for store in (JSONLedger(tmp_path / 'l.json'), SQLiteLedger(':memory:')):
        store.add(entry)
        assert [x['id'] for x in store.list()] == [entry['id']]
        assert store.delete(entry['id']) and not store.list() and not store.delete(entry['id'])


def test_cli_ledger_and_plan_evaluation(tmp_path, capsys):
    files = []
    for i in range(4):
        s = retry_session(f'b{i}', 1000 + i * 100)
        path = tmp_path / f'{s["id"]}.json'
        path.write_text(json.dumps(s))
        files.append(str(path))
    ledger = tmp_path / 'ledger.json'
    assert cli_main(files + ['--ledger', str(ledger), '--apply', 'failure_loop:get_issue', '--applied-at', '2000',
                             '--scope', 'agent=jira-bot']) == 0
    assert json.loads(ledger.read_text())[0]['scope'] == {'agent': 'jira-bot'}
    out = tmp_path / 'r.md'
    assert cli_main(files + ['--ledger', str(ledger), '--format', 'md', '-o', str(out)]) == 0
    text = out.read_text()
    assert '## Savings ledger' in text and '## Verification plans' in text
    report = json.loads((lambda p: (cli_main(files + ['--format', 'json', '-o', str(p)]), p.read_text())[1])(tmp_path / 'r.json'))
    plan_file = tmp_path / 'plan.json'
    plan_file.write_text(json.dumps(report['verification_plans'][0]))
    capsys.readouterr()
    assert cli_main(files + ['--evaluate-plan', str(plan_file)]) == 0
    assert json.loads(capsys.readouterr().out)['verdict'] == 'insufficient_data'
    with pytest.raises(SystemExit):
        cli_main(files + ['--judge'])


# ---------- API ----------

def test_api_verification_ledger_and_judge_status():
    created = []
    try:
        for i in range(3):
            s = tracer.create({'agent': 'api-bot'})
            created.append(s)
            for _ in range(2):
                tracer.emit(s, 'tool.before', 'get_issue', {'arguments': {'k': 1}})
                tracer.emit(s, 'tool.error', 'get_issue', {'error': 'bad'})
        with TestClient(app) as client:
            assert client.get('/api/advisor/judge').json()['configured'] is False
            assert client.get('/api/advisor', params={'judge': 'true'}).status_code == 400
            plans = client.get('/api/advisor/verification', params={'top': 2}).json()
            assert plans and plans[0]['experiment_id'].startswith('exp-')
            ev = client.post('/api/advisor/verification/evaluate', json={'plan': plans[0]})
            assert ev.status_code == 200 and ev.json()['verdict'] == 'insufficient_data'
            assert client.post('/api/advisor/verification/evaluate', json={'plan': {}}).status_code == 422
            add = client.post('/api/advisor/ledger', json={'recommendation_id': 'failure_loop:get_issue', 'scope': {'agent': 'api-bot'}})
            assert add.status_code == 200
            entry_id = add.json()['id']
            ledger = client.get('/api/advisor/ledger').json()
            assert ledger['entries'][0]['status'] == 'awaiting_data'
            assert client.get('/api/advisor').json()['ledger']['entries'][0]['id'] == entry_id
            assert client.post('/api/advisor/ledger', json={'recommendation_id': 'missing:x'}).status_code == 404
            assert client.delete(f'/api/advisor/ledger/{entry_id}').status_code == 200
            assert client.delete(f'/api/advisor/ledger/{entry_id}').status_code == 404
            timeline = client.get(f'/api/sessions/{created[0].id}/advisor').json()['sessions'][0]['timeline']
            assert timeline and timeline[0]['kind'] == 'tool'
    finally:
        for s in created:
            tracer.sessions.pop(s.id, None)
        for e in ledger_store.list():
            ledger_store.delete(e['id'])


def test_judge_backends_request_shape(monkeypatch):
    from backend.advisor import judge as judge_mod
    sent = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append((url, headers, json))
        if url.endswith('/chat/completions'):
            return Resp({'choices': [{'message': {'content': '{"appropriate": true, "confidence": 0.9}'}}]})
        return Resp({'content': [{'type': 'text', 'text': '{"appropriate": false, "confidence": 0.8}'}]})

    import httpx
    monkeypatch.setattr(httpx, 'post', fake_post)
    oa = judge_mod.OpenAICompatibleJudge('http://vllm:8000/v1/', 'qwen', 'k')
    assert json.loads(oa.complete('hi'))['appropriate'] is True
    url, headers, body = sent[-1]
    assert url == 'http://vllm:8000/v1/chat/completions' and headers['Authorization'] == 'Bearer k'
    assert body['temperature'] == 0 and body['messages'][0]['role'] == 'system'
    an = judge_mod.AnthropicJudge('claude-model', 'key')
    assert json.loads(an.complete('hi'))['appropriate'] is False
    url, headers, body = sent[-1]
    assert url == 'https://api.anthropic.com/v1/messages' and headers['x-api-key'] == 'key' and body['system']
