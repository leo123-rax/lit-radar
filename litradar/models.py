"""Plain dataclasses shared by sources, extractors, the store and the digest."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)


def normalize_doi(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    m = DOI_RE.search(doi)
    if not m:
        return None
    return m.group(0).rstrip(".,;)").lower()


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
