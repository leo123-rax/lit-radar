"""Trend detection over the stored corpus: what changed this week versus the recent past.

Pure SQL + Python over the existing SQLite schema (papers, mentions, archive_records,
tracked_author_hits). No network access and no external API: every number here is derived
from rows the pipeline already stored, so this module is safe to run against a database
snapshot and adds no dependencies.

Definitions
-----------
* **Week k** (k = 0 is the current week) covers the seven days ending ``until - 7k``; the
  current week is ``[until - 6 days, until]``. A paper's event date is its ``pub_date`` when
  present, otherwise the date part of ``first_seen`` (the same rule as ``Store.papers_in_window``).
  Archive records use ``first_public``.
* **Baseline** = weeks 1..``weeks`` (default 13) that actually contain data for that family
  (papers or archive records). Weeks before the corpus started are *not* counted as zeros, so
  a corpus with five weeks of history is compared against five weeks, not thirteen. A week whose
  family total is below ``min_week_fraction`` (default 0.25) of the median of the *other* positive
  weekly totals is a **partial week** (the first run of a corpus, an interrupted run, a 7-day pull
  whose extra boundary day lands alone in an earlier bucket) and is excluded from the baseline as
  well; the excluded weeks are listed in ``history`` and in the rendered header.
  ``min_week_fraction=0`` disables the rule.
* **Comparable week** (papers only): a baseline week whose *source mix* covers the current week's.
  A source is represented in a baseline week when its share of that week's papers is at least
  ``min_week_fraction`` of its share of the current week; the week is comparable when the sources
  represented in it supply at least ``min_source_coverage`` (default 0.75) of the current week's
  papers. A backfill is typically a capped, single-source sample by publication date while the live
  week is multi-source, and a z-score against such weeks would measure the step in coverage, not a
  change in the literature, so non-comparable weeks are left out of the paper baseline (listed in
  ``history.paper_weeks_dropped_source_mix``). ``min_source_coverage=0`` disables the rule.
* **mean / std** = arithmetic mean and sample standard deviation of the entity's weekly count
  over the baseline weeks (an entity absent in a baseline week counts as 0 there). ``std`` is
  ``None`` with fewer than two baseline weeks (undefined, rendered as ``n/a``).
* **z** = ``(current - mean) / max(std, 1.0)``. The floor of one count keeps integer series
  with a near-zero spread (3, 3, 3, 3 ...) from producing infinite scores. ``z`` is ``None``
  when fewer than ``min_baseline_weeks`` (default 3) baseline weeks have data.
* **notable** = ``|z| >= 2`` and at least 5 counts on the side that carries the signal: the
  current week for a riser, the baseline mean for a faller (a platform that dropped from 20
  papers a week to 0 is a faller worth seeing even though this week's count is 0).
* **new this week** = an entity whose earliest ``papers.first_seen`` across all its mentions
  falls in the current week (first appearance in the corpus). Archive instrument models have no
  ingestion timestamp, so their earliest ``first_public`` is used instead. When nothing in the
  store was seen before the current week (``cold_start``), every entity is new and the renderer
  says so instead of listing them all.
* **conversion** = a preprint whose ``published_doi`` resolves to another paper in the store
  (``papers.id = 'doi:' || lower(published_doi)``), reported in the week in which the journal
  version was published or first seen. A preprint row being re-upserted (new version, overlap
  day of consecutive pulls) does not re-report an old conversion; the flip side is that a
  ``published_doi`` back-filled onto a preprint whose journal version was already stored in an
  earlier week is never reported. Inside the digest the conversions are printed once, under the
  top list (``Store.preprint_conversions``, which also adds title matches); the digest passes
  ``conversions_note`` to the renderer, which then prints that pointer instead of its own table.
* **movers** (``top_movers``) come in two tiers that are never interleaved: rows with a z first,
  ranked by z, then rows without one, ranked by week-over-week change among themselves. A family
  whose z is suppressed (too few comparable baseline weeks) is never ranked by its raw delta next
  to z-scored rows (a 10,000-experiment archive delta would outrank every paper z): its rows only
  fill the places left below the z-scored movers, and only when its 'last week' is a complete,
  comparable bucket; otherwise they are left out and the renderer says why.

Week length versus the digest window
------------------------------------
Trend weeks are seven-day buckets ending on ``until``. The pipeline's own window
(``pipeline.window(days=7)``) is eight inclusive days, so a digest table and the trends section
in the same document can show different counts for the same entity. ``render_trends_markdown``
always states the bucket rule; pass ``window_note=digest_window_note(since, until)`` from the
digest renderer to spell out the digest window next to it.

Entity families
---------------
instruments (papers/week from ``mentions`` kind='instrument'; experiments/week from
``archive_records`` by declared ``instrument_model``), companies (papers/week, with the
same role split as the digest: author affiliation / funding-COI-ack / subject), subgroups
(papers/week by the keyword + OpenAlex-topic rule in ``config/subgroups.yaml``, plus
tracked-author papers/week from ``tracked_author_hits``) and journals (papers/week).

Public API
----------
``compute_trends(store, until, weeks=13, ...)`` returns a JSON-serialisable dict (including a
``warnings`` list: anything that silently degraded, such as a failed keyword query, is named
there and printed by the renderer); ``render_trends_markdown(trends, window_note=None)`` turns
it into a compact "What changed" section; ``top_movers(trends)`` picks the risers and fallers
the renderer shows; ``digest_window_note(since, until)`` builds the sentence that reconciles the
digest window with the seven-day trend week.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import DEFAULT_CONFIG_DIR, load_yaml

log = logging.getLogger(__name__)

NOTABLE_Z = 2.0
NOTABLE_MIN_COUNT = 5
MIN_BASELINE_WEEKS = 3
STD_FLOOR = 1.0
DEFAULT_WEEKS = 13
MIN_WEEK_FRACTION = 0.25  # a baseline week below this fraction of the median weekly total is partial
MIN_SOURCE_COVERAGE = 0.75  # a paper baseline week must be fed by sources supplying this share of the current week

_SOURCE_LABEL = "CASE WHEN p.source LIKE 'rss:%' THEN 'rss' ELSE COALESCE(NULLIF(p.source, ''), 'unknown') END"

ROLE_SECTIONS = {
    "as_author": ("affiliations",),
    "as_funder_or_coi": ("funding", "coi", "ack"),
    "as_subject": ("title", "abstract", "methods"),
}

# event date of a paper: publication date when known, else ingestion date (same rule as the store)
_EVENT_DATE = "CASE WHEN p.pub_date != '' THEN substr(p.pub_date, 1, 10) ELSE substr(p.first_seen, 1, 10) END"
# papers in the analysis window with their week index (0 = current week); malformed dates get wk NULL
_PW = f"""
WITH pw AS (
  SELECT id, CAST((julianday(:until) - julianday(d)) / 7 AS INTEGER) AS wk
  FROM (SELECT p.id AS id, {_EVENT_DATE} AS d FROM papers p)
  WHERE d >= :since AND d <= :until
)
"""


# ----------------------------------------------------------------------------- helpers
def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError as exc:
        raise ValueError(f"until must be YYYY-MM-DD, got {s!r}") from exc


def _iso(d: date) -> str:
    return d.isoformat()


def _round(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def _delta(
    by_week: Dict[int, int],
    weeks: int,
    active_baseline: Sequence[int],
    min_baseline_weeks: int,
    notable_z: float,
    min_count: int,
) -> Dict[str, Any]:
    """Current vs previous vs baseline statistics for one weekly series."""
    current = int(by_week.get(0, 0))
    previous = int(by_week.get(1, 0))
    baseline = [int(by_week.get(w, 0)) for w in active_baseline]
    n = len(baseline)
    mean = statistics.fmean(baseline) if n else None
    std = statistics.stdev(baseline) if n >= 2 else None  # undefined below two weeks
    z: Optional[float] = None
    if n >= min_baseline_weeks and mean is not None:
        z = (current - mean) / max(std if std is not None else 0.0, STD_FLOOR)
    notable = False
    if z is not None and abs(z) >= notable_z:
        gate = current if z > 0 else (mean or 0.0)
        notable = gate >= min_count
    delta = current - previous
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return {
        "current": current,
        "previous": previous,
        "delta": delta,
        "pct_change": _round(100.0 * delta / previous, 1) if previous else None,
        "mean": _round(mean),
        "std": _round(std),
        "z": _round(z),
        "notable": notable,
        "direction": direction,
        "baseline_weeks": n,
        "series": [int(by_week.get(w, 0)) for w in range(weeks, -1, -1)],
    }


def _week_ok(wk: Any, weeks: int) -> bool:
    return isinstance(wk, int) and 0 <= wk <= weeks


def _week_bounds(end: date, k: int) -> Tuple[str, str]:
    """(start, end) ISO dates of week k, where week 0 ends on `end`."""
    return _iso(end - timedelta(days=7 * k + 6)), _iso(end - timedelta(days=7 * k))


def _active_weeks(totals: Dict[int, int], weeks: int, min_week_fraction: float, end: date) -> Tuple[List[int], List[Dict[str, Any]]]:
    """Baseline weeks (1..weeks) with data, minus partial weeks.

    A week is partial when its family total is below `min_week_fraction` of the median of the
    *other* positive weekly totals (leave-one-out, so a week is never measured against itself and
    two weeks of history already separate a 20 % boundary-day bucket from a full week). With a
    single week of data nothing is compared, so nothing is dropped.
    Returns (active week indices, dropped weeks as {week, start, end, total, median}).
    """
    active = [w for w in range(1, weeks + 1) if totals.get(w, 0) > 0]
    dropped: List[Dict[str, Any]] = []
    if min_week_fraction > 0 and len(active) >= 2:
        keep: List[int] = []
        for w in active:
            med = float(statistics.median([totals[o] for o in active if o != w]))
            if totals[w] < min_week_fraction * med:
                start, stop = _week_bounds(end, w)
                dropped.append({"week": w, "start": start, "end": stop, "total": int(totals[w]), "median": _round(med, 1)})
            else:
                keep.append(w)
        active = keep
    return active, dropped


def _comparable_weeks(
    by_source: Dict[int, Dict[str, int]], weeks: int, min_week_fraction: float, min_source_coverage: float, end: date
) -> Tuple[List[int], List[Dict[str, Any]]]:
    """Baseline weeks (1..weeks) with papers whose source mix covers the current week's.

    A source is represented in a baseline week when its share of that week's papers is at least `min_week_fraction`
    of its share of the current week (shares, not counts, so a uniformly small partial week is left to the
    partial-week rule). The week is comparable when the sources represented in it supply at least
    `min_source_coverage` of the current week's papers. Returns (comparable week indices, dropped weeks as
    {week, start, end, total, covered_share, missing}); `missing` lists the current week's sources the week lacks,
    largest first.
    """
    current = by_source.get(0) or {}
    total0 = sum(current.values())
    with_data = [w for w in range(1, weeks + 1) if sum((by_source.get(w) or {}).values()) > 0]
    if min_source_coverage <= 0 or total0 <= 0:
        return with_data, []
    keep: List[int] = []
    dropped: List[Dict[str, Any]] = []
    for w in with_data:
        wk = by_source.get(w) or {}
        total_w = sum(wk.values())
        covered = 0
        missing: List[str] = []
        for src, n0 in current.items():
            share0 = n0 / total0
            share_w = wk.get(src, 0) / total_w
            if share_w > 0 and share_w >= min_week_fraction * share0:
                covered += n0
            else:
                missing.append(src)
        share = covered / total0
        if share >= min_source_coverage:
            keep.append(w)
        else:
            start, stop = _week_bounds(end, w)
            dropped.append({"week": w, "start": start, "end": stop, "total": int(total_w), "covered_share": round(share, 3),
                            "missing": sorted(missing, key=lambda s: (-current[s], s))})
    return keep, dropped


def _week_coverage(covered_from: Optional[str], prev_start: str, prev_end: str) -> Dict[str, Any]:
    """How much of 'last week' the runs (and, for papers, a recorded backfill) that built this store fetched for."""
    if covered_from is None or covered_from <= prev_start:
        days = 7
    elif covered_from > prev_end:
        days = 0
    else:
        days = (_parse_date(prev_end) - _parse_date(covered_from)).days + 1
    return {"covered_from": covered_from, "days_covered": days, "partial": days < 7}


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _collect(rows: Iterable[Tuple], weeks: int, key_cols: int, count_cols: int) -> Dict[Tuple, Dict[str, Dict[int, int]]]:
    """Group (key..., wk, count...) rows into {key: {metric_index: {wk: count}}}; drops bad weeks."""
    out: Dict[Tuple, Dict[str, Dict[int, int]]] = {}
    for row in rows:
        key = tuple(row[:key_cols])
        wk = row[key_cols]
        if not _week_ok(wk, weeks):
            continue
        counts = row[key_cols + 1 : key_cols + 1 + count_cols]
        slot = out.setdefault(key, {})
        for i, c in enumerate(counts):
            slot.setdefault(str(i), {})[wk] = int(c or 0)
    return out


def _fts_phrase(kw: str) -> str:
    return '"' + kw.replace('"', " ").strip() + '"'


def load_subgroups(config_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Subgroup definitions (id, name, keywords, openalex_topics) from config/subgroups.yaml."""
    path = Path(config_dir or DEFAULT_CONFIG_DIR) / "subgroups.yaml"
    if not path.exists():
        return []
    data = load_yaml(path) or {}
    return list(data.get("subgroups") or [])


