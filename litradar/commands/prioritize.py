"""`litradar prioritize`: weight coverage toward what matters, stored in radar.yaml under `priorities:`.

  litradar prioritize subgroup spatial --weight 1.5
  litradar prioritize company illumina --weight 2        (id, name, alias or ticker from companies.yaml)
  litradar prioritize instrument-category sequencer.long_read --weight 1.3
  litradar prioritize journal "Nature Methods" --weight 1.25
  litradar prioritize source europepmc --weight 0.8
  litradar prioritize company illumina --clear
  litradar prioritize list [--json]

The weights are multipliers (1.0 = neutral) read by litradar.track.priority_multiplier(), which the
pipeline applies to the heuristic score, the full-text fetch order and the LLM shortlist order.
"""
from __future__ import annotations

import json
import sys
from typing import Tuple

from .. import track as T
from ..config import Settings

KNOWN_SOURCES = ("biorxiv", "medrxiv", "europepmc", "openalex", "crossref", "arxiv", "rss", "sra", "ena", "sec")


def _resolve(settings: Settings, kind: str, target: str) -> Tuple[str, str]:
    """(canonical key to store, note for the terminal) or raise TrackError."""
    kind = T.normalize_kind(kind)
    target = (target or "").strip()
    if kind == "subgroup":
        groups = (settings.yaml("subgroups") or {}).get("subgroups") or []
        for g in groups:
            if str(g.get("id", "")).lower() == target.lower() or str(g.get("name", "")).lower() == target.lower():
                return str(g["id"]), f"subgroup {g['id']} ({g.get('name')})"
        raise T.TrackError(f"subgroup {target!r} not found (have: {', '.join(str(g.get('id')) for g in groups)})")
    if kind == "company":
        e = T.find_company(settings, target)
        if not e:
            raise T.TrackError(f"{target!r} is not in companies.yaml (id, name, alias or ticker); add it with `litradar track company` first")
        return str(e["id"]), f"company {e['id']} ({e.get('name')}, {e.get('ticker')})"
    if kind == "instrument_category":
        base = target.split(".", 1)[0]
        if base not in T.INSTRUMENT_CATEGORIES:
            raise T.TrackError(f"unknown instrument category {target!r}; the first segment must be one of {', '.join(T.INSTRUMENT_CATEGORIES)}")
        return target, f"instrument category {target} (prefix match: '{base}' covers every {base}.* category)"
    if kind == "journal":
        journals = (settings.yaml("journals") or {}).get("journals") or []
        for j in journals:
            if str(j.get("name", "")).lower() == target.lower():
                return str(j["name"]), f"journal {j['name']} (tier {j.get('tier', '?')})"
        raise T.TrackError(f"journal {target!r} is not in journals.yaml (names must match exactly, case-insensitive)")
    if kind == "source":
        base = target.split(".", 1)[0].split(":", 1)[0]
        note = f"source {target}"
        if base not in KNOWN_SOURCES:
            note += f" (warning: not one of {', '.join(KNOWN_SOURCES)}; it will only match papers whose source field is exactly this)"
        return target, note
    raise T.TrackError(f"unsupported kind {kind}")


def cmd_prioritize(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    if args.kind == "list":
        pri = T.load_priorities(settings.radar)
        if args.json:
            print(json.dumps(pri, indent=1))
            return 0
        if not pri:
            print("no priorities configured (every multiplier is 1.0)")
            return 0
        for kind in T.PRIORITY_KINDS:
            table = pri.get(kind) or {}
            for key, w in sorted(table.items(), key=lambda kv: -kv[1]):
                print(f"{kind:20} {key:40} x{w:g}")
        return 0
    if not args.target:
        print("error: a target is required (or use `prioritize list`)", file=sys.stderr)
        return 1
    if args.clear == (args.weight is not None):
        print("error: pass exactly one of --weight W or --clear", file=sys.stderr)
        return 1
    try:
        key, note = _resolve(settings, args.kind, args.target)
        kind, key, bak = T.set_priority(settings, args.kind, key, None if args.clear else float(args.weight))
    except T.TrackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.clear:
        print(f"cleared priority for {note}")
    else:
        print(f"priorities.{kind}.{key} = {float(args.weight):g}  ({note})")
    if bak:
        print(f"  backup: {bak}")
    print("takes effect on the next `litradar run` or `litradar rematch` (scores are recomputed there)")
    return 0


def register(sub) -> None:
    p = sub.add_parser("prioritize", help="weight a subgroup, company, instrument category, journal or source in ranking and fetch order")
    p.add_argument("kind", choices=["subgroup", "company", "instrument-category", "instrument_category", "journal", "source", "list"])
    p.add_argument("target", nargs="?", help="id or name (company: id, name, alias or ticker)")
    p.add_argument("--weight", type=float, help=f"multiplier between {T.PRIORITY_MIN:g} and {T.PRIORITY_MAX:g}; 1.0 is neutral")
    p.add_argument("--clear", action="store_true", help="remove the priority (back to 1.0)")
    p.add_argument("--json", action="store_true", help="with `list`: machine-readable output")
    p.set_defaults(func=cmd_prioritize)
