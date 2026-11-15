"""Step 3: aggregate findings across sessions into ranked, owner-aware recommendations."""
from __future__ import annotations

from collections import defaultdict

# Levers per detector. 'server_owner' levers are offered only when you own the tool.
LEVERS: dict[str, dict] = {
    'chatty_tool': {
        'title': 'Move {tool} calls to code mode or a CLI',
        'why': 'Each page or item costs a full model turn; a script can paginate and fan out in one step and return only the aggregate.',
        'harness_owner': ['Expose {tool} through code mode: generate typed stubs from tools/list (MCPorter emit-ts, code2mcp) and let the model write one script that loops in a sandbox.',
                          'For bulk reads, call the official CLI/REST API through the egress gateway with delegated credentials.'],
        'server_owner': ['Add a composite, task-shaped tool (e.g. a summary over all pages) so one call replaces the loop.'],
        'gateway_operator': ['Publish a virtual tool on the gateway that paginates server-side and returns the aggregate.'],
        'vendor_request': ['Ask the vendor for a batch/aggregate endpoint or larger page sizes, citing call counts.'],
    },
    'dead_schema': {
        'title': 'Defer or list-only unused {tool} tools',
        'why': 'These schemas are paid for on every model turn but the tools are rarely called.',
        'harness_owner': ['Mark the unused {tool} tools deferred (tool search / defer_loading) so schemas load only when searched.',
                          'Or remove them from this agent\'s allowlist and move the workflow into a skill that loads on demand.'],
        'server_owner': ['Shorten descriptions and drop rarely-used optional parameters.'],
        'gateway_operator': ['Expose a curated virtual {tool} server that omits these tools for this agent.'],
        'vendor_request': [],
    },
    'large_low_use_result': {
        'title': 'Shrink {tool} results before they reach the model',
        'why': 'Large results are paid for on this and every later turn, yet most of the content is never used.',
        'harness_owner': ['Post-process {tool} results: project only needed fields, truncate, or summarise before adding to context.',
                          'Store the full result as a file/resource and give the model a handle plus a short preview.',
                          'If the tool already supports field selection or compact views (e.g. responseFields, view=compact), add that to the agent\'s instructions or a skill.'],
        'server_owner': ['Make field projection or a compact view the default; add smaller default page sizes.'],
        'gateway_operator': ['Apply a response transform (field projection / size cap) on the gateway route for {tool}.'],
        'vendor_request': ['Request field selection or a compact response mode, citing measured unused share.'],
    },
    'duplicate_fetch': {
        'title': 'Memoize {tool} reads within a session',
        'why': 'The same read with the same arguments was repeated, re-adding identical data to context.',
        'harness_owner': ['Cache read-only results per session keyed by tool + arguments; return the cached copy or a reference.',
                          'Keep earlier results reachable (avoid compacting them away) so the model does not re-fetch.'],
        'server_owner': ['Mark the tool as read-only/idempotent so clients can cache safely.'],
        'gateway_operator': ['Enable a short-TTL response cache for {tool} on the gateway.'],
        'vendor_request': [],
    },
    'failure_loop': {
        'title': 'Stop retry loops on {tool}',
        'why': 'Identical failing calls were repeated without changing arguments.',
        'harness_owner': ['Add a retry guard: block an identical (tool, arguments) call after a failure and surface the error to the model.',
                          'Add a circuit breaker for persistent upstream errors.'],
        'server_owner': ['Return actionable errors that name the missing/invalid argument and the fix.'],
        'gateway_operator': ['Validate required arguments at the gateway and reject early with a clear message.'],
        'vendor_request': ['Report the unhelpful error message with examples.'],
    },
    'high_error_rate': {
        'title': 'Fix {tool} schema and descriptions',
        'why': 'A high share of calls fail, usually from mis-specified arguments rather than upstream outages.',
        'harness_owner': ['Validate arguments against the schema before calling and give the model a corrective message.',
                          'Add usage examples for {tool} in a skill or system prompt.'],
        'server_owner': ['Tighten the input schema (enums, required fields) and clarify the description.'],
        'gateway_operator': ['Track error classes per tool and alert on spikes.'],
        'vendor_request': ['Share failing argument patterns and ask for clearer schema/docs.'],
    },
    'serial_independent_calls': {
        'title': 'Run independent tool calls in parallel',
        'why': 'Calls that did not depend on each other still waited for separate model turns.',
        'harness_owner': ['Enable parallel tool calls and say so in the system prompt.', 'Or add a batch tool / code mode for multi-item lookups.'],
        'server_owner': ['Offer a batch variant of frequently fanned-out tools.'],
        'gateway_operator': [],
        'vendor_request': [],
    },
    'protocol_overhead': {
        'title': 'Cut transport overhead on the {tool} server',
        'why': 'Most of the tool time is spent outside the upstream API (MCP client/server, gateway, serialization).',
        'harness_owner': ['Reuse warm MCP sessions/connections; use stdio for local servers.', 'For hot paths, call the API directly (CLI/REST) through the gateway.'],
        'server_owner': ['Co-locate the {tool} MCP server with the API it calls.'],
        'gateway_operator': ['Remove redundant gateway hops and check per-request auth/introspection latency.'],
        'vendor_request': [],
    },
    'cache_prefix_unstable': {
        'title': 'Stabilise the cached prompt prefix',
        'why': 'Low cache-read ratio while the system/tool prefix changes between turns.',
        'harness_owner': ['Make tool order and system prompt deterministic; move volatile content (time, IDs) after the cache breakpoint.',
                          'Use deferred tools so discovered tools are appended rather than changing the prefix.'],
        'server_owner': [], 'gateway_operator': [], 'vendor_request': [],
    },
    'context_dominance': {
        'title': 'Compact conversation history',
        'why': 'History dominates late-turn input and keeps growing.',
        'harness_owner': ['Add compaction or prompt compression after N turns; keep decisions and drop raw tool output.',
                          'Offload noisy sub-tasks to a subagent so only its summary returns.'],
        'server_owner': [], 'gateway_operator': [], 'vendor_request': [],
    },
}

