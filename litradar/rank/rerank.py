"""Re-rank a digest window with the v2 heuristic, the store-derived context and the soft diversity caps.

`Pipeline.rank` scores papers as they stream in with the minimal record it has at that moment and no context.
At digest time the store knows more: the whole window's news, which concepts are new to the corpus, which
preprints already have a journal version, every mention section. `rerank_window` re-scores every paper of the
window with the full record (`rank.heuristics.score_paper_v2`), blends in any stored LLM composite exactly as the
pipeline does, writes the refreshed scores back (`ranking.v2.persist`, default on, so `litradar search` and the
dashboard show the same numbers as the digest), folds preprints into their journal versions, and returns the
diversified top list with per-entry components, a template "why it matters" line and a subgroup tag.

Configuration (config/radar.yaml):

    ranking:
      v2: {enabled: true, persist: true, concept_history_weeks: 4, candidate_pool: 0}
      diversity: {enabled: true, max_per_kind: 15, max_per_subgroup: 8, cluster_same_group: true}

Complexity is linear in the window: one `papers_in_window` query, one chunked `mentions` query and one chunked
`tracked_author_hits` query per 500 papers, then a single pass over the rows.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..store import _json_dict
from ..track import priority_multiplier
from .context import build_context, context_summary, journal_tier_for, journal_tiers
from .diversity import describe_deferred, diversify
from .explain import why_it_matters
from .heuristics import blend, score_paper_v2
from .subgroups import assign_subgroups, load_subgroup_defs, primary_subgroup

log = logging.getLogger(__name__)

DEFAULT_MAX_PER_KIND = 15
DEFAULT_MAX_PER_SUBGROUP = 8


_CHUNK = 500


def _chunks(items: Sequence[str], n: int = _CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _mentions_by_paper(store: Any, ids: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    """All mentions of the given papers in a handful of chunked queries (one per 500 ids), keyed by paper id."""
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(list(ids)):
        cur = store.conn.execute(
            f"SELECT * FROM mentions WHERE paper_id IN ({','.join('?' * len(chunk))}) ORDER BY paper_id, kind, entity_id, section", tuple(chunk)
        )
        cols = [d[0] for d in cur.description]
        for row in cur:
            d = dict(zip(cols, row))
            out[str(d["paper_id"])].append(d)
    return out


def _tracked_by_paper(store: Any, ids: Sequence[str]) -> Dict[str, Any]:
    """{paper id: "openalex_id" | "name"} for papers with tracked-author hits; an id match outranks a name match."""
    out: Dict[str, Any] = {}
    for chunk in _chunks(list(ids)):
        for pid, matched_by in store.conn.execute(
            f"SELECT paper_id, matched_by FROM tracked_author_hits WHERE paper_id IN ({','.join('?' * len(chunk))})", tuple(chunk)
        ):
            kind = "openalex_id" if str(matched_by or "") == "openalex_id" else "name"
            if out.get(pid) != "openalex_id":
                out[str(pid)] = kind
    return out


def _author_parts(name: Any) -> Tuple[str, str]:
    """('surname', 'first name or initial') for 'First M. Surname' or 'Surname, First M.'; ('', '') when unusable."""
    s = re.sub(r"[.’'`]", "", str(name or "")).strip().lower()
    if not s:
        return "", ""
    if "," in s:
        last, _, first = s.partition(",")
        last, first = last.strip(), first.strip().split()[0] if first.strip() else ""
    else:
        parts = s.split()
        if len(parts) < 2:
            return "", ""
        last, first = parts[-1], parts[0]
    return last, first


def _author_key(name: Any) -> str:
    """'surname f' (surname plus first initial); '' when unusable."""
    last, first = _author_parts(name)
    return f"{last} {first[:1]}".strip() if last and first else ""


def _full_name(name: Any) -> str:
    """'first surname' when the record spells the first name out; '' for initials-only names ('Zhang Z', 'Meng, L.'),
    which are never used for clustering: on a busy preprint day a shared surname and initial is not one group."""
    last, first = _author_parts(name)
    return f"{first} {last}" if last and len(first) >= 2 else ""


_TITLE_TOKEN_RE = re.compile(r"[a-z][a-z\-]{5,}")
_TITLE_STOP = frozenset(
    """analysis through between against during without within toward towards across single using based their these those
    which while reveals revealed identifies identified associated multiple specific following characterization evaluation
    effects patients cancer cancers cellular protein proteins disease models results clinical function functional
    expression development molecular genetic genomic integrated improves response responses predicts prediction regulates
    regulation mechanism mechanisms dependent independent mediated derived induced treatment therapy therapeutic targets
    landscape dynamics profiling insights evidence systematic comprehensive differential structural structure signaling
    signalling pathway pathways network networks""".split()
)


def _informative_tokens(title: Any) -> frozenset:
    return frozenset(t for t in _TITLE_TOKEN_RE.findall(str(title or "").lower()) if t not in _TITLE_STOP)


def _group_keys(row: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    """Keys that place a paper in a 'same group, same day, same venue' cluster: the full name of the last author and of
    the first author, each with the venue and the publication day."""
    try:
        authors = json.loads(row.get("authors_json") or "[]") or []
    except ValueError:
        return []
    names = [a.get("name") if isinstance(a, dict) else a for a in authors]
    journal = str(row.get("journal") or row.get("source") or "").strip().lower()
    day = str(row.get("pub_date") or "")[:10]
    if not names or not journal or not day:
        return []
    keys: List[Tuple[str, str, str, str]] = []
    last, first = _full_name(names[-1]), _full_name(names[0])
    if last:
        keys.append(("last", journal, day, last))
    if first and first != last:
        keys.append(("first", journal, day, first))
    return keys


def cluster_related(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold near-duplicates into the best-scoring one. Two papers are one story when they appeared in the same venue on
    the same day, share a first or last author whose first name is spelled out identically on both records, and either
    share an informative title token (six letters or more, not boilerplate) or share both first and last author. The
    leader gains `related` = [{id, title, url, final}]; rows must already be in score order. Linear in the rows."""
    seen: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    tokens_of: Dict[int, frozenset] = {}
    out: List[Dict[str, Any]] = []
    for r in rows:
        keys = _group_keys(r)
        tokens = _informative_tokens(r.get("title"))
        hits = [seen[k] for k in keys if k in seen]
        leader = None
        for cand in hits:
            both_authors = sum(1 for h in hits if h is cand) > 1
            if both_authors or (tokens & tokens_of.get(id(cand), frozenset())):
                leader = cand
                break
        if leader is not None:
            leader.setdefault("related", []).append({"id": r.get("id"), "title": r.get("title"), "url": r.get("url"), "final": r.get("final")})
            continue
        for k in keys:
            seen.setdefault(k, r)
        tokens_of[id(r)] = tokens
        out.append(r)
    return out


