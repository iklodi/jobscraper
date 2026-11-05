"""Application outcomes from LinkedIn's status emails (issue #19).

LinkedIn emails jobs-noreply@linkedin.com updates about applications sent
through it, or recorded on it: "Your application to <role> at <company>" (a
rejection, when its links say email_jobs_application_rejected) and "Your
application was viewed by <company>". The body carries almost nothing; the
subject and LinkedIn's own tracking name in the links carry the meaning.

Only jobs already in Applied are ever matched, and nothing is guessed:
- a rejection needs the company and the role to match exactly one job, which
  then moves to Declined with a note;
- "viewed" needs the company to match exactly one job, and only adds a note;
- anything else is reported in the summary, never acted on.

Mail is read exactly as alerts.py reads it: over IMAP, read-only.
"""

import datetime
import re

import alerts
import db
from dedupe import _norm, title_key

SENDER = 'jobs-noreply@linkedin.com'

APPLIED = ('applied',)

SUBJECT_TO = re.compile(r'^\s*Your application to (?P<role>.+) at (?P<company>.+?)\s*$', re.I | re.S)
SUBJECT_VIEWED = re.compile(r'^\s*Your application was viewed by (?P<company>.+?)\s*$', re.I | re.S)
REJECTED = re.compile(r'email_jobs_application_rejected', re.I)
VIEWED = re.compile(r'email_jobs_job_application_viewed', re.I)


def classify(subject, body):
    """(kind, company, role) - kind is 'rejected', 'viewed' or None."""
    subject = ' '.join((subject or '').split())
    m = SUBJECT_VIEWED.match(subject)
    if m or VIEWED.search(body or ''):
        return 'viewed', (m.group('company') if m else None), None
    m = SUBJECT_TO.match(subject)
    if m and REJECTED.search(body or ''):
        return 'rejected', m.group('company'), m.group('role')
    return None, None, None


def applied_matches(company, role=None):
    """Jobs in Applied at `company` (and, given a role, with that title)."""
    conn = db.get_connection()
    rows = conn.execute(
        f"SELECT job_id, title, company FROM jobs WHERE status IN ({','.join('?' * len(APPLIED))})",
        APPLIED).fetchall()
    conn.close()
    matches = [r for r in rows if _norm(r[2]) == _norm(company)]
    if role is not None:
        matches = [r for r in matches if title_key(r[1]) == title_key(role)]
    return matches


def handle(kind, company, role, when):
    """Act on one outcome. Returns a line for the summary."""
    what = f'{role} at {company}' if role else company
    if not company:
        return f'An application update could not be read (no company in the subject); not acted on.'
    matches = applied_matches(company, role if kind == 'rejected' else None)
    if len(matches) != 1:
        found = 'no job' if not matches else f'{len(matches)} jobs'
        return (f'LinkedIn: application {kind} - {what}. Matched {found} in Applied, '
                f'so nothing was changed.')
    job_id, title, _ = matches[0]
    if kind == 'rejected':
        db.update_job_status(job_id, 'declined')
        db.add_job_note(job_id, f'LinkedIn: application declined ({when}).')
        return f'LinkedIn: {title} at {company} - application declined; moved to Declined.'
    db.add_job_note(job_id, f'LinkedIn: application viewed by {company} ({when}).')
    return f'LinkedIn: {title} at {company} - application viewed.'


def ingest(imap_factory=None):
    """Read new status emails and apply them. Returns summary lines."""
    db.init_db()
    lines = []
    for message_id, message in alerts.read_mail(SENDER, 'outcome_emails', imap_factory):
        subject = alerts._decode(message.get('Subject'))
        kind, company, role = classify(subject, _raw_text(message))
        when = alerts.received_at(message)[:10]
        line = handle(kind, company, role, when) if kind else None
        if line:
            lines.append(line)
            print('  ' + line)
        conn = db.get_connection()
        conn.execute('INSERT OR IGNORE INTO outcome_emails (message_id, received_at, kind, company, '
                     'role, result, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (message_id, alerts.received_at(message), kind, company, role, line,
                      datetime.datetime.now().isoformat(sep=' ', timespec='seconds')))
        conn.commit()
        conn.close()
    return lines


def _raw_text(message):
    """Every text part, undecoded links included - the tracking names live there."""
    parts = []
    for part in message.walk():
        if part.get_content_maintype() == 'text':
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(part.get_content_charset() or 'utf-8', errors='replace'))
    return '\n'.join(parts)


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    if not alerts.configured():
        raise SystemExit('Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD in .env first.')
    for line in ingest():
        print(line)
