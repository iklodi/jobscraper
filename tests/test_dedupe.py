"""One advert from several sources is one job (dedupe.py)."""
import pytest

import db
import dedupe


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()

    def add(job_id, title, company, status):
        db.add_job(job_id, title, company, '', 'text', 'x')
        db.update_job_status(job_id, status)
    return add


@pytest.mark.parametrize('a, b', [('Example Switzerland', 'Example'), ('Example Switzerland AG', 'Example'),
                                  ('Example (Schweiz) AG', 'Example'), ('Groupe Example', 'Example SA'),
                                  ('Example Long Name & Partners', 'ELN Switzerland')])
def test_one_employer_under_several_names(a, b, monkeypatch):
    monkeypatch.setenv('COMPANY_ALIASES', 'Example Long Name Partners=ELN')
    assert dedupe.same_company(a, b)


def test_different_employers_stay_apart():
    assert not dedupe.same_company('Swiss Re', 'Swiss Life')
    assert not dedupe.same_company('Example AG', 'Other AG')


def test_an_alert_copy_of_an_adzuna_job_is_listed(jobs):
    jobs('adzuna-1', 'Manager / Senior Manager - Data - AI Platforms', 'Example', 'applied')
    assert dedupe.already_listed('Manager / Senior Manager – Data - AI Platforms',
                                 'Example Switzerland')


def test_alerts_skip_a_job_already_on_the_board(jobs):
    import alerts
    jobs('adzuna-1', 'AI Architect', 'Example AG', 'to_apply')
    added = alerts.store_jobs('x', [{'job_id': '4000000009', 'title': 'AI Architect',
                                     'company': 'Example', 'location': '', 'link': 'l'}])
    assert added == 0 and not db.job_exists('4000000009')


def status(job_id):
    return db.get_job_status(job_id)


def test_sweep_keeps_the_copy_furthest_along(jobs):
    jobs('adzuna-1', 'AI Architect', 'Example', 'applied')
    jobs('4000000001', 'AI Architect', 'Example Switzerland', 'easy_apply')   # applied already
    jobs('4000000002', 'AI Architect', 'Example AG', 'new')                   # waiting
    jobs('4000000003', 'Data Lead', 'Example', 'approved')
    jobs('4000000004', 'Data Lead', 'Example', 'easy_apply')                  # no application yet
    moved = dict(dedupe.sweep())
    assert moved == {'4000000001': 'adzuna-1', '4000000002': 'adzuna-1'}
    assert status('4000000001') == 'rejected' and status('4000000002') == 'rejected'
    assert status('4000000003') == 'approved' and status('4000000004') == 'easy_apply'
    conn = db.get_connection()
    note = conn.execute("SELECT note FROM job_history WHERE job_id = '4000000001' "
                        "AND new_status = 'rejected'").fetchone()[0]
    conn.close()
    assert note == 'Duplicate of #adzuna-1'


def test_dry_run_moves_nothing(jobs):
    jobs('1', 'AI Architect', 'Example', 'to_apply')
    jobs('2', 'AI Architect', 'Example', 'new')
    assert dedupe.sweep(dry_run=True) == [('2', '1')]
    assert status('2') == 'new'
