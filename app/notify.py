"""Email notifications via Gmail SMTP (app password).
Minimum-necessary PHI: emails carry initials only + deep links to the access-controlled record."""
import smtplib
import socket

import requests
from email.message import EmailMessage

from . import config
from .retry import with_backoff


def _use_apps_script() -> bool:
    return bool(config.SHEETS_WEBHOOK_URL and config.SHEETS_WEBHOOK_SECRET) and config.EMAIL_TRANSPORT != "smtp"


def enabled() -> bool:
    return _use_apps_script() or bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD)


class RelayError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, RelayError):
        return exc.status in (408, 429) or exc.status >= 500
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    return isinstance(exc, (smtplib.SMTPException, socket.error, TimeoutError))


def send(subject: str, body: str, to: list[str], log=None):
    if not enabled() or not to:
        if log:
            log(f"email skipped (not configured): {subject}")
        return

    def _relay():
        r = requests.post(config.SHEETS_WEBHOOK_URL, timeout=45, json={
            "secret": config.SHEETS_WEBHOOK_SECRET, "action": "email",
            "to": ",".join(to), "subject": subject, "body": body})
        if r.status_code != 200:
            raise RelayError(r.status_code, r.text[:200])
        try:
            data = r.json()
        except ValueError:
            raise RelayError(502, "relay returned non-JSON")
        if not data.get("ok"):
            raise RuntimeError(f"Email relay error: {data.get('error')}")

    if _use_apps_script():
        return with_backoff(_relay, is_transient=_is_transient, label="email.relay", log=log)

    def _do():
        msg = EmailMessage()
        msg["From"] = config.GMAIL_USER
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            s.send_message(msg)

    with_backoff(_do, is_transient=_is_transient, label="email.send", log=log)


def initials(first: str | None, last: str | None) -> str:
    return f"{(first or '?')[:1]}.{(last or '?')[:1]}."


def new_order(run: dict, fields: dict, order_id: str, sheet_link: str, log=None):
    pt = initials(fields.get("patient_first_name"), fields.get("patient_last_name"))
    body = f"""New DME order received and recorded.

Order ID:            {order_id}
Patient:             {pt}  (full details in the order sheet)
Ordering provider:   {fields.get('ordering_provider')}
Equipment:           {fields.get('equipment_requested')}
Date of service:     {fields.get('date_of_service')}
Channel:             {run.get('channel')}
Review flags:        {'; '.join(fields.get('warnings') or []) or 'none'}

Open the record:     {sheet_link}
Pipeline run:        {config.PUBLIC_BASE_URL}/runs/{run['id']}
Status dashboard:    {config.PUBLIC_BASE_URL}/
"""
    send(f"[New DME Order] {order_id} - {fields.get('equipment_requested')}", body, config.NOTIFY_EMAIL_TO, log)


def needs_attention(run: dict, status: str, reason: str, log=None):
    """Alert for FAILED (system problem) and NEEDS_REVIEW / REJECTED (document problem)."""
    tag = "ALERT - Run Failed" if status == "FAILED" else "Action Needed - Document Review"
    body = f"""A DME intake run needs attention.

Status:     {status}
Run ID:     {run['id']}
File:       {run.get('filename')}
Channel:    {run.get('channel')}
Attempts:   {run.get('attempts')}
Reason:     {reason}

Run details:  {config.PUBLIC_BASE_URL}/runs/{run['id']}
Dashboard:    {config.PUBLIC_BASE_URL}/

Next steps: see RUNBOOK.md. Replay after fixing the cause:
  curl -X POST {config.PUBLIC_BASE_URL}/runs/{run['id']}/replay -H "X-API-Key: $INGEST_API_KEY"
"""
    send(f"[DME Intake] {tag}: {run.get('filename')}", body, config.ALERT_EMAIL_TO, log)
