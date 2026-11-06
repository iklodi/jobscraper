"""Job links collected by the candidate, queued on the board and read later.

A list of adverts - pasted links, a saved page of them - is queued as Pending
Eval jobs with only a link (`queue`). `fetch_pending` then reads each advert
the way the dashboard's + button does (generate_from_url), always in a fresh
browser with no profile, and stores its title, company, location and text;
the nightly scoring files it on To Do or Discarded like any other job.

These are single adverts the candidate chose, read once each, 5-10 s apart:
not a crawl of any site. A LinkedIn link is not read here - it is reduced to
its job id and left to public_jobs.py, which reads LinkedIn's public pages.

Settings (.env): LINK_FETCH_MAX_PER_RUN (default 60). A link that cannot be
read is tried on later runs; public_jobs.expire_stale retires it after
SAFE_FETCH_MAX_AGE_DAYS.
"""

import asyncio
import html
import os
import random
import re
import sys
import urllib.parse

from playwright.async_api import async_playwright

import db
import dedupe
import generate_from_url as by_link

SOURCE = 'link list'
PLACEHOLDER = '(to fetch) '
URL = re.compile(r'https?://[^\s"\'<>`]+')
# Links in a saved page that are not adverts: fonts, scripts, styles.
NOT_ADVERTS = re.compile(r'fonts\.(googleapis|gstatic)\.com|cdnjs\.|cdn\.jsdelivr|unpkg\.com|'
                         r'\.(css|js|png|jpe?g|svg|woff2?)(\?|$)', re.I)
PAUSE = (5.0, 10.0)


def urls_in(text):
    """The advert links in a pasted list or a saved HTML page, in order, once each."""
    found = (u.rstrip('.,);\'"') for u in URL.findall(html.unescape(text)))
    return list(dict.fromkeys(u for u in found if not NOT_ADVERTS.search(u)))


def queue(urls):
    """Add each link as a Pending Eval job with no description yet.
    Returns {'added': n, 'known': [(url, job_id, status)]}."""
    db.init_db()
    added, known = 0, []
    for url in urls:
        url = by_link.canonical_url(url)
        existing = by_link.existing_job(url)
        if existing:
            known.append((url, existing['job_id'], existing['status']))
            continue
        host = urllib.parse.urlparse(url).hostname or url
        host = re.sub(r'^www\.', '', host)
        if db.add_job(by_link.job_id_for(url), PLACEHOLDER + host, host, '', '', url):
            conn = db.get_connection()
            conn.execute('UPDATE jobs SET source = ? WHERE job_id = ?', (SOURCE, by_link.job_id_for(url)))
            conn.commit()
            conn.close()
            added += 1
    return {'added': added, 'known': known}


