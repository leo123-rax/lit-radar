"""Instrument usage time series: monthly counts per instrument model from NCBI SRA, ENA and Europe PMC.

The question this answers: "how many PacBio (or Nanopore, Illumina, ...) experiments and papers show
up over time, by model?" It counts *deposited SRA experiments* (sra), *ENA experiment-run rows, i.e. runs*
(ena) and *open-access papers naming the model in their Methods* (epmc). It never counts installed
machines; see docs/sources/timeseries.md for the caveats (deposit lag, clinical and commercial sequencing
that is never deposited, SRA Properties index vs esummary differences, runs vs experiments).

Units differ by source and are written into every output (CSV `unit` column, Markdown headers):
  sra   experiments   one SRA experiment = one library on one instrument (SRX accession)
  ena   runs          ENA's `read_experiment` count endpoint returns one row per experiment-RUN pair
                      (verified live 2026-10-17: study PRJNA1152220 counts 38,728 with result=read_experiment
                      and 38,728 with result=read_run, while it holds 341 distinct experiments), so a
                      multi-lane project inflates ENA against SRA by the runs-per-experiment ratio
  epmc  papers        hitCount of papers whose Methods name any configured term

Failures are kept apart from zeros: a count request that fails (HTTP status, no answer, unexpected payload)
is reported under `backfill.failures` with its reason and never cached; the run's `errors` (exit 1) list
only sources where EVERY request of the run failed, partial failures are `warnings`.

One series per (source, entity): an entity's archive strings (`sra`, `ena` in config/archive_models.yaml)
are OR-ed into ONE count request per month, exactly as its Europe PMC terms already were, so the request
budget is one request per entity per source per month whatever the vocabulary looks like (ilmn-hiseq
lists nine HiSeq strings and costs the same as Revio). The series key (`model` column) is the pipe-joined
string list ("sequel ii|sequel iie"), which for a single string is the string itself. Verified live on
2026-10-18 for 2025-06: SRA `("instrument sequel ii"[Properties] OR "instrument sequel iie"[Properties])`
gave 5,139 = 3,474 + 1,665 (the two single-string counts), the querytranslation kept both [Properties]
clauses, and a phrase with no records inside the OR is dropped from the translation and listed under
warninglist.quotedphrasesnotfound while the other clauses stand; ENA
`(instrument_model="Sequel II" OR instrument_model="Sequel IIe")` gave 5,190 = 3,493 + 1,697. Whole-string
matching makes the strings disjoint, so the union equals the sum. A cache written per string by an
earlier version is folded into entity rows by `consolidate_cache` (sum of the strings' counts for every
month where all of them are cached; the per-string rows are then pruned so a SUM over the table never
double-counts) - no request is spent on history.

Every (source, entity, model, month) count is cached in the `timeseries` table of the radar database,
so backfills are incremental: a month is fetched again only when it is missing, when it is recent
enough that its count is still growing (the current month plus the `refresh_months` months before it),
or with `force=True`. Each stored count is committed on its own, so an interrupted backfill keeps what it
fetched, and a fresh 0 never replaces a positive cached count unless `force=True` (deposits are not
withdrawn wholesale; that pattern means the archive renamed or dropped the vocabulary string).

Endpoints, all verified live on 2026-10-17 (full record in docs/sources/timeseries.md):

NCBI SRA, E-utilities esearch (docs: https://www.ncbi.nlm.nih.gov/books/NBK25499/)
  GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=sra&retmax=0&retmode=json
      &term=2025/09/01:2025/09/30[PDAT] AND "instrument revio"[Properties]
  -> {"header": {...}, "esearchresult": {"count": "851", "retmax": "0", "retstart": "0", "idlist": [], ...}}
  The Properties phrase is "instrument " + SRA's own instrument_model spelling in lower case. It matches
  the whole string: "instrument illumina novaseq x" gives 18,126 with or without
  `NOT "instrument illumina novaseq x plus"[Properties]`, and 500/500 sampled ExpXml records for that
  query read exactly instrument_model="Illumina NovaSeq X". A string with no records returns count "0"
  plus warninglist.quotedphrasesnotfound (querytranslation still carries the [Properties] clause).
  A misspelt field tag is NOT an error: "instrument revio"[Propertiez] returns HTTP 200, the same count,
  a querytranslation ending in [All Fields] and an `errorlist` key. sra_count() therefore accepts a count
  only when `errorlist` is absent and `querytranslation` still contains "[Properties]"; anything else is a
  failed call (None), never cached. Pacing: 3 requests/s without NCBI_API_KEY, 10/s with one (the shared
  Http client enforces this; never run two NCBI loops at once, that is what earns a 429).

ENA portal API, count endpoint (docs: https://www.ebi.ac.uk/ena/portal/api/ and
  https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/advanced-search.html)
  GET https://www.ebi.ac.uk/ena/portal/api/count?result=read_experiment&format=json
      &query=first_public>=2024-01-01 AND first_public<=2024-01-31 AND instrument_model="Revio"
  -> {"count":"38"}       (without format=json the body is text/plain "count\\n38"; the number is a string)
  instrument_model values must be exact vocabulary strings of SRA.common.xsd ("Revio", "Sequel IIe",
  "Illumina NovaSeq X Plus", "PromethION", ...), quoted or unquoted. Matching is case-insensitive
  ("revio" and "REVIO" also give 38); "PacBio Revio" is not a vocabulary value and returns 0. The count is
  of experiment-run ROWS (one per run), not of distinct experiments: see the unit table above.

Europe PMC REST search (docs: https://europepmc.org/RestfulWebService , https://europepmc.org/searchsyntax)
  GET https://www.ebi.ac.uk/europepmc/webservices/rest/search?format=json&resultType=idlist&pageSize=1
      &query=(METHODS:"Revio" OR METHODS:"Sequel II") AND (FIRST_PDATE:[2024-01-01 TO 2024-01-31])
  -> {"version": "6.9", "hitCount": 117, "nextCursorMark": "...", "resultList": {"result": [{"id": "38168995", ...}]}}
  hitCount is the number of papers whose Methods match any term (papers, not mentions). Phrase search
  is token-prefix inclusive ("NovaSeq X" also matches "NovaSeq X Plus").

No new hosts: eutils.ncbi.nlm.nih.gov and www.ebi.ac.uk already have DEFAULT_MIN_INTERVAL entries in
litradar/http.py.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import logging
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import yaml

from .http import Http
from .sources.sra import usable_api_key
from .store import Store

log = logging.getLogger(__name__)

EUTILS_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ENA_COUNT = "https://www.ebi.ac.uk/ena/portal/api/count"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SOURCES: Tuple[str, ...] = ("sra", "ena", "epmc")
# what one count means per source; written into the CSV `unit` column and the Markdown headers
UNITS: Dict[str, str] = {"sra": "experiments", "ena": "runs", "epmc": "papers"}
UNIT_NOTES: Dict[str, str] = {
    "sra": "SRA experiments by instrument_model",
    "ena": "ENA runs (experiment-run rows) by instrument_model",
    "epmc": "open-access papers naming the model in Methods",
}
DEFAULT_REFRESH_MONTHS = 2
DEFAULT_SPAN_MONTHS = 36
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")

# Categorical palette in fixed slot order (identity colours follow the series, never its rank).
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]


# ---- months ---------------------------------------------------------------------------------
def parse_month(s: str) -> Tuple[int, int]:
    m = MONTH_RE.match((s or "").strip())
    if not m:
        raise ValueError(f"month must be YYYY-MM, got {s!r}")
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        raise ValueError(f"month out of range: {s!r}")
    return y, mo


def shift_month(month: str, delta: int) -> str:
    y, m = parse_month(month)
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_range(start: str, end: str) -> List[str]:
    """Inclusive list of YYYY-MM strings from start to end."""
    y0, m0 = parse_month(start)
    y1, m1 = parse_month(end)
    if (y1, m1) < (y0, m0):
        raise ValueError(f"--to {end} is before --from {start}")
    out: List[str] = []
    cur = f"{y0:04d}-{m0:02d}"
    while True:
        out.append(cur)
        if cur == f"{y1:04d}-{m1:02d}":
            return out
        cur = shift_month(cur, 1)


def month_bounds(month: str) -> Tuple[str, str]:
    """First and last ISO date of a month (both inclusive)."""
    y, m = parse_month(month)
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


# ---- configuration --------------------------------------------------------------------------
@dataclass
class ModelSpec:
    entity_id: str
    vendor: str = ""
    sra: List[str] = field(default_factory=list)
    ena: List[str] = field(default_factory=list)
    epmc_terms: List[str] = field(default_factory=list)
    proposed: bool = False
    notes: str = ""
    launch: str = ""  # YYYY-MM the model was announced; months before it are stored as 0 without a request

    def terms_for(self, source: str) -> List[str]:
        """The strings OR-ed into one count request per month for this source (empty = no series)."""
        if source == "sra":
            return [s for s in self.sra if s]
        if source == "ena":
            return [s for s in self.ena if s]
        if source == "epmc":
            return [t for t in self.epmc_terms if t]
        return []

    def series_key(self, source: str) -> str:
        """The `model` column of this entity's series for a source: its strings pipe-joined ('' = no series)."""
        return series_label_for(self.terms_for(source))

    def models_for(self, source: str) -> List[str]:
        """The series keys this entity has for a source: one (its OR-ed string set) or none."""
        key = self.series_key(source)
        return [key] if key else []


