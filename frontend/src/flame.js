import {usageDetails} from './spanInsights.js';
// Preserve timestamps: never infer nesting or pair ambiguous concurrent calls.
export function buildSpans(events) {
  const sorted = events.filter(e => Number.isFinite(e.ts)).slice().sort((a,b)=>a.ts-b.ts || a.seq-b.seq);
  const end = sorted.at(-1)?.ts ?? 0, pending = [], spans = [], markers = [];
  for (const e of sorted) {
    const [family, phase] = e.kind.split('.');
    const id = e.call_id ?? e.payload?.call_id;
    if (['before','start'].includes(phase) && ['llm','tool','agent'].includes(family)) {
      const s = {id: id || `event-${e.seq}`, parent: e.parent_call_id ?? e.payload?.parent_call_id, family, name:e.name, start:e.ts, end, open:true, event:e};
      pending.push(s); spans.push(s);
    } else if (['after','error','failed','completed','cancelled'].includes(phase)) {
      const candidates = pending.filter(s=>s.family===family && (id ? s.id===id : s.name===e.name));
      if (candidates.length===1) {
        const s=candidates[0]; s.end=e.ts; s.open=false; s.finish=e; s.error=['error','failed'].includes(phase); pending.splice(pending.indexOf(s),1);
        const metrics=usageDetails(e.payload);
        s.tokens=metrics.total;
        s.cost=metrics.cost;
      } else markers.push(e);
    } else markers.push(e);
  }
  const byId=new Map(spans.map(s=>[s.id,s]));
  for (const s of spans) {
    let p=byId.get(s.parent), seen=new Set([s.id]); s.depth=0;
    while(p && !seen.has(p.id)){seen.add(p.id);s.depth++;p=byId.get(p.parent)}
    s.invalidParent=!!p;
    s.duration=Math.max(0,s.end-s.start);
  }
  // A row per parent depth, with extra lanes for overlapping siblings.
  const rows=[];
  for(const depth of [...new Set(spans.map(s=>s.depth))].sort((a,b)=>a-b)) {
    const lanes=[];
    for(const s of spans.filter(s=>s.depth===depth)){
      let lane=lanes.findIndex(t=>t<=s.start); if(lane<0)lane=lanes.length;
      lanes[lane]=s.end; s.row=rows.length+lane;
    }
    rows.push(...lanes);
  }
  return {spans,markers,start:sorted[0]?.ts??0,end,rows:rows.length};
}
export function observedSummary({spans,start,end}) {
  const calls=spans.filter(s=>s.family!=='agent'&&!s.open).sort((a,b)=>b.duration-a.duration);
  if(!calls.length)return 'Waiting for completed LLM or tool calls.';
  const s=calls[0], errors=spans.filter(s=>s.error).length;
  return `Longest observed call: ${s.name} (${(s.duration*1000).toFixed(0)} ms${end>start ? `, ${Math.round(s.duration/(end-start)*100)}% of trace elapsed time` : ''}). ${errors} failed calls. Overlapping durations are not additive; this identifies timing, not root cause.`;
}
