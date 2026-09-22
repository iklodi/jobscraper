"""Safe mode: fetching descriptions logged out (issue #14), against a local server."""
import asyncio
import http.server
import threading

import pytest

import db
import public_jobs

DESCRIPTION = 'Lead the architecture of an example platform. ' * 12
PAGES = {
    '/ok': ('<html><body><div class="show-more-less-html__markup">' + DESCRIPTION + '</div>'
            '<a data-tracking-control-name="public_jobs_apply-link-offsite_sign-in">Apply</a>'
            '</body></html>', 200),
    '/easy': ('<html><body><div class="description__text">' + DESCRIPTION + '</div>'
              '<button data-tracking-control-name="public_jobs_apply-link-simple">Easy Apply</button>'
              '</body></html>', 200),
    '/closed': ('<html><body><p>No longer accepting applications</p></body></html>', 200),
    '/short': ('<html><body><div class="description__text">too short</div></body></html>', 200),
    '/wall': ('<html><body>Request denied</body></html>', 999),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body, status = PAGES.get(self.path, ('not found', 404))
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()


def add(job_id, path, created):
    conn = db.get_connection()
    conn.execute("INSERT INTO jobs (job_id, title, company, location, description, link, status, created_at) "
                 "VALUES (?, 'Role', 'Example Co', 'Somewhere', '', ?, 'new', ?)", (job_id, path, created))
    conn.commit()
    conn.close()


def row(job_id):
    conn = db.get_connection()
    r = conn.execute('SELECT status, description, apply_type, fetch_attempts FROM jobs WHERE job_id = ?',
                     (job_id,)).fetchone()
    conn.close()
    return r


def test_public_url_is_rebuilt_from_the_id():
    assert public_jobs.public_url(('4000000001', 'https://www.linkedin.com/comm/jobs/view/4000000001/?otpToken=X')) \
        == 'https://www.linkedin.com/jobs/view/4000000001/'


def test_fetch_reads_descriptions_and_stops_at_the_wall(server, scratch_db, monkeypatch):
    # Newest first: ok, easy, closed, short, then the wall, then one that must wait.
    for i, (jid, path) in enumerate([('ok', '/ok'), ('easy', '/easy'), ('closed', '/closed'),
                                     ('short', '/short'), ('wall', '/wall'), ('after', '/ok')]):
        add(jid, server + path, f'2026-09-23 10:00:{59 - i:02d}')
    asyncio.run(public_jobs.fetch_descriptions(pause=(0, 0)))

    assert row('ok')[1].startswith('Lead the architecture') and row('ok')[2] == 'offsite'
    assert row('easy')[2] == 'easy_apply'
    assert row('closed')[0] == 'scored'                      # nothing to apply for
    assert row('short')[1] == '' and row('short')[3] == 1    # counted as a failed attempt
    assert row('wall')[1] == '' and row('wall')[3] in (0, None)  # the wall is not the job's fault
    assert row('after')[1] == ''                             # run stopped at the wall

    # Jobs with a description are now scoreable; the rest are not.
    assert sorted(j[0] for j in db.get_unscored_jobs()) == ['easy', 'ok']


def test_a_job_that_keeps_failing_stops_being_retried(server, scratch_db, monkeypatch):
    monkeypatch.setenv('SAFE_FETCH_MAX_ATTEMPTS', '2')
    add('short', server + '/short', '2026-09-23 10:00:00')
    for _ in range(3):
        asyncio.run(public_jobs.fetch_descriptions(pause=(0, 0)))
    assert row('short')[3] == 2
