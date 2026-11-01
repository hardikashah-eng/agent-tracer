"""Run with AGENT_TRACER_INGEST_TOKEN set on both the API and client."""
import os
import time
from backend.external_client import TelemetryClient

client = TelemetryClient('http://127.0.0.1:8000', os.environ['AGENT_TRACER_INGEST_TOKEN'])
sid = client.connect()
print('Open http://localhost:5173/sidecar.html?session=' + sid)
client.emit('agent.start', 'external-python-agent', {'task': 'Investigate failing tests'})
client.emit('llm.before', 'planner', {'model': 'example-model', 'note': 'example telemetry only'})
time.sleep(1)
client.emit('tool.before', 'pytest', {'command': 'pytest -q'})
time.sleep(1)
client.emit('tool.after', 'pytest', {'exit_code': 1, 'summary': '1 failed'})
client.emit('error.detected', 'test-failure', {'suggestion': 'inspect failed assertion'})
