# Tracking, prioritising and coverage from the command line

`litradar track`, `litradar untrack`, `litradar prioritize` and `litradar coverage` let you change what the
index follows, say what matters most, and see where coverage is thin, without opening a YAML file. Every
write goes through the same validation as `litradar check-config`, is backed up first, and is rolled back
if the result does not load.

```bash
python -m litradar track company "Example Sequencing" --type tools --alias "ExampleSeq" --domain example-seq.com --watch-news
python -m litradar track instrument "Model Q" --vendor "Example Sequencing" --category sequencer.short_read --alias "Model Q system"
python -m litradar track author "Example Researcher" --subgroup spatial            # looks the id up, asks before writing
python -m litradar track topic organoids --name "Organoids & disease models" --keyword organoid --openalex-topic T12345
python -m litradar track term "optical pooled screening"
python -m litradar track list topic
python -m litradar untrack company example-sequencing

python -m litradar prioritize subgroup spatial --weight 1.5
python -m litradar prioritize company illumina --weight 2
python -m litradar prioritize list

python -m litradar coverage --days 7            # Markdown
python -m litradar coverage --days 7 --json
```

After adding or removing gazetteer entries run `python -m litradar rematch --since 90d` so stored papers are
re-annotated; scores (and therefore priorities) are recomputed by `rematch` and by every `run`.

(`python -m litradar` and `litradar` are the same program; the short form exists once the package is installed
with `pip install -e .`. The docs use either.)

To **see** a priority or gazetteer change land, look at the scores: `python -m litradar show illumina --json`
lists the recent papers with `score: {final, heuristic, reasons}` (the terminal view prints `(score 6.10)`
per paper), `python -m litradar search Illumina --since 30d --json` and `python -m litradar entity illumina
--json` carry the same `score` object per row, and `reasons` gains `priority x2` after a `rematch`.

## How the files are edited

The config files are documents, with comments and section headers that carry meaning, so the editor never
dumps a whole file back through the YAML emitter. Each change is one line inserted, replaced or removed:

| file | style | what `track` does |
|---|---|---|
| `config/companies.yaml`, `config/instruments.yaml` | one flow mapping per line under `entries:` | appends a `- {id: ..., name: ..., aliases: ["..."]}` line after the last entry; removes the matching line |
| `config/subgroups.yaml` | block style | inserts a `- {name: "...", openalex_id: "...", institution: "..."}` line after the subgroup's last tracked author; rewrites the `keywords:` / `openalex_topics:` flow list in place (the trailing comment survives); appends a five-line block for a new subgroup |
| `config/radar.yaml` | block style | appends to `watchlist_terms:`; maintains a `priorities:` section (see below) |

Every write:

1. refuses up front if the id already exists or the name/alias is already claimed by another entry
   (case-insensitive, the same rule `check-config` reports as an alias collision);
2. copies the file to `.cache/config-backups/<file>.<UTC timestamp>`;
3. writes atomically, then reloads the file through the Gazetteer / `yaml.safe_load` and re-runs the
   collision checks; any *new* problem restores the backup and the command exits 1 with the reason.

Pre-existing problems in a file are not held against the edit (the baseline is computed first), so a
collision you already live with does not block adding something unrelated.

### `track company`

Writes `id` (kebab-case of the name, override with `--id`), `name`, `ticker`, `type`, optional `parent`,
`watch_news`, `domains` and `aliases`. Types: `tools | diagnostics | synthesis | ai_bio | pharma | cro |
institute | software`.

