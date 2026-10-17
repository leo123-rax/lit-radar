"""NCBI GEO via E-utilities: newly public GEO Series (GSE) and the platforms (GPL) they ran on.

GEO is the archive behind most published expression / epigenomics / single-cell datasets. Every
Series is tied to one or more Platform records whose title names the instrument and organism
("Illumina NovaSeq 6000 (Homo sapiens)", "Revio (Escherichia coli)", "Element AVITI (Brachypodium
distachyon)"), so a weekly sweep of new Series is a second, study-level census of which machines
are producing public data, complementary to the experiment-level SRA / ENA sweeps.

Official documentation
    E-utilities:               https://www.ncbi.nlm.nih.gov/books/NBK25499/   (esearch / esummary)
    GEO programmatic access:   https://www.ncbi.nlm.nih.gov/geo/info/geo_paccess.html
    GEO DataSets search fields (einfo db=gds): ETYP (Entry Type: DataSet | Series | Platform | Sample),
        PDAT (publication date), ACCN (GEO accession), TITL, PTYP (platform technology type), ORGN, NSAM.

Endpoints, verified live on 2026-10-17
    1. esearch.fcgi  db=gds  term="GSE[ETYP] AND 2026/10/10:2026/10/17[PDAT]"  usehistory=y  retmax=0  retmode=json
       -> esearchresult: count "516", webenv "MCID_...", querykey "1",
          querytranslation "GSE[ETYP] AND 2026/10/10[PDAT] : 2026/10/17[PDAT]"
    2. esummary.fcgi db=gds  WebEnv=... query_key=1  retstart=0 retmax=200  retmode=json
       -> result.uids ["200351148", ...] and one DocSum per uid (GSE uid = 200000000 + series number):
          accession "GSE351130", title, summary, gpl "24247" (semicolon-joined for multi-platform
          series, e.g. "21290;24676"; 42 of 516 series that week), gse "351130", taxon "Mus musculus",
          entrytype "GSE", gdstype "Expression profiling by high throughput sequencing" (semicolon-joined
          when mixed), pdat "2026/10/16", n_samples 8, samples [{accession "GSM10127937", title}],
          bioproject "PRJNA1544684", ftplink, pubmedids [], suppfile "MTX, TSV".
          The DocSum fields `platformtitle`, `platformtaxa`, `samplestaxa`, `seriestitle` are EMPTY
          strings on GSE records, so the platform title needs the second lookup below.
          A 200-record page is ~0.75 MB (the per-sample list is the bulk of it).
    3. esummary.fcgi db=gds  id=100024676,100016791,...  retmode=json   (GPL uid = 100000000 + GPL number)
       -> accession "GPL24676", title "Illumina NovaSeq 6000 (Homo sapiens)", taxon "Homo sapiens",
          ptechtype "high-throughput sequencing" | "in situ oligonucleotide" | "other" | ..., pdat
          "2018/03/02", n_samples 1166851, and a very large `gse` back-reference string listing every
          series on the platform (~200 KB for popular platforms; a 50-id batch is ~1.1 MB, ~0.5 s).
       The GPL uid = 100000000 + number rule is an observed convention, not a documented contract; when a
       batch resolves nothing the adapter recovers the real uids with
       esearch.fcgi db=gds term="(GPL24676[ACCN] OR GPL33484[ACCN]) AND GPL[ETYP]" -> idlist ["100024676", "100033484"]
       (verified live 2026-10-17) and retries the esummary once.
    4. esearch.fcgi  db=gds  term="GSE[ETYP] AND 2025/01/01:2025/12/31[PDAT] AND (GPL24676[ACCN] OR GPL24247[ACCN])"
       retmax=10000 -> count "11139", idlist of 10000 uids; the same term with retstart=10000 returned the
       remaining 1139 uids with no overlap (verified live 2026-10-17). `series_uids_by_platform` pages this way
       and unions the uids, so a series on two platforms of one family is counted once.

Platform-title conventions seen live (GEO uses the sequencer vocabulary without a vendor prefix for
PacBio and ONT): "Illumina NovaSeq X Plus (Anser anser)", "Illumina HiSeq 2500 (Homo sapiens)",
"NextSeq 2000 (Homo sapiens)", "Sequel II (Oxytricha trifallax)", "Revio (Escherichia coli)",
"Onso (Homo sapiens)", "PacBio RS II (Capra hircus)", "PromethION (Drosophila yakuba)",
"Element AVITI (Escherichia coli; Escherichia phage MS2)", "Xenium In Situ Analyzer: Danio rerio",
"[HuGene-2_0-st] Affymetrix Human Gene 2.0 ST Array [transcript (gene) version]".
`instrument_model` is the title with the trailing organism removed ("Illumina NovaSeq 6000").

Record mapping (ArchiveRecord, archive="geo"): one record per (Series, Platform) pair.
    accession        "GSE351130/GPL24247"   (a multi-platform series yields one record per platform)
    study_accession  "GSE351130"
    instrument_model platform title minus organism, e.g. "Illumina NovaSeq 6000"
    platform         GPL technology type, e.g. "high-throughput sequencing" (arrays: "in situ oligonucleotide")
    library_strategy gdstype, e.g. "Expression profiling by high throughput sequencing"
    first_public     pdat as YYYY-MM-DD
    study_title      series title;   organism  series taxon;   center_name  "" (not in the DocSum)

Etiquette: 3 requests/s without an NCBI API key, 10/s with one; the shared Http client paces the
eutils host. A full week is ~3 esearch/esummary pages plus ~3 GPL batches (~5 MB in total).
Pagination stops when `retstart` reaches the esearch count, when a page carries no uids, or at
`max_records`.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from ..http import Http
from ..models import ArchiveRecord

log = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
GPL_UID_BASE = 100000000  # GPL24676 -> uid 100024676
GSE_UID_BASE = 200000000  # GSE351130 -> uid 200351130
SERIES_PAGE = 200  # GSE DocSums per esummary page (~0.75 MB)
GPL_BATCH = 50  # GPL DocSums per esummary call (~1.1 MB; NCBI asks for POST above ~200 ids)
ESEARCH_PAGE = 10000  # esearch retmax ceiling; larger uid sets are paged with retstart
ACCN_CHUNK = 100  # GPLnnn[ACCN] terms OR-ed per esearch call

# State of the most recent fetch_series() sweep: {"start", "end", "total", "fetched", "complete", "error"}.
# A caller that reports coverage (the pipeline) appends sweep_error() to its error list when it is non-empty.
last_sweep: Dict[str, Any] = {}

# trailing "(Homo sapiens)" / "(Escherichia coli; Escherichia phage MS2)" / "(mouse gut metagenome)"
_TRAILING_PAREN = re.compile(r"\s*\(([^()]*)\)\s*$")
# "(V4)", "(v2.1)", "(Version 2)": array design versions, never organisms
_VERSION_LIKE = re.compile(r"^v(er(sion)?)?\s*\d", re.I)


# ----------------------------------------------------------------------------- small helpers
def _term(start: str, end: str, extra_term: str = "") -> str:
    """GEO DataSets query for Series released in [start, end] (dates as YYYY-MM-DD)."""
    t = f"GSE[ETYP] AND {start.replace('-', '/')}:{end.replace('-', '/')}[PDAT]"
    return f"{t} AND {extra_term}" if extra_term else t


def gpl_accession(gpl: Any) -> str:
    """'24676' | 24676 | 'GPL24676' | ' gpl24676 ' -> 'GPL24676'. Returns '' for junk."""
    s = str(gpl or "").strip().upper()
    if s.startswith("GPL"):
        s = s[3:]
    return f"GPL{int(s)}" if s.isascii() and s.isdigit() else ""


def gpl_uid(gpl: Any) -> str:
    acc = gpl_accession(gpl)
    return str(GPL_UID_BASE + int(acc[3:])) if acc else ""


def _split_gpls(raw: Any) -> List[str]:
    """DocSum `gpl` field ('24247' or '21290;24676') -> ['GPL24247'] / ['GPL21290', 'GPL24676']."""
    out: List[str] = []
    for part in str(raw or "").split(";"):
        acc = gpl_accession(part)
        if acc and acc not in out:
            out.append(acc)
    return out


def _date(pdat: Any) -> str:
    """'2026/10/16' -> '2026-10-16' (empty string when absent or unparseable)."""
    s = str(pdat or "").strip().replace("/", "-")
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else ""


def _is_design_detail(inner: str) -> bool:
    """Parenthetical that is an array design note, not an organism: '(V4)', '(2.0)', '(Feature Number version)'."""
    low = inner.lower()
    if _VERSION_LIKE.match(inner) or re.fullmatch(r"[\d.]+", inner):
        return True
    return any(w in low for w in ("version", "array", "feature", "custom"))


def model_from_title(title: str, taxon: str = "") -> str:
    """Platform title -> instrument model string.

    "Illumina NovaSeq 6000 (Homo sapiens)"                 -> "Illumina NovaSeq 6000"
    "Element AVITI (Escherichia coli; Escherichia phage MS2)" -> "Element AVITI"
    "Oxford Nanopore MinION (Escherichia coli K-12)"       -> "Oxford Nanopore MinION"  (strain names carry digits)
    "NanoString CosMx SMI (Human)"                         -> "NanoString CosMx SMI"
    "Xenium In Situ Analyzer: Danio rerio"                 -> "Xenium In Situ Analyzer"
    "Agilent-021169 ... (V4) (Feature Number version)"      -> unchanged (array design detail, kept)

    A trailing parenthetical equal to the GPL `taxon` is always dropped. Otherwise it is dropped when it
    is not a design note (`_is_design_detail`) and is either several words or one alphabetic word.
    """
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if not t:
        return ""
    tx = re.sub(r"\s+", " ", str(taxon or "")).strip()
    if tx:
        for suffix in (f" ({tx})", f": {tx}", f" {tx}"):
            if t.endswith(suffix) and len(t) > len(suffix):
                return t[: -len(suffix)].rstrip(" :")
    m = _TRAILING_PAREN.search(t)
    if m:
        inner = m.group(1).strip()
        words = inner.split()
        if words and not _is_design_detail(inner) and (len(words) >= 2 or inner.isalpha()):
            return t[: m.start()].rstrip(" :")
    return t


def _result(data: Any) -> Tuple[List[str], Dict[str, Any]]:
    """Unpack an esummary JSON body -> (uids, result dict); tolerant of malformed payloads."""
    if not isinstance(data, dict):
        return [], {}
    result = data.get("result")
    if not isinstance(result, dict):
        return [], {}
    uids = result.get("uids")
    if not isinstance(uids, list):
        return [], {}
    return [str(u) for u in uids], result


def _params(api_key: str, **kw: Any) -> Dict[str, Any]:
    p: Dict[str, Any] = {"db": "gds", "retmode": "json"}
    p.update(kw)
    if api_key:
        p["api_key"] = api_key
    return p


# ----------------------------------------------------------------------------- esearch
def _esearchresult(data: Any) -> Optional[Dict[str, Any]]:
    """The `esearchresult` dict of an esearch JSON body, or None when the call failed / payload is malformed."""
    if not isinstance(data, dict):
        return None
    r = data.get("esearchresult")
    return r if isinstance(r, dict) else None


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _esearch_history(http: Http, term: str, api_key: str = "") -> Optional[Tuple[int, str, str]]:
    """-> (count, WebEnv, query_key), or None when the esearch call failed or came back malformed."""
    r = _esearchresult(http.get_json(f"{EUTILS}/esearch.fcgi", params=_params(api_key, term=term, usehistory="y", retmax=0)))
    if r is None:
        return None
    return _int(r.get("count")), str(r.get("webenv") or ""), str(r.get("querykey") or "")


def esearch_history(http: Http, term: str, api_key: str = "") -> Tuple[int, str, str]:
    """-> (count, WebEnv, query_key); (0, '', '') when the call fails or the payload is malformed."""
    return _esearch_history(http, term, api_key) or (0, "", "")


def esearch_uids(http: Http, term: str, api_key: str = "", page: int = ESEARCH_PAGE, max_uids: int = 1000000) -> Tuple[int, Set[str]]:
    """Every uid matching `term` -> (server count, set of uids), paged with retstart/retmax (verified live for >10,000 hits).

    Stops at the server count, on an empty or malformed page, when a page adds no new uids, or at `max_uids`.
    """
    uids: Set[str] = set()
    total = 0
    retstart = 0
    page = max(1, int(page))
    while True:
        r = _esearchresult(http.get_json(f"{EUTILS}/esearch.fcgi", params=_params(api_key, term=term, retstart=retstart, retmax=page)))
        if r is None:
            break
        if retstart == 0:
            total = _int(r.get("count"))
        ids = r.get("idlist")
        if not isinstance(ids, list) or not ids:
            break
        before = len(uids)
        uids.update(str(u) for u in ids)
        retstart += len(ids)
        if len(uids) == before or retstart >= total or len(uids) >= max_uids:
            break
    return total, uids


def count(http: Http, start: str, end: str, api_key: str = "", extra_term: str = "") -> int:
    """Number of GEO Series released in [start, end] (optionally AND-ed with `extra_term`).

    extra_term examples: '"high-throughput sequencing"[PTYP]' (sequencing only), 'GPL24676[ACCN]'.
    """
    r = _esearchresult(http.get_json(f"{EUTILS}/esearch.fcgi", params=_params(api_key, term=_term(start, end, extra_term), retmax=0)))
    return _int(r.get("count")) if r is not None else 0


def search_platforms(http: Http, title_terms: Sequence[str], api_key: str = "", retmax: int = 5000) -> List[str]:
    """GPL accessions whose platform title contains any of `title_terms`, e.g. ['Revio', 'Sequel', 'PacBio', 'Onso'].

    Handy for building a per-vendor series count over time: pass the result to `series_count_by_platform`.
    """
    terms = [t.strip() for t in title_terms if t and t.strip()]
    if not terms:
        return []
    q = " OR ".join(f'"{t}"[TITL]' if " " in t else f"{t}[TITL]" for t in terms)
    r = _esearchresult(http.get_json(f"{EUTILS}/esearch.fcgi", params=_params(api_key, term=f"GPL[ETYP] AND ({q})", retmax=retmax)))
    ids = (r or {}).get("idlist") or []
    out: List[str] = []
    for uid in ids if isinstance(ids, list) else []:
        try:
            n = int(uid) - GPL_UID_BASE
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(f"GPL{n}")
    return out


# ----------------------------------------------------------------------------- platforms (GPL)
def platform_info(
    http: Http,
    gpl_ids: Iterable[Any],
    api_key: str = "",
    batch: int = GPL_BATCH,
    cache: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Dict[str, str]]:
    """Batch esummary of GPL records.

    Returns {'GPL24676': {'accession': 'GPL24676', 'title': 'Illumina NovaSeq 6000 (Homo sapiens)',
    'taxon': 'Homo sapiens', 'technology': 'high-throughput sequencing', 'instrument_model':
    'Illumina NovaSeq 6000', 'released': '2018-03-02'}, ...}. Ids may be 'GPL24676', '24676' or 24676.
    `cache` (accession -> info) is consulted first and filled in place, so a caller can keep one dict
    for a whole run; platforms the server does not know are omitted from the result.
    """
    wanted: List[str] = []
    for g in gpl_ids:
        acc = gpl_accession(g)
        if acc and acc not in wanted:
            wanted.append(acc)
    out: Dict[str, Dict[str, str]] = {}
    todo: List[str] = []
    for acc in wanted:
        if cache is not None and acc in cache:
            out[acc] = cache[acc]
        else:
            todo.append(acc)
    batch = max(1, int(batch))
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        data = http.get_json(f"{EUTILS}/esummary.fcgi", params=_params(api_key, id=",".join(gpl_uid(a) for a in chunk)))
        if _absorb_gpl_page(data, out, cache):
            continue
        # Nothing resolved: the uid = GPL_UID_BASE + number convention may have changed. Recover the real
        # uids by accession (esearch ACCN, verified live) and retry the esummary once.
        real = _gpl_uids_by_accession(http, chunk, api_key)
        if real:
            data = http.get_json(f"{EUTILS}/esummary.fcgi", params=_params(api_key, id=",".join(real)))
            if _absorb_gpl_page(data, out, cache):
                log.warning("GEO: GPL uids recovered by accession for %d ids (%s...); the uid convention may have changed", len(chunk), chunk[0])
                continue
        log.error("GEO: platform lookup failed for %d GPL ids (%s...); their records carry no instrument_model", len(chunk), chunk[0])
    return out


def _absorb_gpl_page(data: Any, out: Dict[str, Dict[str, str]], cache: Optional[Dict[str, Dict[str, str]]]) -> int:
    """Parse one GPL esummary body into `out` (and `cache`); returns how many platforms were resolved."""
    uids, result = _result(data)
    n = 0
    for uid in uids:
        rec = result.get(uid)
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        acc = gpl_accession(rec.get("accession") or rec.get("gpl"))
        if not acc:
            continue
        title = str(rec.get("title") or "").strip()
        taxon = str(rec.get("taxon") or "").strip()
        info = {
            "accession": acc,
            "title": title,
            "taxon": taxon,
            "technology": str(rec.get("ptechtype") or "").strip(),
            "instrument_model": model_from_title(title, taxon),
            "released": _date(rec.get("pdat")),
        }
        out[acc] = info
        if cache is not None:
            cache[acc] = info
        n += 1
    return n


def _gpl_uids_by_accession(http: Http, accessions: Sequence[str], api_key: str = "") -> List[str]:
    """esearch '(GPL24676[ACCN] OR ...) AND GPL[ETYP]' -> the server's uids for those platforms ([] on failure)."""
    accs = [a for a in accessions if a]
    if not accs:
        return []
    term = "(" + " OR ".join(f"{a}[ACCN]" for a in accs) + ") AND GPL[ETYP]"
    r = _esearchresult(http.get_json(f"{EUTILS}/esearch.fcgi", params=_params(api_key, term=term, retmax=len(accs))))
    ids = (r or {}).get("idlist")
    return [str(u) for u in ids] if isinstance(ids, list) else []


