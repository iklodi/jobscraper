"""Safe mode: filling employer forms without LinkedIn (issue #15)."""
import asyncio
import http.server
import threading

import pytest
from playwright.async_api import async_playwright

import applier
import db
import mode

POSTING = 'https://www.linkedin.com/jobs/view/1000000001/'
EMPLOYER = 'https://careers.example.com/jobs/42'


@pytest.mark.parametrize('link, apply_url, apply_type, safe, expected', [
    (POSTING, EMPLOYER, None, True, ('direct', EMPLOYER)),        # pasted link wins
    (POSTING, EMPLOYER, None, False, ('direct', EMPLOYER)),       # in both modes
    (POSTING, None, None, False, ('linkedin', POSTING)),          # full mode: as before
    (EMPLOYER, None, None, True, ('direct', EMPLOYER)),           # not LinkedIn: go there
    (POSTING, None, 'easy_apply', True, ('easy_apply', None)),    # left to the candidate
    (POSTING, None, 'offsite', True, ('needs_link', None)),       # never opens LinkedIn
    (POSTING, None, None, True, ('needs_link', None)),
])
def test_where_an_application_starts(link, apply_url, apply_type, safe, expected):
    assert applier.plan_start(link, apply_url, apply_type, safe) == expected


@pytest.mark.parametrize('url, expected', [
    ('https://www.linkedin.com/jobs/view/1/', True),
    ('https://ch.linkedin.com/in/someone', True),
    ('https://linkedin.com', True),
    ('https://notlinkedin.com/jobs', False),
    ('https://careers.example.com/?ref=linkedin.com', False),
])
def test_is_linkedin(url, expected):
    assert mode.is_linkedin(url) is expected


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<html><body><form><input name="first_name"></form></body></html>')

    def log_message(self, *args):
        pass


def test_the_employer_browser_cannot_reach_linkedin(tmp_path, monkeypatch):
    monkeypatch.setattr(mode, 'EMPLOYER_PROFILE_DIR', str(tmp_path / 'employer_profile'))
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    local = f'http://127.0.0.1:{httpd.server_address[1]}/apply'

    async def run():
        async with async_playwright() as p:
            context = await mode.open_employer_session(p, headless=True)
            try:
                page = await context.new_page()
                await page.goto(local)                       # an employer page loads
                assert await page.locator('input[name=first_name]').count() == 1
                with pytest.raises(Exception):               # LinkedIn never does
                    await page.goto('https://www.linkedin.com/jobs/view/1000000001/', timeout=10000)
            finally:
                await context.close()
    try:
        asyncio.run(run())
    finally:
        httpd.shutdown()
    assert not (tmp_path / 'chrome_profile').exists()


def test_a_linkedin_only_job_stays_approved_with_instructions(tmp_path, monkeypatch):
    monkeypatch.setenv('JOBSCRAPER_MODE', 'safe')
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    db.add_job('1000000001', 'Example Architect', 'Example AG', 'Zurich', 'text', POSTING)
    folder = tmp_path / 'app'
    folder.mkdir()
    monkeypatch.setattr(applier, 'application_files',
                        lambda job_id: (str(folder / 'cv.pdf'), str(folder / 'cl.pdf'), str(folder)))
    monkeypatch.setattr(applier, 'cover_letter_body', lambda *a: '')
    job = ('1000000001', 'Example Architect', 'Example AG', POSTING, 'text')

    status, note = asyncio.run(applier.process_job(None, None, {}, job))
    assert status == 'approved'
    assert "Employer application link" in note


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    db.add_job('1000000001', 'Example Architect', 'Example AG', 'Zurich', 'text', POSTING)
    import app as dashboard
    return dashboard.app.test_client()


def test_the_dashboard_stores_an_employer_link(client):
    res = client.post('/api/jobs/1000000001/apply-url', json={'url': EMPLOYER})
    assert res.status_code == 200
    assert db.get_apply_target('1000000001')[0] == EMPLOYER


def test_the_dashboard_refuses_a_linkedin_link_as_the_employer_page(client):
    res = client.post('/api/jobs/1000000001/apply-url', json={'url': POSTING + '?otpToken=x'})
    assert res.status_code == 400
    assert db.get_apply_target('1000000001')[0] is None
