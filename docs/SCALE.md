# Scale path: from a weekly SQLite file to a persistent global corpus

The radar started as "one SQLite file, one weekly job". The goal now is a corpus that keeps every
paper, mention and archive record ever seen, across all countries and languages, and that can
answer trend questions over years. This note is the engineering plan for getting there without
breaking the weekly job: what the current design costs (measured), where it stops being enough,
what to change in SQLite first, and when and how to add DuckDB (analytics) and Postgres (many users).

Everything below marked **measured** comes from `scripts/bench_store.py` or from exporting the
repository's real database on 2026-10-17 (SQLite 3.51.0, Python 3.9.6, laptop SSD). Everything
marked *estimate* is arithmetic on those measurements.

## 1. How big the corpus gets

Measured on the real database after one seven-day run (`data/radar.sqlite`, 117.7 MB):

| table group | rows | bytes | per row |
|---|---|---|---|
| papers + papers_fts (content + index) + indexes | 3,562 papers | 74.4 MB | **~21 KB per paper** |
| archive_records + 3 indexes | 123,164 experiments | 40.1 MB | **~290 B per record** |
| mentions, scores, news, tracked hits, runs | 8,658 rows | 3.2 MB | — |

The paper footprint is dominated by text that is stored twice: once in `papers` (abstract,
sections JSON, author JSON) and once in the FTS5 content table (`papers_fts_content`, 39.3 MB),
plus the FTS index itself (`papers_fts_data`, 14.8 MB). 12% of papers had open full text; their
`body` averages 7.4 K characters (max 202 K) and lives only in the FTS table.

*Estimates* at the measured per-row costs:

| scope | papers / year | archive records / year | SQLite growth / year |
|---|---|---|---|
| current journal + preprint scope (3.6 K papers and 123 K archive records a week) | ~185 K | ~6.4 M | **~5.8 GB** (3.9 papers + 1.9 archive) |
| global life-science scope (OpenAlex-order coverage, all countries) | ~1 M | ~6.4 M | **~23 GB** |
| global scope with full text for 30% of papers | ~1 M | ~6.4 M | ~35 GB |

So the file passes the 10 GB GitHub Actions cache ceiling within two years at today's scope and
within months at global scope. That, not query speed, is the first wall (section 8).

## 2. Benchmark: what the current design costs

`scripts/bench_store.py` builds a temporary database with N synthetic papers (abstract ~150 words,
Methods ~120 words, 30% with a 400-word body, 10% with Chinese titles and affiliations), 0-4
instrument and 0-2 company mentions per paper with a heavy-tailed popularity, a score per paper,
N archive records and N/20 news items spread over three years, then times the shipped
`Store` API and every query the digest and CLI run.

```bash
python3 scripts/bench_store.py --n 50000                        # shipped Store.upsert_paper
python3 scripts/bench_store.py --n 50000 --load-mode proposed   # with the FTS fix from section 3.1
python3 scripts/bench_store.py --n 200000 --json bench.json --keep --db /tmp/bench.sqlite
```

### 2.1 Write path (N = 50,000, measured)

| metric | shipped `Store.upsert_paper` | proposed upsert (section 3.1) |
|---|---|---|
| load throughput, papers/s, first 10 K → last 10 K | **166 → 19** (falls with N) | 7,586 → 6,725 (flat) |
| load throughput, whole run | 32 papers/s (1,569 s for 50 K) | 7,222 papers/s (6.9 s for 50 K) |
| re-upsert 1,000 existing papers (update path) | 18.3 papers/s | 3,282 papers/s |
| FTS row lookup by `id` (UNINDEXED column), per lookup | **77 ms** at 50 K rows (3.5 ms at 3 K rows: linear) | 0.013 ms by rowid |
| archive records (`upsert_archive_records`, INSERT OR IGNORE) | 170,000 records/s | same |
| database size after load | 624 MB (12.5 KB per synthetic paper) | same |

