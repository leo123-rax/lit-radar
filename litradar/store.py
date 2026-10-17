"""SQLite + FTS5 store: papers, mentions, scores, archive records, news, runs.

One file, zero servers. The FTS table carries title, abstract, methods, funding, COI,
affiliations and (when available) the full body so `litradar search` answers questions
like "everything mentioning Xenium in the last 90 days".
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import ArchiveRecord, Mention, NewsItem, Paper

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
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ---- papers -------------------------------------------------------------------------
    def has_paper(self, paper_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone() is not None

    def get_paper(self, paper_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,))
        rows = _rows(cur)
        return rows[0] if rows else None

    def upsert_paper(self, p: Paper) -> bool:
        """Insert or update; returns True when the paper was new to the store."""
        existing = self.get_paper(p.id)
        now = _now()
        sections = dict(p.sections)
        body = sections.pop("body", "")
        if existing:
            # keep richer fields: never overwrite an abstract / sections with empty values
            old_sections = json.loads(existing.get("sections_json") or "{}")
            for k, v in old_sections.items():
                if k == "body":
                    continue
                if not sections.get(k):
                    sections[k] = v
            if not p.abstract:
                p.abstract = existing.get("abstract") or ""
            if not p.authors and existing.get("authors_json"):
                authors_json = existing["authors_json"]
            else:
                authors_json = json.dumps([a.__dict__ for a in p.authors], ensure_ascii=False)
            affiliations = p.affiliations or json.loads(existing.get("affiliations_json") or "[]")
            fulltext = int(bool(p.fulltext_available) or bool(existing.get("fulltext_available")))
            first_seen = existing.get("first_seen") or now
        else:
            authors_json = json.dumps([a.__dict__ for a in p.authors], ensure_ascii=False)
            affiliations = p.affiliations
            fulltext = int(bool(p.fulltext_available))
            first_seen = now
        self.conn.execute(
            """INSERT INTO papers (id, doi, source, kind, title, abstract, journal, pub_date, url, license,
                 category, published_doi, authors_json, affiliations_json, sections_json, topics_json,
                 fulltext_available, cited_by_count, extra_json, first_seen, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 doi=COALESCE(excluded.doi, papers.doi),
                 source=CASE WHEN papers.source LIKE 'rss:%' THEN excluded.source ELSE papers.source END,
                 kind=excluded.kind,
                 title=CASE WHEN length(excluded.title) > length(papers.title) THEN excluded.title ELSE papers.title END,
                 abstract=excluded.abstract,
                 journal=CASE WHEN excluded.journal != '' THEN excluded.journal ELSE papers.journal END,
                 pub_date=CASE WHEN excluded.pub_date != '' THEN excluded.pub_date ELSE papers.pub_date END,
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
                json.dumps(p.extra, ensure_ascii=False, default=str), first_seen, now,
            ),
        )
        self.conn.execute("DELETE FROM papers_fts WHERE id=?", (p.id,))
        self.conn.execute(
            "INSERT INTO papers_fts (id, title, abstract, methods, funding, coi, affiliations, body) VALUES (?,?,?,?,?,?,?,?)",
            (
                p.id, p.title or "", p.abstract or "", sections.get("methods", ""), sections.get("funding", ""),
                sections.get("coi", ""), "\n".join(affiliations), body,
            ),
        )
        return existing is None

    def commit(self) -> None:
        self.conn.commit()

    # ---- mentions -----------------------------------------------------------------------
    def replace_mentions(self, paper_id: str, mentions: Iterable[Mention]) -> int:
        self.conn.execute("DELETE FROM mentions WHERE paper_id=?", (paper_id,))
        n = 0
        for m in mentions:
            self.conn.execute(
                """INSERT OR REPLACE INTO mentions (paper_id, kind, entity_id, entity_name, vendor, ticker, category,
                   section, count, snippet) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, m.kind, m.entity_id, m.entity_name, m.vendor, m.ticker, m.category, m.section, m.count, m.snippet),
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
            (started, _now(), since, until, json.dumps(counts)),
        )
        self.conn.commit()

    # ---- queries used by the digest -----------------------------------------------------
    def papers_in_window(self, since: str, until: str) -> List[Dict[str, Any]]:
        """Papers published in [since, until] OR first seen in the window (for feeds without dates)."""
        cur = self.conn.execute(
            """SELECT p.*, s.heuristic, s.llm_json, s.final, s.reasons
               FROM papers p LEFT JOIN scores s ON s.paper_id = p.id
               WHERE (p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date = '' AND substr(p.first_seen,1,10) >= ?)
               ORDER BY s.final DESC NULLS LAST, p.pub_date DESC""",
            (since, until, since),
        )
        return _rows(cur)

    def top_papers(self, since: str, until: str, limit: int = 25) -> List[Dict[str, Any]]:
        rows = self.papers_in_window(since, until)
        rows = [r for r in rows if r.get("final") is not None]
        rows.sort(key=lambda r: -(r.get("final") or 0))
        out = rows[:limit]
        for r in out:
            r["mentions"] = self.mentions_for(r["id"])
        return out

    def instrument_counts(self, since: str, until: str, limit: int = 60) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT m.entity_id, m.entity_name, m.vendor, m.ticker, m.category,
                      COUNT(DISTINCT m.paper_id) AS papers,
                      SUM(m.count) AS mentions,
                      GROUP_CONCAT(DISTINCT m.section) AS sections
               FROM mentions m JOIN papers p ON p.id = m.paper_id
               WHERE m.kind='instrument' AND ((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))
               GROUP BY m.entity_id ORDER BY papers DESC, mentions DESC LIMIT ?""",
            (since, until, since, limit),
        )
        rows = _rows(cur)
        for r in rows:
            ex = self.conn.execute(
                """SELECT p.title, p.url FROM mentions m JOIN papers p ON p.id=m.paper_id
                   WHERE m.kind='instrument' AND m.entity_id=? AND ((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))
                   ORDER BY m.count DESC LIMIT 1""",
                (r["entity_id"], since, until, since),
            ).fetchone()
            r["example_title"], r["example_url"] = (ex[0], ex[1]) if ex else ("", "")
        return rows

    def company_counts(self, since: str, until: str, limit: int = 60) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT m.entity_id, m.entity_name, m.ticker, m.vendor AS parent,
                      COUNT(DISTINCT m.paper_id) AS papers,
                      SUM(CASE WHEN m.section='affiliations' THEN 1 ELSE 0 END) AS as_author,
                      SUM(CASE WHEN m.section IN ('funding','coi','ack') THEN 1 ELSE 0 END) AS as_funder_or_coi,
                      SUM(CASE WHEN m.section IN ('title','abstract','methods') THEN 1 ELSE 0 END) AS as_subject
               FROM mentions m JOIN papers p ON p.id = m.paper_id
               WHERE m.kind='company' AND ((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))
               GROUP BY m.entity_id ORDER BY papers DESC LIMIT ?""",
            (since, until, since, limit),
        )
        rows = _rows(cur)
        for r in rows:
            ex = self.conn.execute(
                """SELECT p.title, p.url FROM mentions m JOIN papers p ON p.id=m.paper_id
                   WHERE m.kind='company' AND m.entity_id=? AND ((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))
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

    def archive_instrument_counts(self, since: str, until: str) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT instrument_model, platform, vendor, ticker, COUNT(*) AS experiments, COUNT(DISTINCT study_accession) AS studies,
                      GROUP_CONCAT(DISTINCT archive) AS archives
               FROM archive_records WHERE first_public >= ? AND first_public <= ?
               GROUP BY instrument_model ORDER BY experiments DESC""",
            (since, until),
        )
        return _rows(cur)

    def archive_strategy_counts(self, since: str, until: str, limit: int = 15) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT library_strategy, COUNT(*) AS experiments FROM archive_records
               WHERE first_public >= ? AND first_public <= ? GROUP BY library_strategy ORDER BY experiments DESC LIMIT ?""",
            (since, until, limit),
        )
        return _rows(cur)

    def tracked_hits_in_window(self, since: str, until: str) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT t.subgroup, t.author_name, t.matched_by, p.id, p.title, p.url, p.journal, p.pub_date, p.kind
               FROM tracked_author_hits t JOIN papers p ON p.id=t.paper_id
               WHERE (p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?)
               ORDER BY t.subgroup, t.author_name, p.pub_date DESC""",
            (since, until, since),
        )
        return _rows(cur)

    def news_in_window(self, since: str, limit: int = 200) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT * FROM news_items WHERE substr(first_seen,1,10) >= ? OR substr(published,1,10) >= ?
               ORDER BY published DESC LIMIT ?""",
            (since, since, limit),
        )
        rows = _rows(cur)
        for r in rows:
            r["companies"] = json.loads(r.get("companies_json") or "[]")
        return rows

    def counts(self) -> Dict[str, int]:
        out = {}
        for t in ("papers", "mentions", "scores", "archive_records", "news_items", "tracked_author_hits"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out

    # ---- search -------------------------------------------------------------------------
    def search(self, query: str, since: Optional[str] = None, limit: int = 50, sections: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """Full-text search. `sections` restricts to FTS columns (e.g. ['methods'])."""
        if sections:
            cols = " ".join(sections)
            fts_query = f"{{{cols}}}: ({query})"
        else:
            fts_query = query
        sql = """SELECT p.id, p.doi, p.title, p.journal, p.pub_date, p.url, p.kind, p.source,
                        snippet(papers_fts, -1, '[', ']', ' … ', 18) AS snippet, bm25(papers_fts) AS rank
                 FROM papers_fts JOIN papers p ON p.id = papers_fts.id
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
            return _rows(self.conn.execute(sql, params))

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
