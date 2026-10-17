"""ClinicalTrials.gov API v2: studies first posted in a window whose sponsor, collaborator or
intervention matches watched terms (companies, assays, platforms).

Each study becomes a NewsItem(feed='clinicaltrials') whose summary lists the lead sponsor,
collaborators, interventions, conditions, phase, enrollment and status in plain text, so the
company gazetteer can match sponsor names exactly as the registry spells them.

Verified live on 2026-10-17 against API version 2.0.5 (OpenAPI spec: https://clinicaltrials.gov/api/oas/v2;
documentation: https://clinicaltrials.gov/data-api/api).

    GET https://clinicaltrials.gov/api/v2/studies
        ?filter.advanced=AREA[StudyFirstPostDate]RANGE[2026-10-03,2026-10-17]   # Essie syntax; MAX = open end
        &query.spons=Guardant OR Natera OR "Twist Bioscience"                   # sponsor / collaborator area
        &query.intr="circulating tumor DNA" OR Signatera                        # intervention / arm area
        &query.term=...                                                         # "other terms" (BasicSearch) area
        &fields=NCTId|BriefTitle|StudyFirstPostDate|LeadSponsorName|CollaboratorName|InterventionName|...
        &pageSize=200&countTotal=true                                           # pageSize is coerced down to 1,000
        &pageToken=<nextPageToken from the previous page>                       # omit on the first page

    200 -> {"totalCount": 387, "studies": [{"protocolSection": {...}}, ...], "nextPageToken": "ZVNj7o2E..."}
           nextPageToken is absent on the last page; totalCount is only returned with the first page.
    400 -> plain-text body ("Incorrect pageToken format"); the shared Http client returns None for it.

Sample study with the `fields` subset used here (abridged):

    {"protocolSection": {
       "identificationModule": {"nctId": "NCT07875829", "briefTitle": "Quality of Recovery After ..."},
       "statusModule": {"overallStatus": "RECRUITING", "studyFirstPostDateStruct": {"date": "2026-10-15"}},
       "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Hospital Central do Funchal", "class": "OTHER"},
                                      "collaborators": [{"name": "...", "class": "INDUSTRY"}]},
       "descriptionModule": {"briefSummary": "Minimally invasive techniques ..."},
       "conditionsModule": {"conditions": ["Recovery Outcomes"]},
       "designModule": {"studyType": "INTERVENTIONAL", "phases": ["NA"], "enrollmentInfo": {"count": 110, "type": "ESTIMATED"}},
       "armsInterventionsModule": {"interventions": [{"type": "PROCEDURE", "name": "Robotic vs laparoscopic surgery"}]}}}

Counts observed on 2026-10-17: 1,754 studies first posted 2026-10-03..2026-10-17 (unfiltered); the same window
with query.term="circulating tumor DNA" gave 7, query.intr the same phrase 5, query.spons=Guardant 0
(28 Guardant-sponsored studies overall, 77 for Natera). Essie `OR` works in every search area, so one
request covers a chunk of terms.

Etiquette: the API documentation asks clients to stay around 50 requests per minute per IP (the About API
page is rendered client-side and could not be re-read by script on 2026-10-17, so treat the figure as the
documented ceiling, not a measured one). The shared Http client paces clinicaltrials.gov at 1.25 s per
request (~48/min).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from ..http import Http
from ..models import NewsItem

log = logging.getLogger(__name__)

API = "https://clinicaltrials.gov/api/v2/studies"
STUDY_URL = "https://clinicaltrials.gov/study/{nct}"
FEED = "clinicaltrials"
PAGE_SIZE = 200  # the API coerces anything above 1,000 down to 1,000
MAX_RECORDS = 2000  # per query; a full unfiltered week is ~900 studies
CHUNK = 40  # terms per Essie OR-expression (keeps URLs short and queries cheap to evaluate)
SUMMARY_MAX = 1500  # same cap as the RSS news adapter

# piece names from https://clinicaltrials.gov/data-api/about-api/study-data-structure
FIELDS = [
    "NCTId", "BriefTitle", "OfficialTitle", "StudyFirstPostDate", "OverallStatus", "StudyType",
    "LeadSponsorName", "LeadSponsorClass", "CollaboratorName", "CollaboratorClass",
    "InterventionName", "InterventionType", "Condition", "Phase", "EnrollmentCount", "EnrollmentType",
    "BriefSummary",
]

_PHASES = {
    "EARLY_PHASE1": "Early Phase 1", "PHASE1": "Phase 1", "PHASE2": "Phase 2", "PHASE3": "Phase 3",
    "PHASE4": "Phase 4", "NA": "N/A",
}


# ------------------------------------------------------------------ query building
def essie_term(term: str) -> str:
    """Quote a term for Essie: multi-word or punctuated terms go in double quotes; embedded quotes are dropped."""
    t = (term or "").replace('"', "").strip()
    if not t:
        return ""
    if any(ch in t for ch in " -()[]:,/.&'") or t.upper() in ("OR", "AND", "NOT"):
        return f'"{t}"'
    return t


def essie_or(terms: Iterable[str]) -> str:
    parts = [p for p in (essie_term(t) for t in terms) if p]
    return " OR ".join(dict.fromkeys(parts))


def date_range_filter(start: str, end: Optional[str] = None) -> str:
    """Essie filter on the study's first-post date: AREA[StudyFirstPostDate]RANGE[start,end]; end=None means open."""
    return f"AREA[StudyFirstPostDate]RANGE[{start},{end or 'MAX'}]"


