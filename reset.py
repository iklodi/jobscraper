import sqlite3
import argparse
import sys
import os
import shutil
from github import Github, Auth
from dotenv import load_dotenv



def reset_jobs(search_term=None, score=None, reset_all=False, status='new'):
    conn = sqlite3.connect('jobs.db')
    cursor = conn.cursor()
    
    matches = []
    
    if reset_all:
        # Prevent resetting jobs we've already applied to or explicitly rejected
        cursor.execute("SELECT job_id, company, title, issue_number FROM jobs WHERE status NOT IN ('new', 'applied', 'interviewing', 'offer', 'rejected')")
        matches = cursor.fetchall()
        if not matches:
            print("❌ No active evaluated jobs found to reset.")
            return
    
    elif score is not None:
        # Prevent resetting jobs we've already applied to or explicitly rejected
        cursor.execute("SELECT job_id, company, title, issue_number FROM jobs WHERE score = ? AND status NOT IN ('applied', 'interviewing', 'offer', 'rejected')", (score,))
        matches = cursor.fetchall()
        if not matches:
            print(f"❌ No active jobs found with score {score}.")
            return
    elif search_term:
        cursor.execute("SELECT job_id, company, title, issue_number FROM jobs WHERE job_id = ?", (search_term,))
        matches = cursor.fetchall()
        if not matches:
            cursor.execute("SELECT job_id, company, title, issue_number FROM jobs WHERE company LIKE ?", (f"%{search_term}%",))
            matches = cursor.fetchall()
        if not matches:
            print(f"❌ No jobs found matching '{search_term}'.")
            return
    else:
        print("❌ You must provide either a search term, --score, or --all.")
        return

    # Process all matches
    updated_count = len(matches)
    
    print(f"Resetting {updated_count} job(s)...")
    for j_id, company, title, issue_number in matches:
        print(f"Resetting: {company} - {title}")
        
        # GitHub Sync Removed
        # Clean up files
        cvs_dir = os.environ.get('CVS_DIR', 'cvs')
        jobs_dir = os.path.join(cvs_dir, 'jobs')
        if os.path.exists(jobs_dir):
            for f in os.listdir(jobs_dir):
                if f.startswith(f"{j_id}_"):
                    try: os.remove(os.path.join(jobs_dir, f))
                    except: pass
                    
        apps_dir = os.path.join(cvs_dir, 'applications')
        if os.path.exists(apps_dir):
            for d in os.listdir(apps_dir):
                if d.endswith(f"_{j_id}"):
                    try: shutil.rmtree(os.path.join(apps_dir, d))
                    except: pass
        
        # Update DB for this specific job, wiping out old evaluations
        cursor.execute('''
            UPDATE jobs 
            SET status=?, issue_number=NULL, issue_url=NULL, score=NULL, reasoning=NULL 
            WHERE job_id=?
        ''', (status, j_id))

    conn.commit()
    conn.close()
    print(f"✅ Successfully pushed {updated_count} job(s) back to the '{status}' queue.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Reset jobs in the database to be re-evaluated or regenerated.")
    parser.add_argument("search_term", nargs="?", help="Job ID or Company Name to reset")
    parser.add_argument("--score", type=int, help="Reset all active jobs matching this exact score (e.g., 9)")
    parser.add_argument("--all", action="store_true", help="Reset ALL active evaluated jobs")
    parser.add_argument("--to-apply", action="store_true", help="Push to the 'to_apply' queue instead of 'new'")
    
    args = parser.parse_args()
    
    if not args.search_term and args.score is None and not args.all:
        parser.print_help()
        sys.exit(1)
        
    status = 'to_apply' if args.to_apply else 'new'
    
    reset_jobs(search_term=args.search_term, score=args.score, reset_all=args.all, status=status)
