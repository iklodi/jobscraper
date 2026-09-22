# Disclaimer — read before using this tool

This software is provided **as is, without warranty of any kind**. You use it
entirely **at your own risk**. The authors accept no liability for anything
that happens to your accounts, your applications, your data or your job search
as a result of running it.

## Your LinkedIn account is at risk

LinkedIn's User Agreement prohibits accessing the service with bots, scrapers
or other automated means, and copying data from it. In its default **full
mode**, this tool does exactly that: it drives a browser signed in to *your*
LinkedIn account to run searches and read job postings.

LinkedIn enforces this against the account. Consequences range from security
checkpoints and CAPTCHAs to temporary restriction and **permanent suspension**
— which would take your professional network with it. LinkedIn does not
distinguish a tool that helps you apply for jobs from one that harvests data.
Keeping volumes low reduces the chance of being flagged. It does not remove it.

This project is not affiliated with, endorsed by or connected to LinkedIn, or to
any employer or applicant-tracking system it interacts with.

## Safe mode

For this reason the tool is gaining a **safe mode**
(`JOBSCRAPER_MODE=safe`). In safe mode it never automates your logged-in
account. New jobs come from the **job-alert emails LinkedIn sends you**, and
job descriptions are read from public job pages with no session attached. See
the [safe-mode issues](https://github.com/iklodi/jobscraper/issues?q=label%3Asafe-mode)
for what is finished and what is still being built. Until it is complete, parts
of the pipeline still need full mode.

LinkedIn **Easy Apply is never automated** in either mode.

Safe mode lowers the risk to your account. It does not make scraping public
pages permitted everywhere, and it does not change your responsibility for how
you use the tool.

## Applications are sent in your name

- **You are responsible for everything submitted.** Review filled forms before
  they are sent. To make the tool fill forms but never submit them, set
  `policies.never_submit_without_review: true` in your `profile.yaml`.
- The applier answers only from your own `profile.yaml` and never invents an
  answer. The CVs and cover letters it writes are, however, produced by an AI
  model and **can misstate facts**. Read them before they go out.
- If you allow it, the applier **creates accounts on employers' sites** in your
  name. Their passwords are stored unencrypted on your machine, in
  `CVS_DIR/ats_accounts.yaml`.

## Your data leaves your machine

To score jobs and write documents, the tool sends your career dossier, your CV
text, your profile answers and job descriptions to **the AI providers you
configure**: Infomaniak, Google (Gemini) or Groq. Their own terms and privacy
policies apply. Screenshots, generated documents and the database stay local.
Summary emails go through the SMTP server you configure.

## Everything else

Scores are an AI model's opinion and will sometimes be wrong in both
directions. Laws on automated access, data protection and job applications
vary by country. Nothing here is legal advice. Check what applies to you.
