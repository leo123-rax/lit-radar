"""Personal subscription reading assist: sign in once in a real browser, then read individual articles.

Design, in one breath: the user signs in to a publisher THEMSELVES in a visible Chromium window that uses a
persistent profile under the cache directory; the code never receives, stores or submits a password. Later,
`read` opens one article at a time through that profile (headless), extracts title, abstract and the Methods,
funding, competing-interest and acknowledgement sections from the publisher HTML, runs the normal gazetteer
extraction and scoring, and shows the user a reading card. Pacing and a daily cap keep this at the scale of
a person reading, which is what a personal subscription is for; bulk harvesting is refused by construction.
Storage follows litradar.policy: by default only derived facts (mentions, snippets) are kept for paywalled
text ('facts_only'); `--keep-text` stores sections locally as the reader's personal copy, labelled so the
digest, coverage report and export can tell it apart. Nothing here is used by the weekly harvester or on CI.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from lxml import html as lxml_html

from .config import Settings
from .extract.jats import ACK_RE, COI_RE, DATA_RE, FUNDING_RE, METHODS_RE
from .models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)

DEFAULT_CAP_PER_DAY = 20
DEFAULT_INTERVAL_S = 20.0
DEFAULT_WAIT_S = 300

PUBLISHERS: Dict[str, Dict[str, Any]] = {
    "nature": {
        "hosts": ["www.nature.com", "nature.com"],
        "login_url": "https://idp.nature.com/auth/personal/springernature?redirect_uri=https%3A%2F%2Fwww.nature.com%2F",
        # a paywalled article in the flagship journal (a personal Nature subscription covers Nature itself, not the
        # Nature Research journals such as Nature Methods): the session is live when its Methods come back
        "probe_url": "https://www.nature.com/articles/s41586-026-11143-w",
        "logged_in": lambda final_url, html_text: _has_full_text(html_text, "nature"),
        "doi_prefixes": ["10.1038/"],
        "article_url": lambda doi: f"https://www.nature.com/articles/{doi.split('/', 1)[1]}",
        "paywall_markers": ["Access options", "Buy article", "Access through your institution", "c-article-access-provider"],
    },
    "science": {
        "hosts": ["www.science.org", "science.org"],
        "login_url": "https://www.science.org/action/showLogin?redirectUri=%2F",
        "probe_url": "https://www.science.org/doi/10.1126/science.aeh2982",
        "logged_in": lambda final_url, html_text: _has_full_text(html_text, "science"),
        "doi_prefixes": ["10.1126/"],
        "article_url": lambda doi: f"https://www.science.org/doi/{doi}",
        "paywall_markers": ["Log in to view the full text", "Purchase this article", "Get access to this article", "Already a subscriber"],
    },
}


# ---------------------------------------------------------------------------------------------- helpers
def _has_full_text(html_text: str, publisher: str) -> bool:
    """True when a publisher page carries a real Methods section and no paywall markers (the only honest session test)."""
    art = extract_article(html_text, publisher)
    return bool(art["sections"].get("methods")) and not art["paywalled"]


def publisher_for(target: str) -> Optional[str]:
    """Publisher key for a DOI or URL, or None when it is not a configured subscription host."""
    doi = normalize_doi(target)
    if doi:
        for key, pub in PUBLISHERS.items():
            if any(doi.startswith(p) for p in pub["doi_prefixes"]):
                return key
    host = urlsplit(target if "://" in target else "https://" + target).netloc.lower()
    for key, pub in PUBLISHERS.items():
        if host in pub["hosts"]:
            return key
    return None


def article_url(target: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(publisher, url, doi) for a DOI or publisher URL."""
    pub = publisher_for(target)
    if not pub:
        return None, None, normalize_doi(target)
    doi = normalize_doi(target)
    if doi and not target.startswith("http"):
        return pub, PUBLISHERS[pub]["article_url"](doi), doi
    if target.startswith("http"):
        return pub, target, doi
    return pub, PUBLISHERS[pub]["article_url"](doi) if doi else None, doi


