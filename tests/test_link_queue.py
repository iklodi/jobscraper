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
    assert (result['added'], result['known']) == (3, [])
    assert result['ids'][-1] == '1000000009' 
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


def test_several_links_from_the_dashboard_are_read_and_scored_now(board, monkeypatch):
    db.add_job('77', 'Known Role', 'Example AG', 'Zurich', 'An advert. ' * 30, 'https://jobs.example.com/known')
    link_queue.queue(['https://jobs.example.com/older'])          # queued earlier: not read now

    async def read_page(page, url):
        return {'description': 'Data Lead advert text. ' * 20, 'title': 'Data Lead',
                'ld': {'title': 'Data Lead', 'company': 'Sample SA'}, 'status': 200,
                'final_url': url, 'page_text': ''}
    monkeypatch.setattr(generate_from_url, '_read_page', read_page)
    monkeypatch.setattr(generate_from_url, 'extract_metadata', lambda text, title: {'title': title})
    linkedin_reads = []

    async def fetch_descriptions(max_jobs=None, pause=None):
        linkedin_reads.append(max_jobs)
        return {}
    monkeypatch.setattr(public_jobs, 'fetch_descriptions', fetch_descriptions)
    monkeypatch.setattr(link_queue, 'PAUSE', (0, 0))
    scored = []

    def score(job_id, extra):
        scored.append((job_id, extra))
        db.update_job_score(job_id, 8, 'fits', None, None, None, None, None)

    result = asyncio.run(link_queue.add_and_read(
        ['https://jobs.example.com/new', 'https://jobs.example.com/known',
         'https://www.linkedin.com/jobs/view/1000000010/'], 'emphasise data', score=score))
    new_id = generate_from_url.job_id_for('https://jobs.example.com/new')
    assert scored == [(new_id, 'emphasise data')], 'only the advert read now is scored'
    assert linkedin_reads == [1]
    assert result['queued'] == 2 and result['known'][0]['job_id'] == '77'
    by_id = {job['job_id']: job for job in result['jobs']}
    assert by_id[new_id]['status'] == 'to_apply' and by_id[new_id]['score'] == 8
    assert by_id['1000000010']['status'] == 'new'
    older = generate_from_url.job_id_for('https://jobs.example.com/older')
    assert [job_id for job_id, _ in link_queue.pending(10)] == [older]
