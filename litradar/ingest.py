"""Add papers to the corpus by identifier: DOI, PMID, PMCID, arXiv id, or a URL that carries one.

The weekly pipeline decides on its own what enters the corpus; this module lets a reader add a
paper by hand (a reading list, a reference from a call, a preprint a colleague sent) and have it
treated exactly like a pipeline paper: resolved through the same source adapters, merged with
Pipeline.merge, given full text only where the data policy allows, annotated with the gazetteers
and the tracked-author list through Pipeline.extract, scored through Pipeline.rank and stored with
the same upsert. Adding a paper that is already stored refreshes it.

Resolution order (a step is skipped when it does not apply to the identifier):
  1. Europe PMC  search?query=DOI:"<doi>" | EXT_ID:<pmid> AND SRC:MED | PMCID:<pmcid> | EXT_ID:<ppr> AND SRC:PPR
  2. Crossref    GET /works/<doi>
  3. bioRxiv     GET /details/{biorxiv|medrxiv}/<doi>   (date-shaped 10.1101/ and 10.64898/ DOIs; latest version kept)
  4. arXiv       GET /api/query?id_list=<id>            (arXiv ids and 10.48550/arXiv.<id> DOIs)
A record found by one step that carries a DOI continues through the DOI-keyed steps, and whatever
came back is merged in that fixed order so a hand-added paper looks like a pipeline one.
Field syntax was verified against the live services on 2026-10-17; see docs/add.md.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .http import Http
from .models import Mention, Paper, normalize_doi
from .pipeline import Pipeline
from .policy import fulltext_storage_level
from .sources import arxiv as arxiv_src
from .sources import biorxiv as biorxiv_src
from .sources import crossref as crossref_src
from .sources import europepmc as epmc_src
from .store import Store

log = logging.getLogger(__name__)

BIORXIV_DETAILS = "https://api.biorxiv.org/details/{server}/{doi}"
ARXIV_DOI_PREFIX = "10.48550/arxiv."
# bioRxiv / medRxiv DOIs are date-shaped (10.1101/2023.04.30.538439, 10.64898/2026.10.05.756623; six-digit
# ids before 2019). The 10.1101 prefix also covers CSHL journals (10.1101/gr.…), which are not preprints.
PREPRINT_DOI_RE = re.compile(r"^10\.(?:1101|64898)/(?:\d{4}\.\d{2}\.\d{2}\.)?\d{6,8}")
PREPRINT_SUFFIX_RE = re.compile(r"(?:v\d+)?(?:\.[a-z0-9.\-]+)?", re.I)  # v2, .full, .full.pdf, .abstract …
ARXIV_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?$", re.I)
PMID_RE = re.compile(r"^\d{1,9}$")
PMCID_RE = re.compile(r"^PMC\d+$", re.I)
PPR_RE = re.compile(r"^PPR\d+$", re.I)
URL_RE = re.compile(r"^(?:https?://|www\.)", re.I)
URL_DOI_TAIL_RE = re.compile(r"/(?:full|abstract|pdf|epdf|meta|fulltext|references|figures|supplementary|download)$", re.I)
MERGE_ORDER = ("europepmc", "crossref", "biorxiv", "arxiv")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- identifiers --------------------------------------------------------------------------------
@dataclass
class Identifier:
    kind: str  # doi | pmid | pmcid | ppr | arxiv
    value: str  # normalised: lower-case DOI, digits, PMC<digits>, PPR<digits>, arXiv id as given
    raw: str = ""
    server_hint: str = ""  # biorxiv | medrxiv when the URL said which server

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def canonical_doi(text: str) -> Optional[str]:
    """Lower-case DOI without a preprint version / page suffix, or None when no DOI is present."""
    doi = normalize_doi(text)
    if not doi:
        return None
    m = PREPRINT_DOI_RE.match(doi)
    if m and PREPRINT_SUFFIX_RE.fullmatch(doi[m.end():]):
        return m.group(0)
    return URL_DOI_TAIL_RE.sub("", doi)


def is_preprint_doi(doi: str) -> bool:
    return bool(PREPRINT_DOI_RE.fullmatch(doi or ""))


def _doi_identifier(text: str, raw: str) -> Identifier:
    doi = canonical_doi(text)
    if not doi:
        raise ValueError(f"not a DOI: {raw}")
    if doi.startswith(ARXIV_DOI_PREFIX):
        return _arxiv_identifier(doi[len(ARXIV_DOI_PREFIX):], raw)
    return Identifier("doi", doi, raw)


def _arxiv_identifier(text: str, raw: str) -> Identifier:
    t = text.strip()
    if t.lower().startswith("arxiv:"):
        t = t[6:].strip()
    if t.lower().endswith(".pdf"):
        t = t[:-4]
    if not ARXIV_ID_RE.match(t):
        raise ValueError(f"not an arXiv id: {raw}")
    return Identifier("arxiv", t, raw)


def _pmcid_identifier(text: str, raw: str) -> Identifier:
    t = text.strip()
    if not t.upper().startswith("PMC"):
        t = "PMC" + t
    if not PMCID_RE.match(t):
        raise ValueError(f"not a PMCID: {raw}")
    return Identifier("pmcid", "PMC" + t[3:], raw)


def _from_url(s: str, raw: str) -> Identifier:
    u = urlsplit(s if "://" in s else "https://" + s)
    host = u.netloc.lower()
    path = unquote(u.path or "")
    if host.endswith("doi.org"):
        return _doi_identifier(path, raw)
    if host.endswith(("biorxiv.org", "medrxiv.org")):
        try:
            ident = _doi_identifier(path, raw)
        except ValueError:
            raise ValueError(f"preprint URL without a DOI (use the /content/10.xxxx/… form): {raw}") from None
        ident.server_hint = "medrxiv" if "medrxiv" in host else "biorxiv"
        return ident
    if host.endswith("europepmc.org"):
        m = re.search(r"/(MED|PMC|PPR)/([A-Za-z0-9]+)", path, re.I)
        if m:
            src, val = m.group(1).upper(), m.group(2)
            if src == "MED" and PMID_RE.match(val):
                return Identifier("pmid", val, raw)
            if src == "PMC":
                return _pmcid_identifier(val, raw)
            if src == "PPR":
                val = val if val.upper().startswith("PPR") else "PPR" + val
                if PPR_RE.match(val):
                    return Identifier("ppr", "PPR" + val[3:], raw)
    if host.endswith("pubmed.ncbi.nlm.nih.gov") or "/pubmed/" in path:
        m = re.search(r"/(\d{1,9})/?$", path)
        if m:
            return Identifier("pmid", m.group(1), raw)
    if host.endswith("ncbi.nlm.nih.gov"):
        m = re.search(r"(PMC\d+)", path, re.I)
        if m:
            return _pmcid_identifier(m.group(1), raw)
    if host.endswith("arxiv.org"):
        m = re.search(r"/(?:abs|pdf|html)/(.+?)/?$", path)
        if m:
            return _arxiv_identifier(m.group(1), raw)
    # anything else: a DOI in the path or the query string (publisher pages, api.crossref.org/works/<doi>,
    # journals.plos.org/…?id=<doi>)
    for text in [path] + [v for vs in parse_qs(u.query).values() for v in vs]:
        if canonical_doi(text):
            return _doi_identifier(text, raw)
    raise ValueError(f"no DOI, PMID, PMCID or arXiv id in URL: {raw}")


def parse_identifier(raw: str) -> Identifier:
    """Classify one identifier string; raises ValueError when it is not recognised."""
    s = (raw or "").strip().strip("<>")
    if not s:
        raise ValueError("empty identifier")
    low = s.lower()
    if URL_RE.match(s):
        return _from_url(s, raw)
    if low.startswith("doi:"):
        return _doi_identifier(s[4:], raw)
    if low.startswith("pmid:"):
        v = s[5:].strip()
        if not PMID_RE.match(v):
            raise ValueError(f"not a PMID: {raw}")
        return Identifier("pmid", v, raw)
    if low.startswith("pmcid:"):
        return _pmcid_identifier(s[6:], raw)
    if low.startswith("arxiv:"):
        return _arxiv_identifier(s[6:], raw)
    if PMID_RE.match(s):
        return Identifier("pmid", s, raw)
    if PMCID_RE.match(s):
        return _pmcid_identifier(s, raw)
    if PPR_RE.match(s):
        return Identifier("ppr", "PPR" + s[3:], raw)
    if ARXIV_ID_RE.match(s):
        return _arxiv_identifier(s, raw)
    if low.startswith("10."):
        return _doi_identifier(s, raw)
    raise ValueError(f"unrecognised identifier: {raw}")


def read_id_file(path: Path) -> List[str]:
    """One identifier per line; blank lines and anything after '#' are ignored."""
    out: List[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        item = line.split("#", 1)[0].strip()
        if item:
            out.append(item)
    return out


# ---- resolution ---------------------------------------------------------------------------------
class _Canned:
    """Serves one already-fetched payload to an adapter's fetch() so its record-to-Paper mapping is reused
    verbatim (bioRxiv and arXiv build their Paper inside the paging loop and expose no to_paper())."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, cache: bool = False) -> Any:
        payload, self._payload = self._payload, None
        return payload

    get_text = get_json


