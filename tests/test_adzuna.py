"""Adzuna: jobs from its job-search API, descriptions from its advert pages.

Nothing here calls Adzuna - the API and the pages are faked.
"""
import pathlib

import pytest

import adzuna
import db

FIX = pathlib.Path(__file__).parent / 'fixtures'


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    monkeypatch.setenv('ADZUNA_APP_ID', 'id')
    monkeypatch.setenv('ADZUNA_APP_KEY', 'key')
    for k in ('ADZUNA_COUNTRY', 'ADZUNA_WIDE_COUNTRIES', 'ADZUNA_MAX_PAGES',
              'ADZUNA_MAX_CALLS_PER_RUN', 'ADZUNA_MAX_DAYS_OLD'):
        monkeypatch.delenv(k, raising=False)
    db.init_db()


def ad(n, title='Interim Manager', company='Example AG'):
    return {'id': str(n), 'title': title, 'company': {'display_name': company},
            'location': {'display_name': 'Zürich'}, 'description': 'Cut short…',
            'redirect_url': f'https://www.adzuna.ch/details/{n}?utm_medium=api'}


class Response:
    def __init__(self, payload=None, text='', status=200):
        self.payload, self.text, self.status_code = payload, text, status
        self.ok = status < 400

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f'HTTP {self.status_code}')


