# Agent Tracer installation and usage guide

Agent Tracer v0.9 is a local developer tool for inspecting instrumented agent runs.
It includes a FastAPI backend, React debugger, embeddable sidecar, Python telemetry
client, and potential root-cause diagnosis.

For run comparison, durable storage, replay, regression export, and infrastructure
experiments, see [Investigation workspace](INVESTIGATION.md).

## 1. Requirements

- Git
- Python 3.10 or newer (Python 3.12 was used for validation)
- Node.js 20 or newer, with npm
- Two terminals for development mode

No model API key is required for the included simulated agent or local diagnosis.
This project currently runs from source; there is no pip package or desktop installer.

## 2. Download and install

Run these commands in a terminal:

```bash
git clone https://github.com/hardikashah-eng/agent-tracer.git
cd agent-tracer
git switch main
python -m venv .venv
```

Activate the environment on macOS/Linux:

```bash
source .venv/bin/activate
```

Or on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

If your system uses `python3` instead of `python`, use it to create the environment.
After activation, install the backend dependencies:

```bash
python -m pip install -r requirements.txt
```

Install the frontend dependencies:

```bash
cd frontend
npm ci
cd ..
```

Already cloned? With a clean working tree, update using
`git switch main` followed by `git pull --ff-only origin main`.

## 3. Start in development mode

**Terminal 1**, repository root, with the Python environment activated:

```bash
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

**Terminal 2**, repository root:

```bash
cd frontend
npm run dev -- --host 127.0.0.1
```

Open:

| Page | URL |
| --- | --- |
| Main debugger | http://localhost:5173 |
| Sidecar | http://localhost:5173/sidecar.html |
| API documentation | http://127.0.0.1:8000/docs |

Vite forwards API and WebSocket requests to port 8000.
Keep it on port 5173; if Vite selects another port, stop the other process or
explicitly choose 5173 with `npm run dev -- --host 127.0.0.1 --port 5173 --strictPort`.
Press Ctrl+C in each terminal to stop.

## 4. Your first debugging session

1. In the main debugger, enter a task and leave **Pause at agent start** checked.
2. Click **New run**. The included agent is simulated; it does not edit your files
   or invoke a real model.
3. Select a timeline event to inspect its arguments, output, and captured state.
4. Click **Step** to advance to the next cooperative checkpoint.
5. Add a breakpoint with event kind `tool.before` and name `run_*` to stop before
   matching tool calls. Click **Resume** to continue.
6. Inspect **Context Evolution Explorer** for prompt changes and
   **Context Efficiency Analyzer** for estimated token growth.
7. Click **Diagnose current run** to see potential causes from the captured trace.

Pause takes effect at the next instrumented checkpoint; it cannot interrupt
inference or code already executing inside a tool. The normal demo may produce
no diagnostic signals. That means insufficient evidence, not a guarantee of health.

## 5. Run the compiled UI from the backend

Build before starting/restarting the backend, because static routes are
registered when the backend module loads:

```bash
cd frontend
npm run build
cd ..
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Use http://127.0.0.1:8000 for the debugger and
http://127.0.0.1:8000/sidecar.html for the sidecar. Vite is unnecessary in this mode.
Rebuild and restart after frontend changes.

## 6. Connect a separate Python agent

External integration is telemetry-only: it can report events but cannot honor
pause/step commands. Use the sidecar session picker to inspect external sessions.

Set the same token in the backend terminal and the client terminal.
Generate a token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

On macOS/Linux, set it before starting the backend:

```bash
export AGENT_TRACER_INGEST_TOKEN='paste-your-generated-token'
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

On PowerShell, the environment assignment is:

```powershell
$env:AGENT_TRACER_INGEST_TOKEN = 'paste-your-generated-token'
```

In another terminal, activate the environment, set that same token, and run
from the repository root:

```bash
python -m examples.external_agent
```

The example prints a sidecar URL. It emits sample telemetry; it does not actually
run pytest. For integration with your own application, adapt this code:

```python
import os
from backend.external_client import TelemetryClient

client = TelemetryClient(
    "http://127.0.0.1:8000",
    os.environ["AGENT_TRACER_INGEST_TOKEN"],
)
sid = client.connect()
print(f"Open http://localhost:5173/sidecar.html?session={sid}")
client.emit("agent.start", "my-harness", {"task": "Compare branch with baseline"})
client.emit("tool.before", "simulation", {
    "arguments": {"branch": "feature/example", "baseline": "main"}
})
# Execute the actual tool in your application, then emit its outcome.
client.emit("tool.error", "simulation", {
    "error": "Request timed out after 30 seconds",
    "request_id": "example-request-123",
    "node_id": "worker-01",
    "binary_version": "example-build",
    "timeout_seconds": 30,
})
```

Run this from a script in the repository root, or make the repository importable
in your own project. The client uses Python's standard library and synchronous
HTTP requests with a five-second timeout. It has no automatic retry/buffering;
handle telemetry exceptions in your application so they do not abort useful work.
In an async harness, consider `await asyncio.to_thread(client.emit, ...)` to avoid
blocking its event loop.

Emit `agent.completed` or `agent.failed` to update external session status. External controls are not supported.

## 7. Instrument a cooperative in-process harness

For pause/step, your agent and FastAPI must use the same in-memory
`backend.tracer.tracer` instance and event loop. A different process or worker
has a different session store. Integrate this coroutine into your own backend
startup/route/task lifecycle; simply starting another Python script is insufficient.

```python
from backend.tracer import tracer
from backend.context import snapshot_context

