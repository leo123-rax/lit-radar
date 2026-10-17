"""openFDA device endpoints: 510(k) clearances, De Novo grants and PMA approvals / supplements as NewsItems.

Official documentation
    510(k):          https://open.fda.gov/apis/device/510k/
    PMA:             https://open.fda.gov/apis/device/pma/
    query syntax:    https://open.fda.gov/apis/query-syntax/
    paging:          https://open.fda.gov/apis/paging/
    authentication:  https://open.fda.gov/apis/authentication/
    field reference: https://open.fda.gov/apis/openfda-fields/

Verified live on 2026-10-17 (window 2026-09-17..2026-10-17; dataset `last_updated` 2026-10-05; newest
indexed decision 2026-10-02 for 510(k) and 2026-10-03 for PMA; bulk-file export_date 2026-10-12):

    GET https://api.fda.gov/device/510k.json?search=decision_date:[20260917+TO+20261017]&limit=1000&skip=0
    GET https://api.fda.gov/device/pma.json?search=decision_date:[20260917+TO+20261017]&limit=1000&skip=0

    response: {"meta": {"last_updated": "2026-10-05", "results": {"skip": 0, "limit": 1000, "total": 182}},
               "results": [ {...}, ... ]}
    510(k) record fields (top level): k_number, applicant, device_name, decision_date ("2026-09-18"),
        decision_code (SESE, SESK, SESU, SESD, SESP, SEKD, SN, ST, PT, SI, DENG), decision_description
        ("Substantially Equivalent" / "Unknown"), product_code, advisory_committee ("GU"),
        advisory_committee_description ("Gastroenterology, Urology"), review_advisory_committee,
        clearance_type ("Traditional", "Special", "Abbreviated", "Direct"), statement_or_summary
        ("Summary" | "Statement" | ""), third_party_flag, expedited_review_flag, date_received,
        contact, address_1, address_2, city, state, zip_code, postal_code, country_code,
        openfda: {device_name (classification name), regulation_number, device_class,
                  medical_specialty_description, registration_number[], fei_number[]}
    PMA record fields: pma_number, supplement_number ("" for an original PMA), applicant, trade_name,
        generic_name, product_code, advisory_committee, advisory_committee_description,
        supplement_type ("30-Day Notice", "Normal 180 Day Track", "Panel Track", "Real-Time Process", ...),
        supplement_reason, decision_code ("APPR" original approval, "OK30" 30-day notice accepted, ...),
        decision_date, date_received, ao_statement, docket_number, expedited_review_flag,
        street_1, street_2, city, state, zip, zip_ext, openfda: {device_name, regulation_number,
        device_class, medical_specialty_description, registration_number[], fei_number[]}

    De Novo: there is NO /device/denovo.json (HTTP 404 "Cannot GET /device/denovo.json"; the device
    endpoint list from https://api.fda.gov/download.json is classification, 510k, covid19serology,
    registrationlisting, enforcement, udi, event, recall, pma). However De Novo GRANTS are carried
    inside the 510k dataset: k_number "DEN260007", decision_code "DENG", clearance_type "Direct",
    decision_description "Unknown" (490 such records all-time). This adapter routes them to the
    `fda-denovo` feed and links them to the FDA De Novo database page. The web database remains the
    authoritative De Novo listing: https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/denovo.cfm

    Empty window: HTTP 404 with {"error": {"code": "NOT_FOUND", "message": "No matches found!"}}. The
    adapter requests with ok_statuses=(200, 404), so this routine answer is not retried, not logged with
    the request URL (which may carry the api_key) and not counted in http.stats["failures"]; it is logged
    at INFO as "no matches". None from the client (network error, 5xx after retries, 429 host block) is
    logged as a WARNING so an outage is distinguishable from an empty window.
    Refresh cadence: the official 510(k) and PMA pages state "Frequency of API updates: Monthly". On
    2026-10-17 the index ended at 2026-10-02 (510k) / 2026-10-03 (PMA), i.e. 15 days behind, and just
    before a monthly refresh it can be 4-6 weeks behind. A weekly caller's look-back must exceed one
    refresh interval plus the weekly cadence, so use >= 45 days (RECOMMENDED_LOOKBACK_DAYS; 60 is safer);
    the store dedupes re-fetched decisions by id.
    Paging: `limit` max 1000 (PAGE_SIZE clamp); fetch() defaults to pages of 250 (DEFAULT_PAGE_SIZE)
    because a 510(k) record is 8-20 KB (openfda.registration_number / fei_number lists), which keeps a
    page at ~2-5 MB (250 records of Aug-Oct 2026 = 2.1 MB live). `skip` max 25000 ("Skip value must
    25000 or less."); larger result sets need the Link-header `search_after` cursor or the bulk download.
    A month is ~220-325 510(k)s and ~110-120 PMA decisions, so a 45-60 day window is 3-4 requests;
    multi-year backfills should go month by month.
    Rate limits (per the authentication page): 240 requests/minute and 1,000/day per IP without a
    key; 240/minute and 120,000/day with a free key. The key is read from OPENFDA_API_KEY and sent
    as the documented `api_key` query parameter.

FDA database links used for `NewsItem.url`:
    510(k):  https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K254179
    De Novo: https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/denovo.cfm?ID=DEN260007
    PMA:     https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpma/pma.cfm?id=P200002S015  (original: ?id=P200002)
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Iterable, Iterator, Optional, Sequence, Tuple

from ..http import Http
from ..models import NewsItem

log = logging.getLogger(__name__)

BASE = "https://api.fda.gov/device"
ENDPOINTS = {"510k": f"{BASE}/510k.json", "pma": f"{BASE}/pma.json"}
FEED_510K = "fda-510k"
FEED_PMA = "fda-pma"
FEED_DENOVO = "fda-denovo"
PAGE_SIZE = 1000  # openFDA maximum for `limit` (clamp)
DEFAULT_PAGE_SIZE = 250  # fetch() default: a 510(k) record is 8-20 KB, so 250 keeps one response at ~2-5 MB
MAX_SKIP = 25000  # openFDA refuses skip > 25000
DEFAULT_MAX_RECORDS = MAX_SKIP + PAGE_SIZE
RECOMMENDED_LOOKBACK_DAYS = 45  # the datasets refresh monthly: one refresh interval + the weekly cadence, with margin
ID_PREFIX = "openfda:"

DB_510K = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID={number}"
DB_DENOVO = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/denovo.cfm?ID={number}"
DB_PMA = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpma/pma.cfm?id={number}"

# Advisory committee codes that cover in-vitro diagnostics (live `count=advisory_committee.exact`
# over 2025-01-01..2026-10-17 on the 510(k) endpoint): CH Clinical Chemistry, HE Hematology,
# IM Immunology, MI Microbiology, PA Pathology, TX Clinical Toxicology, MG Medical Genetics.
IVD_ADVISORY_COMMITTEES = ("CH", "HE", "IM", "MI", "PA", "TX", "MG")

# Decision codes seen live; anything else is passed through verbatim.
PMA_DECISIONS = {"APPR": "approved", "OK30": "30-day notice accepted"}
DENOVO_DECISIONS = {"DENG": "De Novo granted"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def api_key_from_env() -> str:
    """OPENFDA_API_KEY raises the daily quota from 1,000 to 120,000 requests; empty string when unset."""
    return (os.environ.get("OPENFDA_API_KEY") or "").strip()


def _compact(d: str) -> str:
    """'2026-09-18' or '20260918' -> '20260918' (the form openFDA range queries use)."""
    s = re.sub(r"[^0-9]", "", d or "")
    if len(s) != 8:
        raise ValueError(f"expected a YYYY-MM-DD or YYYYMMDD date, got {d!r}")
    return s


def _iso(d: Any) -> str:
    """'2026-09-18' or '20260918' -> '2026-09-18'; anything else -> ''."""
    s = re.sub(r"[^0-9]", "", str(d or ""))
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else ""


def _clean(v: Any) -> str:
    return _CONTROL.sub("", str(v or "")).strip()


def _openfda(rec: Dict[str, Any]) -> Dict[str, Any]:
    o = rec.get("openfda")
    return o if isinstance(o, dict) else {}


def _join(parts: Iterable[str]) -> str:
    return " · ".join(p for p in parts if p)


def date_range_query(start: str, end: str, extra: str = "") -> str:
    """`decision_date:[YYYYMMDD TO YYYYMMDD]`, optionally `AND <extra>` (e.g. `applicant:"Roche"`)."""
    q = f"decision_date:[{_compact(start)} TO {_compact(end)}]"
    return f"{q} AND {extra}" if extra else q


def _params(query: str, limit: int, skip: int, api_key: str) -> Dict[str, Any]:
    p: Dict[str, Any] = {}
    if api_key:
        p["api_key"] = api_key  # the docs ask for the key before the other parameters
    p.update({"search": query, "limit": limit, "skip": skip})
    return p


def _request(http: Http, url: str, params: Dict[str, Any], label: str) -> Tuple[Optional[int], Any]:
    """One GET against openFDA. Returns (status, parsed JSON body).

    status None -> the request failed (network error, 5xx after retries, 429 host block); the caller logs it.
    status 404  -> openFDA's routine "No matches found!" answer (not a failure; body is None).
    status 200  -> body is the parsed JSON, or None when the response was not JSON (logged as WARNING).
    Other 4xx (e.g. 400 "Skip value must 25000 or less.") are logged as WARNING with the API's message.
    """
    resp = http.get(url, params=params, headers={"Accept": "application/json"}, ok_statuses=(200, 404))
    if resp is None:
        return None, None
    status = getattr(resp, "status_code", None)
    if status == 404:
        return 404, None
    body: Any = None
    try:
        body = resp.json()
    except ValueError:
        if status == 200:
            log.warning("openFDA %s: non-JSON response", label)
            return 200, None
    if status != 200:
        err = body.get("error") if isinstance(body, dict) else body
        log.warning("openFDA %s: HTTP %s %s", label, status, err or "")
        return status, None
    return 200, body


def search(
    http: Http,
    endpoint: str,
    query: str,
    api_key: str = "",
    page_size: int = PAGE_SIZE,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> Iterator[Dict[str, Any]]:
    """Yield raw result records from one endpoint ('510k' or 'pma', or a full URL), paging with skip/limit.

    Stops when the reported total is reached, a short page arrives, the skip cap is hit, or a page fails.
    An empty window answers HTTP 404 ("No matches found!"), logged at INFO; a failed request (None from
    the client) is logged at WARNING so an outage never looks like an empty window.
    """
    url = ENDPOINTS.get(endpoint, endpoint)
    page_size = max(1, min(int(page_size), PAGE_SIZE))
    skip = 0
    total: Optional[int] = None
    while True:
        status, data = _request(http, url, _params(query, page_size, skip, api_key), f"{endpoint} skip={skip}")
        if status is None:
            if skip == 0:
                log.warning("openFDA %s: request failed for %s (network error, server error or host blocked); no results", endpoint, query)
            else:
                log.warning("openFDA %s: page at skip=%d failed; stopping early after %d records", endpoint, skip, skip)
            return
        if status == 404:
            if skip == 0:
                log.info("openFDA %s: no matches for %s", endpoint, query)
            else:
                log.warning("openFDA %s: HTTP 404 at skip=%d after %d records (result set changed mid-run?)", endpoint, skip, skip)
            return
        if data is None:
            return  # non-JSON or non-200 answer, already logged by _request
        if not isinstance(data, dict):
            log.warning("openFDA %s: unexpected payload type %s", endpoint, type(data).__name__)
            return
        if "error" in data:
            log.warning("openFDA %s: %s", endpoint, data.get("error"))
            return
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return
        if total is None:
            meta_total = ((data.get("meta") or {}).get("results") or {}).get("total")
            if isinstance(meta_total, int):
                total = meta_total
                if total > max_records:
                    log.warning(
                        "openFDA %s: %d matches exceed max_records=%d; narrow the window (skip is capped at %d)",
                        endpoint, total, max_records, MAX_SKIP,
                    )
        for rec in results:
            if isinstance(rec, dict):
                yield rec
        skip += len(results)
        if total is not None and skip >= total:
            return
        if len(results) < page_size:
            return
        if skip >= max_records or skip > MAX_SKIP:
            log.warning("openFDA %s: stopped at skip=%d (cap)", endpoint, skip)
            return


def _committee_code(rec: Dict[str, Any]) -> str:
    """advisory_committee, falling back to review_advisory_committee (De Novo rows leave the former blank)."""
    return _clean(rec.get("advisory_committee")) or _clean(rec.get("review_advisory_committee"))


def _committee(rec: Dict[str, Any]) -> str:
    code = _clean(rec.get("advisory_committee"))
    desc = _clean(rec.get("advisory_committee_description"))
    if desc.lower() == "unknown":
        desc = ""
    if code and desc:
        return f"advisory committee {code} ({desc})"
    if code or desc:
        return f"advisory committee {code or desc}"
    review = _clean(rec.get("review_advisory_committee"))
    return f"review advisory committee {review}" if review else ""


def _product(rec: Dict[str, Any]) -> str:
    o = _openfda(rec)
    code = _clean(rec.get("product_code"))
    name = _clean(o.get("device_name"))
    if code and name:
        return f"product code {code} ({name})"
    return f"product code {code}" if code else (f"classification {name}" if name else "")


def _regulation(rec: Dict[str, Any]) -> str:
    o = _openfda(rec)
    reg = _clean(o.get("regulation_number"))
    cls = _clean(o.get("device_class"))
    return _join([f"regulation {reg}" if reg else "", f"class {cls}" if cls else ""])


def _location(rec: Dict[str, Any]) -> str:
    bits = [_clean(rec.get("city")), _clean(rec.get("state")) or _clean(rec.get("country_code"))]
    return ", ".join(b for b in bits if b)


def news_item_510k(rec: Dict[str, Any]) -> Optional[NewsItem]:
    """One 510(k) (or De Novo, when the number starts with DEN / decision is DENG) record -> NewsItem."""
    number = _clean(rec.get("k_number"))
    if not number:
        return None
    decision_code = _clean(rec.get("decision_code")).upper()
    is_denovo = number.upper().startswith("DEN") or decision_code == "DENG"
    applicant = _clean(rec.get("applicant"))
    device = _clean(rec.get("device_name")) or _clean(_openfda(rec).get("device_name")) or "(unnamed device)"
    decision_date = _iso(rec.get("decision_date"))
    decision_desc = _clean(rec.get("decision_description"))
    if decision_desc.lower() == "unknown":
        decision_desc = ""
    if is_denovo:
        decision_desc = decision_desc or DENOVO_DECISIONS.get(decision_code, "")
    decision = f"decision {decision_code}" if decision_code else "decision"
    if decision_desc:
        decision += f" ({decision_desc})"
    if decision_date:
        decision += f" {decision_date}"
    clearance = _clean(rec.get("clearance_type"))
    statement = _clean(rec.get("statement_or_summary"))
    flags = _join([
        "third-party review" if _clean(rec.get("third_party_flag")).upper() == "Y" else "",
        "expedited review" if _clean(rec.get("expedited_review_flag")).upper() == "Y" else "",
    ])
    received = _iso(rec.get("date_received"))
    label = f"De Novo {number}" if is_denovo else f"510(k) {number}"
    who = _join([f"applicant {applicant}" if applicant else "", _location(rec)]).replace(" · ", ", ", 1)
    summary = _join([
        label,
        _product(rec),
        _regulation(rec),
        _committee(rec),
        decision,
        f"clearance type {clearance}" if clearance else "",
        who,
        f"received {received}" if received else "",
        f"510(k) {statement.lower()} available" if statement else "",
        flags,
    ])
    return NewsItem(
        id=f"{ID_PREFIX}{number}",
        feed=FEED_DENOVO if is_denovo else FEED_510K,
        title=f"{device} — {applicant}" if applicant else device,
        url=(DB_DENOVO if is_denovo else DB_510K).format(number=number),
        published=decision_date,
        summary=summary[:1500],
    )


def news_item_pma(rec: Dict[str, Any]) -> Optional[NewsItem]:
    """One PMA original or supplement record -> NewsItem."""
    pma = _clean(rec.get("pma_number"))
    if not pma:
        return None
    supp = _clean(rec.get("supplement_number"))
    number = f"{pma}{supp}"
    applicant = _clean(rec.get("applicant"))
    device = _clean(rec.get("trade_name")) or _clean(rec.get("generic_name")) or _clean(_openfda(rec).get("device_name")) or "(unnamed device)"
    decision_code = _clean(rec.get("decision_code")).upper()
    decision_date = _iso(rec.get("decision_date"))
    decision = f"decision {decision_code}" if decision_code else "decision"
    if decision_code in PMA_DECISIONS:
        decision += f" ({PMA_DECISIONS[decision_code]})"
    if decision_date:
        decision += f" {decision_date}"
    stype = _clean(rec.get("supplement_type"))
    sreason = _clean(rec.get("supplement_reason"))
    supplement = ""
    if supp:
        supplement = f"supplement {supp}" + (f": {stype}" if stype else "") + (f" — {sreason}" if sreason else "")
    generic = _clean(rec.get("generic_name"))
    ao = _clean(rec.get("ao_statement"))
    docket = _clean(rec.get("docket_number"))
    received = _iso(rec.get("date_received"))
    who = _join([f"applicant {applicant}" if applicant else "", _location(rec)]).replace(" · ", ", ", 1)
    summary = _join([
        f"PMA {pma}" + ("" if supp else " (original approval)"),
        supplement,
        f"generic name {generic}" if generic and generic != device else "",
        _product(rec),
        _regulation(rec),
        _committee(rec),
        decision,
        who,
        f"received {received}" if received else "",
        f"docket {docket}" if docket else "",
        "expedited review" if _clean(rec.get("expedited_review_flag")).upper() == "Y" else "",
        f"AO statement: {ao}" if ao else "",
    ])
    return NewsItem(
        id=f"{ID_PREFIX}{number}",
        feed=FEED_PMA,
        title=f"{device} — {applicant}" if applicant else device,
        url=DB_PMA.format(number=number),
        published=decision_date,
        summary=summary[:1500],
    )


def fetch(
    http: Http,
    start: str,
    end: str,
    kinds: Sequence[str] = ("510k", "pma"),
    api_key: Optional[str] = None,
    advisory_committees: Sequence[str] = (),
    include_supplements: bool = True,
    exclude_supplement_types: Sequence[str] = (),
    page_size: int = DEFAULT_PAGE_SIZE,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> Iterator[NewsItem]:
    """Yield NewsItems for every 510(k) / De Novo / PMA decision dated in [start, end] (inclusive).

    kinds: any of '510k' (also yields the fda-denovo feed) and 'pma'.
    api_key: None -> read OPENFDA_API_KEY from the environment; '' -> anonymous.
    advisory_committees: keep only these codes (e.g. IVD_ADVISORY_COMMITTEES); empty = all. The record's
        advisory_committee is used, falling back to review_advisory_committee (De Novo rows leave the former blank).
    include_supplements / exclude_supplement_types: trim PMA supplement noise (30-day notices are ~half of PMA decisions).
    page_size: 250 by default (one response ~2-5 MB); the openFDA maximum is 1000.
    Callers should request a window that reaches back >= RECOMMENDED_LOOKBACK_DAYS: the datasets refresh monthly.
    """
    key = api_key_from_env() if api_key is None else api_key
    keep = {c.upper() for c in advisory_committees}
    excl = {s.strip().lower() for s in exclude_supplement_types}
    query = date_range_query(start, end)
    for kind in kinds:
        if kind not in ENDPOINTS:
            log.warning("openFDA: unknown kind %r (expected one of %s)", kind, sorted(ENDPOINTS))
            continue
        n = 0
        for rec in search(http, kind, query, api_key=key, page_size=page_size, max_records=max_records):
            if keep and _committee_code(rec).upper() not in keep:
                continue
            if kind == "pma":
                if _clean(rec.get("supplement_number")):
                    if not include_supplements or _clean(rec.get("supplement_type")).lower() in excl:
                        continue
                item = news_item_pma(rec)
            else:
                item = news_item_510k(rec)
            if item is None:
                continue
            n += 1
            yield item
        log.info("openFDA %s: %d items with decisions in %s..%s", kind, n, start, end)


def applicant_counts(
    http: Http,
    start: str,
    end: str,
    applicants: Iterable[str],
    kinds: Sequence[str] = ("510k", "pma"),
    api_key: Optional[str] = None,
) -> Dict[str, Dict[str, Optional[int]]]:
    """{kind: {applicant: total}} for decisions in the window whose applicant matches the phrase (one request each).

    total is 0 when openFDA answers 404 ("No matches found!") and None when the request itself failed.
    """
    key = api_key_from_env() if api_key is None else api_key
    names = list(applicants)  # a one-shot iterable must survive the per-kind loop
    out: Dict[str, Dict[str, Optional[int]]] = {}
    for kind in kinds:
        url = ENDPOINTS.get(kind)
        if not url:
            continue
        counts: Dict[str, Optional[int]] = {}
        for name in names:
            phrase = _clean(name).replace('"', "")
            query = date_range_query(start, end, f'applicant:"{phrase}"')
            status, data = _request(http, url, _params(query, 1, 0, key), f"{kind} applicant {phrase!r}")
            if status is None:
                log.warning("openFDA %s: applicant count for %r failed (network error, server error or host blocked)", kind, name)
                counts[name] = None
                continue
            total = 0
            if isinstance(data, dict):
                meta_total = ((data.get("meta") or {}).get("results") or {}).get("total")
                total = meta_total if isinstance(meta_total, int) else 0
            counts[name] = total
        out[kind] = counts
    return out
