"""Generic RSS 2.0 / RSS 1.0 (RDF) / Atom reader for journal tables of contents, trade press and press wires.

No feedparser dependency: we only need title, link, date, summary, authors and a DOI if present.
"""
from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..http import Http
from ..models import Author, NewsItem, Paper, normalize_doi

log = logging.getLogger(__name__)


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child_text(el: ET.Element, *names: str) -> str:
    """Text of the first direct child whose local name is in `names`, including text nested in sub-elements:
    Atypon feeds (JCO, the Science family) wrap the value, ``<dc:date><dc:date>2026-09-08T07:00:00Z</dc:date></dc:date>``,
    so the outer element's own `.text` is whitespace and only `itertext()` sees the date."""
    for ch in el:
        if _local(ch.tag) in names:
            text = "".join(ch.itertext()).strip()
            if text:
                return text
    return ""


def _parse_date(s: str, today: Optional[date] = None) -> str:
    """YYYY-MM-DD from RFC 2822, ISO 8601 or a bare date. A month-precision value (``2026-11``, what Elsevier puts
    in ``dc:date`` / ``prism:publicationDate`` for in-press items) becomes the first of that month, capped at
    `today`: the item is online now, the month names the issue it will appear in."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m and 1 <= int(m.group(2)) <= 12:
        first = f"{m.group(1)}-{m.group(2)}-01"
        now = (today or datetime.now(timezone.utc).date()).isoformat()
        return min(first, now)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return ""


# item date elements in preference order: the online / posting date first (RSS pubDate, dc:date, Atom published),
# then PRISM's cover and publication dates (the issue date; Elsevier and Atypon feeds carry these when dc:date is
# missing or month-precision)
_ITEM_DATE_TAGS = ("pubDate", "date", "published", "updated", "coverDate", "publicationDate", "coverDisplayDate")


def _item_date(el: ET.Element, tags: tuple = _ITEM_DATE_TAGS) -> str:
    """The first element in `tags` order that parses to a date (a present-but-unparseable one does not stop the search)."""
    for name in tags:
        d = _parse_date(_child_text(el, name))
        if d:
            return d
    return ""


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


# feed-reader tracking parameters publishers append to article links (ascopubs `af=R`, Nature `dgcid=rss_...`,
# the utm_* family, Mailchimp/Facebook/Google click ids); stripping them yields the canonical article URL
_TRACKING_PARAM_RE = re.compile(r"^(utm_[a-z0-9_]+|af|dgcid|rss|ref|fbclid|gclid|mc_cid|mc_eid|cmpid|igshid)$", re.I)


def clean_link(url: str) -> str:
    """Drop known tracking query parameters from an article link; everything else (including DOI paths and
    publisher-required parameters) is left exactly as the feed gave it."""
    u = (url or "").strip()
    if "?" not in u:
        return u
    try:
        parts = urlsplit(u)
    except ValueError:
        return u
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAM_RE.match(k)]
    query = urlencode(pairs) if pairs else ""
    return urlunsplit(parts._replace(query=query))


_BAD_FEEDS: Dict[str, Dict[str, str]] = {}  # url -> {"name", "error"}: feeds whose body did not parse this process


def bad_feeds() -> List[Dict[str, str]]:
    """Feeds that failed to parse in this process, once each, as [{url, name, error}] (for run reports)."""
    return [dict(url=url, **info) for url, info in _BAD_FEEDS.items()]


def _report_bad_feed(feed_name: str, url: str, error: str, detail: str) -> None:
    """One WARNING per feed URL per process (then DEBUG), naming the feed; remembered for ``bad_feeds()``."""
    label = feed_name or url or "unnamed feed"
    where = f" ({url})" if url and feed_name else ""
    key = url or feed_name or "?"
    if key not in _BAD_FEEDS:
        _BAD_FEEDS[key] = {"name": feed_name, "error": error}
        log.warning("feed parse error in %s%s: %s", label, where, detail)
    else:
        log.debug("feed parse error in %s%s: %s", label, where, detail)


def parse_feed(text: str, feed_name: str = "", url: str = "", outcome: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Items from an RSS 2.0 / RSS 1.0 / Atom body. A body that is not XML (an HTML error page, a Cloudflare
    challenge, a truncated download) or that is an (X)HTML document is reported once per feed URL at WARNING,
    naming the feed, then at DEBUG.

    ``outcome``, when a dict is passed, receives ``parsed`` (True when the body was a feed), ``error`` (why it was
    not; '' otherwise) and ``body`` (the text): the log line alone never reaches a run report, so a caller that
    must account for a feed answering something other than a feed reads it from here."""
    if outcome is None:
        outcome = {}
    outcome.update(parsed=False, error="", body=text if isinstance(text, str) else "")
    head = " ".join((text or "")[:80].split()) if isinstance(text, str) else ""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as exc:
        detail = f"{exc}" + (f"; body starts {head!r}" if head else "")
        _report_bad_feed(feed_name, url, str(exc), detail)
        outcome["error"] = f"not XML ({detail})"
        return []
    if _local(root.tag).lower() == "html":
        # a well-formed XHTML page (an error or sign-in page served as XML) is not a feed either
        error = "HTML document, not a feed"
        _report_bad_feed(feed_name, url, error, error + (f"; body starts {head!r}" if head else ""))
        outcome["error"] = error
        return []
    outcome["parsed"] = True
    items: List[Dict[str, Any]] = []
    # Atom
    if _local(root.tag) == "feed":
        for en in root:
            if _local(en.tag) != "entry":
                continue
            link = ""
            for ch in en:
                if _local(ch.tag) == "link" and (ch.get("rel") in (None, "alternate")):
                    link = ch.get("href") or link
            authors = []
            for ch in en:
                if _local(ch.tag) == "author":
                    nm = _child_text(ch, "name")
                    if nm:
                        authors.append(nm)
            items.append(
                {
                    "title": _strip_html(_child_text(en, "title")),
                    "link": clean_link(link or _child_text(en, "id")),
                    "date": _item_date(en, ("published", "updated", "date", "coverDate", "publicationDate")),
                    "summary": _strip_html(_child_text(en, "summary", "content", "description")),
                    "authors": authors,
                    "doi": normalize_doi(_child_text(en, "doi", "identifier") or link),
                }
            )
        return items
    # RSS 2.0 (channel/item) or RSS 1.0 (rdf:RDF/item)
    for it in root.iter():
        if _local(it.tag) != "item":
            continue
        authors = [t for t in [_child_text(it, "creator", "author")] if t]
        if authors and "," in authors[0] and len(authors) == 1:
            authors = [a.strip() for a in authors[0].split(",") if a.strip()]
        link = _child_text(it, "link")
        if not link:
            for ch in it:
                if _local(ch.tag) == "link":
                    link = ch.get("href") or ch.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource") or ""
            if not link:
                link = it.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or ""
        items.append(
            {
                "title": _strip_html(_child_text(it, "title")),
                "link": clean_link(link),
                "date": _item_date(it),
                "summary": _strip_html(_child_text(it, "description", "summary", "content", "encoded")),
                "authors": authors,
                "doi": normalize_doi(_child_text(it, "doi", "identifier") or link),
            }
        )
    return items


