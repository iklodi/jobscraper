"""Employers' decline emails from the candidate's decline folder."""
import email.message

import pytest

import db
import declines


class FakeIMAP:
    def __init__(self, messages):
        self.messages, self.selected, self.criteria = messages, None, None

    def __call__(self, host):
        return self

    def login(self, user, password):
        return 'OK', [b'']

    def select(self, folder, readonly=False):
        self.selected, self.readonly = folder, readonly
        return 'OK', [b'1']

    def search(self, charset, *criteria):
        self.criteria = criteria
        return 'OK', [b' '.join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, num, what):
        assert 'PEEK' in what
        return 'OK', [(b'1', self.messages[int(num) - 1])]

    def logout(self):
        pass


def mail(subject, body, message_id, sender='Talent Team <jobs@example.com>'):
    msg = email.message.EmailMessage()
    msg['From'], msg['Subject'], msg['Message-ID'] = sender, subject, message_id
    msg['Date'] = 'Wed, 21 Oct 2026 09:34:20 +0200'
    msg.set_content(body)
    return msg.as_bytes()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    for k, v in {'IMAP_HOST': 'imap.example.com', 'IMAP_USER': 'u', 'IMAP_PASSWORD': 'p',
                 'DECLINE_FOLDER': 'Job decline'}.items():
        monkeypatch.setenv(k, v)
    db.init_db()
    for job_id, title, company, status in [
            ('1', 'Data Architect', 'Example AG', 'applied'),
            ('2', 'AI Lead', 'Example AG', 'interviewing'),
            ('3', 'Consultant', 'Sample SA', 'applied'),
            ('4', 'Analyst', 'Other GmbH', 'to_apply')]:
        db.add_job(job_id, title, company, '', 'text', 'x')
        db.update_job_status(job_id, status)


def model(read, pick=None):
    """A fake model: `read` answers the reading prompt, `pick` the choice."""
    def ask(prompt, schema):
        if schema is declines.READ_SCHEMA:
            return read
        return {'job_id': pick, 'why': 'test'}
    return ask


def status(job_id):
    return db.get_job_status(job_id)


def history(job_id):
    conn = db.get_connection()
    rows = [r[0] for r in conn.execute("SELECT note FROM job_history WHERE job_id = ? AND note IS NOT NULL "
                                       "ORDER BY id", (job_id,)).fetchall()]
    conn.close()
    return rows


def test_a_matched_decline_moves_the_job_with_reason_and_source(board):
    box = FakeIMAP([mail('Your application', 'We chose another candidate.', '<a@example.com>')])
    lines = declines.ingest(box, model({'is_decline': True, 'company': 'Example AG Switzerland',
                                        'role': 'AI Lead', 'reason': 'Another candidate was chosen.'}, '2'))
    assert status('2') == 'declined' and status('1') == 'applied'
    assert box.selected == '"Job decline"' and 'FROM' not in box.criteria and box.readonly is True
    conn = db.get_connection()
    assert conn.execute("SELECT application_notes FROM jobs WHERE job_id = '2'").fetchone()[0] == \
        'Another candidate was chosen.'
    conn.close()
    note = history('2')[-1]
    assert note.startswith('Declined: Another candidate was chosen.\nFound in: email from Talent Team')
    assert '"Your application", 2026-10-21, folder "Job decline"' in note
    assert lines == ['Decline: AI Lead at Example AG Switzerland - moved to Declined. '
                     'Reason: Another candidate was chosen.']


def test_the_model_cannot_pick_a_job_at_another_company(board):
    box = FakeIMAP([mail('Update', 'No.', '<b@example.com>')])
    declines.ingest(box, model({'is_decline': True, 'company': 'Example AG', 'role': None,
                                'reason': 'No reason given'}, '3'))           # 3 is at Sample SA
    assert status('3') == 'applied' and status('1') == 'applied' and status('2') == 'interviewing'


