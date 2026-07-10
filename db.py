import sqlite3
import datetime

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
            is_promoted BOOLEAN DEFAULT 0
        )
    ''')
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN is_promoted BOOLEAN DEFAULT 0')
    except sqlite3.OperationalError:
        pass # Column already exists
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
    cursor.execute('SELECT job_id, title, company, description, is_promoted FROM jobs WHERE status = "new"')
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

def update_job_score(job_id, score, reasoning):
    conn = get_connection()
    cursor = conn.cursor()
    status = 'scored'
    if score >= 9:
        status = 'to_apply'
    cursor.execute('''
        UPDATE jobs 
        SET score = ?, reasoning = ?, status = ?
        WHERE job_id = ?
    ''', (score, reasoning, status, job_id))
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    print("Database initialized.")
