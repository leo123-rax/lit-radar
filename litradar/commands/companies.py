"""`litradar companies`: the tracked companies as the store sees them.

  litradar companies list [--type pharma] [--public | --private] [--since 90d] [--limit 50] [--json]
  litradar companies show <id|name|alias|ticker> [--since 90d] [--json]
  litradar companies export [--csv companies.csv] [--since 90d]      one row per company, 90-day counts (stdout by default)
  litradar companies graph [--out companies.json] [--since 365d] [--min-weight 1] [--include-unlisted]

`list` and `export` count distinct papers per company in the window (default 90 days) and split them by the
role the company plays: author affiliation, funding / competing-interest statement, subject (title or
abstract) or supplier (Methods). `show` adds the 365-day window, a monthly series, the instruments and
companies co-mentioned in the same papers, recent papers, news and the discovery candidates that resolve
to the entry. `graph` writes the co-mention graph the dashboard draws (`companies/graph.html`).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from .. import companies as C
from .. import query as Q
from ..config import Settings
from ..store import Store

ACTIONS = ("list", "show", "export", "graph")


def _since(args, default: str) -> str:
    return args.since or Q.parse_since(default) or ""


def _print_list(rows: List[Dict[str, Any]], since: str) -> None:
    if not rows:
        print(f"no companies match (window since {since})")
        return
    print(f"{len(rows)} companies, distinct papers since {since} (author / funding-COI / subject / supplier)")
    print(f"{'papers':>6}  {'id':28} {'ticker':10} {'type':11} {'au':>4} {'f/c':>4} {'sub':>4} {'sup':>4}  {'news':>4}  {'cand':>4}  last paper")
    for r in rows:
        ro = r["roles"]
        print(f"{r['papers']:>6}  {r['id'][:28]:28} {(r['ticker'] if r['public'] else 'private')[:10]:10} {r['type'][:11]:11} "
              f"{ro['author']:>4} {ro['funding_coi']:>4} {ro['subject']:>4} {ro['supplier']:>4}  {r['news']:>4}  {r['candidates']:>4}  {r['last_paper'] or '-'}")


def _print_detail(d: Dict[str, Any]) -> None:
    ticker = d["ticker"] if d["public"] else "private"
    head = f"{d['name']}  [{d['id']}]  {ticker}"
    if d.get("type"):
        head += f"  type={d['type']}"
    if d.get("parent"):
        head += f"  parent={d['parent']}"
    print(head)
    if d.get("domains"):
        print(f"  domains: {', '.join(d['domains'])}")
    if d.get("aliases"):
        print(f"  aliases: {', '.join(d['aliases'][:8])}{' …' if len(d['aliases']) > 8 else ''}")
    for key, w in d["windows"].items():
        ro = w["roles"]
        print(f"  last {key:>4}: {w['papers']} papers, {w['mentions']} mentions  "
              f"(author {ro['author']}, funding/COI {ro['funding_coi']}, subject {ro['subject']}, supplier {ro['supplier']}, other {ro['other']})  "
              f"news {w['news']}  window {w['since']}..{w['until']}")
    active = [m for m in d["monthly"] if m["papers"]]
    if active:
        print("  by month: " + "  ".join(f"{m['month']} {m['papers']}" for m in active))
    else:
        print(f"  by month: no papers in the last {len(d['monthly'])} months")
    if d["instruments"]:
        print("  instruments co-mentioned: " + ", ".join(f"{i['name']} ({i['papers']})" for i in d["instruments"]))
    if d["companies"]:
        print("  companies co-mentioned:   " + ", ".join(f"{i['name']} ({i['papers']})" for i in d["companies"]))
    if d["recent_papers"]:
        print(f"  recent papers ({len(d['recent_papers'])}):")
        for p in d["recent_papers"]:
            roles = "/".join(p["roles"])
            print(f"    {p['date'] or 'n.d.':10} [{roles}] {p['title'][:90]}{'…' if len(p['title']) > 90 else ''}  {p['url']}")
    if d["news"]:
        print(f"  news ({len(d['news'])} shown):")
        for n in d["news"]:
            print(f"    {n['date'] or 'n.d.':10} {n['title'][:90]}  {n['url']}")
    if d["candidates"]:
        print("  discovery candidates linked: " + ", ".join(f"{c['display_name']} [{c['status']}, {c['score']:.1f}]" for c in d["candidates"]))


def cmd_companies(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    entries = C.load_entries(settings)
    store = Store(settings.db_path)
    action = args.action or "list"
    try:
        if action == "list":
            since, until = _since(args, "90d"), Q.today_utc().isoformat()
            public = True if args.public else (False if args.private else None)
            rows = C.list_companies(store, entries, since, until, type_=args.type, public=public)
            if args.limit:
                rows = rows[: int(args.limit)]
            if args.json:
                print(json.dumps(rows, indent=1, ensure_ascii=False, default=str))
            else:
                _print_list(rows, since)
            return 0
        if action == "show":
            if not args.name:
                print("companies show: a company id, name, alias or ticker is required", file=sys.stderr)
                return 2
            entry = C.resolve_entry(entries, args.name)
            if entry is None:
                print(f"no company matches {args.name!r} (ids, names, aliases and tickers from companies.yaml are accepted)", file=sys.stderr)
                return 1
            days = (90, 365)
            if args.since:
                span = (Q.today_utc() - __import__("datetime").date.fromisoformat(args.since)).days
                days = tuple(sorted({max(1, span), 365}))
            detail = C.company_detail(store, entry, windows=days)
            if args.json:
                print(json.dumps(detail, indent=1, ensure_ascii=False, default=str))
            else:
                _print_detail(detail)
            return 0
        if action == "export":
            since, until = _since(args, "90d"), Q.today_utc().isoformat()
            rows = C.export_rows(store, entries, since, until)
            if args.csv and args.csv != "-":
                path = Path(args.csv)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    n = C.write_csv(rows, fh)
                print(f"{n} companies written to {path} (window {since}..{until})")
            else:
                C.write_csv(rows, sys.stdout)
            return 0
        if action == "graph":
            since, until = _since(args, "365d"), Q.today_utc().isoformat()
            graph = C.comention_graph(store, since, until, entries, min_weight=args.min_weight, include_unlisted=args.include_unlisted)
            text = json.dumps(graph, ensure_ascii=False, indent=1 if args.pretty else None)
            if args.out and args.out != "-":
                path = Path(args.out)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                print(f"{len(graph['nodes'])} nodes, {len(graph['links'])} links written to {path} (window {since}..{until}, min weight {args.min_weight})")
            else:
                print(text)
            return 0
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"unknown action {action!r}", file=sys.stderr)
    return 2


def register(sub) -> None:
    p = sub.add_parser(
        "companies",
        help="tracked companies as the store sees them: list | show <id> | export --csv | graph --out",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("action", nargs="?", default="list", choices=ACTIONS)
    p.add_argument("name", nargs="?", help="show only: company id, name, alias or ticker (case-insensitive)")
    p.add_argument("--since", type=Q.since_arg, help=f"{Q.SINCE_FORMAT}; default 90d (graph: 365d)")
    p.add_argument("--type", choices=C.COMPANY_TYPES, help="list only: keep one gazetteer type")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--public", action="store_true", help="list only: listed companies (ticker is not private / n/a)")
    g.add_argument("--private", action="store_true", help="list only: companies without a listing")
    p.add_argument("--limit", type=int, default=0, help="list only: rows to print (default all)")
    p.add_argument("--csv", nargs="?", const="-", default=None, metavar="PATH", help="export only: CSV file to write (default stdout)")
    p.add_argument("--out", default=None, metavar="PATH", help="graph only: JSON file to write (default stdout)")
    p.add_argument("--min-weight", type=int, default=1, help="graph only: minimum shared papers per edge (default 1)")
    p.add_argument("--include-unlisted", action="store_true", help="graph only: keep 'unlisted:' institutions as nodes")
    p.add_argument("--pretty", action="store_true", help="graph only: indent the JSON")
    p.add_argument("--json", action="store_true", help="list / show: machine-readable output")
    p.set_defaults(func=cmd_companies)
