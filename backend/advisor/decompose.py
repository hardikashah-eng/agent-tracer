"""Step 1: latency and token-budget decomposition per session."""
from __future__ import annotations

from collections import defaultdict

from .model import SessionTrace

CATEGORIES = ('system', 'tool_definitions', 'conversation', 'tool_results', 'other')


def _snapshot_composition(snapshot: dict | None, catalog: dict) -> tuple[dict, dict, dict, str | None]:
    """Return (estimated tokens per category, tool-result tokens per source, definition tokens per server, prefix hash)."""
    cats = defaultdict(int)
    by_tool = defaultdict(int)
    by_server = defaultdict(int)
    if not snapshot:
        for entry in (catalog.get('tools') or {}).values():
            if entry.get('loaded', True) and entry.get('definition_tokens'):
                cats['tool_definitions'] += entry['definition_tokens']
                by_server[entry.get('server', 'unknown')] += entry['definition_tokens']
        return cats, by_tool, by_server, None
    p = snapshot.get('payload') or {}
    system_hashes = []
    for m in p.get('messages', []):
        tokens = m.get('estimated_tokens', 0) or 0
        cat = m.get('category') or ('tool' if m.get('role') == 'tool' else 'system' if m.get('role') in ('system', 'developer') else 'conversation')
        if cat == 'tool':
            cats['tool_results'] += tokens
            by_tool[m.get('source') or 'unattributed'] += tokens
        elif cat in ('tool_definitions', 'tools'):
            cats['tool_definitions'] += tokens
            by_server[m.get('source') or 'unknown'] += tokens
        elif cat == 'system':
            cats['system'] += tokens
            system_hashes.append(m.get('content_hash') or str(m.get('content', ''))[:200])
        elif cat == 'conversation':
            cats['conversation'] += tokens
        else:
            cats['other'] += tokens
    defs = p.get('tool_definitions')
    if isinstance(defs, list) and defs:
        for d in defs:
            t = d.get('estimated_tokens') or d.get('tokens') or 0
            cats['tool_definitions'] += t
            by_server[d.get('server', 'unknown')] += t
    elif not cats['tool_definitions'] and p.get('use_catalog_definitions', True) and catalog.get('tools'):
        for name, entry in catalog['tools'].items():
            if entry.get('loaded', True) and entry.get('definition_tokens'):
                cats['tool_definitions'] += entry['definition_tokens']
                by_server[entry.get('server', 'unknown')] += entry['definition_tokens']
    prefix = p.get('tool_definitions_hash')
    prefix = f"{prefix}|{'|'.join(system_hashes)}" if (prefix or system_hashes) else None
    return cats, by_tool, by_server, prefix


def decompose(trace: SessionTrace, catalog: dict | None = None) -> dict:
    catalog = catalog or {}
    llm_ms = sum(c.duration_ms or 0 for c in trace.llm_calls)
    tool_ms = upstream = overhead = unattributed = 0.0
    by_transport = defaultdict(float)
    for t in trace.tool_calls:
        d = t.duration_ms or 0
        tool_ms += d
        by_transport[t.transport] += d
        if t.upstream_ms is not None:
            upstream += min(t.upstream_ms, d) if d else t.upstream_ms
            overhead += max(0.0, d - t.upstream_ms)
        else:
            unattributed += d
    covered = llm_ms + tool_ms
    wall = trace.wall_ms if trace.wall_ms is not None else covered
    latency = {
        'wall_ms': round(wall, 1), 'llm_ms': round(llm_ms, 1), 'tool_ms': round(tool_ms, 1),
        'tool_upstream_ms': round(upstream, 1), 'tool_overhead_ms': round(overhead, 1),
        'tool_unattributed_ms': round(unattributed, 1),
        'harness_ms': round(max(0.0, wall - covered), 1),
        'tool_ms_by_transport': {k: round(v, 1) for k, v in by_transport.items()},
    }

    turns = []
    totals = defaultdict(float)
    tool_result_totals = defaultdict(float)
    def_totals = defaultdict(float)
    reported_input = cached = 0
    for i, call in enumerate(trace.llm_calls):
        cats, by_tool, by_server, prefix = _snapshot_composition(call.snapshot, catalog)
        estimated_total = sum(cats.values())
        scale = 1.0
        basis = 'estimated'
        if call.snapshot is None and call.input_tokens:
            # No prompt capture: only schema cost (from the catalog) is known; the rest is unclassified.
            defs = min(cats.get('tool_definitions', 0), call.input_tokens)
            ratio = defs / cats['tool_definitions'] if cats.get('tool_definitions') else 1.0
            by_server = {k: v * ratio for k, v in by_server.items()}
            cats = {'tool_definitions': defs, 'other': call.input_tokens - defs}
            basis = 'catalog+reported' if defs else 'reported_only'
        elif call.input_tokens and estimated_total:
            scale = call.input_tokens / estimated_total
            basis = 'apportioned'
        elif call.input_tokens and not estimated_total:
            cats = {'other': call.input_tokens}
            basis = 'reported_only'
        comp = {k: round(cats.get(k, 0) * scale) for k in CATEGORIES}
        turn = {'index': i, 'seq': call.seq, 'name': call.name, 'basis': basis,
                'input_tokens': call.input_tokens if call.input_tokens is not None else round(estimated_total),
                'output_tokens': call.output_tokens, 'cached_tokens': call.cached_tokens,
                'composition': comp, 'tool_results_by_tool': {k: round(v * scale) for k, v in by_tool.items()},
                'tool_definitions_by_server': {k: round(v * scale) for k, v in by_server.items()},
                'prefix_hash': prefix, 'duration_ms': call.duration_ms}
        turns.append(turn)
        for k, v in comp.items():
            totals[k] += v
        for k, v in turn['tool_results_by_tool'].items():
            tool_result_totals[k] += v
        for k, v in turn['tool_definitions_by_server'].items():
            def_totals[k] += v
        reported_input += call.input_tokens or 0
        cached += call.cached_tokens or 0
    total_input = sum(t['input_tokens'] or 0 for t in turns)
    budget = {
        'turns': turns,
        'total_input_tokens': total_input,
        'total_output_tokens': sum(t['output_tokens'] or 0 for t in turns),
        'reported_input_tokens': reported_input,
        'cached_input_tokens': cached,
        'cache_read_ratio': round(cached / reported_input, 3) if reported_input and cached else None,
        'by_category': {k: round(totals.get(k, 0)) for k in CATEGORIES},
        'tool_results_by_tool': {k: round(v) for k, v in sorted(tool_result_totals.items(), key=lambda x: -x[1])},
        'tool_definitions_by_server': {k: round(v) for k, v in sorted(def_totals.items(), key=lambda x: -x[1])},
    }
    return {'session_id': trace.id, 'status': trace.status, 'llm_turns': len(trace.llm_calls),
            'tool_calls': len(trace.tool_calls), 'latency': latency, 'token_budget': budget}