LEVERS['tool_selection'] = {
    'title': 'Improve tool choice for {tool}',
    'why': 'An LLM judge rated a large share of sampled calls to this tool as a poor choice for the goal.',
    'harness_owner': ['Add routing hints to the system prompt or a skill: when to use {tool} versus the suggested alternative.',
                      'Hide overlapping tools from this agent so the right one is unambiguous.'],
    'server_owner': ['Rename or rewrite the {tool} description so its purpose and limits are explicit; add usage examples.'],
    'gateway_operator': ['Expose a curated virtual server without the overlapping tool for this agent.'],
    'vendor_request': ['Ask the vendor to clarify the tool description and when to prefer alternatives.'],
}

SEVERITY_WEIGHT = {'high': 3, 'medium': 2, 'low': 1}


def _levers(detector: str, tool: str | None, ownership: str) -> list[dict]:
    spec = LEVERS[detector]
    owners = ['harness_owner', 'gateway_operator']
    if ownership in ('first_party', 'unknown'):
        owners.insert(1, 'server_owner')
    if ownership in ('third_party', 'unknown'):
        owners.append('vendor_request')
    out = []
    for owner in owners:
        for action in spec.get(owner, []):
            out.append({'owner': owner, 'action': action.format(tool=tool or 'these tools')})
    return out


def recommend(findings: list[dict], session_count: int, catalog: dict, ownership_by_tool: dict) -> list[dict]:
    n = max(1, session_count)
    groups = defaultdict(list)
    for f in findings:
        groups[(f['detector'], f['tool'])].append(f)
    recs = []
    for (detector, tool), group in groups.items():
        sessions = set()
        for f in group:
            if f.get('session_id'):
                sessions.add(f['session_id'])
        cross = group[0].get('session_id') is None
        if cross:
            per_session = group[0]['savings']
            affected = len(group[0].get('sessions', [])) if detector == 'high_error_rate' else n
        else:
            per_session = {k: sum(f['savings'][k] for f in group) / n for k in ('tokens', 'ms', 'turns')}
            affected = len(sessions)
        ownership = (group[0].get('ownership') or (catalog.get('tools') or {}).get(tool or '', {}).get('ownership')
                     or ownership_by_tool.get(tool, 'unknown'))
        if tool is None:
            ownership = 'harness'
        severity = max((f['severity'] for f in group), key=lambda s: SEVERITY_WEIGHT[s])
        score = (per_session['tokens'] / 1000 + per_session['ms'] / 1000 + per_session['turns'] * 0.5) \
            * (0.5 + 0.5 * affected / n) * SEVERITY_WEIGHT[severity]
        if detector == 'tool_selection':
            # No direct savings estimate: rank by how often the judge flagged the tool.
            score = SEVERITY_WEIGHT[severity] * group[0]['metrics']['flagged'] / n
        spec = LEVERS[detector]
        levers = _levers(detector, tool, ownership if ownership != 'harness' else 'first_party')
        if ownership == 'harness':
            levers = [l for l in levers if l['owner'] in ('harness_owner', 'gateway_operator')]
        note = None
        if ownership == 'third_party':
            note = 'Third-party server: only levers you control are listed (harness, gateway, facade) plus a vendor request.'
        elif ownership == 'unknown' and tool:
            note = 'Ownership unknown; classify this tool in the catalog to filter levers.'
        recs.append({
            'id': f'{detector}:{tool or "harness"}',
            'detector': detector, 'dimension': group[0]['dimension'], 'tool': tool, 'ownership': ownership,
            'title': spec['title'].format(tool=tool or 'harness'),
            'why': spec['why'], 'severity': severity, 'priority_score': round(score, 3),
            'sessions_affected': affected, 'sessions_analyzed': session_count, 'occurrences': len(group),
            'estimated_savings_per_session': {'tokens': int(per_session['tokens']), 'ms': round(per_session['ms'], 1),
                                              'turns': round(per_session['turns'], 2)},
            'levers': levers, 'note': note,
            'evidence': ([{'session_id': x['session_id'], 'seqs': [x['seq']], 'detail': x['reason']} for x in group[0]['evidence_items']]
                         if group[0].get('evidence_items') else
                         [{'session_id': f.get('session_id'), 'seqs': f['seqs'][:12], 'detail': f['detail']} for f in group[:5]]),
            'confidence': group[0].get('confidence', 'heuristic'),
        })
    recs.sort(key=lambda r: -r['priority_score'])
    for i, r in enumerate(recs, 1):
        r['rank'] = i
    return recs
