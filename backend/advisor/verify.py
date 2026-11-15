"""Paired-trial plans to verify top recommendations before rolling them out, and their evaluation.

A plan replays the same tasks with the current harness (baseline arm) and with the recommended change
(variant arm). Sessions are tagged with metadata so evaluation can pair them by task:
experiment_id, arm (baseline|variant), task_id, recommendation_id.
"""
from __future__ import annotations

import math

from .metrics import PRIMARY_METRIC, SAVINGS_KEY, mean, metric_value, stdev
from .model import content_hash

Z_ALPHA, Z_POWER = 1.96, 0.84  # two-sided alpha 0.05, power 0.8
T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23, 12: 2.18,
       15: 2.13, 20: 2.09, 25: 2.06, 30: 2.04}
SUCCESS_MARGIN = 0.05


def _t95(df: int) -> float:
    if df <= 0:
        return float('inf')
    for k in sorted(T95):
        if df <= k:
            return T95[k]
    return 1.96


def _primary_lever(rec):
    for owner in ('harness_owner', 'gateway_operator', 'server_owner'):
        for lever in rec['levers']:
            if lever['owner'] == owner:
                return lever
    return rec['levers'][0] if rec['levers'] else {'owner': 'harness_owner', 'action': rec['title']}


def verification_plans(report: dict, top: int = 3) -> list[dict]:
    rows = report.get('session_metrics', [])
    plans = []
    for rec in report['recommendations'][:top]:
        metric = PRIMARY_METRIC.get(rec['detector'], 'input_tokens')
        sample = [r for r in rows if r['occurrences'].get(rec['id'])] or rows
        values = [metric_value(r, metric) for r in sample]
        base_mean, base_sd = mean(values), stdev(values)
        delta = rec['estimated_savings_per_session'][SAVINGS_KEY[metric]]
        if metric == 'tool_failures':
            occ = [r['occurrences'].get(rec['id'], 0) for r in sample]
            delta = max(delta, (mean(occ) or 0) * 0.5)
        if delta and base_sd:
            pairs = math.ceil(((Z_ALPHA + Z_POWER) * base_sd / delta) ** 2)
            pairs = min(200, max(5, pairs))
            basis = 'paired normal approximation using between-session SD as a conservative proxy for paired-difference SD'
        else:
            pairs = 10
            basis = 'default: too few baseline sessions or no measurable expected delta to size the trial'
        tasks = sorted({r['task_key'] for r in sample})[:max(pairs, 5)]
        lever = _primary_lever(rec)
        exp_id = f"exp-{rec['detector']}-{content_hash(rec['id'])[:8]}"
        plans.append({
            'id': exp_id, 'experiment_id': exp_id, 'recommendation_id': rec['id'], 'title': f"Verify: {rec['title']}",
            'hypothesis': (f"Applying '{lever['action']}' lowers mean {metric} per task by about {round(delta):,} "
                           f"(baseline {round(base_mean or 0):,}) without reducing task success by more than {SUCCESS_MARGIN:.0%}."),
            'control': 'Current harness, tools and prompts unchanged.',
            'intervention': {'lever_owner': lever['owner'], 'change': lever['action'],
                             'alternatives': [l['action'] for l in rec['levers'] if l is not lever][:3]},
            'primary_metric': {'name': metric, 'direction': 'decrease', 'expected_delta_per_task': round(delta, 1),
                               'baseline_mean': round(base_mean, 1) if base_mean is not None else None,
                               'baseline_sd': round(base_sd, 1) if base_sd is not None else None,
                               'baseline_sessions': len(sample)},
            'guardrails': [{'metric': 'success', 'rule': f'variant success rate ≥ baseline − {SUCCESS_MARGIN:.0%}'},
                           {'metric': 'wall_ms' if metric != 'wall_ms' else 'input_tokens', 'rule': 'no increase > 10%'}],
            'design': {'type': 'paired by task, same model and inputs, randomised arm order', 'pairs_required': pairs,
                       'alpha': 0.05, 'power': 0.8, 'sizing_basis': basis, 'tasks': tasks},
            'tagging': {'experiment_id': exp_id, 'arm': 'baseline | variant', 'task_id': '<one of design.tasks>',
                        'recommendation_id': rec['id']},
            'steps': [
                f'Pick {pairs} tasks (start with design.tasks, which showed the problem).',
                'Run each task twice in random order: once unchanged (arm=baseline) and once with the change (arm=variant).',
                'Keep model, temperature, prompts and data identical apart from the change; record usage tokens.',
                'Tag both sessions with the tagging metadata so they can be paired.',
                'Evaluate with POST /api/advisor/verification/evaluate (or python -m backend.advisor --evaluate-plan).',
                'Adopt only if the verdict is confirmed, then record it in the savings ledger.'],
            'warning': 'A plan, not an executed experiment. Sizing is approximate; matched pairs remove most task variance.',
        })
        if any(str((r.get('metadata') or {}).get('experiment_id')) == exp_id for r in rows):
            plans[-1]['result'] = evaluate_plan(plans[-1], rows)
    return plans


