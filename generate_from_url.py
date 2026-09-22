"""Generate a tailored CV and cover letter from a single job URL.

Point it at a LinkedIn posting or any employer's job page; it reads the advert,
records the job, and produces the same documents the nightly pipeline does:

    python generate_from_url.py "https://www.linkedin.com/jobs/view/4448054855/"
    python generate_from_url.py "https://careers.example.com/job/123" -i "stress the cloud work"

LinkedIn pages need the logged-in browser profile the scraper uses. If the page
comes back as a login wall, run once with --headed and sign in.
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

import db
from generator import generate_for_job, get_gemini_client, get_groq_client
from evaluate import GEMINI_MODELS, evaluate_single_job   # the cascade, so one retired model does not break this
from google.genai import types

CHROME_PROFILE_DIR = './chrome_profile'

# Pull the readable advert out of a page: prefer the containers employers
# actually use, and fall back to the biggest block of text on the page.
EXTRACT_JS = """
() => {
    const pick = (sel) => {
        const el = document.querySelector(sel);
        return el ? el.innerText.trim() : '';
    };
    const linkedin = pick('#job-details') || pick('.jobs-description__content');
    let best = '';
    if (!linkedin) {
        const candidates = Array.from(document.querySelectorAll(
            'main, article, [role=main], #content, .content, ' +
            '[class*="job-description" i], [class*="jobDescription" i], [id*="description" i]'
        ));
        for (const el of candidates) {
            const t = (el.innerText || '').trim();
            if (t.length > best.length) best = t;
        }
        if (best.length < 400) best = (document.body ? document.body.innerText : '').trim();
    }
    // Most job pages publish schema.org JobPosting - far more reliable than
    // guessing the employer from prose, especially on anonymised adverts.
    let ld = {};
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
            let data = JSON.parse(node.textContent);
            const items = Array.isArray(data) ? data : (data['@graph'] || [data]);
            for (const item of items) {
                if (!item || item['@type'] !== 'JobPosting') continue;
                const org = item.hiringOrganization;
                const loc = item.jobLocation;
                const addr = (Array.isArray(loc) ? loc[0] : loc || {}).address || {};
                ld = {
                    title: item.title || '',
                    company: (typeof org === 'string' ? org : (org || {}).name) || '',
                    location: [addr.addressLocality, addr.addressCountry]
                        .filter(Boolean).join(', '),
                };
                break;
            }
        } catch (e) { /* malformed block, ignore */ }
        if (ld.title) break;
    }
    const meta = (n) => {
        const el = document.querySelector(`meta[property="${n}"], meta[name="${n}"]`);
        return el ? (el.getAttribute('content') || '').trim() : '';
    };
    return {
        description: linkedin || best,
        title: pick('h1') || document.title || '',
        ld: ld,
        og_title: meta('og:title'),
        og_site: meta('og:site_name'),
        page_text: (document.body ? document.body.innerText : '').slice(0, 4000),
        url: location.href,
    };
}
"""

META_PROMPT = """Read this job advert and return facts about it. JSON only:
{{
  "title": "the job title as advertised",
  "company": "the hiring company (not the job board or recruiter, if the real employer is named)",
  "location": "City, Country as advertised, or 'Remote'",
  "jd_language": "the language the advert is written in, e.g. English, French, German",
  "hiring_manager_name": "the named hiring manager or recruiter if the advert names one, else null"
}}

Use only what the advert says. Where something is not stated, use null.

ADVERT:
{text}
"""

LOGIN_WALL = re.compile(r'sign in to (view|continue)|join linkedin|new to linkedin|'
                        r'log in to continue|please log in', re.I)


def job_id_for(url):
    """LinkedIn's own id where there is one, otherwise a stable id for the URL."""
    m = re.search(r'/jobs/view/(\d+)', url) or re.search(r'currentJobId=(\d+)', url)
    if m:
        return m.group(1)
    return 'url-' + hashlib.sha1(url.encode()).hexdigest()[:12]


