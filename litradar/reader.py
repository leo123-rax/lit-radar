"""Auto-run reading queue: pick the day's subscription articles, read them, commit the FACTS.

Two halves, deliberately separated so that no article text ever leaves the machine that holds the
subscription session:

  local half  (`litradar read-queue`)  picks candidates from the published search index (or the local
      database), reads each one through the signed-in browser profile with `subscription.read_one`
      (facts-only storage, daily cap and pacing unchanged), and appends ONE FACTS RECORD per article
      to ``reads/<YYYY-MM-DD>.jsonl``. A facts record carries identifiers, which sections the article
      had, how long its Methods were, and the gazetteer mentions - never a sentence of the article.

  hosted half (`litradar reads-import`) reads those committed facts into whatever database
      ``LITRADAR_DB`` points at: it marks the paper as read under a personal subscription, records the
      sections that were present (as keys with empty text), replaces that paper's full-text mentions
      with the record's, and re-scores it.

So the session cookies and the article text stay on the reader's machine; the repository and the
hosted database only ever see "paper X names instrument Y in its Methods".

Everything here is inert without a signed-in session: with no session the queue reads nothing, says so,
and exits 0.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import REPO_ROOT, Settings
from .models import canonical_doi

log = logging.getLogger(__name__)

SCHEMA_NAME = "litradar-read-facts/1"
SNIPPET_MAX = 240  # hard ceiling on any excerpt that may appear in a committed facts record
DEFAULT_DAYS = 7
DEFAULT_LIMIT = 20
INDEX_FILENAME = "search-index.json"

# Journal -> publisher routing for the queue. The defaults are exactly what a personal subscription
# covers: the flagship journals. `reading.journals` in config/radar.yaml overrides this wholesale.
DEFAULT_JOURNALS: Dict[str, List[str]] = {"nature": ["Nature"], "science": ["Science"]}

# The Nature Research titles a reader MAY add to `reading.journals.nature` once they know their
# subscription's scope. They are NOT enabled by default: a personal Nature subscription covers the
# flagship journal only, and the rest are separate subscriptions (an unsubscribed title simply comes
# back paywalled, which costs a read against the daily cap for nothing).
NATURE_RESEARCH_TITLES: Tuple[str, ...] = (
    "Nature Methods", "Nature Biotechnology", "Nature Genetics", "Nature Medicine",
    "Nature Cell Biology", "Nature Chemical Biology", "Nature Structural & Molecular Biology",
    "Nature Immunology", "Nature Neuroscience", "Nature Cancer", "Nature Microbiology",
    "Nature Metabolism", "Nature Aging", "Nature Biomedical Engineering", "Nature Chemistry",
    "Nature Materials", "Nature Nanotechnology", "Nature Physics", "Nature Machine Intelligence",
    "Nature Computational Science", "Nature Ecology & Evolution", "Nature Plants",
    "Nature Protocols", "Nature Reviews Genetics", "Nature Reviews Drug Discovery",
    "Nature Reviews Cancer", "Nature Reviews Bioengineering", "Nature Communications",
)
# The Science family, same rule: AAAS sells these separately from Science itself.
SCIENCE_FAMILY_TITLES: Tuple[str, ...] = (
    "Science Advances", "Science Translational Medicine", "Science Immunology",
    "Science Robotics", "Science Signaling",
)

# Mention sections that come from full text, i.e. the ones a read article is the authority on.
FULLTEXT_SECTIONS: Tuple[str, ...] = ("methods", "funding", "coi", "ack", "data_availability")

# What a facts record's `status` says about the read behind it:
#   ok              the article had a real Methods section; the mentions are the article's own
#   preview         an unedited "accelerated article preview": no Methods yet, nothing to extract. The DOI is
#                   NOT finished - it goes back in the queue after `preview_retry_days` so the edited version
#                   is read when it appears. The record carries no mentions.
#   supplement_only the Methods point at the Supplementary Information, so only a few hundred characters were
#                   on the page. Those facts are real but thin; the DOI is finished (the supplement is not read).
STATUS_OK, STATUS_PREVIEW, STATUS_SUPPLEMENT = "ok", "preview", "supplement_only"
FINISHED_STATUSES = (STATUS_OK, STATUS_SUPPLEMENT)
PREVIEW_RETRY_DAYS = 7
PREVIEW_MAX_ATTEMPTS = 3  # after this many sightings a preview stops being re-queued
PREVIEW_MARKER = "accelerated article preview"

_SAFE_NOTIFY = re.compile(r"[^A-Za-z0-9 ,.:;()/_'-]+")


# ---------------------------------------------------------------------------------- configuration
def reading_cfg(settings: Settings) -> Dict[str, Any]:
    cfg = (settings.radar or {}).get("reading")
    return dict(cfg) if isinstance(cfg, dict) else {}


def journal_map(settings: Settings) -> Dict[str, str]:
    """{lower-cased journal name: publisher key} from `reading.journals`, else the flagship defaults."""
    configured = reading_cfg(settings).get("journals")
    source = configured if isinstance(configured, dict) and configured else DEFAULT_JOURNALS
    out: Dict[str, str] = {}
    for publisher, titles in source.items():
        for title in titles or []:
            name = str(title).strip().lower()
            if name:
                out[name] = str(publisher)
    return out


def publisher_for_journal(journal: Optional[str], jmap: Dict[str, str]) -> str:
    """Publisher key for a journal name ('' when the journal is not in the reading list).

    Matching is exact on the lower-cased name: "Nature" must not pull in "Nature Methods".
    """
    return jmap.get(str(journal or "").strip().lower(), "")


def git_remote_slug(repo_root: Path, runner: Optional[Callable[..., Any]] = None) -> str:
    """``owner/name`` of the git remote, or '' (used to derive the published index URL)."""
    run = runner or subprocess.run
    try:
        out = run(["git", "-C", str(repo_root), "remote", "get-url", "origin"],
                  capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    url = (getattr(out, "stdout", "") or "").strip()
    if not url:
        return ""
    url = re.sub(r"\.git$", "", url)
    m = re.search(r"[:/]([^/:]+)/([^/]+)$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def index_url(settings: Settings, override: Optional[str] = None, repo_root: Optional[Path] = None,
              runner: Optional[Callable[..., Any]] = None) -> str:
    """Where the published search index lives: --index-url, else `reading.index_url`, else
    ``LITRADAR_INDEX_URL``, else derived from the git remote as the project's GitHub Pages URL."""
    if override:
        return override
    configured = str(reading_cfg(settings).get("index_url") or "").strip()
    if configured:
        return configured
    env = (os.environ.get("LITRADAR_INDEX_URL") or "").strip()
    if env:
        return env
    slug = git_remote_slug(Path(repo_root or REPO_ROOT), runner=runner)
    if not slug:
        return ""
    owner, name = slug.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/{INDEX_FILENAME}"


