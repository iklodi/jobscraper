import db
import os
import json
import datetime
from docx import Document
from docx.shared import Pt
import subprocess
import sys
from groq import Groq
from google import genai
from google.genai import types
import time
import progress_tracker
import infomaniak
from evaluate import GEMINI_MODELS, GROQ_MODELS, any_model_configured
from playwright.async_api import async_playwright
import PyPDF2

CVS_DIR = os.environ.get('CVS_DIR', 'cvs')

def get_pdf_page_count(pdf_path):
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            return len(reader.pages)
    except Exception as e:
        print(f"Error reading PDF page count for {pdf_path}: {e}")
        return None

def load_prompt(filename):
    path = os.path.join(os.path.dirname(__file__), 'prompts', filename)
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

# Models
CV_TEMPLATE_NAME = os.environ.get('CV_TEMPLATE_NAME', 'Base_CV_Template.docx')
DOSSIER_NAME = os.environ.get('DOSSIER_NAME', 'Career_Dossier.md')
CL_TEMPLATE_NAME = os.environ.get('CL_TEMPLATE_NAME', 'Base_CL_Template.docx')

CV_PATH = os.path.join(CVS_DIR, 'docs', CV_TEMPLATE_NAME)
DOSSIER_PATH = os.path.join(CVS_DIR, 'docs', DOSSIER_NAME)
CL_PATH = os.path.join(CVS_DIR, 'docs', CL_TEMPLATE_NAME)
OUTPUT_DIR = os.path.join(CVS_DIR, 'applications')
# Fallback cascades are shared with evaluate.py. This file used to pin one model
# of each, and the Groq one was retired, so generation had no working fallback.

# Infomaniak only accepts response_format 'json_schema', so Task 1-5 of the
# generator prompt are restated here as a shape.
GEN_SCHEMA = {
    'type': 'object',
    'properties': {
        'cv_summary': {'type': 'string'},
        'cv_replacements': {
            'type': 'array',
            'items': {'type': 'object',
                      'properties': {'old': {'type': 'string'},
                                     'new': {'type': 'string'}},
                      'required': ['old', 'new']},
        },
        'cv_removals': {'type': 'array', 'items': {'type': 'string'}},
        'cover_letter_body': {'type': 'string'},
        'company_hq': {'type': 'string'},
        'display_company': {'type': 'string'},
    },
    'required': ['cv_summary', 'cv_replacements', 'cv_removals',
                 'cover_letter_body', 'company_hq', 'display_company'],
}


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

def generate_tailored_texts(groq_client, gemini_client, job, cv_text, dossier_text, custom_instructions=None, cl_language=None):
    job_id, title, company, location, description, link, score, reasoning, status, created_at, is_promoted, estimated_salary, is_recruiter, hiring_manager_name, jd_language, application_notes = job
    
    address_name = hiring_manager_name.split()[0] if hiring_manager_name else "the hiring manager"
    if cl_language:
        language_instruction = f"- Language Instruction: The user asked for the cover letter in {cl_language}. YOU MUST WRITE THE COVER LETTER ENTIRELY IN {cl_language}, the greeting and the closing formula included, whatever language the job description is in."
    else:
        language_instruction = f"- Language Instruction: The job description is in {jd_language}. Unless the user's custom instructions ask for another language, YOU MUST WRITE THE COVER LETTER ENTIRELY IN {jd_language}, the greeting and the closing formula included (never \"Dear\" or \"Sincerely\" in a letter that is not in English)." if jd_language else ""
    
    candidate_name = os.environ.get('CANDIDATE_NAME', 'Jane Doe')
    
    custom_block = f"\nCRITICAL CUSTOM INSTRUCTIONS FROM THE USER (these override any conflicting rules below, including Task 5):\n{custom_instructions}\n" if custom_instructions else ""

    prompt_template = load_prompt('generator_prompt.txt')
    prompt = prompt_template.replace('{custom_instructions}', custom_block) \
                            .replace('{title}', title) \
                            .replace('{company}', company) \
                            .replace('{location}', location or '') \
                            .replace('{description}', description) \
                            .replace('{cv_text}', cv_text) \
                            .replace('{dossier_text}', dossier_text) \
                            .replace('{address_name}', address_name) \
                            .replace('{candidate_name}', candidate_name) \
                            .replace('{language_instruction}', language_instruction)
        
    max_retries = 5
    for attempt in range(max_retries):
        result = None
        error_msg = ""

        # Infomaniak first when configured; unset, chat_json returns None
        # immediately and the Gemini/Groq path below is unchanged.
        result = infomaniak.chat_json(prompt, GEN_SCHEMA)

        if not result and gemini_client:
            for model_name in GEMINI_MODELS:
                try:
                    response = gemini_client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                        ),
                    )
                    result = json.loads(response.text)
                    break
                except Exception as e:
                    error_msg += f"Gemini {model_name}: {str(e)} | "

        if not result and groq_client:
            for model_name in GROQ_MODELS:
                try:
                    response = groq_client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"},
                        temperature=0.1
                    )
                    result = json.loads(response.choices[0].message.content)
                    break
                except Exception as e:
                    error_msg += f"Groq {model_name}: {str(e)} | "

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

