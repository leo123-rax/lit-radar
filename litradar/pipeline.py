"""The weekly pipeline: fetch -> merge -> full text -> extract -> rank -> store.

Everything is idempotent: re-running the same window updates records in place. Each stage
logs counts so the digest can report coverage honestly (what was pulled, what had full text,
what was scored by the LLM, what failed).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import Settings
from .extract.gazetteer import Gazetteer
from .http import Http
from .models import ArchiveRecord, Author, Mention, NewsItem, Paper
from .rank import heuristics
from .sources import arxiv as arxiv_src
from .sources import biorxiv as biorxiv_src
from .sources import crossref as crossref_src
from .sources import ena as ena_src
from .sources import europepmc as epmc_src
from .sources import openalex as openalex_src
from .sources import rss as rss_src
from .sources import sra as sra_src
from .store import Store

log = logging.getLogger(__name__)

INSTRUMENT_SECTIONS = ("title", "abstract", "methods")
COMPANY_SECTIONS = ("title", "abstract", "methods", "funding", "coi", "ack", "affiliations")


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
        self.http = http or Http(contact_email=settings.contact_email, cache_dir=settings.cache_dir, ncbi_api_key=settings.ncbi_api_key)
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

    # ------------------------------------------------------------------ helpers
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
            existing.extra.setdefault(k, v)
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
            if want_b and p.fulltext_url and nb < cap_b and p.source in ("biorxiv", "medrxiv"):
                nb += 1
                if biorxiv_src.attach_fulltext(self.http, p):
                    self.counts["fulltext.biorxiv"] += 1
                else:
                    self.counts["fulltext.biorxiv_failed"] += 1
                continue
            if want_e and (p.extra or {}).get("pmcid") and ne < cap_e:
                ne += 1
                if epmc_src.attach_fulltext(self.http, p):
                    self.counts["fulltext.europepmc"] += 1
                else:
                    self.counts["fulltext.europepmc_failed"] += 1

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
        return n

    def fetch_news(self, since: str) -> int:
        n = 0
        feeds = list(self.feeds)
        # Google News RSS per watched company (works where publisher feeds block bots) + SEC 8-K Atom feeds
        raw_by_id = {x.get("id"): x for x in (self.settings.yaml("companies") or {}).get("entries", [])}
        sec_on = bool(self._src("sec").get("enabled", True))
        if sec_on and not self.settings.contact_email:
            log.warning("SEC EDGAR feeds skipped: set LITRADAR_CONTACT_EMAIL (the SEC requires a contact in the User-Agent and answers 403 otherwise)")
            sec_on = False
        for e in self.companies.entries:
            raw = raw_by_id.get(e.id, {})
            if raw.get("watch_news"):
                q = f'"{e.name}"'
                feeds.append({"name": f"news:{e.name}", "kind": "gnews", "url": f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en", "company": e.id})
                if sec_on and re.fullmatch(r"[A-Z]{1,5}", e.ticker or ""):
                    feeds.append({"name": f"SEC 8-K {e.ticker}", "kind": "sec", "company": e.id, "company_name": e.name,
                                  "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={e.ticker}&type=8-K&dateb=&owner=include&count=20&output=atom"})
        for f in feeds:
            try:
                items: List[NewsItem] = []
                for it in rss_src.news_items(self.http, f["name"], f["url"], since=since):
                    if f.get("kind") == "sec":
                        it.title = f"{f.get('company_name')}: {it.title} ({it.published})"
                    text = f"{it.title} {it.summary}"
                    comps = {m.entity_id for m in self.companies.find(text)}
                    if f.get("company"):
                        comps.add(f["company"])
                    it.companies = sorted(comps)
                    items.append(it)
                added = self.store.upsert_news(items)
                n += added
                self.counts[f"news.{f['kind']}"] += added
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"feed {f['name']}: {exc}")
        self.store.commit()
        return n

    # ------------------------------------------------------------------ rank
    def rank(self, papers: Dict[str, Paper], use_llm: bool = False) -> None:
        rcfg = self.cfg.get("ranking") or {}
        weights = rcfg.get("weights") or {}
        wl = self.cfg.get("watchlist_terms", [])
        llm_cfg = rcfg.get("llm") or {}
        shortlist: List[Dict[str, Any]] = []
        for p in papers.values():
            mentions = self.store.mentions_for(p.id)
            tracked = bool(self.store.conn.execute("SELECT 1 FROM tracked_author_hits WHERE paper_id=? LIMIT 1", (p.id,)).fetchone())
            h, reasons = heuristics.score_paper(
                {"kind": p.kind, "title": p.title, "abstract": p.abstract, "cited_by_count": p.cited_by_count, "fulltext_available": p.fulltext_available},
                mentions, self.journal_tier(p), wl, tracked, weights,
            )
            prev = self.store.get_score(p.id)
            llm_prev = None
            if prev and prev.get("llm_json"):
                import json

                llm_prev = json.loads(prev["llm_json"])
            final = heuristics.blend(h, llm_prev.get("composite") if llm_prev else None, float(llm_cfg.get("llm_weight", 0.6)))
            self.store.save_score(p.id, h, llm_prev, final, reasons)
            if use_llm and not llm_prev and h >= float(llm_cfg.get("min_heuristic", 3.0)):
                shortlist.append({
                    "id": p.id, "title": p.title, "abstract": p.abstract, "journal": p.journal, "kind": p.kind, "pub_date": p.pub_date,
                    "instruments": sorted({m["entity_name"] for m in mentions if m["kind"] == "instrument"}),
                    "companies": sorted({m["entity_name"] for m in mentions if m["kind"] == "company"}),
                    "_h": h, "_reasons": reasons,
                })
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

    # ------------------------------------------------------------------ rematch
    def paper_from_row(self, row: Dict[str, Any]) -> Paper:
        import json

        authors = [Author(**{k: v for k, v in a.items() if k in Author.__dataclass_fields__}) for a in json.loads(row.get("authors_json") or "[]")]
        sections = json.loads(row.get("sections_json") or "{}")
        body = self.store.conn.execute("SELECT body FROM papers_fts WHERE id=?", (row["id"],)).fetchone()
        if body and body[0]:
            sections["body"] = body[0]
        return Paper(
            id=row["id"], source=row.get("source") or "", kind=row.get("kind") or "article", title=row.get("title") or "",
            doi=row.get("doi"), abstract=row.get("abstract") or "", journal=row.get("journal") or "", pub_date=row.get("pub_date") or "",
            url=row.get("url") or "", license=row.get("license") or "", category=row.get("category") or "",
            published_doi=row.get("published_doi"), authors=authors, affiliations=json.loads(row.get("affiliations_json") or "[]"),
            sections=sections, fulltext_available=bool(row.get("fulltext_available")), cited_by_count=int(row.get("cited_by_count") or 0),
            topics=json.loads(row.get("topics_json") or "[]"), extra=json.loads(row.get("extra_json") or "{}"),
        )

    def rematch(self, since: Optional[str] = None) -> Dict[str, Any]:
        """Re-run gazetteer matching, tracked-author detection and heuristic scoring over stored papers."""
        sql = "SELECT * FROM papers"
        params: List[Any] = []
        if since:
            sql += " WHERE pub_date >= ? OR substr(first_seen,1,10) >= ?"
            params = [since, since]
        cur = self.store.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        papers: Dict[str, Paper] = {}
        for row in rows:
            p = self.paper_from_row(row)
            mentions, hits = self.extract(p)
            self.store.replace_mentions(p.id, mentions)
            self.store.conn.execute("DELETE FROM tracked_author_hits WHERE paper_id=?", (p.id,))
            for name, grp, how in hits:
                self.store.add_tracked_author_hit(p.id, name, grp, how)
            self.counts["mentions"] += len(mentions)
            self.counts["tracked_hits"] += len(hits)
            papers[p.id] = p
        self.store.commit()
        self.rank(papers, use_llm=False)
        self.counts["rematched"] = len(papers)
        return {"since": since, "counts": dict(self.counts)}

    # ------------------------------------------------------------------ run
    def run(self, since: str, until: str, use_llm: bool = False, fulltext: bool = True, only: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        papers = self.fetch_papers(since, until, only=only)
        if fulltext:
            self.attach_fulltext(papers)
        new = 0
        for p in papers.values():
            if self.store.upsert_paper(p):
                new += 1
            mentions, hits = self.extract(p)
            self.store.replace_mentions(p.id, mentions)
            for name, grp, how in hits:
                self.store.add_tracked_author_hit(p.id, name, grp, how)
            self.counts["mentions"] += len(mentions)
            self.counts["tracked_hits"] += len(hits)
        self.store.commit()
        self.counts["papers.new"] = new
        if only is None or "ena" in only or "sra" in only:
            self.fetch_archives(since, until, only=only)
        if only is None or "rss" in only:
            self.fetch_news(since)
        self.rank(papers, use_llm=use_llm)
        report = {"since": since, "until": until, "counts": dict(self.counts), "errors": self.errors, "http": dict(self.http.stats)}
        self.store.record_run(started, since, until, report)
        return report


def window(days: int = 7, until: Optional[date] = None) -> Tuple[str, str]:
    end = until or datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()
