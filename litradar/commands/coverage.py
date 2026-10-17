"""`litradar coverage [--days 7 | --since --until] [--json]`: where the index is thin, from the store alone."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import Settings
from ..coverage import coverage_report, render_coverage_markdown
from ..pipeline import window
from ..store import Store


def _window(args):
    if args.since and args.until:
        return args.since, args.until
    if args.since:
        return args.since, datetime.now(timezone.utc).date().isoformat()
    return window(int(args.days or 7))


def cmd_coverage(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    since, until = _window(args)
    store = Store(settings.db_path)
    report = coverage_report(store, since, until, settings)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print(render_coverage_markdown(store, since, until, settings, report=report))
    return 0


def register(sub) -> None:
    p = sub.add_parser("coverage", help="coverage report: records per source, silent journals and watched entities, errored sources, suggestions")
    p.add_argument("--days", type=int, default=None, help="look-back window (default 7)")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_coverage)
