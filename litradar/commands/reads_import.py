"""`litradar reads-import`: fold the committed reading facts into the database.

  litradar reads-import [paths ...]                      default: reads/*.jsonl
  litradar reads-import --from-read-json IN --to OUT     convert `litradar read --json` lines to facts

The first form is what the hosted jobs run right after the database is restored: for every DOI the
facts name, the paper is marked as read under a personal subscription, the sections it carried are
recorded (names only, never text), its full-text mentions are replaced by the record's, and it is
re-scored. A DOI the database does not hold is counted and skipped - the facts never create papers.

The second form converts the output of a hand-run `litradar read --json` batch into the same facts
format, dropping every text field, so an interactive session's reads land in `reads/` too.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import reader
from ..config import Settings


def _default_paths(settings: Settings) -> list:
    return sorted(reader.reads_dir(settings).glob("*.jsonl"))


def _convert(args, settings: Settings) -> int:
    src = Path(args.from_read_json)
    if not src.is_file():
        print(f"reads-import: no such file: {src}", file=sys.stderr)
        return 1
    names = reader.entity_ids_by_name(settings)
    records, skipped = [], 0
    for obj in reader.iter_records([src]):
        rec = reader.convert_read_json(obj, names)
        if rec is None:
            skipped += 1
            continue
        records.append(rec)
    if not records:
        print(f"reads-import: no successful reads in {src} ({skipped} line(s) skipped); nothing written")
        return 0
    out = Path(args.to) if args.to else reader.reads_dir(settings) / f"{records[0]['read_at'][:10]}.jsonl"
    known = {r.get("doi") for r in reader.iter_records([out])} if out.exists() else set()
    written = 0
    for rec in records:
        if rec["doi"] in known:
            continue
        reader.append_record(out, rec)
        known.add(rec["doi"])
        written += 1
    print(f"converted {written} read(s) into {out} ({skipped} line(s) without a successful read skipped, "
          f"{len(records) - written} already present)")
    return 0


def cmd_reads_import(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    if args.from_read_json:
        return _convert(args, settings)

    paths = [Path(p) for p in args.paths] if args.paths else _default_paths(settings)
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"reads-import: no such file(s): {', '.join(str(m) for m in missing)}", file=sys.stderr)
        return 1
    records = reader.iter_records(paths)
    if not records:
        print("reads-import: no facts records found; nothing to do")
        return 0

    db_path = Path(settings.db_path)
    if not db_path.is_file() or db_path.stat().st_size == 0:
        print(f"reads-import: no database at {db_path} (LITRADAR_DB or db_path in radar.yaml names the file)",
              file=sys.stderr)
        return 1
    from ..store import Store

    store = Store(db_path)
    pipe = None
    if not args.no_rank:
        try:
            from ..pipeline import Pipeline

            pipe = Pipeline(settings, http=object(), store=store)
        except Exception as exc:  # noqa: BLE001 - importing facts matters more than re-scoring them
            print(f"reads-import: re-scoring unavailable ({exc}); importing the facts only", file=sys.stderr)
    try:
        counts = reader.import_records(store, records, pipe=pipe)
    finally:
        store.close()
    report = {
        "files": [str(p) for p in paths], "records": len(records),
        "reads.imported": counts["imported"], "reads.skipped": counts["skipped"],
        "reads.unknown_doi": counts["unknown_doi"], "reads.invalid": counts["invalid"],
        "reads.rescored": counts["rescored"], "reads.mentions": counts["mentions"],
    }
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(f"reads.imported={counts['imported']} reads.skipped={counts['skipped']} "
              f"reads.unknown_doi={counts['unknown_doi']} (from {len(records)} record(s) in {len(paths)} file(s); "
              f"{counts['mentions']} mention(s) replaced, {counts['rescored']} paper(s) re-scored)")
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "reads-import",
        help="import the committed reading facts (reads/*.jsonl) into the database; no article text is involved",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*", help="facts files to import (default: reads/*.jsonl)")
    p.add_argument("--from-read-json", dest="from_read_json",
                   help="convert a `litradar read --json` output file into facts records instead of importing")
    p.add_argument("--to", help="where the converted facts are written (default: reads/<read date>.jsonl)")
    p.add_argument("--no-rank", action="store_true", dest="no_rank", help="do not re-score the imported papers")
    p.add_argument("--json", action="store_true", help="machine-readable counts")
    p.set_defaults(func=cmd_reads_import)
