import sqlite3
import datetime
import os

def min_pass_score():
    """The score at or above which a job goes to To Do. Read live, so the
    dashboard and a long-running process agree with .env without a restart;
    evaluate.min_pass_score() delegates here, so there is one source."""
    return int(os.environ.get('MIN_PASS_SCORE', 7))


# Statuses a person put a job into. Re-scoring or regenerating a job must never
# move it out of one of these - an application you sent is not undone because
# the model now scores the advert differently.
PROTECTED_STATUSES = ('approved', 'ready_to_submit', 'applied', 'interviewing',
                      'account_required', 'easy_apply')


def get_job_status(job_id):
    conn = get_connection()
    row = conn.execute('SELECT status FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
    conn.close()
    return row[0] if row else None

DB_FILE = 'jobs.db'

def get_connection():
    # The dashboard, the pipeline and the applier all touch the DB at once, so
    # wait for the lock instead of failing, and use WAL so readers never block.
    conn = sqlite3.connect(DB_FILE, timeout=30)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
    except sqlite3.Error:
        pass
    return conn

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS job_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            event_type TEXT,
            old_status TEXT,
            new_status TEXT,
            note TEXT,
            created_at TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        )
    ''')
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN is_promoted BOOLEAN DEFAULT 0')
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
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN application_notes TEXT')
    except sqlite3.OperationalError:
        pass
    # Where a job came from: the scraper's search keyword, or in safe mode the
    # name of the LinkedIn job alert that listed it.
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN source TEXT')
    except sqlite3.OperationalError:
        pass
    # Safe mode: alert emails already read, so a run never reads one twice.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_emails (
            message_id TEXT PRIMARY KEY,
            received_at TIMESTAMP,
            alert TEXT,
            jobs_listed INTEGER,
            jobs_added INTEGER,
            processed_at TIMESTAMP
        )
    ''')
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
    # A job without a description cannot be scored. In safe mode an alert email
    # names a job before its description has been fetched, so it waits here as
    # 'new' until it has one, rather than being scored on its title alone.
    cursor.execute('SELECT job_id, title, company, location, description, is_promoted FROM jobs '
                   "WHERE status = 'new' AND COALESCE(TRIM(description), '') != ''")
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
    if score >= min_pass_score():
        status = 'to_apply'
    cursor.execute('''
        UPDATE jobs 
        SET score = ?, reasoning = ?, status = ?, estimated_salary = ?, is_recruiter = ?, hiring_manager_name = ?, jd_language = ?
        WHERE job_id = ?
    ''', (score, reasoning, status, estimated_salary, is_recruiter, hiring_manager_name, jd_language, job_id))
    conn.commit()
    conn.close()

def update_job_status(job_id, status):
    conn = get_connection()
    cursor = conn.cursor()
    # Get old status
    cursor.execute('SELECT status FROM jobs WHERE job_id = ?', (job_id,))
    row = cursor.fetchone()
    old_status = row[0] if row else None
    
    cursor.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (status, job_id))
    
    if old_status != status:
        cursor.execute('''
            INSERT INTO job_history (job_id, event_type, old_status, new_status, created_at)
            VALUES (?, 'status_change', ?, ?, ?)
        ''', (job_id, old_status, status, datetime.datetime.now()))
        
    conn.commit()
    conn.close()

def add_job_note(job_id, note):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE jobs SET application_notes = ? WHERE job_id = ?', (note, job_id))
    cursor.execute('''
        INSERT INTO job_history (job_id, event_type, note, created_at)
        VALUES (?, 'note_added', ?, ?)
    ''', (job_id, note, datetime.datetime.now()))
    conn.commit()
    conn.close()

def get_job_history(job_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT event_type, old_status, new_status, note, created_at FROM job_history WHERE job_id = ? ORDER BY created_at DESC', (job_id,))
    history = cursor.fetchall()
    conn.close()
    return [dict(h) for h in history]

if __name__ == '__main__':
    init_db()
    print("Database initialized.")

def get_competing_jobs(company, exclude_job_id):
    # Not 'approved': an approval is a person's decision, and the comparison is
    # allowed to reject what it picked, never what they picked.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT job_id, title, description
        FROM jobs
        WHERE company = ? AND job_id != ? AND status IN ('to_apply', 'generated', 'synced', 'backlog')
    ''', (company, exclude_job_id))
    jobs = cursor.fetchall()
    conn.close()
    return jobs

def get_status_counts():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status')
    rows = cursor.fetchall()
    conn.close()
    return dict(rows)

def get_job_links(job_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT link FROM jobs WHERE job_id = ?', (job_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'linkedin': row[0]}
    return {'linkedin': None}

def get_jobs_by_company(company):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT description FROM jobs WHERE company = ?', (company,))
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]
