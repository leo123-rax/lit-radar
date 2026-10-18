"""SQLite + FTS5 store: papers, mentions, scores, archive records, news, runs.

One file, zero servers. The FTS table carries title, abstract, methods, funding, COI,
affiliations, (when available) the full body and a `mentions` column with the gazetteer entities
attributed to the paper (canonical names, matched aliases, ids), so `litradar search` answers
questions like "everything mentioning Xenium in the last 90 days" even when the paper itself
only wrote "Next GEM" or when its text is not stored under the data policy.

Keying: `papers_fts` rows are inserted with an explicit rowid equal to `papers.rowid`, so every
lookup, delete and join goes through the integer key instead of the UNINDEXED `id` column (which
would be a full scan of the FTS table per paper). Stores written by earlier versions used arbitrary
FTS rowids or lack the `mentions` column; `Store.__init__` detects either once and rebuilds the FTS
table in place (bodies preserved, logged).

Write discipline: an FTS5 row is rewritten only when one of its indexed columns actually changed
(`upsert_paper` compares with the stored row; `replace_mentions` compares the mentions text), because
every rewrite appends to the FTS5 segment b-tree and a weekly re-ingest of unchanged papers used to
grow the file by megabytes. `Store.fts_changes` counts the rewrites; `Store.optimize()` merges the
segments and refreshes planner statistics after a run with many of them.

Data policy: when a paper's `extra_json.fulltext_policy` is anything but 'full', the store keeps no
full-text sections and no body for it, whatever the caller passes (see litradar/policy.py).

Mention roles: every mentions row carries a `role` -- 'used' (named in the Methods as something the study
ran), 'reanalysis' (the snippet around the hit cites a public dataset: a GEO / SRA / ArrayExpress accession,
"downloaded from", "publicly available", "re-analysed", "previously published") or 'mentioned' (title,
abstract, funding, COI, acknowledgement and affiliation hits). `mention_role` derives it from the section and
the stored snippet, so it costs no extra text and `Store.__init__` back-fills it for stores written before the
column existed. `instrument_counts` reports `papers_used` / `papers_reanalysis` / `papers_mentioned` next to
the unchanged `papers` total, so "who is using Chromium" can leave out papers that only re-analysed somebody
else's data.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .extract.gazetteer import family_label
from .models import ArchiveRecord, Mention, NewsItem, Paper, normalize_doi
from .policy import fulltext_storage_level
from .rank.heuristics import PLATFORM_CATEGORIES

log = logging.getLogger(__name__)

MENTION_ROLES = ("used", "reanalysis", "mentioned")
# sections in which a hit means the study ran the thing (the extractor files body hits under 'methods')
_USED_SECTIONS = frozenset({"methods", "body"})
# phrases that mark the surrounding sentence as data reuse rather than data generation (case-insensitive)
_REANALYSIS_PHRASES = re.compile(
    r"downloaded from|publicly available|public(?:ly)? (?:available )?datasets?|GEO accession|Gene Expression Omnibus|"
    r"re-?analy[sz]|previously published|previously (?:generated|deposited|reported) data|retrieved from|"
    r"ArrayExpress|Sequence Read Archive",
    re.IGNORECASE,
)
# accession tokens: case-sensitive so 'sra' inside a word or a lower-case 'gse' never counts
_REANALYSIS_ACCESSIONS = re.compile(r"\bSRA\b|\bGSE\d{3,}\b|\bGSM\d{4,}\b|\bE-MTAB-\d+\b|\bPRJ[NED][AB]\d+\b|\bSR[PRXS]\d{5,}\b|\bER[PRXS]\d{5,}\b")
# archive rows written by an older SRA parser when the experiment accession was missing: synthetic ids that cannot be
# de-duplicated against ENA and carry no study / platform / strategy (litradar/sources/sra.py documents the cause)
SRA_PLACEHOLDER_PREFIX = "SRA-UID-"


def mention_role(section: str, snippet: Optional[str]) -> str:
    """Derive a mention's role from its section and snippet (see the module docstring)."""
    text = snippet or ""
    if _REANALYSIS_PHRASES.search(text) or _REANALYSIS_ACCESSIONS.search(text):
        return "reanalysis"
    return "used" if section in _USED_SECTIONS else "mentioned"

