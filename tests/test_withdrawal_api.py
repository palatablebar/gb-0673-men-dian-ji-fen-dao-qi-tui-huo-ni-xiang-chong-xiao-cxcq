from fastapi.testclient import TestClient
from app import main


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'api.sqlite'))
    return TestClient(main.app)


def test_withdrawal_page_and_api(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'misissue'}).json()
    rid = run['manifest']['run_id']
    page = c.get(f'/withdrawal?run_id={rid}')
    assert page.status_code == 200
    assert '误发批次撤回' in page.text
    calc = c.post('/api/withdrawal', json={}).json()
    assert calc['withdrawal_plan']['reconciliation']['members'] == 8


def test_publish_flow_and_idempotency_api(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'misissue'}).json()
    rid = run['manifest']['run_id']
    revision = run['revision']
    res = c.post(f'/api/runs/{rid}/withdrawals/publish', json={'revision': revision})
    assert res.status_code == 200
    doc = res.json()
    assert doc['state'] == '已发布'

    again = c.post(f'/api/runs/{rid}/withdrawals/publish',
                   json={'revision': revision, 'idempotency_key': doc['idempotency_key']})
    assert again.status_code == 200
    assert again.json()['id'] == doc['id']
    assert len(c.get(f'/api/runs/{rid}/withdrawals').json()) == 1


def test_stale_revision_api(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'misissue'}).json()['manifest']['run_id']
    res = c.post(f'/api/runs/{rid}/withdrawals/publish', json={'revision': 999})
    assert res.status_code == 409
    assert res.json()['code'] == 'STALE_VERSION'


def test_bad_revision_payload_api(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'misissue'}).json()['manifest']['run_id']
    res = c.post(f'/api/runs/{rid}/withdrawals/publish', json={'revision': 0})
    assert res.status_code == 422


def test_rollback_api_restores(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'misissue'}).json()['manifest']['run_id']
    doc = c.post(f'/api/runs/{rid}/withdrawals/publish', json={}).json()
    detail = c.get(f"/api/withdrawals/{doc['id']}")
    assert detail.status_code == 200
    rolled = c.post(f"/api/withdrawals/{doc['id']}/rollback")
    assert rolled.status_code == 200
    assert rolled.json()['state'] == '已回滚'
    assert 'withdrawal_published' not in c.get(f'/api/runs/{rid}').json()


def test_navigation_includes_withdrawal(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'misissue'}).json()['manifest']['run_id']
    html = c.get(f'/withdrawal?run_id={rid}').text
    assert f'/withdrawal?run_id={rid}' in html


def test_dataset_selector_has_misissue(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'dirty'}).json()['manifest']['run_id']
    html = c.get(f'/intake?run_id={rid}').text
    assert 'misissue' in html and '误发撤回批' in html
