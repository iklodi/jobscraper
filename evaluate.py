import db
import os
import json
from groq import Groq
from google import genai
from google.genai import types
from docx import Document
import time
import progress_tracker

CVS_DIR = os.environ.get('CVS_DIR', 'cvs')
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
    'llama-3.3-70b-versatile',
    'llama-3.1-8b-instant'
]

def close_github_issue(issue_number, reason):
    from github import Github, Auth
    token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPO", "your_username/your_repo")
    if not token:
        print("Cannot close GitHub issue: no GITHUB_TOKEN.")
        return
    try:
        g = Github(auth=Auth.Token(token) if hasattr(Auth, 'Token') else token)
        repo = g.get_repo(repo_name)
        issue = repo.get_issue(issue_number)
        issue.create_comment(f"Automated AI Update: Closing this issue because {reason}")
        issue.edit(state='closed')
        print(f"Closed issue #{issue_number} on GitHub.")
    except Exception as e:
        print(f"Failed to close issue #{issue_number}: {e}")

def compare_jobs(groq_client, gemini_client, comp_prompt):
    max_retries = 3
    delay = 2
    for attempt in range(max_retries):
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
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT company, title FROM jobs WHERE status IN ("applied", "interviewing", "offer", "rejected")')
    rows = cursor.fetchall()
    conn.close()
    return [f"{company} - {title}" for company, title in rows]

def get_evaluation_rules():
    rules_path = os.path.join(CVS_DIR, 'rules.md')
    if not os.path.exists(rules_path):
        return ""
    with open(rules_path, 'r') as f:
        return f.read()

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
    prompt_template = load_prompt('eval_prompt.txt')
    prompt = prompt_template.replace('{cv_text}', cv_text) \
                            .replace('{job_title}', job_title) \
                            .replace('{job_company}', job_company) \
                            .replace('{job_location}', job_location) \
                            .replace('{is_promoted}', str(is_promoted)) \
                            .replace('{job_desc}', job_desc) \
                            .replace('{rules_text}', rules_text) \
                            .replace('{previous_applications}', chr(10).join(previous_applications))
    
    if custom_instructions:
        prompt += f"\n\nCRITICAL CUSTOM INSTRUCTIONS FROM USER FOR RE-EVALUATION:\n{custom_instructions}\n"
    
    
    max_retries = 5
    delay = 10
    
    for attempt in range(max_retries):
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
        
        if not groq_client and not gemini_client:
            print("Error: Neither GROQ_API_KEY nor GEMINI_API_KEY are configured.")
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
    
    if not groq_client and not gemini_client:
        print("Error: You must set either GROQ_API_KEY or GEMINI_API_KEY in your .env file.")
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
    
    eval_stats = {'score_counts': {}, 'recent_backlog': []}

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
            
            eval_stats['score_counts'][score] = eval_stats['score_counts'].get(score, 0) + 1
            if score >= int(os.environ.get('MIN_PASS_SCORE', 9)):
                eval_stats['recent_backlog'].append({
                    'job_id': job_id,
                    'title': title,
                    'company': company,
                    'score': score
                })
                
            print(f"--> Score: {score}/10")
            if score >= 8:
                print(f"--> Salary: {estimated_salary} | Recruiter: {is_recruiter} | HM: {hiring_manager_name} | Lang: {jd_language}")
            
            if score >= int(os.environ.get('MIN_PASS_SCORE', 9)):
                competing = db.get_competing_jobs(company, job_id)
                if competing:
                    c_id, c_title, c_desc, c_issue = competing[0]
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
                        pref = comp_result.get('preferred_job', 'OLD')
                        comp_reason = comp_result.get('reasoning', '')
                        if pref == 'NEW':
                            print(f"--> AI prefers the NEW job. Rejecting old job '{c_title}'...")
                            db.update_job_status(c_id, 'rejected')
                            if c_issue:
                                close_github_issue(c_issue, f"we found a better fitting role at the same company: '{title}'. AI Reasoning: {comp_reason}")
                        else:
                            print(f"--> AI prefers the OLD job. Rejecting new job '{title}'...")
                            score = 1
                            reasoning = f"Rejected in favor of existing backlog job '{c_title}'. AI Reasoning: {comp_reason}"
            
            # Save to DB
            db.update_job_score(job_id, score, reasoning, estimated_salary, is_recruiter, hiring_manager_name, jd_language)
        else:
            print(f"--> Failed to evaluate.")
            
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
    
    if not groq_client and not gemini_client:
        print("Error: You must set either GROQ_API_KEY or GEMINI_API_KEY in your .env file.")
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
        
        db.update_job_score(job_id, score, reasoning, estimated_salary, is_recruiter, hiring_manager_name, jd_language)
        return True
    else:
        print("Failed to evaluate.")
        return False

if __name__ == '__main__':
    run_evaluation()
