"""Semi-automated application filler.

Picks up approved jobs, opens the employer's apply link, fills the form from the
profile answer bank, attaches the generated CV/cover letter, and parks the job for
human review. It never submits, never creates accounts, and never invents an answer.
"""
import asyncio
import json
import os
import re

import yaml
from dotenv import load_dotenv
from google import genai
from google.genai import types
from playwright.async_api import async_playwright

import db
import notifier
import progress_tracker

load_dotenv(override=True)

CHROME_PROFILE_DIR = './chrome_profile'
CVS_DIR = os.environ.get('CVS_DIR', 'cvs')
PROFILE_PATH = os.path.join(CVS_DIR, 'profile.yaml')
OUTPUT_DIR = os.path.join(CVS_DIR, 'applications')

GEMINI_MODELS = [
    'gemini-3.5-flash',
    'gemini-3-flash-preview',
    'gemini-3.1-flash-lite',
]

# Pages that mean "we cannot proceed without a human"
ACCOUNT_WALL_PATTERNS = re.compile(
    r'create an account|create account|sign in to apply|register to apply|'
    r'set a password|password requirements|confirm password',
    re.I,
)
CAPTCHA_PATTERNS = re.compile(r'recaptcha|hcaptcha|captcha|are you a robot|cloudflare', re.I)

# Field inventory: tag every visible control so we can address it later by index.
COLLECT_FIELDS_JS = """
() => {
    const out = [];
    let idx = 0;
    const labelFor = (el) => {
        if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
        const labelledby = el.getAttribute('aria-labelledby');
        if (labelledby) {
            const l = document.getElementById(labelledby);
            if (l) return l.innerText.trim();
        }
        const wrapper = el.closest('div,fieldset,section,li');
        if (wrapper) {
            const t = wrapper.innerText.trim().split('\\n')[0];
            if (t && t.length < 200) return t;
        }
        return '';
    };
    document.querySelectorAll('input, select, textarea').forEach((el) => {
        if (el.type === 'hidden') return;
        const rect = el.getBoundingClientRect();
        const visible = rect.width > 0 && rect.height > 0;
        if (!visible && el.type !== 'file') return;
        el.setAttribute('data-jsapply', String(idx));
        const entry = {
            idx: idx,
            tag: el.tagName.toLowerCase(),
            type: (el.type || '').toLowerCase(),
            name: el.name || '',
            id: el.id || '',
            label: labelFor(el),
            placeholder: el.placeholder || '',
            required: el.required || el.getAttribute('aria-required') === 'true',
            value: el.type === 'file' ? '' : (el.value || ''),
        };
        if (el.tagName.toLowerCase() === 'select') {
            entry.options = Array.from(el.options).map((o) => o.text.trim()).filter(Boolean);
        }
        out.push(entry);
        idx += 1;
    });
    return {
        fields: out,
        iframes: Array.from(document.querySelectorAll('iframe')).map((f) => f.src).filter(Boolean),
        text: document.body ? document.body.innerText.slice(0, 6000) : '',
    };
}
"""

MAPPING_PROMPT = """You are filling in a job application form on behalf of a candidate.

CANDIDATE PROFILE (the ONLY source of truth for answers):
{profile}

JOB: {title} at {company}
JOB DESCRIPTION (for context on role-specific questions):
{description}

FORM FIELDS DETECTED ON THE PAGE (JSON):
{fields}

VISIBLE PAGE TEXT (for context, truncated):
{page_text}

For every field, decide what to do. Output valid JSON only:
{{
  "actions": [
    {{"idx": 0, "action": "fill|select|check|skip", "value": "the exact value to enter"}}
  ],
  "unanswered": [
    {{"idx": 3, "question": "the question as shown", "reason": "why the profile does not answer it"}}
  ],
  "page_kind": "application_form|login_or_register|job_description_only|confirmation|other",
  "notes": "anything the human should know"
}}

ABSOLUTE RULES:
- NEVER invent, guess, or approximate an answer. If the profile does not contain the
  information, put the field in "unanswered" and use action "skip". A wrong answer on a
  job application is worse than an unfilled field.
- For "select" actions the value MUST be one of the field's options, copied verbatim.
- For "check" (checkbox/radio) use value "true" or "false". Only tick consent or
  affirmation boxes when the profile clearly supports it; never tick anything that
  asserts a fact you cannot verify from the profile.
- Skip file inputs entirely (action "skip") - uploads are handled separately.
- Skip password fields and anything that is part of account creation; set page_kind to
  "login_or_register" if the page is primarily a sign-up or sign-in form.
- Free-text questions (motivation, "why this company") may be composed from profile
  facts and the job description, but must not state anything the profile contradicts.
- Prefer the profile's exact phrasing for salary, notice period, and work authorization.
"""


