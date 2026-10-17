"""Instrument usage time series: monthly counts per instrument model from NCBI SRA, ENA and Europe PMC.

The question this answers: "how many PacBio (or Nanopore, Illumina, ...) experiments and papers show
up over time, by model?" It counts *deposited sequencing experiments* (SRA, ENA) and *open-access
papers naming the model in their Methods* (Europe PMC). It never counts installed machines; see
docs/sources/timeseries.md for the caveats (deposit lag, clinical and commercial sequencing that is
never deposited, SRA Properties index vs esummary differences).

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
  ("revio" and "REVIO" also give 38); "PacBio Revio" is not a vocabulary value and returns 0.

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
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from .http import Http
from .store import Store

log = logging.getLogger(__name__)

EUTILS_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ENA_COUNT = "https://www.ebi.ac.uk/ena/portal/api/count"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SOURCES: Tuple[str, ...] = ("sra", "ena", "epmc")
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

    def models_for(self, source: str) -> List[str]:
        if source == "sra":
            return list(self.sra)
        if source == "ena":
            return list(self.ena)
        if source == "epmc":
            return [epmc_label(self.epmc_terms)] if self.epmc_terms else []
        return []


def epmc_label(terms: Sequence[str]) -> str:
    """The `model` column for a Europe PMC series: the OR-ed term set, pipe-joined."""
    return "|".join(t for t in terms if t)


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
        out[str(eid)] = ModelSpec(
            entity_id=str(eid),
            vendor=str(raw.get("vendor") or ""),
            sra=[s.lower() for s in _str_list(raw.get("sra"))],
            ena=_str_list(raw.get("ena")),
            epmc_terms=_str_list(raw.get("epmc_terms")),
            proposed=bool(raw.get("proposed", False)),
            notes=str(raw.get("notes") or ""),
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
    entities: Sequence[str], vendors: Sequence[str], instruments: Dict[str, Dict[str, str]], specs: Dict[str, ModelSpec]
) -> Tuple[List[str], List[str]]:
    """Explicit --entity ids plus --vendor expansions, in config order; unknown ids become warnings."""
    wanted: List[str] = []
    warnings: List[str] = []
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


def sra_term(model: str, start: str, end: str) -> str:
    return f'{start.replace("-", "/")}:{end.replace("-", "/")}[PDAT] AND "instrument {model.lower()}"[Properties]'


def sra_count(http: Http, model: str, start: str, end: str, api_key: str = "") -> Optional[int]:
    """Experiments with the given instrument_model made public in [start, end]; None on failure.

    A count is accepted only when esearch really evaluated the Properties clause: NCBI answers a misspelt
    field tag with HTTP 200, a count, an `errorlist` and a querytranslation rewritten to [All Fields], and
    that number is not a Properties count. A zero-deposit string keeps the [Properties] clause and adds
    warninglist.quotedphrasesnotfound; that is a genuine 0."""
    params: Dict[str, Any] = {"db": "sra", "term": sra_term(model, start, end), "retmax": 0, "retmode": "json"}
    if api_key:
        params["api_key"] = api_key
    data = http.get_json(EUTILS_ESEARCH, params=params)
    if not isinstance(data, dict):
        return None
    result = data.get("esearchresult")
    if not isinstance(result, dict) or "count" not in result:
        return None  # {"error": "..."} and other unexpected shapes
    if result.get("errorlist"):
        log.warning("SRA esearch reported errorlist %s for %r", result.get("errorlist"), params["term"])
        return None
    translation = str(result.get("querytranslation") or "")
    if "[Properties]" not in translation:
        log.warning("SRA esearch did not evaluate the Properties clause (querytranslation=%r)", translation)
        return None
    return _to_int(result.get("count"))


def ena_query(model: str, start: str, end: str) -> str:
    return f'first_public>={start} AND first_public<={end} AND instrument_model="{model}"'


def ena_count(http: Http, model: str, start: str, end: str) -> Optional[int]:
    """read_experiment records with instrument_model == model and first_public in [start, end]; None on failure."""
    data = http.get_json(ENA_COUNT, params={"result": "read_experiment", "query": ena_query(model, start, end), "format": "json"})
    if not isinstance(data, dict):
        return None
    return _to_int(data.get("count"))


def epmc_query(terms: Sequence[str], start: str, end: str) -> str:
    term_part = " OR ".join(f'METHODS:"{t}"' for t in terms if t)
    return f"({term_part}) AND (FIRST_PDATE:[{start} TO {end}])"


def epmc_count(http: Http, terms: Sequence[str], start: str, end: str) -> Optional[int]:
    """Papers first published in [start, end] whose Methods name any of the terms; None on failure."""
    terms = [t for t in terms if t]
    if not terms:
        return None
    data = http.get_json(EPMC_SEARCH, params={"query": epmc_query(terms, start, end), "format": "json", "resultType": "idlist", "pageSize": 1})
    if not isinstance(data, dict) or "hitCount" not in data:
        return None
    return _to_int(data.get("hitCount"))


# ---- backfill -------------------------------------------------------------------------------
@dataclass
class Job:
    source: str
    entity_id: str
    model: str
    month: str
    terms: List[str] = field(default_factory=list)  # Europe PMC only


def plan_jobs(specs: Dict[str, ModelSpec], entity_ids: Sequence[str], months: Sequence[str], sources: Sequence[str]) -> List[Job]:
    jobs: List[Job] = []
    for source in sources:
        for eid in entity_ids:
            spec = specs.get(eid)
            if not spec:
                continue
            for model in spec.models_for(source):
                for month in months:
                    jobs.append(Job(source, eid, model, month, list(spec.epmc_terms) if source == "epmc" else []))
    return jobs


def refresh_cutoff(today: date, refresh_months: int) -> str:
    """Months >= this cutoff are fetched again even when cached (their counts are still growing): the
    current month plus the `refresh_months` calendar months before it (2 on 2026-10-17 -> 2026-08)."""
    return shift_month(today.strftime("%Y-%m"), -max(0, int(refresh_months)))


def job_key(job: Job) -> str:
    return f"{job.source}:{job.entity_id}:{job.model}:{job.month}"


def fetch_count(http: Http, job: Job, api_key: str = "") -> Optional[int]:
    """One live count for a job (None on failure); dispatches on job.source."""
    start, end = month_bounds(job.month)
    if job.source == "sra":
        return sra_count(http, job.model, start, end, api_key)
    if job.source == "ena":
        return ena_count(http, job.model, start, end)
    if job.source == "epmc":
        return epmc_count(http, job.terms, start, end)
    return None


def backfill(
    http: Http,
    store: Store,
    jobs: Sequence[Job],
    refresh_months: int = DEFAULT_REFRESH_MONTHS,
    force: bool = False,
    today: Optional[date] = None,
    api_key: str = "",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fetch and cache every job's count, committing each stored value on its own so an interrupted run
    (Ctrl-C, crash, exception) keeps everything fetched so far. Cached months older than the refresh
    window are skipped unless force=True. Failed calls are reported under `failed` and never cached (an
    older cached value survives). A fresh 0 for a key whose cached count is positive is reported under
    `kept` and NOT stored unless force=True: public deposits are not withdrawn wholesale, so that pattern
    means the archive renamed or dropped the vocabulary string (SRA then also reports
    warninglist.quotedphrasesnotfound), and overwriting history with zeros would hide it."""
    today = today or date.today()
    cutoff = refresh_cutoff(today, refresh_months)
    report: Dict[str, Any] = {"jobs": len(jobs), "requests": 0, "cached": 0, "failed": [], "kept": [], "by_source": {}, "cutoff": cutoff}
    try:
        for job in jobs:
            bs = report["by_source"].setdefault(job.source, {"requests": 0, "cached": 0, "failed": 0, "kept": 0})
            cached = store.get_timeseries_count(job.source, job.entity_id, job.model, job.month)
            if cached is not None and not force and job.month < cutoff:
                report["cached"] += 1
                bs["cached"] += 1
                continue
            if dry_run:
                report["requests"] += 1
                bs["requests"] += 1
                continue
            n = fetch_count(http, job, api_key)
            report["requests"] += 1
            bs["requests"] += 1
            if n is None:
                report["failed"].append(job_key(job))
                bs["failed"] += 1
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
CSV_COLUMNS = ("source", "entity_id", "model", "month", "count")


