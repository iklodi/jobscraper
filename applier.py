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
from collections import Counter
import json
import os
import random
import re
import secrets
import string
import time
import urllib.parse

import yaml
from dotenv import load_dotenv
from google import genai
from google.genai import types
from playwright.async_api import async_playwright

import db
import notifier
import progress_tracker
import infomaniak
import mode

# Not override=True: an explicitly exported variable must beat the .env file,
# otherwise per-run settings passed on the command line are silently ignored.
load_dotenv()

# This host also runs other services, so a long batch must not accumulate tabs.
# 0 means keep every filled form open. A closed form is a lost one - nothing
# persists the values - so capping this silently discards work the run has
# already done. Set it on a host that shares its memory (each tab is ~150MB).
MAX_OPEN_REVIEW_TABS = int(os.environ.get('APPLY_MAX_OPEN_TABS', '0'))
MIN_FREE_MB = int(os.environ.get('APPLY_MIN_FREE_MB', '400'))


# Every wait in this file goes through here. A form filled on a metronome -
# identical gaps between every click, to the millisecond - is one of the
# cheapest automation signals a site can look for, and the applier drives the
# same logged-in LinkedIn session the scraper depends on. The spread is
# multiplicative so a long wait varies more than a short one, and the floor
# keeps a jittered wait from ever being shorter than the page needs.
PACE = float(os.environ.get('APPLY_PACE', '1.0'))


async def pause(page, ms, spread=0.35):
    """Wait roughly `ms`, never exactly `ms`."""
    factor = random.uniform(1 - spread, 1 + spread)
    await page.wait_for_timeout(max(250, int(ms * factor * PACE)))

CHROME_PROFILE_DIR = mode.PROFILE_DIR
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
        // An upload widget's wrapper says "Choose file" and lists accepted
        // formats - neither is the question. These inputs usually carry it on
        // name= instead ("Resume", "Diplomas & Certificates"), so prefer that.
        if (el.type === 'file') {
            const n = (el.name || '').trim();
            if (n && /[a-z]{3}/i.test(n) && !/^input[_-]/i.test(n)) return n;
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
        // visibility heuristics below would wrongly discard them. So are radios
        // and checkboxes: nearly every design system sets opacity:0 on the real
        // input and paints a <label> on top, which is exactly what tick_box
        // exists to click. Discarding those loses whole questions - a Title
        // radio group went unanswered and unreported because of this.
        if (el.type === 'file') return false;
        if (el.type === 'radio' || el.type === 'checkbox') {
            const lab = el.labels && el.labels[0];
            if (lab && lab.getBoundingClientRect().width > 0) return false;
        }
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
            // Raw surroundings. Heuristics cannot reliably turn a styled widget
            // into a question - an upload wrapper says "Choose file" and lists
            // accepted formats - so hand the model the text near the field and
            // let it decide what is being asked.
            context: (() => {
                let box = el.closest('div,fieldset,section,li') || el.parentElement;
                for (let hop = 0; box && hop < 3; hop += 1) {
                    const t = (box.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (t.length > 3) return t.slice(0, 300);
                    box = box.parentElement;
                }
                return '';
            })(),
        };
        if (el.tagName.toLowerCase() === 'select') {
            entry.options = Array.from(el.options).map((o) => o.text.trim()).filter(Boolean);
        }
        if (el.type === 'radio' || el.type === 'checkbox') {
            // The option's own label is "Yes"/"No"; the question lives on the group.
            const siblings = el.name
                ? Array.from(root.querySelectorAll(
                    el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]'))
                : [el];
            let grp = el.parentElement;
            while (grp && grp !== document.body
                   && !siblings.every((s) => grp.contains(s))) grp = grp.parentElement;
            const explicit = el.closest('[role=radiogroup],[role=group]');
            const lg = grp ? grp.querySelector('legend') : null;
            let t = (lg ? lg.innerText
                        : (explicit && explicit.getAttribute('aria-label')) || '').trim();
            if (!t && grp) {
                // The question is the line just above the options, so read the
                // text that precedes the first one rather than the first line
                // of a container that may hold half the form.
                const opts = new Set(siblings
                    .map((s) => s.labels && s.labels[0] ? s.labels[0].innerText.trim() : '')
                    .filter(Boolean));
                let node = grp.previousElementSibling;
                while (node && !(node.innerText || '').trim()) node = node.previousElementSibling;
                if (node) {
                    const lines = (node.innerText || '').trim().split('\\n')
                        .map((x) => x.trim()).filter((x) => x && !opts.has(x));
                    if (lines.length) t = lines[lines.length - 1];
                }
                if (!t) {
                    const lines = (grp.innerText || '').trim().split('\\n')
                        .map((x) => x.trim()).filter((x) => x && !opts.has(x));
                    if (lines.length) t = lines[0];
                }
            }
            if (t) entry.group = t.slice(0, 200);
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
  "uploads": [
    {{"idx": 7, "document": "cv|cover_letter|diplomas|reference_letter|none"}}
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
- Skip file inputs entirely in "actions" (action "skip"); route them in "uploads" instead.

COVER LETTER TEXT BOXES:
{cover_letter_rule}

CHOOSING A DOCUMENT FOR EACH FILE INPUT ("uploads"):
Every file field gets one entry. Available documents:
{documents}
Read the field's "label" and "context" together - an upload widget's own text
is "Choose file" and a list of accepted formats, so the question is usually in
"label" or "name". Match on meaning, not wording: "Diplomas & Certificates",
"Diplomes", "Zeugnisse" and "Qualifications" all want the diplomas document,
while "Reference" or "Recommendation" wants the reference letter. Use "none"
when no available document fits - never send a document the field did not ask
for.
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


SUBMIT_POLICY_KEY = 'never_submit_without_review'


def submit_blocked_by_profile(profile=None):
    """True when profile.yaml forbids sending an application without review.

    policies.never_submit_without_review: true turns every run into a fill:
    forms are completed and left for a person to send. Enforced here, in the
    applier itself, so the dashboard and `--submit` on the command line obey it
    alike.
    """
    if profile is None:
        try:
            profile = load_profile()
        except Exception:
            return False
    return bool((profile.get('policies') or {}).get(SUBMIT_POLICY_KEY, False))


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


# Infomaniak only accepts response_format json_schema, so the two prompt
# shapes above are restated here. Kept loose on `value` (a string either way)
# and on `notes`, which is free text the human reads.
ACTIONS_SCHEMA = {
    'type': 'array',
    'items': {
        'type': 'object',
        'properties': {
            'idx': {'type': 'integer'},
            'action': {'type': 'string'},
            'value': {'type': ['string', 'null']},
        },
        'required': ['idx', 'action'],
    },
}

MAPPING_SCHEMA = {
    'type': 'object',
    'properties': {
        'actions': ACTIONS_SCHEMA,
        'unanswered': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'idx': {'type': 'integer'},
                    'question': {'type': 'string'},
                    'reason': {'type': 'string'},
                },
                'required': ['question'],
            },
        },
        'uploads': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {'idx': {'type': 'integer'},
                               'document': {'type': 'string'}},
                'required': ['idx', 'document'],
            },
        },
        'page_kind': {'type': 'string'},
        'notes': {'type': ['string', 'null']},
    },
    'required': ['actions', 'page_kind'],
}

