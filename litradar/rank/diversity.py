"""Soft diversity re-ranking for the top list.

`diversify` walks the candidates in score order and admits each one while its kind (preprint / article) and its
subgroup are below their caps; candidates that would breach a cap are deferred. When the first pass leaves slots
empty the deferred candidates fill them in score order, so the caps are soft: they reorder, they never shorten
the list. Deferred entries are returned with their original rank so the digest can say what moved.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


def diversify(
    candidates: Sequence[Dict[str, Any]],
    n: int,
    max_per_kind: int = 0,
    max_per_subgroup: int = 0,
    kind_of: Callable[[Dict[str, Any]], str] = lambda e: str(e.get("kind") or "article"),
    subgroup_of: Callable[[Dict[str, Any]], str] = lambda e: str(e.get("subgroup") or "other"),
    exempt_subgroups: Sequence[str] = ("other",),
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (selected, deferred). A cap of 0 (or less) disables that dimension; subgroups in `exempt_subgroups`
    (the catch-all 'other') are never capped. Each deferred record is {entry, rank, cap, readmitted}."""
    n = max(0, int(n))
    selected: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    by_kind: Dict[str, int] = {}
    by_sub: Dict[str, int] = {}
    for rank, e in enumerate(candidates, 1):
        if len(selected) >= n:
            break
        k, s = kind_of(e), subgroup_of(e)
        over: Optional[str] = None
        if max_per_kind > 0 and by_kind.get(k, 0) >= max_per_kind:
            over = f"kind {k}"
        elif max_per_subgroup > 0 and s not in exempt_subgroups and by_sub.get(s, 0) >= max_per_subgroup:
            over = f"subgroup {s}"
        if over:
            deferred.append({"entry": e, "rank": rank, "cap": over, "readmitted": False})
            continue
        by_kind[k] = by_kind.get(k, 0) + 1
        by_sub[s] = by_sub.get(s, 0) + 1
        selected.append(e)
    if len(selected) < n:
        for d in deferred:
            if len(selected) >= n:
                break
            d["readmitted"] = True
            selected.append(d["entry"])
    return selected, deferred


def describe_deferred(deferred: Sequence[Dict[str, Any]], title_of: Callable[[Dict[str, Any]], str] = lambda e: str(e.get("title") or "")) -> List[Dict[str, Any]]:
    """Compact, JSON-friendly view of the deferred list."""
    return [
        {"rank": d["rank"], "cap": d["cap"], "readmitted": bool(d["readmitted"]), "id": d["entry"].get("id"), "title": title_of(d["entry"])}
        for d in deferred
    ]
