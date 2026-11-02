"""Emit a green and red decision with source lineage. Run: python -m examples.goal_provenance_demo"""
from backend.tracer import tracer
from backend.context import snapshot_context


def record_decision(session, call_id, passed):
    source_id = 'test-report'
    snapshot_context(tracer, session, 'planner', [{
        'id': 'tool-result', 'role': 'tool', 'content': 'Tests failed: login timeout',
        'source_id': source_id, 'source_type': 'tool', 'goal_ids': ['fix-login'],
    }], call_id=call_id, goal={'id': 'fix-login', 'text': 'Fix login tests only'})
    tracer.emit(session, 'llm.before', 'planner', {'call_id': call_id})
    tracer.emit(session, 'llm.after', 'planner', {
        'call_id': call_id, 'decision': 'Fix login' if passed else 'Rewrite billing UI',
        'supporting_source_ids': [source_id],
        'goal_alignment': {
            'goal_id': 'fix-login', 'assessor': 'demo-harness',
            'evidence_source_ids': [source_id],
            'checks': [{'name': 'Action stays within approved login scope', 'passed': passed,
                        'detail': 'Synthetic check for UI demonstration; implement real harness checks.'}],
        },
    })


if __name__ == '__main__':
    session = tracer.create()
    tracer.emit(session, 'agent.start', 'goal-demo', {
        'goal': {'id': 'fix-login', 'text': 'Fix login tests only'},
    })
    record_decision(session, 'aligned-call', True)
    record_decision(session, 'drifting-call', False)
    tracer.emit(session, 'agent.completed', 'goal-demo')
    print('Open recorded session in the sidecar or Investigation workspace:', session.id)
