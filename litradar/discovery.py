"""Company discovery: surface companies the gazetteer does not know yet, with evidence.

The gazetteer (config/companies.yaml) only finds companies somebody already listed. This module
looks at everything the store already holds for names that *behave* like companies and are not
listed, so the watchlist grows from the literature instead of from memory.

Signals (each yields Evidence(name, evidence_type, snippet, ref)):

* ``email_domain``      author e-mail domains that are not academic / government / public-mail;
                        the display name comes from the affiliation that carries the domain's brand
                        token, else the domain itself.
* ``coi`` / ``funding`` / ``ack``
                        "employee of X", "founder of X", "consultant for X", "equity in X",
                        "advisory board of X", "funded by X", "supported by X", "grant from X",
                        "sponsored by X", "collaboration with X" in competing-interest, funding and
                        acknowledgement statements. X must be a capitalised name; a corporate suffix
                        (Inc, Ltd, GmbH, Therapeutics, Biosciences, ...) makes the hit confident,
                        a bare 1-3 word name is kept at low confidence.
* ``openalex_company``  OpenAlex company-typed institutions the pipeline already stored as
                        ``unlisted:`` mentions.
* ``news:nih-reporter`` SBIR/STTR awardee organisations (and any awardee with a corporate suffix).
* ``news:clinicaltrials`` industry-class trial sponsors and collaborators with corporate suffixes.
* ``news:fda``          510(k) / De Novo / PMA applicants.
* ``news:press``        financing / launch / acquisition headlines next to sector words.

Everything that matches the existing gazetteer (``Gazetteer.find`` on the name, ``match_domains``
on the e-mail domain) or a generic institution word (University, Institute, Hospital, Foundation,
Council, Ministry, Society, Trust, Fund, Program, ...) is dropped.

Candidates are grouped by a normalised key (lower-case, legal suffixes and punctuation removed),
scored, and kept in their own table ``candidate_companies`` with a status workflow
``new -> ignored | promoted``. No other table is touched.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_companies (
  name_norm TEXT PRIMARY KEY,
  display_name TEXT,
  evidence_json TEXT,
  first_seen TEXT,
  last_seen TEXT,
  papers INTEGER DEFAULT 0,
  score REAL DEFAULT 0,
  status TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_candidate_companies_status ON candidate_companies(status, score);
"""

STATUSES = ("new", "ignored", "promoted")
SNIPPET_RADIUS = 90
MAX_EVIDENCE_STORED = 30

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_CACHE_MAX_AGE_DAYS = 30

# ------------------------------------------------------------------------------------------------
# vocabularies
# ------------------------------------------------------------------------------------------------

# legal forms: stripped when normalising, counted as a corporate suffix when present
LEGAL_SUFFIXES = {
    "inc", "incorporated", "ltd", "limited", "llc", "gmbh", "ag", "sa", "co", "corp", "corporation",
    "company", "plc", "pty", "pvt", "bv", "nv", "ab", "oy", "oyj", "spa", "srl", "kk", "se", "lp", "llp",
    "sas", "sarl", "pte", "tbk", "aps", "ltda", "sl", "sdn", "bhd", "ug", "kg", "mbh", "as", "a/s",
}
# sector words that end company names; counted as a corporate suffix, kept in the normalised key
SECTOR_SUFFIXES = {
    "biosciences", "bioscience", "biotechnologies", "biotechnology", "biotech", "therapeutics",
    "pharmaceuticals", "pharmaceutical", "pharma", "diagnostics", "genomics", "bio", "labs",
    "laboratories", "technologies", "technology", "biologics", "biomedicines", "biomedicine",
    "bioinformatics", "biopharma", "biopharmaceuticals", "medicines", "oncology", "ventures", "lifesciences",
    "lifescience", "biolabs", "biomedical",
}
CORPORATE_SUFFIXES = LEGAL_SUFFIXES | SECTOR_SUFFIXES

# a name containing any of these words is an institution, funder or programme, not a company
GENERIC_WORDS = {
    "university", "universities", "universitat", "universität", "universite", "université", "universidad",
    "universidade", "universita", "università", "universiteit", "universitaet", "institute", "institutes",
    "institution", "institut", "instituto", "istituto", "hospital", "hospitals", "hopital", "hôpital",
    "clinic", "clinics", "clinique", "klinik", "klinikum", "foundation", "foundations", "fondation",
    "fundacion", "fundación", "fundacao", "fundação", "fondazione", "stiftung", "stichting", "council",
    "ministry", "ministère", "ministerio", "society", "trust", "fund", "funds", "program", "programme",
    "programs", "programmes", "department", "dept", "college", "school", "faculty", "academy", "academia",
    "akademie", "agency", "agence", "association", "federation", "government", "federal", "national",
    "centre", "center", "centro", "grant", "grants", "award", "awards", "fellowship", "scholarship",
    "initiative", "consortium", "network", "union", "commission", "office", "authority", "bureau",
    "administration", "philanthropy", "charity", "charitable", "forschungsgemeinschaft", "gemeinschaft",
    "laboratory", "laboratoire", "campus", "museum", "library", "cnrs", "inserm", "inrae", "children's",
    "childrens", "health", "healthcare", "system", "systems", "project", "projects", "plan", "scheme",
    "municipal", "provincial", "province", "state", "states", "action", "committee", "committees",
    "scholarships", "fellowships", "councils", "ministries", "departments", "colleges", "schools", "academies",
    "agencies", "associations", "centres", "centers", "societies", "trusts", "initiatives", "networks",
    "commissions", "offices", "authorities", "bureaus", "fonds", "fondo", "fondet", "fonden", "stiftelsen",
    "stiftelse", "gesellschaft", "endowment", "hub", "senate", "secretary", "deanship", "zoo", "consultant",
    "partnership", "resource", "chair", "professorship", "generalitat", "xunta", "junta", "region", "regional",
}
# single-token names that are generic on their own
GENERIC_SINGLE = {"company", "sponsor", "sponsors", "funder", "funders", "authors", "author", "study", "firm"}
# funder / agency acronyms that look like 1-word company names
GENERIC_ACRONYMS = {
    "nih", "nsf", "nci", "nigms", "nhgri", "niaid", "nhlbi", "niddk", "ninds", "nimh", "nia", "ncats",
    "nibib", "nichd", "nei", "dfg", "erc", "eu", "embo", "cihr", "nserc", "anr", "mrc", "bbsrc", "epsrc",
    "ukri", "nhmrc", "arc", "jsps", "mext", "amed", "kakenhi", "nsfc", "nnsfc", "cas", "doe", "darpa",
    "nasa", "cdc", "fda", "ema", "who", "nhs", "csic", "cnpq", "capes", "fapesp", "conicet", "conacyt",
    "dst", "dbt", "icmr", "snsf", "snf", "fwf", "fwo", "fnrs", "nwo", "zonmw", "vr", "formas", "vinnova",
    "dnrf", "hhmi", "czi", "bmbf", "ec", "hfsp", "isf", "nrf", "most", "moe", "krf", "usda", "va", "dod",
    "onr", "afosr", "arpa-h", "barda", "sfi", "hrb", "irc", "un", "unesco", "mohw", "frqnt", "frqs", "frq",
    "faperj", "fapemig", "colciencias", "minciencias", "ciber", "isciii", "mcin", "mciu", "miciu", "aei", "airc",
    "dtra", "twas", "cbe", "mitacs", "vanier", "prin", "sfb", "trr", "gacr", "ncn", "nkfih", "otka", "tubitak",
    "sshrc", "cfi", "neh", "iarpa", "nist", "noaa", "epa", "usaid", "eit", "efsa", "ecdc", "nextgenerationeu",
    "nextgen", "carlsbergfondet", "studienstiftung", "cancer", "dfg-funded",
}
GENERIC_PHRASES = (
    "research council", "research foundation", "research uk", "research fund", "research agency",
    "research institute", "medical center", "medical centre", "cancer center", "cancer centre",
    "health service", "national science", "wellcome", "max planck", "helmholtz", "fraunhofer",
    "leibniz", "royal society", "academy of sciences", "research hospital", "of medicine", "medical school",
    "health system", "cancer society", "heart association", "core facility", "spectrometry core", "genomics core",
    "sequencing core", "proteomics core", "imaging core", "core laboratory", "core resource", "innovation hub",
    "graduate scholarship", "research chair",
)

PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "ymail.com", "163.com", "126.com", "qq.com", "foxmail.com", "sina.com", "sina.cn", "sohu.com",
    "263.net", "189.cn", "139.com", "yeah.net", "aliyun.com", "tom.com", "protonmail.com", "proton.me",
    "pm.me", "icloud.com", "me.com", "mac.com", "aol.com", "mail.com", "gmx.de", "gmx.net", "gmx.com",
    "gmx.at", "gmx.ch", "web.de", "t-online.de", "yandex.ru", "yandex.com", "mail.ru", "naver.com",
    "daum.net", "hanmail.net", "windowslive.com", "rediffmail.com", "zoho.com", "fastmail.com", "hey.com",
    "hotmail.co.uk", "hotmail.fr", "hotmail.de", "hotmail.it", "yahoo.co.uk", "yahoo.de", "yahoo.fr",
    "yahoo.co.jp", "yahoo.com.br", "outlook.de", "outlook.fr", "live.fr", "live.cn", "live.co.uk",
}
PUBLIC_MAIL_BRANDS = {"gmail", "hotmail", "outlook", "yahoo", "live", "icloud", "protonmail", "gmx"}

# scholarly publishers and preprint servers: companies, but not the kind this index tracks
PUBLISHER_DOMAINS = {
    "oup.com", "elsevier.com", "wiley.com", "springer.com", "springernature.com", "nature.com", "cell.com",
    "plos.org", "biorxiv.org", "medrxiv.org", "frontiersin.org", "mdpi.com", "hindawi.com", "sagepub.com",
    "tandf.co.uk", "tandfonline.com", "bmj.com", "jamanetwork.com", "acs.org", "rsc.org", "aaas.org",
    "science.org", "sciencemag.org", "karger.com", "karger.ch", "thieme.de", "degruyter.com",
    "cshlpress.com", "embopress.org", "rupress.org", "asm.org", "pnas.org", "jci.org", "elifesciences.org",
    "researchsquare.com", "ssrn.com", "f1000.com", "wolterskluwer.com", "lww.com", "iop.org", "ieee.org",
    "acm.org", "emerald.com", "cambridge.org", "benthamscience.com", "dovepress.com", "scielo.br",
    "copernicus.org", "biomedcentral.com", "springeropen.com", "jmir.org", "peerj.com",
}

COMMERCIAL_TLDS = {
    "com", "net", "biz", "io", "ai", "bio", "co", "inc", "health", "tech", "pharma", "life", "science",
    "app", "dev", "cloud", "global", "group", "ltd", "llc", "gmbh", "company", "care", "md", "rx", "ly",
}
EXCLUDED_TLDS = {"edu", "gov", "mil", "int", "org"}
# second-level labels under a country code: academic / government / non-profit on the left,
# commercial on the right
ACADEMIC_SECOND_LEVEL = {"edu", "ac", "gov", "org", "or", "go", "res", "sch", "nhs", "mil", "gob", "gouv", "govt"}
COMMERCIAL_SECOND_LEVEL = {"com", "co", "ne", "biz", "firm", "ltd"}
ACADEMIC_LABEL_TOKENS = (
    "univ", "uni-", "hospital", "hosp", "clinic", "klinik", "inserm", "cnrs", "inrae", "pasteur", "csic",
    "fraunhofer", "helmholtz", "charite", "dkfz", "embl", "ethz", "epfl", "school", "college", "institut",
    "academ", "research", "campus", "faculty", "student", "alumni",
)
ACADEMIC_EXACT_LABELS = {"uni", "univ", "med", "edu", "ac", "sci", "res", "gov", "mpg", "mpi", "cas", "caas", "nhs"}

SBIR_STTR_CODES = {"R41", "R42", "R43", "R44", "U43", "U44", "SB1", "UT1", "UT2"}

SECTOR_WORDS_RE = re.compile(
    r"\b(?:sequenc\w*|genom\w*|proteom\w*|diagnos\w*|spatial|single[- ]cell|biotech\w*|multi-?omic\w*|"
    r"transcriptom\w*|liquid biopsy|cell therap\w*|gene therap\w*|crispr|antibod\w*|drug discovery|"
    r"drug development|life[- ]science\w*|lab(?:oratory)? automation|mass spec\w*|cryo-?em|synthetic biology|"
    r"\bdna\b|\brna\b|oncolog\w*|biomarker\w*|assay\w*|pharma\w*|therapeutic\w*|biologic\w*|bioprocess\w*|"
    r"microbiome|molecular|clinical trial\w*|precision medicine|bioinformatic\w*|protein\w*|immunolog\w*)\b",
    re.IGNORECASE,
)

# ------------------------------------------------------------------------------------------------
# data classes
# ------------------------------------------------------------------------------------------------


@dataclass
class Evidence:
    name: str
    evidence_type: str
    snippet: str
    ref: str  # paper id or news item id
    date: str = ""  # YYYY-MM-DD when known
    confident: bool = True
    domain: str = ""
    title: str = ""
    url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    name_norm: str
    display_name: str
    evidence: List[Evidence] = field(default_factory=list)
    status: str = "new"
    score: float = 0.0

    @property
    def refs(self) -> int:
        return len({e.ref for e in self.evidence})

    @property
    def types(self) -> List[str]:
        return sorted({e.evidence_type for e in self.evidence})

    @property
    def first_seen(self) -> str:
        dates = [e.date for e in self.evidence if e.date]
        return min(dates) if dates else ""

    @property
    def last_seen(self) -> str:
        dates = [e.date for e in self.evidence if e.date]
        return max(dates) if dates else ""

    @property
    def domains(self) -> List[str]:
        return sorted({e.domain for e in self.evidence if e.domain})

    @property
    def variants(self) -> List[str]:
        return sorted({e.name for e in self.evidence if e.name and e.name != self.display_name})

    def has_suffix(self) -> bool:
        return any(has_corporate_suffix(e.name) for e in self.evidence)


# ------------------------------------------------------------------------------------------------
# name helpers
# ------------------------------------------------------------------------------------------------


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _words(name: str) -> List[str]:
    cleaned = re.sub(r"[^\w&/]+", " ", _strip_accents(name or "").lower().replace("&", " and "))
    return [w.strip(".") for w in cleaned.split() if w.strip(".")]


def normalize_name(name: str) -> str:
    """Grouping key: lower-case, accents folded, legal suffixes and punctuation removed, no spaces.

    "Costello Medical" and "costellomedical" (an e-mail brand token) share a key; "Acme Therapeutics"
    and "Acme Diagnostics" do not, because sector words are kept.
    """
    raw = (name or "").strip().lower()
    if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,24}", raw) and " " not in raw:
        return _compact(brand_token(raw))  # an e-mail domain groups with its brand
    words = _words(name)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    while len(words) > 1 and words[0] in {"the"}:
        words.pop(0)
    words = [w for w in words if w not in {"and", "the", "of"}] or words
    return "".join(words)


def has_corporate_suffix(name: str) -> bool:
    words = _words(name)
    return any(w in CORPORATE_SUFFIXES for w in words[-2:]) if words else False


def is_generic_name(name: str) -> bool:
    """True for institutions, funders, programmes and other non-company names."""
    words = _words(name)
    while len(words) > 1 and words[0] in {"the", "a", "an"}:
        words.pop(0)
    if not words:
        return True
    if any(w in GENERIC_WORDS for w in words):
        return True
    if len(words) == 1 and words[0] in GENERIC_SINGLE:
        return True
    if len(words) <= 2 and any(w in GENERIC_ACRONYMS for w in words):
        return True  # "NIH", "UK MRC", "US FDA"
    joined = " ".join(words)
    return any(p in joined for p in GENERIC_PHRASES)


def _looks_like_initials(token: str) -> bool:
    return bool(re.fullmatch(r"(?:[A-Z]\.){1,5}[A-Z]?\.?|[A-Z]", token))


