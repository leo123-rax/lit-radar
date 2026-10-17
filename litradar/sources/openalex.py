"""OpenAlex works API (https://docs.openalex.org). Free, CC0, ~100k calls/day with a mailto.

Used for: (1) new works in tracked topics, (2) works with company-affiliated authors
(`authorships.institutions.type:company`), (3) new works by tracked authors, (4) journal
coverage by ISSN. Abstracts come as an inverted index and are rebuilt here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Iterator, List, Optional

from ..http import Http
from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)
WORKS = "https://api.openalex.org/works"
SELECT = ",".join(
    [
        "id", "doi", "title", "publication_date", "type", "primary_location", "authorships",
        "abstract_inverted_index", "topics", "primary_topic", "cited_by_count", "open_access", "grants", "biblio",
    ]
)


def reconstruct_abstract(inv: Optional[Dict[str, List[int]]]) -> str:
    if not inv:
        return ""
    positions: List[tuple] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


class OpenAlexBudgetExhausted(RuntimeError):
    """OpenAlex refused the request: the free per-IP budget is used up and no API key was given."""


def works(http: Http, filters: Dict[str, str], mailto: str = "", api_key: str = "", max_records: int = 10000, select: str = SELECT) -> Iterator[Dict[str, Any]]:
    """Page through /works. Since 2026 OpenAlex meters anonymous traffic per IP; a free key
    (https://help.openalex.org/api/authentication/) is sent as a Bearer token when configured."""
    cursor = "*"
    n = 0
    flt = ",".join(f"{k}:{v}" for k, v in filters.items())
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    while n < max_records:
        params: Dict[str, Any] = {"filter": flt, "per-page": 200, "cursor": cursor, "select": select}
        if mailto:
            params["mailto"] = mailto
        resp = http.get(WORKS, params=params, headers=headers, ok_statuses=(200, 403, 429))
        if resp is None:
            break
        try:
            data = resp.json()
        except ValueError:
            break
        if resp.status_code != 200 or (isinstance(data, dict) and data.get("error")):
            msg = (data or {}).get("message", "") if isinstance(data, dict) else ""
            if "budget" in msg.lower() or resp.status_code in (403, 429):
                raise OpenAlexBudgetExhausted(msg[:200] or f"HTTP {resp.status_code}")
            break
        results = data.get("results") or []
        yield from results
        n += len(results)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not results or not cursor:
            break


def to_paper(w: Dict[str, Any]) -> Paper:
    doi = normalize_doi(w.get("doi"))
    oa_id = (w.get("id") or "").rsplit("/", 1)[-1]
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    authors: List[Author] = []
    affs: List[str] = []
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        insts = [{"name": i.get("display_name"), "type": i.get("type"), "ror": i.get("ror")} for i in (a.get("institutions") or [])]
        raw = a.get("raw_affiliation_strings") or []
        affs.extend(raw)
        authors.append(
            Author(
                name=au.get("display_name") or "",
                affiliation="; ".join(raw) if raw else "; ".join(i["name"] for i in insts if i.get("name")),
                orcid=(au.get("orcid") or "").rsplit("/", 1)[-1] if au.get("orcid") else "",
                openalex_id=(au.get("id") or "").rsplit("/", 1)[-1],
                institutions=insts,
            )
        )
    topics = [t.get("display_name") for t in (w.get("topics") or []) if t.get("display_name")]
    is_preprint = (w.get("type") or "") == "preprint" or (src.get("type") == "repository")
    grants = [g.get("funder_display_name") for g in (w.get("grants") or []) if g.get("funder_display_name")]
    p = Paper(
        id=Paper.make_id("openalex", oa_id, doi),
        source="openalex",
        kind="preprint" if is_preprint else "article",
        title=(w.get("title") or "").strip(),
        doi=doi,
        abstract=reconstruct_abstract(w.get("abstract_inverted_index")),
        journal=src.get("display_name") or "",
        pub_date=w.get("publication_date") or "",
        url=(f"https://doi.org/{doi}" if doi else (loc.get("landing_page_url") or w.get("id") or "")),
        license=(loc.get("license") or ""),
        authors=authors,
        affiliations=list(dict.fromkeys(a for a in affs if a)),
        cited_by_count=int(w.get("cited_by_count") or 0),
        topics=topics,
        extra={
            "openalex_id": oa_id,
            "primary_topic": ((w.get("primary_topic") or {}).get("id") or "").rsplit("/", 1)[-1],
            "company_institutions": sorted({i["name"] for a in authors for i in a.institutions if (i.get("type") == "company" and i.get("name"))}),
            "grants": grants,
        },
    )
    if grants:
        p.sections["funding"] = "; ".join(grants)
    return p


def or_join(values: Iterable[str]) -> str:
    return "|".join(v for v in values if v)
