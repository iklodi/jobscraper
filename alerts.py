"""Safe mode: new jobs from LinkedIn's job-alert emails (issue #13).

LinkedIn emails a digest for each saved job alert. This reads those emails
over IMAP and turns every job they list into a 'new' row, so safe mode never
browses LinkedIn on the candidate's account to find jobs.

Two rules hold throughout:

* Only the numeric job id is taken from each "View job" link. The links carry
  sign-in parameters (otpToken, midToken), so following or storing one would
  tie later requests back to the account. Links are rebuilt bare, from the id.
* Mail is read with BODY.PEEK and never flagged, moved or deleted: the inbox is
  left exactly as the candidate had it. Processed messages are remembered in
  the alert_emails table instead.

Configuration (.env): IMAP_HOST, IMAP_USER, IMAP_PASSWORD (an app password),
IMAP_FOLDER (default INBOX), ALERT_LOOKBACK_DAYS (default 14, first run only).
"""

import datetime
import email
import email.utils
import html
import imaplib
import os
import re
from email.header import decode_header, make_header

import db

ALERT_SENDER = 'jobalerts-noreply@linkedin.com'

JOB_ID = re.compile(r'linkedin\.com/(?:comm/)?jobs/view/(\d+)')
# "Your job alert for X in Y" in digests; "Your job alert has been created:
# X in Y." in the one-off confirmation LinkedIn sends when an alert is set up.
ALERT_NAME = re.compile(r'^\s*Your job alert (?:for|has been created:) (.+?)\.?\s*$', re.M)
# LinkedIn lists at most this many jobs in one email and links the rest to a
# logged-in search, which safe mode does not open. An email at the cap may
# have left jobs out.
EMAIL_JOB_CAP = 6
SEPARATOR = re.compile(r'^\s*-{8,}\s*$', re.M)
# Header lines that sit above the first job and are not part of it.
PREAMBLE = re.compile(r'^(your job alert (for|has been created)\b|a new job matches\b|new jobs match\b'
                      r'|you[’\']ll receive notifications\b)', re.I)


def job_url(job_id):
    """The public posting, rebuilt from the id - never the tracked email link."""
    return f'https://www.linkedin.com/jobs/view/{job_id}/'


def parse_alert_text(text):
    """(alert_name, [job, ...]) from the plain-text part of an alert email.

    Each job is a block of title, company and location lines, sometimes a
    badge line ("Fast growing", "This company is actively hiring"), then a
    "View job:" link. Digests separate the blocks with rules of dashes.
    """
    name_match = ALERT_NAME.search(text or '')
    alert_name = name_match.group(1) if name_match else None

    jobs, seen = [], set()
    for section in SEPARATOR.split(text or ''):
        view = next((l for l in section.splitlines() if l.strip().lower().startswith('view job')), None)
        if not view:
            continue
        found = JOB_ID.search(view)
        if not found or found.group(1) in seen:
            continue
        job_id = found.group(1)
        seen.add(job_id)

        before = section[:section.index(view)]
        lines = [l.strip() for l in before.splitlines() if l.strip()]
        lines = [l for l in lines if not PREAMBLE.match(l)]
        title, company, location = (lines + [None, None, None])[:3]
        jobs.append({
            'job_id': job_id,
            'title': title,
            'company': company,
            'location': location,
            'link': job_url(job_id),
        })
    return alert_name, jobs


class _TextOfHtml:
    """Last-resort plain text for an email with no text/plain part.

    Keeps each job link's id on a "View job:" line of its own, so the same
    parser handles both parts.
    """

    def __call__(self, markup):
        markup = re.sub(r'(?is)<(script|style).*?</\1>', ' ', markup or '')
        markup = re.sub(r'(?is)<a\b[^>]*href="([^"]*jobs/view/\d+[^"]*)"[^>]*>.*?</a>',
                        lambda m: '\nView job: ' + html.unescape(m.group(1)) + '\n', markup)
        markup = re.sub(r'(?i)<br\s*/?>|</(p|div|tr|td|h\d|li|table)>', '\n', markup)
        text = html.unescape(re.sub(r'<[^>]+>', ' ', markup))
        return '\n'.join(re.sub(r'[ \t\xa0]+', ' ', l).strip() for l in text.splitlines())