TRANSLATE_SCHEMA = {
    'type': 'object',
    'properties': {'translations': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['translations'],
}
TRANSLATE_BATCH = 40


def is_english(language):
    return not language or language.strip().lower() in ('english', 'en')


def ask_json(prompt, schema):
    """One JSON answer from the first provider that gives one; None if none does."""
    result = infomaniak.chat_json(prompt, schema)
    if result:
        return result
    gemini_client = get_gemini_client()
    for model_name in (GEMINI_MODELS if gemini_client else []):
        try:
            response = gemini_client.models.generate_content(
                model=model_name, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"))
            return json.loads(response.text)
        except Exception as e:
            print(f"Gemini {model_name}: {e}")
    groq_client = get_groq_client()
    for model_name in (GROQ_MODELS if groq_client else []):
        try:
            response = groq_client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, temperature=0.1)
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"Groq {model_name}: {e}")
    return None


def translate_texts(items, language):
    """Translate each string into `language`; the list comes back the same length.

    Raises when a batch cannot be translated, so a half-translated document is
    never passed off as a translated one.
    """
    out = []
    for start in range(0, len(items), TRANSLATE_BATCH):
        batch = items[start:start + TRANSLATE_BATCH]
        prompt = (
            f"Translate each string in this JSON list into {language}. These are fragments of a CV "
            "or a cover letter, in order; a fragment may be a heading, a label or part of a sentence.\n"
            "- Return exactly one translation per input string, in the same order, as "
            '{"translations": [...]}.\n'
            "- Keep unchanged: personal names, company and organisation names, product and technology "
            "names, e-mail addresses, phone numbers, URLs, dates and numbers, and anything in square "
            "brackets such as [COMPANY].\n"
            "- Translate place names and country names into their usual form in the target language.\n"
            "- Use the vocabulary of a professional CV in that language. Keep punctuation, a trailing "
            "colon and capitalisation style. A string that needs no translation comes back as it is.\n\n"
            + json.dumps(batch, ensure_ascii=False))
        result = ask_json(prompt, TRANSLATE_SCHEMA)
        translated = (result or {}).get('translations')
        if not isinstance(translated, list) or len(translated) != len(batch):
            raise RuntimeError(f"Translation into {language} failed")
        out.extend(str(t) for t in translated)
    return out


def _run_format(run):
    f = run.font
    return (run.bold, run.italic, run.underline, f.size, f.name,
            str(f.color.rgb) if f.color is not None and f.color.type is not None else None)


