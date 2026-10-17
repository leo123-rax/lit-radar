"""Render the weekly digest as Markdown (plus a JSON summary) from the store.

Everything that comes from outside (paper titles, abstracts, snippets, LLM free text, news headlines,
company and author names, archive strings, error messages) passes through `esc` before it reaches the
Markdown, so it cannot inject HTML or break a table. Links are emitted only for http(s) URLs.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..rank.heuristics import is_platform
from ..store import Store

# which `type` values from config/companies.yaml land where in the Companies section
MAIN_COMPANY_TYPES = ("tools", "diagnostics", "synthesis", "ai_bio")
PHARMA_TYPES = ("pharma", "cro")
INSTITUTE_TYPES = ("institute",)
AI_TOOL_TYPES = ("software", "ai_tool", "ai-tool", "ai tool", "llm")

# news feeds that are structured signals (own digest section), not trade press
SIGNAL_FEED_LABELS = {
    "nih-reporter": "NIH RePORTER awards",
    "clinicaltrials": "ClinicalTrials.gov registrations",
    "fda-510k": "FDA 510(k) clearances",
    "fda-denovo": "FDA De Novo grants",
    "fda-pma": "FDA PMA decisions",
}
SIGNAL_FEEDS = tuple(SIGNAL_FEED_LABELS)
SEQUENCING_ARCHIVES = ("sra", "ena", "geo")

_URL_RE = re.compile(r"^https?://[^\s<>\"'`]+$", re.I)
_WS_RE = re.compile(r"\s+")


def esc(value: Any) -> str:
    """Make untrusted text inert inside Markdown: no HTML, no table breaks, no accidental block syntax.

    Escapes &, <, >, | and [ ], collapses whitespace (so a value stays on one line / in one cell) and prefixes a
    leading #, -, +, * or > with a backslash so a value cannot open a heading, list or quote."""
    s = _WS_RE.sub(" ", str(value if value is not None else "")).strip()
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
    s = s.replace("[", "\\[").replace("]", "\\]").replace("](", "]\\(")
    if s[:1] in ("#", "-", "+", "*") or s[:4] == "&gt;":
        s = "\\" + s
    return s


def safe_url(url: Any) -> str:
    """The URL when it is http(s) and free of characters that would break a Markdown link, else ''."""
    u = str(url or "").strip()
    if not _URL_RE.match(u):
        return ""
    return u.replace("(", "%28").replace(")", "%29")


def _link(title: Any, url: Any, max_len: int = 0) -> str:
    text = esc(title) or "(untitled)"
    if max_len and len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    u = safe_url(url)
    return f"[{text}]({u})" if u else text


def _row(cells: Iterable[Any]) -> str:
    return "| " + " | ".join(esc(c) for c in cells) + " |"


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_nothing in this window_\n"
    out = [_row(headers), "|" + "|".join("---" for _ in headers) + "|"]
    out += [_row(r) for r in rows]
    return "\n".join(out) + "\n"


def _raw_row(cells: Iterable[str]) -> str:
    """A table row whose cells are already rendered Markdown (links); callers escape their own text."""
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _raw_table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return "_nothing in this window_\n"
    out = [_row(headers), "|" + "|".join("---" for _ in headers) + "|"]
    out += [_raw_row(r) for r in rows]
    return "\n".join(out) + "\n"


def _roles(c: Dict[str, Any]) -> str:
    parts = []
    for label, key in (("author", "as_author"), ("funding/COI", "as_funder_or_coi"), ("subject", "as_subject"), ("supplier", "as_supplier")):
        v = int(c.get(key) or 0)
        if v:
            parts.append(f"{label} {v}")
    return ", ".join(parts)


def _company_line(rows: List[Dict[str, Any]], limit: int = 30) -> str:
    return ", ".join(f"{esc(c['entity_name'])} ({int(c['papers'])}" + (f"; {_roles(c)}" if _roles(c) else "") + ")" for c in rows[:limit])


