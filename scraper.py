import asyncio
from playwright.async_api import async_playwright
import db
import dedupe
import urllib.parse
import os
import difflib
import progress_tracker
import notifier
import mode

# Configuration
CHROME_PROFILE_DIR = mode.PROFILE_DIR
# LinkedIn's job-type filter codes (the f_JT search parameter).
JOB_TYPE_CODES = {
    'full-time': 'F', 'fulltime': 'F', 'permanent': 'F',
    'part-time': 'P', 'parttime': 'P',
    'contract': 'C', 'contracting': 'C', 'freelance': 'C',
    'temporary': 'T', 'interim': 'T',
    'internship': 'I', 'volunteer': 'V', 'other': 'O',
}


# LinkedIn's AI job search (late 2026): /jobs/search-results/, markup with
# generated class names. What stays put is each card's componentkey, which
# carries the job id, its "Dismiss <title> job" button, and the detail
# panel's "About the job" section.
CARD_KEY_PREFIX = 'job-card-component-ref-'
CARD_SELECTOR = f'div[role="button"][componentkey^="{CARD_KEY_PREFIX}"]'

SCROLL_JS = """sel => {
    const cards = document.querySelectorAll(sel);
    if (cards.length) cards[cards.length - 1].scrollIntoView();
}"""

# One entry per card: id, title, company, location, promoted. The card's
# lines are the title (twice over, with screen-reader prefixes such as
# "Selected,"), then company, then location.
READ_CARDS_JS = """sel => {
    const seen = new Set(), out = [];
    for (const card of document.querySelectorAll(sel)) {
        const id = card.getAttribute('componentkey').replace('""" + CARD_KEY_PREFIX + """', '');
        if (!/^\\d+$/.test(id) || seen.has(id)) continue;
        seen.add(id);
        const lines = [...card.querySelectorAll('p')].map(p => p.innerText.trim()).filter(Boolean);
        const dismiss = card.querySelector('button[aria-label^="Dismiss "]');
        let title = dismiss ? dismiss.getAttribute('aria-label').replace(/^Dismiss /, '').replace(/ job$/, '') : '';
        if (!title) {
            const spans = card.querySelectorAll('p span');
            title = spans.length ? spans[spans.length - 1].innerText.trim() : (lines[0] || '');
        }
        out.push({id, title: title.trim(), company: lines[1] || '', location: lines[2] || '',
                  promoted: lines.some(l => /^Promoted\\b/.test(l))});
    }
    return out;
}"""

# The open advert's text: the section headed "About the job", without the
# heading or the trailing "… more" of a collapsed box.
READ_DESCRIPTION_JS = """() => {
    const heading = [...document.querySelectorAll('h2')]
        .find(h => /^(About the job|À propos de l.offre d.emploi|Info zum Job|Über den Job)/i.test(h.innerText.trim()));
    if (!heading) return '';
    const section = heading.parentElement.parentElement;
    return section.innerText.replace(heading.innerText, '').replace(/…\\s*more\\s*$/, '').trim();
}"""


def search_pages():
    """Result pages read per keyword and location: LINKEDIN_SEARCH_PAGES
    (default 1, 25 jobs each). Every page is one more search on the
    candidate's own account; the alert emails bring most jobs anyway."""
    try:
        return max(1, int(os.environ.get('LINKEDIN_SEARCH_PAGES', '1')))
    except ValueError:
        return 1


def search_results_url(keyword, location, start=0, type_codes=''):
    """The AI search's results address. It reads the query as a sentence, so
    the location goes into the keywords as well as its own parameter."""
    params = {'keywords': f'{keyword} in {location}', 'location': location, 'start': start}
    if type_codes:
        params['f_JT'] = type_codes
    return f"https://www.linkedin.com/jobs/search-results/?{urllib.parse.urlencode(params)}"


