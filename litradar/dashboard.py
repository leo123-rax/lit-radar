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
back to the date part of `first_seen` for feeds that carry no publication date.

Country of first affiliation is used when the store can supply it: a `papers.country` column if
one exists, else `extra_json.country` / `extra_json.countries[0]`. Neither is written by the
base pipeline, so `countries.html` explains how to enable it rather than inventing a value.

Charting: Chart.js v4.5.1, pinned by exact version with a Subresource Integrity hash, loaded
from cdnjs (verified live 2026-10-17; the same bytes are on jsdelivr, see docs/dashboard.md).
Two measures of different scale never share a y-axis: each instrument gets two small charts
rather than one dual-axis chart.
"""
from __future__ import annotations

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

NAV = [
    ("index.html", "Overview"),
    ("instruments.html", "Instruments"),
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


# ---------------------------------------------------------------------------- sqlite helpers
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
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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
    conn = sqlite3.connect(str(copy))
    return conn, tmp


def _tables(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _q(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[tuple]:
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error as exc:  # a store written by an older version, or a missing table
        log.warning("query failed, section left empty: %s", exc)
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


def _series(buckets: Dict[str, Any], weeks: Sequence[str]) -> List[int]:
    return [int(buckets.get(w, 0)) for w in weeks]


def _paper_date_sql(alias: str = "p") -> str:
    """The date a paper is bucketed by: pub_date, else the date part of first_seen."""
    return f"COALESCE(NULLIF({alias}.pub_date,''), substr({alias}.first_seen,1,10))"


# ---------------------------------------------------------------------------- collection
def _papers_per_week(conn: sqlite3.Connection, weeks: List[str], start: str, end: str) -> Dict[str, Any]:
    expr = _paper_date_sql()
    rows = _q(
        conn,
        f"SELECT {expr} AS d, COALESCE(NULLIF(p.kind,''),'article') AS k, COUNT(*) "
        f"FROM papers p WHERE {expr} >= ? AND {expr} <= ? GROUP BY d, k",
        (start, end),
    )
    total: Dict[str, int] = defaultdict(int)
    by_kind: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for d, kind, n in rows:
        w = week_of(d)
        if not w:
            continue
        total[w] += n
        by_kind[kind][w] += n
    return {
        "total": _series(total, weeks),
        "by_kind": {k: _series(v, weeks) for k, v in sorted(by_kind.items())},
    }


def _fold_tail(series: Dict[str, List[int]], keep: int) -> Dict[str, List[int]]:
    """Keep the `keep` largest series by total; sum the rest into 'Other' (never invent a 9th hue)."""
    if len(series) <= keep:
        return dict(sorted(series.items(), key=lambda kv: -sum(kv[1])))
    ordered = sorted(series.items(), key=lambda kv: -sum(kv[1]))
    head = dict(ordered[:keep])
    tail = ordered[keep:]
    width = len(next(iter(series.values()))) if series else 0
    other = [0] * width
    for _, vals in tail:
        for i, v in enumerate(vals):
            other[i] += v
    head["Other"] = other
    return head


def _instrument_weeks_by_vendor(conn: sqlite3.Connection, weeks: List[str], start: str, end: str) -> Dict[str, Any]:
    expr = _paper_date_sql()
    rows = _q(
        conn,
        f"SELECT {expr} AS d, COALESCE(NULLIF(m.vendor,''),'unattributed') AS vendor, COUNT(DISTINCT m.paper_id) "
        f"FROM mentions m JOIN papers p ON p.id = m.paper_id "
        f"WHERE m.kind='instrument' AND {expr} >= ? AND {expr} <= ? GROUP BY d, vendor",
        (start, end),
    )
    by_vendor: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for d, vendor, n in rows:
        w = week_of(d)
        if w:
            by_vendor[vendor][w] += n
    series = {v: _series(b, weeks) for v, b in by_vendor.items()}
    return {"series": _fold_tail(series, MAX_STACK_SERIES)}


def _archive_weeks_by_vendor(conn: sqlite3.Connection, weeks: List[str], start: str, end: str) -> Dict[str, Any]:
    """Experiments per week by vendor.

    Records whose declared model is not in the gazetteer carry no vendor, so the archive's own
    `instrument_platform` stands in. That field is upper-case ("ILLUMINA") where the gazetteer is
    mixed-case ("Illumina"), so labels are merged case-insensitively and the mixed-case spelling
    wins - otherwise one vendor would appear as two stacked series.
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
        key = str(vendor).lower()
        current = display.get(key)
        if current is None or (current.isupper() and not str(vendor).isupper()):
            display[key] = str(vendor)
        by_vendor[key][w] += n
    series = {display.get(k, k): _series(b, weeks) for k, b in by_vendor.items()}
    return {"series": _fold_tail(series, MAX_STACK_SERIES)}


