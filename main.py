import asyncio
from scraper import run_scraper
from evaluate import run_evaluation
from generator import run_generator
from github_sync import run_sync
from dashboard_gen import generate_dashboard
import os
from dotenv import load_dotenv

load_dotenv(override=True)

import sys

async def main():
    skip_scrape = '--no-scrape' in sys.argv
    
    if not skip_scrape:
        print("=== Step 1: Scraping LinkedIn Jobs ===")
        # Note: Make sure headless=True in scraper.py after initial login
        await run_scraper()
    else:
        print("=== Step 1: Scraping Skipped (--no-scrape flag used) ===")
    
    skip_eval = '--no-eval' in sys.argv
    
    if not skip_eval:
        print("\n=== Step 2: Evaluating Jobs with Gemini ===")
        if not os.environ.get("GEMINI_API_KEY"):
            print("Warning: GEMINI_API_KEY not found. Skipping evaluation.")
        else:
            run_evaluation()
    else:
        print("\n=== Step 2: Evaluating Skipped (--no-eval flag used) ===")
        
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

    print("\n=== Step 5: Updating Dashboard ===")
    generate_dashboard()

    print("\n=== Step 6: Syncing working data to cvs repo ===")
    os.system('cd /path/to/cvs && git add . && git commit -m "Auto-update: JDs, generated applications, and dashboard" && git pull --rebase origin main && git push')

    print("\n=== Pipeline Complete ===")

if __name__ == '__main__':
    asyncio.run(main())
