"""CLI: python -m backend.advisor [files...] [--db path] [--otel] [--catalog tools.json] [--format html|md|json] [-o out]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import AdvisorConfig, JudgeOptions, analyze_sessions, load_sessions
from .judge import judge_from_env
from .ledger import JSONLedger, make_entry
from .otel import spans_to_sessions
from .verify import evaluate_plan
from .report import to_html, to_markdown


def _read_sessions(args) -> list[dict]:
    sessions = []
    for path in args.inputs:
        obj = json.loads(Path(path).read_text(encoding='utf-8'))
        if args.otel or (isinstance(obj, dict) and 'resourceSpans' in obj):
            sessions.extend(spans_to_sessions(obj))
        else:
            for s in load_sessions(obj):
                s.setdefault('id', Path(path).stem)
                sessions.append(s)
    if args.db:
        from ..storage import SQLiteStore
        store = SQLiteStore(args.db)
        try:
            sessions.extend(b for b in store.load() if not (b.get('metadata') or {}).get('analysis'))
        finally:
            store.close()
    return sessions


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m backend.advisor', description='Cross-session harness advisor.')
    parser.add_argument('inputs', nargs='*', help='Session JSON files ({id, events} or lists) or OTLP/JSON exports')
    parser.add_argument('--db', help='Agent Tracer SQLite database to include')
    parser.add_argument('--otel', action='store_true', help='Treat inputs as OTLP/JSON span exports')
    parser.add_argument('--catalog', help='Tool catalog JSON (ownership, transport, kind, definition_tokens)')
    parser.add_argument('--config', help='JSON overrides for AdvisorConfig thresholds')
    parser.add_argument('--format', choices=('html', 'md', 'json'), default='md')
    parser.add_argument('--judge', action='store_true', help='Use the LLM judge configured via AGENT_TRACER_JUDGE* variables')
    parser.add_argument('--judge-max', type=int, default=25, help='Max tool calls sent to the judge for tool-selection review')
    parser.add_argument('--plans', type=int, default=3, help='Verification plans for the top N recommendations (0 disables)')
    parser.add_argument('--evaluate-plan', metavar='PLAN_JSON', help='Evaluate a verification plan against tagged sessions')
    parser.add_argument('--ledger', metavar='LEDGER_JSON', help='Savings ledger file to evaluate (and update with --apply)')
    parser.add_argument('--apply', metavar='RECOMMENDATION_ID', help='Record a recommendation as applied in --ledger')
    parser.add_argument('--applied-at', type=float, help='Unix time the change took effect (default: now)')
    parser.add_argument('--scope', action='append', default=[], metavar='KEY=VALUE', help='Ledger scope: session metadata filter')
    parser.add_argument('--note', default='', help='Ledger note')
    parser.add_argument('-o', '--output', help='Write to file instead of stdout')
    args = parser.parse_args(argv)
    if not args.inputs and not args.db:
        parser.error('provide session files and/or --db')
    sessions = _read_sessions(args)
    if not sessions:
        print('No sessions found.', file=sys.stderr)
        return 1
    catalog = json.loads(Path(args.catalog).read_text(encoding='utf-8')) if args.catalog else {}
    config = AdvisorConfig(**json.loads(Path(args.config).read_text(encoding='utf-8'))) if args.config else AdvisorConfig()
    judge = None
    if args.judge:
        judge = judge_from_env()
        if judge is None:
            parser.error('--judge requires AGENT_TRACER_JUDGE (openai|anthropic) and related variables')
    ledger = JSONLedger(args.ledger) if args.ledger else None
    if args.apply and not ledger:
        parser.error('--apply requires --ledger')
    report = analyze_sessions(sessions, catalog, config, judge=judge, judge_options=JudgeOptions(max_tool_selection=args.judge_max),
                              plans=args.plans, ledger=ledger.list() if ledger else None)
    if args.evaluate_plan:
        plan = json.loads(Path(args.evaluate_plan).read_text(encoding='utf-8'))
        sys.stdout.write(json.dumps(evaluate_plan(plan, report['session_metrics']), indent=2) + '\n')
        return 0
    if args.apply:
        scope = dict(item.split('=', 1) for item in args.scope)
        try:
            entry = ledger.add(make_entry(report, args.apply, applied_at=args.applied_at, scope=scope, note=args.note))
        except KeyError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Recorded {entry['recommendation_id']} as applied (ledger id {entry['id']}); "
              f"baseline from {entry['baseline']['sessions']} sessions.")
        return 0
    out = {'html': to_html, 'md': to_markdown}.get(args.format, lambda r: json.dumps(r, indent=2, default=str))(report)
    if args.output:
        Path(args.output).write_text(out, encoding='utf-8')
        print(f"Wrote {args.format} report for {len(sessions)} sessions with {len(report['recommendations'])} recommendations to {args.output}")
    else:
        sys.stdout.write(out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
