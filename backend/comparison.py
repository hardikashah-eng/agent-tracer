"""Align by explicit step identity, falling back to kind/name occurrence."""
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import zip_longest

IGNORED = {'request_id', 'call_id', 'parent_call_id', 'timestamp', 'ts'}


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items() if k not in IGNORED}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def compare(left, right):
    def indexed(events):
        counts = defaultdict(int)
        result = {}
        for e in events:
            if e['kind'].startswith('debug.'):
                continue
            identity = (e['kind'], e['payload'].get('step_id') or e['name'])
            counts[identity] += 1
            result[(*identity, counts[identity])] = e
        return result
    a, b = indexed(left.events), indexed(right.events)
    differences = []
    keys_a, keys_b = list(a), list(b)
    aligned = []
    for tag, i, j, k, l in SequenceMatcher(a=keys_a, b=keys_b, autojunk=False).get_opcodes():
        aligned.extend(zip_longest(keys_a[i:j], keys_b[k:l]))
    for key_a, key_b in aligned:
        x, y = a.get(key_a), b.get(key_b)
        if key_a != key_b or x is None or y is None or normalize(x['payload']) != normalize(y['payload']):
            differences.append(dict(step=list(key_a or key_b), baseline_seq=x['seq'] if x else None,
                                    candidate_seq=y['seq'] if y else None,
                                    type='changed' if x and y else 'missing' if x else 'added',
                                    baseline=x, candidate=y))
    return dict(baseline_id=left.id, candidate_id=right.id, differences=differences,
                first_divergence=differences[0] if differences else None,
                metadata_differences={k: [left.metadata.get(k), right.metadata.get(k)] for k in left.metadata.keys() | right.metadata.keys() if left.metadata.get(k) != right.metadata.get(k)},
                alignment='step_id or kind/name occurrence; not semantic equivalence',
                ignored_fields=sorted(IGNORED), warning='First observed divergence is not proof of root cause.')
