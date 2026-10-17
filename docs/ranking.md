# Ranking, storage rules and digest conventions

How a fetched record becomes a stored paper, a score and a digest line. Everything here is
implemented in `litradar/pipeline.py`, `litradar/store.py`, `litradar/rank/heuristics.py` and
`litradar/digest/render.py`; the data policy itself lives in `litradar/policy.py`.

## 1. What is stored from full text (data policy)

Before any full text is fetched the pipeline decides a *storage level* with
`policy.fulltext_storage_level(license, open_access, text_available)`:

| level | when | what the store keeps |
|---|---|---|
| `full` | CC-BY family, CC0, public domain, or the Europe PMC open-access subset (`isOpenAccess == 'Y'`) | sections (Methods, funding, COI, acknowledgements, data availability) and the body (FTS only) |
| `facts_only` | restrictive or unknown licence (bioRxiv `cc_no`, author manuscripts, blank licence) | nothing parsed from the text: only the gazetteer mentions with their short snippets; `funding` is rebuilt from grant *metadata* when present |
| `none` | no text to fetch | nothing |

* bioRxiv / medRxiv: the licence is the record's `license` field. Europe PMC: `extra['is_open_access']`.
* The level is written to `papers.extra_json.fulltext_policy`. `Store.upsert_paper` enforces it again
  (a non-`full` level strips sections and body whatever the caller passes), so the rule holds even for
  code paths that bypass the pipeline.
* Papers cached before the policy existed are cleaned by `litradar rematch`: each paper is re-extracted
  on its stored text first, then `Store.purge_paper_fulltext` strips the text. `Store.purge_restricted_fulltext`
  does the bulk version without re-extraction.
* Mentions derived from text that is not kept survive later re-ingests and rematches through
  `Store.replace_mentions(..., keep_sections=FULLTEXT_SECTIONS)`: stored mentions for a section are retained
  when the new extraction has none for that section.

## 2. Re-ingest safety

The same paper arrives from several sources and in several weeks (preprint, then journal version;
metadata-only feeds after a full-text fetch). `Pipeline.run` folds the stored version into the fresh
record **before** full text and extraction (`Pipeline._merge_stored`), so a metadata-only re-fetch never
wipes Methods mentions and already-parsed papers are not fetched again.

`Store.upsert_paper` rules on update:

* `kind` never flips from `article` back to `preprint`.
* `pub_date` keeps the earliest non-empty date.
* `extra_json` is the union of stored and new keys (new non-empty values win; `sources` lists are unioned).
* abstract, sections, authors, affiliations and the body are never overwritten with empty values.
* `cited_by_count` keeps the maximum; `title` keeps the longer string.

## 3. Full-text search keying

`papers_fts` rows carry an explicit `rowid` equal to `papers.rowid`; every delete, lookup and join uses
the integer key. The previous layout keyed lookups on the `UNINDEXED id` column, which is a full scan
of the FTS table per paper (quadratic over a rematch). On open, `Store` checks once whether the FTS
rowids line up with `papers.rowid`; a store written by an older version is rebuilt in place (bodies
preserved) and the rebuild is logged.

`Store.iter_papers` streams rows in rowid order with keyset pagination, so `rematch` never holds more
than one batch of papers (and their bodies) in memory; `Pipeline.rank` accepts any iterable and scores
papers as they stream past while still assembling the LLM shortlist.

## 4. Heuristic score

Unchanged in substance (see the module docstring of `rank/heuristics.py`): journal tier, platforms and
lab tools named, companies named, company-affiliated author, watchlist terms, tracked author, novelty
cues, early citations, scaled to 0-10 and optionally blended with the LLM composite. Semantic Scholar
citation counts are applied to the in-memory papers before ranking when `sources.semantic_scholar` is
enabled, so the "already cited" signal reflects the latest counts.

## 5. Preprint to publication

When a preprint and its journal version are both in the digest window the digest shows **only the
journal version**, marked `(also as preprint <date>)`, and gives it the better of the two scores. The
link is made by the preprint's `published_doi` or, failing that, by an identical normalised title
(25+ characters). `Store.preprint_conversions(since, until)` lists the pairs; the digest prints them under
"Preprints that appeared as journal articles this window".

## 6. Companies by type

`render(..., company_types={id: type})` splits the Companies section using the `type` field of
`config/companies.yaml` (the caller passes the mapping; `Pipeline.company_types` builds it):

* main table: `tools`, `diagnostics`, `synthesis`, `ai_bio` and any unknown type;
* compact table: `pharma`, `cro`;
* one line: `institute`;
* one line "AI tools and software used": `software`, `ai_tool`.

Roles (author / funding-COI / subject / supplier) are kept everywhere.

## 7. Escaping

Every untrusted string (titles, abstracts, snippets, LLM `one_line` / `why_it_matters` and flag values,
news headlines and feed names, company and author names, archive instrument strings, source error
messages) passes through `render.esc`, which escapes `& < > | [ ]`, collapses whitespace and neutralises
a leading `# - + * >`. Links are emitted only for `http(s)` URLs (`render.safe_url`).

## 8. Archive census labels

`Store.archive_instrument_counts` never returns a blank vendor: models the gazetteer cannot map are
labelled from the declared platform string, e.g. `Illumina (model unspecified)` for `ILLUMINA` /
`unspecified`, `Illumina (unmapped model)` for an unknown Illumina model string. Rows carry `archive`
and `vendor_mapped`, and the query accepts an `archives` filter so the digest can show sequencing archives
(SRA, ENA, GEO), proteomics datasets (PRIDE) and single-cell / spatial assays (CELLxGENE) separately.

## 9. Tracked authors

Hits store `matched_by` (`openalex_id` or `name`). Name-only matches are rendered with the existing
caveat and capped per run at `tracked_authors.name_match_cap` hits per tracked name (default 5); the
overflow is counted (`tracked_hits.name_capped`), logged and reported in the Coverage section.

## 10. Run end

`Pipeline.run` folds the WAL into the main database file in a `finally` block (`Store.checkpoint`) so
the `.sqlite-wal` sidecar is empty on disk even when a stage fails; the store stays open because the
caller renders from it afterwards and should close it (`Store.close` is idempotent and also works as a
context manager).
