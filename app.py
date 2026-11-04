import os
import sqlite3
from flask import Flask, jsonify, render_template, request, send_from_directory, Response
from dotenv import load_dotenv
import sys
import subprocess
import threading
import progress_tracker
import db

load_dotenv(override=True)

app = Flask(__name__)

DB_FILE = 'jobs.db'
CVS_DIR = os.environ.get('CVS_DIR', 'cvs')

def _configure_conn(conn):
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
    except Exception:
        pass
    return conn

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return _configure_conn(conn)

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
                    if f.endswith('.pdf') or f.endswith('.docx') or f.endswith('.png'):
                        files.append({
                            'name': f,
                            'url': f"/download/{d}/{f}"
                        })
    return files

# Rejected jobs older than this drop off the board. The rows stay in the
# database so the scraper still knows it has seen them and will not re-add them.
REJECTED_BOARD_DAYS = int(os.environ.get('REJECTED_BOARD_DAYS', 7))

@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    conn = get_db_connection()
    # applied_at: when the job actually moved to Applied. Older rows predate
    # job_history, so fall back to the last thing that happened to them and
    # finally to when they were scraped, rather than sorting them as blank.
    jobs = conn.execute('''
        SELECT j.job_id, j.title, j.company, j.location, j.link, j.score, j.reasoning,
               j.status, j.created_at, j.estimated_salary, j.is_recruiter, j.is_promoted,
               j.description, j.application_notes, j.apply_url, j.apply_type,
               j.source, j.jd_language, j.hiring_manager_name, j.fetch_attempts,
               COALESCE(
                   (SELECT MAX(h.created_at) FROM job_history h
                     WHERE h.job_id = j.job_id AND h.new_status = 'applied'),
                   (SELECT MAX(h.created_at) FROM job_history h
                     WHERE h.job_id = j.job_id),
                   j.created_at
               ) AS applied_at,
               -- Last time anything happened to this job: a move between
               -- columns or a note. Falls back to when it was scraped.
               COALESCE(
                   (SELECT MAX(h.created_at) FROM job_history h
                     WHERE h.job_id = j.job_id),
                   j.created_at
               ) AS updated_at
        FROM jobs j
        WHERE j.status NOT IN ('rejected', 'scored')
           OR j.created_at >= datetime('now', ?)
        ORDER BY updated_at DESC
    ''', (f'-{REJECTED_BOARD_DAYS} days',)).fetchall()
    
    job_list = []
    for job in jobs:
        j_dict = dict(job)
        j_dict['files'] = get_files_for_job(j_dict['job_id'])
        
        # Fetch history for this job
        hist_rows = conn.execute('SELECT event_type, old_status, new_status, note, created_at FROM job_history WHERE job_id = ? ORDER BY created_at DESC', (j_dict['job_id'],)).fetchall()
        j_dict['history'] = [dict(h) for h in hist_rows]
        
        job_list.append(j_dict)
        
    conn.close()
    return jsonify(job_list)

def move_column_to_rejected(statuses, note):
    """Move every job in the given statuses to Rejected, recording each move
    in its history with the note. Returns how many moved."""
    import datetime
    conn = get_db_connection()
    marks = ','.join('?' * len(statuses))
    rows = conn.execute(
        f"SELECT job_id, status FROM jobs WHERE status IN ({marks})", statuses).fetchall()
    now = datetime.datetime.now()
    for row in rows:
        conn.execute("UPDATE jobs SET status = 'rejected' WHERE job_id = ?", (row['job_id'],))
        conn.execute('''
            INSERT INTO job_history (job_id, event_type, old_status, new_status, note, created_at)
            VALUES (?, 'status_change', ?, 'rejected', ?, ?)
        ''', (row['job_id'], row['status'], note, now))
    conn.commit()
    conn.close()
    return len(rows)

@app.route('/api/backlog/clear', methods=['POST'])
def clear_backlog():
    """Move every job in To Do (to_apply, generated) to Rejected."""
    return jsonify({'status': 'ok', 'moved': move_column_to_rejected(('to_apply', 'generated'), 'Backlog cleared')})

@app.route('/api/failed/clear', methods=['POST'])
def clear_failed():
    """Move every job in Failed to Rejected."""
    return jsonify({'status': 'ok', 'moved': move_column_to_rejected(('failed',), 'Failed cleared')})

