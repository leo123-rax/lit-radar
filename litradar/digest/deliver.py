"""Delivery: e-mail (SMTP), Slack webhook. Both optional; both read credentials from Settings."""
from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

import requests

from ..config import Settings

log = logging.getLogger(__name__)


def markdown_to_html(md: str) -> str:
    try:
        import markdown  # optional dependency

        return markdown.markdown(md, extensions=["tables"])
    except ImportError:
        return "<pre style='font-family: ui-monospace, monospace; white-space: pre-wrap'>" + md.replace("<", "&lt;") + "</pre>"


def send_email(settings: Settings, subject: str, md: str) -> bool:
    if not (settings.smtp_host and settings.digest_to and settings.digest_from):
        log.warning("e-mail not configured (SMTP_HOST / DIGEST_FROM / DIGEST_TO)")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.digest_from
    msg["To"] = ", ".join(t.strip() for t in settings.digest_to.split(",") if t.strip())
    msg.set_content(md)
    msg.add_alternative(f"<html><body>{markdown_to_html(md)}</body></html>", subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as s:
        s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    log.info("e-mail sent to %s", msg["To"])
    return True


def send_slack(settings: Settings, text: str, blocks_md: Optional[str] = None) -> bool:
    if not settings.slack_webhook_url:
        log.warning("Slack not configured (SLACK_WEBHOOK_URL)")
        return False
    payload = {"text": text[:3000]}
    resp = requests.post(settings.slack_webhook_url, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=30)
    ok = resp.status_code == 200
    if not ok:
        log.warning("Slack webhook returned %s", resp.status_code)
    return ok
