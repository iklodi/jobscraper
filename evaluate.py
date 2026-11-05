import db
import os
import json
from groq import Groq
from google import genai
from google.genai import types
from docx import Document
import time
import progress_tracker
import infomaniak

CVS_DIR = os.environ.get('CVS_DIR', 'cvs')


def min_pass_score():
    """The score at or above which a job goes to To Do - and from which the
    prompt asks for recruiter, hiring manager and language. The salary
    estimate is asked for every job."""
    return db.min_pass_score()

DOSSIER_NAME = os.environ.get('DOSSIER_NAME', 'Career_Dossier.md')
DOSSIER_PATH = os.path.join(CVS_DIR, 'docs', DOSSIER_NAME)

def load_prompt(filename):
    path = os.path.join(os.path.dirname(__file__), 'prompts', filename)
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()
GEMINI_MODELS = [
#    'gemini-3.1-pro-preview',
    'gemini-3.5-flash',
    'gemini-3-flash-preview',
    'gemini-3.1-flash-lite'
]
GROQ_MODELS = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b'
]

# Infomaniak's endpoint only accepts response_format 'json_schema', so the
# shapes the prompts ask for have to be spelled out. Fields the prompt lets
# the model skip below a score of 8 are nullable rather than omitted.
EVAL_SCHEMA = {
    'type': 'object',
    'properties': {
        'score': {'type': 'integer'},
        'reasoning': {'type': 'string'},
        'missing_skills': {'type': 'array', 'items': {'type': 'string'}},
        'salary_estimate': {'type': ['string', 'null']},
        # The same estimate as one figure, for the relocation rule in code.
        'salary_chf_annual': {'type': ['integer', 'null']},
        'is_recruiter': {'type': ['boolean', 'null']},
        'hiring_manager_name': {'type': ['string', 'null']},
        'jd_language': {'type': ['string', 'null']},
        # Facts, not judgements: location_gate() applies the rules to them.
        'work_arrangement': {'type': 'string', 'enum': ['on-site', 'hybrid', 'remote', 'unknown']},
        'country': {'type': ['string', 'null']},
        'canton': {'type': ['string', 'null']},
        'track': {'type': ['string', 'null']},
    },
    'required': ['work_arrangement', 'country', 'canton', 'score', 'reasoning'],
}

COMPARE_SCHEMA = {
    'type': 'object',
    'properties': {
        'preferred_job': {'type': 'string'},
        'reasoning': {'type': 'string'},
    },
    'required': ['preferred_job', 'reasoning'],
}


def compare_jobs(groq_client, gemini_client, comp_prompt):
    max_retries = 3
    delay = 2
    for attempt in range(max_retries):
        # Infomaniak first when configured: Swiss-hosted, and it keeps the
        # scoring off Gemini's quota. Falls through silently when unset.
        result = infomaniak.chat_json(comp_prompt, COMPARE_SCHEMA)
        if result:
            return result
        if gemini_client:
            for model_name in GEMINI_MODELS:
                try:
                    response = gemini_client.models.generate_content(
                        model=model_name,
                        contents=comp_prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json")
                    )
                    text = response.text.strip()
                    if text.startswith('```json'): text = text[7:]
                    elif text.startswith('```'): text = text[3:]
                    if text.endswith('```'): text = text[:-3]
                    start = text.find('{'); end = text.rfind('}')
                    if start != -1 and end != -1: text = text[start:end+1]
                    return json.loads(text)
                except Exception:
                    continue
        if groq_client:
            for model_name in GROQ_MODELS:
                try:
                    response = groq_client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "user", "content": comp_prompt}],
                        response_format={"type": "json_object"},
                        temperature=0.1
                    )
                    text = response.choices[0].message.content.strip()
                    if text.startswith('```json'): text = text[7:]
                    elif text.startswith('```'): text = text[3:]
                    if text.endswith('```'): text = text[:-3]
                    start = text.find('{'); end = text.rfind('}')
                    if start != -1 and end != -1: text = text[start:end+1]
                    return json.loads(text)
                except Exception:
                    continue
        time.sleep(delay)
        delay *= 2
    return None