# ----------------------------------------------------------------------------- queries
def _coverage_start(conn: sqlite3.Connection) -> Optional[str]:
    """The earliest `since` of the runs that built this store (YYYY-MM-DD), or None when no run is recorded or the
    table is missing. Buckets before it were never fetched for and must not be read as weeks with few papers."""
    try:
        row = conn.execute("SELECT MIN(since) FROM runs WHERE since IS NOT NULL AND since != ''").fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    value = str(row[0])[:10]
    try:
        _parse_date(value)
    except ValueError:
        return None
    return value


def _backfill_start(conn: sqlite3.Connection) -> Optional[str]:
    """First day of the earliest month a backfill recorded in `backfill_progress` (YYYY-MM-DD), or None when no
    backfill ran or the table is missing. A backfill fills earlier weeks by publication date, so the weeks it
    covers count as fetched-for even though no `runs` row has an early `since`."""
    try:
        row = conn.execute("SELECT MIN(month) FROM backfill_progress WHERE month IS NOT NULL AND month != ''").fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    value = str(row[0])[:7] + "-01"
    try:
        _parse_date(value)
    except ValueError:
        return None
    return value


def _weekly_totals(conn: sqlite3.Connection, params: Dict[str, Any], weeks: int) -> Tuple[Dict[int, int], Dict[int, int]]:
    papers: Dict[int, int] = {}
    for wk, n in conn.execute(_PW + "SELECT wk, COUNT(*) FROM pw GROUP BY wk", params):
        if _week_ok(wk, weeks):
            papers[wk] = int(n)
    archive: Dict[int, int] = {}
    for wk, n in conn.execute(
        """SELECT CAST((julianday(:until) - julianday(first_public)) / 7 AS INTEGER) AS wk, COUNT(*)
           FROM archive_records WHERE first_public >= :since AND first_public <= :until GROUP BY wk""",
        params,
    ):
        if _week_ok(wk, weeks):
            archive[wk] = int(n)
    return papers, archive


