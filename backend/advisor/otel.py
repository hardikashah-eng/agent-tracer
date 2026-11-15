"""Convert OTLP/JSON spans (GenAI + MCP semantic conventions) into tracer-style events.

Only attributes needed by the advisor are read. The conventions are still in
Development status upstream, so several aliases are accepted.
"""
from __future__ import annotations

import json
from typing import Any


def _attr_value(v: dict) -> Any:
    for key in ('stringValue', 'intValue', 'doubleValue', 'boolValue'):
        if key in v:
            val = v[key]
            return int(val) if key == 'intValue' else val
    if 'arrayValue' in v:
        return [_attr_value(x) for x in v['arrayValue'].get('values', [])]
    return None


def _attrs(span: dict) -> dict:
    raw = span.get('attributes', {})
    if isinstance(raw, dict):
        return raw
    return {a['key']: _attr_value(a.get('value', {})) for a in raw if 'key' in a}


def _maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def iter_spans(export: dict):
    for rs in export.get('resourceSpans', []):
        for ss in rs.get('scopeSpans', rs.get('instrumentationLibrarySpans', [])):
            yield from ss.get('spans', [])


def spans_to_sessions(export: dict) -> list[dict]:
    """Group spans by trace (or gen_ai.conversation.id) and emit before/after events per span."""
    sessions: dict[str, list] = {}
    for span in iter_spans(export):
        a = _attrs(span)
        sid = str(a.get('gen_ai.conversation.id') or a.get('session.id') or span.get('traceId', 'trace'))
        start = int(span.get('startTimeUnixNano', 0)) / 1e9
        end = int(span.get('endTimeUnixNano', 0)) / 1e9 or start
        op = a.get('gen_ai.operation.name')
        name = span.get('name', '')
        tool = a.get('gen_ai.tool.name') or a.get('mcp.tool.name')
        if not tool and name.startswith('tools/call '):
            tool = name.split(' ', 1)[1]
        failed = (span.get('status', {}).get('code') in (2, 'STATUS_CODE_ERROR')) or bool(a.get('error.type'))
        if tool and (op in (None, 'execute_tool') or a.get('mcp.method.name') == 'tools/call'):
            server = a.get('mcp.server.name') or a.get('server.address') or 'unknown'
            before = {'arguments': _maybe_json(a.get('gen_ai.tool.call.arguments', {})) or {},
                      'transport': 'mcp' if a.get('mcp.method.name') or name.startswith('tools/call') else a.get('tool.transport', 'unknown'),
                      'server': server}
            after = {'result': _maybe_json(a.get('gen_ai.tool.call.result')),
                     'duration_ms': (end - start) * 1000}
            if a.get('tool.upstream_ms') is not None:
                after['upstream_ms'] = a['tool.upstream_ms']
            if failed:
                after['error'] = a.get('error.type') or span.get('status', {}).get('message') or 'error'
                after['error_type'] = a.get('error.type')
            events = sessions.setdefault(sid, [])
            cid = span.get('spanId')
            events.append({'ts': start, 'kind': 'tool.before', 'name': tool, 'call_id': cid, 'payload': before})
            events.append({'ts': end, 'kind': 'tool.error' if failed else 'tool.after', 'name': tool, 'call_id': cid, 'payload': after})
        elif op in ('chat', 'text_completion', 'generate_content') or a.get('gen_ai.usage.input_tokens') is not None:
            model = a.get('gen_ai.request.model') or a.get('gen_ai.response.model') or name or 'model'
            usage = {'input_tokens': a.get('gen_ai.usage.input_tokens'),
                     'output_tokens': a.get('gen_ai.usage.output_tokens'),
                     'cache_read_input_tokens': a.get('gen_ai.usage.cache_read.input_tokens',
                                                      a.get('gen_ai.usage.cache_read_input_tokens'))}
            events = sessions.setdefault(sid, [])
            cid = span.get('spanId')
            events.append({'ts': start, 'kind': 'llm.before', 'name': model, 'call_id': cid, 'payload': {}})
            events.append({'ts': end, 'kind': 'llm.after', 'name': model, 'call_id': cid,
                           'payload': {'usage': usage, 'duration_ms': (end - start) * 1000,
                                       'output': _maybe_json(a.get('gen_ai.output.messages'))}})
    result = []
    for sid, events in sessions.items():
        events.sort(key=lambda e: (e['ts'], 0 if e['kind'].endswith('before') else 1))
        for i, e in enumerate(events, 1):
            e['seq'] = i
        result.append({'id': sid, 'events': events, 'metadata': {'source': 'otel'}})
    return result
