"""Normalise captured tracer events into tool calls, model calls and prompt snapshots.

All token figures prefer reported provider usage; otherwise they are chars/4
estimates and are flagged as such.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

READ_HINTS = re.compile(r'^(get|list|search|read|fetch|query|find|describe|show|lookup|view|count)[_\-a-z]*', re.I)
WRITE_HINTS = re.compile(r'(create|update|delete|write|merge|approve|post|send|put|patch|remove|set)', re.I)


def estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return max(1, (len(text) + 3) // 4) if text else 0


def stable_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(stable_key(value).encode('utf-8')).hexdigest()[:16]


def _num(*values):
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def read_usage(payload: dict) -> dict:
    """Accept the usage aliases already supported by the flame chart."""
    u = payload.get('usage') or payload.get('tokens') or {}
    if not isinstance(u, dict):
        u = {}
    details = u.get('input_tokens_details') or u.get('prompt_tokens_details') or {}
    return {
        'input': _num(u.get('input'), u.get('input_tokens'), u.get('prompt_tokens')),
        'output': _num(u.get('output'), u.get('output_tokens'), u.get('completion_tokens')),
        'cached': _num(u.get('cached'), u.get('cached_input'), u.get('cache_read_input_tokens'),
                       details.get('cached_tokens') if isinstance(details, dict) else None),
    }


def is_failure(kind: str, payload: dict) -> bool:
    return (kind.endswith(('.error', '.failed')) or bool(payload.get('error') or payload.get('exception'))
            or payload.get('success') is False or payload.get('status') in ('error', 'failed')
            or payload.get('isError') is True)


def error_class(payload: dict) -> str | None:
    if payload.get('error_type'):
        return str(payload['error_type'])
    msg = payload.get('error') or payload.get('exception')
    if msg is None:
        return None
    text = msg if isinstance(msg, str) else stable_key(msg)
    return re.sub(r'\d+', '#', text.strip().lower())[:80] or 'error'


@dataclass
class ToolCall:
    session_id: str
    name: str
    seq: int
    end_seq: int | None
    arguments: Any
    result: Any
    ok: bool
    error_class: str | None
    duration_ms: float | None
    upstream_ms: float | None
    result_tokens: int
    result_tokens_reported: bool
    used_ratio: float | None
    transport: str
    server: str
    ownership: str
    kind: str
    llm_turn: int  # number of model calls completed before this tool call started

    @property
    def args_key(self) -> str:
        return stable_key(self.arguments)

    @property
    def is_read(self) -> bool:
        return self.kind == 'read'


@dataclass
class LLMCall:
    session_id: str
    name: str
    seq: int
    duration_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    output: Any
    snapshot: dict | None = None


@dataclass
class SessionTrace:
    id: str
    status: str
    events: list
    tool_calls: list = field(default_factory=list)
    llm_calls: list = field(default_factory=list)
    snapshots: list = field(default_factory=list)
    wall_ms: float | None = None
    metadata: dict = field(default_factory=dict)


def classify(name: str, catalog: dict, before: dict, after: dict) -> tuple[str, str, str, str]:
    entry = (catalog.get('tools') or {}).get(name, {})
    transport = before.get('transport') or after.get('transport') or entry.get('transport') or 'unknown'
    server = before.get('server') or after.get('server') or entry.get('server') or 'unknown'
    ownership = entry.get('ownership') or before.get('ownership') or 'unknown'
    kind = entry.get('kind') or ('write' if WRITE_HINTS.search(name) and not READ_HINTS.match(name)
                                 else 'read' if READ_HINTS.match(name) else 'unknown')
    return transport, server, ownership, kind


def build_trace(session_id: str, events: list, *, catalog: dict | None = None, status: str = 'unknown',
                metadata: dict | None = None) -> SessionTrace:
    catalog = catalog or {}
    events = sorted(events, key=lambda e: (e.get('seq', 0)))
    trace = SessionTrace(id=session_id, status=status, events=events, metadata=metadata or {})
    open_calls: dict[tuple, list] = {}
    llm_done = 0
    last_snapshot = None
    snapshots_by_call: dict = {}
    for e in events:
        if e.get('kind') == 'context.snapshot':
            p = e.get('payload') or {}
            if p.get('call_id'):
                snapshots_by_call[p['call_id']] = e
    for e in events:
        kind, name, p = e.get('kind', ''), e.get('name', ''), e.get('payload') or {}
        family = kind.split('.')[0]
        cid = e.get('call_id') or p.get('call_id')
        if kind == 'context.snapshot':
            last_snapshot = e
            trace.snapshots.append(e)
        elif kind in ('tool.before', 'llm.before'):
            open_calls.setdefault((family, name), []).append((cid, e, llm_done, last_snapshot))
        elif family in ('tool', 'llm') and kind.split('.')[-1] in ('after', 'error', 'failed'):
            stack = open_calls.get((family, name), [])
            match = next((i for i, item in enumerate(stack) if cid and item[0] == cid), None)
            if match is None and stack:
                match = 0
            if match is None:
                continue
            _, before, turn, snap = stack.pop(match)
            bp = before.get('payload') or {}
            dur = _num(p.get('duration_ms'))
            if dur is None and isinstance(e.get('ts'), (int, float)) and isinstance(before.get('ts'), (int, float)):
                dur = max(0.0, (e['ts'] - before['ts']) * 1000)
            if family == 'tool':
                result = p.get('result', p.get('output', p.get('content')))
                transport, server, ownership, tkind = classify(name, catalog, bp, p)
                reported = _num(p.get('result_tokens'))
                trace.tool_calls.append(ToolCall(
                    session_id=session_id, name=name, seq=before.get('seq', 0), end_seq=e.get('seq'),
                    arguments=bp.get('arguments', {}), result=result, ok=not is_failure(kind, p),
                    error_class=error_class(p) if is_failure(kind, p) else None,
                    duration_ms=dur, upstream_ms=_num(p.get('upstream_ms')),
                    result_tokens=int(reported) if reported is not None else estimate_tokens(result),
                    result_tokens_reported=reported is not None, used_ratio=_num(p.get('used_ratio')),
                    transport=transport, server=server, ownership=ownership, kind=tkind, llm_turn=turn))
            else:
                usage = read_usage(p)
                snapshot = snapshots_by_call.get(cid) or snap
                trace.llm_calls.append(LLMCall(
                    session_id=session_id, name=name, seq=before.get('seq', 0), duration_ms=dur,
                    input_tokens=usage['input'], output_tokens=usage['output'], cached_tokens=usage['cached'],
                    output=p.get('output', p.get('response', p.get('decision', p.get('text')))),
                    snapshot=snapshot))
                llm_done += 1
    stamps = [e['ts'] for e in events if isinstance(e.get('ts'), (int, float))]
    if len(stamps) >= 2:
        trace.wall_ms = (max(stamps) - min(stamps)) * 1000
    return trace


def load_sessions(obj: Any) -> list[dict]:
    """Accept a session dict ({id, events}), a list of sessions, a bare event list, or {sessions: [...]}."""
    if isinstance(obj, dict) and 'sessions' in obj:
        obj = obj['sessions']
    if isinstance(obj, dict) and 'events' in obj:
        return [obj]
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and 'kind' in obj[0]:
        return [{'id': 'session', 'events': obj}]
    if isinstance(obj, list):
        return [s for s in obj if isinstance(s, dict) and 'events' in s]
    raise ValueError('Unrecognised session format; expected {id, events} or a list of them')
