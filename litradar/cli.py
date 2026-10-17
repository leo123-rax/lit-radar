"""Command-line interface.

  litradar run      [--days 7 | --since YYYY-MM-DD --until YYYY-MM-DD] [--llm] [--no-fulltext] [--sources a,b] [--email] [--slack]
  litradar digest   [--since --until] [--out DIR]           re-render from the database without fetching
  litradar rematch  [--since 90d]                             re-annotate stored papers after editing the gazetteers
  litradar search   "query" [--since 90d] [--in methods,coi] [--limit 50]
  litradar entity   <entity_id> [--since 90d]                 papers that mention one gazetteer entry
  litradar entities [--kind instrument|company] [--grep text] list gazetteer entries and ids
  litradar stats                                              database totals and last runs
  litradar dashboard --out site/ [--weeks 52]                 build the static trends site from the database
  litradar check-config                                       validate YAML files and report duplicates
  litradar timeseries [--entity id ...] [--vendor name ...] [--from YYYY-MM --to YYYY-MM] [--sources sra,ena,epmc] [--out DIR]
                                                              monthly experiment / paper counts per instrument model
  litradar fetch    <source> [--since --until | --days] [...] [--raw] [--out file.jsonl|-]
  litradar fetch    --list                                    raw access to one source adapter, JSON lines out
  litradar browser-check [--cdp-url URL]                      is the optional browser fetcher usable here?
  litradar show     <id|name|ticker|"First Last"> [--since 365d] [--until DATE|Nd] [--json]   entity dossier from the store
  litradar watch    add|remove|list <term> | run [--since 7d] [--until DATE|Nd] [--json]      free-text / entity / author watchlist
  litradar backfill --from YYYY-MM --to YYYY-MM [--subgroup id ...] [--country CC ...] [--max-per-month N]
                    [--topics-only] [--redo] [--dry-run] | --snapshot-dir DIR | --status
                                                              fill the corpus from OpenAlex (resumable; exit 3 = paused)
  litradar trends   [--until YYYY-MM-DD] [--weeks 13] [--out DIR | --print | --json]
                                                              what changed: this week vs last week vs the trailing baseline
  litradar export   [--parquet DIR | --csv DIR] [--duckdb FILE] [--tables a,b] [--db PATH]  export tables for analytics
  litradar tag      [--since 365d] [--translate] [--check]    tag papers with language + country codes; optionally translate non-English titles with Claude; --check only reports
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from . import __version__, backfill, browser, entities, toolkit, trends
from . import export as export_mod
from . import __version__, i18n
from .config import Settings
from .digest import deliver, render
from .extract.gazetteer import Gazetteer
from .http import Http
from .pipeline import Pipeline, window
from .store import Store


def _parse_since(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.fullmatch(r"(\d+)d", s)
    if m:
        return (datetime.now(timezone.utc).date() - timedelta(days=int(m.group(1)))).isoformat()
    return s


def _dated_window(since_arg: Optional[str], until_arg: Optional[str], default_since: str) -> tuple:
    """Resolve --since / --until for `show` and `watch run`: each is YYYY-MM-DD or Nd (N days before today);
    --until defaults to today. Raises ValueError on anything else or when since is after until."""
    since = _parse_since(since_arg) or _parse_since(default_since)
    until = _parse_since(until_arg) or datetime.now(timezone.utc).date().isoformat()
    for flag, value in (("--since", since), ("--until", until)):
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"bad {flag} {value!r}: use YYYY-MM-DD or Nd (e.g. 30d)") from None
    if since > until:
        raise ValueError(f"--since {since} is after --until {until}")
    return since, until


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


def cmd_dashboard(args) -> int:
    from . import dashboard

    settings = Settings.from_env(config_dir=args.config_dir)
    db_path = Path(args.db) if args.db else settings.db_path
    out_dir = Path(args.out or "site")
    try:
        report = dashboard.build(db_path, out_dir, weeks=args.weeks, max_papers=args.max_papers)
    except (sqlite3.Error, FileNotFoundError) as exc:
        # a locked, corrupt or missing store must not publish an all-zero site
        print(f"dashboard: cannot read {db_path}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print(json.dumps({k: v for k, v in report.items() if k != "files"}, indent=1, default=str))
        print(f"site written to {out_dir} ({report['bytes'] / 1024:.0f} KiB in {len(report['files'])} files)")
    for message in report.get("skipped_queries", []):
        print(f"warning: optional data missing, section left empty: {message}", file=sys.stderr)
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
def _entity_context(settings: Settings) -> tuple:
    instruments = Gazetteer.from_yaml(settings.yaml("instruments"), "instrument")
    companies = Gazetteer.from_yaml(settings.yaml("companies"), "company")
    subgroups = (settings.yaml("subgroups") or {}).get("subgroups", []) or []
    return instruments, companies, subgroups


def cmd_show(args) -> int:
    try:
        since, until = _dated_window(args.since, args.until, "365d")
    except ValueError as exc:
        print(f"litradar show: error: {exc}", file=sys.stderr)
        return 2
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    instruments, companies, subgroups = _entity_context(settings)
    res = entities.resolve(store, args.query, instruments, companies, subgroups)
    if res.kind == "unknown":
        if args.json:
            print(json.dumps(res.to_dict(), indent=1, ensure_ascii=False))
        else:
            print(f"nothing in the gazetteers, mentions or author lists matches {args.query!r}")
            for c in res.candidates:
                print(f"  did you mean: {c['id']:32} {c['kind']:10} {c['name']}")
        return 1
    d = entities.dossier(store, res, since, until, limit=args.limit, instruments=instruments, subgroups=subgroups)
    print(json.dumps(d, indent=1, default=str, ensure_ascii=False) if args.json else entities.render_dossier(d))
    return 0


def cmd_watch(args) -> int:
    if args.action == "run":
        try:
            since, until = _dated_window(args.since, args.until, "7d")
        except ValueError as exc:
            print(f"litradar watch run: error: {exc}", file=sys.stderr)
            return 2
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    as_json = bool(getattr(args, "json", False))
    if args.action == "add":
        instruments, companies, subgroups = _entity_context(settings)
        try:
            row = entities.watch_add(store, args.term, kind=args.kind, instruments=instruments, companies=companies, subgroups=subgroups)
        except (ValueError, sqlite3.OperationalError) as exc:
            print(f"watch: {exc}")
            return 1
        if as_json:
            print(json.dumps(row, indent=1, ensure_ascii=False))
        else:
            verb = "added" if row.get("created") else "already present"
            res = row.get("resolved")
            print(f"{verb}: {row['term']!r} as {row['kind']}" + (f" ({res['kind']} {res['id'] or res['name']}, by {res['matched_by']})" if res else ""))
        return 0
    if args.action == "remove":
        try:
            ok = entities.watch_remove(store, args.term)
        except (ValueError, sqlite3.OperationalError) as exc:
            print(f"watch: {exc}")
            return 1
        print(json.dumps({"term": args.term, "removed": ok}) if as_json else (f"removed {args.term!r}" if ok else f"{args.term!r} was not on the watchlist"))
        return 0 if ok else 1
    if args.action == "list":
        rows = entities.watch_list(store)
        if as_json:
            print(json.dumps(rows, indent=1, ensure_ascii=False))
        elif not rows:
            print("watchlist is empty")
        else:
            for r in rows:
                print(f"{r['kind']:8} {r['added_at'][:10]}  {r['term']}")
        return 0
    instruments, companies, subgroups = _entity_context(settings)
    rep = entities.watch_run(store, since, until, limit=args.limit, instruments=instruments, companies=companies, subgroups=subgroups)
    print(json.dumps(rep, indent=1, default=str, ensure_ascii=False) if as_json else entities.render_watch_report(rep))
    return 0


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r} (at least 1)")
    return value


def cmd_backfill(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    all_groups = (settings.yaml("subgroups") or {}).get("subgroups", []) or []
    store = Store(settings.db_path)
    backfill.ensure_schema(store)
    try:
        if args.status:
            rows = backfill.progress_summary(store)
            if args.json:
                print(json.dumps(rows, indent=1, default=str))
            elif not rows:
                print("no backfill progress recorded yet")
            else:
                for r in rows:
                    print(
                        f"{r['subgroup']:28} {r['country'] or 'all':6} units={r['units']:<6} fetched={r['fetched'] or 0:<8} "
                        f"capped={r.get('capped_units') or 0:<4} {r['first_month']}..{r['last_month']}  last {r['last_done']}"
                    )
            return 0
        groups = backfill.select_subgroups(all_groups, args.subgroup)
        countries = args.country or ()
        if args.redo and args.dry_run:
            print("--redo is ignored with --dry-run (nothing is cleared)")
        if args.snapshot_dir:
            root = Path(args.snapshot_dir)
            if args.redo and not args.dry_run:
                print(f"--redo: cleared {backfill.clear_snapshot_progress(store, root)} snapshot progress row(s)")
            if args.dry_run:
                rows = backfill.snapshot_plan(store, root)
                pending = [rel for rel, done in rows if not done]
                print(f"{len(rows)} snapshot file(s); {len(pending)} pending, {len(rows) - len(pending)} already done")
                for rel, done in rows[:60]:
                    print(f"  {'done   ' if done else 'pending'} {rel}")
                if len(rows) > 60:
                    print(f"  … {len(rows) - 60} more")
                return 0
            report = backfill.run_snapshot(
                store, root, groups, countries=countries,
                from_month=args.from_month or "", to_month=args.to_month or "", topics_only=args.topics_only,
            )
        else:
            if not (args.from_month and args.to_month):
                print("backfill: --from and --to (YYYY-MM) are required unless --snapshot-dir or --status is given", file=sys.stderr)
                return 2
            if args.redo and not args.dry_run:
                n = backfill.clear_progress(store, [g["id"] for g in groups], args.from_month, args.to_month, countries)
                print(f"--redo: cleared {n} progress row(s)")
            if args.dry_run:
                units = backfill.plan(groups, args.from_month, args.to_month, countries)
                pending = [u for u in units if not backfill.progress_done(store, *u, max_per_month=args.max_per_month)]
                print(f"{len(units)} unit(s) of (subgroup, month, country); {len(pending)} pending, {len(units) - len(pending)} already done")
                for sg, month, country in pending[:60]:
                    print(f"  {sg:28} {month}  {country or 'all'}")
                if len(pending) > 60:
                    print(f"  … {len(pending) - 60} more")
                return 0
            http = Http(contact_email=settings.contact_email, cache_dir=settings.cache_dir)
            report = backfill.run_api(
                store, http, groups, args.from_month, args.to_month, countries=countries,
                max_per_month=args.max_per_month, mailto=settings.contact_email, api_key=settings.openalex_api_key,
                topics_only=args.topics_only,
            )
    except backfill.BackfillError as exc:
        print(f"backfill: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(json.dumps(report, indent=1, default=str))
    print(report.get("message", ""))
    return 3 if report.get("stopped") else 0

def _iso_date(s: str) -> str:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}") from None


def cmd_trends(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    until = args.until or datetime.now(timezone.utc).date().isoformat()
    subgroups = (settings.yaml("subgroups") or {}).get("subgroups") or []
    try:
        tr = trends.compute_trends(store, until, weeks=args.weeks, subgroups=subgroups)
    except ValueError as exc:
        print(f"trends: {exc}", file=sys.stderr)
        return 2
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
def cmd_export(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    db_path = Path(args.db) if args.db else settings.db_path
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else None
    if not (args.parquet or args.csv or args.duckdb):
        print("nothing to do: pass --parquet DIR, --csv DIR and/or --duckdb FILE", file=sys.stderr)
        return 2
    reports = []
    failed = False
    try:
        if args.parquet:
            reports.append(export_mod.export_tables(db_path, Path(args.parquet), fmt="parquet", tables=tables, chunk_size=args.chunk_size, gzip_csv=args.gzip))
        if args.csv:
            reports.append(export_mod.export_tables(db_path, Path(args.csv), fmt="csv", tables=tables, chunk_size=args.chunk_size, gzip_csv=args.gzip))
        if args.duckdb:
            try:
                reports.append(export_mod.export_duckdb(db_path, Path(args.duckdb), tables=tables, chunk_size=args.chunk_size))
            except export_mod.OptionalDependencyMissing as exc:
                print(f"--duckdb skipped: {exc}", file=sys.stderr)
                failed = True
    except (OSError, ValueError, sqlite3.Error) as exc:
        # OSError covers a missing database, an output path that is a file, an unwritable destination;
        # sqlite3.Error covers "file is not a database" and locked/corrupt files
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    for rep in reports:
        if rep.message:
            print(rep.message, file=sys.stderr)
    print(json.dumps([r.to_dict() for r in reports], indent=1))
    return 1 if failed else 0
def cmd_tag(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    since, until = _parse_since(args.since), args.until
    if args.check:
        # read-only: what a dashboard would see, plus how many rows lost their tags to a re-ingest
        report = i18n.histograms(store, since=since, until=until)
        if args.json:
            print(json.dumps(report, indent=1, ensure_ascii=False))
        else:
            print(f"{report['tagged']} of {report['papers']} papers in window are tagged")
            print("languages: " + ", ".join(f"{k} {v}" for k, v in report["languages"].items()))
            top = list(report["primary_countries"].items())[:30]
            print("primary countries: " + ", ".join(f"{k} {v}" for k, v in top) + (" …" if len(report["primary_countries"]) > 30 else ""))
        if report["untagged"]:
            print(f"WARNING: {report['untagged']} papers in window are untagged (new or re-ingested since the last tag); run `litradar tag`", file=sys.stderr)
        return 0
    report = i18n.tag_store(store, since=since, until=until)
    if args.translate:
        report["translation"] = i18n.translate_titles(
            store, since=since, until=until, model=settings.llm_model, api_key=settings.anthropic_api_key, max_items=args.max_translate
        )
    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
        return 0
    print(f"tagged {report['tagged']} papers ({report['with_country']} with at least one country)")
    print("languages: " + ", ".join(f"{k} {v}" for k, v in report["languages"].items()))
    top = list(report["countries"].items())[:30]
    print("countries: " + ", ".join(f"{k} {v}" for k, v in top) + (" …" if len(report["countries"]) > 30 else ""))
    if args.translate:
        t = report["translation"]
        line = f"translation: {t['status']}"
        if t["status"] == "skipped":
            line += f" ({t.get('reason')})"
        else:
            line += (
                f" model={t['model']} candidates={t['candidates']} submitted={t['submitted']} translated={t['translated']}"
                f" refused_or_invalid={t['refused_or_invalid']} truncated={t['truncated']} errored={t['errored']}"
            )
            if t["status"] == "error":
                line += f" error={t.get('error')}"
            elif t["status"] == "incomplete":
                line += f" processing_status={t.get('processing_status')} waited_seconds={t.get('waited_seconds')}"
        print(line)
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
    # Plugin commands: any module in litradar/commands/ exposing register(subparsers) is added here,
    # so new features never need to edit this file.
    import importlib
    import pkgutil

    try:
        from . import commands as _commands_pkg

        for mod in pkgutil.iter_modules(_commands_pkg.__path__):
            try:
                m = importlib.import_module(f"litradar.commands.{mod.name}")
                if hasattr(m, "register"):
                    m.register(sub)
            except Exception as exc:  # noqa: BLE001 - a broken plugin must not kill the CLI
                logging.getLogger(__name__).warning("command module %s failed to load: %s", mod.name, exc)
    except ImportError:
        pass
    p = sub.add_parser("dashboard", help="build a static trends site from the database (publishable to GitHub Pages)")
    p.add_argument("--out", default="site", help="output directory (default: site/)")
    p.add_argument("--weeks", type=int, default=52, help="how many weeks of history to chart (default: 52)")
    p.add_argument("--db", help="database file to read (default: the configured store); opened read-only")
    p.add_argument("--max-papers", type=int, default=4000, dest="max_papers",
                   help="cap on the client-side search index (default: 4000 most recent papers in the window)")
    p.add_argument("--json", action="store_true", help="print the full file manifest as JSON")
    p.set_defaults(func=cmd_dashboard)
    p = sub.add_parser("show", help="entity dossier: instrument / company id, name, alias, ticker, or an author's 'First Last'")
    p.add_argument("query")
    p.add_argument("--since", default="365d", help="YYYY-MM-DD or Nd (default 365d)")
    p.add_argument("--until", help="YYYY-MM-DD or Nd (default today)")
    p.add_argument("--limit", type=int, default=15, help="rows per list (recent papers, co-mentions, news)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("watch", help="watchlist of free-text terms, entities and authors, evaluated against the store")
    wsub = p.add_subparsers(dest="action", required=True)
    w = wsub.add_parser("add", help="add a term (kind is auto-detected unless --kind is given)")
    w.add_argument("term")
    w.add_argument("--kind", choices=list(entities.WATCH_KINDS))
    w.add_argument("--json", action="store_true")
    w = wsub.add_parser("remove", help="remove a term")
    w.add_argument("term")
    w.add_argument("--json", action="store_true")
    w = wsub.add_parser("list", help="list watched terms")
    w.add_argument("--json", action="store_true")
    w = wsub.add_parser("run", help="report matches for every watched term in the window")
    w.add_argument("--since", default="7d", help="YYYY-MM-DD or Nd (default 7d)")
    w.add_argument("--until", help="YYYY-MM-DD or Nd (default today)")
    w.add_argument("--limit", type=int, default=20, help="rows shown per term (the match count is never capped)")
    w.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_watch)
    p = sub.add_parser("backfill", help="fill the corpus from OpenAlex month by month, or from a snapshot directory (resumable)")
    p.add_argument("--from", dest="from_month", help="first month, YYYY-MM")
    p.add_argument("--to", dest="to_month", help="last month, YYYY-MM (inclusive)")
    p.add_argument("--subgroup", action="append", help="subgroup id from config/subgroups.yaml; repeatable; default: all")
    p.add_argument("--country", action="append", help="ISO 3166-1 alpha-2 code of author institutions (e.g. CN); repeatable; default: no restriction")
    p.add_argument("--max-per-month", type=_positive_int, default=2000, help="cap per (subgroup, month, country) unit; default 2000; a larger cap reopens capped units")
    p.add_argument("--snapshot-dir", help="read OpenAlex snapshot works files (JSONL / .gz) from this directory instead of the API")
    p.add_argument("--topics-only", action="store_true", help="match on OpenAlex topics alone, without the keyword intersection (both routes)")
    p.add_argument("--redo", action="store_true", help="forget recorded progress for the selected units / snapshot files and pull them again")
    p.add_argument("--dry-run", action="store_true", help="list the pending (subgroup, month, country) units or snapshot files and exit")
    p.add_argument("--status", action="store_true", help="show recorded backfill progress and exit")
    p.add_argument("--json", action="store_true", help="with --status: print JSON")
    p.set_defaults(func=cmd_backfill)
    p = sub.add_parser("trends", help="what changed: this week vs last week vs the trailing 13-week baseline")
    p.add_argument("--until", type=_iso_date, help="last day of the current week, YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--weeks", type=_positive_int, default=trends.DEFAULT_WEEKS, help="baseline length in weeks (default 13)")
    p.add_argument("--top", type=_positive_int, default=10, help="rows per risers/fallers table (default 10)")
    p.add_argument("--out", help="directory for trends-<date>.md (default: the digest directory)")
    p.add_argument("--print", action="store_true", help="print the Markdown instead of writing a file")
    p.add_argument("--json", action="store_true", help="print the structured result as JSON")
    p.set_defaults(func=cmd_trends)
    p = sub.add_parser("export", help="export tables to Parquet/CSV files or a DuckDB database (for trend analysis)")
    p.add_argument("--parquet", metavar="DIR", help="one <table>.parquet per table (needs pyarrow; falls back to CSV with a message)")
    p.add_argument("--csv", metavar="DIR", help="one <table>.csv per table (standard library only)")
    p.add_argument("--duckdb", metavar="FILE", help="write/refresh a DuckDB database (needs the duckdb package)")
    p.add_argument("--tables", help=f"comma-separated subset; default {','.join(export_mod.DEFAULT_TABLES)}; also {','.join(export_mod.OPTIONAL_TABLES)}")
    p.add_argument("--gzip", action="store_true", help="gzip the CSV files")
    p.add_argument("--db", help="database to export (default: the configured LITRADAR_DB / data/radar.sqlite); opened read-only")
    p.add_argument("--chunk-size", type=int, default=20000, help="rows per batch")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("tag", help="write language ('lang') and country codes ('countries') into papers.extra_json for dashboards and trends")
    p.add_argument("--since", help="YYYY-MM-DD or Nd (e.g. 365d); default: everything")
    p.add_argument("--until", help="YYYY-MM-DD; default: open-ended")
    p.add_argument("--translate", action="store_true", help="translate non-English titles/abstracts into English with Claude via one Message Batches request (needs ANTHROPIC_API_KEY; model from LITRADAR_LLM_MODEL)")
    p.add_argument("--max-translate", type=int, default=1000, help="cap on papers sent for translation per run")
    p.add_argument("--check", action="store_true", help="read-only: histograms of already-tagged papers and a warning if any row in the window is untagged (tags are lost on re-ingest)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tag)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