FTS_COLUMNS = ("title", "abstract", "methods", "funding", "coi", "affiliations", "body", "mentions")
# the `mentions` column is maintained from the mentions table; the others are text columns of the paper
FTS_TEXT_COLUMNS = FTS_COLUMNS[:-1]
FTS_TABLE_COLUMNS = ("id",) + FTS_COLUMNS  # declaration order in FTS_DDL = the column numbers FTS5 functions take
SNIPPET_TOKENS = 18  # width of a search snippet, in FTS5 tokens
# Search snippets are highlighted with these control characters inside SQL and swapped for the public '[' / ']'
# once the column is chosen: paper text contains '[' itself (citation numbers such as "[12]"), so counting the
# public markers would mistake a bracketed reference for a match.
_SNIP_L, _SNIP_R = "\x02", "\x03"
FTS_DDL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
  id UNINDEXED, {", ".join(FTS_COLUMNS)},
  tokenize = 'porter unicode61'
);
"""

SCHEMA = f"""
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
{FTS_DDL}
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
  alias TEXT DEFAULT '',
  role TEXT DEFAULT '',
  PRIMARY KEY (paper_id, kind, entity_id, section)
);
CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(kind, entity_id);
CREATE INDEX IF NOT EXISTS idx_mentions_entity_id ON mentions(entity_id);

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

# sections that only exist when full text was parsed; dropped for any policy level other than 'full'
FULLTEXT_SECTIONS = ("methods", "funding", "coi", "ack", "data_availability", "body")
# display order of mention sections (entity_papers(distinct=True) sorts its `sections` list this way)
SECTION_ORDER = ("title", "abstract", "methods", "body", "funding", "coi", "ack", "data_availability", "affiliations")
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


def mentions_text(rows: Iterable[Sequence[Any]]) -> str:
    """The `mentions` FTS column for one paper from (entity_name, alias, entity_id) rows: every distinct canonical
    name, matched alias and id, ' ; '-joined in first-seen order. '' when the paper has no mentions."""
    seen: Dict[str, None] = {}
    for name, alias, entity_id in rows:
        for tok in [name] + [a.strip() for a in str(alias or "").split(";")] + [entity_id]:
            tok = str(tok or "").strip()
            if tok:
                seen.setdefault(tok, None)
    return " ; ".join(seen)


def choose_snippet(per_column: Sequence[str]) -> Tuple[Optional[str], str]:
    """Pick one search snippet from FTS5's per-column snippets (ordered as FTS_COLUMNS, matches highlighted with
    `_SNIP_L` / `_SNIP_R`). Returns ``(column, text)`` with the public ``[`` ``]`` markers.

    The text columns (title, abstract, methods, funding, coi, affiliations, body) come first: the one whose window
    highlights the most matched tokens wins, the earlier column on a tie. The `mentions` column (the gazetteer's
    ' ; '-joined names, aliases and ids, which is not paper text) is shown only when no text column matched, i.e.
    the paper was found through an attribution alone. FTS5's own column choice (`snippet(..., -1, ...)`) prefers
    the column whose window packs the most phrase instances, and the short, repetitive mentions text wins that
    contest for almost every entity name, which is why it is scored separately here. A row with no highlight
    anywhere falls back to the first non-empty text column, unmarked."""
    texts = list(per_column) + [""] * (len(FTS_COLUMNS) - len(per_column))
    best: Optional[str] = None
    best_hits = 0
    for col, text in zip(FTS_TEXT_COLUMNS, texts):
        hits = text.count(_SNIP_L)
        if hits > best_hits:
            best, best_hits = col, hits
    if best is None:
        if _SNIP_L in texts[FTS_COLUMNS.index("mentions")]:
            best = "mentions"
        else:
            best = next((col for col, text in zip(FTS_TEXT_COLUMNS, texts) if text.strip()), None)
    text = texts[FTS_COLUMNS.index(best)] if best else ""
    return best, text.replace(_SNIP_L, "[").replace(_SNIP_R, "]")


