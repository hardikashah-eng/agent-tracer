"""Render advisor results as Markdown or a self-contained HTML page (no external assets)."""
from __future__ import annotations

import html
import json

OWNER_LABEL = {'harness_owner': 'Harness', 'server_owner': 'Server owner', 'gateway_operator': 'Gateway',
               'vendor_request': 'Vendor request'}
CAT_LABEL = {'system': 'System prompt', 'tool_definitions': 'Tool definitions', 'conversation': 'Conversation',
             'tool_results': 'Tool results', 'other': 'Other / unclassified'}
LAT_LABEL = {'llm_ms': 'Model', 'tool_upstream_ms': 'Tool: upstream API', 'tool_overhead_ms': 'Tool: protocol/gateway',
             'tool_unattributed_ms': 'Tool: unsplit', 'harness_ms': 'Harness / idle'}
COLORS = ['#534AB7', '#1D9E75', '#D85A30', '#BA7517', '#888780', '#378ADD', '#D4537E']


def _pct(v):
    return '–' if v is None else f'{v:.0%}'


def _savings(sv) -> str:
    parts = [f"~{_fmt(sv['tokens'])} tokens" if sv['tokens'] else '', f"~{_fmt(sv['ms'])} ms" if sv['ms'] else '',
             f"~{sv['turns']} model turns" if sv['turns'] else '']
    return ' · '.join(p for p in parts if p) or 'no direct saving estimated (efficacy)'


def _plural(n, word):
    return f'{n} {word}' + ('' if n == 1 else 's')


def _fmt(n):
    if n is None:
        return '–'
    if isinstance(n, float) and 0 < n < 10 and n != int(n):
        return f'{n:.2f}'
    return f'{int(round(n)):,}'


