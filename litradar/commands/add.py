"""`litradar add`: put papers into the corpus by identifier. `litradar tags`: find them again.

  litradar add <id> [<id> ...] [--file ids.txt] [--fulltext | --no-fulltext] [--tag LABEL ...] [--json]
  litradar tags [LABEL] [--json]

Identifiers: DOI (bare, doi:, https://doi.org/…), PMID (digits or PMID:…), PMCID (PMC…), arXiv id
(2510.12345, arXiv:…, arxiv.org/abs/…), bioRxiv / medRxiv DOI or URL, Europe PMC / PubMed / PMC URL,
or any publisher / Crossref URL that carries a DOI. See docs/add.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import ingest
from ..config import Settings
from ..pipeline import Pipeline
from ..store import Store


def cmd_add(args) -> int:
    ids = list(args.ids or [])
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"litradar add: file not found: {path}", file=sys.stderr)
            return 2
        ids += ingest.read_id_file(path)
    if not ids:
        print("litradar add: give at least one identifier or --file", file=sys.stderr)
        return 2
    settings = Settings.from_env(config_dir=args.config_dir)
    pipe = Pipeline(settings)
    results = ingest.add_papers(pipe, ids, fulltext=args.fulltext, tags=args.tag or [])
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=1, default=str))
    else:
        print("\n\n".join(ingest.format_result(r) for r in results))
        failed = sum(1 for r in results if r.error)
        new = sum(1 for r in results if not r.error and r.new)
        refreshed = sum(1 for r in results if not r.error and not r.new)
        print(f"\n{len(results)} identifier(s): {new} new, {refreshed} refreshed, {failed} failed")
    return 1 if any(r.error for r in results) else 0


def cmd_tags(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    store = Store(settings.db_path)
    if args.tag:
        rows = ingest.tagged_papers(store, args.tag)
        if args.json:
            print(json.dumps(rows, indent=1, default=str))
            return 0
        if not rows:
            print(f"no papers tagged {args.tag!r}")
            return 0
        print(f"{len(rows)} paper(s) tagged {args.tag!r}")
        for r in rows:
            print(f"- {r['pub_date'] or 'n.d.'}  {r['title']}\n    {r['journal'] or r['kind']}  {r['url']}  [{', '.join(r['tags'])}]")
        return 0
    counts = ingest.tag_counts(store)
    if args.json:
        print(json.dumps(counts, indent=1))
        return 0
    if not counts:
        print("no tags yet (add papers with `litradar add <id> --tag LABEL`)")
        return 0
    for tag, n in counts.items():
        print(f"{n:5d}  {tag}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("add", help="add papers to the corpus by DOI, PMID, PMCID, arXiv id or URL")
    p.add_argument("ids", nargs="*", metavar="ID", help="DOI, PMID, PMCID, arXiv id, or a URL carrying one")
    p.add_argument("--file", help="text file with one identifier per line ('#' starts a comment)")
    p.add_argument("--fulltext", action=argparse.BooleanOptionalAction, default=True,
                   help="fetch open-access full text where the licence allows (default on; --no-fulltext to skip)")
    p.add_argument("--tag", action="append", metavar="LABEL", help="store a label in extra_json.tags (repeatable); list with `litradar tags`")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("tags", help="list tags given with `litradar add --tag`, or the papers carrying one")
    p.add_argument("tag", nargs="?", help="show the papers carrying this tag")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tags)