def _category_clause(categories: Optional[Sequence[str]], platform_only: Optional[bool]) -> Tuple[str, List[Any]]:
    """SQL fragment restricting `m.category` to the given prefixes (or to / away from the platform categories)."""
    negate = False
    if platform_only is True:
        categories = PLATFORM_CATEGORIES
    elif platform_only is False:
        categories, negate = PLATFORM_CATEGORIES, True
    if not categories:
        return "", []
    likes = " OR ".join("COALESCE(m.category, '') LIKE ? ESCAPE '\\'" for _ in categories)
    params = [re.sub(r"([%_\\])", r"\\\1", str(c)) + "%" for c in categories]
    return f" AND {'NOT ' if negate else ''}({likes})", params


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
        # FTS write accounting for this connection (see the module docstring)
        self.fts_rows_written = 0  # full FTS rows (re)written
        self.fts_rows_unchanged = 0  # upserts that left the FTS row alone because nothing indexed changed
        self.fts_mentions_updated = 0  # `mentions` column refreshes
        self.last_search_fallback: Optional[str] = None
        self._ensure_mentions_alias_column()
        self.roles_backfilled = self._ensure_mentions_role_column()
        self.fts_migrated = self._ensure_fts_schema()
        self.fts_rebuilt = self._ensure_fts_keyed_by_rowid()

    # ---- lifecycle ----------------------------------------------------------------------
    def commit(self) -> None:
        self.conn.commit()

    def checkpoint(self) -> None:
        """Commit and fold the WAL back into the main file (so the .sqlite-wal sidecar is empty on disk).

        This does not shrink the file: pages freed by deleted or rewritten rows (FTS5 segments, dropped tables,
        the FTS rebuilds on open) stay on the freelist and are reused by later writes. To return them to the
        operating system run ``VACUUM`` yourself (``sqlite3 data/radar.sqlite 'VACUUM'``, or
        ``Store.conn.execute('VACUUM')`` on an otherwise idle connection) before publishing or compressing a
        snapshot; it rewrites the whole file, needs free disk space of about the file's size and takes seconds to
        minutes on a 100+ MB store, which is why the store never does it on its own. ``PRAGMA freelist_count``
        tells how much there is to reclaim; :meth:`optimize` (FTS5 segment merge + ``PRAGMA optimize``) is the
        cheap step to run first."""
        if self._closed:
            return
        self.conn.commit()
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError as exc:  # pragma: no cover - another connection may hold the WAL
            log.warning("WAL checkpoint skipped: %s", exc)

    @property
    def fts_changes(self) -> int:
        """FTS5 rows (re)written through this connection, including `mentions` column refreshes."""
        return self.fts_rows_written + self.fts_mentions_updated

    def optimize(self) -> None:
        """Merge the FTS5 segment b-trees into one and refresh the query planner's statistics.

        Call after a run that rewrote many FTS rows (`fts_changes`); the pipeline does so automatically. It
        does not shrink the file (see :meth:`checkpoint` for VACUUM guidance)."""
        self.conn.commit()
        self.conn.execute("INSERT INTO papers_fts(papers_fts) VALUES('optimize')")
        self.conn.execute("PRAGMA optimize")
        self.conn.commit()

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

    # ---- schema migrations ---------------------------------------------------------------
    def _ensure_mentions_alias_column(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(mentions)")}
        if "alias" not in cols:
            log.info("mentions table has no 'alias' column; adding it")
            self.conn.execute("ALTER TABLE mentions ADD COLUMN alias TEXT DEFAULT ''")
            self.conn.commit()

    def _ensure_mentions_role_column(self) -> int:
        """Add `mentions.role` to stores written before it existed and derive it for every row that has none.
        Returns the number of rows back-filled (0 when the column was present and complete)."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(mentions)")}
        if "role" not in cols:
            log.info("mentions table has no 'role' column; adding it")
            self.conn.execute("ALTER TABLE mentions ADD COLUMN role TEXT DEFAULT ''")
        n = self.backfill_mention_roles(only_blank=True)
        if n:
            log.info("derived the role of %d stored mention(s)", n)
        self.conn.commit()
        return n

    def backfill_mention_roles(self, only_blank: bool = True) -> int:
        """(Re)derive `mentions.role` from section + snippet; `only_blank=False` recomputes every row (after a
        change to the cue list). Returns the number of rows updated."""
        where = " WHERE role IS NULL OR role = ''" if only_blank else ""
        rows = self.conn.execute(f"SELECT rowid, section, snippet, role FROM mentions{where}").fetchall()
        updates = [(mention_role(sec, snip), rid) for rid, sec, snip, role in rows if mention_role(sec, snip) != (role or "")]
        if updates:
            self.conn.executemany("UPDATE mentions SET role=? WHERE rowid=?", updates)
        return len(updates)

    def _ensure_fts_schema(self) -> bool:
        """Rebuild papers_fts once when it lacks a column of the current schema (e.g. `mentions`). Returns True
        when a rebuild happened."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(papers_fts)")]
        missing = [c for c in FTS_COLUMNS if c not in cols]
        if not missing:
            return False
        log.warning("papers_fts lacks column(s) %s; rebuilding the full-text index with the current schema (bodies preserved)", ", ".join(missing))
        self._rebuild_fts(recreate=True)
        return True

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
        self._rebuild_fts(recreate=False)
        return n

    def _rebuild_fts(self, recreate: bool) -> None:
        """Re-create every FTS row from `papers` (+ the stored bodies, which live only in the FTS table, + the
        mentions table). `recreate=True` drops and re-creates the virtual table (schema change); otherwise the rows
        are deleted and re-inserted keyed by papers.rowid."""
        self.conn.execute("DROP TABLE IF EXISTS temp.fts_old")
        self.conn.execute("CREATE TEMP TABLE fts_old AS SELECT id, MAX(body) AS body FROM papers_fts WHERE body != '' GROUP BY id")
        self.conn.execute("CREATE INDEX temp.fts_old_id ON fts_old(id)")
        if recreate:
            self.conn.execute("DROP TABLE papers_fts")
            self.conn.executescript(FTS_DDL)
        else:
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

    # ---- FTS rows -------------------------------------------------------------------------
    @staticmethod
    def fts_values(title: str, abstract: str, sections: Dict[str, str], affiliations: Sequence[str], body: str) -> Dict[str, str]:
        """The indexed text columns of a paper's FTS row, as stored."""
        return {
            "title": title or "", "abstract": abstract or "", "methods": sections.get("methods", "") or "",
            "funding": sections.get("funding", "") or "", "coi": sections.get("coi", "") or "",
            "affiliations": "\n".join(str(a) for a in affiliations), "body": body or "",
        }

    def _fts_row(self, rowid: int) -> Optional[Dict[str, str]]:
        row = self.conn.execute(f"SELECT {', '.join(FTS_COLUMNS)} FROM papers_fts WHERE rowid=?", (rowid,)).fetchone()
        return {c: (v or "") for c, v in zip(FTS_COLUMNS, row)} if row else None

    def _mentions_text(self, paper_id: str) -> str:
        return mentions_text(self.conn.execute("SELECT entity_name, alias, entity_id FROM mentions WHERE paper_id=? ORDER BY rowid", (paper_id,)).fetchall())

    def _write_fts(
        self, rowid: int, pid: str, title: str, abstract: str, sections: Dict[str, str], affiliations: Sequence[str], body: str,
        mentions: Optional[str] = None,
    ) -> None:
        """(Re)write one FTS row. `mentions` defaults to the text derived from the mentions table."""
        vals = self.fts_values(title, abstract, sections, affiliations, body)
        vals["mentions"] = self._mentions_text(pid) if mentions is None else mentions
        self.conn.execute("DELETE FROM papers_fts WHERE rowid=?", (rowid,))
        self.conn.execute(
            f"INSERT INTO papers_fts (rowid, id, {', '.join(FTS_COLUMNS)}) VALUES ({', '.join('?' * (len(FTS_COLUMNS) + 2))})",
            (rowid, pid, *[vals[c] for c in FTS_COLUMNS]),
        )
        self.fts_rows_written += 1

    def _sync_fts_mentions(self, paper_id: str) -> bool:
        """Refresh the `mentions` FTS column of a paper from the mentions table; True when it changed."""
        rid = self.rowid_for(paper_id)
        if rid is None:
            return False
        cur = self.conn.execute("SELECT mentions FROM papers_fts WHERE rowid=?", (rid,)).fetchone()
        if cur is None:
            return False  # no FTS row yet: upsert_paper writes it with the current mentions
        text = self._mentions_text(paper_id)
        if (cur[0] or "") == text:
            return False
        self.conn.execute("UPDATE papers_fts SET mentions=? WHERE rowid=?", (text, rid))
        self.fts_mentions_updated += 1
        return True

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
        rowid = int(existing["_rowid"] if existing else self.rowid_for(p.id))
        current = self._fts_row(rowid) if existing else None
        if current is not None:
            wanted = self.fts_values(p.title or "", p.abstract or "", sections, affiliations, body)
            if all(current[c] == wanted[c] for c in FTS_TEXT_COLUMNS):
                self.fts_rows_unchanged += 1  # nothing indexed changed: leave the FTS row (and its segments) alone
                return False
        self._write_fts(rowid, p.id, p.title or "", p.abstract or "", sections, affiliations, body, mentions=current["mentions"] if current else None)
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
        none for that section (used when the text they came from is not kept under the data policy).

        Also refreshes the paper's `mentions` full-text column (only when its text changed). A Mention may carry
        an `alias` attribute (extract.gazetteer.MatchedMention); it is stored in `mentions.alias`. Its `role` is
        stored as given, or derived with `mention_role` when blank."""
        new = list(mentions)
        kept: List[Dict[str, Any]] = []
        if keep_sections:
            fresh_sections = {m.section for m in new}
            kept = [r for r in self.mentions_for(paper_id) if r["section"] in keep_sections and r["section"] not in fresh_sections]
        self.conn.execute("DELETE FROM mentions WHERE paper_id=?", (paper_id,))
        n = 0
        for m in new:
            role = str(getattr(m, "role", "") or "") or mention_role(m.section, m.snippet)
            self.conn.execute(
                """INSERT OR REPLACE INTO mentions (paper_id, kind, entity_id, entity_name, vendor, ticker, category,
                   section, count, snippet, alias, role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, m.kind, m.entity_id, m.entity_name, m.vendor, m.ticker, m.category, m.section, m.count, m.snippet,
                 str(getattr(m, "alias", "") or ""), role),
            )
            n += 1
        for r in kept:
            self.conn.execute(
                """INSERT OR IGNORE INTO mentions (paper_id, kind, entity_id, entity_name, vendor, ticker, category,
                   section, count, snippet, alias, role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, r["kind"], r["entity_id"], r["entity_name"], r["vendor"], r["ticker"], r["category"], r["section"], r["count"], r["snippet"],
                 r.get("alias") or "", r.get("role") or mention_role(r["section"], r.get("snippet"))),
            )
            n += 1
        self._sync_fts_mentions(paper_id)
        return n

    def mentions_for(self, paper_id: str) -> List[Dict[str, Any]]:
        return _rows(self.conn.execute("SELECT * FROM mentions WHERE paper_id=? ORDER BY kind, entity_name", (paper_id,)))

    def entity_names(self, kind: Optional[str] = None) -> Dict[str, str]:
        """{entity_id: display name} for every entity that has at least one mention (optionally of one kind)."""
        sql = "SELECT entity_id, MAX(entity_name) FROM mentions"
        params: List[Any] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " GROUP BY entity_id"
        return {eid: (name or eid) for eid, name in self.conn.execute(sql, params).fetchall()}

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

    @staticmethod
    def _family_key(families: Optional[Dict[str, str]]) -> Tuple[str, List[Any]]:
        """SQL expression (and its parameters) that maps `m.entity_id` to its family key, else to itself."""
        if not families:
            return "m.entity_id", []
        params: List[Any] = []
        for eid, fam in families.items():
            params += [eid, fam]
        return "CASE m.entity_id " + " ".join("WHEN ? THEN ?" for _ in families) + " ELSE m.entity_id END", params

    def instrument_counts(
        self, since: str, until: str, limit: int = 60,
        categories: Optional[Sequence[str]] = None, platform_only: Optional[bool] = None,
        families: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """Instrument entities named in the window, most papers first.

        `categories` restricts to gazetteer category prefixes (e.g. ("sequencer", "spatial")); `platform_only=True`
        is shorthand for rank.heuristics.PLATFORM_CATEGORIES and `platform_only=False` for everything else (lab
        infrastructure and consumables). The filter is applied in SQL, before `limit`, so the two groups never
        compete for one row budget.

        Every row carries the role split `papers_used` / `papers_reanalysis` / `papers_mentioned` (distinct papers
        whose mention of the entity has that role; a paper can appear in more than one when it names the entity in
        several sections) next to the unchanged `papers` total.

        `families` ({entity_id: family key}, e.g. `Gazetteer.families`) rolls sibling entries up before the limit:
        the row's `entity_id` becomes the family key, `family` names it, `members` lists the member ids seen, the
        display name is `extract.gazetteer.family_label` and `papers` counts distinct papers across the members
        (a paper naming both NovaSeq X and NovaSeq X Plus counts once). Entries without a family are unchanged and
        carry `family: ''` and `members: [entity_id]`."""
        cat_sql, cat_params = _category_clause(categories, platform_only)
        key_sql, key_params = self._family_key(families)
        cur = self.conn.execute(
            f"""SELECT {key_sql} AS entity_id, m.entity_name, m.vendor, m.ticker, m.category,
                      MIN(length(m.entity_name)) AS _shortest,
                      COUNT(DISTINCT m.paper_id) AS papers,
                      SUM(m.count) AS mentions,
                      GROUP_CONCAT(DISTINCT m.section) AS sections,
                      GROUP_CONCAT(DISTINCT m.entity_id) AS _members,
                      GROUP_CONCAT(DISTINCT m.entity_name) AS _member_names,
                      COUNT(DISTINCT CASE WHEN m.role='used' THEN m.paper_id END) AS papers_used,
                      COUNT(DISTINCT CASE WHEN m.role='reanalysis' THEN m.paper_id END) AS papers_reanalysis,
                      COUNT(DISTINCT CASE WHEN m.role='mentioned' THEN m.paper_id END) AS papers_mentioned
               FROM mentions m JOIN papers p ON p.id = m.paper_id
               WHERE m.kind='instrument' AND {_WINDOW}{cat_sql}
               GROUP BY 1 ORDER BY papers DESC, mentions DESC LIMIT ?""",
            (*key_params, since, until, since, *cat_params, limit),
        )
        rows = _rows(cur)
        for r in rows:
            r.pop("_shortest", None)
            members = [x for x in str(r.pop("_members", "") or "").split(",") if x]
            names = [x for x in str(r.pop("_member_names", "") or "").split(",") if x]
            r["members"] = members or [r["entity_id"]]
            r["family"] = (families or {}).get(members[0], "") if members else ""
            if r["family"]:
                r["entity_name"] = family_label(r["family"], names)
            ex = self.conn.execute(
                f"""SELECT p.title, p.url FROM mentions m JOIN papers p ON p.id=m.paper_id
                   WHERE m.kind='instrument' AND m.entity_id IN ({','.join('?' * len(r['members']))}) AND {_WINDOW}
                   ORDER BY m.count DESC LIMIT 1""",
                (*r["members"], since, until, since),
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

    def distinct_entities_in_window(
        self, since: str, until: str, kind: str,
        categories: Optional[Sequence[str]] = None, platform_only: Optional[bool] = None,
        families: Optional[Dict[str, str]] = None,
    ) -> int:
        """Distinct entities of `kind` mentioned in the window; same category filter (and optional family roll-up)
        as instrument_counts."""
        cat_sql, cat_params = _category_clause(categories, platform_only)
        key_sql, key_params = self._family_key(families)
        row = self.conn.execute(
            f"""SELECT COUNT(DISTINCT {key_sql}) FROM mentions m JOIN papers p ON p.id=m.paper_id
               WHERE m.kind=? AND {_WINDOW}{cat_sql}""",
            (*key_params, kind, since, until, since, *cat_params),
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

    # ---- maintenance (litradar maintenance) ----------------------------------------------
    def count_sra_placeholders(self) -> int:
        """Archive rows whose accession is a synthetic `SRA-UID-...` placeholder (see SRA_PLACEHOLDER_PREFIX)."""
        return int(self.conn.execute("SELECT COUNT(*) FROM archive_records WHERE accession LIKE ? ESCAPE '\\'",
                                     (SRA_PLACEHOLDER_PREFIX.replace("_", "\\_").replace("%", "\\%") + "%",)).fetchone()[0])

    def purge_sra_placeholders(self) -> int:
        """Delete the `SRA-UID-...` placeholder rows from archive_records; returns how many went."""
        cur = self.conn.execute("DELETE FROM archive_records WHERE accession LIKE ? ESCAPE '\\'",
                                (SRA_PLACEHOLDER_PREFIX.replace("_", "\\_").replace("%", "\\%") + "%",))
        self.conn.commit()
        return int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)

    def refresh_entity_metadata(self, entries: Iterable[Any]) -> int:
        """Rewrite the denormalised columns of stored mentions from the current gazetteer entries.

        Ids are stable but display names, vendors, tickers and categories change (a renamed bucket, an acquisition).
        `rematch` re-derives most rows, yet rows kept because their source text is not stored under the data policy
        keep the values they were written with. This brings every row of a known id up to date: `entity_name` and
        `ticker` for all kinds, plus `vendor` and `category` for instruments (company rows use those two columns for
        the parent and the provenance tag, which are left alone). Returns the number of rows changed and refreshes
        the `mentions` full-text column of the papers touched."""
        changed = 0
        papers: set = set()
        for e in entries:
            kind, eid = getattr(e, "kind", ""), getattr(e, "id", "")
            if not kind or not eid:
                continue
            if kind == "instrument":
                where = ("WHERE kind=? AND entity_id=? AND (COALESCE(entity_name,'') IS NOT ? OR COALESCE(ticker,'') IS NOT ? "
                         "OR COALESCE(vendor,'') IS NOT ? OR COALESCE(category,'') IS NOT ?)")
                params = (kind, eid, e.name, e.ticker or "", e.vendor or "", e.category or "")
                set_sql = "SET entity_name=?, ticker=?, vendor=?, category=?"
                set_params = (e.name, e.ticker or "", e.vendor or "", e.category or "")
            else:
                where = "WHERE kind=? AND entity_id=? AND (COALESCE(entity_name,'') IS NOT ? OR COALESCE(ticker,'') IS NOT ?)"
                params = (kind, eid, e.name, e.ticker or "")
                set_sql = "SET entity_name=?, ticker=?"
                set_params = (e.name, e.ticker or "")
            stale = [r[0] for r in self.conn.execute(f"SELECT paper_id FROM mentions {where}", params)]
            if not stale:
                continue
            cur = self.conn.execute(f"UPDATE mentions {set_sql} {where}", (*set_params, *params))
            changed += max(cur.rowcount, 0)
            papers.update(stale)
        for pid in papers:
            self._sync_fts_mentions(pid)
        self.conn.commit()
        return changed

    def rebuild_fts(self) -> int:
        """Drop and re-create papers_fts from `papers` (+ stored bodies and the mentions table), then merge its
        segments. Returns the number of FTS rows written. Use after a tokenizer / schema change or when the index
        is suspected to be out of step with `papers`."""
        before = self.fts_rows_written
        self._rebuild_fts(recreate=True)
        self.optimize()
        return self.fts_rows_written - before

    def file_size(self) -> int:
        """Bytes of the main database file plus its WAL side file (0 for an in-memory store)."""
        total = 0
        for p in (self.path, Path(str(self.path) + "-wal")):
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total

    def vacuum(self) -> Dict[str, int]:
        """Checkpoint, VACUUM and checkpoint again; returns {'bytes_before', 'bytes_after', 'freelist_before'}.
        Rewrites the whole file: needs free disk space of about the file's size (see :meth:`checkpoint`)."""
        self.checkpoint()
        before, free = self.file_size(), int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        self.conn.execute("VACUUM")
        self.checkpoint()
        return {"bytes_before": before, "bytes_after": self.file_size(), "freelist_before": free}

    def table_stats(self) -> Dict[str, Any]:
        """Row counts and on-disk bytes per table (indexes and FTS shadow tables included), plus file totals.

        Bytes come from the `dbstat` virtual table when this SQLite build has it (most do); otherwise they are
        None and `bytes_available` is False. Rows are counted for tables only (None for indexes)."""
        objects = self.conn.execute(
            "SELECT name, type, tbl_name FROM sqlite_master WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY tbl_name, type DESC, name"
        ).fetchall()
        sizes: Dict[str, int] = {}
        bytes_available = True
        try:
            for name, size in self.conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
                sizes[name] = int(size or 0)
        except sqlite3.DatabaseError:
            bytes_available = False
        tables: List[Dict[str, Any]] = []
        for name, typ, tbl in objects:
            rows: Optional[int] = None
            if typ == "table":
                try:
                    rows = int(self.conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                except sqlite3.DatabaseError:
                    rows = None
            tables.append({"name": name, "type": typ, "table": tbl, "rows": rows, "bytes": sizes.get(name) if bytes_available else None})
        page_size = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        return {
            "objects": tables, "bytes_available": bytes_available, "page_size": page_size, "page_count": page_count,
            "freelist_count": freelist, "freelist_bytes": freelist * page_size, "file_bytes": self.file_size(),
            "sra_placeholders": self.count_sra_placeholders(),
        }

    # ---- search -------------------------------------------------------------------------
    def search(self, query: str, since: Optional[str] = None, limit: int = 50, sections: Sequence[str] = (), fallback: bool = True) -> List[Dict[str, Any]]:
        """Full-text search. `sections` restricts to FTS columns (e.g. ['methods'] or ['mentions']).

        Each row carries ``snippet``, an excerpt of SNIPPET_TOKENS tokens with the matched tokens in ``[`` ``]``,
        and ``section``, the FTS column it was cut from. The snippet is paper text whenever paper text matched
        (see :func:`choose_snippet` for the choice between columns); a paper found only through the gazetteer's
        `mentions` column shows that column with ``section == 'mentions'``. Snippets are computed for the
        returned rows alone, after ranking, not for every matching paper.

        When `query` is not valid FTS5 syntax (unbalanced parentheses, an unknown column such as
        ``nosuchcol:Xenium``) the search is retried as a quoted phrase and ``self.last_search_fallback`` holds
        the FTS5 error message, so a caller can tell the user what happened instead of showing a silent empty
        result. Pass ``fallback=False`` to get the ``sqlite3.OperationalError`` instead (an HTTP API turns that
        into a 400). ``last_search_fallback`` is None after a search that needed no fallback."""
        bad = [s for s in sections if s not in FTS_COLUMNS]
        if bad:
            raise ValueError(f"unknown section(s) {', '.join(bad)}; choose from {', '.join(FTS_COLUMNS)}")
        self.last_search_fallback = None
        if sections:
            cols = " ".join(sections)
            fts_query = f"{{{cols}}}: ({query})"
        else:
            fts_query = query
        sql = """SELECT p.id, p.doi, p.title, p.journal, p.pub_date, p.url, p.kind, p.source,
                        papers_fts.rowid AS fts_rowid, bm25(papers_fts) AS rank
                 FROM papers_fts JOIN papers p ON p.rowid = papers_fts.rowid
                 WHERE papers_fts MATCH ?"""
        params: List[Any] = [fts_query]
        if since:
            sql += " AND (p.pub_date >= ? OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))"
            params += [since, since]
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            return self._search_rows(sql, params)
        except sqlite3.OperationalError as exc:
            if not fallback:
                raise
            # fall back to a phrase query when the user's text is not valid FTS5 syntax
            self.last_search_fallback = str(exc)
            phrase = '"' + query.replace('"', " ") + '"'
            params[0] = f"{{{' '.join(sections)}}}: ({phrase})" if sections else phrase
            try:
                return self._search_rows(sql, params)
            except sqlite3.OperationalError as exc2:
                log.warning("search fell back to a phrase query and still failed: %s", exc2)
                return []

    def _search_rows(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        """Run a ranked search statement (``params[0]`` is the FTS5 query, each row has `fts_rowid`) and attach
        `snippet` + `section` to the rows it returned; `fts_rowid` is consumed."""
        rows = _rows(self.conn.execute(sql, params))
        chosen = self.snippets(str(params[0]), [r["fts_rowid"] for r in rows])
        for r in rows:
            r["section"], r["snippet"] = chosen.get(r.pop("fts_rowid"), (None, ""))
        return rows

    def snippets(self, fts_query: str, rowids: Sequence[int]) -> Dict[int, Tuple[Optional[str], str]]:
        """``{papers_fts rowid: (section, snippet)}`` for the given rows of a MATCH on `fts_query`: every FTS
        column's best window is cut by FTS5 and :func:`choose_snippet` picks the column, so paper text beats the
        `mentions` column whenever paper text matched. One statement over the listed rows only (``MATCH ... AND
        rowid IN (...)``), which is what makes snippets affordable after ranking and LIMIT."""
        ids = list(dict.fromkeys(int(r) for r in rowids))
        if not ids:
            return {}
        # FTS5 numbers every declared column, the UNINDEXED `id` included, so `title` is column 1, not 0
        cuts = ", ".join(f"snippet(papers_fts, {FTS_TABLE_COLUMNS.index(c)}, ?, ?, ' … ', {SNIPPET_TOKENS})" for c in FTS_COLUMNS)
        sql = f"SELECT rowid, {cuts} FROM papers_fts WHERE papers_fts MATCH ? AND rowid IN ({', '.join('?' * len(ids))})"
        marks: List[Any] = [_SNIP_L, _SNIP_R] * len(FTS_COLUMNS)
        return {row[0]: choose_snippet(row[1:]) for row in self.conn.execute(sql, marks + [fts_query] + ids)}

    def entity_papers(self, entity_id: str, since: Optional[str] = None, limit: int = 100, distinct: bool = False) -> List[Dict[str, Any]]:
        """Papers mentioning an entity, newest first.

        Default: one row per (paper, section) with that section's `snippet` (a paper naming the entity in the
        abstract and in the Methods appears twice). `distinct=True`: one row per paper with `sections` (list, in
        SECTION_ORDER), `section` (the same, comma-joined), `mentions` (summed count) and the snippet of the
        section with most occurrences; `limit` then counts papers."""
        since_sql, since_params = "", []
        if since:
            since_sql = " AND (p.pub_date >= ? OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))"
            since_params = [since, since]
        if not distinct:
            sql = f"""SELECT DISTINCT p.id, p.title, p.journal, p.pub_date, p.url, p.kind, m.section, m.snippet
                      FROM mentions m JOIN papers p ON p.id=m.paper_id WHERE m.entity_id=?{since_sql}
                      ORDER BY p.pub_date DESC LIMIT ?"""
            return _rows(self.conn.execute(sql, [entity_id, *since_params, limit]))
        sql = f"""SELECT p.id, p.title, p.journal, p.pub_date, p.url, p.kind,
                         GROUP_CONCAT(DISTINCT m.section) AS sections, SUM(m.count) AS mentions,
                         (SELECT m2.snippet FROM mentions m2 WHERE m2.paper_id = p.id AND m2.entity_id = m.entity_id
                          ORDER BY m2.count DESC, m2.section LIMIT 1) AS snippet
                  FROM mentions m JOIN papers p ON p.id=m.paper_id WHERE m.entity_id=?{since_sql}
                  GROUP BY p.id ORDER BY p.pub_date DESC LIMIT ?"""
        rows = _rows(self.conn.execute(sql, [entity_id, *since_params, limit]))
        order = {s: i for i, s in enumerate(SECTION_ORDER)}
        for r in rows:
            secs = sorted({s for s in str(r.get("sections") or "").split(",") if s}, key=lambda s: (order.get(s, len(order)), s))
            r["sections"] = secs
            r["section"] = ", ".join(secs)
            r["mentions"] = int(r.get("mentions") or 0)
        return rows
