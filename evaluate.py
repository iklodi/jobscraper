import db
import os
import json
from groq import Groq
from docx import Document
import time

# Configuration
CV_PATH = '/path/to/cvs/docs/Base_CV_Template.docx'
MODEL_NAME = 'llama-3.3-70b-versatile' # Switching to Groq API for extreme speed and reliability

def extract_text_from_docx(file_path):
    doc = Document(file_path)
    return "\n".join([p.text for p in doc.paragraphs])

def get_previous_applications():
    job_dir = '/path/to/cvs/jobs'
    if not os.path.exists(job_dir):
        return []
    return [f for f in os.listdir(job_dir) if not f.startswith('.')]

def get_evaluation_rules():
    rules_path = '/path/to/cvs/rules.md'
    if not os.path.exists(rules_path):
        return ""
    with open(rules_path, 'r') as f:
        return f.read()

def get_groq_client():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError("Please set the GROQ_API_KEY environment variable. You can get one for free at console.groq.com")
    return Groq(api_key=api_key)

def evaluate_job(client, job_title, job_company, job_desc, cv_text, previous_applications, rules_text, is_promoted):
    prompt = f"""
    You are an expert tech recruiter and career advisor.
    I want you to evaluate the following job posting against my CV.
    
    My CV:
    {cv_text}
    
    Job Title: {job_title}
    Company: {job_company}
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
    
    Your response must be valid JSON in the following format:
    {{
        "score": 8,
        "reasoning": "Strong match with enterprise architecture experience...",
        "missing_skills": ["AWS", "Kubernetes"]
    }}
    """
    
    max_retries = 5
    delay = 10
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            # Parse the JSON response
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            error_str = str(e)
            if '429' in error_str or 'RESOURCE_EXHAUSTED' in error_str or '503' in error_str or 'UNAVAILABLE' in error_str:
                print(f"API overload (429/503). Retrying in {delay} seconds...")
                time.sleep(delay)
                delay *= 2
            else:
                print(f"Error evaluating job: {e}")
                return None
                
    print("Max retries exceeded for evaluating job.")
    return None

def run_evaluation():
    db.init_db()
    unscored_jobs = db.get_unscored_jobs()
    
    if not unscored_jobs:
        print("No unscored jobs found.")
        return
    
    print(f"Found {len(unscored_jobs)} jobs to evaluate.")
    
    try:
        client = get_groq_client()
    except Exception as e:
        print(e)
        return

    print("Extracting CV text...")
    try:
        cv_text = extract_text_from_docx(CV_PATH)
    except Exception as e:
        print(f"Error reading CV: {e}")
        return

    previous_applications = get_previous_applications()
    rules_text = get_evaluation_rules()

    for job in unscored_jobs:
        job_id, title, company, description, is_promoted = job
        print(f"Evaluating: {title} at {company} (Promoted: {is_promoted})...")
        
        result = evaluate_job(client, title, company, description, cv_text, previous_applications, rules_text, is_promoted)
        if result:
            score = result.get('score', 0)
            reasoning = result.get('reasoning', '')
            print(f"--> Score: {score}/10")
            
            # Save to DB
            db.update_job_score(job_id, score, reasoning)
        else:
            print(f"--> Failed to evaluate.")
            
        # Rate limit to avoid 429 errors (Gemini Free Tier is 15 RPM)
        time.sleep(4.5)

if __name__ == '__main__':
    run_evaluation()