def profile_dir(settings: Settings) -> Path:
    cfg = (settings.radar or {}).get("subscription") or {}
    p = Path(cfg.get("profile_dir") or (settings.cache_dir / "browser-profile"))
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def limits(settings: Settings) -> Tuple[int, float]:
    cfg = (settings.radar or {}).get("subscription") or {}
    cap = int(os.environ.get("LITRADAR_READ_CAP") or cfg.get("cap_per_day") or DEFAULT_CAP_PER_DAY)
    interval = float(os.environ.get("LITRADAR_READ_INTERVAL") or cfg.get("interval") or DEFAULT_INTERVAL_S)
    return cap, interval


class ReadLedger:
    """Daily cap + pacing, persisted as a tiny JSON file in the cache directory."""

    def __init__(self, path: Path, cap: int, interval: float, clock: Callable[[], float] = time.time) -> None:
        self.path, self.cap, self.interval, self.clock = path, cap, interval, clock
        try:
            self.state = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            self.state = {}

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")

    def used_today(self) -> int:
        return int(self.state.get(self._today(), 0))

    def check(self) -> Tuple[bool, str]:
        if self.used_today() >= self.cap:
            return False, f"daily reading cap reached ({self.cap}); resets at 00:00 UTC. Raise LITRADAR_READ_CAP if you must."
        return True, ""

    def wait_turn(self, sleep: Callable[[float], None] = time.sleep) -> None:
        last = float(self.state.get("last_ts", 0))
        gap = self.interval - (self.clock() - last)
        if gap > 0:
            sleep(gap)

    def record(self) -> None:
        self.state[self._today()] = self.used_today() + 1
        self.state["last_ts"] = self.clock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state))


# ---------------------------------------------------------------------------------------------- HTML extraction
def _text(el: Any) -> str:
    return re.sub(r"\s+", " ", " ".join(el.itertext())).strip() if el is not None else ""


def _bucket(title: str) -> Optional[str]:
    t = title or ""
    if METHODS_RE.search(t) and not FUNDING_RE.search(t):
        return "methods"
    if FUNDING_RE.search(t):
        return "funding"
    if COI_RE.search(t):
        return "coi"
    if DATA_RE.search(t):
        return "data_availability"
    if ACK_RE.search(t):
        return "ack"
    return None


def extract_article(html_text: str, publisher: str) -> Dict[str, Any]:
    """Title, abstract and the sections we care about from a publisher article page."""
    out: Dict[str, Any] = {"title": "", "abstract": "", "sections": {}, "paywalled": False, "authors": []}
    try:
        root = lxml_html.fromstring(html_text)
    except (ValueError, TypeError):
        return out
    markers = PUBLISHERS.get(publisher, {}).get("paywall_markers", [])
    body_text = _text(root)
    buckets: Dict[str, List[str]] = {}

    def add(title: str, text: str) -> None:
        key = _bucket(title)
        if key and text:
            buckets.setdefault(key, []).append(text)

    if publisher == "nature":
        h1 = root.cssselect("h1.c-article-title") or root.cssselect("h1")
        out["title"] = _text(h1[0]) if h1 else ""
        for sec in root.cssselect("section[data-title]"):
            title = sec.get("data-title") or ""
            content = sec.cssselect("div.c-article-section__content")
            text = _text(content[0]) if content else _text(sec)
            if title.strip().lower() == "abstract":
                out["abstract"] = text
            else:
                add(title, text)
        for a in root.cssselect("a[data-test='author-name'], li.c-article-author-list__item a"):
            name = _text(a)
            if name and name not in out["authors"]:
                out["authors"].append(name)
    else:
        h1 = root.cssselect("h1.article-title, h1[property='name'], h1")
        out["title"] = _text(h1[0]) if h1 else ""
        abstract = root.cssselect("section#abstract, div.abstractSection, section[property='abstract'], div#abstracts")
        if abstract:
            out["abstract"] = re.sub(r"^Abstract\s*", "", _text(abstract[0]))
        # Science-like and generic pages: every <section> with a heading, else h2 blocks
        sections = root.cssselect("section")
        seen_any = False
        for sec in sections:
            heads = sec.cssselect("h2, h3")
            if not heads:
                continue
            title = _text(heads[0])
            text = _text(sec)
            if text and title:
                seen_any = True
                add(title, text[len(title):].strip() if text.startswith(title) else text)
        if not seen_any:
            for h2 in root.cssselect("h2"):
                chunks: List[str] = []
                sib = h2.getnext()
                while sib is not None and sib.tag not in ("h2",):
                    chunks.append(_text(sib))
                    sib = sib.getnext()
                add(_text(h2), " ".join(c for c in chunks if c))
    for key, chunks in buckets.items():
        chunks = sorted(set(chunks), key=len, reverse=True)
        kept: List[str] = []
        for c in chunks:
            if not any(c in k for k in kept):
                kept.append(c)
        out["sections"][key] = "\n\n".join(kept)
    out["paywalled"] = (not out["sections"].get("methods")) and any(m in body_text or m in html_text for m in markers)
    if not out["title"]:
        m = root.cssselect("meta[name='dc.title'], meta[property='og:title']")
        if m:
            out["title"] = m[0].get("content") or ""
    return out