def _source_weekly_counts(conn: sqlite3.Connection, params: Dict[str, Any], weeks: int) -> Dict[int, Dict[str, int]]:
    """{week: {source: papers}} over the analysis window; RSS feeds are folded into one 'rss' source."""
    out: Dict[int, Dict[str, int]] = {}
    for wk, src, n in conn.execute(
        _PW + f"SELECT pw.wk, {_SOURCE_LABEL}, COUNT(*) FROM papers p JOIN pw ON pw.id = p.id GROUP BY 1, 2", params
    ):
        if _week_ok(wk, weeks):
            out.setdefault(wk, {})[str(src)] = int(n)
    return out


def _instrument_paper_rows(conn: sqlite3.Connection, params: Dict[str, Any]) -> List[Tuple]:
    return conn.execute(
        _PW
        + """SELECT m.entity_id, MAX(m.entity_name), MAX(m.vendor), MAX(m.ticker), pw.wk, COUNT(DISTINCT m.paper_id)
             FROM mentions m JOIN pw ON pw.id = m.paper_id
             WHERE m.kind = 'instrument' GROUP BY m.entity_id, pw.wk""",
        params,
    ).fetchall()


def _archive_rows(conn: sqlite3.Connection, params: Dict[str, Any]) -> List[Tuple]:
    return conn.execute(
        """SELECT instrument_model, MAX(COALESCE(vendor, '')), MAX(COALESCE(ticker, '')), MAX(COALESCE(entity_id, '')),
                  CAST((julianday(:until) - julianday(first_public)) / 7 AS INTEGER) AS wk,
                  COUNT(*), COUNT(DISTINCT study_accession)
           FROM archive_records WHERE first_public >= :since AND first_public <= :until
           GROUP BY instrument_model, wk""",
        params,
    ).fetchall()


def _company_rows(conn: sqlite3.Connection, params: Dict[str, Any]) -> List[Tuple]:
    return conn.execute(
        _PW
        + """SELECT m.entity_id, MAX(m.entity_name), MAX(m.vendor), MAX(m.ticker), pw.wk,
                    COUNT(DISTINCT m.paper_id),
                    COUNT(DISTINCT CASE WHEN m.section = 'affiliations' THEN m.paper_id END),
                    COUNT(DISTINCT CASE WHEN m.section IN ('funding', 'coi', 'ack') THEN m.paper_id END),
                    COUNT(DISTINCT CASE WHEN m.section IN ('title', 'abstract', 'methods') THEN m.paper_id END)
             FROM mentions m JOIN pw ON pw.id = m.paper_id
             WHERE m.kind = 'company' GROUP BY m.entity_id, pw.wk""",
        params,
    ).fetchall()


def _journal_rows(conn: sqlite3.Connection, params: Dict[str, Any]) -> List[Tuple]:
    return conn.execute(
        _PW
        + """SELECT lower(trim(p.journal)), MAX(p.journal), pw.wk, COUNT(*)
             FROM papers p JOIN pw ON pw.id = p.id
             WHERE trim(p.journal) != '' GROUP BY lower(trim(p.journal)), pw.wk""",
        params,
    ).fetchall()


def _tracked_rows(conn: sqlite3.Connection, params: Dict[str, Any]) -> List[Tuple]:
    return conn.execute(
        _PW
        + """SELECT t.subgroup, pw.wk, COUNT(DISTINCT t.paper_id)
             FROM tracked_author_hits t JOIN pw ON pw.id = t.paper_id GROUP BY t.subgroup, pw.wk""",
        params,
    ).fetchall()


def _subgroup_paper_weeks(
    conn: sqlite3.Connection, params: Dict[str, Any], group: Dict[str, Any], warnings: Optional[List[str]] = None
) -> Dict[int, int]:
    """Weekly paper counts for one subgroup: keyword phrases in title/abstract (FTS5) OR primary OpenAlex topic.

    A failed keyword query (no FTS table, bad syntax) contributes nothing and is recorded in `warnings`.
    """
    week_of: Dict[str, int] = {}
    kws = [str(k) for k in (group.get("keywords") or []) if k and str(k).strip()]
    if kws:
        q = "{title abstract}: (" + " OR ".join(_fts_phrase(k) for k in kws) + ")"
        try:
            for pid, wk in conn.execute(
                _PW + "SELECT pw.id, pw.wk FROM papers_fts f JOIN pw ON pw.id = f.id WHERE papers_fts MATCH :q",
                dict(params, q=q),
            ):
                if isinstance(wk, int):
                    week_of[pid] = wk
        except sqlite3.OperationalError as exc:  # bad FTS syntax or no FTS table: keywords contribute nothing
            msg = f"subgroup {group.get('id')}: keyword query failed ({exc}); keyword papers counted as 0"
            log.warning(msg)
            if warnings is not None:
                warnings.append(msg)
    topics = [str(t) for t in (group.get("openalex_topics") or []) if t]
    if topics:
        # one parameter style per statement: the window dates are passed by name, the topics inline as a
        # literal IN list (ids are validated to [A-Za-z0-9_] first)
        safe = [t for t in topics if t.replace("_", "").isalnum()]
        if safe:
            lits = ",".join("'" + t + "'" for t in safe)
            sql_json = _PW + f"SELECT pw.id, pw.wk FROM papers p JOIN pw ON pw.id = p.id WHERE json_extract(p.extra_json, '$.primary_topic') IN ({lits})"
            sql_plain = _PW + "SELECT pw.id, pw.wk FROM papers p JOIN pw ON pw.id = p.id WHERE " + " OR ".join(
                f"""instr(p.extra_json, '"primary_topic": "{t}"') > 0""" for t in safe
            )
            try:
                rows = conn.execute(sql_json, params).fetchall()
            except sqlite3.OperationalError:  # SQLite built without JSON1: fall back to a substring match
                rows = conn.execute(sql_plain, params).fetchall()
            for pid, wk in rows:
                if isinstance(wk, int):
                    week_of[pid] = wk
    out: Dict[int, int] = {}
    for wk in week_of.values():
        out[wk] = out.get(wk, 0) + 1
    return out


