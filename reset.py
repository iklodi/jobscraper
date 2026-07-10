import sqlite3
import sys

def reset_job(search_term, status='new'):
    conn = sqlite3.connect('jobs.db')
    cursor = conn.cursor()
    
    # First, let's see how many jobs match
    cursor.execute('SELECT company, title FROM jobs WHERE job_id = ?', (search_term,))
    matches = cursor.fetchall()
    
    if not matches:
        cursor.execute("SELECT company, title FROM jobs WHERE company LIKE ?", (f"%{search_term}%",))
        matches = cursor.fetchall()
        
    if not matches:
        print(f"❌ No jobs found matching '{search_term}'.")
        conn.close()
        return

    # Update the jobs
    cursor.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (status, search_term))
    updated_count = cursor.rowcount
    
    if updated_count == 0:
        cursor.execute("UPDATE jobs SET status = ? WHERE company LIKE ?", (status, f"%{search_term}%"))
        updated_count = cursor.rowcount
        
    conn.commit()
    conn.close()
    
    print(f"✅ Successfully pushed {updated_count} job(s) back to the '{status}' queue:")
    for company, title in matches:
        print(f"   - {company}: {title}")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python reset.py \"<Company Name or Job ID>\" [--to-apply]")
        print("Examples:")
        print("  python reset.py Optomi          (Pushes Optomi back to Step 2 AI Evaluation)")
        print("  python reset.py Optomi --to-apply (Pushes Optomi back to Step 3 Document Generation)")
        sys.exit(1)
        
    status = 'new'
    args = [arg for arg in sys.argv[1:] if not arg.startswith('--')]
    search_term = args[0] if args else ""
    
    if '--to-apply' in sys.argv:
        status = 'to_apply'
        
    reset_job(search_term, status)
