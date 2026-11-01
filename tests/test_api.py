from fastapi.testclient import TestClient
from backend.main import app


def test_external_ingestion_and_websocket(monkeypatch):
    monkeypatch.setenv('AGENT_TRACER_INGEST_TOKEN', 'test-local-token')
    headers = {'X-Tracer-Token': 'test-local-token'}
    with TestClient(app) as client:
        assert client.post('/api/external/sessions').status_code == 401
        sid = client.post('/api/external/sessions', headers=headers).json()['id']
        with client.websocket_connect(f'/ws/sessions/{sid}') as ws:
            assert ws.receive_json()['type'] == 'snapshot'
            response = client.post(f'/api/external/sessions/{sid}/events', headers=headers,
                                   json={'kind': 'tool.after', 'name': 'example', 'payload': {'result': 42}})
            assert response.status_code == 200
            assert ws.receive_json()['data']['payload']['result'] == 42
        assert client.get(f'/api/sessions/{sid}').json()['events'][-1]['name'] == 'example'


def test_compiled_frontend_routes():
    from backend.main import DIST
    import pytest
    if not DIST.is_dir():
        pytest.skip('Run npm run build first to verify compiled UI')
    with TestClient(app) as client:
        for page in ('/', '/sidecar.html'):
            assert client.get(page).status_code == 200
        for asset in (DIST / 'assets').iterdir():
            assert client.get('/assets/' + asset.name).status_code == 200
