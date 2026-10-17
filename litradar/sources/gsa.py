"""GSA, the Genome Sequence Archive at CNCB-NGDC (https://ngdc.cncb.ac.cn/gsa/): the Chinese
counterpart of SRA/ENA. Native records (CRA study / CRX experiment / CRR run) are NOT mirrored into
INSDC, so without this adapter a sequencing census misses most Chinese submissions.

Verified live on 2026-10-17 (no formal API documentation exists; the handbook is at
https://ngdc.cncb.ac.cn/gsa/support/documents). Three routes, all anonymous:

1. Search page (Elasticsearch-backed, server-rendered HTML)
   GET https://ngdc.cncb.ac.cn/gsa/search
       searchTerm='"MM/DD/YYYY - MM/DD/YYYY"[ReleaseDate]'   (the advanced-search syntax; one day = same date twice)
       pageSize=N  from=<1-based page number>                  (pageSize up to at least 3,000 is honoured)
       centerSelectedItem=NGDC  centerSelected=true           (facet: NGDC = native GSA; INSDC = mirrored SRA/ENA/DDBJ)
       <facet>SelectedItem=<value>  <facet>Selected=true      (facet in platform|strategy|source|selection|fileType; combinable)
   Response: "Total Items:&nbsp;N"; one hit per <input class="checkbox_class" data-value="CRX…">, then
   fields "Title", "Accession" (link browse/<CRA>/<CRX>), "Platform name" (FAMILY only: ILLUMINA, BGISEQ,
   OXFORD_NANOPORE, PACBIO_SMRT, GENEMIND, SALUS, ELEMENT, ULTIMA, ION_TORRENT, CAPILLARY, CapitalBio …),
   "Source name", "Strategy name", "Selection name". Seven sidebar facet blocks, in page order
   center_block, downloadable_block, fileType_block, platform_block, source_block, strategy_block,
   selection_block, carry counts for the WHOLE result set, so one request gives a platform histogram.
   Sample digits: 2026-09-17..10-17 NGDC = 33,480 experiments (ILLUMINA 21,891 / BGISEQ 11,014 /
   PACBIO_SMRT 451 / OXFORD_NANOPORE 86 / GENEMIND 11); a 200-hit page is ~900 KB and 4-9 s.
   PAGINATION IS UNSTABLE: there is no sort parameter and the Elasticsearch order changes between
   requests (the same page-1 request twice shared 156 of 200 accessions; 5 pages x 200 for an 813-hit day
   gave 739 distinct). Paging through a day therefore loses 9-27% of it. Verified fix: one page sized to
   the result set is complete (pageSize=1000 -> 813/813 distinct, 3.5 MB, 20.5 s; pageSize=3000 ->
   2,593/2,593 distinct, 11 MB, 53.5 s; ~20-25 ms per hit of server time). `fetch()` probes each day
   (page_size hits + facet counts), then fetches the day as ONE page sized to its total, and when the
   total exceeds `max_page_size` it partitions the day by facet (platform, then strategy, source,
   selection, file type), each partition again as one sized page. Days that still exceed the cap after
   every facet are truncated and logged ("fetched n of N").
   Page shapes (all HTTP 200): a results page carries "Total Items:&nbsp;N"; a day with no releases
   renders "No items found" (27.8 KB, no total, no facet blocks); the nginx JavaScript challenge is a
   1.3 KB page titled "正在进行安全检查". `check_search_page()` accepts the first two and raises on anything
   else, so a markup change or a challenge on /gsa/search surfaces as an error instead of a silent zero.

2. Statistics JSON (cumulative snapshot, ~12 MB)
   GET https://ngdc.cncb.ac.cn/gsa/statistics/statistics.json
   platforms.platformList = [{platforms: "DNBSEQ-T7", experiments: 112846, runs: 119461, platformId: 64}, …] (80 models)
   total.mouthArchiveList = [["2026-10", 731], …]  (CRA studies created per month)
   total.{archiveCount, expCount, runCount, releasedExp, privateExp}; organisms[]; sampleType[];
   agency.agencyList[]; organization.{organizationList[], countrySummaryList[]}; article.publications[]
   ({doi, pubmedId, craAccession (HTML), journal, pDate}). This is the ONLY place the instrument MODEL
   appears without a browser; weekly snapshots diffed over time give the per-model flow.

3. Not usable without a browser: /gsa/browse/** (nginx JavaScript cookie challenge; not circumvented),
   and the RunInfo CSV export (/gsa/search/getRunInfo), which ignores the ReleaseDate term and caps at
   ~10k rows. GSA-Human (HRA/HRX, controlled-access human data) is a separate site: see gsa_human.py.

Etiquette: robots.txt only disallows /openlb/. This module paces ngdc.cncb.ac.cn at one request per
second, requests at most `max_page_size` hits per page (default 1,500, ~6.5 MB) and caps a window sweep
with `max_records` (default 20,000 experiments). Verified cost 2026-10-17: two days (2,593 + 813
experiments) = 11 requests, 110.7 s, every record present.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any, Dict, Iterator, List, Optional

from ..models import ArchiveRecord

log = logging.getLogger(__name__)

HOST = "ngdc.cncb.ac.cn"
SEARCH_URL = f"https://{HOST}/gsa/search"
STATS_URL = f"https://{HOST}/gsa/statistics/statistics.json"
BROWSE_URL = f"https://{HOST}/gsa/browse/"
DEFAULT_MIN_INTERVAL = 1.0
PAGE_SIZE = 200  # probe size: a day with at most this many releases is complete in one request
MAX_PAGE_SIZE = 1500  # largest single page requested (~6.5 MB, ~30-40 s of server time, inside the client's 60 s read timeout)
# facet dimensions used to split a day that is too big for one page, in order; value = the request-parameter stem
PARTITION_DIMS = [("platform", "platform"), ("strategy", "strategy"), ("source", "source"), ("selection", "selection"), ("file_type", "fileType")]

_TOTAL_RE = re.compile(r"Total Items:&nbsp;\s*([\d,]+)")
_HIT_SPLIT_RE = re.compile(r'<input[^>]*class="checkbox_class"[^>]*data-value="([A-Za-z]+\d+)"')
_FIELD_RE = re.compile(
    r'<span class="result_title">\s*([^<:]+?)\s*:?\s*</span>\s*'
    r'(?:<span class="result_context">\s*([^<]*?)\s*</span>|<a href="([^"]*)"[^>]*>\s*([^<]*?)\s*</a>)',
    re.S,
)
_LINK_RE = re.compile(r"browse/([A-Za-z]+\d+)/([A-Za-z]+\d+)")
_FACET_BLOCK_RE = re.compile(r'class="(center|downloadable|fileType|platform|source|strategy|selection)_block"')
_FACET_KEYS = {"fileType": "file_type"}  # CSS class -> facet key; the others are used as-is
_RESULTS_MARKER = "Total Items"
_EMPTY_MARKER = "No items found"
_CHALLENGE_MARKER = "安全检查"
_FACET_PAIR_RE = re.compile(r">\s*([^<>()]+?)\s*(?:<[^<>]*>\s*){1,4}\((\d[\d,]*)\)")

# GSA family labels -> the ENA/SRA `instrument_platform` spelling, so the census tables line up
FAMILY_TO_PLATFORM = {
    "ILLUMINA": "ILLUMINA",
    "BGISEQ": "BGISEQ",
    "OXFORD_NANOPORE": "OXFORD_NANOPORE",
    "PACBIO_SMRT": "PACBIO_SMRT",
    "ION_TORRENT": "ION_TORRENT",
    "GENEMIND": "GENEMIND",
    "SALUS": "SALUS",
    "ELEMENT": "ELEMENT",
    "ULTIMA": "ULTIMA",
    "CAPILLARY": "CAPILLARY",
    "COMPLETE_GENOMICS": "COMPLETE_GENOMICS",
    "ABI_SOLID": "ABI_SOLID",
    "LS454": "LS454",
    "CapitalBio": "CAPITALBIO",
    "BioNano": "BIONANO",
}


def _pace(http: Any) -> None:
    """Register a polite per-host interval on the shared client (no-op for fakes)."""
    mi = getattr(http, "min_interval", None)
    if isinstance(mi, dict):
        mi.setdefault(HOST, DEFAULT_MIN_INTERVAL)


def release_term(start: str, end: str) -> str:
    """The advanced-search ReleaseDate term for an inclusive YYYY-MM-DD window."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return f'"{s:%m/%d/%Y} - {e:%m/%d/%Y}"[ReleaseDate]'