def write_csv(rows: Sequence[Dict[str, Any]], path: Path) -> None:
    """Long format: source,entity_id,model,month,count (one row per cached count)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([r.get(c, "") for c in CSV_COLUMNS])


def _series(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[Tuple[str, str], Dict[str, int]]]:
    """source -> (entity_id, model) -> {month: count}, in input order."""
    out: Dict[str, Dict[Tuple[str, str], Dict[str, int]]] = OrderedDict()
    for r in rows:
        src = out.setdefault(r["source"], OrderedDict())
        src.setdefault((r["entity_id"], r["model"]), {})[r["month"]] = int(r["count"])
    return out


def render_markdown(rows: Sequence[Dict[str, Any]], months: Sequence[str], title: str = "Instrument usage time series") -> str:
    """One table per source, pivoted by month: rows are months, columns are entity/model series."""
    lines = [f"# {title}", ""]
    if months:
        lines.append(f"Months {months[0]} to {months[-1]}. Counts are deposited experiments (sra, ena) or open-access papers (epmc) per calendar month; blank = not fetched.")
        lines.append("")
    series = _series(rows)
    if not series:
        lines.append("_No data._")
        return "\n".join(lines) + "\n"
    for source, per_model in series.items():
        unit = "papers naming the model in Methods" if source == "epmc" else "experiments by instrument_model"
        lines.append(f"## {source} ({unit})")
        lines.append("")
        cols = list(per_model.keys())
        header = ["month"] + [f"{eid}: {model}" for eid, model in cols] + ["total"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        col_totals = [0] * len(cols)
        for month in months:
            cells: List[str] = []
            total = 0
            for i, key in enumerate(cols):
                v = per_model[key].get(month)
                if v is None:
                    cells.append("")
                else:
                    cells.append(str(v))
                    total += v
                    col_totals[i] += v
            lines.append("| " + " | ".join([month] + cells + [str(total)]) + " |")
        lines.append("| " + " | ".join(["**total**"] + [str(t) for t in col_totals] + [str(sum(col_totals))]) + " |")
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
) -> Dict[str, Any]:
    """Resolve entities, backfill the cache, and write CSV + Markdown (+ PNG when matplotlib exists)."""
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
    entity_ids, warnings = resolve_entities(entities, vendors, instruments, specs)
    report["warnings"].extend(warnings)
    if not entity_ids:
        report["errors"].append("no entities selected: use --entity <id> (see `litradar entities`) and/or --vendor <name|ticker|id-prefix>")
        return report
    d_start, d_end = default_months(today)
    start, end = start or d_start, end or d_end
    try:
        months = month_range(start, end)
    except ValueError as exc:
        report["errors"].append(str(exc))
        return report
    jobs = plan_jobs(specs, entity_ids, months, sources)
    report.update({"entities": entity_ids, "from": months[0], "to": months[-1], "months": len(months), "sources": list(sources)})
    http = http or Http(contact_email=contact_email, cache_dir=None, ncbi_api_key=ncbi_api_key)
    store = store or Store(Path(db_path))
    report["backfill"] = backfill(http, store, jobs, refresh_months=refresh_months, force=force, today=today, api_key=ncbi_api_key, dry_run=dry_run)
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
    stem = f"timeseries_{months[0]}_{months[-1]}"
    csv_path, md_path, png_path = out_dir / f"{stem}.csv", out_dir / f"{stem}.md", out_dir / f"{stem}.png"
    write_csv(rows, csv_path)
    md_path.write_text(render_markdown(rows, months), encoding="utf-8")
    report["outputs"] = {"csv": str(csv_path), "markdown": str(md_path), "rows": len(rows)}
    if chart:
        report["outputs"]["png"] = str(png_path) if write_png(rows, months, png_path) else None
    if report["backfill"]["failed"]:
        report["errors"].append(f"{len(report['backfill']['failed'])} count requests failed (not cached; re-run to retry)")
    if report["backfill"]["kept"]:
        report["warnings"].append(
            f"{len(report['backfill']['kept'])} fresh zero counts ignored because a positive count is cached (see backfill.kept; "
            "check the vocabulary string, or --force to overwrite)"
        )
    return report
