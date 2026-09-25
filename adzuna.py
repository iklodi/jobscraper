"""New jobs from Adzuna's official job-search API.

Adzuna aggregates many boards (jobs.ch, company sites, Job-Room, ...) and
offers a documented API with a free key, so this is a sanctioned source that
never touches the candidate's LinkedIn account. It runs in both modes.

The searches are the ones in CVS_DIR/search_criteria.md, the same file the
LinkedIn scraper reads. Adzuna searches one country at a time, so each location
is mapped to a country (COUNTRIES); a city searches ADZUNA_COUNTRY around that
place; "Worldwide", "Europe" and "Remote" expand to ADZUNA_WIDE_COUNTRIES, and
are skipped when that is empty.

The API cuts every description to 500 characters, too little to score. The
full text is on the advert's Adzuna page - the redirect_url the API returns -
which is read with a plain HTTP request, one page at a time, 5-10 seconds
apart. Read faster, Adzuna's site starts refusing (HTTP 403); the fetch then
stops for the run and the rest wait for the next one.

Adzuna's terms: at most 25 calls a minute and 250 a day, and adverts shown must
be labelled "Jobs by Adzuna" (the dashboard does). Links are kept as Adzuna's
redirect_url, which its terms require for sending a person to the advert.

Settings (.env): ADZUNA_APP_ID, ADZUNA_APP_KEY, ADZUNA_COUNTRY (default ch),
ADZUNA_WIDE_COUNTRIES, ADZUNA_MAX_DAYS_OLD (default 3), ADZUNA_MAX_PAGES
(default 2), ADZUNA_MAX_CALLS_PER_RUN (default 40).
"""

import os
import random
import re
import time

import requests
from lxml import html as lxml_html

import db

API = 'https://api.adzuna.com/v1/api/jobs/{country}/search/{page}'
RESULTS_PER_PAGE = 50
CALL_GAP = 3.0                    # seconds between API calls: 25 a minute at most
FIRST_RUN_DAYS = 14
ID_PREFIX = 'adzuna-'
USER_AGENT = 'Mozilla/5.0 (compatible; personal job search)'
# Adzuna's site answers these when it thinks the pages are read too fast; the
# fetch stops for the run instead of pressing on.
BLOCKED_STATUS = {403, 429}

# Location names as they appear in search_criteria.md -> Adzuna country codes.
COUNTRIES = {
    'switzerland': 'ch', 'schweiz': 'ch', 'suisse': 'ch', 'svizzera': 'ch',
    'germany': 'de', 'deutschland': 'de', 'austria': 'at', 'österreich': 'at',
    'france': 'fr', 'italy': 'it', 'spain': 'es', 'netherlands': 'nl',
    'belgium': 'be', 'poland': 'pl', 'united kingdom': 'gb', 'uk': 'gb',
    'united states': 'us', 'usa': 'us', 'canada': 'ca', 'australia': 'au',
    'new zealand': 'nz', 'singapore': 'sg', 'india': 'in', 'brazil': 'br',
    'mexico': 'mx', 'south africa': 'za',
}
WIDE = {'worldwide', 'europe', 'emea', 'remote', 'anywhere'}

# LinkedIn job types -> Adzuna's one-at-a-time filters. Applied only when the
# criteria name exactly one of them; otherwise every type is searched.
JOB_TYPE_FILTERS = {'contract': 'contract', 'temporary': 'contract',
                    'part-time': 'part_time', 'full-time': 'full_time'}

BLOCK_TAGS = {'p', 'div', 'li', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'ul', 'ol'}


def configured():
    return bool(os.environ.get('ADZUNA_APP_ID') and os.environ.get('ADZUNA_APP_KEY'))


def plan_searches(keywords, locations):
    """[(keyword, country, where or None)] for the criteria, plus the
    locations that could not be mapped."""
    home = os.environ.get('ADZUNA_COUNTRY', 'ch').strip().lower()
    wide = [c.strip().lower() for c in os.environ.get('ADZUNA_WIDE_COUNTRIES', '').split(',') if c.strip()]
    searches, skipped = [], []
    for keyword, pinned in keywords:
        for location in pinned or locations:
            name = location.strip().lower()
            if name in COUNTRIES:
                targets = [(COUNTRIES[name], None)]
            elif name in WIDE:
                targets = [(c, None) for c in wide]
                if not wide:
                    skipped.append(location)
            else:
                targets = [(home, location.strip())]
            for country, where in targets:
                if (keyword, country, where) not in searches:
                    searches.append((keyword, country, where))
    return searches, sorted(set(skipped))


def type_filter(job_types):
    mapped = {JOB_TYPE_FILTERS[t.strip().lower()] for t in job_types
              if t.strip().lower() in JOB_TYPE_FILTERS}
    return mapped.pop() if len(mapped) == 1 else None


def _max_days_old():
    conn = db.get_connection()
    seen = conn.execute("SELECT 1 FROM jobs WHERE job_id LIKE ? LIMIT 1",
                        (ID_PREFIX + '%',)).fetchone()
    conn.close()
    return int(os.environ.get('ADZUNA_MAX_DAYS_OLD', '3')) if seen else FIRST_RUN_DAYS


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


def search(keyword, country, where, page, days, contract_filter, session):
    # title_only, not what/what_phrase: those match anywhere in the advert, so
    # "General Manager" brought in "Executive Assistant to the General Manager".
    params = {
        'app_id': os.environ['ADZUNA_APP_ID'], 'app_key': os.environ['ADZUNA_APP_KEY'],
        'results_per_page': RESULTS_PER_PAGE, 'title_only': keyword,
        'max_days_old': days, 'sort_by': 'date',
    }
    if where:
        params['where'] = where
    if contract_filter:
        params[contract_filter] = '1'
    response = session.get(API.format(country=country, page=page), params=params,
                           headers={'Accept': 'application/json'}, timeout=30)
    response.raise_for_status()
    return response.json()