**The finding.** `Store.upsert_paper` runs `DELETE FROM papers_fts WHERE id=?` on every paper, new
or not. `id` is an `UNINDEXED` FTS5 column, so that statement is a full scan of the FTS content
table (`EXPLAIN QUERY PLAN` → `SCAN papers_fts VIRTUAL TABLE INDEX 0:`), and its cost grows
linearly with the corpus: 3.5 ms at 3 K papers, 76 ms at 50 K, and (*estimate*) ~1.5 s at
1 M papers. A weekly run of 5 K papers against a 1 M-paper corpus would spend about two hours in
that one statement. This is the single change that matters for the write path.

### 2.2 Read path (N = 50,000 papers, 60 K instrument mentions, three-year spread; median ms)

"As stored" is the schema in this change (it already contains `idx_mentions_entity_id`, see
below); "+ANALYZE" adds only planner statistics; "+indexes" adds the remaining candidate indexes
(`idx_mentions_kind_paper`, `idx_archive_public_model`) and re-runs ANALYZE. `PROPOSED_*` rows
are the query rewrites in section 3.2, run on the same data and checked to return the same
entities and counts as the shipped queries. Medians of 5 runs; a second benchmark process was
loading data on the same machine during this run, so treat differences under ~10% as noise.

| query | rows | as stored | +ANALYZE | +indexes |
|---|---:|---:|---:|---:|
| papers_in_window, 7-day window | 2797 | 34.77 | 33.88 | 34.99 |
| papers_in_window, 365-day window | 18231 | 228.92 | 233.46 | 230.68 |
| top_papers, 7 days | 25 | 34.87 | 34.96 | 33.62 |
| instrument_counts, 7 days | 60 | 387.37 | 197.74 | 200.77 |
| instrument_counts, 30 days | 60 | 391.27 | 197.99 | 202.31 |
| instrument_counts, 365 days | 60 | 386.67 | 255.79 | 258.7 |
| PROPOSED instrument_counts, 7 days (3.2) | 60 | 13.18 | 12.48 | 12.0 |
| PROPOSED instrument_counts, 365 days (3.2) | 60 | 89.56 | 88.98 | 85.28 |
| company_counts, 7 days | 60 | 210.41 | 111.03 | 112.88 |
| company_counts, 365 days | 60 | 216.55 | 169.17 | 152.13 |
| PROPOSED company_counts, 7 days (3.2) | 60 | 10.49 | 10.77 | 9.98 |
| new_entities (instrument), 7 days | 0 | 223.39 | 219.3 | 214.5 |
| PROPOSED new_entities, 7 days (3.2) | 0 | 8.99 | 10.34 | 9.1 |
| archive_instrument_counts, 7 days | 33 | 0.25 | 0.24 | 0.36 |
| archive_instrument_counts, 365 days (16 K rows) | 58 | 11.38 | 10.57 | 10.94 |
| archive_strategy_counts, 7 days | 10 | 0.12 | 0.16 | 0.17 |
| entity_papers, most popular entity | 100 | 8.99 | 9.57 | 9.19 |
| entity_papers, rare entity | 21 | 0.08 | 0.08 | 0.08 |
| entity_papers, popular entity, since 90d | 100 | 7.36 | 8.05 | 7.13 |
| search, word in most documents | 50 | 238.85 | 247.45 | 245.24 |
| search, same word, --since 90d | 50 | 236.53 | 217.52 | 239.43 |
| search, word in no document | 0 | 0.05 | 0.05 | 0.05 |
| search, phrase "single cell" | 50 | 37.73 | 39.03 | 37.71 |
| search, --in methods | 50 | 188.2 | 195.42 | 192.77 |
| search, Chinese substring (unicode61: 0 hits) | 50 | 10.64 | 10.66 | 10.38 |
| news_in_window, 7 days | 200 | 1.23 | 1.2 | 1.24 |
| counts (6 COUNT(*)) | 6 | 0.14 | 0.15 | 0.14 |

Reading the table:

* **The digest's entity tables are the slow queries.** `instrument_counts` and `company_counts`
  are each two passes over every mention of that kind ever stored: the planner starts from
  `idx_mentions_entity (kind=?)`, joins each mention to its paper, and only then applies the date
  window; then the per-entity "example paper" subquery repeats that for each of the top 60
  entities. Cost is O(all mentions), not O(mentions in the window). ANALYZE alone switches the
  main query to a papers-first plan (`idx_papers_pub_date` → mentions by primary key), which is
  why "+ANALYZE" roughly halves them; the example subquery still scans. The rewrite in 3.2 makes
  both O(window).
