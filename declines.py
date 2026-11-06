"""Employers' decline emails, from a mail folder the candidate files them in.

Declines arrive from anyone - an applicant-tracking system, a recruiter, the
hiring manager - in any wording and language, so they are not parsed by
pattern. The candidate files them under a label (DECLINE_FOLDER, e.g. a Gmail
label), and for each new email the model reads who declined, for which role,
and why. The code then picks the candidate jobs: applications in Applied,
Interviewing or Declined at that company (dedupe.company_key). The model
chooses among those, or none; it is never offered a job at another company.

A whole conversation is often filed under the label, not just the refusal in
it, so the emails are grouped into conversations (`threads`) and only the
newest of each is read - the refusal, with the earlier exchange quoted below
it - while the others are recorded as handled with it.

A match moves the job to Declined, with the reason in its notes and a history
entry saying where it was found (sender, subject, date, folder). A job already
in Declined only gets the note. Anything else - no company, no candidate, no
confident pick, or an email that is not a decline - goes to the summary and
changes nothing: a wrong match would hide a live application.

Mail is read as alerts.py reads it, with BODY.PEEK, and never marked or moved.
The email's text goes only to Infomaniak or Claude (llm.py, LLM_PROVIDERS),
never to the Gemini or Groq fallbacks.

Settings (.env): DECLINE_FOLDER (empty = off), DECLINE_LOOKBACK_DAYS (default
90, first run only).
"""

import datetime
import email.utils
import os
import re

import alerts
import db
import llm
from dedupe import same_company

CANDIDATE_STATUSES = ('applied', 'interviewing', 'declined')
BODY_LIMIT = 6000

READ_SCHEMA = {
    'type': 'object',
    'properties': {
        'is_decline': {'type': 'boolean'},
        'company': {'type': ['string', 'null']},
        'role': {'type': ['string', 'null']},
        'reason': {'type': ['string', 'null']},
    },
    'required': ['is_decline', 'company', 'role', 'reason'],
}

PICK_SCHEMA = {
    'type': 'object',
    'properties': {'job_id': {'type': ['string', 'null']}, 'why': {'type': 'string'}},
    'required': ['job_id', 'why'],
}

READ_PROMPT = """This email was filed by a job seeker as a decline of one of their job applications.

From: {sender}
Subject: {subject}
Date: {date}{thread_note}

{body}

Report, as JSON:
- "is_decline": true if the email tells the candidate their application will not go further (rejected, position filled, another candidate chosen, process closed). False for anything else (an invitation, an acknowledgement, a newsletter).
- "company": the employer the application was for, as it would appear on a job advert. If the email comes from a recruitment agency on a client's behalf and names the client, give the client. null if it cannot be told.
- "role": the job title applied for, if the email names it, else null.
- "reason": the reason given for the decline, in one or two short sentences in English, close to the email's own words. "No reason given" when it gives none.
"""

PICK_PROMPT = """A job seeker received this decline:

Company: {company}
Role named in the email: {role}
Subject: {subject}
Excerpt: {excerpt}

These are their applications at that company:
{candidates}

Which application is being declined? Answer with its job_id. If the email names a role that matches none of them, or the email could be about more than one and nothing tells them apart, answer null. Do not guess.
"""


def configured():
    return alerts.configured() and bool(folder()) and llm.configured(private=True)


def folder():
    return os.environ.get('DECLINE_FOLDER', '').strip()


def candidates(company):
    """Applications at `company` that a decline could be about."""
    conn = db.get_connection()
    marks = ','.join('?' * len(CANDIDATE_STATUSES))
    rows = conn.execute(f'SELECT job_id, title, company, status FROM jobs WHERE status IN ({marks})',
                        CANDIDATE_STATUSES).fetchall()
    conn.close()
    return [r for r in rows if same_company(r[2], company)]


def read_decline(message, ask, thread_size=1):
    body = alerts.message_text(message)[:BODY_LIMIT]
    note = (f'\n(This is the newest of {thread_size} emails in one conversation; earlier ones '
            f'may be quoted below it.)' if thread_size > 1 else '')
    prompt = READ_PROMPT.format(sender=alerts._decode(message.get('From')),
                                subject=alerts._decode(message.get('Subject')),
                                date=message.get('Date') or '', body=body, thread_note=note)
    return ask(prompt, READ_SCHEMA), body


def pick(facts, subject, body, jobs, ask):
    """The job_id the decline is about, or None."""
    listing = '\n'.join(f'- job_id {job_id}: {title} ({status})' for job_id, title, _, status in jobs)
    answer = ask(PICK_PROMPT.format(company=facts['company'], role=facts.get('role') or 'none',
                                    subject=subject, excerpt=body[:1500], candidates=listing),
                 PICK_SCHEMA) or {}
    chosen = str(answer.get('job_id') or '')
    return chosen if chosen in {job_id for job_id, *_ in jobs} else None


def file_decline(job_id, status, reason, source):
    """Move the job to Declined (unless it is there) and record why and where."""
    now = datetime.datetime.now()
    conn = db.get_connection()
    if status != 'declined':
        conn.execute("UPDATE jobs SET status = 'declined' WHERE job_id = ?", (job_id,))
        conn.execute("INSERT INTO job_history (job_id, event_type, old_status, new_status, note, created_at) "
                     "VALUES (?, 'status_change', ?, 'declined', ?, ?)", (job_id, status, source, now))
    conn.execute('UPDATE jobs SET application_notes = ? WHERE job_id = ?', (reason, job_id))
    conn.execute("INSERT INTO job_history (job_id, event_type, note, created_at) VALUES (?, 'note_added', ?, ?)",
                 (job_id, f'Declined: {reason}\nFound in: {source}', now))
    conn.commit()
    conn.close()


