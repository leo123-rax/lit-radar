"""CZ CELLxGENE Discover: curated single-cell and spatial datasets made public, with the assay declared.

CELLxGENE Discover is the largest curated corpus of single-cell / spatial datasets (every dataset is
annotated against the CELLxGENE schema, so the assay is an EFO ontology term, not free text). It records
the *assay chemistry* behind each dataset ("10x 3' v3", "Visium Spatial Gene Expression V1",
"Slide-seqV2", "Parse Evercode Whole Transcriptome v2", "BD Rhapsody Whole Transcriptome Analysis" ...),
which SRA/ENA almost never capture. It does not record the sequencer, so it complements the SRA/ENA
census rather than replacing it.

Verified live on 2026-10-17 (anonymous, honest User-Agent; official spec at
https://api.cellxgene.cziscience.com/curation/openapi.json, Swagger UI at
https://api.cellxgene.cziscience.com/curation/ui/, code examples at
https://github.com/chanzuckerberg/single-cell-curation/tree/main/notebooks/curation_api/python_raw,
portal docs at https://cellxgene.cziscience.com/docs/):

GET https://api.cellxgene.cziscience.com/curation/v1/datasets?visibility=PUBLIC
    -> HTTP 200, JSON array of 2,243 `dataset_index` objects (~9.8 MB) in ONE request. The endpoint has no
       pagination (no limit/offset parameters; unknown query parameters are ignored). Query parameters per
       the OpenAPI spec: visibility=PUBLIC (PRIVATE needs a curator bearer token), analysis=pre-analysis |
       post-analysis, schema_version=<x.y.z>.
    Fields used, with live sample values:
      dataset_id          "8f3cb9c4-38d5-4b9c-b309-bcccd76d0075"      dataset_version_id "b840a9b5-..."
      collection_id       "384e373e-4db1-4605-812f-d9da7cfd246f"      collection_name "A Single-Cell and ... Atlas"
      collection_doi      "10.1002/mco2.71022"                        title "Brain tumour single-cell atlas (...)"
      assay               [{"label": "10x 3' v3", "ontology_term_id": "EFO:0009922"}]   (1 to 11 entries per dataset)
      organism            [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}]
      cell_count          638930                                      primary_cell_count 638930
      published_at        "2026-10-16T16:35:23+00:00"                 revised_at null | ISO-8601 timestamp
      spatial             null | {"is_single": true, "has_fullres": false}
      suspension_type     ["cell", "nucleus"]      schema_version "7.1.0"      visibility "PUBLIC"
      tombstone           false                    processing_status "SUCCESS"

GET https://api.cellxgene.cziscience.com/curation/v1/collections?visibility=PUBLIC            (fallback route)
    -> JSON array of 401 `collection_list` objects (~3.2 MB): collection_id, name, doi, published_at,
       revised_at, created_at, visibility, consortia, datasets[] (dataset summaries WITHOUT cell_count or
       published_at).
GET https://api.cellxgene.cziscience.com/curation/v1/collections/{collection_id}
    -> one collection whose datasets[] DO carry cell_count, primary_cell_count, published_at, revised_at.

No rate limit is published for this API; the shared Http client paces the host at one request per second
and the weekly pull is a single ~10 MB request.

Record shape: one ArchiveRecord per (dataset, assay) with archive='cellxgene', accession=dataset_id
(suffixed '#<EFO id>' when a dataset declares several assays), instrument_model=<assay label>,
library_strategy=<assay EFO id>, platform='single-cell' or 'spatial', first_public=published_at,
study_accession=collection_id, study_title='<collection name> | <dataset title> | <n> cells',
organism=<organism label(s)>, and entity_id / vendor / ticker from the assay -> gazetteer map below.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..http import Http
from ..models import ArchiveRecord

log = logging.getLogger(__name__)

BASE = "https://api.cellxgene.cziscience.com/curation/v1"
DATASETS = f"{BASE}/datasets"
COLLECTIONS = f"{BASE}/collections"
ARCHIVE = "cellxgene"

# ---- assay -> gazetteer -------------------------------------------------------------------------
# Keyed by EFO ontology id (stable across label edits). Every id seen in the live corpus on 2026-10-17
# that maps to a commercial kit / instrument in config/instruments.yaml is listed; academic protocols
# (Drop-seq, inDrop, CEL-seq2, sci-RNA-seq3, Seq-Well, MERFISH, snmC-seq2, Patch-seq, ...) are left
# unmapped on purpose: they are methods, not products.
ASSAY_ENTITY: Dict[str, str] = {
    # 10x Genomics Chromium chemistries -> the instrument they run on
    "EFO:0009901": "txg-chromium",       # 10x 3' v1
    "EFO:0009899": "txg-chromium",       # 10x 3' v2
    "EFO:0009922": "txg-chromium",       # 10x 3' v3
    "EFO:0022604": "txg-chromium",       # 10x 3' v4
    "EFO:0011025": "txg-chromium",       # 10x 5' v1
    "EFO:0009900": "txg-chromium",       # 10x 5' v2
    "EFO:0022605": "txg-chromium",       # 10x 5' v3
    "EFO:0030003": "txg-chromium",       # 10x 3' transcription profiling
    "EFO:0030004": "txg-chromium",       # 10x 5' transcription profiling
    "EFO:0030080": "txg-chromium",       # 10x transcription profiling
    "EFO:0030007": "txg-chromium",       # 10x scATAC-seq
    "EFO:0030059": "txg-multiome",       # 10x multiome
    "EFO:0022606": "txg-chromium-flex",  # 10x gene expression flex
    "EFO:0920087": "txg-chromium-flex",  # 10x Next GEM Flex v1
    "EFO:0920088": "txg-chromium-flex",  # 10x GEM-X Flex v1
    "EFO:0920089": "txg-chromium-flex",  # 10x Flex Apex
    # 10x spatial
    "EFO:0022857": "txg-visium",         # Visium Spatial Gene Expression V1
    # Parse Biosciences (SPLiT-seq is the academic ancestor; the gazetteer lists it under Evercode)
    "EFO:0022601": "parse-evercode",     # Parse Evercode Whole Transcriptome v2
    "EFO:0009919": "parse-evercode",     # SPLiT-seq
    # BD
    "EFO:0700003": "bd-rhapsody",        # BD Rhapsody Whole Transcriptome Analysis
    "EFO:0700004": "bd-rhapsody",        # BD Rhapsody Targeted mRNA
    # Curio Bioscience (commercial Slide-seq)
    "EFO:0030062": "curio-seeker",       # Slide-seqV2
    "EFO:0920002": "curio-seeker",       # curio biosciences seeker, 3mm
    # others
    "EFO:0022490": "scale-bio",          # ScaleBio single cell RNA sequencing
    "EFO:0700011": "singleron",          # GEXSCOPE technology
    "EFO:0900002": "honeycomb-hive",     # HIVE CLX Single-Cell RNAseq Solution
    "EFO:0900000": "fluent-pipseq",      # particle-templated instant partition sequencing (PIP-seq)
    "EFO:0700016": "takara-smart-seq",   # Smart-seq v4
    "EFO:0010058": "takara-smart-seq",   # Fluidigm C1-based SMARTer library preparation
}

# Label rules for EFO ids not listed above (new terms appear a few times a year). Order matters:
# the first match wins, so the specific 10x products come before the generic "10x" rule.
_LABEL_RULES: Sequence[Tuple[re.Pattern[str], str]] = (
    (re.compile(r"visium\s*hd", re.I), "txg-visium-hd"),
    (re.compile(r"visium", re.I), "txg-visium"),
    (re.compile(r"xenium", re.I), "txg-xenium"),
    (re.compile(r"10x.*multiome|multiome.*10x", re.I), "txg-multiome"),
    (re.compile(r"10x.*flex|flex.*10x", re.I), "txg-chromium-flex"),
    (re.compile(r"\b10x\b|chromium", re.I), "txg-chromium"),
    (re.compile(r"evercode|parse bio|split-seq", re.I), "parse-evercode"),
    (re.compile(r"rhapsody", re.I), "bd-rhapsody"),
    (re.compile(r"slide-?seq|curio|slide-?tags", re.I), "curio-seeker"),
    (re.compile(r"scale\s*bio|quantumscale", re.I), "scale-bio"),
    (re.compile(r"gexscope|singleron", re.I), "singleron"),
    (re.compile(r"\bhive\b|honeycomb", re.I), "honeycomb-hive"),
    (re.compile(r"pip-?seq|particle-templated", re.I), "fluent-pipseq"),
    (re.compile(r"tapestri|mission bio", re.I), "missionbio-tapestri"),
    (re.compile(r"icell8", re.I), "takara-icell8"),
    (re.compile(r"smart-?seq\s*v4|smarter", re.I), "takara-smart-seq"),
    (re.compile(r"stereo-?seq|stomics", re.I), "bgi-stereo-seq"),
    (re.compile(r"merscope", re.I), "vizgen-merscope"),
    (re.compile(r"cosmx", re.I), "bruker-cosmx"),
    (re.compile(r"geomx", re.I), "bruker-geomx"),
)

# vendor / ticker for the ids above, as spelled in config/instruments.yaml (tests/test_sources_cellxgene.py
# checks they stay in sync). The pipeline's gazetteer match may override these at store time.
ENTITY_VENDORS: Dict[str, Tuple[str, str]] = {
    "txg-chromium": ("10x Genomics", "TXG"),
    "txg-chromium-flex": ("10x Genomics", "TXG"),
    "txg-multiome": ("10x Genomics", "TXG"),
    "txg-visium": ("10x Genomics", "TXG"),
    "txg-visium-hd": ("10x Genomics", "TXG"),
    "txg-xenium": ("10x Genomics", "TXG"),
    "parse-evercode": ("Parse Biosciences", "private"),
    "bd-rhapsody": ("BD", "BDX"),
    "curio-seeker": ("Curio Bioscience", "private"),
    "scale-bio": ("Scale Biosciences", "private"),
    "singleron": ("Singleron Biotechnologies", "private"),
    "honeycomb-hive": ("Honeycomb Biotechnologies", "private"),
    "fluent-pipseq": ("Illumina", "ILMN"),
    "takara-smart-seq": ("Takara Bio", "4974.T"),
    "takara-icell8": ("Takara Bio", "4974.T"),
    "missionbio-tapestri": ("Mission Bio", "private"),
    "bgi-stereo-seq": ("BGI / STOmics", "300676.SZ"),
    "vizgen-merscope": ("Vizgen", "private"),
    "bruker-cosmx": ("Bruker", "BRKR"),
    "bruker-geomx": ("Bruker", "BRKR"),
}

_SPATIAL_LABEL = re.compile(r"visium|xenium|slide-?seq|seeker|merfish|merscope|stereo-?seq|cosmx|geomx|spatial", re.I)
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def entity_for(label: str, ontology_term_id: str = "") -> str:
    """Gazetteer id for an assay, by EFO id first and label rules second; '' when nothing obvious applies."""
    eid = (ontology_term_id or "").strip()
    if eid in ASSAY_ENTITY:
        return ASSAY_ENTITY[eid]
    text = (label or "").strip()
    if not text:
        return ""
    for rx, entity in _LABEL_RULES:
        if rx.search(text):
            return entity
    return ""


# ---- small parsers ------------------------------------------------------------------------------
def _date(value: Any) -> str:
    """'2026-10-16T16:35:23+00:00' -> '2026-10-16'; anything unparseable -> ''."""
    if not isinstance(value, str):
        return ""
    m = _DATE.match(value.strip())
    return m.group(1) if m else ""


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:  # not NaN
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _labels(items: Any) -> List[str]:
    out: List[str] = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            lab = str(it.get("label") or "").strip()
        else:
            lab = str(it or "").strip()
        if lab and lab not in out:
            out.append(lab)
    return out


def _assays(ds: Dict[str, Any]) -> List[Tuple[str, str]]:
    """[(label, EFO id)] declared on a dataset, de-duplicated and in declared order."""
    out: Dict[Tuple[str, str], None] = {}  # insertion-ordered, used as an ordered set
    for a in ds.get("assay") if isinstance(ds.get("assay"), list) else []:
        if isinstance(a, dict):
            label = str(a.get("label") or "").strip()
            eid = str(a.get("ontology_term_id") or "").strip()
        elif isinstance(a, str):
            label, eid = a.strip(), ""
        else:
            continue
        if label or eid:
            out[(label, eid)] = None
    return list(out.keys())


def in_window(ds: Dict[str, Any], start: str, end: str, include_revisions: bool = False) -> bool:
    """True when the dataset was first published in [start, end] (or revised in it, when asked)."""
    pub = _date(ds.get("published_at"))
    if pub and start <= pub <= end:
        return True
    if include_revisions:
        rev = _date(ds.get("revised_at"))
        if rev and start <= rev <= end:
            return True
    return False


def _study_title(ds: Dict[str, Any], cells: Optional[int]) -> str:
    parts = [str(ds.get("collection_name") or "").strip(), str(ds.get("title") or "").strip()]
    if cells is not None:
        parts.append(f"{cells:,} cells")
    return " | ".join(p for p in parts if p)


def to_records(ds: Any) -> List[ArchiveRecord]:
    """One ArchiveRecord per assay declared on a dataset; [] for anything that is not a dataset dict."""
    if not isinstance(ds, dict):
        return []
    dataset_id = str(ds.get("dataset_id") or "").strip()
    if not dataset_id:
        return []
    assays = _assays(ds) or [("", "")]  # keep a dataset with no assay annotation, like SRA/ENA do
    cells = _int(ds.get("cell_count"))
    first_public = _date(ds.get("published_at"))
    collection_id = str(ds.get("collection_id") or "").strip()
    organism = "; ".join(_labels(ds.get("organism")))
    title = _study_title(ds, cells)
    spatial_flag = bool(ds.get("spatial"))
    multi = len(assays) > 1
    out: List[ArchiveRecord] = []
    for label, eid in assays:
        entity = entity_for(label, eid)
        vendor, ticker = ENTITY_VENDORS.get(entity, ("", ""))
        platform = "spatial" if (spatial_flag or _SPATIAL_LABEL.search(label or "")) else "single-cell"
        out.append(
            ArchiveRecord(
                archive=ARCHIVE,
                accession=f"{dataset_id}#{eid or label}" if multi else dataset_id,
                study_accession=collection_id,
                instrument_model=label,
                platform=platform,
                library_strategy=eid,
                first_public=first_public,
                center_name="",
                study_title=title,
                organism=organism,
                entity_id=entity,
                vendor=vendor,
                ticker=ticker,
            )
        )
    return out


# ---- fetching -----------------------------------------------------------------------------------
def fetch_datasets(http: Http) -> Optional[List[Dict[str, Any]]]:
    """The whole public dataset index in one request, or None when the endpoint is unavailable / malformed."""
    data = http.get_json(DATASETS, params={"visibility": "PUBLIC"})
    if isinstance(data, list):
        return data
    if data is not None:
        log.warning("CELLxGENE: /datasets returned %s, expected a JSON array", type(data).__name__)
    return None


def _collection_in_window(c: Dict[str, Any], start: str, end: str) -> bool:
    # a revision (new datasets added) bumps revised_at, so both dates make a collection a candidate
    for key in ("published_at", "revised_at"):
        d = _date(c.get(key))
        if d and start <= d <= end:
            return True
    return False


def datasets_via_collections(http: Http, start: str, end: str, max_collection_details: int = 50) -> List[Dict[str, Any]]:
    """Fallback: list collections, then fetch the detail of each candidate collection in the window.

    Only the detail endpoint carries cell_count and dataset-level published_at. Candidates are visited
    newest first and the walk stops after `max_collection_details` requests (0 = no cap).
    """
    colls = http.get_json(COLLECTIONS, params={"visibility": "PUBLIC"})
    if not isinstance(colls, list):
        log.warning("CELLxGENE: /collections unavailable or malformed")
        return []
    candidates = [c for c in colls if isinstance(c, dict) and c.get("collection_id") and _collection_in_window(c, start, end)]
    candidates.sort(key=lambda c: max(_date(c.get("revised_at")), _date(c.get("published_at"))), reverse=True)
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(candidates):
        if max_collection_details and i >= max_collection_details:
            log.warning("CELLxGENE: stopping after %d collection detail calls (%d candidates)", i, len(candidates))
            break
        detail = http.get_json(f"{COLLECTIONS}/{c['collection_id']}")
        if not isinstance(detail, dict):
            continue
        for ds in detail.get("datasets") if isinstance(detail.get("datasets"), list) else []:
            if not isinstance(ds, dict):
                continue
            ds = dict(ds)
            ds.setdefault("collection_id", c["collection_id"])
            ds.setdefault("collection_name", detail.get("name") or c.get("name") or "")
            ds.setdefault("collection_doi", detail.get("doi") or c.get("doi"))
            out.append(ds)
    return out


def fetch(
    http: Http,
    start: str,
    end: str,
    include_revisions: bool = False,
    max_records: int = 0,
    max_collection_details: int = 50,
) -> Iterator[ArchiveRecord]:
    """Yield one record per (dataset, assay) for datasets first published in [start, end] (YYYY-MM-DD).

    include_revisions=True also yields datasets whose latest revision landed in the window (their
    first_public stays the original publication date). max_records caps the yield (0 = all).
    """
    datasets = fetch_datasets(http)
    if datasets is None:
        log.warning("CELLxGENE: /datasets unavailable; falling back to per-collection detail calls")
        datasets = datasets_via_collections(http, start, end, max_collection_details)
    n = 0
    seen_ds = 0
    for ds in datasets:
        if not isinstance(ds, dict) or not in_window(ds, start, end, include_revisions):
            continue
        seen_ds += 1
        for rec in to_records(ds):
            yield rec
            n += 1
            if max_records and n >= max_records:
                log.warning("CELLxGENE: stopping at max_records=%d", max_records)
                return
    log.info("CELLxGENE: %d datasets / %d assay records published %s..%s", seen_ds, n, start, end)


# ---- summaries (used by the live check and handy for a CLI) --------------------------------------
def assay_histogram(datasets: Sequence[Any], start: str = "", end: str = "", include_revisions: bool = False) -> List[Dict[str, Any]]:
    """Datasets, cells and collections per assay label, optionally restricted to a window; sorted by datasets."""
    rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        if start and end and not in_window(ds, start, end, include_revisions):
            continue
        cells = _int(ds.get("cell_count")) or 0
        for label, eid in _assays(ds):
            row = rows.setdefault((label, eid), {"assay": label, "efo": eid, "entity_id": entity_for(label, eid), "datasets": 0, "cells": 0, "collections": set()})
            row["datasets"] += 1
            row["cells"] += cells
            if ds.get("collection_id"):
                row["collections"].add(ds["collection_id"])
    out = []
    for row in rows.values():
        vendor, ticker = ENTITY_VENDORS.get(row["entity_id"], ("", ""))
        out.append({**row, "collections": len(row["collections"]), "vendor": vendor, "ticker": ticker})
    out.sort(key=lambda r: (-r["datasets"], -r["cells"], r["assay"]))
    return out


def window_summary(datasets: Sequence[Any], start: str, end: str, include_revisions: bool = False) -> Dict[str, Any]:
    """Counts for a window: datasets, distinct collections, cells, plus the assay histogram."""
    in_win = [ds for ds in datasets if isinstance(ds, dict) and in_window(ds, start, end, include_revisions)]
    return {
        "start": start,
        "end": end,
        "datasets": len(in_win),
        "collections": len({ds.get("collection_id") for ds in in_win if ds.get("collection_id")}),
        "cells": sum(_int(ds.get("cell_count")) or 0 for ds in in_win),
        "assays": assay_histogram(in_win),
    }
