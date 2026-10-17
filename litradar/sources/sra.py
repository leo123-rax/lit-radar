"""NCBI SRA via E-utilities: the global census of which sequencers produced newly public data.

esearch:  db=sra  term=YYYY/MM/DD:YYYY/MM/DD[PDAT]  usehistory=y      -> count + WebEnv/query_key
esummary: db=sra  WebEnv/query_key  retstart/retmax=500              -> per-experiment ExpXml with
          <Platform instrument_model="Illumina NovaSeq X Plus">ILLUMINA</Platform>, study, organism,
          library strategy, submitting centre.
Also: "instrument illumina novaseq x plus"[Properties] gives an instant per-model count.

Etiquette: 3 requests/s without an NCBI API key, 10/s with one (the shared Http client paces this).
A full week is ~120k experiments = ~240 esummary pages (~190 MB); `max_records` caps it.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, Iterator, Tuple

from ..http import Http
from ..models import ArchiveRecord

log = logging.getLogger(__name__)
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _term(start: str, end: str) -> str:
    return f"{start.replace('-', '/')}:{end.replace('-', '/')}[PDAT]"


def esearch_history(http: Http, term: str, api_key: str = "") -> Tuple[int, str, str]:
    params: Dict[str, Any] = {"db": "sra", "term": term, "usehistory": "y", "retmax": 0, "retmode": "json"}
    if api_key:
        params["api_key"] = api_key
    data = http.get_json(f"{EUTILS}/esearch.fcgi", params=params)
    if not data:
        return 0, "", ""
    r = data.get("esearchresult") or {}
    return int(r.get("count") or 0), r.get("webenv") or "", r.get("querykey") or ""


def count(http: Http, term: str, api_key: str = "") -> int:
    params: Dict[str, Any] = {"db": "sra", "term": term, "retmax": 0, "retmode": "json"}
    if api_key:
        params["api_key"] = api_key
    data = http.get_json(f"{EUTILS}/esearch.fcgi", params=params)
    return int(((data or {}).get("esearchresult") or {}).get("count") or 0)


def counts_by_instrument(http: Http, start: str, end: str, models: Iterable[str], api_key: str = "") -> Dict[str, int]:
    """Per-model counts via the Properties index, e.g. models=['Illumina NovaSeq X Plus', 'Revio']."""
    out: Dict[str, int] = {}
    for m in models:
        prop = f'"instrument {m.lower()}"[Properties]'
        out[m] = count(http, f"{_term(start, end)} AND {prop}", api_key)
    return out


def _parse_expxml(xml_text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not xml_text:
        return out
    try:
        root = ET.fromstring(f"<r>{html.unescape(xml_text)}</r>")
    except ET.ParseError:
        m = re.search(r'instrument_model="([^"]+)"', html.unescape(xml_text))
        return {"instrument_model": m.group(1)} if m else {}
    plat = root.find(".//Platform")
    if plat is not None:
        out["instrument_model"] = plat.get("instrument_model") or ""
        out["platform"] = (plat.text or "").strip()
    for tag, key in (("Title", "title"),):
        el = root.find(f".//{tag}")
        if el is not None:
            out[key] = (el.text or "").strip()
    study = root.find(".//Study")
    if study is not None:
        out["study_accession"] = study.get("acc") or ""
        out["study_title"] = study.get("name") or ""
    exp = root.find(".//Experiment")
    if exp is not None:
        out["experiment_accession"] = exp.get("acc") or ""
    org = root.find(".//Organism")
    if org is not None:
        out["organism"] = org.get("ScientificName") or ""
    sub = root.find(".//Submitter")
    if sub is not None:
        out["center_name"] = sub.get("center_name") or ""
    strat = root.find(".//LIBRARY_STRATEGY")
    if strat is not None:
        out["library_strategy"] = (strat.text or "").strip()
    bp = root.find(".//Bioproject")
    if bp is not None:
        out["bioproject"] = (bp.text or "").strip()
    return out


def fetch(http: Http, start: str, end: str, api_key: str = "", max_records: int = 150000, batch: int = 500) -> Iterator[ArchiveRecord]:
    total, webenv, qk = esearch_history(http, _term(start, end), api_key)
    if not total or not webenv:
        log.warning("SRA: no records or history failed for %s..%s", start, end)
        return
    todo = min(total, max_records)
    if todo < total:
        log.warning("SRA: sweeping %d of %d experiments (max_records cap)", todo, total)
    log.info("SRA: %d experiments published %s..%s", total, start, end)
    retstart = 0
    while retstart < todo:
        params: Dict[str, Any] = {"db": "sra", "WebEnv": webenv, "query_key": qk, "retstart": retstart, "retmax": min(batch, todo - retstart), "retmode": "json"}
        if api_key:
            params["api_key"] = api_key
        data = http.get_json(f"{EUTILS}/esummary.fcgi", params=params)
        if not data or "result" not in data:
            log.warning("SRA: esummary failed at retstart=%d", retstart)
            break
        result = data["result"]
        uids = result.get("uids") or []
        if not uids:
            break
        for uid in uids:
            rec = result.get(uid) or {}
            x = _parse_expxml(rec.get("expxml") or "")
            acc = x.get("experiment_accession") or f"SRA-UID-{uid}"
            created = (rec.get("createdate") or "").replace("/", "-")[:10]
            yield ArchiveRecord(
                archive="sra",
                accession=acc,
                study_accession=x.get("study_accession") or x.get("bioproject") or "",
                instrument_model=x.get("instrument_model") or "",
                platform=x.get("platform") or "",
                library_strategy=x.get("library_strategy") or "",
                first_public=created or start,
                center_name=x.get("center_name") or "",
                study_title=x.get("study_title") or x.get("title") or "",
                organism=x.get("organism") or "",
            )
        retstart += len(uids)
