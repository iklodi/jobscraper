"""Links queued from a list, read later in a fresh browser."""
import asyncio

import pytest

import db
import generate_from_url
import link_queue
import public_jobs

SAVED_PAGE = '''<html><head>
<link href="https://fonts.googleapis.com/css2?family=Example&amp;display=swap" rel="stylesheet">
</head><body><script>const jobs = [
 {url: "https://jobs.example.com/job/1234/senior-architect"},
 {url: "https://www.example.org/emplois/detail/abc-def/"},
 {url: "https://jobs.example.com/job/1234/senior-architect"},
 {url: "https://www.linkedin.com/jobs/view/1000000009/?trk=x"}];</script></body></html>'''


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()


def test_links_are_found_once_each_without_fonts():
    assert link_queue.urls_in(SAVED_PAGE) == [
        'https://jobs.example.com/job/1234/senior-architect',
        'https://www.example.org/emplois/detail/abc-def/',
        'https://www.linkedin.com/jobs/view/1000000009/?trk=x']


def test_queueing_adds_pending_jobs_and_skips_known_ones(board):
    result = link_queue.queue(link_queue.urls_in(SAVED_PAGE))
    assert result == {'added': 3, 'known': []}
    again = link_queue.queue(['https://jobs.example.com/job/1234/senior-architect'])
    assert again['added'] == 0 and again['known'][0][2] == 'new'
    conn = db.get_connection()
    rows = dict(conn.execute('SELECT job_id, title FROM jobs').fetchall())
    conn.close()
    # LinkedIn's link is reduced to its id, and read by public_jobs, not here.
    assert rows['1000000009'] == '(to fetch) linkedin.com'
    assert [job_id for job_id, _ in link_queue.pending(10)] == \
        [generate_from_url.job_id_for('https://jobs.example.com/job/1234/senior-architect'),
         generate_from_url.job_id_for('https://www.example.org/emplois/detail/abc-def/')]
    assert [job_id for job_id, _ in public_jobs.pending_jobs(10)] == ['1000000009']


def test_a_read_advert_fills_in_the_job_and_a_known_one_is_retired(board, monkeypatch):
    db.add_job('99', 'Senior Architect', 'Example AG', 'Zurich', 'An advert. ' * 30, 'x')
    link_queue.queue(['https://jobs.example.com/a', 'https://jobs.example.com/b'])
    pages = {'https://jobs.example.com/a': ('Data Lead', 'Sample SA'),
             'https://jobs.example.com/b': ('Senior Architect', 'Example AG Switzerland')}

    async def read_page(page, url):
        return {'description': f'{pages[url][0]} advert text. ' * 20, 'title': pages[url][0],
                'ld': {'title': pages[url][0], 'company': pages[url][1]}, 'status': 200,
                'final_url': url, 'page_text': ''}
    monkeypatch.setattr(generate_from_url, '_read_page', read_page)
    monkeypatch.setattr(generate_from_url, 'extract_metadata', lambda text, title: {'title': title})
    stats = asyncio.run(link_queue.fetch_pending(pause=(0, 0)))
    assert stats == {'read': 1, 'duplicates': 1, 'closed': 0, 'failed': 0, 'pending': 0}
    conn = db.get_connection()
    rows = conn.execute("SELECT title, company, status FROM jobs WHERE job_id LIKE 'url-%' ORDER BY title").fetchall()
    conn.close()
    assert rows == [('Data Lead', 'Sample SA', 'new'), ('Senior Architect', 'Example AG Switzerland', 'rejected')]


def test_a_refused_page_stays_queued_and_a_gone_one_closes(board, monkeypatch):
    link_queue.queue(['https://jobs.example.com/refused', 'https://jobs.example.com/gone'])
    codes = {'https://jobs.example.com/refused': 403, 'https://jobs.example.com/gone': 410}

    async def read_page(page, url):
        return {'description': '', 'status': codes[url], 'final_url': url, 'page_text': ''}
    monkeypatch.setattr(generate_from_url, '_read_page', read_page)
    stats = asyncio.run(link_queue.fetch_pending(pause=(0, 0)))
    assert (stats['failed'], stats['closed'], stats['pending']) == (1, 1, 1)
    conn = db.get_connection()
    assert conn.execute("SELECT status, score FROM jobs WHERE link = 'https://jobs.example.com/gone'").fetchone() == ('scored', 0)
    conn.close()
