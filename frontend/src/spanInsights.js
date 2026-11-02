const numeric=(...values)=>values.find(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0)??null;
export function usageDetails(payload={}) {
 const t=payload.tokens??payload.usage??{};
 const input=numeric(t.input,t.input_tokens,t.prompt_tokens), output=numeric(t.output,t.output_tokens,t.completion_tokens);
 return {input,output,total:numeric(t.total_tokens,input!==null&&output!==null?input+output:null),cached:numeric(t.cached_tokens,t.input_tokens_details?.cached_tokens,t.prompt_tokens_details?.cached_tokens),reasoning:numeric(t.reasoning_tokens,t.output_tokens_details?.reasoning_tokens,t.completion_tokens_details?.reasoning_tokens),cost:numeric(payload.cost_usd)};
}
export function spanInsights(span,events) {
 const start=span.event, end=span.finish;
 const snapshots=events.filter(e=>e.kind==='context.snapshot').slice().sort((a,b)=>a.seq-b.seq);
 const linked=snapshots.find(e=>(e.call_id??e.payload?.call_id)===span.id);
 // Legacy snapshots have no call IDs. Never assume a nearest snapshot is exact attribution.
 const context=linked??(span.family==='llm'?snapshots.filter(e=>e.seq<=start.seq).at(-1):null);
 const prior=context?snapshots.filter(e=>e.seq<context.seq).at(-1):null;
 const messages=context?.payload?.messages??[], previous=prior?.payload?.messages??[];
 const old=new Map(previous.map(m=>[m.id,m])), now=new Map(messages.map(m=>[m.id,m]));
 const changes={added:messages.filter(m=>!old.has(m.id)),removed:previous.filter(m=>!now.has(m.id)),modified:messages.filter(m=>old.has(m.id)&&JSON.stringify(old.get(m.id))!==JSON.stringify(m)).map(m=>({before:old.get(m.id),after:m}))};
 const evidence=[];
 for(const event of [start,end].filter(Boolean))for(const key of ['plan','decision','explanation','rationale','reasoning_summary','validation','validation_results'])if(event.payload?.[key]!==undefined)evidence.push({seq:event.seq,field:key,value:event.payload[key]});
 const p=end?.payload??{};
 return {usage:usageDetails(p),context,prior,messages,changes,contextAttribution:linked?'Explicit call ID link':context?'Nearest preceding snapshot; association is not verified':'No associated context snapshot',evidence,model:p.model??start.payload?.model??null,retryOf:start.payload?.retry_of??null,attempt:start.payload?.attempt??null};
}