def to_markdown(report: dict) -> str:
    s = report['summary']
    lines = ['# Harness Advisor report', '',
             f"Sessions **{s['sessions']}** · model turns **{s['llm_turns']}** · tool calls **{s['tool_calls']}** · "
             f"input tokens **{_fmt(s['total_input_tokens'])}** · cache-read ratio **{s['cache_read_ratio'] if s['cache_read_ratio'] is not None else 'n/a'}**", '',
             '## Latency (all sessions)', '', '| Component | ms | Share |', '|---|---:|---:|']
    wall = sum(s['latency_ms'][k] for k in LAT_LABEL) or 1
    for k, label in LAT_LABEL.items():
        lines.append(f"| {label} | {_fmt(s['latency_ms'][k])} | {s['latency_ms'][k] / wall:.0%} |")
    lines += ['', '## Token budget (input, all turns)', '', '| Category | Tokens | Share |', '|---|---:|---:|']
    total = sum(s['token_budget'].values()) or 1
    for k, label in CAT_LABEL.items():
        lines.append(f"| {label} | {_fmt(s['token_budget'][k])} | {s['token_budget'][k] / total:.0%} |")
    if s['tool_definitions_by_server']:
        lines += ['', 'Tool definitions by server: ' + ', '.join(f'{k} {_fmt(v)}' for k, v in s['tool_definitions_by_server'].items())]
    if s['tool_results_by_tool']:
        lines += ['', 'Tool results by tool: ' + ', '.join(f'{k} {_fmt(v)}' for k, v in list(s['tool_results_by_tool'].items())[:8])]
    e, f = s['efficiency'], s['efficacy']
    lines += ['', '## Efficiency and efficacy', '',
              f"- Duplicate fetch calls: {e['duplicate_fetch_calls']}",
              f"- Estimated recoverable input tokens (combined, overlapping fixes discounted): {_fmt(e['estimated_recoverable_input_tokens'])}"
              + (f" ({e['estimated_recoverable_share']:.0%})" if e['estimated_recoverable_share'] is not None else ''),
              f"- Tool error rate: {f['tool_error_rate'] if f['tool_error_rate'] is not None else 'n/a'}",
              f"- Repeated identical failing calls: {f['repeated_failure_calls']}",
              f"- Model turns per tool call: {f['avg_model_turns_per_tool_call']}", '',
              '## Recommendations', '']
    if not report['recommendations']:
        lines.append('_No recommendations from captured evidence. This does not certify the harness as efficient._')
    for r in report['recommendations']:
        sv = r['estimated_savings_per_session']
        lines += [f"### {r['rank']}. {r['title']}  `{r['severity']}` · {r['dimension']}", '',
                  f"{r['why']} Affects {r['sessions_affected']}/{r['sessions_analyzed']} sessions "
                  f"({_plural(r['occurrences'], 'occurrence')}). Ownership: **{r['ownership']}**.", '',
                  f"Estimated saving per session: {_savings(sv)}.", '']
        if r['note']:
            lines += [f"> {r['note']}", '']
        for lever in r['levers']:
            lines.append(f"- **{OWNER_LABEL[lever['owner']]}:** {lever['action']}")
        lines += ['', '<details><summary>Evidence</summary>', '']
        for ev in r['evidence']:
            where = f"session `{ev['session_id']}` seqs {ev['seqs']}" if ev['session_id'] else 'cross-session'
            lines.append(f"- {where}: {ev['detail']}")
        lines += ['', '</details>', '']
    lines += ['## Tool scorecard', '', '| Tool | Server | Transport | Ownership | Calls | Sessions | Errors | Dupes | Avg result tok | Avg ms | Def tok | Flags |',
              '|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for t in report['tools']:
        lines.append(f"| {t['tool']} | {t['server']} | {t['transport']} | {t['ownership']} | {t['calls']} | {t['sessions_used']} | "
                     f"{_pct(t['error_rate'])} | {_pct(t['duplicate_rate'])} | {_fmt(t['avg_result_tokens'])} | "
                     f"{_fmt(t['avg_duration_ms'])} | {_fmt(t['definition_tokens'])} | {', '.join(t['flags'])} |")
    lines += ['', '## Limitations', ''] + [f'- {x}' for x in report['limitations']]
    return '\n'.join(lines) + '\n'


def _bar(parts: list[tuple[str, float]], height=26) -> str:
    total = sum(v for _, v in parts) or 1
    segs, legend = [], []
    for i, (label, v) in enumerate(parts):
        if v <= 0:
            continue
        color = COLORS[i % len(COLORS)]
        pct = v / total * 100
        segs.append(f'<div class="seg" style="width:{pct:.2f}%;background:{color}" title="{html.escape(label)}: {_fmt(v)} ({pct:.0f}%)"></div>')
        legend.append(f'<span class="lg"><i style="background:{color}"></i>{html.escape(label)} <b>{_fmt(v)}</b> <em>{pct:.0f}%</em></span>')
    return f'<div class="bar" style="height:{height}px">{"".join(segs)}</div><div class="legend">{"".join(legend)}</div>'


def _hbars(items: dict, unit='tokens', limit=8) -> str:
    if not items:
        return '<p class="muted">Not captured.</p>'
    top = list(items.items())[:limit]
    peak = max(v for _, v in top) or 1
    rows = ''.join(f'<div class="hrow"><span>{html.escape(str(k))}</span><div class="htrack"><div style="width:{v / peak * 100:.1f}%"></div></div><b>{_fmt(v)}</b></div>' for k, v in top)
    return f'<div class="hbars">{rows}</div><p class="muted small">{unit}</p>'


def _turn_chart(session: dict) -> str:
    turns = session['token_budget']['turns']
    if not turns:
        return ''
    peak = max((t['input_tokens'] or 0) for t in turns) or 1
    cols = []
    for t in turns:
        comp = t['composition']
        h_total = (t['input_tokens'] or 0) / peak * 120
        total = sum(comp.values()) or 1
        stack = ''.join(f'<div style="height:{comp[k] / total * h_total:.1f}px;background:{COLORS[i]}" title="{CAT_LABEL[k]} {_fmt(comp[k])}"></div>'
                        for i, k in enumerate(CAT_LABEL) if comp.get(k))
        cols.append(f'<div class="tcol" title="turn {t["index"] + 1}: {_fmt(t["input_tokens"])} input tokens">{stack}</div>')
    return (f'<div class="turns"><div class="tlabel">{html.escape(session["session_id"])} · {len(turns)} turns · '
            f'{session["tool_calls"]} tool calls</div><div class="tchart">{"".join(cols)}</div></div>')


def to_html(report: dict, title='Harness Advisor report') -> str:
    s = report['summary']
    e, f = s['efficiency'], s['efficacy']
    cards = [('Sessions', _fmt(s['sessions'])), ('Model turns', _fmt(s['llm_turns'])), ('Tool calls', _fmt(s['tool_calls'])),
             ('Input tokens', _fmt(s['total_input_tokens'])),
             ('Recoverable (est.)', f"{e['estimated_recoverable_share']:.0%}" if e['estimated_recoverable_share'] is not None else '–'),
             ('Tool error rate', f"{f['tool_error_rate']:.0%}" if f['tool_error_rate'] is not None else '–'),
             ('Turns per tool call', _fmt(f['avg_model_turns_per_tool_call'])),
             ('Cache-read ratio', f"{s['cache_read_ratio']:.0%}" if s['cache_read_ratio'] is not None else '–')]
    card_html = ''.join(f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>' for k, v in cards)
    lat = _bar([(LAT_LABEL[k], s['latency_ms'][k]) for k in LAT_LABEL])
    tok = _bar([(CAT_LABEL[k], s['token_budget'][k]) for k in CAT_LABEL])
    recs = []
    for r in report['recommendations']:
        sv = r['estimated_savings_per_session']
        by_owner = {}
        for lever in r['levers']:
            by_owner.setdefault(lever['owner'], []).append(lever['action'])
        levers = ''.join(f'<div class="owner"><span class="tag t-{o}">{OWNER_LABEL[o]}</span><ul>' +
                         ''.join(f'<li>{html.escape(a)}</li>' for a in acts) + '</ul></div>' for o, acts in by_owner.items())
        ev = ''.join(f'<li>{"session <code>" + html.escape(x["session_id"]) + "</code> seqs " + html.escape(str(x["seqs"])) if x["session_id"] else "cross-session"}: {html.escape(x["detail"])}</li>' for x in r['evidence'])
        note = f'<p class="note">{html.escape(r["note"])}</p>' if r['note'] else ''
        recs.append(f'''<div class="rec sev-{r['severity']}"><div class="rhead"><span class="rank">{r['rank']}</span>
<div><h3>{html.escape(r['title'])}</h3><div class="meta"><span class="tag">{r['severity']}</span><span class="tag">{r['dimension']}</span>
<span class="tag">{html.escape(r['ownership'])}</span> {r['sessions_affected']}/{r['sessions_analyzed']} sessions · {_plural(r['occurrences'], 'occurrence')}</div></div>
<div class="save">{_savings(sv)}<br><span class="muted small">est. per session</span></div></div>
<p>{html.escape(r['why'])}</p>{note}{levers}<details><summary>Evidence</summary><ul>{ev}</ul></details></div>''')
    rec_html = ''.join(recs) or '<p class="muted">No recommendations from captured evidence. This does not certify the harness as efficient.</p>'
    rows = ''.join(
        f"<tr><td>{html.escape(t['tool'])}</td><td>{html.escape(t['server'])}</td><td>{html.escape(t['transport'])}</td><td>{html.escape(t['ownership'])}</td>"
        f"<td class=n>{t['calls']}</td><td class=n>{t['sessions_used']}</td><td class=n>{_pct(t['error_rate'])}</td>"
        f"<td class=n>{_pct(t['duplicate_rate'])}</td><td class=n>{_fmt(t['avg_result_tokens'])}</td>"
        f"<td class=n>{_fmt(t['avg_duration_ms'])}</td><td class=n>{_fmt(t['avg_overhead_ms'])}</td><td class=n>{_fmt(t['definition_tokens'])}</td>"
        f"<td>{''.join(f'<span class=flag>{html.escape(x)}</span>' for x in t['flags'])}</td></tr>" for t in report['tools'])
    turns = ''.join(_turn_chart(x) for x in report['sessions'][:8])
    limits = ''.join(f'<li>{html.escape(x)}</li>' for x in report['limitations'])
    data = html.escape(json.dumps({'summary': s, 'recommendations': report['recommendations']}, default=str))
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--bg:#fbfaf7;--fg:#1f1e1c;--mut:#6b6a65;--line:#e4e2da;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1b19;--fg:#ecebe6;--mut:#a3a29c;--line:#34332f;--card:#252421}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:32px 20px 60px}} h1{{font-size:26px;font-weight:600;margin:0 0 4px}}
h2{{font-size:18px;font-weight:600;margin:36px 0 12px}} h3{{font-size:16px;margin:0;font-weight:600}}
.muted{{color:var(--mut)}} .small{{font-size:12px}} code{{font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(122px,1fr));gap:10px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}} .k{{font-size:12px;color:var(--mut)}} .v{{font-size:22px;font-weight:600}}
.bar{{display:flex;border-radius:6px;overflow:hidden;background:var(--line)}} .seg{{height:100%}}
.legend{{display:flex;flex-wrap:wrap;gap:6px 16px;margin:8px 0 0;font-size:13px}} .lg i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}} .lg em{{color:var(--mut);font-style:normal}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:20px}} @media(max-width:760px){{.grid2{{grid-template-columns:1fr}}}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.hrow{{display:grid;grid-template-columns:160px 1fr 70px;gap:8px;align-items:center;font-size:13px;margin:4px 0}} .hrow span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.htrack{{background:var(--line);height:10px;border-radius:5px}} .htrack div{{background:#534AB7;height:100%;border-radius:5px}} .hrow b{{text-align:right;font-weight:500}}
.rec{{background:var(--card);border:1px solid var(--line);border-left:4px solid #888780;border-radius:10px;padding:14px 16px;margin:12px 0}}
.sev-high{{border-left-color:#D85A30}} .sev-medium{{border-left-color:#BA7517}} .sev-low{{border-left-color:#1D9E75}}
.rhead{{display:grid;grid-template-columns:34px 1fr auto;gap:12px;align-items:start}} .rank{{font-size:20px;font-weight:600;color:var(--mut)}}
.save{{text-align:right;font-size:13px;font-weight:500}} .meta{{font-size:12px;color:var(--mut);margin-top:4px}}
.tag{{display:inline-block;font-size:11px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);margin-right:4px}}
.t-harness_owner{{background:#EEEDFE;color:#3C3489;border-color:#CECBF6}} .t-server_owner{{background:#E1F5EE;color:#085041;border-color:#9FE1CB}}
.t-gateway_operator{{background:#FAEEDA;color:#633806;border-color:#FAC775}} .t-vendor_request{{background:#F1EFE8;color:#444441;border-color:#D3D1C7}}
.owner{{margin:6px 0}} .owner ul{{margin:4px 0 4px 18px;padding:0}} .note{{font-size:13px;background:var(--bg);border:1px dashed var(--line);padding:6px 10px;border-radius:6px}}
details{{font-size:13px;margin-top:6px}} table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}} td.n{{text-align:right}}
.flag{{display:inline-block;font-size:11px;background:#FAECE7;color:#712B13;border-radius:4px;padding:0 6px;margin:1px}}
.turns{{margin:10px 0}} .tlabel{{font-size:12px;color:var(--mut)}} .tchart{{display:flex;align-items:flex-end;gap:2px;height:124px;border-bottom:1px solid var(--line)}}
.tcol{{flex:1;max-width:28px;display:flex;flex-direction:column-reverse}}
</style></head><body><main>
<h1>{html.escape(title)}</h1><p class="muted">Heuristic analysis of captured sessions. Savings are estimates; validate top items with paired trials.</p>
<div class="cards">{card_html}</div>
<h2>Where time goes</h2><div class="panel">{lat}</div>
<h2>Where input tokens go</h2><div class="panel">{tok}</div>
<div class="grid2"><div><h2>Tool definitions by server</h2><div class="panel">{_hbars(s['tool_definitions_by_server'])}</div></div>
<div><h2>Tool results by tool</h2><div class="panel">{_hbars(s['tool_results_by_tool'])}</div></div></div>
<h2>Recommendations</h2>{rec_html}
<h2>Tool scorecard</h2><div class="panel" style="overflow-x:auto"><table><thead><tr><th>Tool</th><th>Server</th><th>Transport</th><th>Ownership</th><th>Calls</th><th>Sessions</th><th>Errors</th><th>Dupes</th><th>Avg result tok</th><th>Avg ms</th><th>Avg overhead ms</th><th>Def tok</th><th>Flags</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>Per-turn input composition</h2><div class="panel">{turns or '<p class="muted">No model turns captured.</p>'}<div class="legend">{''.join(f'<span class="lg"><i style="background:{COLORS[i]}"></i>{v}</span>' for i, v in enumerate(CAT_LABEL.values()))}</div></div>
<h2>Limitations</h2><ul class="muted">{limits}</ul>
<script type="application/json" id="advisor-data">{data}</script>
</main></body></html>'''