def get_previous_applications():
    """(company, title) of every application actually sent: Applied,
    Interviewing, Offer or Declined, or Rejected after having been Applied (a
    decline recorded before the Declined column existed).
    Plain 'rejected' also covers jobs turned down by hand or by the
    same-company comparison - never applied to - and listing those told the
    model they were applications."""
    conn = db.get_connection()
    rows = conn.execute('''
        SELECT company, title FROM jobs j
        WHERE status IN ('applied', 'interviewing', 'offer', 'declined')
           OR (status = 'rejected' AND EXISTS (SELECT 1 FROM job_history h
                WHERE h.job_id = j.job_id AND h.new_status = 'applied'))''').fetchall()
    conn.close()
    return [(company or '', title or '') for company, title in rows]


def applications_at(company, previous_applications):
    """The past applications that could be duplicates of a job at `company` -
    only that company's. The whole list was 40,000+ characters that buried
    the rules."""
    from dedupe import company_key
    key = company_key(company)
    same = [f'{c} - {t}' for c, t in previous_applications
            if key and (company_key(c) == key or key in company_key(c) or company_key(c) in key)]
    return same or ['(none at this company)']


def _allowed(setting):
    return {v.strip().lower() for v in os.environ.get(setting, '').split(',') if v.strip()}


def location_gate(result):
    """The hard location rules, applied in code to the facts the model reports.
    The model knows a town's canton, but it does not reliably apply a rule
    such as "on-site only in VD, VS or FR": asked directly, it called an
    on-site Geneva role compliant. Settings (.env, empty = no rule):
    ONSITE_CANTONS (two-letter codes), HYBRID_COUNTRIES, and for a move
    abroad RELOCATION_COUNTRIES with RELOCATION_MIN_SALARY_CHF: a job there
    that breaks the other rules passes when the model's annual pay estimate
    (salary_chf_annual) reaches the bar. Returns the reason when a rule is
    broken, else None."""
    arrangement = (result.get('work_arrangement') or '').lower()
    country = (result.get('country') or '').strip()
    canton = (result.get('canton') or '').strip()
    cantons, countries = _allowed('ONSITE_CANTONS'), _allowed('HYBRID_COUNTRIES')
    broken = None
    if arrangement == 'on-site' and cantons and canton.lower() not in cantons:
        broken = f"Location mismatch: on-site in {canton or country or 'an unknown place'}"
    elif arrangement == 'hybrid' and countries and country.lower() not in countries:
        broken = f"Location mismatch: hybrid in {country or 'an unknown country'}"
    if broken and country.lower() in _allowed('RELOCATION_COUNTRIES'):
        bar = int(os.environ.get('RELOCATION_MIN_SALARY_CHF', '0') or 0)
        pay = result.get('salary_chf_annual')
        if isinstance(pay, (int, float)) and pay >= bar:
            return None
        return (f"Location mismatch: {arrangement} in {country}, worth a move only from "
                f"CHF {bar:,} a year (estimated: {f'CHF {int(pay):,}' if isinstance(pay, (int, float)) else 'unknown'})")
    return broken


OTHER_TRACK = 'Other'


def get_tracks():
    # Imported here, as adzuna.py does: scraper pulls in Playwright.
    from scraper import get_tracks as read_tracks
    return read_tracks()


def tracks_text(tracks):
    """The track list for the prompt, one "- Name: description" per line."""
    return '\n'.join(f'- {name}: {about}' if about else f'- {name}' for name, about in tracks)


def normalise_track(value, tracks):
    """The configured track name the model meant (case and spacing aside), or
    "Other" - never a name it made up. None when no tracks are configured."""
    if not tracks:
        return None
    wanted = ' '.join(str(value or '').split()).lower()
    for name, _ in tracks:
        if ' '.join(name.split()).lower() == wanted:
            return name
    return OTHER_TRACK


def get_evaluation_rules():
    rules_path = os.path.join(CVS_DIR, 'rules.md')
    if not os.path.exists(rules_path):
        return ""
    with open(rules_path, 'r') as f:
        return f.read()

def any_model_configured(groq_client=None, gemini_client=None):
    """True when at least one provider can answer. Infomaniak alone is enough;
    the gates used to check only Gemini and Groq, so an Infomaniak-only setup
    silently skipped evaluation."""
    return bool(infomaniak.get_config() or groq_client or gemini_client)


def get_groq_client():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    return Groq(api_key=api_key)

def get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    return genai.Client(api_key=api_key)

