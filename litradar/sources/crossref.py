"""Crossref REST API journal feeds (https://api.crossref.org).

GET /journals/{issn}/works?filter=from-created-date:YYYY-MM-DD,until-created-date:YYYY-MM-DD&rows=200&cursor=*

Why the *created* date: ``from-pub-date`` filters on the publisher's deposited publication date, and
several large titles deposit it at month precision (``published: {date-parts: [[2026, 10]]}``), so a
one-week ``from-pub-date`` window returned 0 items for them while ``from-created-date`` (the day the DOI
was registered) returned every new article. ``created`` is what "covers every DOI-registered article
the day it is registered" actually means. If created-date returns nothing for an ISSN the adapter
retries once with ``from-online-pub-date`` before giving up on the window.

Stored date: ``Paper.pub_date`` keeps the publication date when the publisher deposits a full
YYYY-MM-DD; when only a month is deposited, the DOI registration day (``created``) is used so the
paper lands inside the window it was registered in instead of on the first of the month. The raw
deposited parts are kept in ``extra['published_parts']``.

Abstracts are present only when the publisher deposits them (Nature portfolio and Cell Press mostly
do; Science mostly does not).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterator, List, Optional

from ..http import Http
from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)
BASE = "https://api.crossref.org"
SELECT = "DOI,title,abstract,author,container-title,published,published-online,created,URL,type,funder,ISSN,subject,license"
ARTICLE_TYPES = {"journal-article", "posted-content", "proceedings-article"}
DATE_FILTERS = ("created", "online-pub")  # tried in this order; the second only when the first yields nothing


def _page(http: Http, issn: str, flt: str, cursor: str, mailto: str) -> Optional[Dict[str, Any]]:
    params: Dict[str, Any] = {"filter": flt, "rows": 200, "cursor": cursor, "select": SELECT}
    if mailto:
        params["mailto"] = mailto
    data = http.get_json(f"{BASE}/journals/{issn}/works", params=params)
    if not data or data.get("status") != "ok":
        return None
    return data.get("message") or {}


def journal_works(http: Http, issn: str, start: str, end: str, mailto: str = "", max_records: int = 5000) -> Iterator[Dict[str, Any]]:
    """Yield Crossref items for one ISSN registered (created) in [start, end]; falls back to online-pub-date."""
    for kind in DATE_FILTERS:
        flt = f"from-{kind}-date:{start},until-{kind}-date:{end}"
        cursor = "*"
        n = 0
        while n < max_records:
            msg = _page(http, issn, flt, cursor, mailto)
            if msg is None:
                break
            items = msg.get("items") or []
            yield from items
            n += len(items)
            cursor = msg.get("next-cursor")
            if not items or not cursor:
                break
        if n:
            return
        log.info("crossref %s: 0 items with %s-date filter in %s..%s", issn, kind, start, end)


def _date_parts(parts: Dict[str, Any]) -> List[int]:
    dp = (parts or {}).get("date-parts") or [[]]
    d = dp[0] if dp else []
    out: List[int] = []
    for x in d or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            break
    return out


def _date(parts: Dict[str, Any]) -> str:
    """'date-parts' -> YYYY-MM-DD, padding missing month/day with 01; '' when the year is missing."""
    d = _date_parts(parts)
    if not d:
        return ""
    y = d[0]
    m = d[1] if len(d) > 1 else 1
    day = d[2] if len(d) > 2 else 1
    return f"{y:04d}-{m:02d}-{day:02d}"


def _created_day(it: Dict[str, Any]) -> str:
    created = it.get("created") or {}
    dt = created.get("date-time") or ""
    if len(dt) >= 10:
        return dt[:10]
    return _date(created)


def pub_date(it: Dict[str, Any]) -> str:
    """Full-precision published / published-online date, else the DOI registration day, else a padded date."""
    for key in ("published", "published-online"):
        if len(_date_parts(it.get(key) or {})) >= 3:
            return _date(it.get(key))
    created = _created_day(it)
    if created:
        return created
    return _date(it.get("published") or it.get("published-online") or {})


def to_paper(it: Dict[str, Any], journal_name: str = "") -> Paper:
    doi = normalize_doi(it.get("DOI"))
    authors: List[Author] = []
    affs: List[str] = []
    for a in it.get("author") or []:
        name = " ".join(x for x in [a.get("given"), a.get("family")] if x) or a.get("name") or ""
        aff = [x.get("name") for x in (a.get("affiliation") or []) if x.get("name")]
        affs.extend(aff)
        if name:
            authors.append(Author(name=name, affiliation="; ".join(aff), orcid=(a.get("ORCID") or "").rsplit("/", 1)[-1]))
    abstract = it.get("abstract") or ""
    # Crossref abstracts are JATS-flavoured XML
    if "<" in abstract:
        abstract = re.sub(r"<[^>]+>", " ", abstract)
        abstract = re.sub(r"\s+", " ", abstract).strip()
    title = " ".join(it.get("title") or []).strip()
    container = " ".join(it.get("container-title") or []) or journal_name
    funders = [f.get("name") for f in (it.get("funder") or []) if f.get("name")]
    issns = [x for x in (it.get("ISSN") or []) if x]
    p = Paper(
        id=Paper.make_id("crossref", doi or title, doi),
        source="crossref",
        kind="preprint" if it.get("type") == "posted-content" else "article",
        title=title,
        doi=doi,
        abstract=abstract,
        journal=container,
        pub_date=pub_date(it),
        url=it.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
        authors=authors,
        affiliations=list(dict.fromkeys(a for a in affs if a)),
        extra={
            "crossref_type": it.get("type"),
            "funders": funders,
            "subject": it.get("subject"),
            "issn": issns,
            "published_parts": _date_parts(it.get("published") or {}),
            "created": _created_day(it),
        },
    )
    if funders:
        p.sections["funding"] = "; ".join(funders)
    return p
