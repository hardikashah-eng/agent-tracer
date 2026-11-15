import React, {useEffect, useMemo, useRef, useState} from 'react';
import {CATEGORIES, LATENCY, STATUS, fmt, fmtMs, groupLevers, ledgerFor, planFor, segments, sortTools, ticks, turnColumns, waterfall} from './advisorView.js';
import './advisor.css';

const Badge = ({status}) => {
  const [label, color] = STATUS[status] || [status, '#94a3b8'];
  return <span className="adv-badge" style={{borderColor: color, color}}>{label}</span>;
};

function StackBar({values, spec}) {
  const {parts} = segments(values, spec);
  return <><div className="adv-bar">{parts.map(p => <div key={p.key} style={{width: p.pct + '%', background: p.color}} title={`${p.label}: ${fmt(p.value)} (${p.pct.toFixed(0)}%)`}/>)}</div>
    <div className="adv-legend">{parts.map(p => <span key={p.key}><i style={{background: p.color}}/>{p.label} <b>{spec === LATENCY ? fmtMs(p.value) : fmt(p.value)}</b> <em>{p.pct.toFixed(0)}%</em></span>)}</div></>;
}

function TurnChart({session}) {
  const cols = turnColumns(session?.token_budget?.turns);
  if (!cols.length) return <p className="adv-muted">No model turns captured for this session.</p>;
  return <><div className="adv-turns">{cols.map(c => <div key={c.index} className="adv-turn" title={`Turn ${c.index + 1}: ${fmt(c.input)} input tokens${c.cached ? ` (${fmt(c.cached)} cached)` : ''} · ${c.basis}`}>
    <div className="adv-turn-stack" style={{height: c.height + '%'}}>{c.parts.map(p => <div key={p.key} style={{flexGrow: p.value, background: p.color}}/>)}</div>
    <small>{c.index + 1}</small></div>)}</div>
    <div className="adv-legend">{CATEGORIES.map(([k, l, c]) => <span key={k}><i style={{background: c}}/>{l}</span>)}</div></>;
}

function Waterfall({session}) {
  const {span, rows, truncated} = useMemo(() => waterfall(session?.timeline), [session]);
  if (!rows.length) return <p className="adv-muted">No timed model or tool calls captured.</p>;
  return <div className="adv-wf">
    <div className="adv-wf-axis">{ticks(span).map(t => <span key={t.pct} style={{left: t.pct + '%'}}>{t.label}</span>)}</div>
    {rows.map(r => <div className="adv-wf-row" key={r.key}>
      <span className="adv-wf-label" title={r.name}>{r.kind === 'llm' ? '◆' : '▸'} {r.name}</span>
      <div className="adv-wf-track">
        <div className={'adv-wf-bar ' + r.kind + (r.ok ? '' : ' err')} style={{left: r.left + '%', width: r.width + '%'}}
             title={`${r.name} · ${fmtMs(r.duration)}${r.overheadMs !== null ? ` · protocol/gateway ${fmtMs(r.overheadMs)}` : ''}${r.ok ? '' : ' · failed'}`}>
          {r.upstreamPct !== null && <div className="adv-wf-upstream" style={{width: r.upstreamPct + '%'}}/>}
        </div>
      </div>
      <span className="adv-wf-dur">{fmtMs(r.duration)}</span>
    </div>)}
    {truncated && <p className="adv-muted">Showing the first {rows.length} calls.</p>}
    <div className="adv-legend"><span><i style={{background: '#a78bfa'}}/>Model</span><span><i style={{background: '#22c55e'}}/>Tool: upstream API</span>
      <span><i style={{background: '#f97316'}}/>Tool: protocol/gateway (or unsplit)</span><span><i style={{background: 'transparent', border: '1px solid #fb7185'}}/>Failed</span></div>
  </div>;
}

const COLUMNS = [['tool', 'Tool'], ['server', 'Server'], ['transport', 'Transport'], ['ownership', 'Ownership'], ['calls', 'Calls'],
  ['sessions_used', 'Sessions'], ['error_rate', 'Errors'], ['duplicate_rate', 'Dupes'], ['avg_result_tokens', 'Avg result tok'],
  ['avg_duration_ms', 'Avg time'], ['avg_overhead_ms', 'Avg overhead'], ['definition_tokens', 'Schema tok'], ['flags', 'Flags']];
const pct = v => v === null || v === undefined ? '–' : Math.round(v * 100) + '%';

