"""Render the weekly digest as Markdown (plus a JSON summary) from the store.

Everything that comes from outside (paper titles, abstracts, snippets, LLM free text, news headlines,
company and author names, archive strings, error messages) passes through `esc` before it reaches the
Markdown, so it cannot inject HTML or break a table. Links are emitted only for http(s) URLs.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..concepts import render_concepts_markdown
from ..discovery import render_candidates_markdown
from ..rank.heuristics import company_role
from ..rank.rerank import rerank_window
from ..store import Store, _json_dict
from ..trends import compute_trends, render_trends_markdown


def _trends_section(store: Store, until: str, dcfg: Dict[str, Any]) -> str:
    trends = compute_trends(store, until, weeks=int(dcfg.get("trend_weeks", 13)))
    return render_trends_markdown(trends, top_n=int(dcfg.get("trend_top_n", 10)), heading_level=2)

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
SEQUENCING_ARCHIVE_LABELS = {"sra": "NCBI SRA", "ena": "ENA", "geo": "GEO"}
# how a tracked-author hit was made -> the caveat shown next to it
TRACKED_HINTS = {
    "openalex_id": "",
    "name": " <sub>(name match; add the OpenAlex id to make it exact)</sub>",
    "name_no_ids": " <sub>(name match; the record carried no OpenAlex author ids, so the configured id could not be checked)</sub>",
}
SUMMARY_CHARS = 420  # abstract / fallback summary length in the top list
_COI_SECTIONS = ("coi", "funding", "ack")

_URL_RE = re.compile(r"^https?://[^\s<>\"'`]+$", re.I)
_WS_RE = re.compile(r"\s+")
_INTRO_HEADING_RE = re.compile(r"^(introduction|background|main|main text|summary)\b[\s:.\-]*", re.I)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def esc(value: Any) -> str:
    """Make untrusted text inert inside Markdown: no HTML, no table breaks, no accidental block syntax.

    Escapes <, >, | and [ ], collapses whitespace (so a value stays on one line / in one cell) and prefixes a
    leading #, -, +, * or > with a backslash so a value cannot open a heading, list or quote. A bare & is left
    alone: it is harmless in Markdown and `&amp;` would leak into plain-text readers (e-mail, chat)."""
    s = _WS_RE.sub(" ", str(value if value is not None else "")).strip()
    s = s.replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
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


def fallback_summary(store: Store, paper: Dict[str, Any], max_chars: int = SUMMARY_CHARS) -> Tuple[str, str]:
    """A summary for a top-list entry whose record has no abstract: the opening of the stored full-text body
    (its introduction), else the first sentence of the stored Methods. Returns (text, label); ('', '') when the
    store holds no text for the paper (facts-only policy, metadata-only record)."""
    body = store.body_for(paper["id"], rowid=paper.get("_rowid"))
    if body:
        text = _INTRO_HEADING_RE.sub("", _WS_RE.sub(" ", body).strip())
        if text:
            return text[:max_chars].rstrip() + ("…" if len(text) > max_chars else ""), "(from full text; no abstract on record)"
    methods = (_json_dict(paper.get("sections_json")).get("methods") or "").strip()
    if methods:
        first = _SENTENCE_END_RE.split(_WS_RE.sub(" ", methods), maxsplit=1)[0]
        return first[:max_chars].rstrip() + ("…" if len(first) > max_chars else ""), "(first Methods sentence; no abstract on record)"
    return "", ""


def _ranking_method_line(info: Dict[str, Any]) -> str:
    """One italic line under the top-list heading saying how the list was ranked and what the diversity caps did."""
    ctx = info.get("context") or {}
    caps = info.get("caps") or {}
    notes: List[str] = []
    if ctx.get("news_items"):
        notes.append(f"{int(ctx['news_items'])} news items checked for co-mentions")
    if not ctx.get("concept_history_ok"):
        notes.append(f"first-in-corpus concepts not scored ({int(ctx.get('concept_history_weeks') or 0)} week(s) of concept history on record, 4 needed)")
    if info.get("clustered"):
        n = int(info["clustered"])
        notes.append(f"{n} near-duplicate{'s' if n != 1 else ''} (same group, same venue, same day) folded into the better-scoring entry as '+N related'")
    if caps.get("max_per_kind") or caps.get("max_per_subgroup"):
        moved = [d for d in (info.get("deferred") or []) if not d.get("readmitted")]
        kind_cap = caps.get("max_per_kind") or "no"
        sub_cap = caps.get("max_per_subgroup") or "no"
        text = f"soft diversity caps: {kind_cap} per kind (preprint / article), {sub_cap} per subgroup"
        text += f"; {len(moved)} higher-scoring entr{'y' if len(moved) == 1 else 'ies'} deferred below the cut" if moved else "; nothing deferred"
        notes.append(text)
    head = (
        f"Ranked by the v2 heuristic over {int(info.get('candidates') or 0)} papers in the window: venue prior, the first platform "
        "and company named (further ones are listed, not scored), platform in title/abstract, watchlist terms, tracked authors, "
        "novelty and new-platform cues, early citations, preprint-to-journal conversion, news co-mention, first-in-corpus concepts. "
        "Every component and its points appear under each entry as Signals; the subgroup tag follows the score."
    )
    return "_" + head + ((" " + "; ".join(notes) + ".") if notes else "") + "_\n"


def _company_tail(mentions: Sequence[Dict[str, Any]], company_types: Dict[str, str]) -> List[str]:
    """The per-paper 'Companies / Institutes / AI tools' fragments, with a role tag for entities that appear only in
    funding, competing-interest or acknowledgement text (so a disclosed equity stake does not read like a supplier)."""
    sections: Dict[str, Set[str]] = defaultdict(set)
    role_of: Dict[str, str] = {}
    for m in mentions:
        if m["kind"] != "company" or m["entity_id"].startswith("unlisted:"):
            continue
        sections[m["entity_name"]].add(m["section"])
        role_of[m["entity_name"]] = company_role(m["entity_id"], company_types)

    def label(name: str) -> str:
        secs = sections[name]
        tag = ""
        if secs and secs <= set(_COI_SECTIONS):
            tag = " (COI)" if "coi" in secs else (" (funding)" if "funding" in secs else " (ack)")
        return esc(name) + tag

    out = []
    for role, heading in (("company", "Companies"), ("institute", "Institutes / funders"), ("ai_tool", "AI tools")):
        names = sorted(n for n, r in role_of.items() if r == role)
        if names:
            out.append(f"{heading}: " + ", ".join(label(n) for n in names))
    return out


def render(
    store: Store,
    since: str,
    until: str,
    cfg: Dict[str, Any],
    run_report: Optional[Dict[str, Any]] = None,
    company_types: Optional[Dict[str, str]] = None,
    company_names: Optional[Dict[str, str]] = None,
    config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build the digest. `company_types` maps gazetteer company id -> type (tools, diagnostics, synthesis, ai_bio,
    pharma, cro, institute, software / ai_tool); the caller builds it from config/companies.yaml (see
    Pipeline.company_types). Without it every company lands in the main table. `company_names` (id -> display
    name) labels the news groups; ids without a name fall back to the mentions table, then to the id itself.

    The top list is re-ranked at render time with the v2 heuristic (`rank.rerank.rerank_window`: full record,
    window context, soft diversity caps; scores are written back unless `ranking.v2.persist` is false) when
    `ranking.v2.enabled` is true (the default); with it off the stored scores are listed as before. `config_dir`
    locates journals.yaml / subgroups.yaml for the re-ranker (default: the configured directory).

    Table budgets (cfg['digest']): `platforms_table_n` (default: `instruments_table_n`, 40) and `lab_tools_table_n`
    (25) are separate, so reagent and QC products never push a platform out of the platform table; a cut table
    says how many rows it dropped."""
    company_types = {k: (v or "").lower() for k, v in (company_types or {}).items()}
    dcfg = cfg.get("digest") or {}
    rcfg = cfg.get("ranking") or {}
    top_n = int(dcfg.get("top_n", 25))
    news_n = int(dcfg.get("news_n", 40))
    signals_n = int(dcfg.get("signals_n", 15))
    platforms_n = int(dcfg.get("platforms_table_n", dcfg.get("instruments_table_n", 40)))
    tools_n = int(dcfg.get("lab_tools_table_n", 25))
    title = str(dcfg.get("title", "Science literature radar"))
    show_why = bool(dcfg.get("why_it_matters", True))
    show_subgroup = bool(dcfg.get("show_subgroup", True))
    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rank_info: Optional[Dict[str, Any]] = None
    if (rcfg.get("v2") or {}).get("enabled", True):
        rank_info = rerank_window(store, since, until, cfg, company_types=company_types, config_dir=config_dir, top_n=top_n)
        top = rank_info["top"]
    else:
        top = store.top_papers(since, until, limit=top_n)
    conversions = store.preprint_conversions(since, until)
    platforms = store.instrument_counts(since, until, limit=platforms_n, platform_only=True)
    infra = store.instrument_counts(since, until, limit=tools_n, platform_only=False)
    instruments = platforms + infra
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
    n_platforms = store.distinct_entities_in_window(since, until, "instrument", platform_only=True)
    n_infra = max(0, n_inst - n_platforms)
    n_comp = store.distinct_entities_in_window(since, until, "company")
    n_fulltext = sum(1 for p in in_window if p.get("fulltext_available"))
    names = store.entity_names("company")
    names.update({k: v for k, v in (company_names or {}).items() if v})

    md: List[str] = []
    md.append(f"# {esc(title)}: {esc(since)} to {esc(until)}\n")
    md.append(
        f"{len(in_window)} new papers and preprints in the window; {n_fulltext} with parsed full text; "
        f"{n_inst} instrument entities and {n_comp} companies detected; {sum(int(r['experiments']) for r in archive)} new sequencing experiments in public archives"
        + (f"; {sum(int(r['experiments']) for r in archive_proteomics)} proteomics datasets" if archive_proteomics else "")
        + (f"; {sum(int(r['experiments']) for r in archive_singlecell)} single-cell / spatial dataset assays" if archive_singlecell else "")
        + ".\n"
    )
    md.append(f"_Rendered {rendered_at}; every count is a snapshot of the database at that moment._\n")

    # ---- top innovations
    md.append(f"## Top {len(top)} innovations\n")
    if rank_info:
        md.append(_ranking_method_line(rank_info))
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
        venue = esc(p.get("journal") or p.get("source") or "") + (" (preprint)" if p.get("kind") == "preprint" else "")
        for pre in p.get("preprints") or []:
            venue += f" (also as preprint {esc(pre.get('pub_date') or 'n.d.')})"
        score = p.get("final")
        score_s = f"{float(score):.1f}" if score is not None else "n/a"
        tag = f" · {esc(p['subgroup'])}" if show_subgroup and p.get("subgroup") else ""
        md.append(f"**{i}. {_link(p['title'], p['url'])}**  \n_{venue}, {esc(p.get('pub_date') or 'n.d.')} · score {score_s}{tag}_  ")
        if llm:
            md.append(f"\n{esc(llm.get('one_line', ''))}  \n*Why it matters:* {esc(llm.get('why_it_matters', ''))}  ")
            flags = (
                f"novelty {esc(llm.get('novelty'))}/5 · commercial {esc(llm.get('commercial_implication'))}/5 · "
                f"instrument role: {esc(llm.get('instrument_role'))} · readiness: {esc(llm.get('clinical_readiness'))} · hype risk {esc(llm.get('hype_risk'))}/5"
            )
            md.append(f"\n<sub>{flags}</sub>  ")
        else:
            abstract = (p.get("abstract") or "").strip()
            label = ""
            if abstract:
                abstract = abstract[:SUMMARY_CHARS].rstrip() + ("…" if len(abstract) > SUMMARY_CHARS else "")
            else:
                abstract, label = fallback_summary(store, p)
            if abstract:
                md.append(f"\n{esc(abstract)}" + (f" <sub>{label}</sub>" if label else "") + "  ")
            if show_why and p.get("why"):
                md.append(f"\n*Why it matters:* {esc(p['why'])} <sub>(from the score components; no LLM)</sub>  ")
        tail = []
        if inst:
            tail.append("Instruments: " + ", ".join(esc(x) for x in inst))
        tail.extend(_company_tail(p["mentions"], company_types))
        tail.append("Signals: " + esc(p.get("reasons") or ""))
        md.append("\n<sub>" + " · ".join(tail) + "</sub>\n")
        related = p.get("related") or []
        if related:
            links = "; ".join(_link(x.get("title"), x.get("url"), max_len=90) for x in related[:3])
            more = f" and {len(related) - 3} more" if len(related) > 3 else ""
            md.append(f"<sub>+{len(related)} related from the same group, same venue, same day: {links}{more}</sub>\n")
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
    if n_platforms > len(platforms):
        md.append(f"_… and {n_platforms - len(platforms)} more platform(s) named in the window; the table shows the {len(platforms)} with most papers (digest.platforms_table_n)._\n")
    md.append("### Lab infrastructure and consumables named\n")
    md.append("Library prep, QC, PCR, automation, imaging, cytometry and bioprocess products. Useful for supplier share, not for innovation.\n")
    md.append(_table(
        ["Product line", "Vendor", "Ticker", "Papers"],
        [[r["entity_name"], r["vendor"], r["ticker"], r["papers"]] for r in infra],
    ))
    if n_infra > len(infra):
        md.append(f"_… and {n_infra - len(infra)} more product line(s) named in the window; the table shows the {len(infra)} with most papers (digest.lab_tools_table_n)._\n")
    if new_instruments and has_history:
        md.append("**First appearance in the corpus this window:** " + ", ".join(f"{esc(r['entity_name'])} ({int(r['papers'])})" for r in new_instruments[:20]) + "\n")

    # ---- archives
    archives_present = {a for r in archive for a in (r.get("archive") or "").split(",") if a}
    archive_label = ", ".join(SEQUENCING_ARCHIVE_LABELS[a] for a in SEQUENCING_ARCHIVES if a in archives_present) or ", ".join(SEQUENCING_ARCHIVE_LABELS.values())
    md.append(f"## Instruments behind newly public sequencing data ({archive_label})\n")
    md.append("Each row is a sequencing *experiment* (SRA / ENA) or *series x platform* (GEO) released in the window, with the instrument model the submitter declared (SRA covers submissions worldwide; ENA adds European and Japanese records SRA has not mirrored yet; GEO is the study-level view behind most published expression and single-cell datasets). This is a census of what machines are producing data right now, independent of what gets written up. Deposits lag sequencing by months to years, and clinical or commercial sequencing is never deposited. Models the gazetteer cannot map are grouped under their declared platform, e.g. 'Illumina (model unspecified)'.\n")
    if archive:
        md.append(_vendor_share_table(archive))
        md.append(_archive_table(archive, int(dcfg.get("archive_table_n", 30)), show_archive=True))
    else:
        md.append("_nothing in this window_\n")
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
                how = TRACKED_HINTS.get(str(t.get("matched_by") or ""), " <sub>(name match)</sub>")
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
        md.append(f"**{esc(names.get(comp_id) or comp_id)}**")
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

    # ---- derived layers: what changed, emerging concepts, companies not yet tracked ----------------
    for label, builder in (
        ("trends", lambda: _trends_section(store, until, dcfg)),
        ("concepts", lambda: render_concepts_markdown(store, since, until, top=int(dcfg.get("concepts_n", 15)))),
        ("candidates", lambda: render_candidates_markdown(store, since, until, top=int(dcfg.get("candidates_n", 15)))),
    ):
        try:
            section = builder()
            if section and section.strip():
                md.append(section.rstrip() + "\n")
        except Exception as exc:  # noqa: BLE001 - a derived section must never cost the digest
            md.append(f"## {label.title()}\n\n_section unavailable: {esc(str(exc))[:200]}_\n")

    # ---- coverage / methodology
    md.append("## Coverage and method\n")
    # the window's own numbers come from the database; the run counters below describe one pipeline process
    by_source: Counter = Counter()
    ft_by_source: Counter = Counter()
    for p in in_window:
        src = str(p.get("source") or "unknown")
        base = "rss" if src.startswith("rss:") else src
        by_source[base] += 1
        if p.get("fulltext_available"):
            ft_by_source[base] += 1
    md.append(
        f"**This window ({esc(since)} to {esc(until)}), counted in the database:** {len(in_window)} papers, {n_fulltext} with parsed full text; by source: "
        + (", ".join(f"{esc(k)} {v} ({ft_by_source[k]} with full text)" for k, v in by_source.most_common()) or "none")
        + ".  "
    )
    if not run_report:
        run_report = _latest_run_report(store, since, until)
    if run_report:
        c = run_report.get("counts", {}) or {}
        r_since, r_until = str(run_report.get("since") or "?"), str(run_report.get("until") or "?")
        when = str(run_report.get("finished") or run_report.get("started") or "")[:16].replace("T", " ")
        same_window = (r_since, r_until) == (since, until)
        scope = "" if same_window else "; that is NOT this digest's window, so these counters describe that run alone"
        fetched = {k.split(".", 1)[1]: v for k, v in c.items() if k.startswith("fetched.")}
        md.append(
            f"\n**Last pipeline run** (finished {esc(when) or 'n/a'} UTC, window {esc(r_since)} to {esc(r_until)}{scope}). "
            "Records pulled per source in that run, before de-duplication: " + (", ".join(f"{esc(k)} {esc(v)}" for k, v in sorted(fetched.items())) or "none") + ".  "
        )
        md.append(
            f"\nIn that run: unique papers after de-duplication {esc(c.get('papers.unique', 0))} ({esc(c.get('papers.new', 0))} new to the database, {esc(c.get('papers.reingested', 0))} already known and merged). "
            f"Full text parsed: bioRxiv {esc(c.get('fulltext.biorxiv', 0))} (failed {esc(c.get('fulltext.biorxiv_failed', 0))}), Europe PMC {esc(c.get('fulltext.europepmc', 0))} (failed {esc(c.get('fulltext.europepmc_failed', 0))}); "
            f"stored in full {esc(c.get('fulltext.policy_full', 0))}, facts only (licence does not allow redistribution) {esc(c.get('fulltext.policy_facts_only', 0))}"
            + (f"; abstracts recovered from full-text XML {esc(c.get('fulltext.abstract_from_jats'))}" if c.get("fulltext.abstract_from_jats") else "")
            + f". LLM-scored: {esc(c.get('llm.scored', 0))}.  "
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
        http = run_report.get("http") or {}
        if http.get("blocked_hosts"):
            md.append("\nHosts skipped after repeated HTTP 429 in that run: " + ", ".join(esc(h) for h in http["blocked_hosts"]) + ".  ")
        if run_report.get("errors"):
            md.append(f"\nSource errors in that run ({len(run_report['errors'])}): " + "; ".join(esc(e) for e in run_report["errors"][:10]) + (" …" if len(run_report["errors"]) > 10 else "") + ".  ")
        else:
            md.append("\nSource errors in that run: none recorded.  ")
    else:
        md.append("\nNo pipeline run is recorded for this database yet.  ")
    md.append(f"\nDatabase totals: {counts['papers']} papers, {counts['mentions']} entity mentions, {counts['archive_records']} archive records, {counts['news_items']} news items. "
              "Scores: a transparent heuristic (venue prior, the first platform and company named, watchlist terms, tracked authors, novelty and new-platform cues, "
              "early citations, preprint-to-journal conversion, news co-mention, first-in-corpus concepts; see docs/ranking.md) optionally blended with an LLM rubric. "
              "Only metadata, abstracts and open-licensed full text are stored; for other full text only the derived facts (entity mentions with short snippets) are kept, and subscription content is linked, never copied.\n")

    summary = {
        "since": since, "until": until, "papers_in_window": len(in_window),
        "top": [{"id": p["id"], "title": p["title"], "url": p["url"], "journal": p.get("journal"), "score": p.get("final"),
                 "kind": p.get("kind"), "subgroup": p.get("subgroup"), "reasons": p.get("reasons"), "why": p.get("why"),
                 "components": p.get("components"), "related": p.get("related") or [],
                 "preprints": p.get("preprints") or [],
                 "instruments": sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "instrument"}),
                 "companies": sorted({m["entity_name"] for m in p["mentions"] if m["kind"] == "company"})} for p in top],
        "ranking": ({k: rank_info.get(k) for k in ("candidates", "clustered", "pool", "caps", "deferred", "context", "persisted")} | {"method": "v2"})
        if rank_info else {"method": "stored"},
        "preprint_conversions": conversions,
        "instruments": instruments, "platforms": platforms, "lab_tools": infra,
        "platforms_total": n_platforms, "lab_tools_total": n_infra,
        "archive": archive, "archive_proteomics": archive_proteomics, "archive_singlecell": archive_singlecell,
        "companies": companies, "company_groups": {"main": [c["entity_id"] for c in main], "pharma": [c["entity_id"] for c in pharma],
                                                   "institutes": [c["entity_id"] for c in institutes], "ai_tools": [c["entity_id"] for c in ai_tools]},
        "tracked": tracked, "signals": signals,
        "counts": counts, "run": run_report,
    }
    return {"markdown": "\n".join(md), "summary": summary}


def _latest_run_report(store: Store, since: str, until: str) -> Optional[Dict[str, Any]]:
    """The recorded report of the last run for exactly this window, else the last run of all (labelled with its
    own window by the caller). `started` / `finished` / `since` / `until` are filled from the runs table when the
    stored report lacks them (reports written before they were recorded there)."""
    row = store.conn.execute(
        "SELECT started, finished, since, until, counts_json FROM runs WHERE since=? AND until=? ORDER BY id DESC LIMIT 1", (since, until)
    ).fetchone()
    if not row:
        row = store.conn.execute("SELECT started, finished, since, until, counts_json FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row[4]:
        return None
    try:
        report = json.loads(row[4])
    except ValueError:
        return None
    if not isinstance(report, dict):
        return None
    for key, val in (("started", row[0]), ("finished", row[1]), ("since", row[2]), ("until", row[3])):
        if not report.get(key) and val:
            report[key] = val
    return report


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
