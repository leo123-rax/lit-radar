"""Subgroup tags for ranked papers, mirroring `Pipeline.assign_subgroups` (which is computed but not persisted).

A paper belongs to a subgroup from config/subgroups.yaml when its title or abstract contains one of the group's
keywords (case-insensitive substring), when its OpenAlex `primary_topic` is one of the group's topic ids, or when a
backfill tagged it (`extra_json.subgroups`). The first match in file order is the *primary* tag the digest shows.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..trends import load_subgroups

OTHER = "other"


def load_subgroup_defs(config_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    return load_subgroups(config_dir)


def assign_subgroups(title: Optional[str], abstract: Optional[str], extra: Optional[Dict[str, Any]], defs: Sequence[Dict[str, Any]]) -> List[str]:
    text = f"{title or ''} {abstract or ''}".lower()
    extra = extra or {}
    tagged = {str(s) for s in (extra.get("subgroups") or []) if s}
    topic = extra.get("primary_topic")
    out: List[str] = []
    for g in defs:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        if gid in tagged or any(str(k).lower() in text for k in (g.get("keywords") or []) if k) or (topic and topic in (g.get("openalex_topics") or [])):
            out.append(gid)
    return out


def primary_subgroup(subgroups: Sequence[str]) -> str:
    return subgroups[0] if subgroups else OTHER
