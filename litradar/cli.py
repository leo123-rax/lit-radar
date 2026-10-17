"""Command-line interface.

  litradar run      [--days 7 | --since YYYY-MM-DD --until YYYY-MM-DD] [--llm] [--no-fulltext] [--sources a,b] [--email] [--slack]
  litradar digest   [--since --until] [--out DIR]           re-render from the database without fetching
  litradar rematch  [--since 90d]                             re-annotate stored papers after editing the gazetteers
  litradar search   "query" [--since 90d] [--in methods,coi] [--limit 50]
  litradar entity   <entity_id> [--since 90d]                 papers that mention one gazetteer entry
  litradar entities [--kind instrument|company] [--grep text] list gazetteer entries and ids
  litradar stats                                              database totals and last runs
  litradar check-config                                       validate YAML files and report duplicates
  litradar timeseries [--entity id ...] [--vendor name ...] [--from YYYY-MM --to YYYY-MM] [--sources sra,ena,epmc] [--out DIR]
                                                              monthly experiment / paper counts per instrument model
  litradar fetch    <source> [--since --until | --days] [...] [--raw] [--out file.jsonl|-]
  litradar fetch    --list                                    raw access to one source adapter, JSON lines out
  litradar browser-check [--cdp-url URL]                      is the optional browser fetcher usable here?
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

from . import __version__, browser, toolkit
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
    # archive_models.yaml (instrument time series): ids must exist in instruments.yaml unless `proposed`
    models_path = Path(settings.config_dir) / "archive_models.yaml"
    if models_path.exists():
        from . import timeseries as ts

        try:
            specs = ts.load_archive_models(models_path)
        except yaml.YAMLError as exc:
            print(f"archive_models: YAML error {exc}")
            return 1
        warnings = ts.check_models(specs, ts.load_instrument_index(Path(settings.config_dir) / "instruments.yaml"))
        print(f"archive_models: {len(specs)} entities, {sum(len(x.ena) for x in specs.values())} archive model strings, {len(warnings)} warnings")
        for w in warnings:
            print(f"  {w}")
        ok = ok and not any("NOT in instruments.yaml" in w or "must be lower case" in w for w in warnings)
    return 0 if ok else 1


def cmd_timeseries(args) -> int:
    from . import timeseries as ts

    settings = Settings.from_env(config_dir=args.config_dir)
    sources = [s.strip() for s in (args.sources or ",".join(ts.SOURCES)).split(",") if s.strip()]
    report = ts.run(
        config_dir=settings.config_dir,
        db_path=settings.db_path,
        out_dir=Path(args.out or (Path(settings.out_dir) / "timeseries")),
        entities=args.entity or [],
        vendors=args.vendor or [],
        start=args.start,
        end=args.end,
        sources=sources,
        refresh_months=args.refresh_months,
        force=args.force,
        chart=not args.no_chart,
        dry_run=args.dry_run,
        contact_email=settings.contact_email,
        ncbi_api_key=settings.ncbi_api_key,
    )
    print(json.dumps(report, indent=1, default=str))
    return 1 if report.get("errors") else 0


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

    p = sub.add_parser("timeseries", help="monthly counts of SRA/ENA experiments and Europe PMC papers per instrument model")
    p.add_argument("--entity", action="append", help="gazetteer id from config/archive_models.yaml (repeatable), e.g. pacb-revio")
    p.add_argument("--vendor", action="append", help="all entities of a vendor: name, ticker or id prefix (repeatable), e.g. pacb")
    p.add_argument("--from", dest="start", help="first month YYYY-MM (default: 36 months back)")
    p.add_argument("--to", dest="end", help="last month YYYY-MM (default: current month)")
    p.add_argument("--sources", help="comma-separated subset of sra,ena,epmc (default: all)")
    p.add_argument("--out", help="output directory (default: <out_dir>/timeseries)")
    p.add_argument("--refresh-months", type=int, default=2, help="re-fetch cached counts for the current month and the N months before it (deposit lag; default 2)")
    p.add_argument("--force", action="store_true", help="re-fetch every month, ignoring the cache")
    p.add_argument("--no-chart", action="store_true", help="skip the PNG even when matplotlib is installed")
    p.add_argument("--dry-run", action="store_true", help="plan only: report how many requests a run would make")
    p.set_defaults(func=cmd_timeseries)

    # raw access to every source adapter without the pipeline (litradar/toolkit.py)
    toolkit.add_fetch_parser(sub)

    p = sub.add_parser("browser-check", help="report whether the optional real-browser fetcher is installed and a Chrome DevTools endpoint is listening")
    p.add_argument("--cdp-url", help="Chrome DevTools URL to probe (default: browser.cdp_url in radar.yaml, else http://localhost:9222)")
    p.set_defaults(func=browser.cmd_browser_check)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
