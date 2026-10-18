"""NCBI SRA via E-utilities: the global census of which sequencers produced newly public data.

esearch:  db=sra  term=YYYY/MM/DD:YYYY/MM/DD[PDAT]  usehistory=y      -> count + WebEnv/query_key
esummary: db=sra  WebEnv/query_key  retstart/retmax=500  retmode=json -> per-experiment ExpXml with
          <Platform instrument_model="Illumina NovaSeq X Plus">ILLUMINA</Platform>, study, organism,
          library strategy, submitting centre.
Also: "instrument illumina novaseq x plus"[Properties] gives an instant per-model count.

Etiquette: 3 requests/s without an NCBI API key, 10/s with one (the shared Http client paces this at
0.4 s / 0.12 s and tolerates up to six consecutive 429s from E-utilities before giving up, so a 240-page
census is not silently truncated by a transient pair of 429s). A full week is ~120k experiments =
~240 esummary pages (~190 MB); `max_records` caps it.

ExpXml parsing (important)
    With ``retmode=json`` the ``expxml`` string is *already* well-formed XML: the JSON decoder performed
    the one level of unescaping, so ``&amp;`` inside it is a correctly escaped ampersand. Running
    ``html.unescape()`` on it before parsing turns ``&amp;`` into a bare ``&``, which is illegal XML, so
    every record whose centre, study or title contains an ampersand used to fall into the regex fallback
    and lose its accession (stored as ``SRA-UID-<uid>``), study, strategy, organism and centre. The parser
    now tries the raw string first and only unescapes when the raw parse fails (the ``retmode=xml`` form
    is double-escaped and needs it). The final regex fallback also reads the experiment and study
    accessions so an accession is never synthetic unless the record really lacks one.

    Rows written by the old parser carry accession ``SRA-UID-*`` and cannot de-duplicate against ENA.
    After upgrading, delete them and re-run the affected window::

        DELETE FROM archive_records WHERE accession LIKE 'SRA-UID-%';
        litradar run --since YYYY-MM-DD --until YYYY-MM-DD --only sra
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

from ..http import Http
from ..models import ArchiveRecord

log = logging.getLogger(__name__)
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_API_KEY_RE = re.compile(r"^[A-Za-z0-9]+$")  # NCBI keys are 36 hex characters; anything with spaces, '#' or URL characters is a comment
_warned_bad_key = False


class SraError(RuntimeError):
    """NCBI E-utilities refused or failed the request (HTTP 4xx/5xx, an `error` payload, no answer). Carries the
    status and NCBI's message so a run reports *why* the census is empty instead of a silent zero."""


def usable_api_key(api_key: str) -> str:
    """The key to send, or '' when the configured value cannot be a real NCBI key.

    A `.env` line such as ``NCBI_API_KEY=      # get one at ...`` used to reach the request as
    ``api_key=# get one at ...`` and NCBI answered HTTP 400 for every call. Only a plain alphanumeric token is
    sent; anything else is dropped with one warning and the request goes out anonymously (3 requests/s instead
    of 10). A wrong-but-well-formed key still reaches NCBI and now surfaces as SraError (HTTP 400)."""
    global _warned_bad_key
    key = (api_key or "").strip()
    if not key:
        return ""
    if _API_KEY_RE.match(key):
        return key
    if not _warned_bad_key:
        _warned_bad_key = True
        log.warning("NCBI_API_KEY does not look like an NCBI key (%d chars, contains %s); sending requests without a key",
                    len(key), "spaces/'#'" if (" " in key or "#" in key) else "unexpected characters")
    return ""


def _term(start: str, end: str) -> str:
    return f"{start.replace('-', '/')}:{end.replace('-', '/')}[PDAT]"


def _esearch(http: Http, params: Dict[str, Any], what: str) -> Dict[str, Any]:
    """One esearch call. Returns the `esearchresult` mapping; raises SraError on a non-200 answer, an `error`
    envelope or NCBI's own ``ERROR`` field, and when the client gave up (network failure, rate limit)."""
    url = f"{EUTILS}/esearch.fcgi"
    getter = getattr(http, "get", None)
    if callable(getter):
        resp = getter(url, params=params, headers={"Accept": "application/json"})
        if resp is None:
            raise SraError(f"NCBI esearch gave no answer for {what} (network failure, retries exhausted or host rate-limited)")
        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            body = " ".join((getattr(resp, "text", "") or "").split())[:200]
            hint = "; check NCBI_API_KEY (a malformed key is the usual cause)" if status == 400 else ""
            raise SraError(f"NCBI esearch answered HTTP {status} for {what}{hint}" + (f": {body}" if body else ""))
        try:
            data = resp.json()
        except ValueError:
            raise SraError(f"NCBI esearch returned a non-JSON body for {what}") from None
    else:  # duck-typed clients that only offer get_json (tests, thin wrappers)
        data = http.get_json(url, params=params)
        if data is None:
            raise SraError(f"NCBI esearch gave no answer for {what}")
    if not isinstance(data, dict):
        raise SraError(f"NCBI esearch returned an unexpected payload for {what}")
    if data.get("error"):
        raise SraError(f"NCBI esearch error for {what}: {str(data['error'])[:200]}")
    result = data.get("esearchresult")
    if not isinstance(result, dict):
        raise SraError(f"NCBI esearch payload has no esearchresult for {what}")
    if result.get("ERROR"):
        raise SraError(f"NCBI esearch error for {what}: {str(result['ERROR'])[:200]}")
    return result