def series_label_for(terms: Sequence[str]) -> str:
    """The `model` column for a series: the OR-ed string set, pipe-joined (a single string is itself)."""
    return "|".join(t for t in terms if t)


epmc_label = series_label_for  # the Europe PMC name this convention started under


def _str_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v is not None and str(v) != ""]


def load_archive_models(path: Path) -> Dict[str, ModelSpec]:
    """entity_id -> ModelSpec from config/archive_models.yaml (empty when the file is missing)."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    entities = data.get("entities") if isinstance(data, dict) else None
    out: Dict[str, ModelSpec] = OrderedDict()
    for eid, raw in (entities or {}).items():
        raw = raw or {}
        if not isinstance(raw, dict):
            log.warning("archive_models: entry %s is not a mapping; skipped", eid)
            continue
        launch = str(raw.get("launch") or "").strip()
        if launch and not MONTH_RE.match(launch):
            log.warning("archive_models: %s has launch=%r (expected YYYY-MM); ignored", eid, launch)
            launch = ""
        out[str(eid)] = ModelSpec(
            entity_id=str(eid),
            vendor=str(raw.get("vendor") or ""),
            sra=[s.lower() for s in _str_list(raw.get("sra"))],
            ena=_str_list(raw.get("ena")),
            epmc_terms=_str_list(raw.get("epmc_terms")),
            proposed=bool(raw.get("proposed", False)),
            notes=str(raw.get("notes") or ""),
            launch=launch,
        )
    return out


def load_instrument_index(path: Path) -> Dict[str, Dict[str, str]]:
    """id -> {name, vendor, ticker} from config/instruments.yaml (empty when the file is missing)."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out: Dict[str, Dict[str, str]] = {}
    for e in (data.get("entries") if isinstance(data, dict) else None) or []:
        if isinstance(e, dict) and e.get("id"):
            out[str(e["id"])] = {"name": str(e.get("name") or ""), "vendor": str(e.get("vendor") or ""), "ticker": str(e.get("ticker") or "")}
    return out