* **`new_entities` is O(all mentions) by construction** (GROUP BY over every mention of the kind,
  then HAVING on the minimum first_seen). The NOT EXISTS rewrite in 3.2 is O(window).
* **`entity_papers` (the `litradar entity` command) was a full scan of mentions** because the
  only index starts with `kind` and the query filters on `entity_id` alone
  (`SCAN m USING COVERING INDEX sqlite_autoindex_mentions_1`). `idx_mentions_entity_id` fixes
  that: 4.87 ms → 0.08 ms for a rare entity, measured on the run made before the index was added
  to the schema (the popular entity stays at ~9 ms because its 100 result rows dominate). The
  index is now in the schema (additive `CREATE INDEX IF NOT EXISTS`, 2.7 MB at 50 K papers,
  0.07 s to build on an existing database).
* **FTS search is proportional to the number of matching documents**, because bm25 ranking has
  to score every hit before `LIMIT`: a word present in most documents costs ~240 ms at 50 K
  papers, a rare word 0.05 ms, a phrase 36 ms. The `--since` filter does not help because it is
  applied after the FTS scan. Section 3.3 has the fix (a date token column inside the FTS table).
* **Archive queries are cheap** (index range scan + GROUP BY): 0.26 ms for a 7-day window at this
  density; the 365-day window (16 K rows, ~11 ms) is the closer proxy for one real week (123 K rows,
  *estimate* ~90 ms).
* The 7-day window holds 2,797 rows, not ~320, because the 5% of synthetic papers without a
  publication date count as "first seen this week", exactly as undated feed items do in the real
  store; `papers_in_window` cost is row materialisation (every column, JSON included) at ~13 µs
  per row.
* The other two candidate indexes did not pay for themselves: `idx_mentions_kind_paper` only
  matters when ANALYZE has not run (the primary key already serves the papers-first plan), and
  `idx_archive_public_model` turns the archive scan into a covering scan with no measurable gain.
  Neither is added.

### 2.3 Unicode and Chinese text

The FTS table uses `tokenize = 'porter unicode61'`. unicode61 splits on spaces and punctuation, so
a Chinese title becomes one token per run of characters and `litradar search 转录组` finds
nothing (measured: 0 hits against a stored Chinese title; the bench's `search_cjk_unicode61` row
is this case). SQLite's `trigram` tokenizer (3.34+) indexes every 3-character window and finds
the substring. Proposal (section 3.4): a second FTS table with the trigram tokenizer for
title + abstract, roughly 3x the index size of those two columns.

## 3. Changes inside SQLite (proposals, measured where possible)

Only one item below is applied in this change (the index in 3.5); the rest are proposals that
touch `litradar/store.py` queries and belong in a dedicated change with a migration.

### 3.1 Key the FTS row by rowid; skip the delete for new papers

`papers` is a rowid table, so every paper has a stable integer rowid as long as the table is never
`VACUUM`ed into new rowids (it is not; and `INTEGER PRIMARY KEY` would make it explicit). Write the
FTS row with that rowid and delete by it:

```sql
-- in upsert_paper, after the INSERT ... ON CONFLICT on papers:
DELETE FROM papers_fts WHERE rowid = (SELECT rowid FROM papers WHERE id = ?);   -- only when the paper existed
INSERT INTO papers_fts (rowid, id, title, abstract, methods, funding, coi, affiliations, body)
  SELECT rowid, ?, ?, ?, ?, ?, ?, ?, ? FROM papers WHERE id = ?;
```

Measured: lookup by rowid 0.013 ms vs 77 ms by `id` at 50 K papers; load throughput flat at
~7 K papers/s instead of falling with N. One-time migration for existing databases:
`INSERT INTO papers_fts(papers_fts) VALUES('rebuild')` is not enough because the rowids are
unrelated; rebuild the table from `papers` + the old FTS body column with
`INSERT INTO new_fts(rowid, ...) SELECT p.rowid, ... FROM papers p JOIN old_fts f ON f.id = p.id`.
The `search()` join `ON p.id = papers_fts.id` keeps working unchanged (or becomes
`ON p.rowid = papers_fts.rowid`, which is faster).

