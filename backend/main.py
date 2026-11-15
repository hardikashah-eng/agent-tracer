import asyncio
import os
import secrets
from fastapi import Header
from pathlib import Path
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from .tracer import tracer
from .diagnosis import diagnose
import copy
from .agent import example_agent
from .efficiency import analyze
from .causal import provenance, experiment
from .comparison import compare
from .replay import export_fixture, evaluate, intervention
from .infrastructure import correlate, experiment_plan
from .trials import summarize
from .advisor import analyze_sessions
from .advisor.judge import judge_from_env, judge_status
from .advisor.ledger import SQLiteLedger, evaluate_ledger, make_entry
from .advisor.verify import evaluate_plan
from fastapi import Query
from fastapi.responses import JSONResponse
import json


def load_catalog():
    """Optional tool catalog for the advisor (ownership, transport, definition tokens)."""
    path = os.environ.get('AGENT_TRACER_TOOL_CATALOG')
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f'Could not read AGENT_TRACER_TOOL_CATALOG: {exc}') from exc

app = FastAPI(title='Agent Tracer', version='1.1.0')
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

class SessionMetadata(BaseModel):
    metadata: dict[str, str] = Field(default_factory=dict)

class StartRequest(SessionMetadata):
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
    call_id: str | None = None
    parent_call_id: str | None = None

class Command(BaseModel):
    action: str

def get_session(sid):
    s = tracer.sessions.get(sid)
    if not s: raise HTTPException(404, 'Session not found')
    return s

def snapshot(s):
    return {'id': s.id, 'status': s.status, 'paused': s.paused, 'events': s.events, 'breakpoints': s.breakpoints, 'state': s.state, 'metadata': s.metadata, 'restored': s.restored}

@app.post('/api/sessions')
async def create(req: StartRequest):
    s = tracer.create(req.metadata)
    if req.start_paused: s.breakpoints.append({'kind': 'agent.start', 'name': '*', 'enabled': True, '_temporary': True})
    s.task = asyncio.create_task(example_agent(s, req.task))
    return snapshot(s)

@app.post('/api/external/sessions')
async def create_external(req: SessionMetadata | None = None, x_tracer_token: str | None = Header(default=None)):
    require_ingest_token(x_tracer_token)
    s = tracer.create(req.metadata if req else {})
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
    if s.restored: raise HTTPException(409, 'Create a new external session after restart')
    if len(str(req.payload)) > 100000:
        raise HTTPException(413, 'Event payload too large')
    payload = dict(req.payload)
    if req.call_id: payload['call_id'] = req.call_id
    if req.parent_call_id: payload['parent_call_id'] = req.parent_call_id
    return tracer.emit(s, req.kind, req.name, payload)

@app.get('/api/sessions')
def list_sessions():
    return [{'id': s.id, 'status': s.status, 'events': len(s.events), 'metadata': s.metadata, 'restored': s.restored} for s in tracer.sessions.values()]

@app.get('/api/sessions/{sid}/diagnosis')
async def diagnosis(sid: str):
    s = get_session(sid)
    return diagnose(copy.deepcopy(s.events), s.status, s.paused)

@app.get('/api/sessions/{sid}/efficiency')
def efficiency(sid: str):
    return analyze(get_session(sid).events)


def _advisor_body(s):
    return {'id': s.id, 'status': s.status, 'events': copy.deepcopy(s.events), 'metadata': s.metadata}

ledger_store = SQLiteLedger(os.environ.get('AGENT_TRACER_DB', 'data/agent-tracer.sqlite3'))
judge_cache: dict = {}

def _chosen_sessions(session_id):
    if session_id:
        return [get_session(sid) for sid in session_id]
    return [s for s in tracer.sessions.values() if not (s.metadata or {}).get('analysis')]

def _run_advisor(chosen, use_judge=False, plans=3):
    judge = None
    if use_judge:
        try:
            judge = judge_from_env()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if judge is None:
            raise HTTPException(400, 'LLM judge not configured; set AGENT_TRACER_JUDGE and related variables')
    return analyze_sessions([_advisor_body(s) for s in chosen], load_catalog(), judge=judge, judge_cache=judge_cache,
                            plans=plans, ledger=ledger_store.list())

@app.get('/api/sessions/{sid}/advisor')
def session_advisor(sid: str, judge: bool = False):
    return _run_advisor([get_session(sid)], judge)

@app.get('/api/advisor')
def advisor(session_id: list[str] | None = Query(default=None), judge: bool = False, plans: int = Query(default=3, ge=0, le=20)):
    return _run_advisor(_chosen_sessions(session_id), judge, plans)

@app.get('/api/advisor/judge')
def advisor_judge_status():
    return judge_status()

@app.get('/api/advisor/verification')
def advisor_verification(session_id: list[str] | None = Query(default=None), top: int = Query(default=3, ge=1, le=20)):
    return _run_advisor(_chosen_sessions(session_id), plans=top)['verification_plans']

class PlanEvaluation(BaseModel):
    plan: dict

@app.post('/api/advisor/verification/evaluate')
def advisor_evaluate(req: PlanEvaluation):
    plan = req.plan
    if not isinstance(plan.get('experiment_id'), str) or not isinstance(plan.get('primary_metric'), dict) \
            or not isinstance(plan.get('design'), dict):
        raise HTTPException(422, 'Supply a plan produced by /api/advisor/verification')
    report = analyze_sessions([_advisor_body(s) for s in _chosen_sessions(None)], load_catalog(), plans=0)
    return evaluate_plan(plan, report['session_metrics'])