# Where a bulk action on the board may move jobs: the columns a person
# decides on. The transient statuses belong to a background run.
BULK_STATUSES = ('to_apply', 'approved', 'failed', 'account_required', 'applied',
                 'interviewing', 'rejected')
BUSY_STATUSES = ('generating', 'evaluating', 'applying')

def bg_bulk_generate(job_ids):
    """Write the documents for jobs approved together, one after another -
    a thread each would run every generation at once."""
    for job_id in job_ids:
        try:
            bg_generate(job_id, None, final_status='approved')
        except Exception as e:
            print(f"Generation failed for {job_id}: {e}")

def bg_bulk_reevaluate(jobs):
    for job_id, prior_status in jobs:
        try:
            bg_reevaluate(job_id, None, prior_status)
        except Exception as e:
            print(f"Re-evaluation failed for {job_id}: {e}")

@app.route('/api/jobs/bulk', methods=['POST'])
def bulk_action():
    """The board's selection menu: move the selected jobs to one status, or
    re-evaluate them. Jobs a background run holds are skipped and counted."""
    import datetime
    data = request.json or {}
    job_ids = [str(j) for j in data.get('job_ids') or []]
    action = data.get('action')
    new_status = data.get('status')
    if not job_ids:
        return jsonify({'error': 'No jobs selected'}), 400
    if action not in ('status', 'reevaluate'):
        return jsonify({'error': f'Unknown action: {action}'}), 400
    if action == 'status' and new_status not in BULK_STATUSES:
        return jsonify({'error': f'Cannot move jobs to {new_status}'}), 400

    conn = get_db_connection()
    marks = ','.join('?' * len(job_ids))
    rows = conn.execute(f'SELECT job_id, status FROM jobs WHERE job_id IN ({marks})',
                        job_ids).fetchall()
    ready = [(r['job_id'], r['status']) for r in rows if r['status'] not in BUSY_STATUSES]
    skipped = len(job_ids) - len(ready)

    if action == 'reevaluate':
        for job_id, _ in ready:
            conn.execute('UPDATE jobs SET status = "evaluating" WHERE job_id = ?', (job_id,))
        conn.commit()
        conn.close()
        if ready:
            threading.Thread(target=bg_bulk_reevaluate, args=(ready,)).start()
        return jsonify({'status': 'ok', 'moved': len(ready), 'skipped': skipped})

    now = datetime.datetime.now()
    to_generate = []
    moved = 0
    for job_id, old_status in ready:
        if old_status == new_status:
            continue
        moved += 1
        conn.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (new_status, job_id))
        conn.execute('''
            INSERT INTO job_history (job_id, event_type, old_status, new_status, note, created_at)
            VALUES (?, 'status_change', ?, ?, 'Bulk action', ?)
        ''', (job_id, old_status, new_status, now))
        # Approving commissions the documents, as it does one job at a time.
        if new_status == 'approved' and not get_files_for_job(job_id):
            to_generate.append(job_id)
    conn.commit()
    conn.close()
    if to_generate:
        threading.Thread(target=bg_bulk_generate, args=(to_generate,)).start()
    return jsonify({'status': 'ok', 'moved': moved, 'skipped': skipped,
                    'generating': to_generate})

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

    # Approving is what commissions the CV and cover letter - the nightly run
    # no longer writes assets for everything that clears the bar. Skip it when
    # the job already has documents (re-approving after a detour through
    # Failed, say); use Regenerate Assets to deliberately redo them.
    generating = False
    if new_status == 'approved' and old_status != 'approved' and not get_files_for_job(job_id):
        import threading
        threading.Thread(target=bg_generate,
                         args=(job_id, None), kwargs={'final_status': 'approved'}).start()
        generating = True

    return jsonify({'success': True, 'status': new_status, 'generating': generating})

@app.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    conn = get_db_connection()
    job = conn.execute('''
        SELECT job_id, title, company, location, link, score, reasoning, status, created_at, 
               estimated_salary, is_recruiter, is_promoted, description, application_notes,
               apply_url, apply_type
        FROM jobs WHERE job_id = ?
    ''', (job_id,)).fetchone()
    conn.close()
    
    if not job:
        return jsonify({'error': 'Not found'}), 404
        
    j_dict = dict(job)
    j_dict['files'] = get_files_for_job(job_id)
    return jsonify(j_dict)