REGISTRATION_SCHEMA = {
    'type': 'object',
    'properties': {
        'actions': ACTIONS_SCHEMA,
        'page_kind': {'type': 'string'},
        'submit_idx': {'type': ['integer', 'null']},
        'notes': {'type': ['string', 'null']},
    },
    'required': ['actions', 'page_kind'],
}


def ask_model(client, prompt, schema):
    """Infomaniak first (gemma by default), Gemini only if it is unreachable.

    `client` may be None: with Infomaniak configured there is nothing for
    Gemini to do, and the applier should not refuse to start over a key it
    no longer needs.
    """
    result = infomaniak.chat_json(prompt, schema)
    if result:
        return result
    if not client:
        return None
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



class Trace:
    """An ordered, numbered screenshot trail for one application.

    Every page the applier touches gets a shot, numbered in the order it
    happened, so a run can be reconstructed afterwards without guessing -
    which matters most when something submitted that should not have, or
    did not submit when it should.
    """

    def __init__(self, job_id, folder=None):
        self.job_id = job_id
        self.folder = folder or OUTPUT_DIR
        self.n = 0
        self.shots = []
        self.uploads = []
        self.clear_previous()

    def clear_previous(self):
        """Delete the last run's trail for this job.

        Re-filling a form produces a fresh numbered sequence, and leaving the
        old one behind interleaves two runs in the same folder - the dashboard
        sorts by the number in the name, so step 3 of an abandoned attempt sits
        between steps of the real one. Only this job's own .png and manifest
        are touched; the CV and cover letter are left alone.
        """
        removed = 0
        for directory in {self.folder, OUTPUT_DIR}:
            if not directory or not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                if not name.startswith(f'{self.job_id}_'):
                    continue
                if not (name.endswith('.png') or name == f'{self.job_id}_run.json'):
                    continue
                try:
                    os.remove(os.path.join(directory, name))
                    removed += 1
                except OSError as e:
                    print(f'  -> could not remove {name}: {e}')
        if removed:
            print(f'  -> cleared {removed} file(s) from the previous run')
        return removed

    def set_folder(self, folder):
        if folder:
            self.folder = folder

    async def shot(self, page, label):
        self.n += 1
        safe = re.sub(r'[^A-Za-z0-9]+', '_', label)[:32].strip('_')
        name = f'{self.job_id}_{self.n:02d}_{safe}.png'
        try:
            os.makedirs(self.folder, exist_ok=True)
            await page.screenshot(path=os.path.join(self.folder, name), full_page=True)
            self.shots.append(name)
            return name
        except Exception as e:
            self.shots.append(f'{name} (FAILED: {type(e).__name__})')
            return None

    def record_upload(self, field_label, path):
        self.uploads.append({'field': field_label or '(unlabelled file input)',
                             'file': os.path.basename(path),
                             'path': path})

    def upload_lines(self):
        if not self.uploads:
            return ['FILES UPLOADED: none - the form had no file inputs the applier could match.']
        lines = ['FILES UPLOADED:']
        for u in self.uploads:
            lines.append(f'  - {u["file"]}  ->  "{u["field"]}"')
        return lines

    def write_manifest(self, job, status, submitted):
        """A manifest beside the documents, so the folder is self-describing."""
        try:
            os.makedirs(self.folder, exist_ok=True)
            data = {
                'job_id': self.job_id,
                'title': job[1], 'company': job[2], 'linkedin': job[3],
                'run_at': datetime.datetime.now().isoformat(timespec='seconds'),
                'status': status,
                'submitted': bool(submitted),
                'uploads': self.uploads,
                'screenshots': self.shots,
            }
            with open(os.path.join(self.folder, f'{self.job_id}_run.json'), 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f'  -> Could not write the run manifest: {e}')

EASY_APPLY_LABEL = re.compile(r'\beasy apply\b', re.I)


async def is_easy_apply(page):
    """True when the posting's apply control is LinkedIn Easy Apply.

    Read from the accessible name only - nothing is clicked. Easy Apply is not
    automated at all: it runs inside LinkedIn on the candidate's own session,
    which is the riskiest thing this tool could do with that account, to save
    about three clicks.
    """
    for role in ('button', 'link'):
        control = page.get_by_role(role, name=EASY_APPLY_LABEL).first
        try:
            await control.wait_for(state='visible', timeout=4000)
            return True
        except Exception:
            continue
    return False


