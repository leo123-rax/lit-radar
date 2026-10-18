"""Europe PMC REST API (https://europepmc.org/RestfulWebService).

Why it matters here: Europe PMC indexes PubMed + PMC + preprints, serves open-access full text as
XML, and supports *section-level* search fields (METHODS:, COMP_INT:, ACK_FUND: ...). That means we
can ask "which open-access papers published this week name 'NovaSeq' in their Methods" without
downloading anything.

Search:    GET /webservices/rest/search?query=...&format=json&pageSize=1000&cursorMark=*&resultType=core
Full text: GET /webservices/rest/{PMCID}/fullTextXML

Date windows (``date_clause``) combine FIRST_PDATE (first publication date) with CREATION_DATE (the day the record
entered the index), because PubMed records reach Europe PMC 1-14 days after publication; see the function.

Record shapes worth knowing (resultType=core, verified live):
* Preprints (``source == "PPR"``) have no top-level ``publisher`` and ``journalInfo`` is null; the
  server name is ``bookOrReportDetails.publisher`` ("bioRxiv", "medRxiv", "Research Square", ...).
  ``license`` is null and ``isOpenAccess`` is "N" for PPR records even when the server is CC-licensed.
* Journal articles carry ``journalInfo.journal.{title, issn, essn}``; ``license`` ("cc by",
  "cc by-nc-nd", ...) is populated for the open-access subset and null otherwise; ``isOpenAccess``
  is "Y"/"N". ``fullTextXML`` answers HTTP 500 for non-OA PMCIDs, so attach_fulltext skips records
  that are explicitly not open access and uses a single retry for the rest.
``Paper.license`` is set from the search ``license`` field; ``Paper.extra`` keeps ``is_open_access``
and ``license`` too so the pipeline can gate full-text storage on them.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional

from ..extract.jats import parse_jats
from ..http import Http
from ..models import Author, Paper, normalize_doi
from . import clean_text, jats_abstract

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


def fulltext_xml(http: Http, pmcid: str, retries: int = 1) -> Optional[bytes]:
    """Open-access JATS XML for a PMCID. One retry by default: a 5xx here is almost always deterministic
    (the record is not in the OA subset), and the default policy would sleep 31 s per failing id."""
    resp = http.get(f"{BASE}/{pmcid}/fullTextXML", cache=True, retries=retries)
    if resp is None or resp.status_code != 200:
        return None
    return resp.content


def is_open_access(paper: Paper) -> Optional[bool]:
    """True/False from the search record's isOpenAccess flag; None when Europe PMC did not say."""
    flag = str((paper.extra or {}).get("is_open_access") or "").strip().upper()
    if flag == "Y":
        return True
    if flag == "N":
        return False
    return None


def preprint_server(rec: Dict[str, Any]) -> str:
    """Server name for a PPR record: bookOrReportDetails.publisher, else journalInfo, else ''."""
    details = rec.get("bookOrReportDetails") or {}
    name = (details.get("publisher") or "").strip() if isinstance(details, dict) else ""
    if not name:
        name = (((rec.get("journalInfo") or {}).get("journal") or {}).get("title") or "").strip()
    if not name:
        name = (rec.get("publisher") or "").strip()
    return name


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
    jinfo = ((rec.get("journalInfo") or {}).get("journal") or {}) if isinstance(rec.get("journalInfo"), dict) else {}
    details = rec.get("bookOrReportDetails") if isinstance(rec.get("bookOrReportDetails"), dict) else {}
    journal = jinfo.get("title") or rec.get("journalTitle") or details.get("publisher") or ""
    issns = [x for x in (jinfo.get("issn"), jinfo.get("essn")) if x]
    pmcid = rec.get("pmcid") or ""
    url = f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{src}/{rec.get('id')}"
    is_preprint = src == "PPR"
    grants = [g.get("agency") for g in ((rec.get("grantsList") or {}).get("grant") or []) if g.get("agency")]
    license_str = (rec.get("license") or "").strip()
    p = Paper(
        id=Paper.make_id("europepmc", native, doi),
        source="europepmc",
        kind="preprint" if is_preprint else "article",
        title=clean_text(rec.get("title") or "").rstrip("."),
        doi=doi,
        abstract=clean_text(rec.get("abstractText") or ""),
        journal=journal if not is_preprint else (preprint_server(rec) or "preprint"),
        pub_date=rec.get("firstPublicationDate") or "",
        url=url,
        license=license_str,
        authors=authors,
        affiliations=list(dict.fromkeys(a for a in affs if a)),
        extra={
            "pmid": rec.get("pmid"),
            "pmcid": pmcid,
            "is_open_access": rec.get("isOpenAccess"),
            "license": license_str,
            "in_epmc": rec.get("inEPMC"),
            "grants": grants,
            "epmc_id": native,
            "issn": issns,
            "medline_abbreviation": jinfo.get("medlineAbbreviation") or "",
        },
    )
    if grants:
        p.sections["funding"] = "; ".join(grants)
    return p


