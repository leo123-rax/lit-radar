"""Delivery: e-mail (SMTP), Slack webhook, GitHub Issue, Atom feed.

E-mail and Slack are optional and read their credentials from Settings. The GitHub Issue and the Atom
feed need no credential of their own: the issue is created through the ``gh`` CLI with the token a
GitHub Actions job already holds (``GH_TOKEN``), and the feed is a file written next to the dashboard.
Repository watchers (Watch -> Custom -> Issues) receive each digest issue by e-mail from GitHub; feed
readers subscribe to ``feed.xml`` on the Pages site. See docs/delivery.md.

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
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


# ---- zero-credential delivery: GitHub Issue and Atom feed ---------------------------------------
#
# Both reuse what a GitHub Actions run already has: the digest files in the checkout and the job's
# GITHUB_TOKEN, used through the `gh` CLI that is preinstalled on the hosted runners. Nothing to
# provision, nothing to rotate. Every gh call is a list-form subprocess (no shell); the issue body
# travels on stdin (--body-file -), so third-party text in the digest never reaches a command line.

ISSUE_TITLE_PREFIX = "Weekly digest "
ISSUE_BODY_LIMIT = 60_000       # GitHub caps an issue body at 65,536 characters; keep a margin
ISSUE_LABEL = "digest"
ISSUE_LIST_LIMIT = 200
FEED_ENTRIES = 12
FEED_CONTENT_CHARS = 2_000
ATOM_NS = "http://www.w3.org/2005/Atom"
GH_TIMEOUT = 60

_DIGEST_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_ISSUE_TITLE_RE = re.compile(r"^Weekly digest (\d{4}-\d{2}-\d{2})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RENDERED_RE = re.compile(r"_Rendered (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}) UTC")
_ISSUE_URL_RE = re.compile(r"/issues/(\d+)\s*$")
# characters XML 1.0 forbids even when escaped (control characters other than tab/newline/CR)
_XML_BAD_RE = re.compile("[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")

_run = subprocess.run  # module attribute so tests can substitute a fake runner


class GhError(Exception):
    """The GitHub CLI is missing, timed out or exited non-zero; the message is one line, fit for a log."""


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def delivery_config(radar: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``digest.delivery`` from radar.yaml with defaults; absent keys mean the channel is on."""
    raw = ((radar or {}).get("digest") or {}).get("delivery") or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        feed_entries = int(raw.get("feed_entries") or FEED_ENTRIES)
    except (TypeError, ValueError):
        feed_entries = FEED_ENTRIES
    return {
        "github_issue": _as_bool(raw.get("github_issue"), True),
        "close_previous": _as_bool(raw.get("close_previous"), True),
        "label": str(raw.get("label") or ISSUE_LABEL).strip() or ISSUE_LABEL,
        "feed_entries": max(1, feed_entries),
        "pages_url": str(raw.get("pages_url") or "").strip(),
    }


# ---- digest files -------------------------------------------------------------------------------
def list_digests(out_dir: Path) -> List[Path]:
    """Dated digest files (YYYY-MM-DD.md) newest first. latest.md, trends-*.md and the JSON summaries are ignored."""
    d = Path(out_dir)
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and _DIGEST_FILE_RE.match(p.name)]
    return sorted(files, key=lambda p: p.name, reverse=True)


def latest_digest(out_dir: Path, date: Optional[str] = None) -> Optional[Path]:
    """The newest dated digest, or the one for ``date`` (YYYY-MM-DD) when given; None when absent."""
    if date:
        p = Path(out_dir) / f"{date}.md"
        return p if p.is_file() and _DIGEST_FILE_RE.match(p.name) else None
    files = list_digests(out_dir)
    return files[0] if files else None


def digest_title(md: str, date: str) -> str:
    """The digest's H1 without the hashes, else ``Weekly digest <date>``."""
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip() or f"{ISSUE_TITLE_PREFIX}{date}"
        if s:
            break
    return f"{ISSUE_TITLE_PREFIX}{date}"


