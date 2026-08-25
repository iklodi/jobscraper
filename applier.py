"""Semi-automated application filler.

Picks up approved jobs, opens the employer's apply link, fills the form from the
profile answer bank, attaches the generated CV/cover letter, and either parks the
job for human review or - with --submit - sends it.

It never invents an answer, and it will not submit an application that fails the
pre-submit checks: every required field filled, no question left unanswered, and
no field that refused input. Anything short of that is parked for a human.
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

# Not override=True: an explicitly exported variable must beat the .env file,
# otherwise per-run settings passed on the command line are silently ignored.
load_dotenv()

# This host also runs other services, so a long batch must not accumulate tabs.
MAX_OPEN_REVIEW_TABS = int(os.environ.get('APPLY_MAX_OPEN_TABS', '3'))
MIN_FREE_MB = int(os.environ.get('APPLY_MIN_FREE_MB', '400'))

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
    // When a modal is open (LinkedIn Easy Apply and friends), only it matters:
    // reading the whole page makes the form look like a job description.
    // Only a dialog that actually holds inputs is the form; cookie and privacy
    // overlays are dialogs too, and scoping to one of those hides the real page.
    const dialogs = Array.from(document.querySelectorAll('[role=dialog]'))
        .filter((d) => { const r = d.getBoundingClientRect(); return r.width > 200 && r.height > 150; })
        .filter((d) => d.querySelectorAll('input,select,textarea').length > 0);
    const root = dialogs.length ? dialogs[dialogs.length - 1] : document;
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
        // File inputs are legitimately hidden behind styled upload buttons, so the
        // visibility heuristics below would wrongly discard them.
        if (el.type === 'file') return false;
        const st = window.getComputedStyle(el);
        if (st.opacity === '0' || st.visibility === 'hidden' || st.display === 'none') return true;
        const r = el.getBoundingClientRect();
        if (r.left < -500 || r.top < -500) return true;           // parked off-screen
        if (el.tabIndex === -1 && el.getAttribute('autocomplete') === 'off'
            && HONEYPOT_NAME.test(el.id || '')) return true;
        return false;
    };
    root.querySelectorAll('input, select, textarea').forEach((el) => {
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
        if (el.type === 'radio' || el.type === 'checkbox') {
            // The option's own label is "Yes"/"No"; the question lives on the group.
            const grp = el.closest('fieldset,[role=radiogroup],[role=group]');
            if (grp) {
                const lg = grp.querySelector('legend');
                const t = (lg ? lg.innerText : grp.getAttribute('aria-label') || '').trim();
                if (t) entry.group = t.split('\\n')[0].slice(0, 200);
            }
        }
        out.push(entry);
        idx += 1;
    });
    // Custom dropdowns (Workday, react-select, Ashby...) are not <select> elements.
    root.querySelectorAll(
        '[role=combobox], button[aria-haspopup=listbox], [aria-haspopup=listbox],' +
        '[data-automation-id*="selectinput"], [data-uxi-widget-type="selectinput"]'
    ).forEach((el) => {
        if (el.hasAttribute('data-jsapply')) return;
        const rect = el.getBoundingClientRect();
        if (!(rect.width > 0 && rect.height > 0)) return;
        let label = el.getAttribute('aria-label') || '';
        const lb = el.getAttribute('aria-labelledby');
        if (!label && lb) {
            const l = document.getElementById(lb);
            if (l) label = l.innerText.trim();
        }
        if (!label) {
            const w = el.closest('div,section,li');
            if (w) label = (w.innerText || '').trim().split('\\n')[0].slice(0, 120);
        }
        if (HONEYPOT_TEXT.test(label)) return;
        el.setAttribute('data-jsapply', String(idx));
        out.push({
            idx: idx, tag: 'widget', type: 'select-custom',
            name: el.getAttribute('name') || '', id: el.id || '',
            label: label, placeholder: '',
            required: el.getAttribute('aria-required') === 'true',
            value: (el.innerText || '').trim().slice(0, 60),
            options: null,
        });
        idx += 1;
    });
    return {
        fields: out,
        iframes: Array.from(document.querySelectorAll('iframe')).map((f) => f.src).filter(Boolean),
        in_dialog: dialogs.length > 0,
        text: (root === document ? (document.body ? document.body.innerText : '')
                                  : root.innerText).slice(0, 6000),
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

WHAT COUNTS AS AN application_form:
- Many employers split the application over several steps ("My Information",
  "My Experience", "Application Questions", "Voluntary Disclosures", "Self Identify",
  "Review"). EACH of those steps is an "application_form" - classify them as such even
  though they are only part of the application.
- A newsletter, job-alert or "join our talent network" signup is NOT an application form;
  classify it as "other" and skip every field.

ABSOLUTE RULES:
- NEVER invent, guess, or approximate an answer. If the profile does not contain the
  information, put the field in "unanswered" and use action "skip". A wrong answer on a
  job application is worse than an unfilled field.
- For "select" actions on a field that lists "options", the value MUST be one of those
  options, copied verbatim.
- A field of type "select-custom" is a dropdown whose choices are not visible yet, and
  its "options" is null. Still use action "select", and give the exact visible text you
  expect the option to have (e.g. "Mobile", "LinkedIn", "No"). Answer these whenever the
  profile supports it - they are usually required.
- A radio or checkbox field may carry a "group" holding the actual question; answer based
  on the group question, and use action "check" on the option that matches the profile.
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


async def form_dialog_open(page):
    """True when a modal holding actual form fields is up.

    LinkedIn's messaging widget is also role=dialog, so presence alone is not
    enough - the dialog must contain inputs.
    """
    try:
        return await page.evaluate(
            """() => Array.from(document.querySelectorAll('[role=dialog]'))
                   .filter(d => { const r = d.getBoundingClientRect();
                                  return r.width > 300 && r.height > 200; })
                   .some(d => d.querySelectorAll('input,select,textarea').length > 0)"""
        )
    except Exception:
        return False


async def find_apply_url(page, linkedin_url, job_id='unknown'):
    """Open the LinkedIn posting and follow its apply button to the employer's form."""
    await page.goto(linkedin_url, timeout=60000)
    await page.wait_for_timeout(5000)

    # LinkedIn ships obfuscated class names and renders the apply control as a
    # button, a link, or a div with role=button depending on the posting, so go
    # through the accessibility tree rather than CSS.
    # Anchored first, then loose: the button's accessible name varies ("Apply",
    # "Easy Apply", "Apply on LinkedIn", sometimes prefixed by icon text).
    strict_re = re.compile(r'^\s*(easy apply|apply)\b', re.I)
    loose_re = re.compile(r'\b(easy apply|apply)\b', re.I)
    candidates = [
        page.get_by_role('button', name=strict_re),
        page.get_by_role('link', name=strict_re),
        page.locator('button.jobs-apply-button, a.jobs-apply-button'),
        page.locator('[class*="jobs-apply"] button, [class*="jobs-apply"] a'),
        page.get_by_role('button', name=loose_re),
        page.get_by_role('link', name=loose_re),
    ]
    for candidate in candidates:
        button = candidate.first
        try:
            await button.wait_for(state='visible', timeout=6000)
            await button.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            continue

        # On many postings the apply control is an <a> whose click does nothing
        # under automation. Navigating to its href directly is far more reliable.
        try:
            href = await button.get_attribute('href')
        except Exception:
            href = None
        # Only follow links that leave LinkedIn: an in-site href belongs to Easy
        # Apply, where navigating away closes the modal we actually want.
        external = False
        if href and href.startswith('http'):
            host = urllib.parse.urlparse(href).netloc.lower()
            external = 'linkedin.com' not in host
        if external:
            try:
                await page.goto(href, timeout=60000)
                await page.wait_for_timeout(4000)
                return page, 'external_link'
            except Exception:
                pass

        # Click, then verify something actually happened. Some apply controls are
        # inert anchors, so a click that changes nothing means try the next one.
        before_url = page.url
        before_pages = len(page.context.pages)
        try:
            await button.click(timeout=8000)
        except Exception:
            continue

        # Some controls ignore a synthesised click; dispatching one on the
        # element itself still triggers the site's own handler.
        reacted = False
        for _ in range(4):
            await page.wait_for_timeout(1000)
            if (len(page.context.pages) > before_pages or page.url != before_url
                    or await form_dialog_open(page)):
                reacted = True
                break
        if not reacted:
            try:
                await button.evaluate('el => el.click()')
            except Exception:
                pass

        for _ in range(12):                      # up to ~12s for a reaction
            await page.wait_for_timeout(1000)
            if len(page.context.pages) > before_pages:
                new_page = page.context.pages[-1]
                try:
                    await new_page.wait_for_load_state('domcontentloaded', timeout=30000)
                except Exception:
                    pass
                await new_page.wait_for_timeout(2000)
                return new_page, 'external'
            if await form_dialog_open(page):
                await page.wait_for_timeout(1500)
                return page, 'easy_apply'
            if page.url != before_url:
                await page.wait_for_timeout(3000)
                return page, 'same_tab'
        # Nothing happened - fall through and try the next candidate.

    # Nothing matched - leave a screenshot behind so the failure is diagnosable.
    try:
        await page.screenshot(
            path=os.path.join(OUTPUT_DIR, f'{job_id}_apply_lookup_failure.png'))
    except Exception:
        pass

    # Distinguish a LinkedIn Easy Apply posting, which does not drive reliably
    # under automation, from a genuinely broken or closed one - they need
    # different things from the candidate.
    easy_js = (
        "() => {"
        " const a = Array.from(document.querySelectorAll('button,a,[role=button]'))"
        "   .find(e => /^\\s*(easy apply|apply)\\b/i.test((e.innerText || '').trim()));"
        " if (!a) return false;"
        " const href = a.getAttribute('href') || '';"
        " return !href || href.startsWith('#') || href.includes('linkedin.com');"
        "}"
    )
    try:
        is_easy = await page.evaluate(easy_js)
    except Exception:
        is_easy = False
    return (None, 'easy_apply_manual') if is_easy else (None, None)


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