class LedgerRequest(BaseModel):
    recommendation_id: str
    applied_at: float | None = None
    scope: dict[str, str] = Field(default_factory=dict)
    note: str = Field(default='', max_length=2000)
    plan_id: str | None = None

@app.get('/api/advisor/ledger')
def advisor_ledger():
    report = analyze_sessions([_advisor_body(s) for s in _chosen_sessions(None)], load_catalog(), plans=0)
    return evaluate_ledger(ledger_store.list(), report)

@app.post('/api/advisor/ledger')
def advisor_ledger_add(req: LedgerRequest):
    report = analyze_sessions([_advisor_body(s) for s in _chosen_sessions(None)], load_catalog(), plans=0)
    try:
        entry = make_entry(report, req.recommendation_id, applied_at=req.applied_at, scope=req.scope,
                           note=req.note, plan_id=req.plan_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return ledger_store.add(entry)

@app.delete('/api/advisor/ledger/{entry_id}')
def advisor_ledger_delete(entry_id: str):
    if not ledger_store.delete(entry_id):
        raise HTTPException(404, 'Ledger entry not found')
    return {'deleted': entry_id}


@app.get('/api/sessions/{sid}/provenance')
def context_provenance(sid: str):
    return provenance(get_session(sid).events)

@app.post('/api/sessions/{sid}/experiments')
async def run_experiment(sid: str, req: AblationRequest):
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
async def add_breakpoint(sid: str, bp: Breakpoint):
    s = get_session(sid); s.breakpoints.append(bp.model_dump()); tracer.persist(s); return snapshot(s)

@app.delete('/api/sessions/{sid}/breakpoints/{index}')
async def remove_breakpoint(sid: str, index: int):
    s = get_session(sid)
    if index < 0 or index >= len(s.breakpoints): raise HTTPException(404, 'Breakpoint not found')
    s.breakpoints.pop(index); tracer.persist(s); return snapshot(s)

@app.post('/api/sessions/{sid}/command')
async def command(sid: str, cmd: Command):
    s = get_session(sid)
    if s.restored: raise HTTPException(409, 'Restored traces are read-only; start a new run')
    if cmd.action == 'cancel':
        if s.task and not s.task.done(): s.task.cancel()
    elif cmd.action in ('pause', 'resume', 'step'):
        if s.status in ('completed', 'cancelled', 'failed', 'interrupted'): raise HTTPException(409, 'Session already finished')
        tracer.command(s, cmd.action)
    else: raise HTTPException(400, 'Unknown action')
    tracer.persist(s)
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

class Assertion(BaseModel):
    call_id: str
    path: list[str] = Field(default_factory=list, max_length=20)
    equals: object

class ReplayRequest(BaseModel):
    assertions: list[Assertion] = Field(min_length=1, max_length=100)

class InterventionRequest(ReplayRequest):
    call_id: str
    replacement: dict

class Pair(BaseModel):
    trial_id: str
    baseline: StrictBool
    variant: StrictBool

class TrialRequest(BaseModel):
    baseline_id: str
    candidate_id: str
    pairs: list[Pair] = Field(min_length=2, max_length=1000)

@app.get('/api/compare')
async def compare_runs(baseline: str, candidate: str):
    return compare(get_session(baseline), get_session(candidate))

@app.get('/api/infrastructure')
async def infrastructure():
    return correlate(list(tracer.sessions.values()))

@app.get('/api/experiment-plan')
async def plan(baseline: str, candidate: str):
    return experiment_plan(get_session(baseline), get_session(candidate))

@app.post('/api/trials')
async def trials(req: TrialRequest):
    a, b = get_session(req.baseline_id), get_session(req.candidate_id)
    report = summarize([p.model_dump() for p in req.pairs])
    s = tracer.create({'analysis': 'paired-trials'})
    s.status = 'completed'
    tracer.emit(s, 'analysis.completed', 'paired-trials', {'baseline_id': a.id, 'candidate_id': b.id,
                'plan': experiment_plan(a, b), 'report': report, 'pairs': [p.model_dump() for p in req.pairs]})
    return {'analysis_session_id': s.id, 'report': report}

def fixture_for(sid, req):
    try:
        return export_fixture(get_session(sid), [a.model_dump() for a in req.assertions])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@app.post('/api/sessions/{sid}/regression')
async def regression(sid: str, req: ReplayRequest):
    return JSONResponse(fixture_for(sid, req), headers={'Content-Disposition': 'attachment; filename="regression.json"'})

@app.post('/api/sessions/{sid}/replay')
async def replay(sid: str, req: ReplayRequest):
    return evaluate(fixture_for(sid, req))

@app.post('/api/sessions/{sid}/intervention')
async def intervene(sid: str, req: InterventionRequest):
    fixture = fixture_for(sid, req)
    try:
        report = intervention(fixture, req.call_id, req.replacement)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    analysis = tracer.create({'analysis': 'recorded-intervention', 'source_session_id': sid})
    analysis.status = 'completed'
    tracer.emit(analysis, 'analysis.completed', 'recorded-intervention', {'report': report})
    return {**report, 'analysis_session_id': analysis.id}

DIST = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'
@app.get('/')
def index():
    path = DIST / 'index.html'
    if path.exists(): return FileResponse(path)
    return {'message': 'Frontend not built; run npm run dev in frontend'}

# Register after API routes so the compiled multi-page UI serves its assets.
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
