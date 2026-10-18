"""`litradar update`: extend the corpus from the last recorded run forward (live mode).

Unlike `run`, this renders no digest: the digest and dashboard are produced weekly from the
database. The window starts at the last run's `until` minus an overlap (Europe PMC indexes most
PubMed records a day or two after publication; SRA/ENA set `first_public` on release and keep
adding records for a day), is capped at `--max-days`, and defaults to 7 days when the store has
no run yet. See docs/live.md for the measured lags behind the defaults.

  litradar update [--overlap-days 2] [--max-days 14] [--sources sra,ena] [--llm] [--no-fulltext]
                  [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .. import incremental
from .. import query as Q
from ..config import Settings
from ..pipeline import Pipeline
from ..store import Store

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_update(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    last = incremental.last_run(store.conn)
    plan = incremental.compute_window(
        last["until"] if last else None,
        overlap_days=args.overlap_days,
        max_days=args.max_days,
        since=args.since,
        until=args.until,
    )
    only: Optional[List[str]] = [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else None
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan, "sources": only or "all", "db_path": str(settings.db_path), "last_run": last}, indent=1, default=str))
        store.close()
        return 0

    log.info("update window %s..%s (%s days, basis=%s%s)", plan["since"], plan["until"], plan["days"], plan["basis"], ", capped" if plan["capped"] else "")
    started = _now()
    pipe = Pipeline(settings, store=store)
    try:
        report = pipe.run(plan["since"], plan["until"], use_llm=args.llm, fulltext=not args.no_fulltext, only=only)
    except Exception as exc:  # noqa: BLE001 - leave a summary behind so the scheduler artifact explains the failure
        finished = _now()
        failure = {
            "started": started,
            "finished": finished,
            "since": plan["since"],
            "until": plan["until"],
            "window": {k: plan[k] for k in ("days", "basis", "capped", "last_until", "overlap_days", "max_days")},
            "sources": only or "all",
            "failed": f"{type(exc).__name__}: {exc}",
            "errors": list(pipe.errors),
            "error_sources": incremental.error_sources(pipe.errors),
            "db_path": str(settings.db_path),
        }
        incremental.checkpoint(store.conn)
        path = incremental.write_summary(failure, Path(settings.out_dir))
        log.exception("update failed; no run recorded, the next update re-covers this window")
        print(f"update FAILED ({failure['failed']}); nothing recorded, summary at {path}")
        return 2
    finished = _now()
    incremental.checkpoint(store.conn)
    summary = incremental.summarize_update(report, plan, started, finished, Path(settings.db_path), only)
    path = incremental.write_summary(summary, Path(settings.out_dir))
    print(incremental.summary_paragraph(summary))
    print(f"summary written to {path}")
    # per-source errors are reported, not fatal (same contract as `run`); `litradar status` raises the alarm
    return 0


def register(sub) -> None:
    p = sub.add_parser("update", help="extend the corpus from the last run forward (daily live mode; no digest)")
    p.add_argument("--overlap-days", type=int, default=incremental.DEFAULT_OVERLAP_DAYS, help=f"re-fetch this many days before the last run's end (default {incremental.DEFAULT_OVERLAP_DAYS})")
    p.add_argument("--max-days", type=int, default=incremental.DEFAULT_MAX_DAYS, help=f"cap on the window length (default {incremental.DEFAULT_MAX_DAYS}); repair a longer outage with --since")
    p.add_argument("--since", type=Q.iso_date_arg, help="override the window start (YYYY-MM-DD)")
    p.add_argument("--until", type=Q.iso_date_arg, help="override the window end (YYYY-MM-DD; default today UTC)")
    p.add_argument("--sources", help="comma-separated subset: biorxiv,europepmc,openalex,crossref,arxiv,rss,sra,ena")
    p.add_argument("--llm", action="store_true", help="score the shortlist with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--no-fulltext", action="store_true", help="skip full-text retrieval (default: on)")
    p.add_argument("--dry-run", action="store_true", help="print the window that would be fetched and exit")
    p.set_defaults(func=cmd_update)
