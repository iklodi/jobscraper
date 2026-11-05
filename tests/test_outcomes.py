"""Application outcomes from LinkedIn's status emails (issue #19).

The messages copy the shape of real ones - the meaning is in the subject and in
LinkedIn's tracking names in the links - with every name and token replaced.
"""
import email.message

import pytest

import db
import outcomes

FOOTER = ('\n----------------------------------------\n\nThis email was intended for Jane Doe\n'
          'Learn why we included this: https://www.linkedin.com/help/linkedin/answer/4788?'
          'lipi=urn%3Ali%3Apage%3A{kind}%3BFAKE&otpToken=FAKETOKEN\n')


def mail(subject, kind, message_id, top=''):
    msg = email.message.EmailMessage()
    msg['From'] = outcomes.SENDER
    msg['Subject'] = subject
    msg['Message-ID'] = message_id
    msg['Date'] = 'Thu, 24 Sep 2026 10:43:18 +0000'
    msg.set_content(top + FOOTER.format(kind=kind))
    return msg.as_bytes()


def rejected(role, company, n):
    return mail(f'Your application to {role} at {company}', 'email_jobs_application_rejected_01',
                f'<r{n}@x>', top=f'Your update from {company}\n')


def viewed(company, n):
    return mail(f'Your application was viewed by {company}', 'email_jobs_job_application_viewed_01',
                f'<v{n}@x>')


class FakeIMAP:
    readonly = None

    def __init__(self, messages):
        self.messages = messages

    def __call__(self, host):
        return self

    def login(self, *a):
        return 'OK', [b'']

    def select(self, folder, readonly=False):
        FakeIMAP.readonly = readonly
        return 'OK', [b'1']

    def search(self, charset, *criteria):
        assert outcomes.SENDER in ' '.join(criteria)
        return 'OK', [b' '.join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, num, what):
        assert 'PEEK' in what
        return 'OK', [(b'1', self.messages[int(num) - 1])]

    def logout(self):
        pass


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    for k, v in {'IMAP_HOST': 'imap.example.com', 'IMAP_USER': 'u', 'IMAP_PASSWORD': 'p'}.items():
        monkeypatch.setenv(k, v)
    db.init_db()
    for job_id, title, company, status in [
            ('1', 'Senior Consultant, Lead Architecture (m/w/d)', 'Example Conseil', 'applied'),
            ('2', 'AI Engineer', 'Example Group AG', 'applied'),
            ('3', 'Data Architect', 'Twice Corp', 'applied'),
            ('4', 'Cloud Architect', 'Twice Corp', 'applied'),
            ('5', 'Head of AI', 'Not Yet AG', 'approved')]:
        db.add_job(job_id, title, company, '', 'text', f'https://example.com/{job_id}')
        db.update_job_status(job_id, status)


def status(job_id):
    return db.get_job_status(job_id)


def notes(job_id):
    conn = db.get_connection()
    row = conn.execute('SELECT application_notes FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
    conn.close()
    return row[0] or ''


def test_a_rejection_matching_one_applied_job_moves_it(board):
    lines = outcomes.ingest(FakeIMAP([rejected('Senior Consultant, Lead Architecture', 'Example Conseil', 1)]))
    assert status('1') == 'declined'
    assert 'declined' in notes('1')
    assert lines == ['LinkedIn: Senior Consultant, Lead Architecture (m/w/d) at Example Conseil - '
                     'application declined; moved to Declined.']
    assert FakeIMAP.readonly is True


def test_a_rejection_for_another_role_at_the_company_changes_nothing(board):
    lines = outcomes.ingest(FakeIMAP([rejected('Machine Learning Engineer', 'Example Group AG', 1)]))
    assert status('2') == 'applied'
    assert 'Matched no job' in lines[0]


def test_viewed_adds_a_note_only(board):
    outcomes.ingest(FakeIMAP([viewed('Example Group AG', 1)]))
    assert status('2') == 'applied'
    assert 'viewed by Example Group AG' in notes('2')


def test_ambiguous_viewed_is_reported_not_applied(board):
    lines = outcomes.ingest(FakeIMAP([viewed('Twice Corp', 1)]))
    assert 'Matched 2 jobs' in lines[0]
    assert notes('3') == '' and notes('4') == ''


def test_only_jobs_in_applied_are_matched(board):
    outcomes.ingest(FakeIMAP([rejected('Head of AI', 'Not Yet AG', 1)]))
    assert status('5') == 'approved'


def test_other_linkedin_mail_is_ignored_and_nothing_is_read_twice(board):
    recommendation = mail('New jobs similar to Architect at Example', 'email_jobs_similar_jobs_01', '<o@x>')
    box = FakeIMAP([recommendation, viewed('Example Group AG', 1)])
    assert len(outcomes.ingest(box)) == 1
    assert outcomes.ingest(box) == []


@pytest.mark.parametrize('subject, body, expected', [
    ('Your application to Head of AI at Scale at Example AG', 'email_jobs_application_rejected_01',
     ('rejected', 'Example AG', 'Head of AI at Scale')),
    ('Your application was viewed by Example AG', '', ('viewed', 'Example AG', None)),
    ('Your application to Architect at Example AG', 'email_jobs_application_sent_01', (None, None, None)),
])
def test_classify(subject, body, expected):
    assert outcomes.classify(subject, body) == expected