def reads_dir(settings: Optional[Settings] = None, repo_root: Optional[Path] = None) -> Path:
    """The repository directory the committed facts files live in (``reads/``)."""
    root = Path(repo_root or REPO_ROOT)
    if settings is not None:
        configured = str(reading_cfg(settings).get("reads_dir") or "").strip()
        if configured:
            p = Path(configured)
            return p if p.is_absolute() else root / p  # a relative setting is relative to the repository
    return root / "reads"


# ---------------------------------------------------------------------------------- candidates
@dataclass
class Candidate:
    doi: str
    title: str = ""
    journal: str = ""
    pub_date: str = ""
    publisher: str = ""
    score: float = 0.0
    paper_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _record_field(rec: Dict[str, Any], *names: str) -> Any:
    for n in names:
        if rec.get(n) not in (None, ""):
            return rec[n]
    return ""


def fetch_index(url: str, opener: Optional[Callable[[str], str]] = None, timeout: int = 60) -> Dict[str, Any]:
    """Download and parse the published search index. `opener(url) -> text` is injectable for tests."""
    if opener is not None:
        text = opener(url)
    elif url.startswith("file://") or "://" not in url:
        text = Path(url[len("file://"):] if url.startswith("file://") else url).read_text(encoding="utf-8")
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "lit-radar read-queue"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https URL from configuration
            text = resp.read().decode("utf-8", "replace")
    payload = json.loads(text)
    return payload if isinstance(payload, dict) else {"records": payload}


def candidates_from_index(payload: Dict[str, Any], jmap: Dict[str, str], days: int = DEFAULT_DAYS,
                          today: Optional[date] = None) -> List[Candidate]:
    """Index records in the window whose journal routes to a publisher, best score first.

    Both spellings of the index schema are read: the compact keys the dashboard writes (``t``, ``j``,
    ``d``, ``s``) and the long ones (``title``, ``journal``, ``pub_date``, ``score``).
    """
    today = today or datetime.now(timezone.utc).date()
    start = (today - timedelta(days=max(1, int(days)) - 1)).isoformat()
    end = today.isoformat()
    out: List[Candidate] = []
    for rec in payload.get("records") or []:
        if not isinstance(rec, dict):
            continue
        journal = str(_record_field(rec, "j", "journal") or "")
        publisher = publisher_for_journal(journal, jmap)
        if not publisher:
            continue
        pub_date = str(_record_field(rec, "d", "pub_date", "date") or "")
        if not (start <= pub_date[:10] <= end):
            continue
        doi = canonical_doi(str(_record_field(rec, "doi") or "")) or ""
        if not doi:
            continue
        try:
            score = float(_record_field(rec, "s", "score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        out.append(Candidate(doi=doi, title=str(_record_field(rec, "t", "title") or ""), journal=journal,
                             pub_date=pub_date[:10], publisher=publisher, score=score,
                             paper_id=str(rec.get("id") or "")))
    return _ordered(out)


