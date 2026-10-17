"""NIH RePORTER v2 API: funded projects as NewsItems.

Three signals live here, all from the same endpoint:

* **S10 shared-instrumentation awards** ("Shared Instrumentation Grant", "High-End Instrumentation"):
  each one is a core facility buying a specific machine, so the titles are an installed-base signal
  for sequencers, mass spectrometers, cryo-EM and spatial platforms, dated by the award notice.
* **SBIR/STTR awards** (R41-R44, U43/U44, SB1): small companies' R&D, filterable by organisation name.
* **Keyword hits** in project titles/abstracts (an instrument name, a vendor, a technique) across any
  activity code; P30 cancer-centre shared resources, for example, list the instruments they run.

Verified live on 2026-10-17 (full notes in docs/sources/nih_reporter.md). Official documentation:
https://api.reporter.nih.gov/ (field list, paging rules and etiquette); machine-readable spec:
https://api.reporter.nih.gov/swagger/v2/swagger.json (OpenAPI 3.0.1).

POST https://api.reporter.nih.gov/v2/projects/search        JSON body, no key required
  {"criteria": {"award_notice_date": {"from_date": "2026-07-19", "to_date": "2026-10-17"},
                "activity_codes": ["S10"],
                "advanced_text_search": {"operator": "advanced",
                                         "search_field": "projecttitle,abstracttext",
                                         "search_text": "\\"NovaSeq X\\" OR Revio"},
                "org_names": ["..."]},                     # case-insensitive *substring* filter
   "include_fields": ["ApplId", "ProjectNum", "ProjectTitle", ...],   # PascalCase in, snake_case out
   "offset": 0, "limit": 500, "sort_field": "award_notice_date", "sort_order": "asc"}
  -> {"meta": {"search_id": "...", "total": 57, "offset": 0, "limit": 500, "sort_field": ...,
               "sorted_by_relevance": false, "properties": {"URL": "..."}},
      "results": [{"appl_id": 11365533, "project_num": "1S10GM164903-01",
                   "project_title": "2-laser CytoFLEX Flow Cytometer equipped with Plate Loader",
                   "award_notice_date": "2026-08-05T00:00:00", "project_start_date": "2026-09-01T00:00:00",
                   "activity_code": "S10", "award_amount": 232415, "fiscal_year": 2026,
                   "organization": {"org_name": "...", "org_city": "CARSON", "org_state": "CA",
                                    "org_country": "UNITED STATES", ...},
                   "agency_ic_admin": {"code": "GM", "abbreviation": "NIGMS", "name": "..."},
                   "project_detail_url": "https://reporter.nih.gov/project-details/11365533",
                   "abstract_text": "Flow cytometry is ...", ...}]}

Key spelling (verified): the Swagger spec writes camelCase (awardNoticeDate, activityCodes, includeFields,
sortField) but the live API SILENTLY IGNORES camelCase keys -- a camelCase body returned every project in
RePORTER (total 2,983,350, sort_field null, full 45-key records) instead of an error. Only the snake_case
spelling from the documentation page filters; this module builds every body itself so callers cannot
misspell a key.

Paging (all verified):
  * limit > 500 -> HTTP 400 "System doesn't support limit value greater than 500".
  * offset > 14,999 -> HTTP 400 "Maximum offset is 14,999" (split the date window for bigger pulls).
  * offset >= meta.total -> HTTP 400 "Requested offset N exceeded total records count of M" -- NOT an
    empty page, so the loop below stops at meta.total instead of probing past the end.
  * limit = 0 -> meta only (a free count).
  * With a text search meta says sorted_by_relevance: true, yet rows still arrive in sort_field order
    (705 rows over two pages, union == total). The order *within* one notice date is not stable between
    requests, so search() advances by date (keyset paging) instead of trusting offsets; see its docstring.
    criteria.use_relevance = false (in the Swagger spec) has no observable effect.
  * Errors arrive either as {"message": "..."} or as a JSON *list* of strings; both are treated as no data.
  * Unknown include_fields are silently ignored; dates may carry a time component.
Rate limit: the docs ask for no more than one request per second (large jobs at night or weekends);
response headers show a 200-per-minute window (X-Rate-Limit-Limit: 1m, X-Rate-Limit-Remaining).
DEFAULT_MIN_INTERVAL["api.reporter.nih.gov"] = 1.0 in litradar/http.py enforces the published etiquette,
and Http.post_json retries transient failures (429/5xx/network) with backoff like Http.get.
Text search: operators "and" / "or" split search_text on whitespace (so "NovaSeq X" becomes two words);
operator "advanced" honours quoted phrases and AND/OR/NOT, which is what this module uses.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..http import Http
from ..models import NewsItem

log = logging.getLogger(__name__)

API_URL = "https://api.reporter.nih.gov/v2/projects/search"
DOCS_URL = "https://api.reporter.nih.gov/"
PROJECT_URL = "https://reporter.nih.gov/project-details/{appl_id}"
FEED = "nih-reporter"

MAX_LIMIT = 500  # API hard cap per page
MAX_OFFSET = 14999  # API hard cap on offset; beyond this, narrow the date window
SUMMARY_ABSTRACT_CHARS = 1500
DEFAULT_SEARCH_FIELDS = "projecttitle,abstracttext"  # also accepts "terms"
DEFAULT_TERMS_PER_QUERY = 25  # phrases OR-ed into one advanced_text_search

S10_CODES = ("S10",)
# STTR phase I/II, SBIR phase I/II, SBIR cooperative agreements, Commercialization Readiness Pilot.
SBIR_STTR_CODES = ("R41", "R42", "R43", "R44", "U43", "U44", "SB1")

INCLUDE_FIELDS = (
    "ApplId", "ProjectNum", "ProjectTitle", "AbstractText", "ActivityCode", "AwardAmount",
    "AwardNoticeDate", "ProjectStartDate", "ProjectEndDate", "BudgetStart", "FiscalYear",
    "Organization", "AgencyIcAdmin", "ProjectDetailUrl", "IsNew", "AwardType", "OpportunityNumber",
)


# -- small helpers --------------------------------------------------------------------------
def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _date(s: Any) -> str:
    """'2026-08-05T00:00:00' -> '2026-08-05'; anything else -> ''."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(s or ""))
    return m.group(1) if m else ""


