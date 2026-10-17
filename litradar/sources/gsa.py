"""GSA, the Genome Sequence Archive at CNCB-NGDC (https://ngdc.cncb.ac.cn/gsa/): the Chinese
counterpart of SRA/ENA. Native records (CRA study / CRX experiment / CRR run) are NOT mirrored into
INSDC, so without this adapter a sequencing census misses most Chinese submissions.

Verified live on 2026-10-17 (no formal API documentation exists; the handbook is at
https://ngdc.cncb.ac.cn/gsa/support/documents). Three routes, all anonymous:

1. Search page (Elasticsearch-backed, server-rendered HTML)
   GET https://ngdc.cncb.ac.cn/gsa/search
       searchTerm='"MM/DD/YYYY - MM/DD/YYYY"[ReleaseDate]'   (the advanced-search syntax; one day = same date twice)
       pageSize=200  from=<1-based page number>
       centerSelectedItem=NGDC  centerSelected=true           (facet: NGDC = native GSA; INSDC = mirrored SRA/ENA/DDBJ)
   Response: "Total Items:&nbsp;N"; one hit per <input class="checkbox_class" data-value="CRX…">, then
   fields "Title", "Accession" (link browse/<CRA>/<CRX>), "Platform name" (FAMILY only: ILLUMINA, BGISEQ,
   OXFORD_NANOPORE, PACBIO_SMRT, GENEMIND, SALUS, ELEMENT, ULTIMA, ION_TORRENT, CAPILLARY, CapitalBio …),
   "Source name", "Strategy name", "Selection name". Sidebar facets (platform_block, strategy_block,
   source_block) carry counts for the WHOLE result set, so one request gives a platform histogram.
   Sample digits: 2026-09-17..10-17 NGDC = 33,480 experiments (ILLUMINA 21,891 / BGISEQ 11,014 /
   PACBIO_SMRT 451 / OXFORD_NANOPORE 86 / GENEMIND 11); a 200-hit page is ~900 KB and 4-9 s.

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
second and caps a window sweep with `max_records` (default 20,000 experiments = ~100 pages).
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
PAGE_SIZE = 200

_TOTAL_RE = re.compile(r"Total Items:&nbsp;\s*([\d,]+)")
_HIT_SPLIT_RE = re.compile(r'<input[^>]*class="checkbox_class"[^>]*data-value="([A-Za-z]+\d+)"')
_FIELD_RE = re.compile(
    r'<span class="result_title">\s*([^<:]+?)\s*:?\s*</span>\s*'
    r'(?:<span class="result_context">\s*([^<]*?)\s*</span>|<a href="([^"]*)"[^>]*>\s*([^<]*?)\s*</a>)',
    re.S,
)
_LINK_RE = re.compile(r"browse/([A-Za-z]+\d+)/([A-Za-z]+\d+)")
_FACET_BLOCK_RE = re.compile(r'class="(platform|strategy|source|center)_block"')
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
    """Sidebar facet counts: {'platform': {'ILLUMINA': 21891, …}, 'strategy': {…}, 'source': {…}, 'center': {…}}."""
    out: Dict[str, Dict[str, int]] = {}
    if not html:
        return out
    marks = list(_FACET_BLOCK_RE.finditer(html))
    for i, m in enumerate(marks):
        name = m.group(1)
        seg = html[m.end() : marks[i + 1].start() if i + 1 < len(marks) else m.end() + 20000]
        # a block ends at the next facet heading; keep the first contiguous run of "(n)" pairs
        counts: Dict[str, int] = {}
        for p in _FACET_PAIR_RE.finditer(seg):
            label = re.sub(r"\s+", " ", p.group(1)).strip()
            if not label or label.lower() in ("platform", "strategy", "source", "center", "selection", "downloadable", "file type"):
                continue
            counts[label] = counts.get(label, 0) + int(p.group(2).replace(",", ""))
        if counts:
            out[name] = counts
    return out


# ---------------------------------------------------------------- fetchers
def _search(http: Any, start: str, end: str, page: int, page_size: int, center: str) -> Optional[str]:
    params: Dict[str, Any] = {"searchTerm": release_term(start, end), "pageSize": page_size, "from": page}
    if center:
        params["centerSelectedItem"] = center
        params["centerSelected"] = "true"
    return http.get_text(SEARCH_URL, params=params)


def platform_histogram(http: Any, start: str, end: str, center: str = "NGDC") -> Dict[str, Any]:
    """One request: how many experiments were released in [start, end] and on which platform families."""
    _pace(http)
    html = _search(http, start, end, 1, 20, center)
    if not html:
        log.warning("GSA: search page unavailable for %s..%s", start, end)
        return {"total": 0, "platforms": {}, "strategies": {}, "sources": {}, "center": center}
    facets = parse_facets(html)
    return {
        "total": parse_total(html),
        "platforms": facets.get("platform", {}),
        "strategies": facets.get("strategy", {}),
        "sources": facets.get("source", {}),
        "center": center,
    }


def _days(start: str, end: str) -> Iterator[str]:
    d, e = date.fromisoformat(start), date.fromisoformat(end)
    while d <= e:
        yield d.isoformat()
        d += timedelta(days=1)


def fetch(http: Any, start: str, end: str, center: str = "NGDC", page_size: int = PAGE_SIZE, max_records: int = 20000) -> Iterator[ArchiveRecord]:
    """Yield one ArchiveRecord per experiment released in [start, end], day by day (exact first_public).

    `platform` is the family the search exposes; `instrument_model` is labelled "<family> (model n/a)"
    because the model lives on the browser-gated browse pages. `study_title` carries the experiment
    title (the only free text available). Stops at `max_records`, when a day is exhausted, or when a
    page repeats accessions (pagination guard).
    """
    _pace(http)
    seen: set = set()
    yielded = 0
    for day in _days(start, end):
        page = 1
        day_total: Optional[int] = None
        while yielded < max_records:
            html = _search(http, day, day, page, page_size, center)
            if not html:
                log.warning("GSA: no response for %s page %d; moving on", day, page)
                break
            if day_total is None:
                day_total = parse_total(html)
                if day_total:
                    log.info("GSA: %d experiments released %s (%s)", day_total, day, center or "all")
            hits = parse_hits(html)
            new = [h for h in hits if h["accession"] not in seen]
            if not hits or not new:
                break
            for h in new:
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
                    center_name=center or "",
                    study_title=h["title"],
                    organism="",
                )
                yielded += 1
                if yielded >= max_records:
                    log.warning("GSA: stopping at max_records=%d", max_records)
                    break
            if len(hits) < page_size or (day_total and len(seen) >= day_total):
                break
            page += 1


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
