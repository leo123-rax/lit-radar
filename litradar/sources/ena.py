"""ENA Portal API: sequencing experiments made public in a window, with the instrument model.

GET https://www.ebi.ac.uk/ena/portal/api/search?result=read_experiment&query=first_public>=YYYY-MM-DD AND first_public<=YYYY-MM-DD
    &fields=experiment_accession,study_accession,instrument_model,instrument_platform,library_strategy,...&format=json&limit=0

ENA mirrors NCBI SRA and DDBJ, so this is the closest thing to a global census of which sequencers
produced data last week. `instrument_model` is a controlled vocabulary ("Illumina NovaSeq X",
"PacBio Revio", "ONT PromethION 2 Solo", ...), which we map onto the gazetteer.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional

from ..http import Http
from ..models import ArchiveRecord

log = logging.getLogger(__name__)
PORTAL = "https://www.ebi.ac.uk/ena/portal/api/search"
FIELDS_EXPERIMENT = [
    "experiment_accession", "study_accession", "instrument_model", "instrument_platform",
    "library_strategy", "library_source", "scientific_name", "center_name", "first_public", "study_title",
]
FIELDS_RUN = [
    "run_accession", "study_accession", "instrument_model", "instrument_platform",
    "library_strategy", "library_source", "scientific_name", "center_name", "first_public", "study_title",
]


def _query(http: Http, result: str, fields: List[str], start: str, end: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    params = {
        "result": result,
        "query": f'first_public>="{start}" AND first_public<="{end}"',
        "fields": ",".join(fields),
        "format": "json",
        "limit": limit,
    }
    data = http.get_json(PORTAL, params=params)
    if data is None:
        # retry without quotes (the portal accepts both forms, but be lenient)
        params["query"] = f"first_public>={start} AND first_public<={end}"
        data = http.get_json(PORTAL, params=params)
    if isinstance(data, list):
        return data
    return None


def fetch(http: Http, start: str, end: str, limit: int = 0) -> Iterator[ArchiveRecord]:
    """Yield one record per experiment made public in [start, end]. limit=0 means no limit."""
    rows = _query(http, "read_experiment", FIELDS_EXPERIMENT, start, end, limit)
    key = "experiment_accession"
    if rows is None:
        rows = _query(http, "read_run", FIELDS_RUN, start, end, limit)
        key = "run_accession"
    if rows is None:
        log.warning("ENA portal returned nothing for %s..%s", start, end)
        return
    log.info("ENA: %d records in %s..%s", len(rows), start, end)
    for r in rows:
        yield ArchiveRecord(
            archive="ena",
            accession=r.get(key) or "",
            study_accession=r.get("study_accession") or "",
            instrument_model=r.get("instrument_model") or "",
            platform=r.get("instrument_platform") or "",
            library_strategy=r.get("library_strategy") or "",
            first_public=(r.get("first_public") or "")[:10],
            center_name=r.get("center_name") or "",
            study_title=r.get("study_title") or "",
            organism=r.get("scientific_name") or "",
        )
