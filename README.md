# agent-tracer
Tooling to debug on going agent sessions 

# Agent Tracer — live agent debugger

A working FastAPI + WebSocket + React debugger. **Python 3.10+ and Node 20+**.

## Installation and usage guide

See [Getting started](docs/GETTING_STARTED.md) for setup on macOS/Linux/Windows,
a debugging walkthrough, Python integration, sidecar embedding, potential RCA,
and troubleshooting.

## Investigation workspace v0.9

[Run comparison, durable traces, recorded replay, regression export, and infrastructure trials](docs/INVESTIGATION.md) are now available. See the guide for execution boundaries and examples.

## Start

Terminal 1 (from repository root):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

Terminal 2:

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. Click **New run** with *Pause at agent start* checked, then use **Step** or **Resume**. Add `tool.before` breakpoints to stop before tools. The API is documented at http://localhost:8000/docs.

To serve the compiled UI directly from FastAPI: `cd frontend && npm run build`, then visit http://localhost:8000.

## Integrate with your own harness

```python
from backend.tracer import tracer

s = tracer.create()
# In your async agent loop:
await tracer.checkpoint(s, 'llm.before', 'my-model', {'messages': messages})
response = await call_model(messages)
tracer.emit(s, 'llm.after', 'my-model', {'response': response})
await tracer.checkpoint(s, 'tool.before', tool_name, {'arguments': arguments})
result = await call_tool(tool_name, arguments)
tracer.emit(s, 'tool.after', tool_name, {'result': result})
```