function Scorecard({tools}) {
  const [sort, setSort] = useState(['flags', 'desc']);
  const rows = sortTools(tools, ...sort);
  const cell = (t, k) => k === 'error_rate' || k === 'duplicate_rate' ? pct(t[k]) : k.endsWith('_ms') ? fmtMs(t[k])
    : k === 'flags' ? t.flags.map(f => <span key={f} className="adv-flag">{f}</span>) : typeof t[k] === 'number' ? fmt(t[k]) : (t[k] ?? '–');
  return <div className="adv-table-wrap"><table className="adv-table"><thead><tr>{COLUMNS.map(([k, l]) =>
    <th key={k} onClick={() => setSort(([sk, d]) => [k, sk === k && d === 'desc' ? 'asc' : 'desc'])} className={sort[0] === k ? 'sorted' : ''}>
      {l}{sort[0] === k ? (sort[1] === 'desc' ? ' ↓' : ' ↑') : ''}</th>)}</tr></thead>
    <tbody>{rows.map(t => <tr key={t.tool}>{COLUMNS.map(([k]) => <td key={k} className={typeof t[k] === 'number' ? 'num' : ''}>{cell(t, k)}</td>)}</tr>)}</tbody></table></div>;
}

function Plan({plan}) {
  const pm = plan.primary_metric;
  return <div className="adv-plan">
    <p><b>Hypothesis:</b> {plan.hypothesis}</p>
    <p><b>Variant arm:</b> {plan.intervention.change}</p>
    <p><b>Primary metric:</b> {pm.name} · baseline {fmt(pm.baseline_mean)} (SD {fmt(pm.baseline_sd)}) · expected −{fmt(pm.expected_delta_per_task)} per task</p>
    <p><b>Design:</b> {plan.design.pairs_required} task pairs · {plan.design.type}</p>
    <p><b>Guardrails:</b> {plan.guardrails.map(g => `${g.metric}: ${g.rule}`).join(' · ')}</p>
    <p><b>Tag sessions with</b></p><pre>{JSON.stringify(plan.tagging, null, 2)}</pre>
    <p className="adv-muted">Tasks to start with: {plan.design.tasks.slice(0, 8).join(', ')}</p>
    {plan.result && <div className="adv-result"><Badge status={plan.result.verdict}/> {plan.result.pairs_observed} pairs
      {plan.result.mean_delta !== undefined && <> · mean change {fmt(plan.result.mean_delta)} ({Math.round(plan.result.relative_change * 100)}%) · 95% CI {fmt(plan.result.ci95[0])} to {fmt(plan.result.ci95[1])}</>}
      <div className="adv-muted">{plan.result.explanation}</div></div>}
  </div>;
}

function Recommendation({rec, report, onApply, busy}) {
  const [open, setOpen] = useState(rec.rank === 1);
  const [showPlan, setShowPlan] = useState(false);
  const [note, setNote] = useState('');
  const plan = planFor(report, rec.id);
  const applied = ledgerFor(report, rec.id);
  const s = rec.estimated_savings_per_session;
  const savings = [s.tokens && `~${fmt(s.tokens)} tok`, s.ms && `~${fmtMs(s.ms)}`, s.turns && `~${s.turns} turns`].filter(Boolean).join(' · ') || 'efficacy';
  return <article className={'adv-rec sev-' + rec.severity}>
    <header onClick={() => setOpen(o => !o)}>
      <span className="adv-rank">{rec.rank}</span>
      <div><h4>{rec.title}</h4><div className="adv-meta"><span className="adv-tag">{rec.severity}</span><span className="adv-tag">{rec.dimension}</span>
        <span className="adv-tag">{rec.ownership}</span>{rec.confidence === 'llm_judge' && <span className="adv-tag judge">LLM judge</span>}
        {rec.sessions_affected}/{rec.sessions_analyzed} sessions{applied.length ? <> · applied <Badge status={applied[0].status}/></> : null}</div></div>
      <div className="adv-save">{savings}<small>est. per session</small></div>
    </header>
    {open && <div className="adv-rec-body">
      <p>{rec.why}</p>{rec.note && <p className="adv-note">{rec.note}</p>}
      {groupLevers(rec.levers).map(g => <div key={g.owner} className="adv-lever"><span className={'adv-owner o-' + g.owner}>{g.label}</span><ul>{g.actions.map(a => <li key={a}>{a}</li>)}</ul></div>)}
      <details><summary>Evidence</summary><ul>{rec.evidence.map((e, i) => <li key={i}>{e.session_id ? <code>{e.session_id}</code> : 'cross-session'} {e.seqs?.length ? `#${e.seqs.slice(0, 6).join(', #')}` : ''} — {e.detail}</li>)}</ul></details>
      <div className="adv-actions">
        <button disabled={!plan} onClick={() => setShowPlan(v => !v)} title={plan ? '' : 'Plans are generated for the top recommendations'}>{showPlan ? 'Hide' : 'Show'} verification plan</button>
        <input placeholder="What changed? (optional note)" value={note} onChange={e => setNote(e.target.value)}/>
        <button className="primary" disabled={busy} onClick={() => onApply(rec, note, plan?.id).then(() => setNote(''))}>Mark applied</button>
      </div>
      {showPlan && plan && <Plan plan={plan}/>}
    </div>}
  </article>;
}

