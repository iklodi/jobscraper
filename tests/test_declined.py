"""Declined: an application the employer turned down."""
import db
import dedupe
import evaluate


def test_declined_is_a_protected_status_and_an_application():
    assert 'declined' in db.PROTECTED_STATUSES
    assert 'declined' in dedupe.APPLIED


def test_a_declined_job_still_counts_as_applied_to(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    db.add_job('1', 'Example Role', 'Example AG', '', 'text', 'x')
    db.update_job_status('1', 'declined')
    assert evaluate.get_previous_applications() == [('Example AG', 'Example Role')]


def test_the_board_can_move_jobs_to_declined(tmp_path, monkeypatch):
    import app as dashboard
    path = str(tmp_path / 'jobs.db')
    monkeypatch.setattr(db, 'DB_FILE', path)
    monkeypatch.setattr(dashboard, 'DB_FILE', path)
    db.init_db()
    db.add_job('1', 'Example Role', 'Example AG', '', 'text', 'x')
    db.update_job_status('1', 'applied')
    res = dashboard.app.test_client().post('/api/jobs/bulk', json={
        'job_ids': ['1'], 'action': 'status', 'status': 'declined'})
    assert res.get_json()['moved'] == 1
    assert db.get_job_status('1') == 'declined'


def test_a_reason_given_with_a_bulk_decline_is_saved_on_each_job(tmp_path, monkeypatch):
    import app as dashboard
    path = str(tmp_path / 'jobs.db')
    monkeypatch.setattr(db, 'DB_FILE', path)
    monkeypatch.setattr(dashboard, 'DB_FILE', path)
    db.init_db()
    for job_id in ('1', '2'):
        db.add_job(job_id, f'Role {job_id}', 'Example AG', '', 'text', 'x')
        db.update_job_status(job_id, 'interviewing')
    dashboard.app.test_client().post('/api/jobs/bulk', json={
        'job_ids': ['1', '2'], 'action': 'status', 'status': 'declined', 'notes': ' Went with an internal candidate '})
    conn = db.get_connection()
    assert conn.execute('SELECT DISTINCT application_notes FROM jobs').fetchall() == [('Went with an internal candidate',)]
    assert conn.execute("SELECT COUNT(*) FROM job_history WHERE event_type = 'note_added'").fetchone()[0] == 2
    conn.close()
