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
  `Store.replace_mentions(..., keep_sections=...)`: stored mentions for a section are retained when the new
  extraction has none for that section. `rematch` keeps every section it cannot re-derive from stored text
  (`Pipeline.underivable_sections`: Methods when neither Methods nor a body is stored, funding / COI /
  acknowledgements / data availability when that section is absent), whatever the policy level, so a paper whose
  text was never stored does not lose its company mentions on the next `litradar rematch`. Title, abstract and
  affiliations are always stored and therefore always re-derived (a stale mention there is dropped).
* The derived facts stay searchable even when the text is not: the `mentions` full-text column (docs/store.md)
  holds the entity names, matched aliases and ids of every stored mention, so `litradar search Revio` finds a
  facts-only paper whose Methods named a Revio, while `search --in methods Revio` (the text column) does not.

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

See the module docstring of `rank/heuristics.py`: journal tier, platforms and lab tools named, companies
named, company-affiliated author, watchlist terms, tracked author, novelty cues, early citations, scaled
to 0-10 and optionally blended with the LLM composite. Semantic Scholar citation counts are applied to the
in-memory papers before ranking when `sources.semantic_scholar` is enabled, so the "already cited" signal
reflects the latest counts.

Company mentions are scored by the entry's `type` in `config/companies.yaml` (`Pipeline.rank` passes
`Pipeline.company_types`; `heuristics.company_role` decides):

| type | role | effect |
|---|---|---|
| tools, diagnostics, synthesis, ai_bio, pharma, cro, unknown | `company` | counted in "N company mention(s)" and eligible for the company-author bonus |
| institute, foundation, non_profit | `institute` | listed as "N institute mention(s), not scored"; an institute affiliation is not a company-affiliated author |
| software, ai_tool, llm | `ai_tool` | listed as "N AI tool(s), not scored" (language-polishing and structure-prediction credits are not commercial signals) |

The top list applies the same split: its per-paper line reads `Companies: ...`, `Institutes / funders: ...`
and `AI tools: ...`, and an entity that appears only in funding, competing-interest or acknowledgement text
carries a role tag, e.g. `Profluent Bio (COI)`, so a disclosed equity stake never reads like supplier usage.

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
messages) passes through `render.esc`, which escapes `< > | [ ]`, collapses whitespace and neutralises
a leading `# - + * >`. A bare `&` is left as is: Markdown renders it correctly and `&amp;` would leak into
plain-text readers (e-mail, chat); because `<` is always escaped, an entity sequence in the input can only ever
render as visible text. Links are emitted only for `http(s)` URLs (`render.safe_url`).

## 8. Archive census labels

`Store.archive_instrument_counts` never returns a blank vendor: models the gazetteer cannot map are
labelled from the declared platform string, e.g. `Illumina (model unspecified)` for `ILLUMINA` /
`unspecified`, `Illumina (unmapped model)` for an unknown Illumina model string. Rows carry `archive`
and `vendor_mapped`, and the query accepts an `archives` filter so the digest can show sequencing archives
(SRA, ENA, GEO), proteomics datasets (PRIDE) and single-cell / spatial assays (CELLxGENE) separately.

## 9. Tracked authors

An OpenAlex author id on the record is an exact match (`matched_by = openalex_id`). Without one the match
is by name, and a name match requires the **full given name and the surname** (`pipeline._norm_name`:
`First M. Last`, `First Last` and `Last, First M.` all reduce to `first last`) or the **exact display name**
as configured. A surname plus an initial (`Zhang F`, `Anderson, D. J.`, `F. Zhang`) never matches: on a common
name that is not evidence of identity. When the tracked person has an id configured and the record carries
author ids, a different id means a different person and the name match is dropped
(`tracked_hits.name_rejected_other_id`).

Hits store `matched_by`: `openalex_id`; `name` (no id configured: the digest says "add the OpenAlex id to
make it exact"); `name_no_ids` (an id is configured but the record carried no author ids, so it could not be
checked, and the digest says so instead of asking for an id that already exists). Name matches of both kinds
are capped per run at `tracked_authors.name_match_cap` hits per tracked name (default 5); the overflow is
counted (`tracked_hits.name_capped`), logged and reported in the Coverage section.