@dataclass
class Resolution:
    identifier: Identifier
    paper: Optional[Paper] = None
    via: List[str] = field(default_factory=list)  # which lookups answered, in resolution order
    notes: List[str] = field(default_factory=list)  # lookups that did not


class Resolver:
    """Looks an identifier up in the public indexes and merges what comes back into one Paper."""

    def __init__(self, http: Http, contact_email: str = "") -> None:
        self.http = http
        self.contact_email = contact_email

    @staticmethod
    def epmc_query(ident: Identifier) -> Optional[str]:
        if ident.kind == "doi":
            return f'DOI:"{ident.value}"'
        if ident.kind == "pmid":
            return f"EXT_ID:{ident.value} AND SRC:MED"
        if ident.kind == "pmcid":
            return f"PMCID:{ident.value}"
        if ident.kind == "ppr":
            return f"EXT_ID:{ident.value} AND SRC:PPR"
        return None

    def europepmc(self, query: str, doi: Optional[str] = None) -> Optional[Dict[str, Any]]:
        recs = list(epmc_src.search(self.http, query, max_records=5, result_type="core", page_size=5))
        if not recs:
            return None
        if doi:
            exact = [r for r in recs if normalize_doi(r.get("doi")) == doi]
            recs = exact or recs
        recs.sort(key=lambda r: 0 if (r.get("source") or "").upper() == "MED" else 1)  # stable: MED first
        return recs[0]

    def crossref(self, doi: str) -> Optional[Dict[str, Any]]:
        params = {"mailto": self.contact_email} if self.contact_email else None
        data = self.http.get_json(f"{crossref_src.BASE}/works/{quote(doi, safe='/')}", params=params)
        if not data or data.get("status") != "ok" or data.get("message-type") != "work":
            return None
        return data.get("message") or None

    def biorxiv(self, doi: str, first: str = "") -> Tuple[Optional[Paper], str]:
        servers = ["biorxiv", "medrxiv"]
        if first in servers:
            servers.remove(first)
            servers.insert(0, first)
        for server in servers:
            data = self.http.get_json(BIORXIV_DETAILS.format(server=server, doi=doi))
            if not data or not data.get("collection"):
                continue
            papers = list(biorxiv_src.fetch(_Canned(data), "", "", server=server))  # keeps the latest version
            if papers:
                return papers[0], server
        return None, ""

    def arxiv(self, arxiv_id: str) -> Optional[Paper]:
        text = self.http.get_text(arxiv_src.API, params={"id_list": arxiv_id, "max_results": 5})
        if not text:
            return None
        for p in arxiv_src.fetch(_Canned(text), "", "", ["q-bio.GN"]):
            if "api/errors" in (p.extra.get("arxiv_id") or ""):
                continue  # older API builds answer a malformed id with an error entry
            return p
        return None

    def resolve(self, ident: Identifier) -> Resolution:
        res = Resolution(ident)
        found: Dict[str, Paper] = {}
        doi: Optional[str] = ident.value if ident.kind == "doi" else None

        if ident.kind == "arxiv":
            p = self.arxiv(ident.value)
            if p:
                found["arxiv"] = p
                res.via.append(f"arxiv:{p.extra.get('arxiv_id')}")
                doi = p.doi  # journal DOI when arXiv knows it: lets the DOI-keyed lookups run too
            else:
                res.notes.append("arxiv: no record")

        query = self.epmc_query(ident) or (f'DOI:"{doi}"' if doi else None)
        if query:
            rec = self.europepmc(query, doi)
            if rec:
                p = epmc_src.to_paper(rec)
                found["europepmc"] = p
                res.via.append(f"europepmc:{p.extra.get('epmc_id')}")
                doi = doi or p.doi
            else:
                res.notes.append("europepmc: no record")

        if doi:
            msg = self.crossref(doi)
            if msg:
                p = crossref_src.to_paper(msg)
                if msg.get("ISSN"):
                    p.extra["issn"] = list(msg["ISSN"])  # lets Pipeline.journal_tier() work from the ISSN
                cc = next((x.get("URL") for x in (msg.get("license") or []) if "creativecommons.org" in (x.get("URL") or "")), "")
                if cc:
                    p.license = cc
                found["crossref"] = p
                res.via.append("crossref")
            else:
                res.notes.append("crossref: no record")
            if is_preprint_doi(doi):
                p, server = self.biorxiv(doi, ident.server_hint)
                if p:
                    found["biorxiv"] = p
                    res.via.append(f"{server} v{p.extra.get('version')}")
                else:
                    res.notes.append("biorxiv/medrxiv: no record")

        merged: Optional[Paper] = None
        for key in MERGE_ORDER:
            p = found.get(key)
            if p is None:
                continue
            merged = p if merged is None else Pipeline.merge(merged, p)
        if merged is not None:
            merged.extra["sources"] = sorted(set(merged.extra.get("sources") or []) | {p.source for p in found.values()})
        res.paper = merged
        return res