When `--ticker` is omitted the name is fuzzy-matched against the SEC's `company_tickers.json`
(https://www.sec.gov/files/company_tickers.json), cached in `.cache/sec_company_tickers.json` for seven
days. The match and its confidence are printed; below `0.85` nothing is guessed and the entry is written with
`ticker: private` (re-run with `--ticker`, or pass `--no-lookup` to skip the request). The SEC requires a
contact in the User-Agent and refuses User-Agents that carry a URL, so the lookup sends
`lit-radar/0.1 <LITRADAR_CONTACT_EMAIL>` and is skipped with a message when no e-mail is configured.
Companies listed only outside the US (Hong Kong, Shanghai, Swiss, Tokyo tickers) are not in the SEC file; pass
`--ticker` for those.

### `track instrument`

`--vendor` is resolved against `companies.yaml` (id, name, alias or ticker) and the vendor's ticker is copied,
so the company has to exist first (or pass `--ticker`). The id takes the prefix the vendor's existing
instruments already use (`pacb-`, `ont-`, `ilmn-` ...). `--category` must start with one of the categories in
the header of `instruments.yaml` (`sequencer.long_read`, `spatial`, `proteomics.ms` ...). `--context`
terms set `context_required: true`, which is what you want for product names that are also ordinary words.
Add archive vocabulary to `config/archive_models.yaml` by hand if the instrument should appear in
`litradar timeseries`.

### `track author`

Appends to the subgroup's `tracked_authors`. The pipeline matches tracked authors by OpenAlex id when one is
present and by normalised display name otherwise, so an id makes matching exact.

* `--openalex A...` records the id directly; `--orcid 0000-0002-...` records an ORCID iD directly (no lookup);
  `--institution "..."` sets the institution string stored with the author (otherwise the lookup's answer is used).
* Otherwise, if `OPENALEX_API_KEY` is set, `authors?search=<name>` is queried and the top profile is proposed
  with its works count, citations and last known institution; `--yes` accepts it, an interactive terminal is
  asked, a non-interactive run records the name only. Anonymous OpenAlex traffic is metered per IP and
  returns HTTP 429 once the shared daily budget is spent, so there is no key-less lookup.
* With no OpenAlex id, the public ORCID API (`pub.orcid.org/v3.0/expanded-search/`, query
  `given-names:"First" AND family-name:"Last"`) is tried. A single exact name match is recorded as `orcid:`
  automatically; several candidates are listed and need `--yes` or a confirmation. The ORCID is stored for
  later resolution; the matcher itself does not use it yet.
* `--no-lookup` writes the name (and anything passed explicitly) without any request.

### `track topic`, `track term`, `track list`, `untrack`

`track topic <subgroup-id> --keyword K --openalex-topic T...` adds to an existing subgroup (duplicates are
ignored case-insensitively); with `--name` it creates the subgroup with an empty `tracked_authors: []`, which
the first `track author` turns into a block list. `track term` appends to `watchlist_terms`.
`track list [company|instrument|author|topic|term|priority] [--json]` prints what is tracked.

`untrack company|instrument <id|name>`, `untrack author <name|A...> [--subgroup id]`, `untrack topic
<subgroup-id> [--keyword K] [--openalex-topic T]` (without flags the whole subgroup block is removed) and
`untrack term <text>` remove things, with the same backup and validation.

## Priorities

`litradar prioritize <kind> <target> --weight W` writes to a `priorities:` section at the end of
`radar.yaml`, created on first use and edited line by line afterwards (`--clear` removes a line; an empty
kind header is removed with its last entry). Kinds and how targets are resolved:

| kind | target | stored as |
|---|---|---|
| `subgroup` | subgroup id or name | the id |
| `company` | id, name, alias or ticker from `companies.yaml` | the id |
| `instrument-category` | a category or prefix (`sequencer`, `sequencer.long_read`) | as given; prefix matches `sequencer.*` |
| `journal` | a name from `journals.yaml` (case-insensitive) | the canonical name |
| `source` | `biorxiv`, `europepmc`, `openalex`, `crossref`, `arxiv`, `rss` ... | as given; prefix matches `europepmc.journals`, `rss:<feed>` |

```yaml
priorities:
  subgroup:
    spatial: 1.5
  company:
    illumina: 2
  journal:
    Nature Methods: 1.25
```

Weights are multipliers between 0.05 and 20; 1.0 is neutral and anything not listed is 1.0. Semantics of
`litradar.track.priority_multiplier(paper_row, mentions, journal, subgroups, priorities=None)`: within one
kind the largest matching weight wins (two sequencers from one prioritised category do not compound),
and the kinds multiply. `paper_row` may be a `Paper`, a `papers` row or any object with `source`; `mentions`
may be `Mention` objects or `mentions` rows; `subgroups` are the ids from `Pipeline.assign_subgroups()`. Pass
`priorities=settings.radar.get("priorities")`; without it the default `radar.yaml` is read (and re-read when
it changes).

### Wiring the hook into the pipeline (not done by this module)

`litradar/pipeline.py`, `Pipeline.rank()`, right after `heuristics.score_paper(...)`:

```python
from .track import priority_multiplier
...
h, reasons = heuristics.score_paper(...)
mult = priority_multiplier(p, mentions, p.journal, self.assign_subgroups(p), priorities=self.cfg.get("priorities"))
if mult != 1.0:
    h = round(min(10.0, h * mult), 2)
    reasons += f"; priority x{mult:g}"
```

That single change covers all three uses: the heuristic (a), and, because the LLM shortlist is built from
`h` and sorted by `-d["_h"]`, the LLM shortlist order (c). For the full-text fetch order (b), in
`Pipeline.attach_fulltext()` change the end of the inner `priority()` function from `return -score` to

```python
return -score * priority_multiplier(p, self.instruments.find(p.abstract or "") + self.companies.find(p.abstract or ""), p.journal, self.assign_subgroups(p), priorities=self.cfg.get("priorities"))
```

## Coverage

`litradar coverage [--days 7 | --since YYYY-MM-DD --until YYYY-MM-DD] [--json]` reads only the store and
the config. It reports:

* **Records per source and share**: papers in the window by `source` (RSS feeds collapsed into one row),
  how many have parsed full text and the full-text share, plus the records each source pulled in the last
  run (from `runs.counts_json`, before de-duplication). Everything taken from the `runs` table ("Pulled in
  last run", "Sources that errored in the last run") describes the **most recent run only**, not every run
  behind the window; the header says so.
* **Journals with zero items in the window**: every journal in `journals.yaml` with no paper whose
  `journal` matches its name or whose stored ISSN list matches (so abbreviated titles from Europe PMC still
  count), with its tier, ISSNs, the last date it was seen in the store (or `never`) and its trailing
  13-week average per week.
* **Watched entities with zero paper mentions**: companies with `watch_news: true` that no paper mentioned
  (with their news-item count in the window, those with news listed first, and the trailing 13-week paper
  average), current instrument platforms (the `is_platform()` categories, not discontinued) with neither a
  paper mention nor an archive deposit in the window, tracked authors with no new paper (and how many lack
  an OpenAlex id), and watchlist terms that matched no title/abstract.
* **Sources that errored in the last run**, from the `errors` array the pipeline stores with each run.
  An error that names configuration rather than an outage (a missing contact e-mail or key, an exhausted
  budget) is tagged as such.
* **Suggestions**: errored sources to re-run alone (`litradar run --sources <id> -v`, with the real source id,
  so an SEC feed skipped for a missing contact address points at `.env`, not at a made-up source), missing
  OpenAlex key or contact e-mail, enabled sources that contributed nothing (and whether the last run pulled
  them at all, which a `--sources` subset would not), journals whose ISSN pull returned 0 and have never been
  seen ("check the ISSN", only after a run that pulled the ISSN sources over a full week), journals silent for
  13+ weeks, watched companies with no aliases and no mentions (add an alias by editing the entry's
  `aliases:` list, which keeps ticker/type/watch_news; `untrack` + `track` would drop them), tracked authors
  without ids, watchlist terms that never match, preprints without full text.

The window follows the digest: dated papers by `pub_date`, undated ones by `first_seen`. "Trailing 13 weeks"
is the 91 days before `since`, divided by 13 to give a per-week rate.

### Embedding in the digest (not done by this module)

`litradar.coverage.render_coverage_markdown(store, since, until, settings)` returns the same Markdown as the
command, starting with a `## Coverage: ...` heading. `render.render()` only receives `settings.radar`, so
the simplest wiring is in `litradar/cli.py`, in both `cmd_run` and `cmd_digest`, before the Markdown is
written:

```python
from .coverage import render_coverage_markdown
...
out["markdown"] += "\n" + render_coverage_markdown(pipe.store, since, until, settings)   # cmd_run
out["markdown"] += "\n" + render_coverage_markdown(store, since, until, settings)        # cmd_digest
```

Alternatively give `render.render()` a `settings` keyword and append the section after "Coverage and
method". `coverage_report(store, since, until, settings)` returns the underlying dict if the JSON summary
should carry it too.

## Limitations

* Edits are line-based by design: an entry that is not written as one flow mapping per line (gazetteers) or
  a subgroup whose `keywords:` is a multi-line block list is reported as "edit it by hand".
* The SEC list covers US-listed companies only; names are matched after stripping corporate suffixes
  (`Inc`, `Corp`, `/DE/` ...), and a confidence below 0.85 is never written.
* `untrack` removes the config entry but not stored mentions; run `rematch` to drop them.
* The coverage report says what was *not* seen; it cannot tell a journal that published nothing from an
  ISSN pull that failed silently. "Last seen" and the trailing average are the evidence to weigh.