@pytest.mark.parametrize('read, pick, expected', [
    ({'is_decline': False, 'company': 'Example AG', 'role': None, 'reason': None}, '1', 'does not read as a decline'),
    ({'is_decline': True, 'company': None, 'role': None, 'reason': 'x'}, '1', 'no company could be read'),
    ({'is_decline': True, 'company': 'Unknown Ltd', 'role': None, 'reason': 'x'}, None, 'no application at Unknown Ltd'),
    ({'is_decline': True, 'company': 'Example AG', 'role': None, 'reason': 'x'}, None, 'could not tell which of 2'),
    ({'is_decline': True, 'company': 'Other GmbH', 'role': None, 'reason': 'x'}, '4', 'no application at Other GmbH'),
])
def test_anything_unclear_changes_nothing(board, read, pick, expected):
    lines = declines.ingest(FakeIMAP([mail('S', 'B', '<c@example.com>')]), model(read, pick))
    assert expected in lines[0] and 'nothing changed' in lines[0]
    assert [status(j) for j in '1234'] == ['applied', 'interviewing', 'applied', 'to_apply']


def test_each_email_is_handled_once_but_a_silent_model_means_retry(board):
    box = FakeIMAP([mail('S', 'B', '<d@example.com>')])
    assert 'did not answer' in declines.ingest(box, lambda prompt, schema: None)[0]
    good = model({'is_decline': True, 'company': 'Sample SA', 'role': None, 'reason': 'Filled.'}, '3')
    assert len(declines.ingest(box, good)) == 1 and status('3') == 'declined'
    assert declines.ingest(box, good) == []


def test_a_job_already_declined_only_gets_the_reason(board):
    db.update_job_status('3', 'declined')
    declines.ingest(FakeIMAP([mail('S', 'B', '<e@example.com>')]),
                    model({'is_decline': True, 'company': 'Sample SA', 'role': None, 'reason': 'Filled.'}, '3'))
    conn = db.get_connection()
    changes = conn.execute("SELECT COUNT(*) FROM job_history WHERE job_id = '3' AND new_status = 'declined'").fetchone()[0]
    conn.close()
    assert changes == 1 and history('3')[-1].startswith('Declined: Filled.')


def threaded(subject, body, message_id, date, refs='', sender='Talent Team <jobs@example.com>', to='me@example.com'):
    msg = email.message.EmailMessage()
    msg['From'], msg['To'], msg['Subject'], msg['Message-ID'], msg['Date'] = sender, to, subject, message_id, date
    if refs:
        msg['References'] = refs
        msg['In-Reply-To'] = refs.split()[-1]
    msg.set_content(body)
    return msg.as_bytes()


def test_a_conversation_is_read_once_from_its_newest_email(board, monkeypatch):
    monkeypatch.setenv('IMAP_USER', 'me@example.com')
    box = FakeIMAP([
        threaded('Interview next week', 'When are you free?', '<t1@example.com>', 'Mon, 5 Oct 2026 09:00:00 +0000'),
        threaded('Re: Interview next week', 'Tuesday works.', '<t2@example.com>', 'Tue, 6 Oct 2026 09:00:00 +0000',
                 refs='<t1@example.com>', sender='Me <me@example.com>', to='jobs@example.com'),
        threaded('Re: Interview next week', 'We have decided not to proceed.', '<t3@example.com>',
                 'Wed, 21 Oct 2026 09:00:00 +0000', refs='<t1@example.com> <t2@example.com>'),
    ])
    seen = []

    def ask(prompt, schema):
        if schema is declines.READ_SCHEMA:
            seen.append(prompt)
            return {'is_decline': True, 'company': 'Sample SA', 'role': None, 'reason': 'Not proceeding.'}
        return {'job_id': '3', 'why': 'only one'}
    lines = declines.ingest(box, ask)
    assert len(lines) == 1 and len(seen) == 1
    assert 'We have decided not to proceed.' in seen[0] and 'newest of 3 emails' in seen[0]
    assert 'newest of 3 in the conversation' in history('3')[-1]
    conn = db.get_connection()
    assert conn.execute('SELECT COUNT(*) FROM decline_emails').fetchone()[0] == 3
    conn.close()
    assert declines.ingest(box, ask) == []


def test_the_same_generic_subject_from_two_senders_stays_two_declines(board):
    box = FakeIMAP([
        threaded('Your application', 'Not this time.', '<u1@example.com>', 'Mon, 5 Oct 2026 09:00:00 +0000',
                 sender='Example AG <no-reply@ats.example>'),
        threaded('Your application', 'Position filled.', '<u2@example.com>', 'Tue, 6 Oct 2026 09:00:00 +0000',
                 sender='Sample SA <hr@sample.example>'),
    ])
    lines = declines.ingest(box, model({'is_decline': False, 'company': None, 'role': None, 'reason': None}))
    assert len(lines) == 2