def esearch_history(http: Http, term: str, api_key: str = "") -> Tuple[int, str, str]:
    """(count, WebEnv, query_key) for a term; raises SraError when NCBI refuses or fails the call."""
    params: Dict[str, Any] = {"db": "sra", "term": term, "usehistory": "y", "retmax": 0, "retmode": "json"}
    key = usable_api_key(api_key)
    if key:
        params["api_key"] = key
    r = _esearch(http, params, term)
    return int(r.get("count") or 0), r.get("webenv") or "", r.get("querykey") or ""


def count(http: Http, term: str, api_key: str = "") -> int:
    """Instant count for a term; 0 when NCBI cannot answer (the failure is logged, not raised: counts are advisory)."""
    params: Dict[str, Any] = {"db": "sra", "term": term, "retmax": 0, "retmode": "json"}
    key = usable_api_key(api_key)
    if key:
        params["api_key"] = key
    try:
        return int(_esearch(http, params, term).get("count") or 0)
    except SraError as exc:
        log.warning("%s", exc)
        return 0


def counts_by_instrument(http: Http, start: str, end: str, models: Iterable[str], api_key: str = "") -> Dict[str, int]:
    """Per-model counts via the Properties index, e.g. models=['Illumina NovaSeq X Plus', 'Revio']."""
    out: Dict[str, int] = {}
    for m in models:
        prop = f'"instrument {m.lower()}"[Properties]'
        out[m] = count(http, f"{_term(start, end)} AND {prop}", api_key)
    return out


def _parse_xml(xml_text: str) -> Optional[ET.Element]:
    """Parse ExpXml: raw first (retmode=json form), then html-unescaped (retmode=xml form)."""
    try:
        root = ET.fromstring(f"<r>{xml_text}</r>")
        # a double-escaped payload parses as one text node that itself looks like markup
        if len(root) or not (root.text or "").lstrip().startswith("<"):
            return root
    except ET.ParseError:
        pass
    unescaped = html.unescape(xml_text)
    if unescaped == xml_text:
        return None
    try:
        return ET.fromstring(f"<r>{unescaped}</r>")
    except ET.ParseError:
        return None


_RX_FALLBACK = {
    "instrument_model": re.compile(r'instrument_model="([^"]+)"'),
    "experiment_accession": re.compile(r'<Experiment\b[^>]*\bacc="([^"]+)"'),
    "study_accession": re.compile(r'<Study\b[^>]*\bacc="([^"]+)"'),
    "study_title": re.compile(r'<Study\b[^>]*\bname="([^"]+)"'),
    "library_strategy": re.compile(r"<LIBRARY_STRATEGY>([^<]+)</LIBRARY_STRATEGY>"),
    "organism": re.compile(r'<Organism\b[^>]*\bScientificName="([^"]+)"'),
    "center_name": re.compile(r'<Submitter\b[^>]*\bcenter_name="([^"]+)"'),
}


def _parse_expxml(xml_text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not xml_text:
        return out
    root = _parse_xml(xml_text)
    if root is None:
        # last resort: regexes over the (possibly malformed) text so the accession is never synthetic
        text = html.unescape(xml_text) if "&lt;" in xml_text else xml_text
        for key, rx in _RX_FALLBACK.items():
            m = rx.search(text)
            if m:
                out[key] = html.unescape(m.group(1)).strip()
        return out
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
    """Yield one ArchiveRecord per SRA experiment made public in [start, end].

    Raises SraError when NCBI refuses or fails the esearch call (HTTP 400 for a malformed api_key, 429/5xx with
    retries exhausted, an `error` payload), so a run reports the failure instead of a silently empty census."""
    api_key = usable_api_key(api_key)
    total, webenv, qk = esearch_history(http, _term(start, end), api_key)
    if not total:
        log.info("SRA: no experiments published %s..%s", start, end)
        return
    if not webenv or not qk:
        raise SraError(f"NCBI esearch reported {total} experiments for {start}..{end} but returned no WebEnv/query_key to page them")
    todo = min(total, max_records)
    if todo < total:
        log.warning("SRA: sweeping %d of %d experiments (max_records cap)", todo, total)
    log.info("SRA: %d experiments published %s..%s", total, start, end)
    retstart = 0
    synthetic = 0
    while retstart < todo:
        params: Dict[str, Any] = {"db": "sra", "WebEnv": webenv, "query_key": qk, "retstart": retstart, "retmax": min(batch, todo - retstart), "retmode": "json"}
        if api_key:
            params["api_key"] = api_key
        data = http.get_json(f"{EUTILS}/esummary.fcgi", params=params)
        if not data or "result" not in data:
            log.warning("SRA: esummary failed at retstart=%d; census truncated at %d of %d records", retstart, retstart, todo)
            break
        result = data["result"]
        uids = result.get("uids") or []
        if not uids:
            break
        for uid in uids:
            rec = result.get(uid) or {}
            x = _parse_expxml(rec.get("expxml") or "")
            acc = x.get("experiment_accession")
            if not acc:
                acc = f"SRA-UID-{uid}"
                synthetic += 1
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
    if synthetic:
        log.warning("SRA: %d record(s) had no parseable experiment accession and were stored as SRA-UID-*", synthetic)
