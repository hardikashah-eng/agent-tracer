"""Harness Advisor: cross-session decomposition, detectors and recommendations.

See docs/HARNESS_ADVISOR.md for the design.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from .decompose import CATEGORIES, decompose
from .detectors import AdvisorConfig, detect_cross_session, detect_session
from .model import build_trace, load_sessions
from .recommend import recommend

__all__ = ['AdvisorConfig', 'analyze_sessions', 'load_sessions']


def _tool_scorecard(traces, findings, catalog, n):
    rows = defaultdict(lambda: {'calls': 0, 'failures': 0, 'sessions': set(), 'result_tokens': 0,
                                'duration_ms': 0.0, 'overhead_ms': 0.0, 'timed_upstream': 0,
                                'duplicate_calls': 0, 'transport': 'unknown', 'server': 'unknown',
                                'ownership': 'unknown', 'kind': 'unknown'})
    for t in traces:
        for c in t.tool_calls:
            r = rows[c.name]
            r['calls'] += 1
            r['failures'] += 0 if c.ok else 1
            r['sessions'].add(t.id)
            r['result_tokens'] += c.result_tokens
            r['duration_ms'] += c.duration_ms or 0
            if c.upstream_ms is not None and c.duration_ms:
                r['overhead_ms'] += max(0.0, c.duration_ms - c.upstream_ms)
                r['timed_upstream'] += 1
            for k in ('transport', 'server', 'ownership', 'kind'):
                if getattr(c, k) != 'unknown':
                    r[k] = getattr(c, k)
    for f in findings:
        if f['detector'] == 'duplicate_fetch':
            rows[f['tool']]['duplicate_calls'] += f['metrics']['calls'] - 1
    for name, entry in (catalog.get('tools') or {}).items():
        r = rows[name]
        for k in ('transport', 'server', 'ownership', 'kind'):
            if entry.get(k) and r[k] == 'unknown':
                r[k] = entry[k]
    flags = defaultdict(set)
    for f in findings:
        if f['tool']:
            flags[f['tool']].add(f['detector'])
    out = []
    for name, r in rows.items():
        entry = (catalog.get('tools') or {}).get(name, {})
        out.append({'tool': name, 'server': r['server'], 'transport': r['transport'], 'ownership': r['ownership'],
                    'kind': r['kind'], 'calls': r['calls'], 'sessions_used': len(r['sessions']),
                    'session_share': round(len(r['sessions']) / n, 3),
                    'error_rate': round(r['failures'] / r['calls'], 3) if r['calls'] else None,
                    'duplicate_rate': round(r['duplicate_calls'] / r['calls'], 3) if r['calls'] else None,
                    'avg_result_tokens': round(r['result_tokens'] / r['calls']) if r['calls'] else 0,
                    'avg_duration_ms': round(r['duration_ms'] / r['calls'], 1) if r['calls'] else None,
                    'avg_overhead_ms': round(r['overhead_ms'] / r['timed_upstream'], 1) if r['timed_upstream'] else None,
                    'definition_tokens': entry.get('definition_tokens'),
                    'flags': sorted(flags.get(name, set()))})
    out.sort(key=lambda r: (-len(r['flags']), -r['calls'], r['tool']))
    return out


def analyze_sessions(sessions: list[dict], catalog: dict | None = None, config: AdvisorConfig | None = None) -> dict:
    """sessions: [{'id', 'events', 'status'?, 'metadata'?}] in the tracer event schema."""
    catalog = catalog or {}
    config = config or AdvisorConfig()
    traces = [build_trace(str(s.get('id', i)), s.get('events', []), catalog=catalog,
                          status=s.get('status', 'unknown'), metadata=s.get('metadata'))
              for i, s in enumerate(sessions)]
    decompositions = [decompose(t, catalog) for t in traces]
    findings = []
    for t, d in zip(traces, decompositions):
        findings.extend(detect_session(t, d, config))
    findings.extend(detect_cross_session(traces, decompositions, catalog, config))
    ownership = {c.name: c.ownership for t in traces for c in t.tool_calls}
    n = max(1, len(traces))
    recs = recommend(findings, len(traces), catalog, ownership)

    lat_keys = ('wall_ms', 'llm_ms', 'tool_upstream_ms', 'tool_overhead_ms', 'tool_unattributed_ms', 'harness_ms')
    latency = {k: round(sum(d['latency'][k] for d in decompositions), 1) for k in lat_keys}
    budget = {k: sum(d['token_budget']['by_category'][k] for d in decompositions) for k in CATEGORIES}
    by_server = defaultdict(int)
    by_tool = defaultdict(int)
    for d in decompositions:
        for k, v in d['token_budget']['tool_definitions_by_server'].items():
            by_server[k] += v
        for k, v in d['token_budget']['tool_results_by_tool'].items():
            by_tool[k] += v
    total_input = sum(d['token_budget']['total_input_tokens'] for d in decompositions)
    reported = sum(d['token_budget']['reported_input_tokens'] for d in decompositions)
    cached = sum(d['token_budget']['cached_input_tokens'] for d in decompositions)
    tool_calls = sum(d['tool_calls'] for d in decompositions)
    failed = sum(1 for t in traces for c in t.tool_calls if not c.ok)
    dup = sum(f['metrics']['calls'] - 1 for f in findings if f['detector'] == 'duplicate_fetch')
    loops = sum(f['metrics']['attempts'] - 1 for f in findings if f['detector'] == 'failure_loop')
    # Fixes overlap (e.g. code mode also removes repeated schema cost), so combine them multiplicatively:
    # each fix removes its share of what remains, capped at 90% per fix.
    avg_input = total_input / n if total_input else 0
    remaining = 1.0
    for r in recs:
        if avg_input:
            remaining *= 1 - min(0.9, r['estimated_savings_per_session']['tokens'] / avg_input)
    recoverable_share = round(1 - remaining, 3) if avg_input else None
    summary = {
        'sessions': len(traces), 'llm_turns': sum(d['llm_turns'] for d in decompositions), 'tool_calls': tool_calls,
        'total_input_tokens': total_input, 'total_output_tokens': sum(d['token_budget']['total_output_tokens'] for d in decompositions),
        'cache_read_ratio': round(cached / reported, 3) if reported and cached else None,
        'latency_ms': latency, 'token_budget': budget,
        'tool_definitions_by_server': dict(sorted(by_server.items(), key=lambda x: -x[1])),
        'tool_results_by_tool': dict(sorted(by_tool.items(), key=lambda x: -x[1])),
        'efficiency': {'duplicate_fetch_calls': dup,
                       'estimated_recoverable_input_tokens': int(total_input * recoverable_share) if recoverable_share is not None else 0,
                       'estimated_recoverable_share': recoverable_share},
        'efficacy': {'tool_error_rate': round(failed / tool_calls, 3) if tool_calls else None,
                     'repeated_failure_calls': loops,
                     'avg_model_turns_per_tool_call': round(sum(d['llm_turns'] for d in decompositions) / tool_calls, 2) if tool_calls else None},
        'avg_turns_per_session': round(statistics.mean([d['llm_turns'] for d in decompositions]), 2) if decompositions else 0,
    }
    return {
        'engine': 'harness-advisor-v1', 'summary': summary, 'recommendations': recs, 'findings': findings,
        'tools': _tool_scorecard(traces, findings, catalog, n), 'sessions': decompositions,
        'limitations': [
            'Findings are heuristics from captured events, not confirmed root causes; confidence is not a calibrated probability.',
            'Token composition is estimated (chars/4) and apportioned onto reported usage when available.',
            'Usage of tool results is inferred from lexical overlap unless the harness reports used_ratio.',
            'Savings are estimates; validate top recommendations with paired trials before adopting them.',
            'Missing instrumentation hides problems; an empty report does not certify an efficient harness.'],
    }
