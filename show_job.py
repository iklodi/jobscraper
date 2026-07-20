import sys
import sqlite3

DB_FILE = 'jobs.db'

def get_job_details(job_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM jobs WHERE job_id = ?', (job_id,))
    job = cursor.fetchone()
    conn.close()
    return job

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python show_job.py <job_id>")
        sys.exit(1)
        
    job_id = sys.argv[1]
    job = get_job_details(job_id)
    
    if not job:
        print(f"Error: Job with ID '{job_id}' not found in database.")
        sys.exit(1)
        
    print("=" * 60)
    print(f" JOB DETAILS: {job_id}")
    print("=" * 60)
    
    for key in job.keys():
        if key == 'description' or key == 'reasoning':
            continue
        print(f"{key.upper():<20} | {job[key]}")
        
    if job['reasoning']:
        print("-" * 60)
        print("REASONING:")
        print(job['reasoning'])
        
    if job['description']:
        print("-" * 60)
        print("DESCRIPTION:")
        print(job['description'])
    print("=" * 60)
