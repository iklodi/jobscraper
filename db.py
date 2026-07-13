import sqlite3
import datetime
import os

MIN_PASS_SCORE = int(os.environ.get('MIN_PASS_SCORE', 9))

DB_FILE = 'jobs.db'

def get_connection():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            description TEXT,
            link TEXT,
            score INTEGER,
            reasoning TEXT,
            status TEXT,
            created_at TIMESTAMP,
            is_promoted BOOLEAN DEFAULT 0,
            issue_number INTEGER,
            issue_url TEXT
        )
    ''')
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN is_promoted BOOLEAN DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN issue_number INTEGER')
        cursor.execute('ALTER TABLE jobs ADD COLUMN issue_url TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN estimated_salary TEXT')
        cursor.execute('ALTER TABLE jobs ADD COLUMN is_recruiter BOOLEAN')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN hiring_manager_name TEXT')
        cursor.execute('ALTER TABLE jobs ADD COLUMN jd_language TEXT')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def add_job(job_id, title, company, location, description, link, is_promoted=False):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO jobs (job_id, title, company, location, description, link, status, created_at, is_promoted)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
        ''', (job_id, title, company, location, description, link, datetime.datetime.now(), is_promoted))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Job already exists
        return False
    finally:
        conn.close()

def get_unscored_jobs():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT job_id, title, company, location, description, is_promoted FROM jobs WHERE status = "new"')
    jobs = cursor.fetchall()
    conn.close()
    return jobs

def job_exists(job_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM jobs WHERE job_id = ?', (job_id,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def update_job_score(job_id, score, reasoning, estimated_salary=None, is_recruiter=None, hiring_manager_name=None, jd_language=None):
    conn = get_connection()
    cursor = conn.cursor()
    status = 'scored'
    if score >= MIN_PASS_SCORE:
        status = 'to_apply'
    cursor.execute('''
        UPDATE jobs 
        SET score = ?, reasoning = ?, status = ?, estimated_salary = ?, is_recruiter = ?, hiring_manager_name = ?, jd_language = ?
        WHERE job_id = ?
    ''', (score, reasoning, status, estimated_salary, is_recruiter, hiring_manager_name, jd_language, job_id))
    conn.commit()
    conn.close()

def update_job_issue(job_id, issue_number, issue_url):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE jobs
        SET issue_number = ?, issue_url = ?, status = "synced"
        WHERE job_id = ?
    ''', (issue_number, issue_url, job_id))
    conn.commit()
    conn.close()

def update_job_status(job_id, status):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (status, job_id))
    conn.commit()
    conn.close()

def get_all_jobs_with_issues():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT job_id, issue_number FROM jobs WHERE issue_number IS NOT NULL')
    jobs = cursor.fetchall()
    conn.close()
    return jobs

if __name__ == '__main__':
    init_db()
    print("Database initialized.")
