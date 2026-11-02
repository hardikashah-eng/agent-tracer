// This is an evidence display, not a semantic judge or causal attribution engine.
export function goalProvenance(span,events,context) {
 const origin=events.slice().sort((a,b)=>a.seq-b.seq).find(e=>e.kind==='agent.start');
 const raw=origin?.payload?.goal;
 const goal=typeof raw==='object'&&raw?raw:{id:null,text:typeof raw==='string'?raw:origin?.payload?.task};
 const p=span.finish?.payload??span.event.payload??{};
 const assessment=p.goal_alignment;
 const decisionSources=Array.isArray(p.supporting_source_ids)?p.supporting_source_ids:[];
 const messages=Array.isArray(context?.payload?.messages)?context.payload.messages:[];
 const sources=messages.map((m,i)=>({id:m.source_id??m.source??m.id??`message-${i}`,messageId:m.id,type:m.source_type??m.category??'unknown',hash:m.content_hash??null,eventSeq:m.source_event_seq??null,goalIds:Array.isArray(m.goal_ids)?m.goal_ids:[],tokens:typeof m.estimated_tokens==='number'&&Number.isFinite(m.estimated_tokens)&&m.estimated_tokens>=0?m.estimated_tokens:0}));
 const known=new Set(sources.map(s=>s.id));
 const unresolved=decisionSources.filter(id=>!known.has(id));
 const total=sources.reduce((n,s)=>n+s.tokens,0);
 const groups={};for(const s of sources)groups[s.type]=(groups[s.type]??0)+s.tokens;
 const shares=Object.entries(groups).map(([type,tokens])=>({type,tokens,percent:total?100*tokens/total:null}));
 let status='unknown',reason='No goal assessment captured. Source provenance alone cannot determine semantic alignment.';
 const evidenceIds=Array.isArray(assessment?.evidence_source_ids)?assessment.evidence_source_ids:[];
 const checks=Array.isArray(assessment?.checks)?assessment.checks:[];
 const validChecks=checks.filter(c=>typeof c?.passed==='boolean'&&typeof c?.name==='string');
 const assessmentSources=evidenceIds.filter(id=>!known.has(id));
 const linked=context&&(context.call_id??context.payload?.call_id)===span.id;
 if(assessment){
   status='warning';reason='Assessment is incomplete or cannot be linked to the original goal and source evidence.';
   if(goal.id&&assessment.goal_id&&assessment.goal_id!==goal.id){status='drift';reason='Reported decision goal differs from the original goal ID.'}
   else if(goal.id&&assessment.goal_id===goal.id&&assessment.assessor&&linked&&evidenceIds.length&&!assessmentSources.length){
     if(validChecks.some(c=>c.passed===false)){status='drift';reason='One or more reported goal checks failed.'}
     else if(validChecks.length&&validChecks.length===checks.length&&validChecks.every(c=>c.passed===true)){status='aligned';reason='All captured goal checks passed against the original goal and linked source evidence.'}
   }
 }
 if(unresolved.length&&status!=='drift'){status='warning';reason='Decision cites source IDs missing from the associated snapshot.'}
 return {goal,origin,status,reason,assessment,checks:validChecks,evidenceIds,unresolved:[...new Set([...unresolved,...assessmentSources])],sources,shares,decisionSources,linked};
}