def load_profile():
    if not os.path.exists(PROFILE_PATH):
        raise FileNotFoundError(
            f"No profile answer bank at {PROFILE_PATH}. Copy profile.example.yaml there and fill it in."
        )
    with open(PROFILE_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def get_gemini_client():
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        return None
    return genai.Client(api_key=api_key)


def application_files(job_id):
    """Return (cv_pdf, cl_pdf, folder) for a job, or (None, None, None)."""
    if not os.path.isdir(OUTPUT_DIR):
        return None, None, None
    for d in os.listdir(OUTPUT_DIR):
        if d.endswith(f"_{job_id}"):
            folder = os.path.join(OUTPUT_DIR, d)
            cv = cl = None
            for f in os.listdir(folder):
                if not f.endswith('.pdf'):
                    continue
                if 'CoverLetter' in f:
                    cl = os.path.join(folder, f)
                elif 'CV' in f:
                    cv = os.path.join(folder, f)
            return cv, cl, folder
    return None, None, None


def ask_gemini(client, prompt):
    """Run the prompt through the Gemini cascade; returns parsed JSON or None."""
    for model_name in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type='application/json'),
            )
            text = response.text.strip()
            start, end = text.find('{'), text.rfind('}')
            if start != -1 and end != -1:
                text = text[start:end + 1]
            return json.loads(text)
        except Exception as e:
            print(f"  -> Gemini error on {model_name}: {e}")
            continue
    return None


async def find_apply_url(page, linkedin_url):
    """Open the LinkedIn posting and follow its apply button to the employer's form."""
    await page.goto(linkedin_url, timeout=60000)
    await page.wait_for_timeout(5000)

    # LinkedIn ships obfuscated class names and renders the apply control as a
    # button, a link, or a div with role=button depending on the posting, so go
    # through the accessibility tree rather than CSS.
    apply_re = re.compile(r'^\s*(easy apply|apply)\b', re.I)
    candidates = [
        page.get_by_role('button', name=apply_re),
        page.get_by_role('link', name=apply_re),
        page.locator('button.jobs-apply-button, a.jobs-apply-button'),
        page.locator('[class*="jobs-apply"] button, [class*="jobs-apply"] a'),
    ]
    for candidate in candidates:
        button = candidate.first
        try:
            await button.wait_for(state='visible', timeout=6000)
            await button.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            continue

        context = page.context
        try:
            async with context.expect_page(timeout=15000) as new_page_info:
                await button.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state('domcontentloaded', timeout=30000)
            return new_page, 'external'
        except Exception:
            # No new tab: either an in-page Easy Apply modal or a same-tab navigation.
            await page.wait_for_timeout(3000)
            return page, 'same_tab'

    # Nothing matched - leave a screenshot behind so the failure is diagnosable.
    try:
        await page.screenshot(path=os.path.join(OUTPUT_DIR, 'last_apply_lookup_failure.png'))
    except Exception:
        pass
    return None, None


async def upload_documents(page, fields, cv_path, cl_path):
    """Attach the generated PDFs to any file inputs on the page."""
    uploaded = []
    for field in fields:
        if field.get('type') != 'file':
            continue
        haystack = ' '.join(
            [field.get('label', ''), field.get('name', ''), field.get('id', '')]
        ).lower()
        target = None
        if re.search(r'cover|motivation|lettre|anschreiben', haystack):
            target = cl_path
        elif re.search(r'cv|resume|lebenslauf', haystack) or not uploaded:
            target = cv_path
        if not target or not os.path.exists(target):
            continue
        try:
            await page.locator(f'[data-jsapply="{field["idx"]}"]').set_input_files(target)
            uploaded.append(os.path.basename(target))
            await page.wait_for_timeout(1500)
        except Exception as e:
            print(f"  -> Upload failed for field {field['idx']}: {e}")
    return uploaded


