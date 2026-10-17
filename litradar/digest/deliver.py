"""Delivery: e-mail (SMTP), Slack webhook. Both optional; both read credentials from Settings.

E-mail security notes
* STARTTLS and implicit TLS (port 465) both use ``ssl.create_default_context()``: certificate and
  hostname are verified. The standard library's default for ``smtplib.SMTP.starttls()`` is an
  *unverified* context, which would hand the SMTP credentials to any on-path host.
* The HTML alternative is built from the Markdown digest, which contains text copied from third
  parties (paper titles, abstracts, news headlines). When the optional ``markdown`` package is
  installed its output is passed through :func:`sanitize_html`, which keeps only a small allow-list
  of formatting tags and strips every attribute except ``href`` (http/https/mailto only). Without
  the package the digest is escaped into a ``<pre>`` block. The plain-text alternative is sent as is.
* A delivery failure is logged and reported as False; it never raises, so a finished digest is
  never lost to an SMTP hiccup.
"""
from __future__ import annotations

import html
import json
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

import requests

from ..config import Settings

log = logging.getLogger(__name__)

ALLOWED_TAGS = frozenset(
    {"a", "b", "strong", "em", "i", "sub", "sup", "p", "h1", "h2", "h3", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "br", "code", "pre"}
)
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)([^<>]*?)(/?)>", re.S)
_HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
_SAFE_HREF_RE = re.compile(r"^(https?://|mailto:)", re.I)
_DROP_WITH_CONTENT_RE = re.compile(r"<(script|style|iframe|object|embed)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _rebuild_tag(m: re.Match) -> str:
    closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3) or ""
    if name not in ALLOWED_TAGS:
        return ""
    if closing:
        return f"</{name}>"
    if name == "a":
        hm = _HREF_RE.search(attrs)
        href = (hm.group(1) or hm.group(2) or hm.group(3) or "").strip() if hm else ""
        if href and _SAFE_HREF_RE.match(href):
            return f'<a href="{html.escape(href, quote=True)}">'
        return "<a>"
    if name == "br":
        return "<br>"
    return f"<{name}>"


def sanitize_html(fragment: str) -> str:
    """Keep only ALLOWED_TAGS (attributes dropped, except a safe href on <a>); strip everything else.

    Script/style/iframe/object/embed elements are removed together with their content; comments are
    removed; any other unknown tag is removed but its text is kept. The result is still HTML, so text
    nodes keep whatever entity escaping the converter produced.
    """
    s = _COMMENT_RE.sub("", fragment or "")
    s = _DROP_WITH_CONTENT_RE.sub("", s)
    return _TAG_RE.sub(_rebuild_tag, s)


def markdown_to_html(md: str) -> str:
    """Convert the digest Markdown to e-mail HTML.

    With the optional ``markdown`` package: convert with the ``tables`` extension, then sanitise the
    result (see :func:`sanitize_html`). Without it: escape the Markdown and wrap it in ``<pre>``.
    """
    try:
        import markdown  # optional dependency
    except ImportError:
        return "<pre style='font-family: ui-monospace, monospace; white-space: pre-wrap'>" + html.escape(md or "", quote=False) + "</pre>"
    return sanitize_html(markdown.markdown(md or "", extensions=["tables"]))


def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


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
    context = _tls_context()
    try:
        if int(settings.smtp_port) == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=60) as s:
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as s:
                s.starttls(context=context)
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        log.error("e-mail delivery failed (%s: %s); the digest file is still on disk", type(exc).__name__, exc)
        return False
    log.info("e-mail sent to %s", msg["To"])
    return True


def send_slack(settings: Settings, text: str, blocks_md: Optional[str] = None) -> bool:
    if not settings.slack_webhook_url:
        log.warning("Slack not configured (SLACK_WEBHOOK_URL)")
        return False
    payload = {"text": text[:3000]}
    try:
        resp = requests.post(settings.slack_webhook_url, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=30)
    except requests.RequestException as exc:
        log.warning("Slack webhook failed: %s", exc)
        return False
    ok = resp.status_code == 200
    if not ok:
        log.warning("Slack webhook returned %s", resp.status_code)
    return ok