# ---- full text under the data policy -----------------------------------------------------------
def attach_fulltext(http: Http, paper: Paper, cfg: Dict[str, Any], enabled: bool = True) -> Tuple[str, str]:
    """Fetch open full text where litradar.policy allows. Returns (level, note); level is full | facts_only | none.

    'full' stores sections and body; 'facts_only' means the text was read for gazetteer mentions but no
    section or body is stored (the caller strips them after extraction); 'none' means nothing was fetched.
    """
    if not enabled:
        return "none", "disabled (--no-fulltext)"
    sources = cfg.get("sources") or {}
    ecfg, bcfg = sources.get("europepmc") or {}, sources.get("biorxiv") or {}
    notes: List[str] = []
    pmcid = (paper.extra or {}).get("pmcid")
    if pmcid:
        if not ecfg.get("fulltext", True):
            notes.append("europepmc: full text disabled in radar.yaml (sources.europepmc.fulltext)")
        else:
            level = fulltext_storage_level(paper.license, open_access=(paper.extra.get("is_open_access") == "Y"))
            if epmc_src.attach_fulltext(http, paper):
                why = "" if level == "full" else " (not in the open-access subset: mentions kept, text not stored)"
                return level, f"Europe PMC {pmcid}{why}"
            notes.append(f"europepmc: no XML for {pmcid}")
    if paper.fulltext_url:
        if not bcfg.get("fulltext", True):
            notes.append("biorxiv: full text disabled in radar.yaml (sources.biorxiv.fulltext)")
        else:
            level = fulltext_storage_level(paper.license)
            if biorxiv_src.attach_fulltext(http, paper):
                why = "" if level == "full" else " (licence forbids redistribution: mentions kept, text not stored)"
                return level, f"preprint JATS, licence {paper.license or 'unknown'}{why}"
            notes.append("biorxiv: JATS not retrieved (the host answers 429 to scripted clients, and a fresh version may have no XML yet)")
    if not notes:
        notes.append("no open-access text (no PMC id, no preprint XML)")
    return "none", "; ".join(notes)