def model_label(family: str) -> str:
    """GSA search hits expose the platform FAMILY only; label the model column honestly."""
    fam = (family or "").strip() or "unspecified"
    return f"{fam} (model n/a)"


# ---------------------------------------------------------------- search page parsing
def parse_total(html: str) -> int:
    m = _TOTAL_RE.search(html or "")
    return int(m.group(1).replace(",", "")) if m else 0


def parse_hits(html: str) -> List[Dict[str, str]]:
    """One dict per hit: accession, study_accession, title, platform, source, strategy, selection."""
    out: List[Dict[str, str]] = []
    if not html:
        return out
    marks = list(_HIT_SPLIT_RE.finditer(html))
    for i, m in enumerate(marks):
        seg = html[m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(html)]
        rec: Dict[str, str] = {"accession": m.group(1), "study_accession": "", "title": "", "platform": "", "source": "", "strategy": "", "selection": ""}
        for f in _FIELD_RE.finditer(seg):
            label = f.group(1).strip().lower()
            value = (f.group(2) if f.group(2) is not None else f.group(4) or "").strip()
            href = f.group(3) or ""
            if label.startswith("title"):
                rec["title"] = value
            elif label.startswith("accession"):
                lk = _LINK_RE.search(href)
                if lk:
                    rec["study_accession"], rec["accession"] = lk.group(1), lk.group(2)
                elif value:
                    rec["accession"] = value
            elif label.startswith("platform"):
                rec["platform"] = value
            elif label.startswith("source"):
                rec["source"] = value
            elif label.startswith("strategy"):
                rec["strategy"] = value
            elif label.startswith("selection"):
                rec["selection"] = value
        out.append(rec)
    return out