def check_models(specs: Dict[str, ModelSpec], instruments: Dict[str, Dict[str, str]]) -> List[str]:
    """Human-readable warnings: ids missing from the gazetteer, stale `proposed` flags, upper-case SRA strings."""
    warnings: List[str] = []
    for eid, spec in specs.items():
        if instruments and eid not in instruments:
            tag = "proposed, add the gazetteer entry" if spec.proposed else "NOT in instruments.yaml"
            warnings.append(f"{eid}: {tag}")
        elif instruments and spec.proposed:
            warnings.append(f"{eid}: is in instruments.yaml now; drop `proposed: true` from archive_models.yaml")
        for s in spec.sra:
            if s != s.lower():
                warnings.append(f"{eid}: sra string {s!r} must be lower case")
    return warnings


def expand_vendor(token: str, instruments: Dict[str, Dict[str, str]], specs: Dict[str, ModelSpec]) -> List[str]:
    """Entity ids (present in archive_models) for a vendor given as id prefix ('pacb'), ticker ('PACB'),
    vendor name ('Pacific Biosciences') or a word of it ('nanopore', 'pacific'). A partial token must be at
    least three characters and start a word of the vendor name, so 'i' or 'e' select nothing."""
    t = (token or "").strip().lower()
    if not t:
        return []
    word = re.compile(r"\b" + re.escape(t)) if len(t) >= 3 else None
    out: List[str] = []
    for eid, spec in specs.items():
        meta = instruments.get(eid, {})
        vendor = (meta.get("vendor") or spec.vendor or "").lower()
        ticker = (meta.get("ticker") or "").lower()
        prefix = eid.split("-", 1)[0].lower()
        if t in (prefix, ticker) or (vendor and (t == vendor or (word is not None and word.search(vendor)))):
            out.append(eid)
    return out


def resolve_entities(
    entities: Sequence[str],
    vendors: Sequence[str],
    instruments: Dict[str, Dict[str, str]],
    specs: Dict[str, ModelSpec],
    all_entities: bool = False,
) -> Tuple[List[str], List[str]]:
    """Explicit --entity ids plus --vendor expansions, in config order; unknown ids become warnings.
    `all_entities` (--all) selects every entity in archive_models.yaml; --entity / --vendor given with it
    are redundant and noted as a warning, never an error."""
    wanted: List[str] = []
    warnings: List[str] = []
    if all_entities:
        if entities or vendors:
            warnings.append("--all selects every entity in archive_models.yaml; --entity / --vendor were ignored")
        return list(specs), warnings
    for e in entities:
        if e in specs:
            wanted.append(e)
        else:
            warnings.append(f"entity {e!r} has no entry in archive_models.yaml")
    for v in vendors:
        ids = expand_vendor(v, instruments, specs)
        if not ids:
            warnings.append(f"vendor {v!r} matched no entity with archive models")
        wanted.extend(ids)
    ordered = [eid for eid in specs if eid in set(wanted)]
    return ordered, warnings


# ---- count calls ----------------------------------------------------------------------------
def _to_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


Models = Union[str, Sequence[str]]


def _model_list(models: Models) -> List[str]:
    """One string or a sequence of strings -> the non-empty list of strings (order kept, duplicates dropped)."""
    items = [models] if isinstance(models, str) else list(models or [])
    return list(OrderedDict.fromkeys(str(m) for m in items if m is not None and str(m) != ""))


def sra_term(models: Models, start: str, end: str) -> str:
    """esearch term: the month as a [PDAT] range AND the instrument string(s) as whole-value [Properties]
    phrases; several strings are OR-ed inside parentheses (one request per entity, see the module doc)."""
    phrases = [f'"instrument {m.lower()}"[Properties]' for m in _model_list(models)]
    clause = phrases[0] if len(phrases) == 1 else "(" + " OR ".join(phrases) + ")"
    return f'{start.replace("-", "/")}:{end.replace("-", "/")}[PDAT] AND {clause}'


NO_ANSWER = "no answer (network failure, retries exhausted or host rate-limited)"


def _get_json_detail(http: Http, url: str, params: Dict[str, Any]) -> Tuple[Any, str]:
    """(parsed JSON or None, failure reason). Uses Http.get when the client has it so an HTTP status can be
    named; a client that only offers get_json (tests, thin wrappers) yields 'no answer' on None."""
    getter = getattr(http, "get", None)
    if callable(getter):
        resp = getter(url, params=params, headers={"Accept": "application/json"})
        if resp is None:
            return None, NO_ANSWER
        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            body = " ".join((getattr(resp, "text", "") or "").split())[:120]
            return None, f"HTTP {status}" + (f": {body}" if body else "")
        try:
            return resp.json(), ""
        except ValueError:
            return None, "non-JSON body"
    data = http.get_json(url, params=params)
    if data is None:
        return None, NO_ANSWER
    return data, ""


