import asyncio
import copy
import fnmatch
import time
import uuid
import os
from threading import RLock
from .storage import SQLiteStore
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    id: str
    status: str = 'created'
    events: list = field(default_factory=list)
    breakpoints: list = field(default_factory=list)
    seq: int = 0
    step_budget: int | None = None
    paused: bool = False
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: set = field(default_factory=set)
    task: asyncio.Task | None = None
    state: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    restored: bool = False
    pending_calls: dict = field(default_factory=dict)


class Tracer:
    def __init__(self, store=None):
        self.store = store
        self.lock = RLock()
        self.sessions: dict[str, Session] = {}
        if store:
            for body in store.load():
                s = Session(**body, restored=True)
                if s.status not in ('completed', 'failed', 'cancelled'):
                    s.status = 'interrupted'
                self.sessions[s.id] = s
                self.persist(s)

    def persist(self, s):
        if self.store:
            self.store.save(s)

    def create(self, metadata=None):
        s = Session(id=uuid.uuid4().hex, metadata=copy.deepcopy(metadata or {})); self.persist(s); self.sessions[s.id] = s
        return s

    def emit(self, s: Session, kind: str, name: str, payload: dict | None = None):
        with self.lock:
            payload = copy.deepcopy(payload or {})
            old_pending = copy.deepcopy(s.pending_calls)
            call_id = payload.get('call_id')
            if kind in ('tool.before', 'llm.before'):
                call_id = call_id or uuid.uuid4().hex
                s.pending_calls.setdefault((kind.split('.')[0], name), []).append(call_id)
            elif kind in ('tool.after', 'tool.error', 'llm.after', 'llm.error'):
                pending = s.pending_calls.get((kind.split('.')[0], name), [])
                if call_id and call_id in pending:
                    pending.remove(call_id)
                elif not call_id and len(pending) == 1:
                    call_id = pending.pop()
            event = {'seq': s.seq + 1, 'ts': time.time(), 'kind': kind, 'name': name,
                     'call_id': call_id, 'parent_call_id': payload.get('parent_call_id'), 'payload': payload}
            if kind.endswith(('.error', '.failed')):
                event['error'] = {'type': payload.get('error_type', 'unspecified'), 'message': str(payload.get('error', payload.get('exception', 'Failure reported'))), 'retryable': payload.get('retryable')}
            old_status = s.status
            if kind in ('agent.completed', 'agent.failed', 'agent.error', 'agent.cancelled'):
                s.status = {'agent.error': 'failed'}.get(kind, kind.split('.')[1])
            s.events.append(event); s.seq += 1
            try:
                self.persist(s)
            except Exception:
                s.events.pop(); s.seq -= 1; s.status = old_status; s.pending_calls = old_pending
                raise
        for q in tuple(s.subscribers):
            try: q.put_nowait(event)
            except asyncio.QueueFull: s.subscribers.discard(q)
        return event

    def matches(self, s, kind, name, payload):
        return any(bp.get('enabled', True) and fnmatch.fnmatch(kind, bp.get('kind', '*')) and fnmatch.fnmatch(name, bp.get('name', '*')) and (not bp.get('contains') or bp['contains'].lower() in str(payload).lower()) for bp in s.breakpoints)

    async def checkpoint(self, s: Session, kind: str, name: str, payload: dict | None = None):
        payload = payload or {}
        self.emit(s, kind, name, payload)
        hit = self.matches(s, kind, name, payload)
        if s.step_budget is not None:
            if s.step_budget <= 0: hit = True
            else: s.step_budget -= 1
        if not hit: return
        s.paused = True; s.status = 'paused'; s.resume_event.clear()
        self.emit(s, 'debug.paused', name, {'reason': 'breakpoint' if self.matches(s, kind, name, payload) else 'step', 'at': kind})
        await s.resume_event.wait()
        s.paused = False; s.status = 'running'
        self.emit(s, 'debug.resumed', name)

    def command(self, s: Session, action: str):
        if action == 'pause':
            s.breakpoints.append({'kind': '*', 'name': '*', 'enabled': True, '_temporary': True})
        elif action in ('resume', 'step'):
            s.breakpoints = [b for b in s.breakpoints if not b.get('_temporary')]
            s.step_budget = 0 if action == 'step' else None
            s.resume_event.set()
        else: raise ValueError('Unknown command')

tracer = Tracer(SQLiteStore(os.environ.get('AGENT_TRACER_DB', 'data/agent-tracer.sqlite3')))
