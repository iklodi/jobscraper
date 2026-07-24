import os
import smtplib
from email.message import EmailMessage
import markdown
import db
import urllib.parse

def format_summary(duration, keyword_stats, eval_stats, status_counts):
    lines = []
    lines.append("# AI Job Application Pipeline Summary\n")
    lines.append(f"**Total Duration:** {duration}\n")
    
    lines.append("## Scraping Stats (New Jobs Found)")
    if keyword_stats:
        total_scraped = sum(keyword_stats.values())
        lines.append(f"- **Total**: {total_scraped}")
        for kw, count in keyword_stats.items():
            lines.append(f"- *{kw}*: {count}")
    else:
        lines.append("- Scraping skipped or no new jobs found.")
    lines.append("")
        
    lines.append("## Evaluation Stats\n")
    score_counts = eval_stats.get('score_counts', {})
    total_evaluated = sum(score_counts.values())
    lines.append(f"**Total Jobs Evaluated:** {total_evaluated}\n")
    if total_evaluated > 0:
        for score in range(1, 11):
            if score_counts.get(score, 0) > 0:
                lines.append(f"- **Score {score}**: {score_counts[score]}")
    lines.append("")
                
    recent_backlog = eval_stats.get('recent_backlog', [])
    if recent_backlog:
        lines.append("## New Backlog Additions (Score 9+)\n")
        for job in recent_backlog:
            job_id = job.get('job_id')
            title = job['title']
            company = job['company']
            score = job['score']
            
            links_text = []
            if job_id:
                links_text.append(f"[View Details](http://100.88.206.96:5050/?job_id={job_id})")
                job_links = db.get_job_links(job_id)
                if job_links.get('linkedin'):
                    links_text.append(f"[LinkedIn]({job_links['linkedin']})")
            
            link_str = f" - {' | '.join(links_text)}" if links_text else ""
            lines.append(f"- **[{score}/10]** {title} @ **{company}**{link_str}")
    lines.append("")
            
    lines.append("## Pipeline Status (Total Backlog)\n")
    if status_counts:
        for status, count in status_counts.items():
            lines.append(f"- **{status}**: {count}")
    else:
        lines.append("- No status counts available.")
        
    return "\n".join(lines)

def send_email_summary(duration, keyword_stats, eval_stats, status_counts):
    # 1. Generate text
    md_summary = format_summary(duration, keyword_stats, eval_stats, status_counts)
    html_summary = markdown.markdown(md_summary)
    
    # 2. Print to console
    print("\n" + md_summary + "\n")
    
    # 3. Send email if configured
    smtp_host = os.environ.get('SMTP_HOST')
    smtp_port = os.environ.get('SMTP_PORT', 465)
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASS')
    smtp_from = os.environ.get('SMTP_FROM')
    smtp_to = os.environ.get('SMTP_TO')
    
    if not all([smtp_host, smtp_user, smtp_pass, smtp_from, smtp_to]):
        print("Note: Email notification skipped due to missing SMTP configuration in .env.")
        return
        
    if smtp_user in ["your-email@gmail.com", "your-email@infomaniak.com", "your-email@example.com"] and (not smtp_pass or smtp_pass == "your-email-password" or smtp_pass == "your-app-password"):
        print("Note: Email notification skipped because SMTP credentials are still the default placeholders.")
        return

    try:
        msg = EmailMessage()
        msg['Subject'] = 'AI Job Scraper - Daily Summary'
        msg['From'] = smtp_from
        msg['To'] = smtp_to
        
        msg.set_content(md_summary)
        msg.add_alternative(html_summary, subtype='html')
        
        # Use SSL since default is port 465 usually
        if int(smtp_port) == 465:
            server = smtplib.SMTP_SSL(smtp_host, int(smtp_port))
        else:
            server = smtplib.SMTP(smtp_host, int(smtp_port))
            server.starttls()
            
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        print("Successfully sent email summary.")
    except Exception as e:
        print(f"Failed to send email summary: {e}")