def sra_count_detail(http: Http, model: Models, start: str, end: str, api_key: str = "") -> Tuple[Optional[int], str]:
    """(experiments with the given instrument_model(s) made public in [start, end], or None, failure reason).

    A count is accepted only when esearch really evaluated the Properties clause: NCBI answers a misspelt
    field tag with HTTP 200, a count, an `errorlist` and a querytranslation rewritten to [All Fields], and
    that number is not a Properties count. A zero-deposit string keeps the [Properties] clause and adds
    warninglist.quotedphrasesnotfound; that is a genuine 0 (inside an OR of several strings the empty phrase
    is dropped from the translation and the remaining clauses stand, verified live 2026-10-18)."""
    if not _model_list(model):
        return None, "no SRA instrument strings configured"
    params: Dict[str, Any] = {"db": "sra", "term": sra_term(model, start, end), "retmax": 0, "retmode": "json"}
    key = usable_api_key(api_key)
    if key:
        params["api_key"] = key
    data, reason = _get_json_detail(http, EUTILS_ESEARCH, params)
    if reason:
        return None, reason
    if not isinstance(data, dict):
        return None, "unexpected payload (not a JSON object)"
    if data.get("error"):
        return None, f"NCBI error: {str(data['error'])[:120]}"
    result = data.get("esearchresult")
    if not isinstance(result, dict) or "count" not in result:
        return None, "unexpected payload (no esearchresult.count)"
    if result.get("errorlist"):
        log.warning("SRA esearch reported errorlist %s for %r", result.get("errorlist"), params["term"])
        return None, f"esearch errorlist {result.get('errorlist')}"
    translation = str(result.get("querytranslation") or "")
    if "[Properties]" not in translation:
        log.warning("SRA esearch did not evaluate the Properties clause (querytranslation=%r)", translation)
        return None, f"Properties clause not evaluated (querytranslation={translation[:80]!r})"
    n = _to_int(result.get("count"))
    return (n, "") if n is not None else (None, f"non-numeric count {result.get('count')!r}")


def sra_count(http: Http, model: Models, start: str, end: str, api_key: str = "") -> Optional[int]:
    """Experiments with the given instrument_model(s) made public in [start, end]; None on failure (see *_detail)."""
    return sra_count_detail(http, model, start, end, api_key)[0]


def ena_query(models: Models, start: str, end: str) -> str:
    """ENA portal query: the first_public range AND the instrument_model value(s); several values are OR-ed
    inside parentheses (verified live 2026-10-18: the union equals the sum of the single-value counts)."""
    values = [f'instrument_model="{m}"' for m in _model_list(models)]
    clause = values[0] if len(values) == 1 else "(" + " OR ".join(values) + ")"
    return f"first_public>={start} AND first_public<={end} AND {clause}"


def ena_count_detail(http: Http, model: Models, start: str, end: str) -> Tuple[Optional[int], str]:
    """(ENA read_experiment rows, i.e. RUNS, with instrument_model in the given value(s) and first_public in
    [start, end], or None, failure reason). The endpoint counts experiment-run pairs, not distinct experiments."""
    if not _model_list(model):
        return None, "no ENA instrument_model values configured"
    data, reason = _get_json_detail(http, ENA_COUNT, {"result": "read_experiment", "query": ena_query(model, start, end), "format": "json"})
    if reason:
        return None, reason
    if not isinstance(data, dict):
        return None, "unexpected payload (not a JSON object)"
    n = _to_int(data.get("count"))
    return (n, "") if n is not None else (None, f"non-numeric count {data.get('count')!r}")


def ena_count(http: Http, model: Models, start: str, end: str) -> Optional[int]:
    """ENA runs (experiment-run rows) with instrument_model in the value(s) and first_public in [start, end]; None on failure."""
    return ena_count_detail(http, model, start, end)[0]


def epmc_query(terms: Sequence[str], start: str, end: str) -> str:
    term_part = " OR ".join(f'METHODS:"{t}"' for t in terms if t)
    return f"({term_part}) AND (FIRST_PDATE:[{start} TO {end}])"


def epmc_count_detail(http: Http, terms: Sequence[str], start: str, end: str) -> Tuple[Optional[int], str]:
    """(papers first published in [start, end] whose Methods name any of the terms, or None, failure reason)."""
    terms = [t for t in terms if t]
    if not terms:
        return None, "no Europe PMC terms configured"
    data, reason = _get_json_detail(http, EPMC_SEARCH, {"query": epmc_query(terms, start, end), "format": "json", "resultType": "idlist", "pageSize": 1})
    if reason:
        return None, reason
    if not isinstance(data, dict) or "hitCount" not in data:
        return None, "unexpected payload (no hitCount)"
    n = _to_int(data.get("hitCount"))
    return (n, "") if n is not None else (None, f"non-numeric hitCount {data.get('hitCount')!r}")


def epmc_count(http: Http, terms: Sequence[str], start: str, end: str) -> Optional[int]:
    """Papers first published in [start, end] whose Methods name any of the terms; None on failure."""
    return epmc_count_detail(http, terms, start, end)[0]