# ---- adding -------------------------------------------------------------------------------------
@dataclass
class AddResult:
    raw: str
    identifier: str = ""
    error: str = ""
    paper_id: str = ""
    new: bool = False
    title: str = ""
    journal: str = ""
    pub_date: str = ""
    kind: str = ""
    doi: str = ""
    url: str = ""
    published_doi: str = ""
    sources: List[str] = field(default_factory=list)
    resolved_via: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    instruments: List[Dict[str, Any]] = field(default_factory=list)
    companies: List[Dict[str, Any]] = field(default_factory=list)
    tracked_authors: List[Dict[str, str]] = field(default_factory=list)
    score: Optional[float] = None
    reasons: str = ""
    fulltext_level: str = ""
    fulltext_note: str = ""
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _group(mentions: Iterable[Mention], kind: str) -> List[Dict[str, Any]]:
    by: Dict[str, Dict[str, Any]] = {}
    for m in mentions:
        if m.kind != kind:
            continue
        d = by.setdefault(m.entity_id, {"id": m.entity_id, "name": m.entity_name, "vendor": m.vendor, "ticker": m.ticker, "sections": [], "count": 0})
        if m.section not in d["sections"]:
            d["sections"].append(m.section)
        d["count"] += m.count
    return sorted(by.values(), key=lambda d: (-d["count"], d["name"]))


