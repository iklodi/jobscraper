import db
import os
import json
import datetime
from docx import Document
from docx.shared import Pt
import subprocess
from groq import Groq
from google import genai
from google.genai import types
from playwright.async_api import async_playwright

CV_PATH = '/path/to/cvs/docs/Base_CV_Template.docx'
DOSSIER_PATH = '/path/to/cvs/docs/Career_Dossier.md'
CL_PATH = '/path/to/cvs/docs/Base_CL_Template.docx' # Using EY as a generic baseline for now
OUTPUT_DIR = '/path/to/cvs/applications'
GROQ_MODEL = 'llama-3.3-70b-versatile'
GEMINI_MODEL = 'gemini-3.5-flash'

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

def generate_tailored_texts(groq_client, gemini_client, job, cv_text, dossier_text):
    job_id, title, company, location, description, link, score, reasoning, status, created_at, is_promoted, issue_number, issue_url, estimated_salary, is_recruiter, hiring_manager_name, jd_language = job
    
    address_name = hiring_manager_name.split()[0] if hiring_manager_name else "the hiring manager"
    language_instruction = f"- Language Instruction: The job description is in {jd_language}. YOU MUST WRITE THE COVER LETTER ENTIRELY IN {jd_language}." if jd_language else ""
    
    company_context = f"addressed to {address_name} at {company}." if not is_recruiter else f"addressed to {address_name} at the recruiting agency '{company}'."
    
    if is_recruiter:
        company_mention_rule = f"- The job is posted by a recruiter/staffing agency. In the opening, DO NOT say you are applying to work AT '{company}' (since they are just the recruiter). Instead, either use the name of their client company if it's mentioned in the job description, or just mention the role without any company name."
    else:
        company_mention_rule = f"- In the opening of the letter, DO NOT mention the company name ('{company}') at all, as it is already obvious from the context. Just mention the role."

    prompt = f"""
    You are an expert career advisor. I am applying for the '{title}' role at '{company}'.
    Here is the job description:
    {description}
    
    Here is my current Base CV (the one being customized):
    {cv_text}
    
    Here is my full Career Dossier (containing all deep historical details of my career):
    {dossier_text}
    
    Task 1: Write a tailored 3-4 sentence professional summary for my CV that specifically highlights my fit for this role.
    Task 2: Customize my CV bullet points, skills, and title. You must output EXACT text from my Base CV that you want to replace or remove. 
    - Replace irrelevant bullet points or skills with customized ones.
    - Customize the main CV title (e.g. "Enterprise Architect") to match the target role if appropriate.
    - You may freely pull specific achievements and details from the Career Dossier to replace or rewrite bullet points in the CV to perfectly match the JD requirements and tone, but keep it concise.
    - Ensure you maintain the exact same length as the original CV. For every bullet point you replace or expand, ensure the overall document length does not grow to strictly preserve the 2-page limit.
    - CRITICAL RULE: NEVER remove or modify the AIESEC experience under any circumstances. It is vital for networking.
    - CRITICAL RULE: If you remove all bullet points under a heading like "Achievements:" or "Responsibilities:", you MUST also add that exact heading word (e.g. "Achievements:") to your cv_removals list so it isn't left hanging.
    
    Task 3: Write a tailored, compelling cover letter (max 150-200 words) {company_context}
    {language_instruction}
    
    CRITICAL TONE INSTRUCTIONS (COVER LETTER):
    - Write in a highly professional, direct, and human tone.
    - DO NOT use long dashes (em-dashes "—" or en-dashes "–"). Use commas, semicolons, or regular parentheses instead.
    - STRICTLY AVOID typical AI clichés and buzzwords (e.g., "delve", "tapestry", "testament", "beacon", "catalyst", "unleash", "elevate", "thrilled to apply", "embark", "spearhead", "pivotal").
    - Keep sentences concise, factual, and impactful. Do not overcomplicate the sentence structure.
    {company_mention_rule}
    
    CRITICAL TONE INSTRUCTIONS (CV BULLET POINTS):
    - NEVER use "I" or first-person structures (e.g. avoid "I coordinate with teams").
    - For Responsibilities: Use structures without verbs at all (e.g. "Strategic advisory and enterprise architecture leadership for global enterprise accounts") OR use base imperative verbs (e.g. "Drive global demand generation"). NEVER use "-ing" structures.
    - For Achievements: ALWAYS use simple past tense (e.g. "Acted as trusted advisor bridging technical architecture and business strategy").
    
    Your response must be valid JSON in this format:
    {{
        "cv_summary": "...",
        "cv_replacements": [
            {{"old": "exact old text to replace", "new": "customized new text"}}
        ],
        "cv_removals": [
            "exact old text to remove entirely"
        ],
        "cover_letter_body": "..."
    }}
    """
    
    max_retries = 5
    for attempt in range(max_retries):
        result = None
        error_msg = ""
        
        if gemini_client:
            try:
                response = gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    ),
                )
                import json
                result = json.loads(response.text)
            except Exception as e:
                error_msg += f"Gemini Error: {str(e)} | "
                
        if not result and groq_client:
            try:
                response = groq_client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.1
                )
                import json
                result = json.loads(response.choices[0].message.content)
            except Exception as e:
                error_msg += f"Groq Error: {str(e)}"
                
        if result:
            break
            
        if '429' in error_msg or 'RESOURCE_EXHAUSTED' in error_msg or 'rate_limit' in error_msg:
            print(f"API Rate Limit Block: {error_msg}")
            print("Falling back to 'Slow and Smart' mode (sleeping 32 seconds to clear TPM quota)...")
            import time
            time.sleep(32)
        elif '503' in error_msg or 'UNAVAILABLE' in error_msg:
            print(f"API overload detected: {error_msg}")
            import time
            time.sleep(10)
        else:
            raise Exception(f"Failed to generate documents from APIs: {error_msg}")
    
    # Aggressive post-processing fallback just in case the AI ignores the prompt
    result['cv_summary'] = result['cv_summary'].replace('—', ' - ').replace('–', ' - ')
    result['cover_letter_body'] = result['cover_letter_body'].replace('—', ' - ').replace('–', ' - ')
    return result

