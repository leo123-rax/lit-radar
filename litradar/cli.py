"""Command-line interface.

  litradar run      [--days 7 | --since YYYY-MM-DD --until YYYY-MM-DD] [--llm] [--no-fulltext] [--sources a,b] [--email] [--slack]
  litradar digest   [--since --until] [--out DIR] [--allow-empty]  re-render from the database without fetching (exit 1 before the first run)
  litradar rematch  [--since 90d]                             re-annotate stored papers after editing the gazetteers
  litradar search   "query" [--since 90d] [--in methods,coi] [--limit 50] [--raw-fts] [--json]
                                                              a plain multi-word query is a phrase; --raw-fts passes FTS5 syntax through
  litradar entity   <entity_id> [--since 90d] [--limit 100]   distinct papers that mention one gazetteer entry
  litradar entities [--kind instrument|company] [--grep text] list gazetteer entries and ids
  litradar stats    [--json]                                  database totals and last runs
  litradar dashboard --out site/ [--weeks 52]                 build the static trends site from the database
  litradar check-config                                       validate YAML files and report duplicates
  litradar timeseries [--all | --entity id ... | --vendor name ...] [--from YYYY-MM --to YYYY-MM] [--sources sra,ena,epmc] [--out DIR] [--json]
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

The list above is the built-in set only. Every module in litradar/commands/ that exposes register(subparsers)
is added at start-up by the plugin loader in main() (update, status, coverage, serve, track, companies, ...);
`litradar --help` prints the complete, current command list.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# The stock macOS Python 3.9 links LibreSSL and urllib3 v2 warns about it on import, on every command.
# Nothing here depends on that OpenSSL feature set, so the warning is noise for this CLI.
warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL")

from . import __version__, backfill, browser, entities, i18n, toolkit, trends  # noqa: E402
from . import export as export_mod  # noqa: E402
from . import query as Q  # noqa: E402
from .config import (  # noqa: E402
    ConfigError,
    Settings,
    check_config_dir,
    duplicate_top_level_keys,
    format_yaml_error,
    load_yaml_checked,
    resolve_config_dir,
)
from .coverage import render_coverage_markdown  # noqa: E402
from .digest import deliver, render  # noqa: E402
from .extract.gazetteer import Gazetteer  # noqa: E402
from .http import Http  # noqa: E402
from .pipeline import Pipeline, window  # noqa: E402
from .store import FTS_COLUMNS, Store  # noqa: E402

_parse_since = Q.parse_since  # kept for callers that imported the old private name


def _dated_window(since_arg: Optional[str], until_arg: Optional[str], default_since: str) -> tuple:
    """Resolve --since / --until for `show` and `watch run`. The parser has already validated and resolved
    both (YYYY-MM-DD or Nd, see litradar.query); --until defaults to today. Raises ValueError when since is
    after until."""
    since = Q.parse_since(since_arg) or Q.parse_since(default_since)
    until = Q.parse_date(until_arg, "--until") or datetime.now(timezone.utc).date().isoformat()
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
    out = render.render(pipe.store, since, until, settings.radar, run_report=report, company_types=pipe.company_types)
    out["markdown"] += "\n" + render_coverage_markdown(pipe.store, since, until, settings)
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
    pipe.store.close()
    return 0  # source errors are reported inside the digest, not as a failed exit


def cmd_digest(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    since, until = _window(args)
    db_path = Path(settings.db_path)
    hint = "no runs yet; run `litradar run --days 7` first (or pass --allow-empty to render an empty digest anyway)"
    if not db_path.exists() and not getattr(args, "allow_empty", False):
        # never create an empty database as a side effect of rendering
        print(f"litradar digest: {hint}: no database at {db_path}", file=sys.stderr)
        return 1
    store = Store(settings.db_path)
    if not getattr(args, "allow_empty", False):
        runs = store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        if not runs:
            store.close()
            print(f"litradar digest: {hint}: {db_path} holds no completed run", file=sys.stderr)
            return 1
    company_types = {str(e["id"]): str(e.get("type") or "") for e in (settings.yaml("companies") or {}).get("entries", []) if e.get("id")}
    out = render.render(store, since, until, settings.radar, company_types=company_types)
    out["markdown"] += "\n" + render_coverage_markdown(store, since, until, settings)
    out_dir = Path(args.out or settings.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{until}.md").write_text(out["markdown"], encoding="utf-8")
    (out_dir / "latest.md").write_text(out["markdown"], encoding="utf-8")
    print(out["markdown"] if args.print else f"digest written to {out_dir / (until + '.md')}")
    return 0


def cmd_rematch(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    pipe = Pipeline(settings)
    report = pipe.rematch(since=args.since or None)
    print(json.dumps(report, indent=1))
    return 0


def _parse_sections(text: Optional[str]) -> tuple:
    """`--in methods,coi` -> ('methods', 'coi'); unknown names raise ValueError with the valid list."""
    parts = []
    for raw in (text or "").split(","):
        s = raw.strip().lower()
        if s and s not in parts:
            parts.append(s)
    bad = [s for s in parts if s not in FTS_COLUMNS]
    if bad:
        raise ValueError(f"unknown section(s) {', '.join(bad)}; choose from {', '.join(FTS_COLUMNS)}")
    return tuple(parts)


def _attach_scores(store: Store, rows: List[dict]) -> None:
    """Add `score: {final, heuristic, reasons}` (or None) to paper rows, so `--json` shows what ranking did."""
    for r in rows:
        sc = store.get_score(r["id"]) if r.get("id") else None
        r["score"] = {"final": sc.get("final"), "heuristic": sc.get("heuristic"), "reasons": sc.get("reasons")} if sc else None


def cmd_search(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    try:
        sections = _parse_sections(args.in_sections)
    except ValueError as exc:
        print(f"litradar search: error: {exc}", file=sys.stderr)
        return 2
    store = Store(settings.db_path)
    query = args.query.strip()
    if not query:
        print("litradar search: error: the query is empty", file=sys.stderr)
        return 2
    fts_query = query if args.raw_fts else Q.auto_phrase(query)
    if fts_query != query:
        print(f"litradar search: searching for the phrase {fts_query} (pass --raw-fts to use FTS5 syntax as typed)", file=sys.stderr)
    err = Q.fts_syntax_error(store.conn, Q.with_sections(fts_query, sections))
    if err:
        fts_query = Q.phrase_query(query)
        print(f"litradar search: note: the query is not valid FTS5 ({err}); searching for it as the phrase {fts_query} instead", file=sys.stderr)
    rows = store.search(fts_query, since=args.since or None, limit=args.limit, sections=sections)
    if args.json:
        _attach_scores(store, rows)
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
    rows, total, kind = entities.entity_papers(store, args.entity_id, since=args.since or None, limit=args.limit)
    if kind is None:
        # never seen in the mentions table: a valid gazetteer id with no hits, or a typo
        instruments, companies, _ = _entity_context(settings)
        known = next((e for gz in (instruments, companies) for e in gz.entries if e.id.lower() == args.entity_id.lower()), None)
        if known is None:
            print(f"litradar entity: {args.entity_id!r} is not a gazetteer id and has no stored mentions "
                  f"(find ids with `litradar entities --grep <text>`; `litradar show <name>` also accepts names, aliases and tickers)", file=sys.stderr)
            return 1
    if args.json:
        _attach_scores(store, rows)
        print(json.dumps({"entity_id": args.entity_id, "kind": kind, "since": args.since or None, "papers": total, "shown": len(rows), "results": rows}, indent=1, default=str))
        return 0
    shown = f" (showing {len(rows)})" if len(rows) < total else ""
    print(f"{total} paper{'s' if total != 1 else ''} mention {args.entity_id}{shown}")
    for r in rows:
        secs = "/".join(r.get("sections") or []) or "-"
        print(f"- {r['pub_date'] or 'n.d.'}  [{secs}] {r['title']}\n    {r['url']}\n    {(r.get('snippet') or '')[:200]}")
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
    counts = store.counts()
    runs = [
        {"id": r[0], "started": r[1], "finished": r[2], "since": r[3], "until": r[4]}
        for r in store.conn.execute("SELECT id, started, finished, since, until FROM runs ORDER BY id DESC LIMIT 5")
    ]
    if getattr(args, "json", False):
        print(json.dumps({"counts": counts, "runs": runs}, indent=1))
        return 0
    for k, v in counts.items():
        print(f"{k:22} {v:>10,}")
    print(f"recent runs ({len(runs)}):" if runs else "recent runs: none recorded")
    for r in runs:
        print(f"  {r['started']} -> {r['finished']}  window {r['since']}..{r['until']}")
    return 0


def cmd_dashboard(args) -> int:
    from . import dashboard

    settings = Settings.from_env(config_dir=args.config_dir)
    db_path = Path(args.db) if args.db else settings.db_path
    out_dir = Path(args.out or "site")
    try:
        # config_dir names the archive models and vendors on timeseries.html; the committed CSV under
        # <out_dir>/timeseries is copied into the site so the page's download link resolves there too
        report = dashboard.build(db_path, out_dir, weeks=args.weeks, max_papers=args.max_papers,
                                 config_dir=Path(settings.config_dir), timeseries_dir=Path(settings.out_dir) / "timeseries")
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

    cfg_dir = resolve_config_dir(args.config_dir)
    problems = check_config_dir(cfg_dir)
    print(f"config dir: {cfg_dir}")
    if problems:
        for pr in problems:
            print(f"check-config: {pr}", file=sys.stderr)
        return 1
    try:
        settings = Settings.from_env(config_dir=cfg_dir)
    except yaml.YAMLError as exc:
        print("check-config: " + format_yaml_error(cfg_dir / "radar.yaml", exc), file=sys.stderr)
        return 1
    ok = True
    for name, kind in (("instruments", "instrument"), ("companies", "company")):
        try:
            data = load_yaml_checked(cfg_dir / f"{name}.yaml")
        except ConfigError as exc:
            print(f"check-config: {exc}", file=sys.stderr)
            return 1
        if not isinstance(data, dict):
            print(f"check-config: {name}.yaml: expected a mapping with an `entries:` list", file=sys.stderr)
            return 1
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
        path = cfg_dir / f"{name}.yaml"
        try:
            d = load_yaml_checked(path)
        except ConfigError as exc:
            print(f"check-config: {exc}", file=sys.stderr)
            ok = False
            continue
        print(f"{name}: ok ({len(d) if isinstance(d, dict) else 0} top-level keys)")
        for key, lines in sorted(duplicate_top_level_keys(path).items()):
            print(f"  WARNING {name}.yaml: top-level key '{key}' appears {len(lines)} times (lines {', '.join(map(str, lines))}); "
                  f"YAML keeps the last one, merge them by hand")
    # archive_models.yaml (instrument time series): ids must exist in instruments.yaml unless `proposed`
    models_path = Path(settings.config_dir) / "archive_models.yaml"
    if models_path.exists():
        from . import timeseries as ts

        try:
            specs = ts.load_archive_models(models_path)
        except yaml.YAMLError as exc:
            print("check-config: " + format_yaml_error(models_path, exc), file=sys.stderr)
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
        all_entities=bool(args.all),
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
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print(timeseries_summary(report))
    return 1 if report.get("errors") else 0


def timeseries_summary(report: dict) -> str:
    """The human-readable account of a `timeseries` run (`--json` prints the whole report instead): the
    selection, the request arithmetic per source, what the cache folding did, the outputs, then every
    warning and error verbatim. Numbers are exact; nothing is rounded."""
    lines = []
    if report.get("entities"):
        lines.append(
            f"timeseries: {len(report['entities'])} entities, {report.get('series', 0)} series, {report.get('months', 0)} months "
            f"({report.get('from')}..{report.get('to')}), sources {','.join(report.get('sources') or [])}"
        )
    bf = report.get("backfill") or {}
    if bf:
        per_source = ", ".join(
            f"{src} {v.get('requests', 0)} requests / {v.get('cached', 0)} cached" for src, v in (bf.get("by_source") or {}).items()
        )
        verb = "would make" if report.get("dry_run") else "made"
        lines.append(
            f"{'dry run: ' if report.get('dry_run') else 'backfill: '}{verb} {bf.get('requests', 0)} requests; {bf.get('cached', 0)} cached, "
            f"{bf.get('pre_launch', 0)} pre-launch zeros, {len(bf.get('failed') or [])} failed, {len(bf.get('kept') or [])} kept; "
            f"refresh cutoff {bf.get('cutoff')}" + (f" ({per_source})" if per_source else "")
        )
    cons = report.get("consolidated") or {}
    if cons.get("derived") or cons.get("pruned") or cons.get("partial"):
        verb = "would fold" if report.get("dry_run") else "folded"
        lines.append(
            f"cache layout: {verb} {cons.get('pruned', 0)} per-string rows into {cons.get('derived', 0)} entity rows "
            f"({len(cons.get('entities') or [])} entity/source pairs; {cons.get('partial', 0)} months left to the backfill)"
        )
    outputs = report.get("outputs") or {}
    if outputs:
        png = outputs.get("png")
        png_text = "" if "png" not in outputs else (f", png {png}" if png else ", png skipped (matplotlib not installed)")
        lines.append(f"outputs: {outputs.get('csv')} ({outputs.get('rows', 0)} rows), {outputs.get('markdown')}{png_text}")
    for w in report.get("warnings") or []:
        lines.append(f"warning: {w}")
    for e in report.get("errors") or []:
        lines.append(f"error: {e}")
    return "\n".join(lines) if lines else "timeseries: nothing to report"
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
    d = entities.dossier(store, res, since, until, limit=args.limit, instruments=instruments, companies=companies, subgroups=subgroups)
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
        instruments, companies, subgroups = _entity_context(settings)
        try:
            removed = entities.watch_remove(store, args.term, instruments=instruments, companies=companies, subgroups=subgroups)
        except (ValueError, sqlite3.OperationalError) as exc:
            print(f"watch: {exc}")
            return 1
        ok = removed is not None
        if as_json:
            print(json.dumps({"term": args.term, "removed": ok, "stored_as": removed}))
        elif ok:
            print(f"removed {removed!r}" + (f" (given as {args.term!r})" if removed != args.term else ""))
        else:
            print(f"{args.term!r} was not on the watchlist (see `litradar watch list`)")
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
    since, until = args.since or None, args.until or None
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


HELP_EPILOG = """\
start here (from the repository root; every command reads config/ and .env from there):
  litradar check-config          validate the YAML in config/ (exit 0 = good to go)
  litradar run --days 7          first pull: 10-30 minutes with keys, longer without; the digest lands in digests/
  litradar status                freshness, per-source counts and which keys are set; exits 1 until the first run
  litradar serve                 read-only JSON API and search page on http://127.0.0.1:8787 (/api lists routes)