def pending(limit):
    """Queued links not yet read: Pending Eval, no description, not LinkedIn."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT job_id, link FROM jobs WHERE status = 'new' AND job_id LIKE 'url-%' "
        "AND COALESCE(TRIM(description), '') = '' AND COALESCE(fetch_attempts, 0) < ? "
        "ORDER BY created_at LIMIT ?",
        (int(os.environ.get('SAFE_FETCH_MAX_ATTEMPTS', '3')), limit)).fetchall()
    conn.close()
    return rows


def failed(job_id):
    conn = db.get_connection()
    conn.execute('UPDATE jobs SET fetch_attempts = COALESCE(fetch_attempts, 0) + 1 WHERE job_id = ?', (job_id,))
    conn.commit()
    conn.close()


GONE = {404, 410}


def closed(job_id, status):
    """The advert is gone from its site: nothing to apply for, nothing to score."""
    conn = db.get_connection()
    conn.execute("UPDATE jobs SET status = 'scored', score = 0, reasoning = ? WHERE job_id = ?",
                 (f'Listing closed - the advert is no longer online (HTTP {status}).', job_id))
    conn.commit()
    conn.close()


def store(job_id, meta, description, final_url):
    """Fill in the queued job, or retire it when the advert is already on the
    board. Returns (summary line, whether it was a duplicate)."""
    title = (meta.get('title') or '').strip() or 'Unknown role'
    company = (meta.get('company') or '').strip() or 'Unknown'
    conn = db.get_connection()
    conn.execute('UPDATE jobs SET title = ?, company = ?, location = ?, description = ?, link = ?, '
                 'hiring_manager_name = COALESCE(?, hiring_manager_name), jd_language = ? WHERE job_id = ?',
                 (title, company, meta.get('location') or '', description, final_url,
                  meta.get('hiring_manager_name'), meta.get('jd_language'), job_id))
    conn.commit()
    conn.close()
    copy = dedupe.duplicate_of(title, company, exclude=job_id)
    if copy:
        db.update_job_status(job_id, 'rejected')
        db.add_job_note(job_id, f'Duplicate of #{copy}')
        return f'{title} at {company}: already on the board as #{copy}', True
    return f'{title} at {company}', False


async def fetch_pending(max_jobs=None, pause=PAUSE):
    """Read queued links. Returns {'read': n, 'duplicates': n, 'closed': n,
    'failed': n, 'pending': n}. A 404 or 410 closes the job; anything else that
    cannot be read (a 403 refusal, a login) is tried again on later runs."""
    db.init_db()
    max_jobs = max_jobs or int(os.environ.get('LINK_FETCH_MAX_PER_RUN', '60'))
    jobs = pending(max_jobs)
    stats = {'read': 0, 'duplicates': 0, 'closed': 0, 'failed': 0, 'pending': 0}
    if jobs:
        async with async_playwright() as p:
            # A fresh browser, never the LinkedIn profile: these are other sites.
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(locale='en-US', viewport={'width': 1440, 'height': 900})
            try:
                for index, (job_id, url) in enumerate(jobs):
                    if index:
                        await asyncio.sleep(random.uniform(*pause))
                    page = await context.new_page()
                    try:
                        data = await by_link._read_page(page, url)
                    except Exception as e:
                        print(f'  -> {url}: could not load ({type(e).__name__})')
                        failed(job_id)
                        stats['failed'] += 1
                        continue
                    finally:
                        await page.close()
                    description = (data.get('description') or '').strip()
                    if data.get('status') in GONE:
                        print(f'  -> {url}: the advert is gone (HTTP {data["status"]})')
                        closed(job_id, data['status'])
                        stats['closed'] += 1
                        continue
                    if (data.get('status') or 200) >= 400 or len(description) < 200 \
                            or by_link.LOGIN_WALL.search(data.get('page_text', '')):
                        print(f'  -> {url}: no advert to read (HTTP {data.get("status")})')
                        failed(job_id)
                        stats['failed'] += 1
                        continue
                    meta = by_link.extract_metadata(description, data.get('title', ''))
                    ld = data.get('ld') or {}
                    for key in ('title', 'company', 'location'):
                        if ld.get(key) and not meta.get(key):
                            meta[key] = ld[key]
                    if not meta.get('company'):
                        meta['company'] = data.get('og_site') or None
                    line, duplicate = store(job_id, meta, description, data.get('final_url') or url)
                    stats['duplicates' if duplicate else 'read'] += 1
                    print(f'  -> {line}')
            finally:
                await browser.close()
    stats['pending'] = len(pending(10_000))
    return stats


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(override=True)
    usage = 'python link_queue.py add <file or URL>... | fetch'
    if len(sys.argv) < 2 or sys.argv[1] not in ('add', 'fetch'):
        raise SystemExit(usage)
    if sys.argv[1] == 'add':
        urls = []
        for arg in sys.argv[2:]:
            urls += urls_in(open(arg, encoding='utf-8', errors='replace').read()) if os.path.exists(arg) else [arg]
        result = queue(urls)
        print(f"Queued {result['added']} link(s); {len(result['known'])} already on the board.")
        for url, job_id, status in result['known']:
            print(f'  already known: {url} -> #{job_id} ({status})')
    else:
        print(asyncio.run(fetch_pending()))