html_to_text = _TextOfHtml()


def message_text(message):
    """The text/plain body of an email, or text made from its HTML."""
    plain = rich = None
    for part in message.walk():
        if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition', '').startswith('attachment'):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        body = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
        if part.get_content_type() == 'text/plain' and plain is None:
            plain = body
        elif part.get_content_type() == 'text/html' and rich is None:
            rich = body
    if plain and JOB_ID.search(plain):
        return plain
    return html_to_text(rich) if rich else (plain or '')


def configured():
    return all(os.environ.get(k) for k in ('IMAP_HOST', 'IMAP_USER', 'IMAP_PASSWORD'))


def _decode(value):
    try:
        return str(make_header(decode_header(value or '')))
    except Exception:
        return value or ''


def _already_processed(conn, message_id):
    return conn.execute('SELECT 1 FROM alert_emails WHERE message_id = ?',
                        (message_id,)).fetchone() is not None


def store_jobs(alert_name, jobs):
    """Add the jobs an alert listed; returns how many were new.

    Title, company and location come from the email, so a job is on the board
    straight away. Its description is fetched later, logged out (#14); until
    then it stays 'new' and out of scoring.
    """
    added = 0
    source = f'alert: {alert_name}' if alert_name else 'alert'
    for job in jobs:
        if db.job_exists(job['job_id']):
            continue
        if db.add_job(job['job_id'], job['title'] or 'Unknown role', job['company'] or 'Unknown',
                      job['location'] or '', '', job['link'], False):
            conn = db.get_connection()
            conn.execute('UPDATE jobs SET source = ? WHERE job_id = ?', (source, job['job_id']))
            conn.commit()
            conn.close()
            added += 1
    return added


def ingest_alerts(imap_factory=None):
    """Read new alert emails and add their jobs. Returns {alert name: jobs added}.

    imap_factory is for tests; by default it connects with IMAP4_SSL.
    """
    db.init_db()
    host = os.environ.get('IMAP_HOST')
    folder = os.environ.get('IMAP_FOLDER', 'INBOX')
    lookback = int(os.environ.get('ALERT_LOOKBACK_DAYS', '14'))

    conn = db.get_connection()
    newest = conn.execute('SELECT MAX(received_at) FROM alert_emails').fetchone()[0]
    conn.close()
    if newest:
        # Start a day before the newest email already read, to be safe across
        # time zones; anything already processed is skipped by Message-ID.
        since = datetime.datetime.fromisoformat(str(newest)[:19]) - datetime.timedelta(days=1)
    else:
        since = datetime.datetime.now() - datetime.timedelta(days=lookback)

    imap = (imap_factory or imaplib.IMAP4_SSL)(host)
    stats = {}
    try:
        imap.login(os.environ['IMAP_USER'], os.environ['IMAP_PASSWORD'])
        imap.select(f'"{folder}"', readonly=True)       # readonly: nothing gets marked
        status, data = imap.search(None, 'FROM', f'"{ALERT_SENDER}"',
                                   'SINCE', since.strftime('%d-%b-%Y'))
        ids = data[0].split() if status == 'OK' and data and data[0] else []
        print(f'{len(ids)} job-alert email(s) since {since:%Y-%m-%d}.')

        for num in ids:
            status, parts = imap.fetch(num, '(BODY.PEEK[])')
            if status != 'OK' or not parts or not isinstance(parts[0], tuple):
                continue
            message = email.message_from_bytes(parts[0][1])
            message_id = (message.get('Message-ID') or '').strip() or f'no-id:{num.decode()}'
            conn = db.get_connection()
            if _already_processed(conn, message_id):
                conn.close()
                continue
            conn.close()

            alert_name, jobs = parse_alert_text(message_text(message))
            added = store_jobs(alert_name, jobs)
            key = alert_name or _decode(message.get('Subject')) or 'job alert'
            stats[key] = stats.get(key, 0) + added

            received = email.utils.parsedate_to_datetime(message.get('Date')) \
                if message.get('Date') else datetime.datetime.now()
            conn = db.get_connection()
            conn.execute(
                'INSERT OR IGNORE INTO alert_emails '
                '(message_id, received_at, alert, jobs_listed, jobs_added, processed_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (message_id, received.replace(tzinfo=None).isoformat(sep=' '),
                 alert_name, len(jobs), added, datetime.datetime.now().isoformat(sep=" ", timespec="seconds")))
            conn.commit()
            conn.close()
            if jobs:
                print(f'  {key}: {len(jobs)} listed, {added} new')
            if len(jobs) >= EMAIL_JOB_CAP:
                print(f'  {key}: the email is at LinkedIn\'s {EMAIL_JOB_CAP}-job cap, so more may '
                      f'match than it listed. Narrow the alert or have it sent more often.')
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return stats