async def find_apply_url(page, linkedin_url, job_id='unknown', trace=None, retry=False):
    """Open the LinkedIn posting and follow its apply link to the employer's form.

    Returns (page, mode). mode 'easy_apply' means the posting only takes Easy
    Apply: nothing was clicked and the job is left for the candidate.
    """
    await page.goto(linkedin_url, timeout=60000)
    await pause(page, 5000)
    if trace:
        await trace.shot(page, 'linkedin_posting')

    if await is_easy_apply(page):
        return None, 'easy_apply'

    # LinkedIn ships obfuscated class names and renders the apply control as a
    # button, a link, or a div with role=button depending on the posting, so go
    # through the accessibility tree rather than CSS. Anchored first, then
    # loose: the accessible name varies ("Apply", "Apply on company website",
    # sometimes prefixed by icon text). Easy Apply was ruled out above.
    strict_re = re.compile(r'^\s*apply\b', re.I)
    loose_re = re.compile(r'\bapply\b', re.I)
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
            if EASY_APPLY_LABEL.search((await button.inner_text()) or ''):
                return None, 'easy_apply'       # belt and braces: never click it
        except Exception:
            continue

        # On many postings the apply control is an <a> whose click does nothing
        # under automation. Navigating to its href directly is more reliable -
        # but only for links that leave LinkedIn.
        try:
            href = await button.get_attribute('href')
        except Exception:
            href = None
        if href and href.startswith('http') and 'linkedin.com' not in urllib.parse.urlparse(href).netloc.lower():
            try:
                await page.goto(href, timeout=60000)
                await pause(page, 4000)
                return page, 'external_link'
            except Exception:
                pass

        # Click, then verify something actually happened: a new tab or a new
        # URL. A click that changes nothing means try the next candidate.
        before_url = page.url
        before_pages = len(page.context.pages)
        try:
            await button.click(timeout=8000)
        except Exception:
            continue
        for attempt in range(12):                # up to ~12s for a reaction
            await pause(page, 1000)
            if attempt == 4 and len(page.context.pages) == before_pages and page.url == before_url:
                # Some controls ignore a synthesised click; the DOM method still
                # runs the site's own handler.
                try:
                    await button.evaluate('el => el.click()')
                except Exception:
                    pass
            if len(page.context.pages) > before_pages:
                new_page = page.context.pages[-1]
                try:
                    await new_page.wait_for_load_state('domcontentloaded', timeout=30000)
                except Exception:
                    pass
                await pause(new_page, 2000)
                return new_page, 'external'
            if page.url != before_url:
                await pause(page, 3000)
                return page, 'same_tab'

    # Nothing reacted. The control is sometimes just late wiring up its handler,
    # so reload once and give it a slower second pass.
    if not retry:
        try:
            await page.reload(timeout=60000)
            await pause(page, 7000)
            return await find_apply_url(page, linkedin_url, job_id, trace, retry=True)
        except Exception:
            pass

    if trace:
        await trace.shot(page, 'apply_lookup_failure')
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


async def click_sign_in_tab(page):
    """Switch to the sign-in form when we already hold credentials for this site."""
    sign_in_re = re.compile(r'^(sign in|log ?in)$|already have an account', re.I)
    for role in ('link', 'button'):
        try:
            control = page.get_by_role(role, name=sign_in_re).last
            await control.wait_for(state='visible', timeout=3000)
            await control.click(timeout=3000)
            await pause(page, 2500)
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
            await pause(page, 2500)
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
                await pause(page, 5000)
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
            await pause(page, 6000)
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
            await pause(page, 5000)
            after = await page.evaluate(COLLECT_FIELDS_JS)
            if re.search(r'something went wrong|please refresh', after['text'], re.I):
                await page.reload(timeout=45000)
                await pause(page, 5000)
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

    plan = ask_model(client, REGISTRATION_PROMPT.format(
        full_name=identity.get('full_name', ''),
        first_name=identity.get('first_name', ''),
        last_name=identity.get('last_name', ''),
        email=email, phone=identity.get('phone', ''),
        country=identity.get('country', ''),
        password=password,
        fields=json.dumps(fields, ensure_ascii=False)[:15000],
        page_text=page_text[:2500],
    ), REGISTRATION_SCHEMA)
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

    await pause(page, 4000)
    after = await page.evaluate(COLLECT_FIELDS_JS)

    # Workday in particular likes to land on a transient "Something went wrong,
    # please refresh" screen straight after a successful sign-up.
    if re.search(r'something went wrong|please refresh the page', after['text'], re.I):
        try:
            await page.reload(timeout=45000)
            await pause(page, 5000)
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
# "Review" is LinkedIn Easy Apply's last step before Submit - without it the
# wizard stalls one click short and reports the page as the end of the form.
NEXT_LABELS = re.compile(
    r'^(save and continue|save & continue|continue|next|next step|save and next|'
    r'review|review your application|weiter|suivant|continuer|'
    r'\u00fcberpr\u00fcfen|v\u00e9rifier)\b', re.I)
SUBMIT_LABELS = re.compile(
    r'^(submit|submit application|send application|send|finish|complete application|'
    r'envoyer|absenden)\b', re.I)


async def form_scope(page):
    """The open form dialog, or the whole page when there is none.

    Fields are collected from the dialog, so controls must come from it too.
    A LinkedIn job page behind an Easy Apply modal carries its own buttons -
    the messaging widget alone offers "Send" - and matching those makes the
    wizard think it has reached a submit page while the real form sits
    untouched behind the overlay.
    """
    try:
        handles = await page.query_selector_all('[role=dialog]')
        for handle in reversed(handles):
            box = await handle.bounding_box()
            if not box or box['width'] < 200 or box['height'] < 150:
                continue
            if await handle.query_selector('input, select, textarea, button'):
                return page.locator('[role=dialog]').nth(handles.index(handle))
    except Exception:
        pass
    return page


async def find_control(page, pattern, prefer_last=True):
    """Return a visible button/link matching the label, or None.

    Scoped to the open dialog when there is one, so a control on the page
    behind a modal is never mistaken for part of the form.
    """
    # With a dialog open, only the dialog counts: falling back to the page would
    # find exactly the controls this scoping exists to avoid, e.g. the messaging
    # widget's "Send" taken for a submit button.
    where = await form_scope(page)
    for role in ('button', 'link'):
        matches = where.get_by_role(role, name=pattern)
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
        await pause(page, 4500)
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
    await pause(page, 7000)
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


