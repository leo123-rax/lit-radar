"""SQLite + FTS5 store: papers, mentions, scores, archive records, news, runs.

One file, zero servers. The FTS table carries title, abstract, methods, funding, COI,
affiliations and (when available) the full body so `litradar search` answers questions
like "everything mentioning Xenium in the last 90 days".

Keying: `papers_fts` rows are inserted with an explicit rowid equal to `papers.rowid`, so every
lookup, delete and join goes through the integer key instead of the UNINDEXED `id` column (which
would be a full scan of the FTS table per paper). Stores written by earlier versions used arbitrary
FTS rowids; `Store.__init__` detects that once and rebuilds the FTS table (logged).

Data policy: when a paper's `extra_json.fulltext_policy` is anything but 'full', the store keeps no
full-text sections and no body for it, whatever the caller passes (see litradar/policy.py).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .models import ArchiveRecord, Mention, NewsItem, Paper, normalize_doi
from .policy import fulltext_storage_level

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  doi TEXT,
  source TEXT,
  kind TEXT,
  title TEXT,
  abstract TEXT,
  journal TEXT,
  pub_date TEXT,
  url TEXT,
  license TEXT,
  category TEXT,
  published_doi TEXT,
  authors_json TEXT,
  affiliations_json TEXT,
  sections_json TEXT,
  topics_json TEXT,
  fulltext_available INTEGER DEFAULT 0,
  cited_by_count INTEGER DEFAULT 0,
  extra_json TEXT,
  first_seen TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_pub_date ON papers(pub_date);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_first_seen ON papers(first_seen);

CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
  id UNINDEXED, title, abstract, methods, funding, coi, affiliations, body,
  tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS mentions (
  paper_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  entity_name TEXT,
  vendor TEXT,
  ticker TEXT,
  category TEXT,
  section TEXT NOT NULL,
  count INTEGER DEFAULT 1,
  snippet TEXT,
  PRIMARY KEY (paper_id, kind, entity_id, section)
);
CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(kind, entity_id);

CREATE TABLE IF NOT EXISTS scores (
  paper_id TEXT PRIMARY KEY,
  heuristic REAL,
  llm_json TEXT,
  final REAL,
  reasons TEXT,
  scored_at TEXT
);

CREATE TABLE IF NOT EXISTS archive_records (
  archive TEXT NOT NULL,
  accession TEXT NOT NULL,
  study_accession TEXT,
  instrument_model TEXT,
  platform TEXT,
  library_strategy TEXT,
  first_public TEXT,
  center_name TEXT,
  study_title TEXT,
  organism TEXT,
  entity_id TEXT,
  vendor TEXT,
  ticker TEXT,
  PRIMARY KEY (accession)
);
CREATE INDEX IF NOT EXISTS idx_archive_first_public ON archive_records(first_public);
CREATE INDEX IF NOT EXISTS idx_archive_model ON archive_records(instrument_model);

CREATE TABLE IF NOT EXISTS tracked_author_hits (
  paper_id TEXT NOT NULL,
  author_name TEXT NOT NULL,
  subgroup TEXT NOT NULL,
  matched_by TEXT,
  PRIMARY KEY (paper_id, author_name, subgroup)
);

CREATE TABLE IF NOT EXISTS news_items (
  id TEXT PRIMARY KEY,
  feed TEXT,
  title TEXT,
  url TEXT,
  published TEXT,
  summary TEXT,
  companies_json TEXT,
  first_seen TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started TEXT,
  finished TEXT,
  since TEXT,
  until TEXT,
  counts_json TEXT
);

CREATE TABLE IF NOT EXISTS timeseries (
  source TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  model TEXT NOT NULL,
  month TEXT NOT NULL,
  count INTEGER NOT NULL,
  fetched_at TEXT,
  PRIMARY KEY (source, entity_id, model, month)
);
CREATE INDEX IF NOT EXISTS idx_timeseries_entity_month ON timeseries(entity_id, month);
"""

FTS_COLUMNS = ("title", "abstract", "methods", "funding", "coi", "affiliations", "body")
# sections that only exist when full text was parsed; dropped for any policy level other than 'full'
FULLTEXT_SECTIONS = ("methods", "funding", "coi", "ack", "data_availability", "body")
# the archive window clause, shared by every digest query: published in the window, or undated and first seen in it
_WINDOW = "((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date = '' AND substr(p.first_seen,1,10) >= ?))"