async def fetch_job_page(url, headed=False):
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE_DIR,
            headless=not headed,
            viewport={'width': 1440, 'height': 900},
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        try:
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(5000)
            # LinkedIn hides most of the advert behind a "see more" toggle.
            for label in ('See more', 'Show more', 'Voir plus'):
                try:
                    btn = page.get_by_role('button', name=re.compile(f'^{label}', re.I)).first
                    await btn.click(timeout=2000)
                    await page.wait_for_timeout(1000)
                    break
                except Exception:
                    continue
            data = await page.evaluate(EXTRACT_JS)
            data['final_url'] = page.url
            return data
        finally:
            await browser.close()


def extract_metadata(text, fallback_title):
    """Ask the model for the advert's title/company/location/language."""
    gemini = get_gemini_client()
    prompt = META_PROMPT.format(text=text[:12000])
    if gemini:
        for model_name in GEMINI_MODELS:
            try:
                response = gemini.models.generate_content(
                    model=model_name, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type='application/json'),
                )
                body = response.text.strip()
                start, end = body.find('{'), body.rfind('}')
                if start != -1 and end != -1:
                    body = body[start:end + 1]
                return json.loads(body)
            except Exception as e:
                print(f'  -> {model_name}: {e}')
                continue

    groq = get_groq_client()
    if groq:
        try:
            response = groq.chat.completions.create(
                model='openai/gpt-oss-120b',
                messages=[{'role': 'user', 'content': prompt}],
                response_format={'type': 'json_object'},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f'  -> Groq: {e}')

    print('Could not read the advert automatically; falling back to the page title.')
    return {'title': fallback_title, 'company': 'Unknown', 'location': '',
            'jd_language': None, 'hiring_manager_name': None}


def record_job(job_id, meta, description, url):
    """Insert or refresh the job so the generator (and dashboard) can see it."""
    db.init_db()
    added = db.add_job(
        job_id, meta.get('title') or 'Unknown role', meta.get('company') or 'Unknown',
        meta.get('location') or '', description, url, False,
    )
    conn = db.get_connection()
    conn.execute(
        'UPDATE jobs SET title = ?, company = ?, location = ?, description = ?, link = ?,'
        ' hiring_manager_name = ?, jd_language = ? WHERE job_id = ?',
        (meta.get('title') or 'Unknown role', meta.get('company') or 'Unknown',
         meta.get('location') or '', description, url,
         meta.get('hiring_manager_name'), meta.get('jd_language'), job_id),
    )
    conn.commit()
    conn.close()
    return added