def chunked(seq: Sequence[str], n: int) -> Iterator[List[str]]:
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


def _params(start: str, end: Optional[str], term: Optional[str], spons: Optional[str], intr: Optional[str],
            page_size: int, fields: Optional[Sequence[str]]) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "filter.advanced": date_range_filter(start, end),
        "pageSize": max(1, min(int(page_size), 1000)),
        "countTotal": "true",
    }
    if term:
        params["query.term"] = term
    if spons:
        params["query.spons"] = spons
    if intr:
        params["query.intr"] = intr
    if fields:
        params["fields"] = "|".join(fields)
    return params


# ------------------------------------------------------------------ raw access
def search(
    http: Http,
    start: str,
    end: Optional[str] = None,
    term: Optional[str] = None,
    spons: Optional[str] = None,
    intr: Optional[str] = None,
    page_size: int = PAGE_SIZE,
    max_records: int = MAX_RECORDS,
    fields: Optional[Sequence[str]] = FIELDS,
) -> Iterator[Dict[str, Any]]:
    """Yield raw study dicts first posted in [start, end], following pageToken until the API stops handing one out."""
    params = _params(start, end, term, spons, intr, page_size, fields)
    label = spons or intr or term or "all"
    seen_tokens: set = set()
    yielded = 0
    page = 0
    while True:
        page += 1
        data = http.get_json(API, params=params)
        if not isinstance(data, dict):
            if page == 1:
                log.warning("ClinicalTrials.gov returned nothing for %s..%s (%s)", start, end or "MAX", label)
            else:
                log.warning("ClinicalTrials.gov page %d failed; keeping %d studies", page, yielded)
            return
        studies = data.get("studies")
        if not isinstance(studies, list):
            log.warning("ClinicalTrials.gov: unexpected payload shape (no 'studies' list)")
            return
        if page == 1 and "totalCount" in data:
            log.info("ClinicalTrials.gov: %s studies first posted %s..%s (%s)", data.get("totalCount"), start, end or "MAX", label)
        if not studies:
            return
        for s in studies:
            if isinstance(s, dict):
                yield s
                yielded += 1
                if yielded >= max_records:
                    log.warning("ClinicalTrials.gov: stopping at max_records=%d", max_records)
                    return
        token = data.get("nextPageToken")
        if not token or not isinstance(token, str) or token in seen_tokens:
            return
        seen_tokens.add(token)
        params = dict(params, pageToken=token)
        params.pop("countTotal", None)  # ignored after the first page anyway


