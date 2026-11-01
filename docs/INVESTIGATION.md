# Investigation workspace (v0.7–v0.9)

Install using [Getting started](GETTING_STARTED.md). Build the frontend and restart
the backend after updating. The workspace is available in the main debugger and
sidecar. Select a baseline and candidate from **Refresh runs**; **Open candidate
trace** opens an older session for inspection.

## v0.7: durable traces and comparison

SQLite defaults to `data/agent-tracer.sqlite3`, relative to the server's working
directory. Override with `AGENT_TRACER_DB` (PowerShell:
`$env:AGENT_TRACER_DB = 'C:\agent-data\traces.sqlite3'`). Use a persistent disk,
one backend process, and the same working directory across restarts. Sessions
are committed before events are published. Do not run multiple backend workers.

After restart, unfinished sessions are marked `interrupted`. Restored traces are
inspectable but cannot resume execution. Python task stacks and hardware state
are not restored. Terminal status, captured state, metadata, events, and
breakpoints are stored. There is no retention/deletion policy yet. For backup,
stop the server before copying the database (or use SQLite's backup API); copying
only the main file while WAL writes are active can lose recent events.

Tool/model boundary events receive `call_id`. Serial before/after calls are
paired automatically; concurrent calls with the same name MUST pass explicit
IDs. Use `parent_call_id` for nesting and `payload.step_id` for alignment between
runs. IDs are unique within a run, not equivalent across different runs.
`TelemetryClient.tool(name, arguments, function)` records a synchronous live tool,
including structured exception type/message. This helper is not used by replay.

**Compare runs** aligns kind + step_id (or name) + occurrence, detects sequence
insertions/deletions and payload changes, and ignores timestamps/request/call IDs.
It reports the first observed difference and metadata differences. It does not
infer semantic equivalence or claim the first difference caused the failure.
Secrets and volatile business fields are not automatically normalized/redacted.

## v0.8: recorded replay and regression fixtures

1. Select the source run as candidate (or use the active run).
2. Expand **Recorded replay and regression test**.
3. Copy a tool's call ID from its event. Set an expected response contract:

```json
[{"call_id":"simulation-1","path":["success"],"equals":true}]
```

4. **Replay recorded responses** checks the expectations against captured output.
5. **Download CI fixture** saves a JSON file. Run from the repository root:

```bash
python -m backend.replay regression.json
```

Exit status is 0 when contracts pass, 1 when they fail. A fixture from a known
failure should fail until the expectation/recording or tested application logic
is intentionally changed. This CLI checks response contracts only; by itself it
does not demonstrate a code fix or rerun a model.

**Test response intervention** replaces one recorded response, then compares
baseline and variant contract outcomes. Replacement is local, leaves the source
trace untouched, and never calls a real tool. The saved analysis session can be
inspected after restart. Contracts are exact key-path equality assertions; no
Python expressions, shell commands, network requests, or uploaded code execute.
Incomplete calls, unknown replacement IDs, mismatched tool names/arguments and
unrecorded requests fail closed with no live fallback.

To test actual application logic in pytest, inject the recorded tool interface:

```python
import json
from backend.replay import replay_workflow

def workflow(tools):
    # Replace with your application's actual logic using this injected interface.
    response = tools.call('simulation', {'scenario': 'demo-scenario'})
    return response['result']['passed']

def test_simulation_workflow():
    with open('regression.json') as stream:
        fixture = json.load(stream)
    assert replay_workflow(fixture, workflow) is True
```

The SDK verifies exact requests and full consumption. The callback is TRUSTED
application code: dependency injection is not an operating-system sandbox. All
side effects must use the supplied tool interface; code that opens its own
network connection is outside this isolation. The HTTP API never accepts Python
callbacks. No generic model/agent checkpoint replay is implemented.

## v0.9: infrastructure and controlled trials

Attach run metadata when connecting:

```python
sid = client.connect({
    'node_id': 'worker-01', 'binary_version': 'build-7',
    'bsp_version': 'bsp-2', 'scenario_id': 'scenario-42',
    'model_version': 'planner-v3',
})
client.emit('infra.metrics', 'worker', {
    'queue_wait_ms': 120, 'download_ms': 800, 'execution_ms': 4200,
})
```

Emit `agent.completed` or `agent.failed` to mark an external run terminal.
**Infrastructure cohorts** shows failure rates by dimension plus timing samples,
means, and nearest-rank p95. Only completed/failed runs enter outcome cohorts;
missing metadata and other statuses are reported. Sparse samples and scenario
assignment can confound associations; a high node failure rate is not proof that
the node is faulty. No Kubernetes/Jenkins/XTest connectors are included.

**Check matching** compares scenario/model/binary/BSP/node dimensions. It considers
one changed dimension with all five dimensions present a matching candidate,
not a verified randomized experiment.

Run actual paired experiments in YOUR harness using trusted callbacks:

```python
from backend.replay import controlled_trials

# Supply your own functions: reset environments and execute independent trials.
# Each function receives a trial index and returns a real boolean outcome.
results = controlled_trials(run_baseline, run_variant, count=30)
print(results['report'])
```

Order alternates between baseline-first and variant-first. The callbacks own
reset, matching, randomization, cost budgets, and tool safety. They can wrap
`replay_workflow` for recorded dependency isolation or your test infrastructure
for live trials; live hardware trials are not side-effect-free.

Paste real `results['pairs']` into **Analyze paired infrastructure trials** (the
UI's two rows are examples, not experimental evidence). The report records
success rates, paired delta, and a conservative 95% Hoeffding interval assuming
independent pairs. Small samples produce wide bounds. Outcomes are caller-supplied;
the server cannot verify independent execution or causal attribution. Reports and
submitted pairs are persisted as analysis sessions. Increasing trial count in the
UI does not execute more jobs.

## Try the complete workspace

Set the same ingest token in the backend and example terminal (see setup guide),
start the backend with the compiled frontend, then run:

```bash
python -m examples.investigation_demo
```

This emits TWO SIMULATED runs, one failing and one passing, with matched scenario,
model, binary and BSP but different nodes. Select them, compare, inspect cohorts,
and use `simulation-1` for the contract example above. Do not interpret the demo
as evidence about real hardware or model quality.

## API reference

- `GET /api/compare?baseline=ID&candidate=ID`
- `POST /api/sessions/ID/replay` and `/regression`: `{ "assertions": [...] }`
- `POST /api/sessions/ID/intervention`: assertions plus `call_id`, `replacement`
- `GET /api/infrastructure`
- `GET /api/experiment-plan?baseline=ID&candidate=ID`
- `POST /api/trials`: `baseline_id`, `candidate_id`, `pairs` with unique `trial_id`
  and boolean `baseline`/`variant` outcomes.

The existing `/experiments` endpoint remains the older synthetic context-ablation
lab; it is separate from recorded interventions and observed paired-trial analysis.

## Security and limits

Local trusted use only. The ingest token still protects external write endpoints
only; other routes and WebSockets remain unauthenticated. Traces/fixtures can
contain sensitive data. No automatic redaction, encryption, retention, multiworker
coordination, generic container sandbox, real-model replay, automatic remediation,
or infrastructure connectors are included. SQLite persistence does not make this
a production-hardened multiuser service. Windows-compatible stdlib SQLite is used;
Windows runtime validation still needs to be performed.
