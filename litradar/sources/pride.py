"""PRIDE Archive REST API v3: proteomics datasets made public in a window, with the mass spectrometers declared.

PRIDE (the ProteomeXchange node at EMBL-EBI) is to mass-spectrometry proteomics what SRA/ENA are to
sequencing: every public dataset (PXD accession) declares its instruments as PSI-MS controlled-vocabulary
terms ("Orbitrap Astral", "timsTOF Pro 2", "Q Exactive HF"), its experiment types ("Bottom-up proteomics",
"Data-independent acquisition"), organisms and a publication date. One record is emitted per
(project, declared instrument), so the weekly census counts datasets per instrument model.

Verified live on 2026-10-17 (follow the API, not memory):

  Documentation : https://www.ebi.ac.uk/pride/markdownpage/pridearchiverestapi
  OpenAPI spec  : https://www.ebi.ac.uk/pride/ws/archive/v3/v3/api-docs
  Swagger UI    : https://www.ebi.ac.uk/pride/ws/archive/v3/webjars/swagger-ui/index.html

  GET https://www.ebi.ac.uk/pride/ws/archive/v3/search/projects
      ?filter=publicationDate==YYYY-MM-DD      one calendar day; "field==value" pairs, comma = AND
      &pageSize=100&page=N                     pageSize is capped at 100 server-side (200/500/1000 all return 100)
      &sortFields=publicationDate&sortDirection=DESC
  -> JSON array of ElasticPrideProject; response header `total_records: N` (N = hits for the filter).
     Fields used: accession "PXD085732", title, publicationDate "2026-10-16", submissionDate,
     instruments ["Orbitrap Astral", "timsTOF HT", ...] (plain CV-term names), experimentTypes
     ["Data-independent acquisition"], organisms ["Homo sapiens (human)"], affiliations [...],
     submissionType "COMPLETE" | "PARTIAL" | "AFFINITY".
  -> Date RANGE filters are NOT supported: `publicationDate==[a TO b]` returns 0 hits and
     `publicationDate=range[a TO b]` / `publication_date==[a TO b]` are ignored (all 42,133 projects).
     Hence one request per day; a 7-day window is 7 requests, a 31-day backfill 31 (plus a second page
     on any day with more than 100 releases, never observed in the test month: max 53/day).
     Sorting by publicationDate DESC and paging until the date drops below the window also works
     (6 pages for the test month) but must walk every newer project first, so it is not used for backfills.
  GET https://www.ebi.ac.uk/pride/ws/archive/v3/facet/projects?filter=publicationDate==YYYY-MM-DD&facetPageSize=100
  -> {"instruments": {"Orbitrap Exploris 480": 9, ...}, "experimentTypes": {...}, "organisms": {...}, ...}
     a ready-made per-day histogram (used by `instrument_counts`, no project records fetched).
  GET https://www.ebi.ac.uk/pride/ws/archive/v3/projects/{accession}
  -> full record; here CV terms are objects {"accession": "MS:1001910", "name": "LTQ Orbitrap Elite"},
     which `_names` also accepts.

Live test 2026-09-17..2026-10-17: 513 projects, 597 instrument declarations, 67 distinct instrument names;
top: Orbitrap Exploris 480 (81), Orbitrap Fusion Lumos (63), Q Exactive HF (61), Q Exactive (41),
Orbitrap Astral (34), timsTOF Pro (31), timsTOF Pro 2 (31), Orbitrap Eclipse (29).

Etiquette: PRIDE publishes no numeric rate limit; the shared Http client already paces www.ebi.ac.uk
(the host it shares with the ENA portal) and retries with backoff. A weekly run is ~7 requests.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, timedelta
from typing import Any, Dict, Iterable, Iterator, List

from ..http import Http
from ..models import ArchiveRecord

log = logging.getLogger(__name__)

BASE = "https://www.ebi.ac.uk/pride/ws/archive/v3"
SEARCH = f"{BASE}/search/projects"
FACET = f"{BASE}/facet/projects"
DOCS_URL = "https://www.ebi.ac.uk/pride/markdownpage/pridearchiverestapi"
PAGE_SIZE = 100  # server-side maximum; larger values are silently reduced to 100
MAX_PAGES_PER_DAY = 50  # safety valve against a server that keeps returning full pages
FACET_PAGE_SIZE = 100  # distinct values per facet per request (67 distinct instruments in a whole month)


def _days(start: str, end: str) -> List[str]:
    """Inclusive list of YYYY-MM-DD strings; a malformed or reversed window gives an empty list."""
    try:
        a, b = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    except ValueError:
        log.warning("PRIDE: bad window %r..%r", start, end)
        return []
    if b < a:
        return []
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def _names(values: Any) -> List[str]:
    """CV terms are plain strings in /search/projects and {'name': ...} objects in /projects/{acc}.

    Returns the distinct, stripped names in their original order; anything else is skipped.
    """
    out: List[str] = []
    if not isinstance(values, list):
        return out
    for v in values:
        if isinstance(v, dict):
            v = v.get("name") or v.get("value") or ""
        if isinstance(v, str):
            v = v.strip()
            if v and v not in out:
                out.append(v)
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"


def day_filter(day: str) -> str:
    return f"publicationDate=={day}"


def search_day(http: Http, day: str, page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES_PER_DAY) -> Iterator[Dict[str, Any]]:
    """Yield every project made public on `day` (YYYY-MM-DD), paging until a short or empty page.

    Stops on: a short page (fewer than `page_size` items), an empty page, a non-list payload, a failed
    request, a page that only repeats accessions already seen, or `max_pages`.
    """
    seen: set = set()
    for page in range(max_pages):
        params: Dict[str, Any] = {
            "filter": day_filter(day),
            "pageSize": page_size,
            "page": page,
            "sortFields": "publicationDate",
            "sortDirection": "DESC",
        }
        data = http.get_json(SEARCH, params=params)
        if data is None:
            log.warning("PRIDE: no response for %s page %d", day, page)
            return
        if not isinstance(data, list):
            log.warning("PRIDE: unexpected payload for %s page %d (%s)", day, page, type(data).__name__)
            return
        fresh = 0
        for proj in data:
            if not isinstance(proj, dict):
                continue
            acc = str(proj.get("accession") or "").strip()
            if acc:
                if acc in seen:
                    continue
                seen.add(acc)
            fresh += 1
            yield proj
        if len(data) < page_size or fresh == 0:
            return
    log.warning("PRIDE: gave up after %d pages for %s", max_pages, day)


def to_records(proj: Dict[str, Any]) -> List[ArchiveRecord]:
    """One ArchiveRecord per declared instrument (one blank-instrument record when none is declared).

    accession        = "<PXD>:<instrument slug>" so several instruments on one dataset stay distinct rows
    study_accession  = the PXD accession
    instrument_model = CV-term name as submitted ("Orbitrap Astral"); the pipeline maps it onto the gazetteer
    platform         = "MS", or "AFFINITY" for Olink-style affinity submissions (submissionType AFFINITY)
    library_strategy = experiment types joined with "; "
    first_public     = publicationDate (YYYY-MM-DD)
    """
    if not isinstance(proj, dict):
        return []
    acc = str(proj.get("accession") or "").strip()
    if not acc:
        return []
    instruments = _names(proj.get("instruments"))
    affiliations = _names(proj.get("affiliations"))
    submission_type = str(proj.get("submissionType") or "").strip().upper()
    common: Dict[str, Any] = dict(
        archive="pride",
        study_accession=acc,
        platform="AFFINITY" if submission_type == "AFFINITY" else "MS",
        library_strategy="; ".join(_names(proj.get("experimentTypes"))),
        first_public=str(proj.get("publicationDate") or "")[:10],
        center_name=affiliations[0][:300] if affiliations else "",
        study_title=str(proj.get("title") or "").strip(),
        organism="; ".join(_names(proj.get("organisms"))),
    )
    if not instruments:
        return [ArchiveRecord(accession=acc, instrument_model="", **common)]
    out: List[ArchiveRecord] = []
    used: set = set()
    for name in instruments:
        slug = _slug(name)
        while slug in used:  # two spellings with the same slug ("timsTOF" / "TimsTOF") still get distinct rows
            slug += "-2"
        used.add(slug)
        out.append(ArchiveRecord(accession=f"{acc}:{slug}", instrument_model=name, **common))
    return out


def fetch(http: Http, start: str, end: str, max_projects: int = 0) -> Iterator[ArchiveRecord]:
    """Yield one record per (project, instrument) for datasets made public in [start, end] (inclusive).

    One request per calendar day (the API has no date-range filter), so the cost is the window length
    in days, independent of how far back the window lies. max_projects=0 means no cap.
    """
    n_proj = n_rec = 0
    for day in _days(start, end):
        for proj in search_day(http, day):
            recs = to_records(proj)
            if not recs:
                continue
            n_proj += 1
            n_rec += len(recs)
            yield from recs
            if max_projects and n_proj >= max_projects:
                log.warning("PRIDE: stopping at %d projects (max_projects cap) on %s", n_proj, day)
                return
    log.info("PRIDE: %d projects / %d instrument records public %s..%s", n_proj, n_rec, start, end)


def instrument_counts(http: Http, start: str, end: str, facet: str = "instruments") -> Dict[str, int]:
    """Per-day facet histogram summed over the window: {"Orbitrap Exploris 480": 81, ...}.

    One request per day via /facet/projects and no project records; `facet` may also be
    "experimentTypes", "organisms" or "submissionType". Counts are datasets, not instruments.
    """
    out: Dict[str, int] = {}
    for day in _days(start, end):
        data = http.get_json(FACET, params={"filter": day_filter(day), "facetPageSize": FACET_PAGE_SIZE, "facetPage": 0})
        if not isinstance(data, dict):
            continue
        bucket = data.get(facet)
        if not isinstance(bucket, dict):
            continue
        for key, val in bucket.items():
            try:
                out[str(key)] = out.get(str(key), 0) + int(val)
            except (TypeError, ValueError):
                continue
    return out


def histogram(records: Iterable[ArchiveRecord]) -> Dict[str, int]:
    """Datasets per instrument_model, most common first (blank model = no instrument declared)."""
    c: Counter = Counter(r.instrument_model for r in records)
    return dict(c.most_common())
