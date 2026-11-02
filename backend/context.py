"""Context snapshots: capture the actual outbound model messages separately from state."""
import copy
import json
import hashlib

def snapshot_context(tracer, session, label, messages, state=None, provenance=None, *, call_id=None, goal=None):
    """Call immediately before sending messages to a model; counts are estimates, not tokenizer output."""
    parts = []
    for i, msg in enumerate(messages):
        content = msg.get('content', '')
        serialized = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        role = msg.get('role', 'unknown')
        category = msg.get('category') or ('tool' if role == 'tool' else 'system' if role in ('system', 'developer') else 'conversation')
        parts.append({'id': msg.get('id', f'{role}-{i}'), 'role': role, 'category': category,
                      'content': serialized, 'chars': len(serialized), 'estimated_tokens': max(1, (len(serialized)+3)//4),
                      'source': msg.get('source', provenance),
                      'source_id': msg.get('source_id', msg.get('source', provenance)),
                      'source_type': msg.get('source_type', category),
                      'source_event_seq': msg.get('source_event_seq'),
                      'goal_ids': copy.deepcopy(msg.get('goal_ids', [])),
                      'content_hash': hashlib.sha256(serialized.encode('utf-8')).hexdigest()})
    return tracer.emit(session, 'context.snapshot', label, {
        'call_id': call_id, 'goal': copy.deepcopy(goal),
        'messages': parts, 'state': copy.deepcopy(state or {}),
        'estimated_tokens': sum(p['estimated_tokens'] for p in parts),
        'note': 'Token counts are approximate; actual provider tokenization may differ.'})