# ---- cache layout: one row per entity, not per vocabulary string -----------------------------
def consolidate_cache(
    store: Store,
    specs: Dict[str, ModelSpec],
    entity_ids: Sequence[str],
    sources: Sequence[str] = SOURCES,
    apply: bool = True,
) -> Dict[str, Any]:
    """Fold per-string cache rows (written by the one-request-per-string version of this module) into the
    entity's series row, without a single request.

    For every selected entity with two or more strings for a source, the entity row for a month is the SUM
    of the strings' cached counts (whole-string matching makes the strings disjoint, so the sum is what the
    OR-ed request returns; verified live, see the module doc). A month is derived only when EVERY string of
    the current configuration is cached for it; a month with some strings missing is left to the backfill
    (`partial`). An entity row that already exists is kept (it is at least as fresh). With `apply=True` the
    per-string rows are then deleted in the same transaction as the derived rows are written, so a SUM over
    the table never counts a month twice; with `apply=False` (dry run) nothing is written and the rows that
    WOULD be derived come back under `virtual` for the planner. Single-string entities need nothing: their
    series key is the string itself. Rows under strings no longer configured are not touched (they never
    reach the outputs; prune them by hand, as before)."""
    report: Dict[str, Any] = {"derived": 0, "pruned": 0, "partial": 0, "entities": [], "virtual": {}}
    for eid in entity_ids:
        spec = specs.get(eid)
        if not spec:
            continue
        for source in sources:
            terms = spec.terms_for(source)
            if source == "epmc" or len(terms) < 2:
                continue  # Europe PMC always was one row per entity; one string is its own key
            key = series_label_for(terms)
            rows = store.timeseries_rows(sources=[source], entity_ids=[eid])
            per_month: Dict[str, Dict[str, int]] = {}
            have_entity_row: Set[str] = set()
            for r in rows:
                if r["model"] == key:
                    have_entity_row.add(str(r["month"]))
                elif r["model"] in terms:
                    per_month.setdefault(str(r["month"]), {})[str(r["model"])] = int(r["count"])
            if not per_month:
                continue
            derived = 0
            for month in sorted(per_month):
                counts = per_month[month]
                if month in have_entity_row:
                    continue
                if set(counts) != set(terms):
                    report["partial"] += 1
                    continue
                total = sum(counts.values())
                report["virtual"][(source, eid, key, month)] = total
                if apply:
                    store.put_timeseries_count(source, eid, key, month, total)
                derived += 1
            pruned = sum(len(c) for c in per_month.values())
            if apply:
                for term in terms:
                    store.conn.execute("DELETE FROM timeseries WHERE source=? AND entity_id=? AND model=?", (source, eid, term))
                store.commit()  # derived rows and the deletion land together
            report["derived"] += derived
            report["pruned"] += pruned
            report["entities"].append({"entity_id": eid, "source": source, "derived": derived, "pruned": pruned})
    return report


# ---- backfill -------------------------------------------------------------------------------
@dataclass
class Job:
    source: str
    entity_id: str
    model: str   # the series key: the entity's strings for this source, pipe-joined
    month: str
    terms: List[str] = field(default_factory=list)  # the strings OR-ed into the one request (every source)


def plan_jobs(specs: Dict[str, ModelSpec], entity_ids: Sequence[str], months: Sequence[str], sources: Sequence[str]) -> List[Job]:
    """One job per (source, entity, month): an entity with several archive strings is ONE request per month."""
    jobs: List[Job] = []
    for source in sources:
        for eid in entity_ids:
            spec = specs.get(eid)
            if not spec:
                continue
            terms = spec.terms_for(source)
            if not terms:
                continue
            key = series_label_for(terms)
            for month in months:
                jobs.append(Job(source, eid, key, month, list(terms)))
    return jobs


def refresh_cutoff(today: date, refresh_months: int) -> str:
    """Months >= this cutoff are fetched again even when cached (their counts are still growing): the
    current month plus the `refresh_months` calendar months before it (2 on 2026-10-17 -> 2026-08)."""
    return shift_month(today.strftime("%Y-%m"), -max(0, int(refresh_months)))


def job_key(job: Job) -> str:
    return f"{job.source}:{job.entity_id}:{job.model}:{job.month}"


def fetch_count_detail(http: Http, job: Job, api_key: str = "") -> Tuple[Optional[int], str]:
    """(one live count for a job or None, failure reason); dispatches on job.source."""
    start, end = month_bounds(job.month)
    terms = job.terms or [job.model]
    if job.source == "sra":
        return sra_count_detail(http, terms, start, end, api_key)
    if job.source == "ena":
        return ena_count_detail(http, terms, start, end)
    if job.source == "epmc":
        return epmc_count_detail(http, terms, start, end)
    return None, f"unknown source {job.source!r}"


def fetch_count(http: Http, job: Job, api_key: str = "") -> Optional[int]:
    """One live count for a job (None on failure); dispatches on job.source."""
    return fetch_count_detail(http, job, api_key)[0]