def _segments(paragraph):
    """A paragraph's runs, grouped into stretches of the same formatting."""
    from docx.oxml.ns import qn
    embedded = {qn('w:drawing'), qn('w:pict'), qn('w:object'),
                '{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent'}
    groups = []
    for run in paragraph.runs:
        # A run holding a picture or a text box is left alone: setting its
        # text would delete what it holds. The box's own paragraphs are
        # translated separately.
        if any(child.tag in embedded for child in run._r):
            groups.append(None)
            continue
        if groups and groups[-1] is not None and _run_format(run) == _run_format(groups[-1][-1]):
            groups[-1].append(run)
        else:
            groups.append([run])
    return [g for g in groups if g]


def all_paragraphs(doc):
    """Body, text-box, header and footer paragraphs: everything a reader sees."""
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    paragraphs = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    # Text boxes (the letter's sender band is one); Word keeps a fallback copy too.
    for box in doc.element.body.iter(qn('w:txbxContent')):
        paragraphs.extend(Paragraph(p, doc) for p in box.iter(qn('w:p')))
    for section in doc.sections:
        for part in (section.header, section.footer):
            paragraphs.extend(part.paragraphs)
    return paragraphs


def translate_paragraphs(paragraphs, language, extra=()):
    """Translate paragraphs in place, keeping each stretch of formatting where it was.

    `extra` strings ride along in the same request; their translations are returned.
    """
    import re
    targets = []
    for p in paragraphs:
        for group in _segments(p):
            text = ''.join(r.text for r in group)
            if re.search(r'[^\W\d_]{2,}', text):
                targets.append((group, text))
    extra = list(extra)
    if not targets and not extra:
        return []
    translated = translate_texts([t.strip() for _, t in targets] + extra, language)
    for (group, text), new in zip(targets, translated):
        lead = text[:len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        group[0].text = lead + new.strip() + trail
        for r in group[1:]:
            r.text = ''
    return translated[len(targets):]


def translate_docx(path, language):
    """Translate a finished document into `language`, formatting kept."""
    doc = Document(path)
    translate_paragraphs(all_paragraphs(doc), language)
    doc.save(path)


def sanitize_generated_texts(texts, company, location, jd_language, requested=None):
    """Deterministic guards for AI output fields that end up verbatim in the documents."""
    import re

    # Header company: the posting company beats a generic punt. If it's an agency,
    # "<Agency> Hiring Team" is still a correct addressee.
    dc = (texts.get('display_company') or '').strip()
    if not dc or dc.lower() in ('hiring team', 'unknown', 'n/a', 'none'):
        texts['display_company'] = company

    # HQ: drop 'Unknown'-style fragments; fall back to the job's location
    hq = (texts.get('company_hq') or '').strip()
    parts = [p.strip() for p in hq.split(',')
             if p.strip() and p.strip().lower() not in ('unknown', 'n/a', 'none')]
    texts['company_hq'] = ', '.join(parts) if parts else re.sub(r'\s*\([^)]*\)', '', location or '').strip()

    # The letter's own language decides, not the advert's: the user may have
    # asked for another one in the custom instructions.
    # A language picked on the dashboard is certain; otherwise read it off the letter.
    jd_language = requested or letter_language(texts.get('cover_letter_body') or '') or jd_language

    # Greeting: the first line must be a well-formed salutation, never a bare name
    lines = (texts.get('cover_letter_body') or '').split('\n')
    starters = ('dear', 'hi', 'hello', 'greetings', 'bonjour', 'cher', 'chère',
                'madame', 'monsieur', 'sehr geehrte', 'guten tag', 'hallo', 'liebe')
    for i, line in enumerate(lines):
        first = line.strip()
        if not first:
            continue
        if any(first.lower().startswith(s) for s in starters):
            if first[-1] not in ',:':
                lines[i] = f"{first},"
        elif len(first) <= 40 and len(first.split()) <= 4 and first[-1] not in '.!?:':
            jl = (jd_language or '').lower()
            prefix = 'Bonjour' if 'french' in jl else 'Guten Tag' if 'german' in jl else 'Dear'
            lines[i] = f"{prefix} {first.rstrip(',')},"
        break
    texts['cover_letter_body'] = localise_letter_formulas('\n'.join(lines), jd_language)
    return jd_language


# The model sometimes writes a French or German letter and still opens it with
# "Dear" and signs off "Sincerely". Swiss usage for the closings.
LETTER_FORMULAS = {
    'french': {'dear': 'Bonjour', 'team': 'Madame, Monsieur,', 'closing': 'Meilleures salutations,'},
    'german': {'dear': 'Guten Tag', 'team': 'Sehr geehrte Damen und Herren,', 'closing': 'Freundliche Grüsse,'},
    'italian': {'dear': 'Gentile', 'team': 'Gentili Signore e Signori,', 'closing': 'Cordiali saluti,'},
}
ENGLISH_CLOSINGS = ('sincerely', 'yours sincerely', 'sincerely yours', 'yours faithfully',
                    'best regards', 'kind regards', 'warm regards', 'warmest regards',
                    'regards', 'best', 'with best regards', 'with kind regards',
                    'best wishes', 'respectfully', 'thank you')


LANGUAGE_MARKERS = {
    'english': {'the', 'and', 'of', 'to', 'in', 'my', 'with', 'your', 'have', 'for', 'is', 'that'},
    'french': {'le', 'la', 'les', 'et', 'de', 'des', 'je', 'mon', 'ma', 'mes', 'vos', 'votre', 'pour', 'une', 'est', 'dans'},
    'german': {'der', 'die', 'das', 'und', 'ich', 'mit', 'für', 'ihre', 'ihr', 'meine', 'ist', 'eine', 'nicht', 'bei'},
    'italian': {'il', 'gli', 'e', 'di', 'della', 'che', 'mia', 'mio', 'per', 'con', 'sono', 'una', 'nel', 'vostra'},
}


def letter_language(body):
    """The language a letter is written in, from its common words; None when unclear."""
    import re
    words = re.findall(r"[a-zà-ÿß]+", body.lower())
    if len(words) < 20:
        return None
    counts = {lang: sum(w in marks for w in words) for lang, marks in LANGUAGE_MARKERS.items()}
    lang, best = max(counts.items(), key=lambda kv: kv[1])
    return lang.capitalize() if best >= 5 else None


def localise_letter_formulas(body, jd_language):
    """Translate an English greeting and closing in a letter written in another language."""
    import re
    jl = (jd_language or '').lower()
    forms = next((f for lang, f in LETTER_FORMULAS.items() if lang in jl), None)
    if not forms:
        return body
    lines = body.split('\n')
    for i, line in enumerate(lines):
        first = line.strip()
        if not first:
            continue
        m = re.match(r'(?i)dear\s+(.+?)[,:]?$', first)
        if m:
            name = m.group(1).strip()
            generic = re.search(r'(?i)\b(hiring|team|manager|sir|madam|recruit|whom)', name)
            lines[i] = forms['team'] if generic else f"{forms['dear']} {name},"
        break
    for i in range(len(lines) - 1, max(len(lines) - 5, -1), -1):
        last = lines[i].strip().rstrip(',').strip().lower()
        if last in ENGLISH_CLOSINGS:
            lines[i] = forms['closing']
            break
    return '\n'.join(lines)

def adapt_cl(base_cl_path, new_cl_path, body_text, company, display_company, location, hiring_manager_name, jd_language):
    doc = Document(base_cl_path)
    
    start_idx = -1
    for i, p in enumerate(doc.paragraphs):
        if p.text.strip().lower().startswith("dear "):
            start_idx = i
            break
            
    if start_idx != -1:
        import re
        company_location_clean = re.sub(r'\s*\([^)]*\)', '', location or '').strip()

        # A letter in another language gets its header in that language too:
        # "Hiring Team", the country names, the sender's own address. Done on
        # the template, so the company's name never goes through a translator.
        if not is_english(jd_language):
            from docx.oxml.ns import qn
            header = doc.paragraphs[:start_idx]
            boxes = [p for p in all_paragraphs(doc) if p._p.getparent().tag == qn('w:txbxContent')]
            try:
                extra = [company_location_clean] if company_location_clean else []
                done = translate_paragraphs(header + boxes, jd_language, extra)
                if extra:
                    company_location_clean = done[0]
            except Exception as e:
                print(f"Letter header left in English: {e}")
        
        # Replace the hard-coded addressee info in the header (paragraphs before "Dear ")
        today_str = datetime.datetime.now().strftime("%d.%m.%Y")
        for p in doc.paragraphs[:start_idx]:
            if jd_language and "french" in jd_language.lower():
                # Localise the sender's own city line for French-language letters.
                home_city = os.environ.get('HOME_CITY', '')
                home_city_fr = os.environ.get('HOME_CITY_FR', '')
                if home_city and home_city_fr:
                    replace_text_in_paragraph(p, home_city, home_city_fr)
                
            if display_company and display_company.lower() != "hiring team":
                replace_text_in_paragraph(p, "[COMPANY]", display_company)
            else:
                # Template already reads "[COMPANY] Hiring Team" — drop the
                # placeholder (and its trailing space) instead of injecting
                # another "Hiring Team" in front of it.
                replace_text_in_paragraph(p, "[COMPANY] ", "")
                replace_text_in_paragraph(p, "[COMPANY]", "")
                
            replace_text_in_paragraph(p, "[DATE]", today_str)
            
            if location:
                replace_text_in_paragraph(p, "[LOCATION]", company_location_clean)
            else:
                replace_text_in_paragraph(p, "\n[LOCATION]", "")
                replace_text_in_paragraph(p, "[LOCATION]\n", "")
                replace_text_in_paragraph(p, "[LOCATION]", "")

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

# LibreOffice's last-resort fonts. Seeing one of these in an exported PDF
# means it could not find the font the document asked for and fell back,
# which silently changes the layout - the failure this guards against.
FALLBACK_FONT_MARKERS = ('LinuxLibertine', 'FrankRuhlHofshi')


def soffice_env():
    """Environment for a headless LibreOffice run.

    On macOS the headless backend goes through fontconfig, which does not see
    the system fonts, so every Microsoft font gets substituted: Arial becomes
    Liberation Sans, and Verdana - which has no metric-compatible clone -
    becomes Linux Libertine, a serif. Forcing the native osx backend makes
    LibreOffice read the real fonts and the PDF match Word again.
    Override with SOFFICE_VCLPLUGIN; on Linux the default backend is correct,
    so nothing is set.
    """
    env = os.environ.copy()
    plugin = os.environ.get('SOFFICE_VCLPLUGIN',
                            'osx' if sys.platform == 'darwin' else '')
    if plugin:
        env['SAL_USE_VCLPLUGIN'] = plugin
    return env


def pdf_fallback_fonts(pdf_path):
    """Names of substituted fonts in a PDF, empty when the real ones were used."""
    found = set()
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                fonts = page['/Resources'].get_object().get('/Font')
                if not fonts:
                    continue
                for ref in fonts.get_object().values():
                    name = str(ref.get_object().get('/BaseFont') or '')
                    if any(m in name for m in FALLBACK_FONT_MARKERS):
                        found.add(name.split('+')[-1])
    except Exception:
        pass
    return sorted(found)


def convert_to_pdf_libreoffice(docx_path):
    pdf_dir = os.path.dirname(docx_path)
    soffice_path = os.environ.get('SOFFICE_PATH', '/Applications/LibreOffice.app/Contents/MacOS/soffice')
    subprocess.run([
        soffice_path,
        '--headless',
        '--convert-to', 'pdf',
        '--outdir', pdf_dir,
        docx_path
    ], check=True, env=soffice_env(),
       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    pdf_path = os.path.splitext(docx_path)[0] + '.pdf'
    substituted = pdf_fallback_fonts(pdf_path)
    if substituted:
        print(f"  -> WARNING: {os.path.basename(pdf_path)} fell back to "
              f"{', '.join(substituted)}. The PDF will not match the Word layout. "
              f"LibreOffice cannot see the template's fonts on this machine.")
    return pdf_path

async def generate_for_job(job_id, custom_instructions=None, final_status='generated',
                           cv_language=None, cl_language=None):
    """Write the job's CV and cover letter.

    By default the CV is in English and the letter in the advert's language;
    `cv_language` / `cl_language` ask for another one (the dashboard's
    Regenerate dropdowns).
    """
    import db
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('''SELECT job_id, title, company, location, description, link, score, reasoning, status,
                             created_at, is_promoted, estimated_salary, is_recruiter, hiring_manager_name,
                             jd_language, application_notes
                      FROM jobs WHERE job_id = ?''', (job_id,))
    job = cursor.fetchone()
    # A failed regeneration of an approved or applied job leaves it where it was;
    # Failed is for jobs that never got as far as a person's decision.
    failure_status = final_status if final_status in db.PROTECTED_STATUSES else 'failed'
    if not job:
        print(f"Job {job_id} not found.")
        conn.close()
        return False

    # No addressee yet? A LinkedIn posting often names its poster in the
    # "Meet the hiring team" card, which the description does not include.
    # One logged-out look at the public page, in either mode.
    if not job[13] and str(job_id).isdigit():
        import public_jobs
        poster = await public_jobs.fetch_poster(job_id)
        if poster:
            print(f"  -> Job poster: {poster}")
            job = tuple(job[:13]) + (poster,) + tuple(job[14:])

    groq_client = get_groq_client()
    gemini_client = get_gemini_client()
    
    if not any_model_configured(groq_client, gemini_client):
        print("Error: no model configured - set INFOMANIAK_API_TOKEN, GEMINI_API_KEY or GROQ_API_KEY.")
        conn.close()
        return False
        
    cv_text = extract_text(CV_PATH)
    try:
        with open(DOSSIER_PATH, 'r', encoding='utf-8') as f:
            dossier_text = f.read()
    except Exception as e:
        print(f"Error reading Career Dossier: {e}")
        conn.close()
        return False
        
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    job_id, title, company, location, description, link, score, reasoning, status, created_at, is_promoted, estimated_salary, is_recruiter, hiring_manager_name, jd_language, application_notes = job
    print(f"Generating documents for {company}: {title}...")
    
    baseline_pdf_path = CV_PATH.replace('.docx', '.pdf')
    if not os.path.exists(baseline_pdf_path):
        print("Converting baseline CV to PDF for page counting...")
        convert_to_pdf_libreoffice(CV_PATH)
    baseline_page_count = get_pdf_page_count(baseline_pdf_path) or 3
    
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            texts = generate_tailored_texts(groq_client, gemini_client, job, cv_text, dossier_text,
                                            custom_instructions, cl_language)
            if not texts:
                print(f"AI returned empty result for job {job_id}")
                cursor.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (failure_status, job_id))
                conn.commit()
                conn.close()
                return False
        except Exception as e:
            print(f"Error generating texts for job {job_id}: {e}")
            cursor.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (failure_status, job_id))
            conn.commit()
            conn.close()
            return False
            
        today = datetime.datetime.now().strftime("%Y%m%d")
        safe_company = "".join(x for x in company if x.isalnum())
        job_dir = os.path.join(OUTPUT_DIR, f"{today}_{safe_company}_{job_id}")
        
        import shutil
        if os.path.exists(OUTPUT_DIR):
            for d in os.listdir(OUTPUT_DIR):
                if d.endswith(f"_{job_id}"):
                    try:
                        shutil.rmtree(os.path.join(OUTPUT_DIR, d))
                    except Exception as ex:
                        print(f"Could not delete old directory {d}: {ex}")
                        
        os.makedirs(job_dir, exist_ok=True)
        
        new_cv_docx = os.path.join(job_dir, f"{today}_{safe_company}_CV.docx")
        new_cl_docx = os.path.join(job_dir, f"{today}_{safe_company}_CoverLetter.docx")
        new_cv_md = os.path.join(job_dir, f"{today}_{safe_company}_CV.md")
        new_cl_md = os.path.join(job_dir, f"{today}_{safe_company}_CoverLetter.md")
        
        # Adapt DOCX files
        adapt_cv(CV_PATH, new_cv_docx, texts)
        if not is_english(cv_language):
            print(f"Translating the CV into {cv_language}...")
            try:
                translate_docx(new_cv_docx, cv_language)
            except Exception as e:
                print(f"CV left in English: {e}")
                cursor.execute("""INSERT INTO job_history (job_id, event_type, note, created_at)
                                  VALUES (?, 'note_added', ?, ?)""",
                               (job_id, f"The CV could not be translated into {cv_language}; it is in English.",
                                datetime.datetime.now()))
                conn.commit()
        letter_lang = sanitize_generated_texts(texts, company, location, jd_language, cl_language)
        company_hq = texts['company_hq']
        display_company = texts['display_company']
        adapt_cl(CL_PATH, new_cl_docx, texts['cover_letter_body'], company, display_company, company_hq, hiring_manager_name, letter_lang)
        
        # Save MD format
        with open(new_cv_md, 'w') as f:
            f.write(extract_text(new_cv_docx))
        with open(new_cl_md, 'w') as f:
            f.write(extract_text(new_cl_docx))
        
        # Convert to PDF
        print(f"Converting DOCX to PDF for {company} (Attempt {attempt})...")
        try:
            convert_to_pdf_libreoffice(new_cv_docx)
            convert_to_pdf_libreoffice(new_cl_docx)
        except Exception as e:
            print(f"PDF conversion failed: {e}")
            
        new_cv_pdf = new_cv_docx.replace('.docx', '.pdf')
        new_page_count = get_pdf_page_count(new_cv_pdf)
        
        if new_page_count and new_page_count > baseline_page_count:
            if attempt < max_attempts:
                print(f"Generated CV is {new_page_count} pages (baseline is {baseline_page_count}). Retrying...")
                custom_instructions = (custom_instructions or "") + "\n\nCRITICAL: The previous generation was too long. You MUST shorten your bullet point replacements slightly to ensure the CV fits within its original page limit."
                continue
            else:
                print(f"Generated CV is still too long after {max_attempts} attempts. Keeping it.")
                break
        else:
            break
            
        
    # Save Job Description as PDF using Playwright
    print(f"Saving JD as PDF for {company}...")
    pdf_dir = os.path.join(CVS_DIR, 'jobs')
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
    # Generation triggered from the Approve button must leave the job in the
    # Approved column, not bounce it back to To Do.
    cursor.execute('UPDATE jobs SET status = ? WHERE job_id = ?', (final_status, job_id))
    conn.commit()
    conn.close()
    print(f"Finished generating assets for {company}.")
    return True

async def run_generator():
    import db
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT job_id, title, company, description, reasoning, location FROM jobs WHERE status = "to_apply"')
    unprocessed_jobs = cursor.fetchall()
    conn.close()
    
    if not unprocessed_jobs:
        print("No jobs ready for document generation.")
        return
        
    total_jobs = len(unprocessed_jobs)
    for idx, job in enumerate(unprocessed_jobs, 1):
        if progress_tracker.is_stop_requested():
            print("Stop requested during generation!")
            break
        progress_tracker.set_status("Generating Assets", idx, total_jobs)
        
        job_id = job[0]
        print(f"[{idx}/{total_jobs}] Processing job {job_id}...")
        await generate_for_job(job_id)

if __name__ == '__main__':
    import asyncio
    asyncio.run(run_generator())
