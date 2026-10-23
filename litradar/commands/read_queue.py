"""`litradar read-queue`: read the day's subscription articles unattended and commit only the facts.

  litradar read-queue [--limit 20] [--days 7] [--source index|db] [--index-url URL] [--dry-run] [--json]

Candidates are the papers in the published search index (or the local database) whose journal is in
`reading.journals` and whose publisher has a saved session, published in the window, not read before,
best score first. Each read goes through `litradar read` (facts-only storage, the same daily cap and
pacing) and appends one facts record to `reads/<YYYY-MM-DD>.jsonl`. Nothing happens, and nothing fails,
when a session is missing or there is nothing new: see docs/reading-assist.md, "Auto-run".
"""
from __future__ import annotations

import argparse
import json
import sys

from .. import reader
from ..config import Settings


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if n < 1:
        raise argparse.ArgumentTypeError(f"expected 1 or more, got {value!r}")
    return n


def cmd_read_queue(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    try:
        rep = reader.run_queue(
            settings, limit=args.limit, days=args.days, source=args.source, url=args.index_url,
            dry_run=args.dry_run, keep_snippets=args.snippets,
            log_line=(lambda line: None) if args.json else print,
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"read-queue: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rep.as_dict(), indent=1, default=str))
        return 0
    for note in rep.notes:
        print(note)
    if args.dry_run:
        for c in rep.planned:
            print(f"  would read {c['doi']}  {c['score']:>5.2f}  {c['journal']} {c['pub_date']}  {c['title'][:70]}")
    if rep.read:
        print(f"read {len(rep.read)} article(s); facts appended to {rep.out_file}")
    if rep.failed:
        print(f"{len(rep.failed)} article(s) could not be read:")
        for f in rep.failed:
            print(f"  {f['doi']}: {f['error']}")
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "read-queue",
        help="read the day's subscription articles through the saved sessions and append the facts to reads/ (unattended; capped and paced)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--limit", type=_positive_int, default=reader.DEFAULT_LIMIT,
                   help=f"most articles to read in this run (default {reader.DEFAULT_LIMIT}; the daily cap still applies)")
    p.add_argument("--days", type=_positive_int, default=reader.DEFAULT_DAYS,
                   help=f"publication window in days (default {reader.DEFAULT_DAYS})")
    p.add_argument("--source", choices=["index", "db"], default="index",
                   help="candidate list: the published search index (default) or the local database")
    p.add_argument("--index-url", dest="index_url",
                   help="search-index.json URL (default: reading.index_url, LITRADAR_INDEX_URL, else derived from the git remote)")
    p.add_argument("--snippets", action="store_true",
                   help=f"include each mention's matched snippet, cut to {reader.SNIPPET_MAX} characters (default: no snippets at all)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print the queue and stop: no session check, no reads, nothing written")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.set_defaults(func=cmd_read_queue)