def _new_mention_entities(conn: sqlite3.Connection, kind: str, week_start: str, until: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """SELECT m.entity_id, MAX(m.entity_name), MAX(m.vendor), MAX(m.ticker),
                  MIN(substr(p.first_seen, 1, 10)) AS fs, COUNT(DISTINCT m.paper_id)
           FROM mentions m JOIN papers p ON p.id = m.paper_id WHERE m.kind = ?
           GROUP BY m.entity_id HAVING fs >= ? AND fs <= ? ORDER BY COUNT(DISTINCT m.paper_id) DESC, m.entity_id""",
        (kind, week_start, until),
    )
    return [
        {"id": r[0], "name": r[1] or r[0], "vendor": r[2] or "", "ticker": r[3] or "", "first_seen": r[4], "papers": int(r[5])}
        for r in cur.fetchall()
    ]


def _new_journals(conn: sqlite3.Connection, week_start: str, until: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """SELECT lower(trim(journal)), MAX(journal), MIN(substr(first_seen, 1, 10)) AS fs, COUNT(*)
           FROM papers WHERE trim(journal) != ''
           GROUP BY lower(trim(journal)) HAVING fs >= ? AND fs <= ? ORDER BY COUNT(*) DESC, lower(trim(journal))""",
        (week_start, until),
    )
    return [{"id": r[0], "name": r[1], "first_seen": r[2], "papers": int(r[3])} for r in cur.fetchall()]


def _new_archive_models(conn: sqlite3.Connection, week_start: str, until: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """SELECT instrument_model, MAX(COALESCE(vendor, '')), MAX(COALESCE(ticker, '')), MIN(first_public) AS fp, COUNT(*)
           FROM archive_records WHERE instrument_model != ''
           GROUP BY instrument_model HAVING fp >= ? AND fp <= ? ORDER BY COUNT(*) DESC, instrument_model""",
        (week_start, until),
    )
    return [{"id": r[0], "name": r[0], "vendor": r[1], "ticker": r[2], "first_public": r[3], "experiments": int(r[4])} for r in cur.fetchall()]


def _conversions(conn: sqlite3.Connection, week_start: str, until: str) -> List[Dict[str, Any]]:
    """Preprint -> journal pairs whose journal version was published or first seen in the current week.

    The preprint row's own `updated_at` is deliberately not a trigger: preprints are re-upserted on
    every new version and on the overlap day of consecutive pulls, which would resurface old pairs.
    """
    cur = conn.execute(
        """SELECT p.id, p.doi, p.title, p.journal, p.pub_date, p.url,
                  j.id, j.doi, j.title, j.journal, j.pub_date, j.url, substr(j.first_seen, 1, 10)
           FROM papers p JOIN papers j ON j.id = 'doi:' || lower(trim(p.published_doi))
           WHERE p.published_doi IS NOT NULL AND trim(p.published_doi) != '' AND p.id != j.id
             AND ((j.pub_date != '' AND j.pub_date >= :ws AND j.pub_date <= :until)
                  OR (substr(j.first_seen, 1, 10) >= :ws AND substr(j.first_seen, 1, 10) <= :until))
           ORDER BY j.pub_date DESC, p.id""",
        {"ws": week_start, "until": until},
    )
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        lag: Optional[int] = None
        try:
            if r[4] and r[10]:
                lag = (date.fromisoformat(r[10][:10]) - date.fromisoformat(r[4][:10])).days
        except ValueError:
            lag = None
        out.append({
            "preprint_id": r[0], "preprint_doi": r[1], "preprint_title": r[2] or "", "preprint_server": r[3] or "",
            "preprint_date": r[4] or "", "preprint_url": r[5] or "",
            "article_id": r[6], "article_doi": r[7], "article_title": r[8] or "", "journal": r[9] or "",
            "article_date": r[10] or "", "article_url": r[11] or "", "article_first_seen": r[12] or "",
            "lag_days": lag,
        })
    return out


