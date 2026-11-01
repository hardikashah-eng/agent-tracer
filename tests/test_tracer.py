import asyncio
import pytest
from backend.tracer import Tracer

@pytest.mark.asyncio
async def test_step_and_breakpoint():
    t = Tracer(); s = t.create(); s.breakpoints.append({'kind':'tool.before','name':'run_*'})
    async def work():
        await t.checkpoint(s,'tool.before','run_tests',{'x':1})
        await t.checkpoint(s,'llm.before','model')
        t.emit(s,'done','agent')
    task = asyncio.create_task(work()); await asyncio.sleep(.02)
    assert s.paused and s.events[-1]['kind']=='debug.paused'
    t.command(s,'step'); await asyncio.sleep(.02)
    assert s.paused and s.events[-1]['kind']=='debug.paused'
    t.command(s,'resume'); await task
    assert s.events[-1]['kind']=='done'