## 10. Run end

`Pipeline.run` folds the WAL into the main database file in a `finally` block (`Store.checkpoint`) so
the `.sqlite-wal` sidecar is empty on disk even when a stage fails; the store stays open because the
caller renders from it afterwards and should close it (`Store.close` is idempotent and also works as a
context manager). Before that, `run` (and `rematch`) merge the FTS5 segments when the run rewrote at least
`fts_optimize_after` full-text rows (default 50; `radar.yaml` top-level key), see docs/store.md. The run
report carries `started`, `finished`, `since`, `until`, `counts`, `errors` and `http` (client counters plus
`blocked_hosts` and `failures_by_host`).

## 11. Which failures reach the error list

Every source adapter's exception lands in `errors` as `<source>: <message>`; the first word before the colon
is the source id that `litradar status` and the coverage section report and that the suggested re-run command
uses (`litradar run --sources <id> -v`), so messages are prefixed with that id (`sec: SEC 8-K feeds skipped (...)`).
In addition, at the end of a run (`Pipeline._fold_http_errors`):

* a host the HTTP client skipped after repeated 429s is reported as
  `<source>: host <host> rate-limited (HTTP 429 N times in a row); skipped for the rest of the run, K request(s) not sent`;
* a host that was asked and never answered successfully is reported as `<source>: all N request(s) to <host> failed`;
* OpenAlex queries report per group (`topics`, `company_authors`, `tracked_authors`, `journals`):
  `openalex: <group>: F of T queries failed (<API message>)`, with a WARNING per failed query carrying the API's
  message (the client also logs the first HTTP 400 body per host at WARNING);
* Semantic Scholar enrichment that failed every batch without matching a DOI is an error, not a counter.

Hosts map to source ids through `pipeline.HOST_SOURCES`; a source that already reported its own error (gdelt,
OpenAlex) is not listed twice.

## 12. Coverage and method block

The digest's "Coverage and method" section separates two kinds of numbers that used to be mixed:

* **This window, counted in the database**: papers in the digest window and how many have parsed full
  text, by source. These are the numbers the headline refers to.
* **Last pipeline run**: the counters of one pipeline process (records pulled per source before
  de-duplication, unique / new / merged papers, full text parsed, archive records, signals, citation
  enrichment, skipped hosts, errors), labelled with the run's finish time and its own window. When the last
  recorded run for exactly this window exists it is used; otherwise the most recent run of all, and the block
  says in so many words that its window is not the digest's. A digest for a database without runs says so.

Under the title a line records when the digest was rendered: every count is a snapshot of the database at
that moment, which is why a committed digest and a later copy of the database can disagree.

## 13. Top-list summaries

An entry shows the LLM one-liner when there is one, else the abstract (cut at ~420 characters). When the
record has no abstract the pipeline first tries to recover it from the full-text XML it already fetched
(`extract.jats.parse_jats` returns `abstract`; `Pipeline._fill_abstract_from_jats` uses the client's disk cache,
so this costs no request; counted as `fulltext.abstract_from_jats` and recorded in `extra_json.abstract_source`).
If the stored record still has none, the renderer falls back to the opening of the stored body ("(from full
text; no abstract on record)") or to the first sentence of the stored Methods; facts-only papers without any
stored text get no summary line.

## 14. Platform and lab-tool tables

`Store.instrument_counts(..., platform_only=True / False)` filters by gazetteer category in SQL before the row
limit, so the "Platforms named in this week's papers" table and the "Lab infrastructure and consumables" table
have separate budgets (`digest.platforms_table_n`, default `instruments_table_n` = 40, and
`digest.lab_tools_table_n`, default 25). A cut table ends with "… and N more ... named in the window". The
summary JSON carries `platforms`, `lab_tools`, `platforms_total`, `lab_tools_total` and the combined
`instruments` list.