function Ledger({ledger, onDelete}) {
  if (!ledger?.entries?.length) return <p className="adv-muted">Nothing recorded yet. Use <b>Mark applied</b> on a recommendation after you ship the change; later sessions are compared with the sessions before it.</p>;
  return <>
    <div className="adv-cards small"><div><small>Cumulative tokens saved</small><strong>{fmt(ledger.cumulative_tokens_saved)}</strong></div>
      <div><small>Saved per session</small><strong>{fmt(ledger.tokens_saved_per_session)}</strong></div>
      {Object.entries(ledger.status_counts).map(([k, v]) => <div key={k}><small>{(STATUS[k] || [k])[0]}</small><strong>{v}</strong></div>)}</div>
    <div className="adv-table-wrap"><table className="adv-table"><thead><tr><th>Applied</th><th>Recommendation</th><th>Status</th><th>Expected tok/session</th><th>Realized tok/session</th><th>Finding share</th><th>Sessions</th><th/></tr></thead>
      <tbody>{ledger.entries.map(e => <tr key={e.id}>
        <td className="nowrap">{new Date(e.applied_at * 1000).toLocaleDateString()}</td>
        <td>{e.title}{e.note && <div className="adv-muted">{e.note}</div>}</td>
        <td><Badge status={e.status}/><div className="adv-muted">{e.explanation}</div></td>
        <td className="num">{fmt(e.expected_savings_per_session.tokens)}</td>
        <td className="num">{fmt(e.realized?.waste_tokens_saved_per_session)}</td>
        <td className="num">{pct(e.baseline.finding_share)} → {pct(e.after.finding_share)}</td>
        <td className="num">{e.baseline.sessions} → {e.after.sessions}</td>
        <td><button onClick={() => onDelete(e.id)} title="Remove from ledger">×</button></td>
      </tr>)}</tbody></table></div>
    <p className="adv-muted">{ledger.warning}</p></>;
}

