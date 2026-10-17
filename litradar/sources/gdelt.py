"""GDELT DOC 2.0 API: per-company news without scraping a search engine.

Endpoint
    GET https://api.gdeltproject.org/api/v2/doc/doc?query=<q>&mode=ArtList&format=json&timespan=7d
        &maxrecords=250&sort=DateDesc

    * ``query`` is a free-text expression: quoted phrases, ``OR`` groups **must** be wrapped in
      parentheses (the API answers ``Queries containing OR'd terms must be surrounded by ().`` as a
      200 text/html body otherwise), plus operators such as ``sourcelang:english``,
      ``domain:example.com``, ``domainis:``, ``sourcecountry:``, ``theme:``.
    * ``timespan`` accepts minutes/hours/days/weeks/months (``15min`` minimum, ``3m`` maximum window).
    * ``maxrecords`` defaults to 75 and caps at 250.
    * A result is ``{"articles": [{url, url_mobile, title, seendate (YYYYMMDDTHHMMSSZ), socialimage,
      domain, language, sourcecountry}, ...]}``; an empty result is the literal body ``{}``.

Terms
    GDELT states its data are "available for unlimited and unrestricted use for any academic,
    commercial, or governmental use of any kind without fee" and asks for a citation when
    redistributed; the DOC API is built for programmatic access. The rate limit is one request every
    five seconds per client (HTTP 429 with a plain-text body otherwise); the shared Http client paces
    ``api.gdeltproject.org`` at 5.5 s and backs off 5 s on a 429. Bursting the endpoint puts the client
    IP in a penalty state that outlasts the burst by many minutes, so a run with ~75 company queries
    takes about seven minutes and must never be parallelised.

Coverage caveat
    GDELT crawls mainstream and regional news in 65 languages; paywalled trade titles are mostly
    absent, so this layer complements (does not replace) publisher RSS, press-wire RSS and SEC EDGAR.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from ..http import Http
from ..models import NewsItem

log = logging.getLogger(__name__)
DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250
_OR_GROUP_RE = re.compile(r"\bOR\b")


def build_query(company: str, context: str = "", language: str = "english", extra: str = "") -> str:
    """Compose a DOC API query: the company name as a phrase, the sector context clause, a language filter.

    The context clause from ``news.query_context`` in radar.yaml is already parenthesised; a bare
    ``a OR b`` clause is wrapped so the API accepts it.
    """
    parts: List[str] = [f'"{company.strip()}"'] if company and company.strip() else []
    ctx = (context or "").strip()
    if ctx:
        if _OR_GROUP_RE.search(ctx) and not (ctx.startswith("(") and ctx.endswith(")")):
            ctx = f"({ctx})"
        parts.append(ctx)
    if extra and extra.strip():
        parts.append(extra.strip())
    if language:
        parts.append(f"sourcelang:{language}")
    return " ".join(parts)


def _seen_date(s: str) -> str:
    """'20261015T143000Z' -> '2026-10-15' (UTC); anything unparseable -> ''."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        pass
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def search(http: Http, query: str, timespan: str = "7d", max_records: int = MAX_RECORDS) -> Optional[List[Dict[str, Any]]]:
    """One DOC API call. Returns the article list ([] when GDELT has none) or None on transport/API failure."""
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "timespan": timespan,
        "maxrecords": max(1, min(int(max_records), MAX_RECORDS)),
        "sort": "DateDesc",
    }
    resp = http.get(DOC_API, params=params, headers={"Accept": "application/json"})
    if resp is None or resp.status_code != 200:
        return None
    body = (resp.text or "").strip()
    if not body or body == "{}":
        return []
    try:
        data = resp.json()
    except ValueError:
        # the API reports query-syntax problems as a 200 text/html one-liner
        log.warning("GDELT rejected query %r: %s", query, body[:160])
        return None
    if not isinstance(data, dict):
        return None
    arts = data.get("articles") or []
    return [a for a in arts if isinstance(a, dict)]


def to_news_item(article: Dict[str, Any], feed: str, company_id: str = "") -> Optional[NewsItem]:
    url = (article.get("url") or "").strip()
    title = re.sub(r"\s+", " ", (article.get("title") or "")).strip()
    if not url or not title:
        return None
    return NewsItem(
        id=hashlib.sha1(url.encode()).hexdigest()[:20],
        feed=feed,
        title=title,
        url=url,
        published=_seen_date(article.get("seendate") or ""),
        summary="",  # the DOC API returns no snippet; the digest links out
        companies=[company_id] if company_id else [],
    )


def company_news(
    http: Http,
    company_name: str,
    company_id: str = "",
    context: str = "",
    timespan: str = "7d",
    language: str = "english",
    since: Optional[str] = None,
    max_records: int = MAX_RECORDS,
) -> Iterator[NewsItem]:
    """Yield NewsItem(feed='gdelt:<company>') for one company. Empty when GDELT has nothing or is rate-limiting."""
    query = build_query(company_name, context, language=language)
    feed = f"gdelt:{company_name}"
    arts = search(http, query, timespan=timespan, max_records=max_records)
    if arts is None:
        log.info("GDELT: no answer for %r", company_name)
        return
    n = 0
    for a in arts:
        item = to_news_item(a, feed, company_id)
        if item is None:
            continue
        if since and item.published and item.published < since:
            continue
        n += 1
        yield item
    log.debug("GDELT: %d article(s) for %r", n, company_name)
