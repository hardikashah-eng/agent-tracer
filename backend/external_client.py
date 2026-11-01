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

    def connect(self):
        self.session_id = self._post('/api/external/sessions', {})['id']
        return self.session_id

    def emit(self, kind, name, payload=None):
        if not self.session_id:
            raise RuntimeError('Call connect() first')
        return self._post(f'/api/external/sessions/{self.session_id}/events', {
            'kind': kind, 'name': name, 'payload': payload or {},
        })