export default function AdvisorPanel({sid, api = '', events = []}) {
  const [scope, setScope] = useState('all'), [useJudge, setUseJudge] = useState(false), [judge, setJudge] = useState(null);
  const [report, setReport] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState(''), [selected, setSelected] = useState('');
  const ticket = useRef(0);
  useEffect(() => { fetch(`${api}/api/advisor/judge`).then(r => r.json()).then(setJudge).catch(() => setJudge({configured: false})); }, [api]);
  useEffect(() => { if (!sid && scope === 'run') setScope('all'); }, [sid, scope]);
  async function analyze() {
    const t = ++ticket.current; setBusy(true); setError('');
    try {
      const q = new URLSearchParams(); if (useJudge) q.set('judge', 'true');
      const url = scope === 'run' && sid ? `${api}/api/sessions/${encodeURIComponent(sid)}/advisor?${q}` : `${api}/api/advisor?${q}`;
      const r = await fetch(url); if (!r.ok) throw Error((await r.json().catch(() => ({}))).detail || `Advisor failed (${r.status})`);
      const data = await r.json();
      if (t === ticket.current) { setReport(data); setSelected(s => data.sessions.some(x => x.session_id === s) ? s : (data.sessions.find(x => x.session_id === sid) || data.sessions[0])?.session_id || ''); }
    } catch (e) { if (t === ticket.current) setError(e.message); } finally { if (t === ticket.current) setBusy(false); }
  }
  async function apply(rec, note, planId) {
    setBusy(true); setError('');
    try {
      const r = await fetch(`${api}/api/advisor/ledger`, {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({recommendation_id: rec.id, note, plan_id: planId || null})});
      if (!r.ok) throw Error((await r.json().catch(() => ({}))).detail || `Could not record (${r.status})`);
      await analyze();
    } catch (e) { setError(e.message); setBusy(false); }
  }
  async function remove(id) {
    try { await fetch(`${api}/api/advisor/ledger/${encodeURIComponent(id)}`, {method: 'DELETE'}); await analyze(); } catch (e) { setError(e.message); }
  }
  const session = report?.sessions.find(s => s.session_id === selected) || report?.sessions[0];
  const s = report?.summary;
  return <section className="advisor-panel" aria-label="Harness advisor">
    <div className="adv-head"><div><h3>Harness Advisor</h3><p className="adv-muted">Where time and tokens go across sessions, what is wasted or failing, and what to change. Heuristic; savings are estimates.</p></div>
      <div className="adv-controls">
        <select value={scope} onChange={e => setScope(e.target.value)} aria-label="Scope"><option value="all">All captured runs</option><option value="run" disabled={!sid}>This run</option></select>
        <label title={judge?.configured ? `${judge.backend} · ${judge.model}` : (judge?.error || 'Set AGENT_TRACER_JUDGE to enable')}>
          <input type="checkbox" disabled={!judge?.configured} checked={useJudge} onChange={e => setUseJudge(e.target.checked)}/> LLM judge{judge?.configured ? ` (${judge.model})` : ' (off)'}</label>
        <button className="primary" disabled={busy} onClick={analyze}>{busy ? 'Analyzing…' : report ? 'Re-analyze' : 'Analyze sessions'}</button>
      </div></div>
    {error && <p className="error" role="alert">{error}</p>}
    {!report && !busy && <p className="adv-muted">Analyze to see the token budget, latency waterfall, tool scorecard, recommendations, verification plans and savings ledger{events.length ? '' : ' (capture or import some sessions first)'}.</p>}
    {report && <>
      <div className="adv-cards">
        <div><small>Sessions</small><strong>{s.sessions}</strong></div><div><small>Model turns</small><strong>{fmt(s.llm_turns)}</strong></div>
        <div><small>Tool calls</small><strong>{fmt(s.tool_calls)}</strong></div><div><small>Input tokens</small><strong>{fmt(s.total_input_tokens)}</strong></div>
        <div><small>Recoverable (est.)</small><strong>{s.efficiency.estimated_recoverable_share === null ? '–' : pct(s.efficiency.estimated_recoverable_share)}</strong></div>
        <div><small>Tool error rate</small><strong>{pct(s.efficacy.tool_error_rate)}</strong></div>
        <div><small>Cache-read ratio</small><strong>{pct(s.cache_read_ratio)}</strong></div>
        <div><small>Tokens saved (ledger)</small><strong>{fmt(report.ledger?.cumulative_tokens_saved ?? 0)}</strong></div>
      </div>
      <div className="adv-grid">
        <div className="adv-box"><h4>Input token budget · all sessions</h4><StackBar values={s.token_budget} spec={CATEGORIES}/></div>
        <div className="adv-box"><h4>Latency · all sessions</h4><StackBar values={s.latency_ms} spec={LATENCY}/></div>
      </div>
      <div className="adv-box">
        <div className="adv-boxhead"><h4>Session drill-down</h4>
          <select value={session?.session_id || ''} onChange={e => setSelected(e.target.value)} aria-label="Session">{report.sessions.map(x => <option key={x.session_id} value={x.session_id}>{x.session_id} · {x.llm_turns} turns · {x.tool_calls} tools</option>)}</select></div>
        <div className="adv-grid"><div><h5>Per-turn input composition</h5><TurnChart session={session}/></div>
          <div><h5>Latency waterfall</h5><Waterfall session={session}/></div></div>
      </div>
      <h4 className="adv-section">Recommendations ({report.recommendations.length})</h4>
      {report.recommendations.length ? report.recommendations.map(r => <Recommendation key={r.id} rec={r} report={report} onApply={apply} busy={busy}/>)
        : <p className="adv-muted">No recommendations from captured evidence. This does not certify the harness as efficient.</p>}
      {report.judge && <div className="adv-box"><h4>LLM judge · {report.judge.backend} · {report.judge.model}</h4>
        <p className="adv-muted">{report.judge.judge_calls} calls ({report.judge.cache_hits} cached) · {report.judge.tool_selection_flagged}/{report.judge.tool_selection_judged} tool choices flagged · {report.judge.result_usage_judged} results judged · {report.judge.errors.length} errors. {report.judge.disclaimer}</p>
        <ul>{report.judge.tool_selection.filter(v => !v.appropriate).slice(0, 8).map(v => <li key={v.session_id + v.seq}><code>{v.tool}</code> in {v.session_id}: {v.reason}{v.better_tool ? ` (better: ${v.better_tool})` : ''}</li>)}
          {report.judge.result_usage.slice(0, 6).map(u => <li key={'u' + u.session_id + u.seq}><code>{u.tool}</code> result ~{pct(u.used_ratio)} used{u.unused_fields.length ? ` · unused: ${u.unused_fields.slice(0, 5).join(', ')}` : ''}</li>)}</ul></div>}
      <h4 className="adv-section">Tool scorecard</h4><Scorecard tools={report.tools}/>
      <h4 className="adv-section">Savings ledger</h4><Ledger ledger={report.ledger} onDelete={remove}/>
      <details className="adv-limits"><summary>Limitations</summary><ul>{report.limitations.map(x => <li key={x}>{x}</li>)}</ul></details>
    </>}
  </section>;
}
