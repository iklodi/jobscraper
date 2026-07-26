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
4. Present evaluated jobs in an interactive **Local Kanban Dashboard** so you can easily review them.

## Installation

### Windows Users
See [HANDOVER.md](HANDOVER.md) for detailed step-by-step Windows instructions.

### macOS / Linux Users
1. **Install Prerequisites**: Ensure Python 3, Git, and LibreOffice are installed.
2. **Virtual Environment**: Open terminal and run:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
3. **Install Dependencies**:
   ```cmd
   pip install -r requirements.txt
   playwright install chromium
   ```
4. **LibreOffice Requirement**: Download and install LibreOffice for Windows (required for DOCX to PDF conversion). 
   *Important*: Ensure you add `SOFFICE_PATH` to your `.env` file pointing to your Windows path (e.g., `SOFFICE_PATH="C:\Program Files\LibreOffice\program\soffice.exe"`).
5. **Configuration**: Create a `.env` file in the root directory (you can copy the contents of `.env.example`) and add your API keys and configuration.
6. **Start the Dashboard**: You can double-click the `start_dashboard.bat` file or run it in the command prompt to automatically activate the environment, launch the web server, and open the dashboard in your browser.

## Customizing Templates
The repository comes with default, fictitious templates out-of-the-box in the `cvs/docs/` directory. To use this tool for yourself, you must replace these with your own information:

1. **`cvs/docs/Base_CV_Template.docx`**: Replace this with your own CV. Ensure you have a paragraph named `Skills` where your skills are separated by a middle dot (` · `), as the AI uses this to dynamically inject tailored skills!
2. **`cvs/docs/Base_CL_Template.docx`**: Replace this with your own Cover Letter. **Crucial**: Keep the exact text `[COMPANY]`, `[LOCATION]`, and `[DATE]` in the addressee block so the AI can automatically replace them for each application.
3. **`cvs/docs/Career_Dossier.md`**: Update this markdown file with your own career history, strengths, and goals. The AI reads this to understand your background and answer "Why are you a good fit?" when rewriting the Cover Letter.
