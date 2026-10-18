"""Generic RSS 2.0 / RSS 1.0 (RDF) / Atom reader for journal tables of contents, trade press and press wires.

No feedparser dependency: we only need title, link, date, summary, authors and a DOI if present.
"""
from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..http import Http
from ..models import Author, NewsItem, Paper, normalize_doi

log = logging.getLogger(__name__)


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child_text(el: ET.Element, *names: str) -> str:
    for ch in el:
        if _local(ch.tag) in names and (ch.text or "").strip():
            return (ch.text or "").strip()
    return ""


def _parse_date(s: str) -> str:
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
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
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


def parse_feed(text: str, feed_name: str = "", url: str = "") -> List[Dict[str, Any]]:
    """Items from an RSS 2.0 / RSS 1.0 / Atom body. A body that is not XML (an HTML error page, a Cloudflare
    challenge, a truncated download) is reported once per feed URL at WARNING, naming the feed, then at DEBUG."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as exc:
        label = feed_name or url or "unnamed feed"
        where = f" ({url})" if url and feed_name else ""
        head = " ".join((text or "")[:80].split()) if isinstance(text, str) else ""
        detail = f"{exc}" + (f"; body starts {head!r}" if head else "")
        key = url or feed_name or "?"
        if key not in _BAD_FEEDS:
            _BAD_FEEDS[key] = {"name": feed_name, "error": str(exc)}
            log.warning("feed parse error in %s%s: %s", label, where, detail)
        else:
            log.debug("feed parse error in %s%s: %s", label, where, detail)
        return []
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
                    "date": _parse_date(_child_text(en, "published", "updated", "date")),
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
                "date": _parse_date(_child_text(it, "pubDate", "date", "published", "updated")),
                "summary": _strip_html(_child_text(it, "description", "summary", "content", "encoded")),
                "authors": authors,
                "doi": normalize_doi(_child_text(it, "doi", "identifier") or link),
            }
        )
    return items


def fetch_items(http: Http, url: str, feed_name: str = "") -> List[Dict[str, Any]]:
    text = http.get_text(url)
    if not text:
        return []
    return parse_feed(text, feed_name=feed_name, url=url)


def journal_papers(http: Http, feed_name: str, url: str, journal: str, since: Optional[str] = None) -> Iterator[Paper]:
    for it in fetch_items(http, url, feed_name=feed_name):
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


def news_items(http: Http, feed_name: str, url: str, since: Optional[str] = None) -> Iterator[NewsItem]:
    for it in fetch_items(http, url, feed_name=feed_name):
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
