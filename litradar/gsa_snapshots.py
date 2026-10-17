"""Time series for the GSA census: weekly snapshots of the cumulative per-model counts from
https://ngdc.cncb.ac.cn/gsa/statistics/statistics.json, stored next to the main tables and diffed over time.

The GSA search page exposes only the platform FAMILY per experiment, while the statistics JSON exposes the
instrument MODEL but only as cumulative totals. Storing one snapshot per run turns the totals into a flow:
`platform_deltas()` reports, per model, experiments added between two snapshots (DNBSEQ-T7 vs. NovaSeq 6000
vs. GenoLab M …). Snapshot deltas include records released retroactively, so they are a trend signal, not
an exact weekly release count; the per-day search counts in `sources.gsa` are the exact ones.

Schema is additive (CREATE TABLE IF NOT EXISTS) and lives here so `litradar/store.py` stays untouched;
pass `store.conn`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .sources import gsa

SCHEMA = """
CREATE TABLE IF NOT EXISTS gsa_platform_snapshots (
  snapshot_date TEXT NOT NULL,
  platform TEXT NOT NULL,
  experiments INTEGER,
  runs INTEGER,
  PRIMARY KEY (snapshot_date, platform)
);
CREATE TABLE IF NOT EXISTS gsa_monthly_archives (
  snapshot_date TEXT NOT NULL,
  month TEXT NOT NULL,
  archives INTEGER,
  PRIMARY KEY (snapshot_date, month)
);
CREATE TABLE IF NOT EXISTS gsa_totals (
  snapshot_date TEXT PRIMARY KEY,
  archive_count INTEGER,
  exp_count INTEGER,
  run_count INTEGER,
  released_exp INTEGER,
  private_exp INTEGER,
  fetched_at TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_snapshot(conn: sqlite3.Connection, stats: Dict[str, Any], snapshot_date: str) -> int:
    """Store one day's snapshot of the statistics JSON. Re-running on the same date overwrites. Returns platform rows written."""
    ensure_schema(conn)
    rows = gsa.platform_snapshot(stats, snapshot_date)
    conn.executemany(
        "INSERT OR REPLACE INTO gsa_platform_snapshots (snapshot_date, platform, experiments, runs) VALUES (?,?,?,?)",
        [(r["snapshot_date"], r["platform"], r["experiments"], r["runs"]) for r in rows],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO gsa_monthly_archives (snapshot_date, month, archives) VALUES (?,?,?)",
        [(snapshot_date, m["month"], m["archives"]) for m in gsa.monthly_archives(stats)],
    )
    t = gsa.totals(stats)
    conn.execute(
        "INSERT OR REPLACE INTO gsa_totals (snapshot_date, archive_count, exp_count, run_count, released_exp, private_exp, fetched_at) VALUES (?,?,?,?,?,?,?)",
        (snapshot_date, t.get("archiveCount", 0), t.get("expCount", 0), t.get("runCount", 0), t.get("releasedExp", 0), t.get("privateExp", 0), _now()),
    )
    conn.commit()
    return len(rows)


def snapshot_dates(conn: sqlite3.Connection) -> List[str]:
    ensure_schema(conn)
    return [r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM gsa_platform_snapshots ORDER BY snapshot_date")]


def platform_deltas(conn: sqlite3.Connection, since: Optional[str] = None, limit: int = 60) -> List[Dict[str, Any]]:
    """Per model: cumulative experiments at the latest snapshot vs. the baseline snapshot, and the difference.

    The baseline is the most recent snapshot on/before `since` (default: the snapshot before the latest).
    With a single snapshot the deltas are None (nothing to diff yet) and the rows still list the totals.
    """
    dates = snapshot_dates(conn)
    if not dates:
        return []
    latest = dates[-1]
    baseline: Optional[str] = None
    if since:
        earlier = [d for d in dates if d <= since and d < latest]
        baseline = earlier[-1] if earlier else (dates[0] if dates[0] < latest else None)
    elif len(dates) >= 2:
        baseline = dates[-2]
    cur = conn.execute(
        """SELECT a.platform, a.experiments AS experiments_now, a.runs AS runs_now,
                  b.experiments AS experiments_then, b.runs AS runs_then
           FROM gsa_platform_snapshots a
           LEFT JOIN gsa_platform_snapshots b ON b.platform = a.platform AND b.snapshot_date = ?
           WHERE a.snapshot_date = ?
           ORDER BY a.experiments DESC LIMIT ?""",
        (baseline or "", latest, limit),
    )
    out: List[Dict[str, Any]] = []
    for platform, exp_now, runs_now, exp_then, _runs_then in cur.fetchall():
        delta = (exp_now or 0) - (exp_then or 0) if (baseline and exp_then is not None) else None
        out.append({
            "platform": platform,
            "experiments": exp_now or 0,
            "runs": runs_now or 0,
            "experiments_baseline": exp_then,
            "delta_experiments": delta,
            "latest": latest,
            "baseline": baseline,
        })
    if baseline:
        out.sort(key=lambda r: -(r["delta_experiments"] or 0))
    return out


def monthly_series(conn: sqlite3.Connection, snapshot_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """CRA studies created per month as of the given (default latest) snapshot."""
    dates = snapshot_dates(conn)
    if not dates:
        return []
    d = snapshot_date or dates[-1]
    cur = conn.execute("SELECT month, archives FROM gsa_monthly_archives WHERE snapshot_date=? ORDER BY month", (d,))
    return [{"month": m, "archives": a, "snapshot_date": d} for m, a in cur.fetchall()]