def add_paper(pipe: Pipeline, paper: Paper, fulltext: bool = True, tags: Sequence[str] = (), raw_id: str = "", result: Optional[AddResult] = None) -> AddResult:
    """Full text (policy permitting) -> extract -> upsert -> mentions/tracked hits -> rank, exactly as Pipeline.run does."""
    r = result or AddResult(raw=raw_id)
    store = pipe.store
    existing = store.get_paper(paper.id)
    prev_extra = json.loads(existing.get("extra_json") or "{}") if existing else {}
    metadata_sections = dict(paper.sections)  # what the indexes gave us (e.g. funders); kept at every level
    level, note = attach_fulltext(pipe.http, paper, pipe.cfg, enabled=fulltext)
    if level == "none" and existing:
        # a refresh must not erase text stored by an earlier run or add
        prev = pipe.paper_from_row(existing)
        for k, v in prev.sections.items():
            if v and not paper.sections.get(k):
                paper.sections[k] = v
        if prev.fulltext_available:
            paper.fulltext_available = True
            level, note = prev_extra.get("fulltext_level") or "full", "kept from an earlier run"
    mentions, hits = pipe.extract(paper)
    if level == "facts_only":
        paper.sections = metadata_sections
        paper.fulltext_available = False
        paper.extra.pop("emails", None)
    paper.extra["tags"] = sorted({*(prev_extra.get("tags") or []), *(t.strip() for t in tags if t and t.strip())})
    paper.extra["added_at"] = prev_extra.get("added_at") or _now()
    paper.extra["added_ids"] = sorted({*(prev_extra.get("added_ids") or []), *([raw_id] if raw_id else [])})
    paper.extra["fulltext_level"] = level
    new = store.upsert_paper(paper)
    store.replace_mentions(paper.id, mentions)
    store.conn.execute("DELETE FROM tracked_author_hits WHERE paper_id=?", (paper.id,))
    for name, grp, how in hits:
        store.add_tracked_author_hit(paper.id, name, grp, how)
    store.commit()
    pipe.rank({paper.id: paper}, use_llm=False)
    score = store.get_score(paper.id) or {}

    r.paper_id, r.new = paper.id, new
    r.title, r.journal, r.pub_date, r.kind = paper.title, paper.journal, paper.pub_date, paper.kind
    r.doi, r.url, r.published_doi = paper.doi or "", paper.url, paper.published_doi or ""
    r.sources = list(paper.extra.get("sources") or [paper.source])
    r.instruments, r.companies = _group(mentions, "instrument"), _group(mentions, "company")
    r.tracked_authors = [{"name": n, "subgroup": g, "matched_by": how} for n, g, how in hits]
    r.score, r.reasons = score.get("final"), score.get("reasons") or ""
    r.fulltext_level, r.fulltext_note, r.tags = level, note, list(paper.extra["tags"])
    return r


