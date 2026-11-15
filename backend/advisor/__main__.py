"""CLI: python -m backend.advisor [files...] [--db path] [--otel] [--catalog tools.json] [--format html|md|json] [-o out]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import AdvisorConfig, analyze_sessions, load_sessions
from .otel import spans_to_sessions
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
    report = analyze_sessions(sessions, catalog, config)
    out = {'html': to_html, 'md': to_markdown}.get(args.format, lambda r: json.dumps(r, indent=2, default=str))(report)
    if args.output:
        Path(args.output).write_text(out, encoding='utf-8')
        print(f"Wrote {args.format} report for {len(sessions)} sessions with {len(report['recommendations'])} recommendations to {args.output}")
    else:
        sys.stdout.write(out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
