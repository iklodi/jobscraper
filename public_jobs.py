"""Safe mode: job descriptions from public LinkedIn pages, logged out (issue #14).

A job-alert email names a job but carries no description. This opens the
public posting - https://www.linkedin.com/jobs/view/<id>/, rebuilt from the id
- in a throwaway browser with no profile and no cookies, so nothing ties the
request to the candidate's account, and reads the description LinkedIn shows
to any visitor.

It does not evade anything. When LinkedIn answers with its HTTP 999 wall, a
sign-in wall or a challenge, the run stops, the remaining jobs stay pending,
and the next run tries again. No proxies, no retries in a loop, no disguise.

Settings (.env): SAFE_FETCH_MAX_PER_RUN (default 60), SAFE_FETCH_MAX_ATTEMPTS
(default 3) before a job is reported as unfetchable.
"""

import asyncio
import os
import random
import re

from playwright.async_api import async_playwright

import db

DESCRIPTION_SELECTORS = ('.show-more-less-html__markup', '.description__text', '#job-details')
WALL_URL = re.compile(r'/(authwall|login|checkpoint|signup)\b', re.I)
BLOCKED_STATUS = {429, 999}

# What the public page's apply control is for. Logged out, it never exposes the
# employer's own URL (the link loops back through a LinkedIn sign-up page), but
# its tracking name still says whether the job is applied for off-site or only
# through Easy Apply - enough to route it (#15).
READ_PAGE_JS = """
(selectors) => {
    let description = '';
    for (const s of selectors) {
        const el = document.querySelector(s);
        if (el && el.innerText.trim().length > description.length) description = el.innerText.trim();
    }
    const tracked = Array.from(document.querySelectorAll('[data-tracking-control-name]'))
        .map(e => e.getAttribute('data-tracking-control-name'));
    let applyType = null;
    if (tracked.some(t => /apply-link-offsite|offsite-apply/i.test(t))) applyType = 'offsite';
    else if (tracked.some(t => /apply-link-(simple|onsite)|easy-?apply/i.test(t))) applyType = 'easy_apply';
    const closed = /no longer accepting applications/i.test(document.body.innerText);
    return {description, applyType, closed,
            head: document.body.innerText.slice(0, 600)};
}
"""


def public_url(job):
    """The bare public posting for a job: rebuilt from a LinkedIn id, or the
    stored link for anything else."""
    job_id, link = job
    if str(job_id).isdigit():
        return f'https://www.linkedin.com/jobs/view/{job_id}/'
    return link


def pending_jobs(limit):
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT job_id, link FROM jobs WHERE status = 'new' "
        "AND COALESCE(TRIM(description), '') = '' "
        "AND COALESCE(fetch_attempts, 0) < ? "
        "ORDER BY created_at DESC LIMIT ?",
        (int(os.environ.get('SAFE_FETCH_MAX_ATTEMPTS', '3')), limit)).fetchall()
    conn.close()
    return rows


def record(job_id, description=None, apply_type=None, closed=False):
    conn = db.get_connection()
    if description:
        conn.execute('UPDATE jobs SET description = ?, apply_type = COALESCE(?, apply_type) '
                     'WHERE job_id = ?', (description, apply_type, job_id))
    else:
        conn.execute('UPDATE jobs SET fetch_attempts = COALESCE(fetch_attempts, 0) + 1 '
                     'WHERE job_id = ?', (job_id,))
    if closed:
        # Nothing to apply for; do not spend a score on it.
        conn.execute("UPDATE jobs SET status = 'scored', score = 0, "
                     "reasoning = 'Listing closed - no longer accepting applications.' "
                     "WHERE job_id = ?", (job_id,))
    conn.commit()
    conn.close()


async def fetch_descriptions(max_jobs=None, pause=(4.0, 9.0)):
    """Fill in descriptions for jobs that arrived without one.

    Returns {'fetched': n, 'failed': n, 'closed': n, 'blocked': bool, 'pending': n}.
    """
    db.init_db()
    max_jobs = max_jobs or int(os.environ.get('SAFE_FETCH_MAX_PER_RUN', '60'))
    jobs = pending_jobs(max_jobs)
    stats = {'fetched': 0, 'failed': 0, 'closed': 0, 'blocked': False, 'pending': 0}
    if not jobs:
        return stats

    async with async_playwright() as p:
        # launch() + new_context(): a fresh, empty browser each run. Never
        # mode.open_linkedin_session(), which is the logged-in profile.
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale='en-US')
        page = await context.new_page()
        try:
            for index, (job_id, link) in enumerate(jobs):
                if index:
                    await asyncio.sleep(random.uniform(*pause))
                url = public_url((job_id, link))
                try:
                    response = await page.goto(url, timeout=45000, wait_until='domcontentloaded')
                    await page.wait_for_timeout(1500)
                except Exception as e:
                    print(f'  -> {job_id}: could not load ({type(e).__name__})')
                    record(job_id)
                    stats['failed'] += 1
                    continue

                status = response.status if response else None
                if status in BLOCKED_STATUS or WALL_URL.search(page.url):
                    print(f'  -> LinkedIn walled the logged-out fetcher (HTTP {status}, {page.url[:60]}). '
                          f'Stopping for this run; the rest stay pending.')
                    stats['blocked'] = True
                    break

                info = await page.evaluate(READ_PAGE_JS, list(DESCRIPTION_SELECTORS))
                if info['closed']:
                    record(job_id, closed=True)
                    stats['closed'] += 1
                elif len(info['description']) >= 200:
                    record(job_id, info['description'], info['applyType'])
                    stats['fetched'] += 1
                else:
                    record(job_id)
                    stats['failed'] += 1
        finally:
            await browser.close()

    stats['pending'] = len(pending_jobs(10_000))
    return stats


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    print(asyncio.run(fetch_descriptions()))