A further step for the storage double-count in section 1 is an **external-content FTS5 table**
(`content='papers_text', content_rowid='rowid'`), where the text lives once in an ordinary table
and FTS holds only the index. SQLite 3.43+ also offers `contentless_delete=1`. Both halve the
paper footprint; both are rebuilds, not in-place migrations.

### 3.2 Window-first entity queries

Replace the two-pass `instrument_counts` / `company_counts` with one pass that starts from the
papers in the window and takes the example row from that small set (the bench's
`proposed_entity_counts`, which returns identical entities and counts):

```sql
CREATE TEMP TABLE win_mentions AS
  SELECT m.entity_id, m.entity_name, m.vendor, m.ticker, m.category, m.section, m.count, m.paper_id, p.title, p.url
  FROM papers p CROSS JOIN mentions m ON m.paper_id = p.id        -- CROSS JOIN pins papers as the outer loop
  WHERE m.kind = ? AND ((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date = '' AND substr(p.first_seen,1,10) >= ?));
CREATE INDEX temp.win_mentions_entity ON win_mentions(entity_id, count DESC);
SELECT entity_id, entity_name, vendor, ticker, category, COUNT(DISTINCT paper_id) AS papers, SUM(count) AS mentions,
       GROUP_CONCAT(DISTINCT section) AS sections,
       (SELECT title FROM win_mentions x WHERE x.entity_id = w.entity_id ORDER BY x.count DESC LIMIT 1) AS example_title,
       (SELECT url   FROM win_mentions x WHERE x.entity_id = w.entity_id ORDER BY x.count DESC LIMIT 1) AS example_url
FROM win_mentions w GROUP BY entity_id ORDER BY papers DESC, mentions DESC LIMIT ?;
```

and `new_entities` with a candidate-then-reject form (`proposed_new_entities`). `first_seen` is an
ISO timestamp, so `p.first_seen >= 'YYYY-MM-DD'` is the same test as the shipped
`substr(p.first_seen,1,10) >= ?` and lets `idx_papers_first_seen` serve the range:

```sql
WITH cand AS (
  SELECT m.entity_id, m.entity_name, m.ticker, MIN(substr(p.first_seen,1,10)) AS first_seen, COUNT(DISTINCT m.paper_id) AS papers
  FROM papers p CROSS JOIN mentions m ON m.paper_id = p.id
  WHERE m.kind = ? AND p.first_seen >= ?
  GROUP BY m.entity_id)
SELECT entity_id, entity_name, ticker, first_seen, papers FROM cand c
WHERE NOT EXISTS (SELECT 1 FROM mentions m2 CROSS JOIN papers p2 ON p2.id = m2.paper_id
                  WHERE m2.kind = ? AND m2.entity_id = c.entity_id AND p2.first_seen < ?)
ORDER BY papers DESC;
```

Measured gains are in the table above (`PROPOSED_*` rows); the bench checks that both rewrites
return exactly the shipped entities and counts. Both are O(papers in the window) plus one probe
per candidate entity, so they stay flat as the corpus grows; the shipped versions grow linearly
with total mentions. Two lessons from measuring them: without `CROSS JOIN` the planner picked the
mentions-first order for the rewrite as well until ANALYZE had run (230 ms instead of 14 ms), and
a correlated NOT EXISTS must sit on the grouped candidates, not on every mention row, or it is
evaluated thousands of times.

Smaller items in the same spirit:

* `news_in_window` filters on `substr(first_seen,1,10)`, which no index can serve. Store a
  `published_day` column (or compare the full ISO string: `first_seen >= ?` is equivalent for
  `YYYY-MM-DD...` strings) and index it. News is small today (1,263 rows) so this is not urgent.
* `papers_in_window` returns every column, including `authors_json` and `sections_json`, for
  every paper in the window and the digest then uses a handful. A projection (`id, title, url,
  journal, pub_date, kind, source, final, reasons, fulltext_available`) cuts the 365-day query
  from 282 ms to tens of ms (*estimate*; row materialisation dominates).
