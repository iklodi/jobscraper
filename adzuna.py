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
apart and at most ADZUNA_FETCH_MAX_PER_RUN (default 60) a run. Some adverts
have no Adzuna page: Adzuna sends visitors straight to the advertiser and
refuses automated ones, browsers included. Those are scored on the API's
500-character summary, marked as such - see fetch_descriptions.

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
from dedupe import already_listed, title_key  # noqa: F401 (title_key: tests)

API = 'https://api.adzuna.com/v1/api/jobs/{country}/search/{page}'
RESULTS_PER_PAGE = 50
CALL_GAP = 3.0                    # seconds between API calls: 25 a minute at most
FIRST_RUN_DAYS = 14
ID_PREFIX = 'adzuna-'
USER_AGENT = 'Mozilla/5.0 (compatible; personal job search)'
# Adzuna refuses (403) any automated visit to an advert it has no page for:
# those it sends straight on to the advertiser through /land/ad/, and refuses
# even a real browser there. That is per advert, not a block of the reader, so
# one refusal only costs that job an attempt; several in a row mean Adzuna is
# refusing everything, and the run stops.
REFUSED_STATUS = {403, 429}
REFUSALS_BEFORE_STOP = 3
GONE_STATUS = {404, 410}                 # the advert has been taken down
PAGE_GAP = (5.0, 10.0)                  # seconds between advert pages
LAND_ONLY = re.compile(r'/land/ad/')
# Marks a description that is only Adzuna's summary, for the scorer and the reader.
SUMMARY_NOTE = ("[Adzuna's summary - the full advert is only on the employer's site, "
                "which Adzuna does not let automated readers through to.]\n\n")

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
# The countries Adzuna's API serves; anything else answers 404.
SUPPORTED = {'gb', 'us', 'at', 'au', 'be', 'br', 'ca', 'ch', 'de', 'es', 'fr', 'in', 'it',
             'mx', 'nl', 'nz', 'pl', 'sg', 'za'}

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
    unsupported = [c for c in wide if c not in SUPPORTED]
    if unsupported:
        print(f"Adzuna has no {', '.join(unsupported)} site; skipping. It covers: "
              f"{', '.join(sorted(SUPPORTED))}.")
        wide = [c for c in wide if c in SUPPORTED]
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
    if not response.ok:
        # Never raise_for_status(): its message is the full URL, app_key included,
        # and it would end up in cron.log and the summary email.
        raise RuntimeError(f'HTTP {response.status_code}')
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
    conn.execute('UPDATE jobs SET source = ?, snippet = ? WHERE job_id = ?',
                 (f'adzuna: {keyword}', (result.get('description') or '').strip() or None, job_id))
    conn.commit()
    conn.close()
    return True


def keep_snippet(result):
    """Store the summary for a job already known without one (jobs added
    before summaries were kept). True if one was stored."""
    snippet = (result.get('description') or '').strip()
    if not snippet:
        return False
    conn = db.get_connection()
    changed = conn.execute('UPDATE jobs SET snippet = ? WHERE job_id = ? AND snippet IS NULL',
                           (snippet, ID_PREFIX + str(result['id']))).rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def ingest(criteria=None, session=None, sleep=time.sleep, days=None):
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
    days = days or _max_days_old()
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
            for r in results:
                keep_snippet(r)
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
    """Adzuna jobs still without a description: readable pages first, then the
    ones Adzuna only redirects (they need no request - see fetch_descriptions)."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT job_id, link, snippet FROM jobs WHERE job_id LIKE ? AND status = 'new' "
        "AND COALESCE(TRIM(description), '') = '' AND COALESCE(fetch_attempts, 0) < ? "
        "ORDER BY link LIKE '%/land/ad/%', created_at DESC LIMIT ?",
        (ID_PREFIX + '%', max_attempts(), limit)).fetchall()
    conn.close()
    return rows


def max_attempts():
    return int(os.environ.get('SAFE_FETCH_MAX_ATTEMPTS', '3'))


def _save(job_id, **columns):
    conn = db.get_connection()
    sets = ', '.join(f'{k} = ?' for k in columns)
    conn.execute(f'UPDATE jobs SET {sets} WHERE job_id = ?', (*columns.values(), job_id))
    conn.commit()
    conn.close()


def _failed(job_id, snippet):
    """One more failed attempt; on the last, fall back to Adzuna's summary."""
    conn = db.get_connection()
    conn.execute('UPDATE jobs SET fetch_attempts = COALESCE(fetch_attempts, 0) + 1 WHERE job_id = ?',
                 (job_id,))
    attempts = conn.execute('SELECT fetch_attempts FROM jobs WHERE job_id = ?', (job_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    if attempts >= max_attempts() and snippet:
        _save(job_id, description=SUMMARY_NOTE + snippet)
        return True
    return False


def fetch_descriptions(max_jobs=None, pause=PAGE_GAP, session=None, sleep=time.sleep):
    """Give Adzuna jobs a description.

    - A job whose link is Adzuna's advert page: read the page.
    - A job Adzuna only redirects to the advertiser (/land/ad/): Adzuna refuses
      every automated visitor there, browsers included, so no request is made;
      its summary from the API becomes the description, marked as such.
    - An advert that is gone (404/410): closed, like a closed LinkedIn listing.
    - A refused page (403) costs that job an attempt; after its last attempt
      the summary is used. Several refusals in a row mean Adzuna is refusing
      everything: the run stops and the rest wait. No retries, no disguise.

    Returns {'fetched', 'summary', 'closed', 'failed', 'blocked', 'pending'}.
    """
    db.init_db()
    jobs = pending_jobs(max_jobs or int(os.environ.get('ADZUNA_FETCH_MAX_PER_RUN', '60')))
    session = session or requests.Session()
    stats = {'fetched': 0, 'summary': 0, 'closed': 0, 'failed': 0, 'blocked': False, 'pending': 0}
    refusals, requested = 0, False
    for job_id, link, snippet in jobs:
        if LAND_ONLY.search(link or ''):
            if snippet:
                _save(job_id, description=SUMMARY_NOTE + snippet)
                stats['summary'] += 1
            continue                         # no summary yet: the next search may bring it
        if requested:
            sleep(random.uniform(*pause))
        requested = True
        text, status = '', None
        try:
            response = session.get(link, headers={'User-Agent': USER_AGENT}, timeout=30)
            status = response.status_code
            if response.ok and not LAND_ONLY.search(response.url):
                text = page_text(response.text)
        except Exception as e:
            print(f'  -> {job_id}: could not load ({type(e).__name__})')

        if len(text) >= 200:
            _save(job_id, description=text)
            stats['fetched'] += 1
            refusals = 0
        elif status in GONE_STATUS:
            _save(job_id, status='scored', score=0,
                  reasoning='Listing closed - the advert is no longer on Adzuna.')
            stats['closed'] += 1
            refusals = 0
        else:
            stats['summary' if _failed(job_id, snippet) else 'failed'] += 1
            refusals = refusals + 1 if status in REFUSED_STATUS else 0
            if refusals >= REFUSALS_BEFORE_STOP:
                print(f'  -> Adzuna refused {refusals} pages in a row. Stopping for this run; '
                      f'the rest stay pending.')
                stats['blocked'] = True
                break
    stats['pending'] = len(pending_jobs(10_000))
    return stats


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    if not configured():
        raise SystemExit('Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env first.')
    import sys
    if '--backfill-summaries' in sys.argv:
        # Summaries were not kept before 2026-09-30: search a month back once so
        # jobs Adzuna only redirects get theirs (and a description).
        print(ingest(days=30))
    else:
        print(ingest())
    print(fetch_descriptions())