def parse_facets(html: str) -> Dict[str, Dict[str, int]]:
    """Sidebar facet counts, one key per block: platform, strategy, source, selection, center, downloadable,
    file_type -> {'ILLUMINA': 21891, …}. Every block the live page renders is recognised, so a block's
    segment ends at the next block and selection values never leak into `strategy`."""
    out: Dict[str, Dict[str, int]] = {}
    if not html:
        return out
    marks = list(_FACET_BLOCK_RE.finditer(html))
    for i, m in enumerate(marks):
        name = _FACET_KEYS.get(m.group(1), m.group(1))
        seg = html[m.end() : marks[i + 1].start() if i + 1 < len(marks) else m.end() + 20000]
        counts: Dict[str, int] = {}
        for p in _FACET_PAIR_RE.finditer(seg):
            label = re.sub(r"\s+", " ", p.group(1)).strip()
            if not label or label.lower() in ("platform", "strategy", "source", "center", "selection", "file type"):
                continue  # a block heading, never a value
            counts[label] = counts.get(label, 0) + int(p.group(2).replace(",", ""))
        if counts:
            out[name] = counts
    return out


def check_search_page(html: str, context: str) -> bool:
    """True for a results page, False for the genuine "No items found" page; raise for anything else.

    The adapter rides on server-rendered HTML with no API contract, so a renamed class or the site's
    JavaScript cookie challenge would otherwise parse as zero hits and be stored as a normal result.
    """
    if _RESULTS_MARKER in html:
        return True
    if _EMPTY_MARKER in html:
        return False
    why = "JavaScript challenge page" if _CHALLENGE_MARKER in html else "markup change or error page"
    raise RuntimeError(f"GSA: unexpected search page for {context} ({why}; {len(html)} bytes, no 'Total Items' / 'No items found')")


# ---------------------------------------------------------------- fetchers
def _search(http: Any, start: str, end: str, page: int, page_size: int, center: str, filters: Optional[Dict[str, str]] = None) -> Optional[str]:
    """One search request. `filters` = {facet key: value} using the keys of `parse_facets()` (platform, strategy, …)."""
    params: Dict[str, Any] = {"searchTerm": release_term(start, end), "pageSize": page_size, "from": page}
    if center:
        params["centerSelectedItem"] = center
        params["centerSelected"] = "true"
    stems = dict(PARTITION_DIMS)
    for key, value in (filters or {}).items():
        stem = stems.get(key, key)
        params[f"{stem}SelectedItem"] = value
        params[f"{stem}Selected"] = "true"
    return http.get_text(SEARCH_URL, params=params)


def platform_histogram(http: Any, start: str, end: str, center: str = "NGDC") -> Dict[str, Any]:
    """One request: how many experiments were released in [start, end] and on which platform families."""
    _pace(http)
    html = _search(http, start, end, 1, 20, center)
    if not html:
        log.warning("GSA: search page unavailable for %s..%s", start, end)
        return {"total": 0, "platforms": {}, "strategies": {}, "sources": {}, "selections": {}, "file_types": {}, "center": center}
    check_search_page(html, f"{start}..{end}")  # raises on a challenge page / markup change
    facets = parse_facets(html)
    return {
        "total": parse_total(html),
        "platforms": facets.get("platform", {}),
        "strategies": facets.get("strategy", {}),
        "sources": facets.get("source", {}),
        "selections": facets.get("selection", {}),
        "file_types": facets.get("file_type", {}),
        "center": center,
    }