REPLY_PREFIX = re.compile(r'^\s*((re|fwd?|aw|wg|tr|rif|sv|vs)\s*(\[\d+\])?\s*:\s*)+', re.I)


def subject_key(message):
    """The subject without reply and forward prefixes (Re:, Fwd:, AW:, TR:...)."""
    return ' '.join(REPLY_PREFIX.sub('', alerts._decode(message.get('Subject')) or '').lower().split())


def correspondent(message):
    """The other party's address: the sender, or for the candidate's own
    replies, the recipient."""
    me = os.environ.get('IMAP_USER', '').strip().lower()
    sender = email.utils.parseaddr(message.get('From') or '')[1].lower()
    if me and sender == me:
        return email.utils.parseaddr(message.get('To') or '')[1].lower()
    return sender


def threads(messages):
    """Group [(message_id, message)] into conversations, newest message last
    in each. Two emails are one conversation when one's References or
    In-Reply-To names the other's thread, or - for mail without those headers
    - when they are with the same correspondent and their subjects match once
    the reply prefixes are gone. Subject alone is not enough: applicant-
    tracking systems send many employers' declines as "Your application"."""
    parent = {}

    def find(key):
        while parent.setdefault(key, key) != key:
            key = parent[key]
        return key

    def join(a, b):
        parent[find(a)] = find(b)

    for message_id, message in messages:
        ids = re.findall(r'<[^>]+>', (message.get('References') or '') + ' ' + (message.get('In-Reply-To') or ''))
        for other in ids:
            join(message_id, other)
        if subject_key(message):
            join(message_id, f'subject:{correspondent(message)}:{subject_key(message)}')
    groups = {}
    for message_id, message in messages:
        groups.setdefault(find(message_id), []).append((message_id, message))
    for group in groups.values():
        group.sort(key=lambda item: alerts.received_at(item[1]))
    return sorted(groups.values(), key=lambda g: alerts.received_at(g[-1][1]))


def handle(message, ask, thread_size=1):
    """Act on one decline email. Returns (job_id or None, summary line, done):
    done is False when the model did not answer, so the email is read again
    next run."""
    sender = alerts._decode(message.get('From'))
    subject = alerts._decode(message.get('Subject'))
    when = alerts.received_at(message)[:10]
    source = (f'email from {sender}, "{subject}", {when}, folder "{folder()}"'
              + (f' (newest of {thread_size} in the conversation)' if thread_size > 1 else ''))
    facts, body = read_decline(message, ask, thread_size)
    if not facts:
        return None, f'Decline email "{subject}" ({when}): the model did not answer; left for the next run.', False
    if not facts.get('is_decline'):
        return None, f'"{subject}" ({when}) in "{folder()}" does not read as a decline; nothing changed.', True
    company = (facts.get('company') or '').strip()
    if not company:
        return None, f'Decline "{subject}" ({when}): no company could be read; nothing changed.', True
    jobs = candidates(company)
    if not jobs:
        return None, f'Decline from {company} ({when}): no application at {company} on the board; nothing changed.', True
    job_id = pick(facts, subject, body, jobs, ask)
    if not job_id:
        return None, (f'Decline from {company} ({when}): could not tell which of {len(jobs)} '
                      f'application(s) it is about; nothing changed.'), True
    title, status = next((t, s) for j, t, _, s in jobs if j == job_id)
    reason = (facts.get('reason') or 'No reason given').strip()
    file_decline(job_id, status, reason, source)
    moved = 'already in Declined, reason added' if status == 'declined' else 'moved to Declined'
    return job_id, f'Decline: {title} at {company} - {moved}. Reason: {reason}', True


def ingest(imap_factory=None, ask=None):
    """Read new emails in DECLINE_FOLDER and file them. Returns summary lines.
    `ask(prompt, schema)` is for tests; by default Infomaniak or Claude (llm.py)."""
    db.init_db()
    ask = ask or (lambda prompt, schema: llm.chat_json(prompt, schema, private=True))
    lookback = int(os.environ.get('DECLINE_LOOKBACK_DAYS', '90'))
    lines = []
    unread = list(alerts.read_mail(None, 'decline_emails', imap_factory,
                                   folder=folder(), lookback_days=lookback))
    for conversation in threads(unread):
        newest = conversation[-1][1]
        job_id, line, done = handle(newest, ask, len(conversation))
        lines.append(line)
        print('  ' + line)
        if not done:
            continue                  # not recorded: tried again next run
        conn = db.get_connection()
        for message_id, message in conversation:
            conn.execute('INSERT OR IGNORE INTO decline_emails (message_id, received_at, job_id, result, '
                         'processed_at) VALUES (?, ?, ?, ?, ?)',
                         (message_id, alerts.received_at(message), job_id, line,
                          datetime.datetime.now().isoformat(sep=' ', timespec='seconds')))
        conn.commit()
        conn.close()
    return lines


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(override=True)
    if not configured():
        raise SystemExit('Set IMAP_HOST, IMAP_USER, IMAP_PASSWORD and DECLINE_FOLDER in .env first.')
    if not ingest():
        print('No new decline emails.')
