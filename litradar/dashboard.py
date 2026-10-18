"""Static trends dashboard: turn the SQLite corpus into a self-contained site you can publish.

The weekly digest answers "what happened last week". A persistent corpus can answer
"what is happening over time", which is a different question and needs a different surface:
small multiples per entity, week-over-week movers, and a client-side search over the recent
window. This module reads the store **read-only** and writes plain files - no build step,
no server, no framework.

What it reads (every table is optional; a missing table degrades to an empty section):

  papers(id, title, journal, pub_date, first_seen, url, kind, extra_json[, country])
  mentions(paper_id, kind='instrument'|'company', entity_id, entity_name, vendor, ticker, section)
  scores(paper_id, final)
  archive_records(accession, instrument_model, vendor, platform, entity_id, first_public)
  news_items(id, published, first_seen, companies_json)
  tracked_author_hits(paper_id, author_name, subgroup)
  timeseries(source, entity_id, model, month, count)   -- optional, written by other adapters

Week buckets are ISO weeks keyed by the Monday (UTC). A paper is dated by `pub_date`, falling
back to the date part of `first_seen` for feeds that carry no publication date. A paper whose
date a padding source (PADDED_DATE_SOURCES: OpenAlex) stored as the 1st of a month has no known
week - month- and year-precision dates are padded to the 1st - so every weekly series leaves it
out and the overview reports how many; lists and the search index keep it.

Coverage. The store records what its ingestion covered: `runs.since` (the weekly multi-source
runs, which also take the archive census) and `backfill_progress` (months of publication metadata
only). Weekly series are None - "n/a", never 0 - for the weeks before the earliest relevant
record, charts shade those weeks and mark where the weekly runs begin after backfilled weeks,
and the movers compare two windows only when every recorded layer covers both alike (fully or
not at all); otherwise the table is suppressed and the page says which layer starts or ends
inside the compared weeks. A store that records nothing is zero-filled and compared as before.

"Optional" means exactly a missing table or column (`no such table` / `no such column`): those
queries are skipped, logged and counted in the report. Any other database error - locked file,
corrupt image, torn WAL copy - is raised, so a broken store fails the build loudly instead of
publishing an all-zero site.

Movers compare the last four COMPLETE weeks against the four before them. The axis ends with the
week containing today; unless today is a Sunday that week is still in progress and is left out of
the comparison, and the page says so. Movers rank every entity, not only those given a card.

Instrument cards are the union of the top-N by papers and the top-N by archive experiments, so a
sequencer that archives see constantly but papers rarely name by model is not silently dropped.

Country of first affiliation is used when the store can supply it: a `papers.country` column if
one exists, else `extra_json.country` / `extra_json.countries[0]`. Neither is written by the
base pipeline, so `countries.html` explains how to enable it rather than inventing a value.

Charting: Chart.js v4.5.1, pinned by exact version with a Subresource Integrity hash, loaded
from cdnjs (verified live 2026-10-17; the same bytes are on jsdelivr, see docs/dashboard.md).
Two measures of different scale never share a y-axis: each instrument gets two small charts
rather than one dual-axis chart.
"""
from __future__ import annotations

import html
import json
import logging
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# Pinned chart library. Verified live on 2026-10-17:
#   https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js -> 200, 208,522 bytes
#   https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js      -> 200, identical bytes
CHARTJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js"
CHARTJS_SRI = "sha384-jb8JQMbMoBUzgWatfe6COACi2ljcDdZQ2OxczGA3bGNeWe+6DChMTBJemed7ZnvJ"
# d3 for the company co-mention graph (companies/graph.html): pinned, with SRI; 279,706 bytes, identical on jsdelivr
D3_URL = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"
D3_SRI = "sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i"
COMPANY_PAGE_MIN_PAPERS = 3   # a company gets companies/<id>.html once it has this many distinct papers in the window
COMPANY_PAGE_MONTHS = 24      # months of history on the profile page's monthly chart
GRAPH_MAX_DEFAULT_LINKS = 400 # the graph page's default minimum edge weight keeps about this many edges on screen

NAV = [
    ("index.html", "Overview"),
    ("instruments.html", "Instruments"),
    ("timeseries.html", "Time series"),
    ("companies.html", "Companies"),
    ("subgroups.html", "Subgroups"),
    ("countries.html", "Countries"),
    ("search.html", "Search"),
]

# How many entities get their own card / series before the tail folds into "Other".
MAX_INSTRUMENT_CARDS = 48
MAX_COMPANY_CARDS = 48
MAX_STACK_SERIES = 6          # + "Other"
MAX_MOVERS = 15
DEFAULT_MAX_PAPERS = 4000     # cap on the client-side search index

AUTHOR_SECTIONS = ("affiliations",)
FUNDING_SECTIONS = ("funding", "coi", "ack")
SUBJECT_SECTIONS = ("title", "abstract", "methods")

# Archive `instrument_platform` codes (the ENA/SRA controlled vocabulary) -> the vendor as the
# gazetteer spells it. Used ONLY for records whose model is not in the gazetteer and therefore
# carry no vendor, so a platform code and the company name do not stand as two stacked series for
# one vendor. Codes with no gazetteer vendor (GENEMIND, GENAPSYS, ABI_SOLID, LS454, CAPILLARY) are
# left as they are rather than guessed.
PLATFORM_VENDORS = {
    "ILLUMINA": "Illumina",
    "BGISEQ": "MGI Tech",
    "DNBSEQ": "MGI Tech",
    "PACBIO_SMRT": "Pacific Biosciences",
    "OXFORD_NANOPORE": "Oxford Nanopore Technologies",
    "ION_TORRENT": "Thermo Fisher Scientific",
    "ELEMENT": "Element Biosciences",
    "ULTIMA": "Ultima Genomics",
}

# Movers compare the last `MOVERS_SPAN` complete weeks against the `MOVERS_SPAN` before them.
MOVERS_SPAN = 4


# ---------------------------------------------------------------------------- sqlite helpers
class Connection(sqlite3.Connection):
    """A sqlite3 connection that remembers which *optional* queries were skipped.

    Only a missing table or column is optional (an older store, or an adapter that has not run
    yet); every other database error - locked, corrupt, torn WAL copy - propagates, so a broken
    store fails the build instead of publishing an all-zero site.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.skipped_queries: List[str] = []


def connect_readonly(db_path: Path) -> Tuple[sqlite3.Connection, Optional[Path]]:
    """Open the store without writing to it.

    A database left in WAL mode needs a writable -shm/-wal pair, which a read-only open cannot
    create; when that happens we copy the database (and any sidecars) to a temporary directory
    and read the copy, so the caller's file is never touched. The second element of the return
    value is that temporary directory, for the caller to clean up.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, factory=Connection)
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        return conn, None
    except sqlite3.Error as exc:
        log.info("read-only open failed (%s); reading a temporary copy instead", exc)
    tmp = Path(tempfile.mkdtemp(prefix="litradar-dashboard-"))
    copy = tmp / db_path.name
    shutil.copy2(db_path, copy)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(copy) + suffix))
    conn = sqlite3.connect(str(copy), factory=Connection)
    return conn, tmp