def _days(start: str, end: str) -> Iterator[str]:
    d, e = date.fromisoformat(start), date.fromisoformat(end)
    while d <= e:
        yield d.isoformat()
        d += timedelta(days=1)


def _partition(http: Any, day: str, center: str, filters: Dict[str, str], expected: int, facets: Dict[str, Dict[str, int]],
               page_size: int, max_page_size: int, dims: List[tuple]) -> Iterator[Dict[str, str]]:
    """Yield every hit of one partition (a day plus facet filters) using single sized pages only.

    A partition that fits `max_page_size` is fetched as one page of exactly `expected` hits (complete,
    because the server's order is unstable only ACROSS pages). A bigger one is split on the first facet
    dimension in `dims` that has two or more values: sub-partitions that fit are fetched directly from
    the parent's facet counts; bigger ones are probed once (for their own facet counts) and recursed.
    """
    label = f"{day} {filters}" if filters else day
    if expected <= 0:
        return
    if expected <= max_page_size:
        html = _search(http, day, day, 1, expected, center, filters)
        if not html:
            log.warning("GSA: no response for %s (page of %d); moving on", label, expected)
            return
        if not check_search_page(html, label):
            return
        for h in parse_hits(html):
            yield h
        return
    for i, (dim, _stem) in enumerate(dims):
        counts = facets.get(dim) or {}
        if len(counts) < 2:
            continue  # a single value cannot split anything; try the next dimension
        log.info("GSA: %s: %d experiments exceed one page of %d; splitting by %s (%d values)", label, expected, max_page_size, dim, len(counts))
        for value, n in counts.items():
            sub = dict(filters)
            sub[dim] = value
            if n <= max_page_size:
                for h in _partition(http, day, center, sub, n, {}, page_size, max_page_size, dims[i + 1:]):
                    yield h
                continue
            probe = _search(http, day, day, 1, page_size, center, sub)
            if not probe:
                log.warning("GSA: no response for %s %s probe; moving on", day, sub)
                continue
            if not check_search_page(probe, f"{day} {sub}"):
                continue
            sub_total, probe_hits = parse_total(probe), parse_hits(probe)
            if sub_total <= len(probe_hits):
                for h in probe_hits:
                    yield h
                continue
            for h in _partition(http, day, center, sub, sub_total, parse_facets(probe), page_size, max_page_size, dims[i + 1:]):
                yield h
        return
    # every facet exhausted and still too big: take one capped page and say so
    log.warning("GSA: %s: %d experiments cannot be split further; fetching the first %d only", label, expected, max_page_size)
    html = _search(http, day, day, 1, max_page_size, center, filters)
    if html and check_search_page(html, label):
        for h in parse_hits(html):
            yield h


def fetch(http: Any, start: str, end: str, center: str = "NGDC", page_size: int = PAGE_SIZE, max_records: int = 20000,
          max_page_size: int = MAX_PAGE_SIZE) -> Iterator[ArchiveRecord]:
    """Yield one ArchiveRecord per experiment released in [start, end], day by day (exact first_public).

    Per day: one probe request of `page_size` hits (complete when the day has at most that many
    releases), otherwise one page sized to the day's total, or facet partitions of at most
    `max_page_size` each (see `_partition`); never a page walk, because the server's page order is
    unstable. `platform` is the family the search exposes; `instrument_model` is labelled
    "<family> (model n/a)" because the model lives on the browser-gated browse pages. `study_title`
    carries the experiment title (the only free text available). `center_name` is left empty: the
    hit names no submitter, and the NGDC/INSDC facet is a provenance filter, not a centre. Stops at
    `max_records`; a day that ends short of its total is logged ("fetched n of N"). Raises
    RuntimeError when the server answers with something other than a results page or the genuine
    "No items found" page (see `check_search_page`).
    """
    _pace(http)
    seen: set = set()
    yielded = 0
    for day in _days(start, end):
        probe = _search(http, day, day, 1, page_size, center)
        if not probe:
            log.warning("GSA: no response for %s; moving on", day)
            continue
        if not check_search_page(probe, f"{day} page 1"):
            log.info("GSA: 0 experiments released %s (%s)", day, center or "all")
            continue
        day_total, probe_hits = parse_total(probe), parse_hits(probe)
        log.info("GSA: %d experiments released %s (%s)", day_total, day, center or "all")
        if day_total <= len(probe_hits):
            source: Iterator[Dict[str, str]] = iter(probe_hits)
        else:
            source = _partition(http, day, center, {}, day_total, parse_facets(probe), page_size, max_page_size, PARTITION_DIMS)
        day_seen = 0
        for h in source:
            if h["accession"] in seen:
                continue
            seen.add(h["accession"])
            fam = h["platform"]
            yield ArchiveRecord(
                archive="gsa",
                accession=h["accession"],
                study_accession=h["study_accession"],
                instrument_model=model_label(fam),
                platform=FAMILY_TO_PLATFORM.get(fam, fam.upper() if fam else ""),
                library_strategy=h["strategy"],
                first_public=day,
                center_name="",
                study_title=h["title"],
                organism="",
            )
            yielded += 1
            day_seen += 1
            if yielded >= max_records:
                log.warning("GSA: stopping at max_records=%d", max_records)
                break
        if day_total and day_seen < day_total:
            log.warning("GSA: %s: fetched %d of %d experiments (%s)", day, day_seen, day_total,
                        "max_records reached" if yielded >= max_records else "partition shortfall")
        if yielded >= max_records:
            return