def backfill(
    http: Http,
    store: Store,
    jobs: Sequence[Job],
    refresh_months: int = DEFAULT_REFRESH_MONTHS,
    force: bool = False,
    today: Optional[date] = None,
    api_key: str = "",
    dry_run: bool = False,
    launches: Optional[Dict[str, str]] = None,
    virtual_cache: Optional[Dict[Tuple[str, str, str, str], int]] = None,
) -> Dict[str, Any]:
    """Fetch and cache every job's count, committing each stored value on its own so an interrupted run
    (Ctrl-C, crash, exception) keeps everything fetched so far. Cached months older than the refresh
    window are skipped unless force=True. Failed calls are reported under `failed` (keys) and `failures`
    ([{key, reason}], the HTTP status or cause) and never cached (an older cached value survives). A fresh 0
    for a key whose cached count is positive is reported under `kept` and NOT stored unless force=True:
    public deposits are not withdrawn wholesale, so that pattern means the archive renamed or dropped the
    vocabulary string (SRA then also reports warninglist.quotedphrasesnotfound), and overwriting history
    with zeros would hide it. `launches` (entity_id -> YYYY-MM) stores 0 without a request for months before
    a model was announced (`pre_launch` in the report): nothing can name or deposit from an instrument that
    did not exist yet, and Europe PMC phrase matches before the launch were only ever false positives.
    `virtual_cache` ((source, entity_id, model, month) -> count) is consulted before the store: a dry run
    passes the rows `consolidate_cache` WOULD derive, so the plan it prints is the plan the real run follows."""
    today = today or date.today()
    cutoff = refresh_cutoff(today, refresh_months)
    launches = launches or {}
    virtual_cache = virtual_cache or {}
    report: Dict[str, Any] = {"jobs": len(jobs), "requests": 0, "cached": 0, "pre_launch": 0, "failed": [], "failures": [], "kept": [],
                              "by_source": {}, "cutoff": cutoff}
    try:
        for job in jobs:
            bs = report["by_source"].setdefault(job.source, {"requests": 0, "cached": 0, "failed": 0, "kept": 0, "pre_launch": 0})
            cached: Optional[Dict[str, Any]] = store.get_timeseries_count(job.source, job.entity_id, job.model, job.month)
            if cached is None and (job.source, job.entity_id, job.model, job.month) in virtual_cache:
                cached = {"count": virtual_cache[(job.source, job.entity_id, job.model, job.month)]}
            if cached is not None and not force and job.month < cutoff:
                report["cached"] += 1
                bs["cached"] += 1
                continue
            launch = launches.get(job.entity_id, "")
            if launch and job.month < launch:
                report["pre_launch"] += 1
                bs["pre_launch"] += 1
                if not dry_run and (cached is None or force or int(cached["count"]) != 0):
                    store.put_timeseries_count(job.source, job.entity_id, job.model, job.month, 0)
                    store.commit()
                continue
            if dry_run:
                report["requests"] += 1
                bs["requests"] += 1
                continue
            n, reason = fetch_count_detail(http, job, api_key)
            report["requests"] += 1
            bs["requests"] += 1
            if n is None:
                report["failed"].append(job_key(job))
                report["failures"].append({"key": job_key(job), "source": job.source, "entity_id": job.entity_id, "model": job.model,
                                           "month": job.month, "reason": reason or "unknown"})
                bs["failed"] += 1
                log.warning("timeseries: %s failed: %s", job_key(job), reason or "unknown")
                continue
            if n == 0 and not force and cached is not None and int(cached["count"]) > 0:
                log.warning(
                    "timeseries: %s returned 0 for %s but %d is cached; keeping the cached value (--force overwrites)",
                    job.source, job_key(job), int(cached["count"]),
                )
                report["kept"].append(job_key(job))
                bs["kept"] += 1
                continue
            store.put_timeseries_count(job.source, job.entity_id, job.model, job.month, n)
            store.commit()  # one count per transaction: a commit costs far less than the 0.34 s pacing
    finally:
        store.commit()
    return report


# ---- outputs --------------------------------------------------------------------------------
CSV_COLUMNS = ("source", "entity_id", "model", "month", "count", "unit")