# ----------------------------------------------------------------------------- main entry point
def compute_trends(
    store: Any,
    until: str,
    weeks: int = DEFAULT_WEEKS,
    subgroups: Optional[Sequence[Dict[str, Any]]] = None,
    min_baseline_weeks: int = MIN_BASELINE_WEEKS,
    notable_z: float = NOTABLE_Z,
    min_count: int = NOTABLE_MIN_COUNT,
    min_week_fraction: float = MIN_WEEK_FRACTION,
    min_source_coverage: float = MIN_SOURCE_COVERAGE,
) -> Dict[str, Any]:
    """Structured week-over-week and week-vs-baseline deltas for every entity family.

    `store` is a litradar.store.Store (anything with a sqlite3 `conn` works). `until` (YYYY-MM-DD)
    is the last day of the current week. `subgroups` is the list from config/subgroups.yaml (see
    `load_subgroups`); without it, subgroup trends use tracked-author hits only. Baseline weeks
    whose family total is below `min_week_fraction` of the median weekly total are treated as
    partial and left out of the baseline (0 disables this); paper baseline weeks whose source mix
    covers less than `min_source_coverage` of the current week's papers are not comparable and are
    left out as well (0 disables this; see the module docstring).
    """
    if weeks < 1:
        raise ValueError("weeks must be >= 1")
    if not 0 <= min_week_fraction < 1:
        raise ValueError("min_week_fraction must be in [0, 1)")
    if not 0 <= min_source_coverage <= 1:
        raise ValueError("min_source_coverage must be in [0, 1]")
    end = _parse_date(until)
    until = _iso(end)
    week_start = _iso(end - timedelta(days=6))
    since = _iso(end - timedelta(days=7 * (weeks + 1) - 1))
    prev_start, prev_end = _week_bounds(end, 1)
    conn: sqlite3.Connection = store.conn
    params = {"until": until, "since": since}
    warnings: List[str] = []

    paper_totals, archive_totals = _weekly_totals(conn, params, weeks)
    paper_sources = _source_weekly_counts(conn, params, weeks)
    comparable, dropped_source_mix = _comparable_weeks(paper_sources, weeks, min_week_fraction, min_source_coverage, end)
    active_papers, dropped_papers = _active_weeks({w: paper_totals.get(w, 0) for w in comparable}, weeks, min_week_fraction, end)
    active_archive, dropped_archive = _active_weeks(archive_totals, weeks, min_week_fraction, end)

    def delta(by_week: Dict[int, int], active: Sequence[int]) -> Dict[str, Any]:
        return _delta(by_week, weeks, active, min_baseline_weeks, notable_z, min_count)

    # instruments: papers/week
    inst_papers: List[Dict[str, Any]] = []
    for key, metrics in _collect(_instrument_paper_rows(conn, params), weeks, 4, 1).items():
        row = {"id": key[0], "name": key[1] or key[0], "vendor": key[2] or "", "ticker": key[3] or "", "kind": "instrument", "metric": "papers"}
        row.update(delta(metrics["0"], active_papers))
        inst_papers.append(row)

    # instruments: experiments/week from archives (by declared instrument model)
    inst_experiments: List[Dict[str, Any]] = []
    for key, metrics in _collect(_archive_rows(conn, params), weeks, 4, 2).items():
        name = key[0] or "(unknown model)"
        row = {"id": name, "name": name, "vendor": key[1], "ticker": key[2], "entity_id": key[3], "kind": "instrument", "metric": "experiments"}
        row.update(delta(metrics["0"], active_archive))
        row["studies"] = delta(metrics["1"], active_archive)
        inst_experiments.append(row)

    # companies: papers/week plus role split
    companies: List[Dict[str, Any]] = []
    role_names = list(ROLE_SECTIONS)
    for key, metrics in _collect(_company_rows(conn, params), weeks, 4, 4).items():
        row = {"id": key[0], "name": key[1] or key[0], "parent": key[2] or "", "ticker": key[3] or "", "kind": "company", "metric": "papers",
               "unlisted": str(key[0]).startswith("unlisted:")}
        row.update(delta(metrics["0"], active_papers))
        row["roles"] = {name: delta(metrics[str(i + 1)], active_papers) for i, name in enumerate(role_names)}
        companies.append(row)

    # subgroups: keyword/topic papers (when definitions are given) + tracked-author papers
    tracked = {key[0]: metrics["0"] for key, metrics in _collect(_tracked_rows(conn, params), weeks, 1, 1).items()}
    subgroup_rows: List[Dict[str, Any]] = []
    names = {g.get("id"): g.get("name") or g.get("id") for g in (subgroups or []) if g.get("id")}
    for g in subgroups or []:
        gid = g.get("id")
        if not gid:
            continue
        row = {"id": gid, "name": names[gid], "kind": "subgroup", "metric": "papers"}
        row.update(delta(_subgroup_paper_weeks(conn, params, g, warnings), active_papers))
        row["tracked_author_papers"] = delta(tracked.get(gid, {}), active_papers)
        subgroup_rows.append(row)
    for gid, by_week in tracked.items():
        if gid in names:
            continue
        row = {"id": gid, "name": gid, "kind": "subgroup", "metric": "tracked_author_papers"}
        row.update(delta(by_week, active_papers))
        row["tracked_author_papers"] = delta(by_week, active_papers)
        subgroup_rows.append(row)

    # journals: papers/week
    journals: List[Dict[str, Any]] = []
    for key, metrics in _collect(_journal_rows(conn, params), weeks, 2, 1).items():
        row = {"id": key[0], "name": key[1] or key[0], "kind": "journal", "metric": "papers"}
        row.update(delta(metrics["0"], active_papers))
        journals.append(row)

    for rows in (inst_papers, inst_experiments, companies, subgroup_rows, journals):
        rows.sort(key=lambda r: (-(r["current"]), -(r["previous"]), str(r["id"])))

    first_seen_min = conn.execute("SELECT MIN(substr(first_seen, 1, 10)) FROM papers").fetchone()[0]
    cold_start = first_seen_min is None or first_seen_min >= week_start
    weeks_with_papers = sum(1 for w in range(1, weeks + 1) if paper_totals.get(w, 0) > 0)  # comparable or not
    # how much of 'last week' the runs that built this store actually covered: a corpus whose first run pulled
    # 2026-10-10..17 has one day of the 2026-10-04..10 bucket, and week-over-week movers against it are artefacts.
    # A recorded backfill (backfill_progress) extends the coverage back to its first month: its papers carry old
    # publication dates and land in the baseline weeks by pub_date, whatever their ingestion day.
    runs_since = _coverage_start(conn)
    backfill_from = _backfill_start(conn)
    covered_from = runs_since
    if backfill_from and (covered_from is None or backfill_from < covered_from):
        covered_from = backfill_from
    previous_week_coverage = _week_coverage(covered_from, prev_start, prev_end)
    # archive records are fetched by the runs alone (a backfill writes papers), so their 'last week' is covered from
    # the earliest run only: a store whose first run pulled 2 of the 7 days holds 2 days of archive 'last week'
    previous_week_coverage_archive = _week_coverage(runs_since, prev_start, prev_end)
    new_entities = {
        "instrument": _new_mention_entities(conn, "instrument", week_start, until),
        "company": _new_mention_entities(conn, "company", week_start, until),
        "journal": _new_journals(conn, week_start, until),
        "archive_instrument": _new_archive_models(conn, week_start, until),
    }
    conversions = _conversions(conn, week_start, until)

    groups = {
        "instrument_papers": inst_papers, "instrument_experiments": inst_experiments,
        "company": companies, "subgroup": subgroup_rows, "journal": journals,
    }
    notable: List[Dict[str, Any]] = []
    for gname, rows in groups.items():
        for r in rows:
            if r["notable"]:
                notable.append(dict(r, group=gname))
    notable.sort(key=lambda r: (-abs(r["z"] or 0.0), -r["current"], str(r["id"])))

    return {
        "until": until,
        "week_start": week_start,
        "previous_week": {"start": prev_start, "end": prev_end},
        "baseline": {"weeks": weeks, "since": since, "end": prev_end, "min_weeks_for_z": min_baseline_weeks,
                     "min_week_fraction": min_week_fraction, "min_source_coverage": min_source_coverage},
        "history": {
            "paper_weeks_with_data": len(active_papers),
            "paper_weeks_holding_papers": weeks_with_papers,  # every baseline week with papers, before the partial and source-mix rules
            "archive_weeks_with_data": len(active_archive),
            "z_available_papers": len(active_papers) >= min_baseline_weeks,
            "z_available_archive": len(active_archive) >= min_baseline_weeks,
            "paper_weeks_dropped_partial": dropped_papers,
            "archive_weeks_dropped_partial": dropped_archive,
            # paper weeks whose source mix does not cover the current week's (a single-source backfill under a
            # multi-source live week); the current week's own mix is given so the renderer can name the step
            "paper_weeks_dropped_source_mix": dropped_source_mix,
            "current_week_sources": dict(sorted((paper_sources.get(0) or {}).items(), key=lambda kv: (-kv[1], kv[0]))),
            # 'last week' is not a complete, comparable bucket for the family (partial, or a different source mix)
            "previous_week_partial_papers": any(d["week"] == 1 for d in dropped_papers) or any(d["week"] == 1 for d in dropped_source_mix),
            "previous_week_partial_archive": any(d["week"] == 1 for d in dropped_archive),
            "previous_week_coverage": previous_week_coverage,
            "previous_week_coverage_archive": previous_week_coverage_archive,
            # where the coverage came from: the earliest `runs.since` and the first backfilled month (None = none)
            "coverage_sources": {"runs_since": runs_since, "backfill_from": backfill_from},
            # every paper was ingested this week although earlier weeks hold publication-dated data (first run after
            # a backfill): the baseline statistics are by publication date, "new this week" is not meaningful
            "ingested_this_week_only_with_history": bool(cold_start and weeks_with_papers > 0),
        },
        "thresholds": {"notable_z": notable_z, "min_count": min_count, "std_floor": STD_FLOOR},
        "cold_start": cold_start,
        "warnings": warnings,
        "totals": {
            "papers": delta(paper_totals, active_papers),
            "archive_experiments": delta(archive_totals, active_archive),
        },
        "instruments": {"papers": inst_papers, "experiments": inst_experiments},
        "companies": companies,
        "subgroups": subgroup_rows,
        "journals": journals,
        "new_entities": new_entities,
        "conversions": conversions,
        "notable": notable,
    }


