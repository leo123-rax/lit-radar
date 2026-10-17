"""Render the weekly digest as Markdown (plus a JSON summary) from the store."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List

from ..rank.heuristics import is_platform
from ..store import Store


def _link(title: str, url: str) -> str:
    title = (title or "(untitled)").replace("|", "/").replace("\n", " ").strip()
    return f"[{title}]({url})" if url else title


def _row(cells: List[Any]) -> str:
    return "| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in cells) + " |"


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_nothing in this window_\n"
    out = [_row(headers), "|" + "|".join("---" for _ in headers) + "|"]
    out += [_row(r) for r in rows]
    return "\n".join(out) + "\n"


def render(store: Store, since: str, until: str, cfg: Dict[str, Any], run_report: Dict[str, Any] = None) -> Dict[str, Any]:
    dcfg = cfg.get("digest") or {}
    top_n = int(dcfg.get("top_n", 25))
    title = dcfg.get("title", "Science literature radar")
    top = store.top_papers(since, until, limit=top_n)
    instruments = store.instrument_counts(since, until, limit=int(dcfg.get("instruments_table_n", 40)))
    archive = store.archive_instrument_counts(since, until)
    strategies = store.archive_strategy_counts(since, until)
    companies = store.company_counts(since, until, limit=int(dcfg.get("companies_table_n", 40)))
    new_companies = [c for c in store.new_entities(since, "company")]
    new_instruments = [c for c in store.new_entities(since, "instrument")]
    tracked = store.tracked_hits_in_window(since, until)
    news = store.news_in_window(since, limit=int(dcfg.get("news_n", 40)) * 4)
    counts = store.counts()
    in_window = store.papers_in_window(since, until)
    has_history = store.has_history_before(since)
    n_inst = store.distinct_entities_in_window(since, until, "instrument")
    n_comp = store.distinct_entities_in_window(since, until, "company")
    platforms = [r for r in instruments if is_platform(r.get("category") or "")]
    infra = [r for r in instruments if not is_platform(r.get("category") or "")]

    md: List[str] = []
    md.append(f"# {title}: {since} to {until}\n")
    md.append(
        f"{len(in_window)} new papers and preprints in the window; {sum(1 for p in in_window if p.get('fulltext_available'))} with parsed full text; "
        f"{n_inst} instrument entities and {n_comp} companies detected; {sum(r['experiments'] for r in archive)} new sequencing experiments in public archives.\n"
    )

    # ---- top innovations
    md.append(f"## Top {len(top)} innovations\n")
    if not top:
        md.append("_no scored papers in this window_\n")
    for i, p in enumerate(top, 1):
        llm = json.loads(p["llm_json"]) if p.get("llm_json") else None
        inst = sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "instrument"})
        comp = sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "company" and not m["entity_id"].startswith("unlisted:")})
        venue = f"{p.get('journal') or p.get('source')}" + (" (preprint)" if p.get("kind") == "preprint" else "")
        md.append(f"**{i}. {_link(p['title'], p['url'])}**  \n_{venue}, {p.get('pub_date') or 'n.d.'} · score {p.get('final'):.1f}_  ")
        if llm:
            md.append(f"\n{llm.get('one_line', '')}  \n*Why it matters:* {llm.get('why_it_matters', '')}  ")
            flags = f"novelty {llm.get('novelty')}/5 · commercial {llm.get('commercial_implication')}/5 · instrument role: {llm.get('instrument_role')} · readiness: {llm.get('clinical_readiness')} · hype risk {llm.get('hype_risk')}/5"
            md.append(f"\n<sub>{flags}</sub>  ")
        else:
            abstract = (p.get("abstract") or "").strip()
            if abstract:
                md.append(f"\n{abstract[:420].rstrip()}{'…' if len(abstract) > 420 else ''}  ")
        tail = []
        if inst:
            tail.append("Instruments: " + ", ".join(inst))
        if comp:
            tail.append("Companies: " + ", ".join(comp))
        tail.append("Signals: " + (p.get("reasons") or ""))
        md.append("\n<sub>" + " · ".join(tail) + "</sub>\n")

    # ---- instruments in the literature
    md.append("## Platforms named in this week's papers\n")
    md.append("Sequencers, single-cell and spatial systems, proteomics, cryo-EM, arrays, synthesis and cell-engineering platforms, counted from titles, abstracts and parsed Methods sections (open-access full text and preprints). Papers without accessible full text can only contribute abstract mentions.\n")
    md.append(_table(
        ["Platform", "Vendor", "Ticker", "Papers", "Example"],
        [[r["entity_name"], r["vendor"], r["ticker"], r["papers"], _link((r["example_title"] or "")[:70], r["example_url"])] for r in platforms],
    ))
    md.append("### Lab infrastructure and consumables named\n")
    md.append("Library prep, QC, PCR, automation, imaging, cytometry and bioprocess products. Useful for supplier share, not for innovation.\n")
    md.append(_table(
        ["Product line", "Vendor", "Ticker", "Papers"],
        [[r["entity_name"], r["vendor"], r["ticker"], r["papers"]] for r in infra[:25]],
    ))
    if new_instruments and has_history:
        md.append("**First appearance in the corpus this window:** " + ", ".join(f"{r['entity_name']} ({r['papers']})" for r in new_instruments[:20]) + "\n")

    # ---- archives
    md.append("## Instruments behind newly public sequencing data (NCBI SRA + ENA)\n")
    md.append("Each row is a sequencing *experiment* released in the window, with the instrument model the submitter declared (SRA covers submissions worldwide; ENA adds European and Japanese records SRA has not mirrored yet). This is a census of what machines are producing data right now, independent of what gets written up. Deposits lag sequencing by months to years, and clinical or commercial sequencing is never deposited.\n")
    vend: Dict[str, int] = defaultdict(int)
    for r in archive:
        vend[r.get("vendor") or r.get("platform") or "unknown"] += r["experiments"]
    total_exp = sum(vend.values()) or 1
    md.append(_table(["Vendor / platform", "Experiments", "Share"], [[k, v, f"{100.0 * v / total_exp:.1f}%"] for k, v in sorted(vend.items(), key=lambda kv: -kv[1])]))
    md.append(_table(
        ["Instrument model", "Vendor", "Ticker", "Experiments", "Studies"],
        [[r["instrument_model"], r.get("vendor") or "", r.get("ticker") or "", r["experiments"], r["studies"]] for r in archive[: int(dcfg.get("archive_table_n", 30))]],
    ))
    if strategies:
        md.append("Library strategies: " + ", ".join(f"{s['library_strategy'] or 'n/a'} ({s['experiments']})" for s in strategies) + "\n")

    # ---- companies
    md.append("## Companies in the literature\n")
    md.append("How each company appears: **author** = an author's affiliation; **funding / COI** = named in funding, competing-interest or acknowledgement statements; **subject** = named in the title or abstract (the company or its product is what the paper is about); **supplier** = its product is named in the Methods.\n")
    listed = [c for c in companies if not c["entity_id"].startswith("unlisted:")]
    unlisted = [c for c in companies if c["entity_id"].startswith("unlisted:")]
    md.append(_table(
        ["Company", "Ticker", "Papers", "Author", "Funding / COI", "Subject", "Supplier", "Example"],
        [[c["entity_name"], c["ticker"], c["papers"], c["as_author"], c["as_funder_or_coi"], c["as_subject"], c.get("as_supplier", 0), _link((c["example_title"] or "")[:60], c["example_url"])] for c in listed],
    ))
    if unlisted:
        md.append("**Company-type institutions not in the gazetteer (candidates to add):** " + ", ".join(f"{c['entity_name']} ({c['papers']})" for c in unlisted[:30]) + "\n")
    if new_companies and has_history:
        md.append("**First appearance in the corpus this window:** " + ", ".join(f"{r['entity_name']}" for r in new_companies[:30]) + "\n")

    # ---- tracked researchers
    md.append("## Tracked researchers\n")
    if not tracked:
        md.append("_no new papers from tracked authors in this window (add OpenAlex author ids in config/subgroups.yaml to make matching exact)_\n")
    else:
        by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in tracked:
            by_group[t["subgroup"]].append(t)
        for grp, rows in by_group.items():
            md.append(f"**{grp}**\n")
            for t in rows:
                how = "" if t.get("matched_by") == "openalex_id" else " <sub>(name match; add the OpenAlex id to make it exact)</sub>"
                md.append(f"- {t['author_name']}: {_link(t['title'], t['url'])} — {t.get('journal') or ''} {t.get('pub_date') or ''}{' (preprint)' if t.get('kind') == 'preprint' else ''}{how}")
            md.append("")

    # ---- news
    md.append("## Trade press & company news\n")
    seen_titles = set()
    by_company: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    general: List[Dict[str, Any]] = []
    for n in news:
        key = (n.get("title") or "").lower()[:80]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        if n.get("companies"):
            for c in n["companies"][:2]:
                by_company[c].append(n)
        else:
            general.append(n)
    shown = 0
    for comp, items in sorted(by_company.items(), key=lambda kv: -len(kv[1])):
        md.append(f"**{comp}**")
        for n in items[:4]:
            md.append(f"- {_link(n['title'], n['url'])} <sub>{n.get('feed')} · {n.get('published') or ''}</sub>")
            shown += 1
        md.append("")
        if shown >= int(dcfg.get("news_n", 40)):
            break
    if general and shown < int(dcfg.get("news_n", 40)):
        md.append("**Other headlines**")
        for n in general[: max(5, int(dcfg.get("news_n", 40)) - shown)]:
            md.append(f"- {_link(n['title'], n['url'])} <sub>{n.get('feed')} · {n.get('published') or ''}</sub>")
        md.append("")

    # ---- coverage / methodology
    md.append("## Coverage and method\n")
    if run_report:
        c = run_report.get("counts", {})
        fetched = {k.split(".", 1)[1]: v for k, v in c.items() if k.startswith("fetched.")}
        md.append("Records pulled per source: " + ", ".join(f"{k} {v}" for k, v in sorted(fetched.items())) + ".  ")
        md.append(f"\nUnique papers after de-duplication: {c.get('papers.unique', 0)} ({c.get('papers.new', 0)} new to the database). Full text parsed: bioRxiv {c.get('fulltext.biorxiv', 0)} (failed {c.get('fulltext.biorxiv_failed', 0)}), Europe PMC {c.get('fulltext.europepmc', 0)} (failed {c.get('fulltext.europepmc_failed', 0)}). LLM-scored: {c.get('llm.scored', 0)}.  ")
        if run_report.get("errors"):
            md.append("\nSource errors this run: " + "; ".join(run_report["errors"][:10]) + ".  ")
    md.append(f"\nDatabase totals: {counts['papers']} papers, {counts['mentions']} entity mentions, {counts['archive_records']} archive records, {counts['news_items']} news items. "
              "Scores: a transparent heuristic (journal tier, instruments and companies named, watchlist terms, tracked authors, novelty cues) optionally blended with an LLM rubric. "
              "Only metadata, abstracts and open-licensed full text are stored; subscription content is linked, never copied.\n")

    summary = {
        "since": since, "until": until, "papers_in_window": len(in_window),
        "top": [{"id": p["id"], "title": p["title"], "url": p["url"], "journal": p.get("journal"), "score": p.get("final"),
                 "instruments": sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "instrument"}),
                 "companies": sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "company"})} for p in top],
        "instruments": instruments, "archive": archive, "companies": companies, "tracked": tracked,
        "counts": counts, "run": run_report,
    }
    return {"markdown": "\n".join(md), "summary": summary}
