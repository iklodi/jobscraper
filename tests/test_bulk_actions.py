"""The board's selection menu (POST /api/jobs/bulk)."""
import pytest

import db


@pytest.fixture
def board(tmp_path, monkeypatch):
    import app as dashboard
    path = str(tmp_path / 'jobs.db')
    monkeypatch.setattr(db, 'DB_FILE', path)
    monkeypatch.setattr(dashboard, 'DB_FILE', path)
    db.init_db()
    for job_id, status in [('1', 'to_apply'), ('2', 'to_apply'), ('3', 'generating'), ('4', 'failed')]:
        db.add_job(job_id, f'Role {job_id}', 'Example AG', '', 'text', 'x')
        db.update_job_status(job_id, status)
    started = []
    monkeypatch.setattr(dashboard, 'bg_bulk_generate', lambda ids: started.append(('generate', ids)))
    monkeypatch.setattr(dashboard, 'bg_bulk_reevaluate', lambda jobs: started.append(('reevaluate', jobs)))
    monkeypatch.setattr(dashboard, 'get_files_for_job', lambda job_id: [])
    monkeypatch.setattr(dashboard.threading, 'Thread',
                        lambda target, args=(), kwargs=None: type('T', (), {'start': lambda self: target(*args)})())
    return dashboard.app.test_client(), started


def statuses():
    conn = db.get_connection()
    out = dict(conn.execute('SELECT job_id, status FROM jobs').fetchall())
    conn.close()
    return out


def test_moves_the_selection_and_skips_busy_jobs(board):
    client, started = board
    res = client.post('/api/jobs/bulk', json={'job_ids': ['1', '3', '4'], 'action': 'status', 'status': 'rejected'})
    assert res.get_json() == {'status': 'ok', 'moved': 2, 'skipped': 1, 'generating': []}
    assert statuses() == {'1': 'rejected', '2': 'to_apply', '3': 'generating', '4': 'rejected'}
    conn = db.get_connection()
    notes = conn.execute("SELECT job_id, note FROM job_history WHERE note = 'Bulk action' ORDER BY job_id").fetchall()
    conn.close()
    assert notes == [('1', 'Bulk action'), ('4', 'Bulk action')]
    assert started == []


def test_approving_writes_documents_one_run_for_all(board):
    client, started = board
    res = client.post('/api/jobs/bulk', json={'job_ids': ['1', '2'], 'action': 'status', 'status': 'approved'})
    assert res.get_json()['generating'] == ['1', '2']
    assert started == [('generate', ['1', '2'])]


def test_reevaluate_keeps_each_prior_status(board):
    client, started = board
    client.post('/api/jobs/bulk', json={'job_ids': ['1', '4'], 'action': 'reevaluate'})
    assert sorted(started[0][1]) == [('1', 'to_apply'), ('4', 'failed')]
    assert statuses()['1'] == 'evaluating'


@pytest.mark.parametrize('body', [
    {'job_ids': [], 'action': 'status', 'status': 'rejected'},
    {'job_ids': ['1'], 'action': 'delete'},
    {'job_ids': ['1'], 'action': 'status', 'status': 'generating'},
])
def test_refuses_bad_requests(board, body):
    client, _ = board
    assert client.post('/api/jobs/bulk', json=body).status_code == 400
    assert statuses()['1'] == 'to_apply'