* Run `PRAGMA optimize` at the end of each weekly run (or `ANALYZE` after large loads). It is
  free (0.08 s at 50 K papers) and changed the `instrument_counts` plan on its own.

### 3.3 Date tokens inside the FTS table (search over a window)

Add an FTS column `when_` holding tokens like `y2026 y2026m10 y2026w42` and query
`{when_}: y2026m10 AND (<user query>)`. FTS5 intersects the posting lists before ranking, so
`litradar search "Xenium" --since 90d` scores only the window's hits instead of every Xenium
paper ever stored. Costs one short column per row; requires a rebuild to backfill.

### 3.4 A trigram FTS table for Chinese, Japanese and Korean text

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts_cjk USING fts5(id UNINDEXED, title, abstract, affiliations, tokenize = 'trigram');
```

populated alongside `papers_fts` for papers whose text contains CJK code points (or for all
papers, at ~3x the index size of those columns). `search()` then tries `papers_fts` first and
falls back to the trigram table when the query contains CJK characters. SQLite 3.34+ (Python 3.9
wheels on GitHub's ubuntu runners ship 3.37+; this machine has 3.51).

### 3.5 Applied in this change

```sql
CREATE INDEX IF NOT EXISTS idx_mentions_entity_id ON mentions(entity_id);
```

Additive, built on first open of an existing database (0.07 s per 50 K papers), and the
`litradar entity` path goes from a scan of all mentions to an index lookup (section 2.2).

## 4. When SQLite is enough, and when it is not

SQLite's own guidance: fine for "device-local storage with low writer concurrency and less than a
terabyte of content"; one writer at a time per file, unlimited concurrent readers; avoid it when
"the same database will be accessed directly ... simultaneously from many computers over a
network" (https://www.sqlite.org/whentouse.html; hard limit 281 TB per file,
https://www.sqlite.org/limits.html).

For this project that translates to:

| dimension | SQLite is fine while | you have outgrown it when |
|---|---|---|
| rows | tens of millions of papers and hundreds of millions of mentions/archive records, with the section 3 fixes | never for row count alone |
| size | the file fits the machine that runs the weekly job and can be moved around (tens of GB on a laptop or a VM with a persistent disk) | it must live in GitHub Actions' cache (10 GB) or be committed (100 MiB per file), see section 6 |
| write concurrency | one weekly job, or a few jobs serialised by WAL + `busy_timeout` | several pipelines write at the same time from different machines |
| read concurrency | any number of local readers (CLI, notebooks, DuckDB attached read-only) | many people query over a network, or a web app needs sub-second queries while the job writes |
| analytics | digest-style aggregates over a window | year-over-year trends across the whole corpus, joins with external tables, window functions over millions of rows every time a dashboard loads |

Practical thresholds for this schema (*estimates* from section 1): below ~5 GB everything works
on Actions with the cache; 5-50 GB needs a persistent disk or object storage for the file and
DuckDB/Parquet for analytics; beyond that, or as soon as more than one person needs live write
access, Postgres becomes the system of record.

## 5. Moving the analytics to DuckDB and Parquet

DuckDB reads the SQLite file directly, so the first step costs nothing:

```sql
-- in the duckdb shell or python: the sqlite extension autoloads on first use
ATTACH 'data/radar.sqlite' AS radar (TYPE sqlite, READ_ONLY);
SELECT substr(p.pub_date, 1, 7) AS month, m.entity_name, COUNT(DISTINCT m.paper_id) AS papers
FROM radar.mentions m JOIN radar.papers p ON p.id = m.paper_id
WHERE m.kind = 'instrument' GROUP BY 1, 2 ORDER BY 1, 3 DESC;
```

Verified live on 2026-10-17 against this repository's database with DuckDB 1.4.5: `ATTACH`
took 1.2 s (extension download included), every table including `papers_fts` was readable, and
`CREATE TABLE papers AS SELECT * FROM radar.papers` took 0.01 s for 3,562 rows. Type mapping
follows the declared SQLite types (INT→BIGINT, TEXT→VARCHAR, REAL→DOUBLE); a stored value that
contradicts its declared type raises a Mismatch Type Error unless `SET GLOBAL sqlite_all_varchar
= true` (https://duckdb.org/docs/current/core_extensions/sqlite.html).

`litradar export` is the second step, for when the SQLite file should not be touched by analysts
or when the data needs to leave the machine:

```bash
litradar export --parquet exports/2026-10-17          # one <table>.parquet per table (pyarrow; CSV fallback with a message)
litradar export --csv exports/2026-10-17 --gzip       # standard library only
litradar export --duckdb exports/radar.duckdb         # CREATE TABLE AS from the attached SQLite file (duckdb package)
litradar export --parquet out --tables papers,mentions,papers_fts   # include the FTS body text
```

Measured on the real database (138,946 rows across 8 tables):

| format | size | time | notes |
|---|---|---|---|
| SQLite source | 117.7 MB | — | includes FTS index |
| CSV (8 files) | 85.0 MB | 1.1 s | `papers_fts.csv` 37.6 MB, `archive_records.csv` 27.4 MB |
| Parquet, zstd (8 files) | 19.5 MB | 0.6 s | `archive_records.parquet` 0.63 MB for 123 K rows |
| DuckDB file (8 tables) | 83.6 MB | 2.4 s | 2.2 s of it is `papers_fts` copied row by row (virtual table) |

Design of the export:

* The source is opened read-only (`file:...?mode=ro`) and every table is read inside one `BEGIN`,
  so a running weekly job cannot change the snapshot halfway through.
* Columns declared INTEGER/REAL are written as int64/double only when `typeof()` confirms every
  stored value matches; otherwise they are written as text so nothing is dropped or coerced.
* `pyarrow` and `duckdb` are imported lazily. Without pyarrow, `--parquet` writes CSV and says
  so; without duckdb, `--duckdb` is skipped with a message and a non-zero exit.
* Parquet files are the long-term archive format: columnar, compressed, readable by DuckDB,
  pandas, Polars, Spark and R without the SQLite schema. Partition by year of `pub_date`
  (`papers/year=2026/part.parquet`) once a single file passes ~1 GB; DuckDB reads
  `read_parquet('exports/papers/*/*.parquet')` as one table.

A trend layer on top of the exports is then plain SQL in DuckDB (or a notebook): vendor share of
archive experiments by month, first-appearance curves per instrument, country mix from affiliation
strings, Chinese-affiliated share per subgroup. Keep those queries in `docs/` or a `queries/`
folder next to the exports; they do not need to be in the pipeline.

## 6. Postgres for many users

Move to Postgres when more than one process writes, or when people query over a network. The
`Store` surface is about 25 methods and the SQL is ordinary; the mapping is:

| SQLite | Postgres |
|---|---|
| `TEXT` ids, `*_json TEXT` | `text`, `jsonb` (GIN index on `authors_json` for author/affiliation lookups) |
| `INSERT ... ON CONFLICT(id) DO UPDATE` | identical syntax |
| `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` |
| `GROUP_CONCAT(DISTINCT x)` | `string_agg(DISTINCT x, ',')` |
| `substr(first_seen,1,10) >= ?` | `first_seen::date >= ?` or a generated `first_seen_day date` column |
| FTS5 `papers_fts`, `bm25()`, `snippet()` | `tsvector` column + GIN index, `ts_rank_cd()`, `ts_headline()`; `pg_trgm` for CJK substring search; or keep FTS in SQLite/DuckDB and use Postgres for the relational part |
| `PRAGMA journal_mode=WAL` | not needed |

Loading: `litradar export --parquet` then `COPY papers FROM '...csv'` (Postgres reads CSV
natively; for Parquet use DuckDB's postgres extension, `ATTACH 'dbname=radar' AS pg (TYPE postgres);
INSERT INTO pg.papers SELECT * FROM read_parquet('papers.parquet')`). Implement `PgStore` with the
same method names behind a `LITRADAR_DB=postgresql://...` setting and keep `Store` for laptops and
forks; the pipeline, digest and CLI do not know which one they talk to.

