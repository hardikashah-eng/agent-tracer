"""Create two clearly simulated runs for the investigation workspace."""
import os
from backend.external_client import TelemetryClient

for success, node in ((False, 'demo-node-a'), (True, 'demo-node-b')):
    client = TelemetryClient('http://127.0.0.1:8000', os.environ['AGENT_TRACER_INGEST_TOKEN'])
    sid = client.connect({'node_id': node, 'binary_version': 'demo-build-1',
                          'bsp_version': 'demo-bsp-1', 'scenario_id': 'demo-scenario', 'model_version': 'no-real-model'})
    client.emit('agent.start', 'demo', {'simulation': True})
    client.emit('tool.before', 'simulation', {'arguments': {'scenario': 'demo-scenario'}, 'step_id':'evaluate'}, call_id='simulation-1')
    client.emit('tool.after', 'simulation', {'success': success, 'result': {'passed': success}, 'step_id':'evaluate'}, call_id='simulation-1')
    client.emit('infra.metrics', 'worker', {'queue_wait_ms': 100 if success else 900, 'execution_ms': 500})
    client.emit('agent.completed' if success else 'agent.failed', 'demo', {'simulation':True})
    print(('Candidate success: ' if success else 'Baseline failure: ')+sid)
print('Open http://127.0.0.1:8000 and select both runs in Investigation workspace.')
print('Assertion: [{"call_id":"simulation-1","path":["success"],"equals":true}]')