async def tick_box(page, locator, field):
    """Tick a checkbox/radio, coping with inputs hidden behind styled labels."""
    try:
        await locator.check(timeout=4000)
        return
    except Exception:
        pass
    # Most design systems hide the real input and style a <label> on top of it.
    field_id = field.get('id') if field else None
    if field_id:
        label = page.locator(f'label[for="{field_id}"]').first
        try:
            await label.click(timeout=3000)
            if await locator.is_checked():
                return
        except Exception:
            pass
    try:
        await locator.click(force=True, timeout=3000)
        if await locator.is_checked():
            return
    except Exception:
        pass
    await locator.check(force=True, timeout=3000)


async def apply_actions(page, actions, fields=None):
    """Execute the model's fill plan; returns (filled_count, errors)."""
    by_idx = {f['idx']: f for f in (fields or [])}
    filled, errors = 0, []
    for action in actions:
        kind = action.get('action')
        if kind in (None, 'skip'):
            continue
        value = action.get('value', '')
        selector = f'[data-jsapply="{action.get("idx")}"]'
        locator = page.locator(selector)
        try:
            if kind == 'fill':
                await locator.fill(str(value), timeout=5000)
            elif kind == 'select':
                await locator.select_option(label=str(value), timeout=5000)
            elif kind == 'check':
                if str(value).lower() in ('true', 'yes', '1'):
                    await tick_box(page, locator, by_idx.get(action.get('idx')))
            else:
                continue
            filled += 1
            await page.wait_for_timeout(150)
        except Exception as e:
            errors.append(f"field {action.get('idx')}: {type(e).__name__}")
    return filled, errors


async def process_job(page, client, profile, job):
    """Fill one application. Returns (new_status, note)."""
    job_id, title, company, link, description = job

    cv_path, cl_path, folder = application_files(job_id)
    if not cv_path:
        return 'failed', 'No generated CV/cover letter found for this job - regenerate the assets first.'

    target, mode = await find_apply_url(page, link)
    if not target:
        return 'failed', 'Could not find an Apply button on the LinkedIn posting (listing may be closed).'

    apply_url = target.url
    body = await target.evaluate(COLLECT_FIELDS_JS)
    fields, page_text = body['fields'], body['text']

    if CAPTCHA_PATTERNS.search(page_text):
        return 'failed', f'Blocked by a CAPTCHA / bot check at {apply_url}. Needs a human, ideally over VNC.'

    if ACCOUNT_WALL_PATTERNS.search(page_text) or any(f.get('type') == 'password' for f in fields):
        return 'account_required', (
            f'The employer requires creating an account or signing in before the form can be '
            f'filled, so nothing was entered. Apply manually at: {apply_url}\n'
            f'Documents ready in: {folder}'
        )

    if not fields:
        iframe_note = ''
        if body.get('iframes'):
            iframe_note = f" The form is likely inside an iframe ({body['iframes'][0]})."
        return 'failed', f'No form fields detected at {apply_url}.{iframe_note}'

    plan = ask_gemini(client, MAPPING_PROMPT.format(
        profile=yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
        title=title, company=company,
        description=(description or '')[:4000],
        fields=json.dumps(fields, ensure_ascii=False)[:20000],
        page_text=page_text[:3000],
    ))
    if not plan:
        return 'failed', f'Could not map the form fields (all Gemini models failed) at {apply_url}.'

    if plan.get('page_kind') == 'login_or_register':
        return 'account_required', (
            f'The apply link leads to a sign-in / registration page. Apply manually at: {apply_url}\n'
            f'Documents ready in: {folder}'
        )

    filled, errors = await apply_actions(target, plan.get('actions', []), fields)
    uploaded = await upload_documents(target, fields, cv_path, cl_path)

    shot_path = os.path.join(folder, f'{job_id}_filled_form.png')
    try:
        await target.screenshot(path=shot_path, full_page=True)
    except Exception:
        shot_path = None

    unanswered = plan.get('unanswered') or []
    lines = [
        f'Form filled but NOT submitted. Review and submit here: {apply_url}',
        f'Filled {filled} field(s); uploaded: {", ".join(uploaded) if uploaded else "nothing"}.',
    ]
    if shot_path:
        lines.append(f'Screenshot of the filled form: {os.path.basename(shot_path)}')
    if errors:
        lines.append(f'Fields that would not accept input: {"; ".join(errors)}')
    if unanswered:
        lines.append('\nQUESTIONS THE PROFILE DOES NOT ANSWER (left blank):')
        for item in unanswered:
            lines.append(f'  - {item.get("question")}  ({item.get("reason")})')
        lines.append('Add these to profile.yaml so future applications answer them automatically.')
    if plan.get('notes'):
        lines.append(f'\nAgent notes: {plan["notes"]}')

    return 'ready_to_submit', '\n'.join(lines)


