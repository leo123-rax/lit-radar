"""`litradar maintenance`: store housekeeping that the weekly run never does on its own.

  litradar maintenance [--stats-by-table] [--purge-sra-placeholders] [--refresh-entity-names] [--fts-rebuild] [--vacuum] [--json]

  --stats-by-table         row counts and on-disk bytes per table / index (the default when no action is given)
  --purge-sra-placeholders delete archive_records rows whose accession is a synthetic `SRA-UID-...` placeholder
                           (written by an older SRA parser when the experiment accession was missing; they carry
                           no study, platform or library strategy and cannot be de-duplicated against ENA)
  --refresh-entity-names   rewrite entity_name / ticker (and vendor / category for instruments) of every stored
                           mention from the current gazetteers: ids are stable, display names are not, and the
                           rows `rematch` keeps because their text is not stored would otherwise show the old name
  --fts-rebuild            drop and re-create the papers_fts index from `papers` (bodies and mentions preserved)
  --vacuum                 return freed pages to the operating system (rewrites the file; needs free disk space
                           of about the file's size)

Actions run in that order (purge, refresh, rebuild, vacuum) and the stats, when asked for, describe the store
afterwards. Every number printed is measured on this database, nothing is estimated.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from ..config import Settings
from ..extract.gazetteer import Entry, Gazetteer
from ..store import Store

ACTIONS = ("purge_sra_placeholders", "refresh_entity_names", "fts_rebuild", "vacuum", "stats_by_table")


def gazetteer_entries(settings: Settings) -> List[Entry]:
    """Every instrument and company entry of the configured gazetteers."""
    out: List[Entry] = []
    for name, kind in (("instruments", "instrument"), ("companies", "company")):
        out.extend(Gazetteer.from_yaml(settings.yaml(name) or {}, kind).entries)
    return out


def run_maintenance(store: Store, purge_sra_placeholders: bool = False, refresh_entity_names: bool = False, fts_rebuild: bool = False,
                    vacuum: bool = False, stats_by_table: bool = False, entries: Optional[Sequence[Entry]] = None) -> Dict[str, Any]:
    """Run the requested actions on `store` and return what happened, as plain data. `entries` are the gazetteer
    entries `refresh_entity_names` rewrites from (the CLI passes the configured gazetteers)."""
    report: Dict[str, Any] = {"actions": []}
    if purge_sra_placeholders:
        before = store.count_sra_placeholders()
        deleted = store.purge_sra_placeholders()
        report["purge_sra_placeholders"] = {"found": before, "deleted": deleted, "remaining": store.count_sra_placeholders(),
                                            "archive_records": store.counts()["archive_records"]}
        report["actions"].append("purge_sra_placeholders")
    if refresh_entity_names:
        entries = list(entries or [])
        report["refresh_entity_names"] = {"entries": len(entries), "rows_updated": store.refresh_entity_metadata(entries)}
        report["actions"].append("refresh_entity_names")
    if fts_rebuild:
        written = store.rebuild_fts()
        report["fts_rebuild"] = {"rows_written": written, "papers": store.counts()["papers"]}
        report["actions"].append("fts_rebuild")
    if vacuum:
        report["vacuum"] = store.vacuum()
        report["actions"].append("vacuum")
    if stats_by_table:
        report["stats"] = store.table_stats()
        report["actions"].append("stats_by_table")
    return report


def format_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    p = report.get("purge_sra_placeholders")
    if p:
        lines.append(f"SRA placeholder rows: {p['found']:,} found, {p['deleted']:,} deleted, {p['remaining']:,} remaining "
                     f"({p['archive_records']:,} archive records left)")
    r = report.get("refresh_entity_names")
    if r:
        lines.append(f"entity names refreshed from {r['entries']:,} gazetteer entries: {r['rows_updated']:,} mention row(s) updated")
    f = report.get("fts_rebuild")
    if f:
        lines.append(f"papers_fts rebuilt: {f['rows_written']:,} rows written for {f['papers']:,} papers; segments merged")
    v = report.get("vacuum")
    if v:
        saved = v["bytes_before"] - v["bytes_after"]
        lines.append(f"VACUUM: {v['bytes_before']:,} -> {v['bytes_after']:,} bytes ({saved:,} bytes returned; "
                     f"freelist before: {v['freelist_before']:,} pages)")
    s = report.get("stats")
    if s:
        if lines:
            lines.append("")
        lines.append(f"file: {s['file_bytes']:,} bytes · page size {s['page_size']:,} · {s['page_count']:,} pages · "
                     f"freelist {s['freelist_count']:,} pages ({s['freelist_bytes']:,} bytes)")
        if s.get("sra_placeholders"):
            lines.append(f"ATTENTION: {s['sra_placeholders']:,} SRA placeholder rows (accession 'SRA-UID-...') in archive_records; "
                         "run `litradar maintenance --purge-sra-placeholders`")
        if not s.get("bytes_available"):
            lines.append("(this SQLite build has no dbstat table: bytes per object are unavailable)")
        lines.append(f"{'object':44} {'type':6} {'rows':>10} {'bytes':>14}")
        for o in s["objects"]:
            rows = f"{o['rows']:,}" if o["rows"] is not None else "-"
            size = f"{o['bytes']:,}" if o["bytes"] is not None else "n/a"
            lines.append(f"{o['name']:44} {o['type']:6} {rows:>10} {size:>14}")
    if not lines:
        lines.append("nothing to do (choose --stats-by-table, --purge-sra-placeholders, --refresh-entity-names, --fts-rebuild and/or --vacuum)")
    return "\n".join(lines)


def cmd_maintenance(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    wanted = {a: bool(getattr(args, a, False)) for a in ACTIONS}
    if not any(wanted.values()):
        wanted["stats_by_table"] = True  # read-only default
    entries = gazetteer_entries(settings) if wanted["refresh_entity_names"] else None
    store = Store(settings.db_path)
    try:
        report = run_maintenance(store, entries=entries, **wanted)
    finally:
        store.close()
    print(json.dumps(report, indent=1, default=str) if args.json else format_report(report))
    return 0


def register(sub) -> None:
    p = sub.add_parser("maintenance", help="store housekeeping: table stats, purge SRA placeholder rows, refresh entity names, rebuild the FTS index, VACUUM")
    p.add_argument("--stats-by-table", action="store_true", dest="stats_by_table", help="row counts and bytes per table / index (default action)")
    p.add_argument("--purge-sra-placeholders", action="store_true", dest="purge_sra_placeholders",
                   help="delete archive_records rows with a synthetic 'SRA-UID-...' accession and report how many")
    p.add_argument("--refresh-entity-names", action="store_true", dest="refresh_entity_names",
                   help="rewrite stored mentions' entity_name / ticker (and vendor / category for instruments) from the current gazetteers")
    p.add_argument("--fts-rebuild", action="store_true", dest="fts_rebuild", help="drop and re-create papers_fts from the papers table")
    p.add_argument("--vacuum", action="store_true", help="VACUUM the database file (rewrites it; needs free disk space of about its size)")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.set_defaults(func=cmd_maintenance)