def _movers(labels: Dict[str, str], weekly: Dict[str, List[int]], weeks: List[str], span: int = 4) -> List[Dict[str, Any]]:
    """Week-over-week movers: the last `span` complete weeks against the `span` before them."""
    if len(weeks) < 2:
        return []
    span = max(1, min(span, len(weeks) // 2))
    out = []
    for key, vals in weekly.items():
        recent = sum(vals[-span:])
        previous = sum(vals[-2 * span:-span])
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


def _instrument_entities(conn: sqlite3.Connection, weeks: List[str], start: str, end: str) -> List[Dict[str, Any]]:
    expr = _paper_date_sql()
    rows = _q(
        conn,
        f"SELECT m.entity_id, MAX(m.entity_name), MAX(m.vendor), MAX(m.ticker), {expr} AS d, COUNT(DISTINCT m.paper_id) "
        f"FROM mentions m JOIN papers p ON p.id = m.paper_id "
        f"WHERE m.kind='instrument' AND {expr} >= ? AND {expr} <= ? GROUP BY m.entity_id, d",
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
        pw = _series(papers.get(eid, {}), weeks)
        aw = _series(experiments.get(eid, {}), weeks)
        entities.append({
            "id": eid,
            "name": info["name"],
            "vendor": info["vendor"],
            "ticker": info["ticker"],
            "papers_per_week": pw,
            "experiments_per_week": aw,
            "papers_total": sum(pw),
            "experiments_total": sum(aw),
        })
    entities.sort(key=lambda e: (-e["papers_total"], -e["experiments_total"], e["name"]))
    entities = entities[:MAX_INSTRUMENT_CARDS]

    for e in entities:
        recent = _q(
            conn,
            f"SELECT p.title, p.url, p.journal, {expr} AS d FROM mentions m JOIN papers p ON p.id = m.paper_id "
            f"WHERE m.kind='instrument' AND m.entity_id=? AND {expr} >= ? AND {expr} <= ? "
            f"GROUP BY p.id ORDER BY d DESC LIMIT 8",
            (e["id"], start, end),
        )
        e["recent_papers"] = [
            {"title": t or "(untitled)", "url": u or "", "journal": j or "", "date": d or ""} for t, u, j, d in recent
        ]
    return entities


def _company_entities(conn: sqlite3.Connection, weeks: List[str], start: str, end: str) -> List[Dict[str, Any]]:
    expr = _paper_date_sql()
    rows = _q(
        conn,
        f"SELECT m.entity_id, MAX(m.entity_name), MAX(m.ticker), m.section, {expr} AS d, COUNT(DISTINCT m.paper_id) "
        f"FROM mentions m JOIN papers p ON p.id = m.paper_id "
        f"WHERE m.kind='company' AND {expr} >= ? AND {expr} <= ? GROUP BY m.entity_id, m.section, d",
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
        author = _series(buckets["author"], weeks)
        funding = _series(buckets["funding_coi"], weeks)
        subject = _series(buckets["subject"], weeks)
        news = news_counts.get(eid) or news_counts.get(info["name"]) or [0] * len(weeks)
        entities.append({
            "id": eid,
            "name": info["name"],
            "ticker": info["ticker"],
            "author": author,
            "funding_coi": funding,
            "subject": subject,
            "news": news,
            "total": sum(author) + sum(funding) + sum(subject),
            "news_total": sum(news),
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


def _subgroups(conn: sqlite3.Connection, weeks: List[str], start: str, end: str, tables: set) -> Dict[str, Any]:
    """Papers per week per subgroup: tracked-author hits, plus keyword assignment from extra_json when present."""
    expr = _paper_date_sql()
    counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sources = set()
    if "tracked_author_hits" in tables:
        rows = _q(
            conn,
            f"SELECT t.subgroup, {expr} AS d, COUNT(DISTINCT t.paper_id) "
            f"FROM tracked_author_hits t JOIN papers p ON p.id = t.paper_id "
            f"WHERE {expr} >= ? AND {expr} <= ? GROUP BY t.subgroup, d",
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
        f"WHERE {expr} >= ? AND {expr} <= ? AND p.extra_json LIKE '%subgroup%'",
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
        {"id": g, "counts": _series(b, weeks), "total": sum(_series(b, weeks))}
        for g, b in counts.items()
    ]
    groups.sort(key=lambda g: (-g["total"], g["id"]))
    return {
        "available": bool(groups),
        "sources": sorted(sources),
        "groups": groups[:MAX_STACK_SERIES + 2],
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


def _countries(conn: sqlite3.Connection, weeks: List[str], start: str, end: str) -> Dict[str, Any]:
    """Papers per week by country of first affiliation, when the store carries one."""
    expr = _paper_date_sql()
    columns = _columns(conn, "papers")
    buckets: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    source = None
    if "country" in columns:
        rows = _q(
            conn,
            f"SELECT {expr} AS d, p.country, COUNT(*) FROM papers p "
            f"WHERE {expr} >= ? AND {expr} <= ? AND p.country IS NOT NULL AND p.country != '' GROUP BY d, p.country",
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
            f"WHERE {expr} >= ? AND {expr} <= ? AND p.extra_json LIKE '%countr%'",
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
    countries = [{"code": c, "counts": _series(b, weeks), "total": sum(_series(b, weeks))} for c, b in buckets.items()]
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
            max_papers: int = DEFAULT_MAX_PAPERS) -> Dict[str, Any]:
    """Read every dataset the site needs. Missing tables yield empty sections, never an exception."""
    today = today or datetime.now(timezone.utc).date()
    weeks = max(2, int(weeks))
    axis = week_axis(today, weeks)
    start, end = axis[0], (monday(today) + timedelta(days=6)).isoformat()
    tables = _tables(conn)

    have_papers = "papers" in tables
    have_mentions = "mentions" in tables and have_papers
    have_archive = "archive_records" in tables
    have_news = "news_items" in tables

    papers_week = _papers_per_week(conn, axis, start, end) if have_papers else {"total": [0] * weeks, "by_kind": {}}
    instruments_vendor = _instrument_weeks_by_vendor(conn, axis, start, end) if have_mentions else {"series": {}}
    archive_vendor = _archive_weeks_by_vendor(conn, axis, start, end) if have_archive else {"series": {}}
    instrument_entities = _instrument_entities(conn, axis, start, end) if (have_mentions or have_archive) else []
    company_entities = _company_entities(conn, axis, start, end) if have_mentions else []
    if not have_news:
        for c in company_entities:
            c["news"], c["news_total"] = [0] * weeks, 0

    labels = {e["id"]: e["name"] for e in instrument_entities}
    movers_papers = _movers(labels, {e["id"]: e["papers_per_week"] for e in instrument_entities}, axis)
    movers_archive = _movers(labels, {e["id"]: e["experiments_per_week"] for e in instrument_entities}, axis)
    company_labels = {c["id"]: c["name"] for c in company_entities}
    movers_companies = _movers(
        company_labels,
        {c["id"]: [a + f + s for a, f, s in zip(c["author"], c["funding_coi"], c["subject"])] for c in company_entities},
        axis,
    )

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
        "papers_per_week": papers_week,
        "instruments_by_vendor": instruments_vendor,
        "archive_by_vendor": archive_vendor,
        "movers": {"instrument_papers": movers_papers, "archive_experiments": movers_archive,
                   "companies": movers_companies},
        "instruments": instrument_entities,
        "companies": company_entities,
        "subgroups": _subgroups(conn, axis, start, end, tables) if have_papers else
                     {"available": False, "sources": [], "groups": []},
        "countries": _countries(conn, axis, start, end) if have_papers else
                     {"available": False, "source": None, "series": {}, "table": []},
        "timeseries": _timeseries(conn, tables),
        "papers": _search_index(conn, start, end, max_papers) if have_papers else [],
    }


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
  <p>Generated {generated} from the local corpus. Counts are of records in the store, not of
  everything published; see the project documentation for coverage and method.</p>
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


def _index_html(data: Dict[str, Any]) -> str:
    body = (
        _card("Papers per week", "Every paper and preprint in the corpus, bucketed by publication week "
              "(ISO weeks, Monday-keyed). One series: the trend is the point.", "papers-week")
        + _card("Instruments named per week, by vendor",
                "Distinct papers whose title, abstract or Methods name an instrument from the gazetteer, "
                "stacked by the instrument's vendor. A paper naming two vendors counts once per vendor.",
                "instruments-week")
        + _card("Archive experiments per week, by vendor",
                "Sequencing experiments made public in public archives in that week, by the vendor behind the "
                "declared instrument model. This is a census of machines producing data, not of papers.",
                "archive-week")
        + '  <section class="card">\n    <h2>Top movers</h2>\n'
          '    <p class="note">Last four weeks against the four before them. Counts, not rates; a short corpus '
          'makes these noisy, which is why the raw numbers are shown beside the change.</p>\n'
          '    <div id="movers"></div>\n  </section>\n'
    )
    if data["timeseries"]["available"]:
        body += _card("Monthly series from the timeseries table",
                      "Pre-aggregated monthly counts written by other adapters (source, entity, model, month, count).",
                      "timeseries-month")
    payload = {
        "weeks": data["weeks"], "papers_per_week": data["papers_per_week"],
        "instruments_by_vendor": data["instruments_by_vendor"], "archive_by_vendor": data["archive_by_vendor"],
        "movers": data["movers"], "timeseries": data["timeseries"], "totals": data["totals"],
        "window": data["window"],
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
    payload = {"weeks": data["weeks"], "entities": data["instruments"], "timeseries": data["timeseries"]}
    script = "litradar.renderInstruments(litradar.pageData());"
    lede = (
        "One card per instrument: papers naming it per week, and sequencing experiments released per week whose "
        "declared model maps to it. The two measures sit in separate charts on purpose - they differ by orders of "
        "magnitude, and a shared second axis would invent a correlation."
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
    payload = {"weeks": data["weeks"], "entities": data["companies"]}
    script = "litradar.renderCompanies(litradar.pageData());"
    lede = (
        "Mentions per week split by the role the company plays in the paper: author affiliation, funding or "
        "competing-interest statement, or subject of the work (title, abstract, Methods). News counts come from "
        "the stored headline feed."
    )
    return _page("Companies", "companies.html", lede, body, payload, script, data["generated"])


def _subgroups_html(data: Dict[str, Any]) -> str:
    body = _card("Papers per week per subgroup",
                 "Assembled from tracked-author hits and, when the store carries them, keyword subgroup "
                 "assignments in extra_json.", "subgroups-week")
    payload = {"weeks": data["weeks"], "subgroups": data["subgroups"]}
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
    payload = {"weeks": data["weeks"], "countries": data["countries"]}
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
.lede { max-width: 72ch; }
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
}
.chart { position: relative; height: 300px; }
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
.entity-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; margin-bottom: 8px; }
.entity-head .tag {
  font-size: 12px; color: var(--text-secondary); border: 1px solid var(--border);
  border-radius: 999px; padding: 1px 8px;
}
.site-foot { color: var(--text-muted); font-size: 12px; padding-bottom: 32px; }
.empty { color: var(--text-secondary); font-size: 14px; }
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
    return new Chart(canvas, {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: baseOptions({ legend: series.length > 1, pointStyle: "line" })
    });
  }

  function stackedBar(canvas, labels, series) {
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
    return new Chart(canvas, {
      type: "bar",
      data: { labels: labels, datasets: datasets },
      options: baseOptions({ legend: series.length > 1, stacked: true })
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
      wrapper = el("div", null, { "class": "table-scroll" });
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
      tr.appendChild(el("th", h.label, h.num ? { "class": "num" } : null));
    });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (row) {
      var r = el("tr");
      row.forEach(function (cell, i) {
        var head = headers[i] || {};
        var td = el("td", null, head.num ? { "class": "num" } : null);
        if (cell && typeof cell === "object" && cell.href) {
          var a = el("a", cell.text, { href: cell.href, rel: "noopener" });
          td.appendChild(a);
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
      return [w].concat(series.map(function (s) { return s.data[i] || 0; }));
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
    var papers = [{ label: "Papers", data: (data.papers_per_week || {}).total || [] }];
    register("papers-week", function (c) { return lineChart(c, weeks, papers); });
    seriesTable("papers-week-table", weeks, papers);

    var inst = asSeries((data.instruments_by_vendor || {}).series);
    register("instruments-week", function (c) { return stackedBar(c, weeks, inst); });
    seriesTable("instruments-week-table", weeks, inst);

    var arch = asSeries((data.archive_by_vendor || {}).series);
    register("archive-week", function (c) { return stackedBar(c, weeks, arch); });
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
      [["Instruments, papers", (data.movers || {}).instrument_papers],
       ["Instruments, archive experiments", (data.movers || {}).archive_experiments],
       ["Companies, mentions", (data.movers || {}).companies]].forEach(function (pair) {
        holder.appendChild(el("h3", pair[0]));
        var slot = el("div");
        holder.appendChild(slot);
        var rows = (pair[1] || []).map(function (m) {
          var cls = m.delta > 0 ? "up" : (m.delta < 0 ? "down" : "");
          return [m.name, m.recent, m.previous,
                  { text: (m.delta > 0 ? "+" : "") + m.delta, cls: cls },
                  m.pct === null || m.pct === undefined ? "n/a" : (m.pct > 0 ? "+" : "") + m.pct + "%"];
        });
        table(slot, [{ label: "Entity" }, { label: "Last 4 wks", num: true }, { label: "Prior 4 wks", num: true },
                     { label: "Change", num: true }, { label: "Change %", num: true }], rows,
              { collapsible: false, empty: "Not enough history in this window." });
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
          pending.push([canvasId, values, label]);
        }
        grid.appendChild(box);
      });
      card.appendChild(grid);

      var tableSlot = el("div");
      card.appendChild(tableSlot);
      var rows = weeks.map(function (w, i) {
        return [w, e.papers_per_week[i] || 0, e.experiments_per_week[i] || 0];
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
        register(job[0], function (c) { return lineChart(c, weeks, [{ label: job[2], data: job[1] }]); });
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
        return [w, e.author[i] || 0, e.funding_coi[i] || 0, e.subject[i] || 0, (e.news || [])[i] || 0];
      });
      table(slot, [{ label: "Week" }, { label: "Author", num: true }, { label: "Funding / COI", num: true },
                   { label: "Subject", num: true }, { label: "News", num: true }], rows);
      host.appendChild(card);
      /* same rule as the instruments page: attach first, then build the chart */
      register(canvasId, function (c) { return stackedBar(c, weeks, series); });
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
    var groups = ((data.subgroups || {}).groups) || [];
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
    register("subgroups-week", function (c) { return lineChart(c, weeks, series); });
    seriesTable("subgroups-week-table", weeks, series);
    afterRender();
  }

  function renderCountries(data) {
    var weeks = data.weeks || [];
    var countries = data.countries || {};
    if (!countries.available) { afterRender(); return; }
    var series = asSeries(countries.series);
    register("countries-week", function (c) { return stackedBar(c, weeks, series); });
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
      table(results, [{ label: "Date" }, { label: "Title" }, { label: "Venue" },
                      { label: "Instruments" }, { label: "Companies" }], rows,
            { collapsible: false, empty: "No matches." });
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
    renderSearch: renderSearch
  };
})();
"""


# ---------------------------------------------------------------------------- build
def write_site(data: Dict[str, Any], out_dir: Path) -> Dict[str, int]:
    """Write every page, data file and asset. Returns {relative path: bytes}."""
    out_dir = Path(out_dir)
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)

    written: Dict[str, int] = {}

    def put(rel: str, text: str) -> None:
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written[rel] = path.stat().st_size

    put("assets/styles.css", STYLES)
    put("assets/app.js", APP_JS)

    data_files = {
        "data/overview.json": {
            "generated": data["generated"], "weeks": data["weeks"], "window": data["window"],
            "totals": data["totals"], "papers_per_week": data["papers_per_week"],
            "instruments_by_vendor": data["instruments_by_vendor"],
            "archive_by_vendor": data["archive_by_vendor"], "movers": data["movers"],
            "timeseries": data["timeseries"],
        },
        "data/instruments.json": {"weeks": data["weeks"], "entities": data["instruments"]},
        "data/companies.json": {"weeks": data["weeks"], "entities": data["companies"]},
        "data/subgroups.json": {"weeks": data["weeks"], "subgroups": data["subgroups"]},
        "data/countries.json": {"weeks": data["weeks"], "countries": data["countries"]},
        "data/papers.json": {"window": data["window"], "papers": data["papers"]},
    }
    for rel, payload in data_files.items():
        put(rel, json.dumps(payload, ensure_ascii=False, default=str))

    put("index.html", _index_html(data))
    put("instruments.html", _instruments_html(data))
    put("companies.html", _companies_html(data))
    put("subgroups.html", _subgroups_html(data))
    put("countries.html", _countries_html(data))
    put("search.html", _search_html(data))
    put(".nojekyll", "")  # GitHub Pages: serve assets/ and data/ verbatim
    return written


def build(db_path: Path, out_dir: Path, weeks: int = 52, max_papers: int = DEFAULT_MAX_PAPERS,
          today: Optional[date] = None) -> Dict[str, Any]:
    """Read the store and write the static site. Returns a manifest with per-file byte sizes."""
    conn, tmp = connect_readonly(Path(db_path))
    try:
        data = collect(conn, weeks=weeks, today=today, max_papers=max_papers)
    finally:
        conn.close()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    files = write_site(data, Path(out_dir))
    return {
        "out": str(Path(out_dir)),
        "generated": data["generated"],
        "window": data["window"],
        "weeks": len(data["weeks"]),
        "counts": {
            "instruments": len(data["instruments"]),
            "companies": len(data["companies"]),
            "subgroups": len(data["subgroups"]["groups"]),
            "countries": len(data["countries"]["table"]),
            "papers_indexed": len(data["papers"]),
            "timeseries_months": len(data["timeseries"]["months"]),
        },
        "totals": data["totals"],
        "files": files,
        "bytes": sum(files.values()),
    }
