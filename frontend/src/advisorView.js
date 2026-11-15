// Pure view-model helpers for the Harness Advisor panel (unit-tested with node --test).
export const CATEGORIES = [
  ['system', 'System prompt', '#8877df'],
  ['tool_definitions', 'Tool definitions', '#42c8a4'],
  ['conversation', 'Conversation', '#4d9ce5'],
  ['tool_results', 'Tool results', '#e6a35d'],
  ['other', 'Other', '#7b8798'],
];
export const LATENCY = [
  ['llm_ms', 'Model', '#a78bfa'],
  ['tool_upstream_ms', 'Tool: upstream API', '#22c55e'],
  ['tool_overhead_ms', 'Tool: protocol/gateway', '#f97316'],
  ['tool_unattributed_ms', 'Tool: unsplit', '#38bdf8'],
  ['harness_ms', 'Harness / idle', '#64748b'],
];
export const OWNER_LABEL = {harness_owner: 'Harness', server_owner: 'Server owner', gateway_operator: 'Gateway', vendor_request: 'Vendor request'};
export const STATUS = {
  realized: ['Realized', '#22c55e'], partial: ['Partial', '#eab308'], no_effect: ['No effect', '#94a3b8'],
  regressed: ['Regressed', '#fb7185'], awaiting_data: ['Awaiting data', '#38bdf8'],
  confirmed: ['Confirmed', '#22c55e'], inconclusive: ['Inconclusive', '#eab308'], insufficient_data: ['Needs data', '#38bdf8'],
};

const finite = v => typeof v === 'number' && Number.isFinite(v);

export function fmt(n, digits = 0) {
  if (!finite(n)) return '–';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e4) return Math.round(n / 1e3) + 'k';
  return n.toLocaleString(undefined, {maximumFractionDigits: digits});
}

export function fmtMs(ms) {
  if (!finite(ms)) return '–';
  return ms >= 1000 ? (ms / 1000).toFixed(ms >= 10000 ? 0 : 1) + ' s' : Math.round(ms) + ' ms';
}

// Stacked-bar segments with percentages; zero/invalid values are dropped.
export function segments(values, spec) {
  const rows = spec.map(([key, label, color]) => ({key, label, color, value: finite(values?.[key]) && values[key] > 0 ? values[key] : 0}));
  const total = rows.reduce((n, r) => n + r.value, 0);
  return {total, parts: rows.filter(r => r.value > 0).map(r => ({...r, pct: total ? 100 * r.value / total : 0}))};
}

// Per-turn stacked columns scaled to the largest turn.
export function turnColumns(turns = []) {
  const peak = Math.max(1, ...turns.map(t => (finite(t.input_tokens) ? t.input_tokens : 0)));
  return turns.map((t, i) => {
    const {total, parts} = segments(t.composition, CATEGORIES);
    const height = 100 * (finite(t.input_tokens) ? t.input_tokens : total) / peak;
    return {index: i, seq: t.seq, height, input: t.input_tokens, cached: t.cached_tokens, basis: t.basis,
      parts: parts.map(p => ({...p, height: total ? height * p.value / total : 0}))};
  });
}

// Waterfall rows: offsets are percentages of the session span. Missing start times fall back to running order.
export function waterfall(timeline = [], limit = 80) {
  let cursor = 0;
  const items = timeline.slice(0, limit).map(t => {
    const dur = finite(t.duration_ms) && t.duration_ms >= 0 ? t.duration_ms : 0;
    const start = finite(t.start_ms) ? t.start_ms : cursor;
    cursor = Math.max(cursor, start + dur);
    return {...t, start, dur};
  });
  const span = Math.max(1, ...items.map(i => i.start + i.dur));
  return {span, truncated: timeline.length > limit, rows: items.map(i => {
    const upstream = i.kind === 'tool' && finite(i.upstream_ms) ? Math.min(i.upstream_ms, i.dur) : null;
    return {key: `${i.kind}-${i.seq}`, kind: i.kind, name: i.name, ok: i.ok !== false, transport: i.transport,
      left: 100 * i.start / span, width: Math.max(0.3, 100 * i.dur / span), duration: i.dur,
      upstreamPct: upstream === null ? null : (i.dur ? 100 * upstream / i.dur : 0),
      overheadMs: upstream === null ? null : i.dur - upstream};
  })};
}

export function ticks(span, count = 5) {
  return Array.from({length: count + 1}, (_, i) => ({pct: 100 * i / count, label: fmtMs(span * i / count)}));
}

export function sortTools(tools = [], key = 'calls', dir = 'desc') {
  const sign = dir === 'asc' ? 1 : -1;
  const val = t => key === 'flags' ? t.flags.length : t[key];
  return [...tools].sort((a, b) => {
    const x = val(a), y = val(b);
    if (x === y) return a.tool.localeCompare(b.tool);
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return typeof x === 'string' ? sign * x.localeCompare(y) : sign * (x - y);
  });
}

export function groupLevers(levers = []) {
  const out = [];
  for (const l of levers) {
    const g = out.find(x => x.owner === l.owner);
    if (g) g.actions.push(l.action); else out.push({owner: l.owner, label: OWNER_LABEL[l.owner] || l.owner, actions: [l.action]});
  }
  return out;
}

export function planFor(report, recId) {
  return (report?.verification_plans || []).find(p => p.recommendation_id === recId) || null;
}

export function ledgerFor(report, recId) {
  return (report?.ledger?.entries || []).filter(e => e.recommendation_id === recId);
}