def write_csv(rows: Sequence[Dict[str, Any]], path: Path) -> None:
    """Long format: source,entity_id,model,month,count,unit (one row per cached count; unit = experiments for
    sra, runs for ena, papers for epmc)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([r.get(c, UNITS.get(str(r.get("source")), "") if c == "unit" else "") for c in CSV_COLUMNS])


def _series(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[Tuple[str, str], Dict[str, int]]]:
    """source -> (entity_id, model) -> {month: count}, in input order."""
    out: Dict[str, Dict[Tuple[str, str], Dict[str, int]]] = OrderedDict()
    for r in rows:
        src = out.setdefault(r["source"], OrderedDict())
        src.setdefault((r["entity_id"], r["model"]), {})[r["month"]] = int(r["count"])
    return out


def _md_label(text: str) -> str:
    """A table-safe label: pipes (the epmc term joiner) become commas, stray pipes are escaped."""
    return ", ".join(p.strip() for p in str(text).split("|") if p.strip()).replace("|", "\\|")


def series_label(entity_id: str, model: str, names: Optional[Dict[str, str]] = None) -> str:
    """Column label: the gazetteer name with the archive string or term set, e.g. 'Revio (revio)' or
    'Sequel II (Sequel II, Sequel IIe)'; the raw id and model when no name is known."""
    name = (names or {}).get(entity_id, "")
    model_label = _md_label(model)
    if not name:
        return f"{entity_id}: {model_label}"
    return f"{name} ({model_label})"


def render_markdown(
    rows: Sequence[Dict[str, Any]],
    months: Sequence[str],
    title: str = "Instrument usage time series",
    sources: Optional[Sequence[str]] = None,
    names: Optional[Dict[str, str]] = None,
    failed: Optional[Set[Tuple[str, str, str, str]]] = None,
    order: Optional[Sequence[Tuple[str, str, str]]] = None,
) -> str:
    """One table per source, pivoted by month: rows are months, columns are entity/model series.

    Sources appear in `sources` order (the --sources argument), columns in `order` (config order of the
    planned series), numbers right-aligned with thousands separators, labels from `names` (gazetteer names).
    Cells: a number; blank = never fetched; `fail` = the request failed in this run (`failed` holds
    (source, entity_id, model, month) keys) so a failed month is never read as "no deposits"."""
    lines = [f"# {title}", ""]
    if months:
        lines.append(
            f"Months {months[0]} to {months[-1]}. Per calendar month: sra = SRA experiments, ena = ENA runs (experiment-run rows; "
            "a multi-lane project counts once per run, so ena runs above sra experiments), epmc = open-access papers naming the "
            "model in Methods. Blank = never fetched; fail = the request failed in this run (see `backfill.failures` in the report)."
        )
        lines.append("")
    series = _series(rows)
    failed = failed or set()
    if not series and not failed:
        lines.append("_No data._")
        return "\n".join(lines) + "\n"
    source_order = [s for s in (sources or []) if s in series or any(f[0] == s for f in failed)]
    source_order += [s for s in series if s not in source_order]
    source_order += sorted({f[0] for f in failed} - set(source_order))
    for source in source_order:
        per_model = series.get(source, OrderedDict())
        cols: List[Tuple[str, str]] = []
        if order:
            cols = [(eid, model) for (src, eid, model) in order if src == source]
        for key in list(per_model.keys()) + sorted({(f[1], f[2]) for f in failed if f[0] == source}):
            if key not in cols:
                cols.append(key)
        lines.append(f"## {source} ({UNIT_NOTES.get(source, source)})")
        lines.append("")
        header = ["month"] + [series_label(eid, model, names) for eid, model in cols] + ["total"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] + ["---:"] * (len(header) - 1)) + "|")
        col_totals = [0] * len(cols)
        for month in months:
            cells: List[str] = []
            total = 0
            for i, (eid, model) in enumerate(cols):
                v = per_model.get((eid, model), {}).get(month)
                if v is None:
                    cells.append("fail" if (source, eid, model, month) in failed else "")
                else:
                    cells.append(f"{v:,}")
                    total += v
                    col_totals[i] += v
            lines.append("| " + " | ".join([month] + cells + [f"{total:,}"]) + " |")
        lines.append("| " + " | ".join(["**total**"] + [f"{t:,}" for t in col_totals] + [f"{sum(col_totals):,}"]) + " |")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_png(rows: Sequence[Dict[str, Any]], months: Sequence[str], path: Path, title: str = "Instrument usage time series") -> bool:
    """One panel per source (own y-axis, never a dual axis), lines per entity/model in fixed palette order.
    Returns False when matplotlib is not importable; it is an optional extra, never a dependency."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 - any import problem means "no chart", not a failure
        return False
    series = _series(rows)
    if not series or not months:
        return False
    sources = list(series.keys())
    fig, axes = plt.subplots(len(sources), 1, figsize=(11, 3.4 * len(sources)), sharex=True, squeeze=False)
    x = list(range(len(months)))
    for ax, source in zip(axes[:, 0], sources):
        per_model = series[source]
        ranked = sorted(per_model.items(), key=lambda kv: -sum(kv[1].values()))
        shown = ranked[: len(PALETTE)]
        rest = ranked[len(PALETTE):]
        if rest:
            other: Dict[str, int] = {}
            for _, data in rest:
                for m, v in data.items():
                    other[m] = other.get(m, 0) + v
            shown = shown[: len(PALETTE) - 1] + [(("other", f"{len(rest) + 1} models"), other)]
        for i, ((eid, model), data) in enumerate(shown):
            y = [float(data[m]) if m in data else float("nan") for m in months]
            label = f"{eid}: {model}" if eid != "other" else f"other ({model})"
            ax.plot(x, y, color=PALETTE[i], linewidth=2, label=label)
            if len(shown) <= 4:
                last = [(j, v) for j, v in enumerate(y) if v == v]  # drop NaN
                if last:
                    j, v = last[-1]
                    ax.annotate(label, (j, v), xytext=(4, 0), textcoords="offset points", fontsize=8, color="#52514e", va="center")
        unit = "papers / month" if source == "epmc" else "experiments / month"
        ax.set_title(f"{source}: {unit}", loc="left", fontsize=10, color="#0b0b0b")
        ax.grid(axis="y", color="#e6e6e3", linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#c3c2b7")
        ax.tick_params(colors="#52514e", labelsize=8)
        ax.set_ylim(bottom=0)
        ax.legend(frameon=False, fontsize=8, loc="upper left")
    step = 12 if len(months) > 36 else (6 if len(months) > 12 else 1)
    ticks = list(range(0, len(months), step))
    axes[-1, 0].set_xticks(ticks)
    axes[-1, 0].set_xticklabels([months[i] for i in ticks], rotation=0)
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color="#0b0b0b")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(path), dpi=144, facecolor="#fcfcfb")
    plt.close(fig)
    return True


# ---- orchestration --------------------------------------------------------------------------
def default_months(today: Optional[date] = None, span: int = DEFAULT_SPAN_MONTHS) -> Tuple[str, str]:
    today = today or date.today()
    end = today.strftime("%Y-%m")
    return shift_month(end, -(span - 1)), end