def bg_generate(job_id, instructions, final_status='generated', cv_language=None, cl_language=None):
    import asyncio
    import importlib
    import generator
    importlib.reload(generator)          # same reason as run_applier_bg
    asyncio.run(generator.generate_for_job(job_id, instructions, final_status=final_status,
                                           cv_language=cv_language, cl_language=cl_language))


def language_choice(value):
    """A language name from the dashboard's dropdown, or None for the default."""
    import re
    value = (value or '').strip()
    # Letters, spaces and brackets only: the name goes into a prompt.
    return value if re.fullmatch(r"[^\W\d_]+(?:[ ()'-]+[^\W\d_]*)*", value) and len(value) <= 40 else None

@app.route('/api/jobs/<job_id>/regenerate', methods=['POST'])
def regenerate_job(job_id):
    conn = get_db_connection()
    row = conn.execute('SELECT status FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
    # Regenerating hands a job back to where a person put it - Approved,
    # Applied, Interviewing... - rather than dropping it into To Do.
    final_status = (row['status'] if row and row['status'] in db.PROTECTED_STATUSES
                    else 'generated')
    conn.execute('UPDATE jobs SET status = "generating" WHERE job_id = ?', (job_id,))
    conn.commit()
    conn.close()

    data = request.json or {}
    instructions = data.get('instructions')
    import threading
    t = threading.Thread(target=bg_generate, args=(job_id, instructions),
                         kwargs={'final_status': final_status,
                                 'cv_language': language_choice(data.get('cv_language')),
                                 'cl_language': language_choice(data.get('cl_language'))})
    t.start()
    return jsonify({'success': True})

def bg_reevaluate(job_id, instructions, prior_status=None):
    import importlib
    import evaluate
    importlib.reload(evaluate)           # same reason as run_applier_bg
    try:
        evaluate.evaluate_single_job(job_id, instructions)
    finally:
        # A new score informs; it does not move a job someone approved or
        # applied to. Without this, re-scoring an Applied job dropped it into
        # Rejected or To Do by score.
        if prior_status in db.PROTECTED_STATUSES:
            db.update_job_status(job_id, prior_status)

@app.route('/api/jobs/<job_id>/reevaluate', methods=['POST'])
def reevaluate_job(job_id):
    prior_status = db.get_job_status(job_id)
    conn = get_db_connection()
    conn.execute('UPDATE jobs SET status = "evaluating" WHERE job_id = ?', (job_id,))
    conn.commit()
    conn.close()
    
    data = request.json or {}
    instructions = data.get('instructions')
    t = threading.Thread(target=bg_reevaluate, args=(job_id, instructions, prior_status))
    t.start()
    return jsonify({'success': True})

scraper_thread = None

def run_scraper_bg(mode='full'):
    global scraper_thread
    try:
        cmd = [sys.executable, 'main.py']
        if mode == 'eval_only':
            cmd.append('--no-scrape')
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
    is_running = (scraper_thread is not None and scraper_thread.is_alive()) or \
                 (apply_thread is not None and apply_thread.is_alive())
    status_data = progress_tracker.get_status()
    status_data['is_running'] = is_running
    # The outcome of the last finished run, handed over once so the dashboard
    # can say what happened instead of just dropping the Stop button.
    if not is_running:
        status_data['last_result'] = progress_tracker.take_result()
    return jsonify(status_data)

apply_thread = None

def run_applier_bg(limit, job_ids, auto_submit=False):
    global apply_thread
    import asyncio
    import importlib
    import applier
    # The dashboard is long-lived, so an edit to applier.py after it started
    # would otherwise never take effect - a run would quietly use the code the
    # process imported hours ago. Reloading here is safe: apply_thread already
    # prevents two runs at once.
    importlib.reload(applier)
    run_applications = applier.run_applications
    try:
        asyncio.run(run_applications(limit=limit, job_ids=job_ids, auto_submit=auto_submit))
    except Exception as e:
        print(f"Applier error: {e}")
        progress_tracker.set_result({'ok': False, 'headline': f'The application run failed: {e}',
                                     'single': False, 'jobs': []})
    finally:
        progress_tracker.clear_status()
        apply_thread = None

@app.route('/api/mode', methods=['GET'])
def get_mode():
    """The current mode, and which features need the logged-in LinkedIn session.

    Both work in either mode now: adding a job by link reads pages logged out
    (#16), and the applier starts from the employer's page in a browser that
    never reaches LinkedIn (#15). Kept so the dashboard can grey out a feature
    again should one ever need the logged-in session.
    """
    import mode
    return jsonify({
        'mode': mode.current_mode(),
        'apply_available': True, 'apply_reason': '',
        'add_url_available': True, 'add_url_reason': '',
    })

@app.route('/api/jobs/<job_id>/apply-url', methods=['POST'])
def set_apply_url(job_id):
    """The employer's own application page for a job. The applier starts there
    instead of at the LinkedIn posting - in safe mode, the only way to fill a
    job that came in with nothing but a LinkedIn link (#15)."""
    import mode
    url = ((request.json or {}).get('url') or '').strip()
    if url and not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Enter a full link starting with http.'}), 400
    if url and mode.is_linkedin(url):
        return jsonify({'error': "That is a LinkedIn link. Paste the employer's page - the one "
                                 "LinkedIn's Apply button leads to."}), 400
    if not db.job_exists(job_id):
        return jsonify({'error': 'No such job.'}), 404
    db.set_apply_url(job_id, url)
    return jsonify({'status': 'ok', 'apply_url': url or None})

@app.route('/api/apply/policy', methods=['GET'])
def apply_policy():
    """Whether Apply Now may submit, and if not, what would allow it."""
    import applier
    blocked = applier.submit_blocked_by_profile()
    return jsonify({
        'submit_allowed': not blocked,
        'reason': (f'Submitting is switched off: policies.{applier.SUBMIT_POLICY_KEY} is true '
                   f'in {applier.PROFILE_PATH}. Set it to false to enable Apply Now. '
                   'Fill Now still completes the form for you to send.') if blocked else '',
    })

@app.route('/api/apply', methods=['POST'])
def trigger_apply():
    global apply_thread
    if apply_thread and apply_thread.is_alive():
        return jsonify({'status': 'already_running'})

    data = request.json or {}
    # No limit by default: a partial batch fills forms it then closes, and a
    # closed form is lost, so the job looks done while the work is gone.
    limit = data.get('limit')
    limit = int(limit) if limit else None
    job_ids = data.get('job_ids')

    # Apply Now omits `submit` or sends true; Fill Now sends false. When it is
    # on, the pre-submit gate in applier.py still refuses anything incomplete.
    auto_submit = bool(data.get('submit', True))
    import applier
    if auto_submit and applier.submit_blocked_by_profile():
        return jsonify({'status': 'blocked',
                        'error': f'policies.{applier.SUBMIT_POLICY_KEY} is true in profile.yaml '
                                 '- use Fill Now, or set it to false to submit.'}), 409
    apply_thread = threading.Thread(target=run_applier_bg, args=(limit, job_ids, auto_submit))
    apply_thread.start()
    return jsonify({'status': 'started'})

add_url_thread = None
add_url_result = None

def run_add_url_bg(url, instructions):
    """Fetch a job advert and score it onto the To Do board, for the + button."""
    global add_url_thread, add_url_result
    import asyncio
    import importlib
    import generate_from_url
    importlib.reload(generate_from_url)          # same reason as run_applier_bg
    run_for_url = generate_from_url.run_for_url
    try:
        add_url_result = asyncio.run(run_for_url(
            url, instructions,
            on_progress=lambda msg: progress_tracker.set_status(msg, 0, 0),
        ))
    except Exception as e:
        add_url_result = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    finally:
        add_url_thread = None
        progress_tracker.clear_status()

@app.route('/api/jobs/from-url', methods=['POST'])
def add_job_from_url():
    global add_url_thread, add_url_result
    if add_url_thread and add_url_thread.is_alive():
        return jsonify({'status': 'already_running'})

    data = request.json or {}
    url = (data.get('url') or '').strip()
    if not url.startswith(('http://', 'https://')):
        return jsonify({'status': 'error', 'error': 'Enter a full job URL starting with http.'}), 400

    # A link for a job already on file is only re-read when the person says so.
    import generate_from_url
    known = generate_from_url.existing_job(url)
    if known and known['status'] in ('generating', 'evaluating', 'applying'):
        return jsonify({'status': 'error', 'error': 'That job is busy (being written, scored or '
                        'applied to) - try again when it has finished.'}), 409
    if known and not data.get('confirm'):
        known['protected'] = known['status'] in db.PROTECTED_STATUSES
        return jsonify({'status': 'exists', 'job': known})

    add_url_result = None
    add_url_thread = threading.Thread(
        target=run_add_url_bg, args=(url, (data.get('instructions') or '').strip() or None))
    add_url_thread.start()
    return jsonify({'status': 'started'})

@app.route('/api/jobs/from-url/status', methods=['GET'])
def add_job_from_url_status():
    running = add_url_thread is not None and add_url_thread.is_alive()
    return jsonify({
        'running': running,
        'stage': progress_tracker.get_status().get('current_stage') if running else '',
        'result': None if running else add_url_result,
    })

# The two files that steer the search: what to look for, and how to score it.
SETTINGS_FILES = {
    'search_criteria': 'search_criteria.md',
    'rules': 'rules.md',
}

@app.route('/api/settings', methods=['GET'])
def get_settings():
    out = {}
    for key, filename in SETTINGS_FILES.items():
        path = os.path.join(CVS_DIR, filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                out[key] = {'content': f.read(), 'path': path}
        except FileNotFoundError:
            out[key] = {'content': '', 'path': path}
        except Exception as e:
            return jsonify({'error': f'Could not read {filename}: {e}'}), 500
    out['min_pass_score'] = os.environ.get('MIN_PASS_SCORE', '7')
    # In safe mode LinkedIn is not searched: the candidate's job alerts are the
    # searches (#17). Show which alerts have been arriving, and which to create
    # to cover search_criteria.md - which still drives Adzuna.
    import mode
    out['mode'] = mode.current_mode()
    if mode.safe_mode():
        import alerts, adzuna, importlib, scraper
        importlib.reload(scraper)
        try:
            keywords, locations, _ = scraper.get_search_criteria()
            suggested = alerts.suggested_alerts(keywords, locations)
        except Exception:
            suggested = []
        db.init_db()
        out['alerts'] = {'recent': alerts.recent_alerts(), 'suggested': suggested,
                         'imap_configured': alerts.configured(),
                         'adzuna_configured': adzuna.configured(),
                         'cap': alerts.EMAIL_JOB_CAP}
    return jsonify(out)

@app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.json or {}
    saved = []
    for key, filename in SETTINGS_FILES.items():
        if key not in data:
            continue
        content = data[key]
        if not isinstance(content, str) or not content.strip():
            return jsonify({'error': f'{filename} would be empty - not saving.'}), 400
        path = os.path.join(CVS_DIR, filename)
        try:
            # Keep one backup so a bad edit is recoverable.
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    previous = f.read()
                with open(path + '.bak', 'w', encoding='utf-8') as f:
                    f.write(previous)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content if content.endswith('\n') else content + '\n')
            saved.append(filename)
        except Exception as e:
            return jsonify({'error': f'Could not write {filename}: {e}'}), 500

    # Report back how the search parses now, so a typo is visible immediately.
    summary = None
    try:
        import importlib, scraper
        importlib.reload(scraper)
        keyword_specs, locations, job_types = scraper.get_search_criteria()
        summary = {
            'keywords': [{'keyword': k, 'locations': locs or locations} for k, locs in keyword_specs],
            'job_types': job_types,
            # One search per keyword/location pair, each read three pages deep.
            'searches': sum(len(locs or locations) for _, locs in keyword_specs),
            'pages': sum(len(locs or locations) for _, locs in keyword_specs) * 3,
        }
    except Exception as e:
        summary = {'error': str(e)}

    return jsonify({'saved': saved, 'parsed': summary})

@app.route('/favicon.ico')
def favicon():
    # Browsers and bookmark tools ask for /favicon.ico regardless of <link> tags.
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico',
                               mimetype='image/x-icon')

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
    # Debug off by default: under launchd the reloader spawns a second process
    # that fights for the port. Set FLASK_DEBUG=1 for local development.
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(host='0.0.0.0', debug=debug, port=int(os.environ.get('DASHBOARD_PORT', 5050)))