This sample assumes the harness runs in the **same Python process and event loop** as the API. For a separate process, use the token-protected external telemetry API described below; external pause/step control is not implemented. See `backend/agent.py` for a runnable example and [the integration guide](docs/GETTING_STARTED.md#6-connect-a-separate-python-agent) for setup.

## Debug semantics

- `pause`: installs a temporary breakpoint; stops at the **next** cooperative checkpoint, not mid-inference.
- `step`: when paused, resumes until the **next** checkpoint.
- `resume`: clears the step budget and temporary pause breakpoint, but preserves user-defined breakpoints.
- `cancel`: cancels the sample task; in a production harness, cancellation behavior depends on the tools involved.
- Breakpoints match `kind` and `name` using shell globs; optional `contains` matches a substring in checkpoint payload.
- WebSocket sends an initial snapshot and subsequent events; clients de-duplicate by sequence number.

## Limits and security

Development prototype, not production-hardened: SQLite-backed captured session storage, no authentication, no redaction, no trace retention policy, no distributed workers, and no deterministic model replay. The example agent deliberately simulates LLM/tool calls and reports synthetic token counts. Never expose the server publicly or send secrets in trace payloads. The `step` control is checkpoint-level stepping, not Python source-line stepping or stepping inside model inference. Captured sessions survive restart; unfinished runs become interrupted and cannot resume.

## Context Evolution Explorer

The React UI includes a stacked context-growth chart and before/after message diff. The backend emits `context.snapshot` events immediately before sample model calls, with message IDs, roles, category, provenance, approximate token counts and a separate copy of stored agent state. Select two snapshots to compare added/removed/modified messages.

To integrate a real model call, import `snapshot_context` from `backend.context` and call `snapshot_context(tracer, session, 'model-name', actual_outbound_messages, agent_state)` **after constructing the final prompt and before the provider API call**. Provide stable message IDs, optional `category` and `source` fields for accurate diffs. The demo uses estimated token counts; replace with the model tokenizer/provider usage for precise numbers. Do not capture secrets or sensitive user data without appropriate redaction, access control and retention rules.

## Context Efficiency Analyzer (v0.3)

The React UI now includes a **Context Efficiency Analyzer** beneath the Context Evolution Explorer. It updates as WebSocket events arrive, showing estimated prompt tokens per model call, tool-result share, cumulative prompt size, configurable oversized-tool warnings, disappeared messages, overlapping text, and tool results without attributed inclusion in subsequent prompts. The backend exposes `GET /api/sessions/{id}/efficiency` for structured analysis.

These are **heuristics**, not provider-billed token counts or proof of wasted context. The unused-result check depends on correct `source` provenance, and removed messages may represent intentional compaction. The example agent remains simulated. The API has no authentication and should be used only in a trusted local development environment; context snapshots may contain sensitive prompts and tool output.


## Context Influence Lab (v0.4)

This release adds `GET /api/sessions/{id}/provenance`, which builds an **observed-in-prompt** bipartite graph from captured snapshots, and `POST /api/sessions/{id}/experiments` with `{ "snapshot_seq": 5, "remove_ids": ["tool-0"], "trials": 30, "seed": 7 }`. The React panel lists provenance and runs paired sample ablations. Experiment events are streamed over the existing WebSocket.

**Critical limitation:** the ablation experiment uses a documented deterministic **surrogate policy** with synthetic outcomes, not the actual agent or model. It demonstrates the experiment workflow, not causal influence in a real LLM. To support real replay, implement provider/tool recording, forkable state checkpoints, side-effect isolation, and repeated matched model invocations. Provenance edges show prompt inclusion only. IDs/hashes help distinguish modified content; the graph is in-memory and has no access controls.


## v0.5 — Live sidecar and external telemetry

Open **http://localhost:5173/sidecar.html** to use a compact resizable developer panel. Embed it beside your harness using an iframe (example below). The UI offers live event graph, event inspection, context growth, pause/step/resume for cooperative sessions, and session selection.

```html
<div style="display:grid;grid-template-columns:minmax(0,1fr) 400px;height:100vh">
  <main id="your-agent-ui">Your harness UI</main>
  <iframe title="Agent Tracer" src="http://localhost:5173/sidecar.html" style="width:100%;height:100%;border:0"></iframe>
</div>
```

External Python harnesses can stream events through the token-protected ingestion API:

```bash
export AGENT_TRACER_INGEST_TOKEN='replace-with-a-long-random-local-token'
uvicorn backend.main:app --port 8000
# separate terminal, same token and repository root
python -m examples.external_agent
```

The standalone Python client is in `backend/external_client.py`. Create an external session with `POST /api/external/sessions`, then send `{kind,name,payload}` to `POST /api/external/sessions/{id}/events` using `X-Tracer-Token`. WebSocket subscribers receive events live. External ingestion is **telemetry-only**; it cannot pause or step the remote harness. Only cooperative in-process harnesses can honor debugger controls.

**Security:** The ingest token protects only the two external write endpoints. The other API and WebSocket endpoints remain unauthenticated, so this is a local trusted-development prototype; do not expose it on a shared network or embed it on untrusted origins. Redact sensitive payloads before sending. CORS and iframe framing need production hardening.

## v0.6 — Potential root-cause diagnosis

Click **Diagnose current run** in the debugger or sidecar, or call
`GET /api/sessions/{id}/diagnosis`. This read-only endpoint works for running,
paused, completed, and failed sessions. Reports describe the captured trace
through `analyzed_through_seq`; rerun diagnosis when new events arrive.

The local rule engine ranks explicit errors/timeouts first, repeated identical
tool requests and removed system/developer messages next, and unattributed
tool results last. Each hypothesis includes observations, event references,
next checks, and missing evidence. Evidence buttons scroll to captured events.
No signals means insufficient evidence, not a healthy-run certification.

Instrument `tool.before` with `payload.arguments`, explicit failures with
`payload.error`, `payload.exception`, `payload.success=false`, or
`payload.status=failed/error`; emit context snapshots with stable message IDs
and `source` attribution. Include node ID, binary/BSP version, request IDs,
and timeout settings in error payloads for manual infrastructure investigation.
These metadata do not automatically prove a node or binary caused a failure.

This release uses deterministic local rules only: no LLM, external log queries,
automatic remediation, or real model replay. Confidence is labeled heuristic,
never a calibrated probability. Repeated calls can be intentional; removed
instructions can be replaced; missing attribution does not prove an omitted
result. The existing local-only API security limitations still apply.

Validation: `python -m pytest -q`; `cd frontend && npm ci && npm run build`.




### Execution flame chart

Both the main debugger and sidecar include an interactive flame chart. Call bars use
actual start/end timestamps; explicit `parent_call_id` links determine nesting, and
overlapping calls get separate lanes. Click a bar to inspect inputs and outputs or
select its start/end event in the existing inspector. Zoom and search help with long traces.
Errors are marked; incomplete calls extend only to the last recorded event.

The measure selector also supports reported tokens (`payload.tokens` or `payload.usage`)
and USD cost (`payload.cost_usd` on the completion event). These views use metric widths,
not elapsed time. Missing measurements remain unavailable, rather than being estimated.
The timing summary reports observed calls and does not infer root cause or a critical path.
Unmatched events, including debug pauses and ambiguous legacy completions, remain
accessible in the markers list. For concurrent calls with the same name, supply unique
`call_id` values on both boundaries; supply `parent_call_id` on the start event for nesting.
No automatic retry relationship or dependency inference is performed.

Run chart layout tests with `cd frontend && npm test`.


### Live and recorded operation insights

Click any flame-chart call to open Overview, Tokens & cost, Content, Context changes,
and Decision evidence. The drawer updates when new events arrive and provides the
same analysis when opening a recorded candidate trace in Investigation workspace
(or selecting a saved session in the sidecar). No model or tool execution is required
for recorded analysis.

Usage supports `tokens`/`usage`, input/output aliases, provider total tokens, cached
input details and reasoning output details. Cached/reasoning counts are subsets.
USD cost is read from `cost_usd`; absent data is never priced or estimated.
Explicit plans, decisions, explanations, rationale, reasoning summaries and validation
fields are shown with source event links. Hidden chain-of-thought is not exposed.
Prompt snapshots linked by call ID are preferred; legacy nearest-preceding snapshots
are labeled unverified. Context changes compare with the previous recorded snapshot,
which may belong to another concurrent call. Character-based context contributions
are labeled estimates and are separate from reported usage.


### Interactive UI sample

Try the [standalone flame-chart and insights sample](docs/samples/flame-insights.html).
On GitHub, use **Download raw file**, then open the saved HTML in your browser.
It works offline without installing dependencies or running the backend.

Click operation bars, switch elapsed-time/token/cost views, and explore the five
insight tabs. All data is synthetic. The live/recorded selector illustrates viewing
modes and does not connect to a running agent. This is an illustrative sample, not
a screenshot or a replacement for the full debugger.

### Goal alignment and source provenance

Flame-chart calls now have green/red/amber alignment indicators and a **Goal &
provenance** detail tab in both live and recorded views. The first `agent.start`
event anchors the original goal: `payload.goal = {"id":"g1","text":"Fix login tests"}`.
Goal changes require a new run; later payloads cannot overwrite that anchor.

Capture source metadata on each prompt message: `source_id`, `source_type`,
`source_event_seq`, and `goal_ids`. `snapshot_context(..., call_id="call-1", goal=...)`
retains these fields and adds a SHA-256 content version. Pass the same call ID on
both LLM boundaries. Completion events can include `supporting_source_ids` and:

```json
{"goal_alignment":{"goal_id":"g1","assessor":"scope-validator",
"evidence_source_ids":["test-report"],"checks":[
{"name":"Only approved files modified","passed":true}]}}
```

Green means all supplied checks passed with an explicit original-goal match and
linked, resolvable source evidence. Red signals failed reported checks or a
mismatched goal ID. Amber marks incomplete/unverified evidence; gray means no
assessment. Colors describe reported evidence, not an independent semantic judge
or a security guarantee. Existing traces without this instrumentation remain gray.
A token-size composition percentage is not causal influence. The current version
does not run an LLM drift judge, infer hidden reasoning, or automatically block actions.

Run `python -m examples.goal_provenance_demo`, then open its recorded session to
see synthetic green/red examples. Implement actual checks in your harness before
using alignment signals operationally. Browser interaction testing remains pending.