def _title_case_caps(name: str) -> str:
    """NIH RePORTER returns ALL-CAPS organisation names; make them readable without mangling acronyms."""
    if not name or name != name.upper() or not re.search(r"[A-Z]", name):
        return name
    out = name.title()
    fixes = {"Llc": "LLC", "Plc": "PLC", "Usa": "USA", "Ai": "AI", "Dna": "DNA", "Rna": "RNA", "Ii": "II",
             "Iii": "III", "Ag": "AG", "Sa": "SA", "Bv": "BV", "Nv": "NV", "Kk": "KK", "Llp": "LLP", "Lp": "LP",
             "Uk": "UK", "Us": "US", "Pcr": "PCR", "Ngs": "NGS", "Md": "MD", "Rx": "RX", "3D": "3D", "Mri": "MRI"}
    return " ".join(fixes.get(w.strip(".,"), w) if w.strip(".,") in fixes else w for w in out.split())


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", _strip_accents(name).lower()).strip("-")
    return s or "candidate"


# ------------------------------------------------------------------------------------------------
# (a) e-mail domains
# ------------------------------------------------------------------------------------------------

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


def registrable_domain(domain: str) -> str:
    """'rd.nestle.com' -> 'nestle.com'; 'x.example.co.uk' -> 'example.co.uk'."""
    labels = domain.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in ACADEMIC_SECOND_LEVEL | COMMERCIAL_SECOND_LEVEL | {"com", "net", "org"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def brand_token(domain: str) -> str:
    return registrable_domain(domain).split(".")[0]


def classify_domain(domain: str) -> str:
    """One of: invalid, public_mail, publisher, academic, excluded_tld, corporate, cctld.

    ``corporate`` = a generic commercial TLD (.com, .io, .bio, .ai, co.uk, com.cn, ...) with no academic
    token in its labels. ``cctld`` = a plain country-code domain (.de, .ch, .fr, ...), which in author
    e-mails is usually a university, so callers require affiliation corroboration before accepting it.
    """
    d = (domain or "").lower().strip().strip(".")
    if not _DOMAIN_RE.match(d):
        return "invalid"
    labels = d.split(".")
    tld = labels[-1]
    reg = registrable_domain(d)
    if reg in PUBLIC_MAIL_DOMAINS or d in PUBLIC_MAIL_DOMAINS or brand_token(d) in PUBLIC_MAIL_BRANDS:
        return "public_mail"
    if reg in PUBLISHER_DOMAINS or d in PUBLISHER_DOMAINS:
        return "publisher"
    if tld in EXCLUDED_TLDS:
        return "excluded_tld"
    if len(tld) == 2 and len(labels) >= 2 and labels[-2] in ACADEMIC_SECOND_LEVEL:
        return "excluded_tld"
    for lab in labels[:-1]:
        if lab in ACADEMIC_EXACT_LABELS or any(tok in lab for tok in ACADEMIC_LABEL_TOKENS):
            return "academic"
        if re.fullmatch(r"[a-z]{3,}lab", lab):  # personal lab vanity domains (smithlab.com)
            return "academic"
    if tld in COMMERCIAL_TLDS:
        return "corporate"
    if len(tld) == 2 and len(labels) >= 3 and labels[-2] in COMMERCIAL_SECOND_LEVEL:
        return "corporate"
    if len(tld) == 2:
        return "cctld"
    return "cctld"


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(s or "").lower())


def _affiliation_segments(affiliation: str) -> List[str]:
    parts = [p.strip(" .;: \n\t") for p in re.split(r"[,;\n]", affiliation or "")]
    out: List[str] = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        # re-attach a bare legal suffix split off by the comma: "Merck & Co., Inc." -> ["Merck & Co.", "Inc."]
        while i + 1 < len(parts) and _words(parts[i + 1]) and all(w in LEGAL_SUFFIXES for w in _words(parts[i + 1])):
            seg = f"{seg}, {parts[i + 1].strip()}"
            i += 1
        if seg:
            out.append(seg)
        i += 1
    return out


def display_name_from_affiliations(domain: str, affiliations: Iterable[str]) -> Optional[str]:
    """The affiliation segment that carries the domain's brand token, e.g. lactiga.com + "Lactiga US Inc."."""
    brand = _compact(brand_token(domain))
    if len(brand) < 3:
        return None
    best: Optional[Tuple[int, int, str]] = None
    for aff in affiliations:
        for seg in _affiliation_segments(aff):
            if brand in _compact(seg) and len(seg) <= 120:
                rank = (0 if has_corporate_suffix(seg) else 1, len(seg), seg)  # legal entity first, then shortest
                if best is None or rank < best:
                    best = rank
    return best[2] if best else None


def email_evidence(paper: Dict[str, Any], gazetteer=None) -> List[Evidence]:
    """Candidates from author e-mail domains of one stored paper row."""
    extra = _loads(paper.get("extra_json"), {})
    authors = _loads(paper.get("authors_json"), [])
    affiliations = _loads(paper.get("affiliations_json"), [])
    emails = [str(e).strip().lower() for e in (extra.get("emails") or []) if e]
    emails += [str(a.get("email")).strip().lower() for a in authors if isinstance(a, dict) and a.get("email")]
    by_author_aff: Dict[str, List[str]] = defaultdict(list)
    for a in authors:
        if isinstance(a, dict) and a.get("email") and a.get("affiliation"):
            by_author_aff[str(a["email"]).strip().lower()].append(str(a["affiliation"]))
    out: List[Evidence] = []
    seen: set = set()
    corporate_affils = [a for a in affiliations if has_corporate_suffix(a) and not is_generic_name(a)]
    for e in emails:
        if "@" not in e:
            continue
        dom = e.split("@")[-1]
        reg = registrable_domain(dom)
        if reg in seen:
            continue
        seen.add(reg)
        if gazetteer is not None and gazetteer.match_domains([e]):
            continue
        kind = classify_domain(dom)
        if kind not in ("corporate", "cctld"):
            continue
        pool = by_author_aff.get(e) or affiliations
        display = display_name_from_affiliations(dom, pool)
        if display is not None:
            if is_generic_name(display):
                continue  # a university / hospital with a vanity .com
            if gazetteer is not None and gazetteer.find(display, ignore_context=True):
                continue
            if kind == "cctld" and not has_corporate_suffix(display):
                continue  # a country-code domain needs a legal-entity affiliation (GmbH, B.V., Ltd, ...)
            confident = True
        else:
            if kind == "cctld":
                continue  # a bare country-code domain with no corroborating affiliation: almost always academic
            display = reg
            confident = bool(corporate_affils)
        aff_note = f"; affiliation: {display}" if display != reg else (f"; corporate affiliation on paper: {corporate_affils[0][:80]}" if corporate_affils else "")
        out.append(Evidence(
            name=display, evidence_type="email_domain", snippet=f"author e-mail @{dom}{aff_note}",
            ref=paper["id"], date=_paper_date(paper), confident=confident, domain=reg,
            title=paper.get("title") or "", url=paper.get("url") or "",
        ))
    return out


# ------------------------------------------------------------------------------------------------
# (b) competing-interest, funding and acknowledgement statements
# ------------------------------------------------------------------------------------------------

_NAME_TOKEN = r"[A-Z0-9][\w&'’.\-/]*"
_CONNECTOR = r"(?:of|and|&|de|del|della|des|du|der|van|von|for|the|da|do|dos|la|le|y|e|zu|en)"
_NAME_SPAN = rf"(?P<name>{_NAME_TOKEN}(?:[  ]+(?:{_CONNECTOR}[  ]+)?{_NAME_TOKEN})*)"
_TRIGGERS = [
    r"employees?\s+(?:of|at)",
    r"employed\s+(?:by|at)",
    r"(?:co-?)?founders?\s+(?:of|and\s+\w+\s+of)",
    r"consultants?\s+(?:for|to|of)",
    r"consult(?:s|ed|ing)\s+for",
    r"(?:holds?|has|have|own|owns|hold)\s+(?:an?\s+)?(?:equity|stock|shares|stock\s+options|equity\s+(?:interests?|stakes?)|ownership\s+interests?|financial\s+interests?)\s+in",
    r"(?:equity|stock|shares|ownership)\s+(?:interests?|stakes?|holdings?)?\s*in",
    r"(?:share|stock)holders?\s+(?:of|in)",
    r"advisory\s+boards?\s+(?:of|for|at)",
    r"(?:funded|financed)\s+(?:in\s+part\s+)?by",
    r"supported\s+(?:in\s+part\s+|partly\s+|partially\s+|financially\s+)?by",
    r"(?:research\s+)?grants?\s+from",
    r"sponsored\s+by",
    r"collaborations?\s+with",
    r"(?:received|receives|receiving)\s+(?:research\s+)?(?:support|funding|grants?)\s+from",
    r"(?:research\s+)?funding\s+from",
]
_STATEMENT_RE = re.compile(rf"(?i:(?:{'|'.join(_TRIGGERS)}))[  ]+(?:(?i:the|a|an)[  ]+)?{_NAME_SPAN}")
_LIST_CONT_RE = re.compile(rf"^(?:,[  ]+(?:and[  ]+)?|[  ]+and[  ]+)(?:(?i:the)[  ]+)?{_NAME_SPAN}")
_TRAILING_LEGAL_RE = re.compile(
    r"^,?[  ]*(?P<suf>(?:Inc|Ltd|LLC|L\.L\.C|GmbH|AG|S\.A|SA|Co|Corp|Corporation|plc|PLC|B\.V|N\.V|Pty|Pvt|Ltda|Pte|S\.p\.A|S\.r\.l|K\.K|A/S|AB|Oy|LP|LLP|Limited|Incorporated)\.?)(?=[\s,;.)]|$)"
)


