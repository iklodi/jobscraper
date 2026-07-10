import asyncio
from scraper import run_scraper
from evaluate import run_evaluation
from generator import run_generator
from github_sync import run_sync
import os

async def main():
    print("=== Step 1: Scraping LinkedIn Jobs ===")
    # Note: Make sure headless=True in scraper.py after initial login
    await run_scraper()
    
    print("\n=== Step 2: Evaluating Jobs with Gemini ===")
    if not os.environ.get("GEMINI_API_KEY"):
        print("Skipping evaluation: GEMINI_API_KEY not set.")
    else:
        run_evaluation()
        
    print("\n=== Step 3: Generating Tailored CVs & Cover Letters ===")
    if not os.environ.get("GEMINI_API_KEY"):
        print("Skipping generation: GEMINI_API_KEY not set.")
    else:
        run_generator()
        
    print("\n=== Step 4: Syncing to GitHub Issues ===")
    if not os.environ.get("GITHUB_TOKEN"):
        print("Skipping sync: GITHUB_TOKEN not set.")
    else:
        run_sync()

    print("\n=== Pipeline Complete ===")

if __name__ == '__main__':
    asyncio.run(main())
