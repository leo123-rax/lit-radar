# The store: schema notes, the `mentions` search column, write discipline and maintenance

Everything here is implemented in `litradar/store.py`. Search syntax and the HTTP API are documented in
docs/search.md; ranking and digest rules in docs/ranking.md.

## 1. Tables

| table | what |
|---|---|
| `papers` | one row per paper: metadata, abstract, `sections_json` (Methods, funding, COI, acknowledgements, data availability when stored), authors / affiliations / topics as JSON, `extra_json` |
| `papers_fts` | FTS5 index (`porter unicode61`), one row per paper keyed by `papers.rowid`; columns `id UNINDEXED, title, abstract, methods, funding, coi, affiliations, body, mentions` |
| `mentions` | gazetteer hits: `(paper_id, kind, entity_id, section)` with `entity_name, vendor, ticker, category, count, snippet, alias, role` (§7) |
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
  compressing or publishing a snapshot: `litradar maintenance --vacuum` (or `sqlite3 data/radar.sqlite 'VACUUM'`).
  It rewrites the whole file, needs free disk space of about the file's size and takes seconds to minutes on a
  100+ MB store, which is why the store never does it on its own.
* `litradar maintenance` (plugin module `litradar/commands/maintenance.py`) bundles the housekeeping:

  | flag | does | store method |
  |---|---|---|
  | `--stats-by-table` (default) | row counts and on-disk bytes per table and index (FTS shadow tables included), page/freelist totals, a warning when SRA placeholder rows exist | `Store.table_stats()` (bytes via the `dbstat` virtual table; `bytes_available: false` on builds without it) |
  | `--purge-sra-placeholders` | deletes `archive_records` rows whose accession is a synthetic `SRA-UID-...` (an older SRA parser wrote them when the experiment accession was missing; they carry no study, platform or strategy and cannot be de-duplicated against ENA) and reports found / deleted / remaining | `Store.purge_sra_placeholders()`, `Store.count_sra_placeholders()` |
  | `--refresh-entity-names` | rewrites `entity_name` and `ticker` (plus `vendor` and `category` for instruments; company rows keep the parent / provenance those columns hold) of every stored mention from the current gazetteers and refreshes the FTS `mentions` column of the papers touched. Ids are stable, display names are not: `rematch` re-derives most rows, but the rows it keeps because their text is not stored under the data policy would otherwise show the old name (after the 2026-10-17 renames a rematched copy still showed "Illumina sequencing" on kept rows) | `Store.refresh_entity_metadata(entries)` |
  | `--fts-rebuild` | drops and re-creates `papers_fts` from `papers` (bodies and the `mentions` column preserved) and merges the segments | `Store.rebuild_fts()` |
  | `--vacuum` | checkpoint, `VACUUM`, checkpoint; reports bytes before / after and the freelist it reclaimed | `Store.vacuum()` |

  Actions run in the order purge, refresh, rebuild, vacuum; the stats describe the store afterwards. `--json` prints
  the report as data. Measured on a copy of the live store (137,060,352 bytes; 3,562 papers, 123,164 archive records,
  freelist 4,290 pages): `--purge-sra-placeholders` found and removed 5,755 rows (4.7 % of the archive census, leaving
  117,409), `--fts-rebuild` wrote 3,562 rows, and `--vacuum` took the file from 137,060,352 to 114,896,896 bytes
  (22,163,456 bytes returned; freelist 4,480 pages before, 0 after).

## 5. Search ordering and fallback