async def run_for_url(url, instructions=None, headed=False, on_progress=None,
                      generate=False):
    """Read a job advert, score it, and put it on the To Do board.

    A URL pasted by hand is a job the candidate already wants to look at, so it
    lands in To Do whatever it scores - the score is there to inform, not to
    filter something deliberately chosen. Documents are written when it is
    approved, not here; pass generate=True to write them straight away.

    Shared by the CLI and the dashboard. Returns a dict describing what happened
    so the caller can report it however it likes.
    """
    def step(message):
        print(message, flush=True)
        if on_progress:
            on_progress(message)

    step(f'Reading {url} ...')
    page = await fetch_job_page(url, headed=headed)
    description = (page.get('description') or '').strip()

    if len(description) < 200 or LOGIN_WALL.search(page.get('page_text', '')):
        hint = ('Sign in to LinkedIn in the browser on this machine, then try again.'
                if 'linkedin.com' in url else
                'The page may need JavaScript, a login, or be region-blocked.')
        return {'ok': False, 'error': f'Could not read the advert at {url}. {hint}'}

    step(f'Read {len(description)} characters. Identifying the role ...')
    meta = extract_metadata(description, page.get('title', ''))

    # Structured data beats the model's reading of the prose, and rescues
    # anonymised adverts ("our client is...") where no employer is named.
    ld = page.get('ld') or {}
    for key in ('title', 'company', 'location'):
        if ld.get(key) and not meta.get(key):
            meta[key] = ld[key]
    if not meta.get('company'):
        # LinkedIn's og:title reads "Company hiring Role in Place"; the site
        # name works for employer career pages.
        og = page.get('og_title') or ''
        m = re.match(r'^(.*?)\s+hiring\s', og, re.I)
        meta['company'] = (m.group(1).strip() if m
                           else page.get('og_site') or '').strip() or None
    if not meta.get('company'):
        # The logged-in LinkedIn app ships no JSON-LD or og: tags, but its page
        # title reads "Role | Company | LinkedIn".
        m = re.match(r'^(?P<title>.+?)\s*\|\s*(?P<company>.+?)\s*\|\s*LinkedIn\s*$',
                     page.get('title') or '')
        if m:
            meta['company'] = m.group('company').strip()
            if not meta.get('title'):
                meta['title'] = m.group('title').strip()

    final_url = page.get('final_url') or url
    job_id = job_id_for(final_url)
    record_job(job_id, meta, description, final_url)

    step(f"Scoring {meta.get('company') or 'Unknown'}: "
         f"{meta.get('title') or 'the role'} ...")
    score = reasoning = None
    prior_status = db.get_job_status(job_id)
    try:
        evaluate_single_job(job_id, instructions)
        row = db.get_connection().execute(
            'SELECT score, reasoning FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
        if row:
            score, reasoning = row[0], row[1]
    except Exception as e:
        step(f'Scoring failed ({e}); the job is on the board without a score.')

    # Whatever it scored, it stays in To Do: evaluate_single_job files a job
    # below the bar as 'scored', which would drop a hand-picked URL into
    # Rejected the moment it was added. Unless the link is for a job already
    # approved or applied to - then it goes back exactly where it was.
    db.update_job_status(job_id, prior_status if prior_status in db.PROTECTED_STATUSES
                         else 'to_apply')

    folder = None
    if generate:
        step('Generating documents ...')
        if await generate_for_job(job_id, instructions, final_status='to_apply'):
            apps = os.path.join(os.environ.get('CVS_DIR', 'cvs'), 'applications')
            made = ([d for d in os.listdir(apps) if d.endswith(f'_{job_id}')]
                    if os.path.isdir(apps) else [])
            folder = os.path.join(apps, made[0]) if made else None

    return {
        'ok': True,
        'job_id': job_id,
        'company': meta.get('company'),
        'title': meta.get('title'),
        'location': meta.get('location'),
        'language': meta.get('jd_language'),
        'hiring_manager': meta.get('hiring_manager_name'),
        'score': score,
        'reasoning': reasoning,
        'folder': folder,
        'company_identified': bool(meta.get('company')),
    }


async def main():
    parser = argparse.ArgumentParser(
        description='Generate a tailored CV and cover letter from a job URL.')
    parser.add_argument('url', help='LinkedIn job URL, or any employer job page')
    parser.add_argument('-i', '--instructions',
                        help='Extra instructions for the AI, e.g. "the company is Hone"')
    parser.add_argument('--headed', action='store_true',
                        help='Show the browser (use once to log in to LinkedIn)')
    parser.add_argument('--generate', action='store_true',
                        help='Also write the CV and cover letter now')
    args = parser.parse_args()

    result = await run_for_url(args.url, args.instructions, headed=args.headed,
                               generate=args.generate)
    if not result['ok']:
        print('\n' + result['error'])
        return 1

    if not result['company_identified']:
        print('  ! Could not identify the employer - documents say "Unknown".')
        print('    Re-run with  -i "the company is X"  to set it.')
    print(f"  {result['title']} at {result['company']} "
          f"({result.get('location') or 'location not stated'}, "
          f"{result.get('language') or 'language unknown'})")
    if result.get('score') is not None:
        print(f"  Scored {result['score']}/10 - on the To Do board.")
        if result.get('reasoning'):
            print(f"  {result['reasoning']}")
    else:
        print('  Not scored - on the To Do board anyway.')
    if result.get('folder'):
        print(f"\nDocuments are in: {result['folder']}")
        for f in sorted(os.listdir(result['folder'])):
            print(f'  {f}')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