def evaluate_job(groq_client, gemini_client, job_title, job_company, job_location, job_desc, cv_text, previous_applications, rules_text, is_promoted, custom_instructions=None):
    """Score one job: the model's verdict, then the hard location rules on top."""
    if previous_applications and isinstance(previous_applications[0], tuple):
        previous_applications = applications_at(job_company, previous_applications)
    result = _ask_evaluator(groq_client, gemini_client, job_title, job_company, job_location, job_desc,
                            cv_text, previous_applications, rules_text, is_promoted, custom_instructions)
    if result:
        result['track'] = normalise_track(result.get('track'), get_tracks())
        broken = location_gate(result)
        if broken and (result.get('score') or 0) > 2:
            result['score'] = 2
            result['reasoning'] = f"{broken}. {result.get('reasoning') or ''}".strip()
    return result


def _ask_evaluator(groq_client, gemini_client, job_title, job_company, job_location, job_desc, cv_text, previous_applications, rules_text, is_promoted, custom_instructions=None):
    prompt_template = load_prompt('eval_prompt.txt')
    prompt = prompt_template.replace('{cv_text}', cv_text) \
                            .replace('{job_title}', job_title) \
                            .replace('{job_company}', job_company) \
                            .replace('{job_location}', job_location) \
                            .replace('{is_promoted}', str(is_promoted)) \
                            .replace('{job_desc}', job_desc) \
                            .replace('{rules_text}', rules_text) \
                            .replace('{previous_applications}', chr(10).join(previous_applications)) \
                            .replace('{min_score}', str(min_pass_score())) \
                            .replace('{tracks}', tracks_text(get_tracks()) or '(none configured)')
    
    if custom_instructions:
        prompt += f"\n\nCRITICAL CUSTOM INSTRUCTIONS FROM USER FOR RE-EVALUATION:\n{custom_instructions}\n"
    
    
    max_retries = 5
    delay = 10
    
    for attempt in range(max_retries):
        # Infomaniak first when configured; unset, this costs one dict lookup.
        result = infomaniak.chat_json(prompt, EVAL_SCHEMA)
        if result:
            return result

        # Try all Gemini models in descending order of quality
        if gemini_client:
            for model_name in GEMINI_MODELS:
                try:
                    response = gemini_client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                        ),
                    )
                    
                    text = response.text.strip()
                    if text.startswith('```json'):
                        text = text[7:]
                    elif text.startswith('```'):
                        text = text[3:]
                    if text.endswith('```'):
                        text = text[:-3]
                    
                    # Safety catch to only parse what's inside the braces
                    start = text.find('{')
                    end = text.rfind('}')
                    if start != -1 and end != -1:
                        text = text[start:end+1]
                        
                    return json.loads(text)
                except Exception as e:
                    error_str = str(e).lower()
                    if '429' in error_str or 'resource_exhausted' in error_str or 'quota' in error_str:
                        print(f"  -> Gemini {model_name} rate limited. Cascading to next model...")
                        continue
                    elif '503' in error_str or 'unavailable' in error_str:
                        print(f"  -> Gemini {model_name} overloaded. Cascading to next model...")
                        continue
                    else:
                        print(f"  -> Gemini Error on {model_name}: {e}")
                        continue
        
        # Fallback to Groq models
        if groq_client:
            for model_name in GROQ_MODELS:
                try:
                    response = groq_client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"},
                        temperature=0.1
                    )
                    
                    text = response.choices[0].message.content.strip()
                    if text.startswith('```json'):
                        text = text[7:]
                    elif text.startswith('```'):
                        text = text[3:]
                    if text.endswith('```'):
                        text = text[:-3]
                        
                    start = text.find('{')
                    end = text.rfind('}')
                    if start != -1 and end != -1:
                        text = text[start:end+1]
                        
                    return json.loads(text)
                except Exception as e:
                    error_str = str(e).lower()
                    if '429' in error_str or 'rate_limit' in error_str:
                        print(f"  -> Groq {model_name} rate limited. Cascading...")
                        continue
                    elif '503' in error_str:
                        print(f"  -> Groq {model_name} overloaded. Cascading...")
                        continue
                    else:
                        print(f"  -> Groq Error on {model_name}: {e}")
                        continue
        
        if not any_model_configured(groq_client, gemini_client):
            print("Error: no model configured - set INFOMANIAK_API_TOKEN, GEMINI_API_KEY or GROQ_API_KEY.")
            return None
            
        # If we exhausted ALL models in the cascade, we hit a hard wall.
        print(f"All models exhausted. Sleeping for {delay} seconds to clear quotas before trying again...")
        time.sleep(delay)
        delay *= 2
                
    print("Max retries exceeded. Could not evaluate job.")
    return None

