import db
import os
import json
from groq import Groq
from google import genai
from google.genai import types
from docx import Document
import time

# Configuration
DOSSIER_PATH = '/path/to/cvs/docs/Career_Dossier.md'
GEMINI_MODELS = [
#    'gemini-3.1-pro-preview',
    'gemini-3.5-flash',
    'gemini-3-flash-preview',
    'gemini-2.5-flash'
]
GROQ_MODELS = [
    'llama-3.3-70b-versatile',
    'llama-3.1-8b-instant'
]


def get_previous_applications():
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT company, title FROM jobs WHERE status IN ("applied", "interviewing", "offer", "rejected")')
    rows = cursor.fetchall()
    conn.close()
    return [f"{company} - {title}" for company, title in rows]

def get_evaluation_rules():
    rules_path = '/path/to/cvs/rules.md'
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

def evaluate_job(groq_client, gemini_client, job_title, job_company, job_location, job_desc, cv_text, previous_applications, rules_text, is_promoted):
    prompt = f"""
    You are an expert tech recruiter and career advisor.
    I want you to evaluate the following job posting against my CV.
    
    My CV:
    {cv_text}
    
    Job Title: {job_title}
    Company: {job_company}
    Location: {job_location}
    Is Promoted Job: {is_promoted}
    Job Description:
    {job_desc}
    
    Evaluate the fit on a scale of 1 to 10 (10 being a perfect match).
    NOTE: If "Is Promoted Job" is True, be slightly more critical of the match, as promoted jobs often have lower organic relevance.
    
    {rules_text}
    
    Here is a list of job applications I have ALREADY submitted in the past (based on my archive):
    {chr(10).join(previous_applications)}
    
    CRITICAL DUPLICATE RULE: If this job is highly likely to be the exact same role at the exact same company as one of the past applications in the list above, you MUST score it a 1 and set the reasoning strictly to "Already applied".
    
    Provide a brief reasoning, and list any key missing skills.
    
    If the score you are giving is 8 or higher, you MUST also determine:
    1. "salary_estimate": An estimate of the salary range (e.g. "120k-150k CHF", "100k-120k EUR") based on the location, role, and typical market rates. If completely unknown, output "Unknown".
    2. "is_recruiter": A boolean (true/false). Set to true if the job is posted by a recruiting/staffing agency (e.g. Optomi, Hays, Michael Page). Set to false if it's a direct role with the employing company.
    If the score is below 8, you may leave these as null.
    
    Your response must be valid JSON in the following format:
    {{
        "score": 8,
        "reasoning": "Strong match with enterprise architecture experience...",
        "missing_skills": ["AWS", "Kubernetes"],
        "salary_estimate": "130k-160k CHF",
        "is_recruiter": false
    }}
    """
    
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

    for job in unscored_jobs:
        job_id, title, company, location, description, is_promoted = job
        print(f"Evaluating: {title} at {company} ({location}) (Promoted: {is_promoted})...")
        
        result = evaluate_job(
            groq_client,
            gemini_client,
            title, company, location, description, cv_text, previous_applications, rules_text, is_promoted)
        if result:
            score = result.get('score', 0)
            reasoning = result.get('reasoning', '')
            estimated_salary = result.get('salary_estimate', None)
            is_recruiter = result.get('is_recruiter', None)
            print(f"--> Score: {score}/10")
            if score >= 8:
                print(f"--> Salary: {estimated_salary} | Recruiter: {is_recruiter}")
            
            # Save to DB
            db.update_job_score(job_id, score, reasoning, estimated_salary, is_recruiter)
        else:
            print(f"--> Failed to evaluate.")
            
        # Dynamic Rate limit sleep
        # We don't want a fixed 4.5s delay if we are using Pro models, but 1.5s should be safe since the cascade handles the rest.
        time.sleep(1.5)

if __name__ == '__main__':
    run_evaluation()