def digest_rendered_at(md: str, date: str) -> str:
    """RFC 3339 timestamp of the render (the digest's ``_Rendered YYYY-MM-DD HH:MM UTC_`` line), else midnight UTC of ``date``."""
    m = _RENDERED_RE.search(md or "")
    if m:
        return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:00Z"
    return f"{date}T00:00:00Z"


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- where things live --------------------------------------------------------------------------
@dataclass
class RepoLinks:
    """URLs the issue and the feed point at. ``repo`` is ``owner/name``."""

    repo: str
    branch: str = "main"
    server_url: str = "https://github.com"
    pages_url: str = ""
    digests_dir: str = "digests"

    def __post_init__(self) -> None:
        self.server_url = self.server_url.rstrip("/")
        if not self.pages_url:
            self.pages_url = f"https://{self.owner.lower()}.github.io/{self.name}/"
        if not self.pages_url.endswith("/"):
            self.pages_url += "/"

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1]

    def repo_url(self) -> str:
        return f"{self.server_url}/{self.repo}"

    def file_url(self, date: str) -> str:
        return f"{self.repo_url()}/blob/{self.branch}/{self.digests_dir}/{date}.md"

    def feed_url(self) -> str:
        return self.pages_url + "feed.xml"


def resolve_repo(explicit: Optional[str] = None) -> str:
    """``owner/name`` from the flag, ``GH_REPO``, ``GITHUB_REPOSITORY``, else ``gh repo view`` on the checkout."""
    for cand in (explicit, os.environ.get("GH_REPO"), os.environ.get("GITHUB_REPOSITORY")):
        cand = (cand or "").strip()
        if cand:
            if not _REPO_RE.match(cand):
                raise GhError(f"repository must be owner/name, got {cand!r}")
            return cand
    out = _gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], timeout=30).strip()
    if not _REPO_RE.match(out):
        raise GhError("could not determine the repository: pass --repo owner/name or set GH_REPO")
    return out


def resolve_links(radar: Optional[Dict[str, Any]], repo: Optional[str] = None, branch: Optional[str] = None,
                  pages_url: Optional[str] = None) -> RepoLinks:
    """Repository, branch and Pages URL: flags first, then the Actions environment, then radar.yaml, then defaults."""
    cfg = delivery_config(radar)
    return RepoLinks(
        repo=resolve_repo(repo),
        branch=(branch or os.environ.get("GITHUB_REF_NAME") or "main").strip(),
        server_url=(os.environ.get("GITHUB_SERVER_URL") or "https://github.com").strip(),
        pages_url=(pages_url or os.environ.get("LITRADAR_PAGES_URL") or cfg["pages_url"] or "").strip(),
    )


# ---- gh CLI -------------------------------------------------------------------------------------
def _gh(args: List[str], input_text: Optional[str] = None, timeout: int = GH_TIMEOUT) -> str:
    """Run ``gh <args>`` without a shell and return stdout; any failure becomes a one-line GhError."""
    cmd = ["gh", *args]
    try:
        proc = _run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise GhError("the GitHub CLI (gh) is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise GhError(f"gh {' '.join(args[:2])} timed out after {timeout}s") from None
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout or "").strip().split())[:400]
        raise GhError(f"gh {' '.join(args[:2])} exited {proc.returncode}: {detail or 'no output'}")
    return proc.stdout or ""


def _gh_json(args: List[str], timeout: int = GH_TIMEOUT) -> Any:
    out = _gh(args, timeout=timeout)
    try:
        return json.loads(out or "null")
    except ValueError as exc:
        raise GhError(f"gh {' '.join(args[:2])} returned invalid JSON: {exc}") from None


def find_digest_issues(repo: str) -> List[Dict[str, Any]]:
    """Issues titled ``Weekly digest YYYY-MM-DD`` (open and closed), newest date first; the title is the identity."""
    rows = _gh_json(["issue", "list", "-R", repo, "--state", "all", "--limit", str(ISSUE_LIST_LIMIT),
                     "--json", "number,title,state,url"])
    found = []
    for r in rows or []:
        m = _ISSUE_TITLE_RE.match(str(r.get("title") or "").strip())
        if m and r.get("number") is not None:
            found.append({"number": int(r["number"]), "title": r["title"], "state": str(r.get("state") or "").upper(),
                          "url": str(r.get("url") or ""), "date": m.group(1)})
    return sorted(found, key=lambda r: (r["date"], r["number"]), reverse=True)