# Locations too broad for one alert: its email would hit the six-job cap.
WIDE_LOCATIONS = {'worldwide', 'europe', 'emea', 'remote', 'anywhere'}


def recent_alerts(days=14):
    """The alerts seen in emails over the last `days`, busiest first:
    [{'alert', 'emails', 'listed', 'added', 'capped', 'last'}]."""
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat(sep=' ')
    conn = db.get_connection()
    rows = conn.execute(
        'SELECT COALESCE(alert, ?), COUNT(*), SUM(jobs_listed), SUM(jobs_added), '
        'SUM(jobs_listed >= ?), MAX(received_at) FROM alert_emails WHERE received_at >= ? '
        'GROUP BY 1 ORDER BY 3 DESC',
        ('job alert', EMAIL_JOB_CAP, since)).fetchall()
    conn.close()
    return [dict(zip(('alert', 'emails', 'listed', 'added', 'capped', 'last'), r)) for r in rows]


def suggested_alerts(keywords, locations):
    """The LinkedIn job alerts that reproduce search_criteria.md: one per
    keyword and location, as [{'keyword', 'location', 'wide'}]. `wide` marks a
    location broad enough that the alert's email will hit the six-job cap -
    better split by country."""
    out = []
    for keyword, pinned in keywords:
        for location in pinned or locations:
            item = {'keyword': keyword, 'location': location,
                    'wide': location.strip().lower() in WIDE_LOCATIONS}
            if item not in out:
                out.append(item)
    return out


def capped_since(since):
    """{alert: emails} for emails processed since `since` (ISO string) that
    listed a full EMAIL_JOB_CAP jobs, and so may have left some out."""
    conn = db.get_connection()
    rows = conn.execute('SELECT COALESCE(alert, ?), COUNT(*) FROM alert_emails '
                        'WHERE processed_at >= ? AND jobs_listed >= ? GROUP BY 1',
                        ('job alert', since, EMAIL_JOB_CAP)).fetchall()
    conn.close()
    return dict(rows)


if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    if '--suggest' in sys.argv:
        # The alerts to create on LinkedIn -> Jobs -> Job alerts, from the
        # searches in search_criteria.md. Reading the file touches nothing.
        from scraper import get_search_criteria
        keywords, locations, _ = get_search_criteria()
        print('Create these on LinkedIn (Jobs -> search -> "Set alert"), daily, by email:')
        for item in suggested_alerts(keywords, locations):
            print(f"  {item['keyword']}  in  {item['location']}"
                  + ('   <- broad: one alert per country fits LinkedIn\'s six-job emails better'
                     if item['wide'] else ''))
        raise SystemExit(0)
    if not configured():
        raise SystemExit('Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD in .env first.')
    print(ingest_alerts())