# legal forms conventionally written with a trailing period ("Inc.", "Co., Ltd."); "LLC." or "Corporation." end a sentence
_DOTTED_ABBREVS = {"inc", "ltd", "co", "corp", "bv", "nv", "sa", "spa", "srl", "kk", "pte", "pty", "pvt", "ltda", "sl", "aps", "ag", "mbh"}
_LEGAL_BARE = {s.replace(".", "") for s in LEGAL_SUFFIXES}


def _is_legal_abbrev(token: str) -> bool:
    bare = token.rstrip(".,;:").lower().replace(".", "")
    if token.endswith("."):
        return bare in _DOTTED_ABBREVS
    return bare in _LEGAL_BARE


def _rstrip_name(s: str) -> str:
    """Drop trailing punctuation but keep the period of a legal abbreviation ("Acme Inc." stays, "Scientific." does not)."""
    s = s.rstrip(",;: ")
    if s.endswith(".") and not _is_legal_abbrev(s.split()[-1]):
        s = s.rstrip(". ")
    return s


def _trim_name_tokens(span: str) -> Tuple[str, bool]:
    """Cut a captured span to one company name. Returns (name, confident); ('', False) when it is not one."""
    tokens = span.split()
    run: List[str] = []  # tokens up to the sentence boundary
    for tok in tokens:
        if _looks_like_initials(tok):
            break
        run.append(tok)
        if tok.endswith(".") and not _is_legal_abbrev(tok):
            break
    if not run:
        return "", False

    def _first_suffix(tokens: List[str]) -> Optional[int]:
        return next((i for i, tok in enumerate(tokens)
                     if i > 0 and tok.strip(".,;:").lower() in CORPORATE_SUFFIXES and tok.lower() not in _CONNECTOR_SET), None)

    full_generic = is_generic_name(" ".join(run))  # judged on the whole span: "Science and Technology Support Project"
    cut = _first_suffix(run)
    and_at = next((i for i, tok in enumerate(run) if i > 0 and tok.lower() == "and"), None)
    if cut is not None and and_at is not None and and_at < cut:
        run = run[:and_at]  # "GEn1E Lifesciences and Interline Therapeutics": the first item only; the list continues after it
        cut = _first_suffix(run)
    if cut is not None:
        j = cut + 1
        while j < len(run) and run[j].strip(".,;:").lower() in CORPORATE_SUFFIXES:
            j += 1  # "BridgeBio Oncology Therapeutics", "Acme Biotech Co. Ltd"
        remainder = run[j:]
        if remainder and len(remainder) <= 3 and _is_legal_abbrev(remainder[-1]):
            j, remainder = len(run), []  # "OBI Pharma USA Inc."
        if remainder and run[cut].strip(".,;:").lower() in SECTOR_SUFFIXES and is_generic_name(" ".join(remainder)):
            return "", False  # "Science and Technology Support Project of the Department": a programme, not a company
        return _rstrip_name(" ".join(run[:j])), True
    # no corporate suffix: a bare 1-3 word name; a list ("A and B") is cut at "and"
    if full_generic or is_generic_name(" ".join(run)):
        return "", False
    words: List[str] = []
    for tok in run:
        if tok.lower() == "and":
            break
        words.append(tok)
    while words and words[-1].lower() in _CONNECTOR_SET:
        words.pop()
    content = [w for w in words if w.lower() not in _CONNECTOR_SET]
    if not content or len(content) > 3:
        return "", False
    if len(content) == 1 and re.fullmatch(r"[A-Z]{2,3}", content[0].strip(".,;:")):
        return "", False  # a lone 2-3 letter capital token is an author's initials far more often than a company
    if content[0].strip(".,;:").lower() in GENERIC_ACRONYMS:
        return "", False  # "NRF Incentive grant", "NIH Clinical Center"
    return _rstrip_name(" ".join(words)), False


# a low-confidence name followed by one of these lower-case words is an adjective, not a company
# ("the Korean government", "a Guangdong provincial grant")
_NEXT_WORD_GENERIC = {
    "government", "ministry", "state", "province", "provincial", "municipal", "national", "federal", "research",
    "foundation", "university", "institute", "council", "agency", "grant", "grants", "program", "programme",
    "project", "fund", "funding", "department", "scholarship", "fellowship", "award", "initiative", "consortium",
    "hospital", "school", "college", "society", "trust", "authority", "committee", "centre", "center",
}


_CONNECTOR_SET = {"of", "and", "&", "de", "del", "della", "des", "du", "der", "van", "von", "for", "the", "da", "do", "dos", "la", "le", "y", "e", "zu", "en"}


def _finish_name(text: str, start: int, raw: str) -> Tuple[str, bool, int]:
    """Trim a span, absorb a following ', Inc.' and return (name, confident, end_offset_in_text)."""
    name, confident = _trim_name_tokens(raw)
    if not name:
        return "", False, start + len(raw)
    k = len(name.split())
    ends = [m.end() for m in re.finditer(r"\S+", raw)]
    consumed = start + (ends[k - 1] if k - 1 < len(ends) else len(raw))
    m = _TRAILING_LEGAL_RE.match(text[consumed:])
    if m:
        suf = m.group("suf")
        if suf.endswith(".") and not _is_legal_abbrev(suf):
            suf = suf.rstrip(".")  # "LLC." ends the sentence; "Inc." keeps its period
        name = f"{name}, {suf}" if m.group(0).lstrip().startswith(",") else f"{name} {suf}"
        consumed += m.end()
        confident = True
    if not confident:
        nxt = re.match(r"[  ]+([a-z][a-z-]*)", text[consumed:])
        if nxt and nxt.group(1) in _NEXT_WORD_GENERIC:
            return "", False, consumed
        if " " not in name and re.match(r"[  ]+[A-Z]\.", text[consumed:]):
            return "", False, consumed  # "the Thomas R. Brown Distinguished Chair": a person, not a company
    return name.strip(), confident, consumed


def _snippet(text: str, lo: int, hi: int) -> str:
    a, b = max(0, lo - SNIPPET_RADIUS), min(len(text), hi + SNIPPET_RADIUS)
    return re.sub(r"\s+", " ", text[a:b]).strip()


def extract_statement_names(text: str, gazetteer=None) -> List[Tuple[str, bool, str]]:
    """(name, confident, snippet) for every company-like X after a relationship trigger in `text`."""
    if not text:
        return []
    out: List[Tuple[str, bool, str]] = []
    seen: set = set()
    for m in _STATEMENT_RE.finditer(text):
        pos = m.start("name")
        raw = m.group("name")
        items = 0
        while raw and items < 6:
            name, confident, consumed = _finish_name(text, pos, raw)
            # list continuations ("..., and X") are accepted only when X carries a corporate suffix: a bare
            # capitalised token there is usually the next author's initials
            if items > 0 and not confident:
                name = ""
            items += 1
            if name:
                key = normalize_name(name)
                if key and key not in seen and _accept_name(name, gazetteer):
                    seen.add(key)
                    out.append((name, confident, _snippet(text, m.start(), consumed)))
            cont = _LIST_CONT_RE.match(text[consumed:])
            if not cont:
                break
            pos = consumed + cont.start("name")
            raw = cont.group("name")
    return out


