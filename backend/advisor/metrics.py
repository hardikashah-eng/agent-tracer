"""Per-session outcome metrics shared by verification plans and the savings ledger."""
from __future__ import annotations

from collections import defaultdict

# Primary metric used to verify each kind of recommendation, and the direction that counts as better.
PRIMARY_METRIC = {
    'chatty_tool': 'input_tokens', 'dead_schema': 'input_tokens', 'large_low_use_result': 'input_tokens',
    'duplicate_fetch': 'input_tokens', 'cache_prefix_unstable': 'uncached_input_tokens', 'context_dominance': 'input_tokens',
    'serial_independent_calls': 'wall_ms', 'protocol_overhead': 'wall_ms',
    'failure_loop': 'tool_failures', 'high_error_rate': 'tool_failures', 'tool_selection': 'tool_failures',
}
SAVINGS_KEY = {'input_tokens': 'tokens', 'uncached_input_tokens': 'tokens', 'wall_ms': 'ms', 'tool_failures': 'turns'}


def rec_id(detector: str, tool: str | None) -> str:
    return f'{detector}:{tool or "harness"}'


def task_key(trace) -> str:
    m = trace.metadata or {}
    return str(m.get('task_id') or m.get('scenario_id') or trace.goal or trace.id)


def session_success(trace) -> bool | None:
    m = trace.metadata or {}
    if isinstance(m.get('success'), bool):
        return m['success']
    if str(m.get('success', '')).lower() in ('true', 'false'):
        return str(m['success']).lower() == 'true'
    if trace.status in ('completed',):
        return True
    if trace.status in ('failed', 'cancelled', 'interrupted'):
        return False
    return None


def session_metrics(traces, decompositions, findings, recs) -> list[dict]:
    per_session = defaultdict(lambda: {'waste': defaultdict(float), 'occurrences': defaultdict(int)})
    cross = [f for f in findings if f.get('session_id') is None]
    for f in findings:
        if f.get('session_id'):
            row = per_session[f['session_id']]
            key = rec_id(f['detector'], f['tool'])
            row['waste'][key] += f['savings']['tokens']
            row['occurrences'][key] += 1
    rec_by_id = {r['id']: r for r in recs}
    rows = []
    for t, d in zip(traces, decompositions):
        row = per_session[t.id]
        for f in cross:
            key = rec_id(f['detector'], f['tool'])
            if f['detector'] == 'dead_schema':
                row['waste'][key] += rec_by_id.get(key, {}).get('estimated_savings_per_session', {}).get('tokens', 0)
                row['occurrences'][key] += 1
            elif f['detector'] == 'high_error_rate':
                fails = sum(1 for c in t.tool_calls if c.name == f['tool'] and not c.ok)
                if fails:
                    row['occurrences'][key] += fails
            elif f['detector'] == 'tool_selection':
                n = sum(1 for x in f.get('evidence_items', []) if x['session_id'] == t.id)
                if n:
                    row['occurrences'][key] += n
        budget = d['token_budget']
        rows.append({
            'session_id': t.id, 'started_at': t.started_at, 'metadata': t.metadata, 'task_key': task_key(t),
            'success': session_success(t),
            'input_tokens': budget['total_input_tokens'], 'output_tokens': budget['total_output_tokens'],
            'uncached_input_tokens': budget['total_input_tokens'] - (budget['cached_input_tokens'] or 0),
            'model_turns': d['llm_turns'], 'tool_calls': d['tool_calls'],
            'tool_failures': sum(1 for c in t.tool_calls if not c.ok), 'wall_ms': d['latency']['wall_ms'],
            'waste': {k: round(v) for k, v in row['waste'].items()}, 'occurrences': dict(row['occurrences']),
        })
    return rows


def metric_value(row: dict, name: str):
    return row.get(name)


def in_scope(row: dict, scope: dict | None) -> bool:
    if not scope:
        return True
    meta = row.get('metadata') or {}
    return all(str(meta.get(k)) == str(v) for k, v in scope.items())


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def stdev(values):
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5
