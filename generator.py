import db
import os
import datetime
from docx import Document
from docx2pdf import convert
from google import genai
from google.genai import types

CV_PATH = '/path/to/cvs/docs/Base_CV_Template.docx'
CL_PATH = '/path/to/cvs/docs/Base_CL_Template.docx' # Using EY as a generic baseline for now
OUTPUT_DIR = '/path/to/cvs/applications'
MODEL_NAME = 'gemini-2.5-pro'

def get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Please set the GEMINI_API_KEY environment variable.")
    return genai.Client(api_key=api_key)

def generate_tailored_texts(client, job, cv_text):
    job_id, title, company, location, description, link, score, reasoning, status, created_at = job
    
    prompt = f"""
    You are an expert career advisor. I am applying for the '{title}' role at '{company}'.
    Here is the job description:
    {description}
    
    Here is my current CV:
    {cv_text}
    
    Task 1: Write a tailored 3-4 sentence professional summary for my CV that specifically highlights my fit for this role.
    Task 2: Write a tailored, compelling cover letter (around 250 words) addressed to the hiring manager at {company}.
    
    Your response must be valid JSON in this format:
    {{
        "cv_summary": "...",
        "cover_letter_body": "..."
    }}
    """
    
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        import json
        return json.loads(response.text)
    except Exception as e:
        print(f"Error generating tailored text: {e}")
        return None

def extract_text(file_path):
    doc = Document(file_path)
    return "\n".join([p.text for p in doc.paragraphs])

def adapt_cv(base_cv_path, new_cv_path, summary_text):
    doc = Document(base_cv_path)
    # Very simplistic replacement: we find the first paragraph that looks like a summary
    # and replace its text. In a real scenario, we might use placeholder tags like {{SUMMARY}}
    replaced = False
    for p in doc.paragraphs:
        if len(p.text) > 100 and not replaced:
            # Assume the first long paragraph is the summary
            p.text = summary_text
            replaced = True
            break
    doc.save(new_cv_path)

def adapt_cl(base_cl_path, new_cl_path, body_text):
    doc = Document(base_cl_path)
    # Replace the main body of the cover letter
    replaced = False
    for p in doc.paragraphs:
        if len(p.text) > 50 and not replaced:
            p.text = body_text
            replaced = True
            break
    doc.save(new_cl_path)

def run_generator():
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM jobs WHERE status = "to_apply"')
    jobs = cursor.fetchall()
    
    if not jobs:
        print("No jobs ready for document generation.")
        return
        
    client = get_gemini_client()
    cv_text = extract_text(CV_PATH)
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    for job in jobs:
        job_id, title, company, location, description, link, score, reasoning, status, created_at = job
        print(f"Generating documents for {company} - {title}...")
        
        texts = generate_tailored_texts(client, job, cv_text)
        if not texts:
            continue
            
        today = datetime.datetime.now().strftime("%Y%m%d")
        safe_company = "".join(x for x in company if x.isalnum())
        job_dir = os.path.join(OUTPUT_DIR, f"{today}_{safe_company}_{job_id}")
        os.makedirs(job_dir, exist_ok=True)
        
        new_cv_docx = os.path.join(job_dir, f"{today}_{safe_company}_CV.docx")
        new_cl_docx = os.path.join(job_dir, f"{today}_{safe_company}_CoverLetter.docx")
        new_cv_md = os.path.join(job_dir, f"{today}_{safe_company}_CV.md")
        new_cl_md = os.path.join(job_dir, f"{today}_{safe_company}_CoverLetter.md")
        
        # Adapt DOCX files
        adapt_cv(CV_PATH, new_cv_docx, texts['cv_summary'])
        adapt_cl(CL_PATH, new_cl_docx, texts['cover_letter_body'])
        
        # Save MD format
        with open(new_cv_md, 'w') as f:
            f.write(extract_text(new_cv_docx))
        with open(new_cl_md, 'w') as f:
            f.write(extract_text(new_cl_docx))
        
        # Convert to PDF
        print(f"Converting DOCX to PDF for {company}...")
        try:
            convert(new_cv_docx, new_cv_docx.replace('.docx', '.pdf'))
            convert(new_cl_docx, new_cl_docx.replace('.docx', '.pdf'))
        except Exception as e:
            print(f"PDF conversion failed (requires MS Word on Mac): {e}")
            
        # Update status
        cursor.execute('UPDATE jobs SET status = "generated" WHERE job_id = ?', (job_id,))
        conn.commit()
        print(f"Finished generating assets for {company}.")
        
    conn.close()

if __name__ == '__main__':
    run_generator()
