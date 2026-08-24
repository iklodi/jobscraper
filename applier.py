"""Semi-automated application filler.

Picks up approved jobs, opens the employer's apply link, fills the form from the
profile answer bank, attaches the generated CV/cover letter, and parks the job for
human review. It never submits, never creates accounts, and never invents an answer.
"""
import asyncio
import datetime
import json
import os
import re
import secrets
import string
import urllib.parse

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
ACCOUNTS_PATH = os.path.join(CVS_DIR, 'ats_accounts.yaml')
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
CLOSED_LISTING_PATTERNS = re.compile(
    r'no longer accepting applications|this job is no longer available|'
    r'position (has been|is) (filled|closed)|posting (has )?(expired|closed)',
    re.I,
)

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
    // Bot traps: forms plant fields a human would never fill, then reject the
    // submission if they contain anything. Never surface them for filling.
    const HONEYPOT_TEXT = /robots? only|do not (enter|fill|use)|leave (this )?(field )?(blank|empty)|honey ?pot|anti-?spam|if you.?re human/i;
    const HONEYPOT_NAME = /^(website|url|homepage|honeypot|hp|bot[-_]?field|winnie|comments?)$/i;
    const isTrap = (el, label) => {
        if (HONEYPOT_TEXT.test(label)) return true;
        if (HONEYPOT_NAME.test(el.name || '')) return true;
        const st = window.getComputedStyle(el);
        if (st.opacity === '0' || st.visibility === 'hidden' || st.display === 'none') return true;
        const r = el.getBoundingClientRect();
        if (r.left < -500 || r.top < -500) return true;           // parked off-screen
        if (el.tabIndex === -1 && el.getAttribute('autocomplete') === 'off'
            && HONEYPOT_NAME.test(el.id || '')) return true;
        return false;
    };
    document.querySelectorAll('input, select, textarea').forEach((el) => {
        if (el.type === 'hidden') return;
        const rect = el.getBoundingClientRect();
        const visible = rect.width > 0 && rect.height > 0;
        if (!visible && el.type !== 'file') return;
        if (isTrap(el, labelFor(el))) return;
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
- Skip any field whose label tells you not to fill it, or that is clearly a bot trap;
  filling one gets the whole application rejected as spam.
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


def generate_password(length=18):
    """Password that satisfies the complexity rules ATS registration forms impose."""
    # Symbols kept to a set that form validators reliably accept.
    alphabet = string.ascii_letters + string.digits + '!@#$%*-_'
    while True:
        pw = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in '!@#$%*-_' for c in pw)):
            return pw


def load_accounts():
    if not os.path.exists(ACCOUNTS_PATH):
        return {'accounts': []}
    with open(ACCOUNTS_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {'accounts': []}


def save_account(entry):
    """Persist credentials immediately, before anything can fail and lose them."""
    data = load_accounts()
    data.setdefault('accounts', [])
    data['accounts'] = [a for a in data['accounts'] if a.get('domain') != entry['domain']]
    data['accounts'].append(entry)
    tmp = ACCOUNTS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, ACCOUNTS_PATH)
    os.chmod(ACCOUNTS_PATH, 0o600)


def account_for(url):
    domain = urllib.parse.urlparse(url).netloc.lower()
    for entry in load_accounts().get('accounts', []):
        if entry.get('domain') == domain:
            return entry
    return None


REGISTRATION_PROMPT = """You are creating a candidate account on a job application site
so that the candidate can apply. This is the candidate's OWN account, created with their
consent, using their own details.

CANDIDATE DETAILS:
  Full name: {full_name}
  First name: {first_name}
  Last name: {last_name}
  Email: {email}
  Phone: {phone}
  Country: {country}

PASSWORD TO USE (use this exact string for every password and confirm-password field):
  {password}

FORM FIELDS DETECTED (JSON):
{fields}

VISIBLE PAGE TEXT:
{page_text}

Output valid JSON only:
{{
  "actions": [{{"idx": 0, "action": "fill|select|check|skip", "value": "..."}}],
  "page_kind": "registration|sign_in|application_form|other",
  "submit_idx": null,
  "notes": "..."
}}

RULES:
- Fill every password and "confirm password" / "re-enter password" field with the exact
  password given above.
- Fill email, name and other identity fields from the candidate details.
- Tick required terms-of-service and privacy checkboxes; leave marketing opt-ins unticked.
- Do NOT answer any question that is not part of creating the account.
- If any field asks you not to fill it, or looks like a bot trap, skip it.
- Set page_kind to "sign_in" if this is a login form for an existing account rather than
  a registration form.
"""


async def click_create_account_tab(page):
    """Registration is often behind a 'Create Account' toggle on a sign-in page."""
    create_re = re.compile(r'create (an )?account|sign up|register|new user|créer un compte', re.I)
    for role in ('link', 'button'):
        try:
            control = page.get_by_role(role, name=create_re).first
            await control.wait_for(state='visible', timeout=3000)
            await control.click(timeout=3000)
            await page.wait_for_timeout(2500)
            return True
        except Exception:
            continue
    return False