def _tables(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return []


_OPTIONAL_ERRORS = ("no such table", "no such column")


def _q(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[tuple]:
    """Run a query. A missing table or column empties the section; anything else is an error."""
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if not message.startswith(_OPTIONAL_ERRORS):
            raise
        log.warning("optional table or column missing, section left empty: %s", message)
        skipped = getattr(conn, "skipped_queries", None)
        if skipped is not None:
            skipped.append(message)
        return []


# ---------------------------------------------------------------------------- week buckets
def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_axis(end: date, weeks: int) -> List[str]:
    last = monday(end)
    return [(last - timedelta(weeks=i)).isoformat() for i in range(weeks - 1, -1, -1)]


def week_of(value: str) -> str:
    """Monday of the ISO week containing a YYYY-MM-DD (or ISO timestamp) string; '' when unparseable."""
    if not value:
        return ""
    try:
        day = date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except (ValueError, IndexError):
        return ""
    return monday(day).isoformat()


def _week_end(monday_iso: str) -> str:
    """The Sunday that closes the ISO week keyed by `monday_iso`."""
    return (date.fromisoformat(monday_iso) + timedelta(days=6)).isoformat()


def _series(buckets: Dict[str, Any], weeks: Sequence[str], covered_from: Optional[str] = None) -> List[Optional[int]]:
    """Zero-fill the axis. When `covered_from` - the first day a run, backfill or census fetched for - is known, a
    week that ends before it and holds nothing is None: never fetched for, not zero. A week before it that does hold
    rows keeps its count. Without a recorded start every week is zero-filled, as a store built outside the pipeline
    gives nothing better to go on."""
    out: List[Optional[int]] = []
    for w in weeks:
        n = int(buckets.get(w, 0))
        out.append(None if covered_from and n == 0 and _week_end(w) < covered_from else n)
    return out


def _total(series: Sequence[Optional[int]]) -> int:
    return sum(v or 0 for v in series)


def _paper_date_sql(alias: str = "p") -> str:
    """The date a paper is bucketed by: pub_date, else the date part of first_seen."""
    return f"COALESCE(NULLIF({alias}.pub_date,''), substr({alias}.first_seen,1,10))"


# Sources that store a month- or year-precision publication date padded to the first of the month: OpenAlex pads
# "2026-01" and "2026" alike to 2026-01-01 and exposes no precision flag. Such a paper has no publication week, so
# every weekly series leaves it out and the overview says how many; the few papers really published on the 1st
# cannot be told apart and go with them. Lists (recent papers, the search index) keep every paper.
PADDED_DATE_SOURCES: Tuple[str, ...] = ("openalex",)


def _week_known_sql(alias: str = "p") -> str:
    """SQL predicate: the paper's publication week is known (not a first-of-month date from a padding source)."""
    sources = ", ".join(f"'{s}'" for s in PADDED_DATE_SOURCES)
    return f"NOT (COALESCE({alias}.source,'') IN ({sources}) AND substr({alias}.pub_date, 9, 2) = '01')"


# ---------------------------------------------------------------------------- collection
def _papers_per_week(conn: sqlite3.Connection, weeks: List[str], start: str, end: str,
                     covered_from: Optional[str] = None) -> Dict[str, Any]:
    """Papers per week whose publication week is known. Papers dated the 1st of a month by a padding source
    (PADDED_DATE_SOURCES) are counted apart in `first_of_month`, by the week their padded date falls in, and never
    enter `total` or `by_kind`: charted as published they draw a sawtooth of month boundaries."""
    expr, known = _paper_date_sql(), _week_known_sql()
    rows = _q(
        conn,
        f"SELECT {expr} AS d, COALESCE(NULLIF(p.kind,''),'article') AS k, {known} AS known, COUNT(*) "
        f"FROM papers p WHERE {expr} >= ? AND {expr} <= ? GROUP BY d, k, known",
        (start, end),
    )
    total: Dict[str, int] = defaultdict(int)
    padded: Dict[str, int] = defaultdict(int)
    by_kind: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for d, kind, known_week, n in rows:
        w = week_of(d)
        if not w:
            continue
        if not known_week:
            padded[w] += n
            continue
        total[w] += n
        by_kind[kind][w] += n
    return {
        "total": _series(total, weeks, covered_from),
        "by_kind": {k: _series(v, weeks, covered_from) for k, v in sorted(by_kind.items())},
        "first_of_month": _series(padded, weeks),
        "first_of_month_total": sum(padded.values()),
        "padded_sources": list(PADDED_DATE_SOURCES),
    }


def _fold_tail(series: Dict[str, List[Optional[int]]], keep: int) -> Dict[str, List[Optional[int]]]:
    """Keep the `keep` largest series by total; sum the rest into 'Other' (never invent a 9th hue). A week that is
    None (not covered) in every folded series stays None in 'Other'."""
    if len(series) <= keep:
        return dict(sorted(series.items(), key=lambda kv: -_total(kv[1])))
    ordered = sorted(series.items(), key=lambda kv: -_total(kv[1]))
    head = dict(ordered[:keep])
    tail = ordered[keep:]
    width = len(next(iter(series.values()))) if series else 0
    other: List[Optional[int]] = [None] * width
    for _, vals in tail:
        for i, v in enumerate(vals):
            if v is not None:
                other[i] = (other[i] or 0) + v
    head["Other"] = other
    return head


def _instrument_weeks_by_vendor(conn: sqlite3.Connection, weeks: List[str], start: str, end: str,
                                covered_from: Optional[str] = None) -> Dict[str, Any]:
    expr = _paper_date_sql()
    rows = _q(
        conn,
        f"SELECT {expr} AS d, COALESCE(NULLIF(m.vendor,''),'unattributed') AS vendor, COUNT(DISTINCT m.paper_id) "
        f"FROM mentions m JOIN papers p ON p.id = m.paper_id "
        f"WHERE m.kind='instrument' AND {expr} >= ? AND {expr} <= ? AND {_week_known_sql()} GROUP BY d, vendor",
        (start, end),
    )
    by_vendor: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for d, vendor, n in rows:
        w = week_of(d)
        if w:
            by_vendor[vendor][w] += n
    series = {v: _series(b, weeks, covered_from) for v, b in by_vendor.items()}
    return {"series": _fold_tail(series, MAX_STACK_SERIES)}


def _archive_weeks_by_vendor(conn: sqlite3.Connection, weeks: List[str], start: str, end: str,
                             covered_from: Optional[str] = None) -> Dict[str, Any]:
    """Experiments per week by vendor. Weeks before `covered_from` (the start of the archive census) are None.

    Records whose declared model is not in the gazetteer carry no vendor, so the archive's own
    `instrument_platform` stands in, translated through PLATFORM_VENDORS when the code is a known
    vendor's. Codes outside that map are upper-case ("GENEMIND") where the gazetteer is mixed-case,
    so labels are also merged case-insensitively with the mixed-case spelling winning - otherwise
    one vendor would appear as two stacked series.
    """
    rows = _q(
        conn,
        "SELECT first_public, COALESCE(NULLIF(vendor,''), NULLIF(platform,''),'unattributed') AS v, COUNT(*) "
        "FROM archive_records WHERE first_public >= ? AND first_public <= ? GROUP BY first_public, v",
        (start, end),
    )
    by_vendor: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    display: Dict[str, str] = {}
    for d, vendor, n in rows:
        w = week_of(d)
        if not w:
            continue
        vendor = PLATFORM_VENDORS.get(str(vendor).upper(), str(vendor))
        key = str(vendor).lower()
        current = display.get(key)
        if current is None or (current.isupper() and not str(vendor).isupper()):
            display[key] = str(vendor)
        by_vendor[key][w] += n
    series = {display.get(k, k): _series(b, weeks, covered_from) for k, b in by_vendor.items()}
    return {"series": _fold_tail(series, MAX_STACK_SERIES)}


# ---------------------------------------------------------------------------- ingestion coverage
def _iso_day(value: Any) -> Optional[str]:
    """'YYYY-MM-DD' from a date or timestamp string, or None when it does not parse."""
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return None


def _month_bounds(month: Any) -> Tuple[Optional[str], Optional[str]]:
    """(first day, last day) of a 'YYYY-MM' string, or (None, None) when it does not parse."""
    first = _iso_day(f"{str(month or '')[:7]}-01")
    if not first:
        return None, None
    d = date.fromisoformat(first)
    last = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first, last.isoformat()


def _earliest(*values: Optional[str]) -> Optional[str]:
    known = [v for v in values if v]
    return min(known) if known else None


def _coverage(conn: sqlite3.Connection, tables: set) -> Dict[str, Optional[str]]:
    """Where the ingestion that built this store starts and stops, from the records the pipeline keeps.

    runs_since     the earliest `runs.since`: from that publication date on the weekly runs fetched every configured
                   source (full text where available, hence Methods-level mentions) and took the archive census.
    backfill_from  the first day of the earliest month in `backfill_progress`, backfill_to the last day of the
                   latest: publication metadata only (title- and abstract-level mentions) for those months.
    archive_since  the earlier of runs_since and the earliest archive record: the census had not run before it.
    papers_since   the earlier of runs_since and backfill_from: no run fetched papers for the weeks before it.
    A value is None when the store carries no such record; a missing table is not an error. The same two
    records drive the coverage flags of the weekly trends report, so the two surfaces agree."""
    runs_since = None
    if "runs" in tables:
        rows = _q(conn, "SELECT MIN(since) FROM runs WHERE since IS NOT NULL AND since != ''")
        runs_since = _iso_day(rows[0][0]) if rows and rows[0] else None
    backfill_from = backfill_to = None
    if "backfill_progress" in tables:
        rows = _q(conn, "SELECT MIN(month), MAX(month) FROM backfill_progress WHERE month IS NOT NULL AND month != ''")
        if rows and rows[0] and rows[0][0]:
            backfill_from, _ = _month_bounds(rows[0][0])
            _, backfill_to = _month_bounds(rows[0][1])
    archive_min = None
    if "archive_records" in tables:
        rows = _q(conn, "SELECT MIN(first_public) FROM archive_records WHERE first_public IS NOT NULL AND first_public != ''")
        archive_min = _iso_day(rows[0][0]) if rows and rows[0] else None
    return {
        "runs_since": runs_since,
        "backfill_from": backfill_from,
        "backfill_to": backfill_to,
        "archive_since": _earliest(runs_since, archive_min),
        "papers_since": _earliest(runs_since, backfill_from),
    }


def _axis_coverage(weeks: Sequence[str], since: Optional[str]) -> Dict[str, Any]:
    """How a week axis relates to an ingestion start: how many leading weeks end before `since` (never fetched for),
    which week is the first covered one and how many of its days `since` leaves."""
    if not since:
        return {"since": None, "uncovered_weeks": 0, "first_week": None, "first_week_days": 0}
    uncovered = sum(1 for w in weeks if _week_end(w) < since)
    first = weeks[uncovered] if uncovered < len(weeks) else None
    days = 0
    if first:
        begin = max(date.fromisoformat(since), date.fromisoformat(first))
        days = (date.fromisoformat(_week_end(first)) - begin).days + 1
    return {"since": since, "uncovered_weeks": uncovered, "first_week": first, "first_week_days": days}


def _papers_axis_coverage(weeks: Sequence[str], cov: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """The papers axis against both paper layers: the uncovered lead (before any run or backfill), the layer dates,
    and `runs_week_index` - the first axis week the weekly runs cover when backfilled weeks precede it, where the
    chart draws its boundary (None when the runs cover the whole axis, none of it, or no backfill precedes them)."""
    out = _axis_coverage(weeks, cov.get("papers_since"))
    runs_since, backfill_from = cov.get("runs_since"), cov.get("backfill_from")
    out.update({"runs_since": runs_since, "backfill_from": backfill_from, "backfill_to": cov.get("backfill_to")})
    boundary = None
    if runs_since and backfill_from and backfill_from < runs_since:
        before = sum(1 for w in weeks if _week_end(w) < runs_since)
        if 0 < before < len(weeks):
            boundary = before
    out["runs_week_index"] = boundary
    return out


def _layer_relation(start: Optional[str], end: Optional[str], span_start: str, span_end: str) -> str:
    """How an ingestion layer covering the days `start`..`end` (`end` None = open-ended) relates to a span of days:
    'full', 'partial', 'none', or 'unknown' when the layer is not recorded."""
    if not start:
        return "unknown"
    if start > span_end or (end is not None and end < span_start):
        return "none"
    if start <= span_start and (end is None or end >= span_end):
        return "full"
    return "partial"


_RELATION_WORDS = {"full": "fully", "partial": "only in part", "none": "not at all"}

# Which ingestion layers feed each movers table: paper mentions come from the weekly runs and the backfill, archive
# experiments from the census the weekly runs take.
MOVERS_LAYERS: Dict[str, Tuple[str, ...]] = {
    "instrument_papers": ("runs", "backfill"),
    "archive_experiments": ("archive",),
    "companies": ("runs", "backfill"),
}
MOVERS_LABELS = {
    "instrument_papers": "Instruments by papers",
    "archive_experiments": "Instruments by archive experiments",
    "companies": "Companies by mentions",
}


def _movers_coverage(window: Dict[str, Any], cov: Dict[str, Optional[str]]) -> Dict[str, Dict[str, Any]]:
    """Per movers table: may the two windows be compared?

    They may when every recorded ingestion layer that feeds the table relates to both windows alike, and fully or
    not at all. A layer that starts, stops or runs partly inside the compared weeks - a first weekly run
    mid-window, a census that began last month, a backfill that ends between the windows - makes the difference
    one of coverage rather than of the literature, so the table is suppressed and the reason is given. A store
    that records no layer at all is compared as before; one whose recorded layers cover neither window is not."""
    span = int(window.get("span") or 0)
    if span < 1:
        return {k: {"comparable": False, "reason": ""} for k in MOVERS_LAYERS}
    recent = (window["recent"][0], _week_end(window["recent"][-1]))
    previous = (window["previous"][0], _week_end(window["previous"][-1]))
    # (start, end, subject, verb): the windows are day spans, written "first day through last day"
    layers = {
        "runs": (cov.get("runs_since"), None, f"the weekly runs (publication dates from {cov.get('runs_since')})", "cover"),
        "backfill": (cov.get("backfill_from"), cov.get("backfill_to"),
                     f"the backfilled months ({cov.get('backfill_from')} to {cov.get('backfill_to')})", "cover"),
        "archive": (cov.get("archive_since"), None, f"the archive census (records public from {cov.get('archive_since')})", "covers"),
    }
    out: Dict[str, Dict[str, Any]] = {}
    for table, names in MOVERS_LAYERS.items():
        recorded = covered = False
        reason = ""
        for name in names:
            start, end, label, verb = layers[name]
            r, p = _layer_relation(start, end, *recent), _layer_relation(start, end, *previous)
            if r == "unknown":
                continue
            recorded = True
            if r != p or "partial" in (r, p):
                reason = (f"{label} {verb} the recent window ({recent[0]} through {recent[1]}) {_RELATION_WORDS[r]} and "
                          f"the previous one ({previous[0]} through {previous[1]}) {_RELATION_WORDS[p]}, so the two were "
                          f"not ingested alike and a change between them would be one of coverage, not of the literature")
                break
            covered = covered or r == "full"
        if not reason and recorded and not covered:
            reason = (f"no run, backfill or census recorded in this store covers the compared weeks "
                      f"({previous[0]} through {recent[1]})")
        out[table] = {"comparable": not reason, "reason": reason}
    return out


def _movers_window(weeks: List[str], today: date, span: int = MOVERS_SPAN) -> Dict[str, Any]:
    """Which buckets the movers compare.

    The axis ends with the ISO week containing `today`. Unless today is a Sunday that week is still
    in progress, and counting it as "recent" would undercount every entity against a full previous
    window; so the trailing bucket is left out and the comparison uses complete weeks only.
    """
    offset = 0 if today.weekday() == 6 else 1
    usable = len(weeks) - offset
    if usable < 2:
        return {"span": 0, "offset": offset, "recent": [], "previous": [], "excludes_current_week": bool(offset)}
    span = max(1, min(span, usable // 2))
    stop = len(weeks) - offset
    return {
        "span": span,
        "offset": offset,
        "recent": weeks[stop - span:stop],
        "previous": weeks[stop - 2 * span:stop - span],
        "excludes_current_week": bool(offset),
    }


def _movers(labels: Dict[str, str], weekly: Dict[str, List[int]], window: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Week-over-week movers over the buckets `_movers_window` chose."""
    span, offset = int(window.get("span") or 0), int(window.get("offset") or 0)
    if span < 1:
        return []
    out = []
    for key, vals in weekly.items():
        stop = len(vals) - offset
        recent = _total(vals[stop - span:stop])
        previous = _total(vals[stop - 2 * span:stop - span])
        if recent == 0 and previous == 0:
            continue
        out.append({
            "id": key,
            "name": labels.get(key, key),
            "recent": recent,
            "previous": previous,
            "delta": recent - previous,
            "pct": None if previous == 0 else round(100.0 * (recent - previous) / previous, 1),
        })
    out.sort(key=lambda r: (-abs(r["delta"]), -r["recent"]))
    return out[:MAX_MOVERS]


def _instrument_entities(conn: sqlite3.Connection, weeks: List[str], start: str, end: str,
                         papers_from: Optional[str] = None, archive_from: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every instrument entity with any signal in the window: papers per week and experiments per week.

    Returns ALL of them - the caller decides which get a card. Movers must be computed over the
    full list, or an archive-heavy sequencer that few papers name would vanish from both.
    `papers_from` / `archive_from` are the ingestion starts the two series are None before.
    """
    expr = _paper_date_sql()
    rows = _q(
        conn,
        f"SELECT m.entity_id, MAX(m.entity_name), MAX(m.vendor), MAX(m.ticker), {expr} AS d, COUNT(DISTINCT m.paper_id) "
        f"FROM mentions m JOIN papers p ON p.id = m.paper_id "
        f"WHERE m.kind='instrument' AND {expr} >= ? AND {expr} <= ? AND {_week_known_sql()} GROUP BY m.entity_id, d",
        (start, end),
    )
    meta: Dict[str, Dict[str, str]] = {}
    papers: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for eid, name, vendor, ticker, d, n in rows:
        meta.setdefault(eid, {"name": name or eid, "vendor": vendor or "", "ticker": ticker or ""})
        w = week_of(d)
        if w:
            papers[eid][w] += n

    arch_rows = _q(
        conn,
        "SELECT entity_id, first_public, COUNT(*) FROM archive_records "
        "WHERE entity_id IS NOT NULL AND entity_id != '' AND first_public >= ? AND first_public <= ? "
        "GROUP BY entity_id, first_public",
        (start, end),
    )
    experiments: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for eid, d, n in arch_rows:
        w = week_of(d)
        if w:
            experiments[eid][w] += n
    model_rows = _q(
        conn,
        "SELECT entity_id, MAX(vendor), MAX(ticker), MAX(instrument_model) FROM archive_records "
        "WHERE entity_id IS NOT NULL AND entity_id != '' GROUP BY entity_id",
    )
    for eid, vendor, ticker, model in model_rows:
        meta.setdefault(eid, {"name": model or eid, "vendor": vendor or "", "ticker": ticker or ""})

    entities = []
    for eid in set(list(papers) + list(experiments)):
        info = meta.get(eid, {"name": eid, "vendor": "", "ticker": ""})
        pw = _series(papers.get(eid, {}), weeks, papers_from)
        aw = _series(experiments.get(eid, {}), weeks, archive_from)
        entities.append({
            "id": eid,
            "name": info["name"],
            "vendor": info["vendor"],
            "ticker": _ticker(info["ticker"]),
            "papers_per_week": pw,
            "experiments_per_week": aw,
            "papers_total": _total(pw),
            "experiments_total": _total(aw),
        })
    entities.sort(key=lambda e: (-e["papers_total"], -e["experiments_total"], e["name"]))
    return entities


def _select_instrument_cards(entities: List[Dict[str, Any]], limit: int = MAX_INSTRUMENT_CARDS) -> List[Dict[str, Any]]:
    """The union of the `limit` most-named instruments and the `limit` with the most experiments.

    One ranking alone hides the other measure: ranking by papers drops sequencers that archives
    see constantly but papers rarely name by model, ranking by experiments drops every
    non-sequencing instrument (which has no archive at all).
    """
    by_papers = sorted(entities, key=lambda e: (-e["papers_total"], -e["experiments_total"], e["name"]))
    by_experiments = sorted(entities, key=lambda e: (-e["experiments_total"], -e["papers_total"], e["name"]))
    chosen = {e["id"] for e in by_papers[:limit] if e["papers_total"] > 0}
    chosen |= {e["id"] for e in by_experiments[:limit] if e["experiments_total"] > 0}
    if not chosen:  # nothing has a non-zero total (cannot happen for a real window, but stay total)
        chosen = {e["id"] for e in by_papers[:limit]}
    return [e for e in by_papers if e["id"] in chosen]


def _recent_papers(conn: sqlite3.Connection, entity_id: str, start: str, end: str, limit: int = 8) -> List[Dict[str, Any]]:
    expr = _paper_date_sql()
    rows = _q(
        conn,
        f"SELECT p.title, p.url, p.journal, {expr} AS d FROM mentions m JOIN papers p ON p.id = m.paper_id "
        f"WHERE m.kind='instrument' AND m.entity_id=? AND {expr} >= ? AND {expr} <= ? "
        f"GROUP BY p.id ORDER BY d DESC LIMIT ?",
        (entity_id, start, end, int(limit)),
    )
    return [{"title": t or "(untitled)", "url": u or "", "journal": j or "", "date": d or ""} for t, u, j, d in rows]


def _company_entities(conn: sqlite3.Connection, weeks: List[str], start: str, end: str,
                      covered_from: Optional[str] = None) -> List[Dict[str, Any]]:
    expr = _paper_date_sql()
    rows = _q(
        conn,
        f"SELECT m.entity_id, MAX(m.entity_name), MAX(m.ticker), m.section, {expr} AS d, COUNT(DISTINCT m.paper_id) "
        f"FROM mentions m JOIN papers p ON p.id = m.paper_id "
        f"WHERE m.kind='company' AND {expr} >= ? AND {expr} <= ? AND {_week_known_sql()} "
        f"GROUP BY m.entity_id, m.section, d",
        (start, end),
    )
    meta: Dict[str, Dict[str, str]] = {}
    roles: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: {"author": defaultdict(int), "funding_coi": defaultdict(int), "subject": defaultdict(int)}
    )
    for eid, name, ticker, section, d, n in rows:
        meta.setdefault(eid, {"name": name or eid, "ticker": ticker or ""})
        w = week_of(d)
        if not w:
            continue
        if section in AUTHOR_SECTIONS:
            role = "author"
        elif section in FUNDING_SECTIONS:
            role = "funding_coi"
        elif section in SUBJECT_SECTIONS:
            role = "subject"
        else:
            continue
        roles[eid][role][w] += n

    news_counts = _news_per_company(conn, weeks, start, end)
    entities = []
    for eid, buckets in roles.items():
        info = meta.get(eid, {"name": eid, "ticker": ""})
        author = _series(buckets["author"], weeks, covered_from)
        funding = _series(buckets["funding_coi"], weeks, covered_from)
        subject = _series(buckets["subject"], weeks, covered_from)
        news = news_counts.get(eid) or news_counts.get(info["name"]) or [0] * len(weeks)
        entities.append({
            "id": eid,
            "name": info["name"],
            "ticker": _ticker(info["ticker"]),
            "author": author,
            "funding_coi": funding,
            "subject": subject,
            "news": news,
            "total": _total(author) + _total(funding) + _total(subject),
            "news_total": _total(news),
            "unlisted": str(eid).startswith("unlisted:"),
        })
    entities.sort(key=lambda e: (-e["total"], e["name"]))
    return entities[:MAX_COMPANY_CARDS]


def _news_per_company(conn: sqlite3.Connection, weeks: List[str], start: str, end: str) -> Dict[str, List[int]]:
    rows = _q(
        conn,
        "SELECT COALESCE(NULLIF(substr(published,1,10),''), substr(first_seen,1,10)) AS d, companies_json "
        "FROM news_items WHERE COALESCE(NULLIF(substr(published,1,10),''), substr(first_seen,1,10)) >= ? "
        "AND COALESCE(NULLIF(substr(published,1,10),''), substr(first_seen,1,10)) <= ?",
        (start, end),
    )
    buckets: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for d, companies_json in rows:
        w = week_of(d)
        if not w:
            continue
        try:
            companies = json.loads(companies_json or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(companies, list):
            continue
        for c in companies:
            if isinstance(c, str):
                buckets[c][w] += 1
    return {c: _series(b, weeks) for c, b in buckets.items()}


def _subgroups(conn: sqlite3.Connection, weeks: List[str], start: str, end: str, tables: set,
               covered_from: Optional[str] = None) -> Dict[str, Any]:
    """Papers per week per subgroup: tracked-author hits, plus keyword assignment from extra_json when present."""
    expr, known = _paper_date_sql(), _week_known_sql()
    counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sources = set()
    if "tracked_author_hits" in tables:
        rows = _q(
            conn,
            f"SELECT t.subgroup, {expr} AS d, COUNT(DISTINCT t.paper_id) "
            f"FROM tracked_author_hits t JOIN papers p ON p.id = t.paper_id "
            f"WHERE {expr} >= ? AND {expr} <= ? AND {known} GROUP BY t.subgroup, d",
            (start, end),
        )
        for group, d, n in rows:
            w = week_of(d)
            if w and group:
                counts[str(group)][w] += n
                sources.add("tracked authors")

    keyword_rows = _q(
        conn,
        f"SELECT {expr} AS d, p.extra_json FROM papers p "
        f"WHERE {expr} >= ? AND {expr} <= ? AND {known} AND p.extra_json LIKE '%subgroup%'",
        (start, end),
    )
    for d, extra_json in keyword_rows:
        w = week_of(d)
        if not w:
            continue
        for group in _extra_subgroups(extra_json):
            counts[group][w] += 1
            sources.add("keyword assignment (extra_json)")

    groups = [
        {"id": g, "counts": _series(b, weeks, covered_from), "total": sum(b.values())}
        for g, b in counts.items()
    ]
    groups.sort(key=lambda g: (-g["total"], g["id"]))
    # every group is listed in the table; the chart keeps the largest and folds the rest into
    # "Other", for the same reason as the stacked vendor charts (no ninth distinguishable hue)
    series = _fold_tail({g["id"]: g["counts"] for g in groups}, MAX_STACK_SERIES)
    return {
        "available": bool(groups),
        "sources": sorted(sources),
        "groups": groups,
        "series": series,
        "folded": len(groups) > MAX_STACK_SERIES,
    }


def _extra_subgroups(extra_json: Optional[str]) -> List[str]:
    try:
        extra = json.loads(extra_json or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(extra, dict):
        return []
    value = extra.get("subgroups", extra.get("subgroup"))
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def _countries(conn: sqlite3.Connection, weeks: List[str], start: str, end: str,
               covered_from: Optional[str] = None) -> Dict[str, Any]:
    """Papers per week by country of first affiliation, when the store carries one."""
    expr, known = _paper_date_sql(), _week_known_sql()
    columns = _columns(conn, "papers")
    buckets: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    source = None
    if "country" in columns:
        rows = _q(
            conn,
            f"SELECT {expr} AS d, p.country, COUNT(*) FROM papers p "
            f"WHERE {expr} >= ? AND {expr} <= ? AND {known} AND p.country IS NOT NULL AND p.country != '' "
            f"GROUP BY d, p.country",
            (start, end),
        )
        for d, country, n in rows:
            w = week_of(d)
            if w:
                buckets[str(country)][w] += n
        source = "papers.country column"
    if not buckets:
        rows = _q(
            conn,
            f"SELECT {expr} AS d, p.extra_json FROM papers p "
            f"WHERE {expr} >= ? AND {expr} <= ? AND {known} AND p.extra_json LIKE '%countr%'",
            (start, end),
        )
        hits = 0
        for d, extra_json in rows:
            w = week_of(d)
            country = _extra_country(extra_json)
            if w and country:
                buckets[country][w] += 1
                hits += 1
        if hits:
            source = "extra_json.country"
    countries = [{"code": c, "counts": _series(b, weeks, covered_from), "total": sum(b.values())} for c, b in buckets.items()]
    countries.sort(key=lambda c: (-c["total"], c["code"]))
    folded = _fold_tail({c["code"]: c["counts"] for c in countries}, MAX_STACK_SERIES)
    return {
        "available": bool(countries),
        "source": source,
        "series": folded,
        "table": countries[:40],
    }


def _extra_country(extra_json: Optional[str]) -> str:
    try:
        extra = json.loads(extra_json or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(extra, dict):
        return ""
    value = extra.get("country")
    if isinstance(value, str) and value:
        return value
    countries = extra.get("countries")
    if isinstance(countries, list) and countries and isinstance(countries[0], str):
        return countries[0]
    return ""


def _timeseries(conn: sqlite3.Connection, tables: set) -> Dict[str, Any]:
    """The optional monthly `timeseries` table (source, entity_id, model, month, count)."""
    if "timeseries" not in tables:
        return {"available": False, "months": [], "series": {}, "by_entity": {}}
    cols = set(_columns(conn, "timeseries"))
    if not {"source", "month", "count"} <= cols:
        return {"available": False, "months": [], "series": {}, "by_entity": {}}
    rows = _q(conn, "SELECT source, month, SUM(count) FROM timeseries GROUP BY source, month ORDER BY month")
    months = sorted({str(m) for _, m, _ in rows})
    series: Dict[str, List[int]] = {}
    for src, month, total in rows:
        key = str(src or "unknown")
        series.setdefault(key, [0] * len(months))
        series[key][months.index(str(month))] = int(total or 0)
    by_entity: Dict[str, Dict[str, int]] = defaultdict(dict)
    if "entity_id" in cols:
        for eid, month, total in _q(
            conn,
            "SELECT entity_id, month, SUM(count) FROM timeseries WHERE entity_id IS NOT NULL AND entity_id != '' "
            "GROUP BY entity_id, month",
        ):
            by_entity[str(eid)][str(month)] = int(total or 0)
    return {"available": bool(months), "months": months, "series": series, "by_entity": dict(by_entity)}


# How many trailing months of the archive series are marked incomplete: archives index deposits with a lag
# and the weekly refresh re-fetches the current month and the two before it (timeseries.DEFAULT_REFRESH_MONTHS).
TIMESERIES_INCOMPLETE_MONTHS = 2
TIMESERIES_TABLE_MONTHS = 12   # the per-vendor table under each chart shows this many trailing months


def _empty_timeseries_page() -> Dict[str, Any]:
    return {"available": False, "months": [], "incomplete": [], "sources": [], "vendors": [], "entities": 0,
            "series": 0, "rows": 0, "fetched_at": "", "csv": "", "json": "data/timeseries.json",
            "table_months": TIMESERIES_TABLE_MONTHS}


def _slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in str(text).lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "vendor"


def _timeseries_labels(conn: sqlite3.Connection, tables: set, config_dir: Optional[Path],
                       entity_ids: Sequence[str]) -> Tuple[Dict[str, str], Dict[str, str], List[str]]:
    """(entity -> display name, entity -> vendor, entity order) for timeseries.html.

    Names and vendors come from config/instruments.yaml, then config/archive_models.yaml (`vendor`), then the
    mentions table (entity_name, vendor), then the id itself; the order is the config order of
    archive_models.yaml with unknown ids after it, alphabetically."""
    from . import timeseries as T

    specs: Dict[str, Any] = {}
    instruments: Dict[str, Dict[str, str]] = {}
    if config_dir is not None:
        specs = T.load_archive_models(Path(config_dir) / "archive_models.yaml")
        instruments = T.load_instrument_index(Path(config_dir) / "instruments.yaml")
    mention_meta: Dict[str, Tuple[str, str]] = {}
    if "mentions" in tables:
        for eid, name, vendor in _q(conn, "SELECT entity_id, MAX(entity_name), MAX(vendor) FROM mentions "
                                          "WHERE kind='instrument' AND entity_id IS NOT NULL AND entity_id != '' GROUP BY entity_id"):
            mention_meta[str(eid)] = (str(name or ""), str(vendor or ""))
    names: Dict[str, str] = {}
    vendors: Dict[str, str] = {}
    for eid in entity_ids:
        meta = instruments.get(eid) or {}
        spec = specs.get(eid)
        m_name, m_vendor = mention_meta.get(eid, ("", ""))
        names[eid] = meta.get("name") or m_name or eid
        vendors[eid] = meta.get("vendor") or (getattr(spec, "vendor", "") if spec else "") or m_vendor or "Other"
    order = [eid for eid in specs if eid in set(entity_ids)] + sorted(eid for eid in entity_ids if eid not in specs)
    return names, vendors, order


def _timeseries_page(conn: sqlite3.Connection, tables: set, config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Everything timeseries.html needs: per vendor, per entity (model), one list per source aligned to the
    month axis (`null` = never fetched, 0 = fetched and zero). An entity's rows are summed per month, which is
    exact whether the cache holds one row per entity or - an older layout - one row per vocabulary string."""
    from . import timeseries as T

    page = _empty_timeseries_page()
    if "timeseries" not in tables:
        return page
    cols = set(_columns(conn, "timeseries"))
    if not {"source", "entity_id", "month", "count"} <= cols:
        return page
    rows = _q(conn, "SELECT source, entity_id, month, SUM(count), COUNT(*) FROM timeseries "
                    "WHERE entity_id IS NOT NULL AND entity_id != '' AND month IS NOT NULL GROUP BY source, entity_id, month")
    if not rows:
        return page
    months = sorted({str(m) for _, _, m, _, _ in rows})
    index = {m: i for i, m in enumerate(months)}
    per_entity: Dict[str, Dict[str, List[Optional[int]]]] = {}
    n_rows = 0
    for source, eid, month, total, n in rows:
        series = per_entity.setdefault(str(eid), {}).setdefault(str(source or "unknown"), [None] * len(months))
        series[index[str(month)]] = int(total or 0)
        n_rows += int(n or 0)
    names, vendor_of, order = _timeseries_labels(conn, tables, config_dir, list(per_entity))
    present_sources = {s for e in per_entity.values() for s in e}
    sources = [s for s in T.SOURCES if s in present_sources] + sorted(present_sources - set(T.SOURCES))
    source_meta = [{"id": s, "label": {"sra": "SRA experiments", "ena": "ENA runs", "epmc": "Europe PMC papers"}.get(s, s),
                    "unit": T.UNITS.get(s, "count"), "note": T.UNIT_NOTES.get(s, "")} for s in sources]
    vendors: List[Dict[str, Any]] = []
    by_vendor: Dict[str, Dict[str, Any]] = {}
    used_slugs: set = set()
    for eid in order:
        vendor = vendor_of[eid]
        if vendor not in by_vendor:
            slug = _slug(vendor)
            while slug in used_slugs:
                slug += "-x"
            used_slugs.add(slug)
            by_vendor[vendor] = {"name": vendor, "slug": slug, "entities": []}
            vendors.append(by_vendor[vendor])
        by_vendor[vendor]["entities"].append({"id": eid, "name": names[eid], "series": per_entity[eid]})
    fetched = ""
    if "fetched_at" in cols:
        got = _q(conn, "SELECT MAX(fetched_at) FROM timeseries")
        fetched = str(got[0][0] or "") if got else ""
    page.update({
        "available": True, "months": months, "incomplete": months[-TIMESERIES_INCOMPLETE_MONTHS:], "sources": source_meta,
        "vendors": vendors, "entities": len(per_entity), "series": sum(len(e) for e in per_entity.values()), "rows": n_rows,
        "fetched_at": fetched,
    })
    return page


def _search_index(conn: sqlite3.Connection, start: str, end: str, max_papers: int) -> List[Dict[str, Any]]:
    expr = _paper_date_sql()
    has_scores = "scores" in _tables(conn)
    score_select = "s.final" if has_scores else "NULL"
    score_join = "LEFT JOIN scores s ON s.paper_id = p.id" if has_scores else ""
    rows = _q(
        conn,
        f"SELECT p.id, p.title, p.journal, {expr} AS d, p.url, p.kind, {score_select} "
        f"FROM papers p {score_join} WHERE {expr} >= ? AND {expr} <= ? "
        f"ORDER BY d DESC LIMIT ?",
        (start, end, int(max_papers)),
    )
    index = {
        r[0]: {
            "id": r[0], "title": r[1] or "(untitled)", "journal": r[2] or "", "date": r[3] or "",
            "url": r[4] or "", "kind": r[5] or "", "score": r[6], "instruments": [], "companies": [],
        }
        for r in rows
    }
    if not index:
        return []
    mention_rows = _q(
        conn,
        f"SELECT m.paper_id, m.kind, MAX(m.entity_name) FROM mentions m JOIN papers p ON p.id = m.paper_id "
        f"WHERE {expr} >= ? AND {expr} <= ? GROUP BY m.paper_id, m.kind, m.entity_id",
        (start, end),
    )
    for paper_id, kind, name in mention_rows:
        row = index.get(paper_id)
        if not row or not name:
            continue
        bucket = "instruments" if kind == "instrument" else "companies"
        if name not in row[bucket]:
            row[bucket].append(name)
    return list(index.values())


def collect(conn: sqlite3.Connection, weeks: int = 52, today: Optional[date] = None,
            max_papers: int = DEFAULT_MAX_PAPERS, config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Read every dataset the site needs. Missing tables yield empty sections, never an exception.
    `config_dir` (optional) names the instrument models and vendors on timeseries.html; without it the page
    falls back to the mentions table and then to the raw entity ids."""
    today = today or datetime.now(timezone.utc).date()
    weeks = max(2, int(weeks))
    axis = week_axis(today, weeks)
    start, end = axis[0], (monday(today) + timedelta(days=6)).isoformat()
    tables = _tables(conn)

    have_papers = "papers" in tables
    have_mentions = "mentions" in tables and have_papers
    have_archive = "archive_records" in tables
    have_news = "news_items" in tables

    # what the store's ingestion covered: weekly series are None before it, movers compare only alike-covered windows
    cov = _coverage(conn, tables)
    papers_since, archive_since = cov["papers_since"], cov["archive_since"]

    papers_week = _papers_per_week(conn, axis, start, end, papers_since) if have_papers else {
        "total": [0] * weeks, "by_kind": {}, "first_of_month": [0] * weeks, "first_of_month_total": 0,
        "padded_sources": list(PADDED_DATE_SOURCES),
    }
    papers_week["coverage"] = _papers_axis_coverage(axis, cov)
    instruments_vendor = _instrument_weeks_by_vendor(conn, axis, start, end, papers_since) if have_mentions else {"series": {}}
    archive_vendor = _archive_weeks_by_vendor(conn, axis, start, end, archive_since) if have_archive else {"series": {}}
    archive_vendor["coverage"] = _axis_coverage(axis, archive_since)
    all_instruments = (_instrument_entities(conn, axis, start, end, papers_since, archive_since)
                       if (have_mentions or have_archive) else [])
    company_entities = _company_entities(conn, axis, start, end, papers_since) if have_mentions else []
    if not have_news:
        for c in company_entities:
            c["news"], c["news_total"] = [0] * weeks, 0

    # movers over EVERY entity; cards for the union of the two top-N lists. A table whose two windows were not
    # ingested alike (a run, census or backfill starting or ending inside them) is suppressed, with the reason.
    window = _movers_window(axis, today)
    window["coverage"] = _movers_coverage(window, cov)
    comparable = {k: bool(v.get("comparable")) for k, v in window["coverage"].items()}
    labels = {e["id"]: e["name"] for e in all_instruments}
    movers_papers = (_movers(labels, {e["id"]: e["papers_per_week"] for e in all_instruments}, window)
                     if comparable.get("instrument_papers") else [])
    movers_archive = (_movers(labels, {e["id"]: e["experiments_per_week"] for e in all_instruments}, window)
                      if comparable.get("archive_experiments") else [])
    company_labels = {c["id"]: c["name"] for c in company_entities}
    movers_companies = _movers(
        company_labels,
        {c["id"]: [(a or 0) + (f or 0) + (s or 0) for a, f, s in zip(c["author"], c["funding_coi"], c["subject"])]
         for c in company_entities},
        window,
    ) if comparable.get("companies") else []
    instrument_cards = _select_instrument_cards(all_instruments, limit=MAX_INSTRUMENT_CARDS)
    for e in instrument_cards:
        e["recent_papers"] = _recent_papers(conn, e["id"], start, end) if have_mentions else []

    totals = {}
    for table in ("papers", "mentions", "archive_records", "news_items", "tracked_author_hits", "scores"):
        if table in tables:
            rows = _q(conn, f"SELECT COUNT(*) FROM {table}")
            totals[table] = rows[0][0] if rows else 0

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weeks": axis,
        "window": {"start": start, "end": end, "weeks": weeks},
        "totals": totals,
        "coverage": cov,
        "papers_per_week": papers_week,
        "instruments_by_vendor": instruments_vendor,
        "archive_by_vendor": archive_vendor,
        "movers": {"instrument_papers": movers_papers, "archive_experiments": movers_archive,
                   "companies": movers_companies},
        "movers_window": window,
        "instruments": instrument_cards,
        "instruments_total": len(all_instruments),
        "companies": company_entities,
        "subgroups": _subgroups(conn, axis, start, end, tables, papers_since) if have_papers else
                     {"available": False, "sources": [], "groups": [], "series": {}, "folded": False},
        "countries": _countries(conn, axis, start, end, papers_since) if have_papers else
                     {"available": False, "source": None, "series": {}, "table": []},
        "timeseries": _timeseries(conn, tables),
        "timeseries_page": _timeseries_page(conn, tables, config_dir),
        "papers": _search_index(conn, start, end, max_papers) if have_papers else [],
        "company_pages": _company_pages(conn, start, end, today) if have_mentions else _empty_company_pages(start, end),
        "skipped_queries": list(getattr(conn, "skipped_queries", [])),
    }


# ---------------------------------------------------------------------------- company profiles
def _empty_company_pages(start: str, end: str) -> Dict[str, Any]:
    return {"details": [], "pages": {}, "graph": {"window": {"start": start, "end": end}, "min_weight": 1, "nodes": [], "links": []}}


def _company_pages(conn: sqlite3.Connection, start: str, end: str, today: date) -> Dict[str, Any]:
    """Profile payloads for every company with >= COMPANY_PAGE_MIN_PAPERS distinct papers in the window, plus the
    co-mention graph. Company metadata comes from the mentions table (the dashboard reads only the database), so
    `type` is blank unless the store carries it."""
    from . import companies as C

    entries = C.entries_from_store(conn)
    counts = C.paper_counts(conn, start, end)
    details: List[Dict[str, Any]] = []
    for e in entries:
        c = counts.get(e["id"])
        if not c or c["papers"] < COMPANY_PAGE_MIN_PAPERS:
            continue
        d = C.company_detail(conn, e, today=today, windows=(90, 365), months=COMPANY_PAGE_MONTHS)
        d["window_papers"] = c["papers"]
        details.append(d)
    details.sort(key=lambda d: (-d["window_papers"], d["name"].lower()))
    pages = {d["id"]: f"companies/{d['slug']}.html" for d in details}
    for d in details:
        for co in d["companies"]:
            co["page"] = pages.get(co["id"], "")
    graph = C.comention_graph(conn, start, end, entries)
    for n in graph["nodes"]:
        n["page"] = pages.get(n["id"], "")
    return {"details": details, "pages": pages, "graph": graph}


# ---------------------------------------------------------------------------- rendering
def _json_blob(payload: Any) -> str:
    """JSON for an inline <script type="application/json"> block (no HTML-special characters)."""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _nav(active: str) -> str:
    items = []
    for href, label in NAV:
        current = ' aria-current="page"' if href == active else ""
        items.append(f'<a href="{href}"{current}>{label}</a>')
    return "\n      ".join(items)


_TICKER_PLACEHOLDERS = {"n/a", "na", "none", "null", "-", "--", "?"}


def _ticker(value: Any) -> str:
    """A ticker worth a pill, or '' for the placeholders the gazetteers use for entities without one
    (`n/a` for open-source software; the page used to render a literal 'n/a' tag)."""
    s = str(value or "").strip()
    return "" if s.lower() in _TICKER_PLACEHOLDERS else s


def _page(title: str, active: str, lede: str, body: str, payload: Any, script: str, generated: str) -> str:
    return f"""<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="assets/styles.css">
</head>
<body>
<header class="site-head">
  <div class="wrap">
    <div class="brand">Literature radar <span class="muted">trends</span></div>
    <nav class="nav">
      {_nav(active)}
    </nav>
    <button type="button" id="theme-toggle" class="theme-toggle" aria-label="Switch colour theme">Theme</button>
  </div>
</header>
<main class="wrap">
  <h1>{title}</h1>
  <p class="lede">{lede}</p>
{body}
</main>
<footer class="wrap site-foot">
  <p>Generated {generated} from the radar database this site was built from. Counts are of records in
  the store, not of everything published; see the project documentation for coverage and method.</p>
</footer>
<script type="application/json" id="page-data">{_json_blob(payload)}</script>
<script src="{CHARTJS_URL}" integrity="{CHARTJS_SRI}" crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script src="assets/app.js"></script>
<script>
{script}
</script>
</body>
</html>
"""


def _card(title: str, note: str, canvas_id: str, extra: str = "") -> str:
    note_html = f'<p class="note">{note}</p>' if note else ""
    return (
        f'  <section class="card">\n'
        f'    <h2>{title}</h2>\n'
        f'    {note_html}\n'
        f'    <div class="chart"><canvas id="{canvas_id}"></canvas></div>\n'
        f'    <div class="table-slot" id="{canvas_id}-table"></div>\n'
        f"{extra}"
        f"  </section>\n"
    )


def _movers_note(window: Dict[str, Any]) -> str:
    """One sentence saying exactly which weeks the movers tables compare."""
    span = int(window.get("span") or 0)
    if span < 1:
        return "Not enough complete weeks in this window to compare."
    recent, previous = window.get("recent") or [], window.get("previous") or []
    word = {1: "one", 2: "two", 3: "three", 4: "four"}.get(span, str(span))
    text = (f"The last {word} complete week{'s' if span != 1 else ''} ({recent[0]} to {recent[-1]}) against the "
            f"{word} before {'them' if span != 1 else 'it'} ({previous[0]} to {previous[-1]}).")
    if window.get("excludes_current_week"):
        text += " The current, unfinished week is left out."
    return text


def _uncovered_sentence(cov: Dict[str, Any], what: str) -> str:
    """The sentence a chart carries when its axis begins before the ingestion that feeds it: how many leading weeks
    were never fetched for (shaded, n/a when empty) and how much of the first covered week the start leaves."""
    n = int(cov.get("uncovered_weeks") or 0)
    if not n or not cov.get("since"):
        return ""
    plural = n != 1
    text = (f" {what} in this store starts {cov['since']}: the {n} earlier week{'s' if plural else ''} (shaded) "
            f"{'were' if plural else 'was'} never fetched for, and an empty one is shown as n/a, not as zero")
    days = int(cov.get("first_week_days") or 0)
    if cov.get("first_week") and 0 < days < 7:
        text += f"; the week of {cov['first_week']} holds {days} day{'s' if days != 1 else ''} of records"
    return text + "."


def _papers_note(data: Dict[str, Any]) -> str:
    pw = data["papers_per_week"]
    text = ("Every paper and preprint in the corpus with a day-precision publication date, bucketed by publication "
            "week (ISO weeks, Monday-keyed). One series: the trend is the point.")
    padded = int(pw.get("first_of_month_total") or 0)
    if padded:
        sources = ", ".join(pw.get("padded_sources") or [])
        text += (f" {padded:,} papers dated the 1st of a month by a source that pads month- and year-precision dates "
                 f"to it ({sources}) are left out of every weekly series on this site and listed in the table's "
                 "second column: their publication week is unknown, and the few really published on the 1st cannot "
                 "be told apart.")
    cov = pw.get("coverage") or {}
    text += _uncovered_sentence(cov, "Paper ingestion (the earliest run or backfill)")
    if cov.get("runs_week_index") is not None:
        text += (f" The dashed line marks {cov['runs_since']}, the first publication date the weekly multi-source runs "
                 "cover; earlier weeks hold backfilled publication metadata only, so a step there is a change of "
                 "ingestion, not of publishing.")
    return text


def _archive_note(data: Dict[str, Any]) -> str:
    text = ("Sequencing experiments made public in public archives in that week, by the vendor behind the "
            "declared instrument model. This is a census of machines producing data, not of papers.")
    return text + _uncovered_sentence(data["archive_by_vendor"].get("coverage") or {}, "The archive census")


def _movers_suppressed(window: Dict[str, Any]) -> str:
    """One sentence per movers table whose windows were not ingested alike, naming the layer and the dates."""
    parts = []
    for key, info in (window.get("coverage") or {}).items():
        if not info.get("comparable") and info.get("reason"):
            parts.append(f" {MOVERS_LABELS.get(key, key)}: suppressed, because {info['reason']}.")
    return "".join(parts)


def _index_html(data: Dict[str, Any]) -> str:
    body = (
        _card("Papers per week", _papers_note(data), "papers-week")
        + _card("Instruments named per week, by vendor",
                "Distinct papers whose title, abstract or Methods name an instrument from the gazetteer, "
                "stacked by the instrument's vendor. A paper naming two vendors counts once per vendor. "
                "Papers without a day-precision date are left out, as on the chart above.",
                "instruments-week")
        + _card("Archive experiments per week, by vendor", _archive_note(data), "archive-week")
        + '  <section class="card">\n    <h2>Top movers</h2>\n'
          f'    <p class="note">{_movers_note(data["movers_window"])} Every instrument and company in the store '
          'is ranked here, not only those with a card. Counts, not rates; a short corpus makes these noisy, '
          'which is why the raw numbers are shown beside the change. A table appears only when both windows were '
          f'ingested alike.{_movers_suppressed(data["movers_window"])}</p>\n'
          '    <div id="movers"></div>\n  </section>\n'
    )
    if data["timeseries"]["available"]:
        body += _card("Monthly series from the timeseries table",
                      "Pre-aggregated monthly counts written by other adapters (source, entity, model, month, count).",
                      "timeseries-month")
    payload = {
        "weeks": data["weeks"], "papers_per_week": data["papers_per_week"],
        "instruments_by_vendor": data["instruments_by_vendor"], "archive_by_vendor": data["archive_by_vendor"],
        "movers": data["movers"], "movers_window": data["movers_window"], "timeseries": data["timeseries"],
        "totals": data["totals"], "window": data["window"],
    }
    script = "litradar.renderOverview(litradar.pageData());"
    lede = (
        f'{data["totals"].get("papers", 0):,} papers, {data["totals"].get("mentions", 0):,} entity mentions and '
        f'{data["totals"].get("archive_records", 0):,} archive records in the store. '
        f'Window: {data["window"]["start"]} to {data["window"]["end"]} ({data["window"]["weeks"]} weeks).'
    )
    return _page("Overview", "index.html", lede, body, payload, script, data["generated"])


def _instruments_html(data: Dict[str, Any]) -> str:
    body = (
        '  <section class="card">\n'
        '    <h2>Filter</h2>\n'
        '    <div class="filters">\n'
        '      <label for="inst-filter">Instrument or vendor</label>\n'
        '      <input type="search" id="inst-filter" placeholder="type to filter" autocomplete="off">\n'
        '    </div>\n'
        '  </section>\n'
        '  <div id="instrument-cards"></div>\n'
    )
    payload = {"weeks": data["weeks"], "entities": data["instruments"], "timeseries": data["timeseries"],
               "papers_coverage": data["papers_per_week"].get("coverage"),
               "archive_coverage": data["archive_by_vendor"].get("coverage")}
    script = "litradar.renderInstruments(litradar.pageData());"
    shown, total = len(data["instruments"]), int(data.get("instruments_total") or len(data["instruments"]))
    coverage = (
        f"{shown} of {total} instruments with any signal in this window are shown: the {MAX_INSTRUMENT_CARDS} most "
        f"named in papers plus the {MAX_INSTRUMENT_CARDS} with the most archive experiments. "
        if total > shown else f"All {total} instruments with any signal in this window are shown. "
    )
    lede = (
        "One card per instrument: papers naming it per week, and sequencing experiments released per week whose "
        "declared model maps to it. The two measures sit in separate charts on purpose - they differ by orders of "
        f"magnitude, and a shared second axis would invent a correlation. {coverage}"
        "The movers on the overview rank every instrument, carded or not."
        + _uncovered_sentence(data["archive_by_vendor"].get("coverage") or {}, "The archive census")
        + _uncovered_sentence(data["papers_per_week"].get("coverage") or {}, "Paper ingestion (the earliest run or backfill)")
    )
    return _page("Instruments", "instruments.html", lede, body, payload, script, data["generated"])


def _companies_html(data: Dict[str, Any]) -> str:
    body = (
        '  <section class="card">\n'
        '    <h2>Filter</h2>\n'
        '    <div class="filters">\n'
        '      <label for="comp-filter">Company</label>\n'
        '      <input type="search" id="comp-filter" placeholder="type to filter" autocomplete="off">\n'
        '      <label class="checkbox"><input type="checkbox" id="comp-unlisted"> include institutions not in the gazetteer</label>\n'
        '    </div>\n'
        '  </section>\n'
        '  <div id="company-cards"></div>\n'
    )
    body += _company_links_html(data)
    pages = (data.get("company_pages") or {}).get("pages") or {}
    entities = [dict(e, page=pages.get(e["id"], "")) for e in data["companies"]]
    payload = {"weeks": data["weeks"], "entities": entities, "papers_coverage": data["papers_per_week"].get("coverage")}
    script = "litradar.renderCompanies(litradar.pageData());"
    lede = (
        "Mentions per week split by the role the company plays in the paper: author affiliation, funding or "
        "competing-interest statement, or subject of the work (title, abstract, Methods). News counts come from "
        "the stored headline feed."
    )
    return _page("Companies", "companies.html", lede, body, payload, script, data["generated"])


def _company_links_html(data: Dict[str, Any]) -> str:
    """The card on companies.html that links to the per-company profile pages and the co-mention graph."""
    pages = data.get("company_pages") or {}
    details = pages.get("details") or []
    links = " ".join(
        f'<a href="companies/{html.escape(d["slug"])}.html">{html.escape(d["name"])}</a> '
        f'<span class="muted">{int(d.get("window_papers", 0))}</span>'
        for d in details
    )
    body = (
        '  <section class="card">\n'
        '    <h2>Company profiles and co-mention graph</h2>\n'
        f'    <p class="note">One page per company with at least {COMPANY_PAGE_MIN_PAPERS} papers in this window: mentions by '
        'month, roles, recent papers, co-mentioned instruments and companies, news. The '
        '<a href="companies/graph.html">co-mention graph</a> draws every company that shares a paper with another '
        'one. The number after each name is its papers in the window.</p>\n'
    )
    if links:
        body += f'    <p class="profile-links">{links}</p>\n'
    else:
        body += '    <p class="empty">No company reaches the profile threshold in this window.</p>\n'
    return body + "  </section>\n"


def _nav_at(active: str, root: str) -> str:
    items = []
    for href, label in NAV:
        current = ' aria-current="page"' if href == active else ""
        items.append(f'<a href="{root}{href}"{current}>{label}</a>')
    return "\n      ".join(items)


def _nested_page(title: str, lede: str, body: str, payload: Any, script: str, generated: str,
                 root: str = "../", extra_scripts: str = "") -> str:
    """The `_page` shell for a page one directory down (companies/): assets and nav resolve through `root`."""
    return f"""<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{root}assets/styles.css">
</head>
<body>
<header class="site-head">
  <div class="wrap">
    <div class="brand">Literature radar <span class="muted">trends</span></div>
    <nav class="nav">
      {_nav_at("companies.html", root)}
    </nav>
    <button type="button" id="theme-toggle" class="theme-toggle" aria-label="Switch colour theme">Theme</button>
  </div>
</header>
<main class="wrap">
  <h1>{title}</h1>
  <p class="lede">{lede}</p>
{body}
</main>
<footer class="wrap site-foot">
  <p>Generated {generated} from the radar database this site was built from. Counts are of records in
  the store, not of everything published; see the project documentation for coverage and method.</p>
</footer>
<script type="application/json" id="page-data">{_json_blob(payload)}</script>
<script src="{CHARTJS_URL}" integrity="{CHARTJS_SRI}" crossorigin="anonymous" referrerpolicy="no-referrer"></script>
{extra_scripts}<script src="{root}assets/app.js"></script>
<script>
{script}
</script>
</body>
</html>
"""


def _slot_card(title: str, note: str, slot_id: str) -> str:
    note_html = f'<p class="note">{note}</p>' if note else ""
    return f'  <section class="card">\n    <h2>{title}</h2>\n    {note_html}\n    <div id="{slot_id}"></div>\n  </section>\n'


def _company_detail_html(d: Dict[str, Any], generated: str) -> str:
    """companies/<slug>.html: one company's profile (payload = litradar.companies.company_detail output)."""
    esc = html.escape
    w90 = (d.get("windows") or {}).get("90d") or {}
    w365 = (d.get("windows") or {}).get("365d") or {}
    tags = []
    if d.get("public") and _ticker(d.get("ticker")):
        tags.append(_ticker(d.get("ticker")))
    if d.get("type"):
        tags.append(str(d["type"]))
    tags += [f"{int(w90.get('papers', 0))} papers / 90 days", f"{int(w365.get('papers', 0))} papers / 365 days",
             f"{int(w365.get('news', 0))} news items / 365 days"]
    head = f'<h2>{esc(d["name"])}</h2>' + "".join(f'<span class="tag">{esc(t)}</span>' for t in tags)
    meta = []
    if d.get("parent"):
        meta.append(f"Parent: {esc(str(d['parent']))}")
    if d.get("domains"):
        meta.append("E-mail domains: " + esc(", ".join(d["domains"])))
    if d.get("aliases"):
        meta.append("Also written: " + esc(", ".join(d["aliases"][:8])))
    meta_html = "".join(f'<p class="note">{m}</p>' for m in meta)
    body = (
        '  <section class="card">\n'
        f'    <div class="entity-head">{head}</div>\n'
        f"{meta_html}"
        '    <p class="note"><a href="../companies.html">All companies</a> &middot; <a href="graph.html">Co-mention graph</a></p>\n'
        "  </section>\n"
        + _card("Mentions by month",
                f"Distinct papers naming the company per calendar month over the last {COMPANY_PAGE_MONTHS} months, "
                "with the summed mention count. A paper is dated by its publication date, else by the day it was first seen.",
                "comp-months")
        + _slot_card("Roles", "Distinct papers per role in the last 90 and 365 days. A paper counts once per role it "
                     "shows the company in: author affiliation, funding or competing-interest statement, subject "
                     "(title or abstract), supplier (Methods).", "comp-roles")
        + _slot_card("Recent papers", "Newest papers naming the company in the last 365 days, with the roles it plays in each.", "comp-papers")
        + _slot_card("Co-mentioned instruments", "Gazetteer instruments named in the same papers, by shared papers (last 365 days).", "comp-instruments")
        + _slot_card("Co-mentioned companies", "Companies named in the same papers, by shared papers (last 365 days). Linked names have a profile page.", "comp-companies")
        + _slot_card("News", "Headlines from the stored feed tagged with this company (last 365 days).", "comp-news")
    )
    if d.get("candidates"):
        body += _slot_card("Discovery candidates that resolve to this company",
                           "Rows of the candidate table whose spelling, alias or e-mail domain matches this entry.", "comp-candidates")
    lede = ("Company profile built from the radar store: how often the company appears in papers, in which role, "
            "next to which instruments and companies, and in the news feed.")
    return _nested_page(esc(d["name"]), lede, body, d, "litradar.renderCompanyDetail(litradar.pageData());", generated)


def _graph_default_min_weight(graph: Dict[str, Any]) -> int:
    weights = sorted((int(l_.get("weight", 1)) for l_ in graph.get("links") or []), reverse=True)
    if len(weights) <= GRAPH_MAX_DEFAULT_LINKS:
        return 1
    return max(2, weights[GRAPH_MAX_DEFAULT_LINKS - 1])


def _company_graph_html(graph: Dict[str, Any], generated: str) -> str:
    """companies/graph.html: the co-mention graph (d3 force layout) with the same data as tables."""
    nodes = graph.get("nodes") or []
    links = graph.get("links") or []
    default_min = _graph_default_min_weight(graph)
    body = (
        '  <section class="card">\n'
        '    <h2>Filter</h2>\n'
        '    <div class="filters">\n'
        '      <label for="graph-min">Minimum shared papers</label>\n'
        f'      <input type="number" id="graph-min" min="1" value="{default_min}">\n'
        '      <label for="graph-top">Companies shown (largest first)</label>\n'
        f'      <input type="number" id="graph-top" min="2" max="{max(2, len(nodes))}" value="{min(60, max(2, len(nodes)))}">\n'
        '      <label class="checkbox"><input type="checkbox" id="graph-labels" checked> show labels</label>\n'
        '    </div>\n'
        '    <div class="legend" id="graph-legend"></div>\n'
        '  </section>\n'
        '  <section class="card">\n'
        '    <h2>Co-mention graph</h2>\n'
        '    <p class="note">Each node is a company; an edge joins two companies named in the same paper, weighted by how many '
        'papers they share in the window. Node size follows papers in the window, colour follows the gazetteer type. Drag to '
        'rearrange, scroll to zoom, click a node to open its profile. The tables below carry the same numbers.</p>\n'
        '    <div class="graph"><svg id="graph" role="img" aria-label="Company co-mention graph"></svg></div>\n'
        '  </section>\n'
        + _slot_card("Strongest pairs", "Company pairs by shared papers, strongest first.", "graph-links")
        + _slot_card("Companies in the graph", "Every company with at least one shared paper in the window.", "graph-nodes")
    )
    payload = dict(graph, default_min=default_min, counts={"nodes": len(nodes), "links": len(links)})
    lede = (f"{len(nodes)} companies and {len(links)} co-mention pairs between "
            f"{html.escape(str((graph.get('window') or {}).get('start', '')))} and "
            f"{html.escape(str((graph.get('window') or {}).get('end', '')))}.")
    d3_tag = (f'<script src="{D3_URL}" integrity="{D3_SRI}" crossorigin="anonymous" referrerpolicy="no-referrer"></script>\n')
    return _nested_page("Company co-mention graph", lede, body, payload, "litradar.renderCompanyGraph(litradar.pageData());",
                        generated, extra_scripts=d3_tag)


def _subgroups_html(data: Dict[str, Any]) -> str:
    note = ("Assembled from tracked-author hits and, when the store carries them, keyword subgroup "
            "assignments in extra_json.")
    if data["subgroups"].get("folded"):
        note += (f" The chart keeps the {MAX_STACK_SERIES} largest subgroups and folds the rest into 'Other'; "
                 "the table below lists every subgroup separately.")
    body = _card("Papers per week per subgroup", note, "subgroups-week")
    payload = {"weeks": data["weeks"], "subgroups": data["subgroups"],
               "papers_coverage": data["papers_per_week"].get("coverage")}
    script = "litradar.renderSubgroups(litradar.pageData());"
    sources = ", ".join(data["subgroups"]["sources"]) or "no subgroup signal in this store yet"
    lede = f"Sources in this build: {sources}."
    return _page("Subgroups", "subgroups.html", lede, body, payload, script, data["generated"])


def _countries_html(data: Dict[str, Any]) -> str:
    if data["countries"]["available"]:
        body = _card("Papers per week by country of first affiliation",
                     "Stacked by country; the tail beyond the top countries is folded into 'Other'.",
                     "countries-week")
    else:
        body = (
            '  <section class="card">\n'
            "    <h2>No country signal in this store yet</h2>\n"
            '    <p class="note">This page draws papers per week by the country of the first affiliation. It needs '
            "one of two things in the store, and neither is written by the base pipeline:</p>\n"
            "    <ol>\n"
            "      <li>a <code>country</code> column on <code>papers</code> (added by an adapter with "
            "<code>CREATE TABLE IF NOT EXISTS</code> / <code>ALTER TABLE ... ADD COLUMN</code>), or</li>\n"
            "      <li>a <code>country</code> string (or a <code>countries</code> list, first element used) inside "
            "each paper's <code>extra_json</code>.</li>\n"
            "    </ol>\n"
            '    <p class="note">Country is usually derived from the first author affiliation string or from an '
            "institution record carrying a country code; store it as an ISO 3166-1 alpha-2 code so the corpus stays "
            "comparable across sources and languages. Once either is present, rebuild the site and this page fills "
            "in automatically - no configuration.</p>\n"
            "  </section>\n"
        )
    payload = {"weeks": data["weeks"], "countries": data["countries"],
               "papers_coverage": data["papers_per_week"].get("coverage")}
    script = "litradar.renderCountries(litradar.pageData());"
    lede = (
        f'Source: {data["countries"]["source"]}.' if data["countries"]["available"]
        else "Not enabled in this store - the panel below explains the two ways to switch it on."
    )
    return _page("Countries", "countries.html", lede, body, payload, script, data["generated"])


def _search_html(data: Dict[str, Any]) -> str:
    body = (
        '  <section class="card">\n'
        '    <h2>Search the window</h2>\n'
        '    <div class="filters">\n'
        '      <label for="q">Query</label>\n'
        '      <input type="search" id="q" placeholder="title, journal, instrument, company" autocomplete="off">\n'
        '      <label for="kind">Kind</label>\n'
        '      <select id="kind"><option value="">all</option><option value="article">articles</option>'
        '<option value="preprint">preprints</option></select>\n'
        "    </div>\n"
        '    <p class="note" id="search-count"></p>\n'
        '    <div id="results"></div>\n'
        "  </section>\n"
    )
    payload = {"papers": data["papers"], "window": data["window"]}
    script = "litradar.renderSearch(litradar.pageData());"
    lede = (
        f'{len(data["papers"]):,} papers from {data["window"]["start"]} to {data["window"]["end"]}, searched in the '
        "browser - no server, no index service. Matching is a plain substring over title, journal, instruments and "
        "companies."
    )
    return _page("Search", "search.html", lede, body, payload, script, data["generated"])


def _timeseries_html(data: Dict[str, Any]) -> str:
    """timeseries.html: one card per vendor, its models as line series on one chart (measure and axis toggles
    are page-wide), the trailing months shaded as incomplete, a vendor total, and a 12-month table under each
    chart. Every name on the page comes from configuration or the store and is escaped on the way in; the
    client-side script only ever writes text nodes."""
    ts = data.get("timeseries_page") or _empty_timeseries_page()
    esc = html.escape
    if not ts["available"]:
        body = (
            '  <section class="card">\n'
            "    <h2>No archive time series in this database yet</h2>\n"
            '    <p class="note">This page charts monthly counts per instrument model from the <code>timeseries</code> table: '
            "NCBI SRA experiments, ENA runs and Europe PMC papers naming the model in Methods. The table is empty in the database "
            "this site was built from. Run <code>litradar timeseries --all</code> first (it fills the table from the archives; a "
            "first run over 36 months is a few thousand count requests, every later run refreshes only the newest three months), "
            "then rebuild the site.</p>\n"
            "  </section>\n"
        )
        lede = "Monthly archive deposits and papers per instrument model. Nothing to chart yet: run `litradar timeseries --all` first."
        return _page("Time series", "timeseries.html", esc(lede), body, {"timeseries": ts},
                     "litradar.renderTimeseries(litradar.pageData());", data["generated"])

    months = ts["months"]
    sources = ts["sources"]
    radios = []
    for i, s in enumerate(sources):
        radios.append(
            f'<label class="seg-item"><input type="radio" name="ts-source" value="{esc(s["id"])}"{" checked" if i == 0 else ""}> '
            f'{esc(s["label"])}</label>'
        )
    units = "; ".join(f'{esc(s["label"])} = {esc(s["note"])}' for s in sources)
    incomplete = ", ".join(esc(m) for m in ts["incomplete"])
    downloads = []
    if ts.get("csv"):
        downloads.append(f'<a href="{esc(ts["csv"])}" download>CSV, long format (the committed file: <code>{esc(ts["csv"])}</code>)</a>')
    else:
        downloads.append("no committed CSV was found next to this build (<code>litradar timeseries --all</code> writes one under "
                         "the digests directory)")
    downloads.append(f'<a href="{esc(ts["json"])}">JSON (this page\'s data, derived from the database)</a>')
    refreshed = f' Cache last refreshed {esc(ts["fetched_at"])}.' if ts.get("fetched_at") else ""
    body = (
        '  <section class="card">\n'
        "    <h2>Measure and axis</h2>\n"
        '    <div class="filters ts-controls">\n'
        f'      <fieldset class="seg" id="ts-source"><legend>Measure</legend>{"".join(radios)}</fieldset>\n'
        '      <fieldset class="seg" id="ts-scale"><legend>Axis</legend>'
        '<label class="seg-item"><input type="radio" name="ts-scale" value="linear" checked> linear</label>'
        '<label class="seg-item"><input type="radio" name="ts-scale" value="log"> log</label></fieldset>\n'
        "    </div>\n"
        f'    <p class="note"><span class="shade-key" aria-hidden="true"></span> Shaded months ({incomplete}) are <strong>incomplete '
        "(deposit lag)</strong>: archives index deposits and papers for weeks after the month ends, and the weekly refresh re-fetches "
        "the current month and the two before it, so those counts will still grow. One line per model, a dashed line for the vendor "
        f"total. Units: {units}. On the log axis a month with zero is a gap, not a point.</p>\n"
        f'    <p class="note ts-downloads">Download: {" · ".join(downloads)}.{refreshed}</p>\n'
        "  </section>\n"
    )
    for v in ts["vendors"]:
        model_names = ", ".join(esc(e["name"]) for e in v["entities"])
        n = len(v["entities"])
        body += (
            f'  <section class="card ts-vendor" id="ts-{esc(v["slug"])}">\n'
            f'    <h2>{esc(v["name"])}</h2>\n'
            f'    <p class="note">{n} model{"s" if n != 1 else ""}: {model_names}. The table shows the last '
            f'{ts["table_months"]} months of the selected measure; * marks an incomplete month.</p>\n'
            f'    <div class="chart ts-chart"><canvas id="ts-{esc(v["slug"])}-chart" role="img" aria-label="{esc(v["name"])}: monthly counts per model"></canvas></div>\n'
            f'    <div class="table-slot" id="ts-{esc(v["slug"])}-table"></div>\n'
            "  </section>\n"
        )
    lede = (
        f'{ts["series"]} monthly series for {ts["entities"]} instrument models from {len(ts["vendors"])} vendors, '
        f'{months[0]} to {months[-1]}: what the public archives and the open-access literature say about which sequencers are '
        "in use. Deposited experiments and runs are a census of machines producing public data, not of machines installed; "
        "clinical and commercial sequencing is never deposited. Measure and axis switches apply to every chart on the page."
    )
    return _page("Time series", "timeseries.html", esc(lede), body, {"timeseries": ts},
                 "litradar.renderTimeseries(litradar.pageData());", data["generated"])


def _timeseries_csv_name(names: Sequence[str]) -> str:
    """Pick the committed CSV to link: the `_all` selection first, then the latest `--to`, then the widest range."""
    best = ""
    best_key: Tuple[int, str, str] = (-1, "", "")
    for name in names:
        stem = name[:-4] if name.endswith(".csv") else name
        parts = stem.split("_", 3)   # timeseries, from, to, selection
        if len(parts) != 4 or parts[0] != "timeseries":
            continue
        _, start, end, selection = parts
        key = (1 if selection == "all" else 0, end, "".join(chr(255 - ord(c)) for c in start))  # earlier start ranks higher
        if key > best_key:
            best, best_key = name, key
    return best


def _copy_timeseries_csv(src_dir: Optional[Path], out_dir: Path) -> Tuple[str, int]:
    """Copy the committed time-series CSV into the site under the SAME relative path it has in the repository
    (digests/timeseries/<file>), so the page's link resolves both in a checkout and on the published site.
    Returns (relative path, bytes) or ('', 0) when there is nothing to copy."""
    if src_dir is None:
        return "", 0
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        return "", 0
    name = _timeseries_csv_name(sorted(p.name for p in src_dir.glob("timeseries_*.csv")))
    if not name:
        return "", 0
    rel = f"digests/timeseries/{name}"
    dest = out_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_dir / name, dest)
    return rel, dest.stat().st_size


# ---------------------------------------------------------------------------- assets
STYLES = """/* Chart surfaces and ink come from one place so light and dark swap in a single step. */
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11, 11, 11, 0.10);
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --series-3: #1baf7a;
  --series-4: #eda100;
  --series-5: #e87ba4;
  --series-6: #008300;
  --series-7: #6250d6;
  --series-8: #e34948;
  --good: #006300;
  --critical: #d03b3b;
  --shade: rgba(11, 11, 11, 0.07);
  --radius: 10px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --text-primary: #f0efec;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255, 255, 255, 0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --series-4: #c98500;
    --series-5: #d55181;
    --series-6: #008300;
    --series-7: #9085e9;
    --series-8: #e66767;
    --good: #0ca30c;
    --critical: #d03b3b;
    --shade: rgba(255, 255, 255, 0.08);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d;
  --surface: #1a1a19;
  --text-primary: #f0efec;
  --text-secondary: #c3c2b7;
  --text-muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255, 255, 255, 0.10);
  --series-1: #3987e5;
  --series-2: #d95926;
  --series-3: #199e70;
  --series-4: #c98500;
  --series-5: #d55181;
  --series-6: #008300;
  --series-7: #9085e9;
  --series-8: #e66767;
  --good: #0ca30c;
  --critical: #d03b3b;
  --shade: rgba(255, 255, 255, 0.08);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--page);
  color: var(--text-primary);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 0 16px; }
.site-head { border-bottom: 1px solid var(--border); background: var(--surface); }
.site-head .wrap { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; padding-top: 10px; padding-bottom: 10px; }
.brand { font-weight: 600; }
.muted, .note, .lede { color: var(--text-secondary); }
.note { font-size: 13px; margin: 0 0 12px; }
.lede { max-width: 72ch; overflow-wrap: anywhere; }
.nav { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-left: auto; }
.nav a { color: var(--text-secondary); text-decoration: none; padding: 4px 2px; border-bottom: 2px solid transparent; }
.nav a:hover { color: var(--text-primary); }
.nav a[aria-current="page"] { color: var(--text-primary); border-bottom-color: var(--series-1); }
.theme-toggle {
  background: transparent; color: var(--text-secondary); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 5px 10px; cursor: pointer; font: inherit; font-size: 13px;
}
h1 { font-size: 24px; margin: 24px 0 6px; }
h2 { font-size: 17px; margin: 0 0 6px; }
h3 { font-size: 15px; margin: 0 0 2px; }
a { color: var(--series-1); }
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px; margin: 16px 0;
  /* a card never widens the page: charts and tables shrink or scroll inside it (phone widths) */
  min-width: 0; max-width: 100%; overflow: hidden; box-sizing: border-box;
}
.chart { position: relative; height: 300px; min-width: 0; max-width: 100%; }
.chart canvas { max-width: 100% !important; }
.chart.small { height: 170px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid2 > * { min-width: 0; }
canvas { max-width: 100%; }
@media (max-width: 720px) { .grid2 { grid-template-columns: 1fr; } .chart { height: 240px; } }
.filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.filters label { color: var(--text-secondary); font-size: 13px; }
.filters input[type="search"], .filters select {
  font: inherit; padding: 6px 9px; border-radius: var(--radius); border: 1px solid var(--border);
  background: var(--page); color: var(--text-primary); min-width: 0; flex: 1 1 220px; max-width: 100%;
}
.checkbox { display: inline-flex; gap: 6px; align-items: center; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--text-secondary); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.up { color: var(--good); }
.down { color: var(--critical); }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; }
/* a wide table scrolls inside its card; it never widens the page */
.table-scroll { overflow-x: auto; }
.table-scroll.capped { max-height: 320px; overflow-y: auto; }
.table-scroll table { min-width: 100%; width: max-content; }
/* a table that must stay in view (search results) fits the card and lets its long text column wrap */
.table-scroll.fit table { width: 100%; min-width: 0; table-layout: auto; }
td.wrap, th.wrap { white-space: normal; min-width: 16em; max-width: 46em; overflow-wrap: anywhere; }
.entity-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; margin-bottom: 8px; }
.entity-head .tag {
  font-size: 12px; color: var(--text-secondary); border: 1px solid var(--border);
  border-radius: 999px; padding: 1px 8px;
}
.site-foot { color: var(--text-muted); font-size: 12px; padding-bottom: 32px; }
.empty { color: var(--text-secondary); font-size: 14px; }
/* company profile pages and the co-mention graph */
.profile-links { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 14px; line-height: 1.6; }
.profile-links .muted { font-size: 12px; }
a.tag { text-decoration: none; }
ul.plain { list-style: none; padding: 0; margin: 0; font-size: 14px; line-height: 1.7; }
.graph svg { width: 100%; height: 600px; display: block; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
.graph svg text { font-size: 11px; fill: var(--text-secondary); pointer-events: none; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; color: var(--text-secondary); margin-top: 10px; }
.legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; background: var(--swatch); vertical-align: middle; }
.filters input[type="number"] { width: 6em; }
/* archive time series page: segmented radio controls, the incomplete-months key, taller charts */
.ts-controls { gap: 12px 24px; align-items: flex-start; }
.seg { border: 0; padding: 0; margin: 0; min-width: 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.seg legend { float: left; color: var(--text-secondary); font-size: 13px; padding: 0; margin-right: 4px; }
.seg-item {
  display: inline-flex; align-items: center; gap: 5px; font-size: 13px; padding: 4px 10px; cursor: pointer;
  border: 1px solid var(--border); border-radius: 999px; color: var(--text-secondary); background: var(--page);
}
.seg-item:has(input:checked) { color: var(--text-primary); border-color: var(--series-1); }
.seg-item input { margin: 0; accent-color: var(--series-1); }
.shade-key { display: inline-block; width: 14px; height: 14px; vertical-align: -2px; border-radius: 3px; background: var(--shade); border: 1px solid var(--border); }
.ts-chart { height: 340px; }
@media (max-width: 720px) { .ts-chart { height: 260px; } }
.ts-vendor .table-slot { margin-top: 8px; }
.ts-vendor td.num.incomplete, .ts-vendor th.num.incomplete { background: var(--shade); }
.ts-vendor tr.total td { font-weight: 600; border-top: 1px solid var(--axis); }
.ts-downloads { overflow-wrap: anywhere; }
"""


APP_JS = r"""/* lit-radar dashboard: Chart.js wiring, theme handling, tables and client-side search.
   No framework, no build step. Every chart ships a data table so no value is hover-gated. */
var litradar = (function () {
  "use strict";

  var SERIES = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5", "--series-6",
                "--series-7", "--series-8"];
  var charts = [];
  var builders = [];

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function colorFor(i) { return cssVar(SERIES[i % SERIES.length]); }
  function otherColor() { return cssVar("--text-muted"); }

  function pageData() {
    var el = document.getElementById("page-data");
    return el ? JSON.parse(el.textContent) : {};
  }

  function el(tag, text, attrs) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    if (attrs) { Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); }); }
    return node;
  }

  /* "2026-10-12" -> "10-12"; anything that is not a plain date is left alone */
  function shortWeek(w) {
    return (typeof w === "string" && /^\d{4}-\d{2}-\d{2}$/.test(w)) ? w.slice(5) : String(w === undefined ? "" : w);
  }

  function baseOptions(opts) {
    opts = opts || {};
    var muted = cssVar("--text-muted");
    var grid = cssVar("--grid");
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          display: !!opts.legend,
          position: "bottom",
          labels: { color: cssVar("--text-secondary"), boxWidth: 10, boxHeight: 10, usePointStyle: true,
                    pointStyle: opts.pointStyle || "rect", padding: 12 }
        },
        tooltip: {
          backgroundColor: cssVar("--surface"),
          borderColor: cssVar("--border"),
          borderWidth: 1,
          titleColor: cssVar("--text-secondary"),
          bodyColor: cssVar("--text-primary"),
          padding: 10,
          displayColors: true,
          callbacks: {
            /* the axis is abbreviated to stay legible; the tooltip always names the full period */
            title: function (items) {
              var label = items.length ? items[0].label : "";
              return /^\d{4}-\d{2}-\d{2}$/.test(label) ? "Week of " + label : label;
            }
          }
        }
      },
      scales: {
        x: {
          stacked: !!opts.stacked,
          grid: { display: false },
          ticks: {
            color: muted,
            maxRotation: 0,
            autoSkipPadding: 18,
            callback: function (value, index) {
              return shortWeek(this.getLabelForValue(index));
            }
          },
          border: { color: cssVar("--axis") }
        },
        y: {
          stacked: !!opts.stacked,
          beginAtZero: true,
          grid: { color: grid, drawTicks: false },
          ticks: { color: muted, precision: 0, callback: function (v) { return compact(v); } },
          border: { display: false }
        }
      }
    };
  }

  function sum(values) {
    return (values || []).reduce(function (a, b) { return a + (b || 0); }, 0);
  }

  /* a null in a weekly series means "not covered: no run, census or backfill fetched for this week", and the
     tables say so instead of printing a 0 that would read as a measured nothing */
  function cell(v) {
    return v === null ? "n/a" : (v || 0);
  }

  /* Coverage marks. `shades` greys the leading weeks an ingestion start leaves uncovered (the table shows n/a
     there); `boundaries` draw a dashed line where the ingestion changed. The tooltip title carries the same
     words, so the caveat travels with the number. */
  function coveragePlugin(marks) {
    return {
      id: "litradarCoverage",
      beforeDatasetsDraw: function (chart) {
        var x = chart.scales.x, area = chart.chartArea;
        if (!x || !area) { return; }
        var n = (chart.data.labels || []).length;
        function edge(i) {
          if (i <= 0) { return area.left; }
          if (i >= n) { return area.right; }
          return (x.getPixelForValue(i - 1) + x.getPixelForValue(i)) / 2;
        }
        var ctx = chart.ctx;
        ctx.save();
        (marks.shades || []).forEach(function (sh) {
          var right = Math.min(Math.max(edge(sh.to), area.left), area.right);
          if (right > area.left) {
            ctx.fillStyle = cssVar("--shade") || "rgba(0,0,0,0.07)";
            ctx.fillRect(area.left, area.top, right - area.left, area.bottom - area.top);
          }
        });
        (marks.boundaries || []).forEach(function (b) {
          var px = edge(b.at);
          ctx.strokeStyle = cssVar("--text-muted") || "#888888";
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 4]);
          ctx.beginPath();
          ctx.moveTo(px, area.top);
          ctx.lineTo(px, area.bottom);
          ctx.stroke();
        });
        ctx.restore();
      }
    };
  }

  function coverageTitle(marks) {
    return function (items) {
      if (!items.length) { return ""; }
      var i = items[0].dataIndex, label = items[0].label;
      var title = /^\d{4}-\d{2}-\d{2}$/.test(label) ? "Week of " + label : label;
      (marks.shades || []).forEach(function (sh) { if (i < sh.to && sh.label) { title += " (" + sh.label + ")"; } });
      (marks.boundaries || []).forEach(function (b) {
        var word = i < b.at ? b.before : b.after;
        if (word) { title += " (" + word + ")"; }
      });
      return title;
    };
  }

  function withCoverage(options, marks) {
    if (!marks) { return { options: options, plugins: [] }; }
    options.plugins.tooltip.callbacks.title = coverageTitle(marks);
    return { options: options, plugins: [coveragePlugin(marks)] };
  }

  /* marks from the coverage block the build writes beside a series: the leading weeks nothing fetched for,
     and where the weekly runs begin when backfilled weeks precede them */
  function coverageMarks(cov, uncoveredLabel) {
    if (!cov) { return null; }
    var marks = { shades: [], boundaries: [] };
    if (cov.uncovered_weeks > 0) { marks.shades.push({ to: cov.uncovered_weeks, label: uncoveredLabel }); }
    if (cov.runs_week_index !== null && cov.runs_week_index !== undefined) {
      marks.boundaries.push({ at: cov.runs_week_index, before: "backfilled metadata only",
                              after: "weekly runs from " + cov.runs_since });
    }
    return marks.shades.length || marks.boundaries.length ? marks : null;
  }

  /* Only http(s) URLs become links. Record URLs come from feeds and archives the site does not
     control, and the published page is public, so anything else is rendered as plain text. */
  function isWebUrl(href) {
    return typeof href === "string" && /^https?:\/\//i.test(href);
  }

  function compact(v) {
    var n = Number(v);
    if (!isFinite(n)) { return v; }
    if (Math.abs(n) >= 1000000) { return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M"; }
    if (Math.abs(n) >= 1000) { return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K"; }
    return String(n);
  }

  /* The chart library is the one thing loaded from a CDN. If it is blocked or offline, the page
     still has to work: every chart is optional decoration over a data table that is already in the
     DOM, so a failure replaces the canvas with a line of text and never aborts the render. */
  function safeBuild(build, canvas) {
    if (typeof Chart === "undefined") {
      noChart(canvas, "Chart library unavailable - the data table below has the same numbers.");
      return null;
    }
    try {
      return build(canvas);
    } catch (err) {
      noChart(canvas, "This chart could not be drawn - the data table below has the same numbers.");
      return null;
    }
  }

  function noChart(canvas, message) {
    var box = canvas.parentNode;
    if (!box || box.dataset.failed) { return; }
    box.dataset.failed = "1";
    box.style.height = "auto";
    box.textContent = "";
    box.appendChild(el("p", message, { "class": "empty" }));
  }

  function register(canvasId, build) {
    builders.push({ id: canvasId, build: build });
    var canvas = document.getElementById(canvasId);
    if (canvas) { charts.push(safeBuild(build, canvas)); }
  }

  function rebuildAll() {
    charts.forEach(function (c) { if (c && c.destroy) { c.destroy(); } });
    charts = builders.map(function (b) {
      var canvas = document.getElementById(b.id);
      return canvas ? safeBuild(b.build, canvas) : null;
    });
  }

  function lineChart(canvas, labels, series, opts) {
    opts = opts || {};
    var datasets = series.map(function (s, i) {
      var color = s.other ? otherColor() : colorFor(i);
      return {
        label: s.label,
        data: s.data,
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHitRadius: 16,
        tension: 0,
        fill: false
      };
    });
    var cfg = withCoverage(baseOptions({ legend: series.length > 1, pointStyle: "line" }), opts.coverage);
    return new Chart(canvas, {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: cfg.options,
      plugins: cfg.plugins
    });
  }

  function stackedBar(canvas, labels, series, opts) {
    opts = opts || {};
    var surface = cssVar("--surface");
    var datasets = series.map(function (s, i) {
      var color = s.other ? otherColor() : colorFor(i);
      return {
        label: s.label,
        data: s.data,
        backgroundColor: color,
        borderColor: surface,
        borderWidth: { top: 2 },
        borderRadius: 4,
        borderSkipped: "bottom",
        maxBarThickness: 24
      };
    });
    var cfg = withCoverage(baseOptions({ legend: series.length > 1, stacked: true }), opts.coverage);
    return new Chart(canvas, {
      type: "bar",
      data: { labels: labels, datasets: datasets },
      options: cfg.options,
      plugins: cfg.plugins
    });
  }

  function table(container, headers, rows, options) {
    options = options || {};
    container.textContent = "";
    if (!rows.length) {
      container.appendChild(el("p", options.empty || "No rows in this window.", { "class": "empty" }));
      return;
    }
    var wrapper;
    if (options.collapsible === false) {
      wrapper = el("div", null, { "class": options.fit ? "table-scroll fit" : "table-scroll" });
      container.appendChild(wrapper);
    } else {
      var details = el("details");
      details.appendChild(el("summary", options.summary || "Data table"));
      wrapper = el("div", null, { "class": "table-scroll capped" });
      details.appendChild(wrapper);
      container.appendChild(details);
    }
    var t = el("table");
    var thead = el("thead");
    var tr = el("tr");
    headers.forEach(function (h) {
      tr.appendChild(el("th", h.label, h.num ? { "class": "num" } : (h.wrap ? { "class": "wrap" } : null)));
    });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (row) {
      var r = el("tr");
      row.forEach(function (cell, i) {
        var head = headers[i] || {};
        var td = el("td", null, head.num ? { "class": "num" } : (head.wrap ? { "class": "wrap" } : null));
        if (cell && typeof cell === "object" && cell.href && isWebUrl(cell.href)) {
          var a = el("a", cell.text, { href: cell.href, rel: "noopener" });
          td.appendChild(a);
        } else if (cell && typeof cell === "object" && cell.href !== undefined) {
          /* a stored URL that is not http(s) (javascript:, data:, a bare DOI, junk) is shown, not linked */
          td.textContent = cell.text === null || cell.text === undefined ? "" : String(cell.text);
        } else if (cell && typeof cell === "object" && cell.cls) {
          td.textContent = cell.text;
          td.className = (head.num ? "num " : "") + cell.cls;
        } else {
          td.textContent = cell === null || cell === undefined ? "" : String(cell);
        }
        r.appendChild(td);
      });
      tbody.appendChild(r);
    });
    t.appendChild(tbody);
    wrapper.appendChild(t);
  }

  function seriesTable(slotId, weeks, series) {
    var slot = document.getElementById(slotId);
    if (!slot) { return; }
    var headers = [{ label: "Week" }].concat(series.map(function (s) { return { label: s.label, num: true }; }));
    var rows = weeks.map(function (w, i) {
      return [w].concat(series.map(function (s) { return cell(s.data[i]); }));
    });
    table(slot, headers, rows);
  }

  function asSeries(obj) {
    return Object.keys(obj || {}).map(function (k) {
      return { label: k, data: obj[k], other: k === "Other" };
    });
  }

  /* ---------------------------------------------------------------- pages */
  function renderOverview(data) {
    var weeks = data.weeks || [];
    var pw = data.papers_per_week || {};
    var papers = [{ label: "Papers", data: pw.total || [] }];
    var paperMarks = coverageMarks(pw.coverage, "before any run or backfill");
    register("papers-week", function (c) { return lineChart(c, weeks, papers, { coverage: paperMarks }); });
    /* the papers whose week is unknown are not charted, but the table still accounts for them */
    var paperColumns = papers.slice();
    if (sum(pw.first_of_month) > 0) { paperColumns.push({ label: "Dated to the 1st (not charted)", data: pw.first_of_month }); }
    seriesTable("papers-week-table", weeks, paperColumns);

    var inst = asSeries((data.instruments_by_vendor || {}).series);
    register("instruments-week", function (c) { return stackedBar(c, weeks, inst, { coverage: paperMarks }); });
    seriesTable("instruments-week-table", weeks, inst);

    var arch = asSeries((data.archive_by_vendor || {}).series);
    var archiveMarks = coverageMarks((data.archive_by_vendor || {}).coverage, "before the archive census");
    register("archive-week", function (c) { return stackedBar(c, weeks, arch, { coverage: archiveMarks }); });
    seriesTable("archive-week-table", weeks, arch);

    if (data.timeseries && data.timeseries.available) {
      var ts = asSeries(data.timeseries.series);
      var months = data.timeseries.months || [];
      register("timeseries-month", function (c) { return lineChart(c, months, ts); });
      seriesTable("timeseries-month-table", months, ts);
    }

    var holder = document.getElementById("movers");
    if (holder) {
      holder.textContent = "";
      var win = data.movers_window || {};
      var span = win.span || 0;
      var coverage = win.coverage || {};
      var recentLabel = span ? "Last " + span + (span === 1 ? " wk" : " wks") : "Recent";
      var priorLabel = span ? "Prior " + span + (span === 1 ? " wk" : " wks") : "Previous";
      [["Instruments, papers", "instrument_papers"],
       ["Instruments, archive experiments", "archive_experiments"],
       ["Companies, mentions", "companies"]].forEach(function (pair) {
        holder.appendChild(el("h3", pair[0]));
        var slot = el("div");
        holder.appendChild(slot);
        var info = coverage[pair[1]] || {};
        /* a suppressed table says so: the two windows were not ingested alike, and the note names the layer */
        var empty = info.comparable === false && info.reason
          ? "Suppressed for this window: the compared weeks were not ingested alike (the note above says which run, census or backfill starts or ends inside them)."
          : "Not enough history in this window.";
        var rows = ((data.movers || {})[pair[1]] || []).map(function (m) {
          var cls = m.delta > 0 ? "up" : (m.delta < 0 ? "down" : "");
          return [m.name, m.recent, m.previous,
                  { text: (m.delta > 0 ? "+" : "") + m.delta, cls: cls },
                  m.pct === null || m.pct === undefined ? "n/a" : (m.pct > 0 ? "+" : "") + m.pct + "%"];
        });
        table(slot, [{ label: "Entity" }, { label: recentLabel, num: true }, { label: priorLabel, num: true },
                     { label: "Change", num: true }, { label: "Change %", num: true }], rows,
              { collapsible: false, empty: empty });
      });
    }
    afterRender();
  }

  function renderInstruments(data) {
    var weeks = data.weeks || [];
    var host = document.getElementById("instrument-cards");
    if (!host) { return; }
    var entities = data.entities || [];
    if (!entities.length) {
      host.appendChild(el("p", "No instrument mentions or archive records in this window.", { "class": "empty" }));
      return;
    }
    var paperMarks = coverageMarks(data.papers_coverage, "before any run or backfill");
    var archiveMarks = coverageMarks(data.archive_coverage, "before the archive census");
    entities.forEach(function (e, idx) {
      var card = el("section", null, { "class": "card", "data-name": (e.name + " " + (e.vendor || "")).toLowerCase() });
      var head = el("div", null, { "class": "entity-head" });
      head.appendChild(el("h2", e.name));
      if (e.vendor) { head.appendChild(el("span", e.vendor, { "class": "tag" })); }
      if (e.ticker) { head.appendChild(el("span", e.ticker, { "class": "tag" })); }
      head.appendChild(el("span", e.papers_total + " papers", { "class": "tag" }));
      head.appendChild(el("span", e.experiments_total + " experiments", { "class": "tag" }));
      card.appendChild(head);

      var grid = el("div", null, { "class": "grid2" });
      var pending = [];
      ["papers", "experiments"].forEach(function (which) {
        var box = el("div");
        box.appendChild(el("p", which === "papers" ? "Papers naming it per week"
                                                   : "Archive experiments per week", { "class": "note" }));
        var values = which === "papers" ? e.papers_per_week : e.experiments_per_week;
        var label = which === "papers" ? "Papers" : "Experiments";
        if (sum(values) === 0) {
          /* an all-zero chart is a flat line pretending to be data; say so in words instead */
          box.appendChild(el("p", which === "papers" ? "No papers named it in this window."
                                                     : "No archive records mapped to it in this window.",
                              { "class": "empty" }));
        } else {
          var chartBox = el("div", null, { "class": "chart small" });
          var canvasId = "inst-" + idx + "-" + which;
          chartBox.appendChild(el("canvas", null, { id: canvasId }));
          box.appendChild(chartBox);
          pending.push([canvasId, values, label, which === "papers" ? paperMarks : archiveMarks]);
        }
        grid.appendChild(box);
      });
      card.appendChild(grid);

      var tableSlot = el("div");
      card.appendChild(tableSlot);
      var rows = weeks.map(function (w, i) {
        return [w, cell(e.papers_per_week[i]), cell(e.experiments_per_week[i])];
      });
      table(tableSlot, [{ label: "Week" }, { label: "Papers", num: true }, { label: "Experiments", num: true }], rows);

      var papersSlot = el("div");
      card.appendChild(papersSlot);
      var paperRows = (e.recent_papers || []).map(function (p) {
        return [p.date, { text: p.title, href: p.url }, p.journal];
      });
      table(papersSlot, [{ label: "Date" }, { label: "Recent papers" }, { label: "Venue" }], paperRows,
            { summary: "Recent papers", empty: "No papers in this window." });
      host.appendChild(card);
      /* charts are registered only once the canvas is in the document: Chart.js sizes to the
         laid-out element, and a detached canvas would render nothing at all */
      pending.forEach(function (job) {
        register(job[0], function (c) { return lineChart(c, weeks, [{ label: job[2], data: job[1] }], { coverage: job[3] }); });
      });
    });

    var filter = document.getElementById("inst-filter");
    if (filter) {
      filter.addEventListener("input", function () {
        var q = filter.value.toLowerCase();
        Array.prototype.forEach.call(host.children, function (card) {
          var name = card.getAttribute("data-name") || "";
          card.style.display = (!q || name.indexOf(q) !== -1) ? "" : "none";
        });
      });
    }
    afterRender();
  }

  function renderCompanies(data) {
    var weeks = data.weeks || [];
    var host = document.getElementById("company-cards");
    if (!host) { return; }
    var entities = data.entities || [];
    if (!entities.length) {
      host.appendChild(el("p", "No company mentions in this window.", { "class": "empty" }));
      return;
    }
    var paperMarks = coverageMarks(data.papers_coverage, "before any run or backfill");
    entities.forEach(function (e, idx) {
      var card = el("section", null, {
        "class": "card",
        "data-name": (e.name + " " + (e.ticker || "")).toLowerCase(),
        "data-unlisted": e.unlisted ? "1" : "0"
      });
      var head = el("div", null, { "class": "entity-head" });
      head.appendChild(el("h2", e.name));
      if (e.ticker) { head.appendChild(el("span", e.ticker, { "class": "tag" })); }
      head.appendChild(el("span", e.total + " mentions", { "class": "tag" }));
      head.appendChild(el("span", e.news_total + " news items", { "class": "tag" }));
      if (e.unlisted) { head.appendChild(el("span", "not in gazetteer", { "class": "tag" })); }
      if (e.page) { head.appendChild(el("a", "Profile", { "class": "tag", href: e.page })); }
      card.appendChild(head);

      var chartBox = el("div", null, { "class": "chart small" });
      var canvasId = "comp-" + idx;
      chartBox.appendChild(el("canvas", null, { id: canvasId }));
      card.appendChild(chartBox);
      var series = [
        { label: "Author affiliation", data: e.author },
        { label: "Funding / COI", data: e.funding_coi },
        { label: "Subject of the work", data: e.subject }
      ];

      var slot = el("div");
      card.appendChild(slot);
      var rows = weeks.map(function (w, i) {
        return [w, cell(e.author[i]), cell(e.funding_coi[i]), cell(e.subject[i]), (e.news || [])[i] || 0];
      });
      table(slot, [{ label: "Week" }, { label: "Author", num: true }, { label: "Funding / COI", num: true },
                   { label: "Subject", num: true }, { label: "News", num: true }], rows);
      host.appendChild(card);
      /* same rule as the instruments page: attach first, then build the chart */
      register(canvasId, function (c) { return stackedBar(c, weeks, series, { coverage: paperMarks }); });
    });

    function applyFilter() {
      var filter = document.getElementById("comp-filter");
      var unlisted = document.getElementById("comp-unlisted");
      var q = filter ? filter.value.toLowerCase() : "";
      var showUnlisted = unlisted ? unlisted.checked : true;
      Array.prototype.forEach.call(host.children, function (card) {
        var name = card.getAttribute("data-name") || "";
        var isUnlisted = card.getAttribute("data-unlisted") === "1";
        var ok = (!q || name.indexOf(q) !== -1) && (showUnlisted || !isUnlisted);
        card.style.display = ok ? "" : "none";
      });
    }
    ["comp-filter", "comp-unlisted"].forEach(function (id) {
      var node = document.getElementById(id);
      if (node) { node.addEventListener(node.type === "checkbox" ? "change" : "input", applyFilter); }
    });
    applyFilter();
    afterRender();
  }

  function renderSubgroups(data) {
    var weeks = data.weeks || [];
    var sub = data.subgroups || {};
    var groups = sub.groups || [];
    /* the chart draws the folded series (largest groups + "Other"); the table lists every group */
    var chartSeries = sub.series ? asSeries(sub.series)
                                 : groups.map(function (g) { return { label: g.id, data: g.counts }; });
    var series = groups.map(function (g) { return { label: g.id, data: g.counts }; });
    if (!series.length) {
      var slot = document.getElementById("subgroups-week-table");
      if (slot) {
        slot.appendChild(el("p", "No subgroup signal in this store yet. Tracked-author hits appear once "
          + "authors are configured; keyword assignment appears once papers carry a subgroup in extra_json.",
          { "class": "empty" }));
      }
      afterRender();
      return;
    }
    var marks = coverageMarks(data.papers_coverage, "before any run or backfill");
    register("subgroups-week", function (c) { return lineChart(c, weeks, chartSeries, { coverage: marks }); });
    seriesTable("subgroups-week-table", weeks, series);
    afterRender();
  }

  function renderCountries(data) {
    var weeks = data.weeks || [];
    var countries = data.countries || {};
    if (!countries.available) { afterRender(); return; }
    var series = asSeries(countries.series);
    var marks = coverageMarks(data.papers_coverage, "before any run or backfill");
    register("countries-week", function (c) { return stackedBar(c, weeks, series, { coverage: marks }); });
    seriesTable("countries-week-table", weeks, series);
    afterRender();
  }

  function renderSearch(data) {
    var papers = data.papers || [];
    var results = document.getElementById("results");
    var countNode = document.getElementById("search-count");
    var q = document.getElementById("q");
    var kind = document.getElementById("kind");
    if (!results) { return; }

    var haystacks = papers.map(function (p) {
      return (p.title + " " + p.journal + " " + (p.instruments || []).join(" ") + " "
              + (p.companies || []).join(" ")).toLowerCase();
    });

    function run() {
      var query = (q ? q.value : "").trim().toLowerCase();
      var want = kind ? kind.value : "";
      var rows = [];
      for (var i = 0; i < papers.length && rows.length < 300; i++) {
        var p = papers[i];
        if (want && p.kind !== want) { continue; }
        if (query && haystacks[i].indexOf(query) === -1) { continue; }
        rows.push([p.date, { text: p.title, href: p.url }, p.journal,
                   (p.instruments || []).join(", "), (p.companies || []).join(", ")]);
      }
      /* the title wraps and the table fits the card, so venue / instruments / companies stay in view
         instead of sitting in an unmarked horizontal scroll region */
      table(results, [{ label: "Date" }, { label: "Title", wrap: true }, { label: "Venue" },
                      { label: "Instruments" }, { label: "Companies" }], rows,
            { collapsible: false, fit: true, empty: "No matches." });
      if (countNode) {
        countNode.textContent = rows.length >= 300
          ? "Showing the first 300 matches of " + papers.length + " indexed papers."
          : rows.length + " matches of " + papers.length + " indexed papers.";
      }
    }
    if (q) { q.addEventListener("input", run); }
    if (kind) { kind.addEventListener("change", run); }
    run();
    afterRender();
  }

  /* ---------------------------------------------------------------- theme */
  /* ------------------------------------------------------- company profile (companies/<id>.html) */
  var ROLE_LABELS = { author: "Author affiliation", funding_coi: "Funding / COI", subject: "Subject of the work",
                      supplier: "Supplier (Methods)", other: "Other" };
  var ROLE_ORDER = ["author", "funding_coi", "subject", "supplier", "other"];

  /* a relative link (profile pages live next to each other); external URLs still go through isWebUrl */
  function localLink(text, href) {
    var a = el("a", text, { href: href });
    return a;
  }

  function linkedList(container, items, empty) {
    container.textContent = "";
    if (!items.length) { container.appendChild(el("p", empty, { "class": "empty" })); return; }
    var ul = el("ul", null, { "class": "plain" });
    items.forEach(function (it) {
      var li = el("li");
      if (it.href) { li.appendChild(localLink(it.text, it.href)); } else { li.appendChild(el("span", it.text)); }
      if (it.tail) { li.appendChild(el("span", " " + it.tail, { "class": "muted" })); }
      ul.appendChild(li);
    });
    container.appendChild(ul);
  }

  function renderCompanyDetail(d) {
    var monthly = d.monthly || [];
    var months = monthly.map(function (m) { return m.month; });
    var papers = monthly.map(function (m) { return m.papers || 0; });
    var mentionCounts = monthly.map(function (m) { return m.mentions || 0; });
    var monthSlot = document.getElementById("comp-months-table");
    if (monthSlot) {
      table(monthSlot, [{ label: "Month" }, { label: "Papers", num: true }, { label: "Mentions", num: true }],
            months.map(function (m, i) { return [m, papers[i], mentionCounts[i]]; }));
    }
    var canvas = document.getElementById("comp-months");
    if (canvas) {
      if (sum(papers) === 0) {
        noChart(canvas, "No papers named the company in the last " + months.length + " months.");
      } else {
        register("comp-months", function (c) {
          return lineChart(c, months, [{ label: "Papers", data: papers }, { label: "Mentions", data: mentionCounts }]);
        });
      }
    }

    var roles = document.getElementById("comp-roles");
    var windows = d.windows || {};
    var keys = Object.keys(windows);
    if (roles) {
      var headers = [{ label: "Role" }].concat(keys.map(function (k) { return { label: "Last " + k, num: true }; }));
      var rows = ROLE_ORDER.map(function (r) {
        return [ROLE_LABELS[r] || r].concat(keys.map(function (k) { return ((windows[k] || {}).roles || {})[r] || 0; }));
      });
      rows.push(["All papers"].concat(keys.map(function (k) { return (windows[k] || {}).papers || 0; })));
      rows.push(["News items"].concat(keys.map(function (k) { return (windows[k] || {}).news || 0; })));
      table(roles, headers, rows, { collapsible: false, fit: true });
    }

    var papersSlot = document.getElementById("comp-papers");
    if (papersSlot) {
      table(papersSlot, [{ label: "Date" }, { label: "Paper", wrap: true }, { label: "Venue" }, { label: "Roles" }],
            (d.recent_papers || []).map(function (p) {
              return [p.date || "n.d.", { text: p.title, href: p.url }, p.journal || "",
                      (p.roles || []).map(function (r) { return ROLE_LABELS[r] || r; }).join(", ")];
            }), { collapsible: false, fit: true, empty: "No papers in the last 365 days." });
    }

    var inst = document.getElementById("comp-instruments");
    if (inst) {
      table(inst, [{ label: "Instrument" }, { label: "Vendor" }, { label: "Shared papers", num: true }],
            (d.instruments || []).map(function (i) { return [i.name, i.vendor || "", i.papers]; }),
            { collapsible: false, fit: true, empty: "No gazetteer instrument shares a paper with this company." });
    }

    var cos = document.getElementById("comp-companies");
    if (cos) {
      linkedList(cos, (d.companies || []).map(function (c) {
        var href = c.page ? c.page.replace(/^companies\//, "") : "";
        return { text: c.name + (c.ticker ? " (" + c.ticker + ")" : ""), href: href, tail: c.papers + " shared" };
      }), "No other company shares a paper with this one.");
    }

    var news = document.getElementById("comp-news");
    if (news) {
      table(news, [{ label: "Date" }, { label: "Headline", wrap: true }, { label: "Feed" }],
            (d.news || []).map(function (n) { return [n.date || "n.d.", { text: n.title, href: n.url }, n.feed || ""]; }),
            { collapsible: false, fit: true, empty: "No news items tagged with this company." });
    }

    var cands = document.getElementById("comp-candidates");
    if (cands) {
      table(cands, [{ label: "Candidate" }, { label: "Status" }, { label: "Score", num: true }, { label: "Papers / items", num: true }],
            (d.candidates || []).map(function (c) { return [c.display_name, c.status, c.score, c.papers]; }),
            { collapsible: false, fit: true });
    }
    afterRender();
  }

  /* ------------------------------------------------------- co-mention graph (companies/graph.html) */
  var TYPE_SWATCH = { tools: 0, pharma: 1, diagnostics: 2, cro: 3, ai_bio: 4, synthesis: 5, software: 6, institute: 7 };

  function typeColor(type) {
    var i = TYPE_SWATCH[type];
    return i === undefined ? otherColor() : colorFor(i);
  }

  function renderCompanyGraph(data) {
    var allNodes = data.nodes || [];
    var allLinks = data.links || [];
    var byId = {};
    allNodes.forEach(function (n) { byId[n.id] = n; });
    var name = function (id) { return byId[id] ? byId[id].name : id; };

    var linksSlot = document.getElementById("graph-links");
    if (linksSlot) {
      table(linksSlot, [{ label: "Company" }, { label: "Company" }, { label: "Shared papers", num: true }],
            allLinks.slice(0, 300).map(function (l) { return [name(l.source), name(l.target), l.weight]; }),
            { summary: "Pairs (top 300)", empty: "No two companies share a paper in this window." });
    }
    var nodesSlot = document.getElementById("graph-nodes");
    if (nodesSlot) {
      table(nodesSlot, [{ label: "Company" }, { label: "Ticker" }, { label: "Type" }, { label: "Papers", num: true }, { label: "Partners", num: true }],
            allNodes.map(function (n) { return [n.name, n.ticker || "", n.type || "", n.papers, n.degree]; }),
            { summary: "Companies", empty: "No company in the graph." });
    }
    var legend = document.getElementById("graph-legend");
    if (legend) {
      var types = {};
      allNodes.forEach(function (n) { types[n.type || "unknown"] = true; });
      Object.keys(types).sort().forEach(function (t) {
        var span = el("span", t);
        span.style.setProperty("--swatch", typeColor(t));
        legend.appendChild(span);
      });
    }

    var svg = document.getElementById("graph");
    if (!svg) { afterRender(); return; }
    var box = svg.parentNode;
    if (typeof d3 === "undefined") {
      box.textContent = "";
      box.appendChild(el("p", "Graph library unavailable - the tables below carry the same pairs.", { "class": "empty" }));
      afterRender();
      return;
    }
    var minInput = document.getElementById("graph-min");
    var topInput = document.getElementById("graph-top");
    var labelsInput = document.getElementById("graph-labels");
    var sim = null;

    function draw() {
      var minW = Math.max(1, parseInt(minInput ? minInput.value : "1", 10) || 1);
      var top = Math.max(2, parseInt(topInput ? topInput.value : "60", 10) || 60);
      var keep = {};
      allNodes.slice(0, top).forEach(function (n) { keep[n.id] = true; });
      var links = allLinks.filter(function (l) { return l.weight >= minW && keep[l.source] && keep[l.target]; })
                          .map(function (l) { return { source: l.source, target: l.target, weight: l.weight }; });
      var used = {};
      links.forEach(function (l) { used[l.source] = true; used[l.target] = true; });
      var nodes = allNodes.filter(function (n) { return used[n.id]; }).map(function (n) {
        return { id: n.id, name: n.name, type: n.type, papers: n.papers, degree: n.degree, page: n.page, ticker: n.ticker };
      });
      if (sim) { sim.stop(); }
      var root = d3.select(svg);
      root.selectAll("*").remove();
      var width = svg.clientWidth || 900, height = svg.clientHeight || 600;
      if (!nodes.length) {
        root.append("text").attr("x", 16).attr("y", 32).text("No pairs at this threshold - lower the minimum shared papers.");
        return;
      }
      var g = root.append("g");
      root.call(d3.zoom().scaleExtent([0.3, 5]).on("zoom", function (ev) { g.attr("transform", ev.transform); }));
      var radius = function (n) { return 4 + Math.sqrt(n.papers || 1) * 1.6; };
      var link = g.append("g").attr("stroke", cssVar("--axis")).attr("stroke-opacity", 0.7)
        .selectAll("line").data(links).join("line")
        .attr("stroke-width", function (l) { return 1 + Math.log2(l.weight); });
      link.append("title").text(function (l) { return name(l.source.id || l.source) + " - " + name(l.target.id || l.target) + ": " + l.weight + " shared papers"; });
      var node = g.append("g").selectAll("g").data(nodes).join("g").attr("class", "node")
        .style("cursor", function (n) { return n.page ? "pointer" : "grab"; })
        .call(d3.drag()
          .on("start", function (ev, n) { if (!ev.active) { sim.alphaTarget(0.3).restart(); } n.fx = n.x; n.fy = n.y; })
          .on("drag", function (ev, n) { n.fx = ev.x; n.fy = ev.y; })
          .on("end", function (ev, n) { if (!ev.active) { sim.alphaTarget(0); } n.fx = null; n.fy = null; }));
      node.append("circle").attr("r", radius).attr("fill", function (n) { return typeColor(n.type); })
        .attr("stroke", cssVar("--surface")).attr("stroke-width", 1.5);
      node.append("title").text(function (n) { return n.name + (n.ticker ? " (" + n.ticker + ")" : "") + ": " + n.papers + " papers, " + n.degree + " partners"; });
      var showLabels = !labelsInput || labelsInput.checked;
      node.filter(function (n) { return showLabels && (n.papers >= 3 || nodes.length <= 40); })
        .append("text").attr("dx", function (n) { return radius(n) + 3; }).attr("dy", "0.35em").text(function (n) { return n.name; });
      node.on("click", function (ev, n) { if (n.page) { window.location.href = n.page.replace(/^companies\//, ""); } });
      sim = d3.forceSimulation(nodes)
        .force("link", d3.forceLink(links).id(function (n) { return n.id; }).distance(function (l) { return 90 - Math.min(60, l.weight * 4); }).strength(function (l) { return Math.min(1, 0.2 + l.weight / 40); }))
        .force("charge", d3.forceManyBody().strength(-160))
        .force("collide", d3.forceCollide().radius(function (n) { return radius(n) + 4; }))
        .force("center", d3.forceCenter(width / 2, height / 2))
        .on("tick", function () {
          link.attr("x1", function (l) { return l.source.x; }).attr("y1", function (l) { return l.source.y; })
              .attr("x2", function (l) { return l.target.x; }).attr("y2", function (l) { return l.target.y; });
          node.attr("transform", function (n) { return "translate(" + n.x + "," + n.y + ")"; });
        });
    }
    [minInput, topInput, labelsInput].forEach(function (inp) {
      if (inp) { inp.addEventListener("change", draw); }
    });
    var toggle = document.getElementById("theme-toggle");
    if (toggle) { toggle.addEventListener("click", function () { window.setTimeout(draw, 0); }); }
    afterRender();
    draw();
  }

  /* ------------------------------------------------------- archive time series (timeseries.html) */
  function renderTimeseries(data) {
    var ts = data.timeseries || {};
    if (!ts.available) { afterRender(); return; }
    var months = ts.months || [];
    var sources = ts.sources || [];
    var incomplete = ts.incomplete || [];
    var vendors = ts.vendors || [];
    var tableMonths = ts.table_months || 12;
    var state = { source: sources.length ? sources[0].id : "", scale: "linear" };
    var firstIncomplete = -1;
    for (var i = 0; i < months.length; i++) {
      if (incomplete.indexOf(months[i]) !== -1) { firstIncomplete = i; break; }
    }
    function sourceMeta() {
      for (var j = 0; j < sources.length; j++) { if (sources[j].id === state.source) { return sources[j]; } }
      return { id: state.source, label: state.source, unit: "" };
    }
    /* null = never fetched (a gap); 0 is a real zero */
    function values(entity) {
      var arr = (entity.series || {})[state.source] || [];
      return months.map(function (m, idx) { var v = arr[idx]; return (v === undefined || v === null) ? null : Number(v); });
    }
    function vendorTotal(vendor) {
      var cols = vendor.entities.map(values);
      return months.map(function (m, idx) {
        var any = false, t = 0;
        cols.forEach(function (col) { if (col[idx] !== null) { any = true; t += col[idx]; } });
        return any ? t : null;
      });
    }
    function plotValues(vals) {
      /* a logarithmic axis has no zero: a zero month is a gap there, and the page says so */
      return state.scale === "log" ? vals.map(function (v) { return v !== null && v > 0 ? v : null; }) : vals;
    }
    var shadePlugin = {
      id: "litradarIncompleteShade",
      beforeDatasetsDraw: function (chart) {
        if (firstIncomplete < 0) { return; }
        var x = chart.scales.x, area = chart.chartArea;
        if (!x || !area) { return; }
        var left = firstIncomplete > 0
          ? (x.getPixelForValue(firstIncomplete - 1) + x.getPixelForValue(firstIncomplete)) / 2
          : area.left;
        left = Math.max(left, area.left);
        var ctx = chart.ctx;
        ctx.save();
        ctx.fillStyle = cssVar("--shade") || "rgba(0,0,0,0.07)";
        ctx.fillRect(left, area.top, area.right - left, area.bottom - area.top);
        ctx.restore();
      }
    };
    function lineDataset(label, vals, color, dashed) {
      return {
        label: label, data: plotValues(vals), borderColor: color, backgroundColor: color,
        borderWidth: dashed ? 1.5 : 2, borderDash: dashed ? [6, 4] : [], pointRadius: 0, pointHoverRadius: 4,
        pointHitRadius: 16, tension: 0, fill: false, spanGaps: false
      };
    }
    function buildChart(canvas, vendor) {
      var meta = sourceMeta();
      var datasets = vendor.entities.map(function (e, idx) { return lineDataset(e.name, values(e), colorFor(idx), false); });
      if (vendor.entities.length > 1) {
        datasets.push(lineDataset("All " + vendor.name + " models", vendorTotal(vendor), cssVar("--text-secondary"), true));
      }
      var options = baseOptions({ legend: true, pointStyle: "line" });
      options.plugins.tooltip.callbacks.title = function (items) {
        var label = items.length ? items[0].label : "";
        return incomplete.indexOf(label) !== -1 ? label + " (incomplete: deposit lag)" : label;
      };
      options.plugins.tooltip.callbacks.label = function (item) {
        var v = item.parsed.y;
        return " " + item.dataset.label + ": " + (v === null || v === undefined ? "no data" : v.toLocaleString("en-US") + " " + meta.unit);
      };
      if (state.scale === "log") {
        options.scales.y = {
          type: "logarithmic",
          grid: { color: cssVar("--grid"), drawTicks: false },
          ticks: { color: cssVar("--text-muted"), callback: function (v) { return compact(v); } },
          border: { display: false }
        };
      }
      return new Chart(canvas, { type: "line", data: { labels: months, datasets: datasets }, options: options, plugins: [shadePlugin] });
    }
    function renderTables() {
      var meta = sourceMeta();
      var last = months.slice(-tableMonths);
      var offset = months.length - last.length;
      vendors.forEach(function (vendor) {
        var slot = document.getElementById("ts-" + vendor.slug + "-table");
        if (!slot) { return; }
        var headers = [{ label: "Model (" + meta.unit + ")" }].concat(last.map(function (m) {
          return { label: incomplete.indexOf(m) !== -1 ? m + " *" : m, num: true, incomplete: incomplete.indexOf(m) !== -1 };
        }));
        function row(name, vals) {
          return [name].concat(last.map(function (m, j) {
            var v = vals[offset + j];
            return v === null ? "" : v.toLocaleString("en-US");
          }));
        }
        var rows = vendor.entities.map(function (e) { return row(e.name, values(e)); });
        if (vendor.entities.length > 1) { rows.push(row("All " + vendor.name + " models", vendorTotal(vendor))); }
        table(slot, headers, rows, { collapsible: false });
        /* mark the incomplete columns and the total row for the stylesheet; text only, no markup */
        var t = slot.querySelector("table");
        if (t) {
          headers.forEach(function (h, ci) {
            if (!h.incomplete) { return; }
            Array.prototype.forEach.call(t.querySelectorAll("tr"), function (tr) {
              var cell = tr.children[ci];
              if (cell) { cell.className = (cell.className ? cell.className + " " : "") + "incomplete"; }
            });
          });
          if (vendor.entities.length > 1) {
            var trs = t.querySelectorAll("tbody tr");
            if (trs.length) { trs[trs.length - 1].className = "total"; }
          }
        }
      });
    }
    vendors.forEach(function (vendor) {
      register("ts-" + vendor.slug + "-chart", function (c) { return buildChart(c, vendor); });
    });
    renderTables();
    function wire(fieldsetId, key) {
      var box = document.getElementById(fieldsetId);
      if (!box) { return; }
      box.addEventListener("change", function (ev) {
        var input = ev.target;
        if (!input || input.type !== "radio" || !input.checked) { return; }
        state[key] = input.value;
        rebuildAll();
        renderTables();
      });
    }
    wire("ts-source", "source");
    wire("ts-scale", "scale");
    afterRender();
  }

  function currentTheme() {
    try { return localStorage.getItem("litradar-theme") || "auto"; } catch (e) { return "auto"; }
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("litradar-theme", theme); } catch (e) { /* private mode */ }
  }
  function afterRender() {
    var button = document.getElementById("theme-toggle");
    if (!button || button.dataset.wired) { return; }
    button.dataset.wired = "1";
    var order = ["auto", "light", "dark"];
    function label() {
      var t = document.documentElement.getAttribute("data-theme") || "auto";
      button.textContent = "Theme: " + t;
    }
    button.addEventListener("click", function () {
      var now = document.documentElement.getAttribute("data-theme") || "auto";
      applyTheme(order[(order.indexOf(now) + 1) % order.length]);
      label();
      rebuildAll();
    });
    label();
  }

  applyTheme(currentTheme());
  if (window.matchMedia) {
    try {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        if ((document.documentElement.getAttribute("data-theme") || "auto") === "auto") { rebuildAll(); }
      });
    } catch (e) { /* older browsers: no live theme switch */ }
  }

  return {
    pageData: pageData,
    renderOverview: renderOverview,
    renderInstruments: renderInstruments,
    renderCompanies: renderCompanies,
    renderSubgroups: renderSubgroups,
    renderCountries: renderCountries,
    renderSearch: renderSearch,
    renderCompanyDetail: renderCompanyDetail,
    renderCompanyGraph: renderCompanyGraph,
    renderTimeseries: renderTimeseries
  };
})();
"""


# ---------------------------------------------------------------------------- build
def write_site(data: Dict[str, Any], out_dir: Path, timeseries_dir: Optional[Path] = None) -> Dict[str, int]:
    """Write every page, data file and asset. Returns {relative path: bytes}. `timeseries_dir` is where
    `litradar timeseries` wrote its CSVs (digests/timeseries in a checkout); the newest `_all` file is copied
    into the site at the same relative path so timeseries.html can link it."""
    out_dir = Path(out_dir)
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)

    written: Dict[str, int] = {}
    ts_page = data.get("timeseries_page") or _empty_timeseries_page()
    data["timeseries_page"] = ts_page
    csv_rel, csv_bytes = _copy_timeseries_csv(timeseries_dir, out_dir)
    ts_page["csv"] = csv_rel
    if csv_rel:
        written[csv_rel] = csv_bytes

    def put(rel: str, text: str) -> None:
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written[rel] = path.stat().st_size

    put("assets/styles.css", STYLES)
    put("assets/app.js", APP_JS)

    # the JSON files carry the same coverage blocks as the pages, so a consumer sees the same caveats a reader does
    papers_coverage = data["papers_per_week"].get("coverage")
    archive_coverage = data["archive_by_vendor"].get("coverage")
    data_files = {
        "data/overview.json": {
            "generated": data["generated"], "weeks": data["weeks"], "window": data["window"],
            "totals": data["totals"], "coverage": data.get("coverage"), "papers_per_week": data["papers_per_week"],
            "instruments_by_vendor": data["instruments_by_vendor"],
            "archive_by_vendor": data["archive_by_vendor"], "movers": data["movers"],
            "movers_window": data["movers_window"], "timeseries": data["timeseries"],
        },
        "data/instruments.json": {"weeks": data["weeks"], "entities": data["instruments"],
                                  "papers_coverage": papers_coverage, "archive_coverage": archive_coverage},
        "data/companies.json": {"weeks": data["weeks"], "entities": data["companies"], "papers_coverage": papers_coverage},
        "data/subgroups.json": {"weeks": data["weeks"], "subgroups": data["subgroups"], "papers_coverage": papers_coverage},
        "data/countries.json": {"weeks": data["weeks"], "countries": data["countries"], "papers_coverage": papers_coverage},
        "data/papers.json": {"window": data["window"], "papers": data["papers"]},
        "data/timeseries.json": dict(ts_page, generated=data["generated"]),
    }
    for rel, payload in data_files.items():
        put(rel, json.dumps(payload, ensure_ascii=False, default=str))

    put("index.html", _index_html(data))
    put("instruments.html", _instruments_html(data))
    put("timeseries.html", _timeseries_html(data))
    put("companies.html", _companies_html(data))
    put("subgroups.html", _subgroups_html(data))
    put("countries.html", _countries_html(data))
    put("search.html", _search_html(data))
    pages = data.get("company_pages") or _empty_company_pages(data["window"]["start"], data["window"]["end"])
    for d in pages.get("details") or []:
        put(f"companies/{d['slug']}.html", _company_detail_html(d, data["generated"]))
    put("companies/graph.html", _company_graph_html(pages.get("graph") or {}, data["generated"]))
    put("data/companies-graph.json", json.dumps(pages.get("graph") or {}, ensure_ascii=False, default=str))
    put(".nojekyll", "")  # GitHub Pages: serve assets/ and data/ verbatim
    return written


def build(db_path: Path, out_dir: Path, weeks: int = 52, max_papers: int = DEFAULT_MAX_PAPERS,
          today: Optional[date] = None, config_dir: Optional[Path] = None,
          timeseries_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Read the store and write the static site. Returns a manifest with per-file byte sizes.
    `config_dir` labels the archive time series (models, vendors); `timeseries_dir` holds the committed CSV
    that timeseries.html links (both optional; the page degrades to ids and to no CSV link)."""
    conn, tmp = connect_readonly(Path(db_path))
    try:
        data = collect(conn, weeks=weeks, today=today, max_papers=max_papers, config_dir=config_dir)
    finally:
        conn.close()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    files = write_site(data, Path(out_dir), timeseries_dir=timeseries_dir)
    # client-side search (search.html, search-index.json, search-entities.json): built by dashboard_search
    from .dashboard_search import write_search_site

    files.update(write_search_site(Path(db_path), Path(out_dir), data=data, today=today))
    return {
        "out": str(Path(out_dir)),
        "generated": data["generated"],
        "window": data["window"],
        "weeks": len(data["weeks"]),
        "counts": {
            "instruments": len(data["instruments"]),
            "instruments_total": data["instruments_total"],
            "companies": len(data["companies"]),
            "subgroups": len(data["subgroups"]["groups"]),
            "countries": len(data["countries"]["table"]),
            "papers_indexed": len(data["papers"]),
            "timeseries_months": len(data["timeseries"]["months"]),
            "timeseries_series": data["timeseries_page"]["series"],
            "timeseries_vendors": len(data["timeseries_page"]["vendors"]),
        },
        "movers_window": data["movers_window"],
        "company_pages": {
            "pages": len((data.get("company_pages") or {}).get("details") or []),
            "min_papers": COMPANY_PAGE_MIN_PAPERS,
            "graph_nodes": len(((data.get("company_pages") or {}).get("graph") or {}).get("nodes") or []),
            "graph_links": len(((data.get("company_pages") or {}).get("graph") or {}).get("links") or []),
        },
        "skipped_queries": data["skipped_queries"],
        "totals": data["totals"],
        "files": files,
        "bytes": sum(files.values()),
    }