def extract_text(file_path):
    doc = Document(file_path)
    return "\n".join([p.text for p in doc.paragraphs])

def adapt_cv(base_cv_path, new_cv_path, texts):
    doc = Document(base_cv_path)
    summary_text = texts.get('cv_summary', '')
    replacements = texts.get('cv_replacements', [])
    removals = texts.get('cv_removals', [])
    
    replaced_summary = False
    for p in doc.paragraphs:
        p_text = p.text.strip()
        if not p_text:
            continue
            
        # 1. Replace Summary
        if len(p_text) > 100 and not replaced_summary:
            if p.runs:
                p.runs[0].text = summary_text
                for r in p.runs[1:]:
                    r.text = ""
            else:
                p.text = summary_text
            replaced_summary = True
            continue
            
        # 2. Check Removals
        if any(rem.strip() and rem.strip() in p_text for rem in removals):
            delete_paragraph(p)
            continue
            
        # 3. Check Replacements
        for rep in list(replacements):
            old_val = rep.get('old', '').strip()
            new_val = rep.get('new', '')
            if old_val and new_val and old_val in p_text:
                import re
                # Use regex with word boundaries to prevent partial word replacements like "Architect" replacing inside "Architecture"
                # If old_val contains special characters, re.escape handles them.
                pattern = r'\b' + re.escape(old_val) + r'\b'
                if re.search(pattern, p.text):
                    full_text = re.sub(pattern, new_val, p.text)
                    font_name = "Verdana"
                    font_size = None
                    if p.runs:
                        if p.runs[0].font.name:
                            font_name = p.runs[0].font.name
                        if p.runs[0].font.size:
                            font_size = p.runs[0].font.size
                            
                    p.clear()
                    run = p.add_run(full_text)
                    run.font.name = font_name
                    if font_size:
                        run.font.size = font_size
                    
                    replacements.remove(rep)
                    break
                elif old_val == p_text:
                    # Fallback for exact matches where word boundaries might fail (e.g. symbols)
                    replace_text_in_paragraph(p, old_val, new_val)
                    replacements.remove(rep)
                    break
                
    doc.save(new_cv_path)

def delete_paragraph(paragraph):
    p = paragraph._element
    p.getparent().remove(p)
    paragraph._p = paragraph._element = None

def replace_text_in_paragraph(paragraph, old_text, new_text):
    if old_text in paragraph.text:
        full_text = paragraph.text.replace(old_text, new_text)
        font_name = "Verdana"
        font_size = None
        if paragraph.runs:
            if paragraph.runs[0].font.name:
                font_name = paragraph.runs[0].font.name
            if paragraph.runs[0].font.size:
                font_size = paragraph.runs[0].font.size
                
        paragraph.clear()
        run = paragraph.add_run(full_text)
        run.font.name = font_name
        if font_size:
            run.font.size = font_size

