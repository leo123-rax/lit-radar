"""Plain dataclasses shared by sources, extractors, the store and the digest."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)


def normalize_doi(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    m = DOI_RE.search(doi)
    if not m:
        return None
    return m.group(0).rstrip(".,;)").lower()


# Version suffixes that publishers mint as separate DOIs for one work. Each variant resolves on its own, so
# `Paper.doi` keeps the DOI a source gave; only the row key (`Paper.make_id`) folds them, so one work is one row.
#   eLife reviewed preprints: 10.7554/elife.108336.1, .2, .3 -> version of record 10.7554/elife.108336
#     (a single digit only: the three-digit 10.7554/elife.00003.001 form is a figure / component DOI, left alone)
#   Research Square:          10.21203/rs.3.rs-1234567/v1, /v2 -> 10.21203/rs.3.rs-1234567
_DOI_VERSION_SUFFIXES = (
    re.compile(r"^(10\.7554/elife\.\d{5,6})\.\d$"),
    re.compile(r"^(10\.21203/rs\.3\.rs-\d+)/v\d+$"),
)


def canonical_doi(doi: Optional[str]) -> Optional[str]:
    """`normalize_doi` plus the version-suffix folding above; the DOI form used to key `papers` rows."""
    nd = normalize_doi(doi)
    if not nd:
        return None
    for rx in _DOI_VERSION_SUFFIXES:
        m = rx.match(nd)
        if m:
            return m.group(1)
    return nd


# OpenAlex `type` values stored as papers (docs/DESIGN.md: "Always add type:article|preprint to avoid dataset deposits
# polluting leaderboards"; reviews are papers too). Everything else OpenAlex types - dataset, supplementary-materials,
# other, paratext, peer-review, software, dissertation, erratum, book-chapter, conference-abstract ... - is not a paper.
PAPER_WORK_TYPES = ("article", "preprint", "review")

# Component records that publisher portals mint one DOI per figure / table / data file for, titled
# "Figure S5 from <article>", "Table 2 from <article>", "Supplementary material from "<article>"", "Data from <article>".
# Some arrive typed `review` or `article`, so the title is checked as well as the type.
_COMPONENT_TITLE_RE = re.compile(
    r"^(?:supplementa(?:ry|l)\s+)?"
    r"(?:figures?|figs?\.?|tables?|data|datasets?|videos?|movies?|images?|files?|materials?|methods?|information|appendix|appendices)"
    r"(?:"
    r"\s+S?\d+[a-z]?(?:\s*[-\u2013]\s*S?\d+)?\.?\s+from\s+\S"  # numbered: "Figure S5 from ...", "Table 2 from ...", "Figures S1-S51 from ..."
    r"|\s+S?\d+[a-z]?\.\s+.{0,200}?\s+from\s+\S"  # "Table S1. <caption> from <article>"
    r"|\s+from\s+(?-i:[\"\u201c'A-Z])"  # bare label: "Data from <Title Case article>", "Supplementary material from \"...\""
    r")",
    re.I | re.S,
)


def is_component_title(title: Optional[str]) -> bool:
    """True for a figure / table / supplement / data-file record of an article (see `_COMPONENT_TITLE_RE`)."""
    return bool(_COMPONENT_TITLE_RE.match((title or "").strip()))


def is_paper_record(work_type: Optional[str], title: Optional[str] = None) -> bool:
    """Is a record a paper (article, preprint or review) rather than a dataset, supplement, software deposit ...?

    `work_type` is the OpenAlex `type` (``extra_json.work_type``); an empty / unknown value is accepted because
    sources other than OpenAlex do not record one. A component title (`is_component_title`) is never a paper."""
    wt = (work_type or "").strip().lower()
    if wt and wt not in PAPER_WORK_TYPES:
        return False
    return not is_component_title(title)

# DOI shapes minted by preprint servers. bioRxiv / medRxiv (10.1101, 10.64898) share the CSHL prefix with Genome
# Research, so the date-stamped (2026.10.05.756623) or plain numeric tail is required; the others carry a server
# token after the prefix (arXiv, Research Square, SSRN, Preprints.org, ChemRxiv, Authorea, ChinaXiv, OSF, TechRxiv,
# PeerJ Preprints, SciELO Preprints). Data repositories (Figshare 10.6084, Zenodo 10.5281, Dryad, Dataverse, wwPDB,
# EMPIAR) never match.
PREPRINT_DOI_RE = re.compile(
    r"^(?:10\.(?:1101|64898)/(?:\d{4}\.\d{2}\.\d{2}\.)?\d{6,8}(?:v\d+)?$"
    r"|10\.48550/arxiv\.|10\.21203/rs\.|10\.2139/ssrn\.|10\.20944/preprints|10\.26434/chemrxiv|10\.22541/"
    r"|10\.12074/|10\.31219/osf\.io|10\.31234/osf\.io|10\.36227/techrxiv|10\.7287/peerj\.preprints|10\.1590/scielopreprints)",
    re.I,
)
# venue / source names that identify a preprint server (substring match, lower case)
PREPRINT_SERVER_NAMES = (
    "biorxiv", "medrxiv", "arxiv", "chinaxiv", "research square", "ssrn", "preprints.org", "chemrxiv", "authorea",
    "scielo preprints", "osf preprints", "psyarxiv", "techrxiv", "peerj preprints",
)
_POSTING_DATE_RE = re.compile(r"^10\.(?:1101|64898)/(\d{4})\.(\d{2})\.(\d{2})\.\d+", re.I)
_PREPRINT_SERVER_BY_DOI = (
    (re.compile(r"^10\.(?:1101|64898)/", re.I), "bioRxiv or medRxiv"),
    (re.compile(r"^10\.48550/", re.I), "arXiv"),
    (re.compile(r"^10\.21203/", re.I), "Research Square"),
    (re.compile(r"^10\.2139/", re.I), "SSRN"),
    (re.compile(r"^10\.20944/", re.I), "Preprints.org"),
    (re.compile(r"^10\.26434/", re.I), "ChemRxiv"),
    (re.compile(r"^10\.22541/", re.I), "Authorea"),
    (re.compile(r"^10\.12074/", re.I), "ChinaXiv"),
    (re.compile(r"^10\.31219/", re.I), "OSF Preprints"),
    (re.compile(r"^10\.31234/", re.I), "PsyArXiv"),
    (re.compile(r"^10\.36227/", re.I), "TechRxiv"),
    (re.compile(r"^10\.7287/", re.I), "PeerJ Preprints"),
    (re.compile(r"^10\.1590/scielopreprints", re.I), "SciELO Preprints"),
)


def is_preprint_doi(doi: Optional[str]) -> bool:
    """True when the DOI was minted by a preprint server (see PREPRINT_DOI_RE)."""
    return bool(PREPRINT_DOI_RE.match((doi or "").strip().lower()))


def is_preprint_server(name: Optional[str]) -> bool:
    """True when a venue, publisher or source name is a known preprint server."""
    low = (name or "").strip().lower()
    return bool(low) and any(s in low for s in PREPRINT_SERVER_NAMES)


def preprint_server_from_doi(doi: Optional[str]) -> str:
    """The preprint server a DOI was minted by ('' when the DOI is not a preprint server's), for records whose
    venue field is blank (OpenAlex often leaves it so for repository deposits)."""
    d = (doi or "").strip()
    if not is_preprint_doi(d):
        return ""
    for pattern, name in _PREPRINT_SERVER_BY_DOI:
        if pattern.match(d):
            return name
    return ""


def preprint_posting_date(doi: Optional[str]) -> str:
    """The first-posting date a bioRxiv / medRxiv DOI encodes (10.1101/2025.12.05.691730 -> 2025-12-05), else ''.

    The API's per-record `date` is the date of the *version* listed, so a revised preprint fetched by a weekly
    pull carries the revision date; the DOI keeps the original posting date."""
    m = _POSTING_DATE_RE.match((doi or "").strip())
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return ""


@dataclass
class Author:
    name: str
    affiliation: str = ""
    orcid: str = ""
    openalex_id: str = ""
    email: str = ""
    institutions: List[Dict[str, Any]] = field(default_factory=list)  # [{name, type, ror}]


@dataclass
class Paper:
    id: str  # canonical id: 'doi:<doi>' when a DOI exists, else '<source>:<native id>'
    source: str  # biorxiv | medrxiv | europepmc | openalex | crossref | arxiv | rss:<feed>
    title: str
    doi: Optional[str] = None
    abstract: str = ""
    journal: str = ""
    pub_date: str = ""  # YYYY-MM-DD
    url: str = ""
    license: str = ""
    category: str = ""
    kind: str = "article"  # article | preprint
    published_doi: Optional[str] = None  # for preprints that later appeared in a journal
    authors: List[Author] = field(default_factory=list)
    affiliations: List[str] = field(default_factory=list)
    sections: Dict[str, str] = field(default_factory=dict)  # methods, funding, coi, ack, data_availability
    fulltext_url: str = ""
    fulltext_available: bool = False
    cited_by_count: int = 0
    topics: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def make_id(source: str, native_id: str, doi: Optional[str]) -> str:
        """'doi:<canonical doi>' when a DOI exists (version suffixes folded, see `canonical_doi`), else
        '<source>:<native id>'."""
        nd = canonical_doi(doi)
        return f"doi:{nd}" if nd else f"{source}:{native_id}"

    def text_for(self, section: str) -> str:
        if section == "title":
            return self.title or ""
        if section == "abstract":
            return self.abstract or ""
        if section == "affiliations":
            return "\n".join(self.affiliations)
        return self.sections.get(section, "")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Mention:
    paper_id: str
    kind: str  # instrument | company
    entity_id: str
    entity_name: str
    vendor: str = ""
    ticker: str = ""
    category: str = ""
    section: str = ""  # title | abstract | methods | funding | coi | ack | affiliations
    count: int = 1
    snippet: str = ""
    role: str = ""  # used | reanalysis | mentioned; '' = let the store derive it from section + snippet (store.mention_role)


@dataclass
class ArchiveRecord:
    archive: str  # ena | sra | geo | pride
    accession: str
    study_accession: str = ""
    instrument_model: str = ""
    platform: str = ""
    library_strategy: str = ""
    first_public: str = ""
    center_name: str = ""
    study_title: str = ""
    organism: str = ""
    entity_id: str = ""  # matched gazetteer id
    vendor: str = ""
    ticker: str = ""


@dataclass
class NewsItem:
    id: str
    feed: str
    title: str
    url: str
    published: str = ""
    summary: str = ""
    companies: List[str] = field(default_factory=list)
