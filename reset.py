import sqlite3
import argparse
import sys
import os
import shutil
from github import Github, Auth
from dotenv import load_dotenv

def close_github_issue(issue_number):
    load_dotenv()
    token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPO", "your_username/your_repo")
    if not token or not issue_number: return False
    
    try:
        auth = Auth.Token(token)
        g = Github(auth=auth)
        repo = g.get_repo(repo_name)
        issue = repo.get_issue(int(issue_number))
        if issue.state != "closed":
            issue.edit(state="closed")
        return True
    except Exception as e:
        print(f"  -> Failed to close GitHub issue #{issue_number}: {e}")
        return False

def reset_jobs(search_term=None, score=None, status='new'):
    conn = sqlite3.connect('jobs.db')
    cursor = conn.cursor()
    
    matches = []
    
    if score is not None:
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
        print("❌ You must provide either a search term or a --score.")
        return

    # Process all matches
    updated_count = len(matches)
    
    print(f"Resetting {updated_count} job(s)...")
    for j_id, company, title, issue_number in matches:
        print(f"Resetting: {company} - {title}")
        
        # Close GitHub issue safely
        if issue_number:
            if close_github_issue(issue_number):
                print(f"  -> Closed GitHub issue #{issue_number}")
                
        # Clean up files
        jobs_dir = '/path/to/cvs/jobs'
        if os.path.exists(jobs_dir):
            for f in os.listdir(jobs_dir):
                if f.startswith(f"{j_id}_"):
                    try: os.remove(os.path.join(jobs_dir, f))
                    except: pass
                    
        apps_dir = '/path/to/cvs/applications'
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
    parser.add_argument("--to-apply", action="store_true", help="Push to the 'to_apply' queue instead of 'new'")
    
    args = parser.parse_args()
    
    if not args.search_term and args.score is None:
        parser.print_help()
        sys.exit(1)
        
    status = 'to_apply' if args.to_apply else 'new'
    
    reset_jobs(search_term=args.search_term, score=args.score, status=status)
