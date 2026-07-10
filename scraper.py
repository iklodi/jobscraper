import asyncio
from playwright.async_api import async_playwright
import db
import urllib.parse
import os

# Configuration
CHROME_PROFILE_DIR = './chrome_profile'
SEARCH_KEYWORDS_LIST = [
    "Enterprise Architect",
    "Solutions Architect",
    "Technology Strategy",
    "Digital Transformation",
    "AI Manager",
    "Technical Program Manager",
    "Head of IT",
    "Innovation Manager"
]
SEARCH_LOCATIONS = ["Switzerland", "Worldwide"]

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
            await page.wait_for_url('**/feed/**', timeout=300000) 
            print("Successfully logged in!")

        for keyword in SEARCH_KEYWORDS_LIST:
            for location in SEARCH_LOCATIONS:
                for page_num in range(3): # Scrape up to 3 pages (75 jobs) per keyword/location combo
                    start = page_num * 25
                    query = urllib.parse.urlencode({'keywords': keyword, 'location': location, 'start': start})
                    search_url = f"https://www.linkedin.com/jobs/search/?{query}"
                    
                    print(f"Searching for jobs: {keyword} in {location} (Page {page_num + 1})")
                    await page.goto(search_url)
                    
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
                            
                            title_elem = await card.query_selector('.job-card-list__title')
                            title = await title_elem.inner_text() if title_elem else "Unknown Title"
                            
                            company_elem = await card.query_selector('.job-card-container__primary-description')
                            company = await company_elem.inner_text() if company_elem else "Unknown Company"
                            
                            location_elem = await card.query_selector('.job-card-container__metadata-item')
                            job_location = await location_elem.inner_text() if location_elem else "Unknown Location"
                            
                            job_id = await card.get_attribute('data-job-id')
                            link = f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else ""
                            
                            desc_elem = await page.query_selector('#job-details')
                            description = await desc_elem.inner_text() if desc_elem else ""
                            
                            if job_id and title and description:
                                added = db.add_job(job_id, title.strip(), company.strip(), job_location.strip(), description.strip(), link)
                                if added:
                                    new_jobs_count += 1
                                    print(f"Added new job: {title} at {company}")
                        except Exception as e:
                            print(f"Error parsing a job card: {e}")
                            
                    print(f"Finished scraping '{keyword}' in {location} (Page {page_num + 1}). Added {new_jobs_count} new jobs.")
                    await asyncio.sleep(3)
        await browser.close()

if __name__ == '__main__':
    asyncio.run(run_scraper())