def store(result, keyword):
    """Add one API result as a 'new' job without a description. True if added."""
    job_id = ID_PREFIX + str(result['id'])
    title = (result.get('title') or '').strip() or 'Unknown role'
    company = ((result.get('company') or {}).get('display_name') or '').strip() or 'Unknown'
    if db.job_exists(job_id) or already_listed(title, company):
        return False
    location = (result.get('location') or {}).get('display_name') or ''
    if not db.add_job(job_id, title, company, location, '', result.get('redirect_url') or '', False):
        return False
    conn = db.get_connection()
    conn.execute('UPDATE jobs SET source = ? WHERE job_id = ?', (f'adzuna: {keyword}', job_id))
    conn.commit()
    conn.close()
    return True


def ingest(criteria=None, session=None, sleep=time.sleep):
    """Run every search and add what is new. Returns {'<keyword> (Adzuna)': added}."""
    db.init_db()
    if criteria is None:
        from scraper import get_search_criteria
        criteria = get_search_criteria()
    keywords, locations, job_types = criteria
    searches, skipped = plan_searches(keywords, locations)
    if skipped:
        print(f"Adzuna: skipping {', '.join(skipped)} - set ADZUNA_WIDE_COUNTRIES "
              f"(e.g. ch,de,fr,gb) to search them.")
    contract_filter = type_filter(job_types)
    days = _max_days_old()
    max_pages = int(os.environ.get('ADZUNA_MAX_PAGES', '2'))
    budget = int(os.environ.get('ADZUNA_MAX_CALLS_PER_RUN', '40'))
    session = session or requests.Session()

    stats, calls = {}, 0
    for keyword, country, where in searches:
        key = f'{keyword} (Adzuna)'
        stats.setdefault(key, 0)
        for page in range(1, max_pages + 1):
            if calls >= budget:
                print(f'Adzuna: stopping at {budget} calls (ADZUNA_MAX_CALLS_PER_RUN).')
                return stats
            if calls:
                sleep(CALL_GAP)
            calls += 1
            try:
                data = search(keyword, country, where, page, days, contract_filter, session)
            except Exception as e:
                print(f"Adzuna: '{keyword}' in {where or country} failed ({e}).")
                break
            results = data.get('results') or []
            added = sum(store(r, keyword) for r in results)
            stats[key] += added
            print(f"Adzuna: '{keyword}' in {where or country.upper()} page {page}: "
                  f"{len(results)} of {data.get('count', '?')}, {added} new")
            if len(results) < RESULTS_PER_PAGE:
                break
    return stats


def page_text(markup):
    """The advert's text from its Adzuna page, or '' if the page has none."""
    tree = lxml_html.fromstring(markup)
    bodies = tree.find_class('adp-body')
    if not bodies:
        return ''
    body = bodies[0]
    for element in body.iter():
        if element.tag in BLOCK_TAGS:
            element.tail = '\n' + (element.tail or '')
    lines = (re.sub(r'[ \t\xa0]+', ' ', l).strip() for l in body.text_content().splitlines())
    return '\n'.join(l for l in lines if l)


def pending_jobs(limit):
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT job_id, link FROM jobs WHERE job_id LIKE ? AND status = 'new' "
        "AND COALESCE(TRIM(description), '') = '' AND COALESCE(fetch_attempts, 0) < ? "
        "ORDER BY created_at DESC LIMIT ?",
        (ID_PREFIX + '%', int(os.environ.get('SAFE_FETCH_MAX_ATTEMPTS', '3')), limit)).fetchall()
    conn.close()
    return rows


def fetch_descriptions(max_jobs=None, pause=(5.0, 10.0), session=None, sleep=time.sleep):
    """Read the full advert for Adzuna jobs that have none yet.

    When Adzuna's site pushes back (403/429, or a redirect onto a refusal page)
    the run stops and the rest stay pending, without spending their attempts -
    the same rule as the LinkedIn fetcher: no retrying in a loop, no disguise.
    Returns {'fetched': n, 'failed': n, 'blocked': bool, 'pending': n}.
    """
    db.init_db()
    jobs = pending_jobs(max_jobs or int(os.environ.get('SAFE_FETCH_MAX_PER_RUN', '60')))
    session = session or requests.Session()
    stats = {'fetched': 0, 'failed': 0, 'blocked': False, 'pending': 0}
    for index, (job_id, link) in enumerate(jobs):
        if index:
            sleep(random.uniform(*pause))
        text = ''
        try:
            response = session.get(link, headers={'User-Agent': USER_AGENT}, timeout=30)
            if response.status_code in BLOCKED_STATUS:
                print(f'  -> Adzuna refused the page fetcher (HTTP {response.status_code}). '
                      f'Stopping for this run; the rest stay pending.')
                stats['blocked'] = True
                break
            if response.ok:
                text = page_text(response.text)
        except Exception as e:
            print(f'  -> {job_id}: could not load ({type(e).__name__})')
        conn = db.get_connection()
        if len(text) >= 200:
            conn.execute('UPDATE jobs SET description = ? WHERE job_id = ?', (text, job_id))
            stats['fetched'] += 1
        else:
            conn.execute('UPDATE jobs SET fetch_attempts = COALESCE(fetch_attempts, 0) + 1 '
                         'WHERE job_id = ?', (job_id,))
            stats['failed'] += 1
        conn.commit()
        conn.close()
    stats['pending'] = len(pending_jobs(10_000))
    return stats


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    if not configured():
        raise SystemExit('Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env first.')
    print(ingest())
    print(fetch_descriptions())
