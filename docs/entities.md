# Entity views: `litradar show` and `litradar watch`

Two commands for *managing* the corpus once it has grown past a single week: a dossier per
instrument, company or person, and a watchlist of free-text terms, entities and authors that is
evaluated against the store without editing any YAML. Both read only the SQLite database that
`litradar run` fills; there is no network access and nothing beyond the Python standard library.

Module: `litradar/entities.py`. Tests: `tests/test_entities.py` (temporary database, offline).

```bash
litradar show pacb-revio                      # instrument by gazetteer id
litradar show illumina --since 180d           # company by id
litradar show ILMN                            # company by ticker (parent wins over subsidiaries sharing it)
litradar show "NovaSeq X Plus"                # instrument by alias
litradar show "First Last" --json             # a person: tracked author or anyone in a stored author list
litradar watch add "methylation ctDNA"        # free-text term, matched with FTS5
litradar watch add Revio                      # resolves to an entity -> stored as pacb-revio
litradar watch add "First Last"               # resolves to a person -> stored as an author watch
litradar watch list
litradar watch run --since 7d                 # matches per watched term in the window
litradar watch remove "methylation ctDNA"
```

Every subcommand takes `--json` and prints plain data instead of text.

## How a query is resolved

In order of precedence; the first rule that matches wins and other hits are listed under `candidates`:

1. gazetteer **id** (`config/instruments.yaml`, `config/companies.yaml`), case-insensitive;
2. gazetteer canonical **name**;
3. gazetteer **alias** (a company alias beats an instrument alias when both match, e.g. a vendor
   name that is also an alias of that vendor's chemistry entry);
4. company **ticker** (`private` is never matched; when several entries share a ticker the one
   without a `parent` wins, e.g. the parent over an acquired brand);
5. an **entity id that exists only in `mentions`**, such as `unlisted:<slug>` company-type
   institutions recorded from OpenAlex, or ids of entries since removed from the YAML;
6. a **person**: a `tracked_authors` entry in `config/subgroups.yaml`, an `author_name` in
   `tracked_author_hits`, or any stored paper whose author list matches.

Person names are reduced to `(first, last)` after ASCII folding: `"Feng Zhang"`, `"Zhang, F."` and
`"Zhang, Feng"` agree; `"Fan Zhang"` does not (a spelled-out first name must match exactly, an
initial only has to agree with the first letter). Surname-only queries are refused as too ambiguous.
Diacritics are folded on both sides, but the pre-filter on the stored JSON uses the folded surname,
so `Müller` stored with the umlaut is found by `Muller` only when the stored form also folds to it.

Unknown queries exit with status 1 and print "did you mean" suggestions (substring matches over
ids, names, aliases and the mentions table).

## What `show` returns

`dossier()` returns one dictionary; `render_dossier()` turns it into terminal text. Fields:

| Field | Meaning |
|---|---|
| `query`, `kind`, `entity_id`, `name`, `matched_by`, `candidates` | the resolution (`kind` is `instrument`, `company`, `person` or `unknown`) |
| `identity` | gazetteer entry: vendor / ticker / category / launch year / status / aliases (instrument); ticker / parent / e-mail domains / aliases (company); subgroups, institution, OpenAlex id and the name forms seen (person) |
| `window` | `since` / `until` actually used (default `--since 365d`, `--until` today) |
| `papers_total` | distinct papers in the window that mention the entity (or list the person as an author) |
| `papers_per_month`, `sparkline` | zero-filled monthly series over the window and an ASCII sparkline (`_` = 0, then `.:-=+*#%@` scaled to the busiest month) |
| `trend` | papers in the last 30 days of the window vs the 30 days before, with `change_pct` (`null` when the prior period is empty) |
| `sections` | where the entity was found: `title`, `abstract`, `methods`, `funding`, `coi`, `ack`, `affiliations`, with paper and mention counts (entities only) |
| `recent_papers` | newest first: `pub_date`, `title`, `venue` (journal or source, `(preprint)` flagged), `url`, `sections`, `snippet` (for a person: the author string that matched) |
| `co_mentioned` | `instruments` and `companies` that appear in the same papers, ranked by papers then mention count; the entity itself is excluded |
| `archive` (instrument) | SRA/ENA experiments per month declaring that instrument, plus a per-model breakdown. `matched_by` is `entity_id`, or `model_name` when no archive row ever resolved to the id and the count comes from `instrument_model` strings containing the canonical name (a `note` says so) |
| `archive` (company) | the same, matched by `ticker` (or by `vendor` name for private companies) |
| `portfolio` (company) | that vendor's gazetteer instruments with paper and archive-experiment counts in the window (instruments whose `vendor`, `parent` or ticker points at the company) |
| `news` (company) | `news_items` tagged with the company id, newest first |
| `tracked_hits` (person) | `tracked_author_hits` rows with subgroup and `matched_by` (`openalex_id` or `name`), so name-collision hits are visible |

The window rule is the one the digest uses (`litradar/store.py`): a paper counts when `pub_date`
falls in `[since, until]`, or when it has no date and was first seen on/after `since`. Months are
taken from `pub_date`, falling back to `first_seen` for undated feed items.

Scratch space is a `TEMP` table, so `show` works on a read-only database file (SQLite permits temp
tables on read-only connections); the watchlist does not, and says so.

## The watchlist

One additive table, created with `CREATE TABLE IF NOT EXISTS` the first time it is touched:

```sql
CREATE TABLE IF NOT EXISTS watchlist (
  term TEXT NOT NULL COLLATE NOCASE PRIMARY KEY,   -- "methylation ctDNA", "pacb-revio", "First Last"
  kind TEXT NOT NULL DEFAULT 'term',               -- term | entity | author
  added_at TEXT NOT NULL                           -- UTC ISO-8601
);
```

`watch add` resolves the text unless `--kind` is given: a gazetteer hit becomes an `entity` watch
and is stored by id, a person becomes an `author` watch, anything else is a free-text `term`.
Adding an existing term (case-insensitively) is a no-op that reports `created: false`.

`watch run --since 7d [--until] [--limit 20]` evaluates every row:

* `term`: `Store.search()` over the FTS5 index (`papers_fts`: title, abstract, methods, funding,
  coi, affiliations, body; porter + unicode61). Several words are an implicit AND; invalid FTS
  syntax falls back to a phrase query inside the store, so `watch run` never aborts on one term.
  **Terms containing CJK characters bypass FTS**, because unicode61 does not segment CJK text, and
  are matched as exact substrings of title / abstract / methods / body (`instr()`); the snippet names
  the column that matched.
* `entity`: papers in the window whose `mentions` carry that id, with the sections and a snippet. An
  id that no longer resolves is searched as free text and flagged with a `note`.
* `author`: papers in the window whose author list matches the name (same rule as `show`), plus
  `tracked_author_hits` for that person.

Output: `{since, until, total_matches, terms: [{term, kind, added_at, matches, items: [...]}]}`;
each item has `id`, `pub_date`, `title`, `journal`/`source`, `kind`, `url`, `snippet`.

## Caveats

* Everything is read from the store; coverage is whatever the weekly runs have collected. On a
  database holding one week, every sparkline is a single bar and `trend.prior_30d` is 0.
* The archive `model_name` fallback exists because archives spell models their own way (`"Revio"`
  alone, which the gazetteer only accepts with vendor context). When it fires, the dossier says so;
  the lasting fix is an alias or `context_terms` change in `config/instruments.yaml` followed by a
  re-run (archive rows are matched at ingest).
* Author matching scans `papers.authors_json` with a `LIKE` pre-filter on the surname and then
  parses JSON in Python. That is instant at tens of thousands of papers; a dedicated authors table
  would be the next step for a multi-year corpus.
* Name-based `tracked_author_hits` can be collisions (common names). The dossier shows
  `matched_by` for each hit so they can be judged; OpenAlex ids in `config/subgroups.yaml` make
  matching exact.
* `watch run` is window-based, not "since last run": pick `--since` to match your cadence.
* No external API is used by this feature, so there was nothing to verify live against a remote
  service; the live test below runs against a read-only copy of a real database.

## Live test (read-only copy of a one-week database, 2026-10-17)

`show pacb-revio` (abridged):

```
Revio  [instrument pacb-revio]  matched by id
  vendor: Pacific Biosciences (PACB) · category: sequencer.long_read · launched 2022
  aliases: Revio system, PacBio Revio
  window: 2025-10-17 .. 2026-10-17 · 2 papers · found in: methods 2, abstract 1

Papers per month
  2025-10 .. 2026-10   ____________@   (max 2/month)
  trend: last 30 days 2 vs prior 30 days 0

Recent papers (2 of 2)
  2026-10-11  [abstract/methods]  The Genetic Landscape of Structural Variants in Aspergillus flavus using ONT and PacBio Sequencing
              biorxiv (preprint)  https://www.biorxiv.org/content/10.64898/2026.10.05.756768v1
  2026-10-11  [methods]  Genome assemblies and annotations of the poneroid ants ...
              biorxiv (preprint)  https://www.biorxiv.org/content/10.64898/2026.10.08.757030v1

Co-mentioned entities (same papers)
  instruments: PacBio HiFi (2), QIAseq (2), Sequel II (2), Beckman Coulter NGS reagents (2), Qubit (1), ...
  companies:   Pacific Biosciences (2), QIAGEN (2), Thermo Fisher Scientific (2), 10x Genomics (1), ...

Public sequencing data (SRA/ENA experiments, matched by model_name) · 99 experiments
  note: no archive record carries entity_id='pacb-revio'; counted instrument_model strings containing 'Revio' instead
  Revio                                   99 experiments     19 studies  [sra]
```

`show illumina --limit 5` (abridged):

```
Illumina  [company illumina]  matched by id
  ticker: ILMN · domains: illumina.com
  window: 2025-10-17 .. 2026-10-17 · 165 papers · found in: methods 160, abstract 6, ack 3, funding 2, coi 1, affiliations 1

Co-mentioned entities (same papers)
  instruments: QIAseq (55), NanoDrop (47), NovaSeq 6000 (46), Bioanalyzer (36), Qubit (32)
  companies:   Thermo Fisher Scientific (97), Agilent Technologies (60), QIAGEN (55), Merck KGaA / MilliporeSigma (42), Danaher (34)

This vendor's instruments in the window (papers / archive experiments)
  NovaSeq 6000                       46     7775   sequencer.short_read
  NovaSeq                            30        4   sequencer.short_read
  HiSeq                              25     2062   sequencer.short_read
  MiSeq                              22     7374   sequencer.short_read
  NovaSeq X                          15     1687   sequencer.short_read
  NextSeq 2000                       10     4519   sequencer.short_read
  ...

Public sequencing data (SRA/ENA experiments, matched by ticker) · 26003 experiments
  Illumina NovaSeq 6000                 7775 experiments    264 studies  [sra]
  Illumina MiSeq                        7374 experiments    118 studies  [sra]
  NextSeq 2000                          4519 experiments     68 studies  [sra]
  ...

News items tagged with this company (0)
  none in the window
```

(The news table in that copy was empty: the one-week run had not reached the feed stage.)

## Wiring

Nothing in `litradar/pipeline.py`, `litradar/digest/render.py` or `config/radar.yaml` has to change:
both commands are wired in `litradar/cli.py` and the watchlist table is created on first use.
Optional follow-ups, each a few lines:

* **Digest section.** In `render.render()` add, after the tracked-researchers block:
  `rep = entities.watch_run(store, since, until, limit=10)` and
  `md.append("## Watchlist\n"); md.append(entities.render_watch_report(rep))`
  (import `from .. import entities`). The section is empty until someone adds a term.
* **Seed from `radar.yaml`.** `watchlist_terms` in `config/radar.yaml` only lift scores; to also
  watch them, run once: `for t in cfg["watchlist_terms"]: entities.watch_add(store, t, kind="term")`
  (e.g. at the end of `Pipeline.run`), or type `litradar watch add "<term>"` per term.
* **Archive ingest.** Add `"Revio"` to `context_terms` handling or an archive-only alias in
  `config/instruments.yaml` so SRA's bare `Revio` resolves to `pacb-revio` at ingest; the dossier's
  `model_name` fallback stops firing once it does.
