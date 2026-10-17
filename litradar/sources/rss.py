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


def parse_feed(text: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as exc:
        log.warning("feed parse error: %s", exc)
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
                    "link": link or _child_text(en, "id"),
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
                "link": link,
                "date": _parse_date(_child_text(it, "pubDate", "date", "published", "updated")),
                "summary": _strip_html(_child_text(it, "description", "summary", "content", "encoded")),
                "authors": authors,
                "doi": normalize_doi(_child_text(it, "doi", "identifier") or link),
            }
        )
    return items


def fetch_items(http: Http, url: str) -> List[Dict[str, Any]]:
    text = http.get_text(url)
    if not text:
        return []
    return parse_feed(text)


def journal_papers(http: Http, feed_name: str, url: str, journal: str, since: Optional[str] = None) -> Iterator[Paper]:
    for it in fetch_items(http, url):
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
    for it in fetch_items(http, url):
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