keys:  cp .env.example .env and fill in what you have; every key is optional and the pipeline runs with none
docs:  README.md, docs/FORKING.md (fork checklist), docs/deploy.md (GitHub Actions, Pages), docs/COOKBOOK.md
"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="litradar",
        description="Weekly science-literature radar",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config-dir", default=None, help="directory with radar.yaml etc. (default: ./config)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"litradar {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    since_help = f"window start: {Q.SINCE_FORMAT}"
    until_help = "window end: YYYY-MM-DD or Nd (default: today, UTC)"
    p = sub.add_parser("run", help="fetch, extract, rank and render the digest")
    p.add_argument("--days", type=_positive_int, default=None, help="look-back window in days when --since is not given (default 7)")
    p.add_argument("--since", type=Q.since_arg, help=since_help)
    p.add_argument("--until", type=Q.until_arg, help=until_help)
    p.add_argument("--llm", action="store_true", help="score the shortlist with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--no-fulltext", action="store_true", help="skip open-access full-text retrieval (metadata and abstracts only)")
    p.add_argument("--sources", help="comma-separated subset: biorxiv,medrxiv,europepmc,openalex,crossref,arxiv,rss,chinaxiv,scielo,kci,jstage,ena,sra,geo,pride,cellxgene,gsa,gsa_human,nih_reporter,clinicaltrials,openfda,semantic_scholar,news (see docs/SOURCES.md)")
    p.add_argument("--out", help="digest directory (default: out_dir in radar.yaml / LITRADAR_OUT_DIR, else digests/)")
    p.add_argument("--email", action="store_true", help="e-mail the digest (SMTP_* and DIGEST_* in .env)")
    p.add_argument("--slack", action="store_true", help="post the top 10 to Slack (SLACK_WEBHOOK_URL in .env)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("digest", help="re-render the digest from the database")
    p.add_argument("--days", type=_positive_int, default=None, help="look-back window in days when --since is not given (default 7)")
    p.add_argument("--since", type=Q.since_arg, help=since_help)
    p.add_argument("--until", type=Q.until_arg, help=until_help)
    p.add_argument("--out", help="digest directory (default: out_dir in radar.yaml / LITRADAR_OUT_DIR, else digests/)")
    p.add_argument("--print", action="store_true", help="print the Markdown to stdout as well as writing it")
    p.add_argument("--allow-empty", action="store_true", dest="allow_empty",
                   help="render even when no run is recorded yet (default: exit 1 with a hint instead of writing a 'nothing in this window' digest)")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("rematch", help="re-run gazetteer matching and scoring over stored papers (after editing config)")
    p.add_argument("--since", type=Q.since_arg, help=f"only papers from this date: {Q.SINCE_FORMAT}; default: everything")
    p.set_defaults(func=cmd_rematch)

    p = sub.add_parser(
        "search",
        help="full-text search of stored papers",
        description="Full-text search (SQLite FTS5, porter stemming) over title, abstract, Methods, funding, competing interests, "
                    "affiliations and open-access body text. A plain multi-word query is searched as a PHRASE "
                    "(\"liquid biopsy\" = those words adjacent, in order); anything using FTS5 syntax is passed through "
                    "as typed: methods:Xenium, \"exact phrase\", Xenium OR Visium, Xenium NOT Visium, NovaSeq*, NEAR(a b, 10), "
                    "{methods coi}: Illumina. Note: matching is on stored text, so a product the gazetteer attributed through "
                    "an alias is found by `litradar entity <id>`, not necessarily by its name here. Full syntax: docs/search.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query")
    p.add_argument("--since", type=Q.since_arg, help=since_help)
    p.add_argument("--in", dest="in_sections", help="restrict to columns: title,abstract,methods,funding,coi,affiliations,body")
    p.add_argument("--limit", type=_positive_int, default=50, help="rows to print (default 50)")
    p.add_argument("--raw-fts", action="store_true", dest="raw_fts", help="pass the query to FTS5 exactly as typed (no automatic phrase quoting)")
    p.add_argument("--json", action="store_true", help="machine-readable output, with each paper's score")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("entity", help="distinct papers mentioning one gazetteer entry (ids: `litradar entities`)")
    p.add_argument("entity_id", help="gazetteer id, e.g. txg-xenium or illumina (list them with `litradar entities --grep <text>`)")
    p.add_argument("--since", type=Q.since_arg, help=since_help + "; default: everything")
    p.add_argument("--limit", type=_positive_int, default=100, help="papers to print (default 100); the headline count is never capped")
    p.add_argument("--json", action="store_true", help="machine-readable output: {entity_id, kind, papers, shown, results[]} with scores")
    p.set_defaults(func=cmd_entity)

    p = sub.add_parser("entities", help="list gazetteer entries")
    p.add_argument("--kind", choices=["instrument", "company"], help="only instruments or only companies (default both)")
    p.add_argument("--grep", help="case-insensitive substring filter over the printed line (id, name, vendor, ticker)")
    p.set_defaults(func=cmd_entities)

    p = sub.add_parser("stats", help="database totals and recent runs")
    p.add_argument("--json", action="store_true", help="one JSON object {counts, runs} instead of text")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("check-config", help="validate config files (exit 1 when the directory, a required file or the YAML is broken)")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("timeseries", help="monthly counts of SRA/ENA experiments and Europe PMC papers per instrument model")
    p.add_argument("--all", action="store_true", help="every entity in config/archive_models.yaml (what the weekly workflow runs)")
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
    p.add_argument("--json", action="store_true", help="print the full report as JSON instead of the summary lines")
    p.set_defaults(func=cmd_timeseries)

    # raw access to every source adapter without the pipeline (litradar/toolkit.py)
    toolkit.add_fetch_parser(sub)

    p = sub.add_parser("browser-check", help="report whether the optional real-browser fetcher is installed and a Chrome DevTools endpoint is listening")
    p.add_argument("--cdp-url", help="Chrome DevTools URL to probe (default: browser.cdp_url in radar.yaml, else http://localhost:9222)")
    p.set_defaults(func=cmd_browser_check)
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
    p.add_argument("--since", type=Q.since_arg, default="365d", help="YYYY-MM-DD or Nd (default 365d)")
    p.add_argument("--until", type=Q.until_arg, help="YYYY-MM-DD or Nd (default today)")
    p.add_argument("--limit", type=_positive_int, default=15, help="rows per list (recent papers, co-mentions, news)")
    p.add_argument("--json", action="store_true", help="machine-readable dossier (recent papers carry their score)")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "watch",
        help="watchlist of free-text terms, entities and authors, evaluated against the store",
        description="Watchlist of free-text terms, gazetteer entities and authors. The list is stored in the SQLite database "
                    "(table `watchlist`), not in config/, so it follows LITRADAR_DB and survives a different --config-dir. "
                    "A multi-word free-text term matches as a phrase (the words adjacent, in order); use FTS5 syntax "
                    "(quotes, AND/OR/NOT, prefix*) for anything else. `remove` accepts the same spelling `add` did.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    wsub = p.add_subparsers(dest="action", required=True)
    w = wsub.add_parser("add", help="add a term (kind is auto-detected unless --kind is given)")
    w.add_argument("term", help="free text (phrase match), a gazetteer id/name/alias/ticker, or an author's 'First Last'")
    w.add_argument("--kind", choices=list(entities.WATCH_KINDS), help="force the kind instead of auto-detecting it")
    w.add_argument("--json", action="store_true")
    w = wsub.add_parser("remove", help="remove a term (by the stored term, or by the name/alias/ticker that resolved to it)")
    w.add_argument("term")
    w.add_argument("--json", action="store_true")
    w = wsub.add_parser("list", help="list watched terms")
    w.add_argument("--json", action="store_true")
    w = wsub.add_parser("run", help="report matches for every watched term in the window")
    w.add_argument("--since", type=Q.since_arg, default="7d", help="YYYY-MM-DD or Nd (default 7d)")
    w.add_argument("--until", type=Q.until_arg, help="YYYY-MM-DD or Nd (default today)")
    w.add_argument("--limit", type=_positive_int, default=20, help="rows shown per term (the match count is never capped)")
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
    p.add_argument("--since", type=Q.since_arg, help="YYYY-MM-DD or Nd (e.g. 365d); default: everything")
    p.add_argument("--until", type=Q.until_arg, help="YYYY-MM-DD or Nd; default: open-ended")
    p.add_argument("--translate", action="store_true", help="translate non-English titles/abstracts into English with Claude via one Message Batches request (needs ANTHROPIC_API_KEY; model from LITRADAR_LLM_MODEL)")
    p.add_argument("--max-translate", type=int, default=1000, help="cap on papers sent for translation per run")
    p.add_argument("--check", action="store_true", help="read-only: histograms of already-tagged papers and a warning if any row in the window is untagged (tags are lost on re-ingest)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tag)

    try:
        args = ap.parse_args(argv)
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        rc = args.func(args)
        sys.stdout.flush()  # small outputs sit in the buffer until exit: surface a closed pipe here, where it is handled
        return rc
    except BrokenPipeError:
        # the reader went away (`litradar entities | head`): finish quietly, and point stdout at /dev/null so the
        # interpreter's final flush does not print "Exception ignored ... BrokenPipeError" either
        _silence_stdout()
        return 0
    except SystemExit:
        # --help, --version and usage errors exit through argparse; give their buffered output the same treatment
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            _silence_stdout()
        raise


def _silence_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def cmd_browser_check(args) -> int:
    """`browser-check`: informational unless the browser fetcher is enabled in radar.yaml (then exit 1 when unusable)."""
    rc = browser.cmd_browser_check(args)
    if rc == 0:
        return 0
    settings = Settings.from_env(config_dir=getattr(args, "config_dir", None))
    enabled = bool(((settings.radar or {}).get("browser") or {}).get("enabled"))
    return rc if enabled else 0


if __name__ == "__main__":
    sys.exit(main())
