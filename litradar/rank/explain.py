"""A "why it matters" one-liner from the score components, for digests rendered without a language model.

The template is deliberately literal: every clause names a fact the heuristic actually scored (venue, the
artefact the paper introduces, early citations, the platforms and companies it names, watchlist topics, a tracked
author, news co-mention, first corpus appearance of a concept). Nothing is inferred beyond that.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from .heuristics import ScoreResult

PREPRINT_SERVERS = {
    "biorxiv": "bioRxiv", "medrxiv": "medRxiv", "arxiv": "arXiv", "chemrxiv": "ChemRxiv", "research square": "Research Square",
    "ssrn": "SSRN", "preprints.org": "Preprints.org",
}


def _join(names: Iterable[str], limit: int = 2) -> str:
    items = [n for n in names if n][:limit]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _server(paper: Dict[str, Any]) -> str:
    raw = str(paper.get("journal") or paper.get("source") or "").strip()
    low = raw.lower()
    for key, label in PREPRINT_SERVERS.items():
        if low.startswith(key) or low == key:
            return label
    return raw or "preprint server"


def venue_phrase(paper: Dict[str, Any], res: ScoreResult) -> str:
    """'Nature paper', 'bioRxiv preprint', 'bioRxiv preprint already published in Nature Methods', 'journal article'."""
    kind = paper.get("kind") or "article"
    facts = res.facts
    if kind == "preprint":
        base = f"{_server(paper)} preprint"
        if facts.get("published_in"):
            return f"{base} already published in {facts['published_in']}"
        if res.component("preprint_published"):
            return f"{base} already accepted by a journal"
        return base
    journal = str(paper.get("journal") or "").strip()
    tier = facts.get("tier") or ""
    if journal and tier in ("A", "B"):
        return f"{journal} paper"
    if journal:
        return f"{journal} article"
    return "journal article"


def why_it_matters(res: ScoreResult, paper: Dict[str, Any], company_types: Optional[Dict[str, str]] = None, max_chars: int = 320) -> str:
    """One sentence built from the components; empty only when the result has no components at all."""
    if not res.components:
        return ""
    facts = res.facts
    clauses: List[str] = []
    opener = venue_phrase(paper, res)
    noun = facts.get("new_platform")
    if facts.get("notice"):
        opener += " that is a correction / retraction notice, not a study"
    elif noun:
        opener += f" introducing a new {noun}"
    clauses.append(opener)
    cit = facts.get("citations")
    if cit and cit.get("scored", True):
        n = cit.get("cited", 0)
        if cit.get("per_month") is not None:
            clause = f"cited {n}x ({cit['per_month']:.0f}/month since publication)"
        elif cit.get("age_days") is not None:
            days = int(cit["age_days"])
            when = "its first week" if days <= 7 else f"its first {max(1, (days + 6) // 7)} weeks"
            clause = f"cited {n}x in {when}"
        else:
            clause = f"already cited {n}x"
        if cit.get("influential"):
            clause += f" ({cit['influential']} influential)"
        clauses.append(clause)
    names: List[str] = list(facts.get("central_platforms") or facts.get("platforms") or [])[:2]
    companies: Sequence[str] = facts.get("companies") or []
    named = _join(names + [c for c in companies if c not in names], limit=3)
    if named:
        clauses.append(f"names {named}")
    if facts.get("author_companies"):
        clauses.append(f"company-affiliated author ({_join(facts['author_companies'], 2)})")
    if facts.get("watchlist"):
        clauses.append("on watchlist topics " + ", ".join(facts["watchlist"][:3]))
    if res.component("tracked_author"):
        clauses.append("by a tracked author" if facts.get("tracked_exact", True) else "by a tracked author (name match, not verified by id)")
    if facts.get("news"):
        clauses.append(f"{_join(facts['news'], 2)} in this week's news")
    if facts.get("first_concepts"):
        clauses.append("first corpus appearance of " + ", ".join(f"'{t}'" for t in facts["first_concepts"][:2]))
    text = "; ".join(clauses)
    text = text[:1].upper() + text[1:]
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip(" ;,") + "…"
    return text
