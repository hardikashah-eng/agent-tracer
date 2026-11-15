"""Generate synthetic PR-review sessions and a Harness Advisor report.

    python -m examples.advisor_demo

Writes examples/advisor/sessions/*.json, examples/advisor/catalog.json and
docs/samples/advisor-report.html. All data is synthetic and deterministic; the
patterns (paginated third-party MCP, retry loop, re-fetches, unused schemas)
mirror what the advisor is designed to surface.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from backend.advisor import analyze_sessions
from backend.advisor.ledger import make_entry
from backend.advisor.report import to_html, to_markdown

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'examples' / 'advisor'

CATALOG = {'tools': {
    'list_pull_requests': {'server': 'bitbucket', 'ownership': 'third_party', 'transport': 'mcp', 'kind': 'read', 'definition_tokens': 410},
    'get_pull_request_diff': {'server': 'bitbucket', 'ownership': 'third_party', 'transport': 'mcp', 'kind': 'read', 'definition_tokens': 380},
    'get_pull_request_activity': {'server': 'bitbucket', 'ownership': 'third_party', 'transport': 'mcp', 'kind': 'read', 'definition_tokens': 360},
    'create_pull_request_comment': {'server': 'bitbucket', 'ownership': 'third_party', 'transport': 'mcp', 'kind': 'write', 'definition_tokens': 450},
    'get_issue': {'server': 'jira', 'ownership': 'third_party', 'transport': 'mcp', 'kind': 'read', 'definition_tokens': 520},
    'search_code': {'server': 'internal-dev', 'ownership': 'first_party', 'transport': 'mcp', 'kind': 'read', 'definition_tokens': 300},
    'run_tests': {'server': 'local', 'ownership': 'first_party', 'transport': 'cli', 'kind': 'read', 'definition_tokens': 150},
    **{f'jira_{n}': {'server': 'jira', 'ownership': 'third_party', 'transport': 'mcp', 'kind': k, 'definition_tokens': t}
       for n, k, t in [('create_issue', 'write', 640), ('transition_issue', 'write', 580), ('add_worklog', 'write', 470),
                       ('search_jql', 'read', 610), ('get_sprint', 'read', 420), ('link_issues', 'write', 430)]},
    **{f'confluence_{n}': {'server': 'confluence', 'ownership': 'third_party', 'transport': 'mcp', 'kind': k, 'definition_tokens': t}
       for n, k, t in [('search', 'read', 560), ('get_page', 'read', 480), ('create_page', 'write', 720), ('update_page', 'write', 690)]},
}}

SYSTEM = 'You are a PR review agent. Review open pull requests, link Jira issues, and summarise risks.'


BASE_TS = 1_790_000_000.0  # synthetic timeline; each session starts one hour after the previous one


class Session:
    def __init__(self, sid, rng, start_ts=BASE_TS):
        self.id, self.rng, self.events, self.seq, self.ts = sid, rng, [], 0, start_ts
        self.messages = [{'id': 'system-0', 'role': 'system', 'category': 'system', 'content': SYSTEM},
                         {'id': 'user-0', 'role': 'user', 'category': 'conversation', 'content': 'Review open PRs in repo adas-sim and flag stale ones.'}]
        self.prefix = 'tools-v1'
        self.calls = 0

    def emit(self, kind, name, payload, call_id=None, dt=0.0):
        self.ts += dt
        self.seq += 1
        self.events.append({'seq': self.seq, 'ts': round(self.ts, 3), 'kind': kind, 'name': name,
                            'call_id': call_id, 'payload': payload})

    def snapshot(self):
        msgs = []
        for m in self.messages:
            text = m['content'] if isinstance(m['content'], str) else json.dumps(m['content'])
            msgs.append({**m, 'content': text, 'estimated_tokens': max(1, (len(text) + 3) // 4)})
        self.emit('context.snapshot', 'prompt', {'messages': msgs, 'tool_definitions_hash': self.prefix,
                                                 'estimated_tokens': sum(x['estimated_tokens'] for x in msgs)})

    def llm(self, output, cached_share=0.1):
        self.snapshot()
        est = sum(max(1, len(m['content'] if isinstance(m['content'], str) else json.dumps(m['content'])) // 4) for m in self.messages)
        defs = sum(t['definition_tokens'] for t in CATALOG['tools'].values())
        inp = est + defs
        cid = f'llm-{self.seq}'
        self.emit('llm.before', 'claude', {}, cid, dt=0.05)
        self.emit('llm.after', 'claude', {'output': output, 'usage': {'input_tokens': inp, 'output_tokens': self.rng.randint(60, 220),
                                                                     'cache_read_input_tokens': int(inp * cached_share)}},
                  cid, dt=self.rng.uniform(1.8, 4.5))
        self.messages.append({'id': f'a-{self.seq}', 'role': 'assistant', 'category': 'conversation', 'content': output})

    def tool(self, name, args, result, ok=True, error=None, upstream_ms=None, overhead_ms=260):
        self.calls += 1
        cid = f'tool-{self.calls}'
        meta = CATALOG['tools'].get(name, {})
        self.emit('tool.before', name, {'arguments': args, 'transport': meta.get('transport'), 'server': meta.get('server')}, cid, dt=0.02)
        up = upstream_ms if upstream_ms is not None else self.rng.uniform(120, 600)
        total = (up + (overhead_ms if meta.get('transport') == 'mcp' else 15)) / 1000
        payload = {'result': result, 'upstream_ms': round(up, 1)} if ok else {'error': error, 'error_type': 'validation_error', 'upstream_ms': round(up, 1)}
        self.emit('tool.after' if ok else 'tool.error', name, payload, cid, dt=total)
        self.messages.append({'id': f't-{self.seq}', 'role': 'tool', 'category': 'tool', 'source': name,
                              'content': result if ok else {'error': error}})


def pr(i):
    return {'id': i, 'title': f'Fix planner edge case #{i}', 'author': {'display_name': f'dev{i % 7}'},
            'created_on': f'2026-10-{(i % 27) + 1:02d}T10:00:00Z', 'state': 'OPEN',
            'links': {'html': f'https://bitbucket.example/adas-sim/pull-requests/{i}'},
            'reviewers': [{'display_name': f'rev{j}', 'approved': j % 3 == 0} for j in range(3)],
            'summary': {'raw': 'Adjusts lateral planner thresholds; adds regression scenario. ' * 3},
            'participants': [{'role': 'REVIEWER', 'state': None, 'user': {'uuid': f'{{u-{i}-{j}}}'}} for j in range(4)]}


def diff(i):
    return {'diffstat': [{'path': f'planner/lateral_{k}.cc', 'lines_added': 40 + k, 'lines_removed': 12} for k in range(6)],
            'patch': '\n'.join(f'+  threshold_{k} = compute_margin(state, {k}); // tuned for scenario {i}' for k in range(160))}


def code_mode_review(s, pages, ids):
    """Variant arm: the model writes one script against typed Bitbucket stubs; only the aggregate returns."""
    script = ('const prs = await bitbucket.listAllPullRequests({repo: "adas-sim", state: "OPEN"});\n'
              'return Promise.all(prs.slice(0, 4).map(async p => summarise(p, await bitbucket.getDiffstat(p.id), '
              'await bitbucket.getActivity(p.id))));')
    summary = [{'id': pid, 'files': 6, 'approvals': 1, 'age_days': 9 + pid % 5, 'risk': 'planner/lateral thresholds'} for pid in ids[:4]]
    s.tool('execute_code', {'language': 'typescript', 'code': script}, {'pages': pages, 'open_prs': len(ids), 'reviewed': summary},
           upstream_ms=rng_upstream(s), overhead_ms=60)
    s.events[-2]['payload'].update({'transport': 'code', 'server': 'bitbucket'})
    s.llm('Reviewed 4 PRs via one code-mode script; 2 stale.')


def rng_upstream(s):
    return s.rng.uniform(2500, 4000)


def make_session(n, rng, *, mode='baseline', start_ts=None, metadata=None, sid=None):
    s = Session(sid or f'pr-review-{n:02d}', rng, start_ts or BASE_TS + n * 3600)
    s.llm('Plan: list open PRs, inspect each, link Jira, summarise.')
    pages = 3 + n % 2
    ids = [page * 10 + k for page in range(1, pages + 1) for k in range(5)]
    if mode == 'codemode':
        code_mode_review(s, pages, ids)
    for page in range(1, pages + 1) if mode != 'codemode' else []:
        values = [pr(page * 10 + k) for k in range(5)]
        s.tool('list_pull_requests', {'workspace': 'xp', 'repo': 'adas-sim', 'state': 'OPEN', 'page': page},
               {'values': values, 'next': page < pages})
        if n == 3:
            s.prefix = f'tools-v{page + 1}'  # tool list re-ordered by a dynamic loader
        s.llm(f'Fetched page {page}; continue paginating.' if page < pages else 'All pages fetched; inspect PRs.')
    for pid in ids[:4] if mode != 'codemode' else []:
        s.tool('get_pull_request_diff', {'repo': 'adas-sim', 'id': pid}, diff(pid), upstream_ms=rng.uniform(300, 700))
        s.tool('get_pull_request_activity', {'repo': 'adas-sim', 'id': pid},
               {'values': [{'approval': None, 'comment': {'content': {'raw': 'LGTM pending sim run'}}} for _ in range(3)]})
        s.llm(f'PR {pid}: touches planner/lateral; risk moderate. Next PR.')
    # Jira lookup retry loop: missing cloudId (third-party server returns a vague error).
    attempts = 3 if n % 3 != 1 and mode != 'retry_guard' else 1
    for _ in range(attempts):
        s.tool('get_issue', {'issueKey': 'ADAS-1432'}, None, ok=False, error='Bad request: invalid input')
        s.llm('Jira lookup failed; retrying.')
    if attempts == 1:
        s.tool('get_issue', {'issueKey': 'ADAS-1432', 'cloudId': 'xp-cloud'}, {'key': 'ADAS-1432', 'status': 'In Review'})
        s.llm('Linked ADAS-1432.')
    # First-party code search re-fetched after compaction.
    for _ in range(2 if n % 2 == 0 else 1):
        s.tool('search_code', {'query': 'compute_margin', 'repo': 'adas-sim'},
               {'matches': [{'path': 'planner/margin.cc', 'line': 88}, {'path': 'planner/lateral_2.cc', 'line': 14}]}, overhead_ms=40)
        s.llm('compute_margin is shared by lateral planners.')
    s.tool('run_tests', {'command': 'bazel test //planner:lateral_test'}, {'passed': 41, 'failed': 0}, upstream_ms=rng.uniform(4000, 9000))
    s.llm('Summary: 2 stale PRs (no approvals > 7 days), ADAS-1432 linked, tests green.', cached_share=0.1)
    s.emit('agent.completed', 'pr-review-agent', {'answer': 'done'}, dt=0.1)
    meta = {'agent': 'pr-review', 'synthetic': 'true', **(metadata or {})}
    return {'id': s.id, 'status': 'completed', 'events': s.events, 'metadata': meta}


def main():
    rng = random.Random(7)
    # 1) Six baseline sessions.
    sessions = [make_session(n, rng) for n in range(1, 7)]
    report = analyze_sessions(sessions, CATALOG)
    # 2) The team applies the retry guard for get_issue after session 6 and records it in the savings ledger.
    applied_at = BASE_TS + 6.5 * 3600
    entry = make_entry(report, 'failure_loop:get_issue', applied_at=applied_at, scope={'agent': 'pr-review'},
                       note='Retry guard: block identical failing calls; surface error with required cloudId.')
    entry['id'] = 'demo-ledger-1'
    # 3) Four later production sessions run with the retry guard.
    sessions += [make_session(n, rng, mode='retry_guard') for n in range(7, 11)]
    # 4) A paired trial verifies the top recommendation (code mode for Bitbucket) on five tasks.
    plan = next(p for p in report['verification_plans'] if p['recommendation_id'] == 'chatty_tool:bitbucket')
    for k, task in enumerate(plan['design']['tasks'][:5]):
        for arm, mode in (('baseline', 'retry_guard'), ('variant', 'codemode')):
            meta = {'experiment_id': plan['experiment_id'], 'arm': arm, 'task_id': task,
                    'recommendation_id': plan['recommendation_id']}
            sessions.append(make_session(20 + k, rng, mode=mode, start_ts=BASE_TS + (12 + k) * 3600 + (0 if arm == 'baseline' else 1800),
                                         metadata=meta, sid=f'trial-{k + 1}-{arm}'))
    (OUT / 'sessions').mkdir(parents=True, exist_ok=True)
    for old in (OUT / 'sessions').glob('*.json'):
        old.unlink()
    for s in sessions:
        (OUT / 'sessions' / f"{s['id']}.json").write_text(json.dumps(s, indent=1), encoding='utf-8')
    (OUT / 'catalog.json').write_text(json.dumps(CATALOG, indent=2), encoding='utf-8')
    (OUT / 'ledger.json').write_text(json.dumps([entry], indent=2), encoding='utf-8')
    final = analyze_sessions(sessions, CATALOG, ledger=[entry])
    samples = ROOT / 'docs' / 'samples'
    samples.mkdir(parents=True, exist_ok=True)
    (samples / 'advisor-report.html').write_text(to_html(final, 'Harness Advisor — synthetic PR-review agent'), encoding='utf-8')
    (samples / 'advisor-report.md').write_text(to_markdown(final), encoding='utf-8')
    print(f"{len(sessions)} sessions, {len(final['recommendations'])} recommendations")
    for r in final['recommendations']:
        print(f"{r['rank']:>2}. [{r['severity']}] {r['title']} ({r['ownership']}) ~{r['estimated_savings_per_session']['tokens']} tok/session")
    for p in final['verification_plans']:
        print('plan', p['experiment_id'], (p.get('result') or {}).get('verdict', 'not run'))
    for e in final['ledger']['entries']:
        print('ledger', e['recommendation_id'], e['status'], e['realized'])


if __name__ == '__main__':
    main()
