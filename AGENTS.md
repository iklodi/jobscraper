# AI Job Application Pipeline — notes for agents and maintainers

End-to-end automation for a job search: scrape LinkedIn, score each advert
against a candidate profile, write tailored documents for the jobs the
candidate approves, fill in the employer's form, and track it all on a local
Kanban board. State lives in SQLite (`jobs.db`, WAL mode). Setup and day-to-day
use are in [README.md](README.md); this file is how it works and what not to break.

## Working rules

- **This repository is public. Nothing personal goes into it** — not in code,
  comments, docstrings, test fixtures, examples or *commit messages*: no names,
  phone numbers, emails, addresses, hostnames, home paths, employers, the
  companies applied to, or document filenames. Personal data lives in
  `CVS_DIR` and `.env`, both outside git. Use placeholders ("+41 12 345 67 89",
  "20260101_Example_CV.pdf") when an example is needed.
- **Never kill or take over a browser on `chrome_profile`.** It may be the
  candidate's own window with a half-filled application in it. If the profile
  is locked, stop and say so.
- **Test with scratch copies.** Copy `jobs.db` elsewhere before exercising code
  that writes to it, and do not let a test harness write `progress.json` or
  `last_run.json` in the repo — the dashboard shows those to the candidate.
- **Driving a live LinkedIn application is the candidate's call.** It spends
  their session and their draft; stubs are fine without asking.
- **The dashboard is long-lived** (a LaunchAgent on macOS). The worker modules
  it calls are reloaded before each run, but a change to `app.py` itself needs
  the dashboard restarted.
- `dashboard.log`, `cron.log`, `progress.json`, `last_run.json` and `jobs.db*`
  are runtime files, not source.

## Components

### `main.py` — the nightly controller
Scrapes, scores, emails the summary. **It does not generate documents**: that
waits for an explicit Approve on the dashboard, so nothing is written for a job
that will never be sent. Flags: `--no-scrape`, `--no-eval`, and `--gen` to
generate for everything on To Do.

### `scraper.py`
Playwright over a persistent Chromium profile (`./chrome_profile`) holding the
logged-in LinkedIn session. Reads `CVS_DIR/search_criteria.md` for keywords,
locations and job types; a keyword may pin its own locations
(`- Interim Manager @ Switzerland / France`). Job types map onto LinkedIn's
`f_JT` filter. The file wins over the `SEARCH_*` env vars, which apply only when
it does not exist. Skips jobs already in the database; emails when the session
has expired and waits five minutes for a login.

### `evaluate.py`
Scores each `new` job 1-10 against the career dossier and `CVS_DIR/rules.md`.
At or above the pass mark it becomes `to_apply`, below it `scored`. The pass
mark comes from one place, `db.min_pass_score()`, read live, and is injected
into the prompt as `{min_score}` — so the board threshold and the score from
which the model is asked for salary, recruiter flag, hiring manager and
language cannot drift apart.

When a job clears the bar and another unapproved job from the same company is
already on To Do, the model picks one and the other is rejected. That is
skipped for recruiters and aggregators, which post for many unrelated clients
under one name, and it never rejects an approved job.

### `generator.py`
Writes the tailored CV and cover letter with `python-docx` and converts them
with headless LibreOffice. CV content is always English; the cover letter
follows the advert's language. Also saves the advert itself as a PDF.

### `applier.py`
Walks from the LinkedIn posting to the employer's form, fills it from
`CVS_DIR/profile.yaml`, and submits when asked to. The longest and most
defensive file here — see **Applier invariants**.

### `infomaniak.py`
Infomaniak AI Services as an OpenAI-compatible provider, tried before Gemini and
Groq everywhere. Its endpoint accepts **only** `response_format: json_schema` —
`json_object` and `text` are rejected — so every caller passes a schema.
Retries with jittered backoff across 408/429/5xx. Any one provider is enough to
run; `evaluate.any_model_configured()` is the gate.

### `generate_from_url.py`
One advert by URL, for the dashboard's + button. Reads the page, scores it, and
files it on To Do *whatever it scores* — a link pasted by hand is a job already
chosen. If the link is for a job already approved or applied to, it goes back
where it was. `--generate` writes the documents immediately.

### `app.py` / `static/app.js` / `templates/index.html`
Flask dashboard. Columns: New, To Do, Approved, Ready to Submit, Applied,
Interviewing, Failed, Account Required, Rejected — every one ordered by last
modification, newest first. `/api/settings` edits `search_criteria.md` and
`rules.md` in place, keeping a `.bak`, refusing empty content, and reporting
what the next run will search for.

## Statuses

| Status | Meaning |
|---|---|
| `new` | Scraped, not yet scored. |
| `scored` | Below the bar. Shown on Rejected for `REJECTED_BOARD_DAYS`, then hidden — the row stays, so the scraper still recognises the job. |
| `rejected` | Rejected by hand, or by the same-company comparison. Same board rule. |
| `to_apply` | At or above the bar. (`generated` is the same column, with documents.) |
| `approved` | Approved by hand; documents are written on this transition. |
| `ready_to_submit` | Form filled, waiting for a person to send it. |
| `easy_apply` | LinkedIn Easy Apply only — left for a person, nothing clicked. Shown with Ready to Submit. |
| `applied` | Submitted — set only when the applier pressed submit itself, or by hand. |
| `interviewing` | Set by hand. |
| `account_required` | The employer wants an account before the form can be filled. |
| `failed` | No apply button, closed listing, CAPTCHA, or unmappable fields. |
| `generating` / `evaluating` / `applying` | Transient, while a background run holds the job. |

