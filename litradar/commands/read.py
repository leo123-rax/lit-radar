"""`litradar read <doi|url> ...`: read individual subscription articles through the signed-in browser profile."""
from __future__ import annotations

import json
from pathlib import Path

from .. import subscription
from ..config import Settings
from ..pipeline import Pipeline


def cmd_read(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    pipe = Pipeline(settings)
    rc = 0
    out_lines = []
    for target in args.targets:
        r = subscription.read_one(pipe, settings, target, keep_text=args.keep_text, store=not args.no_store)
        if args.json:
            out_lines.append(json.dumps(r.__dict__, default=str))
        else:
            out_lines.append(subscription.format_card(r, full=args.full))
            out_lines.append("")
        if r.error:
            rc = 3 if "cap" in r.error else (rc or 1)
    text = "\n".join(out_lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written to {args.out}")
    else:
        print(text)
    pipe.store.close()
    return rc


def register(sub) -> None:
    p = sub.add_parser("read", help="read one or more paywalled articles (nature.com, science.org) through your signed-in browser profile; paced and capped")
    p.add_argument("targets", nargs="+", help="DOIs or article URLs")
    p.add_argument("--keep-text", action="store_true", help="also store the sections locally as your personal copy (never redistribute or commit)")
    p.add_argument("--no-store", action="store_true", help="just show the reading card; store nothing")
    p.add_argument("--full", action="store_true", help="print the whole Methods section instead of the first 4,000 characters")
    p.add_argument("--out", help="write the reading cards to a Markdown file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_read)
