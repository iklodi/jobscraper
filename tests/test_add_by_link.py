"""The dashboard's + button (generate_from_url.py) in safe mode (issue #16)."""
import asyncio
import http.server
import threading

import pytest

import db
import generate_from_url as gfu
import mode

ADVERT = 'Lead the architecture of an example platform for a Swiss client. ' * 10
PAGE = ('<html><head><title>Example Architect</title></head><body><main><h1>Example Architect</h1>'
        f'<div class="job-description">{ADVERT}</div></main></body></html>')


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(PAGE.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()


@pytest.fixture
def safe(monkeypatch):
    monkeypatch.setenv('JOBSCRAPER_MODE', 'safe')

    async def no_profile(*a, **k):
        raise AssertionError('safe mode opened the logged-in profile')
    monkeypatch.setattr(mode, 'open_linkedin_session', no_profile)


@pytest.mark.parametrize('pasted', [
    'https://www.linkedin.com/comm/jobs/view/1000000001/?trackingId=x&otpToken=secret',
    'https://ch.linkedin.com/jobs/view/example-architect-at-example-ag-1000000001?trk=public',
    'https://www.linkedin.com/jobs/search/?currentJobId=1000000001&keywords=architect',
])
def test_linkedin_links_are_reduced_to_the_bare_posting(pasted):
    assert gfu.canonical_url(pasted) == 'https://www.linkedin.com/jobs/view/1000000001/'


def test_other_links_are_left_alone():
    url = 'https://careers.example.com/jobs/42?currentJobId=7'
    assert gfu.canonical_url(url) == url


def test_safe_mode_reads_a_page_without_the_profile(server, safe):
    page = asyncio.run(gfu.fetch_job_page(server + '/job'))
    assert 'example platform' in page['description']
    assert page['status'] == 200


def test_a_walled_linkedin_page_gives_a_clear_error(safe, monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))

    async def walled(url, headed=False):
        assert '?' not in url, 'the sign-in parameters must be stripped before fetching'
        return {'description': '', 'walled': True, 'page_text': ''}
    monkeypatch.setattr(gfu, 'fetch_job_page', walled)
    result = asyncio.run(gfu.run_for_url(
        'https://www.linkedin.com/comm/jobs/view/1000000001/?otpToken=secret'))
    assert result['ok'] is False
    assert 'sign-in wall' in result['error'] and "employer's own job page" in result['error']