def ensure_label(repo: str, label: str) -> bool:
    """Create the label if it is missing. Best effort: False (never an exception) when it exists or cannot be created."""
    try:
        _gh(["label", "create", label, "-R", repo, "--color", "0E8A16",
             "--description", "Weekly literature digest, delivered by the weekly workflow"])
        return True
    except GhError as exc:
        log.debug("label %r not created (%s)", label, exc)
        return False


# ---- the issue ----------------------------------------------------------------------------------
def issue_title(date: str) -> str:
    return f"{ISSUE_TITLE_PREFIX}{date}"


def build_issue_body(md: str, date: str, links: RepoLinks, limit: int = ISSUE_BODY_LIMIT) -> Tuple[str, bool]:
    """Issue body: a link line, the digest (cut at a line boundary to fit ``limit``), a footer. Returns (body, truncated)."""
    head = (f"_Full digest: [digests/{date}.md]({links.file_url(date)}) · "
            f"[trends dashboard]({links.pages_url}) · [Atom feed]({links.feed_url()})_\n\n")
    foot = ("\n\n---\n_Opened by the weekly workflow. Watch this repository with **Custom -> Issues** to get the "
            "next digest by e-mail; `docs/delivery.md` describes every delivery channel._\n")
    note = (f"\n\n_… truncated at {limit:,} characters; the complete digest is "
            f"[digests/{date}.md]({links.file_url(date)})._")
    text = (md or "").rstrip()
    budget = limit - len(head) - len(foot)
    truncated = len(text) > budget
    if truncated:
        budget -= len(note)
        cut = text.rfind("\n", 0, max(budget, 0))
        if cut < budget // 2:  # no usable line break: cut hard rather than lose half the body
            cut = budget
        text = text[:cut].rstrip() + note
    body = head + text + foot
    assert len(body) <= limit, (len(body), limit)
    return body, truncated


def deliver_github_issue(settings: Settings, date: Optional[str] = None, repo: Optional[str] = None,
                         branch: Optional[str] = None, pages_url: Optional[str] = None, dry_run: bool = False,
                         close_previous: Optional[bool] = None, limit: int = ISSUE_BODY_LIMIT) -> Dict[str, Any]:
    """Create or update the issue ``Weekly digest <date>`` for the newest (or the given) digest.

    Idempotent: an existing issue with the same title is edited in place and its open/closed state is
    left alone. With ``close_previous`` (default from radar.yaml, on), every still-open digest issue
    for an EARLIER date is closed with a comment linking the new one. The ``digest`` label is added on a
    best-effort basis (created when missing; a failure is logged, never fatal). Never raises: the result
    carries ``ok`` and ``error``. ``dry_run`` performs only the read-only lookup and returns the body.
    """
    cfg = delivery_config(settings.radar)
    result: Dict[str, Any] = {"ok": False, "action": None, "date": date, "title": None, "number": None, "url": None,
                              "closed": [], "body_chars": 0, "truncated": False, "label": None, "error": None,
                              "dry_run": dry_run}
    if not cfg["github_issue"]:
        result.update(ok=True, action="skipped", reason="digest.delivery.github_issue is false in radar.yaml")
        return result
    path = latest_digest(Path(settings.out_dir), date)
    if path is None:
        result["error"] = (f"no digest {date}.md in {settings.out_dir}" if date
                           else f"no dated digest (YYYY-MM-DD.md) in {settings.out_dir}")
        return result
    date = path.stem
    result["date"] = date
    try:
        links = resolve_links(settings.radar, repo=repo, branch=branch, pages_url=pages_url)
    except GhError as exc:
        result["error"] = str(exc)
        return result
    md = path.read_text(encoding="utf-8", errors="replace")
    title = issue_title(date)
    body, truncated = build_issue_body(md, date, links, limit=limit)
    result.update(title=title, body_chars=len(body), truncated=truncated, digest_chars=len(md), repo=links.repo,
                  file_url=links.file_url(date), pages_url=links.pages_url)
    close_prev = cfg["close_previous"] if close_previous is None else bool(close_previous)
    try:
        existing = find_digest_issues(links.repo)
    except GhError as exc:
        if not dry_run:
            result["error"] = f"could not list issues: {exc}"
            return result
        result["lookup_error"] = str(exc)
        existing = []
    same = [i for i in existing if i["date"] == date]
    older_open = [i for i in existing if i["date"] < date and i["state"] == "OPEN"]
    if dry_run:
        result.update(ok=True, action="would update" if same else "would create",
                      number=same[0]["number"] if same else None, url=same[0]["url"] if same else None,
                      would_close=[i["number"] for i in older_open] if close_prev else [], body=body, label=cfg["label"])
        return result
    try:
        if same:
            number, url = same[0]["number"], same[0]["url"]
            _gh(["issue", "edit", str(number), "-R", links.repo, "--title", title, "--body-file", "-"], input_text=body)
            action = "updated"
        else:
            ensure_label(links.repo, cfg["label"])
            out = _gh(["issue", "create", "-R", links.repo, "--title", title, "--body-file", "-"], input_text=body)
            url = out.strip().splitlines()[-1].strip() if out.strip() else ""
            m = _ISSUE_URL_RE.search(url)
            number = int(m.group(1)) if m else None
            action = "created"
    except GhError as exc:
        result["error"] = str(exc)
        return result
    result.update(ok=True, action=action, number=number, url=url)
    if number is None:
        log.warning("gh issue create returned no issue URL; label and close-previous skipped")
        return result
    try:
        _gh(["issue", "edit", str(number), "-R", links.repo, "--add-label", cfg["label"]])
        result["label"] = cfg["label"]
    except GhError as exc:
        log.warning("label %r not applied: %s", cfg["label"], exc)
    if close_prev:
        for prev in older_open:
            try:
                _gh(["issue", "close", str(prev["number"]), "-R", links.repo,
                     "--comment", f"Superseded by #{number} ({title}): {url}"])
                result["closed"].append(prev["number"])
            except GhError as exc:
                log.warning("could not close #%s: %s", prev["number"], exc)
                result.setdefault("close_errors", []).append({"number": prev["number"], "error": str(exc)})
    return result


