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
        nd = normalize_doi(doi)
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
