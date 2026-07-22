# AI Job Application Pipeline - Handover Document

## Project Overview
This project is an end-to-end, AI-powered automation pipeline designed to streamline the job search process. It automatically scrapes job postings from LinkedIn based on predefined search criteria, uses Large Language Models (LLMs) to evaluate the job's fit against a candidate's profile, generates highly personalized CVs and Cover Letters for approved roles, and synchronizes the entire workflow to a GitHub repository Kanban board for easy tracking.

## Architecture & Core Components

The pipeline consists of several modular scripts that are orchestrated by a central `main.py` controller. The data state is maintained locally in an SQLite database (`jobs.db`).

### 1. The Controller (`main.py`)
Orchestrates the entire pipeline sequentially. It runs the scraper, triggers the evaluator, prompts document generation for approved jobs, builds a local dashboard, and finally syncs everything to GitHub.

### 2. The Scraper (`scraper.py`)
Uses **Playwright (Chromium)** to automate a headless (or headful) browser session on LinkedIn. 
- Searches for specific keywords and locations.
- Extracts job titles, companies, locations, and full job descriptions.
- Saves new, unseen jobs to the local SQLite database with a `pending` status.
- **Note:** Playwright navigation has a 60-second timeout and gracefully skips pages that fail to load to prevent pipeline crashes.

### 3. The Evaluator (`evaluator.py`)
Uses the **Gemini API** (or Groq as a fallback) to evaluate each `pending` job.
- Evaluates the job description against the user's Base CV and Career Dossier.
- Scores the job out of 10.
- If the job scores `MIN_PASS_SCORE` (default: 9) or higher, it promotes the job to `backlog` (or `to_apply` depending on configuration). Otherwise, it marks it as `rejected`.

### 4. The Generator (`generator.py`)
For jobs marked as `to_apply`, this script uses AI to generate tailored application documents.
- Uses **python-docx** to inject tailored professional summaries, custom bullet points, and rewritten Cover Letters directly into Word Document templates.
- Explicitly enforces generating the CV in English, while allowing the Cover Letter to be localized (e.g., to French) if the job description demands it.
- Uses **LibreOffice** headlessly to convert the generated `.docx` files to `.pdf`.
- Saves the original job description as a PDF using Playwright.
- Updates the job status to `generated`.

### 5. The Web Dashboard (`app.py`)
Provides a sleek, modern, and mobile-friendly local Kanban dashboard running on Flask.
- Displays all evaluated jobs in categorized lanes (Approved, Backlog, Applied, Rejected).
- Allows users to seamlessly drag and drop jobs between lanes to track their application status.
- Automatically saves state changes to the local SQLite database.

---

## Setup & Prerequisites

### 1. Environment Variables (`.env`)
The pipeline relies on several API keys. You must have a `.env` file in the root directory:
```env
GROQ_API_KEY="your_groq_key"
GEMINI_API_KEY="your_gemini_key"
GITHUB_TOKEN="your_github_personal_access_token"
GITHUB_REPO="your_username/your_repo"
MIN_PASS_SCORE=9
```

### 2. Dependencies
- **Python 3.14+**
- **Playwright:** Must run `playwright install chromium` after installing python dependencies.
- **LibreOffice:** Set the `SOFFICE_PATH` environment variable in your `.env` file to point to your LibreOffice executable (e.g., `/Applications/LibreOffice.app/Contents/MacOS/soffice` for Mac, or `"C:\Program Files\LibreOffice\program\soffice.exe"` for Windows).

### 3. Base Documents
The generator requires base documents to function:
- **Base CV (.docx):** Must contain a specific `SKILLS` paragraph separated by ` · ` for replacement logic.
- **Career Dossier (.md):** A detailed markdown file containing the user's deep career history to give the AI context.
- **Cover Letter Template (.docx):** A base template for the AI to rewrite.

---

## Deployment & Execution Workflow

### Running Locally
To run the entire pipeline, simply execute:
```bash
python main.py
```
This will step through scraping, evaluating, generating, and syncing sequentially.

### Automation
This pipeline is designed to run locally on a residential IP address (like your MacBook) to avoid LinkedIn's aggressive datacenter IP bans.
- **Recommendation:** Use macOS `crontab` or `launchd` to schedule `main.py` to run daily.
- If your LinkedIn session expires, `scraper.py` running locally will pop open a browser window allowing you to manually log back in and solve CAPTCHAs.

---

## Maintenance & Quirks

1. **LinkedIn Throttling:** LinkedIn search pages sometimes time out. The scraper will wait 60 seconds before printing a warning and skipping the page.
2. **AI Strictness:** The generator prompt contains very strict rules (e.g., "ALL CV content MUST be written in ENGLISH", "NEVER use the company name in the cover letter"). If the AI hallucinates or ignores instructions, the prompt in `generator.py` needs to be adjusted.
3. **Docx Replacements:** `python-docx` is notoriously finicky with formatting. The CV skills replacement is configured to swap the *entire* text block at once to prevent the AI from accidentally duplicating or mis-formatting the paragraph. 
4. **Rate Limits:** If Gemini or Groq hit rate limits, the script will automatically pause for 30 seconds and retry.

---
*Generated by Antigravity*