# ---- the feed -----------------------------------------------------------------------------------
def _xml_text(s: str) -> str:
    return _XML_BAD_RE.sub("", s or "")


def feed_entries(out_dir: Path, links: RepoLinks, entries: int = FEED_ENTRIES,
                 content_chars: int = FEED_CONTENT_CHARS) -> List[Dict[str, str]]:
    """One record per recent digest: title, link, timestamps and the first ``content_chars`` characters."""
    rows = []
    for path in list_digests(out_dir)[: max(0, entries)]:
        date = path.stem
        md = path.read_text(encoding="utf-8", errors="replace")
        content = md[:content_chars] + ("…" if len(md) > content_chars else "")
        rows.append({"date": date, "title": digest_title(md, date), "url": links.file_url(date),
                     "published": f"{date}T00:00:00Z", "updated": digest_rendered_at(md, date), "content": content})
    return rows


def build_feed(out_dir: Path, links: RepoLinks, title: str, entries: int = FEED_ENTRIES,
               content_chars: int = FEED_CONTENT_CHARS, now: Optional[datetime] = None) -> str:
    """Atom 1.0 feed of the newest digests as a string (XML declaration included); valid with zero entries."""
    ET.register_namespace("", ATOM_NS)

    def el(parent: ET.Element, tag: str, text: Optional[str] = None, **attrs: str) -> ET.Element:
        e = ET.SubElement(parent, f"{{{ATOM_NS}}}{tag}", attrs)
        if text is not None:
            e.text = _xml_text(text)
        return e

    rows = feed_entries(out_dir, links, entries=entries, content_chars=content_chars)
    updated = max([r["updated"] for r in rows] or [_rfc3339(now or datetime.now(timezone.utc))])
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    el(feed, "title", title)
    el(feed, "subtitle", "Weekly digests, newest first")
    el(feed, "id", links.pages_url)
    el(feed, "link", rel="self", type="application/atom+xml", href=links.feed_url())
    el(feed, "link", rel="alternate", type="text/html", href=links.pages_url)
    el(feed, "updated", updated)
    el(el(feed, "author"), "name", title)
    el(feed, "generator", "lit-radar", uri=links.repo_url())
    for r in rows:
        entry = el(feed, "entry")
        el(entry, "title", r["title"])
        el(entry, "id", r["url"])
        el(entry, "link", rel="alternate", type="text/html", href=r["url"])
        el(entry, "link", rel="related", type="text/html", href=links.pages_url)
        el(entry, "published", r["published"])
        el(entry, "updated", r["updated"])
        el(entry, "content", r["content"], type="text")
    ET.indent(feed, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"


def write_feed(settings: Settings, path: Path, repo: Optional[str] = None, branch: Optional[str] = None,
               pages_url: Optional[str] = None, entries: Optional[int] = None) -> Dict[str, Any]:
    """Write the Atom feed for ``settings.out_dir`` to ``path``. Never raises; the result carries ``ok`` and ``error``."""
    cfg = delivery_config(settings.radar)
    n = entries or cfg["feed_entries"]
    try:
        links = resolve_links(settings.radar, repo=repo, branch=branch, pages_url=pages_url)
    except GhError as exc:
        return {"ok": False, "path": str(path), "entries": 0, "error": str(exc)}
    title = str(((settings.radar or {}).get("digest") or {}).get("title") or "Literature radar")
    xml = build_feed(Path(settings.out_dir), links, title, entries=n)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(xml, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "path": str(path), "entries": 0, "error": f"cannot write {path}: {exc}"}
    count = len(list_digests(Path(settings.out_dir))[:n])
    return {"ok": True, "path": str(path), "entries": count, "bytes": len(xml.encode("utf-8")), "url": links.feed_url()}


# ---- which channels are configured --------------------------------------------------------------
def channel_status(settings: Settings) -> Dict[str, Any]:
    """Presence only: which delivery channels would work from here. No value of any credential is included."""
    cfg = delivery_config(settings.radar)
    email_missing = [n for n, v in (("SMTP_HOST", settings.smtp_host), ("DIGEST_FROM", settings.digest_from),
                                    ("DIGEST_TO", settings.digest_to)) if not v]
    gh_path = shutil.which("gh")
    token_env = next((n for n in ("GH_TOKEN", "GITHUB_TOKEN") if (os.environ.get(n) or "").strip()), None)
    gh_auth: Optional[bool] = None
    if gh_path and not token_env:
        try:
            _gh(["auth", "status"], timeout=20)
            gh_auth = True
        except GhError:
            gh_auth = False
    repo = None
    feed_url = None
    try:
        links = resolve_links(settings.radar)
        repo, feed_url = links.repo, links.feed_url()
    except GhError:
        pass
    latest = latest_digest(Path(settings.out_dir))
    return {
        "email": {"configured": not email_missing, "missing": email_missing},
        "slack": {"configured": bool(settings.slack_webhook_url), "missing": [] if settings.slack_webhook_url else ["SLACK_WEBHOOK_URL"]},
        "github_issue": {"enabled": cfg["github_issue"], "close_previous": cfg["close_previous"], "label": cfg["label"],
                         "gh_installed": bool(gh_path), "token": token_env or ("gh auth" if gh_auth else None), "repo": repo},
        "feed": {"entries": cfg["feed_entries"], "url": feed_url},
        "latest_digest": str(latest) if latest else None,
        "digests": len(list_digests(Path(settings.out_dir))),
    }


def format_channel_status(report: Dict[str, Any]) -> str:
    e, s, g, f = report["email"], report["slack"], report["github_issue"], report["feed"]
    lines = ["delivery channels (presence only; no value is ever printed):"]
    lines.append("  e-mail (SMTP)   : " + ("configured" if e["configured"] else "not configured (missing " + ", ".join(e["missing"]) + ")"))
    lines.append("  Slack webhook   : " + ("configured" if s["configured"] else "not configured (missing SLACK_WEBHOOK_URL)"))
    gh_bits = [
        "enabled" if g["enabled"] else "disabled (digest.delivery.github_issue: false)",
        "gh installed" if g["gh_installed"] else "gh NOT installed",
        f"token: {g['token']}" if g["token"] else "token: none (set GH_TOKEN or run `gh auth login`)",
        f"repo: {g['repo']}" if g["repo"] else "repo: unknown (pass --repo or set GH_REPO)",
        f"label: {g['label']}",
        "closes previous" if g["close_previous"] else "keeps previous open",
    ]
    lines.append("  GitHub Issue    : " + "; ".join(gh_bits))
    lines.append(f"  Atom feed       : always available (`litradar deliver --feed site/feed.xml`, {f['entries']} entries)"
                 + (f"; published at {f['url']}" if f["url"] else ""))
    lines.append(f"latest digest: {report['latest_digest'] or 'none'} ({report['digests']} dated digest file(s))")
    return "\n".join(lines)
