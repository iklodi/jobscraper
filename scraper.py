import asyncio
from playwright.async_api import async_playwright
import db
import urllib.parse
import os
import difflib
import progress_tracker
import notifier

# Configuration
CHROME_PROFILE_DIR = './chrome_profile'
# LinkedIn's job-type filter codes (the f_JT search parameter).
JOB_TYPE_CODES = {
    'full-time': 'F', 'fulltime': 'F', 'permanent': 'F',
    'part-time': 'P', 'parttime': 'P',
    'contract': 'C', 'contracting': 'C', 'freelance': 'C',
    'temporary': 'T', 'interim': 'T',
    'internship': 'I', 'volunteer': 'V', 'other': 'O',
}


def get_search_criteria():
    """Read keywords, locations and job types from search_criteria.md (or env)."""
    env_keywords = os.environ.get('SEARCH_KEYWORDS')
    env_locations = os.environ.get('SEARCH_LOCATIONS')
    env_job_types = os.environ.get('SEARCH_JOB_TYPES')

    if env_keywords and env_locations:
        keywords = [(k.strip(), None) for k in env_keywords.split(',')]
        locations = [l.strip() for l in env_locations.split(',')]
        job_types = [j.strip() for j in env_job_types.split(',')] if env_job_types else []
        return keywords, locations, job_types

    criteria_path = os.path.join(os.environ.get('CVS_DIR', 'cvs'), 'search_criteria.md')
    keywords = []
    locations = []
    job_types = []
    if not os.path.exists(criteria_path):
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

async def run_scraper():
    db.init_db()
    
    # Ensure profile directory exists
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    
    async with async_playwright() as p:
        print("Launching browser...")
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE_DIR,
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
                "Connect to the scraper machine over VNC (port 5900) and complete the login "
                "in the browser window that is already open.\n\n"
                "This run waits **5 minutes** for the login, then aborts; scraping resumes "
                "on the next scheduled run once you are logged in.\n\n"
                f"Dashboard: {os.environ.get('DASHBOARD_URL', 'http://localhost:5050')}"
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
        total_pages = sum(len(locs or locations_list) for _, locs in keyword_specs) * 3
        pages_processed = 0

        for keyword, keyword_locations in keyword_specs:
            for location in (keyword_locations or locations_list):
                for page_num in range(3): # Scrape up to 3 pages (75 jobs) per keyword/location combo
                    if progress_tracker.is_stop_requested():
                        print("Stop requested during scraping!")
                        await browser.close()
                        return keyword_stats
                    
                    pages_processed += 1
                    progress_tracker.set_status(f"Scraping '{keyword}'", pages_processed, total_pages)
                    start = page_num * 25
                    params = {'keywords': keyword, 'location': location, 'start': start}
                    if type_codes:
                        params['f_JT'] = type_codes
                    query = urllib.parse.urlencode(params)
                    search_url = f"https://www.linkedin.com/jobs/search/?{query}"
                    
                    print(f"Searching for jobs: {keyword} in {location} (Page {page_num + 1})")
                    try:
                        await page.goto(search_url, timeout=60000)
                    except Exception as e:
                        print(f"Failed to navigate to {search_url}: {e}. Skipping page...")
                        continue
                    
                    try:
                        await page.wait_for_selector('.job-card-container', timeout=15000)
                    except Exception:
                        print(f"Timeout waiting for job cards for {keyword} in {location} (Page {page_num + 1}). Skipping...")
                        break # No more jobs
                    
                    print("Scrolling to load jobs...")
                    for _ in range(5):
                        await page.evaluate('''
                            const cards = document.querySelectorAll('.job-card-container');
                            if (cards.length > 0) {
                                cards[cards.length - 1].scrollIntoView();
                            }
                        ''')
                        await asyncio.sleep(2)
                        
                    job_cards = await page.query_selector_all('.job-card-container')
                    print(f"Found {len(job_cards)} job cards on this page.")
                    
                    if len(job_cards) == 0:
                        break
                    
                    new_jobs_count = 0
                    for card in job_cards:
                        try:
                            job_id = await card.get_attribute('data-job-id')
                            if job_id and db.job_exists(job_id):
                                continue # Skip entirely if we already scraped this job
                                
                            await card.click()
                            await asyncio.sleep(1.5)
                            
                            card_text = await card.inner_text()
                            is_promoted = "Promoted" in card_text
                            
                            title_elem = await card.query_selector('.artdeco-entity-lockup__title, .job-card-list__title')
                            title = await title_elem.inner_text() if title_elem else "Unknown Title"
                            
                            company_elem = await card.query_selector('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description')
                            company = await company_elem.inner_text() if company_elem else "Unknown Company"
                            
                            location_elem = await card.query_selector('.artdeco-entity-lockup__caption, .job-card-container__metadata-item')
                            job_location = await location_elem.inner_text() if location_elem else "Unknown Location"
                            
                            job_id = await card.get_attribute('data-job-id')
                            link = f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else ""
                            
                            desc_elem = await page.query_selector('#job-details')
                            description = await desc_elem.inner_text() if desc_elem else ""
                            
                            def is_duplicate(comp, desc):
                                existing_descs = db.get_jobs_by_company(comp)
                                for old_desc in existing_descs:
                                    ratio = difflib.SequenceMatcher(None, desc, old_desc).ratio()
                                    if ratio > 0.90:
                                        return True
                                return False
                            
                            if job_id and title and description:
                                if is_duplicate(company.strip(), description.strip()):
                                    print(f"Skipping duplicate job ({job_id}): {title} at {company}")
                                    continue
                                
                                added = db.add_job(job_id, title.strip(), company.strip(), job_location.strip(), description.strip(), link, is_promoted)
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