**Protected statuses.** `approved`, `ready_to_submit`, `easy_apply`,
`applied`, `interviewing` and `account_required` record a person's decision
(`db.PROTECTED_STATUSES`). Re-evaluating, regenerating or re-adding a job by
link must leave it in one of those — a new score informs, it does not undo an
application.

## Models

Infomaniak first (`google/gemma-4-31B-it` → `mistralai/Mistral-Small-4-119B`
→ `swiss-ai/Apertus-v1.5-70B`), then the Gemini cascade, then Groq's
`gpt-oss` models. The cascades in `evaluate.py` are shared by the generator.

gemma leads despite being slower. Scoring the same 395 jobs with gemma and
Mistral showed they agree on only 11%, with Mistral averaging 1.59 points high
— not taste but rule-following: Mistral treats the hard gates in `rules.md` as
preferences, passing jobs in the wrong location and duplicates of applications
already sent. A lenient evaluator is worse than a slow one.

The models read screenshots accurately but cannot ground coordinates (off by
130-320px on buttons 150px wide). If clicking from a screenshot is ever needed,
number the DOM elements on the image and ask which number.

## Applier invariants

Do not weaken these. Each one exists because it failed once.

- **It never invents an answer.** Anything `profile.yaml` does not cover is
  left blank, listed in the job's note, and collected into the batch email
  (grouped by question) so it can be added there. Single-job runs report on the
  dashboard only.
- **The pre-submit gate** blocks submission unless every required field is
  filled, nothing was left unanswered, no field refused input, and every
  required upload got a document.
- **No blind force-clicks.** A forced click lands at the element's coordinates
  whatever is painted above it; on a modal wizard that is *Next*, and the form
  silently advances. Tick boxes with the DOM `click()` method, which fires the
  page's handler without hit-testing. Setting `el.checked` is not enough: React
  re-renders from its own state and drops it.
- **Controls come from the open dialog, never the page behind it** — a site's
  own chrome (a chat widget's "Send") must not be taken for the form's submit.
- **Values are checked after filling.** A rejected value is retried in other
  shapes (phone numbers: E.164, 00-prefixed, national) and otherwise reported.
- **Submitting can be switched off** with
  `policies.never_submit_without_review: true` in `profile.yaml`. The applier
  then only fills, whatever it is asked (`--submit` included), the endpoint
  refuses a submit request, and Apply Now is disabled with a tooltip saying how
  to enable it.
- **It does not solve CAPTCHAs.** That gate is deliberate.
- **Account creation is allowed** when `policies.allow_account_creation` is set.
  Generated passwords go to `CVS_DIR/ats_accounts.yaml` (chmod 600) *before* the
  registration is submitted.
- **The browser profile is shared** with the scraper and the + button. The
  applier reclaims it only from an orphaned browser; one owned by a live run is
  left alone and the run refuses to start.
- **Every job gets its own tab**, and a filled form stays open until it has been
  idle — values unchanged — for `APPLY_REVIEW_IDLE_MINUTES`. Nothing persists a
  half-filled form, so a closed tab is lost work.
- **Waits are jittered** (`APPLY_PACE`): identical gaps between clicks are a
  cheap automation signal, and this is the scraper's own LinkedIn session.
- **Every page touched is screenshotted** into the application folder, numbered
  in order, with a `<job_id>_run.json` manifest recording which file went into
  which field. Re-filling a job deletes its previous trail first.

**Uploads** are routed by the model from a menu built at run time — the
generated `cv` and `cover_letter` plus the `attachments:` in `profile.yaml` — so
it cannot name a file that does not exist. `ATTACHMENT_RULES` is the fallback.
Where a form wants the cover letter as text rather than a file, the model asks
for it by placeholder and the letter is pasted verbatim from its salutation on.

### LinkedIn Easy Apply is not automated

Deliberately. Easy Apply runs inside LinkedIn on the candidate's own logged-in
account — the most bot-like thing this tool could do with it, for a saving of
about three clicks, against the risk of a restricted account and a lost
network. The applier reads the apply control's accessible name and, if it says
"Easy Apply", stops *without clicking*: the job becomes `easy_apply` with a
direct link, shown in the Ready to Submit column. Do not add Easy Apply
automation back.

Only external applications are automated: the applier follows the posting's
Apply link off LinkedIn and fills the employer's own form.

## Quirks

- **DOCX → PDF fonts.** LibreOffice's headless backend on macOS resolves fonts
  through fontconfig, which cannot see the system fonts, and substitutes all of
  them; Verdana has no metric-compatible clone and falls back to a serif.
  `SOFFICE_VCLPLUGIN=osx` (the macOS default) fixes it, and the converter warns
  when a last-resort font appears in its output. On Linux, install the
  Microsoft core fonts.
- **`pgrep` and a leading `--`.** A pattern starting with `--` is parsed as an
  option; pass `--` first. The profile-lock code was a silent no-op for weeks
  because of this.
- **`python-docx`** is finicky: the prompt makes the model replace the CV's
  skills paragraph whole rather than edit it in place.
- **Memory.** A batch aborts below `APPLY_MIN_FREE_MB` (Linux only — macOS
  reports no figure); a runaway run once took a shared host down.
- **Windows.** Set `SOFFICE_PATH` to `soffice.exe` and start the dashboard with
  `start_dashboard.bat`. The macOS-specific pieces (LaunchAgent, the `osx`
  LibreOffice backend) do not apply.