def candidates_from_db(db_path: Path, jmap: Dict[str, str], days: int = DEFAULT_DAYS,
                       today: Optional[date] = None) -> List[Candidate]:
    """The same list, read from a local database instead of the published index (``--source db``)."""
    today = today or datetime.now(timezone.utc).date()
    start = (today - timedelta(days=max(1, int(days)) - 1)).isoformat()
    end = today.isoformat()
    path = Path(db_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"no database at {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # pragma: no cover - a build without URI support
        conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT p.id, p.doi, p.title, p.journal, p.pub_date, s.final FROM papers p "
            "LEFT JOIN scores s ON s.paper_id = p.id WHERE p.pub_date >= ? AND p.pub_date <= ?",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    out: List[Candidate] = []
    for pid, doi, title, journal, pub_date, final in rows:
        publisher = publisher_for_journal(journal, jmap)
        cdoi = canonical_doi(str(doi or "")) or ""
        if not publisher or not cdoi:
            continue
        out.append(Candidate(doi=cdoi, title=str(title or ""), journal=str(journal or ""),
                             pub_date=str(pub_date or "")[:10], publisher=publisher,
                             score=float(final or 0.0), paper_id=str(pid or "")))
    return _ordered(out)


def _ordered(cands: Iterable[Candidate]) -> List[Candidate]:
    """Best score first, then newest, then DOI - a total order, so two runs plan the same queue."""
    seen: Dict[str, Candidate] = {}
    for c in cands:
        if c.doi not in seen or c.score > seen[c.doi].score:
            seen[c.doi] = c
    return sorted(seen.values(), key=lambda c: (-c.score, _neg_date(c.pub_date), c.doi))


def _neg_date(d: str) -> str:
    """Sort key that puts the newest date first among equal scores."""
    return "".join(chr(ord("9") - int(ch)) if ch.isdigit() else ch for ch in (d or ""))


# ---------------------------------------------------------------------------------- the done set
class DoneSet:
    """What the queue must not read again: DOIs finished, and DOIs put off until a later day.

    Finished DOIs come from a local cache file AND from every facts record already committed to
    ``reads/`` (the durable half: it survives a cleared cache and arrives with a `git pull`). A record
    whose status is not final - an accelerated article preview, which has no Methods yet - does NOT
    finish its DOI; it defers it, so the edited version is read when it appears.
    """

    def __init__(self, cache_path: Path, reads_path: Optional[Path] = None) -> None:
        self.path = Path(cache_path)
        self.dois: Set[str] = set()
        self.retry: Dict[str, Dict[str, Any]] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, ValueError):
            data = {}
        for d in data.get("dois") or []:
            nd = canonical_doi(str(d))
            if nd:
                self.dois.add(nd)
        for doi, entry in (data.get("retry") or {}).items():
            nd = canonical_doi(str(doi))
            if nd and isinstance(entry, dict):
                self.retry[nd] = dict(entry)
        if reads_path is not None:
            for rec in iter_records(Path(reads_path).glob("*.jsonl")):
                nd = canonical_doi(str(rec.get("doi") or ""))
                if nd and str(rec.get("status") or STATUS_OK) in FINISHED_STATUSES:
                    self.dois.add(nd)

    def __contains__(self, doi: str) -> bool:
        """True when the DOI is finished (a deferred DOI is not: see `ready`)."""
        return (canonical_doi(doi) or "") in self.dois

    def seen(self, doi: str) -> bool:
        """True when this DOI was finished OR deferred before (i.e. it has been read at least once)."""
        nd = canonical_doi(doi) or ""
        return nd in self.dois or nd in self.retry

    def ready(self, doi: str, today: Optional[date] = None) -> bool:
        """True when the queue may read this DOI today: not finished, and past any deferral date."""
        nd = canonical_doi(doi) or ""
        if not nd or nd in self.dois:
            return False
        entry = self.retry.get(nd)
        if not entry:
            return True
        if int(entry.get("attempts") or 0) >= PREVIEW_MAX_ATTEMPTS:
            return False
        after = str(entry.get("after") or "")
        return not after or (today or datetime.now(timezone.utc).date()).isoformat() >= after

    def add(self, doi: str) -> None:
        """Finish a DOI: never queued again."""
        nd = canonical_doi(doi)
        if nd:
            self.dois.add(nd)
            self.retry.pop(nd, None)

    def defer(self, doi: str, days: int = PREVIEW_RETRY_DAYS, reason: str = STATUS_PREVIEW,
              today: Optional[date] = None) -> Dict[str, Any]:
        """Put a DOI off for `days` days (an article that is not ready to be read yet)."""
        nd = canonical_doi(doi)
        if not nd:
            return {}
        today = today or datetime.now(timezone.utc).date()
        entry = self.retry.get(nd) or {"attempts": 0}
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["after"] = (today + timedelta(days=max(1, int(days)))).isoformat()
        entry["reason"] = reason
        entry["last_seen"] = today.isoformat()
        if entry["attempts"] >= PREVIEW_MAX_ATTEMPTS:
            # it has stayed a preview for three sightings: stop re-queueing it, but keep it out of `dois`
            # so the record of why is not lost
            entry["gave_up"] = True
        self.retry[nd] = entry
        return entry

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated": _utcnow(), "dois": sorted(self.dois), "retry": dict(sorted(self.retry.items()))}
        self.path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return self.path