def attach_fulltext(http: Http, paper: Paper, retries: int = 1) -> bool:
    """Fetch and attach open-access full text. Records Europe PMC flags as not open access are skipped
    without a request (fullTextXML answers 500 for them and the licence would not allow storage)."""
    pmcid = (paper.extra or {}).get("pmcid")
    if not pmcid:
        return False
    if is_open_access(paper) is False:
        log.debug("%s: not in the Europe PMC open-access subset; metadata only", pmcid)
        return False
    xml = fulltext_xml(http, pmcid, retries=retries)
    if not xml:
        return False
    parsed = parse_jats(xml)
    if not parsed:
        return False
    for key in ("methods", "funding", "coi", "ack", "data_availability", "body"):
        if parsed.get(key):
            paper.sections[key] = parsed[key]
    if not paper.abstract:
        # the search record had no abstractText (true for ~15% of the corpus, e.g. Science family titles) but the
        # full text just downloaded carries <abstract>: fill it here so the digest can summarise the paper
        paper.abstract = jats_abstract(xml)
    if parsed.get("affiliations"):
        paper.affiliations = list(dict.fromkeys(paper.affiliations + parsed["affiliations"]))
    if parsed.get("emails"):
        paper.extra["emails"] = parsed["emails"]
    paper.fulltext_available = bool(parsed.get("methods") or parsed.get("body"))
    return paper.fulltext_available


# ---- query builders -------------------------------------------------------------------------
# How far behind its publication date a record may enter the index and still be collected by the index-date
# route of `date_clause`. Measured 2026-10-17 (docs/live.md): PubMed-load lag 1-14 days, median ~8; a 30-day
# floor also admits publisher-deposited records that Europe PMC re-dates on PubMed load.
INDEX_LAG_DAYS = 30


def date_clause(start: str, end: str, index_window: bool = True) -> str:
    """Date restriction for a [start, end] pull (inclusive, YYYY-MM-DD).

    Records are collected when they were FIRST PUBLISHED in the window (``FIRST_PDATE``) OR when they ENTERED the
    index in the window (``CREATION_DATE``) with a publication date no more than `INDEX_LAG_DAYS` before it. The
    second arm is what a daily / weekly pull needs: Europe PMC indexes most PubMed records 1-14 days after
    publication (median ~8 on 2026-10-17), so a publication-date window over the last day or two is structurally
    empty (``SRC:MED`` 1-day: 27 records by FIRST_PDATE vs 2,831 by CREATION_DATE) and a 2-day overlap between runs
    never catches up; the publication-date floor keeps legacy back-files out. Verified live 2026-10-18 for
    ``ISSN:"0028-0836"`` over 2026-10-11..18: FIRST_PDATE alone 0 hits, the combined clause 102.
    ``index_window=False`` gives the plain publication-date clause (for historical pulls, where the index date is
    meaningless)."""
    pub = f"FIRST_PDATE:[{start} TO {end}]"
    if not index_window:
        return f"({pub})"
    try:
        floor = (date.fromisoformat(start) - timedelta(days=INDEX_LAG_DAYS)).isoformat()
    except ValueError:  # not an ISO date (a test stub, or a Europe PMC relative date): no floor
        return f"(({pub}) OR (CREATION_DATE:[{start} TO {end}]))"
    return f"(({pub}) OR (CREATION_DATE:[{start} TO {end}] AND FIRST_PDATE:[{floor} TO {end}]))"


def journal_query(issns: Iterable[str], start: str, end: str, index_window: bool = True) -> str:
    issn_part = " OR ".join(f'ISSN:"{i}"' for i in issns)
    return f"({issn_part}) AND {date_clause(start, end, index_window)}"


def section_query(field: str, terms: Iterable[str], start: str, end: str, open_access_only: bool = True, index_window: bool = True) -> str:
    """field is one of METHODS, BODY, COMP_INT, ACK_FUND, AFF, ABSTRACT."""
    term_part = " OR ".join(f'{field}:"{t}"' for t in terms)
    q = f"({term_part}) AND {date_clause(start, end, index_window)}"
    if open_access_only:
        q += " AND (OPEN_ACCESS:y)"
    return q


def chunked(items: List[str], size: int) -> Iterator[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