def run_evaluation():
    db.init_db()
    unscored_jobs = db.get_unscored_jobs()
    
    if not unscored_jobs:
        print("No unscored jobs found.")
        return
    
    print(f"Found {len(unscored_jobs)} jobs to evaluate.")
    
    groq_client = get_groq_client()
    gemini_client = get_gemini_client()
    
    if not any_model_configured(groq_client, gemini_client):
        print("Error: no model configured - set INFOMANIAK_API_TOKEN, GEMINI_API_KEY or GROQ_API_KEY.")
        return

    print("Loading Career Dossier...")
    try:
        with open(DOSSIER_PATH, 'r', encoding='utf-8') as f:
            cv_text = f.read()
    except Exception as e:
        print(f"Error reading Career Dossier: {e}")
        return

    previous_applications = get_previous_applications()
    rules_text = get_evaluation_rules()
    
    eval_stats = {'score_counts': {}, 'recent_backlog': [], 'near_misses': []}

    total_jobs = len(unscored_jobs)
    for idx, job in enumerate(unscored_jobs, 1):
        if progress_tracker.is_stop_requested():
            print("Stop requested during evaluation!")
            break
        progress_tracker.set_status("Evaluating Jobs", idx, total_jobs)
        
        job_id, title, company, location, description, is_promoted = job
        print(f"[{idx}/{total_jobs}] Evaluating: {title} at {company} ({location}) (Promoted: {is_promoted})...")
        
        result = evaluate_job(
            groq_client,
            gemini_client,
            title, company, location, description, cv_text, previous_applications, rules_text, is_promoted)
        if result:
            score = result.get('score', 0)
            reasoning = result.get('reasoning', '')
            estimated_salary = result.get('salary_estimate', None)
            is_recruiter = result.get('is_recruiter', None)
            hiring_manager_name = result.get('hiring_manager_name', None)
            jd_language = result.get('jd_language', None)
            
            print(f"--> Score: {score}/10")
            if score >= min_pass_score():
                print(f"--> Salary: {estimated_salary} | Recruiter: {is_recruiter} | HM: {hiring_manager_name} | Lang: {jd_language}")
            
            # One role per company only makes sense for a direct employer. A
            # recruiter or job aggregator posts for many unrelated
            # clients under one name, and comparing those rejected real matches.
            track = result.get('track')
            if score >= min_pass_score() and not is_recruiter:
                competing = db.get_competing_jobs(company, job_id, track)
                if competing:
                    c_id, c_title, c_desc = competing[0]
                    print(f"--> Found competing active job for {company}: '{c_title}'. Invoking AI comparison...")
                    comp_prompt_template = load_prompt('compare_prompt.txt')
                    comp_prompt = comp_prompt_template.replace('{title}', title) \
                                                      .replace('{company}', company) \
                                                      .replace('{description}', description) \
                                                      .replace('{c_title}', c_title) \
                                                      .replace('{c_desc}', c_desc) \
                                                      .replace('{cv_text}', cv_text)
                    comp_result = compare_jobs(groq_client, gemini_client, comp_prompt)
                    if comp_result:
                        # The prompt asks for "new"/"old" in lower case; this
                        # compared against 'NEW' for months, so the new job was
                        # rejected even when the model preferred it.
                        pref = str(comp_result.get('preferred_job', 'old')).strip().lower()
                        comp_reason = comp_result.get('reasoning', '')
                        if pref == 'new':
                            print(f"--> AI prefers the NEW job. Rejecting old job '{c_title}'...")
                            db.update_job_status(c_id, 'rejected')
                        else:
                            print(f"--> AI prefers the OLD job. Rejecting new job '{title}'...")
                            score = 1
                            reasoning = f"Rejected in favor of existing backlog job '{c_title}'. AI Reasoning: {comp_reason}"
            
            # Counted after the comparison, so a job rejected in favour of an
            # existing one is not announced in the summary as a new match.
            eval_stats['score_counts'][score] = eval_stats['score_counts'].get(score, 0) + 1
            entry = {'job_id': job_id, 'title': title, 'company': company, 'score': score}
            if score >= min_pass_score():
                eval_stats['recent_backlog'].append(entry)
            elif score >= int(os.environ.get('NEAR_MISS_SCORE', 6)):
                # Worth a look even though they did not clear the bar - otherwise a
                # run with nothing over the bar produces a summary with nothing to click.
                eval_stats['near_misses'].append(entry)

            db.update_job_score(job_id, score, reasoning, estimated_salary, is_recruiter,
                                hiring_manager_name, jd_language, track)
        else:
            print("--> Failed to evaluate.")
            
        # Dynamic Rate limit sleep
        # We don't want a fixed 4.5s delay if we are using Pro models, but 1.5s should be safe since the cascade handles the rest.
        time.sleep(1.5)
        
    return eval_stats

