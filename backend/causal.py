"""Context provenance and controlled sample-agent counterfactuals.

Experimental replay uses a deterministic surrogate decision function, NOT an LLM.
No claim about internal model reasoning is implied.
"""
import hashlib
import json
import random
from dataclasses import dataclass


def fingerprint(message):
    return hashlib.sha256(str(message.get('content', '')).encode('utf-8')).hexdigest()[:16]


def provenance(events):
    snapshots = [e for e in events if e['kind'] == 'context.snapshot']
    items = {}
    for idx, ev in enumerate(snapshots):
        for msg in ev['payload'].get('messages', []):
            mid = str(msg['id']); digest = fingerprint(msg)
            key = f'{mid}:{digest}'
            entry = items.setdefault(key, {'key': key, 'id': mid, 'hash': digest, 'source': msg.get('source'), 'role': msg.get('role'), 'category': msg.get('category'), 'estimated_tokens': msg.get('estimated_tokens', 0), 'first_seen': ev['seq'], 'seen_in': [], 'last_seen': ev['seq']})
            entry['seen_in'].append({'seq': ev['seq'], 'model': ev['name']})
            entry['last_seen'] = ev['seq']
    nodes = list(items.values())
    edges = [{'source': n['key'], 'target': f"model:{seen['seq']}", 'relation': 'observed_in_prompt'} for n in nodes for seen in n['seen_in']]
    models = [{'id': f"model:{e['seq']}", 'seq': e['seq'], 'name': e['name']} for e in snapshots]
    return {'items': nodes, 'models': models, 'edges': edges, 'disclaimer': 'Edges show observed prompt inclusion, not causal influence.'}


def sample_decision(messages, rng):
    """Explicit toy policy for demonstrating ablation mechanics, not an LLM."""
    text = ' '.join(str(m.get('content', '')).lower() for m in messages)
    signal = ('timeout' in text and 'traceback' in text)
    chance = .90 if signal else .35
    return {'action': 'fix_timeout' if signal else 'investigate', 'success': rng.random() < chance}


def experiment(snapshot, remove_ids, trials=30, seed=7):
    if not 1 <= trials <= 500: raise ValueError('trials must be between 1 and 500')
    messages = snapshot['payload']['messages']
    ids = {str(m['id']) for m in messages}
    missing = set(remove_ids) - ids
    if missing: raise ValueError(f'Unknown message IDs: {sorted(missing)}')
    if any(m['role'] in ('system', 'developer') for m in messages if m['id'] in remove_ids):
        raise ValueError('Removing system/developer instructions is disabled in this demo')
    original = []; altered = []
    for i in range(trials):
        # Paired seeds remove one source of variance between conditions.
        original.append(sample_decision(messages, random.Random(seed + i)))
        altered.append(sample_decision([m for m in messages if m['id'] not in remove_ids], random.Random(seed + i)))
    base = sum(x['success'] for x in original) / trials
    variant = sum(x['success'] for x in altered) / trials
    return {'snapshot_seq': snapshot['seq'], 'removed_ids': remove_ids, 'trials': trials, 'seed': seed, 'baseline_success': base, 'variant_success': variant, 'delta': base - variant, 'baseline_actions': {a:sum(x['action']==a for x in original) for a in ('fix_timeout','investigate')}, 'variant_actions': {a:sum(x['action']==a for x in altered) for a in ('fix_timeout','investigate')}, 'mode': 'deterministic_surrogate', 'warning': 'Demonstration only: deterministic surrogate, not a real agent/model replay. Success is synthetic.'}
