import db
import os
import json
from google import genai
from google.genai import types
from docx import Document
import time

# Configuration
CV_PATH = '/path/to/cvs/docs/Base_CV_Template.docx'
MODEL_NAME = 'gemini-2.5-flash' # Switching to new model to reset daily quota limits

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

def get_gemini_client():
    # Make sure GEMINI_API_KEY is set in your environment variables
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Please set the GEMINI_API_KEY environment variable.")
    return genai.Client(api_key=api_key)

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
    
    max_retries = 3
    delay = 10
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            # Parse the JSON response
            return json.loads(response.text)
        except Exception as e:
            error_str = str(e)
            if '429' in error_str or 'RESOURCE_EXHAUSTED' in error_str:
                print(f"Rate limited (429 RESOURCE_EXHAUSTED). Retrying in {delay} seconds...")
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
        client = get_gemini_client()
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
