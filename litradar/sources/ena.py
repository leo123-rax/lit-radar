"""ENA Portal API: sequencing experiments made public in a window, with the instrument model.

GET https://www.ebi.ac.uk/ena/portal/api/search?result=read_experiment
    &query=first_public>="YYYY-MM-DD" AND first_public<"YYYY-MM-DD+1"
    &fields=experiment_accession,study_accession,instrument_model,instrument_platform,library_strategy,...&format=json&limit=0

ENA mirrors NCBI SRA and DDBJ, so this is the closest thing to a global census of which sequencers
produced data last week. `instrument_model` is a controlled vocabulary ("Illumina NovaSeq X",
"PacBio Revio", "ONT PromethION 2 Solo", ...), which we map onto the gazetteer.

Date bound: the portal compares ``first_public`` against a bare date as midnight on that day, so
``first_public<="2026-10-15"`` matches nothing published *on* the 15th (count 0) while
``first_public<"2026-10-16"`` returns that day's records (count 420 on 2026-10-17). The adapter
therefore sends ``>= start`` and ``< end + 1 day`` so the last day of every window is included.

Granularity: ``result=read_experiment`` is served one row per *run* (the portal appends
``run_accession`` as a primary accession even when not requested), so a window with 1,807 rows holds
1,783 distinct experiments. The adapter de-duplicates on the key accession before yielding and logs
the distinct count.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
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


def exclusive_end(end: str) -> str:
    """YYYY-MM-DD -> the following day, for an exclusive ``first_public<`` bound that still includes ``end``."""
    try:
        return (date.fromisoformat(end[:10]) + timedelta(days=1)).isoformat()
    except ValueError:
        return end


def date_query(start: str, end: str, quoted: bool = True) -> str:
    nxt = exclusive_end(end)
    if quoted:
        return f'first_public>="{start}" AND first_public<"{nxt}"'
    return f"first_public>={start} AND first_public<{nxt}"


def _query(http: Http, result: str, fields: List[str], start: str, end: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    params = {
        "result": result,
        "query": date_query(start, end, quoted=True),
        "fields": ",".join(fields),
        "format": "json",
        "limit": limit,
    }
    data = http.get_json(PORTAL, params=params)
    if data is None:
        # retry without quotes (the portal accepts both forms, but be lenient)
        params["query"] = date_query(start, end, quoted=False)
        data = http.get_json(PORTAL, params=params)
    if isinstance(data, list):
        return data
    return None


def fetch(http: Http, start: str, end: str, limit: int = 0) -> Iterator[ArchiveRecord]:
    """Yield one record per experiment made public in [start, end] (both inclusive). limit=0 means no limit."""
    rows = _query(http, "read_experiment", FIELDS_EXPERIMENT, start, end, limit)
    key = "experiment_accession"
    if rows is None:
        rows = _query(http, "read_run", FIELDS_RUN, start, end, limit)
        key = "run_accession"
    if rows is None:
        log.warning("ENA portal returned nothing for %s..%s", start, end)
        return
    seen: set = set()
    for r in rows:
        acc = r.get(key) or ""
        if not acc or acc in seen:
            continue
        seen.add(acc)
        yield ArchiveRecord(
            archive="ena",
            accession=acc,
            study_accession=r.get("study_accession") or "",
            instrument_model=r.get("instrument_model") or "",
            platform=r.get("instrument_platform") or "",
            library_strategy=r.get("library_strategy") or "",
            first_public=(r.get("first_public") or "")[:10],
            center_name=r.get("center_name") or "",
            study_title=r.get("study_title") or "",
            organism=r.get("scientific_name") or "",
        )
    log.info("ENA: %d distinct %s(s) from %d rows in %s..%s", len(seen), key.replace("_accession", ""), len(rows), start, end)
