import asyncio
import os
import sys
import time
from dotenv import load_dotenv

# Load env before importing internal modules that rely on env vars at module level
load_dotenv(override=True)

from scraper import run_scraper
from evaluate import run_evaluation
from generator import run_generator
import db
import notifier
import progress_tracker

async def main():
    progress_tracker.clear_status()
    progress_tracker.set_status("Initializing...", 0, 0)
    
    start_time = time.time()
    keyword_stats = {}
    eval_stats = {'score_counts': {}, 'recent_backlog': []}
    
    skip_scrape = '--no-scrape' in sys.argv
    
    if not skip_scrape:
        print("=== Step 1: Scraping LinkedIn Jobs ===")
        # Note: Make sure headless=True in scraper.py after initial login
        keyword_stats = await run_scraper()
        if progress_tracker.is_stop_requested():
            print("Stop requested. Exiting.")
            progress_tracker.clear_status()
            return
    else:
        print("=== Step 1: Scraping Skipped (--no-scrape flag used) ===")
    
    skip_eval = '--no-eval' in sys.argv
    
    if not skip_eval:
        print("\n=== Step 2: Evaluating Jobs with Gemini ===")
        if not os.environ.get("GEMINI_API_KEY"):
            print("Warning: GEMINI_API_KEY not found. Skipping evaluation.")
        else:
            result = run_evaluation()
            if result is not None:
                eval_stats = result
            if progress_tracker.is_stop_requested():
                print("Stop requested. Exiting.")
                progress_tracker.clear_status()
                return
    else:
        print("\n=== Step 2: Evaluating Skipped (--no-eval flag used) ===")
        
    skip_gen = '--no-gen' in sys.argv
    if not skip_gen:
        print("\n=== Step 3: Generating Tailored CVs & Cover Letters ===")
        if not os.environ.get("GEMINI_API_KEY"):
            print("Warning: GEMINI_API_KEY not found. Skipping generation.")
        else:
            await run_generator()
            if progress_tracker.is_stop_requested():
                print("Stop requested. Exiting.")
                progress_tracker.clear_status()
                return
    else:
        print("\n=== Step 3: Generation Skipped (--no-gen flag used) ===")
        
    progress_tracker.set_status("Finishing up...", 0, 0)
    print("\n=== Pipeline Complete ===")
    
    end_time = time.time()
    duration_secs = int(end_time - start_time)
    duration_str = f"{duration_secs // 60}m {duration_secs % 60}s"
    
    try:
        status_counts = db.get_status_counts()
    except Exception as e:
        status_counts = {}
        print(f"Error fetching status counts: {e}")
        
    notifier.send_email_summary(duration_str, keyword_stats, eval_stats, status_counts)
    progress_tracker.clear_status()

if __name__ == '__main__':
    asyncio.run(main())