# ----------------------------------------------------------------------------- selection + rendering
def _all_rows(trends: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in trends["instruments"]["papers"]:
        out.append(dict(r, group="instrument_papers"))
    for r in trends["instruments"]["experiments"]:
        out.append(dict(r, group="instrument_experiments"))
    for r in trends["companies"]:
        out.append(dict(r, group="company"))
    for r in trends["subgroups"]:
        out.append(dict(r, group="subgroup"))
    for r in trends["journals"]:
        out.append(dict(r, group="journal"))
    return out


def _family(group: str) -> str:
    return "archive" if group == "instrument_experiments" else "papers"


def week_over_week_comparable(trends: Dict[str, Any], family: str) -> bool:
    """True when 'last week' is a complete, comparable bucket for the family ('papers' | 'archive'): fully covered
    by the runs (or, for papers, a recorded backfill) and not dropped from the baseline as partial or for its
    source mix. Only such families take part in the week-over-week tier of `top_movers` (the rows without a z)."""
    hist = trends.get("history") or {}
    if family == "archive":
        cov = hist.get("previous_week_coverage_archive") or hist.get("previous_week_coverage") or {}
        partial = bool(hist.get("previous_week_partial_archive"))
    else:
        cov = hist.get("previous_week_coverage") or {}
        partial = bool(hist.get("previous_week_partial_papers"))
    return not cov.get("partial") and not partial


def top_movers(trends: Dict[str, Any], n: int = 10, min_count: Optional[int] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(risers, fallers) in two tiers, never interleaved: rows with a z-score first, ranked by z; then rows without
    one, ranked by week-over-week change among themselves.

    z-scores and raw deltas are not one scale (an archive model's week-over-week delta is in thousands of
    experiments, a paper z in units of standard deviation), so a family without a baseline is never ranked by delta
    next to z-scored rows: its rows only fill the places left below every z-scored mover, and with `n` or more
    z-scored risers (or fallers) they do not appear at all. With no z anywhere (a cold start) the tables are the
    week-over-week tier alone. A row without a z takes part only when its family's 'last week' is a complete,
    comparable bucket (`week_over_week_comparable`): a delta against a bucket the runs covered for 3 of 7 days, or
    one dropped from the baseline as partial or for its source mix, is an artefact, so such rows are left out and
    the renderer says which family and why. Entities need at least `min_count` counts this week, last week or on
    average, so one-paper blips do not crowd out real movement.
    """
    mc = trends["thresholds"]["min_count"] if min_count is None else min_count
    rows = [r for r in _all_rows(trends) if max(r["current"], r["previous"], r["mean"] or 0) >= mc]
    scored = [r for r in rows if r["z"] is not None]
    unscored = [r for r in rows if r["z"] is None and week_over_week_comparable(trends, _family(r["group"]))]
    risers = sorted([r for r in scored if r["z"] > 0], key=lambda r: (-r["z"], -r["delta"], -r["current"], str(r["id"])))
    risers += sorted([r for r in unscored if r["delta"] > 0], key=lambda r: (-r["delta"], -r["current"], str(r["id"])))
    fallers = sorted([r for r in scored if r["z"] < 0], key=lambda r: (r["z"], r["delta"], -r["previous"], str(r["id"])))
    fallers += sorted([r for r in unscored if r["delta"] < 0], key=lambda r: (r["delta"], -r["previous"], str(r["id"])))
    return risers[:n], fallers[:n]


_KIND_LABEL = {
    "instrument_papers": "instrument · papers",
    "instrument_experiments": "instrument · experiments",
    "company": "company · papers",
    "subgroup": "subgroup · papers",
    "journal": "journal · papers",
}


def _cell(x: Any) -> str:
    return str(x).replace("|", "/").replace("\n", " ")


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_nothing to show_\n"
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _fmt_z(z: Optional[float]) -> str:
    return "n/a" if z is None else f"{z:+.1f}"


def _fmt_mean(r: Dict[str, Any]) -> str:
    if r.get("mean") is None:
        return "n/a"
    std = "n/a" if r.get("std") is None else f"{r['std']:.1f}"  # std is undefined for a one-week baseline
    return f"{r['mean']:.1f} ± {std}"


def _detail(r: Dict[str, Any]) -> str:
    if r.get("group") == "company" and r.get("roles"):
        roles = r["roles"]
        return (f"author {roles['as_author']['current']} · funding/COI {roles['as_funder_or_coi']['current']}"
                f" · subject {roles['as_subject']['current']}")
    if r.get("group") == "subgroup" and r.get("tracked_author_papers"):
        return f"tracked authors {r['tracked_author_papers']['current']}"
    if r.get("group") == "instrument_experiments" and r.get("studies"):
        return f"studies {r['studies']['current']}"
    if r.get("ticker"):
        return r["ticker"]
    return ""


def _mover_rows(rows: List[Dict[str, Any]]) -> List[List[Any]]:
    out = []
    for r in rows:
        name = f"**{r['name']}**" if r["notable"] else r["name"]
        out.append([name, _KIND_LABEL.get(r["group"], r["group"]), r["current"], r["previous"], _fmt_mean(r), _fmt_z(r["z"]), _detail(r)])
    return out


def _fmt_total(d: Dict[str, Any]) -> str:
    base = f"{d['current']:,} (last week {d['previous']:,}"
    if d["mean"] is not None:
        std = "n/a" if d.get("std") is None else f"{d['std']:,.0f}"
        base += f", baseline {d['mean']:,.0f} ± {std}, z {_fmt_z(d['z'])}"
    return base + ")"


def digest_window_note(since: str, until: str) -> str:
    """Sentence reconciling the digest's window with the seven-day trend week, for `window_note`."""
    try:
        days = (date.fromisoformat(str(until)[:10]) - date.fromisoformat(str(since)[:10])).days + 1
        span = f"{days} inclusive days"
    except ValueError:
        span = "a window of its own"
    return (f"The digest tables cover {since} to {until} ({span}), so their counts can differ from the "
            f"seven-day figures in this section.")


def _partial_note(label: str, dropped: List[Dict[str, Any]], unit: str) -> str:
    parts = [f"{d['start']} to {d['end']} ({d['total']:,} {unit} vs median {d['median']:,.0f})" for d in dropped]
    return f"{label}: " + "; ".join(parts)


def _source_mix_note(trends: Dict[str, Any]) -> str:
    """The coverage-step sentence: which baseline weeks were left out because their sources are not this week's."""
    hist, base = trends["history"], trends["baseline"]
    dropped = hist.get("paper_weeks_dropped_source_mix") or []
    cur: Dict[str, int] = hist.get("current_week_sources") or {}
    total0 = sum(cur.values()) or 1
    missing = sorted({s for d in dropped for s in d.get("missing") or []}, key=lambda s: (-cur.get(s, 0), s))
    present = [s for s in cur if s not in set(missing)]
    present_share = sum(cur[s] for s in present) / total0
    start, end = min(d["start"] for d in dropped), max(d["end"] for d in dropped)
    if hist["z_available_papers"]:
        z_part = (f"z-scores for the paper family are computed against the {_plural(hist['paper_weeks_with_data'], 'comparable week')} only.")
    else:
        z_part = (f"z-scores for the paper family are suppressed until {base['min_weeks_for_z']} comparable weeks exist: against those "
                  "weeks they would measure the change in coverage, not in the literature.")
    tail = " 'Last week' is one of them: read the 'Last week' column as a lower bound." if any(d["week"] == 1 for d in dropped) else ""
    return (f"_Source mix: {_plural(len(dropped), 'baseline week')} ({start} to {end}) hold papers but their sources are not this week's "
            f"({', '.join(present) or 'no source'} suppl{'ies' if len(present) == 1 else 'y'} {present_share:.0%} of this week's {total0:,} papers; "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} absent or far smaller there), so they are left out of the paper baseline "
            f"(a week must be fed by sources supplying {base.get('min_source_coverage', MIN_SOURCE_COVERAGE):.0%} of this week's papers). {z_part}{tail}_\n")


