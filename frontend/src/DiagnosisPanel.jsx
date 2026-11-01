import React, {useEffect, useRef, useState} from 'react';
import './diagnosis.css';

export default function DiagnosisPanel({sid, events, api='', onEvidence}) {
  const [report,setReport]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const generation=useRef(0);
  useEffect(()=>{generation.current++;setReport(null);setError('');setBusy(false);},[sid,api]);
  async function run() {
    const ticket=++generation.current;
    setBusy(true);setError('');
    try {
      const response=await fetch(`${api}/api/sessions/${encodeURIComponent(sid)}/diagnosis`);
      if(!response.ok) throw Error(`Diagnosis failed (${response.status})`);
      const data=await response.json();
      if(ticket===generation.current)setReport(data);
    } catch(e) {if(ticket===generation.current)setError(e.message);}
    finally {if(ticket===generation.current)setBusy(false);}
  }
  const latest=events.at(-1)?.seq||0;
  return <section className="diagnosis-panel" aria-label="Run diagnosis">
    <h3>Potential root causes</h3>
    <p>Local evidence checks · hypotheses require verification</p>
    <button disabled={!sid||busy} onClick={run}>{busy?'Analyzing…':'Diagnose current run'}</button>
    {error&&<p role="alert">{error}</p>}
    {report&&<div aria-live="polite"><p>{report.summary}</p>
      <small>Trace through #{report.analyzed_through_seq} · {report.status}{report.paused?' (paused)':''} {latest>report.analyzed_through_seq?'· New events available — diagnose again':''}</small>
      {report.hypotheses.map(h=><article key={h.id}><h4>{h.title}</h4>
        <p><b>Observed:</b> {h.observation}</p><p><b>Hypothesis:</b> {h.hypothesis}</p>
        <p><b>Evidence:</b> {h.evidence_seqs.map(seq=><button key={seq} onClick={()=>{onEvidence?.(seq);requestAnimationFrame(()=>document.getElementById(`trace-event-${seq}`)?.scrollIntoView({behavior:'smooth',block:'center'}));}}>#{seq}</button>)}</p>
        <b>Next checks</b><ul>{h.next_checks.map(x=><li key={x}>{x}</li>)}</ul>
        <p><b>Missing evidence:</b> {h.missing_evidence.join('; ')}</p>
      </article>)}<details><summary>Diagnostic limitations</summary><ul>{report.limitations.map(x=><li key={x}>{x}</li>)}</ul></details>
    </div>}
  </section>;
}