async def run_applications(limit=5, job_ids=None):
    """Fill applications for approved jobs. Returns a per-job result list."""
    db.init_db()
    profile = load_profile()
    client = get_gemini_client()
    if not client:
        raise RuntimeError('GEMINI_API_KEY is not configured.')

    conn = db.get_connection()
    cursor = conn.cursor()
    if job_ids:
        placeholders = ','.join('?' for _ in job_ids)
        cursor.execute(
            f'SELECT job_id, title, company, link, description FROM jobs WHERE job_id IN ({placeholders})',
            job_ids,
        )
    else:
        cursor.execute(
            'SELECT job_id, title, company, link, description FROM jobs '
            'WHERE status = "approved" ORDER BY score DESC LIMIT ?',
            (limit,),
        )
    jobs = cursor.fetchall()
    conn.close()

    if not jobs:
        print('No approved jobs to apply for.')
        return []

    results = []
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE_DIR,
            headless=False,
            viewport={'width': 1440, 'height': 900},
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        for index, job in enumerate(jobs, start=1):
            if progress_tracker.is_stop_requested():
                print('Stop requested; halting applications.')
                break
            job_id, title, company = job[0], job[1], job[2]
            progress_tracker.set_status(f'Applying: {company}', index, len(jobs))
            print(f'[{index}/{len(jobs)}] {company} - {title}')

            db.update_job_status(job_id, 'applying')
            try:
                status, note = await process_job(page, client, profile, job)
            except Exception as e:
                status, note = 'failed', f'Unexpected error while applying: {type(e).__name__}: {e}'

            db.update_job_status(job_id, status)
            db.add_job_note(job_id, note)
            results.append({'job_id': job_id, 'company': company, 'status': status})
            print(f'  -> {status}')
            # Filled forms stay open in their own tab so they can be reviewed and
            # submitted over VNC. Be polite between employers.
            await page.wait_for_timeout(3000)

        # Hold the filled forms open so a human can check and submit them.
        if any(r['status'] == 'ready_to_submit' for r in results):
            minutes = int(os.environ.get('APPLY_REVIEW_WINDOW_MINUTES', '30'))
            print(f'\n{len(browser.pages) - 1} filled form(s) left open for review.')
            print(f'Connect over VNC to check and submit them. Closing in {minutes} min, '
                  f'or immediately if you press Stop on the dashboard.')
            progress_tracker.set_status(
                'Forms filled - review and submit over VNC, then press Stop', len(results), len(results)
            )
            for _ in range(minutes * 60 // 5):
                if progress_tracker.is_stop_requested():
                    print('Stop requested; closing the browser.')
                    break
                await asyncio.sleep(5)

        await browser.close()

    progress_tracker.clear_status()
    _notify(results)
    return results


def _notify(results):
    if not results:
        return
    ready = [r for r in results if r['status'] == 'ready_to_submit']
    other = [r for r in results if r['status'] != 'ready_to_submit']
    lines = ['## Applications prepared\n']
    if ready:
        lines.append(
            'The filled forms are open in the browser on the server. Connect over VNC '
            '(port 5900) to check and submit them, then press Stop on the dashboard.\n'
        )
    for r in ready:
        lines.append(f'- **{r["company"]}** - form filled, waiting for your review and submit')
    if other:
        lines.append('\n## Needing attention\n')
        for r in other:
            lines.append(f'- **{r["company"]}** - {r["status"]}')
    dashboard = os.environ.get('DASHBOARD_URL', 'http://localhost:5050')
    lines.append(f'\nReview them on the dashboard: {dashboard}')
    notifier.send_email('AI Job Scraper - Applications ready to review', '\n'.join(lines))


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Fill in applications for approved jobs.')
    parser.add_argument('--limit', type=int, default=5, help='How many approved jobs to process')
    parser.add_argument('--job-id', action='append', help='Apply for specific job id(s) only')
    args = parser.parse_args()

    asyncio.run(run_applications(limit=args.limit, job_ids=args.job_id))
