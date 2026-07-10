import asyncio
from playwright.async_api import async_playwright
import urllib.parse
import os

CHROME_PROFILE_DIR = './chrome_profile'

async def debug_card():
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE_DIR,
            headless=True,
            viewport={"width": 1280, "height": 800}
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        
        query = urllib.parse.urlencode({'keywords': 'Engineer', 'location': 'Worldwide'})
        search_url = f"https://www.linkedin.com/jobs/search/?{query}"
        
        print(f"Navigating to {search_url}...")
        await page.goto(search_url)
        
        try:
            await page.wait_for_selector('.job-card-container', timeout=15000)
            card = await page.query_selector('.job-card-container')
            if card:
                html = await card.inner_html()
                print("\n=== CARD HTML ===")
                print(html)
            else:
                print("Card not found after wait.")
        except Exception as e:
            print(f"Error: {e}")
            
        await browser.close()

if __name__ == '__main__':
    asyncio.run(debug_card())
