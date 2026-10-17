"""`litradar concepts`: emerging technical concepts (weekly phrase counts, bursts, new terms).

  litradar concepts [--weeks 26] [--top 30] [--until YYYY-MM-DD] [--llm] [--json] [--out file.md] [--force]

Counts are stored incrementally in concept_counts / concept_papers (see litradar/concepts.py); the
optional `--llm` pass asks Claude for a one-line definition, new-vs-renamed verdict, affected vendor
categories and a 1-5 commercial-relevance score for the top candidates and stores them in `concepts`.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import concepts
from ..config import Settings
from ..store import Store


def cmd_concepts(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    until = args.until or datetime.now(timezone.utc).date().isoformat()
    store = Store(settings.db_path)
    excl = concepts.load_exclusions(settings.config_dir)
    stats = concepts.ensure_counts(
        store, until, weeks_total=int(args.weeks) + concepts.CURRENT_WEEKS, excl=excl, force=bool(args.force)
    )
    report = concepts.burst_report(
        store, until, baseline_weeks=int(args.weeks), current_weeks=concepts.CURRENT_WEEKS,
        min_papers=int(args.min_papers), top=int(args.top), excl=excl,
    )
    report["counting"] = {"weeks_recomputed": stats.get("weeks", []), "papers_scanned": stats.get("papers", 0)}

    if args.llm:
        cands = report["risers"][: int(args.llm_top)]
        seen = {c["term"] for c in cands}
        for d in report["new_terms"]:
            if len(cands) >= int(args.llm_top):
                break
            if d["term"] not in seen:
                cands.append(d)
                seen.add(d["term"])
        llm_cfg = (settings.radar.get("ranking") or {}).get("llm") or {}
        use_batch = bool(llm_cfg.get("use_batch", True)) and len(cands) > concepts.LLM_CHUNK
        est = concepts.estimate_cost_usd(len(cands), settings.llm_model, use_batch)
        print(f"LLM concept pass: {len(cands)} candidates, model {settings.llm_model}, {'batch' if use_batch else 'sync'}; estimated cost ≈ ${est:.3f}", file=sys.stderr)
        if not cands:
            print("nothing to define", file=sys.stderr)
        else:
            try:
                defs = concepts.define_concepts(cands, model=settings.llm_model, api_key=settings.anthropic_api_key, use_batch=use_batch)
            except RuntimeError as exc:
                print(f"LLM pass skipped: {exc}", file=sys.stderr)
                defs = {}
            n = concepts.save_definitions(store, defs, {c["term"]: c.get("display") or c["term"] for c in cands}, settings.llm_model)
            refused = sum(1 for c in cands if defs.get(c["term"]) is None)
            print(f"stored {n} definitions ({refused} unscored/refused)", file=sys.stderr)
            fresh = concepts.load_definitions(store, [c["term"] for c in cands])
            for d in report["risers"] + report["new_terms"]:
                if d["term"] in fresh:
                    d["definition"] = fresh[d["term"]]

    if args.json:
        print(json.dumps(report, indent=1, default=str, ensure_ascii=False))
        return 0
    md = concepts.render_report_markdown(report, top=int(args.top), heading=f"# Emerging concepts: window ending {until}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"report written to {out} ({report['candidates']} candidates, {len(report['risers'])} risers, {len(report['new_terms'])} new terms)")
    else:
        print(md)
    return 0


def register(sub) -> None:
    p = sub.add_parser("concepts", help="emerging technical concepts: weekly phrase counts, bursts and new terms")
    p.add_argument("--weeks", type=int, default=concepts.BASELINE_WEEKS, help="trailing baseline length in weeks (default 26); the current window is always the last 4 weeks")
    p.add_argument("--top", type=int, default=30, help="how many risers / new terms to report (default 30)")
    p.add_argument("--until", help="end date YYYY-MM-DD (default today)")
    p.add_argument("--min-papers", dest="min_papers", type=int, default=concepts.MIN_PAPERS, help="papers in the current window a term needs to be scored (default 5)")
    p.add_argument("--llm", action="store_true", help="define and classify the top candidates with Claude (needs ANTHROPIC_API_KEY and the anthropic package)")
    p.add_argument("--llm-top", dest="llm_top", type=int, default=40, help="how many candidates to send to the model (default 40)")
    p.add_argument("--json", action="store_true", help="print the full report as JSON instead of Markdown")
    p.add_argument("--out", help="write the Markdown report to this file instead of stdout")
    p.add_argument("--force", action="store_true", help="recount every week in the window (default: only missing weeks and the latest two)")
    p.set_defaults(func=cmd_concepts)
