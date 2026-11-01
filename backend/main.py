import asyncio
import os
import secrets
from fastapi import Header
from pathlib import Path
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .tracer import tracer
from .agent import example_agent
from .efficiency import analyze
from .causal import provenance, experiment

app = FastAPI(title='Agent Tracer', version='0.5.0')
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

class StartRequest(BaseModel):
    task: str = 'Implement a simple function and run tests'
    start_paused: bool = False

class Breakpoint(BaseModel):
    kind: str = '*'
    name: str = '*'
    contains: str = ''
    enabled: bool = True

class AblationRequest(BaseModel):
    snapshot_seq: int
    remove_ids: list[str] = Field(default_factory=list)
    trials: int = Field(default=30, ge=1, le=500)
    seed: int = 7

class IngestRequest(BaseModel):
    kind: str
    name: str
    payload: dict = Field(default_factory=dict)

class Command(BaseModel):
    action: str

def get_session(sid):
    s = tracer.sessions.get(sid)
    if not s: raise HTTPException(404, 'Session not found')
    return s

def snapshot(s):
    return {'id': s.id, 'status': s.status, 'paused': s.paused, 'events': s.events, 'breakpoints': s.breakpoints, 'state': s.state}

@app.post('/api/sessions')
async def create(req: StartRequest):
    s = tracer.create()
    if req.start_paused: s.breakpoints.append({'kind': 'agent.start', 'name': '*', 'enabled': True, '_temporary': True})
    s.task = asyncio.create_task(example_agent(s, req.task))
    return snapshot(s)

@app.post('/api/external/sessions')
async def create_external(x_tracer_token: str | None = Header(default=None)):
    require_ingest_token(x_tracer_token)
    s = tracer.create()
    s.status = 'running'
    tracer.emit(s, 'agent.connected', 'external-harness', {'mode': 'telemetry-only'})
    return {'id': s.id, 'mode': 'telemetry-only'}

def require_ingest_token(token):
    expected = os.environ.get('AGENT_TRACER_INGEST_TOKEN')
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(401, 'Configure AGENT_TRACER_INGEST_TOKEN and supply X-Tracer-Token')

@app.post('/api/external/sessions/{sid}/events')
async def ingest_event(sid: str, req: IngestRequest, x_tracer_token: str | None = Header(default=None)):
    require_ingest_token(x_tracer_token)
    s = get_session(sid)
    if len(str(req.payload)) > 100000:
        raise HTTPException(413, 'Event payload too large')
    return tracer.emit(s, req.kind, req.name, req.payload)

@app.get('/api/sessions')
def list_sessions():
    return [{'id': s.id, 'status': s.status, 'events': len(s.events)} for s in tracer.sessions.values()]

@app.get('/api/sessions/{sid}/efficiency')
def efficiency(sid: str):
    return analyze(get_session(sid).events)


@app.get('/api/sessions/{sid}/provenance')
def context_provenance(sid: str):
    return provenance(get_session(sid).events)

@app.post('/api/sessions/{sid}/experiments')
def run_experiment(sid: str, req: AblationRequest):
    s = get_session(sid)
    selected = next((e for e in s.events if e['kind'] == 'context.snapshot' and e['seq'] == req.snapshot_seq), None)
    if not selected: raise HTTPException(404, 'Context snapshot not found')
    try: result = experiment(selected, req.remove_ids, req.trials, req.seed)
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    tracer.emit(s, 'experiment.completed', 'context-ablation', result)
    return result

@app.get('/api/sessions/{sid}')
def session(sid: str): return snapshot(get_session(sid))

@app.post('/api/sessions/{sid}/breakpoints')
def add_breakpoint(sid: str, bp: Breakpoint):
    s = get_session(sid); s.breakpoints.append(bp.model_dump()); return snapshot(s)

@app.delete('/api/sessions/{sid}/breakpoints/{index}')
def remove_breakpoint(sid: str, index: int):
    s = get_session(sid)
    if index < 0 or index >= len(s.breakpoints): raise HTTPException(404, 'Breakpoint not found')
    s.breakpoints.pop(index); return snapshot(s)

@app.post('/api/sessions/{sid}/command')
def command(sid: str, cmd: Command):
    s = get_session(sid)
    if cmd.action == 'cancel':
        if s.task and not s.task.done(): s.task.cancel()
    elif cmd.action in ('pause', 'resume', 'step'):
        if s.status in ('completed', 'cancelled', 'failed'): raise HTTPException(409, 'Session already finished')
        tracer.command(s, cmd.action)
    else: raise HTTPException(400, 'Unknown action')
    return snapshot(s)

@app.websocket('/ws/sessions/{sid}')
async def ws(websocket: WebSocket, sid: str):
    s = tracer.sessions.get(sid)
    if not s:
        await websocket.close(code=4404); return
    await websocket.accept()
    q = asyncio.Queue(maxsize=2048); s.subscribers.add(q)
    try:
        await websocket.send_json({'type': 'snapshot', 'data': snapshot(s)})
        while True:
            ev = await q.get()
            await websocket.send_json({'type': 'event', 'data': ev, 'status': s.status, 'paused': s.paused})
    except (WebSocketDisconnect, RuntimeError): pass
    finally: s.subscribers.discard(q)

DIST = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'
@app.get('/')
def index():
    path = DIST / 'index.html'
    if path.exists(): return FileResponse(path)
    return {'message': 'Frontend not built; run npm run dev in frontend'}

# Register after API routes so the compiled multi-page UI serves its assets.
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
