"""Safe mode: reading LinkedIn job-alert emails (issue #13).

Fixtures copy the layout of real alert emails with every name, id and token
replaced - this repository is public.
"""
import email.message
import pathlib

import pytest

import alerts
import db

FIX = pathlib.Path(__file__).parent / 'fixtures'


def read(name):
    return (FIX / name).read_text(encoding='utf-8')


def test_single_job_with_badge_after_a_blank_line():
    name, jobs = alerts.parse_alert_text(read('alert_single_blank_badge.txt'))
    assert name == 'Deputy Chief Technology Officer in Switzerland'
    assert jobs == [{'job_id': '1000000001', 'title': 'Chief Technology Officer',
                     'company': 'Example Talent Network', 'location': 'Zurich, Switzerland',
                     'link': 'https://www.linkedin.com/jobs/view/1000000001/'}]


def test_single_job_with_badge_directly_under_the_location():
    _, jobs = alerts.parse_alert_text(read('alert_single_inline_badge.txt'))
    assert [(j['title'], j['company'], j['location']) for j in jobs] == \
        [('Architecture Advisor (f/m/d)', 'Example Software AG', 'Kloten')]


def test_digest_lists_every_job():
    name, jobs = alerts.parse_alert_text(read('alert_digest.txt'))
    assert name == 'business architect in Zürich Metropolitan Area'
    assert [j['job_id'] for j in jobs] == ['1000000003', '1000000004']
    assert jobs[1]['title'] == 'Senior/Principal Enterprise Architect (f/m/d) - Switzerland'


def test_links_are_rebuilt_bare_never_kept_with_sign_in_tokens():
    for fixture in ('alert_single_blank_badge.txt', 'alert_digest.txt'):
        for job in alerts.parse_alert_text(read(fixture))[1]:
            assert job['link'] == f"https://www.linkedin.com/jobs/view/{job['job_id']}/"
            assert 'Token' not in job['link'] and '?' not in job['link']


def test_a_non_alert_email_yields_nothing():
    assert alerts.parse_alert_text(read('application_update.txt'))[1] == []


def test_html_only_email_falls_back_to_its_markup():
    msg = email.message.EmailMessage()
    msg.set_content('<html><body><table><tr><td>'
                    '<a href="https://www.linkedin.com/comm/jobs/view/1000000009/?otpToken=X">'
                    'Head of Data</a></td></tr></table></body></html>', subtype='html')
    _, jobs = alerts.parse_alert_text(alerts.message_text(msg))
    assert [j['job_id'] for j in jobs] == ['1000000009']


# --- end to end against a fake mailbox and a scratch database -------------

class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL, recording how the mailbox was opened."""
    selected_readonly = None

    def __init__(self, messages):
        self.messages = messages

    def __call__(self, host):
        return self

    def login(self, user, password):
        return 'OK', [b'']

    def select(self, folder, readonly=False):
        FakeIMAP.selected_readonly = readonly
        return 'OK', [b'1']

    def search(self, charset, *criteria):
        return 'OK', [b' '.join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, num, what):
        assert 'PEEK' in what, 'must not mark mail as read'
        return 'OK', [(b'1', self.messages[int(num) - 1])]

    def logout(self):
        pass


def make_mail(body, message_id, date='Tue, 22 Sep 2026 07:41:38 +0000'):
    msg = email.message.EmailMessage()
    msg['From'] = alerts.ALERT_SENDER
    msg['Subject'] = 'Example alert'
    msg['Message-ID'] = message_id
    msg['Date'] = date
    msg.set_content(body)
    return msg.as_bytes()


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    for k, v in {'IMAP_HOST': 'imap.example.com', 'IMAP_USER': 'u', 'IMAP_PASSWORD': 'p'}.items():
        monkeypatch.setenv(k, v)
    db.init_db()


def test_ingest_adds_jobs_once_and_leaves_the_mailbox_untouched(scratch_db):
    mailbox = FakeIMAP([make_mail(read('alert_digest.txt'), '<a@example.com>'),
                        make_mail(read('alert_single_inline_badge.txt'), '<b@example.com>'),
                        make_mail(read('application_update.txt'), '<c@example.com>')])
    first = alerts.ingest_alerts(imap_factory=mailbox)
    assert sum(first.values()) == 3
    assert FakeIMAP.selected_readonly is True

    conn = db.get_connection()
    rows = conn.execute("SELECT job_id, status, link, source, description FROM jobs ORDER BY job_id").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ['1000000002', '1000000003', '1000000004']
    assert {r[1] for r in rows} == {'new'}
    assert all('Token' not in r[2] for r in rows)
    assert rows[1][3] == 'alert: business architect in Zürich Metropolitan Area'

    # No description yet, so nothing is handed to the evaluator.
    assert db.get_unscored_jobs() == []

    # A second run over the same mailbox adds nothing.
    assert sum(alerts.ingest_alerts(imap_factory=mailbox).values()) == 0
