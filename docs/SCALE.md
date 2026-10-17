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
Methods ~120 words, 30% with a 400-word body, 10% with Chinese titles and affiliations written
without spaces, as real ones are), 0-4 instrument and 0-2 company mentions per paper with a
heavy-tailed popularity, a score per paper, N archive records and N/20 news items spread over
three years, then times the shipped `Store` API and every query the digest and CLI run, and
finishes with a Chinese-search micro-benchmark (2.3). A store-mode run costs O(N²) because of the
finding in 2.1 (22 min at 50 K measured, *estimate* ~6 h at the default 200 K; the script prints
the estimate before loading); `--load-mode proposed` is the fast path.

```bash
python3 scripts/bench_store.py --n 50000                        # shipped Store.upsert_paper
python3 scripts/bench_store.py --n 50000 --load-mode proposed   # with the FTS fix from section 3.1
python3 scripts/bench_store.py --n 200000 --json bench.json --keep --db /tmp/bench.sqlite
```

### 2.1 Write path (N = 50,000, measured; two sequential runs on an idle laptop)

| metric | shipped `Store.upsert_paper` | proposed upsert (section 3.1) |
|---|---|---|
| load throughput, papers/s, first 10 K → last 10 K | **218 → 21** (falls with N) | 7,192 → 5,885 (near-flat) |
| load throughput, whole run | 38 papers/s (1,321 s for 50 K) | 6,478 papers/s (7.7 s for 50 K) |
| re-upsert 1,000 existing papers (update path) | 19.8 papers/s | 3,080 papers/s |
| FTS row lookup by `id` (UNINDEXED column), per lookup | **64 ms** at 50 K rows (3.0 ms at 3 K rows: linear) | 0.011 ms by rowid |
| archive records (`upsert_archive_records`, INSERT OR IGNORE) | 180,914 records/s | same |
| database size after load | 624 MB in total; 11.7 KB per synthetic paper in `papers` + FTS + their indexes | same |

**The finding.** `Store.upsert_paper` runs `DELETE FROM papers_fts WHERE id=?` on every paper, new
or not. `id` is an `UNINDEXED` FTS5 column, so that statement is a full scan of the FTS content
table (`EXPLAIN QUERY PLAN` → `SCAN papers_fts VIRTUAL TABLE INDEX 0:`), and its cost grows
linearly with the corpus: 3.0 ms at 3 K papers, 64 ms at 50 K, and (*estimate*) ~1.5 s at
1 M papers. A weekly run of 5 K papers against a 1 M-paper corpus would spend about two hours in
that one statement. This is the single change that matters for the write path.

### 2.2 Read path (N = 50,000 papers, 60 K instrument mentions, three-year spread; median ms)

Provenance: this table is the **shipped-store run** (`--load-mode store`, the same run as the left
column of 2.1), with the proposed-mode run made immediately afterwards on an otherwise idle
machine. The two runs hold identical relational tables (same seed; only the FTS rowids of the
1,000 re-upserted papers differ), and their as-stored read-path medians agree to within
11% (1% median), which is the noise band for this table. An earlier
version of this note used two overlapping runs, one of them with an older script that measured
"+indexes" *before* ANALYZE; that made `idx_mentions_kind_paper` look like a 2x regression of
`instrument_counts`, which was the missing statistics, not the index.

"As stored" is the schema in this change (it already contains `idx_mentions_entity_id`, see
below); "+ANALYZE" adds only planner statistics; "+indexes" adds the remaining candidate indexes
(`idx_mentions_kind_paper`, `idx_archive_public_model`) and re-runs ANALYZE. `PROPOSED_*` rows
are the query rewrites in section 3.2, run on the same data and checked to return the same
entities and counts as the shipped queries. Medians of 5 runs, milliseconds.

