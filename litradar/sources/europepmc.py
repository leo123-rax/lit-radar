"""Europe PMC REST API (https://europepmc.org/RestfulWebService).

Why it matters here: Europe PMC indexes PubMed + PMC + preprints, serves open-access full text as
XML, and supports *section-level* search fields (METHODS:, COMP_INT:, ACK_FUND: ...). That means we
can ask "which open-access papers published this week name 'NovaSeq' in their Methods" without
downloading anything.

Search:    GET /webservices/rest/search?query=...&format=json&pageSize=1000&cursorMark=*&resultType=core
Full text: GET /webservices/rest/{PMCID}/fullTextXML
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Iterator, List, Optional

from ..extract.jats import parse_jats
from ..http import Http
from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)
BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def search(http: Http, query: str, max_records: int = 5000, result_type: str = "core", page_size: int = 1000) -> Iterator[Dict[str, Any]]:
    cursor = "*"
    n = 0
    while n < max_records:
        data = http.get_json(
            f"{BASE}/search",
            params={"query": query, "format": "json", "pageSize": min(page_size, max_records - n), "cursorMark": cursor, "resultType": result_type},
        )
        if not data:
            break
        results = (data.get("resultList") or {}).get("result") or []
        yield from results
        n += len(results)
        nxt = data.get("nextCursorMark")
        if not results or not nxt or nxt == cursor:
            break
        cursor = nxt


def hit_count(http: Http, query: str) -> int:
    data = http.get_json(f"{BASE}/search", params={"query": query, "format": "json", "pageSize": 1, "resultType": "idlist"})
    return int((data or {}).get("hitCount") or 0)


def fulltext_xml(http: Http, pmcid: str) -> Optional[bytes]:
    resp = http.get(f"{BASE}/{pmcid}/fullTextXML", cache=True)
    if resp is None or resp.status_code != 200:
        return None
    return resp.content


def to_paper(rec: Dict[str, Any]) -> Paper:
    doi = normalize_doi(rec.get("doi"))
    src = (rec.get("source") or "MED").upper()
    native = f"{src}:{rec.get('id')}"
    authors: List[Author] = []
    affs: List[str] = []
    for a in ((rec.get("authorList") or {}).get("author") or []):
        name = a.get("fullName") or " ".join(x for x in [a.get("firstName"), a.get("lastName")] if x) or a.get("collectiveName") or ""
        aff_list = []
        for d in ((a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or []):
            if d.get("affiliation"):
                aff_list.append(d["affiliation"])
        if not aff_list and a.get("affiliation"):
            aff_list.append(a["affiliation"])
        orcid = ""
        aid = a.get("authorId") or {}
        if (aid.get("type") or "").upper() == "ORCID":
            orcid = aid.get("value") or ""
        if name:
            authors.append(Author(name=name, affiliation="; ".join(aff_list), orcid=orcid))
        affs.extend(aff_list)
    journal = ((rec.get("journalInfo") or {}).get("journal") or {}).get("title") or rec.get("journalTitle") or rec.get("bookOrReportDetails", {}).get("publisher", "") or ""
    pmcid = rec.get("pmcid") or ""
    url = f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{src}/{rec.get('id')}"
    is_preprint = src == "PPR"
    grants = [g.get("agency") for g in ((rec.get("grantsList") or {}).get("grant") or []) if g.get("agency")]
    p = Paper(
        id=Paper.make_id("europepmc", native, doi),
        source="europepmc",
        kind="preprint" if is_preprint else "article",
        title=(rec.get("title") or "").strip().rstrip("."),
        doi=doi,
        abstract=(rec.get("abstractText") or "").strip(),
        journal=journal if not is_preprint else (rec.get("publisher") or "preprint"),
        pub_date=rec.get("firstPublicationDate") or "",
        url=url,
        license=rec.get("license") or "",
        authors=authors,
        affiliations=list(dict.fromkeys(a for a in affs if a)),
        extra={"pmid": rec.get("pmid"), "pmcid": pmcid, "is_open_access": rec.get("isOpenAccess"), "in_epmc": rec.get("inEPMC"), "grants": grants, "epmc_id": native},
    )
    if grants:
        p.sections["funding"] = "; ".join(grants)
    return p


def attach_fulltext(http: Http, paper: Paper) -> bool:
    pmcid = (paper.extra or {}).get("pmcid")
    if not pmcid:
        return False
    xml = fulltext_xml(http, pmcid)
    if not xml:
        return False
    parsed = parse_jats(xml)
    if not parsed:
        return False
    for key in ("methods", "funding", "coi", "ack", "data_availability", "body"):
        if parsed.get(key):
            paper.sections[key] = parsed[key]
    if parsed.get("affiliations"):
        paper.affiliations = list(dict.fromkeys(paper.affiliations + parsed["affiliations"]))
    if parsed.get("emails"):
        paper.extra["emails"] = parsed["emails"]
    paper.fulltext_available = bool(parsed.get("methods") or parsed.get("body"))
    return paper.fulltext_available


# ---- query builders -------------------------------------------------------------------------
def date_clause(start: str, end: str) -> str:
    return f"(FIRST_PDATE:[{start} TO {end}])"


def journal_query(issns: Iterable[str], start: str, end: str) -> str:
    issn_part = " OR ".join(f'ISSN:"{i}"' for i in issns)
    return f"({issn_part}) AND {date_clause(start, end)}"


def section_query(field: str, terms: Iterable[str], start: str, end: str, open_access_only: bool = True) -> str:
    """field is one of METHODS, BODY, COMP_INT, ACK_FUND, AFF, ABSTRACT."""
    term_part = " OR ".join(f'{field}:"{t}"' for t in terms)
    q = f"({term_part}) AND {date_clause(start, end)}"
    if open_access_only:
        q += " AND (OPEN_ACCESS:y)"
    return q


def chunked(items: List[str], size: int) -> Iterator[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
