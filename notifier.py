import os
import email.utils
import re
import smtplib
from email.message import EmailMessage
import markdown
import db
import urllib.parse

def _score(score):
    """A composite score to two decimals, as the board shows it."""
    try:
        return f'{float(score):.2f}'
    except (TypeError, ValueError):
        return str(score)


def dashboard_url():
    """Base URL for links that have to work from another device."""
    return os.environ.get('DASHBOARD_URL', 'http://localhost:5050').rstrip('/')


def remote_dashboard_url():
    """A second address for the dashboard - e.g. over Tailscale - for when the
    local one does not resolve (away from home). Empty when not configured."""
    return os.environ.get('DASHBOARD_REMOTE_URL', '').strip().rstrip('/')


def dashboard_links(path='', label='Open the dashboard'):
    """Markdown link(s) to a dashboard page: the local address, plus the remote
    one when DASHBOARD_REMOTE_URL is set."""
    links = [f"[{label}]({dashboard_url()}/{path})"]
    if remote_dashboard_url():
        links.append(f"[remote]({remote_dashboard_url()}/{path})")
    return " | ".join(links)


def _job_links(job_id):
    """The ' - [View Details] | [Site]' suffix for a job line.

    No Adzuna links: its tracking redirects (/land/ad/) got whole summaries
    silently dropped on the way to Gmail - the same email arrived once they
    were removed. The dashboard link reaches every job anyway.
    """
    if not job_id:
        return ""
    links = [dashboard_links(f'#job={job_id}', 'View Details')]
    try:
        link = db.get_job_links(job_id).get('linkedin') or ''
        host = urllib.parse.urlparse(link).netloc.lower()
        if host and not re.search(r'(^|\.)adzuna\.', host):
            label = 'LinkedIn' if host.endswith('linkedin.com') else host.removeprefix('www.')
            links.append(f"[{label}]({link})")
    except Exception:
        pass
    return " - " + " | ".join(links)


def format_summary(duration, keyword_stats, eval_stats, status_counts, source_notes=None):
    lines = []
    lines.append("# AI Job Application Pipeline Summary\n")
    lines.append(f"**Total Duration:** {duration}\n")

    if source_notes:
        lines.append("## Sources")
        lines.extend(f"- {note}" for note in source_notes)
        lines.append("")

    lines.append("## New Jobs Found")
    if keyword_stats:
        total_scraped = sum(keyword_stats.values())
        lines.append(f"- **Total**: {total_scraped}")
        for kw, count in keyword_stats.items():
            lines.append(f"- *{kw}*: {count}")
    else:
        lines.append("- No new jobs found (or the search step was skipped).")
    lines.append("")
        
    lines.append("## Evaluation Stats\n")
    score_counts = eval_stats.get('score_counts', {})
    total_evaluated = sum(score_counts.values())
    lines.append(f"**Total Jobs Evaluated:** {total_evaluated}\n")
    if total_evaluated > 0:
        for score in range(1, 11):
            if score_counts.get(score, 0) > 0:
                lines.append(f"- **Score {score}-{score}.99**: {score_counts[score]}")
    lines.append("")
                
    recent_backlog = eval_stats.get('recent_backlog', [])
    if recent_backlog:
        lines.append(f"## New on To Do (scored {os.environ.get('MIN_PASS_SCORE', 7)}+)\n")
        for job in recent_backlog:
            job_id = job.get('job_id')
            title = job['title']
            company = job['company']
            score = job['score']
            
            lines.append(f"- **[{_score(score)}/10]** {title} @ **{company}**" + _job_links(job_id))
    lines.append("")
            
    # Near misses: without these, a run with no 9s has nothing to click.
    near_misses = eval_stats.get('near_misses', [])
    if near_misses:
        lines.append(f"## Worth a look (scored {os.environ.get('NEAR_MISS_SCORE', 6)} to just under "
                     f"{os.environ.get('MIN_PASS_SCORE', 7)})\n")
        for job in sorted(near_misses, key=lambda j: -j['score'])[:10]:
            lines.append(f"- **[{_score(job['score'])}/10]** {job['title']} @ **{job['company']}**"
                         + _job_links(job.get('job_id')))
        lines.append("")

    lines.append("## Pipeline Status (Total Backlog)\n")
    if status_counts:
        for status, count in status_counts.items():
            lines.append(f"- **{status}**: {count}")
    else:
        lines.append("- No status counts available.")

    lines.append(f"\n---\n{dashboard_links()}")

    return "\n".join(lines)

def send_email(subject, md_body):
    """Send a markdown-formatted email using the SMTP settings from .env."""
    smtp_host = os.environ.get('SMTP_HOST')
    smtp_port = os.environ.get('SMTP_PORT', 465)
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASS')
    smtp_from = os.environ.get('SMTP_FROM')
    smtp_to = os.environ.get('SMTP_TO')
    
    if not all([smtp_host, smtp_user, smtp_pass, smtp_from, smtp_to]):
        print("Note: Email notification skipped due to missing SMTP configuration in .env.")
        return
        
    if smtp_user in ["your-email@gmail.com", "your-email@infomaniak.com"] and (not smtp_pass or smtp_pass == "your-email-password" or smtp_pass == "your-app-password"):
        print("Note: Email notification skipped because SMTP credentials are still the default placeholders.")
        return

    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = smtp_from
        msg['To'] = smtp_to
        # A message with no Date or Message-ID looks like spam to receiving
        # servers; two nightly summaries accepted by SMTP never reached Gmail.
        msg['Date'] = email.utils.formatdate(localtime=True)
        msg['Message-ID'] = email.utils.make_msgid(domain=(smtp_from.rsplit('@', 1)[-1] or None))

        msg.set_content(md_body)
        msg.add_alternative(markdown.markdown(md_body), subtype='html')
        
        # Use SSL since default is port 465 usually
        if int(smtp_port) == 465:
            server = smtplib.SMTP_SSL(smtp_host, int(smtp_port))
        else:
            server = smtplib.SMTP(smtp_host, int(smtp_port))
            server.starttls()
            
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        print(f"Successfully sent email: {subject}")
    except Exception as e:
        print(f"Failed to send email '{subject}': {e}")


def send_email_summary(duration, keyword_stats, eval_stats, status_counts, source_notes=None):
    md_summary = format_summary(duration, keyword_stats, eval_stats, status_counts, source_notes)
    print("\n" + md_summary + "\n")
    send_email('AI Job Scraper - Daily Summary', md_summary)