| query | rows | as stored | +ANALYZE | +indexes |
|---|---:|---:|---:|---:|
| papers_in_window, 7-day window | 2749 | 28.29 | 28.5 | 28.79 |
| papers_in_window, 365-day window | 18160 | 192.95 | 189.85 | 192.44 |
| top_papers, 7 days | 25 | 28.87 | 28.57 | 29.53 |
| instrument_counts, 7 days | 60 | 288.24 | 149.28 | 152.58 |
| instrument_counts, 30 days | 60 | 282.73 | 148.54 | 158.32 |
| instrument_counts, 365 days | 60 | 287.08 | 189.67 | 201.15 |
| PROPOSED instrument_counts, 7 days (3.2) | 60 | 9.78 | 10.16 | 9.79 |
| PROPOSED instrument_counts, 365 days (3.2) | 60 | 67.56 | 69.87 | 71.64 |
| company_counts, 7 days | 60 | 150.97 | 84.27 | 87.47 |
| company_counts, 365 days | 60 | 155.17 | 120.44 | 124.83 |
| PROPOSED company_counts, 7 days (3.2) | 60 | 7.84 | 7.91 | 7.74 |
| new_entities (instrument), 7 days | 0 | 160.21 | 162.28 | 171.8 |
| PROPOSED new_entities, 7 days (3.2) | 0 | 6.8 | 6.8 | 7.03 |
| archive_instrument_counts, 7 days | 33 | 0.24 | 0.23 | 0.24 |
| archive_instrument_counts, 365 days (16 K rows) | 58 | 10.07 | 10.01 | 10.09 |
| archive_strategy_counts, 7 days | 10 | 0.12 | 0.13 | 0.12 |
| entity_papers, most popular entity | 100 | 7.26 | 7.06 | 7.43 |
| entity_papers, rare entity | 21 | 0.07 | 0.06 | 0.07 |
| entity_papers, popular entity, since 90d | 100 | 5.46 | 5.38 | 5.68 |
| search, word in most documents | 50 | 204.37 | 204.13 | 209.85 |
| search, same word, --since 90d | 50 | 191.36 | 192.78 | 197.0 |
| search, word in no document | 0 | 0.04 | 0.04 | 0.04 |
| search, phrase "single cell" | 50 | 32.81 | 33.34 | 33.72 |
| search, --in methods | 50 | 160.62 | 164.52 | 166.7 |
| search, Chinese 3-character substring (unicode61, see 2.3) | 0 | 0.04 | 0.04 | 0.04 |
| news_in_window, 7 days | 200 | 1.04 | 1.04 | 1.07 |
| counts (6 COUNT(*)) | 6 | 0.11 | 0.13 | 0.12 |

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
  to score every hit before `LIMIT`: a word present in most documents costs 204 ms at 50 K
  papers, a word in no document 0.04 ms, a phrase 33 ms. The `--since` filter does not help because it is
  applied after the FTS scan. Section 3.3 has the fix (a date token column inside the FTS table).
* **Archive queries are cheap** (index range scan + GROUP BY): 0.24 ms for a 7-day window at this
  density; the 365-day window (16 K rows, 10 ms) is the closer proxy for one real week (123 K rows,
  *estimate* ~90 ms).
* The 7-day window holds 2,797 rows, not ~320, because the 5% of synthetic papers without a
  publication date count as "first seen this week", exactly as undated feed items do in the real
  store; `papers_in_window` cost is row materialisation (every column, JSON included) at ~13 µs
  per row.
* The other two candidate indexes did not pay for themselves: with statistics present the planner
  does pick `idx_mentions_kind_paper (kind=? AND paper_id=?)` for the mention lookup once it exists,
  but the time of `instrument_counts` / `company_counts` is unchanged (149 vs 153 ms; compare the
  +ANALYZE and +indexes columns) because the primary key already served the same papers-first
  plan; `idx_archive_public_model` turns the archive scan into a covering scan with no measurable
  gain. Neither is added.

### 2.3 Unicode and Chinese text