def _tracked_match(store: Any, paper_id: str) -> Any:
    """False when no tracked author wrote the paper; "openalex_id" when at least one hit is an id match; else "name"."""
    return _tracked_by_paper(store, [paper_id]).get(paper_id, False)


def rerank_window(
    store: Any,
    since: str,
    until: str,
    cfg: Dict[str, Any],
    company_types: Optional[Dict[str, str]] = None,
    config_dir: Optional[Path] = None,
    persist: Optional[bool] = None,
    as_of: Optional[str] = None,
    top_n: Optional[int] = None,
) -> Dict[str, Any]:
    """Score, persist, collapse, diversify. Returns {top, deferred, caps, candidates, pool, context, persisted,
    weights}. `top` rows carry everything `Store.top_papers` rows do (`mentions`, `preprints`, `final`, `reasons`)
    plus `heuristic`, `components`, `why`, `subgroups` and `subgroup`."""
    rcfg = cfg.get("ranking") or {}
    v2cfg = rcfg.get("v2") or {}
    dcfg = cfg.get("digest") or {}
    divcfg = rcfg.get("diversity") or {}
    weights = rcfg.get("weights") or {}
    watchlist = cfg.get("watchlist_terms") or []
    llm_weight = float((rcfg.get("llm") or {}).get("llm_weight", 0.6))
    persist = bool(v2cfg.get("persist", True)) if persist is None else bool(persist)
    n = int(dcfg.get("top_n", 25)) if top_n is None else int(top_n)
    priorities = cfg.get("priorities") or {}
    company_types = {k: (v or "").lower() for k, v in (company_types or {}).items()}

    by_name, by_issn = journal_tiers(config_dir)
    defs = load_subgroup_defs(config_dir)
    ctx = build_context(store, since, until, by_name, by_issn, as_of=as_of,
                        concept_history_weeks=int(v2cfg.get("concept_history_weeks", 4)))

    rows: List[Dict[str, Any]] = store.papers_in_window(since, until)
    ids = [r["id"] for r in rows]
    mentions_of = _mentions_by_paper(store, ids)
    tracked_of = _tracked_by_paper(store, ids)
    for r in rows:
        mentions = mentions_of.get(r["id"], [])
        tracked = tracked_of.get(r["id"], False)
        extra = _json_dict(r.get("extra_json"))
        paper = dict(r)
        paper["extra"] = extra
        tier = journal_tier_for(r.get("journal"), extra, by_name, by_issn)
        res = score_paper_v2(paper, mentions, tier, watchlist, tracked, weights, company_types, ctx)
        h, reasons = res.scaled, res.reasons
        subs = assign_subgroups(r.get("title"), r.get("abstract"), extra, defs)
        if priorities:
            mult = priority_multiplier(r, mentions, r.get("journal") or "", subs, priorities=priorities)
            if mult != 1.0:
                h = round(min(10.0, h * mult), 2)
                reasons += f"; priority x{mult:g}"
        llm_prev = None
        if r.get("llm_json"):
            try:
                llm_prev = json.loads(r["llm_json"])
            except ValueError:
                llm_prev = None
            if not isinstance(llm_prev, dict):
                llm_prev = None
        final = blend(h, llm_prev.get("composite") if llm_prev else None, llm_weight)
        r.update({
            "heuristic": h, "final": final, "reasons": reasons, "mentions": mentions,
            "components": res.as_dict()["components"], "score_raw": round(res.raw, 3),
            "why": why_it_matters(res, paper, company_types), "subgroups": subs, "subgroup": primary_subgroup(subs),
        })
        if persist:
            store.save_score(r["id"], h, llm_prev, final, reasons)
    if persist:
        store.commit()

    rows, _conversions = store.collapse_preprints(rows)
    # deterministic order: score, then raw heuristic, then newest, then title
    rows.sort(key=lambda r: (r.get("title") or "").lower())
    rows.sort(key=lambda r: r.get("pub_date") or "", reverse=True)
    rows.sort(key=lambda r: (-(r.get("final") or 0.0), -(r.get("heuristic") or 0.0)))
    n_before_cluster = len(rows)
    if divcfg.get("cluster_same_group", True):
        rows = cluster_related(rows)

    pool_n = int(v2cfg.get("candidate_pool") or 0) or max(4 * n, 100)
    pool = rows[:pool_n]
    caps = {"max_per_kind": 0, "max_per_subgroup": 0}
    deferred: List[Dict[str, Any]] = []
    if divcfg.get("enabled", True):
        caps = {
            "max_per_kind": int(divcfg.get("max_per_kind", DEFAULT_MAX_PER_KIND) or 0),
            "max_per_subgroup": int(divcfg.get("max_per_subgroup", DEFAULT_MAX_PER_SUBGROUP) or 0),
        }
        top, deferred = diversify(pool, n, **caps)
    else:
        top = pool[:n]
    return {
        "top": top,
        "deferred": describe_deferred(deferred),
        "caps": caps,
        "candidates": n_before_cluster,
        "clustered": n_before_cluster - len(rows),
        "pool": len(pool),
        "context": context_summary(ctx),
        "persisted": persist,
        "weights": weights,
    }


def titles(entries: List[Dict[str, Any]]) -> List[str]:
    return [str(e.get("title") or "") for e in entries]
