"""What kind of record a stored paper really is: a research paper, or something that only looks like one.

OpenAlex topic and author feeds return everything the publisher registered a DOI for: dataset and software
deposits (Zenodo, figshare, MassIVE, PRIDE, Dryad ...), supplementary files, dissertations, peer-review reports,
front matter, editorials and letters to the editor. Crossref journal feeds carry correspondence and editorials
under the journal's name. None of these is a research paper, yet every one of them arrives with a venue, a
title and often a tracked author or a platform name, so the ranker and the search index would otherwise treat
them as papers (a NEJM letter inherits NEJM's tier-A prior; a Zenodo dataset deposited 24 times fills the first
page of a search).

`classify()` decides from what is already stored, in this order: the OpenAlex `work_type` (written to
`extra_json` by the backfill), the DOI prefix (repositories mint under their own prefixes; NEJM codes letters
as `nejmc`, editorials as `nejme`, perspectives as `nejmp`), the venue name, then title and abstract cues
("To the Editor:", "Reply to ...", "Supplementary Figure 2 from ...", "MassIVE MSV...", "github.com/..."). On a
general repository (Zenodo, figshare) an explicit `work_type` of ``preprint`` is trusted and the record stays a
paper; a data, software or protocol repository (MassIVE, PRIDE, Dryad, Code Ocean, protocols.io ...) never
holds a research paper, whatever OpenAlex calls the record.

`sql_predicate()` is the same rule as one SQL expression over the `papers` table (used by `Store.search` to put
research papers first); the two are built from the same constants so they cannot drift apart.

Stdlib only; nothing here reads the database.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# OpenAlex `type` values that are never research papers -> label
NON_RESEARCH_WORK_TYPES: Dict[str, str] = {
    "dataset": "dataset",
    "software": "software",
    "supplementary-materials": "supplement",
    "dissertation": "dissertation",
    "peer-review": "peer_review",
    "paratext": "paratext",
    "editorial": "editorial",
    "letter": "letter",
    "erratum": "notice",
    "retraction": "notice",
}

# DOI prefixes of repositories (data, software, protocols, general deposits) -> label. OSF (10.17605) is left out on
# purpose: it hosts preprints as well as deposits, so only its work_type decides.
REPOSITORY_DOI_PREFIXES: Dict[str, str] = {
    "10.5281": "deposit",       # Zenodo
    "10.6084": "deposit",       # figshare
    "10.25345": "dataset",      # MassIVE
    "10.6019": "dataset",       # PRIDE / EMPIAR (EBI)
    "10.2210": "dataset",       # wwPDB
    "10.5061": "dataset",       # Dryad
    "10.17632": "dataset",      # Mendeley Data
    "10.7910": "dataset",       # Harvard Dataverse
    "10.60586": "dataset",      # SenNet
    "10.35079": "dataset",      # HuBMAP
    "10.57760": "dataset",      # ScienceDB
    "10.26275": "dataset",      # Pennsieve Discover
    "10.24433": "software",     # Code Ocean
    "10.82595": "software",     # SciLifeLab Serve
    "10.17504": "protocol",     # protocols.io
}

# venue (OpenAlex source display name) substrings, lower-case -> label; checked after the DOI prefix
REPOSITORY_VENUES: Dict[str, str] = {
    "zenodo": "deposit",
    "figshare": "deposit",
    "dryad": "dataset",
    "dataverse": "dataset",
    "mendeley data": "dataset",
    "massive": "dataset",
    "worldwide protein data bank": "dataset",
    "empiar": "dataset",
    "pennsieve": "dataset",
    "sciencedb": "dataset",
    "brain image library": "dataset",
    "code ocean": "software",
    "protocols.io": "protocol",
}

# general-purpose repositories host preprints too, so an explicit work_type of `preprint` is trusted there; a data,
# software or protocol repository never holds a research paper, whatever OpenAlex calls the record
GENERAL_REPOSITORY_LABELS = frozenset({"deposit"})

# NEJM article-type codes inside the DOI
NEJM_DOI_TYPES: Dict[str, str] = {"10.1056/nejmc": "letter", "10.1056/nejme": "editorial", "10.1056/nejmp": "editorial"}

LETTER_ABSTRACT_RE = re.compile(r"^\s*(?:to the editors?|dear editor|sir|madam)\b[\s:,—-]", re.I)
LETTER_TITLE_RE = re.compile(
    r"^\s*(?:(?:reply|response|in reply|authors'? reply|in response) to\b|comment(?:ary)? on\b|correspondence\b|letter to the editor|letter:)",
    re.I,
)
EDITORIAL_TITLE_RE = re.compile(r"^\s*editorial\s*[:—–-]", re.I)
SUPPLEMENT_TITLE_RE = re.compile(r"^\s*supplementary\s+(?:figure|table|data|material|method|information|file|appendix)s?\b.*\bfrom\b", re.I)
DATASET_TITLE_RE = re.compile(r"^\s*massive\s+msv\d+\b", re.I)
SOFTWARE_TITLE_RE = re.compile(r"^\s*(?:https?://)?(?:www\.)?(?:github|gitlab|bitbucket)\.(?:com|org)/", re.I)

PHRASES: Dict[str, str] = {
    "dataset": "dataset deposit",
    "software": "software deposit",
    "deposit": "repository deposit",
    "protocol": "protocol deposit",
    "supplement": "supplementary material",
    "dissertation": "dissertation",
    "peer_review": "peer-review report",
    "paratext": "front matter",
    "letter": "letter / correspondence",
    "editorial": "editorial / commentary",
    "notice": "correction / retraction notice",
}


@dataclass(frozen=True)
class RecordType:
    """Why a record is not a research paper: `label` (a PHRASES key) and `evidence` (which rule fired)."""

    label: str
    evidence: str

    @property
    def phrase(self) -> str:
        return PHRASES.get(self.label, self.label)


def _doi_prefix(doi: str) -> str:
    doi = (doi or "").strip().lower()
    return doi.split("/", 1)[0] if "/" in doi else ""


def classify(title: str = "", abstract: str = "", doi: str = "", journal: str = "", work_type: str = "") -> Optional[RecordType]:
    """`RecordType` when the record is a deposit, letter, editorial, notice ... ; None for a research paper."""
    wt = (work_type or "").strip().lower()
    label = NON_RESEARCH_WORK_TYPES.get(wt)
    if label:
        return RecordType(label, f"work_type {wt}")
    doi = (doi or "").strip().lower()
    for code, lab in NEJM_DOI_TYPES.items():
        if doi.startswith(code):
            return RecordType(lab, f"doi {code}")
    prefix = _doi_prefix(doi)
    lab = REPOSITORY_DOI_PREFIXES.get(prefix)
    if lab and not (wt == "preprint" and lab in GENERAL_REPOSITORY_LABELS):
        return RecordType(lab, f"doi prefix {prefix}")
    venue = (journal or "").lower()
    for needle, lab in REPOSITORY_VENUES.items():
        if needle in venue and not (wt == "preprint" and lab in GENERAL_REPOSITORY_LABELS):
            return RecordType(lab, f"venue {needle}")
    title = title or ""
    if DATASET_TITLE_RE.search(title):
        return RecordType("dataset", "title")
    if SOFTWARE_TITLE_RE.search(title):
        return RecordType("software", "title")
    if SUPPLEMENT_TITLE_RE.search(title):
        return RecordType("supplement", "title")
    if LETTER_TITLE_RE.search(title) or LETTER_ABSTRACT_RE.search(abstract or ""):
        return RecordType("letter", "title" if LETTER_TITLE_RE.search(title) else "abstract")
    if EDITORIAL_TITLE_RE.search(title):
        return RecordType("editorial", "title")
    return None


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_predicate(alias: str = "p") -> str:
    """A 0/1 SQL expression over a `papers` row (`alias`) that is 1 for the records `classify` would flag.

    The same rules, written for the query planner: the `work_type` list, the repository DOI prefixes and venue
    substrings (both skipped when work_type is `preprint`), the title cues and the letter cues. Title cues are
    LIKE patterns, so they are a little narrower than the regular expressions (`github.com/...` without a scheme,
    `Supplementary ... from ...`); the Python side is the reference."""
    a = alias
    wt = f"lower(coalesce(json_extract({a}.extra_json, '$.work_type'), ''))"
    doi = f"lower(coalesce({a}.doi, ''))"
    venue = f"lower(coalesce({a}.journal, ''))"
    title = f"lower(coalesce({a}.title, ''))"
    abstract = f"lower(coalesce({a}.abstract, ''))"
    types = ", ".join(_sql_str(t) for t in NON_RESEARCH_WORK_TYPES)
    nejm = " OR ".join(f"{doi} LIKE {_sql_str(code + '%')}" for code in NEJM_DOI_TYPES)
    general = " OR ".join(
        [f"{doi} LIKE {_sql_str(p + '/%')}" for p, lab in REPOSITORY_DOI_PREFIXES.items() if lab in GENERAL_REPOSITORY_LABELS]
        + [f"{venue} LIKE {_sql_str('%' + v + '%')}" for v, lab in REPOSITORY_VENUES.items() if lab in GENERAL_REPOSITORY_LABELS]
    )
    specific = " OR ".join(
        [f"{doi} LIKE {_sql_str(p + '/%')}" for p, lab in REPOSITORY_DOI_PREFIXES.items() if lab not in GENERAL_REPOSITORY_LABELS]
        + [f"{venue} LIKE {_sql_str('%' + v + '%')}" for v, lab in REPOSITORY_VENUES.items() if lab not in GENERAL_REPOSITORY_LABELS]
    )
    titles = " OR ".join(
        f"{title} LIKE {_sql_str(pat)}"
        for pat in ("massive msv%", "github.com/%", "https://github.com/%", "http://github.com/%", "gitlab.com/%", "https://gitlab.com/%",
                    "supplementary % from %", "reply to%", "response to%", "in reply to%", "authors' reply to%", "comment on%",
                    "commentary on%", "correspondence%", "letter to the editor%", "letter:%", "editorial:%", "editorial -%", "editorial —%")
    )
    letters = f"{abstract} LIKE 'to the editor%' OR {abstract} LIKE 'dear editor%'"
    return (
        f"(CASE WHEN {wt} IN ({types}) OR {nejm} OR {specific} OR ({wt} <> 'preprint' AND ({general})) OR {titles} OR {letters} "
        f"THEN 1 ELSE 0 END)"
    )


def record_type_of(row: Dict[str, object]) -> Optional[RecordType]:
    """`classify` over a `papers` row dict (`title`, `abstract`, `doi`, `journal`, and `work_type` or `extra_json`)."""
    wt = row.get("work_type")
    if wt is None and row.get("extra_json"):
        try:
            ex = json.loads(str(row.get("extra_json") or "{}"))
            wt = ex.get("work_type") if isinstance(ex, dict) else None
        except ValueError:
            wt = None
    return classify(str(row.get("title") or ""), str(row.get("abstract") or ""), str(row.get("doi") or ""),
                    str(row.get("journal") or ""), str(wt or ""))


__all__: Tuple[str, ...] = ("RecordType", "classify", "sql_predicate", "record_type_of", "PHRASES")