`Store.search(query, since=None, limit=50, sections=(), fallback=True, sort="relevance", collapse=True)` puts
research papers before records that are not papers (letters, editorials, dataset / software / protocol deposits,
supplementary files, dissertations, peer-review reports; `litradar/worktype.py::sql_predicate` is the rule, each
row's `record_type` the reason, None for a paper), then orders by `sort`: `relevance` = `bm25(papers_fts,
FTS_BM25_WEIGHTS)` (title 4, abstract and mentions 2, body 0.5, the rest 1; lower `rank` is better), `score` =
the stored `scores.final` (unscored rows last), `date` = newest first. With `collapse` rows sharing a title fold
into the best-placed one (`duplicates` counts the rest; the query reads `limit x 5` rows to have enough to fold).
bm25 normalises by document length, which is why a 100-character deposit record would otherwise outrank a
full-text paper naming the same platform in its title; see docs/search.md.

`Store.search` retries a query that FTS5 rejects (unbalanced parentheses, an unknown column such as
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

## 7. Mention roles: used vs re-analysis vs mentioned

A Methods hit does not always mean the study ran the instrument: "GSE196239 (10x Genomics Chromium v3, platform
GPL24676)" is a downloaded dataset, and "who is using Chromium" should not count it. Every `mentions` row therefore
carries a `role`:

| role | rule (`store.mention_role(section, snippet)`) |
|---|---|
| `reanalysis` | the stored snippet (±90 characters around the hit) contains a re-use cue: `downloaded from`, `publicly available`, `public dataset(s)`, `GEO accession`, `Gene Expression Omnibus`, `re-analy...` / `reanaly...`, `previously published`, `previously generated/deposited/reported data`, `retrieved from`, `ArrayExpress`, `Sequence Read Archive` (case-insensitive), or an accession token `SRA`, `GSE\d+`, `GSM\d+`, `E-MTAB-\d+`, `PRJNA/PRJEB/PRJDB\d+`, `SRP/SRR/SRX/SRS\d+`, `ERP/ERR/ERX/ERS\d+` (case-sensitive) -- in any section |
| `used` | otherwise, a hit in `methods` (body hits are filed as methods by the extractor) |
| `mentioned` | otherwise: title, abstract, funding, COI, acknowledgements, affiliations |

The role is set by `Store.replace_mentions` (a `Mention.role` given by the caller wins; blank is derived), kept for
rows that survive under `keep_sections`, and back-filled once for stores written before the column existed
(`Store.__init__` -> `_ensure_mentions_role_column`, `Store.roles_backfilled` says how many rows it touched;
`Store.backfill_mention_roles(only_blank=False)` recomputes every row after a change to the cue list).

`Store.instrument_counts` reports, next to the unchanged `papers` total, `papers_used`, `papers_reanalysis` and
`papers_mentioned` (distinct papers whose mention of the entity has that role; a paper naming the entity in the
abstract and in the Methods appears in two of them). Nothing else changes: the digest, the dashboard and the API
keep their numbers until they choose to show the split. On a copy of the live store the back-fill derived a role
for all 4,423 stored mentions: 3,823 `used`, 561 `mentioned`, 39 `reanalysis` (20 instrument rows, 19 company
rows). Among the 39: the Chromium GEO case above, two Visium rows ("GSE203612 contributed ... a UCEC3 Visium
section", "43 10x Visium samples ... obtained from the GEO database (GSE210616)"), four Affymetrix array
re-analyses credited to `thermo-axiom`, three Cell Ranger runs over GSE matrices, and seven `10x-genomics` company
rows ("publicly available 10x Genomics Visium dataset GSE179572"). Two of the 39 are over-reach the rule accepts:
"downloaded from UniProt" next to a MaxQuant mention, and "retrieved from the AlphaFold database" (a model
database, not a dataset).

## 8. Family roll-ups

`Store.instrument_counts(..., families={entity_id: family_key})` and `distinct_entities_in_window(..., families=...)`
group sibling entries before the limit (the map is `Gazetteer.families`, built from the `family` key in
`config/instruments.yaml`; see docs/gazetteer.md §1 "Families"). A rolled-up row has `entity_id` = the family key,
`family` = the key, `members` = the member ids seen in the window, `entity_name` = `extract.gazetteer.family_label`
("NovaSeq X" for `novaseq-x`), and `papers` counts distinct papers across the members, so a paper naming both NovaSeq X
and NovaSeq X Plus counts once. Rows of entries without a family carry `family: ''` and `members: [entity_id]`, and
the default (`families=None`) is byte-for-byte the previous behaviour.