## 7. How the weekly job keeps working

Nothing in the weekly job changes until section 6. The job stays: restore the SQLite file → run →
render → save. The additions are a post-run export and a snapshot (section 8), both as extra
steps in `.github/workflows/weekly.yml`:

```yaml
      - name: Export analytics files
        run: |
          pip install pyarrow duckdb || true          # optional; CSV fallback is automatic
          python -m litradar export --parquet exports/latest --duckdb exports/radar.duckdb
      - name: Upload exports as a workflow artifact (90-day retention)
        uses: actions/upload-artifact@v4
        with:
          name: exports-${{ github.run_id }}
          path: exports/
```

Rules of thumb that keep the single-writer model safe:

* The job is the only writer. Analysts attach read-only (`ATTACH ... (TYPE sqlite, READ_ONLY)`)
  or use the exports. `litradar export` itself opens read-only.
* Backfills (`litradar run --since 2024-01-01 --until 2024-03-31`) are the same writer run at a
  different window; run them serially, never as parallel matrix jobs on one file.
* Add `PRAGMA busy_timeout = 30000` to `Store.__init__` once a second local process (dashboard,
  notebook) might hold a read transaction while the job commits (proposal; one line).
* `PRAGMA optimize` at the end of `Pipeline.run` (section 3.2).

