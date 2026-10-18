"""`litradar search-index`: the client-side search index and page, without the rest of the dashboard.

Writes search-index.json, search-entities.json and search.html into --out. `litradar dashboard`
writes the same three files as part of a full site build; this command is for rebuilding the
index alone (a larger window, a different cap, a quick look at the sizes).

  litradar search-index --out site/ [--days 180] [--max 20000] [--max-bytes 4000000] [--db PATH] [--json]

The database is opened read-only. Exit 1 when it is missing, locked or corrupt.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .. import dashboard_search
from ..config import Settings


def _non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if n < 0:
        raise argparse.ArgumentTypeError(f"expected 0 or more, got {value!r}")
    return n


def _positive_int(value: str) -> int:
    n = _non_negative_int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"expected 1 or more, got {value!r}")
    return n


def cmd_search_index(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    db_path = Path(args.db) if args.db else Path(settings.db_path)
    out_dir = Path(args.out or "site")
    try:
        report = dashboard_search.build_search(
            db_path, out_dir, days=args.days, max_records=args.max_records, max_bytes=args.max_bytes,
        )
    except (sqlite3.Error, FileNotFoundError) as exc:
        # a locked, corrupt or missing store must not publish an empty index as if it were the corpus
        print(f"search-index: cannot read {db_path}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=1, default=str))
        return 0
    s = report["stats"]
    print(
        f"indexed {s['records']:,} of {s['total_in_window']:,} papers since {report['window']['start']} "
        f"({s['dropped_by_cap']:,} over the record cap, {s['dropped_by_budget']:,} over the byte budget; "
        f"{s['with_snippet']:,} with an abstract excerpt)"
    )
    for rel, size in report["files"].items():
        print(f"  {rel}: {size:,} bytes")
    print(f"written to {out_dir}")
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "search-index",
        help="build the client-side search index and page (search-index.json, search-entities.json, search.html)",
    )
    p.add_argument("--out", default="site", help="output directory (default: site/)")
    p.add_argument("--days", type=_positive_int, default=dashboard_search.DEFAULT_DAYS,
                   help=f"look-back window in days (default {dashboard_search.DEFAULT_DAYS})")
    p.add_argument("--max", type=_non_negative_int, default=dashboard_search.DEFAULT_MAX_RECORDS, dest="max_records",
                   help=f"cap on indexed papers, best rank score then newest first (default {dashboard_search.DEFAULT_MAX_RECORDS:,})")
    p.add_argument("--max-bytes", type=_non_negative_int, default=dashboard_search.DEFAULT_MAX_BYTES, dest="max_bytes",
                   help=f"raw byte budget for search-index.json; the tail of the ranking is dropped to fit (default {dashboard_search.DEFAULT_MAX_BYTES:,}; 0 = no budget)")
    p.add_argument("--db", help="database file to read (default: the configured store); opened read-only")
    p.add_argument("--json", action="store_true", help="print the report (window, stats, file sizes) as JSON")
    p.set_defaults(func=cmd_search_index)
