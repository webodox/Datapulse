"""Alert notifications for Slack and Email when sources fail or data checks break.

Handles Slack Block Kit webhooks, SMTP/email dispatch, validation, error handling,
and intelligent break detection so alerts only fire when something new breaks.
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("datapulse.notify")

EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")
URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def validate_slack_webhook(url: Optional[str]) -> str:
    """Validate a Slack incoming webhook URL.

    Allows standard Slack webhook URLs (https://hooks.slack.com/...) as well as
    compatible webhook targets (e.g. mock test endpoints or local proxies).
    """
    if not url or not url.strip():
        raise ValueError("Slack webhook URL cannot be empty.")
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Slack webhook URL must start with https:// or http:// and have a valid domain.")
    if not URL_RE.match(cleaned):
        raise ValueError("Slack webhook URL contains invalid characters.")
    return cleaned


def validate_email(email: str) -> str:
    """Validate a single email address."""
    cleaned = email.strip().lower()
    if not EMAIL_RE.match(cleaned):
        raise ValueError(f"Invalid email address: '{email}'.")
    return cleaned


def parse_and_validate_emails(raw: Union[str, Iterable[str], None]) -> List[str]:
    """Parse comma/semicolon/newline-delimited string or iterable of emails and validate each."""
    if not raw:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,\s;]+", raw) if p.strip()]
    else:
        parts = [str(p).strip() for p in raw if str(p).strip()]

    validated = []
    seen = set()
    for item in parts:
        valid = validate_email(item)
        if valid not in seen:
            seen.add(valid)
            validated.append(valid)
    return validated


def is_new_break(
    previous_status: Optional[str],
    current_status: str,
    previous_error: Optional[str] = None,
    current_error: Optional[str] = None,
    previous_issues: Optional[Sequence[str]] = None,
    current_issues: Optional[Sequence[str]] = None,
    policy: str = "on_break",
) -> bool:
    """Determine whether the current status represents something new breaking.

    Policies:
    - 'always': Notify on every run regardless of status.
    - 'always_on_failure': Notify whenever the run failed or had an error.
    - 'on_break' (default): Notify when transitioning from healthy to broken,
      when an error changes or breaks overnight, or when new quality issues appear.
    """
    if policy == "always":
        return True
    if policy == "always_on_failure":
        return current_status in ("failed", "error")

    # Default policy: 'on_break'
    if current_status == "passed":
        return False

    # 1. First time running and failed/error, or previously passed and now broken
    if previous_status in (None, "passed"):
        return current_status in ("failed", "error")

    # 2. Source-level error (e.g. database down, fetch failed overnight)
    if current_status == "error":
        if previous_status != "error":
            return True
        # If the error message changed (e.g. new failure reason)
        prev_clean = (previous_error or "").strip()
        curr_clean = (current_error or "").strip()
        return prev_clean != curr_clean

    # 3. Quality check failed
    if current_status == "failed":
        if previous_status != "failed":
            return True
        # Check if new issues appeared that were not present previously
        prev_set = set(previous_issues or [])
        curr_set = set(current_issues or [])
        new_issues = curr_set - prev_set
        return len(new_issues) > 0

    return False


def format_slack_payload(
    project_name: str,
    source_name: str,
    status: str,
    summary: str,
    issues: Optional[List[Dict[str, Any]]] = None,
    error: Optional[str] = None,
    run_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Format a rich Slack Block Kit message payload with fallbacks and clean links."""
    is_err = status == "error"
    emoji = "💥" if is_err else "🚨" if status == "failed" else "✅"
    title_text = f"{emoji} DataPulse Alert: {source_name}"
    if is_err:
        headline = f"Source *{source_name}* broke overnight"
    elif status == "failed":
        headline = f"Quality checks failed for *{source_name}*"
    else:
        headline = f"Checks passed for *{source_name}*"

    fallback_text = f"{emoji} [DataPulse] {source_name} ({status.upper()}) in {project_name}: {summary}"

    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title_text[:150], "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": headline},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Project:*\n{project_name}"},
                {"type": "mrkdwn", "text": f"*Source:*\n{source_name}"},
                {"type": "mrkdwn", "text": f"*Status:*\n*{status.upper()}*"},
                {"type": "mrkdwn", "text": f"*Summary:*\n{summary[:200]}"},
            ],
        },
    ]

    # Error details if broken overnight
    if error:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Error details:*\n```{error[:1000]}```",
            },
        })

    # Top quality issues if checks failed
    if issues:
        issue_lines = []
        for i in issues[:6]:
            sev = str(i.get("severity", "warning")).upper()
            title = i.get("title") or i.get("name") or "Issue"
            detail = i.get("summary") or i.get("message") or ""
            issue_lines.append(f"• *[{sev}]* *{title}*{': ' + detail[:120] if detail else ''}")
        if len(issues) > 6:
            issue_lines.append(f"_...and {len(issues) - 6} more issues._")

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Issues detected:*\n" + "\n".join(issue_lines),
            },
        })

    # Dashboard or run link (carefully formatted Slack mrkdwn link)
    if dashboard_url and dashboard_url.strip():
        clean_url = dashboard_url.strip()
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"👉 <{clean_url}|Open {source_name} in DataPulse>",
            },
        })

    return {"text": fallback_text, "blocks": blocks}


