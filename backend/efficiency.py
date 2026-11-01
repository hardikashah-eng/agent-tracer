"""Context efficiency diagnostics. Estimates are explicitly heuristic, not provider billing."""
import json
from collections import defaultdict


def _words(text):
    import re
    return set(re.findall(r'[a-zA-Z0-9_]{4,}', text.lower()))


def analyze(events, *, large_tool_tokens=100, duplicate_similarity=0.85):
    snapshots = [e for e in events if e['kind'] == 'context.snapshot']
    findings = []
    calls = []
    seen = {}
    source_first_seen = {}
    all_used_ids = set()
    for index, event in enumerate(snapshots):
        p = event['payload']
        messages = p.get('messages', [])
        current_ids = {str(m['id']) for m in messages}
        all_used_ids.update(current_ids)
        by_category = defaultdict(int)
        for m in messages:
            by_category[m.get('category', 'unknown')] += m.get('estimated_tokens', 0)
            mid = str(m['id'])
            if mid in seen and seen[mid] != m.get('content'):
                findings.append({'severity': 'info', 'kind': 'message_modified', 'snapshot_seq': event['seq'], 'message_id': mid, 'detail': 'Existing message changed between snapshots.'})
            seen[mid] = m.get('content')
            if m.get('category') == 'tool':
                source_first_seen.setdefault(mid, event['seq'])
                size = m.get('estimated_tokens', 0)
                if size >= large_tool_tokens and (index == 0 or mid not in {str(x['id']) for x in snapshots[index-1]['payload'].get('messages', [])}):
                    findings.append({'severity': 'warning', 'kind': 'large_tool_output', 'snapshot_seq': event['seq'], 'message_id': mid, 'estimated_tokens': size, 'detail': f'Tool output adds approximately {size} tokens; consider selective extraction.'})
        if index:
            prev = snapshots[index - 1]['payload'].get('messages', [])
            prev_ids = {str(m['id']) for m in prev}
            for m in prev:
                if str(m['id']) not in current_ids:
                    findings.append({'severity': 'warning', 'kind': 'context_removed', 'snapshot_seq': event['seq'], 'message_id': str(m['id']), 'detail': 'Message disappeared from the outbound prompt; could be truncation, compaction, or intentional omission.'})
        unique = []
        for m in messages:
            tokens = _words(str(m.get('content', '')))
            if len(tokens) < 8:
                continue
            for prior in unique:
                prev_tokens = prior[1]
                similarity = len(tokens & prev_tokens) / max(1, len(tokens | prev_tokens))
                if similarity >= duplicate_similarity:
                    findings.append({'severity': 'info', 'kind': 'duplicate_content', 'snapshot_seq': event['seq'], 'message_id': str(m['id']), 'related_id': str(prior[0]['id']), 'similarity': round(similarity, 2), 'detail': 'Two messages contain highly overlapping text within this prompt.'})
                    break
            unique.append((m, tokens))
        calls.append({'seq': event['seq'], 'name': event['name'], 'estimated_tokens': p.get('estimated_tokens', 0), 'categories': dict(by_category), 'message_count': len(messages)})
    # Tool responses not present in any observed outbound prompt. Only report if a subsequent snapshot exists.
    for event in events:
        if event['kind'] != 'tool.after':
            continue
        subsequent = [s for s in snapshots if s['seq'] > event['seq']]
        if not subsequent:
            continue
        matching = [m for s in subsequent for m in s['payload'].get('messages', []) if m.get('category') == 'tool' and m.get('source') == event['name']]
        if not matching:
            findings.append({'severity': 'warning', 'kind': 'unused_tool_result', 'snapshot_seq': subsequent[0]['seq'], 'source_event_seq': event['seq'], 'detail': f'Tool result from {event["name"]} was not attributed to a subsequent model prompt. Check provenance instrumentation.'})
    totals = [c['estimated_tokens'] for c in calls]
    return {'snapshots': len(calls), 'calls': calls, 'findings': findings,
            'summary': {'latest_prompt_estimated_tokens': totals[-1] if totals else 0,
                        'peak_prompt_estimated_tokens': max(totals, default=0),
                        'cumulative_prompt_estimated_tokens': sum(totals),
                        'potential_repeated_prompt_tokens': sum(totals[:-1]),
                        'warning_count': sum(f['severity'] == 'warning' for f in findings)},
            'disclaimer': 'Token counts are estimated from captured message text (not provider usage). Repeated prompt tokens are not necessarily waste. Removed messages do not prove truncation; unused results depend on source attribution.'}