async def fill_wizard(page, client, profile, job, cv_path, cl_path, attachments, folder,
                      max_steps=10, auto_submit=False, trace=None):
    """Fill an application, walking multi-step wizards, and stop before submitting.

    Returns (summary_lines, reached_submit).
    """
    job_id, title, company, _link, description = job
    # The generated CV and cover letter are attachments like any other. Merging
    # them here, once, is what keeps the menu the model chooses from and the
    # files actually available to upload from drifting apart - when they did,
    # the model was told the CV did not exist and answered "none" for it.
    attachments = dict(attachments or {})
    attachments.setdefault('cv', cv_path)
    attachments.setdefault('cover_letter', cl_path)
    attachments = {k: v for k, v in attachments.items() if v}
    # Read once: a form may ask for the letter as prose on any step.
    cover_letter = cover_letter_body(folder, cl_path)

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
            plan = ask_model(client, MAPPING_PROMPT.format(
                profile=yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
                title=title, company=company,
                description=(description or '')[:3000],
                fields=json.dumps(fields, ensure_ascii=False)[:20000],
                page_text=page_text[:3000],
                documents=describe_documents(attachments),
                cover_letter_rule=cover_letter_rule(fields, cover_letter),
            ), MAPPING_SCHEMA)
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
                filled, errors = await apply_actions(page, plan.get('actions', []),
                                                     fields, cover_letter)
                unanswered.extend(plan.get('unanswered') or [])
                if errors:
                    all_errors.extend(errors)
                    lines.append(f'Step {step} ({step_name}): fields that refused input - {"; ".join(errors)}')

        uploaded, skipped_uploads = await upload_documents(
            page, fields, cv_path, cl_path, attachments, trace, (plan or {}).get('uploads'))
        uploaded_all.extend(uploaded)
        if skipped_uploads:
            all_errors.append('required upload(s) left empty: ' + '; '.join(skipped_uploads))
            lines.append(f'Step {step}: no document matched required upload(s) - '
                         + '; '.join(skipped_uploads))

        shot = await trace.shot(page, f'step{step}_{step_name}') if trace else None

        summary = f'Step {step} ({step_name}): filled {filled} field(s)'
        if uploaded:
            summary += f', uploaded {", ".join(uploaded)}'
        if shot:
            summary += f' [{shot}]'
        lines.append(summary + '.')

        # Stop at the final step rather than sending the application.
        if await find_control(page, SUBMIT_LABELS):
            reached_submit = True
            if trace:
                await trace.shot(page, f'step{step}_before_submit')
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
                if trace:
                    await trace.shot(page, 'submitted' if ok else 'submit_failed')
            break

        if not await advance_step(page):
            # A step with no fields is often a chooser ("Apply Manually",
            # "Start Your Application") rather than the end of the wizard.
            advanced = await try_advance_to_form(page) if not fields else None
            if advanced is not None:
                page = advanced
                lines.append(f'Step {step}: no fields here - followed the page through to the form.')
                continue
            lines.append('No "Save and Continue" control found, so this looks like the last page.')
            break
    else:
        lines.append(f'Stopped after {max_steps} steps without reaching a submit page.')

    if unanswered:
        lines.append(f'\n{UNANSWERED_HEADER} (left blank):')
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
                await pause(page, 1200)
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
            fields_before = await page.evaluate(
                '() => document.querySelectorAll("input,select,textarea").length')
            try:
                async with page.context.expect_page(timeout=8000) as new_page_info:
                    await control.click(timeout=5000)
                new_page = await new_page_info.value
                await new_page.wait_for_load_state('domcontentloaded', timeout=30000)
                await pause(new_page, 2500)
                return new_page
            except Exception:
                # No new tab: a same-tab navigation or an in-page reveal - or
                # nothing at all, in which case try the next candidate rather
                # than reporting that the wizard moved on.
                await pause(page, 3500)
                fields_after = await page.evaluate(
                    '() => document.querySelectorAll("input,select,textarea").length')
                if page.url != before or fields_after != fields_before:
                    return page
    return None


# Which stored document answers which upload field, most specific first.
#
# Two things drive the order. "Diplomas & Certificates" contains the word
# "certificate", so it has to be tested before any reference rule that also
# matches it. And the diplomas PDF is a bundle - diplomas plus a work
# certificate - so it is the better answer to a vague "additional documents"
# slot than the standalone reference letter, which it already contains.
# Only a field that names a reference or recommendation gets that letter on
# its own.
ATTACHMENT_RULES = [
    ('cover_letter', r'cover|motivation|lettre de motivation|anschreiben'),
    ('cv', r'\bcv\b|resume|resum|lebenslauf|curriculum'),
    ('diplomas', r'diplom|degree|certificat|zeugnis|qualification|transcript|'
                 r'attestation|education document'),
    ('reference_letter', r'reference|recommendation|referenz|empfehlung'),
    ('diplomas', r'additional|other document|supporting|attachment|annexe'),
]


def attachment_paths(profile):
    """Resolve the profile's named attachments to absolute paths.

    Reads profile["attachments"], falling back to the older
    employment.reference_letter so an un-migrated profile still works.
    """
    configured = dict(profile.get('attachments') or {})
    legacy = (profile.get('employment') or {}).get('reference_letter')
    if legacy and 'reference_letter' not in configured:
        configured['reference_letter'] = legacy

    resolved = {}
    for key, rel in configured.items():
        if not rel:
            continue
        path = rel if os.path.isabs(rel) else os.path.join(CVS_DIR, rel)
        if os.path.exists(path):
            resolved[key] = path
        else:
            print(f'  -> profile attachment "{key}" not found at {path}')
    return resolved


DOCUMENT_BLURBS = {
    'cv': 'the tailored CV / resume for this job',
    'cover_letter': 'the tailored cover letter for this job',
    'diplomas': 'university diplomas and work certificates, in one PDF',
    'reference_letter': 'a written reference letter from a former employer',
}


def describe_documents(attachments):
    """The document menu the model picks from, limited to what actually exists."""
    lines = []
    for key in ('cv', 'cover_letter', 'diplomas', 'reference_letter'):
        if attachments.get(key):
            lines.append(f'  - "{key}": {DOCUMENT_BLURBS[key]}')
    for key, path in sorted(attachments.items()):
        if key not in DOCUMENT_BLURBS and path:
            lines.append(f'  - "{key}": {os.path.basename(path)}')
    return '\n'.join(lines) or '  (none configured)'


