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
| **Scrape** | `scraper.py` walks the searches in `search_criteria.md` and stores anything new. |
| **Score** | `evaluate.py` scores each job 1-10 against your dossier and `rules.md`. At or above `MIN_PASS_SCORE` it lands on **To Do**; below, on **Rejected**. Re-scoring never moves a job you approved or applied to. |
| **Approve** | You press **Approve** on the dashboard. *Only then* are documents written — nothing is generated for a job you will never send. |
| **Apply** | **Apply Now** fills the employer's form and submits it; **Fill Now** does the same but stops at the submit button. Every step is screenshotted. LinkedIn **Easy Apply** is never automated — those jobs get a direct link for you to apply by hand. |

The nightly run stops after scoring. Pass `--gen` to `main.py` if you want it to
generate for everything on To Do the old way.

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
   (`INFOMANIAK_API_TOKEN` + `INFOMANIAK_PRODUCT_ID`, `GEMINI_API_KEY` or
   `GROQ_API_KEY` — any one is enough) and `CVS_DIR`. Then copy
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

Everything personal sits in `CVS_DIR` and is never committed:

| File | What it is |
|---|---|
| `docs/<CV_TEMPLATE_NAME>` | Your CV. Keep the skills in one paragraph separated by ` · ` — the generator's prompt has the model rewrite that paragraph whole. |
| `docs/<CL_TEMPLATE_NAME>` | Cover letter template. Must keep `[COMPANY]`, `[LOCATION]` and `[DATE]` in the addressee block. |
| `docs/<DOSSIER_NAME>` | Career history in markdown. The scorer and generator both read it. |
| `search_criteria.md` | Keywords, locations and job types. A keyword can pin its own: `- Interim Manager @ Switzerland / France`. |
| `rules.md` | Scoring rules, injected into the evaluator prompt. |
| `profile.yaml` | Answer bank for application forms — identity, work authorisation, salary and contract rates, screening answers. The applier never invents an answer; anything missing is reported so you can add it here. |
| `attach/` | Diplomas, reference letters and anything else a form may ask to upload, wired up under `attachments:` in `profile.yaml`. |
| `applications/` | Generated documents, the screenshot trail, and a `<job_id>_run.json` manifest per application. |

`search_criteria.md` and `rules.md` are editable from the dashboard's
**⚙️ Search Settings** button, which validates and reports what the next run
will search for.

## Using the dashboard

- **+** — paste any job URL (LinkedIn or an employer's own page). It reads the
  advert, scores it, and puts it on To Do whatever it scores; approving it there
  writes the documents.
- **⚙️ Search Settings** — edit the keywords and scoring rules.
- **Run Pipeline / Run Evaluations Only** — trigger a run by hand.
- **📤 Apply to Approved / 📝 Fill Approved** — work through every approved job.
  Apply is greyed out when `policies.never_submit_without_review` is `true` in
  `profile.yaml`; hover it to see why. Fill always works.
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
