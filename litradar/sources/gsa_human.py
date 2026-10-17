"""GSA-Human (https://ngdc.cncb.ac.cn/gsa-human/): the controlled-access human-data archive at
CNCB-NGDC (HRA study / HRX experiment / HRR run). Metadata is open even though the reads are not,
and unlike the main GSA search it exposes the instrument MODEL per run.

Verified live on 2026-10-17 (no formal API documentation; the pages are server-rendered Struts actions):

1. Released-study list
   GET https://ngdc.cncb.ac.cn/gsa-human/browse/
       pageNo=<1-based>  order=b.release_time  desc=desc
       releaseTime="MM/DD/YYYY - MM/DD/YYYY"   (inclusive; same format as the GSA search)
   Response: one HTML table, 10 rows per page, "from 1 to 10, Total N". Columns (in order):
   Study (HRA), Title, Organization, DAC, Access, Requests, Valid Requests, Approved, Project (PRJCA),
   Strategy, Study Type, Disease, Individuals, Samples, Experiments, Release Time, Sharing Rank,
   Last Processed, Request.  Sample digits: 2026-09-17..10-17 = 243 studies released.
   The same parameters work as a POST; GET is used here because it is cacheable and idempotent.

2. Runs of one study (JSON)
   GET https://ngdc.cncb.ac.cn/gsa-human/ajaxb/runinstudy?accession=HRA000001&pageSize=500&pageNo=1
   -> {"totalCount": n, "pageNo": 1, "pageSize": 500, "runViews": [{"expName": "HRX000001",
       "expTitle": "WGS of …", "platform": "Illumina HiSeq 2000", "runAcc": "HRR000001",
       "strategyName": "WGS", "biosampleAcc": "SAMC019696", "sampleAcc": "HRS000001",
       "runFileName": "…", "runFileSize": "30.95"}, …]}
   pageSize up to at least 500 is honoured. One row per run; several runs share an experiment.

The study page (browse/<HRA>) also carries the release date and BioProject but is not needed.
Etiquette: paced at one request per second on ngdc.cncb.ac.cn; `max_studies` caps a sweep because
each study costs one JSON request (243 studies in a 30-day window = ~4 minutes).
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date
from typing import Any, Dict, Iterator, List, Optional

from ..models import ArchiveRecord

log = logging.getLogger(__name__)

HOST = "ngdc.cncb.ac.cn"
BROWSE_URL = f"https://{HOST}/gsa-human/browse/"
RUNS_URL = f"https://{HOST}/gsa-human/ajaxb/runinstudy"
DEFAULT_MIN_INTERVAL = 1.0
ROWS_PER_PAGE = 10
RUN_PAGE_SIZE = 500

_TOTAL_RE = re.compile(r"Total\s+([\d,]+)")
_TABLE_RE = re.compile(r"<table.*?</table>", re.S)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
COLUMNS = ["accession", "title", "organization", "dac", "access", "requests", "valid_requests", "approved", "bioproject",
           "strategy", "study_type", "disease", "individuals", "samples", "experiments", "release_time", "rank", "last_processed", "request"]


def _pace(http: Any) -> None:
    mi = getattr(http, "min_interval", None)
    if isinstance(mi, dict):
        mi.setdefault(HOST, DEFAULT_MIN_INTERVAL)


def release_range(start: str, end: str) -> str:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return f"{s:%m/%d/%Y} - {e:%m/%d/%Y}"


def _clean(cell: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", cell)).replace("&nbsp;", " ").strip()


def parse_total(html: str) -> int:
    m = _TOTAL_RE.search(html or "")
    return int(m.group(1).replace(",", "")) if m else 0


def parse_study_table(html: str) -> List[Dict[str, str]]:
    """Rows of the released-study table as dicts keyed by COLUMNS (only rows whose first cell is an HRA id)."""
    out: List[Dict[str, str]] = []
    if not html:
        return out
    t = _TABLE_RE.search(html)
    if not t:
        return out
    for row in _ROW_RE.findall(t.group(0)):
        cells = [_clean(c) for c in _CELL_RE.findall(row)]
        if not cells or not re.fullmatch(r"HRA\d+", cells[0]):
            continue
        rec = {COLUMNS[i]: (cells[i] if i < len(cells) else "") for i in range(len(COLUMNS))}
        m = re.search(r"\d{4}-\d{2}-\d{2}", rec.get("release_time") or "")
        rec["release_time"] = m.group(0) if m else ""
        out.append(rec)
    return out


def studies(http: Any, start: str, end: str, max_pages: int = 200) -> Iterator[Dict[str, str]]:
    """Studies released in [start, end], newest first. Stops on an empty page, a repeated page or `max_pages`."""
    _pace(http)
    seen: set = set()
    total: Optional[int] = None
    page = 1
    while page <= max_pages:
        params = {"pageNo": page, "order": "b.release_time", "desc": "desc", "releaseTime": release_range(start, end)}
        html = http.get_text(BROWSE_URL, params=params)
        if not html:
            log.warning("GSA-Human: no response for page %d", page)
            break
        if total is None:
            total = parse_total(html)
            log.info("GSA-Human: %d studies released %s..%s", total, start, end)
        rows = parse_study_table(html)
        new = [r for r in rows if r["accession"] not in seen]
        if not new:
            break
        for r in new:
            seen.add(r["accession"])
            yield r
        if len(rows) < ROWS_PER_PAGE or (total and len(seen) >= total) or (total and page >= math.ceil(total / ROWS_PER_PAGE)):
            break
        page += 1


def runs(http: Any, accession: str, page_size: int = RUN_PAGE_SIZE, max_pages: int = 50) -> Iterator[Dict[str, Any]]:
    """All run rows of one HRA study from the runinstudy JSON, following pageNo until totalCount is reached."""
    _pace(http)
    got = 0
    seen: set = set()
    page = 1
    while page <= max_pages:
        data = http.get_json(RUNS_URL, params={"accession": accession, "pageSize": page_size, "pageNo": page})
        if not isinstance(data, dict):
            if page == 1:
                log.warning("GSA-Human: runinstudy failed for %s", accession)
            break
        views = data.get("runViews") or []
        if not isinstance(views, list) or not views:
            break
        new = [v for v in views if isinstance(v, dict) and (v.get("runAcc") or v.get("expName")) not in seen]
        if not new:
            break
        for v in new:
            seen.add(v.get("runAcc") or v.get("expName"))
            yield v
        got += len(views)
        try:
            total = int(data.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0
        if len(views) < page_size or (total and got >= total):
            break
        page += 1


def fetch(http: Any, start: str, end: str, max_studies: int = 400) -> Iterator[ArchiveRecord]:
    """One ArchiveRecord per EXPERIMENT (HRX) of every study released in the window.

    instrument_model comes from the run's `platform` (model level, e.g. "Illumina NovaSeq 6000",
    "DNBSEQ-T7"); first_public is the study release date; organism is Homo sapiens by definition of
    the archive; center_name is the submitting organization; study_accession is the HRA id.
    """
    n_studies = 0
    for st in studies(http, start, end):
        if n_studies >= max_studies:
            log.warning("GSA-Human: stopping at max_studies=%d", max_studies)
            break
        n_studies += 1
        seen_exp: Dict[str, ArchiveRecord] = {}
        for v in runs(http, st["accession"]):
            exp = str(v.get("expName") or "").strip() or str(v.get("runAcc") or "").strip()
            if not exp or exp in seen_exp:
                continue
            seen_exp[exp] = ArchiveRecord(
                archive="gsa-human",
                accession=exp,
                study_accession=st["accession"],
                instrument_model=str(v.get("platform") or "").strip(),
                platform=_family(str(v.get("platform") or "")),
                library_strategy=str(v.get("strategyName") or st.get("strategy") or "").strip(),
                first_public=st.get("release_time") or start,
                center_name=st.get("organization") or "",
                study_title=st.get("title") or str(v.get("expTitle") or ""),
                organism="Homo sapiens",
            )
        if not seen_exp:
            # no run metadata exposed: keep the study visible as one row so the census is not silently short
            yield ArchiveRecord(
                archive="gsa-human", accession=st["accession"], study_accession=st["accession"], instrument_model="",
                platform="", library_strategy=st.get("strategy") or "", first_public=st.get("release_time") or start,
                center_name=st.get("organization") or "", study_title=st.get("title") or "", organism="Homo sapiens",
            )
        yield from seen_exp.values()


def _family(model: str) -> str:
    m = model.lower()
    if "illumina" in m or "nextseq" in m or "hiseq" in m or "novaseq" in m or "miseq" in m:
        return "ILLUMINA"
    if "dnbseq" in m or "mgiseq" in m or "bgiseq" in m:
        return "BGISEQ"
    if "nanopore" in m or "minion" in m or "gridion" in m or "promethion" in m:
        return "OXFORD_NANOPORE"
    if "pacbio" in m or "sequel" in m or "revio" in m:
        return "PACBIO_SMRT"
    if "ion " in m or "ion torrent" in m:
        return "ION_TORRENT"
    if "genolab" in m or "fastaseq" in m:
        return "GENEMIND"
    return ""