def get_search_criteria():
    """Read keywords, locations and job types from search_criteria.md (or env)."""
    # search_criteria.md is the source of truth - it is what the dashboard's
    # Search Settings edits. The env vars are only a fallback for when the file
    # does not exist; they used to be checked first, so anyone who copied
    # .env.example had the file, and every edit made in the dashboard, ignored.
    criteria_path = os.path.join(os.environ.get('CVS_DIR', 'cvs'), 'search_criteria.md')
    keywords = []
    locations = []
    job_types = []
    if not os.path.exists(criteria_path):
        env_keywords = os.environ.get('SEARCH_KEYWORDS')
        env_locations = os.environ.get('SEARCH_LOCATIONS')
        env_job_types = os.environ.get('SEARCH_JOB_TYPES')
        if env_keywords and env_locations:
            return ([(k.strip(), None) for k in env_keywords.split(',') if k.strip()],
                    [l.strip() for l in env_locations.split(',') if l.strip()],
                    [j.strip() for j in env_job_types.split(',') if j.strip()]
                    if env_job_types else [])
        return [("Software Engineer", None)], ["Remote"], []
        
    with open(criteria_path, 'r') as f:
        lines = f.readlines()
        
    current_section = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('# Search Keywords'):
            current_section = 'keywords'
        elif line.startswith('# Search Locations'):
            current_section = 'locations'
        elif line.startswith('# Job Types'):
            current_section = 'job_types'
        elif line.startswith('# Tracks'):
            current_section = 'tracks'     # read by get_tracks()
        elif line.startswith('#'):
            continue                      # a comment inside a section
        elif line.startswith('- ') and current_section == 'keywords':
            entry = line[2:].strip()
            if ' @ ' in entry:
                kw, where = entry.split(' @ ', 1)
                keywords.append((kw.strip(),
                                 [w.strip() for w in where.split('/') if w.strip()]))
            else:
                keywords.append((entry, None))
        elif line.startswith('- ') and current_section == 'locations':
            locations.append(line[2:].strip())
        elif line.startswith('- ') and current_section == 'job_types':
            job_types.append(line[2:].strip())
            
    if not keywords:
        keywords = [("Software Engineer", None)]
    if not locations:
        locations = ["Remote"]

    return keywords, locations, job_types

def get_tracks():
    """The job-search tracks from search_criteria.md's "# Tracks" section, as
    [(name, description)]. Each line is "- Name: what the track is". Every
    scored job is filed under one of them (or "Other"); empty when the
    section is missing."""
    path = os.path.join(os.environ.get('CVS_DIR', 'cvs'), 'search_criteria.md')
    if not os.path.exists(path):
        return []
    tracks, inside = [], False
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('# '):
                section = line[2:].strip().lower()
                if section in ('search keywords', 'search locations', 'job types', 'tracks'):
                    inside = section == 'tracks'
                continue
            if inside and line.startswith('- '):
                name, _, about = line[2:].partition(':')
                if name.strip():
                    tracks.append((name.strip(), about.strip()))
    return tracks

