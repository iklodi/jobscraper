"""The nightly controller (main.py) in safe mode, with every source stubbed.

Nothing here reaches LinkedIn, Adzuna, a mailbox, a model or an SMTP server,
and progress.json / last_run.json are redirected away from the repository.
"""
import asyncio
import datetime

import pytest

import alerts
import db
import main
import mode
import notifier
import progress_tracker


@pytest.fixture
def safe_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(progress_tracker, 'PROGRESS_FILE', str(tmp_path / 'progress.json'))
    monkeypatch.setattr(progress_tracker, 'RESULT_FILE', str(tmp_path / 'last_run.json'))
    monkeypatch.setenv('JOBSCRAPER_MODE', 'safe')
    monkeypatch.setattr('sys.argv', ['main.py', '--no-eval'])
    db.init_db()

    async def no_profile(*a, **k):
        raise AssertionError('safe mode opened the logged-in profile')
    monkeypatch.setattr(mode, 'open_linkedin_session', no_profile)

    async def no_scrape():
        raise AssertionError('safe mode ran the LinkedIn scraper')
    monkeypatch.setattr(main, 'run_scraper', no_scrape)

    sent = {}
    monkeypatch.setattr(notifier, 'send_email',
                        lambda subject, body: sent.update(subject=subject, body=body))
    return monkeypatch, sent


def test_safe_run_reads_alerts_fetches_and_reports_its_sources(safe_run):
    monkeypatch, sent = safe_run
    monkeypatch.setattr(alerts, 'configured', lambda: True)

    def ingest():
        # One capped email for the summary to flag.
        conn = db.get_connection()
        conn.execute("INSERT INTO alert_emails (message_id, received_at, alert, jobs_listed, "
                     "jobs_added, processed_at) VALUES ('<m@x>', '2026-01-01', 'Example in Europe', "
                     "6, 6, ?)", (datetime.datetime.now().isoformat(sep=' ', timespec='seconds'),))
        conn.commit()
        conn.close()
        return {'Example in Europe': 6}
    monkeypatch.setattr(alerts, 'ingest_alerts', ingest)

    async def fetch():
        return {'fetched': 4, 'failed': 1, 'closed': 1, 'blocked': True, 'pending': 2}
    monkeypatch.setattr(main.public_jobs, 'fetch_descriptions', fetch)
    monkeypatch.setattr(main.adzuna, 'configured', lambda: False)

    asyncio.run(main.main())

    body = sent['body']
    assert 'Mode: **safe**' in body
    assert 'LinkedIn alert: Example in Europe*: 6' in body
    assert 'maximum of 6 jobs' in body
    assert '4 fetched, 1 failed, 1 closed, 2 still pending' in body
    assert 'walled the fetcher' in body


def test_safe_run_without_imap_says_so(safe_run):
    monkeypatch, sent = safe_run
    monkeypatch.setattr(alerts, 'configured', lambda: False)

    async def fetch():
        return {'fetched': 0, 'failed': 0, 'closed': 0, 'blocked': False, 'pending': 0}
    monkeypatch.setattr(main.public_jobs, 'fetch_descriptions', fetch)
    monkeypatch.setattr(main.adzuna, 'configured', lambda: False)

    asyncio.run(main.main())
    assert 'IMAP is not configured' in sent['body']
