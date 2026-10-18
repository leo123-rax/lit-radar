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
litradar show "Krokidis M"                    # PubMed / Europe PMC "Surname I" form works too, as does "M. Krokidis"
litradar show 胡文欣                           # a CJK name, matched exactly (also against a gloss like "Wenxin Hu (胡文欣)")
litradar show illumina --since 90d --until 30d   # --since/--until take YYYY-MM-DD or Nd (N days before today)
litradar watch add "methylation ctDNA"        # free-text term, matched with FTS5
litradar watch add Revio                      # resolves to an entity -> stored as pacb-revio
litradar watch add "First Last"               # resolves to a person -> stored as an author watch
litradar watch list
litradar watch run --since 7d                 # matches per watched term in the window
litradar watch remove "methylation ctDNA"
```

Every subcommand takes `--json` and prints plain data instead of text. Bad input is a one-line message,
never a traceback: an invalid `--since`/`--until` (or `--since` after `--until`) exits 2, an empty watch
term or a read-only database on `watch add|remove` exits 1.

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

### Person-name forms

Sources store author names differently, so `name_key()` accepts all of these and reduces each to
`(first, last)` after ASCII folding:

| Stored or typed form | Example | Key |
|---|---|---|
| Western `First Last`, with or without middle names | `Evan E. Eichler` | `("evan", "eichler")` |
| Western `Last, First` / `Last, F.` (bioRxiv, Crossref) | `Zhang, F.` | `("f", "zhang")` |
| PubMed / Europe PMC `Surname I` / `Surname IJ` (no comma; 99% of Europe PMC author strings) | `Krokidis M`, `Zhang FJ`, `van der Berg J` | `("m", "krokidis")`, `("f", "zhang")`, `("j", "berg")` |
| Leading initials | `F.J. Zhang`, `FJ Zhang` | `("f", "zhang")` |
| All-caps surname with initials | `WANG J` | `("j", "wang")` |
| Latin name with a CJK gloss | `Wenxin Hu (胡文欣)` | `("wenxin", "hu")`; the gloss `胡文欣` is matched separately |
| No Latin letters (CJK, Cyrillic, ...) | `张锋`, `张 锋`, `Иванов, А.` | `("*", "张锋")`, `("*", "ИвановА")`: exact match only |

Matching: same surname and a compatible first name (`"Feng Zhang"`, `"Zhang, F."`, `"Zhang F"` and
`"Zhang, Feng"` agree; `"Fan Zhang"` does not, because a spelled-out first name must match exactly
while an initial only has to agree with the first letter). Two *spelled-out* names also match with the
order swapped, so `"Zhang Feng"` finds `"Feng Zhang"` and vice versa, because romanised East Asian
names appear in both orders across sources. This is deliberately ambiguous (a stored `"Feng, Zhang"`
would also match) and initials never swap: `"Zhang Feng"` does **not** match Europe PMC's `"Zhang F"`;
type the Western order or the stored form for those. A CJK query matches a stored CJK-only name or the
CJK gloss of a Latin name exactly (no substring matching: `胡文` does not find `胡文欣`). Surname-only
Latin queries are refused as too ambiguous.

Diacritics are folded on both sides, but the `LIKE` pre-filter on the stored JSON uses the folded
surname, so `Müller` stored with the umlaut is found by `Muller` only when the stored form also folds
to it. For non-Latin keys the pre-filter is the first character of the name (SQLite `LIKE` is exact on
non-ASCII text), then the exact rule is applied in Python.

The pipeline's own tracked-author matcher (`_norm_name` in `litradar/pipeline.py`) still assumes
Western order, so name-based `tracked_author_hits` miss Europe PMC rows at ingest; the dossier does not
depend on it (it scans author lists directly). Switching that helper to `entities.name_key` is a
one-line follow-up outside this module.

Unknown queries exit with status 1 and print "did you mean" suggestions (substring matches over
ids, names, aliases and the mentions table).

## What `show` returns

`dossier()` returns one dictionary; `render_dossier()` turns it into terminal text. Fields:

| Field | Meaning |
|---|---|
| `query`, `kind`, `entity_id`, `name`, `matched_by`, `candidates` | the resolution (`kind` is `instrument`, `company`, `person` or `unknown`) |
| `identity` | gazetteer entry: vendor / ticker / category / launch year / status / aliases (instrument); ticker / parent / e-mail domains / aliases (company); subgroups, institution, OpenAlex id and the name forms seen (person) |
| `window` | `since` / `until` actually used (default `--since 365d`, `--until` today; both accept `YYYY-MM-DD` or `Nd`) |
| `papers_total` | distinct papers in the window that mention the entity (or list the person as an author) |
| `papers_per_month`, `sparkline` | zero-filled monthly series over exactly the window's months and an ASCII sparkline (`_` = 0, then `.:-=+*#%@` scaled to the busiest month) |
| `papers_out_of_range` | papers in the total whose month is outside the window's months (undated items first seen after `--until`, malformed dates); reported so the digits reconcile instead of being drawn as a stray bar |
| `trend` | `recent_30d` = papers in the last 30 days of the window (clipped to `since`; `recent_days` says how long the period really is) vs `prior_30d` = the 30 days before, counted over every section (`basis`). `prior_30d` is `null` with a `note` when that period would start before `since` (windows shorter than 60 days), and the text says `n/a`; `change_pct` is `null` when the prior period is empty or unmeasured. For an instrument or company the two periods are also checked for comparable coverage: when the share of papers matched *only* outside the title/abstract (Methods, funding, affiliations, which exist only where full text or rich metadata was harvested) differs by 25 points or more (both periods having at least 5 papers), or the store holds 1.5x as many papers of any kind in one period as in the other (both at least 100), `change_pct` is `null`, `note` says why and the text prints `change n/a, see note`. `coverage` gives both periods' `papers`, `title_abstract` and `corpus` counts, and `like_for_like` the title/abstract-only `recent_30d` / `prior_30d` / `change_pct`, which every source can fill; the text prints that line whenever the headline change is withheld |
| `sections` | where the entity was found: `title`, `abstract`, `methods`, `funding`, `coi`, `ack`, `affiliations`, with paper and mention counts (entities only) |
| `recent_papers` | newest first: `pub_date`, `title`, `venue` (journal or source, `(preprint)` flagged), `url`, `sections`, `snippet` (for a person: the author string that matched) |
| `co_mentioned` | `instruments` and `companies` that appear in the same papers, ranked by papers then mention count; the entity itself is excluded |
| `archive` (instrument) | SRA/ENA experiments per month declaring that instrument, plus a per-model breakdown and `experiments_out_of_range` (rows whose `first_public` month is outside the window's months). `matched_by` is `entity_id`, or `model_name` when no archive row ever resolved to the id and the count comes from `instrument_model` strings containing the canonical name (a `note` says so) |
| `archive` (company) | the same, matched by `ticker` (or by `vendor` name for private companies) |
| `portfolio` (company) | that vendor's gazetteer instruments with paper and archive-experiment counts in the window (instruments whose `vendor`, `parent` or ticker points at the company) |
| `news` (company) | `news_items` tagged with the company id, newest first |
| `tracked_hits` (person) | `tracked_author_hits` rows with subgroup and `matched_by` (`openalex_id` or `name`), so name-collision hits are visible |

The window rule is the one the digest uses (`litradar/store.py`): a paper counts when `pub_date`
falls in `[since, until]`, or when it has no date and was first seen on/after `since`. Months are
taken from `pub_date`, falling back to `first_seen` for undated feed items.

Scratch space is a `TEMP` table, so `show` works on a read-only database file (SQLite permits temp
tables on read-only connections); the watchlist does not, and says so. `dossier()` commits the
implicit transaction its scratch INSERT opens (unless the caller was already inside one), so calling it
from a long-lived connection such as the pipeline's never pins a WAL read snapshot.

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

`watch run --since 7d [--until] [--limit 20]` evaluates every row. For each term it reports
`matches`, the **full count** of papers in the window (a `COUNT(*)` that `--limit` never caps), and
`items`, the newest `--limit` of them ordered by publication date (`shown` = how many were printed; the
text header reads `785 matches (showing 20)` when they differ):

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

Output: `{since, until, total_matches, terms: [{term, kind, added_at, matches, shown, items: [...]}]}`;
`total_matches` sums the full counts; each item has `id`, `pub_date`, `title`, `journal`/`source`,
`kind`, `url`, `snippet`.

## Caveats

* Everything is read from the store; coverage is whatever the weekly runs have collected. On a
  database holding one week, every sparkline is a single bar; with the default 365-day window the
  prior 30-day period is inside the window and genuinely empty (`prior 30 days 0`), while a window
  shorter than 60 days reports `prior 30 days n/a` rather than a measured zero.
* Romanised East Asian names are matched in both orders when both parts are spelled out (see
  "Person-name forms"), which can merge two different people whose given name and surname mirror each
  other; initials never swap, so Europe PMC's `Surname I` rows are only found from the Western order or
  the stored form.
* `watch add <name>` auto-detects `author` only when the name already occurs in a stored author list
  (or is tracked); a CJK or Latin name with no paper yet is stored as a free-text `term`. Use
  `--kind author` to watch a name before its first paper arrives.
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
* `--until Nd` means N days before today (same rule as `--since`), so `--since 90d --until 30d` is a
  60-day window ending a month ago.
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

Checks of the person-name and counting rules on the same snapshot (3562 papers, of which 537 come
from Europe PMC with authors stored as `Surname I`):

```
show "M. Krokidis"   -> M. Krokidis  [person]  matched by authors · name forms: Krokidis M · 1 papers
show "Krokidis, M."  -> same paper; show "Krokidis M" -> same paper
show 胡文欣           -> 胡文欣  [person]  matched by authors · name forms: Wenxin Hu (胡文欣) · 1 papers
show "Wenxin Hu"     -> 4 papers (the initial rule also admits "Hu, W." rows: a visible, documented collision)
show 张锋             -> exit 1, nothing matches (no such author in that corpus)
show illumina --since 7d   -> trend: last 8 days 165 vs prior 30 days n/a (window starts 2026-10-10)
show illumina --until 30d  -> window: 2025-10-17 .. 2026-09-17 · 0 papers
show illumina --until garbage -> exit 2: bad --until 'garbage': use YYYY-MM-DD or Nd (e.g. 30d)
watch add "" -> exit 1: watch: empty watch term; watch add foo (read-only file) -> exit 1: the database is read-only
```

`watch run --since 7d` on a writable copy with six terms (`methylation ctDNA`, `Revio` -> `pacb-revio`,
`sequencing`, `甲基化`, `Krokidis M`, `胡文欣`): `total_matches 790`; `sequencing` reports
`785 matches (showing 20)`, and a direct `SELECT COUNT(*)` over `papers_fts` for the same window
returns 785.

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
* **Tracked-author matching at ingest.** In `litradar/pipeline.py`, replace `_norm_name` with
  `entities.name_key` (compare keys with `entities.names_match`) so name-based `tracked_author_hits`
  also catch Europe PMC's `Surname I` rows; today the dossier compensates by scanning author lists.
