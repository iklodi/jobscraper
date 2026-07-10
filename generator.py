import db
import os
import json
import datetime
from docx import Document
from docx.shared import Pt
import subprocess
from google import genai
from google.genai import types
from playwright.async_api import async_playwright

CV_PATH = '/path/to/cvs/docs/Base_CV_Template.docx'
CL_PATH = '/path/to/cvs/docs/Base_CL_Template.docx' # Using EY as a generic baseline for now
OUTPUT_DIR = '/path/to/cvs/applications'
MODEL_NAME = 'gemini-2.5-flash'

def get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Please set the GEMINI_API_KEY environment variable.")
    return genai.Client(api_key=api_key)

def generate_tailored_texts(client, job, cv_text):
    job_id, title, company, location, description, link, score, reasoning, status, created_at, is_promoted, issue_number, issue_url = job
    
    prompt = f"""
    You are an expert career advisor. I am applying for the '{title}' role at '{company}'.
    Here is the job description:
    {description}
    
    Here is my current CV:
    {cv_text}
    
    Task 1: Write a tailored 3-4 sentence professional summary for my CV that specifically highlights my fit for this role.
    Task 2: Write a tailored, compelling cover letter (around 250 words) addressed to the hiring manager at {company}.
    
    CRITICAL TONE INSTRUCTIONS:
    - Write in a highly professional, direct, and human tone.
    - DO NOT use long dashes (em-dashes "—" or en-dashes "–"). Use commas, semicolons, or regular parentheses instead.
    - STRICTLY AVOID typical AI clichés and buzzwords (e.g., "delve", "tapestry", "testament", "beacon", "catalyst", "unleash", "elevate", "thrilled to apply", "embark", "spearhead", "pivotal").
    - Keep sentences concise, factual, and impactful. Do not overcomplicate the sentence structure.
    
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
        result = json.loads(response.text)
        
        # Aggressive post-processing fallback just in case the AI ignores the prompt
        result['cv_summary'] = result['cv_summary'].replace('—', ' - ').replace('–', ' - ')
        result['cover_letter_body'] = result['cover_letter_body'].replace('—', ' - ').replace('–', ' - ')
        
        return result
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
            if p.runs:
                p.runs[0].text = summary_text
                for r in p.runs[1:]:
                    r.text = ""
            else:
                p.text = summary_text
            replaced = True
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

def adapt_cl(base_cl_path, new_cl_path, body_text, company, location):
    doc = Document(base_cl_path)
    
    start_idx = -1
    for i, p in enumerate(doc.paragraphs):
        if p.text.strip().lower().startswith("dear "):
            start_idx = i
            break
            
    if start_idx != -1:
        # Replace the hard-coded addressee info in the header (paragraphs before "Dear ")
        today_str = datetime.datetime.now().strftime("%d.%m.%Y")
        for p in doc.paragraphs[:start_idx]:
            replace_text_in_paragraph(p, "Ernst & Young Hiring Team", f"{company} Hiring Team")
            replace_text_in_paragraph(p, "Ernst & Young", company)
            replace_text_in_paragraph(p, "Zurich, Switzerland", location)
            replace_text_in_paragraph(p, "10.04.2026", today_str)
            replace_text_in_paragraph(p, "[COMPANY]", company)
            replace_text_in_paragraph(p, "[LOCATION]", location)
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
                doc.add_paragraph()
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
        
    client = get_gemini_client()
    cv_text = extract_text(CV_PATH)
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    for job in jobs:
        job_id, title, company, location, description, link, score, reasoning, status, created_at, is_promoted, issue_number, issue_url = job
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
        adapt_cl(CL_PATH, new_cl_docx, texts['cover_letter_body'], company, location)
        
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
