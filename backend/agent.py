import asyncio
from .tracer import tracer, Session
from .context import snapshot_context

async def example_agent(s: Session, task: str):
    """Deterministic sample agent. Replace model_call/tool_call with real integrations."""
    s.status = 'running'
    try:
        s.state = {'task': task, 'plan': [], 'files': {}, 'iteration': 0}
        messages = [{'id':'system-0','role':'system','content':'You are a coding assistant. Inspect, implement, and test.'}, {'id':'user-0','role':'user','content':task}]
        await tracer.checkpoint(s, 'agent.start', 'example-agent', {'task': task})
        await asyncio.sleep(.25)
        snapshot_context(tracer, s, 'planner', messages, s.state)
        await tracer.checkpoint(s, 'llm.before', 'planner', {'messages': messages})
        await asyncio.sleep(.4)
        s.state['plan'] = ['inspect workspace', 'write implementation', 'run tests']
        tracer.emit(s, 'llm.after', 'planner', {'output': s.state['plan'], 'tokens': {'input': 42, 'output': 28}})
        messages.append({'id':'assistant-plan','role':'assistant','content':'Plan: '+', '.join(s.state['plan'])})
        for i, (tool, args) in enumerate([('list_files', {'path': '.'}), ('write_file', {'path': 'solution.py', 'content': 'def answer(): return 42'}), ('run_tests', {'command': 'pytest -q'})]):
            s.state['iteration'] = i + 1
            await tracer.checkpoint(s, 'tool.before', tool, {'arguments': args, 'state': s.state})
            await asyncio.sleep(.65)
            if tool == 'list_files': result = ['README.md', 'tests/', 'traceback: timeout in API client']
            elif tool == 'write_file':
                s.state['files']['solution.py'] = args['content']; result = {'written': True}
            else: result = {'passed': 3, 'failed': 0}
            tracer.emit(s, 'tool.after', tool, {'result': result, 'state': s.state})
            messages.append({'id':f'tool-{i}','role':'tool','source':tool,'content':{'name':tool,'result':result}})
            snapshot_context(tracer, s, f'reasoner-{i+1}', messages, s.state, provenance=tool)
            await tracer.checkpoint(s, 'llm.before', 'reasoner', {'tool_result': result})
            await asyncio.sleep(.35)
            tracer.emit(s, 'llm.after', 'reasoner', {'decision': 'continue' if i < 2 else 'finish', 'tokens': {'input': 110 + i * 20, 'output': 34}})
            messages.append({'id':f'assistant-{i}','role':'assistant','content':'Continue to next step.' if i < 2 else 'All tests passed; finish.'})
        await tracer.checkpoint(s, 'agent.before_finish', 'example-agent', {'state': s.state})
        s.status = 'completed'
        tracer.emit(s, 'agent.completed', 'example-agent', {'answer': 'Implementation completed; 3 tests passed', 'state': s.state})
    except asyncio.CancelledError:
        s.status = 'cancelled'; tracer.emit(s, 'agent.cancelled', 'example-agent'); raise
    except Exception as e:
        s.status = 'failed'; tracer.emit(s, 'agent.error', 'example-agent', {'error': str(e)})
