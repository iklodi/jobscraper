"""The dashboard's Clear button on To Do (POST /api/backlog/clear)."""
import pytest

import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app as dashboard
    path = str(tmp_path / 'jobs.db')
    monkeypatch.setattr(db, 'DB_FILE', path)
    monkeypatch.setattr(dashboard, 'DB_FILE', path)
    db.init_db()
    for job_id, status in [('1', 'to_apply'), ('2', 'generated'), ('3', 'approved'),
                           ('4', 'new'), ('5', 'applied')]:
        db.add_job(job_id, f'Role {job_id}', 'Example AG', '', 'text', 'x')
        db.update_job_status(job_id, status)
    return dashboard.app.test_client()


def statuses():
    conn = db.get_connection()
    out = dict(conn.execute('SELECT job_id, status FROM jobs').fetchall())
    conn.close()
    return out


def test_clear_moves_only_to_do_and_records_why(client):
    res = client.post('/api/backlog/clear')
    assert res.get_json() == {'status': 'ok', 'moved': 2}
    assert statuses() == {'1': 'rejected', '2': 'rejected', '3': 'approved', '4': 'new', '5': 'applied'}
    conn = db.get_connection()
    notes = conn.execute("SELECT job_id, old_status, note FROM job_history WHERE new_status = 'rejected' "
                         "ORDER BY job_id").fetchall()
    conn.close()
    assert notes == [('1', 'to_apply', 'Backlog cleared'), ('2', 'generated', 'Backlog cleared')]


def test_an_empty_backlog_moves_nothing(client):
    client.post('/api/backlog/clear')
    assert client.post('/api/backlog/clear').get_json()['moved'] == 0