async def instrumented_agent(messages, call_model, call_tool):
    session = tracer.create()
    session.status = "running"
    try:
        snapshot_context(tracer, session, "planner", messages)
        await tracer.checkpoint(session, "llm.before", "planner", {})
        answer = await call_model(messages)
        tracer.emit(session, "llm.after", "planner", {"response": str(answer)})

        arguments = {"branch": "main"}
        await tracer.checkpoint(
            session, "tool.before", "simulation", {"arguments": arguments}
        )
        result = await call_tool(arguments)
        tracer.emit(session, "tool.after", "simulation", {"result": result})
        session.status = "completed"
        tracer.emit(session, "agent.completed", "my-agent")
    except Exception as exc:
        session.status = "failed"
        tracer.emit(session, "agent.error", "my-agent", {"error": str(exc)})
        raise
    return session.id
```

When integrating for real, emit context snapshots immediately before each model
call using the final outbound messages, stable message IDs, and `source` metadata.
Redact sensitive values before emitting them.

## 8. Use potential root-cause diagnosis

After the timeout example above, open the sidecar, select the session, and click
**Diagnose current run**. Expect a timeout hypothesis with the cited event number.

Reports include:

- Observed facts and a separate potential explanation.
- Evidence event numbers; click them to navigate to the event.
- Next checks and missing evidence.
- The last analyzed sequence number. Rerun diagnosis when new events arrive.

The endpoint is also available directly:

```bash
curl http://127.0.0.1:8000/api/sessions/SESSION_ID/diagnosis
```

Replace SESSION_ID with the ID printed by the client or shown in the UI.

| Signal | Instrumentation needed |
| --- | --- |
| Timeout/error | Explicit `error` or `exception` field, `success: false`, `status: "failed"/"error"`, or an event kind ending in `.error`/`.failed` |
| Repeated tool request | At least three `tool.before` events with the same name and identical `payload.arguments` |
| Removed instruction | Two `context.snapshot` events with stable message IDs and system/developer roles |
| Unattributed result | `tool.after`, followed by context snapshots whose messages lack matching `source` attribution |

These are local rules, not LLM analysis or confirmed RCA. Repeated requests may
be intentional; missing attribution may be an instrumentation gap. Node/build
metadata helps manual investigation but does not automatically identify a bad node.
There are no external log queries, automatic fixes, or real-model replay.

## 9. Embed the sidecar

For the compiled backend-served UI:

```html
<iframe
  title="Agent Tracer"
  src="http://127.0.0.1:8000/sidecar.html?session=SESSION_ID"
  style="width:420px;height:100vh;border:0">
</iframe>
```

Replace SESSION_ID. In development, use port 5173 instead.
Use only a trusted local host application. Remote origins, HTTPS embedding,
CORS, and framing policies require additional configuration and security work.

## 10. Verify your installation

From the repository root, with the Python environment activated:

```bash
cd frontend
npm ci
npm run build
cd ..
python -m pytest -q
```

Run the current backend test suite. Building first enables the compiled
frontend route check; without a build that test is skipped. Dependency
deprecation warnings may appear. Interactive browser behavior is not covered by
this test suite.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Module not found | Activate .venv, install requirements, and run from repository root. |
| UI is blank or JS assets return 404 | Build frontend, then restart backend; or use Vite on port 5173. |
| WebSocket disconnected | Confirm backend port 8000 is reachable; inspect browser and backend logs. |
| External ingestion returns 401 | Set the same nonempty token in the backend and client; restart backend after changing its environment. |
| Pause/step has no effect on an external agent | External integration is telemetry-only; use cooperative in-process checkpoints for controls. |
| No diagnosis findings | Emit explicit failure fields, arguments, and context/provenance; absence of findings does not mean no failure occurred. |
| Sessions disappeared | Check AGENT_TRACER_DB and working directory; traces persist, but unfinished runs become interrupted. |
| Port already in use | Stop the existing process. If changing backend ports, update Vite proxy targets and client URLs. |

## Operating limits

Keep the server bound to loopback. Only external ingestion writes require a token;
read APIs, debugger controls, and WebSockets are unauthenticated. There is no
automatic redaction, retention policy, multi-worker support. SQLite stores captured traces, not resumable task stacks.
Do not expose the server publicly or trace credentials/private payloads.
Use one backend worker. Context token counts and demo ablation results are
estimates/synthetic, not provider billing or proof of causal model influence.