def evaluate_plan(plan: dict, session_rows: list[dict]) -> dict:
    exp = plan['experiment_id']
    metric = plan['primary_metric']['name']
    arms = {}
    for r in session_rows:
        meta = r.get('metadata') or {}
        if str(meta.get('experiment_id')) != exp or meta.get('arm') not in ('baseline', 'variant'):
            continue
        task = str(meta.get('task_id') or r['task_key'])
        arms.setdefault(task, {}).setdefault(meta['arm'], []).append(r)
    diffs, succ_pairs = [], []
    for task, by_arm in sorted(arms.items()):
        if 'baseline' not in by_arm or 'variant' not in by_arm:
            continue
        b = mean([metric_value(x, metric) for x in by_arm['baseline']])
        v = mean([metric_value(x, metric) for x in by_arm['variant']])
        if b is not None and v is not None:
            diffs.append({'task_id': task, 'baseline': b, 'variant': v, 'delta': v - b})
        bs = [x['success'] for x in by_arm['baseline'] if x['success'] is not None]
        vs = [x['success'] for x in by_arm['variant'] if x['success'] is not None]
        if bs and vs:
            succ_pairs.append((sum(bs) / len(bs), sum(vs) / len(vs)))
    n = len(diffs)
    result = {'experiment_id': exp, 'recommendation_id': plan.get('recommendation_id'), 'metric': metric,
              'pairs_observed': n, 'pairs_required': plan['design']['pairs_required'], 'pairs': diffs[:50],
              'expected_delta_per_task': -abs(plan['primary_metric']['expected_delta_per_task'])}
    if n < 2:
        return {**result, 'verdict': 'insufficient_data',
                'explanation': 'Need at least two tasks with both a baseline and a variant session tagged for this experiment.'}
    ds = [d['delta'] for d in diffs]
    m, sd = mean(ds), stdev(ds) or 0.0
    half = _t95(n - 1) * sd / math.sqrt(n)
    base_mean = mean([d['baseline'] for d in diffs])
    success = None
    if succ_pairs:
        bsr = mean([p[0] for p in succ_pairs])
        vsr = mean([p[1] for p in succ_pairs])
        success = {'baseline_rate': round(bsr, 3), 'variant_rate': round(vsr, 3), 'delta': round(vsr - bsr, 3),
                   'non_inferior': vsr >= bsr - SUCCESS_MARGIN}
    lo, hi = m - half, m + half
    success_ok = success is None or success['non_inferior']
    if hi < 0 and success_ok:
        verdict, why = 'confirmed', 'The 95% interval of the paired change is entirely below zero and success did not drop.'
    elif lo > 0 or not success_ok:
        verdict, why = 'regressed', ('The metric got worse.' if lo > 0 else 'Task success dropped beyond the non-inferiority margin.')
    else:
        verdict, why = 'inconclusive', 'The interval includes zero; run more pairs or accept that the effect is small.'
    if verdict == 'confirmed' and n < plan['design']['pairs_required']:
        why += f' Fewer pairs than planned ({n}/{plan["design"]["pairs_required"]}); confirm on a larger sample before broad rollout.'
    return {**result, 'verdict': verdict, 'explanation': why,
            'mean_delta': round(m, 1), 'ci95': [round(lo, 1), round(hi, 1)],
            'relative_change': round(m / base_mean, 3) if base_mean else None, 'success': success,
            'method': 'Paired differences by task with a t-interval; success non-inferiority by point estimate.',
            'warning': 'Assumes tasks are independent and arms differ only by the change. Association is not proof of mechanism.'}
