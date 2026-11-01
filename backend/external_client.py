"""Minimal standard-library telemetry client for an external Python agent."""
import json
import urllib.request

class TelemetryClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.session_id = None

    def _post(self, path, payload):
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(self.base_url + path, data=data, headers={
            'Content-Type': 'application/json', 'X-Tracer-Token': self.token,
        }, method='POST')
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def connect(self, metadata=None):
        self.session_id = self._post('/api/external/sessions', {'metadata': metadata or {}})['id']
        return self.session_id

    def emit(self, kind, name, payload=None, *, call_id=None, parent_call_id=None):
        if not self.session_id:
            raise RuntimeError('Call connect() first')
        return self._post(f'/api/external/sessions/{self.session_id}/events', {
            'kind': kind, 'name': name, 'payload': payload or {},
            'call_id': call_id, 'parent_call_id': parent_call_id,
        })

    def tool(self, name, arguments, function, *, parent_call_id=None):
        """Record a live tool invocation; only call from your live harness, never replay."""
        import uuid
        cid = uuid.uuid4().hex
        self.emit('tool.before', name, {'arguments': arguments}, call_id=cid, parent_call_id=parent_call_id)
        try:
            result = function(**arguments)
        except Exception as exc:
            self.emit('tool.error', name, {'error': str(exc), 'error_type': type(exc).__name__, 'success': False}, call_id=cid)
            raise
        self.emit('tool.after', name, {'result': result, 'success': True}, call_id=cid)
        return result