## 8. Backups: cache, committed snapshot, or releases

The SQLite file is the corpus; losing it means re-fetching years of windows that some sources
(preprint servers, archive `first_public` dates) can rebuild and others (RSS feeds, news) cannot.
Three places to keep it, with the limits from GitHub's documentation:

| option | limits (GitHub docs) | good for | not good for |
|---|---|---|---|
| **Actions cache** (`actions/cache`, what `weekly.yml` does today) | 10 GB per repository by default; entries not accessed for 7 days are evicted; oldest-by-last-access evicted first when the repository hits its limit; runs cannot restore caches from sibling branches (https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/caching-dependencies-to-speed-up-workflows) | the hot copy between weekly runs; zero setup | the only copy: one missed week (schedules are skipped on inactive repositories) and the entry is gone; no history; 10 GB ceiling |
| **Committed compressed snapshot** (`data/radar.sqlite.zst` in git) | Git warns at 50 MiB and blocks files over 100 MiB; repositories should stay under 1 GB, 5 GB strongly recommended (https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github) | small forks (the real 117 MB file compresses to roughly 30-40 MB with zstd — *estimate*, text-heavy); point-in-time history for free | anything that grows: every weekly commit adds a full copy to history, and a 1 GB corpus is over the limit before compression |
| **Release assets** (`gh release create snapshot-2026-10-17 radar.sqlite.zst exports/*.parquet`) | each file under 2 GiB; up to 1,000 assets per release; no limit on total release size or bandwidth (https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases) | durable, versioned, downloadable without cloning, outside the repository size; Parquet exports alongside the file | files above 2 GiB need splitting (`split -b 1900m`) or must move to object storage |

Recommended policy:

1. Keep the Actions cache as the hot copy (no change).
2. After every successful run, upload `exports/` as a workflow artifact (90-day retention, free).
3. Monthly, or when the file changes by more than 10%, publish a release `snapshot-YYYY-MM-DD`
   with `radar.sqlite.zst` and the Parquet exports; keep the last 12 and prune older ones. The
   restore step in `weekly.yml` falls back to the newest snapshot release when the cache misses.
4. Do not commit the database. The `.gitignore` already excludes `*.sqlite`; add `*.duckdb`,
   `*.parquet` and `exports/`.
5. When the compressed snapshot passes ~1.5 GiB, move snapshots to object storage (S3, R2, GCS;
   DuckDB and `litradar export` both read and write there with a URL) and keep only the Parquet
   exports on releases.

## 9. Order of work

1. Done here: `idx_mentions_entity_id`; `scripts/bench_store.py`; `litradar export`.
2. Next change to `store.py` (one PR, with a rebuild migration): rowid-keyed FTS rows and no
   delete for new papers (3.1), window-first entity queries (3.2), `PRAGMA optimize` at the end of
   a run. Re-run the benchmark at N = 200 K before and after; the load curve should be flat.
3. FTS date tokens and the trigram table (3.3, 3.4) when windowed search and Chinese search are
   needed; both are rebuilds, so batch them with step 2 if possible.
4. Weekly workflow: export step, artifact upload, monthly snapshot release (7, 8).
5. Postgres only when a second writer or a shared query endpoint exists (6).
