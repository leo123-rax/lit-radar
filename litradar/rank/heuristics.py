"""Cheap, transparent scoring. Every point has a reason string so the digest can show its work.

Score components (defaults live in config/radar.yaml -> ranking.weights):
  journal tier        A=4, B=2.5, C=1, preprint=1, unknown=0.5
  platforms named     +1.0 per distinct platform entity (cap 3): sequencers, single-cell, spatial, proteomics, cryo-EM, arrays, synthesis, cell engineering
  lab tools named     +0.2 per distinct infrastructure entity (cap 3): QC, library prep, PCR, automation, imaging, cytometry, bioprocess
  companies named     +0.5 per distinct company entity (cap 3)
  company author      +1.0 if an author has a company affiliation
  watchlist terms     +1.0 per matched watchlist term (cap 3)
  tracked author      +3.0
  novelty language    +0.5 per cue (cap 1.0)
  early citations     +1.0 if cited_by_count > 0 within the window (rare, strong signal)
  full text parsed    +0 by default (configurable)
The sum is scaled to 0-10 for blending with the optional LLM score.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

DEFAULT_WEIGHTS = {
    "tier_A": 4.0,
    "tier_B": 2.5,
    "tier_C": 1.0,
    "tier_preprint": 1.0,
    "tier_unknown": 0.5,
    "instrument": 1.0,          # per distinct PLATFORM entity (sequencer, single-cell, spatial, proteomics, cryo-EM, array, synthesis, cell engineering)
    "instrument_infra": 0.2,    # per distinct lab-infrastructure entity (qc, library prep, pcr, automation, imaging, cytometry, bioprocess, software)
    "instrument_cap": 3,
    "instrument_infra_cap": 3,
    "company": 0.5,
    "company_cap": 3,
    "company_author": 1.0,
    "watchlist": 1.0,
    "watchlist_cap": 3,
    "tracked_author": 3.0,
    "novelty": 0.5,
    "novelty_cap": 1.0,
    "early_citations": 1.0,
    "fulltext": 0.0,            # having full text is a data-availability artefact, not a quality signal
    "scale_max_raw": 14.0,
}

PLATFORM_CATEGORIES = ("sequencer", "single_cell", "spatial", "proteomics", "cryo_em", "array", "synthesis", "cell_engineering")


def is_platform(category: str) -> bool:
    """Platforms are the instruments an investor tracks; the rest is lab infrastructure and consumables."""
    return category.startswith(PLATFORM_CATEGORIES)


NOVELTY_CUES = [
    r"\bfirst\b", r"\bnovel\b", r"\bwe present\b", r"\bwe introduce\b", r"\bwe develop(ed)?\b", r"\bnew (method|platform|approach|technology)\b",
    r"\bscalable\b", r"\bhigh[- ]throughput\b", r"\bsingle[- ]molecule\b", r"\bgenome[- ]wide\b", r"\batlas\b", r"\bfoundation model\b",
]
NOVELTY_RE = re.compile("|".join(NOVELTY_CUES), re.I)


def _watchlist_hits(text: str, terms: Iterable[str]) -> List[str]:
    hits = []
    low = text.lower()
    for t in terms:
        if t.lower() in low:
            hits.append(t)
    return hits


def score_paper(
    paper: Dict[str, Any],
    mentions: Sequence[Dict[str, Any]],
    journal_tier: str,
    watchlist_terms: Sequence[str],
    tracked_author: bool,
    weights: Dict[str, Any] = None,
) -> Tuple[float, str]:
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    reasons: List[str] = []
    raw = 0.0
    kind = paper.get("kind") or "article"
    if kind == "preprint":
        raw += w["tier_preprint"]
        reasons.append("preprint")
    else:
        key = f"tier_{journal_tier}" if journal_tier in ("A", "B", "C") else "tier_unknown"
        raw += w[key]
        reasons.append(f"journal tier {journal_tier or '?'}")

    platforms = {m["entity_id"] for m in mentions if m.get("kind") == "instrument" and is_platform(m.get("category") or "")}
    infra = {m["entity_id"] for m in mentions if m.get("kind") == "instrument" and not is_platform(m.get("category") or "")}
    companies = {m["entity_id"] for m in mentions if m.get("kind") == "company"}
    if platforms:
        n = min(len(platforms), int(w["instrument_cap"]))
        raw += n * w["instrument"]
        reasons.append(f"{len(platforms)} platform(s)")
    if infra:
        n = min(len(infra), int(w["instrument_infra_cap"]))
        raw += n * w["instrument_infra"]
        reasons.append(f"{len(infra)} lab tool(s)")
    if companies:
        n = min(len(companies), int(w["company_cap"]))
        raw += n * w["company"]
        reasons.append(f"{len(companies)} company mention(s)")
    if any(m.get("kind") == "company" and m.get("section") == "affiliations" for m in mentions):
        raw += w["company_author"]
        reasons.append("company-affiliated author")

    text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}"
    wl = _watchlist_hits(text, watchlist_terms)
    if wl:
        n = min(len(wl), int(w["watchlist_cap"]))
        raw += n * w["watchlist"]
        reasons.append("watchlist: " + ", ".join(wl[:4]))
    if tracked_author:
        raw += w["tracked_author"]
        reasons.append("tracked author")
    cues = len(NOVELTY_RE.findall(text))
    if cues:
        raw += min(cues * w["novelty"], w["novelty_cap"])
        reasons.append(f"{cues} novelty cue(s)")
    if int(paper.get("cited_by_count") or 0) > 0:
        raw += w["early_citations"]
        reasons.append(f"already cited {paper.get('cited_by_count')}x")
    if paper.get("fulltext_available") and w["fulltext"]:
        raw += w["fulltext"]
        reasons.append("full text parsed")
    scaled = max(0.0, min(10.0, 10.0 * raw / float(w["scale_max_raw"])))
    return round(scaled, 2), "; ".join(reasons)


def blend(heuristic: float, llm_score: float = None, llm_weight: float = 0.6) -> float:
    if llm_score is None:
        return round(heuristic, 2)
    return round((1 - llm_weight) * heuristic + llm_weight * llm_score, 2)