class FakeSession:
    """Serves queued responses and records every request."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        return self.responses.pop(0)


def test_locations_map_to_countries_cities_and_wide_areas(monkeypatch):
    monkeypatch.delenv('ADZUNA_COUNTRY', raising=False)
    monkeypatch.delenv('ADZUNA_WIDE_COUNTRIES', raising=False)
    keywords = [('Head of Example', None), ('Interim Manager', ['Switzerland'])]
    searches, skipped = adzuna.plan_searches(keywords, ['Switzerland', 'Zürich', 'Worldwide'])
    assert searches == [('Head of Example', 'ch', None), ('Head of Example', 'ch', 'Zürich'),
                        ('Interim Manager', 'ch', None)]
    assert skipped == ['Worldwide']

    monkeypatch.setenv('ADZUNA_WIDE_COUNTRIES', 'de, fr')
    searches, skipped = adzuna.plan_searches([('X', None)], ['Worldwide', 'Germany'])
    assert searches == [('X', 'de', None), ('X', 'fr', None)] and skipped == []


def test_a_job_type_filter_only_when_the_criteria_name_one():
    assert adzuna.type_filter(['Contract']) == 'contract'
    assert adzuna.type_filter(['Contract', 'Temporary']) == 'contract'
    assert adzuna.type_filter(['Full-time', 'Contract']) is None
    assert adzuna.type_filter([]) is None


def test_ingest_pages_until_short_and_adds_each_job_once(scratch_db):
    full_page = {'count': 51, 'results': [ad(n) for n in range(50)]}
    # 50 identical titles at one company are one role: only the first is kept.
    session = FakeSession([Response(full_page), Response({'count': 51, 'results': [ad(99, 'Head of Example')]})])
    stats = adzuna.ingest(([('Interim Manager', None)], ['Switzerland'], []),
                          session=session, sleep=lambda s: None)
    assert stats == {'Interim Manager (Adzuna)': 2}
    assert [c[0] for c in session.calls] == [adzuna.API.format(country='ch', page=1),
                                             adzuna.API.format(country='ch', page=2)]
    params = session.calls[0][1]
    assert params['title_only'] == 'Interim Manager' and params['max_days_old'] == adzuna.FIRST_RUN_DAYS

    conn = db.get_connection()
    rows = conn.execute("SELECT job_id, status, description, link, source FROM jobs ORDER BY job_id").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ['adzuna-0', 'adzuna-99']
    assert {r[1] for r in rows} == {'new'} and {r[2] for r in rows} == {''}
    assert rows[0][3].startswith('https://www.adzuna.ch/details/0')
    assert rows[0][4] == 'adzuna: Interim Manager'
    # The 500-character snippet is never scored.
    assert db.get_unscored_jobs() == []


def test_a_job_already_found_on_linkedin_is_not_added_again(scratch_db):
    db.add_job('1000000001', 'Interim Manager (m/f/d)', 'Example AG', 'Zürich', 'full text', 'x')
    session = FakeSession([Response({'results': [ad(1, 'Interim Manager m/f/d')]})])
    stats = adzuna.ingest(([('Interim Manager', None)], ['Switzerland'], []),
                          session=session, sleep=lambda s: None)
    assert stats == {'Interim Manager (Adzuna)': 0}


def test_ingest_stops_at_the_call_budget(scratch_db, monkeypatch):
    monkeypatch.setenv('ADZUNA_MAX_CALLS_PER_RUN', '2')
    session = FakeSession([Response({'results': []})] * 5)
    adzuna.ingest(([('A', None), ('B', None), ('C', None)], ['Switzerland'], []),
                  session=session, sleep=lambda s: None)
    assert len(session.calls) == 2


def test_advert_text_comes_from_the_page_body_only():
    text = adzuna.page_text((FIX / 'adzuna_details.html').read_text(encoding='utf-8'))
    assert text.splitlines()[0] == 'Interim Example Manager'
    assert 'Start date: Immediate' in text
    assert 'Manage cash flow and liquidity planning.' in text
    assert 'Apply for this job' not in text and 'tracking' not in text


def test_fetch_fills_descriptions_and_counts_failures(scratch_db):
    db.add_job('adzuna-1', 'A', 'Example AG', '', '', 'https://www.adzuna.ch/details/1')
    db.add_job('adzuna-2', 'B', 'Example AG', '', '', 'https://www.adzuna.ch/details/2')
    db.add_job('1000000001', 'C', 'Example AG', '', '', 'https://www.linkedin.com/jobs/view/1000000001/')
    page = (FIX / 'adzuna_details.html').read_text(encoding='utf-8')
    session = FakeSession([Response(text=page), Response(status=404)])
    stats = adzuna.fetch_descriptions(session=session, sleep=lambda s: None)
    assert stats == {'fetched': 1, 'failed': 1, 'blocked': False, 'pending': 1}
    # LinkedIn jobs are left to public_jobs; Adzuna pages only here.
    assert all('adzuna' in url for url, _ in session.calls)
    conn = db.get_connection()
    rows = dict(conn.execute("SELECT job_id, fetch_attempts FROM jobs "
                             "WHERE job_id LIKE 'adzuna-%' AND description = ''").fetchall())
    filled = conn.execute("SELECT job_id FROM jobs WHERE description != ''").fetchall()
    conn.close()
    assert len(rows) == 1 and list(rows.values()) == [1]
    assert len(filled) == 1 and filled[0][0].startswith('adzuna-')


def test_fetch_stops_when_adzuna_refuses_and_keeps_the_attempts(scratch_db):
    for n in (1, 2, 3):
        db.add_job(f'adzuna-{n}', f'Role {n}', 'Example AG', '', '', f'https://www.adzuna.ch/details/{n}')
    session = FakeSession([Response(status=403)])
    stats = adzuna.fetch_descriptions(session=session, sleep=lambda s: None)
    assert stats['blocked'] is True and len(session.calls) == 1
    assert stats['pending'] == 3
    conn = db.get_connection()
    attempts = {r[0] for r in conn.execute('SELECT COALESCE(fetch_attempts, 0) FROM jobs')}
    conn.close()
    assert attempts == {0}


def test_copies_of_one_advert_are_recognised(scratch_db):
    db.add_job('adzuna-1', 'Product Manager AI Empowerment 80 - 100 % (f/m/d)', 'Hamilton AG', '', '', 'x')
    assert adzuna.already_listed('Product Manager AI Empowerment 80 - 100 %', 'Hamilton AG')
    assert adzuna.already_listed('Product Manager AI Empowerment 80 - 100 %', 'Job-Room')
    assert not adzuna.already_listed('Product Manager AI Empowerment', 'Another Employer AG')