def _accept_name(name: str, gazetteer=None) -> bool:
    if len(name) < 3 or len(name) > 90 or not re.search(r"[A-Za-z]", name):
        return False
    if re.fullmatch(r"[\d\W]+", name) or re.fullmatch(r"[A-Z0-9]{2,3}", name):
        return False
    if "/" in name.replace("A/S", "") or re.search(r"\b10\.\d{4,}", name):
        return False  # funder identifiers and DOIs: "MCIU/AEI/10.13039/501100011033"
    toks = name.split()
    if len(toks) <= 2 and any(re.fullmatch(r"[A-Z0-9-]*\d{3,}[A-Z0-9-]*", t) for t in toks):
        return False  # grant codes: "P30AG059298", "CFI-36490", "PRIN 2020"
    if is_generic_name(name):
        return False
    if gazetteer is not None and gazetteer.find(name, ignore_context=True):
        return False
    return True


def statement_evidence(paper: Dict[str, Any], gazetteer=None) -> List[Evidence]:
    sections = _loads(paper.get("sections_json"), {})
    out: List[Evidence] = []
    for sec in ("coi", "funding", "ack"):
        text = sections.get(sec) or ""
        for name, confident, snippet in extract_statement_names(text, gazetteer):
            out.append(Evidence(
                name=name, evidence_type=sec, snippet=snippet, ref=paper["id"], date=_paper_date(paper),
                confident=confident, title=paper.get("title") or "", url=paper.get("url") or "",
            ))
    return out


# ------------------------------------------------------------------------------------------------
# (c) OpenAlex company-typed institutions stored as 'unlisted:' mentions
# ------------------------------------------------------------------------------------------------


def unlisted_evidence(conn: sqlite3.Connection, since: str, until: str, gazetteer=None) -> List[Evidence]:
    rows = conn.execute(
        """SELECT m.paper_id, m.entity_name, m.snippet, p.title, p.url, p.pub_date, p.first_seen
           FROM mentions m JOIN papers p ON p.id = m.paper_id
           WHERE m.entity_id LIKE 'unlisted:%' AND ((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date = '' AND substr(p.first_seen,1,10) >= ?))""",
        (since, until, since),
    ).fetchall()
    out: List[Evidence] = []
    for paper_id, name, snippet, title, url, pub_date, first_seen in rows:
        name = (name or "").strip()
        if not name or not _accept_name(name, gazetteer):
            continue
        out.append(Evidence(
            name=name, evidence_type="openalex_company", snippet=snippet or f"OpenAlex company-typed institution: {name}",
            ref=paper_id, date=(pub_date or (first_seen or "")[:10])[:10], confident=True, title=title or "", url=url or "",
        ))
    return out


# ------------------------------------------------------------------------------------------------
# (d) news items: NIH RePORTER, ClinicalTrials.gov, openFDA, press / Google News
# ------------------------------------------------------------------------------------------------

_NIH_PROJECT_NUM_RE = re.compile(r"^\d?[A-Z]\d{2}[A-Z]{2}\d{5,6}")
_NIH_CODE_RE = re.compile(r"^(?:[A-Z]\d{2}|[A-Z]{2}\d|[A-Z]\d[A-Z]\d?|SB1)$")


def parse_nih_reporter(summary: str, title: str = "") -> Optional[Tuple[str, str, bool]]:
    """(org_name, activity_code, is_sbir) from an NIH RePORTER NewsItem summary; None when no org."""
    lines = [ln for ln in (summary or "").splitlines() if ln.strip()]
    if not lines:
        return None
    parts = [p.strip() for p in lines[-1].split(" | ") if p.strip()]
    code = parts[0] if parts and _NIH_CODE_RE.match(parts[0]) else ""
    org = ""
    for p in parts[1 if code else 0:]:
        is_code = " " not in p and re.search(r"\d", p) and re.fullmatch(r"[A-Z0-9$,.\-/]+", p)  # project number, amount, FY
        if _NIH_PROJECT_NUM_RE.match(p) or is_code or p.startswith("$") or re.fullmatch(r"[A-Z]{2,6}", p):
            continue
        org = p
        break
    if not org and " — " in (title or ""):
        org = title.split(" — ", 1)[1].split(" (")[0]
    org = re.sub(r"\s*\([^()]*\)\s*$", "", org).strip(" .;")
    if not org:
        return None
    return _title_case_caps(org), code, code in SBIR_STTR_CODES


_CT_SENTINEL = r"(?:Collaborators|Interventions|Conditions|Phase|Enrollment|Status|Type|Summary):"
_CT_SPONSOR_RE = re.compile(rf"Sponsor:\s*(?P<name>.+?)(?:\s*\((?P<cls>[A-Za-z /]+)\))?\.?(?:\s+(?={_CT_SENTINEL})|\s*$)")
_CT_COLLAB_RE = re.compile(rf"Collaborators:\s*(?P<names>.+?)\.?(?:\s+(?={_CT_SENTINEL})|\s*$)")


def parse_clinicaltrials(summary: str) -> List[Tuple[str, str, bool]]:
    """[(name, role, confident)] for the sponsor (any class) and collaborators of a trial summary."""
    out: List[Tuple[str, str, bool]] = []
    m = _CT_SPONSOR_RE.search(summary or "")
    if m:
        name = m.group("name").strip()
        cls = (m.group("cls") or "").strip().lower()
        if name:
            out.append((name, f"sponsor ({cls})" if cls else "sponsor", cls == "industry" or has_corporate_suffix(name)))
    c = _CT_COLLAB_RE.search(summary or "")
    if c:
        for name in re.split(r";\s*", c.group("names")):
            name = name.strip(" .")
            if name:
                out.append((name, "collaborator", has_corporate_suffix(name)))
    return out


def parse_fda_applicant(title: str, summary: str = "") -> Optional[str]:
    if title and " — " in title:
        applicant = title.rsplit(" — ", 1)[1].strip()
        if applicant:
            return applicant
    m = re.search(r"applicant ([^·]+?)(?:, [^,·]+, [A-Z]{2})?(?: ·|$)", summary or "")
    return m.group(1).strip() if m else None


_PRESS_VERBS = (
    r"raises|raised|closes|closed|secures|secured|lands|landed|nabs|bags|nets|snags|launches|launched|unveils|"
    r"debuts|introduces|acquires|acquired|to acquire|buys|merges|emerges from stealth|exits stealth|files for ipo|"
    r"goes public|wins|receives|gets|earns|obtains|inks|signs|announces|completes|expands|partners|teams"
)
_PRESS_DESCRIPTORS = {"firm", "startup", "start-up", "company", "developer", "maker", "biotech", "spinout", "spinoff", "unicorn", "provider", "player", "specialist", "outfit", "upstart"}
_PRESS_LEAD_SKIP = {
    "why", "how", "what", "when", "where", "who", "the", "a", "an", "this", "these", "here", "is", "are", "can", "will",
    "inside", "meet", "exclusive", "report", "opinion", "analysis", "breaking", "watch", "video", "podcast", "q&a",
    "stat+", "stat", "update", "updated", "as", "after", "with", "in", "on", "for", "from", "over", "us",
}
_HEADLINE_NAME_RE = re.compile(rf"^(?P<name>{_NAME_TOKEN}(?:[  ]+(?:{_NAME_TOKEN}|&))*?)(?P<poss>['’]s?)?[  ]+(?P<rest>(?i:{_PRESS_VERBS})\b.*)$")
_ROUND_RE = re.compile(rf"^(?P<name>{_NAME_TOKEN}(?:[  ]+(?:{_NAME_TOKEN}|&))*?)(?P<poss>['’]s?)?[  ]+(?:[$£€]\s?[\d.,]+\s*[MBK]?(?:illion)?\s+)?(?i:series [a-d]\b|seed (?:round|financing|funding)|funding round|financing round)")
_TARGET_RE = re.compile(rf"(?i:acquires?|to acquire|buys|acquisition of|merges with)\s+(?:(?i:select|certain|key|the|a)\s+)?(?:(?i:assets of|stake in|majority of)\s+)?(?P<name>{_NAME_TOKEN}(?:[  ]+{_NAME_TOKEN})*)")


def _clean_headline(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "")).strip()
    t = re.sub(r"^(?:STAT\+|Exclusive|Breaking|Update|Opinion|Analysis)\s*:\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+[-–—]\s+[^-–—]{2,40}$", "", t)  # " - GenomeWeb" source suffix from Google News
    return t