def add_papers(pipe: Pipeline, raw_ids: Sequence[str], fulltext: bool = True, tags: Sequence[str] = ()) -> List[AddResult]:
    resolver = Resolver(pipe.http, pipe.settings.contact_email)
    results: List[AddResult] = []
    for raw in raw_ids:
        r = AddResult(raw=raw)
        results.append(r)
        try:
            ident = parse_identifier(raw)
        except ValueError as exc:
            r.error = str(exc)
            continue
        r.identifier = str(ident)
        try:
            res = resolver.resolve(ident)
        except Exception as exc:  # noqa: BLE001 - one bad lookup must not stop the rest of the list
            log.exception("resolving %s failed", ident)
            r.error = f"lookup failed: {exc}"
            continue
        r.resolved_via, r.notes = res.via, res.notes
        if res.paper is None:
            r.error = "no record found (" + "; ".join(res.notes) + ")"
            continue
        add_paper(pipe, res.paper, fulltext=fulltext, tags=tags, raw_id=raw, result=r)
    return results


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def format_result(r: AddResult) -> str:
    """One-screen summary of one add."""
    pad = " " * 8
    if r.error:
        lines = [f"FAILED  {r.raw}", f"{pad}{r.error}"]
        return "\n".join(lines)
    lines = [f"{'NEW    ' if r.new else 'REFRESH'} {r.paper_id}", f"{pad}{_clip(r.title, 100)}"]
    meta = " · ".join(x for x in (r.journal or (r.sources[0] if r.sources else ""), r.pub_date or "n.d.", r.kind) if x)
    lines.append(f"{pad}{meta} · via {', '.join(r.resolved_via)}")
    if r.published_doi:
        lines.append(f"{pad}published as doi:{r.published_doi}")
    inst = ", ".join(f"{d['name']} [{'/'.join(d['sections'])}]" for d in r.instruments[:8]) or "none found"
    comp = ", ".join(f"{d['name']} [{'/'.join(d['sections'])}]" for d in r.companies[:8]) or "none found"
    lines.append(f"{pad}instruments  {inst}")
    lines.append(f"{pad}companies    {comp}")
    if r.tracked_authors:
        lines.append(f"{pad}tracked      " + ", ".join(f"{t['name']} ({t['subgroup']})" for t in r.tracked_authors))
    score = f"{r.score:.2f}" if r.score is not None else "n/a"
    lines.append(f"{pad}score        {score}  {r.reasons}")
    lines.append(f"{pad}full text    {r.fulltext_level}: {r.fulltext_note}")
    if r.tags:
        lines.append(f"{pad}tags         {', '.join(r.tags)}")
    if r.notes:
        lines.append(f"{pad}not found at {'; '.join(r.notes)}")
    if r.url:
        lines.append(f"{pad}{r.url}")
    return "\n".join(lines)


# ---- tags ---------------------------------------------------------------------------------------
def tagged_papers(store: Store, tag: Optional[str] = None) -> List[Dict[str, Any]]:
    """Papers carrying a tag from `litradar add --tag` (all tagged papers when tag is None)."""
    cur = store.conn.execute(
        """SELECT id, doi, title, journal, pub_date, url, kind, extra_json FROM papers
           WHERE extra_json LIKE '%"tags"%' ORDER BY pub_date DESC, id"""
    )
    want = (tag or "").strip().lower()
    out: List[Dict[str, Any]] = []
    for pid, doi, title, journal, pub_date, url, kind, extra_json in cur.fetchall():
        extra = json.loads(extra_json or "{}")
        tags = [t for t in (extra.get("tags") or []) if isinstance(t, str)]
        if not tags or (want and want not in {t.lower() for t in tags}):
            continue
        out.append({"id": pid, "doi": doi, "title": title, "journal": journal, "pub_date": pub_date, "url": url, "kind": kind,
                    "tags": tags, "added_at": extra.get("added_at")})
    return out


def tag_counts(store: Store) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in tagged_papers(store):
        for t in row["tags"]:
            counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
