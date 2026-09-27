"""Is this advert already on the board? Shared by every source of jobs.

The same advert often arrives from several places - LinkedIn, Adzuna, an
employer's own feed, a board that re-lists it - under slightly different
titles. Two titles are the same role when they match once gender markers,
workload and bracketed asides are removed; the company must match too, unless
one copy comes from a board that re-lists other employers' adverts.
"""
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


def already_listed(title, company):
    """True when the same role is already in the database, from LinkedIn or an
    earlier search - the same advert is often on several boards. The company
    must match too, unless either copy comes from a board that re-lists."""
    key, company = title_key(title), _norm(company)
    conn = db.get_connection()
    rows = conn.execute('SELECT title, company FROM jobs').fetchall()
    conn.close()
    for other_title, other_company in rows:
        if title_key(other_title) != key:
            continue
        other_company = _norm(other_company)
        if company == other_company or company in RELISTERS or other_company in RELISTERS:
            return True
    return False