async def upload_documents(page, fields, cv_path, cl_path, attachments=None, trace=None,
                           uploads=None):
    """Attach the right stored document to each file input on the page.

    Records what went where on `trace`, so the note and the manifest can say
    which file answered which field rather than just how many were sent.
    """
    attachments = dict(attachments or {})
    attachments.setdefault('cv', cv_path)
    attachments.setdefault('cover_letter', cl_path)
    routing = {u['idx']: u.get('document') for u in (uploads or []) if 'idx' in u}
    skipped = []

    uploaded = []
    for field in fields:
        if field.get('type') != 'file':
            continue
        haystack = ' '.join(
            [field.get('label', ''), field.get('name', ''), field.get('id', '')]
        ).lower()
        target = None
        chosen = routing.get(field['idx'])
        if chosen == 'none':
            continue
        if chosen:
            target = attachments.get(chosen)
            if not target:
                print(f'  -> model asked for "{chosen}" but no such attachment is '
                      f'configured; falling back to the label rules')
        if not target:
            # No routing, or a document the profile does not have: fall back to
            # matching the label, which is deterministic and needs no model.
            for key, pattern in ATTACHMENT_RULES:
                if re.search(pattern, haystack):
                    target = attachments.get(key)
                    if not target:
                        print(f'  -> no "{key}" attachment configured for field '
                              f'"{(field.get("label") or "")[:50]}"')
                    break
            else:
                # An unlabelled first file input is almost always the CV.
                if not uploaded:
                    target = attachments.get('cv')
        if not target or not os.path.exists(target):
            if field.get('required'):
                skipped.append(field.get('label') or field.get('name') or f'#{field["idx"]}')
            continue
        try:
            await page.locator(f'[data-jsapply="{field["idx"]}"]').set_input_files(target)
            uploaded.append(os.path.basename(target))
            if trace:
                trace.record_upload(field.get('label') or field.get('name') or field.get('id'),
                                    target)
            await pause(page, 1500)
        except Exception as e:
            print(f"  -> Upload failed for field {field['idx']}: {e}")
    if skipped:
        # A required upload left empty blocks the application, so it must not
        # pass quietly - the pre-submit gate refuses to send in this state.
        print('  -> WARNING: required upload(s) with no document: ' + '; '.join(skipped))
    return uploaded, skipped


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
    # No blind force-click here. A forced click on a hidden input is delivered
    # at its coordinates regardless of what is painted on top, so on a modal
    # wizard it lands on whatever covers it - which on LinkedIn Easy Apply is
    # the Next button. That silently advanced the form past the resume step,
    # and the application went out with the wrong CV attached.
    # Setting the property and announcing it is equivalent for the page and
    # cannot hit another control.
    # el.click() is the DOM method, not a pointer event: it fires the page's
    # own handler without hit-testing, so React sees a real click and nothing
    # painted on top can intercept it. Setting el.checked directly is not
    # enough - React re-renders from its own state and drops it.
    try:
        await locator.evaluate('el => el.click()')
        await page.wait_for_timeout(300)
        if await locator.is_checked():
            return
    except Exception:
        pass
    # Last resort: the styled label, clicked the same way.
    try:
        if field_id:
            await page.locator(f'label[for="{field_id}"]').first.evaluate('el => el.click()')
            await page.wait_for_timeout(300)
            if await locator.is_checked():
                return
    except Exception:
        pass
    raise RuntimeError('could not tick it without clicking through the page')


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
    await pause(page, 900)

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
                await pause(page, 600)
                return True
            except Exception:
                continue

    # Some widgets are type-ahead: type the value and take the first suggestion.
    try:
        await page.keyboard.type(str(value), delay=40)
        await pause(page, 1200)
        option = page.locator('[role=option]').first
        await option.wait_for(state='visible', timeout=2500)
        await option.click(timeout=3000)
        return True
    except Exception:
        pass
    await page.keyboard.press('Escape')
    return False


FIELD_INVALID_JS = """
(el) => {
    if (!el) return null;
    // The browser's own verdict first, then the patterns component libraries
    // use: aria-invalid, an error class, or an error node tied by aria-describedby.
    if (el.willValidate && !el.checkValidity()) return el.validationMessage || 'invalid';
    if (el.getAttribute('aria-invalid') === 'true') return 'aria-invalid';
    const described = el.getAttribute('aria-describedby');
    if (described) {
        for (const id of described.split(/\\s+/)) {
            const n = document.getElementById(id);
            if (n && n.offsetParent !== null && /invalid|not valid|incorrect|erreur|ung.ltig/i.test(n.innerText || ''))
                return (n.innerText || '').trim().slice(0, 120);
        }
    }
    // Otherwise look just below the field for a freshly rendered error line.
    const wrap = el.closest('div, fieldset, label') || el.parentElement;
    if (wrap) {
        for (const n of wrap.querySelectorAll('[class*="error"], [class*="invalid"], [role="alert"]')) {
            const t = (n.innerText || '').trim();
            if (t && n.offsetParent !== null) return t.slice(0, 120);
        }
    }
    return null;
}
"""


def phone_variants(value):
    """The same number in the shapes forms actually accept.

    A validator that rejects "+41 12 345 67 89" usually wants E.164 with no
    spaces, and a few insist on the national form. Ordered most standard first.
    """
    raw = str(value or '').strip()
    digits = re.sub(r'[^\d+]', '', raw)
    out = [raw, digits]
    if digits.startswith('+'):
        body = digits[1:]
        out.append('00' + body)
        # Swiss numbers: +41 79... -> 079...
        if body.startswith('41') and len(body) > 2:
            out.append('0' + body[2:])
    elif digits.startswith('00'):
        out.append('+' + digits[2:])
    elif digits.startswith('0'):
        out.append('+41' + digits[1:])
    seen, uniq = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def looks_like_phone(field):
    haystack = ' '.join([str(field.get('label') or ''), str(field.get('name') or ''),
                         str(field.get('id') or ''), str(field.get('type') or '')]).lower()
    return bool(re.search(r'\btel\b|phone|mobile|telefon|t.l.phone|handy|natel', haystack))


async def fill_checked(page, locator, value, field):
    """Fill a field and make sure the page accepted it.

    Phone numbers are the recurring offender: the profile stores one readable
    form and each validator wants a different one, so try the alternatives
    rather than leaving a required field flagged red and the application stuck.
    Returns (value_used, error_message_or_None).
    """
    candidates = phone_variants(value) if looks_like_phone(field) else [str(value)]
    problem = None
    for candidate in candidates:
        await locator.fill(candidate, timeout=5000)
        await pause(page, 400)
        try:
            problem = await locator.evaluate(FIELD_INVALID_JS)
        except Exception:
            problem = None
        if not problem:
            return candidate, None
    return candidates[-1], problem


COVER_LETTER_MARKER = '<COVER_LETTER>'
SALUTATION = re.compile(
    r'^\s*(dear\b|to whom it may concern|madame|monsieur|mesdames|messieurs|'
    r'ch[e\u00e8]re?s?\b|sehr geehrte|gentile|egregio|estimad[oa])', re.I)


