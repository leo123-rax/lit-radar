"""`litradar candidates`: review, ignore and promote companies spotted by litradar.discovery.

  litradar candidates [--since 90d] [--min-score 2] [--status new] [--limit 50] [--evidence 1] [--hints] [--json]
  litradar candidates refresh [--since 90d] [--until YYYY-MM-DD]      recompute candidates from the store
  litradar candidates show <name> [--hints]                           every piece of evidence for one candidate
  litradar candidates ignore <name>                                   status -> ignored
  litradar candidates promote <name> [--ticker T] [--type tools]      status -> promoted; hands the entry to
                                                                      litradar.track.add_company when that exists,
                                                                      else prints the companies.yaml line to paste
  litradar candidates reset <name>                                    status -> new (undo ignore / promote)

<name> is the display name, the normalised key, or any unique substring of either.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from .. import discovery
from ..config import Settings
from ..store import Store

ACTIONS = ("list", "refresh", "show", "ignore", "promote", "reset")


def _statuses(arg: Optional[str]) -> List[str]:
    if not arg or arg == "new":
        return ["new"]
    if arg == "all":
        return list(discovery.STATUSES)
    return [s.strip() for s in arg.split(",") if s.strip()]


def _hint(name: str, settings: Settings, enabled: bool) -> Optional[Dict[str, Any]]:
    if not enabled:
        return None
    try:
        return discovery.sec_ticker_hint(name, cache_dir=settings.cache_dir, contact_email=settings.contact_email)
    except Exception:  # a hint must never break the listing
        return None


def _print_candidate(row: Dict[str, Any], n_evidence: int, hint: Optional[Dict[str, Any]]) -> None:
    types = ", ".join(f"{k}({v})" for k, v in sorted((row.get("types") or {}).items()))
    flag = "" if row.get("confident", True) else "  (low confidence)"
    hint_s = f"   SEC hint: {hint['ticker']} ({hint['title']}, {hint['match']:.2f})" if hint else ""
    print(f"{row['score']:5.1f}  {row['display_name'][:44]:44} {types:40} {row['papers']:>3} ref{'s' if row['papers'] != 1 else ' '}  "
          f"last {row.get('last_seen') or 'n.d.'}  [{row['status']}]{flag}{hint_s}")
    for ev in (row.get("evidence") or [])[:n_evidence]:
        snippet = (ev.get("snippet") or "").replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:157].rstrip() + "…"
        print(f"       [{ev.get('evidence_type')}] {ev.get('ref')}  \"{snippet}\"")
    variants = [v for v in row.get("variants") or [] if v][:4]
    if variants and n_evidence > 1:
        print(f"       variants: {', '.join(variants)}")


def cmd_candidates(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    action = args.action or "list"
    since = discovery.parse_since(args.since)
    try:
        if action == "refresh":
            gz = discovery.load_company_gazetteer(settings)
            report = discovery.refresh(store, gz, since, args.until)
            print(json.dumps(report, indent=1))
            return 0
        if action == "list":
            rows = discovery.list_candidates(store, since=since, min_score=args.min_score, statuses=_statuses(args.status), limit=args.limit)
            if args.json:
                for r in rows:
                    h = _hint(r["display_name"], settings, args.hints)
                    if h:
                        r["sec_hint"] = h
                print(json.dumps(rows, indent=1, default=str, ensure_ascii=False))
                return 0
            if not rows:
                print(f"no candidates with score >= {args.min_score:g} since {since} (run `litradar candidates refresh` after a `litradar run`)")
                return 0
            print(f"{len(rows)} candidate companies (status {','.join(_statuses(args.status))}, score >= {args.min_score:g}, last seen since {since})")
            for r in rows:
                _print_candidate(r, max(1, args.evidence), _hint(r["display_name"], settings, args.hints))
            print("\nnext: `litradar candidates show <name>` · `ignore <name>` · `promote <name> [--ticker T --type tools]`")
            return 0
        if not args.name:
            print(f"candidates {action}: a <name> is required", file=sys.stderr)
            return 2
        if action == "show":
            row = discovery.get_candidate(store, args.name)
            if row is None:
                print(f"no candidate matches {args.name!r}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(row, indent=1, default=str, ensure_ascii=False))
                return 0
            _print_candidate(row, len(row.get("evidence") or []), _hint(row["display_name"], settings, True))
            if row.get("promotion"):
                print(f"       promoted: {row['promotion'].get('yaml')}")
            return 0
        if action in ("ignore", "reset"):
            row = discovery.set_status(store, args.name, "ignored" if action == "ignore" else "new")
            print(f"{row['display_name']}: status -> {row['status']}")
            return 0
        if action == "promote":
            result = discovery.promote(store, args.name, ticker=args.ticker, type_=args.type)
            print(f"{result['candidate']}: status -> promoted")
            if result["tracked"]:
                print("entry handed to litradar.track.add_company")
            else:
                if result["error"]:
                    print(f"litradar.track.add_company failed ({result['error']}); add the entry by hand:")
                else:
                    print("paste into config/companies.yaml under `entries:` (then `litradar rematch`):")
                print(result["yaml"])
            if not args.ticker:
                h = _hint(result["candidate"], settings, True)
                if h:
                    print(f"SEC ticker hint (verify before use): {h['ticker']} = {h['title']} (match {h['match']:.2f})")
            return 0
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"unknown action {action!r}", file=sys.stderr)
    return 2


def register(sub) -> None:
    p = sub.add_parser(
        "candidates",
        help="companies spotted in stored papers and news that are not in the gazetteer: list | refresh | show | ignore | promote | reset",
        description=__doc__,
    )
    p.add_argument("action", nargs="?", default="list", choices=ACTIONS)
    p.add_argument("name", nargs="?", help="candidate display name, normalised key or unique substring")
    p.add_argument("--since", help="YYYY-MM-DD or Nd (default 90d)")
    p.add_argument("--until", help="refresh only: end of the window (default today)")
    p.add_argument("--min-score", type=float, default=2.0, help="list only (default 2)")
    p.add_argument("--status", default="new", help="list only: comma-separated new,ignored,promoted or all (default new)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--evidence", type=int, default=1, help="snippets to print per candidate (default 1)")
    p.add_argument("--hints", action="store_true", help="fuzzy-match names against the SEC ticker list (one cached download; hint only)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--ticker", help="promote only: ticker for the new entry (default private)")
    p.add_argument("--type", help="promote only: tools | diagnostics | synthesis | ai_bio | pharma | cro | institute (default tools)")
    p.set_defaults(func=cmd_candidates)
