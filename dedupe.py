"""Is this advert already on the board? Shared by every source of jobs.

The same advert often arrives from several places - LinkedIn, Adzuna, an
employer's own feed, a board that re-lists it - under slightly different
titles. Two titles are the same role when they match once gender markers,
workload and bracketed asides are removed; the company must match too, unless
one copy comes from a board that re-lists other employers' adverts.
"""
import os
import re

import db

# Boards that re-list other employers' adverts under their own name. Job-Room
# is the Swiss public employment service's portal; much of what it lists is
# also on Adzuna under the real employer.
RELISTERS = {'job room'}

# Parts of a title that vary between copies of one advert: gender markers
# ("(m/w/d)", "all genders"), workload ("80 - 100 %") and anything in brackets.
TITLE_NOISE = re.compile(r'\([^)]*\)|\b[mwfdxa](?:\s*/\s*[mwfdxa])+\b|\ball genders\b'
                         r'|\d+\s*(?:[-–]\s*\d+\s*)?%', re.I)


def _norm(text):
    return re.sub(r'\W+', ' ', (text or '').lower()).strip()


def title_key(title):
    return _norm(TITLE_NOISE.sub(' ', title or ''))


# What differs between one employer's names on different boards: legal forms
# and the country of the local entity ("Example Switzerland", "Example AG",
# "Example"). The country is HOME_COUNTRY, in its own spellings (locales.py).
LEGAL_FORMS = ('ag', 'sa', 'gmbh', 'sarl', 's a r l', 's a', 'ltd', 'limited', 'inc', 'llc', 'plc',
               'se', 'nv', 'bv', 'spa', 'srl', 'co', 'kg', 'holding', 'group', 'groupe', 'gruppe',
               'international')
_noise = {}


def company_noise():
    import locales
    home = locales.home_country()
    if home not in _noise:
        words = LEGAL_FORMS + tuple(_norm(w) for w in locales.country_words(home))
        _noise[home] = re.compile(r'\b(?:' + '|'.join(re.escape(w) for w in words) + r')\b')
    return _noise[home]

def company_aliases():
    """One firm under two names, where a board shows one and LinkedIn the other.

    COMPANY_ALIASES in .env: "long name=short name" pairs, comma-separated.
    """
    pairs = {}
    for item in os.environ.get('COMPANY_ALIASES', '').split(','):
        if '=' in item:
            long, short = item.split('=', 1)
            pairs[_norm(long)] = _norm(short)
    return pairs


def company_key(company):
    """The employer, with legal forms and country names left out."""
    text = _norm(company).replace('&', ' ')
    text = re.sub(r'\s+', ' ', company_noise().sub(' ', text)).strip() or _norm(company)
    return company_aliases().get(text, text)


def same_company(a, b):
    a, b = company_key(a), company_key(b)
    return bool(a) and (a == b or a in RELISTERS or b in RELISTERS)


def already_listed(title, company):
    """True when the same role is already in the database, from LinkedIn or an
    earlier search - the same advert is often on several boards. The company
    must match too, unless either copy comes from a board that re-lists."""
    return duplicate_of(title, company) is not None


def duplicate_of(title, company, exclude=None):
    """The id of a job already in the database that is the same advert, or None."""
    key = title_key(title)
    conn = db.get_connection()
    rows = conn.execute('SELECT job_id, title, company FROM jobs').fetchall()
    conn.close()
    for job_id, other_title, other_company in rows:
        if job_id != exclude and title_key(other_title) == key and same_company(company, other_company):
            return job_id
    return None


# Which copy of a duplicated advert to keep: the one furthest along.
KEEP_ORDER = ('offer', 'interviewing', 'declined', 'applied', 'ready_to_submit', 'easy_apply', 'approved',
              'account_required', 'generated', 'to_apply', 'failed', 'new', 'scored', 'rejected')
APPLIED = {'applied', 'interviewing', 'offer', 'declined'}
SWEEPABLE = {'new', 'to_apply', 'generated'}


def sweep(dry_run=False):
    """Retire the extra copies of adverts that arrived from several sources.

    In each group of copies the one furthest along is kept. Another copy is
    moved to Rejected, with a note naming the one kept, when it is still
    waiting (New, To Do), or when it is approved or ready but the advert has
    already been applied to through the copy kept. Any other copy that records
    a decision is left alone. Returns [(job_id, kept_id)] for every copy moved.
    """
    conn = db.get_connection()
    rows = conn.execute("SELECT job_id, title, company, status, created_at FROM jobs").fetchall()
    groups = {}
    for job_id, title, company, status, created in rows:
        key = (title_key(title), company_key(company))
        if all(key):
            groups.setdefault(key, []).append((job_id, status, str(created or '')))
    rank = {s: i for i, s in enumerate(KEEP_ORDER)}
    moved = []
    for copies in groups.values():
        if len(copies) < 2:
            continue
        copies.sort(key=lambda c: (rank.get(c[1], len(rank)), c[2]))
        kept, kept_status = copies[0][0], copies[0][1]
        for job_id, status, _ in copies[1:]:
            if status in SWEEPABLE or (status in db.PROTECTED_STATUSES and status not in APPLIED
                                       and status != 'account_required' and kept_status in APPLIED):
                moved.append((job_id, kept, status))
    if not dry_run:
        import datetime
        now = datetime.datetime.now()
        for job_id, kept, status in moved:
            conn.execute("UPDATE jobs SET status = 'rejected' WHERE job_id = ?", (job_id,))
            conn.execute("INSERT INTO job_history (job_id, event_type, old_status, new_status, note, created_at) "
                         "VALUES (?, 'status_change', ?, 'rejected', ?, ?)",
                         (job_id, status, f'Duplicate of #{kept}', now))
        conn.commit()
    conn.close()
    return [(job_id, kept) for job_id, kept, _ in moved]