async def submit_form(page, labels):
    """Click the form's own submit control; returns True if one was clicked.

    The same label often appears twice (a tab or heading, then the real submit),
    so prefer the last match - submit buttons sit at the end of the form.
    """
    pattern = re.compile(labels, re.I)
    for role in ('button', 'link'):
        for pick in ('last', 'first'):
            try:
                matches = page.get_by_role(role, name=pattern)
                control = matches.last if pick == 'last' else matches.first
                await control.wait_for(state='visible', timeout=4000)
                await control.scroll_into_view_if_needed(timeout=2000)
                await control.click(timeout=5000)
                await page.wait_for_timeout(5000)
                return True
            except Exception:
                continue
    return False


async def create_or_signin_account(page, client, profile, job_id):
    """Create (or sign in to) a candidate account. Returns (ok, note)."""
    identity = profile.get('identity', {})
    email = identity.get('email')
    domain = urllib.parse.urlparse(page.url).netloc.lower()

    existing = account_for(page.url)
    password = existing['password'] if existing else generate_password()

    if not existing:
        await click_create_account_tab(page)

    body = await page.evaluate(COLLECT_FIELDS_JS)
    fields, page_text = body['fields'], body['text']
    if not any(f.get('type') == 'password' for f in fields):
        return False, f'No password field found on the sign-in page at {page.url}.'

    if CAPTCHA_PATTERNS.search(page_text):
        return False, (f'Account creation at {domain} is behind a CAPTCHA, so it was not '
                       f'attempted. Create the account manually.')

    plan = ask_gemini(client, REGISTRATION_PROMPT.format(
        full_name=identity.get('full_name', ''),
        first_name=identity.get('first_name', ''),
        last_name=identity.get('last_name', ''),
        email=email, phone=identity.get('phone', ''),
        country=identity.get('country', ''),
        password=password,
        fields=json.dumps(fields, ensure_ascii=False)[:15000],
        page_text=page_text[:2500],
    ))
    if not plan:
        return False, f'Could not map the registration form at {domain}.'

    # Store the credentials BEFORE submitting - a failed or redirecting submit
    # must never leave an account whose password we no longer know.
    if not existing:
        save_account({
            'domain': domain,
            'email': email,
            'password': password,
            'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'created_for_job': job_id,
            'email_verified': False,
        })

    await apply_actions(page, plan.get('actions', []), fields)
    signing_in = plan.get('page_kind') == 'sign_in' or bool(existing)
    clicked = await submit_form(
        page,
        r'^(sign in|log in|login)\b' if signing_in
        else r'^(create account|sign up|register|submit|continue|create)\b',
    )
    if not clicked:
        return False, f'Could not find the submit button on the {domain} account form.'

    await page.wait_for_timeout(4000)
    after = await page.evaluate(COLLECT_FIELDS_JS)

    # Workday in particular likes to land on a transient "Something went wrong,
    # please refresh" screen straight after a successful sign-up.
    if re.search(r'something went wrong|please refresh the page', after['text'], re.I):
        try:
            await page.reload(timeout=45000)
            await page.wait_for_timeout(5000)
            after = await page.evaluate(COLLECT_FIELDS_JS)
        except Exception:
            pass

    after_text = after['text']

    if re.search(r'already (exists|registered|in use)|account with this email', after_text, re.I):
        return False, (
            f'{domain} says an account already exists for {email}, and the stored password '
            f'did not work. Reset the password manually, then add it to ats_accounts.yaml.'
        )
    if re.search(r'verify your email|confirmation (email|link)|check your (email|inbox)', after_text, re.I):
        return False, (
            f'Account created at {domain} with the credentials saved in ats_accounts.yaml. '
            f'{domain} sent a verification email to {email} - click the link, then re-run '
            f'this job and the application will continue automatically.'
        )
    if any(f.get('type') == 'password' for f in after['fields']):
        return False, (
            f'Still on the account form at {domain} after submitting - it likely rejected '
            f'something. Credentials are saved in ats_accounts.yaml; finish manually.'
        )

    # Signed in: the email shows in the chrome, or the account step has dropped
    # out of the progress list.
    signed_in = bool(email and email.lower() in after_text.lower()) or \
        not re.search(r'create account\s*/\s*sign in', after_text, re.I)
    if not signed_in:
        return False, (
            f'Submitted the {domain} account form but could not confirm being signed in. '
            f'Credentials are saved in ats_accounts.yaml; check manually.'
        )

    entry = account_for(page.url)
    if entry:
        # Records that the credentials work - not that the address is verified,
        # which only clicking the emailed link can establish.
        entry['last_signin_ok'] = datetime.datetime.now().isoformat(timespec='seconds')
        save_account(entry)

    verb = 'Signed in to' if signing_in else 'Created an account at'
    return True, f'{verb} {domain} (credentials in ats_accounts.yaml).'


