# LinkedIn AI Job Scraper

> [!WARNING]
> **Use at your own risk.** In its default mode this tool automates your
> logged-in LinkedIn account, which LinkedIn's User Agreement prohibits; your
> account can be restricted or permanently banned. It sends applications in
> your name and sends your career data to third-party AI providers. A
> [**safe mode**](DISCLAIMER.md#safe-mode) that never touches your logged-in
> account is being built. Read [DISCLAIMER.md](DISCLAIMER.md) before using it.

An automated agent that scrapes LinkedIn, scores each job against your CV, and
— once you approve it — writes a tailored CV and cover letter and fills in the
employer's application form.

Everything runs locally: an SQLite database, a Flask Kanban dashboard, and a
Playwright browser using your own logged-in LinkedIn session.

## How a job moves through it

| Stage | What happens |
|---|---|
| **Find** | `scraper.py` walks the searches in `search_criteria.md` on LinkedIn (full mode only) and stores anything new. LinkedIn's job-alert emails, Adzuna's job API, employers' own job feeds and links you collected add more, in either mode. A job seen on two of them is kept once. |
| **Score** | `evaluate.py` rates each job against your dossier and `rules.md` on ten factors (role fit, seniority, experience, skills, industry, location, salary, language, employer, growth), each 0-100, in one model call, and computes the score from them: a weighted composite from 0 to 10, shown to two decimals (see [How a job is scored](#how-a-job-is-scored)). It also files the job under one of your search tracks. The location rules are applied in code (see [Where you will work](#where-you-will-work)). At or above `MIN_PASS_SCORE` it lands on **To Do**; below, on **Discarded**. Re-scoring never moves a job you approved or applied to. A job whose description could not be read within `SAFE_FETCH_MAX_AGE_DAYS` (14) is discarded unscored. |
| **Approve** | You press **Approve** on the dashboard. *Only then* are documents written — nothing is generated for a job you will never send. |
| **Apply** | **Apply Now** fills the employer's form and submits it; **Fill Now** does the same but stops at the submit button, leaving the form open in the browser for you (the card stays in Approved, tagged "Form filled"). Every step is screenshotted. LinkedIn **Easy Apply** is never automated — those jobs get a direct link for you to apply by hand. |
| **Hear back** | A decline moves the job to **Declined** with the reason: from LinkedIn's status emails, from the decline emails you file under a mail label, or by hand. |

The nightly run stops after scoring. Pass `--gen` to `main.py` if you want it to
generate for everything on To Do the old way.

## Full mode and safe mode

`JOBSCRAPER_MODE` in `.env` chooses how new jobs are found:

- **full** (the default) scrapes LinkedIn through a browser signed in to your
  account. See [DISCLAIMER.md](DISCLAIMER.md) for why that is risky. With
  `IMAP_*` set it also reads your LinkedIn job-alert emails, as safe mode does:
  they bring jobs the scraper's searches miss.
- **safe** never automates your logged-in account. It reads the **job-alert
  emails** LinkedIn sends you over IMAP, from `IMAP_FOLDER` (a Gmail label
  works: let a filter file them there), leaving them unread unless you set
  `IMAP_MARK_READ=true`. Set up LinkedIn job alerts
  for your searches (daily, by email), then fill in the `IMAP_*` settings.
  Descriptions are then read from the public job pages, logged out, and the
  jobs are scored as usual. The nightly email opens with a **Sources** section:
  jobs per alert, alerts whose email hit LinkedIn's six-job limit, and whether
  a fetcher was blocked. The **+** button works too: a pasted LinkedIn link
  is stripped of its tracking and sign-in parameters and read logged out.
  **Fill Now** and **Apply Now** work from the employer's own page, in a
  separate browser (`employer_profile`) that is blocked from reaching LinkedIn.
  LinkedIn does not show that page to a logged-out visitor, so for a job that
  came from LinkedIn, open the posting yourself, follow its Apply button and
  paste where it leads into the job's **Employer application link**. Jobs from
  other sources (Adzuna, pasted employer pages) need nothing.

### Setting up job alerts for safe mode

In safe mode LinkedIn is never searched: the alerts you keep on LinkedIn are
the searches. To recreate the searches in `search_criteria.md`:

1. Run `python alerts.py --suggest`. It prints one alert per keyword and
   location. The dashboard's **Search Settings** shows the same list in safe
   mode, next to the alerts that have actually been arriving.
2. On LinkedIn, search each one, press **Set alert**, and choose **daily** and
   **email**. LinkedIn offers only daily or weekly.
3. Put `IMAP_*` in `.env` so the emails can be read.

Know the trade-offs:
- **Each email lists at most six jobs.** A broad alert ("Europe",
  "Worldwide") hits that on most days and drops the rest. Prefer one alert per
  country. The nightly email flags alerts that hit the cap.
- **LinkedIn matches loosely.** An alert for "Interim Manager" also brings spa
  and production managers. The scorer filters them out, but they are not what
  the keyword search would have returned.
- **Timing.** A job reaches you when LinkedIn sends the alert, up to a day after
  it was posted.
- `search_criteria.md` still drives Adzuna in both modes.

### Application updates from LinkedIn

With `IMAP_*` set, each run also reads LinkedIn's application-status emails
from `jobs-noreply@linkedin.com`, in both modes. It only ever looks at jobs in
Applied. A decline that names the company and the role of exactly one of them
moves it to Declined with a note. "Your application was viewed" adds a note.
Anything that matches no job, or more than one, is listed in the summary email
and left alone.

### Declines you file in your mailbox

Employers decline by email in any wording, from an applicant-tracking system,
a recruiter or the hiring manager. File those emails under a label of their
own and set `DECLINE_FOLDER` to it. Each run, an AI model reads every new one
for the company, the role and the reason, and picks the application from your
jobs at that company in Applied, Interviewing or Declined. A match moves it to
Declined, with the reason in its notes and where it was found (sender,
subject, date, folder) in its history. Labelling a Gmail conversation labels
all of it, so a whole conversation counts as one email: its newest message.
Anything uncertain changes nothing and is listed in the summary email. The
first run looks `DECLINE_LOOKBACK_DAYS` (90) back.

## Where you will work

The scorer reports each job's work arrangement, country and region, and the
code - not the model - applies your rules: a job that breaks them gets 0 for
the location factor ("Location mismatch"), which weighs heavily in the score
but does not cap it. In `.env`:

- `HOME_COUNTRY`: on-site and hybrid jobs pass anywhere in it.
- `REGION_TYPE`: what a region is there and how to write it - a Swiss canton
  ("VD"), a US state ("CA"), a French department ("75"). `ONSITE_REGIONS`
  limits on-site jobs to some of them; `HYBRID_COUNTRIES` allows hybrid jobs
  in more countries.
- `RELOCATION_COUNTRIES` with `RELOCATION_MIN_SALARY`: a job there that
  breaks the rules still passes when the estimated pay is worth a move. The
  bar is in `SALARY_CURRENCY` (EUR, CHF, GBP...), and the scorer converts its
  estimate to it.
- **CV norms** follow the job's region too (`cv_norms.py`): no photo, date of
  birth or similar in the US, UK, Ireland, Australia and New Zealand, a résumé
  rather than a CV in North America, the region's page length, nationality for
  the Gulf, and for Germany, Austria and Switzerland one dossier PDF of
  letter, CV and certificates (`dossier:` in profile.yaml). The job's history
  says what was applied.
- What else depends on a country - letter greetings, closings, date and
  spelling, phone formats, the languages offered first - follows the job's
  country, and `HOME_COUNTRY` where that is unknown (`locales.py`).
- `RELOCATION_PENALTY` and `REGION_MOVE_PENALTY` (with `HOME_REGIONS`) take
  points off a job that means moving, so it ranks below an equal fit nearer
  home. The reasoning says so: "A move to United Kingdom: -1 (fit 9)". Jobs
  scored before you set them: `python evaluate.py --move-penalty`.

## A second source: Adzuna

[Adzuna](https://developer.adzuna.com) aggregates many job boards and offers
an official search API with a free key. Set `ADZUNA_APP_ID` and
`ADZUNA_APP_KEY` and every run also sends your `search_criteria.md` searches
there, matching keywords against job titles. It never touches LinkedIn, so it
runs in both modes. Details:

- **Locations.** Country names map to Adzuna's countries. A city is searched
  inside `ADZUNA_COUNTRY`. "Worldwide", "Europe" and "Remote" use
  `ADZUNA_WIDE_COUNTRIES`, and are skipped when that is empty.
- **Descriptions.** The API returns only the first 500 characters. The full
  text is read from each advert's Adzuna page, 5-10 seconds apart. Some adverts
  have no Adzuna page: Adzuna forwards visitors to the advertiser and refuses
  automated ones. Those are scored on Adzuna's 500-character summary, marked as
  such in the description.
- **Duplicates.** An advert already on the board, from LinkedIn or from Adzuna
  under another listing, is not added twice.
- **Labels.** Adzuna's terms ask for its adverts to be labelled, so they show
  "Jobs by Adzuna" on the dashboard. Its limits are 25 calls a minute and 250
  a day; a run makes at most `ADZUNA_MAX_CALLS_PER_RUN`.

## A third source: employers' own job feeds

Many employers publish their openings through a hiring system with a public
job feed: Greenhouse, Lever, Ashby, Personio, SmartRecruiters or Recruitee.
List the employers you want to follow in `CVS_DIR/companies.yaml` (see
`companies.example.yaml`). `python ats_feeds.py --detect <link>` works out the
entry from any of their job links.

Each run reads every feed once. It keeps the postings whose title matches a
keyword and whose location matches a location, from `search_criteria.md` or
the company's own `keywords:` / `locations:`. Jobs from feeds come with the full
description and **the employer's application link**, so Fill Now and Apply Now
work on them in both modes, with nothing to paste. It never touches LinkedIn.

## Links you collected

Adverts you found yourself - a list of links, or a saved page of them - can be
added from the dashboard's **+** (read and scored at once), or queued:
`python link_queue.py add links.txt`. Queued ones wait in Pending Eval with
only their link, and each night up to `LINK_FETCH_MAX_PER_RUN` are read in a
fresh browser, 5-10 seconds apart, and scored like any other job. An advert
that is gone (404/410) is closed; one a site refuses is tried again on later
runs, then discarded after 14 days. LinkedIn links are read from LinkedIn's
public pages instead.

## Setup

1. **Prerequisites**: Python 3.14+, Git, LibreOffice.

2. **Environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

3. **Configuration**: copy `.env.example` to `.env` and fill it in. Every
   setting is documented there; the ones you cannot skip are one model provider
   (`INFOMANIAK_API_TOKEN` + `INFOMANIAK_PRODUCT_ID`, `ANTHROPIC_API_KEY` for
   the Claude API, `GEMINI_API_KEY` or `GROQ_API_KEY` — any one is enough;
   `LLM_PROVIDERS` sets the default provider and the fallback order) and `CVS_DIR`. Then copy
   `profile.example.yaml` to `CVS_DIR/profile.yaml` and fill in your answers.

4. **Log in to LinkedIn once**:
   ```bash
   python scraper.py
   ```
   A browser opens; sign in. The session is saved in `./chrome_profile` and
   reused by every later run. If it expires, the scraper emails you and waits
   five minutes for you to sign in again.

5. **Start the dashboard**:
   ```bash
   python app.py          # http://localhost:5050
   ```

## Your files live outside the repo

## How a job is scored

The model rates every factor 0-100 in a single answer, and the score is computed
in code (`scoring.py`):

    score = sum(weight × factor) / sum(default weights) / 10

Nothing caps it: a weak factor can be made up by strong ones. Location weighs
the most after role fit, and a job that breaks your location rules (checked in
code) gets 0 for location. Only `HARD_RULES` (default `Already applied,
Blacklisted company`) rule a job out, at 1. The weights are in
`scoring_weights.yaml`; put your own in `CVS_DIR/scoring_weights.yaml` to
change what matters (only the keys you set change). Each job's detail view
shows its factors and the model that scored it.

Models grade differently - one's 70 is another's 85 - so each model can have its
own weight set under `models:`: the default weights scaled factor by factor
to a common average, then as a whole so that every model passes the same
share of the same jobs at one pass mark. `python scoring.py --fit results.json
--pass-share 0.5` computes the sets and that mark from a comparison of models
on the same jobs. `MIN_PASS_SCORE` may have decimals (`6.5`).

Everything personal sits in `CVS_DIR` and is never committed:

| File | What it is |
|---|---|
| `docs/<CV_TEMPLATE_NAME>` | Your CV. Keep the skills in one paragraph separated by ` · ` — the generator's prompt has the model rewrite that paragraph whole. |
| `docs/<CL_TEMPLATE_NAME>` | Cover letter template. Must keep `[COMPANY]`, `[LOCATION]` and `[DATE]` in the addressee block. |
| `docs/<DOSSIER_NAME>` | Career history in markdown. The scorer and generator both read it. |
| `search_criteria.md` | Keywords, locations and job types. A keyword can pin its own: `- Interim Manager @ Switzerland / France`. An optional `# Tracks` section (`- Name: what the track is`) files every scored job under one track, and the board filters by them. |
| `rules.md` | Scoring rules, injected into the evaluator prompt. |
| `scoring_weights.yaml` | Optional: your own factor weights, over the repository's `scoring_weights.yaml`. |
| `profile.yaml` | Answer bank for application forms — identity, work authorisation, salary and contract rates, screening answers. The applier never invents an answer; anything missing is reported so you can add it here. |
| `attach/` | Diplomas, reference letters and anything else a form may ask to upload, wired up under `attachments:` in `profile.yaml`. |
| `applications/` | Generated documents, the screenshot trail, and a `<job_id>_run.json` manifest per application. |

`search_criteria.md` and `rules.md` are editable from the dashboard's
**⚙️ Search Settings** button, which validates and reports what the next run
will search for.

## Using the dashboard

- **+** — paste any job URL (LinkedIn or an employer's own page). It reads the
  advert, scores it, and puts it on To Do whatever it scores; approving it there
  writes the documents. Paste several, one per line, or **Upload a text file**
  of them (or a saved page of links): they are read and scored one after
  another, filed on To Do or Discarded by score, and listed when done.
  Ctrl/Cmd+Enter sends.
- **⚙️ Search Settings** — edit the keywords and scoring rules.
- **Run Pipeline / Run Evaluations Only** — trigger a run by hand.
- **📤 Apply to Approved / 📝 Fill Approved** — work through every approved job.
  Apply is greyed out when `policies.never_submit_without_review` is `true` in
  `profile.yaml`; hover it to see why. Fill always works.
- **Checkboxes** on every card, and **Select all** per column (the cards on
  screen, not those a filter hides). With cards selected, the column offers
  its moves in a menu: approve, fill or apply, re-evaluate, discard, mark as
  declined (with one reason for all)...
- **Track filter** above the board, for the tracks in `search_criteria.md`.
- **Mark as Declined** on an applied job asks for the employer's reason.
- **Sort** any column by last change, date added, score or salary (the arrow
  flips the order); **Clear filters** on every column resets its filters. To empty a column, Select all and move the jobs.
- **On a phone** the board shows one column at a time, with a strip of column
  names to jump between them.
- After a batch, the email lists every question your profile could not answer,
  grouped by question, so you can add the answers to `profile.yaml`.
- Each card shows its score, location, estimated salary, whether the poster is a
  recruiter, and 📢 for LinkedIn's promoted (paid) listings, which the evaluator
  marks down. Opening a job shows its documents and the full screenshot trail of
  the last application attempt.

## Scheduling

Run it on a residential connection — LinkedIn is aggressive about datacentre IPs.
On macOS use a launchd agent for `main.py` (this repo uses
`com.example.jobscraper.plist` as the template); on Linux, cron or a systemd timer.

## Command line

```bash
python main.py                    # scrape, score, email the summary
python main.py --no-scrape        # score only
python main.py --gen              # also generate for everything on To Do
python applier.py --limit 5       # fill approved applications, without sending
python applier.py --submit        # fill and submit
python generate_from_url.py URL   # score one advert by link (--generate to write documents too)
python infomaniak.py              # list available models and check the credentials
python adzuna.py                  # run the Adzuna searches and fetch their descriptions
python alerts.py --suggest        # the LinkedIn job alerts that match search_criteria.md
python ats_feeds.py               # read the employers' feeds in companies.yaml
python ats_feeds.py --detect URL  # the companies.yaml entry for an employer's job link
python outcomes.py                # apply LinkedIn's application-status emails now
python declines.py                # file the decline emails in DECLINE_FOLDER now
python link_queue.py add FILE|URL # queue collected job links (fetch: read them now)
python evaluate.py --classify-tracks [--all]  # file older jobs under a track
python evaluate.py --move-penalty # apply the move penalty to jobs already on To Do
```

## Notes

- **Windows**: set `SOFFICE_PATH` to your LibreOffice `soffice.exe` and start
  the dashboard with `start_dashboard.bat`.
- **How it works, and what not to break**: [AGENTS.md](AGENTS.md) — also the
  file coding agents (Claude Code, Codex and others) load as project
  instructions.
- **Fonts**: if an exported PDF does not match the Word original, LibreOffice
  could not see the template's fonts and substituted them. The converter warns
  when that happens; see `SOFFICE_VCLPLUGIN` in `.env.example`.
