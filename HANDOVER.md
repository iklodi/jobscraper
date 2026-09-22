# AI Job Application Pipeline — Handover

End-to-end automation for a job search: scrape LinkedIn, score each advert
against a candidate profile, write tailored documents for the jobs the
candidate approves, fill in the employer's form, and track it all on a local
Kanban board. State lives in SQLite (`jobs.db`, WAL mode); nothing is sent
anywhere except the employer's own site and a summary email.

## Components

### `main.py` — the nightly controller
Scrapes, scores, emails the summary. **It no longer generates documents**:
that waits for an explicit Approve on the dashboard, so nothing is written for
a job that will never be sent. Flags: `--no-scrape`, `--no-eval`, and `--gen`
to restore the old sweep over everything on To Do.

### `scraper.py`
Playwright over a persistent Chromium profile (`./chrome_profile`) holding the
logged-in LinkedIn session. Reads `search_criteria.md` for keywords, locations
and job types; a keyword may pin its own locations (`- Interim Manager @
Switzerland / France`). Job types map onto LinkedIn's `f_JT` filter. Skips
anything already in the database, and emails when the session has expired.

### `evaluate.py`
Scores each new job 1-10 against the career dossier and `rules.md`. At or above
`MIN_PASS_SCORE` the job becomes `to_apply`; below, `scored`. The threshold is
read in exactly one place (`min_pass_score()`) and injected into the prompt as
`{min_score}`, so the board threshold and the score from which the model is
asked for salary, recruiter flag, hiring manager and language cannot drift apart.

### `generator.py`
Writes the tailored CV and cover letter with `python-docx`, then converts them
with headless LibreOffice. CV content is always English; the cover letter
follows the advert's language. Also saves the advert itself as a PDF.

### `applier.py`
Walks from the LinkedIn posting to the employer's form, fills it from
`profile.yaml`, and submits when asked to. The longest and most defensive file
here — see **Applier invariants** below.

### `infomaniak.py`
Infomaniak AI Services as an OpenAI-compatible provider, tried before Gemini
everywhere. Its endpoint accepts **only** `response_format: json_schema` —
`json_object` and `text` are rejected outright — so every caller passes a
schema. Retries with jittered backoff across 408/429/5xx.

### `generate_from_url.py`
One advert by URL, for the dashboard's ＋ button. Reads the page, scores it, and
files it on To Do *whatever it scores* — a link pasted by hand is a job already
chosen, so the score informs that choice rather than overruling it.
`--generate` writes the documents immediately.

### `app.py` / `static/app.js` / `templates/index.html`
Flask dashboard. Columns: New, To Do, Approved, Ready to Submit, Applied,
Interviewing, Failed, Account Required, Rejected. Every board is ordered by last
modification, newest first. `/api/settings` edits `search_criteria.md` and
`rules.md` in place, refusing empty content and reporting what the next run will
search for.

Long-lived process, so `applier`, `generator`, `evaluate` and
`generate_from_url` are reloaded before each background run — otherwise an edit
made after start-up would never take effect.

## Statuses

| Status | Meaning |
|---|---|
| `scored` | Evaluated, below the bar. Shown on Rejected for `REJECTED_BOARD_DAYS`, then hidden — the row stays so the scraper still recognises it. |
| `to_apply` | At or above the bar. |
| `approved` | Approved by hand; documents are written on this transition. |
| `ready_to_submit` | Form filled, waiting for a human to send it. |
| `applied` | Submitted — only ever set when the applier pressed submit itself, or by hand. |
| `account_required` | The employer wants an account before the form can be filled. |
| `failed` | No apply button, closed listing, CAPTCHA, or the fields could not be mapped. |
| `generating` / `evaluating` / `applying` | Transient, while a background run holds the job. |

## Models

Infomaniak first (`google/gemma-4-31B-it` → `mistralai/Mistral-Small-4-119B`
→ `swiss-ai/Apertus-v1.5-70B`), then Gemini, then Groq.

gemma leads despite being slower. Scoring the same 395 jobs with gemma and
Mistral showed they agree on only 11%, with Mistral averaging 1.59 points high
— not a matter of taste but of rule-following: Mistral treats the hard gates in
`rules.md` as preferences. On the widest disagreements gemma was the one calling
"Location mismatch" on a Dublin hybrid, spotting a US-residents-only remote
posting from its HIPAA mandates, and catching an "Already applied" duplicate
that Mistral scored 8.

## Applier invariants

Do not weaken these. Each exists because it failed once.

- **It never invents an answer.** Anything `profile.yaml` does not cover goes
  into the job note and the summary email so it can be added there.
- **The pre-submit gate** blocks submission unless every required field is
  filled, nothing was left unanswered, and no field refused input. It has
  already prevented a CV-less submission and a half-complete one.
- **No blind force-clicks.** A forced click is delivered at the element's
  coordinates whatever is painted above it; on a modal wizard that lands on
  *Next* and silently advances the form. Checkboxes and radios are ticked with
  the DOM `click()` method, which fires the page's handler without hit-testing.
- **Controls are found inside the open dialog**, not the whole page — the
  LinkedIn page behind an Easy Apply modal offers its own "Send" button.
- **It does not solve CAPTCHAs.** That gate is deliberate.
- **Account creation is allowed** (the candidate authorised it). Generated
  passwords are written to `CVS_DIR/ats_accounts.yaml` (chmod 600) *before* the
  registration is submitted, so a crash cannot lose one.
- **Waits are jittered** (`APPLY_PACE`). Identical millisecond gaps between
  clicks are among the cheapest automation signals a site can look for, and this
  code drives the same session the scraper depends on.
- **Every page touched is screenshotted** into the application folder, numbered
  in order, alongside a `<job_id>_run.json` manifest recording which file went
  into which field. The previous trail is deleted when a job is re-filled.

### LinkedIn Easy Apply

Three things that are not obvious:

1. On a direct `/jobs/view/<id>` URL — what the scraper stores — Easy Apply is
   an `<a>` that does nothing under automation. The same job opened through
   `/jobs/search/?currentJobId=<id>` renders a real `<button>`. The applier
   retries that way when the control does not respond.
2. **"Review"** is the last step before Submit and counts as an advance.
3. The résumé step offers previously uploaded CVs as radio cards and preselects
   the last one used — there is no file field to route. The applier uploads the
   tailored CV explicitly. LinkedIn also **resumes a saved draft mid-way**, so
   that step may never render; the review page then names the résumé it is about
   to send and offers `aria-label="Edit Resume"` as the way back.

## Quirks

- **DOCX → PDF fonts.** LibreOffice's headless backend on macOS resolves fonts
  through fontconfig, which cannot see the system fonts, and substitutes every
  one. Arial and Times have metric-compatible clones so they merely look wrong;
  Verdana has none and falls back to a serif, which reads as smaller, different
  type. `SOFFICE_VCLPLUGIN=osx` fixes it, and the converter now warns when a
  last-resort font appears in its output. On Linux, install the Microsoft fonts.
- **Filled forms are held open** until they have been idle for
  `APPLY_REVIEW_IDLE_MINUTES`. Nothing persists a half-filled form, so a flat
  timer used to destroy work in progress.
- **`python-docx`** is finicky: the CV skills block is replaced whole rather
  than edited in place.
- **Memory.** A batch aborts below `APPLY_MIN_FREE_MB`; a runaway run once took
  a shared host down.
- **Personal data never enters the repo.** Names, emails, hostnames and
  documents live in `CVS_DIR` and `.env`, both gitignored.