# archive `platform` strings (SRA/ENA controlled vocabulary, plus the other archives' labels) -> vendor label used
# when the instrument_model string could not be mapped to a gazetteer entry
PLATFORM_LABELS = {
    "ILLUMINA": "Illumina",
    "PACBIO_SMRT": "Pacific Biosciences",
    "OXFORD_NANOPORE": "Oxford Nanopore Technologies",
    "DNBSEQ": "MGI Tech",
    "BGISEQ": "MGI Tech (BGISEQ)",
    "COMPLETE_GENOMICS": "MGI Tech (Complete Genomics)",
    "ELEMENT": "Element Biosciences",
    "ULTIMA": "Ultima Genomics",
    "GENEMIND": "GeneMind",
    "GENAPSYS": "GenapSys",
    "ION_TORRENT": "Thermo Fisher Scientific (Ion Torrent)",
    "ABI_SOLID": "Thermo Fisher Scientific (SOLiD)",
    "LS454": "Roche (454)",
    "HELICOS": "Helicos",
    "VELA_DIAGNOSTICS": "Vela Diagnostics",
    "TAPESTRI": "Mission Bio",
    "CAPILLARY": "Capillary (Sanger) sequencing",
    "MS": "Mass spectrometry",
    "AFFINITY": "Affinity proteomics",
    "SINGLE-CELL": "Single-cell assay",
    "SPATIAL": "Spatial assay",
    "HIGH-THROUGHPUT_SEQUENCING": "Sequencing (GEO)",
    "IN_SITU_OLIGONUCLEOTIDE": "Microarray (GEO)",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _json_dict(text: Optional[str]) -> Dict[str, Any]:
    try:
        d = json.loads(text) if text else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _norm_title(title: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def archive_vendor_label(vendor: Optional[str], platform: Optional[str], instrument_model: Optional[str]) -> str:
    """Vendor cell for an archive row: the mapped vendor, else a label derived from the raw platform string.

    'ILLUMINA' + 'unspecified' -> 'Illumina (model unspecified)'; 'ILLUMINA' + 'Illumina FooSeq' (unknown to the
    gazetteer) -> 'Illumina (unmapped model)'; no platform at all -> 'unknown platform' / 'vendor unmapped'.
    """
    if vendor:
        return vendor
    plat = (platform or "").strip()
    key = plat.upper().replace(" ", "_")
    base = PLATFORM_LABELS.get(key) or (plat.replace("_", " ").title() if plat else "")
    model = (instrument_model or "").strip()
    unspecified = model == "" or model.lower() in ("unspecified", "unknown", "n/a", "na")
    if not base:
        return "unknown platform" if unspecified else "vendor unmapped"
    return f"{base} (model unspecified)" if unspecified else f"{base} (unmapped model)"


def restricted_sections(sections: Dict[str, str], extra: Dict[str, Any]) -> Dict[str, str]:
    """Sections a non-'full' policy level may keep: nothing parsed from full text, funding only from grant metadata."""
    kept = {k: v for k, v in sections.items() if k not in FULLTEXT_SECTIONS and v}
    grants = extra.get("grants")
    if isinstance(grants, list) and grants:
        kept["funding"] = "; ".join(str(g) for g in grants if g)
    return kept


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._closed = False
        self.fts_rebuilt = self._ensure_fts_keyed_by_rowid()

    # ---- lifecycle ----------------------------------------------------------------------
    def commit(self) -> None:
        self.conn.commit()

    def checkpoint(self) -> None:
        """Commit and fold the WAL back into the main file (so the .sqlite-wal sidecar is empty on disk)."""
        if self._closed:
            return
        self.conn.commit()
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError as exc:  # pragma: no cover - another connection may hold the WAL
            log.warning("WAL checkpoint skipped: %s", exc)

    def close(self) -> None:
        """Checkpoint and close; safe to call more than once."""
        if self._closed:
            return
        try:
            self.checkpoint()
        finally:
            self._closed = True
            self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- FTS keying --------------------------------------------------------------------
    def _ensure_fts_keyed_by_rowid(self) -> int:
        """Rebuild papers_fts once when its rowids do not line up with papers.rowid (stores from older versions).

        Returns the number of mismatched / missing rows that triggered the rebuild (0 = nothing to do)."""
        bad = self.conn.execute(
            "SELECT COUNT(*) FROM papers_fts f LEFT JOIN papers p ON p.rowid = f.rowid WHERE p.id IS NULL OR p.id != f.id"
        ).fetchone()[0]
        missing = self.conn.execute(
            "SELECT COUNT(*) FROM papers p WHERE NOT EXISTS (SELECT 1 FROM papers_fts f WHERE f.rowid = p.rowid)"
        ).fetchone()[0]
        n = int(bad or 0) + int(missing or 0)
        if n == 0:
            return 0
        log.warning("papers_fts is keyed by arbitrary rowids (%d mismatched rows); rebuilding it keyed by papers.rowid", n)
        self.conn.execute("DROP TABLE IF EXISTS temp.fts_old")
        self.conn.execute("CREATE TEMP TABLE fts_old AS SELECT id, MAX(body) AS body FROM papers_fts WHERE body != '' GROUP BY id")
        self.conn.execute("CREATE INDEX temp.fts_old_id ON fts_old(id)")
        self.conn.execute("DELETE FROM papers_fts")
        cur = self.conn.execute("SELECT rowid, id, title, abstract, sections_json, affiliations_json FROM papers")
        while True:
            chunk = cur.fetchmany(500)
            if not chunk:
                break
            for rowid, pid, title, abstract, sections_json, affiliations_json in chunk:
                sections = _json_dict(sections_json)
                try:
                    affiliations = json.loads(affiliations_json or "[]")
                except ValueError:
                    affiliations = []
                old = self.conn.execute("SELECT body FROM fts_old WHERE id=?", (pid,)).fetchone()
                self._write_fts(rowid, pid, title or "", abstract or "", sections, affiliations, (old[0] if old else "") or "")
        self.conn.execute("DROP TABLE temp.fts_old")
        self.conn.commit()
        return n

    def _write_fts(self, rowid: int, pid: str, title: str, abstract: str, sections: Dict[str, str], affiliations: Sequence[str], body: str) -> None:
        self.conn.execute("DELETE FROM papers_fts WHERE rowid=?", (rowid,))
        self.conn.execute(
            "INSERT INTO papers_fts (rowid, id, title, abstract, methods, funding, coi, affiliations, body) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                rowid, pid, title, abstract, sections.get("methods", "") or "", sections.get("funding", "") or "",
                sections.get("coi", "") or "", "\n".join(str(a) for a in affiliations), body or "",
            ),
        )

    def rowid_for(self, paper_id: str) -> Optional[int]:
        row = self.conn.execute("SELECT rowid FROM papers WHERE id=?", (paper_id,)).fetchone()
        return int(row[0]) if row else None

    def body_for(self, paper_id: str, rowid: Optional[int] = None) -> str:
        """The stored full-text body (lives only in the FTS table), '' when none."""
        rid = rowid if rowid is not None else self.rowid_for(paper_id)
        if rid is None:
            return ""
        row = self.conn.execute("SELECT body FROM papers_fts WHERE rowid=?", (rid,)).fetchone()
        return (row[0] or "") if row else ""

    # ---- papers -------------------------------------------------------------------------
    def has_paper(self, paper_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone() is not None

    def get_paper(self, paper_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute("SELECT rowid AS _rowid, * FROM papers WHERE id=?", (paper_id,))
        rows = _rows(cur)
        return rows[0] if rows else None

    def iter_papers(self, since: Optional[str] = None, batch: int = 500) -> Iterator[Dict[str, Any]]:
        """Stream paper rows (with `_rowid`) in rowid order, `batch` at a time, without materialising the table.

        Keyset pagination, so writes to other tables between batches are safe."""
        last = 0
        while True:
            sql = "SELECT rowid AS _rowid, * FROM papers WHERE rowid > ?"
            params: List[Any] = [last]
            if since:
                sql += " AND (pub_date >= ? OR substr(first_seen,1,10) >= ?)"
                params += [since, since]
            sql += " ORDER BY rowid LIMIT ?"
            params.append(int(batch))
            rows = _rows(self.conn.execute(sql, params))
            if not rows:
                return
            yield from rows
            last = rows[-1]["_rowid"]

    def upsert_paper(self, p: Paper) -> bool:
        """Insert or update; returns True when the paper was new to the store.

        Update rules: richer fields win (never overwrite an abstract / sections / authors with empty values); `kind`
        never flips from article back to preprint; `pub_date` keeps the earliest non-empty date; `extra_json` is the
        union of stored and new keys (new non-empty values win, `sources` lists are unioned); a body already stored
        survives a metadata-only re-fetch. Any policy level other than 'full' strips full-text sections and the body.
        """
        existing = self.get_paper(p.id)
        now = _now()
        sections = {k: v for k, v in dict(p.sections).items() if v}
        body = sections.pop("body", "")
        extra = {k: v for k, v in dict(p.extra or {}).items()}
        if existing:
            old_sections = _json_dict(existing.get("sections_json"))
            for k, v in old_sections.items():
                if k == "body":
                    continue
                if not sections.get(k) and v:
                    sections[k] = v
            if not body:
                body = self.body_for(p.id, rowid=existing.get("_rowid"))
            if not p.abstract:
                p.abstract = existing.get("abstract") or ""
            if not p.authors and existing.get("authors_json"):
                authors_json = existing["authors_json"]
            else:
                authors_json = json.dumps([a.__dict__ for a in p.authors], ensure_ascii=False)
            try:
                old_affiliations = json.loads(existing.get("affiliations_json") or "[]")
            except ValueError:
                old_affiliations = []
            affiliations = list(dict.fromkeys(list(p.affiliations) + list(old_affiliations)))
            fulltext = int(bool(p.fulltext_available) or bool(existing.get("fulltext_available")))
            first_seen = existing.get("first_seen") or now
            old_extra = _json_dict(existing.get("extra_json"))
            merged_extra = dict(old_extra)
            for k, v in extra.items():
                if v is None or v == "" or v == [] or v == {}:
                    continue
                merged_extra[k] = v
            if isinstance(old_extra.get("sources"), list) and isinstance(extra.get("sources"), list):
                merged_extra["sources"] = sorted(set(old_extra["sources"]) | set(extra["sources"]))
            extra = merged_extra
        else:
            authors_json = json.dumps([a.__dict__ for a in p.authors], ensure_ascii=False)
            affiliations = list(p.affiliations)
            fulltext = int(bool(p.fulltext_available))
            first_seen = now
        level = extra.get("fulltext_policy")
        if level and level != "full":
            sections = restricted_sections(sections, extra)
            body = ""
        self.conn.execute(
            """INSERT INTO papers (id, doi, source, kind, title, abstract, journal, pub_date, url, license,
                 category, published_doi, authors_json, affiliations_json, sections_json, topics_json,
                 fulltext_available, cited_by_count, extra_json, first_seen, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 doi=COALESCE(excluded.doi, papers.doi),
                 source=CASE WHEN papers.source LIKE 'rss:%' THEN excluded.source ELSE papers.source END,
                 kind=CASE WHEN papers.kind = 'article' THEN 'article' ELSE excluded.kind END,
                 title=CASE WHEN length(excluded.title) > length(papers.title) THEN excluded.title ELSE papers.title END,
                 abstract=excluded.abstract,
                 journal=CASE WHEN excluded.journal != '' THEN excluded.journal ELSE papers.journal END,
                 pub_date=CASE WHEN excluded.pub_date = '' THEN papers.pub_date
                               WHEN papers.pub_date = '' OR papers.pub_date IS NULL THEN excluded.pub_date
                               WHEN excluded.pub_date < papers.pub_date THEN excluded.pub_date
                               ELSE papers.pub_date END,
                 url=CASE WHEN excluded.url != '' THEN excluded.url ELSE papers.url END,
                 license=CASE WHEN excluded.license != '' THEN excluded.license ELSE papers.license END,
                 category=CASE WHEN excluded.category != '' THEN excluded.category ELSE papers.category END,
                 published_doi=COALESCE(excluded.published_doi, papers.published_doi),
                 authors_json=excluded.authors_json,
                 affiliations_json=excluded.affiliations_json,
                 sections_json=excluded.sections_json,
                 topics_json=CASE WHEN excluded.topics_json != '[]' THEN excluded.topics_json ELSE papers.topics_json END,
                 fulltext_available=excluded.fulltext_available,
                 cited_by_count=MAX(excluded.cited_by_count, papers.cited_by_count),
                 extra_json=excluded.extra_json,
                 updated_at=excluded.updated_at
            """,
            (
                p.id, p.doi, p.source, p.kind, p.title or "", p.abstract or "", p.journal or "", p.pub_date or "",
                p.url or "", p.license or "", p.category or "", p.published_doi,
                authors_json, json.dumps(affiliations, ensure_ascii=False), json.dumps(sections, ensure_ascii=False),
                json.dumps(p.topics, ensure_ascii=False), fulltext, int(p.cited_by_count or 0),
                json.dumps(extra, ensure_ascii=False, default=str), first_seen, now,
            ),
        )
        rowid = existing["_rowid"] if existing else self.rowid_for(p.id)
        self._write_fts(int(rowid), p.id, p.title or "", p.abstract or "", sections, affiliations, body)
        return existing is None

    @staticmethod
    def stored_fulltext_level(license_str: Optional[str], extra: Dict[str, Any]) -> str:
        """Policy level for a paper already in the store: the recorded level, else derived from its licence and
        the Europe PMC open-access flag (the text is evidently available, since it was stored)."""
        return extra.get("fulltext_policy") or fulltext_storage_level(license_str, open_access=str(extra.get("is_open_access") or "").upper() == "Y", text_available=True)

    def purge_paper_fulltext(self, paper_id: str, level: str, rowid: Optional[int] = None) -> bool:
        """Strip a stored paper's full-text sections and body (keeping metadata-derived funding) and record
        `level` in extra_json.fulltext_policy. Returns True when text was actually removed."""
        rid = rowid if rowid is not None else self.rowid_for(paper_id)
        if rid is None:
            return False
        row = self.conn.execute("SELECT title, abstract, sections_json, extra_json, affiliations_json FROM papers WHERE rowid=?", (rid,)).fetchone()
        if not row:
            return False
        sections = _json_dict(row[2])
        extra = _json_dict(row[3])
        body = self.body_for(paper_id, rowid=rid)
        kept = restricted_sections(sections, extra)
        # text to remove = a body, or a full-text section that differs from what the restricted level keeps
        # (funding rebuilt from grant metadata is not full text)
        has_text = bool(body) or any(sections.get(k) and sections.get(k) != kept.get(k) for k in FULLTEXT_SECTIONS)
        if not has_text and extra.get("fulltext_policy") == level:
            return False
        extra["fulltext_policy"] = level
        try:
            affiliations = json.loads(row[4] or "[]")
        except ValueError:
            affiliations = []
        self.conn.execute("UPDATE papers SET sections_json=?, extra_json=?, updated_at=? WHERE rowid=?",
                          (json.dumps(kept, ensure_ascii=False), json.dumps(extra, ensure_ascii=False, default=str), _now(), rid))
        self._write_fts(rid, paper_id, row[0] or "", row[1] or "", kept, affiliations, "")
        return has_text

    def purge_restricted_fulltext(self) -> int:
        """Drop stored full-text sections and bodies of every paper whose policy level is not 'full'.

        For papers stored before the policy existed the level is derived from `license` and the Europe PMC
        open-access flag; the result is written to extra_json.fulltext_policy. Returns the number of papers cleaned.
        Pipeline.rematch does the same per paper AFTER re-extracting mentions from the stored text, so the derived
        facts survive; call this directly only when the mentions are already current."""
        n = 0
        cur = self.conn.execute("SELECT rowid, id, license, extra_json FROM papers WHERE fulltext_available = 1 OR sections_json LIKE '%\"methods\"%' OR sections_json LIKE '%\"coi\"%'")
        while True:
            chunk = cur.fetchmany(500)
            if not chunk:
                break
            for rowid, pid, license_str, extra_json in chunk:
                level = self.stored_fulltext_level(license_str, _json_dict(extra_json))
                if level == "full":
                    continue
                if self.purge_paper_fulltext(pid, level, rowid=rowid):
                    n += 1
        self.conn.commit()
        return n

    # ---- mentions -----------------------------------------------------------------------
    def replace_mentions(self, paper_id: str, mentions: Iterable[Mention], keep_sections: Sequence[str] = ()) -> int:
        """Replace a paper's mentions. Stored mentions in `keep_sections` survive when the new extraction has
        none for that section (used when the text they came from is not kept under the data policy)."""
        new = list(mentions)
        kept: List[Dict[str, Any]] = []
        if keep_sections:
            fresh_sections = {m.section for m in new}
            kept = [r for r in self.mentions_for(paper_id) if r["section"] in keep_sections and r["section"] not in fresh_sections]
        self.conn.execute("DELETE FROM mentions WHERE paper_id=?", (paper_id,))
        n = 0
        for m in new:
            self.conn.execute(
                """INSERT OR REPLACE INTO mentions (paper_id, kind, entity_id, entity_name, vendor, ticker, category,
                   section, count, snippet) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, m.kind, m.entity_id, m.entity_name, m.vendor, m.ticker, m.category, m.section, m.count, m.snippet),
            )
            n += 1
        for r in kept:
            self.conn.execute(
                """INSERT OR IGNORE INTO mentions (paper_id, kind, entity_id, entity_name, vendor, ticker, category,
                   section, count, snippet) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, r["kind"], r["entity_id"], r["entity_name"], r["vendor"], r["ticker"], r["category"], r["section"], r["count"], r["snippet"]),
            )
            n += 1
        return n

    def mentions_for(self, paper_id: str) -> List[Dict[str, Any]]:
        return _rows(self.conn.execute("SELECT * FROM mentions WHERE paper_id=? ORDER BY kind, entity_name", (paper_id,)))

    # ---- scores -------------------------------------------------------------------------
    def save_score(self, paper_id: str, heuristic: float, llm: Optional[Dict[str, Any]], final: float, reasons: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO scores (paper_id, heuristic, llm_json, final, reasons, scored_at) VALUES (?,?,?,?,?,?)",
            (paper_id, heuristic, json.dumps(llm, ensure_ascii=False) if llm else None, final, reasons, _now()),
        )

    def get_score(self, paper_id: str) -> Optional[Dict[str, Any]]:
        rows = _rows(self.conn.execute("SELECT * FROM scores WHERE paper_id=?", (paper_id,)))
        return rows[0] if rows else None

    # ---- archive records ----------------------------------------------------------------
    def upsert_archive_records(self, records: Iterable[ArchiveRecord]) -> int:
        n = 0
        for r in records:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO archive_records (archive, accession, study_accession, instrument_model, platform,
                   library_strategy, first_public, center_name, study_title, organism, entity_id, vendor, ticker)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r.archive, r.accession, r.study_accession, r.instrument_model, r.platform, r.library_strategy,
                    r.first_public, r.center_name, r.study_title, r.organism, r.entity_id, r.vendor, r.ticker,
                ),
            )
            n += cur.rowcount if cur.rowcount > 0 else 0
        return n

    # ---- news ---------------------------------------------------------------------------
    def upsert_news(self, items: Iterable[NewsItem]) -> int:
        n = 0
        for it in items:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO news_items (id, feed, title, url, published, summary, companies_json, first_seen)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (it.id, it.feed, it.title, it.url, it.published, it.summary, json.dumps(it.companies), _now()),
            )
            n += cur.rowcount if cur.rowcount > 0 else 0
        return n

    # ---- tracked authors ----------------------------------------------------------------
    def add_tracked_author_hit(self, paper_id: str, author_name: str, subgroup: str, matched_by: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO tracked_author_hits (paper_id, author_name, subgroup, matched_by) VALUES (?,?,?,?)",
            (paper_id, author_name, subgroup, matched_by),
        )

    # ---- runs ---------------------------------------------------------------------------
    def record_run(self, started: str, since: str, until: str, counts: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO runs (started, finished, since, until, counts_json) VALUES (?,?,?,?,?)",
            (started, _now(), since, until, json.dumps(counts, default=str)),
        )
        self.conn.commit()

    # ---- instrument time series (monthly counts per source / entity / model) ------------
    def get_timeseries_count(self, source: str, entity_id: str, model: str, month: str) -> Optional[Dict[str, Any]]:
        rows = _rows(
            self.conn.execute(
                "SELECT source, entity_id, model, month, count, fetched_at FROM timeseries WHERE source=? AND entity_id=? AND model=? AND month=?",
                (source, entity_id, model, month),
            )
        )
        return rows[0] if rows else None

    def put_timeseries_count(self, source: str, entity_id: str, model: str, month: str, count: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO timeseries (source, entity_id, model, month, count, fetched_at) VALUES (?,?,?,?,?,?)",
            (source, entity_id, model, month, int(count), _now()),
        )

    def timeseries_rows(
        self,
        sources: Optional[Sequence[str]] = None,
        entity_ids: Optional[Sequence[str]] = None,
        start_month: Optional[str] = None,
        end_month: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT source, entity_id, model, month, count, fetched_at FROM timeseries WHERE 1=1"
        params: List[Any] = []
        if sources:
            sql += f" AND source IN ({','.join('?' * len(sources))})"
            params += list(sources)
        if entity_ids:
            sql += f" AND entity_id IN ({','.join('?' * len(entity_ids))})"
            params += list(entity_ids)
        if start_month:
            sql += " AND month >= ?"
            params.append(start_month)
        if end_month:
            sql += " AND month <= ?"
            params.append(end_month)
        sql += " ORDER BY source, entity_id, model, month"
        return _rows(self.conn.execute(sql, params))

    # ---- queries used by the digest -----------------------------------------------------
    def papers_in_window(self, since: str, until: str) -> List[Dict[str, Any]]:
        """Papers published in [since, until] OR first seen in the window (for feeds without dates)."""
        cur = self.conn.execute(
            f"""SELECT p.*, s.heuristic, s.llm_json, s.final, s.reasons
               FROM papers p LEFT JOIN scores s ON s.paper_id = p.id
               WHERE {_WINDOW}
               ORDER BY s.final DESC NULLS LAST, p.pub_date DESC""",
            (since, until, since),
        )
        return _rows(cur)

    @staticmethod
    def collapse_preprints(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Fold preprints whose journal version is in the same list into that article.

        A preprint is linked to an article by `published_doi` or by an identical normalised title. The article row
        gains `preprints` = [{id, pub_date, url, matched_by}] and the better of the two scores; the preprint row is
        dropped. Returns (remaining rows, conversions)."""
        articles = [r for r in rows if (r.get("kind") or "article") != "preprint"]
        by_id = {r["id"]: r for r in articles}
        by_doi = {normalize_doi(r.get("doi")): r for r in articles if normalize_doi(r.get("doi"))}
        by_title: Dict[str, Dict[str, Any]] = {}
        for r in articles:
            t = _norm_title(r.get("title"))
            if len(t) >= 25:
                by_title.setdefault(t, r)
        drop = set()
        conversions: List[Dict[str, Any]] = []
        for r in rows:
            if (r.get("kind") or "article") != "preprint":
                continue
            target, how = None, ""
            pd = normalize_doi(r.get("published_doi"))
            if pd and (pd in by_doi or f"doi:{pd}" in by_id):
                target, how = by_doi.get(pd) or by_id.get(f"doi:{pd}"), "published_doi"
            else:
                t = _norm_title(r.get("title"))
                if len(t) >= 25 and t in by_title:
                    target, how = by_title[t], "title"
            if target is None or target["id"] == r["id"]:
                continue
            drop.add(r["id"])
            target.setdefault("preprints", []).append({"id": r["id"], "pub_date": r.get("pub_date") or "", "url": r.get("url") or "", "matched_by": how})
            if r.get("final") is not None and (target.get("final") is None or r["final"] > target["final"]):
                target["final"] = r["final"]
            conversions.append({
                "preprint_id": r["id"], "preprint_date": r.get("pub_date") or "", "preprint_url": r.get("url") or "",
                "article_id": target["id"], "article_title": target.get("title") or "", "journal": target.get("journal") or "",
                "article_date": target.get("pub_date") or "", "article_url": target.get("url") or "", "matched_by": how,
            })
        return [r for r in rows if r["id"] not in drop], conversions

    def preprint_conversions(self, since: str, until: str) -> List[Dict[str, Any]]:
        """Preprints in the window whose journal version is also in the window (by published_doi or title)."""
        _, conversions = self.collapse_preprints(self.papers_in_window(since, until))
        return conversions

    def top_papers(self, since: str, until: str, limit: int = 25) -> List[Dict[str, Any]]:
        rows = [r for r in self.papers_in_window(since, until) if r.get("final") is not None]
        rows, _ = self.collapse_preprints(rows)
        rows.sort(key=lambda r: -(r.get("final") or 0))
        out = rows[:limit]
        for r in out:
            r["mentions"] = self.mentions_for(r["id"])
        return out

    def instrument_counts(self, since: str, until: str, limit: int = 60) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            f"""SELECT m.entity_id, m.entity_name, m.vendor, m.ticker, m.category,
                      COUNT(DISTINCT m.paper_id) AS papers,
                      SUM(m.count) AS mentions,
                      GROUP_CONCAT(DISTINCT m.section) AS sections
               FROM mentions m JOIN papers p ON p.id = m.paper_id
               WHERE m.kind='instrument' AND {_WINDOW}
               GROUP BY m.entity_id ORDER BY papers DESC, mentions DESC LIMIT ?""",
            (since, until, since, limit),
        )
        rows = _rows(cur)
        for r in rows:
            ex = self.conn.execute(
                f"""SELECT p.title, p.url FROM mentions m JOIN papers p ON p.id=m.paper_id
                   WHERE m.kind='instrument' AND m.entity_id=? AND {_WINDOW}
                   ORDER BY m.count DESC LIMIT 1""",
                (r["entity_id"], since, until, since),
            ).fetchone()
            r["example_title"], r["example_url"] = (ex[0], ex[1]) if ex else ("", "")
        return rows

    def company_counts(self, since: str, until: str, limit: int = 60) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            f"""SELECT m.entity_id, m.entity_name, m.ticker, m.vendor AS parent,
                      COUNT(DISTINCT m.paper_id) AS papers,
                      SUM(CASE WHEN m.section='affiliations' THEN 1 ELSE 0 END) AS as_author,
                      SUM(CASE WHEN m.section IN ('funding','coi','ack') THEN 1 ELSE 0 END) AS as_funder_or_coi,
                      SUM(CASE WHEN m.section IN ('title','abstract') THEN 1 ELSE 0 END) AS as_subject,
                      SUM(CASE WHEN m.section='methods' THEN 1 ELSE 0 END) AS as_supplier
               FROM mentions m JOIN papers p ON p.id = m.paper_id
               WHERE m.kind='company' AND {_WINDOW}
               GROUP BY m.entity_id ORDER BY papers DESC LIMIT ?""",
            (since, until, since, limit),
        )
        rows = _rows(cur)
        for r in rows:
            ex = self.conn.execute(
                f"""SELECT p.title, p.url FROM mentions m JOIN papers p ON p.id=m.paper_id
                   WHERE m.kind='company' AND m.entity_id=? AND {_WINDOW}
                   ORDER BY m.count DESC LIMIT 1""",
                (r["entity_id"], since, until, since),
            ).fetchone()
            r["example_title"], r["example_url"] = (ex[0], ex[1]) if ex else ("", "")
        return rows

    def new_entities(self, since: str, kind: str) -> List[Dict[str, Any]]:
        """Entities whose FIRST appearance in the store falls on/after `since` (new entrants)."""
        cur = self.conn.execute(
            """SELECT m.entity_id, m.entity_name, m.ticker, MIN(substr(p.first_seen,1,10)) AS first_seen, COUNT(DISTINCT m.paper_id) AS papers
               FROM mentions m JOIN papers p ON p.id=m.paper_id WHERE m.kind=?
               GROUP BY m.entity_id HAVING first_seen >= ? ORDER BY papers DESC""",
            (kind, since),
        )
        return _rows(cur)

    def archive_instrument_counts(self, since: str, until: str, archives: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Experiments per declared instrument model in the window, optionally restricted to some archives.

        Rows carry `archive` (comma-joined archives the model appeared in), `vendor_mapped` (False when the gazetteer
        could not map the model) and a `vendor` that is never blank: unmapped models are labelled from the raw
        platform string, e.g. 'Illumina (model unspecified)'."""
        sql = """SELECT instrument_model, platform, vendor, ticker, entity_id, COUNT(*) AS experiments,
                        COUNT(DISTINCT study_accession) AS studies, GROUP_CONCAT(DISTINCT archive) AS archives
                 FROM archive_records WHERE first_public >= ? AND first_public <= ?"""
        params: List[Any] = [since, until]
        if archives:
            sql += f" AND archive IN ({','.join('?' * len(archives))})"
            params += list(archives)
        sql += " GROUP BY instrument_model, vendor ORDER BY experiments DESC"
        rows = _rows(self.conn.execute(sql, params))
        for r in rows:
            r["archive"] = r.get("archives") or ""
            r["vendor_mapped"] = bool(r.get("vendor"))
            r["vendor"] = archive_vendor_label(r.get("vendor"), r.get("platform"), r.get("instrument_model"))
            r["ticker"] = r.get("ticker") or ""
        return rows

    def archive_strategy_counts(self, since: str, until: str, limit: int = 15, archives: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        sql = "SELECT library_strategy, COUNT(*) AS experiments FROM archive_records WHERE first_public >= ? AND first_public <= ?"
        params: List[Any] = [since, until]
        if archives:
            sql += f" AND archive IN ({','.join('?' * len(archives))})"
            params += list(archives)
        sql += " GROUP BY library_strategy ORDER BY experiments DESC LIMIT ?"
        params.append(limit)
        return _rows(self.conn.execute(sql, params))

    def tracked_hits_in_window(self, since: str, until: str) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            f"""SELECT t.subgroup, t.author_name, t.matched_by, p.id, p.title, p.url, p.journal, p.pub_date, p.kind
               FROM tracked_author_hits t JOIN papers p ON p.id=t.paper_id
               WHERE {_WINDOW}
               ORDER BY t.subgroup, t.author_name, p.pub_date DESC""",
            (since, until, since),
        )
        return _rows(cur)

    def news_in_window(self, since: str, limit: int = 200, feeds: Optional[Sequence[str]] = None, exclude_feeds: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM news_items WHERE (substr(first_seen,1,10) >= ? OR substr(published,1,10) >= ?)"
        params: List[Any] = [since, since]
        if feeds:
            sql += f" AND feed IN ({','.join('?' * len(feeds))})"
            params += list(feeds)
        if exclude_feeds:
            sql += f" AND feed NOT IN ({','.join('?' * len(exclude_feeds))})"
            params += list(exclude_feeds)
        sql += " ORDER BY published DESC LIMIT ?"
        params.append(limit)
        rows = _rows(self.conn.execute(sql, params))
        for r in rows:
            try:
                r["companies"] = json.loads(r.get("companies_json") or "[]")
            except ValueError:
                r["companies"] = []
        return rows

    def distinct_entities_in_window(self, since: str, until: str, kind: str) -> int:
        row = self.conn.execute(
            f"""SELECT COUNT(DISTINCT m.entity_id) FROM mentions m JOIN papers p ON p.id=m.paper_id
               WHERE m.kind=? AND {_WINDOW}""",
            (kind, since, until, since),
        ).fetchone()
        return int(row[0] if row else 0)

    def has_history_before(self, since: str) -> bool:
        """True when the store already held papers before this window (so 'first appearance' means something)."""
        row = self.conn.execute("SELECT 1 FROM papers WHERE substr(first_seen,1,10) < ? LIMIT 1", (since,)).fetchone()
        return row is not None

    def counts(self) -> Dict[str, int]:
        out = {}
        for t in ("papers", "mentions", "scores", "archive_records", "news_items", "tracked_author_hits"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out

    # ---- search -------------------------------------------------------------------------
    def search(self, query: str, since: Optional[str] = None, limit: int = 50, sections: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """Full-text search. `sections` restricts to FTS columns (e.g. ['methods'])."""
        bad = [s for s in sections if s not in FTS_COLUMNS]
        if bad:
            raise ValueError(f"unknown section(s) {', '.join(bad)}; choose from {', '.join(FTS_COLUMNS)}")
        if sections:
            cols = " ".join(sections)
            fts_query = f"{{{cols}}}: ({query})"
        else:
            fts_query = query
        sql = """SELECT p.id, p.doi, p.title, p.journal, p.pub_date, p.url, p.kind, p.source,
                        snippet(papers_fts, -1, '[', ']', ' … ', 18) AS snippet, bm25(papers_fts) AS rank
                 FROM papers_fts JOIN papers p ON p.rowid = papers_fts.rowid
                 WHERE papers_fts MATCH ?"""
        params: List[Any] = [fts_query]
        if since:
            sql += " AND (p.pub_date >= ? OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))"
            params += [since, since]
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            return _rows(self.conn.execute(sql, params))
        except sqlite3.OperationalError:
            # fall back to a phrase query when the user's text is not valid FTS5 syntax
            phrase = '"' + query.replace('"', " ") + '"'
            params[0] = f"{{{' '.join(sections)}}}: ({phrase})" if sections else phrase
            try:
                return _rows(self.conn.execute(sql, params))
            except sqlite3.OperationalError as exc:
                log.warning("search fell back to a phrase query and still failed: %s", exc)
                return []

    def entity_papers(self, entity_id: str, since: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        sql = """SELECT DISTINCT p.id, p.title, p.journal, p.pub_date, p.url, p.kind, m.section, m.snippet
                 FROM mentions m JOIN papers p ON p.id=m.paper_id WHERE m.entity_id=?"""
        params: List[Any] = [entity_id]
        if since:
            sql += " AND (p.pub_date >= ? OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))"
            params += [since, since]
        sql += " ORDER BY p.pub_date DESC LIMIT ?"
        params.append(limit)
        return _rows(self.conn.execute(sql, params))
