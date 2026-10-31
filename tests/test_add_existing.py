"""The + button asks before re-reading a job that is already in the database."""
import pytest

import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app as dashboard
    path = str(tmp_path / 'jobs.db')
    monkeypatch.setattr(db, 'DB_FILE', path)
    monkeypatch.setattr(dashboard, 'DB_FILE', path)
    db.init_db()
    db.add_job('4000000001', 'Architect', 'Example AG', '', 'text',
               'https://www.linkedin.com/jobs/view/4000000001/')
    db.update_job_status('4000000001', 'applied')
    db.add_job('adzuna-77', 'Analyst', 'Example SA', '', 'text', 'https://www.adzuna.ch/details/77')
    started = []

    class NoThread:
        def __init__(self, target, args):
            started.append(args)

        def start(self):
            pass

        def is_alive(self):
            return False
    monkeypatch.setattr(dashboard.threading, 'Thread', NoThread)
    monkeypatch.setattr(dashboard, 'add_url_thread', None)
    c = dashboard.app.test_client()
    c.started = started
    return c


def test_a_known_linkedin_id_asks_first(client):
    url = 'https://www.linkedin.com/jobs/view/senior-architect-at-example-4000000001/?trk=x&otpToken=y'
    res = client.post('/api/jobs/from-url', json={'url': url}).get_json()
    assert res['status'] == 'exists'
    assert res['job'] == {'job_id': '4000000001', 'title': 'Architect', 'company': 'Example AG',
                          'status': 'applied', 'protected': True}
    assert client.started == []


def test_confirming_refetches(client):
    url = 'https://www.linkedin.com/jobs/view/4000000001/'
    res = client.post('/api/jobs/from-url', json={'url': url, 'confirm': True}).get_json()
    assert res['status'] == 'started' and len(client.started) == 1


def test_a_job_from_another_source_is_found_by_its_link(client):
    res = client.post('/api/jobs/from-url', json={'url': 'https://www.adzuna.ch/details/77'}).get_json()
    assert res['status'] == 'exists' and res['job']['job_id'] == 'adzuna-77'
    assert res['job']['protected'] is False


def test_a_new_link_starts_straight_away(client):
    res = client.post('/api/jobs/from-url',
                      json={'url': 'https://www.linkedin.com/jobs/view/4000000002/'}).get_json()
    assert res['status'] == 'started'


def test_a_busy_job_is_refused(client):
    db.update_job_status('4000000001', 'generating')
    res = client.post('/api/jobs/from-url',
                      json={'url': 'https://www.linkedin.com/jobs/view/4000000001/', 'confirm': True})
    assert res.status_code == 409 and client.started == []