def cover_letter_body(folder, cl_path=None):
    """The letter as prose, from the salutation down.

    A form's cover-letter box wants the letter, not the letterhead: the
    recipient block and date above "Dear ..." are page furniture that reads
    as noise when pasted into a textarea.
    """
    candidates = []
    if folder and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if 'coverletter' in name.lower().replace('_', '').replace(' ', ''):
                if name.lower().endswith(('.docx', '.md', '.txt')):
                    candidates.append(os.path.join(folder, name))
    if cl_path:
        candidates.append(cl_path)

    for path in candidates:
        try:
            if path.lower().endswith('.docx'):
                from docx import Document
                lines = []
                for para in Document(path).paragraphs:
                    lines.extend(para.text.split('\n'))
            elif path.lower().endswith(('.md', '.txt')):
                lines = open(path, encoding='utf-8').read().splitlines()
            else:
                continue
        except Exception as e:
            print(f'  -> could not read {os.path.basename(path)}: {e}')
            continue

        lines = [l.rstrip() for l in lines]
        start = next((i for i, l in enumerate(lines) if SALUTATION.match(l)), None)
        if start is None:
            continue
        body = '\n'.join(lines[start:]).strip()
        # Collapse the blank runs a .docx leaves behind, keeping paragraphs.
        body = re.sub(r'\n{3,}', '\n\n', body)
        if len(body) > 80:
            return body
    return None


def cover_letter_rule(fields, cover_letter):
    """What to tell the model about pasting the letter into a text box."""
    if not cover_letter:
        return ('- No cover letter text is available, so never claim to paste one; '
                'answer motivation questions from the profile instead.')
    has_letter_upload = any(
        f.get('type') == 'file' and re.search(
            r'cover|motivation|lettre|anschreiben',
            ' '.join([str(f.get('label') or ''), str(f.get('name') or ''),
                      str(f.get('id') or '')]).lower())
        for f in fields)
    if has_letter_upload:
        return ('- This form takes the cover letter as a file upload, which is handled '
                'separately. Do not paste the letter into a text box as well.')
    return ('- This form has no file upload for a cover letter. If a free-text field asks '
            'for one - "Cover letter", "Motivation", "Message to the hiring manager", '
            '"Why do you want to work here" - fill it with the exact value ' + repr(COVER_LETTER_MARKER) +
            ' and nothing else. That placeholder is replaced with the real letter, so do '
            'not write it out, summarise it or translate it. Use it for at most one field, '
            'the one most clearly meant for a cover letter, and only when the box is large '
            'enough for prose (a textarea, not a one-line input).')