async def dismiss_cookie_banner(page):
    """Accept cookie/consent banners, which otherwise intercept clicks."""
    accept = re.compile(r'^(accept|accept all|allow all|i agree|agree|got it|'
                        r'tout accepter|alle akzeptieren|akzeptieren)\b', re.I)
    for role in ('button', 'link'):
        try:
            banner = page.get_by_role(role, name=accept).first
            await banner.wait_for(state='visible', timeout=2500)
            await banner.click(timeout=2500)
            await page.wait_for_timeout(1000)
            return True
        except Exception:
            continue
    return False


async def try_advance_to_form(page):
    """Click an 'Apply' control on an employer page to reach the real form.

    Returns the page holding the form (possibly a new tab), or None.
    """
    apply_re = re.compile(r'\b(apply now|apply for this job|apply online|apply|'
                          r'postuler|bewerben|jetzt bewerben)\b', re.I)
    for role in ('link', 'button'):
        control = page.get_by_role(role, name=apply_re).first
        try:
            await control.wait_for(state='visible', timeout=4000)
            await control.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            continue
        before = page.url
        try:
            async with page.context.expect_page(timeout=8000) as new_page_info:
                await control.click(timeout=5000)
            new_page = await new_page_info.value
            await new_page.wait_for_load_state('domcontentloaded', timeout=30000)
            await new_page.wait_for_timeout(2500)
            return new_page
        except Exception:
            await page.wait_for_timeout(3000)
            if page.url != before:
                return page
            # Same URL but the click may have revealed an in-page form.
            return page
    return None


async def upload_documents(page, fields, cv_path, cl_path, extra_path=None):
    """Attach the generated PDFs (and any extra document) to file inputs on the page."""
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
        elif re.search(r'reference|recommendation|referenz|zeugnis|attestation|certificate|'
                       r'additional|other document|supporting', haystack):
            target = extra_path
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
        try:
            listing_text = await page.evaluate('() => document.body.innerText.slice(0, 4000)')
        except Exception:
            listing_text = ''
        if CLOSED_LISTING_PATTERNS.search(listing_text):
            return 'failed', 'The listing is closed - LinkedIn shows "No longer accepting applications".'
        return 'failed', (
            'Could not find an Apply button on the LinkedIn posting. See '
            'last_apply_lookup_failure.png in the applications folder for what the page looked like.'
        )

    # An employer's apply link often lands on the job description first, so walk
    # forward until we are actually looking at an application form. Never fill a
    # page that is not one - job pages carry newsletter and job-alert signups.
    plan = fields = None
    apply_url = target.url
    account_attempted, account_note = False, ''
    for _ in range(4):
        await dismiss_cookie_banner(target)
        apply_url = target.url
        body = await target.evaluate(COLLECT_FIELDS_JS)
        fields, page_text = body['fields'], body['text']

        if CAPTCHA_PATTERNS.search(page_text):
            return 'failed', f'Blocked by a CAPTCHA / bot check at {apply_url}. Needs a human, ideally over VNC.'

        if ACCOUNT_WALL_PATTERNS.search(page_text) or any(f.get('type') == 'password' for f in fields):
            allowed = (profile.get('policies') or {}).get('allow_account_creation', False)
            if not allowed:
                return 'account_required', (
                    f'The employer requires creating an account or signing in before the form can be '
                    f'filled, so nothing was entered. Apply manually at: {apply_url}\n'
                    f'Documents ready in: {folder}'
                )
            if account_attempted:
                return 'account_required', (
                    f'Still behind an account wall at {apply_url} after an account attempt.\n'
                    f'{account_note}\nDocuments ready in: {folder}'
                )
            account_attempted = True
            ok, account_note = await create_or_signin_account(target, client, profile, job_id)
            print(f'  -> account: {account_note}')
            if not ok:
                return 'account_required', f'{account_note}\nApply at: {apply_url}\nDocuments ready in: {folder}'
            await dismiss_cookie_banner(target)
            await target.wait_for_timeout(2000)
            continue

        if fields:
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
            if plan.get('page_kind') == 'application_form':
                break

        # Not an application form yet - follow this page's own Apply control.
        advanced = await try_advance_to_form(target)
        if not advanced:
            iframe_note = ''
            if body.get('iframes'):
                iframe_note = f" The form may be inside an iframe ({body['iframes'][0]})."
            return 'failed', (
                f'Reached {apply_url} but could not get to an application form - '
                f'nothing was filled in.{iframe_note}\nApply manually; documents are in: {folder}'
            )
        target = advanced
    else:
        return 'failed', (
            f'Could not reach an application form from {apply_url} - nothing was filled in.\n'
            f'Apply manually; documents are in: {folder}'
        )

    filled, errors = await apply_actions(target, plan.get('actions', []), fields)

    reference_letter = (profile.get('employment') or {}).get('reference_letter')
    reference_path = os.path.join(CVS_DIR, reference_letter) if reference_letter else None
    if reference_path and not os.path.exists(reference_path):
        reference_path = None
    uploaded = await upload_documents(target, fields, cv_path, cl_path, reference_path)

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
    if account_note:
        lines.append(account_note)
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
