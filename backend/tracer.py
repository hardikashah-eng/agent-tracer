import asyncio
import copy
import fnmatch
import time
import uuid
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


class Tracer:
    def __init__(self):
        self.sessions: dict[str, Session] = {}

    def create(self):
        s = Session(id=uuid.uuid4().hex[:12]); self.sessions[s.id] = s
        return s

    def emit(self, s: Session, kind: str, name: str, payload: dict | None = None):
        s.seq += 1
        event = {'seq': s.seq, 'ts': time.time(), 'kind': kind, 'name': name, 'payload': copy.deepcopy(payload or {})}
        s.events.append(event)
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

tracer = Tracer()