_FAMILY_LABEL = {"papers": "papers", "archive": "archive records"}


def _wow_reason(trends: Dict[str, Any], family: str) -> Tuple[Tuple[Any, ...], str]:
    """Why a family's rows without a z are left out of the movers tables (the week-over-week tier): (grouping key,
    phrase without the family label), or ((), '') when it takes part."""
    hist = trends["history"]
    prev = trends["previous_week"]
    cov = hist.get("previous_week_coverage_archive" if family == "archive" else "previous_week_coverage") or {}
    if cov.get("partial"):
        return (("runs", cov.get("covered_from"), cov.get("days_covered")),
                f"the runs that built this store cover 'last week' ({prev['start']} to {prev['end']}) from {cov.get('covered_from')} only "
                f"({cov.get('days_covered')} of 7 days)")
    if hist.get("previous_week_partial_archive" if family == "archive" else "previous_week_partial_papers"):
        return ("bucket",), f"'last week' ({prev['start']} to {prev['end']}) is a partial or non-comparable bucket (left out of the baseline above)"
    return (), ""


def _grouped(items: List[Tuple[Tuple[Any, ...], str, str]]) -> List[str]:
    """[(key, phrase, family)] -> ['<phrase> for papers and archive records', ...], one entry per distinct phrase."""
    out: Dict[Tuple[Any, ...], Tuple[str, List[str]]] = {}
    for key, phrase, fam in items:
        out.setdefault(key, (phrase, []))[1].append(_FAMILY_LABEL[fam])
    return [f"{phrase} for {' and '.join(labels)}" for phrase, labels in out.values()]


