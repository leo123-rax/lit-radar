# The store: schema notes, the `mentions` search column, write discipline and maintenance

Everything here is implemented in `litradar/store.py`. Search syntax and the HTTP API are documented in
docs/search.md; ranking and digest rules in docs/ranking.md.

## 1. Tables

| table | what |
|---|---|
| `papers` | one row per paper: metadata, abstract, `sections_json` (Methods, funding, COI, acknowledgements, data availability when stored), authors / affiliations / topics as JSON, `extra_json` |
| `papers_fts` | FTS5 index (`porter unicode61`), one row per paper keyed by `papers.rowid`; columns `id UNINDEXED, title, abstract, methods, funding, coi, affiliations, body, mentions` |
| `mentions` | gazetteer hits: `(paper_id, kind, entity_id, section)` with `entity_name, vendor, ticker, category, count, snippet, alias` |
| `scores`, `archive_records`, `news_items`, `tracked_author_hits`, `runs`, `timeseries` | as named |

`papers_fts` is a standalone FTS5 table (not an external-content table): the full-text body of open-licensed
papers lives **only** there, which is why every rebuild first saves the bodies aside.

## 2. The `mentions` full-text column

Gazetteer matching knows things the text does not say: a paper that wrote "Next GEM 5' kit" is attributed to
the Chromium platform, a facts-only paper keeps its Methods mentions after its text is dropped. Before this
column a full-text search for `Chromium` missed both. `papers_fts.mentions` holds, per paper, every distinct
**entity name**, **matched alias** (`mentions.alias`, filled by `extract.gazetteer.MatchedMention`) and
**entity id** of its stored mentions, ` ; `-joined (`store.mentions_text`), e.g.

    Chromium ; Next GEM ; txg-chromium ; NovaSeq X ; ilmn-novaseq-x

Consequences for search:

* `litradar search Xenium` finds every paper with a Xenium mention plus every paper whose text says Xenium;
  `search --in mentions Xenium` restricts to attributed papers; `search --in methods Xenium` restricts to papers
  whose stored Methods text says it (so a facts-only paper is found by the first two and not by the third, which
  is the data policy at work, not a bug).
* Ids tokenize on the hyphen: `"txg-chromium"` (quoted, hyphens are FTS5 syntax) or `txg` both work.
* Removing a mention (rematch with a changed gazetteer) removes it from search in the same transaction.

The column is maintained by `Store.replace_mentions` (which compares the new text with the stored one and
updates only on change) and by `Store.upsert_paper`, which preserves it when it rewrites a row. Stores written
before the column existed are migrated on open: `Store._ensure_fts_schema` logs a WARNING, re-creates the
virtual table with the current schema, re-inserts every row keyed by `papers.rowid` with its body and mentions
text, and sets `Store.fts_migrated`. On a ~140 MB store this took a few seconds and left the file the same size
(the old table's pages went to the freelist; see §4).

## 3. FTS write discipline

FTS5 appends to a segment b-tree on every row write; a weekly re-ingest that rewrote the FTS row of every
overlapping paper grew the file by ~16 MB per day without new content. Now:

* `upsert_paper` reads the stored FTS row and rewrites it only when one of the indexed text columns differs
  (`fts_rows_written` / `fts_rows_unchanged` count both outcomes). A metadata-only re-fetch of an unchanged
  paper touches `papers` but not `papers_fts`.
* `replace_mentions` updates the `mentions` column only when its text changed (`fts_mentions_updated`).
* `Store.fts_changes` is the sum; `Pipeline.run` and `Pipeline.rematch` call `Store.optimize()` when it reaches
  `fts_optimize_after` (default 50, `config/radar.yaml` top-level key) and report `fts.rows_rewritten`,
  `fts.rows_unchanged` and `fts.optimized` in the run counters.

Measured on a copy of a live store (3,562 papers): re-upserting 200 unchanged papers grew the file by
2,916,352 bytes before the change and by 0 bytes after it.

## 4. Maintenance: optimize, checkpoint, VACUUM

* `Store.optimize()` runs `INSERT INTO papers_fts(papers_fts) VALUES('optimize')` (merges all FTS5 segments
  into one) and `PRAGMA optimize`. Safe at any time; the pipeline runs it after busy runs.
* `Store.checkpoint()` commits and folds the WAL into the main file (`wal_checkpoint(TRUNCATE)`). It does
  **not** shrink the file.
* Pages freed by deletes, rewrites and the FTS rebuilds on open stay on the freelist (`PRAGMA freelist_count`)
  and are reused by later writes. To return them to the operating system run `VACUUM` yourself before
  compressing or publishing a snapshot: `sqlite3 data/radar.sqlite 'VACUUM'`. It rewrites the whole file, needs
  free disk space of about the file's size and takes seconds to minutes on a 100+ MB store, which is why the
  store never does it on its own.

## 5. Search fallback

`Store.search(query, ...)` retries a query that FTS5 rejects (unbalanced parentheses, an unknown column such as
`nosuchcol:Xenium`) as a quoted phrase, and records the FTS5 message in `Store.last_search_fallback` (None after
a search that needed none) so a CLI or API can tell the user the column name was wrong instead of showing an
empty result. `search(..., fallback=False)` raises the `sqlite3.OperationalError` instead, for callers that
want to answer HTTP 400.

## 6. Entity queries

* `Store.instrument_counts(since, until, limit, categories=None, platform_only=None)` and
  `Store.distinct_entities_in_window(..., categories=None, platform_only=None)` filter by gazetteer category
  prefix **in SQL, before the limit**; `platform_only=True` means `rank.heuristics.PLATFORM_CATEGORIES`,
  `False` means everything else.
* `Store.entity_papers(entity_id, since, limit, distinct=False)`: by default one row per (paper, section) with
  that section's snippet; `distinct=True` gives one row per paper with `sections` (list, in `SECTION_ORDER`),
  `section` (comma-joined), `mentions` (summed count) and the snippet of the section with most occurrences,
  and `limit` then counts papers.
* `Store.entity_names(kind=None)` maps entity ids to display names from the mentions table (used to label news
  groups by company name rather than id).
