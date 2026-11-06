# AI Job Application Pipeline — notes for agents and maintainers

End-to-end automation for a job search: scrape LinkedIn, score each advert
against a candidate profile, write tailored documents for the jobs the
candidate approves, fill in the employer's form, and track it all on a local
Kanban board. State lives in SQLite (`jobs.db`, WAL mode). Setup and day-to-day
use are in [README.md](README.md); this file is how it works and what not to break.

## Working rules

- **Run the tests**: `venv/bin/python -m pytest -q tests`. Fixtures are
  anonymised copies of real emails; keep them that way.
- **Read [DISCLAIMER.md](DISCLAIMER.md).** Keep it true when behaviour changes -
  it states what the tool does with the candidate's account and data.
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

### `mode.py` — full or safe
`JOBSCRAPER_MODE=full|safe` (anything else counts as safe). `open_linkedin_session()`
is the **only** place allowed to launch the logged-in profile, and it raises
`SafeModeViolation` in safe mode; `tests/test_mode.py` fails if any other file
calls `launch_persistent_context`. Safe mode is being built in issues #12-#18
(epic #20). The applier works in safe mode from the employer's own page (#15):
see **Applier invariants**. `open_employer_session()` is the only other
launcher of a persistent profile, and it cannot reach LinkedIn.

### `alerts.py` — LinkedIn's job-alert emails, in either mode
Reads LinkedIn job-alert emails over IMAP (`BODY.PEEK`, never moved; marked
read only with `IMAP_MARK_READ`, and only once the email's jobs are stored), parses each job's title, company, location and id, and adds it as
`new`. Only the numeric id is kept: alert links carry sign-in tokens, so links
are rebuilt bare. Messages already read are recorded in `alert_emails`, which also feeds the
settings dialog's alert table (`recent_alerts`) and the summary's cap warning.
`suggested_alerts` / `python alerts.py --suggest` turn `search_criteria.md`
into the alerts to create. A job
without a description is never scored — its description comes from the
logged-out fetch (`public_jobs.py`, #14), in both modes. In full mode the alerts
add the jobs the scraper's searches miss (about three in four, measured).

### `public_jobs.py` — safe mode's descriptions
Opens `https://www.linkedin.com/jobs/view/<id>/` in a fresh `chromium.launch()`
context — no profile, no cookies — and reads the description every visitor
sees. Also records `apply_type` (`offsite` / `easy_apply`) from the apply
control's tracking name; the employer's own URL is **not** exposed logged out.
On HTTP 999, a sign-in wall or a challenge it stops for the run and leaves the
rest pending. No proxies or evasion — that is a rule, not a gap. Newest jobs
are fetched first, so one still without a description after
`SAFE_FETCH_MAX_AGE_DAYS` (14) goes to Rejected unscored (`expire_stale`): a
backlog of old alerts otherwise never moved. It also reads
the job poster from the public page's hiring-team card
(`.message-the-recruiter`) into `hiring_manager_name`, and `fetch_poster()`
does the same on demand: the generator calls it before writing a letter for a
LinkedIn job with no addressee (scraped jobs never had the card read). A name
the scorer finds in the advert itself wins; a score without one never erases
a name already recorded.

### `adzuna.py` — a second source, in either mode
Runs the `search_criteria.md` searches through Adzuna's official API
(`title_only`: matching the whole advert brought in assistants to general
managers). Jobs get ids `adzuna-<id>` and Adzuna's `redirect_url` as their link,
which its terms require. The API cuts descriptions to 500 characters, so the
full text is read from the advert's Adzuna page over plain HTTP, 5-10 s apart.
Some adverts have no Adzuna page: their link is `/land/ad/`, Adzuna's redirect
to the advertiser, and Adzuna refuses every automated visitor there, a real
headless browser included (tested 2026-09-30). Those are scored on the API's
summary (kept in `snippet`, prefixed with `SUMMARY_NOTE`); no request is made.
Getting past that refusal would mean disguising the client - do not. A 403 on
an ordinary page costs that job an attempt; three in a row stop the run. A
404/410 closes the job. (An earlier theory that Adzuna rate-limits at 5-10 s
was wrong: the 403s were these redirect-only adverts.) Adverts
already on the board are skipped by normalised title and company; Job-Room
(the Swiss public portal) re-lists employers' adverts, so for it the title
alone decides. The dashboard labels these jobs "Jobs by Adzuna" (Adzuna's
terms). Limits: 25 calls/min, 250/day.

### `outcomes.py` — LinkedIn's application-status emails
Reads `jobs-noreply@linkedin.com` through `alerts.read_mail` (the one IMAP path;
these stay unread whatever `IMAP_MARK_READ` says; processed mail in `outcome_emails`). The body says nothing; the
meaning is the subject plus LinkedIn's tracking name in the links
(`email_jobs_application_rejected` / `email_jobs_job_application_viewed`).
Only jobs in Applied are matched; a decline needs company **and** role to match
exactly one job, "viewed" only adds a note, and everything else goes to the
summary untouched. Do not loosen the matching: a wrong match hides a live
application.

### `declines.py` — employers' decline emails, from a folder
The candidate files declines under a mail label (`DECLINE_FOLDER`, off when
empty). Labelling a Gmail conversation labels every message in it, so the
emails are grouped into conversations (References/In-Reply-To, or subject plus
correspondent - never subject alone: ATS mail is all "Your application") and
only the newest is read. The model reports whether it is a decline, the
company, the role and the reason; the code offers it only that company's jobs
in Applied, Interviewing or Declined (`same_company`) to pick from, and any
doubt changes nothing. A match moves the job to Declined with the reason in
its notes and "Found in: <sender>, <subject>, <date>, <folder>" in its
history. Infomaniak only; a silent model leaves the email for the next run.

### `link_queue.py` — links the candidate collected
`python link_queue.py add <file or URL>...` queues adverts (a list, or a saved
page of links) as Pending Eval jobs with only a link; `fetch_pending` reads
them nightly, in a fresh browser, with the + button's reader
(`generate_from_url._read_page` / `extract_metadata`), and retires a copy of
an advert already on the board. LinkedIn links are left to `public_jobs.py`,
which in turn skips `url-` jobs. Single adverts chosen by the candidate, 5-10 s
apart - not a crawl.

### `ats_feeds.py` — employers' own job feeds, in either mode
One adapter per ATS (Greenhouse, Lever, Ashby, Personio, SmartRecruiters,
Recruitee), each reading the public feed documented for programs: never the
careers page's HTML. The watch list is `CVS_DIR/companies.yaml`. Postings are
kept when a keyword's significant words all appear in the title and the
location matches (country names expand to cities; "Europe" to European places;
"Worldwide" matches all). Ids are `ats-<ats>-<board>-<id>`, `apply_url` is the
employer's form, and a posting under 200 characters is not stored.
SmartRecruiters' listing has no description, so each new match costs one detail
call. A broken feed is reported in the summary, never fatal.

### `dedupe.py`
`already_listed(title, company)`: the cross-source duplicate check every source
uses before inserting - Adzuna, the employer feeds, the alerts and the
scraper. Titles match once punctuation, gender markers and workload are gone;
companies once legal forms and country names are ("Example Switzerland" is
"Example"; `COMPANY_ALIASES` in `.env` adds firms known by two names), with
Job-Room as a re-lister. `sweep()` runs nightly after the
sources: in each group of copies the one furthest along is kept, and a copy
still waiting, or approved when the advert was already applied to through
another copy, moves to Rejected with "Duplicate of #id".

### `main.py` — the nightly controller
Full mode scrapes LinkedIn; safe mode does not. Both then read the job-alert
emails (when IMAP is configured), fetch descriptions logged out and retire
the ones that never came (`expire_stale`), run the Adzuna searches and the
employer feeds, read the queued links, sweep duplicates, read LinkedIn's
status emails and the decline folder, score, and email the summary. Each step
is wrapped: one that fails (a crashed LinkedIn tab, a refused feed) is noted
in the summary and the rest still run. `--no-scrape` skips every source.
**It does not generate documents**: that waits for an explicit Approve on the
dashboard, so nothing is written for a job that will never be sent. Flags: `--no-scrape`, `--no-eval`, and `--gen` to
generate for everything on To Do.

### `scraper.py`
Playwright over a persistent Chromium profile (`./chrome_profile`) holding the
logged-in LinkedIn session. Reads `CVS_DIR/search_criteria.md` for keywords,
locations and job types; a keyword may pin its own locations
(`- Interim Manager @ Switzerland / France`). Job types map onto LinkedIn's
`f_JT` filter. `get_tracks()` reads the `# Tracks` section (`- Name: description`).
Searches go to LinkedIn's AI search (`/jobs/search-results/`, since November
2026), whose class names are generated: cards are found by their
`componentkey` (`job-card-component-ref-<id>`), the title by the card's
"Dismiss <title> job" button, and the description by the detail panel's
"About the job" section, read only once the address shows the clicked card's
`currentJobId`. `LINKEDIN_SEARCH_PAGES` (default 1) pages per search. The file wins over the `SEARCH_*` env vars, which apply only when
it does not exist. Skips jobs already in the database; emails when the session
has expired and waits five minutes for a login.

### `evaluate.py`
Scores each `new` job 1-10 against the career dossier and `CVS_DIR/rules.md`.
At or above the pass mark it becomes `to_apply`, below it `scored`. The pass
mark comes from one place, `db.min_pass_score()`, read live, and is injected
into the prompt as `{min_score}` — so the board threshold and the score from
which the model is asked for the recruiter flag, hiring manager and language
cannot drift apart. The salary estimate is asked for every job, whatever it
scores: a job added by link lands on To Do even at 2/10, and should show one.

**Hard location rules are applied in code** (`location_gate`), not left to the
model. It reports facts - work arrangement, country, region - which it gets
right, and `HOME_COUNTRY` / `ONSITE_REGIONS` / `HYBRID_COUNTRIES` in `.env` cap
a job that breaks them at 2. Nothing is tied to one country: "region" is
whatever `REGION_TYPE` says (a Swiss canton, a US state, a French
department), and the prompt asks for it in that form (`region_fact`). Asked to apply such a rule itself, gemma called an
on-site Geneva role compliant and scored it 9. A job abroad in one of the
`RELOCATION_COUNTRIES` passes those rules when the model's `salary_annual`
(its pay estimate as one annual figure, converted to `SALARY_CURRENCY`)
reaches `RELOCATION_MIN_SALARY`. A job that
passes but means a move loses points (`location_penalty`): `RELOCATION_PENALTY`
outside `HOME_COUNTRY`, `REGION_MOVE_PENALTY` outside `HOME_REGIONS`; the reasoning starts
"A move to <country>: -1 (fit 9)". The facts are stored (`work_arrangement`,
`country`, `region`), and `python evaluate.py --move-penalty` applies the
penalty once to To Do jobs scored before it. The "already applied" list
holds only real applications (Applied, Interviewing, Offer, Declined, or Rejected after
Applied) at the job's own company: the full list, with every hand-rejected
job in it, was 40,000+ characters that buried the rules.

It also files every job under one of the search tracks (`track`). The model
answers with a name, and `normalise_track` keeps only a configured one, else
"Other". `python evaluate.py --classify-tracks [--all]` files jobs scored
before tracks existed, with a short prompt and no re-score.

When a job clears the bar and another unapproved job from the same company and
track is already on To Do, the model picks one and the other is rejected. That is
skipped for recruiters and aggregators, which post for many unrelated clients
under one name, and it never rejects an approved job.

### `generator.py`
Writes the tailored CV and cover letter with `python-docx` and converts them
with headless LibreOffice. The model writes the CV in English; it is
translated afterwards when the advert is in another language, and the cover
letter is written in the advert's language. Also saves the advert itself as a PDF. The
letter is addressed to the office hiring for the role - the job's location -
and to the company's headquarters only for remote or regional roles (a Swiss
role at a US agency was addressed to its US headquarters).
Both documents follow the advert's language by default, and Regenerate on
the dashboard can pick another for each. The CV is written in English and
then translated stretch by stretch of
formatting (`translate_docx`); a letter not in English gets its header -
"Hiring Team", country names, the sender band's text box - translated on the
template, before the company's name is filled in. An English greeting or
closing left in a French, German or Italian letter is replaced in code. A
failed CV translation leaves it in English with a note in the job's history.
The job's track and its description go into the prompt, so a business
analyst application reads like one (`track_vocabulary`). German
documents for a Swiss job use Swiss spelling (ss, never ß).

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
where it was. `--generate` writes the documents immediately. A LinkedIn link
is always reduced to `/jobs/view/<id>/` first (`canonical_url`): links copied
from LinkedIn or its emails carry sign-in tokens. In safe mode every page is
read in a fresh `chromium.launch()` browser, never the profile, and a LinkedIn
sign-in wall is reported, not worked around.

### `app.py` / `static/app.js` / `templates/index.html`
Flask dashboard. Columns: Pending Eval (`new`), To Do, Approved, Applied,
Interviewing, Declined, Failed, Account Required, Discarded (status `rejected`; the
label changed, the status did not) — every one ordered by last
modification, newest first. Every card has a checkbox; "Select all" takes the
cards on screen (not those a filter hides or behind Load More), and while any
are selected the column offers its moves in a menu (`BULK_ACTIONS` in
`app.js`). `/api/jobs/bulk` makes the moves, skips jobs a background run
holds, and writes documents for jobs approved together one after another.
A track filter above the board (`/api/tracks`) applies to every column. Below
768px wide (`style.css`'s phone block) the board shows one column per screen
width, with a sticky `.column-nav` strip of column names and counts to jump
between them (`renderColumnNav` in `app.js`). `/api/settings` edits `search_criteria.md` and
`rules.md` in place, keeping a `.bak`, refusing empty content, and reporting
what the next run will search for.

## Statuses

| Status | Meaning |
|---|---|
| `new` | Scraped, not yet scored. |
| `scored` | Below the bar. Shown on Discarded for `REJECTED_BOARD_DAYS`, then hidden — the row stays, so the scraper still recognises the job. |
| `rejected` | Discarded: by hand, by the same-company comparison, as a duplicate, or never fetched. Same board rule. Shown as "Discarded". |
| `to_apply` | At or above the bar. (`generated` is the same column, with documents.) |
| `approved` | Approved by hand; documents are written on this transition. |
| `ready_to_submit` | Form filled, open in Chrome, waiting for a person to send it. Shown in Approved, tagged "Form filled" — no column of its own. |
| `easy_apply` | LinkedIn Easy Apply only — left for a person, nothing clicked. Shown in Approved, tagged. |
| `applied` | Submitted — set only when the applier pressed submit itself, or by hand. |
| `interviewing` | Set by hand. |
| `declined` | The employer turned the application down: by hand (with a reason) or from LinkedIn's decline email. Never hidden. |
| `account_required` | The employer wants an account before the form can be filled. |
| `failed` | No apply button, closed listing, CAPTCHA, or unmappable fields. |
| `generating` / `evaluating` / `applying` | Transient, while a background run holds the job. |

**Protected statuses.** `approved`, `ready_to_submit`, `easy_apply`,
`applied`, `interviewing`, `declined` and `account_required` record a person's decision
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
- **The browser profile is shared** with the scraper and the + button in full
  mode. The applier reclaims it only from an orphaned browser; one owned by a
  live run is left alone and the run refuses to start.
- **In safe mode the applier never reaches LinkedIn.** It runs in
  `employer_profile` (`mode.open_employer_session`), which aborts every request
  to linkedin.com at the browser level, and starts from the employer's page:
  the job's `apply_url` (pasted on the dashboard) or its link when that is not
  LinkedIn (`applier.plan_start`). A job with only a LinkedIn link stays
  Approved with a note asking for the employer link; do not add a fallback to
  the posting.
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
direct link, shown in the Approved column with an Easy Apply tag. Do not add Easy Apply
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