async def apply_actions(page, actions, fields=None, cover_letter=None):
    """Execute the model's fill plan; returns (filled_count, errors)."""
    by_idx = {f['idx']: f for f in (fields or [])}
    filled, errors = 0, []
    for action in actions:
        kind = action.get('action')
        if kind in (None, 'skip'):
            continue
        value = action.get('value', '')
        # The model asks for the letter by name rather than reproducing it, so
        # what lands in the box is exactly what the PDF says.
        if isinstance(value, str) and COVER_LETTER_MARKER in value:
            if not cover_letter:
                errors.append(f'"{(by_idx.get(action.get("idx")) or {}).get("label", "field")}": '
                              'asked for the cover letter but none could be read')
                continue
            value = value.replace(COVER_LETTER_MARKER, cover_letter)
        selector = f'[data-jsapply="{action.get("idx")}"]'
        locator = page.locator(selector)
        field = by_idx.get(action.get('idx')) or {}
        name = (field.get('label') or field.get('name') or f'#{action.get("idx")}')[:40]
        try:
            if kind == 'fill':
                used, problem = await fill_checked(page, locator, value, field)
                if problem:
                    errors.append(f'"{name}": the form rejected "{used}" - {problem}')
                    continue
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
            await pause(page, 150)
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

    trace = Trace(job_id, folder)
    attachments = attachment_paths(profile)
    cover_letter = cover_letter_body(folder, cl_path)
    target, mode = await find_apply_url(page, link, job_id, trace)
    if mode == 'easy_apply':
        trace.write_manifest(job, 'easy_apply', False)
        return 'easy_apply', (
            'LinkedIn Easy Apply posting - left for you. Easy Apply is not automated: it '
            'runs inside LinkedIn on your own account, which is the riskiest thing this '
            'tool could do with it. Nothing was clicked.\n'
            f'Apply here, about three clicks: {link}\n'
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
    await trace.shot(target, 'apply_page')
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
            await pause(target, 2000)
            continue

        if fields:
            plan = ask_model(client, MAPPING_PROMPT.format(
                profile=yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
                title=title, company=company,
                description=(description or '')[:4000],
                fields=json.dumps(fields, ensure_ascii=False)[:20000],
                page_text=page_text[:3000],
                documents=describe_documents(attachments),
                cover_letter_rule=cover_letter_rule(fields, cover_letter),
            ), MAPPING_SCHEMA)
            if not plan:
                return 'failed', f'Could not map the form fields (every model failed) at {apply_url}.'

            if plan.get('page_kind') == 'login_or_register':
                allowed = (profile.get('policies') or {}).get('allow_account_creation', False)
                if allowed and not account_attempted:
                    account_attempted = True
                    ok, account_note = await create_or_signin_account(
                        target, client, profile, job_id)
                    print(f'  -> account: {account_note}', flush=True)
                    if ok:
                        await dismiss_cookie_banner(target)
                        await pause(target, 2000)
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

    step_lines, reached_submit, submitted = await fill_wizard(
        target, client, profile, job, cv_path, cl_path, attachments, folder,
        auto_submit=auto_submit, trace=trace,
    )

    header = ('APPLICATION SUBMITTED via ' + apply_url) if submitted else \
             ('Application filled but NOT submitted. Review and submit here: ' + apply_url)
    lines = [header]
    if account_note:
        lines.append(account_note)
    lines.extend(step_lines)
    lines.append('')
    lines.extend(trace.upload_lines())
    lines.append(f'\nSTEP-BY-STEP SCREENSHOTS ({len(trace.shots)}) in {folder}:')
    for name in trace.shots:
        lines.append(f'  {name}')
    lines.append(f'Machine-readable record of this run: {job_id}_run.json')
    if not reached_submit:
        lines.append(
            'NOTE: the run did not reach a page with a Submit control, so the application '
            'may be incomplete - check it before submitting.'
        )

    status = 'applied' if submitted else 'ready_to_submit'
    trace.write_manifest(job, status, submitted)
    return status, '\n'.join(lines)


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


def _process_alive_ancestor(pid):
    """The nearest live Python ancestor of a process, or None if it is orphaned.

    Playwright's Chromium hangs off a node driver, which hangs off the Python
    process that launched it. If that Python process is still running, the
    browser belongs to a live run - the nightly scraper, an add-by-URL fetch,
    or a review window someone is filling in by hand.
    """
    import subprocess
    seen = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            out = subprocess.run(['ps', '-o', 'ppid=,comm=', '-p', str(pid)],
                                 capture_output=True, text=True).stdout.strip()
        except Exception:
            return None
        if not out:
            return None
        ppid, _, comm = out.partition(' ')
        if 'python' in comm.lower() and pid != os.getpid():
            return pid
        try:
            pid = int(ppid)
        except ValueError:
            return None
    return None


def release_profile_lock():
    """Reclaim the Chrome profile from a browser nothing owns any more.

    Only orphans are killed. This used to kill every browser on the profile,
    which meant starting an application run could tear down the nightly scrape
    mid-run, or an add-by-URL fetch. A browser still owned by a live run is
    left alone, and the caller is told the profile is busy.

    Returns True when the profile is free (or was freed), False when another
    live run is using it.
    """
    import subprocess
    profile = os.path.abspath(CHROME_PROFILE_DIR)
    try:
        out = subprocess.run(['pgrep', '-f', '--', f'--user-data-dir={profile}'],
                             capture_output=True, text=True).stdout.split()
    except Exception:
        return True
    pids = [int(p) for p in out if p.isdigit()]
    if not pids:
        return True
    owned = [p for p in pids if _process_alive_ancestor(p)]
    if owned:
        return False
    print(f'Closing {len(pids)} orphaned browser process(es) from an earlier run...')
    for pid in pids:
        try:
            os.kill(pid, 15)
        except Exception:
            pass
    time.sleep(4)
    for pid in pids:
        try:
            os.kill(pid, 9)
        except Exception:
            pass
    # Chromium leaves these behind when killed, and refuses to start with them.
    for lock in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        try:
            os.unlink(os.path.join(profile, lock))
        except OSError:
            pass
    time.sleep(1)
    return True


async def run_applications(limit=None, job_ids=None, auto_submit=False, include_blocked=False):
    """Fill applications for approved jobs. Returns a per-job result list.

    limit=None means every approved job. A partial batch is worse than it
    looks: a filled form that gets closed is not saved anywhere, so the job
    reads as done on the board while the work is gone.
    """
    if not acquire_run_lock():
        print('Another application run is already in progress; not starting a second one.')
        return []

    db.init_db()
    profile = load_profile()
    if auto_submit and submit_blocked_by_profile(profile):
        print(f'policies.{SUBMIT_POLICY_KEY} is true in profile.yaml: filling only, '
              f'nothing will be submitted.', flush=True)
        auto_submit = False
    client = get_gemini_client()
    if not client and not infomaniak.get_config():
        release_run_lock()
        raise RuntimeError('No model configured: set INFOMANIAK_API_TOKEN or GEMINI_API_KEY.')

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
        query = ('SELECT job_id, title, company, link, description FROM jobs '
                 f'WHERE status IN ({statuses}) ORDER BY score DESC')
        if limit:
            cursor.execute(query + ' LIMIT ?', (limit,))
        else:
            cursor.execute(query)
    jobs = cursor.fetchall()
    conn.close()

    if not jobs:
        print('No approved jobs to apply for.')
        release_run_lock()
        return []

    results = []
    review_tabs = []
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    if not release_profile_lock():
        release_run_lock()
        raise RuntimeError('The browser profile is in use by another run (the scraper or an '
                           'add-by-link fetch). Try again when it has finished.')
    async with async_playwright() as p:
        browser = await mode.open_linkedin_session(p,
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
            # Every job gets its own tab. Sharing one meant the next job's
            # goto() navigated straight over the form the last one had just
            # filled, and a form's values are not saved anywhere - so the work
            # was gone while the board still said ready_to_submit.
            before = set(browser.pages)
            job_page = await browser.new_page()
            try:
                status, note = await process_job(job_page, client, profile, job, auto_submit)
            except Exception as e:
                status, note = 'failed', f'Unexpected error while applying: {type(e).__name__}: {e}'

            db.update_job_status(job_id, status)
            db.add_job_note(job_id, note)
            results.append({'job_id': job_id, 'company': company, 'status': status,
                            'unanswered': unanswered_questions(note)})
            print(f'  -> {status}', flush=True)

            # Keep this job's tabs only while a human still has to finish the
            # form. Anything else closes straight away: each tab costs ~150MB
            # of Chromium, which matters on a host shared with other services.
            job_tabs = [p for p in browser.pages if p not in before]
            keeping = status == 'ready_to_submit' and (
                not MAX_OPEN_REVIEW_TABS or len(review_tabs) < MAX_OPEN_REVIEW_TABS)
            if keeping:
                review_tabs.extend(p for p in job_tabs if p not in review_tabs)
            else:
                for extra in job_tabs:
                    if extra in review_tabs:
                        continue
                    try:
                        await extra.close()
                    except Exception:
                        pass
            await pause(page, 3000)

        # Hold the filled forms open so a human can check and submit them.
        if any(r['status'] == 'ready_to_submit' for r in results):
            # Measured from the last time anything changed, not from the end of
            # the run. A flat timer closed the browser while the forms were
            # being filled in by hand, losing work that cannot be recovered -
            # nothing persists a half-finished form.
            idle_minutes = int(os.environ.get('APPLY_REVIEW_IDLE_MINUTES', '120'))
            print(f'\n{len(browser.pages) - 1} filled form(s) left open for review.')
            print(f'Check and submit them, then press "Close forms" on the dashboard. '
                  f'They close on their own only after {idle_minutes} min with no activity.')

            async def tab_signature():
                """What the held tabs look like right now."""
                marks = []
                for tab in browser.pages[1:]:
                    try:
                        marks.append(tab.url)
                        # The values, not just the field count: typing into a
                        # long answer changes nothing else on the page, and a
                        # signature that missed it closed the tab mid-sentence.
                        marks.append(await tab.evaluate(
                            '''() => {
                                let h = 0;
                                const s = Array.from(document.querySelectorAll(
                                    "input,select,textarea")).map(e =>
                                    e.type === "file" ? String(e.files.length)
                                    : (e.checked ? "1" : "0") + e.value).join("|")
                                    + "|" + (document.title || "");
                                for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
                                return String(h);
                            }'''))
                    except Exception:
                        marks.append('gone')
                return '\n'.join(marks)

            last_change = time.monotonic()
            previous = await tab_signature()
            while True:
                if progress_tracker.is_stop_requested():
                    print('Stop requested; closing the browser.')
                    break
                if len(browser.pages) <= 1:
                    print('All review tabs were closed by hand; finishing.')
                    break
                idle = time.monotonic() - last_change
                if idle >= idle_minutes * 60:
                    print(f'No activity for {idle_minutes} min; closing the review tabs.')
                    break
                progress_tracker.set_status(
                    f'Done - {len(results)} form(s) filled and left open. Check them, then '
                    f'press "Close forms". Closing after {idle_minutes} min idle '
                    f'({int((idle_minutes * 60 - idle) // 60)} min left).',
                    len(results), len(results), awaiting_review=True
                )
                await asyncio.sleep(5)
                current = await tab_signature()
                if current != previous:
                    previous = current
                    last_change = time.monotonic()

        await browser.close()

    progress_tracker.clear_status()
    release_run_lock()
    single = bool(job_ids) and len(jobs) == 1
    # The dashboard always says what happened. Email is for batches only:
    # filling one form at a time is something you are sat in front of, and a
    # mail per form buries the summaries that are actually worth reading.
    progress_tracker.set_result(run_summary(results, single=single))
    if not single:
        _notify(results, single=False)
    return results


STATUS_WORDS = {
    'applied': 'submitted',
    'easy_apply': 'LinkedIn Easy Apply - left for you to apply by hand',
    'ready_to_submit': 'filled, waiting for you to submit',
    'account_required': 'blocked - the employer wants an account',
    'failed': 'failed',
}


def run_summary(results, single=False):
    """One line the dashboard can show, plus the detail behind it."""
    if not results:
        return {'ok': True, 'headline': 'Nothing to apply for - no approved jobs.',
                'single': single, 'jobs': []}

    jobs = [{'job_id': r['job_id'], 'company': r['company'], 'status': r['status'],
             'word': STATUS_WORDS.get(r['status'], r['status'])} for r in results]

    if single:
        j = jobs[0]
        headline = {
            'applied': f'Applied to {j["company"]}.',
            'ready_to_submit': f'{j["company"]}: form filled, waiting for you to submit.',
            'account_required': f'{j["company"]} needs an account before the form can be filled.',
            'failed': f'{j["company"]}: could not fill the form.',
            'easy_apply': f'{j["company"]} only takes LinkedIn Easy Apply - apply by hand, about three clicks.',
        }.get(j['status'], f'{j["company"]}: {j["word"]}.')
        return {'ok': j['status'] in ('applied', 'ready_to_submit', 'easy_apply'),
                'headline': headline, 'single': True, 'jobs': jobs}

    # Say what actually happened rather than scoring the run out of ten:
    # "2 of 3 completed" called a form still waiting for a human "completed",
    # and told you nothing about what the third one did.
    counts = Counter(r['status'] for r in results)
    plural = lambda n, word: f'{n} {word}' + ('' if n == 1 else 's')
    parts = []
    if counts['applied']:
        parts.append(plural(counts['applied'], 'application') + ' submitted')
    if counts['ready_to_submit']:
        parts.append(plural(counts['ready_to_submit'], 'form') + ' filled')
    if counts['easy_apply']:
        parts.append(plural(counts['easy_apply'], 'Easy Apply job') + ' left for you')
    if counts['account_required']:
        parts.append(plural(counts['account_required'], 'job') + ' needs an account')
    if counts['failed']:
        parts.append(plural(counts['failed'], 'failure'))
    for status, n in counts.items():
        if status not in ('applied', 'ready_to_submit', 'account_required', 'failed', 'easy_apply'):
            parts.append(f'{n} {status}')

    headline = ', '.join(parts[:-1]) + (' and ' if len(parts) > 1 else '') + parts[-1]
    return {'ok': not (counts['failed'] or counts['account_required']),
            'headline': headline[0].upper() + headline[1:] + '.',
            'single': False, 'jobs': jobs}

UNANSWERED_HEADER = 'QUESTIONS THE PROFILE DOES NOT ANSWER'


def unanswered_questions(note):
    """The questions a run left blank, read back from the note fill_wizard wrote."""
    out, inside = [], False
    for line in (note or '').splitlines():
        if line.startswith(UNANSWERED_HEADER):
            inside = True
            continue
        if inside:
            if not line.startswith('  - '):
                break
            # "  - <question>  (<why the profile does not answer it>)"
            out.append(re.sub(r'\s{2}\(.*\)\s*$', '', line[4:]).strip())
    return [q for q in out if q]


def _notify(results, single=False):
    """Email a batch outcome. Single-job runs report through the dashboard
    instead - see run_applications."""
    if not results:
        return
    base = notifier.dashboard_url()
    summary = run_summary(results, single=single)

    # One job per line. A link on its own indented line inside a list item
    # renders as a sibling of the *next* item, so the mail read as if every
    # link belonged to the company below it.
    lines = [f'## {summary["headline"]}', '']
    for j in summary['jobs']:
        lines.append(f'- **{j["company"]}** - {j["word"]} '
                     f'([open]({base}/?job_id={j["job_id"]}))')
    lines.append('')

    # Questions the profile could not answer, grouped so one missing answer
    # that blocked five forms reads as one thing to fix, not five.
    asked = {}
    for r in results:
        for q in r.get('unanswered') or []:
            asked.setdefault(q, []).append(r['company'])
    if asked:
        lines.append('## Questions your profile does not answer')
        lines.append('')
        for q, companies in sorted(asked.items(), key=lambda kv: -len(kv[1])):
            lines.append(f'- {q} — *{", ".join(dict.fromkeys(companies))}*')
        lines.append('')
        lines.append('They were left blank. Add the answers to profile.yaml and future '
                     'applications will fill them in.')
        lines.append('')

    if any(j['status'] == 'ready_to_submit' for j in summary['jobs']):
        lines.append('The filled forms are open in the browser on the machine that ran this. '
                     'Check them, submit the ones you want, then press "Close forms" on '
                     'the dashboard.')
        lines.append('')
    lines.append(f'[Open the dashboard]({base})')
    notifier.send_email('AI Job Scraper - Applications processed', '\n'.join(lines))


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Fill in applications for approved jobs.')
    parser.add_argument('--limit', type=int, default=None,
                        help='Cap how many approved jobs to process (default: all)')
    parser.add_argument('--job-id', action='append', help='Apply for specific job id(s) only')
    parser.add_argument('--include-blocked', action='store_true',
                        help='Also retry jobs parked in Account Required')
    parser.add_argument('--submit', action='store_true',
                        help='Submit applications that pass every pre-submit check')
    args = parser.parse_args()

    asyncio.run(run_applications(limit=args.limit, job_ids=args.job_id,
                                 auto_submit=args.submit, include_blocked=args.include_blocked))
