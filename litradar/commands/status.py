"""`litradar status`: freshness and health of the live corpus.

Prints the last update (time, window, errors), papers/day and archive records/day over the last
14 days, per-source counts for the last three runs with deltas, sources disabled in config, which
credentials are configured (presence only), database size, days with zero new papers in the last
30, and the next scheduled runs as documented. Exit code 1 when the last update is older than
three days or the last run had errors on more than two sources, so a cron or workflow can alert.

  litradar status [--json] [--stale-days 3] [--max-failing-sources 2]
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import incremental
from ..config import Settings
from ..store import SCHEMA


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the database without creating it. A missing file yields an empty in-memory schema."""
    if not db_path.exists():
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        return conn
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(str(db_path))


def cmd_status(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    db_path = Path(settings.db_path)
    conn = _open_readonly(db_path)
    try:
        report = incremental.health(
            conn,
            db_path,
            radar_cfg=settings.radar,
            settings=settings,
            last_summary=incremental.read_summary(Path(settings.out_dir)),
            stale_after_days=args.stale_days,
            max_failing_sources=args.max_failing_sources,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print(incremental.format_status(report))
    return incremental.exit_code(report)


def register(sub) -> None:
    p = sub.add_parser("status", help="freshness and health of the live corpus (exit 1 when stale or many sources failed)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--stale-days", type=int, default=incremental.STALE_AFTER_DAYS, help=f"exit 1 when the last run is older than this many days (default {incremental.STALE_AFTER_DAYS})")
    p.add_argument("--max-failing-sources", type=int, default=incremental.MAX_FAILING_SOURCES, help=f"exit 1 when the last run had errors on more than this many sources (default {incremental.MAX_FAILING_SOURCES})")
    p.set_defaults(func=cmd_status)