def render_trends_markdown(
    trends: Dict[str, Any], top_n: int = 10, heading_level: int = 2, window_note: Optional[str] = None,
    conversions_note: Optional[str] = None,
) -> str:
    """Compact "What changed" Markdown section from `compute_trends` output.

    `window_note` is an optional sentence printed under the header, meant for the digest renderer
    to state its own (longer) window; see `digest_window_note`. `conversions_note`, when given, replaces the
    conversions table with that sentence: the digest prints its own, wider conversions list under the top
    innovations and points here to it, so one document never carries two answers.
    """
    h = "#" * max(1, heading_level)
    hh = "#" * (max(1, heading_level) + 1)
    hist, thr, base = trends["history"], trends["thresholds"], trends["baseline"]
    dropped_mix = hist.get("paper_weeks_dropped_source_mix") or []
    md: List[str] = [f"{h} What changed: {trends['week_start']} to {trends['until']}\n"]
    md.append(
        f"This week vs last week ({trends['previous_week']['start']} to {trends['previous_week']['end']}) vs the trailing "
        f"{base['weeks']}-week baseline ({_plural(hist['paper_weeks_with_data'], 'comparable week' if dropped_mix else 'week')} with paper data, "
        f"{_plural(hist['archive_weeks_with_data'], 'week')} with archive data). "
        f"Weeks are seven-day buckets ending {trends['until']}. "
        f"z = (this week − baseline mean) / max(std, {thr['std_floor']:g}). **Bold** = notable: |z| ≥ {thr['notable_z']:g} with at least "
        f"{thr['min_count']} counts (this week for risers, the baseline mean for fallers).\n"
    )
    if window_note:
        md.append(f"_{window_note.strip()}_\n")
    dropped_p, dropped_a = hist.get("paper_weeks_dropped_partial") or [], hist.get("archive_weeks_dropped_partial") or []
    if dropped_p or dropped_a:
        notes = []
        if dropped_p:
            notes.append(_partial_note("papers", dropped_p, "papers"))
        if dropped_a:
            notes.append(_partial_note("archive", dropped_a, "experiments"))
        partial_prev = any(d["week"] == 1 for d in dropped_p) or any(d["week"] == 1 for d in dropped_a)
        tail = " 'Last week' above is one of them." if partial_prev else ""
        md.append(f"_Partial weeks excluded from the baseline (below {base.get('min_week_fraction', MIN_WEEK_FRACTION):.0%} of the median "
                  f"weekly total): " + " · ".join(notes) + f".{tail}_\n")
    if dropped_mix:
        md.append(_source_mix_note(trends))
    if trends.get("warnings"):
        md.append("_Warnings:_\n" + "\n".join(f"- {w}" for w in trends["warnings"]) + "\n")
    comparable = "comparable " if dropped_mix else ""
    if not hist["z_available_papers"] and not hist["z_available_archive"]:
        md.append(f"_Fewer than {base['min_weeks_for_z']} {comparable}weeks of history: z-scores are suppressed and movers are ranked by week-over-week change._\n")
    elif not hist["z_available_papers"] or not hist["z_available_archive"]:
        missing = "paper" if not hist["z_available_papers"] else "archive"
        fam = "papers" if missing == "paper" else "archive"
        lead = (f"_Fewer than {base['min_weeks_for_z']} {comparable if missing == 'paper' else ''}weeks of {missing} history: z-scores for that family "
                "are suppressed")
        if week_over_week_comparable(trends, fam):
            md.append(lead + ". Its rows (z n/a) are listed after every z-scored row in the tables below, ordered by week-over-week change "
                      f"among themselves, so they appear only when fewer than {top_n} z-scored risers or fallers qualify._\n")
        else:
            why = _wow_reason(trends, fam)[1]
            md.append(lead + ", and its rows are left out of the movers tables below (ranked by z)"
                      + (f": {why} for {_FAMILY_LABEL[fam]}" if why else "") + "._\n")
    tot = trends["totals"]
    md.append(f"**Corpus:** {_fmt_total(tot['papers'])} papers · {_fmt_total(tot['archive_experiments'])} archive experiments.\n")
    notable = trends["notable"]
    if notable:
        by_group: Dict[str, int] = {}
        for r in notable:
            by_group[r["group"]] = by_group.get(r["group"], 0) + 1
        md.append(f"{len(notable)} notable movers: " + ", ".join(f"{_KIND_LABEL.get(g, g)} {n}" for g, n in sorted(by_group.items())) + ".\n")

    risers, fallers = top_movers(trends, n=top_n)
    headers = ["Entity", "Kind", "This week", "Last week", "Baseline mean ± std", "z", "Detail"]
    wow_only = not hist["z_available_papers"] and not hist["z_available_archive"]
    families_with_rows = {_family(r["group"]) for r in _all_rows(trends)} or {"papers"}
    families = [f for f in ("papers", "archive") if f in families_with_rows]
    excluded = [(f, *_wow_reason(trends, f)) for f in families if not week_over_week_comparable(trends, f)]
    partly = []
    for fam in families:
        cov = hist.get("previous_week_coverage_archive" if fam == "archive" else "previous_week_coverage") or {}
        if cov.get("partial"):
            partly.append(((cov.get("covered_from"), cov.get("days_covered")),
                           f"(from {cov.get('covered_from')}, {cov.get('days_covered')} of 7 days)", fam))
    for phrase in _grouped(partly):
        md.append(f"_'Last week' is only partly covered by the runs that built this store {phrase}; read the 'Last week' column as a lower bound._\n")
    if wow_only and excluded and len(excluded) == len(families):
        # with no baseline the movers would be ranked by week-over-week change against a bucket the runs only partly
        # covered (or that is not comparable): every "faller" would be an artefact, so the tables wait
        md.append(f"{hh} Biggest risers and fallers\n")
        md.append(
            "_Suppressed: " + "; ".join(_grouped([(key, phrase, f) for f, key, phrase in excluded])) + ", and with no baseline the movers "
            "would be ranked by week-over-week change against that partial bucket. They appear once a run has covered a full previous week._\n"
        )
    else:
        if wow_only and excluded:
            md.append("_Ranked by week-over-week change (no baseline yet); " + "; ".join(
                f"{'paper' if f == 'papers' else 'archive'} rows are left out because {phrase} for {_FAMILY_LABEL[f]}" for f, _key, phrase in excluded) + "._\n")
        md.append(f"{hh} Biggest risers\n")
        md.append(_table(headers, _mover_rows(risers)))
        md.append(f"{hh} Biggest fallers\n")
        md.append(_table(headers, _mover_rows(fallers)))

    md.append(f"{hh} New this week\n")
    ne = trends["new_entities"]
    if trends["cold_start"] and hist.get("ingested_this_week_only_with_history"):
        md.append(
            f"_Every paper in this corpus was ingested this week although {_plural(hist.get('paper_weeks_holding_papers', hist['paper_weeks_with_data']), 'earlier week')} hold "
            "publication-dated papers (a first run after a backfill): 'new to the corpus' cannot yet be told apart from 'new to the "
            f"database', so the list is omitted ({len(ne['instrument'])} instruments, {len(ne['company'])} companies, {len(ne['journal'])} journals "
            "would all count as new). The week-over-week and baseline statistics above are by publication date and are not affected by the "
            "ingestion day" + ("; the source-mix note under the header says which backfilled weeks are comparable with this one" if dropped_mix else "")
            + "; this list becomes meaningful from the next weekly run._\n"
        )
    elif trends["cold_start"]:
        md.append(
            f"_First week of data in this corpus: every entity is new ({len(ne['instrument'])} instruments, {len(ne['company'])} companies, "
            f"{len(ne['journal'])} journals, {len(ne['archive_instrument'])} archive instrument models), so the list is omitted. "
            "It becomes meaningful from the second weekly run._\n"
        )
    else:
        lines = []
        if ne["instrument"]:
            lines.append("- Instruments (first appearance in the corpus): " + ", ".join(f"{r['name']} ({r['papers']})" for r in ne["instrument"][:20]))
        if ne["company"]:
            lines.append("- Companies: " + ", ".join(f"{r['name']} ({r['papers']})" for r in ne["company"][:20]))
        if ne["journal"]:
            lines.append("- Journals: " + ", ".join(f"{r['name']} ({r['papers']})" for r in ne["journal"][:20]))
        if ne["archive_instrument"]:
            lines.append("- Archive instrument models (first public data): " + ", ".join(f"{r['name']} ({r['experiments']})" for r in ne["archive_instrument"][:20]))
        md.append("\n".join(lines) + "\n" if lines else "_no new entities this week_\n")

    md.append(f"{hh} Preprints that became journal articles\n")
    conv = trends["conversions"]
    if conversions_note:
        md.append(f"_{conversions_note.strip()}_\n")
    elif not conv:
        md.append("_no preprint → journal conversions detected this week_\n")
    else:
        rows = []
        for c in conv[: top_n * 2]:
            pre_label = _cell(c["preprint_title"][:60]) or c["preprint_doi"] or c["preprint_id"]
            pre = f"[{pre_label}]({c['preprint_url']})" if c["preprint_url"] else pre_label
            art_label = _cell(c["article_doi"] or c["article_id"])
            art = f"[{art_label}]({c['article_url']})" if c["article_url"] else art_label
            lag = "n/a" if c["lag_days"] is None else str(c["lag_days"])
            rows.append([pre, art, c["journal"] or "", c["preprint_date"] or "n.d.", c["article_date"] or "n.d.", lag])
        md.append(_table(["Preprint", "Journal article", "Journal", "Posted", "Published", "Lag (days)"], rows))
        lags = [c["lag_days"] for c in conv if c["lag_days"] is not None and c["lag_days"] >= 0]
        if lags:
            md.append(f"{len(conv)} conversions; median preprint-to-journal lag {statistics.median(lags):.0f} days.\n")
    return "\n".join(md)
