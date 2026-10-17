"""Crossref REST API journal feeds (https://api.crossref.org).

GET /journals/{issn}/works?filter=from-pub-date:YYYY-MM-DD,until-pub-date:YYYY-MM-DD&rows=200&cursor=*
Covers every DOI-registered article the day it is registered, usually before PubMed. Abstracts are
present only when the publisher deposits them (Nature portfolio and Cell Press mostly do; Science
mostly does not).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List

from ..http import Http
from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)
BASE = "https://api.crossref.org"
SELECT = "DOI,title,abstract,author,container-title,published,URL,type,funder,ISSN,subject,license"
ARTICLE_TYPES = {"journal-article", "posted-content", "proceedings-article"}


def journal_works(http: Http, issn: str, start: str, end: str, mailto: str = "", max_records: int = 5000) -> Iterator[Dict[str, Any]]:
    cursor = "*"
    n = 0
    while n < max_records:
        params: Dict[str, Any] = {
            "filter": f"from-pub-date:{start},until-pub-date:{end}",
            "rows": 200,
            "cursor": cursor,
            "select": SELECT,
        }
        if mailto:
            params["mailto"] = mailto
        data = http.get_json(f"{BASE}/journals/{issn}/works", params=params)
        if not data or data.get("status") != "ok":
            break
        msg = data.get("message") or {}
        items = msg.get("items") or []
        yield from items
        n += len(items)
        cursor = msg.get("next-cursor")
        if not items or not cursor:
            break


def _date(parts: Dict[str, Any]) -> str:
    dp = (parts or {}).get("date-parts") or [[]]
    d = dp[0] if dp else []
    if not d:
        return ""
    y = d[0]
    m = d[1] if len(d) > 1 else 1
    day = d[2] if len(d) > 2 else 1
    return f"{y:04d}-{m:02d}-{day:02d}"


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
        import re

        abstract = re.sub(r"<[^>]+>", " ", abstract)
        abstract = re.sub(r"\s+", " ", abstract).strip()
    title = " ".join(it.get("title") or []).strip()
    container = " ".join(it.get("container-title") or []) or journal_name
    funders = [f.get("name") for f in (it.get("funder") or []) if f.get("name")]
    p = Paper(
        id=Paper.make_id("crossref", doi or title, doi),
        source="crossref",
        kind="preprint" if it.get("type") == "posted-content" else "article",
        title=title,
        doi=doi,
        abstract=abstract,
        journal=container,
        pub_date=_date(it.get("published") or it.get("published-online") or it.get("created") or {}),
        url=it.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
        authors=authors,
        affiliations=list(dict.fromkeys(a for a in affs if a)),
        extra={"crossref_type": it.get("type"), "funders": funders, "subject": it.get("subject")},
    )
    if funders:
        p.sections["funding"] = "; ".join(funders)
    return p