def _vendor_share_table(rows: List[Dict[str, Any]]) -> str:
    vend: Dict[str, int] = defaultdict(int)
    for r in rows:
        vend[r.get("vendor") or "unknown platform"] += int(r["experiments"])
    total = sum(vend.values()) or 1
    return _table(["Vendor / platform", "Experiments", "Share"], [[k, v, f"{100.0 * v / total:.1f}%"] for k, v in sorted(vend.items(), key=lambda kv: -kv[1])])


def _archive_table(rows: List[Dict[str, Any]], n: int, first_col: str = "Instrument model", unit: str = "Experiments", show_archive: bool = False) -> str:
    headers = [first_col, "Vendor", "Ticker", unit, "Studies"] + (["Archive"] if show_archive else [])
    body = []
    for r in rows[:n]:
        cells = [r["instrument_model"] or "(not declared)", r.get("vendor") or "", r.get("ticker") or "", r["experiments"], r["studies"]]
        if show_archive:
            cells.append(r.get("archive") or "")
        body.append(cells)
    return _table(headers, body)


def render(
    store: Store,
    since: str,
    until: str,
    cfg: Dict[str, Any],
    run_report: Optional[Dict[str, Any]] = None,
    company_types: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build the digest. `company_types` maps gazetteer company id -> type (tools, diagnostics, synthesis, ai_bio,
    pharma, cro, institute, software / ai_tool); the caller builds it from config/companies.yaml (see
    Pipeline.company_types). Without it every company lands in the main table."""
    company_types = {k: (v or "").lower() for k, v in (company_types or {}).items()}
    dcfg = cfg.get("digest") or {}
    top_n = int(dcfg.get("top_n", 25))
    news_n = int(dcfg.get("news_n", 40))
    signals_n = int(dcfg.get("signals_n", 15))
    title = str(dcfg.get("title", "Science literature radar"))
    top = store.top_papers(since, until, limit=top_n)
    conversions = store.preprint_conversions(since, until)
    instruments = store.instrument_counts(since, until, limit=int(dcfg.get("instruments_table_n", 40)))
    archive = store.archive_instrument_counts(since, until, archives=SEQUENCING_ARCHIVES)
    archive_proteomics = store.archive_instrument_counts(since, until, archives=("pride",))
    archive_singlecell = store.archive_instrument_counts(since, until, archives=("cellxgene",))
    strategies = store.archive_strategy_counts(since, until, archives=SEQUENCING_ARCHIVES)
    companies = store.company_counts(since, until, limit=int(dcfg.get("companies_table_n", 40)))
    new_companies = list(store.new_entities(since, "company"))
    new_instruments = list(store.new_entities(since, "instrument"))
    tracked = store.tracked_hits_in_window(since, until)
    news = store.news_in_window(since, limit=news_n * 4, exclude_feeds=SIGNAL_FEEDS)
    signals = store.news_in_window(since, limit=signals_n * 4 * len(SIGNAL_FEEDS), feeds=SIGNAL_FEEDS)
    counts = store.counts()
    in_window = store.papers_in_window(since, until)
    has_history = store.has_history_before(since)
    n_inst = store.distinct_entities_in_window(since, until, "instrument")
    n_comp = store.distinct_entities_in_window(since, until, "company")
    platforms = [r for r in instruments if is_platform(r.get("category") or "")]
    infra = [r for r in instruments if not is_platform(r.get("category") or "")]

    md: List[str] = []
    md.append(f"# {esc(title)}: {esc(since)} to {esc(until)}\n")
    md.append(
        f"{len(in_window)} new papers and preprints in the window; {sum(1 for p in in_window if p.get('fulltext_available'))} with parsed full text; "
        f"{n_inst} instrument entities and {n_comp} companies detected; {sum(int(r['experiments']) for r in archive)} new sequencing experiments in public archives"
        + (f"; {sum(int(r['experiments']) for r in archive_proteomics)} proteomics datasets" if archive_proteomics else "")
        + (f"; {sum(int(r['experiments']) for r in archive_singlecell)} single-cell / spatial dataset assays" if archive_singlecell else "")
        + ".\n"
    )

    # ---- top innovations
    md.append(f"## Top {len(top)} innovations\n")
    if not top:
        md.append("_no scored papers in this window_\n")
    for i, p in enumerate(top, 1):
        llm = None
        if p.get("llm_json"):
            try:
                llm = json.loads(p["llm_json"])
            except ValueError:
                llm = None
            if not isinstance(llm, dict):
                llm = None
        inst = sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "instrument"})
        comp = sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "company" and not m["entity_id"].startswith("unlisted:")})
        venue = esc(p.get("journal") or p.get("source") or "") + (" (preprint)" if p.get("kind") == "preprint" else "")
        for pre in p.get("preprints") or []:
            venue += f" (also as preprint {esc(pre.get('pub_date') or 'n.d.')})"
        score = p.get("final")
        score_s = f"{float(score):.1f}" if score is not None else "n/a"
        md.append(f"**{i}. {_link(p['title'], p['url'])}**  \n_{venue}, {esc(p.get('pub_date') or 'n.d.')} · score {score_s}_  ")
        if llm:
            md.append(f"\n{esc(llm.get('one_line', ''))}  \n*Why it matters:* {esc(llm.get('why_it_matters', ''))}  ")
            flags = (
                f"novelty {esc(llm.get('novelty'))}/5 · commercial {esc(llm.get('commercial_implication'))}/5 · "
                f"instrument role: {esc(llm.get('instrument_role'))} · readiness: {esc(llm.get('clinical_readiness'))} · hype risk {esc(llm.get('hype_risk'))}/5"
            )
            md.append(f"\n<sub>{flags}</sub>  ")
        else:
            abstract = (p.get("abstract") or "").strip()
            if abstract:
                md.append(f"\n{esc(abstract[:420].rstrip())}{'…' if len(abstract) > 420 else ''}  ")
        tail = []
        if inst:
            tail.append("Instruments: " + ", ".join(esc(x) for x in inst))
        if comp:
            tail.append("Companies: " + ", ".join(esc(x) for x in comp))
        tail.append("Signals: " + esc(p.get("reasons") or ""))
        md.append("\n<sub>" + " · ".join(tail) + "</sub>\n")
    if conversions:
        md.append("### Preprints that appeared as journal articles this window\n")
        for c in conversions[:15]:
            how = "linked by the publisher" if c.get("matched_by") == "published_doi" else "matched by title"
            md.append(f"- {_link(c['article_title'], c['article_url'])} — {esc(c.get('journal') or '')} {esc(c.get('article_date') or '')}; preprint {esc(c.get('preprint_date') or 'n.d.')} ({how})")
        md.append("")

    # ---- instruments in the literature
    md.append("## Platforms named in this week's papers\n")
    md.append("Sequencers, single-cell and spatial systems, proteomics, cryo-EM, arrays, synthesis and cell-engineering platforms, counted from titles, abstracts and parsed Methods sections (open-access full text and preprints). Papers without accessible full text can only contribute abstract mentions.\n")
    md.append(_raw_table(
        ["Platform", "Vendor", "Ticker", "Papers", "Example"],
        [[esc(r["entity_name"]), esc(r["vendor"]), esc(r["ticker"]), esc(r["papers"]), _link(r["example_title"] or "", r["example_url"], max_len=70)] for r in platforms],
    ))
    md.append("### Lab infrastructure and consumables named\n")
    md.append("Library prep, QC, PCR, automation, imaging, cytometry and bioprocess products. Useful for supplier share, not for innovation.\n")
    md.append(_table(
        ["Product line", "Vendor", "Ticker", "Papers"],
        [[r["entity_name"], r["vendor"], r["ticker"], r["papers"]] for r in infra[:25]],
    ))
    if new_instruments and has_history:
        md.append("**First appearance in the corpus this window:** " + ", ".join(f"{esc(r['entity_name'])} ({int(r['papers'])})" for r in new_instruments[:20]) + "\n")

    # ---- archives
    md.append("## Instruments behind newly public sequencing data (NCBI SRA, ENA, GEO)\n")
    md.append("Each row is a sequencing *experiment* (SRA / ENA) or *series x platform* (GEO) released in the window, with the instrument model the submitter declared (SRA covers submissions worldwide; ENA adds European and Japanese records SRA has not mirrored yet; GEO is the study-level view behind most published expression and single-cell datasets). This is a census of what machines are producing data right now, independent of what gets written up. Deposits lag sequencing by months to years, and clinical or commercial sequencing is never deposited. Models the gazetteer cannot map are grouped under their declared platform, e.g. 'Illumina (model unspecified)'.\n")
    md.append(_vendor_share_table(archive))
    md.append(_archive_table(archive, int(dcfg.get("archive_table_n", 30)), show_archive=True))
    if strategies:
        md.append("Library strategies: " + ", ".join(f"{esc(s['library_strategy'] or 'n/a')} ({int(s['experiments'])})" for s in strategies) + "\n")
    md.append("### Proteomics instruments behind new datasets (PRIDE)\n")
    md.append("One row per mass spectrometer declared on proteomics datasets made public in the window (one record per dataset x instrument).\n")
    md.append(_archive_table(archive_proteomics, int(dcfg.get("archive_table_n", 30)), unit="Datasets"))
    md.append("### Single-cell and spatial assays behind new datasets (CELLxGENE)\n")
    md.append("Assay chemistry declared on curated single-cell / spatial datasets made public in the window (one record per dataset x assay); the sequencer is not recorded there.\n")
    md.append(_archive_table(archive_singlecell, int(dcfg.get("archive_table_n", 30)), first_col="Assay", unit="Datasets"))

    # ---- companies
    md.append("## Companies in the literature\n")
    md.append("How each company appears: **author** = an author's affiliation; **funding / COI** = named in funding, competing-interest or acknowledgement statements; **subject** = named in the title or abstract (the company or its product is what the paper is about); **supplier** = its product is named in the Methods.\n")
    listed = [c for c in companies if not c["entity_id"].startswith("unlisted:")]
    unlisted = [c for c in companies if c["entity_id"].startswith("unlisted:")]

    def ctype(c: Dict[str, Any]) -> str:
        return company_types.get(c["entity_id"], "")

    pharma = [c for c in listed if ctype(c) in PHARMA_TYPES]
    institutes = [c for c in listed if ctype(c) in INSTITUTE_TYPES]
    ai_tools = [c for c in listed if ctype(c) in AI_TOOL_TYPES]
    main = [c for c in listed if ctype(c) in MAIN_COMPANY_TYPES or ctype(c) not in PHARMA_TYPES + INSTITUTE_TYPES + AI_TOOL_TYPES]
    md.append("### Tools, diagnostics, synthesis and AI-bio companies\n")
    md.append(_raw_table(
        ["Company", "Ticker", "Papers", "Author", "Funding / COI", "Subject", "Supplier", "Example"],
        [[esc(c["entity_name"]), esc(c["ticker"]), esc(c["papers"]), esc(c["as_author"]), esc(c["as_funder_or_coi"]), esc(c["as_subject"]), esc(c.get("as_supplier", 0)), _link(c["example_title"] or "", c["example_url"], max_len=60)] for c in main],
    ))
    md.append("### Pharma and CROs\n")
    md.append("Mostly funding and competing-interest disclosures; shown apart so they do not crowd the tools table.\n")
    md.append(_table(
        ["Company", "Ticker", "Papers", "Author", "Funding / COI", "Subject"],
        [[c["entity_name"], c["ticker"], c["papers"], c["as_author"], c["as_funder_or_coi"], c["as_subject"]] for c in pharma],
    ))
    if institutes:
        md.append("**Institutes:** " + _company_line(institutes) + "\n")
    if ai_tools:
        md.append("**AI tools and software used:** " + _company_line(ai_tools) + "\n")
    if unlisted:
        md.append("**Company-type institutions not in the gazetteer (candidates to add):** " + ", ".join(f"{esc(c['entity_name'])} ({int(c['papers'])})" for c in unlisted[:30]) + "\n")
    if new_companies and has_history:
        md.append("**First appearance in the corpus this window:** " + ", ".join(esc(r["entity_name"]) for r in new_companies[:30]) + "\n")

    # ---- tracked researchers
    md.append("## Tracked researchers\n")
    if not tracked:
        md.append("_no new papers from tracked authors in this window (add OpenAlex author ids in config/subgroups.yaml to make matching exact)_\n")
    else:
        by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in tracked:
            by_group[t["subgroup"]].append(t)
        for grp, rows in by_group.items():
            md.append(f"**{esc(grp)}**\n")
            for t in rows:
                how = "" if t.get("matched_by") == "openalex_id" else " <sub>(name match; add the OpenAlex id to make it exact)</sub>"
                md.append(f"- {esc(t['author_name'])}: {_link(t['title'], t['url'])} — {esc(t.get('journal') or '')} {esc(t.get('pub_date') or '')}{' (preprint)' if t.get('kind') == 'preprint' else ''}{how}")
            md.append("")

    # ---- grants, trials and clearances
    md.append("## Grants, trials and clearances\n")
    md.append("Structured public signals, grouped by source: NIH RePORTER awards (shared-instrumentation grants and awards naming a platform or watched company), ClinicalTrials.gov studies first posted in the window whose sponsor or intervention matches the watch lists, and FDA device decisions whose applicant is a watched company.\n")
    by_feed: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in signals:
        by_feed[s.get("feed") or ""].append(s)
    if not any(by_feed.get(f) for f in SIGNAL_FEEDS):
        md.append("_no new grants, registrations or device decisions in this window (sources nih_reporter / clinicaltrials / openfda in config/radar.yaml)_\n")
    for feed in SIGNAL_FEEDS:
        items = by_feed.get(feed) or []
        if not items:
            continue
        md.append(f"**{SIGNAL_FEED_LABELS[feed]}** ({len(items)})\n")
        for s in items[:signals_n]:
            comps = ", ".join(esc(c) for c in (s.get("companies") or [])[:3])
            meta = " · ".join(x for x in (comps, esc(s.get("published") or "")) if x)
            md.append(f"- {_link(s['title'], s['url'])}" + (f" <sub>{meta}</sub>" if meta else ""))
        if len(items) > signals_n:
            md.append(f"- … and {len(items) - signals_n} more")
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
    for comp_id, items in sorted(by_company.items(), key=lambda kv: -len(kv[1])):
        md.append(f"**{esc(comp_id)}**")
        for n in items[:4]:
            md.append(f"- {_link(n['title'], n['url'])} <sub>{esc(n.get('feed'))} · {esc(n.get('published') or '')}</sub>")
            shown += 1
        md.append("")
        if shown >= news_n:
            break
    if general and shown < news_n:
        md.append("**Other headlines**")
        for n in general[: max(5, news_n - shown)]:
            md.append(f"- {_link(n['title'], n['url'])} <sub>{esc(n.get('feed'))} · {esc(n.get('published') or '')}</sub>")
        md.append("")

    # ---- coverage / methodology
    md.append("## Coverage and method\n")
    if not run_report:
        row = store.conn.execute("SELECT counts_json FROM runs WHERE since=? AND until=? ORDER BY id DESC LIMIT 1", (since, until)).fetchone()
        if row and row[0]:
            try:
                run_report = json.loads(row[0])
            except ValueError:
                run_report = None
    if run_report:
        c = run_report.get("counts", {}) or {}
        fetched = {k.split(".", 1)[1]: v for k, v in c.items() if k.startswith("fetched.")}
        md.append("Records pulled per source: " + ", ".join(f"{esc(k)} {esc(v)}" for k, v in sorted(fetched.items())) + ".  ")
        md.append(
            f"\nUnique papers after de-duplication: {esc(c.get('papers.unique', 0))} ({esc(c.get('papers.new', 0))} new to the database, {esc(c.get('papers.reingested', 0))} already known and merged). "
            f"Full text parsed: bioRxiv {esc(c.get('fulltext.biorxiv', 0))} (failed {esc(c.get('fulltext.biorxiv_failed', 0))}), Europe PMC {esc(c.get('fulltext.europepmc', 0))} (failed {esc(c.get('fulltext.europepmc_failed', 0))}); "
            f"stored in full {esc(c.get('fulltext.policy_full', 0))}, facts only (licence does not allow redistribution) {esc(c.get('fulltext.policy_facts_only', 0))}. LLM-scored: {esc(c.get('llm.scored', 0))}.  "
        )
        arch = {k[len("archive."):-len("_new")]: v for k, v in c.items() if k.startswith("archive.") and k.endswith("_new")}
        if arch:
            md.append("\nNew archive records: " + ", ".join(f"{esc(k.upper())} {esc(v)}" for k, v in sorted(arch.items())) + ".  ")
        sig = {k.split(".", 1)[1]: v for k, v in c.items() if k.startswith("signals.")}
        if sig:
            md.append("\nSignals stored: " + ", ".join(f"{esc(k)} {esc(v)}" for k, v in sorted(sig.items())) + ".  ")
        if c.get("semantic_scholar.requested"):
            md.append(f"\nCitation enrichment: {esc(c.get('semantic_scholar.requested', 0))} DOIs looked up, {esc(c.get('semantic_scholar.matched', 0))} matched, {esc(c.get('semantic_scholar.updated', 0))} counts raised.  ")
        if c.get("tracked_hits.name_capped"):
            md.append(f"\nTracked-author name matches dropped by the per-name cap: {esc(c.get('tracked_hits.name_capped'))} (common names; add OpenAlex ids).  ")
        if run_report.get("errors"):
            md.append("\nSource errors this run: " + "; ".join(esc(e) for e in run_report["errors"][:10]) + ".  ")
    md.append(f"\nDatabase totals: {counts['papers']} papers, {counts['mentions']} entity mentions, {counts['archive_records']} archive records, {counts['news_items']} news items. "
              "Scores: a transparent heuristic (journal tier, instruments and companies named, watchlist terms, tracked authors, novelty cues) optionally blended with an LLM rubric. "
              "Only metadata, abstracts and open-licensed full text are stored; for other full text only the derived facts (entity mentions with short snippets) are kept, and subscription content is linked, never copied.\n")

    summary = {
        "since": since, "until": until, "papers_in_window": len(in_window),
        "top": [{"id": p["id"], "title": p["title"], "url": p["url"], "journal": p.get("journal"), "score": p.get("final"),
                 "preprints": p.get("preprints") or [],
                 "instruments": sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "instrument"}),
                 "companies": sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "company"})} for p in top],
        "preprint_conversions": conversions,
        "instruments": instruments, "archive": archive, "archive_proteomics": archive_proteomics, "archive_singlecell": archive_singlecell,
        "companies": companies, "company_groups": {"main": [c["entity_id"] for c in main], "pharma": [c["entity_id"] for c in pharma],
                                                   "institutes": [c["entity_id"] for c in institutes], "ai_tools": [c["entity_id"] for c in ai_tools]},
        "tracked": tracked, "signals": signals,
        "counts": counts, "run": run_report,
    }
    return {"markdown": "\n".join(md), "summary": summary}


def split_companies_by_type(companies: Sequence[Dict[str, Any]], company_types: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
    """Helper mirroring render()'s grouping, for callers that want the groups without the Markdown."""
    types = {k: (v or "").lower() for k, v in company_types.items()}
    out: Dict[str, List[Dict[str, Any]]] = {"main": [], "pharma": [], "institutes": [], "ai_tools": []}
    for c in companies:
        t = types.get(c["entity_id"], "")
        if t in PHARMA_TYPES:
            out["pharma"].append(c)
        elif t in INSTITUTE_TYPES:
            out["institutes"].append(c)
        elif t in AI_TOOL_TYPES:
            out["ai_tools"].append(c)
        else:
            out["main"].append(c)
    return out
