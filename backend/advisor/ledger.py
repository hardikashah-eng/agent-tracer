"""Savings ledger: record applied recommendations and measure them against later sessions.

Each entry snapshots a baseline (sessions in scope that started before `applied_at`). Evaluation compares
sessions that started after `applied_at`: did the recommendation's measured waste fall, did the finding
stop recurring, and did input tokens, failures and success move the right way? This is an observational
before/after comparison; confirm important changes with a verification plan.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from threading import RLock

from .metrics import PRIMARY_METRIC, in_scope, mean

MIN_AFTER_SESSIONS = 3


def _window(rows, scope, start=None, end=None):
    out = []
    for r in rows:
        ts = r.get('started_at')
        if ts is None or not in_scope(r, scope):
            continue
        # Controlled-trial sessions are evaluated by verification plans, not by the production ledger.
        if (r.get('metadata') or {}).get('experiment_id') and 'experiment_id' not in (scope or {}):
            continue
        if start is not None and ts < start:
            continue
        if end is not None and ts >= end:
            continue
        out.append(r)
    return out


def _summarize(rows, rid, metric):
    succ = [r['success'] for r in rows if r['success'] is not None]
    return {'sessions': len(rows),
            'waste_tokens_mean': round(mean([r['waste'].get(rid, 0) for r in rows]) or 0, 1),
            'finding_share': round(sum(1 for r in rows if r['occurrences'].get(rid)) / len(rows), 3) if rows else None,
            'input_tokens_mean': round(mean([r['input_tokens'] for r in rows]) or 0, 1),
            'model_turns_mean': round(mean([r['model_turns'] for r in rows]) or 0, 2),
            'tool_failures_mean': round(mean([r['tool_failures'] for r in rows]) or 0, 2),
            'wall_ms_mean': round(mean([r['wall_ms'] for r in rows]) or 0, 1),
            'primary_metric': metric,
            'primary_mean': round(mean([r.get(metric) for r in rows]) or 0, 1),
            'success_rate': round(sum(succ) / len(succ), 3) if succ else None}


def make_entry(report: dict, recommendation_id: str, *, applied_at: float | None = None, scope: dict | None = None,
               note: str = '', plan_id: str | None = None) -> dict:
    rec = next((r for r in report['recommendations'] if r['id'] == recommendation_id), None)
    if rec is None:
        raise KeyError(f'Recommendation {recommendation_id} is not in the current report')
    applied_at = time.time() if applied_at is None else float(applied_at)
    metric = PRIMARY_METRIC.get(rec['detector'], 'input_tokens')
    before = _window(report.get('session_metrics', []), scope, end=applied_at)
    return {'id': uuid.uuid4().hex[:12], 'recommendation_id': rec['id'], 'title': rec['title'], 'detector': rec['detector'],
            'tool': rec['tool'], 'ownership': rec['ownership'], 'applied_at': applied_at, 'recorded_at': time.time(),
            'note': note, 'scope': scope or {}, 'plan_id': plan_id,
            'expected_savings_per_session': rec['estimated_savings_per_session'],
            'baseline': _summarize(before, rec['id'], metric)}


def evaluate_entry(entry: dict, report: dict) -> dict:
    rid, metric = entry['recommendation_id'], entry['baseline']['primary_metric']
    after_rows = _window(report.get('session_metrics', []), entry.get('scope'), start=entry['applied_at'])
    after = _summarize(after_rows, rid, metric)
    base = entry['baseline']
    out = {**entry, 'after': after}
    if after['sessions'] < MIN_AFTER_SESSIONS or not base['sessions']:
        need = MIN_AFTER_SESSIONS - after['sessions']
        return {**out, 'status': 'awaiting_data',
                'explanation': (f'Need {need} more session(s) after the change.' if need > 0 else
                                'No baseline sessions before the change; re-record with an earlier applied_at.'),
                'realized': None}
    waste_drop = base['waste_tokens_mean'] - after['waste_tokens_mean']
    input_delta = after['input_tokens_mean'] - base['input_tokens_mean']
    primary_delta = after['primary_mean'] - base['primary_mean']
    expected = entry['expected_savings_per_session']['tokens'] or base['waste_tokens_mean']
    success_drop = (base['success_rate'] - after['success_rate']) if base['success_rate'] is not None and after['success_rate'] is not None else 0
    realized = {'waste_tokens_saved_per_session': round(waste_drop, 1),
                'input_tokens_change_per_session': round(input_delta, 1),
                'primary_metric_change': round(primary_delta, 2),
                'finding_share_change': round((after['finding_share'] or 0) - (base['finding_share'] or 0), 3),
                'success_rate_change': round(-success_drop, 3) if success_drop else 0.0,
                'cumulative_tokens_saved': round(max(0.0, waste_drop) * after['sessions']),
                'share_of_expected': round(waste_drop / expected, 3) if expected else None}
    worse = base['primary_mean'] and primary_delta > 0.1 * abs(base['primary_mean'])
    if success_drop > 0.1 or worse:
        status, why = 'regressed', 'Success fell or the primary metric rose by more than 10% after the change.'
    elif (after['finding_share'] or 0) <= 0.5 * (base['finding_share'] or 0) and waste_drop >= 0.5 * (base['waste_tokens_mean'] or expected or 1):
        status, why = 'realized', 'The finding largely stopped recurring and its measured waste fell by at least half.'
    elif waste_drop > 0 or primary_delta < 0:
        status, why = 'partial', 'Some improvement, but the finding still recurs or waste fell by less than half.'
    else:
        status, why = 'no_effect', 'No measurable change in the finding or the primary metric.'
    return {**out, 'status': status, 'explanation': why, 'realized': realized}


def evaluate_ledger(entries: list[dict], report: dict) -> dict:
    evaluated = [evaluate_entry(e, report) for e in entries]
    total = sum((e['realized'] or {}).get('cumulative_tokens_saved', 0) for e in evaluated if e['status'] in ('realized', 'partial'))
    per_session = sum((e['realized'] or {}).get('waste_tokens_saved_per_session', 0) for e in evaluated if e['status'] in ('realized', 'partial'))
    counts = {}
    for e in evaluated:
        counts[e['status']] = counts.get(e['status'], 0) + 1
    return {'entries': sorted(evaluated, key=lambda e: -e['applied_at']), 'status_counts': counts,
            'cumulative_tokens_saved': total, 'tokens_saved_per_session': round(per_session, 1),
            'warning': 'Before/after comparison over observed sessions; workload mix, model or prompt changes can confound it. '
                       'Use verification plans for causal confirmation.'}


class SQLiteLedger:
    """Ledger rows live next to the session store (same SQLite file, separate table)."""

    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS advisor_ledger (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.commit()

    def list(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM advisor_ledger ORDER BY rowid')]

    def add(self, entry):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO advisor_ledger VALUES (?, ?)', (entry['id'], json.dumps(entry)))
        return entry

    def delete(self, entry_id):
        with self.lock, self.db:
            return self.db.execute('DELETE FROM advisor_ledger WHERE id = ?', (entry_id,)).rowcount > 0


class JSONLedger:
    """File-backed ledger for CLI use."""

    def __init__(self, path):
        self.path = Path(path)

    def list(self):
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []

    def add(self, entry):
        entries = [e for e in self.list() if e['id'] != entry['id']] + [entry]
        self.path.write_text(json.dumps(entries, indent=2), encoding='utf-8')
        return entry

    def delete(self, entry_id):
        entries = self.list()
        kept = [e for e in entries if e['id'] != entry_id]
        self.path.write_text(json.dumps(kept, indent=2), encoding='utf-8')
        return len(kept) != len(entries)