def fetch_items(http: Http, url: str, feed_name: str = "", outcome: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Fetch and parse one feed. ``outcome`` (optional dict) gets ``fetched`` (False when the HTTP layer returned
    nothing; that failure is already counted there) plus what ``parse_feed`` records."""
    text = http.get_text(url)
    if outcome is not None:
        outcome["fetched"] = bool(text)
    if not text:
        return []
    return parse_feed(text, feed_name=feed_name, url=url, outcome=outcome)


def journal_papers(http: Http, feed_name: str, url: str, journal: str, since: Optional[str] = None,
                   outcome: Optional[Dict[str, Any]] = None) -> Iterator[Paper]:
    for it in fetch_items(http, url, feed_name=feed_name, outcome=outcome):
        if since and it["date"] and it["date"] < since:
            continue
        doi = it.get("doi")
        native = doi or hashlib.sha1((it.get("link") or it.get("title") or "").encode()).hexdigest()[:16]
        yield Paper(
            id=Paper.make_id(f"rss:{feed_name}", native, doi),
            source=f"rss:{feed_name}",
            kind="article",
            title=it["title"],
            doi=doi,
            abstract=it.get("summary") or "",
            journal=journal,
            pub_date=it["date"],
            url=it.get("link") or (f"https://doi.org/{doi}" if doi else ""),
            authors=[Author(name=a) for a in it.get("authors") or []],
        )


def news_items(http: Http, feed_name: str, url: str, since: Optional[str] = None,
               outcome: Optional[Dict[str, Any]] = None) -> Iterator[NewsItem]:
    for it in fetch_items(http, url, feed_name=feed_name, outcome=outcome):
        if since and it["date"] and it["date"] < since:
            continue
        key = it.get("link") or it.get("title") or ""
        yield NewsItem(
            id=hashlib.sha1(key.encode()).hexdigest()[:20],
            feed=feed_name,
            title=it["title"],
            url=it.get("link") or "",
            published=it["date"],
            summary=(it.get("summary") or "")[:1500],
        )
