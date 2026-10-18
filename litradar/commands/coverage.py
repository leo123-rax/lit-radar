"""`litradar coverage [--days 7 | --since --until] [--json]`: where the index is thin, from the store alone."""
from __future__ import annotations

import json
import sys

from .. import query as Q
from ..config import Settings
from ..coverage import coverage_report, render_coverage_markdown
from ..store import Store
from ..toolkit import UsageError, resolve_window


def _window(args):
    """The same resolver `run`, `digest` and `fetch` use: --until alone ends a --days window, since after until and a
    --days below 1 raise UsageError (reported as exit 2 by cmd_coverage)."""
    return resolve_window(args.since, args.until, args.days)


def cmd_coverage(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    try:
        since, until = _window(args)
    except UsageError as exc:
        print(f"litradar coverage: error: {exc}", file=sys.stderr)
        return 2
    store = Store(settings.db_path)
    report = coverage_report(store, since, until, settings)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print(render_coverage_markdown(store, since, until, settings, report=report))
    return 0


def register(sub) -> None:
    p = sub.add_parser("coverage", help="coverage report: records per source, silent journals and watched entities, errored sources, suggestions")
    p.add_argument("--days", type=int, default=None, help="look-back window in days when --since is not given (default 7; at least 1)")
    p.add_argument("--since", type=Q.since_arg, help=f"window start: {Q.SINCE_FORMAT}")
    p.add_argument("--until", type=Q.until_arg, help="window end: YYYY-MM-DD or Nd (default: today, UTC; alone it ends the --days window)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_coverage)