def count(http: Http, start: str, end: Optional[str] = None, term: Optional[str] = None,
          spons: Optional[str] = None, intr: Optional[str] = None) -> int:
    """Cheap sizing call: one study per page with countTotal=true."""
    params = _params(start, end, term, spons, intr, 1, ["NCTId"])
    data = http.get_json(API, params=params)
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("totalCount") or 0)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ normalisation
def _enum_label(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    return " ".join(w.capitalize() for w in s.replace("_", " ").split())


def phase_label(phases: Any) -> str:
    if not isinstance(phases, list):
        return ""
    labels = [_PHASES.get(str(p), _enum_label(p)) for p in phases if p]
    labels = [p for p in labels if p]
    if not labels:
        return ""
    if len(labels) > 1 and all(p.startswith("Phase ") for p in labels):
        return "Phase " + "/".join(p.split(" ", 1)[1] for p in labels)
    return "/".join(labels)


def _names(items: Any, key: str = "name") -> List[str]:
    out: List[str] = []
    if not isinstance(items, list):
        return out
    for it in items:
        v = str(it.get(key) or "").strip() if isinstance(it, dict) else str(it or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def study_summary(ps: Dict[str, Any]) -> str:
    """Plain-text summary: sponsor, collaborators, interventions, conditions, phase, enrollment, status, type."""
    spons = ps.get("sponsorCollaboratorsModule") or {}
    design = ps.get("designModule") or {}
    lead = spons.get("leadSponsor") if isinstance(spons.get("leadSponsor"), dict) else {}
    parts: List[str] = []
    lead_name = str(lead.get("name") or "").strip()
    if lead_name:
        cls = _enum_label(lead.get("class"))
        parts.append(f"Sponsor: {lead_name}" + (f" ({cls})" if cls else ""))
    collabs = _names(spons.get("collaborators"))
    if collabs:
        parts.append("Collaborators: " + "; ".join(collabs))
    interventions: List[str] = []
    for iv in (ps.get("armsInterventionsModule") or {}).get("interventions") or []:
        if not isinstance(iv, dict):
            continue
        nm = str(iv.get("name") or "").strip()
        if not nm:
            continue
        typ = _enum_label(iv.get("type"))
        interventions.append(f"{typ}: {nm}" if typ else nm)
    if interventions:
        parts.append("Interventions: " + "; ".join(dict.fromkeys(interventions)))
    conds = _names((ps.get("conditionsModule") or {}).get("conditions"))
    if conds:
        parts.append("Conditions: " + "; ".join(conds))
    ph = phase_label(design.get("phases"))
    if ph:
        parts.append(f"Phase: {ph}")
    enr = design.get("enrollmentInfo") or {}
    if isinstance(enr, dict) and enr.get("count") is not None:
        typ = _enum_label(enr.get("type")).lower()
        parts.append(f"Enrollment: {enr.get('count')}" + (f" ({typ})" if typ else ""))
    status = _enum_label((ps.get("statusModule") or {}).get("overallStatus"))
    if status:
        parts.append(f"Status: {status}")
    stype = _enum_label(design.get("studyType"))
    if stype:
        parts.append(f"Type: {stype}")
    brief = str((ps.get("descriptionModule") or {}).get("briefSummary") or "").strip()
    text = _join_sentences(parts)
    if brief:
        room = SUMMARY_MAX - len(text) - len(". Summary: ")
        if room > 40:
            brief = " ".join(brief.split())
            text = _join_sentences([text, "Summary: " + (brief if len(brief) <= room else brief[: room - 1].rstrip() + "…")])
    return text[:SUMMARY_MAX]


def _join_sentences(parts: Sequence[str]) -> str:
    """Join with '. ' without doubling a period after names like 'Co., Ltd.'."""
    out = ""
    for p in parts:
        if not p:
            continue
        if not out:
            out = p
        else:
            out += (" " if out.endswith(".") else ". ") + p
    return out


def to_news_item(study: Dict[str, Any]) -> Optional[NewsItem]:
    """Convert one API study object into a NewsItem; None when it carries no NCT id."""
    ps = study.get("protocolSection") if isinstance(study, dict) else None
    if not isinstance(ps, dict):
        return None
    ident = ps.get("identificationModule") or {}
    nct = str(ident.get("nctId") or "").strip().upper()
    if not nct.startswith("NCT"):
        return None
    title = str(ident.get("briefTitle") or ident.get("officialTitle") or nct).strip()
    status = ps.get("statusModule") or {}
    posted = str(((status.get("studyFirstPostDateStruct") or {}).get("date")) or "")[:10]
    return NewsItem(
        id=f"ctgov:{nct}",
        feed=FEED,
        title=title,
        url=STUDY_URL.format(nct=nct),
        published=posted,
        summary=study_summary(ps),
    )


# ------------------------------------------------------------------ entry point
def new_studies(
    http: Http,
    start: str,
    end: Optional[str] = None,
    sponsor_terms: Optional[Sequence[str]] = None,
    intervention_terms: Optional[Sequence[str]] = None,
    terms: Optional[Sequence[str]] = None,
    chunk: int = CHUNK,
    page_size: int = PAGE_SIZE,
    max_records: int = MAX_RECORDS,
) -> Iterator[NewsItem]:
    """Studies first posted in [start, end] whose sponsor/collaborator, intervention or free text matches a term.

    One request chain per (search area, chunk of `chunk` terms); results are de-duplicated by NCT id.
    With no terms at all, every study first posted in the window is returned.
    """
    queries: List[Dict[str, str]] = []
    for area, words in (("spons", sponsor_terms), ("intr", intervention_terms), ("term", terms)):
        cleaned = [str(w) for w in (words or []) if w and str(w).strip()]
        for c in chunked(cleaned, max(1, chunk)):
            expr = essie_or(c)
            if expr:
                queries.append({area: expr})
    if not queries:
        queries.append({})
    seen: set = set()
    for q in queries:
        for study in search(http, start, end, term=q.get("term"), spons=q.get("spons"), intr=q.get("intr"),
                            page_size=page_size, max_records=max_records):
            item = to_news_item(study)
            if item is None or item.id in seen:
                continue
            seen.add(item.id)
            yield item
