"""Step 2: deterministic efficiency, efficacy and latency detectors.

Every finding is a heuristic with evidence references and an estimate of the
per-session saving if it were fixed. Nothing here calls a model or a tool.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .model import SessionTrace, content_hash

_WORD = re.compile(r'[A-Za-z0-9_\-./]{4,}')
_STOP = {'true', 'false', 'null', 'none', 'with', 'this', 'that', 'from', 'have', 'were', 'will', 'there', 'their'}


@dataclass
class AdvisorConfig:
    duplicate_min: int = 2
    failure_loop_min: int = 2
    chatty_min_calls: int = 3
    chatty_distinct_ratio: float = 0.6
    large_result_tokens: int = 800
    low_use_ratio: float = 0.15
    dead_schema_max_session_share: float = 0.1
    high_error_rate: float = 0.2
    high_error_min_calls: int = 5
    overhead_min_ms: float = 150.0
    overhead_min_share: float = 0.25
    cache_low_ratio: float = 0.3
    context_dominance_share: float = 0.5
    context_min_turns: int = 6
    serial_min_pairs: int = 2


def _values(obj: Any) -> list[str]:
    """Collect scalar values (not keys) so JSON field names do not count as 'used'."""
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_values(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_values(v))
    elif obj is not None:
        out.append(str(obj))
    return out


def _terms(obj: Any) -> set[str]:
    words = set()
    for text in _values(obj) if not isinstance(obj, str) else [obj]:
        words.update(w.lower() for w in _WORD.findall(text))
    return {w for w in words if w not in _STOP}


def _finding(detector, tool, trace, seqs, severity, detail, metrics, savings, dimension, ownership=None):
    return {'detector': detector, 'dimension': dimension, 'tool': tool, 'session_id': trace.id, 'ownership': ownership,
            'seqs': sorted(set(s for s in seqs if s is not None)), 'severity': severity, 'detail': detail,
            'metrics': metrics, 'savings': {'tokens': int(savings.get('tokens', 0)),
                                            'ms': round(savings.get('ms', 0.0), 1),
                                            'turns': round(savings.get('turns', 0.0), 2)},
            'confidence': 'heuristic'}


def _turn_costs(trace: SessionTrace, decomposition: dict) -> tuple[float, float]:
    turns = decomposition['token_budget']['turns']
    tok = statistics.mean([t['input_tokens'] or 0 for t in turns]) if turns else 0.0
    ms = statistics.mean([c.duration_ms or 0 for c in trace.llm_calls]) if trace.llm_calls else 0.0
    return tok, ms


def detect_session(trace: SessionTrace, decomposition: dict, config: AdvisorConfig) -> list[dict]:
    out: list[dict] = []
    turn_tokens, turn_ms = _turn_costs(trace, decomposition)
    calls = trace.tool_calls

    # duplicate_fetch: same read tool + same args succeeding more than once.
    groups = defaultdict(list)
    for c in calls:
        if c.ok and c.kind in ('read', 'unknown'):
            groups[(c.name, c.args_key)].append(c)
    for (name, _), group in groups.items():
        if len(group) >= config.duplicate_min:
            same = len({content_hash(c.result) for c in group if c.result is not None}) <= 1
            wasted = group[1:]
            extra_turns = sum(1 for prev, c in zip(group, wasted) if c.llm_turn > prev.llm_turn)
            out.append(_finding('duplicate_fetch', name, trace, [c.seq for c in group], 'medium',
                                f'{len(group)} identical successful calls to {name}' + (' returned the same result.' if same else '.'),
                                {'calls': len(group), 'identical_results': same, 'arguments': group[0].arguments},
                                {'tokens': sum(c.result_tokens for c in wasted) + extra_turns * turn_tokens,
                                 'ms': sum(c.duration_ms or 0 for c in wasted) + extra_turns * turn_ms,
                                 'turns': extra_turns}, 'efficiency', ownership=group[0].ownership))

    # failure_loop: same tool + args + error class repeated.
    fails = defaultdict(list)
    for c in calls:
        if not c.ok:
            fails[(c.name, c.args_key, c.error_class)].append(c)
    for (name, _, err), group in fails.items():
        if len(group) >= config.failure_loop_min:
            wasted = group[1:]
            out.append(_finding('failure_loop', name, trace, [c.seq for c in group], 'high',
                                f'{name} failed {len(group)} times with identical arguments and the same error ({err}).',
                                {'attempts': len(group), 'error_class': err, 'arguments': group[0].arguments},
                                {'tokens': len(wasted) * turn_tokens, 'ms': sum((c.duration_ms or 0) + turn_ms for c in wasted),
                                 'turns': len(wasted)}, 'efficacy', ownership=group[0].ownership))

    # large_low_use_result
    later_text_by_seq = []
    for c in trace.llm_calls:
        later_text_by_seq.append((c.seq, c.output))
    for snap in trace.snapshots:
        for m in (snap.get('payload') or {}).get('messages', []):
            if m.get('role') == 'assistant':
                later_text_by_seq.append((snap.get('seq', 0), m.get('content')))
    for c in calls:
        if not c.ok or c.result_tokens < config.large_result_tokens:
            continue
        ratio = c.used_ratio
        if ratio is None:
            terms = _terms(c.result)
            later = [txt for seq, txt in later_text_by_seq if seq > (c.end_seq or c.seq) and txt is not None]
            if len(terms) < 10 or not later:
                continue
            seen = set()
            for txt in later:
                seen |= _terms(txt)
            ratio = len(terms & seen) / len(terms)
        if ratio <= config.low_use_ratio:
            keep = max(ratio, 0.2)
            out.append(_finding('large_low_use_result', c.name, trace, [c.seq, c.end_seq], 'medium',
                                f'{c.name} returned ~{c.result_tokens} tokens; only ~{ratio:.0%} of its distinctive content reappears later.',
                                {'result_tokens': c.result_tokens, 'used_ratio': round(ratio, 3),
                                 'reported_ratio': c.used_ratio is not None},
                                {'tokens': c.result_tokens * (1 - keep)}, 'efficiency', ownership=c.ownership))

    # chatty_tool: many distinct-arg calls to read tools, each in its own model turn.
    # Chatty tools on the same server are merged: code mode / CLI applies per server.
    by_name = defaultdict(list)
    chatty = set()
    for c in calls:
        by_name[c.name].append(c)
    chatty_by_server = defaultdict(list)
    for name, group in by_name.items():
        if len(group) < config.chatty_min_calls or group[0].kind == 'write':
            continue
        distinct = len({c.args_key for c in group}) / len(group)
        turns = len({c.llm_turn for c in group})
        if distinct >= config.chatty_distinct_ratio and turns >= max(2, len(group) - 1):
            chatty.add(name)
            key = group[0].server if group[0].server != 'unknown' else f'tool:{name}'
            chatty_by_server[key].extend(group)
    for key, group in chatty_by_server.items():
        names = sorted({c.name for c in group})
        label = names[0] if key.startswith('tool:') else key
        turns = len({c.llm_turn for c in group})
        result_tokens = sum(c.result_tokens for c in group)
        what = names[0] if len(names) == 1 else f'{len(names)} {label} tools ({", ".join(names)})'
        out.append(_finding('chatty_tool', label, trace, [c.seq for c in group], 'high',
                            f'{len(group)} calls to {what} across {turns} model turns with mostly distinct arguments (pagination or per-item fan-out).',
                            {'calls': len(group), 'model_turns': turns, 'tools': names, 'result_tokens': result_tokens,
                             'transport': group[0].transport, 'server': group[0].server},
                            {'turns': turns - 1, 'tokens': (turns - 1) * turn_tokens + result_tokens * 0.8,
                             'ms': (turns - 1) * turn_ms}, 'efficiency', ownership=group[0].ownership))

    # serial_independent_calls: consecutive reads in different turns, later args not derived from earlier result.
    pairs = []
    reads = [c for c in calls if c.ok and c.kind != 'write' and c.name not in chatty]
    for a, b in zip(reads, reads[1:]):
        # Cross-tool dependencies cannot be inferred lexically, so only same-tool fan-out is flagged.
        if b.llm_turn <= a.llm_turn or a.name != b.name or a.args_key == b.args_key:
            continue
        arg_terms = _terms(b.arguments)
        if arg_terms and not (arg_terms & _terms(a.result)):
            pairs.append((a, b))
    if len(pairs) >= config.serial_min_pairs:
        out.append(_finding('serial_independent_calls', None, trace, [p[1].seq for p in pairs], 'low',
                            f'{len(pairs)} tool calls waited for a new model turn although their arguments did not depend on the previous result.',
                            {'pairs': len(pairs), 'tools': sorted({p[1].name for p in pairs})},
                            {'turns': len(pairs), 'ms': len(pairs) * turn_ms, 'tokens': len(pairs) * turn_tokens * 0.5},
                            'latency'))

    # protocol_overhead per server: time spent outside the upstream API.
    by_server = defaultdict(list)
    for c in calls:
        if c.upstream_ms is not None and c.duration_ms:
            by_server[c.server].append(c)
    for server, timed in by_server.items():
        overheads = [max(0.0, c.duration_ms - c.upstream_ms) for c in timed]
        med = statistics.median(overheads)
        share = sum(overheads) / max(1e-9, sum(c.duration_ms for c in timed))
        if med >= config.overhead_min_ms and share >= config.overhead_min_share:
            out.append(_finding('protocol_overhead', server, trace, [c.seq for c in timed], 'low',
                                f'{server}: median {med:.0f} ms per call ({share:.0%} of tool time) is spent outside the upstream API.',
                                {'median_overhead_ms': round(med, 1), 'overhead_share': round(share, 3), 'calls': len(timed),
                                 'transport': timed[0].transport, 'tools': sorted({c.name for c in timed})},
                                {'ms': sum(overheads) * 0.7}, 'latency', ownership=timed[0].ownership))

    # cache_prefix_unstable / context_dominance from the token budget.
    budget = decomposition['token_budget']
    turns = budget['turns']
    if len(turns) >= 4 and budget['reported_input_tokens']:
        ratio = (budget['cached_input_tokens'] or 0) / budget['reported_input_tokens']
        prefixes = [t['prefix_hash'] for t in turns if t['prefix_hash']]
        changes = sum(1 for x, y in zip(prefixes, prefixes[1:]) if x != y)
        if ratio < config.cache_low_ratio and (changes or not prefixes):
            stable = sum((t['composition']['system'] + t['composition']['tool_definitions']) for t in turns)
            out.append(_finding('cache_prefix_unstable', None, trace, [t['seq'] for t in turns], 'medium' if changes else 'low',
                                f'Only {ratio:.0%} of input tokens were cache reads' + (f'; the system/tool prefix changed {changes} times.' if changes else '; prefix stability is not instrumented.'),
                                {'cache_read_ratio': round(ratio, 3), 'prefix_changes': changes, 'turns': len(turns)},
                                {'tokens': stable * 0.9 * 0.5}, 'efficiency'))
    if len(turns) >= config.context_min_turns:
        late = turns[len(turns) // 2:]
        conv = sum(t['composition']['conversation'] for t in late)
        total = sum(t['input_tokens'] or 0 for t in late)
        first = turns[0]['input_tokens'] or 0
        last = turns[-1]['input_tokens'] or 0
        if total and conv / total >= config.context_dominance_share and last > first * 2:
            out.append(_finding('context_dominance', None, trace, [t['seq'] for t in late], 'medium',
                                f'Conversation history is {conv / total:.0%} of late-turn input; prompt grew from {first} to {last} tokens.',
                                {'history_share': round(conv / total, 3), 'first_input': first, 'last_input': last, 'turns': len(turns)},
                                {'tokens': conv * 0.4}, 'efficiency'))
    return out


def detect_cross_session(traces: list[SessionTrace], decompositions: list[dict], catalog: dict,
                         config: AdvisorConfig) -> list[dict]:
    """Findings that only make sense over many sessions: dead schemas and error rates."""
    out = []
    n = len(traces) or 1
    calls = [c for t in traces for c in t.tool_calls]
    by_tool = defaultdict(list)
    for c in calls:
        by_tool[c.name].append(c)
    for name, group in by_tool.items():
        if len(group) >= config.high_error_min_calls:
            failed = [c for c in group if not c.ok]
            rate = len(failed) / len(group)
            if rate >= config.high_error_rate:
                errs = defaultdict(int)
                for c in failed:
                    errs[c.error_class] += 1
                out.append({'detector': 'high_error_rate', 'dimension': 'efficacy', 'tool': name, 'session_id': None,
                            'seqs': [], 'severity': 'high' if rate >= 0.4 else 'medium',
                            'detail': f'{name} failed on {rate:.0%} of {len(group)} calls across sessions.',
                            'metrics': {'calls': len(group), 'failures': len(failed), 'error_rate': round(rate, 3),
                                        'top_errors': dict(sorted(errs.items(), key=lambda x: -x[1])[:3])},
                            'savings': {'tokens': 0, 'ms': round(sum(c.duration_ms or 0 for c in failed) / n, 1),
                                        'turns': round(len(failed) / n, 2)},
                            'confidence': 'heuristic', 'sessions': sorted({c.session_id for c in failed})})
    used_in = defaultdict(set)
    for c in calls:
        used_in[c.name].add(c.session_id)
    avg_turns = statistics.mean([d['llm_turns'] for d in decompositions]) if decompositions else 0
    dead_by_server = defaultdict(list)
    for name, entry in (catalog.get('tools') or {}).items():
        defs = entry.get('definition_tokens')
        if not defs or not entry.get('loaded', True):
            continue
        share = len(used_in.get(name, set())) / n
        if share <= config.dead_schema_max_session_share:
            dead_by_server[entry.get('server', 'unknown')].append((name, defs, share, entry.get('ownership', 'unknown')))
    for server, tools in dead_by_server.items():
        defs_total = sum(t[1] for t in tools)
        saved = sum(t[1] * avg_turns * (1 - t[2]) for t in tools)
        names = [t[0] for t in sorted(tools, key=lambda t: -t[1])]
        out.append({'detector': 'dead_schema', 'dimension': 'efficiency', 'tool': server, 'session_id': None,
                    'ownership': tools[0][3], 'seqs': [], 'severity': 'medium' if defs_total * avg_turns >= 2000 else 'low',
                    'detail': f'{len(tools)} {server} tool schemas add ~{defs_total} tokens to every turn but were called in at most '
                              f'{max(t[2] for t in tools):.0%} of sessions: {", ".join(names)}.',
                    'metrics': {'tools': names, 'definition_tokens': defs_total, 'avg_turns': round(avg_turns, 1), 'server': server},
                    'savings': {'tokens': int(saved), 'ms': 0.0, 'turns': 0.0},
                    'confidence': 'heuristic', 'sessions': sorted({sid for t in tools for sid in used_in.get(t[0], set())})})
    return out
