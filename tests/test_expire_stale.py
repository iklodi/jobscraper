"""Jobs that wait too long for a description are given up on."""
import datetime

import db
import public_jobs


def test_old_jobs_without_a_description_move_to_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    for job_id, desc in [('1', ''), ('2', ''), ('3', 'An advert. ' * 30), ('adzuna-4', '')]:
        db.add_job(job_id, f'Role {job_id}', 'Example AG', '', desc, 'x')
    old = (datetime.datetime.now() - datetime.timedelta(days=20)).isoformat(sep=' ')
    conn = db.get_connection()
    conn.execute("UPDATE jobs SET status = 'new', created_at = ? WHERE job_id != '2'", (old,))
    conn.execute("UPDATE jobs SET status = 'new' WHERE job_id = '2'")
    conn.commit()

    assert public_jobs.expire_stale(days=14) == 1
    statuses = dict(conn.execute('SELECT job_id, status FROM jobs').fetchall())
    # Old and empty goes; recent, described or Adzuna's (its own fetcher) stay.
    assert statuses == {'1': 'rejected', '2': 'new', '3': 'new', 'adzuna-4': 'new'}
    assert conn.execute("SELECT note FROM job_history WHERE job_id = '1' AND new_status = 'rejected'"
                        ).fetchone()[0] == 'No description after 14 days - not scored'
    conn.close()
