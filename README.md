# LinkedIn AI Job Scraper

This is an automated agent that scrapes LinkedIn, scores jobs using Gemini, and generates tailored CVs and Cover Letters.

## Initial Setup
1. **Activate the environment**:
   ```bash
   source venv/bin/activate
   ```

2. **Authenticate with LinkedIn**:
   Run the scraper manually the first time. It will open a browser window.
   ```bash
   python scraper.py
   ```
   Log into your LinkedIn account. The session will be saved in `./chrome_profile`. Once logged in, the script will scrape jobs.
   *After this first run, you can edit `scraper.py` and set `headless=True` to run it in the background.*

3. **Set Environment Variables**:
   You need API keys for Gemini and GitHub.
   ```bash
   export GEMINI_API_KEY="your-google-gemini-key"
   export GITHUB_TOKEN="your-github-personal-access-token"
   ```

## Running the Pipeline
Once authenticated and keys are set, you can run the entire pipeline:
```bash
python main.py
```

This will:
1. Scrape new jobs.
2. Evaluate them using Gemini (scores 1-10).
3. For jobs > 9, create a tailored CV and Cover Letter in `applications/` and convert them to PDF.
4. Open a GitHub Issue for each tailored application.