def _headline_name(name: str, poss: Optional[str]) -> str:
    tokens = name.split()
    while tokens and tokens[0].lower().strip(":") in _PRESS_LEAD_SKIP:
        tokens.pop(0)
    while len(tokens) > 1 and re.search(r"['’]s?$", tokens[0]):
        tokens.pop(0)  # "France’s ABL Texcell" -> "ABL Texcell"
    tokens = [t for t in tokens if not re.search(r"-(?:backed|based|led|owned|funded|founded)$", t, re.IGNORECASE)]  # "Biogen-backed Estuary"
    cut = max((i for i, t in enumerate(tokens) if t.lower() in _PRESS_DESCRIPTORS), default=-1)
    if cut >= 0 and cut < len(tokens) - 1:
        tokens = tokens[cut + 1 :]  # "Microbiome Medicines Firm Microbiotica" -> "Microbiotica"
    cleaned = " ".join(tokens)
    cleaned = re.sub(r"['’]s?$", "", cleaned).rstrip(".,;:")
    return cleaned


def parse_press_headline(title: str, summary: str = "") -> List[str]:
    """Company names in financing / launch / acquisition headlines that sit next to sector words."""
    head = _clean_headline(title)
    if not head or not SECTOR_WORDS_RE.search(f"{head} {summary or ''}"):
        return []
    names: List[str] = []
    m = _HEADLINE_NAME_RE.match(head) or _ROUND_RE.match(head)
    if m:
        name = _headline_name(m.group("name"), m.group("poss"))
        if name:
            names.append(name)
    t = _TARGET_RE.search(head)
    if t:
        target = _headline_name(t.group("name"), None)
        if target:
            names.append(target)
    out: List[str] = []
    for n in names:
        if not n or not normalize_name(n) or n in out or len(n.split()) > 5:
            continue
        if any(t.lower().strip(".,") in _PRODUCT_WORDS for t in n.split()):
            continue  # "Multi-Cancer Early Detection Assay Receives FDA ...": a product, not a company
        out.append(n)
    return out


_PRODUCT_WORDS = {"assay", "test", "platform", "device", "kit", "trial", "study", "vaccine", "drug", "therapy", "pill", "panel", "sequencer", "software"}


def news_evidence(item: Dict[str, Any], gazetteer=None) -> List[Evidence]:
    feed = (item.get("feed") or "").lower()
    title = item.get("title") or ""
    summary = item.get("summary") or ""
    date_ = (item.get("published") or item.get("first_seen") or "")[:10]
    base = dict(ref=item["id"], date=date_, title=title, url=item.get("url") or "")
    out: List[Evidence] = []
    if feed == "nih-reporter":
        parsed = parse_nih_reporter(summary, title)
        if parsed:
            org, code, sbir = parsed
            if (sbir or has_corporate_suffix(org)) and _accept_name(org, gazetteer):
                out.append(Evidence(name=org, evidence_type="news:nih-reporter", confident=True,
                                    snippet=f"{'SBIR/STTR ' if sbir else ''}award {code or ''} to {org}: {title}".strip(), **base))
    elif feed == "clinicaltrials":
        for name, role, confident in parse_clinicaltrials(summary):
            if confident and _accept_name(name, gazetteer):
                out.append(Evidence(name=name, evidence_type="news:clinicaltrials", confident=True,
                                    snippet=f"trial {role}: {name} — {title}", **base))
    elif feed.startswith("fda-"):
        applicant = parse_fda_applicant(title, summary)
        if applicant and _accept_name(applicant, gazetteer):
            out.append(Evidence(name=applicant, evidence_type="news:fda", confident=True,
                                snippet=f"{feed} applicant: {title}", **base))
    else:
        for name in parse_press_headline(title, summary):
            if _accept_name(name, gazetteer):
                out.append(Evidence(name=name, evidence_type="news:press", confident=has_corporate_suffix(name),
                                    snippet=_clean_headline(title), **base))
    return out


# ------------------------------------------------------------------------------------------------
# collection, grouping, scoring
# ------------------------------------------------------------------------------------------------


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return v if v is not None else default


def _paper_date(paper: Dict[str, Any]) -> str:
    return (paper.get("pub_date") or (paper.get("first_seen") or "")[:10])[:10]


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def collect_evidence(conn: sqlite3.Connection, gazetteer, since: str, until: str) -> List[Evidence]:
    out: List[Evidence] = []
    papers = conn.execute(
        """SELECT id, title, url, pub_date, first_seen, authors_json, affiliations_json, sections_json, extra_json
           FROM papers WHERE (pub_date >= ? AND pub_date <= ?) OR (pub_date = '' AND substr(first_seen,1,10) >= ?)""",
        (since, until, since),
    )
    cols = [c[0] for c in papers.description]
    for row in papers.fetchall():
        p = dict(zip(cols, row))
        out.extend(email_evidence(p, gazetteer))
        out.extend(statement_evidence(p, gazetteer))
    out.extend(unlisted_evidence(conn, since, until, gazetteer))
    news = conn.execute(
        """SELECT id, feed, title, url, published, summary, first_seen FROM news_items
           WHERE (substr(published,1,10) >= ? AND substr(published,1,10) <= ?) OR (substr(first_seen,1,10) >= ? AND (published IS NULL OR published = ''))""",
        (since, until, since),
    )
    ncols = [c[0] for c in news.description]
    for row in news.fetchall():
        out.extend(news_evidence(dict(zip(ncols, row)), gazetteer))
    return out


def _pick_display(evidence: Sequence[Evidence]) -> str:
    counts: Counter = Counter()
    for e in evidence:
        if not e.name:
            continue
        weight = 1.0
        if has_corporate_suffix(e.name):
            weight += 1.0
        if e.evidence_type == "email_domain" and e.name == e.domain:
            weight -= 0.5  # a bare domain is the least informative label
        if e.confident:
            weight += 0.5
        counts[e.name] += weight
    return counts.most_common(1)[0][0] if counts else ""


def group_candidates(evidence: Iterable[Evidence]) -> Dict[str, Candidate]:
    groups: Dict[str, List[Evidence]] = defaultdict(list)
    for e in evidence:
        key = normalize_name(e.name)
        if key:
            groups[key].append(e)
    # merge bare e-mail brand tokens into the one longer name that starts with them ("ipsen" -> "ipsen innovation")
    brand_keys = [k for k, evs in groups.items() if all(ev.evidence_type == "email_domain" and ev.name == ev.domain for ev in evs)]
    for k in brand_keys:
        if len(k) < 4:
            continue
        longer = [o for o in groups if o != k and o.startswith(k)]
        if len(longer) == 1:
            groups[longer[0]].extend(groups.pop(k))
    out: Dict[str, Candidate] = {}
    for key, evs in groups.items():
        evs.sort(key=lambda e: (e.date or ""), reverse=True)
        out[key] = Candidate(name_norm=key, display_name=_pick_display(evs), evidence=evs)
    return out


HIGH_PRECISION_TYPES = {"email_domain", "openalex_company", "news:nih-reporter", "news:clinicaltrials", "news:fda"}


def signal_family(evidence_type: str) -> str:
    """coi / funding / ack are one family: the same sentence is often stored in two of them."""
    return "statement" if evidence_type in ("coi", "funding", "ack") else evidence_type


def score_candidate(cand: Candidate, today: Optional[str] = None) -> float:
    """Transparent score: 0.5 per distinct paper/item (cap 8), +1 per extra signal family, recency,
    +1 for a corporate suffix, +0.5 for a confident high-precision signal, -0.5 when nothing is confident."""
    today_d = date.fromisoformat(today or _today())
    score = 0.5 * min(cand.refs, 8)
    score += 1.0 * max(0, len({signal_family(t) for t in cand.types}) - 1)
    last = cand.last_seen
    if last:
        try:
            age = (today_d - date.fromisoformat(last[:10])).days
        except ValueError:
            age = 999
        if age <= 30:
            score += 1.0
        elif age <= 90:
            score += 0.5
    if cand.has_suffix():
        score += 1.0
    if any(e.confident and e.evidence_type in HIGH_PRECISION_TYPES for e in cand.evidence):
        score += 0.5
    if not any(e.confident for e in cand.evidence):
        score -= 0.5
    return round(score, 2)


