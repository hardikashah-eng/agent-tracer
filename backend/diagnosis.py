"""Read-only, deterministic diagnostic hypotheses over a captured trace prefix."""
import json
import re
from collections import defaultdict


def diagnose(events, status='unknown', paused=False):
    findings = []

    def add(code, title, observation, hypothesis, refs, checks, missing, priority=2):
        findings.append(dict(id=code, title=title, observation=observation,
                             hypothesis=hypothesis, evidence_seqs=sorted(set(refs)),
                             next_checks=checks, missing_evidence=missing,
                             priority=priority, confidence='heuristic', confirmed=False))

    errors = []
    for e in events:
        p = e.get('payload') or {}
        # Inspect explicit error fields; ordinary quoted text is not an error signal.
        explicit = p.get('error') or p.get('exception')
        failure = e.get('kind', '').split('.')[-1] in ('error', 'failed') or e.get('kind') == 'error' or p.get('status') in ('error', 'failed') or p.get('success') is False
        if explicit or failure:
            errors.append(e)
    if errors:
        timeout = [e for e in errors if re.search(r'timeout|timed out|deadline', json.dumps(e.get('payload', {})), re.I)]
        if timeout:
            add('timeout', 'Timeout observed', f'{len(timeout)} explicit error events mention a timeout.',
                'A dependency may be slow/unreachable, or the request may exceed its timeout budget.',
                [e['seq'] for e in timeout], ['Check dependency latency and health using request IDs.', 'Compare the same request on another worker and inspect the timeout setting.'],
                ['Dependency logs, request IDs, timeout budget, and worker metadata'], 1)
        other = [e for e in errors if e not in timeout]
        if other:
            add('execution_error', 'Execution failure observed', f'{len(other)} events carry explicit failure signals.',
                'Tool execution, input validation, or agent code may have failed; the trace alone does not distinguish them.',
                [e['seq'] for e in other], ['Inspect the cited error payloads and correlate server logs.', 'Validate tool arguments against the tool schema.'], ['Stack trace, tool schema, and correlated service logs'], 1)

    calls = defaultdict(list)
    for e in events:
        if e.get('kind') == 'tool.before':
            p = e.get('payload') or {}
            if 'arguments' in p:
                key = (e.get('name'), json.dumps(p['arguments'], sort_keys=True, default=str))
                calls[key].append(e)
    for group in calls.values():
        if len(group) >= 3:
            add('repeated_call_' + str(group[0]['seq']), 'Repeated tool request',
                f'{len(group)} calls to {group[0].get("name")} have identical captured arguments.',
                'A retry loop or missing progress detection is possible; repeated calls may also be intentional.',
                [e['seq'] for e in group], ['Compare intervening tool results and retry policy.', 'Check whether state or outputs changed between attempts.'], ['Retry policy and intended workflow'])

    snaps = [e for e in events if e.get('kind') == 'context.snapshot']
    for before, after in zip(snaps, snaps[1:]):
        current = {m.get('id') for m in after.get('payload', {}).get('messages', [])}
        removed = [m for m in before.get('payload', {}).get('messages', []) if m.get('id') is not None and m.get('id') not in current and m.get('role') in ('system', 'developer')]
        if removed:
            add('instruction_removed_' + str(after['seq']), 'Instruction absent from later prompt',
                f'{len(removed)} system/developer messages disappeared between captured prompts.',
                'Compaction or context assembly may have removed a relevant instruction; replacement or omission may be intentional.',
                [before['seq'], after['seq']], ['Compare both prompts and any replacement instructions.', 'Check compaction rules before attributing a failure to the removal.'], ['Instruction relevance and compaction policy'])
    for e in events:
        if e.get('kind') != 'tool.after':
            continue
        later = [s for s in snaps if s['seq'] > e['seq']]
        if later and not any(m.get('source') == e.get('name') for s in later for m in s.get('payload', {}).get('messages', [])):
            add('unattributed_result_' + str(e['seq']), 'Tool result lacks prompt attribution',
                'No later captured prompt message attributes its source to this tool name.',
                'The result may have been omitted, transformed, or captured without provenance metadata.',
                [e['seq'], later[0]['seq']], ['Inspect prompt assembly and source attribution.', 'Check whether the result was intentionally summarized or unused.'], ['Stable tool-call IDs and complete provenance metadata'], 3)
    findings.sort(key=lambda f: (f['priority'], f['evidence_seqs'][0], f['id']))
    return dict(engine='local-rules-v1', status=status, paused=paused,
                analyzed_through_seq=max((e['seq'] for e in events), default=0),
                event_count=len(events), hypotheses=findings,
                summary=f'{len(findings)} diagnostic hypotheses from captured evidence.' if findings else 'Insufficient diagnostic signals in the captured trace; this does not establish that the run is healthy.',
                limitations=['Hypotheses are heuristic, not confirmed root causes or calibrated probabilities.',
                             'Only captured events are analyzed; missing instrumentation can hide failures.',
                             'No LLM, external service checks, automatic remediation, or replay is performed.'])
