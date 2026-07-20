import os
import sqlite3
from flask import Flask, jsonify, render_template, request, send_from_directory, Response
from dotenv import load_dotenv
import sys
import subprocess
import threading
import progress_tracker

load_dotenv(override=True)

app = Flask(__name__)

DB_FILE = 'jobs.db'
CVS_DIR = os.environ.get('CVS_DIR', '/path/to/cvs')

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    return render_template('index.html')

def get_files_for_job(job_id):
    files = []
    apps_dir = os.path.join(CVS_DIR, 'applications')
    if os.path.exists(apps_dir):
        for d in os.listdir(apps_dir):
            if d.endswith(f"_{job_id}"):
                app_folder = os.path.join(apps_dir, d)
                for f in os.listdir(app_folder):
                    if f.endswith('.pdf') or f.endswith('.docx'):
                        files.append({
                            'name': f,
                            'url': f"/download/{d}/{f}"
                        })
    return files

@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    conn = get_db_connection()
    jobs = conn.execute('''
        SELECT job_id, title, company, location, link, score, reasoning, status, created_at, 
               estimated_salary, is_recruiter, description, application_notes
        FROM jobs 
        ORDER BY score DESC, created_at DESC
    ''').fetchall()
    
    job_list = []
    for job in jobs:
        j_dict = dict(job)
        j_dict = dict(job)
        j_dict['files'] = get_files_for_job(j_dict['job_id'])
        
        # Fetch history for this job
        hist_rows = conn.execute('SELECT event_type, old_status, new_status, note, created_at FROM job_history WHERE job_id = ? ORDER BY created_at DESC', (j_dict['job_id'],)).fetchall()
        j_dict['history'] = [dict(h) for h in hist_rows]
        
        job_list.append(j_dict)
        
    conn.close()
    return jsonify(job_list)

@app.route('/api/jobs/<job_id>/status', methods=['POST'])
def update_status(job_id):
    data = request.json
    new_status = data.get('status')
    notes = data.get('notes')
    
    if not new_status:
        return jsonify({'error': 'Status is required'}), 400
        
    conn = get_db_connection()
    row = conn.execute('SELECT status, application_notes FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
    old_status = row['status'] if row else None
    old_notes = row['application_notes'] if row else None
    
    if notes is not None:
        conn.execute('UPDATE jobs SET status = ?, application_notes = ? WHERE job_id = ?', (new_status, notes, job_id))
    else:
        conn.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (new_status, job_id))
        
    import datetime
    now = datetime.datetime.now()
    if old_status != new_status:
        conn.execute('''
            INSERT INTO job_history (job_id, event_type, old_status, new_status, created_at)
            VALUES (?, 'status_change', ?, ?, ?)
        ''', (job_id, old_status, new_status, now))
    if notes is not None and old_notes != notes:
        conn.execute('''
            INSERT INTO job_history (job_id, event_type, note, created_at)
            VALUES (?, 'note_added', ?, ?)
        ''', (job_id, notes, now))
        
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'status': new_status})

@app.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    conn = get_db_connection()
    job = conn.execute('''
        SELECT job_id, title, company, location, link, score, reasoning, status, created_at, 
               estimated_salary, is_recruiter, description, application_notes
        FROM jobs WHERE job_id = ?
    ''', (job_id,)).fetchone()
    conn.close()
    
    if not job:
        return jsonify({'error': 'Not found'}), 404
        
    j_dict = dict(job)
    j_dict['files'] = get_files_for_job(job_id)
    return jsonify(j_dict)

def bg_generate(job_id, instructions):
    import asyncio
    from generator import generate_for_job
    asyncio.run(generate_for_job(job_id, instructions))

@app.route('/api/jobs/<job_id>/regenerate', methods=['POST'])
def regenerate_job(job_id):
    conn = get_db_connection()
    conn.execute('UPDATE jobs SET status = "generating" WHERE job_id = ?', (job_id,))
    conn.commit()
    conn.close()
    
    data = request.json or {}
    instructions = data.get('instructions')
    import threading
    t = threading.Thread(target=bg_generate, args=(job_id, instructions))
    t.start()
    return jsonify({'success': True})

def bg_reevaluate(job_id, instructions):
    from evaluate import evaluate_single_job
    evaluate_single_job(job_id, instructions)

@app.route('/api/jobs/<job_id>/reevaluate', methods=['POST'])
def reevaluate_job(job_id):
    conn = get_db_connection()
    conn.execute('UPDATE jobs SET status = "evaluating" WHERE job_id = ?', (job_id,))
    conn.commit()
    conn.close()
    
    data = request.json or {}
    instructions = data.get('instructions')
    t = threading.Thread(target=bg_reevaluate, args=(job_id, instructions))
    t.start()
    return jsonify({'success': True})

scraper_thread = None

def run_scraper_bg(mode='full'):
    global scraper_thread
    try:
        cmd = [sys.executable, 'main.py']
        if mode == 'eval_only':
            cmd.extend(['--no-scrape', '--no-gen'])
        subprocess.run(cmd)
    except Exception as e:
        print(f"Scraper error: {e}")
    finally:
        scraper_thread = None
        progress_tracker.clear_status()

@app.route('/api/scrape', methods=['POST'])
def trigger_scrape():
    global scraper_thread
    if scraper_thread and scraper_thread.is_alive():
        return jsonify({'status': 'already_running'})
    
    data = request.json or {}
    mode = data.get('mode', 'full')
    
    scraper_thread = threading.Thread(target=run_scraper_bg, args=(mode,))
    scraper_thread.start()
    return jsonify({'status': 'started'})

@app.route('/api/scrape/stop', methods=['POST'])
def stop_scrape():
    progress_tracker.request_stop()
    return jsonify({'success': True})

@app.route('/api/scrape/status', methods=['GET'])
def scrape_status():
    global scraper_thread
    is_running = scraper_thread is not None and scraper_thread.is_alive()
    status_data = progress_tracker.get_status()
    status_data['is_running'] = is_running
    return jsonify(status_data)

@app.route('/download/<path:subpath>')
def download_file(subpath):
    apps_dir = os.path.join(CVS_DIR, 'applications')
    return send_from_directory(apps_dir, subpath)

@app.route('/api/system/pull', methods=['POST'])
def pull_updates():
    try:
        result = subprocess.run(['git', 'pull', 'origin', 'main'], capture_output=True, text=True, check=True)
        return jsonify({'success': True, 'output': result.stdout})
    except subprocess.CalledProcessError as e:
        return jsonify({'success': False, 'error': e.stderr or e.stdout}), 500

import db

if __name__ == '__main__':
    db.init_db()
    app.run(host='0.0.0.0', debug=True, port=5050)