async def run_scraper():
    db.init_db()
    
    # Ensure profile directory exists
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    
    async with async_playwright() as p:
        print("Launching browser...")
        browser = await mode.open_linkedin_session(p,
            headless=False, # Set to False initially to allow manual login
            viewport={"width": 1280, "height": 800}
        )
        
        page = browser.pages[0] if browser.pages else await browser.new_page()
        
        print("Navigating to LinkedIn...")
        await page.goto('https://www.linkedin.com/login')
        
        try:
            await page.wait_for_selector('div.feed-identity-module, button[type="submit"]', timeout=5000)
        except Exception:
            pass 

        if 'login' in page.url:
            print("Please log into LinkedIn in the opened browser window.")
            print("Waiting for you to log in...")
            notifier.send_email(
                'AI Job Scraper - LinkedIn re-login required',
                "The scraper landed on the LinkedIn login page, so the saved browser session has expired.\n\n"
                "Complete the login in the browser window that is already open on the "
                "machine running the scraper (over VNC if it runs headless).\n\n"
                "This run waits **5 minutes** for the login, then aborts; scraping resumes "
                "on the next scheduled run once you are logged in.\n\n"
                f"Dashboard: {notifier.dashboard_links()}"
            )
            try:
                await page.wait_for_url('**/feed/**', timeout=300000)
            except Exception:
                print("Login was not completed within 5 minutes. Skipping scrape for this run.")
                await browser.close()
                return {}
            print("Successfully logged in!")

        keyword_specs, locations_list, job_types = get_search_criteria()
        # Translate the configured job types into LinkedIn's f_JT filter.
        type_codes = ''.join(dict.fromkeys(
            JOB_TYPE_CODES[t.lower()] for t in job_types if t.lower() in JOB_TYPE_CODES))
        if job_types:
            print(f"Job types: {', '.join(job_types)}"
                  + (f" (f_JT={type_codes})" if type_codes else " - none recognised, ignoring"))
        keyword_stats = {k: 0 for k, _ in keyword_specs}
        session_total_added = 0
        pages_per_search = search_pages()
        total_pages = sum(len(locs or locations_list) for _, locs in keyword_specs) * pages_per_search
        pages_processed = 0

        for keyword, keyword_locations in keyword_specs:
            for location in (keyword_locations or locations_list):
                for page_num in range(pages_per_search):
                    if progress_tracker.is_stop_requested():
                        print("Stop requested during scraping!")
                        await browser.close()
                        return keyword_stats

                    pages_processed += 1
                    progress_tracker.set_status(f"Scraping '{keyword}'", pages_processed, total_pages)
                    search_url = search_results_url(keyword, location, page_num * 25, type_codes)

                    print(f"Searching for jobs: {keyword} in {location} (Page {page_num + 1})")
                    try:
                        await page.goto(search_url, timeout=60000)
                    except Exception as e:
                        print(f"Failed to navigate to {search_url}: {e}. Skipping page...")
                        continue

                    try:
                        await page.wait_for_selector(CARD_SELECTOR, timeout=15000)
                    except Exception:
                        print(f"Timeout waiting for job cards for {keyword} in {location} (Page {page_num + 1}). Skipping...")
                        break # No more jobs

                    print("Scrolling to load jobs...")
                    for _ in range(5):
                        await page.evaluate(SCROLL_JS, CARD_SELECTOR)
                        await asyncio.sleep(2)

                    cards = await page.evaluate(READ_CARDS_JS, CARD_SELECTOR)
                    print(f"Found {len(cards)} job cards on this page.")

                    if len(cards) == 0:
                        break

                    new_jobs_count = 0
                    for info in cards:
                        try:
                            job_id = info['id']
                            if db.job_exists(job_id):
                                continue # Skip entirely if we already scraped this job
                            title, company = info['title'], info['company']
                            job_location = info['location'] or "Unknown Location"
                            if not title or not company:
                                continue
                            # The same advert from Adzuna, an employer feed or an alert.
                            if dedupe.already_listed(title, company):
                                print(f"Skipping duplicate job ({job_id}): {title} at {company}")
                                continue

                            # Clicking a card shows its advert in the detail panel and
                            # puts its id in the address; wait for both, so the text
                            # read is this job's and not the previous one's.
                            await page.click(f'{CARD_SELECTOR}[componentkey="{CARD_KEY_PREFIX}{job_id}"]')
                            try:
                                await page.wait_for_function(
                                    "id => location.href.includes('currentJobId=' + id)", arg=job_id, timeout=10000)
                            except Exception:
                                print(f"Job {job_id} did not open in the detail panel. Skipping...")
                                continue
                            await asyncio.sleep(1.5)
                            description = await page.evaluate(READ_DESCRIPTION_JS)
                            link = f"https://www.linkedin.com/jobs/view/{job_id}/"

                            def is_duplicate(comp, desc):
                                existing_descs = db.get_jobs_by_company(comp)
                                for old_desc in existing_descs:
                                    ratio = difflib.SequenceMatcher(None, desc, old_desc).ratio()
                                    if ratio > 0.90:
                                        return True
                                return False

                            if description:
                                if is_duplicate(company, description):
                                    print(f"Skipping duplicate job ({job_id}): {title} at {company}")
                                    continue

                                added = db.add_job(job_id, title, company, job_location.strip(), description, link, info['promoted'])
                                if added:
                                    new_jobs_count += 1
                                    session_total_added += 1
                                    print(f"[{session_total_added}] Added new job ({job_id}): {title} at {company}")
                        except Exception as e:
                            print(f"Error parsing a job card: {e}")

                    if keyword not in keyword_stats:
                        keyword_stats[keyword] = 0
                    keyword_stats[keyword] += new_jobs_count
                            
                    print(f"Finished scraping '{keyword}' in {location} (Page {page_num + 1}). Added {new_jobs_count} new jobs.")
                    await asyncio.sleep(3)
        await browser.close()
        return keyword_stats

if __name__ == '__main__':
    asyncio.run(run_scraper())