The FTS table uses `tokenize = 'porter unicode61'`. unicode61 treats every character in the
Unicode letter and number categories as a token character and splits only on spaces and
punctuation (https://www.sqlite.org/fts5.html, 4.3.1). Chinese is written without spaces between
words, so a whole title such as 空间转录组学方法在肿瘤研究中的应用 is indexed as **one token**, and
`litradar search 转录组` finds nothing: FTS5 matches whole tokens, not substrings. The bench's
`search_cjk_unicode61` row above is this case (0 rows), and
`tests/test_export_and_bench.py::test_chinese_title_is_one_unicode61_token` pins it offline.

The bench then measures the two ways out proposed in 3.4 on the same database (`cjk_search` in the
JSON report; N = 50 K, 4,894 Chinese titles, the 3-character term 基因组 and the
2-character term 图谱 both taken from a stored title):

| approach | 3-character term | 2-character term | cost |
|---|---|---|---|
| shipped `papers_fts` (porter unicode61) | **0 hits, 0.03 ms** | 0 hits, 0.03 ms | — |
| `LIKE '%term%'` on `papers.title` (no index) | 1412 hits, 26.89 ms | works | full scan of papers per query |
| trigram FTS table over the CJK papers' title + affiliations (3.4) | 1412 hits, 1.17 ms | **0 hits, 0.0 ms** (FTS5: under 3 characters matches nothing) | 2.7 MB, built in 0.023 s |
| pre-segmented shadow column (space-separated words, unicode61) | 1412 hits, 1.06 ms | 1349 hits, 1.02 ms | needs a segmenter; built in 0.025 s; finds only words the segmenter knows |

The real database has no Chinese titles yet (its sources are English-language indexes; checked
on the 2026-10-17 snapshot), so the table is synthetic. The tokenizer behaviour is a property of
SQLite, verified on 3.51.0 with the test above.

## 3. Changes inside SQLite (proposals, measured where possible)

Only one item below is applied in this change (the index in 3.5); the rest are proposals that
touch `litradar/store.py` queries and belong in a dedicated change with a migration.

### 3.1 Key the FTS row by rowid

`papers` is a rowid table, so every paper has a stable integer rowid as long as the table is never
`VACUUM`ed into new rowids (it is not; and `INTEGER PRIMARY KEY` would make it explicit). Write the
FTS row with that rowid and delete by it:

```sql
-- in upsert_paper, after the INSERT ... ON CONFLICT on papers:
DELETE FROM papers_fts WHERE rowid = (SELECT rowid FROM papers WHERE id = ?);   -- a rowid lookup; no-op for a new paper
INSERT INTO papers_fts (rowid, id, title, abstract, methods, funding, coi, affiliations, body)
  SELECT rowid, ?, ?, ?, ?, ?, ?, ?, ? FROM papers WHERE id = ?;
```

Measured: lookup by rowid 0.011 ms vs 64 ms by `id` at 50 K papers; load throughput flat at
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

### 3.4 Chinese, Japanese and Korean text: a trigram table plus segmentation

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts_cjk USING fts5(id UNINDEXED, title, abstract, affiliations, tokenize = 'trigram');
```

populated alongside `papers_fts` for papers whose text contains CJK code points (or for all
papers, at several times the size of the text it covers, see below). `search()` then routes a query that
contains CJK characters to the trigram table. SQLite 3.34+ is required (Python 3.9 wheels on
GitHub's ubuntu runners ship 3.37+; this machine has 3.51); the bench records
`trigram_available` and skips the measurement on older builds. Two limits, both measured in 2.3:

* **Trigram matches only queries of 3 or more unicode characters.** FTS5: "Substrings consisting
  of fewer than 3 unicode characters do not match any rows when used with a full-text query"
  (https://www.sqlite.org/fts5.html, 4.3.4). Many Chinese terms are two characters (测序
  sequencing, 肿瘤 tumour, 免疫 immune, 空间 spatial) and return nothing from a trigram table.
  Options, in order of effort: pad the query with context the user already has (空间转录 instead
  of 空间); or add a **pre-segmented shadow column**: run a dictionary segmenter over
  title/abstract at write time (jieba-style, or the gazetteer's own Chinese aliases as the
  dictionary), store the words space-separated, and index that column with unicode61, which then
  matches words of any length but only the words the segmenter knows (the bench's `segmented_*`
  rows use a greedy longest-match over its vocabulary). FTS5 has no word-segmenting tokenizer of
  its own: its four built-in tokenizers are unicode61, ascii, porter and trigram, and a custom
  tokenizer needs the C API, not the `sqlite3` module (FTS3/4's `icu` tokenizer exists only in
  `SQLITE_ENABLE_ICU` builds; this machine's SQLite is not one).
* **Size.** The trigram table over title + affiliations of the Chinese papers measured 2.7 MB for
  4,894 rows (559 bytes per row, for roughly 100-180 bytes of UTF-8 text per row — *estimate* from
  the synthetic shape — so about 3-5x the text), built in 0.023 s; restrict the table to CJK
  papers rather than indexing everything three characters at a time.

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

Measured on the real database (138,354 rows across 8 tables; a read-only snapshot taken with the
SQLite backup API on 2026-10-17, which is a WAL-mode file without side files, so the export opened
it with `immutable=1` and said so):

| format | size | time | notes |
|---|---|---|---|
| SQLite source | 117.7 MB | — | includes FTS index |
| CSV (8 files) | 83.9 MB | 0.9 s | `papers_fts.csv` 37.4 MB, `archive_records.csv` 27.5 MB, `papers.csv` 17.2 MB |
| Parquet, zstd (8 files) | 19.1 MB | 0.5 s | `archive_records.parquet` 0.63 MB for 123 K rows, `papers_fts.parquet` 13.3 MB |
| DuckDB file (8 tables) | 83.4 MB | 1.9 s | 1.7 s of it is `papers_fts` copied row by row (virtual table) |

Design of the export:

* The source is opened read-only (`file:...?mode=ro`, or `immutable=1` for a bare copy of a WAL
  database that has no -wal/-shm files) and every table is read inside one `BEGIN`, so a running
  weekly job cannot change the snapshot halfway through. This Python path creates no side files.
  The DuckDB path is different: `ATTACH ... (TYPE sqlite, READ_ONLY)` (`READ_ONLY` is a general
  ATTACH option, https://duckdb.org/docs/current/sql/statements/attach.html) is an ordinary SQLite
  reader and may create `-wal`/`-shm` side files next to the source — observed with DuckDB 1.4.5 on
  a read-only copy (`-shm` 32 KB, `-wal` 0 bytes); the file contents are never modified.
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

* The job is the only writer. Analysts attach read-only (`ATTACH ... (TYPE sqlite, READ_ONLY)`;
  this may leave `-wal`/`-shm` side files next to the database, normal for any SQLite reader) or
  use the exports. `litradar export` itself opens read-only.
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
2. Next change to `store.py` (one PR, with a rebuild migration): rowid-keyed FTS rows (3.1),
   window-first entity queries (3.2), `PRAGMA optimize` at the end of a run. Re-run the benchmark at N = 200 K before and after; the load curve should be flat.
3. FTS date tokens and the trigram table plus a pre-segmented column (3.3, 3.4) when windowed
   search and Chinese search are needed; both are rebuilds, so batch them with step 2 if possible.
4. Weekly workflow: export step, artifact upload, monthly snapshot release (7, 8).
5. Postgres only when a second writer or a shared query endpoint exists (6).
