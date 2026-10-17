"""The weekly pipeline: fetch -> merge -> full text -> extract -> rank -> store.

Everything is idempotent: re-running the same window updates records in place. Each stage
logs counts so the digest can report coverage honestly (what was pulled, what had full text,
what was scored by the LLM, what failed).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import policy
from .config import Settings
from .extract.gazetteer import Gazetteer
from .http import Http
from .models import ArchiveRecord, Author, Mention, NewsItem, Paper
from .rank import heuristics
from .sources import arxiv as arxiv_src
from .sources import biorxiv as biorxiv_src
from .sources import cellxgene as cellxgene_src
from .sources import clinicaltrials as ctgov_src
from .sources import crossref as crossref_src
from .sources import ena as ena_src
from .sources import europepmc as epmc_src
from .sources import geo as geo_src
from .sources import nih_reporter as nih_src
from .sources import openalex as openalex_src
from .sources import openfda as openfda_src
from .sources import pride as pride_src
from .sources import rss as rss_src
from .sources import semantic_scholar as s2_src
from .sources import sra as sra_src
from .store import FULLTEXT_SECTIONS, Store, restricted_sections

log = logging.getLogger(__name__)

INSTRUMENT_SECTIONS = ("title", "abstract", "methods")
COMPANY_SECTIONS = ("title", "abstract", "methods", "funding", "coi", "ack", "affiliations")
ARCHIVE_SOURCES = ("sra", "ena", "geo", "pride", "cellxgene")
SIGNAL_SOURCES = ("nih_reporter", "clinicaltrials", "openfda")
SIGNAL_FEEDS = ("nih-reporter", "clinicaltrials", "fda-510k", "fda-pma", "fda-denovo")
DEFAULT_NAME_MATCH_CAP = 5  # per run, per tracked name, when the match is by name only (common names)


def _norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]", " ", s.lower())
    parts = s.split()
    if not parts:
        return ""
    # "First M. Last" and "Last, First" both reduce to "first last"
    return f"{parts[0]} {parts[-1]}" if len(parts) > 1 else parts[0]


class Pipeline:
    def __init__(self, settings: Settings, http: Optional[Http] = None, store: Optional[Store] = None) -> None:
        self.settings = settings
        self.cfg = settings.radar or {}
        if http is not None:
            self.http = http
        else:
            http_kwargs = dict(contact_email=settings.contact_email, cache_dir=settings.cache_dir, ncbi_api_key=settings.ncbi_api_key)
            try:
                from .browser import BrowserUnavailable, make_http

                try:
                    self.http = make_http(settings, **http_kwargs)  # BrowserHttp when radar.yaml browser.enabled is true
                except BrowserUnavailable as exc:
                    log.warning("browser fetcher requested but unavailable (%s); using the plain HTTP client", exc)
                    self.http = Http(**http_kwargs)
            except ImportError:
                self.http = Http(**http_kwargs)
        self.store = store or Store(settings.db_path)
        self.instruments = Gazetteer.from_yaml(settings.yaml("instruments"), "instrument")
        self.companies = Gazetteer.from_yaml(settings.yaml("companies"), "company")
        self.journals: List[Dict[str, Any]] = (settings.yaml("journals") or {}).get("journals", [])
        self.subgroups: List[Dict[str, Any]] = (settings.yaml("subgroups") or {}).get("subgroups", [])
        self.feeds: List[Dict[str, Any]] = (settings.yaml("feeds") or {}).get("feeds", [])
        self.tier_by_journal: Dict[str, str] = {}
        self.tier_by_issn: Dict[str, str] = {}
        for j in self.journals:
            self.tier_by_journal[j["name"].lower()] = j.get("tier", "")
            for issn in j.get("issn", []) or []:
                self.tier_by_issn[issn] = j.get("tier", "")
        # a person may sit in several subgroups, so each key maps to a list of (display name, subgroup)
        self.tracked: Dict[str, List[Tuple[str, str]]] = {}  # normalized name -> [(display name, subgroup)]
        self.tracked_ids: Dict[str, List[Tuple[str, str]]] = {}  # openalex id -> [(display name, subgroup)]
        for g in self.subgroups:
            for a in g.get("tracked_authors", []) or []:
                if a.get("name"):
                    self.tracked.setdefault(_norm_name(a["name"]), []).append((a["name"], g["id"]))
                if a.get("openalex_id"):
                    self.tracked_ids.setdefault(str(a["openalex_id"]), []).append((a.get("name", a["openalex_id"]), g["id"]))
        self.counts: Counter = Counter()
        self.errors: List[str] = []
        self._name_hit_counts: Counter = Counter()  # (display name, subgroup) -> name-only hits this run
        self._company_raw: Dict[str, Dict[str, Any]] = {
            str(x.get("id")): x for x in ((settings.yaml("companies") or {}).get("entries") or []) if isinstance(x, dict) and x.get("id")
        }

    # ------------------------------------------------------------------ helpers
    @property
    def company_types(self) -> Dict[str, str]:
        """{company id: type} from config/companies.yaml, for the digest's typed company tables."""
        return {cid: str(raw.get("type") or "") for cid, raw in self._company_raw.items()}

    def _company_terms(self, watch_only: bool = False, min_len: int = 5) -> List[str]:
        names = []
        for e in self.companies.entries:
            if watch_only and not (self._company_raw.get(e.id) or {}).get("watch_news"):
                continue
            if len(e.name) >= min_len:
                names.append(e.name)
        return sorted(set(names))

    def _platform_terms(self, min_len: int = 4) -> List[str]:
        return sorted({e.name for e in self.instruments.entries
                       if heuristics.is_platform(e.category) and len(e.name) >= min_len and not e.context_required})

    def _src(self, name: str) -> Dict[str, Any]:
        return (self.cfg.get("sources") or {}).get(name) or {}

    def _enabled(self, name: str, only: Optional[Sequence[str]]) -> bool:
        if only is not None:
            return name in only
        return bool(self._src(name).get("enabled", True))

    def journal_tier(self, paper: Paper) -> str:
        if paper.journal and paper.journal.lower() in self.tier_by_journal:
            return self.tier_by_journal[paper.journal.lower()]
        for issn in (paper.extra or {}).get("issn", []) or []:
            if issn in self.tier_by_issn:
                return self.tier_by_issn[issn]
        return ""

    @staticmethod
    def merge(existing: Paper, new: Paper) -> Paper:
        """Combine two records of the same paper from different sources; richer field wins."""
        if len(new.abstract or "") > len(existing.abstract or ""):
            existing.abstract = new.abstract
        if len(new.title or "") > len(existing.title or ""):
            existing.title = new.title
        if not existing.journal or existing.journal in ("biorxiv", "medrxiv", "preprint"):
            existing.journal = new.journal or existing.journal
        if not existing.pub_date or (new.pub_date and new.pub_date < existing.pub_date):
            existing.pub_date = new.pub_date or existing.pub_date
        if new.kind == "article" and existing.kind == "preprint" and new.source != "openalex":
            existing.kind = "article"
        if not existing.url:
            existing.url = new.url
        if not existing.license:
            existing.license = new.license
        if not existing.published_doi:
            existing.published_doi = new.published_doi
        if not existing.fulltext_url:
            existing.fulltext_url = new.fulltext_url
        # authors: prefer the list with institution metadata, else the longer one
        def richness(authors):
            return sum(1 for a in authors if a.affiliation or a.institutions or a.orcid) * 10 + len(authors)

        if richness(new.authors) > richness(existing.authors):
            existing.authors = new.authors
        existing.affiliations = list(dict.fromkeys(existing.affiliations + new.affiliations))
        for k, v in new.sections.items():
            if len(v) > len(existing.sections.get(k, "")):
                existing.sections[k] = v
        existing.fulltext_available = existing.fulltext_available or new.fulltext_available
        existing.cited_by_count = max(existing.cited_by_count, new.cited_by_count)
        existing.topics = list(dict.fromkeys(existing.topics + new.topics))
        srcs = set(existing.extra.get("sources", [existing.source])) | {new.source}
        for k, v in new.extra.items():
            # an empty value from the base record must not block a real one from another source
            if k not in existing.extra or existing.extra[k] in (None, "", [], {}):
                existing.extra[k] = v
        existing.extra["sources"] = sorted(srcs)
        return existing

    # ------------------------------------------------------------------ fetch
    def fetch_papers(self, since: str, until: str, only: Optional[Sequence[str]] = None) -> Dict[str, Paper]:
        papers: Dict[str, Paper] = {}

        def add(p: Paper, src: str) -> None:
            self.counts[f"fetched.{src}"] += 1
            if p.id in papers:
                self.merge(papers[p.id], p)
            else:
                papers[p.id] = p

        # 1. bioRxiv / medRxiv
        if self._enabled("biorxiv", only):
            cfg = self._src("biorxiv")
            for server in cfg.get("servers", ["biorxiv"]):
                cats = cfg.get("medrxiv_categories" if server == "medrxiv" else "categories") or None
                try:
                    for p in biorxiv_src.fetch(self.http, since, until, server=server, categories=cats):
                        add(p, server)
                except Exception as exc:  # noqa: BLE001 - a dead source must not kill the run
                    self.errors.append(f"{server}: {exc}")
                    log.exception("%s failed", server)

        # 2. Europe PMC: journals by ISSN, instrument names in METHODS, company names in COI/funding
        if self._enabled("europepmc", only):
            cfg = self._src("europepmc")
            maxr = int(cfg.get("max_records_per_query", 3000))
            try:
                if cfg.get("journals", True):
                    issns = [i for j in self.journals for i in (j.get("issn") or [])]
                    for chunk in epmc_src.chunked(issns, 25):
                        for rec in epmc_src.search(self.http, epmc_src.journal_query(chunk, since, until), max_records=maxr):
                            add(epmc_src.to_paper(rec), "europepmc.journals")
                if cfg.get("methods_search", True):
                    pull_cats = tuple(cfg.get("methods_search_categories") or ["sequencer", "single_cell", "spatial", "proteomics", "synthesis", "cryo_em", "array", "cell_engineering"])
                    terms = sorted({a for e in self.instruments.entries if e.category.startswith(pull_cats)
                                    for a in e.aliases if len(a) >= 5 and a not in e.case_sensitive_aliases and not e.context_required})
                    self.counts["europepmc.methods_terms"] = len(terms)
                    for chunk in epmc_src.chunked(terms, 40):
                        q = epmc_src.section_query("METHODS", chunk, since, until)
                        for rec in epmc_src.search(self.http, q, max_records=maxr):
                            add(epmc_src.to_paper(rec), "europepmc.methods")
                if cfg.get("coi_search", True):
                    names = sorted({e.name for e in self.companies.entries if len(e.name) >= 5})
                    for chunk in epmc_src.chunked(names, 40):
                        for field in ("COMP_INT", "ACK_FUND"):
                            q = epmc_src.section_query(field, chunk, since, until)
                            for rec in epmc_src.search(self.http, q, max_records=maxr):
                                add(epmc_src.to_paper(rec), f"europepmc.{field.lower()}")
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"europepmc: {exc}")
                log.exception("europepmc failed")

        # 3. OpenAlex: topics, company-affiliated authors, tracked authors, journals
        if self._enabled("openalex", only):
            cfg = self._src("openalex")
            maxr = int(cfg.get("max_records_per_query", 2000))
            base = {"from_publication_date": since, "to_publication_date": until}
            mail, key = self.settings.contact_email, self.settings.openalex_api_key
            try:
                if cfg.get("topics", True):
                    # one query per subgroup: its topics (primary + supporting) intersected with its keywords,
                    # because supporting topics are broad (hundreds of thousands of works)
                    for g in self.subgroups:
                        topics = [t for t in (g.get("openalex_topics") or []) if t]
                        kws = [k for k in (g.get("keywords") or []) if k]
                        if not topics:
                            continue
                        flt = dict(base, **{"primary_topic.id": openalex_src.or_join(topics)})
                        if kws:
                            flt["title_and_abstract.search"] = openalex_src.or_join(kws[:40])
                        for w in openalex_src.works(self.http, flt, mail, key, maxr):
                            add(openalex_src.to_paper(w), "openalex.topics")
                if cfg.get("company_authors", True):
                    kw = sorted({k for g in self.subgroups for k in (g.get("keywords") or [])})
                    # OpenAlex search filters accept OR via |; keep the query short
                    search = openalex_src.or_join(kw[:60])
                    flt = dict(base, **{"authorships.institutions.type": "company", "title_and_abstract.search": search})
                    for w in openalex_src.works(self.http, flt, mail, key, maxr):
                        add(openalex_src.to_paper(w), "openalex.company_authors")
                if cfg.get("tracked_authors", True) and self.tracked_ids:
                    for chunk in epmc_src.chunked(list(self.tracked_ids), 25):
                        flt = dict(base, **{"authorships.author.id": openalex_src.or_join(chunk)})
                        for w in openalex_src.works(self.http, flt, mail, key, maxr):
                            add(openalex_src.to_paper(w), "openalex.tracked_authors")
                if cfg.get("journals", True):
                    issns = [i for j in self.journals for i in (j.get("issn") or [])]
                    for chunk in epmc_src.chunked(issns, 30):
                        flt = dict(base, **{"primary_location.source.issn": openalex_src.or_join(chunk)})
                        for w in openalex_src.works(self.http, flt, mail, key, maxr):
                            add(openalex_src.to_paper(w), "openalex.journals")
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"openalex: {exc}")
                log.exception("openalex failed")

        # 4. Crossref per journal
        if self._enabled("crossref", only):
            try:
                for j in self.journals:
                    issn = (j.get("issn") or [None])[0]
                    if not issn:
                        continue
                    for it in crossref_src.journal_works(self.http, issn, since, until, self.settings.contact_email):
                        if it.get("type") not in crossref_src.ARTICLE_TYPES:
                            continue
                        p = crossref_src.to_paper(it, j["name"])
                        p.extra["issn"] = j.get("issn") or []
                        p.journal = j["name"]
                        add(p, "crossref")
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"crossref: {exc}")
                log.exception("crossref failed")

        # 5. arXiv
        if self._enabled("arxiv", only):
            cfg = self._src("arxiv")
            try:
                for p in arxiv_src.fetch(self.http, since, until, cfg.get("categories") or ["q-bio.GN"]):
                    add(p, "arxiv")
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"arxiv: {exc}")
                log.exception("arxiv failed")

        # 6. Journal RSS tables of contents (metadata backstop; links carry DOIs) + publisher subject feeds
        if self._enabled("rss", only):
            for j in self.journals:
                if not j.get("rss"):
                    continue
                try:
                    for p in rss_src.journal_papers(self.http, j["name"], j["rss"], j["name"], since=since):
                        p.extra["issn"] = j.get("issn") or []
                        add(p, "rss.journals")
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"rss {j['name']}: {exc}")
            for f in (self.settings.yaml("journals") or {}).get("subject_feeds", []) or []:
                try:
                    for p in rss_src.journal_papers(self.http, f["name"], f["url"], "", since=since):
                        add(p, "rss.subjects")
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"subject feed {f['name']}: {exc}")

        log.info("fetched %d unique papers", len(papers))
        self.counts["papers.unique"] = len(papers)
        return papers

    # ------------------------------------------------------------------ full text
    def attach_fulltext(self, papers: Dict[str, Paper]) -> None:
        bcfg, ecfg = self._src("biorxiv"), self._src("europepmc")
        want_b, want_e = bool(bcfg.get("fulltext", True)), bool(ecfg.get("fulltext", True))
        cap_b, cap_e = int(bcfg.get("fulltext_max", 500)), int(ecfg.get("fulltext_max", 400))
        # prioritise: watchlist / instrument / company hits in the abstract first
        wl = [t.lower() for t in self.cfg.get("watchlist_terms", [])]

        def priority(p: Paper) -> float:
            text = f"{p.title} {p.abstract}".lower()
            score = sum(1 for t in wl if t in text)
            score += 2 * len(self.instruments.find(p.abstract or ""))
            score += 1 * len(self.companies.find(p.abstract or ""))
            if self.journal_tier(p) == "A":
                score += 2
            return -score

        ordered = sorted(papers.values(), key=priority)
        nb = ne = 0
        for p in ordered:
            if p.fulltext_available:
                continue
            can_b = want_b and bool(p.fulltext_url) and p.source in ("biorxiv", "medrxiv")
            can_e = want_e and bool((p.extra or {}).get("pmcid"))
            if not (can_b or can_e):
                continue
            # decide what may be kept BEFORE fetching: 'full' keeps sections and body, 'facts_only' keeps only the
            # derived mentions / snippets, 'none' skips the fetch
            level = self.fulltext_policy(p)
            self.counts[f"fulltext.policy_{level}"] += 1
            if level == "none":
                continue
            p.extra["fulltext_policy"] = level
            if can_b and nb < cap_b:
                nb += 1
                if biorxiv_src.attach_fulltext(self.http, p):
                    self.counts["fulltext.biorxiv"] += 1
                else:
                    self.counts["fulltext.biorxiv_failed"] += 1
                continue
            if can_e and ne < cap_e:
                ne += 1
                if epmc_src.attach_fulltext(self.http, p):
                    self.counts["fulltext.europepmc"] += 1
                else:
                    self.counts["fulltext.europepmc_failed"] += 1

    @staticmethod
    def fulltext_policy(p: Paper) -> str:
        """Storage level for a paper's full text (litradar/policy.py): bioRxiv -> Paper.license, Europe PMC ->
        extra['is_open_access'] == 'Y'."""
        extra = p.extra or {}
        open_access = str(extra.get("is_open_access") or "").upper() == "Y"
        has_text = bool(p.fulltext_url or extra.get("pmcid"))
        return policy.fulltext_storage_level(p.license, open_access=open_access, text_available=has_text)

    @staticmethod
    def apply_fulltext_policy(p: Paper) -> Tuple[str, ...]:
        """Enforce the paper's level on the in-memory object before it is stored.

        Returns the sections whose stored mentions must survive future re-ingests (because their text is not kept)."""
        level = (p.extra or {}).get("fulltext_policy")
        if not level or level == "full":
            return ()
        p.sections = restricted_sections(p.sections, p.extra or {})
        return FULLTEXT_SECTIONS

    def _merge_stored(self, row: Dict[str, Any], fresh: Paper) -> Paper:
        """Fold the stored version of a paper into a freshly fetched one so a metadata-only re-fetch keeps the
        stored Methods / funding / COI text (and therefore its mentions). Fresh metadata wins where it is non-empty."""
        stored = self.paper_from_row(row)
        merged = self.merge(stored, fresh)
        for k, v in (fresh.extra or {}).items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            if k == "sources":
                continue
            merged.extra[k] = v
        if not merged.source or merged.source.startswith("rss:"):
            merged.source = fresh.source
        if fresh.cited_by_count > merged.cited_by_count:
            merged.cited_by_count = fresh.cited_by_count
        return merged

    # ------------------------------------------------------------------ tracked-author hits
    def _record_hits(self, paper_id: str, hits: Sequence[Tuple[str, str, str]]) -> int:
        """Store tracked-author hits; name-only matches are capped per tracked name per run (common names)."""
        cap = int((self.cfg.get("tracked_authors") or {}).get("name_match_cap", DEFAULT_NAME_MATCH_CAP))
        n = 0
        for name, grp, how in hits:
            if how == "name":
                key = (name, grp)
                self._name_hit_counts[key] += 1
                if self._name_hit_counts[key] > cap:
                    self.counts["tracked_hits.name_capped"] += 1
                    continue
            self.store.add_tracked_author_hit(paper_id, name, grp, how)
            n += 1
        return n

    def _log_name_overflow(self) -> None:
        cap = int((self.cfg.get("tracked_authors") or {}).get("name_match_cap", DEFAULT_NAME_MATCH_CAP))
        for (name, grp), count in sorted(self._name_hit_counts.items()):
            if count > cap:
                log.warning("tracked author %r (%s): %d name-only matches this run, kept the first %d; add an OpenAlex id to make matching exact",
                            name, grp, count, cap)

    # ------------------------------------------------------------------ extract
    def extract(self, p: Paper) -> Tuple[List[Mention], List[Tuple[str, str, str]]]:
        mentions: List[Mention] = []
        sections = list(INSTRUMENT_SECTIONS)
        if not p.sections.get("methods") and p.sections.get("body"):
            sections.append("body")
        for sec in sections:
            mentions.extend(self.instruments.find(p.text_for(sec), section="methods" if sec == "body" else sec, paper_id=p.id))
        for sec in COMPANY_SECTIONS:
            mentions.extend(self.companies.find(p.text_for(sec), section=sec, paper_id=p.id))
        # corporate authors via e-mail domains and OpenAlex institution typing
        emails = (p.extra or {}).get("emails", []) + [a.email for a in p.authors if a.email]
        for e in self.companies.match_domains(emails):
            mentions.append(Mention(p.id, "company", e.id, e.name, e.parent, e.ticker, "author-email", "affiliations", 1, "author e-mail domain"))
        for inst_name in (p.extra or {}).get("company_institutions", []) or []:
            hits = self.companies.find(inst_name)
            if hits:
                h = hits[0]
                mentions.append(Mention(p.id, "company", h.entity_id, h.entity_name, h.vendor, h.ticker, "openalex-company", "affiliations", 1, inst_name))
            else:
                # unknown company institution: record it under a synthetic id so new entrants surface
                mentions.append(Mention(p.id, "company", "unlisted:" + re.sub(r"[^a-z0-9]+", "-", inst_name.lower()).strip("-"), inst_name, "", "", "openalex-company", "affiliations", 1, inst_name))
        # collapse duplicates (same entity + section)
        merged: Dict[Tuple[str, str, str], Mention] = {}
        for m in mentions:
            key = (m.kind, m.entity_id, m.section)
            if key in merged:
                merged[key].count += m.count
            else:
                merged[key] = m
        # tracked authors
        hits: List[Tuple[str, str, str]] = []
        for a in p.authors:
            if a.openalex_id and a.openalex_id in self.tracked_ids:
                for name, grp in self.tracked_ids[a.openalex_id]:
                    hits.append((name, grp, "openalex_id"))
            elif _norm_name(a.name) in self.tracked:
                for name, grp in self.tracked[_norm_name(a.name)]:
                    hits.append((name, grp, "name"))
        return list(merged.values()), sorted(set(hits))

    def assign_subgroups(self, p: Paper) -> List[str]:
        text = f"{p.title} {p.abstract}".lower()
        out = []
        for g in self.subgroups:
            if any(k.lower() in text for k in g.get("keywords", [])):
                out.append(g["id"])
            elif (p.extra or {}).get("primary_topic") in (g.get("openalex_topics") or []):
                out.append(g["id"])
        return out

    # ------------------------------------------------------------------ archives & news
    def _store_archive(self, records: Iterable[ArchiveRecord], label: str) -> int:
        n = 0
        batch: List[ArchiveRecord] = []
        for r in records:
            if not r.entity_id:  # adapters that map assays themselves (CELLxGENE) keep their id
                e = self.instruments.match_archive_model(r.instrument_model)
                if e:
                    r.entity_id, r.vendor, r.ticker = e.id, e.vendor, e.ticker
            batch.append(r)
            if len(batch) >= 2000:
                n += self.store.upsert_archive_records(batch)
                self.store.commit()
                batch = []
        n += self.store.upsert_archive_records(batch)
        self.store.commit()
        self.counts[f"archive.{label}_new"] = n
        return n

    def fetch_archives(self, since: str, until: str, only: Optional[Sequence[str]] = None) -> int:
        """SRA (NCBI; mirrors most of the world) first, then ENA for ERX/DRX records SRA has not mirrored yet."""
        n = 0
        if self._enabled("sra", only):
            cfg = self._src("sra")
            try:
                n += self._store_archive(sra_src.fetch(self.http, since, until, api_key=self.settings.ncbi_api_key, max_records=int(cfg.get("max_records", 150000))), "sra")
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"sra: {exc}")
                log.exception("sra failed")
        if self._enabled("ena", only):
            cfg = self._src("ena")
            try:
                n += self._store_archive(ena_src.fetch(self.http, since, until, limit=int(cfg.get("limit", 0))), "ena")
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"ena: {exc}")
                log.exception("ena failed")
        # study-level censuses: GEO series x platform, PRIDE proteomics datasets x instrument, CELLxGENE datasets x assay
        if self._enabled("geo", only):
            cfg = self._src("geo")
            try:
                extra = '"high-throughput sequencing"[PTYP]' if cfg.get("sequencing_only", True) else ""
                n += self._store_archive(
                    geo_src.fetch(self.http, since, until, api_key=self.settings.ncbi_api_key,
                                  max_records=int(cfg.get("max_records", 20000)), extra_term=extra),
                    "geo",
                )
                if geo_src.sweep_error():
                    self.errors.append(geo_src.sweep_error())
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"geo: {exc}")
                log.exception("geo failed")
        if self._enabled("pride", only):
            cfg = self._src("pride")
            try:
                n += self._store_archive(pride_src.fetch(self.http, since, until, max_projects=int(cfg.get("max_projects", 0))), "pride")
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"pride: {exc}")
                log.exception("pride failed")
        if self._enabled("cellxgene", only):
            cfg = self._src("cellxgene")
            try:
                n += self._store_archive(
                    cellxgene_src.fetch(
                        self.http, since, until,
                        include_revisions=bool(cfg.get("include_revisions", False)),
                        max_records=int(cfg.get("max_records", 0)),
                        max_collection_details=int(cfg.get("max_collection_details", 50)),
                    ),
                    "cellxgene",
                )
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"cellxgene: {exc}")
                log.exception("cellxgene failed")
        return n

    def fetch_news(self, since: str) -> int:
        """Trade press, press wires, SEC 8-K filings and per-company news.

        Layers (all configurable under ``news:`` and ``sources:`` in radar.yaml):
        * publisher RSS / press-wire RSS from config/feeds.yaml (kinds ``news`` and ``press``);
        * SEC EDGAR 8-K Atom per watched company with a plain US ticker (kind ``sec``; needs
          LITRADAR_CONTACT_EMAIL because EDGAR requires ``<tool> <e-mail>`` in the User-Agent);
        * GDELT DOC 2.0 per watched company (kind ``gdelt``; feed name ``gdelt:<company>``), query =
          ``"<company name>"`` + the sector clause from ``news.query_context`` (or the company's own
          ``news_query``). Built for programmatic use; one request per 5 s;
        * Google News RSS (kind ``gnews``) only when ``news.google_news: true``: news.google.com/robots.txt
          disallows ``/rss/`` and Google's Terms prohibit automated access that ignores it, so this is an
          explicit opt-in for the fork owner.

        Every item passes the ``news.exclude_title_patterns`` filter, and a per-company item (GDELT or
        Google News) is kept only when the gazetteer actually finds that company in its text.
        """
        from urllib.parse import quote_plus

        from .sources import gdelt as gdelt_src

        n = 0
        news_cfg = self.cfg.get("news") or {}
        google_on = bool(news_cfg.get("google_news", False))
        gdelt_on = bool(news_cfg.get("gdelt", True))
        gdelt_timespan = str(news_cfg.get("gdelt_timespan") or "7d")
        gdelt_language = str(news_cfg.get("gdelt_language") or "english")
        feeds: List[Dict[str, Any]] = []
        for f in self.feeds:
            if f.get("kind") == "gnews" and not google_on:
                self.counts["news.gnews_skipped"] += 1
                continue
            feeds.append(dict(f))
        if self.feeds and not google_on and self.counts["news.gnews_skipped"]:
            log.info("Google News RSS is opt-in (news.google_news: false); skipped %d configured gnews feed(s)", self.counts["news.gnews_skipped"])
        raw_by_id = {x.get("id"): x for x in (self.settings.yaml("companies") or {}).get("entries", [])}
        sec_on = bool(self._src("sec").get("enabled", True))
        if sec_on and not self.settings.contact_email:
            msg = "SEC 8-K feeds skipped: LITRADAR_CONTACT_EMAIL not set (EDGAR requires '<tool> <contact e-mail>' in the User-Agent and answers 403 otherwise)"
            log.warning(msg)
            self.errors.append(msg)
            sec_on = False
        context_by_type = news_cfg.get("query_context") or {
            "tools": "(sequencing OR genomics OR proteomics OR \"life sciences\" OR instrument OR biotech)",
            "diagnostics": "(cancer OR diagnostic OR test OR genomics OR biotech OR FDA OR Medicare)",
            "synthesis": "(DNA OR \"synthetic biology\" OR biotech OR genomics)",
            "ai_bio": "(AI OR \"drug discovery\" OR protein OR biology OR biotech)",
            "pharma": "(FDA OR trial OR drug OR pharma OR biotech)",
            "cro": "(biotech OR pharma OR CDMO OR laboratory)",
            "institute": "(research OR genomics OR biology)",
        }
        exclude_re = None
        pats = news_cfg.get("exclude_title_patterns") or []
        if pats:
            exclude_re = re.compile("|".join(pats), re.I)

        def keep(it: NewsItem, company: Optional[str]) -> bool:
            """Exclusion patterns + gazetteer gating; sets it.companies. False = drop."""
            if exclude_re and exclude_re.search(it.title or ""):
                self.counts["news.excluded"] += 1
                return False
            text = f"{it.title} {it.summary}"
            comps = {m.entity_id for m in self.companies.find(text)}
            if company and company not in comps:
                self.counts["news.offtopic"] += 1
                return False  # a company query whose text never names the company (e.g. "holy grail")
            it.companies = sorted(comps)
            return True

        company_feeds: List[Dict[str, Any]] = []
        for e in self.companies.entries:
            raw = raw_by_id.get(e.id, {})
            if not raw.get("watch_news"):
                continue
            ctx = raw.get("news_query") or context_by_type.get(raw.get("type") or "tools", "")
            if gdelt_on:
                company_feeds.append({"name": f"gdelt:{e.name}", "kind": "gdelt", "company": e.id, "company_name": e.name, "context": ctx})
            if google_on:
                q = f'"{e.name}" {ctx} when:7d'.strip()
                feeds.append({"name": f"news:{e.name}", "kind": "gnews", "company": e.id,
                              "url": f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"})
            if sec_on and re.fullmatch(r"[A-Z]{1,5}", e.ticker or ""):
                feeds.append({"name": f"SEC 8-K {e.ticker}", "kind": "sec", "company": e.id, "company_name": e.name,
                              "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={e.ticker}&type=8-K&dateb=&owner=include&count=20&output=atom"})
        for f in feeds:
            try:
                items: List[NewsItem] = []
                for it in rss_src.news_items(self.http, f["name"], f["url"], since=since):
                    if f.get("kind") == "sec":
                        it.title = f"{f.get('company_name')}: {it.title} ({it.published})"
                    if not keep(it, f.get("company") if f.get("kind") == "gnews" else None):
                        continue
                    if f.get("kind") == "sec" and f.get("company") and f["company"] not in it.companies:
                        it.companies = sorted(set(it.companies) | {f["company"]})
                    items.append(it)
                added = self.store.upsert_news(items)
                n += added
                self.counts[f"news.{f['kind']}"] += added
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"feed {f['name']}: {exc}")
        for f in company_feeds:
            if gdelt_src.DOC_API.split("/")[2] in getattr(self.http, "blocked_hosts", set()):
                self.counts["news.gdelt_skipped"] += 1
                continue
            try:
                items = []
                for it in gdelt_src.company_news(self.http, f["company_name"], f["company"], context=f["context"],
                                                 timespan=gdelt_timespan, language=gdelt_language, since=since):
                    if keep(it, f["company"]):
                        items.append(it)
                added = self.store.upsert_news(items)
                n += added
                self.counts["news.gdelt"] += added
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"feed {f['name']}: {exc}")
        if self.counts.get("news.gdelt_skipped"):
            msg = f"gdelt: host rate-limited (HTTP 429) during the run; {self.counts['news.gdelt_skipped']} company quer(y/ies) skipped"
            log.warning(msg)
            self.errors.append(msg)
        self.store.commit()
        return n

    # ------------------------------------------------------------------ signals (grants, trials, clearances)
    def _tag_news(self, items: Iterable[NewsItem], require_company: bool = False, drop_counter: str = "") -> List[NewsItem]:
        """Tag news-like items with the companies the gazetteer finds in title + summary (as fetch_news does)."""
        out: List[NewsItem] = []
        for it in items:
            comps = {m.entity_id for m in self.companies.find(f"{it.title} {it.summary}")}
            if require_company and not comps:
                if drop_counter:
                    self.counts[drop_counter] += 1
                continue
            it.companies = sorted(comps)
            out.append(it)
        return out

    def fetch_signals(self, since: str, until: str, only: Optional[Sequence[str]] = None) -> int:
        """NIH RePORTER awards, ClinicalTrials.gov registrations and FDA device decisions as news items.

        Feeds: 'nih-reporter', 'clinicaltrials', 'fda-510k' / 'fda-denovo' / 'fda-pma'. Items are tagged with gazetteer
        companies and stored through Store.upsert_news; the digest renders them in their own section."""
        n = 0
        if self._enabled("nih_reporter", only):
            cfg = self._src("nih_reporter")
            errors: List[str] = []
            try:
                maxr = int(cfg.get("max_records", 2000))
                decorate = bool(cfg.get("decorate_titles", True))
                items: List[NewsItem] = []
                if cfg.get("s10", True):
                    items.extend(nih_src.s10_awards(self.http, since, until, max_records=maxr, decorate_titles=decorate, errors=errors))
                if cfg.get("keywords", True):
                    terms = (self._platform_terms() + self._company_terms())[: int(cfg.get("max_terms", 150))]
                    if terms:
                        items.extend(nih_src.keyword_awards(self.http, since, until, terms, max_records=maxr, decorate_titles=decorate, errors=errors))
                added = self.store.upsert_news(self._tag_news(items))
                n += added
                self.counts["signals.nih-reporter"] += added
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"nih_reporter: {exc}")
                log.exception("nih_reporter failed")
            self.errors.extend(f"nih_reporter: {e}" for e in errors)
        if self._enabled("clinicaltrials", only):
            cfg = self._src("clinicaltrials")
            try:
                sponsor_terms = list(cfg.get("sponsor_terms") or []) or self._company_terms(watch_only=True)
                intervention_terms = list(cfg.get("intervention_terms") or []) or list(self.cfg.get("watchlist_terms") or [])
                items = ctgov_src.new_studies(
                    self.http, since, until, sponsor_terms=sponsor_terms, intervention_terms=intervention_terms,
                    terms=list(cfg.get("terms") or []), max_records=int(cfg.get("max_records", 2000)),
                )
                added = self.store.upsert_news(self._tag_news(items))
                n += added
                self.counts["signals.clinicaltrials"] += added
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"clinicaltrials: {exc}")
                log.exception("clinicaltrials failed")
        if self._enabled("openfda", only):
            cfg = self._src("openfda")
            try:
                lookback = int(cfg.get("lookback_days", openfda_src.RECOMMENDED_LOOKBACK_DAYS))  # the datasets refresh monthly
                start = (date.fromisoformat(since) - timedelta(days=lookback)).isoformat()
                items = openfda_src.fetch(
                    self.http, start, until,
                    kinds=tuple(cfg.get("kinds") or ("510k", "pma")),
                    advisory_committees=tuple(cfg.get("advisory_committees") or ()),
                    include_supplements=bool(cfg.get("include_supplements", True)),
                    exclude_supplement_types=tuple(cfg.get("exclude_supplement_types") or ()),
                    max_records=int(cfg.get("max_records", 5000)),
                )
                kept = self._tag_news(items, require_company=bool(cfg.get("watched_applicants_only", True)), drop_counter="signals.fda_unwatched")
                by_feed: Counter = Counter()
                for it in kept:
                    if self.store.upsert_news([it]):
                        by_feed[it.feed] += 1
                for feed, c in by_feed.items():
                    self.counts[f"signals.{feed}"] += c
                    n += c
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"openfda: {exc}")
                log.exception("openfda failed")
        self.store.commit()
        return n

    def enrich_citations(self, papers: Dict[str, Paper], since: str) -> None:
        """Semantic Scholar citation counts for stored papers (and the in-memory ones about to be ranked)."""
        cfg = self._src("semantic_scholar")
        try:
            s2 = s2_src.enrich_citations(
                self.http, self.store,
                since=None if cfg.get("whole_store") else s2_src.window_start(since, int(cfg.get("lookback_days", 365))),
                api_key=self.settings.semantic_scholar_api_key,
                limit=int(cfg.get("max_papers", 5000)) or None,
            )
            for k in ("candidates", "requested", "matched", "updated", "unmatched", "failed_batches"):
                self.counts[f"semantic_scholar.{k}"] = int(s2.get(k, 0) or 0)
            s2_src.apply_to_papers(papers.values(), s2.get("citations") or {})
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"semantic_scholar: {exc}")
            log.exception("semantic_scholar failed")

    # ------------------------------------------------------------------ rank
    def rank(self, papers: Iterable[Paper], use_llm: bool = False) -> int:
        """Heuristic (and optionally LLM) scoring. Accepts any iterable of Paper (a dict's values are used), scores
        each paper as it streams past and returns how many were scored. The LLM shortlist is built on the way."""
        if isinstance(papers, dict):
            papers = papers.values()
        rcfg = self.cfg.get("ranking") or {}
        weights = rcfg.get("weights") or {}
        wl = self.cfg.get("watchlist_terms", [])
        llm_cfg = rcfg.get("llm") or {}
        priorities = self.cfg.get("priorities") or {}
        shortlist: List[Dict[str, Any]] = []
        n = 0
        for p in papers:
            n += 1
            mentions = self.store.mentions_for(p.id)
            tracked = bool(self.store.conn.execute("SELECT 1 FROM tracked_author_hits WHERE paper_id=? LIMIT 1", (p.id,)).fetchone())
            h, reasons = heuristics.score_paper(
                {"kind": p.kind, "title": p.title, "abstract": p.abstract, "cited_by_count": p.cited_by_count, "fulltext_available": p.fulltext_available},
                mentions, self.journal_tier(p), wl, tracked, weights,
            )
            if priorities:
                mult = priority_multiplier(p, mentions, p.journal, self.assign_subgroups(p), priorities=priorities)
                if mult != 1.0:
                    h = round(min(10.0, h * mult), 2)
                    reasons += f"; priority x{mult:g}"
            prev = self.store.get_score(p.id)
            llm_prev = None
            if prev and prev.get("llm_json"):
                try:
                    llm_prev = json.loads(prev["llm_json"])
                except ValueError:
                    llm_prev = None
            final = heuristics.blend(h, llm_prev.get("composite") if llm_prev else None, float(llm_cfg.get("llm_weight", 0.6)))
            self.store.save_score(p.id, h, llm_prev, final, reasons)
            if use_llm and not llm_prev and h >= float(llm_cfg.get("min_heuristic", 3.0)):
                shortlist.append({
                    "id": p.id, "title": p.title, "abstract": p.abstract, "journal": p.journal, "kind": p.kind, "pub_date": p.pub_date,
                    "instruments": sorted({m["entity_name"] for m in mentions if m["kind"] == "instrument"}),
                    "companies": sorted({m["entity_name"] for m in mentions if m["kind"] == "company"}),
                    "_h": h, "_reasons": reasons,
                })
            if n % 500 == 0:
                self.store.commit()
        self.store.commit()
        if use_llm and shortlist:
            from .rank import llm as llm_mod

            shortlist.sort(key=lambda d: -d["_h"])
            shortlist = shortlist[: int(llm_cfg.get("max_items", 300))]
            model = self.settings.llm_model
            log.info("LLM scoring %d papers with %s (est. $%.2f)", len(shortlist), model, llm_mod.estimate_cost_usd(len(shortlist), model, bool(llm_cfg.get("use_batch", True))))
            results = llm_mod.score_items(shortlist, wl, model=model, api_key=self.settings.anthropic_api_key, use_batch=bool(llm_cfg.get("use_batch", True)))
            for item in shortlist:
                res = results.get(item["id"])
                final = heuristics.blend(item["_h"], res.get("composite") if res else None, float(llm_cfg.get("llm_weight", 0.6)))
                self.store.save_score(item["id"], item["_h"], res, final, item["_reasons"] + ("; llm scored" if res else "; llm unscored"))
                self.counts["llm.scored" if res else "llm.unscored"] += 1
            self.store.commit()
        return n

    # ------------------------------------------------------------------ rematch
    def paper_from_row(self, row: Dict[str, Any]) -> Paper:
        authors = [Author(**{k: v for k, v in a.items() if k in Author.__dataclass_fields__}) for a in json.loads(row.get("authors_json") or "[]")]
        sections = json.loads(row.get("sections_json") or "{}")
        body = self.store.body_for(row["id"], rowid=row.get("_rowid"))
        if body:
            sections["body"] = body
        return Paper(
            id=row["id"], source=row.get("source") or "", kind=row.get("kind") or "article", title=row.get("title") or "",
            doi=row.get("doi"), abstract=row.get("abstract") or "", journal=row.get("journal") or "", pub_date=row.get("pub_date") or "",
            url=row.get("url") or "", license=row.get("license") or "", category=row.get("category") or "",
            published_doi=row.get("published_doi"), authors=authors, affiliations=json.loads(row.get("affiliations_json") or "[]"),
            sections=sections, fulltext_available=bool(row.get("fulltext_available")), cited_by_count=int(row.get("cited_by_count") or 0),
            topics=json.loads(row.get("topics_json") or "[]"), extra=json.loads(row.get("extra_json") or "{}"),
        )

    def _rematch_stream(self, since: Optional[str]) -> Iterator[Paper]:
        """Stream stored papers one batch at a time: re-extract, store mentions / hits, then hand the paper (without
        its body) to rank(). Nothing holds more than one batch of rows in memory."""
        n = 0
        for row in self.store.iter_papers(since=since):
            p = self.paper_from_row(row)
            mentions, hits = self.extract(p)  # on whatever text is stored, BEFORE the policy strips it
            keep: Tuple[str, ...] = ()
            has_text = any(p.sections.get(k) for k in FULLTEXT_SECTIONS)
            level = self.store.stored_fulltext_level(p.license, p.extra or {}) if (has_text or (p.extra or {}).get("fulltext_policy")) else "full"
            if level != "full":
                keep = FULLTEXT_SECTIONS  # the derived mentions stay; the text they came from does not
                if self.store.purge_paper_fulltext(p.id, level, rowid=row.get("_rowid")):
                    self.counts["fulltext.purged"] += 1
            self.counts["mentions"] += self.store.replace_mentions(p.id, mentions, keep_sections=keep)
            self.store.conn.execute("DELETE FROM tracked_author_hits WHERE paper_id=?", (p.id,))
            self.counts["tracked_hits"] += self._record_hits(p.id, hits)
            p.sections.pop("body", None)
            n += 1
            if n % 500 == 0:
                self.store.commit()
                log.info("rematch: %d papers", n)
            yield p

    def rematch(self, since: Optional[str] = None) -> Dict[str, Any]:
        """Re-run gazetteer matching, tracked-author detection and heuristic scoring over stored papers.

        Papers whose licence does not allow keeping full text (e.g. preprints cached as 'cc_no' before the data
        policy existed) are stripped on the way through, after their mentions have been re-derived."""
        self.counts["fulltext.purged"] = 0
        self.counts["rematched"] = self.rank(self._rematch_stream(since), use_llm=False)
        self.counts["archive_models_remapped"] = self.remap_archive()
        self._log_name_overflow()
        self.store.checkpoint()
        return {"since": since, "counts": dict(self.counts)}

    def remap_archive(self) -> int:
        """Re-tag archive records with the current gazetteer (one UPDATE per distinct instrument_model)."""
        models = [r[0] for r in self.store.conn.execute("SELECT DISTINCT instrument_model FROM archive_records").fetchall()]
        n = 0
        for m in models:
            e = self.instruments.match_archive_model(m)
            # CELLxGENE rows carry the adapter's own assay -> entity mapping; only fill its blanks, never override
            if e:
                self.store.conn.execute(
                    "UPDATE archive_records SET entity_id=?, vendor=?, ticker=? WHERE instrument_model=? AND (archive != 'cellxgene' OR entity_id = '')",
                    (e.id, e.vendor, e.ticker, m),
                )
            else:
                self.store.conn.execute("UPDATE archive_records SET entity_id='', vendor='', ticker='' WHERE instrument_model=? AND archive != 'cellxgene'", (m,))
            n += 1
        self.store.commit()
        return n

    # ------------------------------------------------------------------ run
    def ingest(self, papers: Dict[str, Paper]) -> int:
        """Store fetched papers: extract on the full in-memory text, enforce the data policy, upsert, replace
        mentions (keeping those whose source text is not stored) and record tracked-author hits. Returns new count."""
        new = 0
        for p in list(papers.values()):
            mentions, hits = self.extract(p)
            keep = self.apply_fulltext_policy(p)
            if self.store.upsert_paper(p):
                new += 1
            self.counts["mentions"] += self.store.replace_mentions(p.id, mentions, keep_sections=keep)
            self.store.conn.execute("DELETE FROM tracked_author_hits WHERE paper_id=?", (p.id,))
            self.counts["tracked_hits"] += self._record_hits(p.id, hits)
            p.sections.pop("body", None)  # the body is in the store (when the policy allows); free the memory
        self.store.commit()
        self.counts["papers.new"] = new
        return new

    def post_process(self, since: str, until: str, started: str) -> None:
        """Derived layers that read the freshly stored papers: language/country tags and company candidates.

        Each is isolated so a failure never costs the run; both are idempotent and re-runnable from the CLI
        (`litradar tag`, `litradar candidates refresh`)."""
        try:
            from . import i18n

            rep = i18n.tag_store(self.store, updated_since=started)
            self.counts["i18n.tagged"] = int(rep.get("tagged") or 0)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"i18n: {exc}")
            log.exception("language/country tagging failed")
        try:
            from . import discovery

            rep = discovery.refresh(self.store, self.companies, since, until)
            self.counts["candidates"] = int((rep or {}).get("candidates") or (rep or {}).get("total") or 0)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"discovery: {exc}")
            log.exception("company discovery failed")

    def run(self, since: str, until: str, use_llm: bool = False, fulltext: bool = True, only: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            papers = self.fetch_papers(since, until, only=only)
            # fold in what the store already knows BEFORE full text and extraction, so a metadata-only re-fetch
            # never wipes Methods mentions and already-parsed papers are not fetched again
            for pid in list(papers):
                row = self.store.get_paper(pid)
                if row:
                    papers[pid] = self._merge_stored(row, papers[pid])
                    self.counts["papers.reingested"] += 1
            if fulltext:
                self.attach_fulltext(papers)
            self.ingest(papers)
            if only is None or any(s in only for s in ARCHIVE_SOURCES):
                self.fetch_archives(since, until, only=only)
            if only is None or "rss" in only:
                self.fetch_news(since)
            if only is None or any(s in only for s in SIGNAL_SOURCES):
                self.fetch_signals(since, until, only=only)
            if self._enabled("semantic_scholar", only):
                self.enrich_citations(papers, since)
            self.rank(papers, use_llm=use_llm)
            self.post_process(since, until, started)
            report = {"since": since, "until": until, "counts": dict(self.counts), "errors": self.errors, "http": dict(getattr(self.http, "stats", {}) or {})}
            self.store.record_run(started, since, until, report)
            return report
        finally:
            # the caller may still render from this store, so fold the WAL into the main file here and leave
            # closing to whoever opened it (Store.close is idempotent)
            self._log_name_overflow()
            self.store.checkpoint()


def window(days: int = 7, until: Optional[date] = None) -> Tuple[str, str]:
    end = until or datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()