def platform_titles(http: Http, gpl_ids: Iterable[Any], api_key: str = "", batch: int = GPL_BATCH, cache: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, str]:
    """{'GPL24676': 'Illumina NovaSeq 6000 (Homo sapiens)', ...} for a list of GPL ids (batched esummary)."""
    return {acc: info["title"] for acc, info in platform_info(http, gpl_ids, api_key=api_key, batch=batch, cache=cache).items()}


# ----------------------------------------------------------------------------- series (GSE)
def _series_from_docsum(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    acc = str(rec.get("accession") or "").strip().upper()
    if not acc.startswith("GSE"):
        gse = str(rec.get("gse") or "").strip()
        acc = f"GSE{gse}" if gse.isdigit() else ""
    if not acc:
        return None
    try:
        n_samples = int(rec.get("n_samples") or 0)
    except (TypeError, ValueError):
        n_samples = 0
    pubmed = rec.get("pubmedids") or []
    return {
        "accession": acc,
        "title": str(rec.get("title") or "").strip(),
        "summary": str(rec.get("summary") or "").strip(),
        "gpls": _split_gpls(rec.get("gpl")),
        "taxon": str(rec.get("taxon") or "").strip(),
        "gdstype": str(rec.get("gdstype") or "").strip(),
        "pdat": _date(rec.get("pdat")),
        "n_samples": n_samples,
        "bioproject": str(rec.get("bioproject") or "").strip(),
        "pubmedids": [str(p) for p in pubmed] if isinstance(pubmed, list) else [],
        "platform_title": str(rec.get("platformtitle") or "").strip(),  # empty on live GSE DocSums
    }


def fetch_series(
    http: Http,
    start: str,
    end: str,
    api_key: str = "",
    max_records: int = 20000,
    batch: int = SERIES_PAGE,
    extra_term: str = "",
) -> Iterator[Dict[str, Any]]:
    """Yield one dict per GEO Series released in [start, end] (see `_series_from_docsum` for keys).

    Uses esearch history + paged esummary. Stops at the esearch count, at an empty page, or at `max_records`.
    The module-level `last_sweep` records total vs fetched; `sweep_error()` is non-empty when the window was
    only partially swept (failed esearch, lost history, or a page that never came back), so a caller can
    report partial coverage instead of silently storing a truncated week.
    """
    last_sweep.clear()
    last_sweep.update({"start": start, "end": end, "total": 0, "fetched": 0, "complete": False, "error": ""})
    hist = _esearch_history(http, _term(start, end, extra_term), api_key)
    if hist is None:
        last_sweep["error"] = f"geo: esearch failed for {start}..{end}"
        log.error("GEO: esearch failed for %s..%s", start, end)
        return
    total, webenv, qk = hist
    last_sweep["total"] = total
    if not total:
        last_sweep["complete"] = True
        log.info("GEO: no series released %s..%s", start, end)
        return
    if not webenv or not qk:
        last_sweep["error"] = f"geo: esearch found {total} series but returned no history for {start}..{end}"
        log.error("GEO: esearch found %d series but returned no history for %s..%s", total, start, end)
        return
    todo = min(total, max_records)
    if todo < total:
        log.warning("GEO: sweeping %d of %d series (max_records cap)", todo, total)
    log.info("GEO: %d series released %s..%s", total, start, end)
    retstart = 0
    fetched = 0
    batch = max(1, int(batch))
    while retstart < todo:
        params = _params(api_key, WebEnv=webenv, query_key=qk, retstart=retstart, retmax=min(batch, todo - retstart))
        data = http.get_json(f"{EUTILS}/esummary.fcgi", params=params)
        uids, result = _result(data)
        if not uids:
            log.warning("GEO: esummary returned no records at retstart=%d", retstart)
            break
        fetched += len(uids)
        last_sweep["fetched"] = fetched
        for uid in uids:
            rec = result.get(uid)
            if not isinstance(rec, dict) or rec.get("error"):
                continue
            s = _series_from_docsum(rec)
            if s:
                yield s
        retstart += len(uids)
    if fetched < todo:
        last_sweep["error"] = f"geo: partial sweep {fetched}/{todo} series for {start}..{end}"
        log.error("GEO: partial sweep %d/%d series for %s..%s", fetched, todo, start, end)
    else:
        last_sweep["complete"] = True


def sweep_error() -> str:
    """'' when the most recent fetch_series()/fetch() sweep was complete, else one line for an error log."""
    return str(last_sweep.get("error") or "")


def fetch(
    http: Http,
    start: str,
    end: str,
    api_key: str = "",
    max_records: int = 20000,
    gpl_cache: Optional[Dict[str, Dict[str, str]]] = None,
    extra_term: str = "",
) -> Iterator[ArchiveRecord]:
    """Yield one ArchiveRecord(archive='geo') per (Series, Platform) released in [start, end].

    Platform titles are resolved page by page with batched GPL lookups; pass a dict as `gpl_cache`
    to reuse titles across calls (GPL metadata is stable). `extra_term` narrows the esearch, e.g.
    '"high-throughput sequencing"[PTYP]' to skip microarray series.
    """
    cache: Dict[str, Dict[str, str]] = gpl_cache if gpl_cache is not None else {}
    page: List[Dict[str, Any]] = []
    unresolved = 0

    def flush() -> Iterator[ArchiveRecord]:
        nonlocal unresolved
        gpls = [g for s in page for g in s["gpls"]]
        info = platform_info(http, gpls, api_key=api_key, cache=cache)
        for s in page:
            for gpl in s["gpls"] or [""]:
                pi = info.get(gpl) or {}
                if gpl and not pi:
                    unresolved += 1
                yield ArchiveRecord(
                    archive="geo",
                    accession=f"{s['accession']}/{gpl}" if gpl else s["accession"],
                    study_accession=s["accession"],
                    instrument_model=pi.get("instrument_model") or "",
                    platform=pi.get("technology") or "",
                    library_strategy=s["gdstype"],
                    first_public=s["pdat"] or start,
                    center_name="",
                    study_title=s["title"],
                    organism=s["taxon"],
                )
        page.clear()

    for s in fetch_series(http, start, end, api_key=api_key, max_records=max_records, extra_term=extra_term):
        page.append(s)
        if len(page) >= SERIES_PAGE:
            yield from flush()
    if page:
        yield from flush()
    if unresolved:
        log.warning("GEO: %d series/platform pairs left without a platform title", unresolved)


# ----------------------------------------------------------------------------- summaries
def series_uids_by_platform(
    http: Http, gpl_ids: Iterable[Any], start: str, end: str, api_key: str = "", chunk: int = ACCN_CHUNK, extra_term: str = ""
) -> Set[str]:
    """uids of the Series released in [start, end] that ran on ANY of `gpl_ids` (esearch GPLnnn[ACCN] OR ...).

    `GSE[ETYP] AND GPL24676[ACCN] AND <PDAT range>` restricts to series on that platform (see
    docs/sources/geo.md for the live check). Long id lists are OR-ed in chunks of `chunk` accessions; each
    chunk is paged with `esearch_uids` and the uids are unioned, so a series on two platforms of the same
    family is counted once. GSE uid = 200000000 + series number ("200351130" -> GSE351130).
    """
    accs: List[str] = []
    for g in gpl_ids:
        acc = gpl_accession(g)
        if acc and acc not in accs:
            accs.append(acc)
    uids: Set[str] = set()
    chunk = max(1, int(chunk))
    for i in range(0, len(accs), chunk):
        ors = "(" + " OR ".join(f"{a}[ACCN]" for a in accs[i : i + chunk]) + ")"
        term = _term(start, end, f"{ors} AND {extra_term}" if extra_term else ors)
        uids |= esearch_uids(http, term, api_key=api_key)[1]
    return uids


def series_count_by_platform(
    http: Http, gpl_ids: Iterable[Any], start: str, end: str, api_key: str = "", chunk: int = ACCN_CHUNK, extra_term: str = ""
) -> int:
    """Number of DISTINCT Series released in [start, end] on any of `gpl_ids` (see `series_uids_by_platform`).

    A vendor's whole platform family (from `search_platforms`) therefore gives one de-duplicated count per
    period with a few esearch calls and no DocSum sweep.
    """
    return len(series_uids_by_platform(http, gpl_ids, start, end, api_key=api_key, chunk=chunk, extra_term=extra_term))


def series_counts_by_window(
    http: Http, gpl_ids: Iterable[Any], windows: Sequence[Tuple[str, str]], api_key: str = "", extra_term: str = ""
) -> List[Dict[str, Any]]:
    """[{'start', 'end', 'series'}, ...]: `series_count_by_platform` for each (start, end) window, e.g. one per year."""
    accs = [a for a in (gpl_accession(g) for g in gpl_ids) if a]
    return [
        {"start": s, "end": e, "series": series_count_by_platform(http, accs, s, e, api_key=api_key, extra_term=extra_term)}
        for s, e in windows
    ]


def summarize_window(http: Http, start: str, end: str, api_key: str = "", top: int = 10, max_records: int = 20000) -> Dict[str, Any]:
    """Live-test / CLI helper: series count plus the most common platform titles and instrument models."""
    titles: Counter = Counter()
    models: Counter = Counter()
    technologies: Counter = Counter()
    cache: Dict[str, Dict[str, str]] = {}
    n = 0
    for r in fetch(http, start, end, api_key=api_key, max_records=max_records, gpl_cache=cache):
        n += 1
        gpl = r.accession.split("/", 1)[1] if "/" in r.accession else ""
        titles[(cache.get(gpl) or {}).get("title") or "(unresolved)"] += 1
        models[r.instrument_model or "(unresolved)"] += 1
        technologies[r.platform or "(unresolved)"] += 1
    return {
        "start": start,
        "end": end,
        "records": n,  # (series, platform) pairs; distinct series is the esearch count below
        "series": count(http, start, end, api_key=api_key),
        "platforms_resolved": len(cache),
        "top_platform_titles": titles.most_common(top),
        "top_instrument_models": models.most_common(top),
        "technology_types": technologies.most_common(),
    }