def iter_records(paths: Iterable[Path]) -> List[Dict[str, Any]]:
    """Every JSON object in the given .jsonl files; malformed lines are skipped with a warning."""
    out: List[Dict[str, Any]] = []
    for p in sorted(paths):
        try:
            text = Path(p).read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("could not read %s: %s", p, exc)
            continue
        for n, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                log.warning("%s line %d: not JSON; skipped", p, n)
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


# ---------------------------------------------------------------------------------- supplementary link
# Where the Methods really are when the HTML only stubs them: the supplementary-materials link. It is
# RECORDED, never fetched - a later PDF reader is what would open it, under the same cap and pacing.
SUPPLEMENT_TEXT_HINTS = ("supplementary material", "supplementary information", "supplementary data",
                         "supplementary file", "supporting information")
SUPPLEMENT_HREF_HINTS = ("suppl", "supporting-information", "supporting_information")


def extract_supplement_url(html_text: str, publisher: str = "", base_url: str = "") -> str:
    """The page's supplementary-materials link, absolutised against `base_url` ('' when the page offers none).

    nature.com keeps them in ``section[data-title="Supplementary information"]``; science.org links them
    from an anchor whose text or href says "Supplementary Materials" / "suppl". Nothing is downloaded.
    """
    from urllib.parse import urljoin

    from lxml import html as lxml_html

    if not str(html_text or "").strip():
        return ""
    try:
        root = lxml_html.fromstring(html_text)
    except Exception:  # noqa: BLE001 - lxml raises several unrelated types on junk input
        return ""
    anchors: List[Any] = []
    if publisher == "nature":
        for sec in root.cssselect('section[data-title="Supplementary information"], section[data-title="Supplementary Information"]'):
            anchors.extend(sec.cssselect("a[href]"))
    anchors.extend(a for a in root.cssselect("a[href]") if a not in anchors)
    for a in anchors:
        href = str(a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        text = re.sub(r"\s+", " ", " ".join(a.itertext())).strip().lower()
        low_href = href.lower()
        if any(h in text for h in SUPPLEMENT_TEXT_HINTS) or any(h in low_href for h in SUPPLEMENT_HREF_HINTS):
            return urljoin(base_url, href) if base_url else href
    return ""


def supplement_url_for(result: Any, publisher: str = "") -> str:
    """The supplement link for a read result: the one `read` reported, else one parsed from any page
    HTML the result carries. Absent both, '' - the facts record simply has no link."""
    url = str(getattr(result, "supplement_url", "") or "")
    if url:
        return url
    html_text = str(getattr(result, "html", "") or getattr(result, "page_html", "") or "")
    if not html_text:
        return ""
    return extract_supplement_url(html_text, publisher or str(getattr(result, "publisher", "") or ""),
                                  base_url=str(getattr(result, "url", "") or ""))


# ---------------------------------------------------------------------------------- facts records
def _clip(text: Any) -> str:
    """Any excerpt that may be written to a committed record, cut to `SNIPPET_MAX` characters."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:SNIPPET_MAX]


def facts_record(result: Any, mentions: Sequence[Dict[str, Any]], candidate: Optional[Candidate] = None,
                 read_at: Optional[str] = None, keep_snippets: bool = False,
                 status: str = STATUS_OK) -> Dict[str, Any]:
    """One committed line: identifiers, which sections were present, and the gazetteer mentions.

    NO article text: no abstract, no Methods, no body. Snippets are omitted entirely unless the caller
    asks for them, and are cut to `SNIPPET_MAX` characters when it does. Every mention carries the
    `section` it came from, so a downstream check can drop anything outside the sections it trusts.
    """
    mentions = [] if status == STATUS_PREVIEW else list(mentions)
    sections = [s for s in (getattr(result, "sections", None) or []) if s]
    if not sections:
        # `read` does not report the section list, so derive it: the sections the mentions came from,
        # plus Methods whenever the page actually carried one.
        sections = sorted({str(m.get("section") or "") for m in mentions} & set(FULLTEXT_SECTIONS))
        if int(getattr(result, "methods_chars", 0) or 0) > 0 and "methods" not in sections:
            sections.append("methods")
    ment: List[Dict[str, Any]] = []
    instruments: Dict[str, str] = {}
    companies: Dict[str, str] = {}
    for m in mentions:
        entity_id = str(m.get("entity_id") or "")
        kind = str(m.get("kind") or "")
        if not entity_id or not kind:
            continue
        item: Dict[str, Any] = {
            "kind": kind,
            "entity_id": entity_id,
            "entity_name": str(m.get("entity_name") or ""),
            "section": str(m.get("section") or ""),
            "count": int(m.get("count") or 1),
            "alias": str(m.get("alias") or ""),
        }
        if keep_snippets and m.get("snippet"):
            item["snippet"] = _clip(m.get("snippet"))
        ment.append(item)
        bucket = instruments if kind == "instrument" else companies if kind == "company" else None
        if bucket is not None:
            bucket.setdefault(entity_id, item["entity_name"] or entity_id)
    ment.sort(key=lambda d: (d["kind"], d["entity_id"], d["section"]))
    publisher = str(getattr(result, "publisher", "") or (candidate.publisher if candidate else ""))
    return {
        "schema": SCHEMA_NAME,
        "status": status,
        "methods_in_supplement": bool(getattr(result, "methods_in_supplement", False)) or status == STATUS_SUPPLEMENT,
        "supplement_url": supplement_url_for(result, publisher),
        "doi": canonical_doi(getattr(result, "doi", "") or (candidate.doi if candidate else "")) or "",
        "paper_id": str(getattr(result, "paper_id", "") or (candidate.paper_id if candidate else "")),
        "title": _clip(getattr(result, "title", "") or (candidate.title if candidate else "")),
        "journal": str(getattr(result, "journal", "") or (candidate.journal if candidate else "")),
        "pub_date": str(getattr(result, "pub_date", "") or (candidate.pub_date if candidate else ""))[:10],
        "publisher": publisher,
        "read_at": read_at or _utcnow(),
        "methods_chars": int(getattr(result, "methods_chars", 0) or 0),
        "sections": sorted(sections),
        "instruments": [{"id": k, "name": v} for k, v in sorted(instruments.items())],
        "companies": [{"id": k, "name": v} for k, v in sorted(companies.items())],
        "mentions": ment,
    }


TEXT_KEYS = ("methods", "abstract", "body", "text", "sections_text", "html", "funding", "coi", "ack")


def record_is_clean(rec: Dict[str, Any]) -> bool:
    """True when a record carries no article text: no text-bearing key, no over-long excerpt."""
    for key in TEXT_KEYS:
        if key in rec:
            return False
    for m in rec.get("mentions") or []:
        if len(str(m.get("snippet") or "")) > SNIPPET_MAX:
            return False
    return True


def append_record(path: Path, rec: Dict[str, Any]) -> Path:
    """Append one facts record to a .jsonl file (created on first use)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------- notification
def notify_macos(message: str, runner: Optional[Callable[..., Any]] = None,
                 platform: Optional[str] = None) -> bool:
    """Best-effort desktop notification on macOS. Never raises, never blocks a run.

    The text is reduced to a safe character set before it reaches `osascript`, so nothing a publisher
    or a config file supplies can close the AppleScript string.
    """
    if (platform or sys.platform) != "darwin":
        return False
    safe = _SAFE_NOTIFY.sub(" ", str(message))[:200]
    run = runner or subprocess.run
    try:
        run(["osascript", "-e", f'display notification "{safe}" with title "lit-radar"'],
            capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - platform dependent
        log.debug("desktop notification failed: %s", exc)
        return False


# ---------------------------------------------------------------------------------- the queue
@dataclass
class QueueReport:
    planned: List[Dict[str, Any]] = field(default_factory=list)
    read: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    deferred: List[Dict[str, Any]] = field(default_factory=list)
    candidates: int = 0
    missing_sessions: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    out_file: str = ""
    cap: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidates": self.candidates, "planned": self.planned, "read": self.read, "failed": self.failed,
            "deferred": self.deferred, "missing_sessions": self.missing_sessions, "notes": self.notes,
            "out_file": self.out_file, "cap": self.cap,
        }


def run_queue(
    settings: Settings,
    limit: int = DEFAULT_LIMIT,
    days: int = DEFAULT_DAYS,
    source: str = "index",
    url: Optional[str] = None,
    dry_run: bool = False,
    keep_snippets: bool = False,
    preview_retry_days: int = PREVIEW_RETRY_DAYS,
    today: Optional[date] = None,
    out_dir: Optional[Path] = None,
    opener: Optional[Callable[[str], str]] = None,
    status_fn: Optional[Callable[..., Dict[str, bool]]] = None,
    read_fn: Optional[Callable[..., Any]] = None,
    pipe_factory: Optional[Callable[[], Any]] = None,
    notifier: Optional[Callable[[str], bool]] = None,
    clock: Optional[Callable[[], float]] = None,
    log_line: Callable[[str], None] = print,
) -> QueueReport:
    """Plan the day's reads, do them, write the facts file. Returns a report; never raises on a read."""
    from . import subscription

    rep = QueueReport()
    today = today or datetime.now(timezone.utc).date()
    jmap = journal_map(settings)
    if source == "db":
        cands = candidates_from_db(Path(settings.db_path), jmap, days=days, today=today)
    else:
        resolved = index_url(settings, url)
        if not resolved:
            raise ValueError("no search index URL: pass --index-url, set reading.index_url in radar.yaml, "
                             "or run from a clone with a GitHub remote")
        cands = candidates_from_index(fetch_index(resolved, opener=opener), jmap, days=days, today=today)
        rep.notes.append(f"index: {resolved}")
    rep.candidates = len(cands)

    out_path = Path(out_dir) if out_dir else reads_dir(settings)
    done = DoneSet(Path(settings.cache_dir) / "reads" / "done.json", out_path)
    fresh = [c for c in cands if done.ready(c.doi, today)]
    waiting = sum(1 for c in cands if not done.ready(c.doi, today) and c.doi not in done)
    rep.notes.append(f"{len(cands)} candidate(s) in the last {days} day(s); {len(cands) - len(fresh) - waiting} "
                     f"already read, {waiting} waiting for a later attempt")

    cap, interval = subscription.limits(settings)
    ledger_args = {"clock": clock} if clock else {}
    ledger = subscription.ReadLedger(Path(settings.cache_dir) / "read-ledger.json", cap, interval, **ledger_args)
    remaining = max(0, cap - ledger.used_today())
    rep.cap = {"cap_per_day": cap, "used_today": ledger.used_today(), "remaining": remaining, "interval_s": interval}
    planned = fresh[: max(0, min(int(limit), remaining))]
    rep.planned = [c.as_dict() for c in planned]
    if len(fresh) > len(planned):
        rep.notes.append(f"{len(fresh) - len(planned)} candidate(s) left for a later run (limit {limit}, {remaining} read(s) left today)")
    if not planned:
        rep.notes.append("nothing to read" if fresh else "nothing new to read")
        return rep
    if dry_run:
        rep.notes.append("dry run: nothing was read")
        return rep

    # Only the publishers this queue actually needs are checked, and the whole run stops when one is missing:
    # a signed-out session returns paywalled pages, which would burn the daily cap for nothing.
    needed = sorted({c.publisher for c in planned})
    status = (status_fn or subscription.status)(settings)
    missing = [p for p in needed if not status.get(p)]
    if missing:
        rep.missing_sessions = missing
        msg = f"not signed in: {', '.join(missing)}. Run: litradar login {missing[0]}"
        rep.notes.append(msg)
        log_line(msg)
        (notifier or notify_macos)(f"Reading queue skipped - run litradar login {missing[0]}")
        return rep

    pipe = (pipe_factory or (lambda: _default_pipeline(settings)))()
    reader = read_fn or subscription.read_one
    file_path = out_path / f"{today.isoformat()}.jsonl"
    wrote_any = False
    try:
        for c in planned:
            ok, why = ledger.check()
            if not ok:
                rep.notes.append(why)
                break
            result = reader(pipe, settings, c.doi, keep_text=False, store=True)
            err = str(getattr(result, "error", "") or "")
            if err:
                if PREVIEW_MARKER in err.lower():
                    # an unedited accelerated article preview: there is no Methods yet, so this DOI is not
                    # finished. One record says it was seen; the DOI comes back after `preview_retry_days`.
                    first_sighting = not done.seen(c.doi)
                    entry = done.defer(c.doi, days=preview_retry_days, reason=STATUS_PREVIEW, today=today)
                    done.save()
                    if first_sighting:
                        append_record(file_path, facts_record(result, [], candidate=c, status=STATUS_PREVIEW))
                        wrote_any = True
                    rep.deferred.append({"doi": c.doi, "title": c.title, "status": STATUS_PREVIEW,
                                         "retry_after": entry.get("after", ""), "attempts": entry.get("attempts", 1)})
                    log_line(f"preview {c.doi}: no Methods yet; back in the queue after {entry.get('after', '')}")
                    continue
                rep.failed.append({"doi": c.doi, "title": c.title, "error": err})
                log_line(f"skipped {c.doi}: {err}")
                if "cap" in err:
                    break
                continue
            mentions = []
            try:
                mentions = pipe.store.mentions_for(getattr(result, "paper_id", "") or "")
            except Exception as exc:  # noqa: BLE001 - a store hiccup must not lose the read
                log.warning("could not read back the mentions for %s: %s", c.doi, exc)
            status = STATUS_SUPPLEMENT if getattr(result, "methods_in_supplement", False) else STATUS_OK
            rec = facts_record(result, mentions, candidate=c, keep_snippets=keep_snippets, status=status)
            append_record(file_path, rec)
            wrote_any = True
            done.add(c.doi)
            done.save()
            rep.read.append({k: rec[k] for k in ("doi", "title", "journal", "pub_date", "methods_chars", "status")})
            log_line(f"read {c.doi}: {rec['methods_chars']:,} chars of Methods"
                     f"{' (in the supplement)' if status == STATUS_SUPPLEMENT else ''}, "
                     f"{len(rec['instruments'])} instrument(s), {len(rec['companies'])} company(ies)")
        if wrote_any:
            rep.out_file = str(file_path)
    finally:
        store = getattr(pipe, "store", None)
        if store is not None and hasattr(store, "close"):
            try:
                store.close()
            except Exception:  # noqa: BLE001 - closing must not mask a read error
                pass
    return rep


def _default_pipeline(settings: Settings) -> Any:
    from .pipeline import Pipeline

    return Pipeline(settings)


# ---------------------------------------------------------------------------------- read --json -> facts
def entity_ids_by_name(settings: Settings) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """{(kind, lower-cased name or alias): (entity id, canonical name)} from the gazetteer YAML files.

    Used to turn the plain entity NAMES a `read --json` line carries back into gazetteer ids.
    """
    out: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for kind, yaml_name in (("instrument", "instruments"), ("company", "companies")):
        for entry in (settings.yaml(yaml_name) or {}).get("entries") or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            eid, name = str(entry["id"]), str(entry.get("name") or entry["id"])
            for spelling in [name] + [str(a) for a in entry.get("aliases") or []]:
                key = (kind, spelling.strip().lower())
                if spelling.strip() and key not in out:
                    out[key] = (eid, name)
    return out


def convert_read_json(obj: Dict[str, Any], names: Optional[Dict[Tuple[str, str], Tuple[str, str]]] = None,
                      read_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One `litradar read --json` line -> one facts record, or None when that read produced no facts.

    Every text field the read result carries (`methods` above all) is dropped. The entity NAMES the
    result lists become mentions in the Methods, with gazetteer ids filled in where `names` knows them.
    A line whose error says the article was an accelerated article preview converts to a `preview`
    record (no mentions), so the queue knows to come back to it; any other error converts to nothing.
    """
    if not isinstance(obj, dict) or not obj.get("doi"):
        return None
    err = str(obj.get("error") or "")
    status = STATUS_OK
    if err:
        if PREVIEW_MARKER not in err.lower():
            return None
        status = STATUS_PREVIEW
    elif int(obj.get("methods_chars") or 0) <= 0:
        return None
    elif obj.get("methods_in_supplement"):
        status = STATUS_SUPPLEMENT
    if status == STATUS_PREVIEW:
        obj = dict(obj, instruments=[], companies=[], methods_chars=0, sections=[])
    names = names or {}
    mentions: List[Dict[str, Any]] = []
    instruments: List[Dict[str, str]] = []
    companies: List[Dict[str, str]] = []
    for kind, key, bucket in (("instrument", "instruments", instruments), ("company", "companies", companies)):
        for raw in obj.get(key) or []:
            spelling = str(raw).strip()
            if not spelling:
                continue
            eid, canonical = names.get((kind, spelling.lower()), ("", spelling))
            bucket.append({"id": eid, "name": canonical})
            if eid:
                mentions.append({"kind": kind, "entity_id": eid, "entity_name": canonical,
                                 "section": "methods", "count": 1, "alias": spelling if spelling != canonical else ""})
    sections = [s for s in (obj.get("sections") or []) if s]
    if not sections and status != STATUS_PREVIEW:
        sections = ["methods"]
    return {
        "schema": SCHEMA_NAME,
        "status": status,
        "methods_in_supplement": status == STATUS_SUPPLEMENT,
        "supplement_url": str(obj.get("supplement_url") or ""),
        "doi": canonical_doi(str(obj.get("doi"))) or "",
        "paper_id": str(obj.get("paper_id") or ""),
        "title": _clip(obj.get("title")),
        "journal": str(obj.get("journal") or ""),
        "pub_date": str(obj.get("pub_date") or "")[:10],
        "publisher": str(obj.get("publisher") or ""),
        "read_at": read_at or str(obj.get("read_at") or _utcnow()),
        "methods_chars": int(obj.get("methods_chars") or 0),
        "sections": sorted(sections),
        "instruments": sorted(instruments, key=lambda d: d["name"]),
        "companies": sorted(companies, key=lambda d: d["name"]),
        "mentions": sorted(mentions, key=lambda d: (d["kind"], d["entity_id"])),
    }


# ---------------------------------------------------------------------------------- hosted import
def _paper_row(store: Any, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    doi = canonical_doi(str(rec.get("doi") or "")) or ""
    if doi:
        row = store.get_paper(f"doi:{doi}")
        if row:
            return row
        cur = store.conn.execute("SELECT rowid AS _rowid, * FROM papers WHERE doi=? LIMIT 1", (doi,))
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
        if rows:
            return rows[0]
    pid = str(rec.get("paper_id") or "")
    return store.get_paper(pid) if pid else None


def import_records(store: Any, records: Sequence[Dict[str, Any]], pipe: Optional[Any] = None) -> Dict[str, int]:
    """Upsert committed facts into an open store. Returns the `reads.*` counts.

    Per record, for the paper that DOI names:
      * ``extra_json.fulltext_source = "personal_subscription"`` plus `methods_chars`, `read_at` and the
        list of sections the article had;
      * ``sections_json`` gains a key per section present, with EMPTY text (the facts say the section
        existed; the text itself stays on the reader's machine);
      * the paper's mentions in each full-text section the record covers are replaced by the record's;
      * the paper is re-scored through the pipeline's rank path when one is supplied.
    """
    counts = {"imported": 0, "skipped": 0, "unknown_doi": 0, "invalid": 0, "rescored": 0, "mentions": 0}
    for rec in records:
        if not isinstance(rec, dict) or not rec.get("doi"):
            counts["invalid"] += 1
            continue
        if str(rec.get("status") or STATUS_OK) == STATUS_PREVIEW:
            counts["skipped"] += 1  # an unedited preview carries no facts to import; it is re-read later
            continue
        row = _paper_row(store, rec)
        if not row:
            counts["unknown_doi"] += 1
            continue
        extra = _loads_dict(row.get("extra_json"))
        read_at, chars = str(rec.get("read_at") or ""), int(rec.get("methods_chars") or 0)
        if (extra.get("fulltext_source") == "personal_subscription" and str(extra.get("read_at") or "") == read_at
                and int(extra.get("methods_chars") or 0) == chars):
            counts["skipped"] += 1
            continue
        sections_present = [str(s) for s in (rec.get("sections") or []) if s]
        sections = _loads_dict(row.get("sections_json"))
        for sec in sections_present:
            sections.setdefault(sec, "")  # present, no text
        extra.update({
            "fulltext_source": "personal_subscription",
            "methods_chars": chars,
            "read_at": read_at,
            "sections_present": sorted(sections_present),
        })
        if rec.get("methods_in_supplement"):
            # the page's Methods were a stub pointing at the supplement: say so, and keep the link for a
            # later supplement reader. Nothing is fetched here.
            extra["methods_in_supplement"] = True
            if rec.get("supplement_url"):
                extra["supplement_url"] = str(rec["supplement_url"])
        extra.setdefault("fulltext_policy", "facts_only")
        extra.setdefault("fulltext_level", "facts_only")
        store.conn.execute(
            "UPDATE papers SET extra_json=?, sections_json=?, updated_at=? WHERE id=?",
            (json.dumps(extra, ensure_ascii=False), json.dumps(sections, ensure_ascii=False), _utcnow(), row["id"]),
        )
        counts["mentions"] += _replace_section_mentions(store, row["id"], rec.get("mentions") or [])
        counts["imported"] += 1
        if pipe is not None:
            try:
                paper = pipe.paper_from_row(store.get_paper(row["id"]) or row)
                pipe.rank([paper], use_llm=False)
                counts["rescored"] += 1
            except Exception as exc:  # noqa: BLE001 - a scoring failure must not lose the facts
                log.warning("could not re-score %s: %s", row["id"], exc)
    store.commit()
    return counts


def _replace_section_mentions(store: Any, paper_id: str, mentions: Sequence[Dict[str, Any]]) -> int:
    """Replace the paper's mentions in every full-text section the record covers. Other sections
    (title, abstract, affiliations - derived from metadata the host already has) are left alone."""
    by_section: Dict[str, List[Dict[str, Any]]] = {}
    for m in mentions:
        section = str(m.get("section") or "")
        if section in FULLTEXT_SECTIONS and m.get("entity_id") and m.get("kind"):
            by_section.setdefault(section, []).append(m)
    n = 0
    for section, items in by_section.items():
        store.conn.execute("DELETE FROM mentions WHERE paper_id=? AND section=?", (paper_id, section))
        for m in items:
            snippet = _clip(m.get("snippet")) if m.get("snippet") else ""
            store.conn.execute(
                """INSERT OR REPLACE INTO mentions (paper_id, kind, entity_id, entity_name, vendor, ticker,
                   category, section, count, snippet, alias, role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, str(m["kind"]), str(m["entity_id"]), str(m.get("entity_name") or ""), "", "", "",
                 section, int(m.get("count") or 1), snippet, str(m.get("alias") or ""),
                 "used" if section == "methods" else "mentioned"),
            )
            n += 1
    if by_section:
        try:
            store._sync_fts_mentions(paper_id)  # keeps the search index's `mentions` column truthful
        except Exception as exc:  # noqa: BLE001 - an old database without the column
            log.debug("could not refresh the FTS mentions column for %s: %s", paper_id, exc)
    return n


def _loads_dict(text: Any) -> Dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