# ------------------------------------------------------------------------------------------------
# persistence
# ------------------------------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _conn(store_or_conn) -> sqlite3.Connection:
    conn = getattr(store_or_conn, "conn", store_or_conn)
    ensure_schema(conn)
    return conn


def _row_to_dict(row: Tuple, cols: Sequence[str]) -> Dict[str, Any]:
    d = dict(zip(cols, row))
    payload = _loads(d.pop("evidence_json", None), {})
    d["evidence"] = payload.get("evidence", []) if isinstance(payload, dict) else payload
    d["types"] = payload.get("types", {}) if isinstance(payload, dict) else {}
    d["variants"] = payload.get("variants", []) if isinstance(payload, dict) else []
    d["domains"] = payload.get("domains", []) if isinstance(payload, dict) else []
    d["promotion"] = payload.get("promotion") if isinstance(payload, dict) else None
    d["confident"] = payload.get("confident", True) if isinstance(payload, dict) else True
    return d


COLS = ("name_norm", "display_name", "evidence_json", "first_seen", "last_seen", "papers", "score", "status")


def refresh(store_or_conn, gazetteer, since: str, until: Optional[str] = None, today: Optional[str] = None) -> Dict[str, Any]:
    """Recompute candidates from the store for papers / news in [since, until]. Statuses are preserved."""
    conn = _conn(store_or_conn)
    until = until or _today()
    evidence = collect_evidence(conn, gazetteer, since, until)
    cands = group_candidates(evidence)
    by_type: Counter = Counter(e.evidence_type for e in evidence)
    n_new = n_upd = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for key, cand in cands.items():
        cand.score = score_candidate(cand, today)
        existing = conn.execute("SELECT first_seen, status, evidence_json FROM candidate_companies WHERE name_norm=?", (key,)).fetchone()
        payload = {
            "evidence": [e.to_dict() for e in cand.evidence[:MAX_EVIDENCE_STORED]],
            "types": dict(Counter(e.evidence_type for e in cand.evidence)),
            "variants": cand.variants,
            "domains": cand.domains,
            "confident": any(e.confident for e in cand.evidence),
            "refreshed_at": now,
            "window": [since, until],
        }
        first_seen = cand.first_seen or now[:10]
        if existing:
            old_payload = _loads(existing[2], {})
            if isinstance(old_payload, dict) and old_payload.get("promotion"):
                payload["promotion"] = old_payload["promotion"]
            if existing[0]:
                first_seen = min(existing[0], first_seen)
            n_upd += 1
        else:
            n_new += 1
        conn.execute(
            """INSERT INTO candidate_companies (name_norm, display_name, evidence_json, first_seen, last_seen, papers, score, status)
               VALUES (?,?,?,?,?,?,?,'new')
               ON CONFLICT(name_norm) DO UPDATE SET
                 display_name=excluded.display_name, evidence_json=excluded.evidence_json,
                 first_seen=excluded.first_seen, last_seen=excluded.last_seen,
                 papers=excluded.papers, score=excluded.score""",
            (key, cand.display_name, json.dumps(payload, ensure_ascii=False), first_seen, cand.last_seen or now[:10], cand.refs, cand.score),
        )
    # `new` rows whose evidence fell inside this window but did not come back (an earlier rule version, a
    # re-annotated paper) are stale: drop them. Ignored and promoted rows are decisions and always survive.
    stale = [r[0] for r in conn.execute("SELECT name_norm FROM candidate_companies WHERE status='new' AND last_seen >= ? AND last_seen <= ?", (since, until)).fetchall()
             if r[0] not in cands]
    for key in stale:
        conn.execute("DELETE FROM candidate_companies WHERE name_norm=?", (key,))
    conn.commit()
    return {"since": since, "until": until, "evidence": len(evidence), "candidates": len(cands), "new": n_new,
            "updated": n_upd, "removed": len(stale), "by_type": dict(by_type)}


def list_candidates(store_or_conn, since: Optional[str] = None, min_score: float = 0.0,
                    statuses: Sequence[str] = ("new",), limit: Optional[int] = None) -> List[Dict[str, Any]]:
    conn = _conn(store_or_conn)
    sql = f"SELECT {', '.join(COLS)} FROM candidate_companies WHERE score >= ?"
    params: List[Any] = [float(min_score)]
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    if since:
        sql += " AND last_seen >= ?"
        params.append(since)
    sql += " ORDER BY score DESC, papers DESC, display_name"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_row_to_dict(r, COLS) for r in conn.execute(sql, params).fetchall()]


def get_candidate(store_or_conn, name: str) -> Optional[Dict[str, Any]]:
    """Resolve a display name, normalised key or unique prefix / substring to one candidate row."""
    conn = _conn(store_or_conn)
    key = normalize_name(name)
    sql = f"SELECT {', '.join(COLS)} FROM candidate_companies WHERE name_norm=?"
    row = conn.execute(sql, (key,)).fetchone()
    if row:
        return _row_to_dict(row, COLS)
    needle = _compact(name)
    if len(needle) < 3:
        return None
    rows = conn.execute(f"SELECT {', '.join(COLS)} FROM candidate_companies").fetchall()
    hits = [r for r in rows if needle in r[0] or needle in _compact(r[1] or "")]
    if len(hits) == 1:
        return _row_to_dict(hits[0], COLS)
    if len(hits) > 1:
        raise LookupError("ambiguous candidate name; matches: " + ", ".join(sorted(h[1] or h[0] for h in hits)[:10]))
    return None


def set_status(store_or_conn, name: str, status: str) -> Dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn = _conn(store_or_conn)
    cand = get_candidate(conn, name)
    if cand is None:
        raise LookupError(f"no candidate matches {name!r}")
    conn.execute("UPDATE candidate_companies SET status=? WHERE name_norm=?", (status, cand["name_norm"]))
    conn.commit()
    cand["status"] = status
    return cand


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    if s == "" or re.search(r"[:#,\[\]{}&*!|>'\"%@`]|^\s|\s$|^-", s) or s.lower() in {"true", "false", "null", "yes", "no"}:
        return json.dumps(s, ensure_ascii=False)
    return s


def yaml_line(entry: Dict[str, Any]) -> str:
    """The flow-style companies.yaml line for a promoted candidate."""
    parts = [f"id: {_yaml_scalar(entry['id'])}", f"name: {_yaml_scalar(entry['name'])}", f"ticker: {_yaml_scalar(entry.get('ticker') or 'private')}",
             f"type: {_yaml_scalar(entry.get('type') or 'tools')}", f"watch_news: {_yaml_scalar(bool(entry.get('watch_news', False)))}",
             "domains: [" + ", ".join(_yaml_scalar(d) for d in entry.get("domains") or []) + "]",
             "aliases: [" + ", ".join(_yaml_scalar(a) for a in entry.get("aliases") or []) + "]"]
    return "- {" + ", ".join(parts) + "}"


