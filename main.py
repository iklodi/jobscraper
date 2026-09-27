import asyncio
import datetime
import os
import sys
import time
from dotenv import load_dotenv

# Load env before importing internal modules that rely on env vars at module level
load_dotenv(override=True)

from scraper import run_scraper
from evaluate import run_evaluation, any_model_configured, get_groq_client, get_gemini_client
from generator import run_generator
import db
import notifier
import progress_tracker
import mode
import alerts
import public_jobs
import adzuna
import ats_feeds
import outcomes

async def main():
    progress_tracker.clear_status()
    progress_tracker.set_status("Initializing...", 0, 0)
    
    start_time = time.time()
    keyword_stats = {}
    eval_stats = {'score_counts': {}, 'recent_backlog': []}
    
    skip_scrape = '--no-scrape' in sys.argv
    run_started = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
    # Where this run's jobs came from and what went wrong on the way, for the
    # summary email - a walled fetcher or a capped alert is otherwise invisible.
    source_notes = [f"Mode: **{mode.current_mode()}**"]
    print(f"=== Mode: {mode.current_mode()} ===")

    if mode.safe_mode():
        # Safe mode never browses LinkedIn on the candidate's account. New jobs
        # arrive through job-alert emails instead.
        print("=== Step 1: Safe mode - reading LinkedIn job-alert emails ===")
        if alerts.configured():
            try:
                keyword_stats = {f'LinkedIn alert: {name}': added
                                 for name, added in alerts.ingest_alerts().items()}
            except Exception as e:
                print(f"Reading job-alert emails failed: {e}")
                source_notes.append(f"⚠️ Reading job-alert emails failed: {e}")
            for name, emails in alerts.capped_since(run_started).items():
                source_notes.append(f"⚠️ *{name}*: {emails} email(s) listed LinkedIn's maximum of "
                                    f"{alerts.EMAIL_JOB_CAP} jobs, so some matches were probably left "
                                    f"out. Narrow the alert.")
        else:
            print("Note: IMAP is not configured, so no new jobs were read. "
                  "Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD in .env.")
            source_notes.append("⚠️ IMAP is not configured, so no job-alert emails were read.")
        # Alert emails carry no description; read it from the public posting,
        # logged out. Jobs still without one are left out of scoring.
        print("=== Step 1b: Safe mode - fetching descriptions, logged out ===")
        fetch_stats = await public_jobs.fetch_descriptions()
        print(f"Descriptions: {fetch_stats['fetched']} fetched, {fetch_stats['failed']} failed, "
              f"{fetch_stats['closed']} closed, {fetch_stats['pending']} still pending"
              + (" - LinkedIn walled the fetcher; retrying next run." if fetch_stats['blocked'] else ""))
        source_notes.append(
            f"LinkedIn descriptions (logged out): {fetch_stats['fetched']} fetched, "
            f"{fetch_stats['failed']} failed, {fetch_stats['closed']} closed, "
            f"{fetch_stats['pending']} still pending"
            + (". ⚠️ LinkedIn walled the fetcher; the rest wait for the next run."
               if fetch_stats['blocked'] else ""))
    elif not skip_scrape:
        print("=== Step 1: Scraping LinkedIn Jobs ===")
        keyword_stats = await run_scraper()
        if progress_tracker.is_stop_requested():
            print("Stop requested. Exiting.")
            progress_tracker.clear_status()
            return
    else:
        print("=== Step 1: Scraping Skipped (--no-scrape flag used) ===")

    # Adzuna's API never touches LinkedIn, so it runs in either mode.
    if adzuna.configured() and not skip_scrape:
        print("\n=== Step 1c: Adzuna job search ===")
        try:
            for key, added in adzuna.ingest().items():
                keyword_stats[key] = keyword_stats.get(key, 0) + added
            adzuna_fetch = adzuna.fetch_descriptions()
            print(f"Adzuna descriptions: {adzuna_fetch['fetched']} fetched, "
                  f"{adzuna_fetch['failed']} failed, {adzuna_fetch['pending']} still pending"
                  + (" - Adzuna refused the fetcher; retrying next run." if adzuna_fetch['blocked'] else ""))
            source_notes.append(
                f"Adzuna descriptions: {adzuna_fetch['fetched']} fetched, {adzuna_fetch['failed']} "
                f"failed, {adzuna_fetch['pending']} still pending"
                + (". ⚠️ Adzuna refused the page reader; the rest wait for the next run."
                   if adzuna_fetch['blocked'] else ""))
        except Exception as e:
            print(f"Adzuna search failed: {e}")
            source_notes.append(f"⚠️ Adzuna search failed: {e}")
    
    # Employers' own ATS feeds (#22): public APIs, no LinkedIn, either mode.
    if ats_feeds.configured() and not skip_scrape:
        print("\n=== Step 1d: Employers' job feeds ===")
        try:
            ats_stats, problems = ats_feeds.ingest()
            keyword_stats.update(ats_stats)
            source_notes.append(f"Employer feeds: {len(ats_stats)} companies watched, "
                                f"{sum(ats_stats.values())} new jobs")
            source_notes.extend(f"⚠️ {p}" for p in problems)
        except Exception as e:
            print(f"Employer feeds failed: {e}")
            source_notes.append(f"⚠️ Employer feeds failed: {e}")

    # LinkedIn's application-status emails (#19): read-only, either mode.
    if alerts.configured():
        try:
            source_notes.extend(outcomes.ingest())
        except Exception as e:
            print(f"Reading application updates failed: {e}")
            source_notes.append(f"⚠️ Reading application updates failed: {e}")

    skip_eval = '--no-eval' in sys.argv
    
    if not skip_eval:
        print("\n=== Step 2: Evaluating Jobs ===")
        if not any_model_configured(get_groq_client(), get_gemini_client()):
            print("Warning: no model configured (INFOMANIAK_API_TOKEN, GEMINI_API_KEY or "
                  "GROQ_API_KEY). Skipping evaluation.")
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
        
    # Assets are written only for jobs approved by hand in the dashboard, so
    # the nightly run stops at scoring. Pass --gen to sweep every to_apply
    # job the old way.
    if '--gen' in sys.argv:
        print("\n=== Step 3: Generating Tailored CVs & Cover Letters ===")
        await run_generator()
        if progress_tracker.is_stop_requested():
            print("Stop requested. Exiting.")
            progress_tracker.clear_status()
            return
    else:
        print("\n=== Step 3: Generation skipped - assets are written on Approve ===")
        
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
        
    notifier.send_email_summary(duration_str, keyword_stats, eval_stats, status_counts,
                                source_notes=source_notes)
    progress_tracker.clear_status()

if __name__ == '__main__':
    asyncio.run(main())