def adapt_cl(base_cl_path, new_cl_path, body_text, company, location, hiring_manager_name, jd_language):
    doc = Document(base_cl_path)
    
    start_idx = -1
    for i, p in enumerate(doc.paragraphs):
        if p.text.strip().lower().startswith("dear "):
            start_idx = i
            break
            
    if start_idx != -1:
        import re
        if jd_language and "french" in jd_language.lower():
            location_clean = "Bex, Suisse"
        else:
            location_clean = re.sub(r'\s*\([^)]*\)', '', location).strip()
        
        # Replace the hard-coded addressee info in the header (paragraphs before "Dear ")
        today_str = datetime.datetime.now().strftime("%d.%m.%Y")
        for p in doc.paragraphs[:start_idx]:
            if hiring_manager_name:
                replace_text_in_paragraph(p, "Ernst & Young Hiring Team", hiring_manager_name)
            else:
                replace_text_in_paragraph(p, "Ernst & Young Hiring Team", f"{company} Hiring Team")
                
            replace_text_in_paragraph(p, "Ernst & Young", company)
            replace_text_in_paragraph(p, "Zurich, Switzerland", location_clean)
            replace_text_in_paragraph(p, "10.04.2026", today_str)
            replace_text_in_paragraph(p, "[COMPANY]", company)
            replace_text_in_paragraph(p, "[LOCATION]", location_clean)
            replace_text_in_paragraph(p, "[DATE]", today_str)

        # Find the reference paragraph for styling (the first actual body paragraph)
        ref_p = doc.paragraphs[start_idx]
        for i in range(start_idx, len(doc.paragraphs)):
            if len(doc.paragraphs[i].text) > 50:
                ref_p = doc.paragraphs[i]
                break
                
        style = ref_p.style
        alignment = ref_p.alignment
        font_name = "Verdana"
        font_size = None
        if ref_p.runs:
            if ref_p.runs[0].font.name:
                font_name = ref_p.runs[0].font.name
            if ref_p.runs[0].font.size:
                font_size = ref_p.runs[0].font.size
        
        # Delete old letter body
        paragraphs_to_delete = doc.paragraphs[start_idx:]
        for p in paragraphs_to_delete:
            delete_paragraph(p)
            
        # Append new tailored letter body
        for text_block in body_text.split('\n'):
            text_block = text_block.strip()
            if not text_block:
                continue
            new_p = doc.add_paragraph(style=style)
            if alignment is not None:
                new_p.alignment = alignment
            run = new_p.add_run(text_block)
            run.font.name = font_name
            if font_size:
                run.font.size = font_size

    doc.save(new_cl_path)

def convert_to_pdf_libreoffice(docx_path):
    pdf_dir = os.path.dirname(docx_path)
    subprocess.run([
        '/Applications/LibreOffice.app/Contents/MacOS/soffice',
        '--headless',
        '--convert-to', 'pdf',
        '--outdir', pdf_dir,
        docx_path
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

async def run_generator():
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM jobs WHERE status = "to_apply"')
    jobs = cursor.fetchall()
    
    if not jobs:
        print("No jobs ready for document generation.")
        return
        
    groq_client = get_groq_client()
    gemini_client = get_gemini_client()
    
    if not groq_client and not gemini_client:
        print("Error: You must set either GROQ_API_KEY or GEMINI_API_KEY in your .env file.")
        return
    cv_text = extract_text(CV_PATH)
    
    print("Loading Career Dossier...")
    try:
        with open(DOSSIER_PATH, 'r', encoding='utf-8') as f:
            dossier_text = f.read()
    except Exception as e:
        print(f"Error reading Career Dossier: {e}")
        return
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    total_jobs = len(jobs)
    for idx, job in enumerate(jobs, 1):
        job_id, title, company, location, description, link, score, reasoning, status, created_at, is_promoted, issue_number, issue_url, estimated_salary, is_recruiter, hiring_manager_name, jd_language = job
        print(f"[{idx}/{total_jobs}] Generating documents for {company}: {title}...")
        try:
            texts = generate_tailored_texts(groq_client, gemini_client, job, cv_text, dossier_text)
        except Exception as e:
            print(f"Error generating texts for job {job_id}: {e}")
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
        adapt_cv(CV_PATH, new_cv_docx, texts)
        adapt_cl(CL_PATH, new_cl_docx, texts['cover_letter_body'], company, location, hiring_manager_name, jd_language)
        
        # Save MD format
        with open(new_cv_md, 'w') as f:
            f.write(extract_text(new_cv_docx))
        with open(new_cl_md, 'w') as f:
            f.write(extract_text(new_cl_docx))
        
        # Convert to PDF
        print(f"Converting DOCX to PDF for {company}...")
        try:
            convert_to_pdf_libreoffice(new_cv_docx)
            convert_to_pdf_libreoffice(new_cl_docx)
        except Exception as e:
            print(f"PDF conversion failed: {e}")
            
        # Save Job Description as PDF using Playwright
        print(f"Saving JD as PDF for {company}...")
        pdf_dir = '/path/to/cvs/jobs'
        os.makedirs(pdf_dir, exist_ok=True)
        safe_title = "".join(x for x in title if x.isalnum() or x in " -_")
        jd_pdf_filename = f"{job_id}_{safe_company}_{safe_title}.pdf"
        jd_pdf_path = os.path.join(pdf_dir, jd_pdf_filename)
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                html_content = f"<h1>{title} at {company}</h1><p>{location}</p><hr><pre style='white-space: pre-wrap;'>{description}</pre>"
                await page.set_content(html_content)
                await page.pdf(path=jd_pdf_path)
                await browser.close()
        except Exception as pdf_err:
            print(f"Failed to generate JD PDF for {job_id}: {pdf_err}")
            
        # Update status
        cursor.execute('UPDATE jobs SET status = "generated" WHERE job_id = ?', (job_id,))
        conn.commit()
        print(f"Finished generating assets for {company}.")
        
    conn.close()

if __name__ == '__main__':
    run_generator()