# ---------------------------------------------------------------- statistics snapshot
def fetch_statistics(http: Any) -> Optional[Dict[str, Any]]:
    """The cumulative statistics JSON (~12 MB). None when unavailable or malformed."""
    _pace(http)
    data = http.get_json(STATS_URL)
    if not isinstance(data, dict) or "platforms" not in data:
        log.warning("GSA: statistics.json unavailable or unexpected shape")
        return None
    return data


def _sub(stats: Any, key: str) -> Dict[str, Any]:
    """A nested object of the statistics JSON, or {} when the shape is off."""
    val = (stats or {}).get(key) if isinstance(stats, dict) else None
    return val if isinstance(val, dict) else {}


def platform_snapshot(stats: Dict[str, Any], snapshot_date: str) -> List[Dict[str, Any]]:
    """Rows {snapshot_date, platform, experiments, runs} from statistics.json (cumulative counts per model)."""
    rows: List[Dict[str, Any]] = []
    plist = _sub(stats, "platforms").get("platformList") or []
    if not isinstance(plist, list):
        return rows
    for p in plist:
        if not isinstance(p, dict) or not p.get("platforms"):
            continue
        try:
            rows.append({
                "snapshot_date": snapshot_date,
                "platform": str(p["platforms"]).strip(),
                "experiments": int(p.get("experiments") or 0),
                "runs": int(p.get("runs") or 0),
            })
        except (TypeError, ValueError):
            continue
    return rows


def monthly_archives(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[{month: '2026-10', archives: 731}, …] = CRA studies created per month."""
    out: List[Dict[str, Any]] = []
    items = _sub(stats, "total").get("mouthArchiveList") or []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                out.append({"month": str(item[0]), "archives": int(item[1])})
            except (TypeError, ValueError):
                continue
    return out


def totals(stats: Dict[str, Any]) -> Dict[str, int]:
    t = _sub(stats, "total")
    out: Dict[str, int] = {}
    for k in ("archiveCount", "expCount", "runCount", "releasedExp", "privateExp", "releasedRun", "privateRun"):
        try:
            out[k] = int(t.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


def publications(stats: Dict[str, Any]) -> Iterator[Dict[str, str]]:
    """Dataset -> paper bridge: {doi, pmid, cra, bioproject, journal, pub_date, title} per linked publication."""
    pubs = _sub(stats, "article").get("publications") or []
    for p in pubs if isinstance(pubs, list) else []:
        if not isinstance(p, dict):
            continue
        cra = " ".join(dict.fromkeys(re.findall(r"CRA\d+", str(p.get("craAccession") or ""))))
        prj = " ".join(dict.fromkeys(re.findall(r"PRJCA\d+", str(p.get("proAccession") or ""))))
        yield {
            "doi": (p.get("doi") or "").strip().lower(),
            "pmid": str(p.get("pubmedId") or "").strip(),
            "cra": cra,
            "bioproject": prj,
            "journal": (p.get("journal") or "").strip(),
            "pub_date": (p.get("pDate") or "").strip(),
            "title": re.sub(r"\s+", " ", str(p.get("articleTitle") or "")).strip(),
        }
