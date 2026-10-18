"""OpenAlex works API (https://docs.openalex.org). Free, CC0, ~100k calls/day with a mailto.

Used for: (1) new works in tracked topics, (2) works with company-affiliated authors
(`authorships.institutions.type:company`), (3) new works by tracked authors, (4) journal
coverage by ISSN. Abstracts come as an inverted index and are rebuilt here.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional

from ..http import Http
from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)
WORKS = "https://api.openalex.org/works"
SELECT = ",".join(
    [
        "id", "doi", "title", "publication_date", "type", "primary_location", "authorships",
        "abstract_inverted_index", "topics", "primary_topic", "cited_by_count", "open_access", "biblio",
        # 'grants' is no longer a valid `select` field (OpenAlex returns 400 for it, 2026-10); funders now ride in primary_location/ids only
    ]
)
MAX_RATE_LIMIT_WAIT = 60.0  # seconds; a plain HTTP 429 (per-second rate limit) is retried once after this at most


def reconstruct_abstract(inv: Optional[Dict[str, List[int]]]) -> str:
    if not inv:
        return ""
    positions: List[tuple] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


class OpenAlexBudgetExhausted(RuntimeError):
    """OpenAlex refused the request for budget reasons: the free per-IP budget is used up (no API key), the key's
    own budget is gone, or the answer was HTTP 403. A plain per-second rate limit (HTTP 429 without "budget" in the
    message) is NOT this error; the pager waits and retries that once."""


class OpenAlexRequestError(RuntimeError):
    """OpenAlex rejected the request itself (HTTP 400/404/422...): an invalid `select` field, a malformed filter, an
    unknown parameter. The body's `message` is carried verbatim (truncated) so the caller can print the reason;
    retrying the same request would return the same answer, so the pager raises instead of returning nothing.
    Verified live 2026-10-17: ``select=...,grants`` answers 400 ``{"error": "...", "message": "grants is not a valid
    select field. Valid fields for select are: id, doi, title, ..."}``."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = int(status)
        self.message = (message or "").strip()
        super().__init__(f"HTTP {self.status}" + (f": {self.message}" if self.message else ""))


def _sleep(seconds: float) -> None:  # module-level so tests can replace it
    time.sleep(seconds)


def retry_after_seconds(resp: Any, data: Any, default: float = 5.0) -> float:
    """Seconds to wait after a plain 429: the body's `retryAfter` (OpenAlex) or the Retry-After header, capped."""
    val: Any = data.get("retryAfter") if isinstance(data, dict) else None
    if val is None:
        val = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        secs = float(val)
    except (TypeError, ValueError):
        secs = default
    return max(0.0, min(secs, MAX_RATE_LIMIT_WAIT))


def works(
    http: Http,
    filters: Dict[str, str],
    mailto: str = "",
    api_key: str = "",
    max_records: int = 10000,
    select: str = SELECT,
    extra_params: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """Page through /works. Since 2026 OpenAlex meters anonymous traffic per IP; a free key
    (https://help.openalex.org/api/authentication/) is sent as a Bearer token when configured.

    `extra_params` are merged into the query string, e.g. ``{"search.title_and_abstract": '"a" OR "b"'}`` (the
    documented scoped-search parameter; see https://help.openalex.org/api/searching/).

    `state`, when a dict is passed, reports how paging ended so callers can tell a finished query from an
    interrupted one: ``state["complete"]`` is True only when the API returned its last page; ``state["capped"]``
    is True when `max_records` stopped the pager with a cursor still open; ``state["error"]`` names any other
    early stop (transport failure, non-JSON body, HTTP 5xx, a rate limit that persisted after one retry).
    Budget exhaustion raises OpenAlexBudgetExhausted; a plain HTTP 429 is retried once after `retryAfter` seconds;
    a 4xx answer other than 403/429 (invalid select field, malformed filter) raises OpenAlexRequestError carrying
    the API's message, because repeating the request cannot help and silence would hide a dead source.
    """
    if state is None:
        state = {}
    cursor = "*"
    n = 0
    flt = ",".join(f"{k}:{v}" for k, v in filters.items())
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    retried_429 = False
    while n < max_records:
        params: Dict[str, Any] = {"filter": flt, "per-page": 200, "cursor": cursor, "select": select}
        if mailto:
            params["mailto"] = mailto
        params.update(extra_params or {})
        resp = http.get(WORKS, params=params, headers=headers, ok_statuses=(200, 403, 429))
        if resp is None:
            state["error"] = "transport failure"
            break
        try:
            data = resp.json()
        except ValueError:
            state["error"] = f"non-JSON body (HTTP {resp.status_code})"
            break
        if resp.status_code != 200 or not isinstance(data, dict) or data.get("error"):
            msg = str(data.get("message") or "") if isinstance(data, dict) else ""
            if "budget" in msg.lower() or resp.status_code == 403:
                raise OpenAlexBudgetExhausted(msg[:300] or f"HTTP {resp.status_code}")
            if resp.status_code == 429 and not retried_429:
                retried_429 = True
                wait = retry_after_seconds(resp, data)
                log.warning("OpenAlex rate limit (HTTP 429: %s); waiting %.0fs and retrying the page once", msg[:80] or "no message", wait)
                _sleep(wait)
                continue
            err = msg[:120] or (str(data.get("error"))[:120] if isinstance(data, dict) and data.get("error") else "")
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                # the request itself is wrong (bad select/filter/parameter): deterministic, so say why and stop
                detail = msg or (str(data.get("error")) if isinstance(data, dict) and data.get("error") else "") or (getattr(resp, "text", "") or "")
                log.warning("OpenAlex rejected the request (HTTP %s): %s", resp.status_code, (detail or "no message")[:300])
                state["error"] = f"HTTP {resp.status_code}" + (f": {err}" if err else "")
                raise OpenAlexRequestError(resp.status_code, detail[:300])
            state["error"] = f"HTTP {resp.status_code}" + (f": {err}" if err else "")
            break
        retried_429 = False
        results = data.get("results") or []
        yield from results
        n += len(results)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not results or not cursor:
            state["complete"] = True
            break
    else:
        state["capped"] = True


def to_paper(w: Dict[str, Any]) -> Paper:
    doi = normalize_doi(w.get("doi"))
    oa_id = (w.get("id") or "").rsplit("/", 1)[-1]
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    authors: List[Author] = []
    affs: List[str] = []
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        insts = [{"name": i.get("display_name"), "type": i.get("type"), "ror": i.get("ror")} for i in (a.get("institutions") or [])]
        raw = a.get("raw_affiliation_strings") or []
        affs.extend(raw)
        authors.append(
            Author(
                name=au.get("display_name") or "",
                affiliation="; ".join(raw) if raw else "; ".join(i["name"] for i in insts if i.get("name")),
                orcid=(au.get("orcid") or "").rsplit("/", 1)[-1] if au.get("orcid") else "",
                openalex_id=(au.get("id") or "").rsplit("/", 1)[-1],
                institutions=insts,
            )
        )
    topics = [t.get("display_name") for t in (w.get("topics") or []) if t.get("display_name")]
    is_preprint = (w.get("type") or "") == "preprint" or (src.get("type") == "repository")
    grants = [g.get("funder_display_name") for g in (w.get("grants") or []) if g.get("funder_display_name")]
    p = Paper(
        id=Paper.make_id("openalex", oa_id, doi),
        source="openalex",
        kind="preprint" if is_preprint else "article",
        title=(w.get("title") or "").strip(),
        doi=doi,
        abstract=reconstruct_abstract(w.get("abstract_inverted_index")),
        journal=src.get("display_name") or "",
        pub_date=w.get("publication_date") or "",
        url=(f"https://doi.org/{doi}" if doi else (loc.get("landing_page_url") or w.get("id") or "")),
        license=(loc.get("license") or ""),
        authors=authors,
        affiliations=list(dict.fromkeys(a for a in affs if a)),
        cited_by_count=int(w.get("cited_by_count") or 0),
        topics=topics,
        extra={
            "openalex_id": oa_id,
            "primary_topic": ((w.get("primary_topic") or {}).get("id") or "").rsplit("/", 1)[-1],
            "company_institutions": sorted({i["name"] for a in authors for i in a.institutions if (i.get("type") == "company" and i.get("name"))}),
            "grants": grants,
        },
    )
    if grants:
        p.sections["funding"] = "; ".join(grants)
    return p


def or_join(values: Iterable[str]) -> str:
    return "|".join(v for v in values if v)


# ---- institution corroboration ------------------------------------------------------------------------------------
# OpenAlex resolves raw affiliation strings to ROR institutions and the step is noisy: an ORCID URL has been typed as
# a social-media company, "Mivax Therapeutics" resolved to the unrelated "Mabvax Therapeutics", "Immunology Discovery
# Research, AbbVie ..." to "Discovery Laboratories". Before a company-typed institution becomes an affiliation mention
# (Pipeline.extract), the author's own affiliation text has to support the resolved name. These tokens carry no
# identity and never count as support: legal forms, sector words, generic English, and the country OpenAlex appends
# to display names ("Illumina (United States)", which is stripped before tokenising).
GENERIC_INSTITUTION_TOKENS = frozenset({
    # legal forms
    "inc", "incorporated", "ltd", "llc", "llp", "corp", "corporation", "company", "gmbh", "plc", "pty", "pte", "spa", "srl",
    "limited", "holdings", "holding", "group",
    # sector words
    "technologies", "technology", "tech", "therapeutics", "pharmaceuticals", "pharmaceutical", "pharma", "biopharma",
    "biopharmaceutical", "biopharmaceuticals", "biosciences", "bioscience", "biotech", "biotechnology", "biotechnologies",
    "bio", "biologics", "biomedical", "laboratories", "laboratory", "labs", "lab", "research", "sciences", "science",
    "scientific", "medical", "medicine", "health", "healthcare", "systems", "solutions", "diagnostics", "genomics",
    "genetics", "life", "institute", "institutes", "center", "centre", "discovery", "development", "innovation",
    "innovations", "services", "industries", "industrial", "energy", "digital",
    # generic English and geography that survives the parenthetical strip
    "the", "and", "for", "new", "applied", "advanced", "general", "international", "global", "national", "american",
    "european", "united", "states", "kingdom", "usa", "china",
})
_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")
_INSTITUTION_TOKEN = re.compile(r"[a-z][a-z0-9]*|\d+[a-z][a-z0-9]*")


def institution_tokens(name: str) -> List[str]:
    """Distinctive lower-case tokens of an OpenAlex institution display name: the trailing "(Country)" is dropped, so
    are generic tokens and anything shorter than three characters. "Mabvax Therapeutics (United States)" -> ["mabvax"];
    "Thermo Fisher Scientific (United States)" -> ["thermo", "fisher"]; "Discovery Laboratories (United States)" -> []."""
    core = _TRAILING_PARENTHETICAL.sub("", name or "").lower()
    return [t for t in _INSTITUTION_TOKEN.findall(core) if len(t) >= 3 and t not in GENERIC_INSTITUTION_TOKENS]


def affiliation_corroborates(institution: str, affiliations: Iterable[Optional[str]]) -> bool:
    """True when the raw affiliation strings support OpenAlex's resolution of them to `institution`.

    One distinctive token of the institution name has to start a word in the raw text: "4Genentech, South San
    Francisco" supports "Genentech" (the superscript glued to the name is not a letter), "Amazon Web Services"
    supports "Amazon (United States)", "Thermo Fisher, Waltham" supports "Thermo Fisher Scientific (United States)".
    A name without a distinctive token ("Discovery Laboratories") must appear whole. Empty raw strings leave nothing to
    check against, and the institution is accepted as typed."""
    raws = [r for r in affiliations if r]
    if not raws:
        return True
    low = "\n".join(raws).lower()
    tokens = institution_tokens(institution)
    if tokens:
        return any(re.search(r"(?<![a-z])" + re.escape(t), low) for t in tokens)
    words = re.findall(r"[a-z0-9]+", _TRAILING_PARENTHETICAL.sub("", institution or "").lower())
    if not words:
        return False
    pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(re.escape(w) for w in words) + r"(?![a-z0-9])"
    return re.search(pattern, low) is not None