def build_entry(cand: Dict[str, Any], ticker: Optional[str] = None, type_: Optional[str] = None) -> Dict[str, Any]:
    name = cand["display_name"] or cand["name_norm"]
    aliases = [v for v in cand.get("variants", []) if v and v != name and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", v)]
    return {"id": slugify(name), "name": name, "ticker": (ticker or "private").strip(), "type": (type_ or "tools").strip(),
            "watch_news": False, "domains": list(cand.get("domains", [])), "aliases": aliases[:6]}


class PromotionError(ValueError):
    """The gazetteer write behind `candidates promote` was refused (duplicate id, alias collision, bad type, ...).

    A ValueError so the command layer prints the message and exits non-zero; the candidate keeps its status."""


def _track_add_company(entry: Dict[str, Any], settings=None) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Hand a built entry to litradar.track.add_company with its real keyword signature."""
    from . import track as _track
    from .config import Settings

    settings = settings or Settings.from_env()
    return _track.add_company(
        settings,
        entry["name"],
        ticker=entry.get("ticker") or "",
        type_=entry.get("type") or "tools",
        aliases=list(entry.get("aliases") or []),
        domains=list(entry.get("domains") or []),
        watch_news=bool(entry.get("watch_news", False)),
        entry_id=entry.get("id") or "",
    )


def promote(store_or_conn, name: str, ticker: Optional[str] = None, type_: Optional[str] = None, add_company=None, settings=None) -> Dict[str, Any]:
    """Write a candidate to companies.yaml and mark it promoted.

    The hand-off goes to ``litradar.track.add_company`` (called with its real keyword signature, against
    `settings` or ``Settings.from_env()``) when `add_company` is None; a callable `add_company` is used
    instead (it receives the entry dict); ``add_company=False`` skips the write and only returns the
    flow-style line to paste. The status flips to ``promoted`` only when the write succeeded (or was
    skipped on purpose): a refused write raises PromotionError and leaves the candidate as it was.
    """
    conn = _conn(store_or_conn)
    cand = get_candidate(conn, name)
    if cand is None:
        raise LookupError(f"no candidate matches {name!r}")
    entry = build_entry(cand, ticker, type_)
    tracked = False
    error = ""
    if add_company is not False:
        try:
            if add_company is None:
                _track_add_company(entry, settings)
            else:
                try:
                    add_company(entry)
                except TypeError:
                    add_company(**entry)
            tracked = True
        except Exception as exc:  # noqa: BLE001 - every failure must surface; the status must not flip
            error = f"{type(exc).__name__}: {exc}"
            log.warning("track.add_company failed for %r: %s", entry.get("name"), error)
            raise PromotionError(
                f"{cand['display_name']}: not promoted; litradar.track.add_company refused the entry ({error}). "
                f"Fix the conflict (or pass --ticker/--type) and retry, or add the line by hand: {yaml_line(entry)}"
            ) from exc
    line = yaml_line(entry)
    payload = _loads(conn.execute("SELECT evidence_json FROM candidate_companies WHERE name_norm=?", (cand["name_norm"],)).fetchone()[0], {})
    if not isinstance(payload, dict):
        payload = {"evidence": payload}
    payload["promotion"] = {"entry": entry, "yaml": line, "tracked": tracked, "error": error,
                            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    conn.execute("UPDATE candidate_companies SET status='promoted', evidence_json=? WHERE name_norm=?",
                 (json.dumps(payload, ensure_ascii=False), cand["name_norm"]))
    conn.commit()
    return {"candidate": cand["display_name"], "entry": entry, "yaml": line, "tracked": tracked, "error": error}


# ------------------------------------------------------------------------------------------------
# SEC ticker hint (hint only; never authoritative)
# ------------------------------------------------------------------------------------------------

_SEC_CACHE: Dict[str, List[Tuple[str, str, str]]] = {}


def load_sec_tickers(http=None, cache_dir: Optional[Path] = None, contact_email: str = "", max_age_days: int = SEC_CACHE_MAX_AGE_DAYS) -> List[Tuple[str, str, str]]:
    """[(normalised_title, ticker, title)] from the SEC's company_tickers.json, cached on disk."""
    cache_path = Path(cache_dir) / "sec_company_tickers.json" if cache_dir else None
    cache_key = str(cache_path or "memory")
    if cache_key in _SEC_CACHE:
        return _SEC_CACHE[cache_key]
    data: Any = None
    if cache_path and cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < max_age_days * 86400:
        data = _loads(cache_path.read_text(encoding="utf-8"), None)
    if data is None:
        if http is None:
            try:
                from .http import Http

                http = Http(contact_email=contact_email, max_retries=1, timeout=30.0)
            except Exception:  # pragma: no cover
                http = None
        if http is not None:
            try:
                data = http.get_json(SEC_TICKERS_URL)
            except Exception as exc:  # network trouble is never fatal for a hint
                log.info("SEC ticker list unavailable: %s", exc)
                data = None
        if data and cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass
    rows: List[Tuple[str, str, str]] = []
    items = data.values() if isinstance(data, dict) else (data or [])
    for it in items:
        if isinstance(it, dict) and it.get("ticker") and it.get("title"):
            rows.append((normalize_name(str(it["title"])), str(it["ticker"]), str(it["title"])))
    _SEC_CACHE[cache_key] = rows
    return rows


def _brand_word(name: str) -> str:
    """The first distinctive word of a company name, lower-cased ('Arda Therapeutics' -> 'arda')."""
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in {"the"}]
    return words[0] if words else ""


def sec_ticker_hint(name: str, tickers: Optional[Sequence[Tuple[str, str, str]]] = None, **load_kwargs) -> Optional[Dict[str, Any]]:
    """Best fuzzy match of a candidate name against SEC registrants: {'ticker', 'title', 'match'} or None.

    The brand word (first word) must agree before edit distance is consulted: 'Arda Therapeutics' and
    'Aprea Therapeutics' share a sector suffix and score 0.91 on characters alone, but they are different
    companies, so no hint is offered for them."""
    key = normalize_name(name)
    if len(key) < 4:
        return None
    brand = _brand_word(name)
    rows = tickers if tickers is not None else load_sec_tickers(**load_kwargs)
    best: Optional[Tuple[float, str, str]] = None
    for norm, ticker, title in rows:
        if not norm:
            continue
        if norm == key:
            ratio = 1.0
        elif brand and _brand_word(title) != brand:
            continue  # same sector word, different company: never a hint
        elif len(key) >= 5 and (norm.startswith(key) or key.startswith(norm)) and min(len(norm), len(key)) / max(len(norm), len(key)) >= 0.6:
            ratio = 0.9
        elif abs(len(norm) - len(key)) <= 3:
            ratio = difflib.SequenceMatcher(None, norm, key).ratio()
            if ratio < 0.88:
                continue
        else:
            continue
        if best is None or ratio > best[0]:
            best = (ratio, ticker, title)
    if best is None:
        return None
    return {"ticker": best[1], "title": best[2], "match": round(best[0], 2)}


# ------------------------------------------------------------------------------------------------
# digest section
# ------------------------------------------------------------------------------------------------


def _md_cell(s: Any) -> str:
    return str(s).replace("|", "/").replace("\n", " ").strip()


def render_candidates_markdown(store_or_conn, since: str, until: str, top: int = 15) -> str:
    """Markdown block "Companies spotted (not yet tracked)" for the digest; reads the candidate table only."""
    rows = list_candidates(store_or_conn, since=since, min_score=0.0, statuses=("new",), limit=None)
    rows = [r for r in rows if (r.get("first_seen") or "") <= until][:top]
    md = ["## Companies spotted (not yet tracked)\n"]
    md.append(
        "Names that behave like companies in this window but are not in the company gazetteer: corporate author "
        "e-mail domains, competing-interest and funding statements, company-typed affiliations, SBIR/STTR awards, "
        "industry trial sponsors, FDA applicants and financing headlines. Review with `litradar candidates`, drop one "
        "with `litradar candidates ignore <name>`, add one with `litradar candidates promote <name>`.\n"
    )
    if not rows:
        md.append("_no new candidates in this window_\n")
        return "\n".join(md)
    header = "| Candidate | Score | Signals | Papers / items | Last seen | Evidence |"
    lines = [header, "|---|---|---|---|---|---|"]
    for r in rows:
        types = ", ".join(f"{k} ({v})" for k, v in sorted((r.get("types") or {}).items()))
        ev = (r.get("evidence") or [{}])[0]
        snippet = _md_cell(ev.get("snippet") or "")
        if len(snippet) > 140:
            snippet = snippet[:137].rstrip() + "…"
        label = f"[{ev.get('evidence_type', '')}] {snippet}" if ev else ""
        if ev.get("url"):
            label += f" ([source]({ev['url']}))"
        flag = "" if r.get("confident", True) else " <sub>(low confidence)</sub>"
        lines.append(f"| {_md_cell(r['display_name'])}{flag} | {r['score']:.1f} | {_md_cell(types)} | {r['papers']} | {r.get('last_seen') or ''} | {label} |")
    md.append("\n".join(lines) + "\n")
    return "\n".join(md)


# ------------------------------------------------------------------------------------------------
# convenience
# ------------------------------------------------------------------------------------------------


def parse_since(s: Optional[str], default_days: int = 90) -> str:
    """'90d' or 'YYYY-MM-DD' -> ISO date; None -> today - default_days."""
    if not s:
        return (datetime.now(timezone.utc).date() - timedelta(days=default_days)).isoformat()
    m = re.fullmatch(r"(\d+)d", s.strip())
    if m:
        return (datetime.now(timezone.utc).date() - timedelta(days=int(m.group(1)))).isoformat()
    return s.strip()


def load_company_gazetteer(settings):
    from .extract.gazetteer import Gazetteer

    return Gazetteer.from_yaml(settings.yaml("companies"), "company")
