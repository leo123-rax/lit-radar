#!/usr/bin/env python3
"""Benchmark the SQLite + FTS5 store with synthetic data.

    python3 scripts/bench_store.py --n 50000                       # the real Store API, as the pipeline uses it
    python3 scripts/bench_store.py --n 50000 --load-mode proposed  # the shipped upsert with only its FTS statements rewritten (docs/SCALE.md 3.1)
    python3 scripts/bench_store.py --n 200000 --json bench.json --keep --db /tmp/bench.sqlite

What it measures, on a temporary database (deleted afterwards unless --keep):

* upsert throughput: N synthetic papers, each with an FTS row, 0-4 instrument and 0-2 company mentions
  and a score, committed every --batch papers. Throughput is reported per 10k checkpoint so a cost that
  grows with table size (the FTS delete-by-id in `Store.upsert_paper`) shows up as a falling curve.
* re-upsert (update path) throughput on a random sample of stored papers.
* a micro-benchmark of the FTS row lookup by UNINDEXED id versus by rowid (the proposed fix).
* a micro-benchmark of Chinese (CJK) search: the shipped `porter unicode61` table against a trigram FTS table
  and a pre-segmented shadow column (docs/SCALE.md 3.4). Synthetic Chinese titles are written the way real
  ones are, without spaces, so unicode61 keeps each title as a single token.
* latency of every query the digest and the CLI run: papers_in_window, top_papers, instrument_counts,
  company_counts, new_entities, archive_*_counts, entity_papers, search (several shapes), news_in_window.
  Each query is timed in three states: as stored, after ANALYZE, after the proposed indexes (re-analysed).
  The query rewrites proposed in docs/SCALE.md run alongside (PROPOSED_*) and are checked for equivalence.
* database size on disk, per-table page usage (when the dbstat virtual table is compiled in),
  and EXPLAIN QUERY PLAN output for the queries whose plans matter.

Everything is deterministic for a given --seed. No network. Python 3.9+, standard library plus the
repository's own packages.

Run time: the shipped load path costs O(N) per insert (see docs/SCALE.md 3.1), so a store-mode run grows
with N^2: 22 min at N=50k on an idle laptop, roughly 6 h at the default N=200k. The script prints that estimate
before loading; `--load-mode proposed` runs 200k in a couple of minutes.
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper  # noqa: E402
from litradar.store import Store  # noqa: E402

# Indexes that are purely additive (CREATE INDEX IF NOT EXISTS) and are measured before/after below.
# The ones that pay for themselves are proposed in docs/SCALE.md; the first is in the schema.
PROPOSED_INDEXES = [
    ("idx_mentions_entity_id", "CREATE INDEX IF NOT EXISTS idx_mentions_entity_id ON mentions(entity_id)"),
    ("idx_mentions_kind_paper", "CREATE INDEX IF NOT EXISTS idx_mentions_kind_paper ON mentions(kind, paper_id, entity_id)"),
    ("idx_archive_public_model", "CREATE INDEX IF NOT EXISTS idx_archive_public_model ON archive_records(first_public, instrument_model)"),
]

WORDS = (
    "single cell spatial transcriptomics proteomics genome sequencing long read methylation atlas tumour "
    "immune receptor plasma biopsy variant calling assembly nanopore synthesis library preparation "
    "panel assay cohort validation clinical trial mouse human organoid tissue section imaging probe "
    "barcode droplet microfluidic chromatin accessibility epigenome perturbation screen crispr editing "
    "protein structure prediction model learning benchmark dataset pipeline reproducibility standard "
    "calibration throughput coverage depth quality control normalisation batch effect integration cluster "
    "trajectory lineage differentiation development ageing metabolism microbiome pathogen surveillance "
    "antibody antigen vaccine therapy resistance mutation signature expression regulation network"
).split()
CJK_WORDS = "空间 转录组 单细胞 测序 蛋白质 组学 肿瘤 基因组 甲基化 免疫 临床 队列 方法 分析 图谱 长读长 纳米孔 合成 生物学".split()
SECTIONS_INSTRUMENT = ["methods", "methods", "methods", "abstract", "title", "body"]
SECTIONS_COMPANY = ["affiliations", "affiliations", "funding", "coi", "abstract", "methods", "ack"]
STRATEGIES = ["WGS", "RNA-Seq", "AMPLICON", "WXS", "ChIP-Seq", "ATAC-seq", "Bisulfite-Seq", "OTHER", "Hi-C", "miRNA-Seq"]
ORGANISMS = ["Homo sapiens", "Mus musculus", "metagenome", "Arabidopsis thaliana", "Escherichia coli", "Danio rerio"]


# ---------------------------------------------------------------------------------- synthetic corpus
class Synth:
    """Deterministic synthetic papers, mentions, archive records and news for a given seed."""

    def __init__(self, seed: int, n_instruments: int = 200, n_companies: int = 150, years: int = 3,
                 fulltext_share: float = 0.3, body_words: int = 400, cjk_share: float = 0.1) -> None:
        self.rng = random.Random(seed)
        self.n_instruments = n_instruments
        self.n_companies = n_companies
        self.years = years
        self.fulltext_share = fulltext_share
        self.body_words = body_words
        self.cjk_share = cjk_share
        self.end = date.today()
        self.start = self.end - timedelta(days=365 * years)
        # pre-built sentence pool keeps generation cheap; papers sample from it
        self.sentences = [" ".join(self.rng.choice(WORDS) for _ in range(self.rng.randint(8, 18))) + "." for _ in range(3000)]
        self.instrument_models = [f"Model {i}" for i in range(60)]

    def _text(self, n_words: int) -> str:
        out: List[str] = []
        total = 0
        while total < n_words:
            s = self.rng.choice(self.sentences)
            out.append(s)
            total += s.count(" ") + 1
        return " ".join(out)

    def _entity(self, pool: int) -> int:
        # heavy-tailed popularity: a few entities carry most mentions, like real gazetteer hits
        return min(int(self.rng.expovariate(1.0 / (pool / 8.0))), pool - 1)

    def _date(self) -> str:
        return (self.start + timedelta(days=self.rng.randint(0, 365 * self.years))).isoformat()

    def paper(self, i: int) -> Tuple[Paper, List[Mention], float]:
        pid = f"doi:10.9999/synth.{i}"
        pub_date = "" if self.rng.random() < 0.05 else self._date()
        cjk = self.rng.random() < self.cjk_share
        # real Chinese titles carry no spaces between words: unicode61 then keeps the whole title as one token
        title = ("".join(self.rng.choice(CJK_WORDS) for _ in range(6)) if cjk else self._text(12)).rstrip(".")
        inst_n = self.rng.choices([0, 1, 2, 3, 4], weights=[35, 30, 20, 10, 5])[0]
        comp_n = self.rng.choices([0, 1, 2], weights=[55, 30, 15])[0]
        sections: Dict[str, str] = {"methods": self._text(120), "funding": self._text(15), "coi": self._text(20)}
        fulltext = self.rng.random() < self.fulltext_share
        if fulltext and self.body_words > 0:
            sections["body"] = self._text(self.body_words)
        mentions: List[Mention] = []
        seen = set()
        for _ in range(inst_n):
            k = self._entity(self.n_instruments)
            sec = self.rng.choice(SECTIONS_INSTRUMENT)
            if (k, sec) in seen:
                continue
            seen.add((k, sec))
            mentions.append(Mention(pid, "instrument", f"inst-{k:03d}", f"Instrument {k}", f"Vendor {k % 40}", f"TK{k % 40}",
                                    "sequencer" if k % 3 else "spatial", sec, self.rng.randint(1, 6), "… synthetic snippet …"))
        for _ in range(comp_n):
            k = self._entity(self.n_companies)
            sec = self.rng.choice(SECTIONS_COMPANY)
            if ("c", k, sec) in seen:
                continue
            seen.add(("c", k, sec))
            mentions.append(Mention(pid, "company", f"co-{k:03d}", f"Company {k}", "", f"TK{k % 40}", "", sec, 1, "… synthetic snippet …"))
        affs = [f"Institute {self.rng.randint(0, 999)}, City {self.rng.randint(0, 99)}" for _ in range(self.rng.randint(1, 4))]
        if cjk:
            affs.append("某大学生命科学学院")
        p = Paper(
            id=pid, source=self.rng.choice(["biorxiv", "europepmc", "crossref", "openalex", "arxiv", "rss:feed"]),
            title=title, doi=f"10.9999/synth.{i}", abstract=self._text(150), journal=f"Journal {self.rng.randint(0, 80)}",
            pub_date=pub_date, url=f"https://example.org/{i}", license="cc-by", category="genomics",
            kind="preprint" if self.rng.random() < 0.45 else "article",
            authors=[Author(name=f"Author {self.rng.randint(0, 99999)}", affiliation=affs[0]) for _ in range(self.rng.randint(1, 12))],
            affiliations=affs, sections=sections, fulltext_available=fulltext, cited_by_count=self.rng.randint(0, 30),
            topics=[self.rng.choice(WORDS) for _ in range(3)], extra={"synthetic": True},
        )
        return p, mentions, round(self.rng.uniform(0, 10), 2)

    def archive_records(self, n: int) -> Iterable[ArchiveRecord]:
        for i in range(n):
            k = self._entity(60)
            yield ArchiveRecord(
                archive="sra" if self.rng.random() < 0.9 else "ena", accession=f"SYNX{i:09d}", study_accession=f"SYNP{self.rng.randint(0, n // 20 + 1)}",
                instrument_model=self.instrument_models[k], platform="PLATFORM", library_strategy=self.rng.choice(STRATEGIES),
                first_public=self._date(), center_name=f"Center {self.rng.randint(0, 400)}", study_title="synthetic study",
                organism=self.rng.choice(ORGANISMS), entity_id=f"inst-{k:03d}", vendor=f"Vendor {k % 40}", ticker=f"TK{k % 40}",
            )

    def news(self, n: int) -> Iterable[NewsItem]:
        for i in range(n):
            yield NewsItem(id=f"news-{i}", feed="synthetic", title=self._text(10), url=f"https://example.org/news/{i}",
                           published=self._date(), summary=self._text(40), companies=[f"co-{self._entity(self.n_companies):03d}"])


# ---------------------------------------------------------------------------------- helpers
def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def timed(fn: Callable[[], Any], repeat: int) -> Dict[str, Any]:
    """Run fn `repeat` times; return min/median/max in ms and the size of the last result."""
    samples: List[float] = []
    result: Any = None
    for _ in range(repeat):
        t = _now_ms()
        result = fn()
        samples.append(_now_ms() - t)
    n = len(result) if hasattr(result, "__len__") else None
    return {"min_ms": round(min(samples), 2), "median_ms": round(statistics.median(samples), 2), "max_ms": round(max(samples), 2), "rows": n}


def db_size(conn: sqlite3.Connection, path: Path) -> Dict[str, Any]:
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    out: Dict[str, Any] = {"bytes_logical": page_size * page_count, "bytes_on_disk": 0, "tables": {}}
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            out["bytes_on_disk"] += p.stat().st_size
    try:
        for name, pgsize in conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC"):
            out["tables"][name] = int(pgsize)
    except sqlite3.OperationalError:
        out["tables"] = {"_note": "dbstat virtual table not available in this SQLite build"}
    return out


def explain(conn: sqlite3.Connection, sql: str, params: Sequence) -> List[str]:
    return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()]



# ---------------------------------------------------------------------------------- proposed upsert
class ProposedFtsConnection:
    """Proxy around the store's sqlite3 connection for `--load-mode proposed`.

    `Store.upsert_paper` runs unchanged (its relational INSERT ... ON CONFLICT and its Python-side merge
    of existing rows are the shipped code, not a copy); only its two FTS statements are rewritten into
    the form proposed in docs/SCALE.md 3.1, keyed by the papers.rowid instead of the UNINDEXED id column:

        DELETE FROM papers_fts WHERE rowid = (SELECT rowid FROM papers WHERE id = ?)   -- no-op for a new paper
        INSERT INTO papers_fts (rowid, id, ...) SELECT rowid, ?, ... FROM papers WHERE id = ?

    Every other call passes through, so both load modes produce identical relational tables.
    """

    SHIPPED_DELETE = "DELETE FROM papers_fts WHERE id=?"
    SHIPPED_INSERT = "INSERT INTO papers_fts (id, title, abstract, methods, funding, coi, affiliations, body) VALUES (?,?,?,?,?,?,?,?)"
    PROPOSED_DELETE = "DELETE FROM papers_fts WHERE rowid = (SELECT rowid FROM papers WHERE id=?)"
    PROPOSED_INSERT = ("INSERT INTO papers_fts (rowid, id, title, abstract, methods, funding, coi, affiliations, body) "
                       "SELECT rowid, ?,?,?,?,?,?,?,? FROM papers WHERE id=?")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence = ()):
        if sql == self.SHIPPED_DELETE:
            return self._conn.execute(self.PROPOSED_DELETE, params)
        if sql == self.SHIPPED_INSERT:
            return self._conn.execute(self.PROPOSED_INSERT, tuple(params) + (params[0],))
        return self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    @classmethod
    def check_store_source(cls) -> bool:
        """True when store.py still ships the id-keyed FTS statements this proxy rewrites.

        Once the store keys papers_fts by rowid itself (the shipped design since the hardening pass), the
        'proposed' mode has nothing to rewrite and runs as a plain 'store' benchmark."""
        src = inspect.getsource(Store.upsert_paper)
        return all(stmt in src for stmt in (cls.SHIPPED_DELETE, cls.SHIPPED_INSERT))


@contextlib.contextmanager
def load_mode(store: Store, mode: str):
    """Swap the store's connection for the proxy while mode == 'proposed'; restore it afterwards so the
    query phases run against the plain connection."""
    if mode != "proposed" or not ProposedFtsConnection.check_store_source():
        yield
        return
    real = store.conn
    store.conn = ProposedFtsConnection(real)  # type: ignore[assignment]
    try:
        yield
    finally:
        store.conn = real


# ---------------------------------------------------------------------------------- phases
def load(store: Store, synth: Synth, n: int, batch: int, mode: str, checkpoint: int = 10000) -> Dict[str, Any]:
    t0 = _now_ms()
    last_t, last_i = t0, 0
    curve: List[Dict[str, Any]] = []
    with load_mode(store, mode):
        for i in range(1, n + 1):
            p, mentions, score = synth.paper(i)
            store.upsert_paper(p)
            store.replace_mentions(p.id, mentions)
            store.save_score(p.id, score, None, score, "synthetic")
            if i % batch == 0:
                store.commit()
            if i % checkpoint == 0 or i == n:
                now = _now_ms()
                curve.append({"through": i, "papers_per_s": round((i - last_i) / max((now - last_t) / 1000.0, 1e-9), 1)})
                last_t, last_i = now, i
        store.commit()
    total_s = (_now_ms() - t0) / 1000.0
    # the store stamps first_seen with the load time; a real corpus accumulates over years, so backdate it
    # to the publication date plus 0-6 days (papers without a date keep "seen now", like undated feeds)
    store.conn.execute(
        """UPDATE papers SET first_seen = date(pub_date, '+' || (abs(random()) % 7) || ' days') || 'T12:00:00Z'
           WHERE pub_date != ''"""
    )
    store.commit()
    return {"mode": mode, "papers": n, "seconds": round(total_s, 1), "papers_per_s": round(n / max(total_s, 1e-9), 1), "curve": curve}


def reupsert(store: Store, synth: Synth, n: int, sample: int, mode: str) -> Dict[str, Any]:
    rng = random.Random(1234)
    ids = rng.sample(range(1, n + 1), min(sample, n))
    t0 = _now_ms()
    with load_mode(store, mode):
        for i in ids:
            p, mentions, _ = synth.paper(i)  # a fresh draw for the same id: the update path with changed content
            p.abstract = p.abstract + " Updated."
            store.upsert_paper(p)
            store.replace_mentions(p.id, mentions)
        store.commit()
    s = (_now_ms() - t0) / 1000.0
    return {"mode": mode, "papers": len(ids), "seconds": round(s, 2), "papers_per_s": round(len(ids) / max(s, 1e-9), 1)}


def fts_lookup_micro(conn: sqlite3.Connection, n: int, probes: int = 20) -> Dict[str, Any]:
    rng = random.Random(99)
    ids = [f"doi:10.9999/synth.{rng.randint(1, n)}" for _ in range(probes)]
    by_id = timed(lambda: [conn.execute("SELECT rowid FROM papers_fts WHERE id=?", (i,)).fetchone() for i in ids], 1)
    by_rowid = timed(lambda: [conn.execute("SELECT rowid FROM papers_fts WHERE rowid=(SELECT rowid FROM papers WHERE id=?)", (i,)).fetchone() for i in ids], 1)
    return {
        "probes": probes,
        "by_unindexed_id_ms_per_lookup": round(by_id["min_ms"] / probes, 3),
        "by_rowid_ms_per_lookup": round(by_rowid["min_ms"] / probes, 4),
        "plan_by_id": explain(conn, "SELECT rowid FROM papers_fts WHERE id=?", (ids[0],)),
        "plan_by_rowid": explain(conn, "SELECT rowid FROM papers_fts WHERE rowid=?", (1,)),
    }


def segment_cjk(text: str, vocab: Sequence[str]) -> str:
    """Greedy longest-match segmentation against a fixed vocabulary, returned space-separated so unicode61
    indexes one token per word. A stand-in for a real segmenter (jieba, pkuseg, ...) in the micro-benchmark."""
    words = sorted(vocab, key=len, reverse=True)
    out: List[str] = []
    i = 0
    while i < len(text):
        for w in words:
            if text.startswith(w, i):
                out.append(w)
                i += len(w)
                break
        else:
            out.append(text[i])
            i += 1
    return " ".join(out)


def cjk_search_micro(store: Store, repeat: int) -> Dict[str, Any]:
    """Chinese search on the shipped table versus the two options in docs/SCALE.md 3.4.

    * `papers_fts` (porter unicode61): every CJK code point is a token character, so an unspaced title is one
      token and a substring query finds nothing.
    * a trigram FTS table over the CJK papers' title + affiliations: finds substrings of >= 3 characters; FTS5
      documents that queries shorter than 3 unicode characters match no rows (https://www.sqlite.org/fts5.html).
    * a unicode61 table over a pre-segmented (space-separated) copy of the title: finds whole words of any
      length, including 2-character terms, but only words the segmenter knows.
    `LIKE '%..%'` on papers.title is included as the no-index baseline. The helper tables are dropped afterwards.
    """
    conn = store.conn
    rows = conn.execute(
        "SELECT rowid, id, title, affiliations_json FROM papers WHERE unicode(substr(title,1,1)) BETWEEN 19968 AND 40959"
    ).fetchall()  # U+4E00..U+9FFF: CJK Unified Ideographs
    out: Dict[str, Any] = {"cjk_papers": len(rows)}
    if not rows:
        return out
    sample = rows[0][2]
    words = segment_cjk(sample, CJK_WORDS).split()
    three = next((w for w in words if len(w) == 3), sample[:3])
    two = next((w for w in words if len(w) == 2), sample[:2])
    out["queries"] = {"three_char": three, "two_char": two, "sample_title": sample}
    out["unicode61_three_char"] = timed(lambda: store.search(three), repeat)
    out["unicode61_two_char"] = timed(lambda: store.search(two), repeat)
    out["like_scan_three_char"] = timed(lambda: conn.execute("SELECT id FROM papers WHERE title LIKE ?", (f"%{three}%",)).fetchall(), repeat)
    conn.execute("DROP TABLE IF EXISTS bench_fts_cjk")
    conn.execute("DROP TABLE IF EXISTS bench_fts_cjk_seg")
    try:
        t = _now_ms()
        conn.execute("CREATE VIRTUAL TABLE bench_fts_cjk USING fts5(id UNINDEXED, title, affiliations, tokenize = 'trigram')")
        conn.executemany("INSERT INTO bench_fts_cjk (rowid, id, title, affiliations) VALUES (?,?,?,?)", rows)
        store.commit()
        out["trigram_available"] = True
        out["trigram_build_seconds"] = round((_now_ms() - t) / 1000.0, 3)
        out["trigram_three_char"] = timed(lambda: conn.execute("SELECT id FROM bench_fts_cjk WHERE bench_fts_cjk MATCH ?", (three,)).fetchall(), repeat)
        out["trigram_two_char"] = timed(lambda: conn.execute("SELECT id FROM bench_fts_cjk WHERE bench_fts_cjk MATCH ?", (two,)).fetchall(), repeat)
        try:
            out["trigram_bytes"] = conn.execute("SELECT SUM(pgsize) FROM dbstat WHERE name LIKE 'bench_fts_cjk_%'").fetchone()[0]
        except sqlite3.OperationalError:
            out["trigram_bytes"] = None
    except sqlite3.OperationalError as exc:  # SQLite < 3.34 has no trigram tokenizer
        out["trigram_available"] = False
        out["trigram_error"] = str(exc)
    t = _now_ms()
    conn.execute("CREATE VIRTUAL TABLE bench_fts_cjk_seg USING fts5(id UNINDEXED, title, tokenize = 'unicode61')")
    conn.executemany("INSERT INTO bench_fts_cjk_seg (rowid, id, title) VALUES (?,?,?)",
                     [(r[0], r[1], segment_cjk(r[2], CJK_WORDS)) for r in rows])
    store.commit()
    out["segmented_build_seconds"] = round((_now_ms() - t) / 1000.0, 3)
    out["segmented_two_char"] = timed(lambda: conn.execute("SELECT id FROM bench_fts_cjk_seg WHERE bench_fts_cjk_seg MATCH ?", (two,)).fetchall(), repeat)
    out["segmented_three_char"] = timed(lambda: conn.execute("SELECT id FROM bench_fts_cjk_seg WHERE bench_fts_cjk_seg MATCH ?", (three,)).fetchall(), repeat)
    conn.execute("DROP TABLE IF EXISTS bench_fts_cjk")
    conn.execute("DROP TABLE IF EXISTS bench_fts_cjk_seg")
    store.commit()
    return out


WINDOW_SQL = "((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date = '' AND substr(p.first_seen,1,10) >= ?))"


def proposed_entity_counts(conn: sqlite3.Connection, kind: str, since: str, until: str, limit: int = 60) -> List[Dict[str, Any]]:
    """Query change proposed in docs/SCALE.md for instrument_counts / company_counts: drive from the
    papers in the window (indexed range scan), collect their mentions once into a temp table, and take
    the per-entity example row from that small set instead of re-scanning every mention of each entity.
    CROSS JOIN pins papers as the outer loop (SQLite honours it as a join-order hint), so the plan does
    not depend on whether ANALYZE has run."""
    conn.execute("DROP TABLE IF EXISTS temp.win_mentions")
    conn.execute(
        f"""CREATE TEMP TABLE win_mentions AS
            SELECT m.entity_id, m.entity_name, m.vendor, m.ticker, m.category, m.section, m.count, m.paper_id, p.title, p.url
            FROM papers p CROSS JOIN mentions m ON m.paper_id = p.id
            WHERE m.kind = ? AND {WINDOW_SQL}""",
        (kind, since, until, since),
    )
    conn.execute("CREATE INDEX temp.win_mentions_entity ON win_mentions(entity_id, count DESC)")
    rows = conn.execute(
        """SELECT entity_id, entity_name, vendor, ticker, category, COUNT(DISTINCT paper_id) AS papers, SUM(count) AS mentions,
                  GROUP_CONCAT(DISTINCT section) AS sections,
                  (SELECT title FROM win_mentions x WHERE x.entity_id = w.entity_id ORDER BY x.count DESC LIMIT 1) AS example_title,
                  (SELECT url FROM win_mentions x WHERE x.entity_id = w.entity_id ORDER BY x.count DESC LIMIT 1) AS example_url
           FROM win_mentions w GROUP BY entity_id ORDER BY papers DESC, mentions DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    cols = ["entity_id", "entity_name", "vendor", "ticker", "category", "papers", "mentions", "sections", "example_title", "example_url"]
    return [dict(zip(cols, r)) for r in rows]


def proposed_new_entities(conn: sqlite3.Connection, since: str, kind: str) -> List[Dict[str, Any]]:
    """Query change proposed in docs/SCALE.md for new_entities: candidates are the entities mentioned
    by papers first seen in the window (an indexed range on first_seen: ISO timestamps compare as
    strings, so `first_seen >= 'YYYY-MM-DD'` needs no substr()); each candidate is then rejected by
    the first older mention found (one NOT EXISTS per entity, mentions as the outer loop via CROSS JOIN),
    instead of grouping every mention of the kind ever stored."""
    rows = conn.execute(
        """WITH cand AS (
             SELECT m.entity_id, m.entity_name, m.ticker, MIN(substr(p.first_seen,1,10)) AS first_seen, COUNT(DISTINCT m.paper_id) AS papers
             FROM papers p CROSS JOIN mentions m ON m.paper_id = p.id
             WHERE m.kind = ? AND p.first_seen >= ?
             GROUP BY m.entity_id)
           SELECT entity_id, entity_name, ticker, first_seen, papers FROM cand c
           WHERE NOT EXISTS (SELECT 1 FROM mentions m2 CROSS JOIN papers p2 ON p2.id = m2.paper_id
                             WHERE m2.kind = ? AND m2.entity_id = c.entity_id AND p2.first_seen < ?)
           ORDER BY papers DESC""",
        (kind, since, kind, since),
    ).fetchall()
    cols = ["entity_id", "entity_name", "ticker", "first_seen", "papers"]
    return [dict(zip(cols, r)) for r in rows]


def equivalence(store: Store, synth: Synth) -> Dict[str, bool]:
    """The proposed rewrites must return the same entities and paper counts as the shipped queries."""
    end = synth.end.isoformat()
    w7 = (synth.end - timedelta(days=7)).isoformat()
    out = {}
    for kind, fn in (("instrument", store.instrument_counts), ("company", store.company_counts)):
        a = {(r["entity_id"], r["papers"]) for r in fn(w7, end, 1000)}
        b = {(r["entity_id"], r["papers"]) for r in proposed_entity_counts(store.conn, kind, w7, end, 1000)}
        out[f"{kind}_counts"] = a == b
    a = {(r["entity_id"], r["papers"]) for r in store.new_entities(w7, "instrument")}
    b = {(r["entity_id"], r["papers"]) for r in proposed_new_entities(store.conn, w7, "instrument")}
    out["new_entities"] = a == b
    return out


def query_suite(store: Store, synth: Synth, repeat: int) -> Dict[str, Any]:
    end = synth.end.isoformat()
    w7 = (synth.end - timedelta(days=7)).isoformat()
    w30 = (synth.end - timedelta(days=30)).isoformat()
    w365 = (synth.end - timedelta(days=365)).isoformat()
    popular, rare = "inst-000", f"inst-{synth.n_instruments - 1:03d}"
    common_word, rare_word = WORDS[0], "zzqx"
    q: Dict[str, Callable[[], Any]] = {
        "papers_in_window_7d": lambda: store.papers_in_window(w7, end),
        "papers_in_window_365d": lambda: store.papers_in_window(w365, end),
        "top_papers_7d": lambda: store.top_papers(w7, end, 25),
        "instrument_counts_7d": lambda: store.instrument_counts(w7, end),
        "instrument_counts_30d": lambda: store.instrument_counts(w30, end),
        "instrument_counts_365d": lambda: store.instrument_counts(w365, end),
        "company_counts_7d": lambda: store.company_counts(w7, end),
        "company_counts_365d": lambda: store.company_counts(w365, end),
        "new_entities_instrument_7d": lambda: store.new_entities(w7, "instrument"),
        "archive_instrument_counts_7d": lambda: store.archive_instrument_counts(w7, end),
        "archive_strategy_counts_7d": lambda: store.archive_strategy_counts(w7, end),
        "archive_instrument_counts_365d": lambda: store.archive_instrument_counts(w365, end),
        "PROPOSED_instrument_counts_7d": lambda: proposed_entity_counts(store.conn, "instrument", w7, end),
        "PROPOSED_instrument_counts_365d": lambda: proposed_entity_counts(store.conn, "instrument", w365, end),
        "PROPOSED_company_counts_7d": lambda: proposed_entity_counts(store.conn, "company", w7, end),
        "PROPOSED_new_entities_instrument_7d": lambda: proposed_new_entities(store.conn, w7, "instrument"),
        "entity_papers_popular": lambda: store.entity_papers(popular),
        "entity_papers_rare": lambda: store.entity_papers(rare),
        "entity_papers_popular_90d": lambda: store.entity_papers(popular, since=(synth.end - timedelta(days=90)).isoformat()),
        "search_common_word": lambda: store.search(common_word),
        "search_common_word_90d": lambda: store.search(common_word, since=(synth.end - timedelta(days=90)).isoformat()),
        "search_rare_word": lambda: store.search(rare_word),
        "search_phrase": lambda: store.search('"single cell"'),
        "search_methods_column": lambda: store.search("nanopore", sections=["methods"]),
        "search_cjk_unicode61": lambda: store.search("转录组"),
        "news_in_window_7d": lambda: store.news_in_window(w7),
        "counts": lambda: store.counts(),
    }
    out: Dict[str, Any] = {}
    for name, fn in q.items():
        out[name] = timed(fn, repeat)
    return out


def plans(conn: sqlite3.Connection, synth: Synth) -> Dict[str, List[str]]:
    end = synth.end.isoformat()
    w7 = (synth.end - timedelta(days=7)).isoformat()
    return {
        "instrument_counts": explain(conn, """SELECT m.entity_id, COUNT(DISTINCT m.paper_id), SUM(m.count)
            FROM mentions m JOIN papers p ON p.id = m.paper_id
            WHERE m.kind='instrument' AND ((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))
            GROUP BY m.entity_id ORDER BY 2 DESC LIMIT 60""", (w7, end, w7)),
        "instrument_counts_example_row": explain(conn, """SELECT p.title, p.url FROM mentions m JOIN papers p ON p.id=m.paper_id
            WHERE m.kind='instrument' AND m.entity_id=? AND ((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?))
            ORDER BY m.count DESC LIMIT 1""", ("inst-000", w7, end, w7)),
        "new_entities": explain(conn, """SELECT m.entity_id, MIN(substr(p.first_seen,1,10)) AS first_seen, COUNT(DISTINCT m.paper_id)
            FROM mentions m JOIN papers p ON p.id=m.paper_id WHERE m.kind=? GROUP BY m.entity_id HAVING first_seen >= ?""", ("instrument", w7)),
        "entity_papers": explain(conn, """SELECT DISTINCT p.id, p.title FROM mentions m JOIN papers p ON p.id=m.paper_id
            WHERE m.entity_id=? ORDER BY p.pub_date DESC LIMIT 100""", ("inst-000",)),
        "archive_instrument_counts": explain(conn, """SELECT instrument_model, COUNT(*) FROM archive_records
            WHERE first_public >= ? AND first_public <= ? GROUP BY instrument_model""", (w7, end)),
        "news_in_window": explain(conn, """SELECT * FROM news_items WHERE substr(first_seen,1,10) >= ? OR substr(published,1,10) >= ?
            ORDER BY published DESC LIMIT 200""", (w7, w7)),
        "search_with_since": explain(conn, """SELECT p.id FROM papers_fts JOIN papers p ON p.id = papers_fts.id
            WHERE papers_fts MATCH ? AND (p.pub_date >= ? OR (p.pub_date='' AND substr(p.first_seen,1,10) >= ?)) ORDER BY bm25(papers_fts) LIMIT 50""",
            ("cell", w7, w7)),
    }


# ---------------------------------------------------------------------------------- main
def run(args: argparse.Namespace) -> Dict[str, Any]:
    tmpdir = None
    if args.db:
        db_path = Path(args.db)
        if db_path.exists():
            db_path.unlink()
    else:
        tmpdir = tempfile.mkdtemp(prefix="litradar-bench-")
        db_path = Path(tmpdir) / "bench.sqlite"
    synth = Synth(args.seed, years=args.years, fulltext_share=args.fulltext_share, body_words=args.body_words)
    if not ProposedFtsConnection.check_store_source():
        print("note: the store already keys papers_fts by rowid; 'proposed' mode runs as 'store' mode", file=sys.stderr)
    store = Store(db_path)
    report: Dict[str, Any] = {
        "params": vars(args), "sqlite_version": sqlite3.sqlite_version, "python": sys.version.split()[0], "db": str(db_path),
    }
    print(f"[bench] sqlite {sqlite3.sqlite_version}; db {db_path}; N={args.n} papers, {args.archive_n} archive records, {args.news_n} news", flush=True)

    # 1. load
    if args.load_mode == "store" and args.n > 50000:
        est_min = args.n * args.n / 50000 ** 2 * 22
        print(f"[bench] note: the shipped upsert cost grows linearly with the corpus (FTS delete by UNINDEXED id), "
              f"so load time grows with N^2: expect roughly {est_min:.0f} min for N={args.n} (22 min measured at 50k); "
              f"use --load-mode proposed for a fast run", flush=True)
    report["load"] = load(store, synth, args.n, args.batch, args.load_mode)
    print(f"[bench] load ({args.load_mode}): {report['load']['papers_per_s']} papers/s over {report['load']['seconds']} s; curve {[c['papers_per_s'] for c in report['load']['curve']]}", flush=True)
    t = _now_ms()
    batch_recs: List[ArchiveRecord] = []
    for r in synth.archive_records(args.archive_n):
        batch_recs.append(r)
        if len(batch_recs) >= 5000:
            store.upsert_archive_records(batch_recs)
            store.commit()
            batch_recs = []
    store.upsert_archive_records(batch_recs)
    store.commit()
    s = (_now_ms() - t) / 1000.0
    report["load_archive"] = {"records": args.archive_n, "seconds": round(s, 2), "records_per_s": round(args.archive_n / max(s, 1e-9), 1)}
    t = _now_ms()
    store.upsert_news(list(synth.news(args.news_n)))
    store.commit()
    report["load_news"] = {"items": args.news_n, "seconds": round((_now_ms() - t) / 1000.0, 2)}
    report["counts"] = store.counts()
    report["size_after_load"] = db_size(store.conn, db_path)
    print(f"[bench] archive: {report['load_archive']['records_per_s']} records/s; db {report['size_after_load']['bytes_logical'] / 1e6:.1f} MB", flush=True)

    # 2. update path + FTS micro-benchmark
    report["reupsert"] = reupsert(store, synth, args.n, args.update_sample, args.load_mode)
    print(f"[bench] re-upsert ({args.load_mode}): {report['reupsert']['papers_per_s']} papers/s", flush=True)
    report["fts_lookup"] = fts_lookup_micro(store.conn, args.n)
    print(f"[bench] FTS lookup by UNINDEXED id {report['fts_lookup']['by_unindexed_id_ms_per_lookup']} ms vs by rowid {report['fts_lookup']['by_rowid_ms_per_lookup']} ms", flush=True)

    # 3. queries: as stored -> + ANALYZE (free) -> + proposed indexes (re-analysed)
    report["plans_before"] = plans(store.conn, synth)
    report["queries_before"] = query_suite(store, synth, args.repeat)
    report["equivalence"] = equivalence(store, synth)
    print("[bench] queries (as stored):", {k: v["median_ms"] for k, v in report["queries_before"].items()}, flush=True)
    print("[bench] proposed rewrites return the same results:", report["equivalence"], flush=True)
    if not args.skip_after:
        t = _now_ms()
        store.conn.execute("ANALYZE")
        store.commit()
        report["analyze_seconds"] = round((_now_ms() - t) / 1000.0, 2)
        report["plans_after_analyze"] = plans(store.conn, synth)
        report["queries_after_analyze"] = query_suite(store, synth, args.repeat)
        print("[bench] queries (+ANALYZE):", {k: v["median_ms"] for k, v in report["queries_after_analyze"].items()}, flush=True)
        t = _now_ms()
        for _, sql in PROPOSED_INDEXES:
            store.conn.execute(sql)
        store.conn.execute("ANALYZE")
        store.commit()
        report["index_build"] = {"seconds": round((_now_ms() - t) / 1000.0, 2), "indexes": [n for n, _ in PROPOSED_INDEXES]}
        report["size_after_indexes"] = db_size(store.conn, db_path)
        report["plans_after_indexes"] = plans(store.conn, synth)
        report["queries_after_indexes"] = query_suite(store, synth, args.repeat)
        print("[bench] queries (+ANALYZE +indexes):", {k: v["median_ms"] for k, v in report["queries_after_indexes"].items()}, flush=True)

    # 4. Chinese search: shipped tokenizer vs trigram vs pre-segmented (helper tables are dropped again)
    report["cjk_search"] = cjk_search_micro(store, args.repeat)
    cj = report["cjk_search"]
    if cj.get("cjk_papers"):
        print(f"[bench] CJK search ({cj['cjk_papers']} Chinese titles): unicode61 {cj['unicode61_three_char']['rows']} hits for "
              f"{cj['queries']['three_char']!r}; trigram {cj.get('trigram_three_char', {}).get('rows')} hits, "
              f"{cj.get('trigram_two_char', {}).get('rows')} for the 2-char {cj['queries']['two_char']!r}; "
              f"segmented+unicode61 {cj['segmented_two_char']['rows']} hits for the 2-char term", flush=True)

    store.close()
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"[bench] report written to {args.json}")
    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        if tmpdir:
            os.rmdir(tmpdir)
    else:
        print(f"[bench] database kept at {db_path}")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark the lit-radar SQLite store with synthetic data")
    ap.add_argument("--n", type=int, default=200000, help="synthetic papers to insert (default 200k)")
    ap.add_argument("--archive-n", type=int, default=None, help="synthetic archive records (default: same as --n)")
    ap.add_argument("--news-n", type=int, default=None, help="synthetic news items (default: --n / 20)")
    ap.add_argument("--batch", type=int, default=1000, help="papers per commit")
    ap.add_argument("--load-mode", choices=["store", "proposed"], default="store",
                    help="store = Store.upsert_paper as shipped; proposed = the same upsert with its FTS rows keyed by rowid (docs/SCALE.md 3.1)")
    ap.add_argument("--update-sample", type=int, default=1000, help="papers to re-upsert for the update-path timing")
    ap.add_argument("--repeat", type=int, default=5, help="repetitions per query")
    ap.add_argument("--years", type=int, default=3, help="spread of publication dates")
    ap.add_argument("--fulltext-share", type=float, default=0.3, help="share of papers with a body in the FTS table")
    ap.add_argument("--body-words", type=int, default=400, help="body length for full-text papers")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", help="database path (default: a temp file)")
    ap.add_argument("--keep", action="store_true", help="keep the database afterwards")
    ap.add_argument("--skip-after", action="store_true", help="do not measure the proposed indexes / ANALYZE")
    ap.add_argument("--json", help="write the full report to this JSON file")
    args = ap.parse_args(argv)
    if args.archive_n is None:
        args.archive_n = args.n
    if args.news_n is None:
        args.news_n = max(args.n // 20, 10)
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
