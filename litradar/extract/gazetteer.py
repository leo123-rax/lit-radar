"""Gazetteer matching: find instruments, platforms, kits and companies in text.

Design notes
------------
* Entries live in YAML (config/instruments.yaml, config/companies.yaml). Each entry has a
  canonical name plus aliases as they appear in Methods sections.
* Aliases become regexes with word boundaries. Spaces and hyphens inside an alias match any
  mix of whitespace / hyphen / nothing, so "NovaSeq X Plus", "NovaSeq-X Plus" and "NovaSeqX Plus"
  all hit the same entry.
* Short or ambiguous aliases ("G4", "Vega", "Platinum", "Revio") are case-sensitive and require
  a context term (usually the vendor) within a window, otherwise they are ignored.
* Overlapping matches resolve to the longest alias, so "NovaSeq X Plus" is not double-counted
  as "NovaSeq X".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..models import Mention

CONTEXT_WINDOW = 250  # characters on each side used to look for a context term
SNIPPET_RADIUS = 90


@dataclass
class Entry:
    id: str
    name: str
    kind: str  # instrument | company
    aliases: List[str]
    vendor: str = ""
    ticker: str = ""
    category: str = ""
    parent: str = ""
    launched: Optional[int] = None
    status: str = "current"
    context_required: bool = False
    context_terms: List[str] = field(default_factory=list)
    case_sensitive_aliases: List[str] = field(default_factory=list)
    domains: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class _Pattern:
    entry: Entry
    alias: str
    regex: re.Pattern[str]
    needs_context: bool
    context_regex: Optional[re.Pattern[str]]
    token: str = ""  # longest alphanumeric chunk of the alias, lower-cased; a cheap necessary condition for a match


def _literal_token(alias: str) -> str:
    """The longest run of word characters in an alias, lower-cased.

    Every alias regex matches that run literally (case-insensitively at most), so `token in text.lower()` is a
    necessary condition for the regex to match and lets `find` skip the regex for the vast majority of patterns.
    (The only theoretical gap is Unicode case folding that `str.lower` does not perform, such as the long s.)"""
    chunks = re.findall(r"[^\W_]+", alias)
    return max(chunks, key=len).lower() if chunks else ""


def _alias_to_regex(alias: str, case_sensitive: bool) -> re.Pattern[str]:
    parts = [re.escape(p) for p in re.split(r"[\s\- ]+", alias.strip()) if p]
    body = r"[\s\- ]*".join(parts)
    pattern = rf"(?<![\w\-]){body}(?![\w\-])"
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


def _is_ambiguous(alias: str) -> bool:
    """Heuristic: very short aliases and single dictionary-ish words are risky on their own."""
    compact = alias.replace(" ", "").replace("-", "")
    if len(compact) <= 3:
        return True
    if len(compact) <= 5 and compact.isalpha() and compact.lower() == compact:
        return True
    return False


def load_entries(items: Iterable[dict], kind: str) -> List[Entry]:
    entries: List[Entry] = []
    for raw in items or []:
        if not raw or "id" not in raw:
            continue
        aliases = list(raw.get("aliases") or [])
        name = raw.get("name") or raw["id"]
        if name not in aliases:
            aliases.insert(0, name)
        entries.append(
            Entry(
                id=str(raw["id"]),
                name=str(name),
                kind=kind,
                aliases=[str(a) for a in aliases if a],
                vendor=str(raw.get("vendor") or ""),
                ticker=str(raw.get("ticker") or ""),
                category=str(raw.get("category") or ""),
                parent=str(raw.get("parent") or ""),
                launched=raw.get("launched"),
                status=str(raw.get("status") or "current"),
                context_required=bool(raw.get("context_required", False)),
                context_terms=[str(t) for t in (raw.get("context_terms") or [])],
                case_sensitive_aliases=[str(a) for a in (raw.get("case_sensitive_aliases") or [])],
                domains=[str(d).lower() for d in (raw.get("domains") or [])],
                notes=str(raw.get("notes") or ""),
            )
        )
    return entries


class Gazetteer:
    def __init__(self, entries: Sequence[Entry]) -> None:
        self.entries = list(entries)
        self.by_id: Dict[str, Entry] = {e.id: e for e in self.entries}
        self._patterns: List[_Pattern] = []
        for e in self.entries:
            default_context = [t for t in [e.vendor, e.parent, e.name] if t]
            context_terms = e.context_terms or default_context
            context_regex = None
            if context_terms:
                context_regex = re.compile(
                    "|".join(_alias_to_regex(t, False).pattern for t in context_terms), re.IGNORECASE
                )
            for alias in e.aliases:
                ambiguous = _is_ambiguous(alias) or alias in e.case_sensitive_aliases
                case_sensitive = ambiguous or alias in e.case_sensitive_aliases
                needs_context = e.context_required or (ambiguous and alias != e.name) or alias in e.case_sensitive_aliases
                # the canonical name of a context_required entry still needs context
                if e.context_required:
                    needs_context = True
                self._patterns.append(
                    _Pattern(e, alias, _alias_to_regex(alias, case_sensitive), needs_context, context_regex, _literal_token(alias))
                )
        # longest alias first so overlapping spans resolve to the most specific entry
        self._patterns.sort(key=lambda p: -len(p.alias))

    @classmethod
    def from_yaml(cls, data: dict, kind: str) -> Gazetteer:
        items = data.get("entries") if isinstance(data, dict) else data
        return cls(load_entries(items or [], kind))

    def find(self, text: str, section: str = "", paper_id: str = "", ignore_context: bool = False, _prefilter: bool = True) -> List[Mention]:
        """Return one Mention per entry found in `text` (count = occurrences).

        ignore_context=True disables the context-term requirement; use it only on controlled
        vocabularies (archive instrument_model strings), never on prose.
        _prefilter=False bypasses the literal-token prefilter (identical results, slower; kept for tests)."""
        if not text:
            return []
        low = text.lower() if _prefilter else ""
        taken: List[Tuple[int, int]] = []
        hits: Dict[str, Mention] = {}
        for pat in self._patterns:
            if _prefilter and pat.token and pat.token not in low:
                continue
            for m in pat.regex.finditer(text):
                span = (m.start(), m.end())
                if any(s < span[1] and span[0] < e for s, e in taken):
                    continue
                if pat.needs_context and not ignore_context:
                    lo, hi = max(0, span[0] - CONTEXT_WINDOW), min(len(text), span[1] + CONTEXT_WINDOW)
                    window = text[lo : span[0]] + " " + text[span[1] : hi]
                    if not pat.context_regex or not pat.context_regex.search(window):
                        continue
                taken.append(span)
                e = pat.entry
                if e.id in hits:
                    hits[e.id].count += 1
                    continue
                lo, hi = max(0, span[0] - SNIPPET_RADIUS), min(len(text), span[1] + SNIPPET_RADIUS)
                snippet = re.sub(r"\s+", " ", text[lo:hi]).strip()
                hits[e.id] = Mention(
                    paper_id=paper_id,
                    kind=e.kind,
                    entity_id=e.id,
                    entity_name=e.name,
                    vendor=e.vendor,
                    ticker=e.ticker,
                    category=e.category,
                    section=section,
                    count=1,
                    snippet=snippet,
                )
        return list(hits.values())

    def find_in_sections(self, paper, sections: Sequence[str]) -> List[Mention]:
        out: List[Mention] = []
        for sec in sections:
            out.extend(self.find(paper.text_for(sec), section=sec, paper_id=paper.id))
        return out

    def match_domains(self, emails: Iterable[str]) -> List[Entry]:
        """Company entries whose email domains appear in the given addresses."""
        found: Dict[str, Entry] = {}
        for addr in emails:
            dom = addr.split("@")[-1].lower().strip()
            for e in self.entries:
                if any(dom == d or dom.endswith("." + d) for d in e.domains):
                    found[e.id] = e
        return list(found.values())

    def match_archive_model(self, instrument_model: str) -> Optional[Entry]:
        """Map an archive's controlled-vocabulary instrument string to a gazetteer entry."""
        if not instrument_model:
            return None
        hits = self.find(instrument_model, ignore_context=True)
        if not hits:
            return None
        # prefer the most specific (longest-named) hit
        hits.sort(key=lambda h: -len(h.entity_name))
        return self.by_id.get(hits[0].entity_id)