# ---------------------------------------------------------------------------------------------- browser session
@dataclass
class Session:
    settings: Settings
    headless: bool = True
    _pw: Any = None
    _context: Any = None
    factory: Optional[Callable[[], Any]] = None  # tests inject a fake playwright

    def __enter__(self) -> Session:
        if self.factory:
            self._pw = self.factory()
        else:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("Playwright is not installed: pip install 'lit-radar[browser]' && python -m playwright install chromium") from exc
            self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(str(profile_dir(self.settings)), headless=self.headless)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._pw is not None and hasattr(self._pw, "stop"):
                self._pw.stop()

    def fetch(self, url: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> Tuple[str, str]:
        page = self._context.new_page()
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 - best effort
                pass
            return page.url, page.content()
        finally:
            page.close()

    def open_for_user(self, url: str) -> Any:
        page = self._context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return page


def is_logged_in(session: Session, publisher: str) -> bool:
    pub = PUBLISHERS[publisher]
    try:
        final_url, html_text = session.fetch(pub["probe_url"])
    except Exception as exc:  # noqa: BLE001
        log.warning("%s probe failed: %s", publisher, exc)
        return False
    return bool(pub["logged_in"](final_url, html_text))


def login(settings: Settings, publisher: str, wait_s: int = DEFAULT_WAIT_S, poll_s: float = 4.0, factory: Optional[Callable[[], Any]] = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Open the publisher's sign-in page in a VISIBLE window and wait until the user has signed in.

    The code never fills the form: the user types their own credentials in their own browser window.
    Returns {'publisher', 'logged_in', 'seconds'}."""
    if publisher not in PUBLISHERS:
        raise ValueError(f"unknown publisher {publisher!r}; choose from {sorted(PUBLISHERS)}")
    start = clock()
    with Session(settings, headless=False, factory=factory) as s:
        if is_logged_in(s, publisher):
            return {"publisher": publisher, "logged_in": True, "seconds": 0.0, "note": "already signed in"}
        page = s.open_for_user(PUBLISHERS[publisher]["login_url"])
        print(f"A browser window is open on the {publisher} sign-in page. Sign in there; this command waits up to {wait_s // 60} minutes.", flush=True)
        ok = False
        while clock() - start < wait_s:
            sleep(poll_s)
            if is_logged_in(s, publisher):
                ok = True
                break
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        return {"publisher": publisher, "logged_in": ok, "seconds": round(clock() - start, 1)}


def status(settings: Settings, factory: Optional[Callable[[], Any]] = None) -> Dict[str, bool]:
    with Session(settings, headless=True, factory=factory) as s:
        return {key: is_logged_in(s, key) for key in PUBLISHERS}


# ---------------------------------------------------------------------------------------------- read
@dataclass
class ReadResult:
    target: str
    publisher: str = ""
    url: str = ""
    doi: str = ""
    title: str = ""
    journal: str = ""
    pub_date: str = ""
    paper_id: str = ""
    paywalled: bool = False
    methods_chars: int = 0
    instruments: List[str] = field(default_factory=list)
    companies: List[str] = field(default_factory=list)
    score: Optional[float] = None
    stored_as: str = ""
    error: str = ""
    methods: str = ""


def read_one(pipe: Any, settings: Settings, target: str, keep_text: bool = False, store: bool = True, factory: Optional[Callable[[], Any]] = None, html_override: Optional[str] = None) -> ReadResult:
    """Read one subscription article through the signed-in profile and store it per policy."""
    from . import ingest

    r = ReadResult(target=target)
    pub, url, doi = article_url(target)
    if not pub or not url:
        r.error = "not a configured subscription host (nature.com, science.org); use `litradar add` for everything else"
        return r
    r.publisher, r.url, r.doi = pub, url, doi or ""
    cap, interval = limits(settings)
    ledger = ReadLedger(settings.cache_dir / "read-ledger.json", cap, interval)
    ok, why = ledger.check()
    if not ok:
        r.error = why
        return r
    if html_override is None:
        ledger.wait_turn()
        with Session(settings, headless=True, factory=factory) as s:
            final_url, html_text = s.fetch(url)
        ledger.record()
    else:
        html_text = html_override
    art = extract_article(html_text, pub)
    r.title, r.paywalled = art["title"], art["paywalled"]
    r.methods = art["sections"].get("methods", "")
    r.methods_chars = len(r.methods)
    if r.paywalled or not r.methods:
        r.error = f"no full text returned: {'paywall markers present' if r.paywalled else 'no Methods section found'}. Run `litradar login {pub}` first, then retry."
        return r
    # metadata from the public indexes, then the sections from the page
    paper: Optional[Paper] = None
    if doi:
        res = ingest.Resolver(pipe.http, settings.contact_email).resolve(ingest.parse_identifier(doi)) if hasattr(ingest.Resolver, "resolve") else None
        paper = getattr(res, "paper", None) if res else None
    if paper is None:
        paper = Paper(id=Paper.make_id("subscription", url, doi), source=f"subscription:{pub}", kind="article", title=r.title, doi=doi, journal=pub.title(), url=url)
    if not paper.title:
        paper.title = r.title
    if not paper.abstract and art.get("abstract"):
        paper.abstract = art["abstract"]
    if not paper.authors and art.get("authors"):
        paper.authors = [Author(name=n) for n in art["authors"]]
    paper.sections.update(art["sections"])
    paper.fulltext_available = True
    r.journal, r.pub_date = paper.journal, paper.pub_date
    mentions, hits = pipe.extract(paper)
    r.instruments = sorted({m.entity_name for m in mentions if m.kind == "instrument"})
    r.companies = sorted({m.entity_name for m in mentions if m.kind == "company"})
    if store:
        if keep_text:
            # the store keeps sections only at policy 'full'; the source label tells digest/coverage/export apart
            paper.extra["fulltext_policy"] = "full"
            paper.extra["fulltext_source"] = "personal_subscription"
            paper.extra["fulltext_level"] = "full"
        else:
            paper.sections = {}
            paper.fulltext_available = False
            paper.extra["fulltext_policy"] = "facts_only"
            paper.extra["fulltext_level"] = "facts_only"
        paper.extra["read_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pipe.store.upsert_paper(paper)
        pipe.store.replace_mentions(paper.id, mentions)
        for name, grp, how in hits:
            pipe.store.add_tracked_author_hit(paper.id, name, grp, how)
        pipe.store.commit()
        pipe.rank({paper.id: paper}, use_llm=False)
        sc = pipe.store.get_score(paper.id) or {}
        r.score = sc.get("final")
        r.stored_as = paper.extra["fulltext_level"]
    r.paper_id = paper.id
    return r


def format_card(r: ReadResult, full: bool = False) -> str:
    if r.error:
        return f"{r.target}: {r.error}"
    lines = [f"{r.title}", f"  {r.journal or r.publisher} {r.pub_date}  {r.url}", f"  instruments: {', '.join(r.instruments) or 'none'}", f"  companies: {', '.join(r.companies) or 'none'}"]
    if r.score is not None:
        lines.append(f"  score {r.score:.1f} · stored as {r.stored_as}")
    text = r.methods if full else (r.methods[:4000] + ("…" if len(r.methods) > 4000 else ""))
    lines += ["", "Methods", text]
    return "\n".join(lines)


def quoted_doi_url(doi: str) -> str:
    return "https://doi.org/" + quote(doi, safe="/")