def send_slack_alert(
    webhook_url: str,
    project_name: str,
    source_name: str,
    status: str,
    summary: str,
    issues: Optional[List[Dict[str, Any]]] = None,
    error: Optional[str] = None,
    run_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Send an alert to Slack via incoming webhook URL.

    Guarantees no unhandled exceptions: returns a structured status dictionary.
    """
    try:
        clean_url = validate_slack_webhook(webhook_url)
    except ValueError as e:
        return {"ok": False, "status_code": None, "message": str(e)}

    payload = format_slack_payload(
        project_name=project_name,
        source_name=source_name,
        status=status,
        summary=summary,
        issues=issues,
        error=error,
        run_id=run_id,
        dashboard_url=dashboard_url,
    )

    try:
        response = httpx.post(clean_url, json=payload, timeout=timeout)
        if response.status_code == 200 and response.text.strip().lower() in ("ok", ""):
            return {"ok": True, "status_code": 200, "message": "Slack notification sent successfully."}
        if response.status_code == 200:
            return {"ok": True, "status_code": 200, "message": f"Slack response: {response.text[:200]}"}
        return {
            "ok": False,
            "status_code": response.status_code,
            "message": f"Slack webhook returned HTTP {response.status_code}: {response.text[:200]}",
        }
    except httpx.TimeoutException:
        return {"ok": False, "status_code": None, "message": "Connection to Slack timed out."}
    except Exception as exc:
        return {"ok": False, "status_code": None, "message": f"Failed to send Slack alert: {str(exc)}"}


def format_email_content(
    project_name: str,
    source_name: str,
    status: str,
    summary: str,
    issues: Optional[List[Dict[str, Any]]] = None,
    error: Optional[str] = None,
    run_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
) -> tuple[str, str, str]:
    """Format subject, plain text body, and HTML body for an email alert."""
    badge = "BROKEN" if status == "error" else status.upper()
    subject = f"[DataPulse Alert] {source_name} {badge} ({project_name})"

    text_parts = [
        f"DataPulse Alert: {source_name}",
        f"Project: {project_name}",
        f"Status: {badge}",
        f"Summary: {summary}",
        "",
    ]
    if error:
        text_parts.extend(["Error details:", error, ""])
    if issues:
        text_parts.append(f"Issues detected ({len(issues)}):")
        for i in issues:
            sev = str(i.get("severity", "warning")).upper()
            title = i.get("title") or i.get("name") or "Issue"
            text_parts.append(f" - [{sev}] {title}: {i.get('summary') or ''}")
        text_parts.append("")
    if dashboard_url:
        text_parts.append(f"View in DataPulse: {dashboard_url}")

    text_body = "\n".join(text_parts)

    # HTML body with modern styling
    issues_html = ""
    if issues:
        items = "".join(
            f"<li style='margin-bottom:8px;'><strong>[{str(i.get('severity', 'warning')).upper()}] "
            f"{i.get('title') or i.get('name') or 'Issue'}</strong>: {i.get('summary') or ''}</li>"
            for i in issues[:10]
        )
        issues_html = f"<h3 style='color:#b91c1c;margin-top:16px;'>Issues Detected ({len(issues)})</h3><ul>{items}</ul>"

    error_html = ""
    if error:
        error_html = (
            f"<h3 style='color:#dc2626;margin-top:16px;'>Error Details</h3>"
            f"<pre style='background:#fef2f2;border:1px solid #fecaca;padding:12px;border-radius:6px;overflow-x:auto;font-size:13px;color:#991b1b;'>{error}</pre>"
        )

    link_html = ""
    if dashboard_url:
        link_html = (
            f"<p style='margin-top:24px;'><a href='{dashboard_url}' "
            f"style='display:inline-block;padding:10px 18px;background:#2563eb;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;'>View in DataPulse</a></p>"
        )

    status_color = "#dc2626" if status in ("error", "failed") else "#16a34a"

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1e293b;line-height:1.5;padding:20px;max-width:640px;margin:auto;">
      <div style="border-top:4px solid {status_color};border-radius:8px;background:#ffffff;box-shadow:0 1px 3px rgba(0,0,0,0.1);padding:24px;">
        <h2 style="margin-top:0;color:#0f172a;">DataPulse Alert: {source_name}</h2>
        <p style="margin:4px 0 16px 0;color:#64748b;font-size:14px;">Project: <strong>{project_name}</strong></p>
        <div style="background:#f8fafc;border-left:4px solid {status_color};padding:12px 16px;border-radius:4px;margin-bottom:16px;">
          <p style="margin:0;font-size:15px;"><strong>Status:</strong> <span style="color:{status_color};font-weight:bold;">{badge}</span> &mdash; {summary}</p>
        </div>
        {error_html}
        {issues_html}
        {link_html}
      </div>
    </body>
    </html>
    """

    return subject, text_body, html_body


def send_email_alert(
    emails: List[str],
    subject: str,
    text_body: str,
    html_body: str,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Send email alerts via SMTP.

    If SMTP is not configured in environment variables, logs the alert cleanly
    and returns a success status with logged=True so development and local setups don't crash.
    """
    valid_emails = parse_and_validate_emails(emails)
    if not valid_emails:
        return {"ok": False, "delivered": False, "message": "No valid email recipients provided."}

    smtp_host = os.getenv("DATAPULSE_SMTP_HOST", "").strip()
    if not smtp_host:
        logger.warning(
            "SMTP host not configured (DATAPULSE_SMTP_HOST). Logged alert for %s: %s",
            valid_emails,
            subject,
        )
        return {
            "ok": True,
            "delivered": False,
            "logged": True,
            "message": f"SMTP host not configured. Alert logged for {len(valid_emails)} recipient(s).",
        }

    smtp_port = int(os.getenv("DATAPULSE_SMTP_PORT", "587"))
    smtp_user = os.getenv("DATAPULSE_SMTP_USER", "").strip()
    smtp_password = os.getenv("DATAPULSE_SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("DATAPULSE_SMTP_FROM", smtp_user or "alerts@datapulse.local").strip()
    use_tls = os.getenv("DATAPULSE_SMTP_TLS", "true").lower() in ("true", "1", "yes")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = ", ".join(valid_emails)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as server:
            if use_tls:
                server.starttls()
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return {"ok": True, "delivered": True, "message": f"Email alert sent to {len(valid_emails)} recipient(s)."}
    except Exception as exc:
        logger.error("Failed to send email alert: %s", exc)
        return {"ok": False, "delivered": False, "message": f"Failed to send email: {str(exc)}"}


def send_break_alert(
    notify_config: Dict[str, Any],
    project_name: str,
    source_name: str,
    status: str,
    summary: str,
    issues: Optional[List[Dict[str, Any]]] = None,
    error: Optional[str] = None,
    run_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Send alerts across configured channels (Slack, Email) according to notify_config."""
    results: Dict[str, Any] = {}

    slack_url = (notify_config.get("slack_webhook_url") or "").strip()
    if slack_url:
        results["slack"] = send_slack_alert(
            webhook_url=slack_url,
            project_name=project_name,
            source_name=source_name,
            status=status,
            summary=summary,
            issues=issues,
            error=error,
            run_id=run_id,
            dashboard_url=dashboard_url,
        )

    emails = notify_config.get("emails") or []
    if isinstance(emails, str):
        emails = parse_and_validate_emails(emails)
    if emails:
        subject, text_body, html_body = format_email_content(
            project_name=project_name,
            source_name=source_name,
            status=status,
            summary=summary,
            issues=issues,
            error=error,
            run_id=run_id,
            dashboard_url=dashboard_url,
        )
        results["email"] = send_email_alert(
            emails=emails,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    return results
