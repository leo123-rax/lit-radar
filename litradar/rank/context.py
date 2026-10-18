"""Store-derived context for ranking v2: what the window's news said, which concepts are new to the corpus, and
which preprints already have a journal version on record.

Everything here is read-only SQL over tables the pipeline (or `litradar concepts`) already writes; nothing is
fetched. `build_context` degrades silently: a store without news or without concept counts yields a context in
which those signals are simply absent.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..config import load_yaml, resolve_config_dir
from .heuristics import RankContext

CONCEPT_HISTORY_WEEKS = 4  # weeks of concept counts before the window needed before "first in corpus" is scored

_WINDOW_P = "((p.pub_date >= :since AND p.pub_date <= :until) OR (p.pub_date = '' AND substr(p.first_seen,1,10) >= :since))"


def journal_tiers(config_dir: Optional[Path] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """({lower-case journal name: tier}, {issn: tier}) from config/journals.yaml; empty when the file is missing."""
    path = resolve_config_dir(config_dir) / "journals.yaml"
    by_name: Dict[str, str] = {}
    by_issn: Dict[str, str] = {}
    if not path.exists():
        return by_name, by_issn
    data = load_yaml(path) or {}
    for j in (data.get("journals") or []) if isinstance(data, dict) else []:
        if not isinstance(j, dict):
            continue
        tier = str(j.get("tier") or "").strip().upper()
        if not tier:
            continue
        name = str(j.get("name") or "").strip().lower()
        if name:
            by_name[name] = tier
        for issn in j.get("issn") or []:
            by_issn[str(issn).strip()] = tier
    return by_name, by_issn


def journal_tier_for(journal: Optional[str], extra: Optional[Dict[str, Any]], by_name: Dict[str, str], by_issn: Dict[str, str]) -> str:
    """The tier of a stored paper: by journal name, else by any ISSN the record carries (same rule as the pipeline)."""
    j = (journal or "").strip().lower()
    if j and j in by_name:
        return by_name[j]
    for issn in (extra or {}).get("issn", []) or []:
        if str(issn) in by_issn:
            return by_issn[str(issn)]
    return ""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _monday(day: str) -> str:
    d = date.fromisoformat(str(day)[:10])
    return (d.fromordinal(d.toordinal() - d.weekday())).isoformat()


def build_context(
    store: Any,
    since: str,
    until: str,
    by_name: Optional[Dict[str, str]] = None,
    by_issn: Optional[Dict[str, str]] = None,
    as_of: Optional[str] = None,
    concept_history_weeks: int = CONCEPT_HISTORY_WEEKS,
) -> RankContext:
    """Collect the window's news company ids and headlines, the concepts first seen in the window (when the corpus
    holds at least `concept_history_weeks` earlier weeks of counts) and the journal version of every preprint in
    the window that carries a `published_doi`. `as_of` (default `until`) anchors citation ages."""
    conn: sqlite3.Connection = store.conn
    ctx = RankContext(as_of=str(as_of or until or ""))
    params = {"since": since, "until": until}

    # ---- news of the window
    heads = []
    for title, companies_json in conn.execute(
        """SELECT title, companies_json FROM news_items
           WHERE COALESCE(NULLIF(substr(published,1,10), ''), substr(first_seen,1,10)) BETWEEN :since AND :until""",
        params,
    ):
        ctx.news_items += 1
        if title:
            heads.append(str(title).lower())
        try:
            for cid in json.loads(companies_json or "[]") or []:
                if cid:
                    ctx.news_company_ids.add(str(cid))
        except ValueError:
            continue
    ctx.news_headlines = "\n".join(heads)
    if ctx.news_company_ids and hasattr(store, "entity_names"):
        names = store.entity_names("company")
        ctx.news_company_names = {str(names[cid]).lower() for cid in ctx.news_company_ids if names.get(cid)}

    # ---- concepts first seen in the window
    if _table_exists(conn, "concept_counts") and _table_exists(conn, "concept_papers"):
        try:
            wk0 = _monday(since)
        except ValueError:
            wk0 = ""
        if wk0:
            row = conn.execute("SELECT COUNT(DISTINCT week) FROM concept_counts WHERE week < ?", (wk0,)).fetchone()
            ctx.concept_history_weeks = int(row[0] or 0) if row else 0
            ctx.concept_history_ok = ctx.concept_history_weeks >= int(concept_history_weeks)
            if ctx.concept_history_ok:
                cur = conn.execute(
                    """SELECT cp.paper_id, COALESCE(MAX(cc.display), cp.term)
                       FROM concept_papers cp
                       JOIN (SELECT term, MIN(week) AS first_week FROM concept_counts GROUP BY term) f ON f.term = cp.term
                       LEFT JOIN concept_counts cc ON cc.term = cp.term AND cc.week = cp.week
                       WHERE cp.week >= :wk0 AND f.first_week >= :wk0
                       GROUP BY cp.paper_id, cp.term""",
                    {"wk0": wk0},
                )
                for pid, display in cur:
                    ctx.first_concepts.setdefault(str(pid), []).append(str(display))

    # ---- preprints whose journal version is on record
    by_name = by_name or {}
    by_issn = by_issn or {}
    cur = conn.execute(
        f"""SELECT p.id, j.journal, j.extra_json FROM papers p
            JOIN papers j ON j.id = 'doi:' || lower(trim(p.published_doi))
            WHERE p.kind = 'preprint' AND p.published_doi IS NOT NULL AND trim(p.published_doi) != '' AND p.id != j.id
              AND {_WINDOW_P}""",
        params,
    )
    for pid, journal, extra_json in cur:
        try:
            extra = json.loads(extra_json or "{}") or {}
        except ValueError:
            extra = {}
        ctx.published_venues[str(pid)] = (str(journal or ""), journal_tier_for(journal, extra, by_name, by_issn))
    return ctx


def context_summary(ctx: RankContext) -> Dict[str, Any]:
    """JSON-serialisable description of what the context contributed (for the digest summary and the docs)."""
    return {
        "as_of": ctx.as_of,
        "news_items": ctx.news_items,
        "news_company_ids": len(ctx.news_company_ids),
        "concept_history_weeks": ctx.concept_history_weeks,
        "concept_history_ok": ctx.concept_history_ok,
        "papers_with_first_concepts": len(ctx.first_concepts),
        "preprints_with_published_version": len(ctx.published_venues),
    }