def run(
    config_dir: Path,
    db_path: Path,
    out_dir: Path,
    entities: Sequence[str] = (),
    vendors: Sequence[str] = (),
    start: Optional[str] = None,
    end: Optional[str] = None,
    sources: Sequence[str] = SOURCES,
    refresh_months: int = DEFAULT_REFRESH_MONTHS,
    force: bool = False,
    chart: bool = True,
    dry_run: bool = False,
    contact_email: str = "",
    ncbi_api_key: str = "",
    http: Optional[Http] = None,
    store: Optional[Store] = None,
    today: Optional[date] = None,
    all_entities: bool = False,
) -> Dict[str, Any]:
    """Resolve entities, fold any per-string cache rows into entity rows, backfill the cache, and write
    CSV + Markdown (+ PNG when matplotlib exists). `all_entities` (--all) selects every entity in
    archive_models.yaml; the output stem is then `timeseries_<from>_<to>_all`."""
    today = today or date.today()
    config_dir = Path(config_dir)
    specs_path = config_dir / "archive_models.yaml"
    specs = load_archive_models(specs_path)
    instruments = load_instrument_index(config_dir / "instruments.yaml")
    report: Dict[str, Any] = {"warnings": check_models(specs, instruments), "errors": []}
    if not specs:
        report["errors"].append(f"{specs_path} not found or has no `entities`")
        return report
    bad_sources = [s for s in sources if s not in SOURCES]
    if bad_sources:
        report["errors"].append(f"unknown sources {bad_sources}; choose from {list(SOURCES)}")
        return report
    entity_ids, warnings = resolve_entities(entities, vendors, instruments, specs, all_entities=all_entities)
    report["warnings"].extend(warnings)
    if not entity_ids:
        report["errors"].append("no entities selected: use --all, --entity <id> (see `litradar entities`) and/or --vendor <name|ticker|id-prefix>")
        return report
    d_start, d_end = default_months(today)
    start, end = start or d_start, end or d_end
    current = today.strftime("%Y-%m")
    try:
        parse_month(start)
        parse_month(end)
        if start > current:
            report["errors"].append(f"--from {start} is in the future (current month {current}); nothing can be deposited or published yet")
            return report
        if end > current:
            report["warnings"].append(f"--to {end} is in the future; clamped to the current month {current} (future months cannot have deposits)")
            end = current
        months = month_range(start, end)
    except ValueError as exc:
        report["errors"].append(str(exc))
        return report
    jobs = plan_jobs(specs, entity_ids, months, sources)
    launches = {eid: specs[eid].launch for eid in entity_ids if specs[eid].launch}
    report.update({"entities": entity_ids, "from": months[0], "to": months[-1], "months": len(months), "sources": list(sources),
                   "series": len({(j.source, j.entity_id, j.model) for j in jobs})})
    http = http or Http(contact_email=contact_email, cache_dir=None, ncbi_api_key=ncbi_api_key)
    store = store or Store(Path(db_path))
    # a cache written per vocabulary string (earlier versions) is folded into entity rows first, so the
    # backfill below asks the archives only for months no entity row covers; a dry run only plans with it
    consolidated = consolidate_cache(store, specs, entity_ids, sources, apply=not dry_run)
    report["consolidated"] = {k: consolidated[k] for k in ("derived", "pruned", "partial", "entities")}
    report["backfill"] = backfill(http, store, jobs, refresh_months=refresh_months, force=force, today=today, api_key=ncbi_api_key,
                                  dry_run=dry_run, launches=launches, virtual_cache=consolidated["virtual"] if dry_run else None)
    if dry_run:
        report["dry_run"] = True
        return report
    # only the series planned for this run: rows left behind by renamed model lists or epmc_terms stay in
    # the table (prune them by hand if you like) but never leak into the outputs as stale columns
    keep = {(j.source, j.entity_id, j.model) for j in jobs}
    rows = [
        r
        for r in store.timeseries_rows(sources=list(sources), entity_ids=entity_ids, start_month=months[0], end_month=months[-1])
        if (r["source"], r["entity_id"], r["model"]) in keep
    ]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem(months[0], months[-1], () if all_entities else entities, () if all_entities else vendors, entity_ids)
    csv_path, md_path, png_path = out_dir / f"{stem}.csv", out_dir / f"{stem}.md", out_dir / f"{stem}.png"
    if csv_path.exists() or md_path.exists():
        report["warnings"].append(f"replacing the earlier outputs {stem}.csv/.md in {out_dir} (same months and selection)")
    failed_keys = {(f["source"], f["entity_id"], f["model"], f["month"]) for f in report["backfill"]["failures"]}
    names = {eid: (instruments.get(eid) or {}).get("name") or "" for eid in entity_ids}
    order = [(j.source, j.entity_id, j.model) for j in jobs]
    order = list(OrderedDict.fromkeys(order))
    write_csv(rows, csv_path)
    md_path.write_text(render_markdown(rows, months, sources=list(sources), names=names, failed=failed_keys, order=order), encoding="utf-8")
    report["outputs"] = {"csv": str(csv_path), "markdown": str(md_path), "rows": len(rows)}
    if chart:
        report["outputs"]["png"] = str(png_path) if write_png(rows, months, png_path) else None
    # failures: WHY, per source. A source where every request of this run failed is an error (exit 1);
    # a source with some failed months is a warning: the cached values still stand and a re-run retries them.
    for source in sources:
        bs = report["backfill"]["by_source"].get(source) or {}
        n_failed, n_requests = int(bs.get("failed") or 0), int(bs.get("requests") or 0)
        if not n_failed:
            continue
        reasons = Counter(f["reason"] for f in report["backfill"]["failures"] if f["key"].startswith(f"{source}:"))
        why = "; ".join(f"{r} (x{n})" for r, n in reasons.most_common(3))
        if n_failed >= n_requests:
            report["errors"].append(f"{source}: all {n_requests} count request(s) of this run failed: {why}. Nothing cached for them; fix the cause and re-run")
        else:
            report["warnings"].append(f"{source}: {n_failed} of {n_requests} count request(s) failed (shown as `fail`, not cached): {why}. Re-run to retry")
    if report["backfill"]["kept"]:
        report["warnings"].append(
            f"{len(report['backfill']['kept'])} fresh zero counts ignored because a positive count is cached (see backfill.kept; "
            "check the vocabulary string, or --force to overwrite)"
        )
    return report


def output_stem(first_month: str, last_month: str, entities: Sequence[str], vendors: Sequence[str], entity_ids: Sequence[str]) -> str:
    """`timeseries_<from>_<to>_<selection>`: the selection is the --vendor / --entity tokens (slugified, '+'-joined),
    so two runs for different vendors into one --out directory keep both files; a long selection is shortened to its
    first token plus a 6-character hash of the resolved entity ids."""
    tokens = [re.sub(r"[^a-z0-9]+", "-", str(t).lower()).strip("-") for t in list(vendors) + list(entities)]
    tokens = [t for t in dict.fromkeys(tokens) if t]
    selection = "+".join(tokens) or "all"
    if len(selection) > 60:
        digest = hashlib.sha1("|".join(entity_ids).encode("utf-8")).hexdigest()[:6]
        selection = f"{tokens[0][:24]}+{len(entity_ids)}ids-{digest}"
    return f"timeseries_{first_month}_{last_month}_{selection}"
