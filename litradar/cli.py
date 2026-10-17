"""Command-line interface.

  litradar run      [--days 7 | --since YYYY-MM-DD --until YYYY-MM-DD] [--llm] [--no-fulltext] [--sources a,b] [--email] [--slack]
  litradar digest   [--since --until] [--out DIR]           re-render from the database without fetching
  litradar rematch  [--since 90d]                             re-annotate stored papers after editing the gazetteers
  litradar search   "query" [--since 90d] [--in methods,coi] [--limit 50]
  litradar entity   <entity_id> [--since 90d]                 papers that mention one gazetteer entry
  litradar entities [--kind instrument|company] [--grep text] list gazetteer entries and ids
  litradar stats                                              database totals and last runs
  litradar check-config                                       validate YAML files and report duplicates
  litradar trends   [--until YYYY-MM-DD] [--weeks 13] [--out DIR | --print | --json]
                                                              what changed: this week vs last week vs the trailing baseline
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from . import __version__, trends
from .config import Settings
from .digest import deliver, render
from .extract.gazetteer import Gazetteer
from .pipeline import Pipeline, window
from .store import Store


def _parse_since(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.fullmatch(r"(\d+)d", s)
    if m:
        return (datetime.now(timezone.utc).date() - timedelta(days=int(m.group(1)))).isoformat()
    return s


def _window(args) -> tuple:
    if getattr(args, "since", None) and getattr(args, "until", None):
        return args.since, args.until
    if getattr(args, "since", None):
        return args.since, datetime.now(timezone.utc).date().isoformat()
    return window(int(getattr(args, "days", 7) or 7))


def cmd_run(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    since, until = _window(args)
    only = args.sources.split(",") if args.sources else None
    pipe = Pipeline(settings)
    report = pipe.run(since, until, use_llm=args.llm, fulltext=not args.no_fulltext, only=only)
    out = render.render(pipe.store, since, until, settings.radar, run_report=report)
    out_dir = Path(args.out or settings.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{until}.md"
    md_path.write_text(out["markdown"], encoding="utf-8")
    (out_dir / "latest.md").write_text(out["markdown"], encoding="utf-8")
    (out_dir / f"{until}.json").write_text(json.dumps(out["summary"], indent=1, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1))
    print(f"digest written to {md_path}")
    if args.email:
        deliver.send_email(settings, f"{settings.radar.get('digest', {}).get('title', 'Literature radar')}: {since} to {until}", out["markdown"])
    if args.slack:
        top = out["summary"]["top"][:10]
        text = f"*Literature radar {since} to {until}*\n" + "\n".join(f"• <{t['url']}|{t['title']}> ({t['journal']})" for t in top)
        deliver.send_slack(settings, text)
    return 0 if not report["errors"] else 0  # errors are reported in the digest, not as a failed exit


def cmd_digest(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    since, until = _window(args)
    store = Store(settings.db_path)
    out = render.render(store, since, until, settings.radar)
    out_dir = Path(args.out or settings.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{until}.md").write_text(out["markdown"], encoding="utf-8")
    (out_dir / "latest.md").write_text(out["markdown"], encoding="utf-8")
    print(out["markdown"] if args.print else f"digest written to {out_dir / (until + '.md')}")
    return 0


def cmd_rematch(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    pipe = Pipeline(settings)
    report = pipe.rematch(since=_parse_since(args.since))
    print(json.dumps(report, indent=1))
    return 0


def cmd_search(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    sections = args.in_sections.split(",") if args.in_sections else ()
    rows = store.search(args.query, since=_parse_since(args.since), limit=args.limit, sections=sections)
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    if not rows:
        print("no matches")
        return 0
    for r in rows:
        print(f"- {r['pub_date'] or 'n.d.'}  {r['title']}\n    {r['journal'] or r['source']}  {r['url']}\n    …{r['snippet']}…")
    return 0


def cmd_entity(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    rows = store.entity_papers(args.entity_id, since=_parse_since(args.since), limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    print(f"{len(rows)} papers mention {args.entity_id}")
    for r in rows:
        print(f"- {r['pub_date'] or 'n.d.'}  [{r['section']}] {r['title']}\n    {r['url']}\n    {r['snippet'][:200]}")
    return 0


def cmd_entities(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    kinds = [args.kind] if args.kind else ["instrument", "company"]
    for kind in kinds:
        gz = Gazetteer.from_yaml(settings.yaml("instruments" if kind == "instrument" else "companies"), kind)
        for e in gz.entries:
            line = f"{kind:10} {e.id:32} {e.name:40} {e.vendor or e.parent:28} {e.ticker}"
            if not args.grep or args.grep.lower() in line.lower():
                print(line)
    return 0


def cmd_stats(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    print(json.dumps(store.counts(), indent=1))
    for r in store.conn.execute("SELECT started, finished, since, until, counts_json FROM runs ORDER BY id DESC LIMIT 5"):
        print(r[0], "->", r[1], f"window {r[2]}..{r[3]}")
    return 0


def cmd_check_config(args) -> int:
    import yaml

    settings = Settings.from_env(config_dir=args.config_dir)
    ok = True
    for name, kind in (("instruments", "instrument"), ("companies", "company")):
        data = settings.yaml(name)
        entries = data.get("entries", [])
        ids = [e.get("id") for e in entries]
        dups = sorted({i for i in ids if ids.count(i) > 1})
        missing = [e for e in entries if not e.get("id") or not e.get("name")]
        gz = Gazetteer.from_yaml(data, kind)
        print(f"{name}: {len(entries)} entries, {sum(len(e.aliases) for e in gz.entries)} aliases, duplicates={dups or 'none'}, missing id/name={len(missing)}")
        ok = ok and not dups and not missing
        # alias collisions across entries
        seen = {}
        for e in gz.entries:
            for a in e.aliases:
                key = a.lower()
                if key in seen and seen[key] != e.id:
                    print(f"  alias collision: '{a}' in {seen[key]} and {e.id}")
                seen[key] = e.id
    for name in ("journals", "subgroups", "feeds", "radar"):
        try:
            d = settings.yaml(name)
            print(f"{name}: ok ({len(d) if isinstance(d, dict) else 0} top-level keys)")
        except yaml.YAMLError as exc:
            print(f"{name}: YAML error {exc}")
            ok = False
    return 0 if ok else 1


def cmd_trends(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    until = args.until or datetime.now(timezone.utc).date().isoformat()
    subgroups = (settings.yaml("subgroups") or {}).get("subgroups") or []
    tr = trends.compute_trends(store, until, weeks=args.weeks, subgroups=subgroups)
    if args.json:
        print(json.dumps(tr, indent=1, default=str))
        return 0
    md = trends.render_trends_markdown(tr, top_n=args.top)
    if args.print:
        print(md)
        return 0
    out_dir = Path(args.out or settings.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"trends-{tr['until']}.md"
    path.write_text(md, encoding="utf-8")
    print(f"trends written to {path} ({len(tr['notable'])} notable movers, {len(tr['conversions'])} conversions)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="litradar", description="Weekly science-literature radar")
    ap.add_argument("--config-dir", default=None, help="directory with radar.yaml etc. (default: ./config)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"litradar {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="fetch, extract, rank and render the digest")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--llm", action="store_true", help="score the shortlist with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--no-fulltext", action="store_true")
    p.add_argument("--sources", help="comma-separated subset: biorxiv,europepmc,openalex,crossref,arxiv,rss,ena")
    p.add_argument("--out")
    p.add_argument("--email", action="store_true")
    p.add_argument("--slack", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("digest", help="re-render the digest from the database")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--out")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("rematch", help="re-run gazetteer matching and scoring over stored papers (after editing config)")
    p.add_argument("--since", help="YYYY-MM-DD or Nd; default: everything")
    p.set_defaults(func=cmd_rematch)

    p = sub.add_parser("search", help="full-text search of stored papers")
    p.add_argument("query")
    p.add_argument("--since", help="YYYY-MM-DD or Nd (e.g. 90d)")
    p.add_argument("--in", dest="in_sections", help="restrict to columns: title,abstract,methods,funding,coi,affiliations,body")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("entity", help="papers mentioning one gazetteer entry (see `entities`)")
    p.add_argument("entity_id")
    p.add_argument("--since")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_entity)

    p = sub.add_parser("entities", help="list gazetteer entries")
    p.add_argument("--kind", choices=["instrument", "company"])
    p.add_argument("--grep")
    p.set_defaults(func=cmd_entities)

    p = sub.add_parser("stats", help="database totals and recent runs")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("check-config", help="validate config files")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("trends", help="what changed: this week vs last week vs the trailing 13-week baseline")
    p.add_argument("--until", help="last day of the current week, YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--weeks", type=int, default=trends.DEFAULT_WEEKS, help="baseline length in weeks (default 13)")
    p.add_argument("--top", type=int, default=10, help="rows per risers/fallers table (default 10)")
    p.add_argument("--out", help="directory for trends-<date>.md (default: the digest directory)")
    p.add_argument("--print", action="store_true", help="print the Markdown instead of writing a file")
    p.add_argument("--json", action="store_true", help="print the structured result as JSON")
    p.set_defaults(func=cmd_trends)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
