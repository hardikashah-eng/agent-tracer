"""Paired observed outcomes from a caller-run controlled experiment."""
import math


def summarize(pairs):
    if not 2 <= len(pairs) <= 1000:
        raise ValueError('Supply 2–1000 independently executed paired trials')
    if any(type(p.get('baseline')) is not bool or type(p.get('variant')) is not bool for p in pairs):
        raise ValueError('Each trial requires boolean baseline and variant outcomes')
    ids = [p.get('trial_id') for p in pairs]
    if any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError('Trial IDs must be nonempty and unique')
    delta = [int(p['variant'])-int(p['baseline']) for p in pairs]
    n = len(delta); mean = sum(delta)/n
    # Conservative Hoeffding interval for paired differences bounded in [-1,1].
    margin = math.sqrt(2*math.log(40)/n)
    return dict(trials=n, baseline_success=sum(p['baseline'] for p in pairs)/n,
                variant_success=sum(p['variant'] for p in pairs)/n, paired_delta=mean,
                delta_interval_95=[max(-1, mean-margin), min(1, mean+margin)],
                method='Hoeffding bound for independent paired differences',
                warning='Caller-supplied observed outcomes. Independence, matching, and attribution are not verified; association is not causal proof.')