async def click_sign_in_tab(page):
    """Switch to the sign-in form when we already hold credentials for this site."""
    sign_in_re = re.compile(r'^(sign in|log ?in)$|already have an account', re.I)
    for role in ('link', 'button'):
        try:
            control = page.get_by_role(role, name=sign_in_re).last
            await control.wait_for(state='visible', timeout=3000)
            await control.click(timeout=3000)
            await page.wait_for_timeout(2500)
            return True
        except Exception:
            continue
    return False


async def form_errors(page):
    """Inline validation messages, so a rejection is not reported as a mystery."""
    try:
        msgs = await page.evaluate(
            """() => Array.from(document.querySelectorAll(
                   '[role=alert],[data-automation-id*=error],[class*=error]'))
                   .map(e => (e.innerText || '').trim())
                   .filter(t => t && t.length < 200)
                   .filter(t => !/^(current )?step \\d+ of \\d+$/i.test(t))
                   .slice(0, 5)"""
        )
        return '; '.join(dict.fromkeys(msgs))
    except Exception:
        return ''


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

    stored = account_for(page.url)
    password = stored['password'] if stored else generate_password()

    # Credentials are written before the registration is submitted so a password
    # can never be lost - but that means a stored entry does not prove the
    # account exists. Only a confirmed one is worth signing in with.
    existing = bool(stored and stored.get('registration_confirmed'))

    if existing:
        await click_sign_in_tab(page)
    else:
        await click_create_account_tab(page)

    body = await page.evaluate(COLLECT_FIELDS_JS)
    fields, page_text = body['fields'], body['text']
    if not any(f.get('type') == 'password' for f in fields):
        return False, f'No password field found on the sign-in page at {page.url}.'

    if CAPTCHA_PATTERNS.search(page_text):
        return False, (f'Account creation at {domain} is behind a CAPTCHA, so it was not '
                       f'attempted. Create the account manually.')

    # Signing in is a two-field form; do it deterministically rather than paying
    # for an LLM round-trip that can misread it.
    if existing:
        pw_fields = [f for f in fields if f.get('type') == 'password']
        text_fields = [f for f in fields if f.get('type') in ('text', 'email')]
        if pw_fields and text_fields:
            await page.locator(f'[data-jsapply="{text_fields[0]["idx"]}"]').fill(email)
            await page.locator(f'[data-jsapply="{pw_fields[0]["idx"]}"]').fill(password)
            if not await submit_form(page, r'^(sign in|log ?in)$'):
                return False, f'Could not find the sign-in button at {domain}.'
            await page.wait_for_timeout(6000)
            after = await page.evaluate(COLLECT_FIELDS_JS)
            if any(f.get('type') == 'password' for f in after['fields']):
                detail = await form_errors(page)
                return False, (
                    f'Sign-in at {domain} did not go through'
                    + (f': "{detail}"' if detail else ' (no error shown)')
                    + '. Credentials are in ats_accounts.yaml; check them manually.'
                )
            entry = dict(stored or {})
            entry['registration_confirmed'] = True
            entry['last_signin_ok'] = datetime.datetime.now().isoformat(timespec='seconds')
            if entry.get('domain'):
                save_account(entry)
            return True, f'Signed in to {domain} with the stored credentials.'

    # Registration forms are near-identical everywhere: an email, one or two
    # password boxes and a consent tick. Filling them directly is far more
    # reliable than an LLM mapping, which kept missing the confirm-password box.
    pw_fields = [f for f in fields if f.get('type') == 'password']
    text_fields = [f for f in fields if f.get('type') in ('text', 'email')]
    if pw_fields and text_fields:
        try:
            await page.locator(f'[data-jsapply="{text_fields[0]["idx"]}"]').fill(email)
            for pw in pw_fields:
                await page.locator(f'[data-jsapply="{pw["idx"]}"]').fill(password)
            for box in [f for f in fields if f.get('type') == 'checkbox']:
                loc = page.locator(f'[data-jsapply="{box["idx"]}"]')
                try:
                    await tick_box(page, loc, box)
                except Exception:
                    pass
            if not await submit_form(page, r'^(create account|sign up|register|continue|submit)\b'):
                return False, f'Could not find the create-account button at {domain}.'
            await page.wait_for_timeout(5000)
            after = await page.evaluate(COLLECT_FIELDS_JS)
            if re.search(r'something went wrong|please refresh', after['text'], re.I):
                await page.reload(timeout=45000)
                await page.wait_for_timeout(5000)
                after = await page.evaluate(COLLECT_FIELDS_JS)
            if not any(f.get('type') == 'password' for f in after['fields']):
                entry = account_for(page.url) or {}
                if entry:
                    entry['registration_confirmed'] = True
                    entry['last_signin_ok'] = datetime.datetime.now().isoformat(timespec='seconds')
                    save_account(entry)
                return True, f'Created an account at {domain} (credentials in ats_accounts.yaml).'
            detail = await form_errors(page)
            return False, (
                f'The {domain} account form did not accept the registration'
                + (f': "{detail}"' if detail else ' and gave no error')
                + '. Credentials are saved in ats_accounts.yaml; finish manually.'
            )
        except Exception as e:
            return False, f'Account creation at {domain} failed: {type(e).__name__}.'

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
            'registration_confirmed': False,
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
        detail = await form_errors(page)
        return False, (
            f'Still on the account form at {domain} after submitting'
            + (f': "{detail}"' if detail else ' and it gave no error message')
            + '. Credentials are saved in ats_accounts.yaml; finish manually.'
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


# Controls that move a multi-step application forward, and the ones that send it.
NEXT_LABELS = re.compile(
    r'^(save and continue|save & continue|continue|next|next step|save and next|'
    r'weiter|suivant|continuer)\b', re.I)
SUBMIT_LABELS = re.compile(
    r'^(submit|submit application|send application|send|finish|complete application|'
    r'envoyer|absenden)\b', re.I)


async def find_control(page, pattern, prefer_last=True):
    """Return a visible button/link matching the label, or None."""
    for role in ('button', 'link'):
        matches = page.get_by_role(role, name=pattern)
        for control in ([matches.last, matches.first] if prefer_last else [matches.first]):
            try:
                await control.wait_for(state='visible', timeout=2500)
                return control
            except Exception:
                continue
    return None


async def advance_step(page):
    """Click the wizard's 'next' control. Never clicks a submit control."""
    control = await find_control(page, NEXT_LABELS)
    if not control:
        return False
    try:
        await control.scroll_into_view_if_needed(timeout=3000)
        await control.click(timeout=6000)
        await page.wait_for_timeout(4500)
        return True
    except Exception:
        return False


EMPTY_REQUIRED_JS = """
() => {
    const out = [];
    document.querySelectorAll('[required], [aria-required="true"]').forEach((el) => {
        const r = el.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0)) return;
        const tag = el.tagName.toLowerCase();
        let empty = false;
        if (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio')) {
            const name = el.name;
            if (name) {
                empty = !document.querySelector(`input[name="${CSS.escape(name)}"]:checked`);
            } else {
                empty = !el.checked;
            }
        } else if (tag === 'input' || tag === 'textarea' || tag === 'select') {
            empty = !String(el.value || '').trim();
        } else {
            const t = (el.innerText || '').trim();
            empty = !t || /^select one$/i.test(t);
        }
        if (!empty) return;
        let label = el.getAttribute('aria-label') || '';
        if (!label && el.labels && el.labels.length) label = el.labels[0].innerText.trim();
        if (!label) {
            const w = el.closest('div,fieldset,section,li');
            if (w) label = (w.innerText || '').trim().split('\\n')[0].slice(0, 80);
        }
        if (label && !out.includes(label)) out.push(label);
    });
    return out.slice(0, 12);
}
"""


async def preflight_problems(page, unanswered, fill_errors):
    """Reasons this application must NOT be auto-submitted."""
    problems = []
    if unanswered:
        problems.append(f'{len(unanswered)} question(s) the profile could not answer')
    if fill_errors:
        problems.append(f'{len(fill_errors)} field(s) that refused input')
    try:
        empty = await page.evaluate(EMPTY_REQUIRED_JS)
    except Exception:
        empty = []
        problems.append('could not verify required fields')
    if empty:
        problems.append('required field(s) still empty: ' + '; '.join(empty[:6]))
    return problems


async def do_submit(page):
    """Click the real submit control and confirm the application went through."""
    control = await find_control(page, SUBMIT_LABELS)
    if not control:
        return False, 'submit control disappeared'
    try:
        await control.scroll_into_view_if_needed(timeout=3000)
        await control.click(timeout=8000)
    except Exception as e:
        return False, f'submit click failed ({type(e).__name__})'
    await page.wait_for_timeout(7000)
    try:
        text = await page.evaluate('() => document.body.innerText.slice(0, 3000)')
    except Exception:
        text = ''
    if re.search(r'thank you|application (has been )?(submitted|received|sent)|'
                 r'successfully (submitted|applied)|we have received|merci', text, re.I):
        return True, 'confirmed by the site'

    # A challenge on submit is a deliberate human gate; we do not defeat it.
    try:
        challenge = await page.evaluate(
            """() => {
                const hit = /drag the shape|select all images|i am not a robot|
                             verify you are human|puzzle/ix;
                if (hit.test(document.body.innerText)) return true;
                return !!document.querySelector(
                    'iframe[src*="captcha"], iframe[title*="captcha" i], '
                    + '[class*="captcha" i], [id*="captcha" i]');
            }"""
        )
    except Exception:
        challenge = False
    if challenge or CAPTCHA_PATTERNS.search(text):
        return False, (
            'a CAPTCHA appeared on submit. The form is complete - connect over VNC, '
            'solve the challenge and press submit yourself'
        )

    if await find_control(page, SUBMIT_LABELS):
        detail = await form_errors(page)
        return False, f'still on the form after clicking submit{": " + detail if detail else ""}'
    return True, 'submitted (no explicit confirmation message found)'


async def fill_wizard(page, client, profile, job, cv_path, cl_path, ref_path, folder,
                      max_steps=10, auto_submit=False):
    """Fill an application, walking multi-step wizards, and stop before submitting.

    Returns (summary_lines, reached_submit).
    """
    job_id, title, company, _link, description = job
    lines, unanswered, uploaded_all = [], [], []
    seen = set()
    reached_submit = False
    submitted = False
    all_errors = []

    for step in range(1, max_steps + 1):
        await dismiss_cookie_banner(page)
        body = await page.evaluate(COLLECT_FIELDS_JS)
        fields, page_text = body['fields'], body['text']

        if CAPTCHA_PATTERNS.search(page_text):
            lines.append(f'Step {step}: stopped at a CAPTCHA - finish this one by hand.')
            break

        # A page we have already filled means the wizard did not actually advance.
        signature = (page.url, tuple(f.get('label', '')[:40] for f in fields))
        if signature in seen:
            lines.append(f'Step {step}: the form did not advance (it may be rejecting a value).')
            break
        seen.add(signature)

        step_name = 'form'
        heading = re.search(r'current step \d+ of \d+\s*\|?\s*([^\n|]{3,40})', page_text, re.I)
        if heading:
            step_name = heading.group(1).strip()

        filled = 0
        if fields:
            plan = ask_gemini(client, MAPPING_PROMPT.format(
                profile=yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
                title=title, company=company,
                description=(description or '')[:3000],
                fields=json.dumps(fields, ensure_ascii=False)[:20000],
                page_text=page_text[:3000],
            ))
            kind = plan.get('page_kind') if plan else None
            if kind == 'confirmation':
                lines.append(f'Step {step}: reached a confirmation page - the application appears to be in.')
                break
            if kind and kind != 'application_form':
                lines.append(
                    f'Step {step}: stopped - this page is a "{kind}", not part of the '
                    f'application, so nothing was filled in.'
                )
                break
            if plan:
                filled, errors = await apply_actions(page, plan.get('actions', []), fields)
                unanswered.extend(plan.get('unanswered') or [])
                if errors:
                    all_errors.extend(errors)
                    lines.append(f'Step {step} ({step_name}): fields that refused input - {"; ".join(errors)}')

        uploaded = await upload_documents(page, fields, cv_path, cl_path, ref_path)
        uploaded_all.extend(uploaded)

        try:
            shot = os.path.join(folder, f'{job_id}_step{step}_{re.sub(r"[^A-Za-z0-9]+", "_", step_name)[:24]}.png')
            await page.screenshot(path=shot, full_page=True)
        except Exception:
            pass

        summary = f'Step {step} ({step_name}): filled {filled} field(s)'
        if uploaded:
            summary += f', uploaded {", ".join(uploaded)}'
        lines.append(summary + '.')

        # Stop at the final step rather than sending the application.
        if await find_control(page, SUBMIT_LABELS):
            reached_submit = True
            problems = await preflight_problems(page, unanswered, all_errors)
            if not auto_submit:
                lines.append('Reached the final step. Nothing was sent; review it and submit yourself.')
            elif problems:
                lines.append(
                    'Reached the final step but did NOT submit, because: '
                    + '; '.join(problems) + '. Fix these and submit yourself.'
                )
            else:
                ok, detail = await do_submit(page)
                submitted = ok
                lines.append(
                    f'SUBMITTED - {detail}.' if ok
                    else f'Tried to submit but it did not go through: {detail}. Nothing was sent.'
                )
                try:
                    await page.screenshot(
                        path=os.path.join(folder, f'{job_id}_submitted.png'), full_page=True)
                except Exception:
                    pass
            break

        if not await advance_step(page):
            lines.append('No "Save and Continue" control found, so this looks like the last page.')
            break
    else:
        lines.append(f'Stopped after {max_steps} steps without reaching a submit page.')

    if unanswered:
        lines.append('\nQUESTIONS THE PROFILE DOES NOT ANSWER (left blank):')
        for item in unanswered:
            lines.append(f'  - {item.get("question")}  ({item.get("reason")})')
        lines.append('Add these to profile.yaml so future applications answer them automatically.')

    return lines, reached_submit, submitted


async def dismiss_cookie_banner(page):
    """Clear consent overlays, which otherwise intercept clicks.

    Sites often stack two - a cookie dialog and a privacy notice - so keep
    dismissing until nothing matches.
    """
    accept = re.compile(r'^(accept|accept all|accept all cookies|allow all|i agree|agree|'
                        r'got it|ok|proceed|proceed & close|continue|close|'
                        r'tout accepter|alle akzeptieren|akzeptieren)\b', re.I)
    dismissed = False
    for _ in range(3):
        clicked = False
        for role in ('button', 'link'):
            try:
                banner = page.get_by_role(role, name=accept).first
                await banner.wait_for(state='visible', timeout=2000)
                await banner.click(timeout=2500)
                await page.wait_for_timeout(1200)
                clicked = dismissed = True
                break
            except Exception:
                continue
        if not clicked:
            break
    return dismissed


async def try_advance_to_form(page):
    """Click an 'Apply' control on an employer page to reach the real form.

    Returns the page holding the form (possibly a new tab), or None.
    """
    # Workday-style chooser first ("Apply Manually" beats "Autofill with Resume",
    # whose parser mangles job titles, and "Apply With LinkedIn", which re-auths).
    patterns = [
        re.compile(r'^\s*apply manually\s*$', re.I),
        re.compile(r'^\s*(start (your )?application|apply now|apply for this job|apply online)\s*$', re.I),
        re.compile(r'\b(apply|postuler|bewerben|jetzt bewerben)\b', re.I),
    ]
    for apply_re in patterns:
        for role in ('link', 'button'):
            control = page.get_by_role(role, name=apply_re).first
            try:
                await control.wait_for(state='visible', timeout=3000)
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
                # No new tab: either a same-tab navigation or an in-page reveal.
                await page.wait_for_timeout(3500)
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


async def select_native(locator, value, field):
    """Choose an option in a <select>, tolerating near-miss wording.

    The model answers "No" where the option reads "No - I do not require
    sponsorship", so fall back to matching against the options we collected.
    """
    value = str(value)
    for attempt in (
        lambda: locator.select_option(label=value, timeout=4000),
        lambda: locator.select_option(value=value, timeout=3000),
    ):
        try:
            await attempt()
            return True
        except Exception:
            continue

    options = field.get('options') or []
    if not options:
        # Some selects are populated by JS after the page snapshot was taken.
        try:
            options = await locator.evaluate(
                'el => Array.from(el.options || []).map(o => o.text.trim()).filter(Boolean)')
        except Exception:
            options = []
    wanted = value.strip().lower()
    ranked = [o for o in options if o.strip().lower() == wanted] \
        or [o for o in options if o.strip().lower().startswith(wanted)] \
        or [o for o in options if wanted and wanted in o.strip().lower()] \
        or [o for o in options if o.strip().lower() and o.strip().lower() in wanted]
    for option in ranked[:3]:
        try:
            await locator.select_option(label=option, timeout=3000)
            return True
        except Exception:
            continue
    return False


async def select_custom(page, locator, value):
    """Choose a value from a custom dropdown widget (no native <select>)."""
    await locator.scroll_into_view_if_needed(timeout=3000)
    await locator.click(timeout=5000)
    await page.wait_for_timeout(900)

    exact = re.compile(rf'^\s*{re.escape(str(value))}\s*$', re.I)
    loose = re.compile(re.escape(str(value)), re.I)
    for pattern in (exact, loose):
        for getter in (
            lambda p: page.get_by_role('option', name=p),
            lambda p: page.locator('[role=option]').filter(has_text=p),
            lambda p: page.locator('li,div[role=listitem]').filter(has_text=p),
        ):
            try:
                option = getter(pattern).first
                await option.wait_for(state='visible', timeout=2500)
                await option.click(timeout=3000)
                await page.wait_for_timeout(600)
                return True
            except Exception:
                continue

    # Some widgets are type-ahead: type the value and take the first suggestion.
    try:
        await page.keyboard.type(str(value), delay=40)
        await page.wait_for_timeout(1200)
        option = page.locator('[role=option]').first
        await option.wait_for(state='visible', timeout=2500)
        await option.click(timeout=3000)
        return True
    except Exception:
        pass
    await page.keyboard.press('Escape')
    return False


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
        field = by_idx.get(action.get('idx')) or {}
        name = (field.get('label') or field.get('name') or f'#{action.get("idx")}')[:40]
        try:
            if kind == 'fill':
                await locator.fill(str(value), timeout=5000)
            elif kind == 'select':
                # Only a real <select> takes select_option; anything else is a
                # widget that has to be opened and clicked.
                native = field.get('type') in ('select-one', 'select-multiple')
                picked = (await select_native(locator, value, field) if native
                          else await select_custom(page, locator, value))
                if not picked and native:
                    picked = await select_custom(page, locator, value)   # last resort
                if not picked:
                    opts = ', '.join((field.get('options') or [])[:6])
                    errors.append(f'"{name}": no option matching "{value}"'
                                  + (f' (options: {opts})' if opts else ''))
                    continue
            elif kind == 'check':
                if str(value).lower() in ('true', 'yes', '1'):
                    await tick_box(page, locator, field)
            else:
                continue
            filled += 1
            await page.wait_for_timeout(150)
        except Exception as e:
            reason = str(e).strip().split('\n')[0][:90] or type(e).__name__
            errors.append(f'"{name}" ({kind}): {reason}')
    return filled, errors


async def snap(page, job_id, tag):
    """Save a diagnostic screenshot so a failure can be understood after the fact."""
    try:
        path = os.path.join(OUTPUT_DIR, f'{job_id}_{tag}.png')
        await page.screenshot(path=path, full_page=True)
        return os.path.basename(path)
    except Exception:
        return None


async def process_job(page, client, profile, job, auto_submit=False):
    """Fill one application. Returns (new_status, note)."""
    job_id, title, company, link, description = job

    cv_path, cl_path, folder = application_files(job_id)
    if not cv_path:
        return 'failed', 'No generated CV/cover letter found for this job - regenerate the assets first.'

    target, mode = await find_apply_url(page, link, job_id)
    if not target and mode == 'easy_apply_manual':
        return 'ready_to_submit', (
            'This is a LinkedIn Easy Apply posting. Easy Apply does not drive reliably '
            'under automation and repeatedly forcing it risks the LinkedIn session the '
            'scraper depends on, so nothing was attempted.\n'
            f'It takes about three clicks by hand: {link}\n'
            f'Your tailored CV and cover letter are in: {folder}'
        )
    if not target:
        try:
            listing_text = await page.evaluate('() => document.body.innerText.slice(0, 4000)')
        except Exception:
            listing_text = ''
        if CLOSED_LISTING_PATTERNS.search(listing_text):
            return 'failed', 'The listing is closed - LinkedIn shows "No longer accepting applications".'
        return 'failed', (
            'Could not find an Apply button on the LinkedIn posting. See '
            f'{job_id}_apply_lookup_failure.png in the applications folder.'
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
                allowed = (profile.get('policies') or {}).get('allow_account_creation', False)
                if allowed and not account_attempted:
                    account_attempted = True
                    ok, account_note = await create_or_signin_account(
                        target, client, profile, job_id)
                    print(f'  -> account: {account_note}', flush=True)
                    if ok:
                        await dismiss_cookie_banner(target)
                        await target.wait_for_timeout(2000)
                        continue
                    return 'account_required', (
                        f'{account_note}\nApply at: {apply_url}\nDocuments ready in: {folder}')
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
            shot = await snap(target, job_id, 'no_form')
            return 'failed', (
                f'Reached {apply_url} but could not get to an application form - '
                f'nothing was filled in.{iframe_note}'
                + (f'\nWhat the page looked like: {shot}' if shot else '')
                + f'\nApply manually; documents are in: {folder}'
            )
        target = advanced
    else:
        shot = await snap(target, job_id, 'no_form')
        return 'failed', (
            f'Could not reach an application form from {apply_url} - nothing was filled in.'
            + (f'\nWhat the page looked like: {shot}' if shot else '')
            + f'\nApply manually; documents are in: {folder}'
        )

    reference_letter = (profile.get('employment') or {}).get('reference_letter')
    reference_path = os.path.join(CVS_DIR, reference_letter) if reference_letter else None
    if reference_path and not os.path.exists(reference_path):
        reference_path = None

    step_lines, reached_submit, submitted = await fill_wizard(
        target, client, profile, job, cv_path, cl_path, reference_path, folder,
        auto_submit=auto_submit,
    )

    header = ('APPLICATION SUBMITTED via ' + apply_url) if submitted else \
             ('Application filled but NOT submitted. Review and submit here: ' + apply_url)
    lines = [header]
    if account_note:
        lines.append(account_note)
    lines.extend(step_lines)
    lines.append(f'\nScreenshots of each step are in: {folder}')
    if not reached_submit:
        lines.append(
            'NOTE: the run did not reach a page with a Submit control, so the application '
            'may be incomplete - check it before submitting.'
        )

    return ('applied' if submitted else 'ready_to_submit'), '\n'.join(lines)


RUN_LOCK = '/tmp/jobscraper_applier.lock'


def acquire_run_lock():
    """Refuse to start a second applier: two runs fight over the browser and DB."""
    if os.path.exists(RUN_LOCK):
        try:
            pid = int(open(RUN_LOCK).read().strip())
            os.kill(pid, 0)          # raises if that process is gone
            return False
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass                      # stale lock from a crashed run
    with open(RUN_LOCK, 'w') as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(release_run_lock)   # also releases if the run crashes
    return True


def release_run_lock():
    try:
        if os.path.exists(RUN_LOCK) and open(RUN_LOCK).read().strip() == str(os.getpid()):
            os.unlink(RUN_LOCK)
    except OSError:
        pass


def available_memory_mb():
    """MemAvailable in MB, or None if it cannot be read."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def release_profile_lock():
    """Close a browser left open by an earlier run.

    Runs hold the browser open so filled forms can be reviewed over VNC, which
    keeps the Chrome profile locked. A new run supersedes that review session,
    so reclaim the profile rather than failing to launch.
    """
    import subprocess
    profile = os.path.abspath(CHROME_PROFILE_DIR)
    try:
        out = subprocess.run(['pgrep', '-f', f'--user-data-dir={profile}'],
                             capture_output=True, text=True).stdout.split()
    except Exception:
        return
    if not out:
        return
    print(f'Closing {len(out)} browser process(es) left over from an earlier run...')
    for pid in out:
        try:
            os.kill(int(pid), 15)
        except Exception:
            pass
    import time
    time.sleep(4)
    for pid in out:
        try:
            os.kill(int(pid), 9)
        except (ProcessLookupError, ValueError):
            pass
        except Exception:
            pass
    # Chromium leaves these behind when killed, and refuses to start with them.
    for lock in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        try:
            os.unlink(os.path.join(profile, lock))
        except OSError:
            pass
    time.sleep(1)


async def run_applications(limit=5, job_ids=None, auto_submit=False, include_blocked=False):
    """Fill applications for approved jobs. Returns a per-job result list."""
    if not acquire_run_lock():
        print('Another application run is already in progress; not starting a second one.')
        return []

    db.init_db()
    profile = load_profile()
    client = get_gemini_client()
    if not client:
        release_run_lock()
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
        statuses = '"approved", "account_required"' if include_blocked else '"approved"'
        cursor.execute(
            'SELECT job_id, title, company, link, description FROM jobs '
            f'WHERE status IN ({statuses}) ORDER BY score DESC LIMIT ?',
            (limit,),
        )
    jobs = cursor.fetchall()
    conn.close()

    if not jobs:
        print('No approved jobs to apply for.')
        release_run_lock()
        return []

    results = []
    review_tabs = []
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    release_profile_lock()
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE_DIR,
            headless=False,
            viewport={'width': 1440, 'height': 900},
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        for index, job in enumerate(jobs, start=1):
            if progress_tracker.is_stop_requested():
                print('Stop requested; halting applications.', flush=True)
                break
            free_mb = available_memory_mb()
            if free_mb is not None and free_mb < MIN_FREE_MB:
                print(f'Only {free_mb}MB RAM available; stopping the batch so other '
                      f'services on this host keep running.', flush=True)
                break
            job_id, title, company = job[0], job[1], job[2]
            progress_tracker.set_status(f'Applying: {company}', index, len(jobs))
            print(f'[{index}/{len(jobs)}] {company} - {title}')

            db.update_job_status(job_id, 'applying')
            try:
                status, note = await process_job(page, client, profile, job, auto_submit)
            except Exception as e:
                status, note = 'failed', f'Unexpected error while applying: {type(e).__name__}: {e}'

            db.update_job_status(job_id, status)
            db.add_job_note(job_id, note)
            results.append({'job_id': job_id, 'company': company, 'status': status})
            print(f'  -> {status}', flush=True)

            # Only forms still awaiting a human stay open, and only a few: each
            # extra tab costs ~150MB of Chromium, and this box shares its memory
            # with other services. Everything else is closed straight away.
            if status == 'ready_to_submit' and len(review_tabs) < MAX_OPEN_REVIEW_TABS:
                review_tabs.extend(t for t in browser.pages[1:] if t not in review_tabs)
            for extra in browser.pages[1:]:
                if extra in review_tabs:
                    continue
                try:
                    await extra.close()
                except Exception:
                    pass
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
    release_run_lock()
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
    parser.add_argument('--include-blocked', action='store_true',
                        help='Also retry jobs parked in Account Required')
    parser.add_argument('--submit', action='store_true',
                        help='Submit applications that pass every pre-submit check')
    args = parser.parse_args()

    asyncio.run(run_applications(limit=args.limit, job_ids=args.job_id,
                                 auto_submit=args.submit, include_blocked=args.include_blocked))