def evaluate_single_job(job_id, custom_instructions=None):
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT job_id, title, company, location, description, is_promoted FROM jobs WHERE job_id = ?', (job_id,))
    job = cursor.fetchone()
    conn.close()
    
    if not job:
        print(f"Job {job_id} not found.")
        return False
        
    groq_client = get_groq_client()
    gemini_client = get_gemini_client()
    
    if not any_model_configured(groq_client, gemini_client):
        print("Error: no model configured - set INFOMANIAK_API_TOKEN, GEMINI_API_KEY or GROQ_API_KEY.")
        return False
        
    try:
        with open(DOSSIER_PATH, 'r', encoding='utf-8') as f:
            cv_text = f.read()
    except Exception as e:
        print(f"Error reading Career Dossier: {e}")
        return False
        
    previous_applications = get_previous_applications()
    rules_text = get_evaluation_rules()
    
    job_id, title, company, location, description, is_promoted = job
    print(f"Evaluating: {title} at {company} ({location}) (Promoted: {is_promoted})...")
    
    result = evaluate_job(
        groq_client,
        gemini_client,
        title, company, location, description, cv_text, previous_applications, rules_text, is_promoted, custom_instructions)
        
    if result:
        score = result.get('score', 0)
        reasoning = result.get('reasoning', '')
        estimated_salary = result.get('salary_estimate', None)
        is_recruiter = result.get('is_recruiter', None)
        hiring_manager_name = result.get('hiring_manager_name', None)
        jd_language = result.get('jd_language', None)
        print(f"--> Score: {score}/10")
        
        db.update_job_score(job_id, score, reasoning, estimated_salary, is_recruiter,
                            hiring_manager_name, jd_language, result.get('track'))
        return True
    else:
        print("Failed to evaluate.")
        return False

TRACK_SCHEMA = {
    'type': 'object',
    'properties': {'track': {'type': 'string'}},
    'required': ['track'],
}
# Columns a person works from; Rejected and Pending Eval are left out unless asked.
ACTIVE_STATUSES = ('to_apply', 'generated', 'approved', 'ready_to_submit', 'easy_apply',
                   'applied', 'interviewing', 'declined', 'failed', 'account_required')


def classify_tracks(statuses=ACTIVE_STATUSES, ask=None):
    """File jobs scored before tracks existed under one, without re-scoring
    them: a short prompt per job. Returns {track: jobs filed}.
    `ask` is for tests; by default the Infomaniak model answers."""
    tracks = get_tracks()
    if not tracks:
        print('No "# Tracks" section in search_criteria.md - nothing to file jobs under.')
        return {}
    ask = ask or (lambda prompt: infomaniak.chat_json(prompt, TRACK_SCHEMA))
    template = load_prompt('track_prompt.txt')
    conn = db.get_connection()
    marks = ','.join('?' * len(statuses))
    jobs = conn.execute(f"SELECT job_id, title, company, description FROM jobs "
                        f"WHERE track IS NULL AND status IN ({marks})", statuses).fetchall()
    conn.close()
    counts = {}
    for index, (job_id, title, company, description) in enumerate(jobs, 1):
        prompt = template.replace('{tracks}', tracks_text(tracks)) \
                         .replace('{job_title}', title or '') \
                         .replace('{job_company}', company or '') \
                         .replace('{job_desc}', (description or '')[:3000])
        result = ask(prompt)
        if not result:
            print(f'[{index}/{len(jobs)}] {job_id}: no answer, left unfiled')
            continue
        track = normalise_track(result.get('track'), tracks)
        conn = db.get_connection()
        conn.execute('UPDATE jobs SET track = ? WHERE job_id = ?', (track, job_id))
        conn.commit()
        conn.close()
        counts[track] = counts.get(track, 0) + 1
        print(f'[{index}/{len(jobs)}] {title} at {company}: {track}')
    return counts


if __name__ == '__main__':
    import sys
    if '--classify-tracks' in sys.argv:
        # --all includes Pending Eval and Rejected too.
        print(classify_tracks(('new', 'scored', 'rejected') + ACTIVE_STATUSES
                              if '--all' in sys.argv else ACTIVE_STATUSES))
    else:
        run_evaluation()