def _amount(v: Any) -> Optional[int]:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def date_range(start: str, end: str) -> Dict[str, str]:
    return {"from_date": start, "to_date": end}


def _window(criteria: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """The criteria's award_notice_date window as (from, to) YYYY-MM-DD strings, or None if absent/odd."""
    w = criteria.get("award_notice_date")
    if not isinstance(w, dict):
        return None
    lo, hi = _date(w.get("from_date")), _date(w.get("to_date"))
    return (lo, hi) if lo and hi and lo <= hi else None


def _strings(values: Optional[Iterable[Any]]) -> List[str]:
    """Clean a list of strings, dropping blanks (order kept)."""
    return [_clean(v) for v in (values or []) if _clean(v)]


def _terms(terms: Optional[Iterable[str]]) -> List[str]:
    """Clean, strip embedded quotes and de-duplicate (order kept) so a repeated term never costs a request."""
    out: List[str] = []
    for t in terms or []:
        t = _clean(t).replace('"', "")
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out


def text_search(terms: Iterable[str], search_fields: str = DEFAULT_SEARCH_FIELDS) -> Optional[Dict[str, str]]:
    """advanced_text_search criterion: each term as a quoted phrase, OR-ed (phrases need operator 'advanced')."""
    phrases = [f'"{t}"' for t in _terms(terms)]
    if not phrases:
        return None
    return {"operator": "advanced", "search_field": search_fields, "search_text": " OR ".join(phrases)}


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _fail(errors: Optional[List[str]], msg: str) -> None:
    """Record an API failure: logged at ERROR and, when the caller passed a list, appended to it."""
    log.error(msg)
    if errors is not None:
        errors.append(msg)


# -- raw search -----------------------------------------------------------------------------
def _page(http: Http, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = http.post_json(API_URL, body, headers={"Accept": "application/json"})
    if not isinstance(data, dict):
        # None (HTTP error / network), or the API's list-of-strings validation error
        return None
    return data


def count(http: Http, criteria: Dict[str, Any]) -> int:
    """meta.total for a criteria object without pulling rows (limit=0 is accepted by the API)."""
    data = _page(http, {"criteria": criteria, "include_fields": ["ApplId"], "offset": 0, "limit": 0})
    meta = (data or {}).get("meta")
    try:
        return int((meta or {}).get("total") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def search(
    http: Http,
    criteria: Dict[str, Any],
    limit: int = MAX_LIMIT,
    max_records: int = 5000,
    include_fields: Sequence[str] = INCLUDE_FIELDS,
    sort_field: str = "award_notice_date",
    sort_order: str = "asc",
    errors: Optional[List[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield raw project rows for `criteria`, de-duplicated on appl_id, stopping at meta.total.

    Paging. The API pages by offset/limit (max 500). Rows come back in `sort_field` order even when a
    text search makes meta say sorted_by_relevance, but the order *within* one notice date changes
    between requests, so a plain offset walk can drop or repeat rows that straddle a page boundary.
    When the criteria carry an award_notice_date window and the sort is by award_notice_date, this
    function therefore advances by date ("keyset" paging): the next request narrows the window to start
    (or, descending, end) at the last date seen, offset 0, and rows already yielded are skipped. It falls
    back to offset paging, with a warning, only when a whole page shares one date (more than `limit`
    notices on a single day) or a page is not in date order.

    Never raises on API trouble: a missing or malformed page ends the iteration with a message logged at
    ERROR and appended to `errors` (when a list is given); rows already yielded are kept. An incomplete
    pull (fewer unique rows than the first page's meta.total) is logged as a warning.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    asc = str(sort_order).lower() != "desc"
    window = _window(criteria) if sort_field == "award_notice_date" else None
    crit: Dict[str, Any] = dict(criteria)
    offset = 0
    yielded = 0
    first_total: Optional[int] = None
    failed = False
    seen: set = set()
    while yielded < max_records:
        if offset > MAX_OFFSET:
            log.warning("NIH RePORTER: offset %d exceeds the API cap of %d; narrow the date window", offset, MAX_OFFSET)
            break
        body: Dict[str, Any] = {
            "criteria": crit,
            "include_fields": list(include_fields),
            "offset": offset,
            "limit": min(limit, max_records - yielded),
            "sort_field": sort_field,
            "sort_order": sort_order,
        }
        data = _page(http, body)
        if data is None:
            _fail(errors, f"NIH RePORTER: no usable response at offset {offset} (window {crit.get('award_notice_date')})")
            failed = True
            break
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        results = data.get("results")
        if not isinstance(results, list):
            _fail(errors, f"NIH RePORTER: response without a results list at offset {offset}")
            failed = True
            break
        try:
            total = int(meta.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if first_total is None:
            first_total = total
        if not results:
            break
        for row in results:
            if not isinstance(row, dict):
                continue
            appl_id = row.get("appl_id")
            if appl_id is not None:
                if appl_id in seen:
                    continue
                seen.add(appl_id)
            yield row
            yielded += 1
            if yielded >= max_records:
                break
        if yielded >= max_records or offset + len(results) >= total:
            break
        if window:
            dates = [_date(r.get("award_notice_date")) if isinstance(r, dict) else "" for r in results]
            ordered = all(dates) and dates == sorted(dates, reverse=not asc)
            edge = dates[-1] if ordered else ""
            lo, hi = window
            if edge and (edge > lo if asc else edge < hi):
                window = (edge, hi) if asc else (lo, edge)
                crit = dict(crit, award_notice_date=date_range(*window))
                offset = 0
                continue
            why = f"more than {len(results)} notices dated {edge}" if ordered else "page not in date order"
            log.warning("NIH RePORTER: %s; paging by offset within %s..%s (ties at the page edge may be missed)", why, lo, hi)
        offset += len(results)
    if yielded >= max_records:
        log.info("NIH RePORTER: stopped at max_records=%d", max_records)
    elif not failed and first_total is not None and yielded < first_total:
        log.warning("NIH RePORTER: pulled %d unique rows of %d reported", yielded, first_total)


# -- normalisation --------------------------------------------------------------------------
def to_news_item(row: Dict[str, Any], feed: str = FEED, decorate_title: bool = False) -> Optional[NewsItem]:
    """One project row -> NewsItem; None when the row lacks an appl_id or a title."""
    appl_id = row.get("appl_id")
    title = _clean(row.get("project_title"))
    if appl_id in (None, "") or not title:
        return None
    org = row.get("organization") if isinstance(row.get("organization"), dict) else {}
    ic = row.get("agency_ic_admin") if isinstance(row.get("agency_ic_admin"), dict) else {}
    org_name = _clean(org.get("org_name"))
    place = ", ".join(_clean(org.get(k)) for k in ("org_city", "org_state") if _clean(org.get(k)))
    org_label = f"{org_name} ({place})" if org_name and place else org_name or place
    code = _clean(row.get("activity_code"))
    amount = _amount(row.get("award_amount"))
    amount_s = f"${amount:,}" if amount is not None else ""
    fy = _clean(row.get("fiscal_year"))
    meta_parts = [p for p in (code, _clean(row.get("project_num")), org_label, amount_s, _clean(ic.get("abbreviation")), f"FY{fy}" if fy else "") if p]
    abstract = _clean(row.get("abstract_text"))[:SUMMARY_ABSTRACT_CHARS]
    summary = abstract + ("\n" if abstract and meta_parts else "") + " | ".join(meta_parts)
    if decorate_title:
        tail = ", ".join(p for p in (code, amount_s) if p)
        title = f"{title} — {org_name}" if org_name else title
        title = f"{title} ({tail})" if tail else title
    return NewsItem(
        id=f"{FEED}:{appl_id}",
        feed=feed,
        title=title,
        url=_clean(row.get("project_detail_url")) or PROJECT_URL.format(appl_id=appl_id),
        published=_date(row.get("award_notice_date")) or _date(row.get("project_start_date")) or _date(row.get("budget_start")),
        summary=summary,
    )


def _items(
    http: Http, criteria: Dict[str, Any], max_records: int, decorate_titles: bool, seen: set, errors: Optional[List[str]]
) -> Iterator[NewsItem]:
    for row in search(http, criteria, max_records=max_records, errors=errors):
        item = to_news_item(row, decorate_title=decorate_titles)
        if item is None or item.id in seen:
            continue
        seen.add(item.id)
        yield item


# -- entry points ---------------------------------------------------------------------------
def s10_awards(
    http: Http,
    start: str,
    end: str,
    terms: Optional[Sequence[str]] = None,
    max_records: int = 5000,
    decorate_titles: bool = False,
    errors: Optional[List[str]] = None,
) -> Iterator[NewsItem]:
    """S10 shared-instrumentation awards with an award notice date in [start, end] (YYYY-MM-DD).

    `terms` optionally narrows to titles/abstracts naming any of the phrases (an instrument, a vendor).
    Without terms every S10 in the window is returned (~60 per quarter), which is what the digest wants:
    the instrument gazetteer is then run over the titles downstream. API failures are appended to
    `errors` (when given) instead of raised.
    """
    criteria: Dict[str, Any] = {"award_notice_date": date_range(start, end), "activity_codes": list(S10_CODES)}
    seen: set = set()
    terms = _terms(terms)
    if terms:
        for chunk in _chunks(terms, DEFAULT_TERMS_PER_QUERY):
            ts = text_search(chunk)
            if ts:
                yield from _items(http, dict(criteria, advanced_text_search=ts), max_records, decorate_titles, seen, errors)
        return
    yield from _items(http, criteria, max_records, decorate_titles, seen, errors)


def keyword_awards(
    http: Http,
    start: str,
    end: str,
    terms: Sequence[str],
    activity_codes: Optional[Sequence[str]] = None,
    org_names: Optional[Sequence[str]] = None,
    exact_org_names: bool = False,
    search_fields: str = DEFAULT_SEARCH_FIELDS,
    terms_per_query: int = DEFAULT_TERMS_PER_QUERY,
    max_records: int = 5000,
    decorate_titles: bool = False,
    errors: Optional[List[str]] = None,
) -> Iterator[NewsItem]:
    """Awards noticed in [start, end] whose title/abstract names any of `terms` (quoted phrases, OR-ed).

    `activity_codes` restricts the mechanism (e.g. SBIR_STTR_CODES); `org_names` restricts the awardee
    (substring match unless exact_org_names=True, which uses the API's org_names_exact_match).
    At least one of terms / activity_codes / org_names must be non-blank after cleaning, otherwise the
    query would be "every NIH award in the window" and a ValueError is raised.
    Long term lists are split into batches of `terms_per_query` phrases; results are de-duplicated.
    API failures are appended to `errors` (when given) instead of raised.
    """
    terms = _terms(terms)
    codes = _strings(activity_codes)
    orgs = _strings(org_names)
    if not terms and not codes and not orgs:
        raise ValueError("keyword_awards needs terms, activity_codes or org_names")
    base: Dict[str, Any] = {"award_notice_date": date_range(start, end)}
    if codes:
        base["activity_codes"] = codes
    if orgs:
        base["org_names_exact_match" if exact_org_names else "org_names"] = orgs
    seen: set = set()
    if not terms:
        yield from _items(http, base, max_records, decorate_titles, seen, errors)
        return
    for chunk in _chunks(terms, terms_per_query):
        ts = text_search(chunk, search_fields)
        if ts:
            yield from _items(http, dict(base, advanced_text_search=ts), max_records, decorate_titles, seen, errors)


def sbir_awards(
    http: Http,
    start: str,
    end: str,
    org_names: Optional[Sequence[str]] = None,
    terms: Optional[Sequence[str]] = None,
    activity_codes: Sequence[str] = SBIR_STTR_CODES,
    exact_org_names: bool = False,
    max_records: int = 5000,
    decorate_titles: bool = False,
    errors: Optional[List[str]] = None,
) -> Iterator[NewsItem]:
    """SBIR/STTR awards (R41-R44, U43/U44, SB1 by default) noticed in [start, end], optionally by company or keyword."""
    yield from keyword_awards(
        http, start, end, list(terms or []), activity_codes=activity_codes, org_names=org_names,
        exact_org_names=exact_org_names, max_records=max_records, decorate_titles=decorate_titles, errors=errors,
    )
