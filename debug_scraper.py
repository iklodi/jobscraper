import asyncio
from playwright.async_api import async_playwright
import urllib.parse
import mode

CHROME_PROFILE_DIR = mode.PROFILE_DIR

async def debug():
    async with async_playwright() as p:
        browser = await mode.open_linkedin_session(p,
            headless=True,
            viewport={"width": 1280, "height": 800}
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        query = urllib.parse.urlencode({'keywords': 'Software Engineer', 'location': 'Remote'})
        await page.goto(f"https://www.linkedin.com/jobs/search/?{query}")
        
        await page.wait_for_selector('.job-card-container', timeout=15000)
        
        count = await page.locator('.job-card-container').count()
        
        if count > 0:
            await page.locator('.job-card-container').nth(0).click()
            await asyncio.sleep(2)
            
            # Try different selectors
            selectors = ['.jobs-description-content', '.jobs-box__html-content', '#job-details', 'article']
            for s in selectors:
                el = await page.query_selector(s)
                if el:
                    text = await el.inner_text()
                    print(f"Selector {s} found. Length: {len(text)}. First 50 chars: {text[:50].replace(chr(10), ' ')}")
                else:
                    print(f"Selector {s} NOT found.")
            
        await browser.close()

if __name__ == '__main__':
    asyncio.run(debug())
