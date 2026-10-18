# Trends: what changed this week

`litradar/trends.py` turns the accumulating SQLite corpus into a weekly "What changed" section:
this week versus last week versus a trailing baseline, for instruments, companies, research
subgroups and journals, plus first appearances and preprint-to-journal conversions.

It is pure SQL + Python over tables the pipeline already writes (`papers`, `mentions`,
`archive_records`, `tracked_author_hits`). **No external API is involved**, so there is nothing to
verify live and nothing new to install; it runs the same against a laptop database or a snapshot
restored from the GitHub Actions cache.

## Usage

```bash
python -m litradar trends                      # writes digests/trends-<today>.md
python -m litradar trends --print              # Markdown to stdout
python -m litradar trends --json > trends.json # the structured result
python -m litradar trends --until 2026-10-17 --weeks 8 --top 15 --out /tmp/out
```

`--until` is the last day of the current week (default: today, UTC). `--weeks` is the baseline
length (default 13). Both are validated at the parser (`--weeks 0` or `--until 17/10/2026` exit
with the usual argparse message, status 2). `LITRADAR_DB` selects another database file, as for
every other command.

From Python:

```python
from litradar import trends
from litradar.store import Store

store = Store("data/radar.sqlite")
tr = trends.compute_trends(store, "2026-10-17", weeks=13, subgroups=trends.load_subgroups())
print(trends.render_trends_markdown(tr))
```

## Method

| Term | Definition |
|---|---|
| Week *k* | the seven days ending `until − 7k`; *k* = 0 is the current week `[until − 6, until]`, *k* = 1 the previous week |
| Event date | a paper's `pub_date`, or the date part of `first_seen` when `pub_date` is empty (same rule as `Store.papers_in_window`); archive records use `first_public` |
| Baseline | weeks 1..`weeks` that contain any data for the family (papers, or archive records). Weeks before the corpus started are *not* counted as zeros, so five weeks of history are compared against five weeks |
| Partial week | a baseline week whose **family total** is below `min_week_fraction` (default 0.25) of the median of the *other* positive weekly totals (leave-one-out, so a week is never measured against itself). It is excluded from the baseline (not from `series` or from "last week"), listed in `history.*_weeks_dropped_partial` with the median it was compared against, and named in the rendered header. Typical causes: the first run of a corpus, an interrupted run, the extra boundary day of an 8-day pull landing alone in an earlier bucket. The rule needs at least two baseline weeks to compare; `min_week_fraction=0` disables it |
| mean, std | arithmetic mean and **sample** standard deviation of the entity's weekly count over the baseline weeks; an entity absent in a baseline week counts 0 there. `std` is `null` (rendered `n/a`) with fewer than two baseline weeks |
| z | `(current − mean) / max(std, 1.0)`. The floor of one count stops integer series with no spread (3, 3, 3, …) from producing infinite scores. `z` is `null` while fewer than `min_baseline_weeks` (default 3) baseline weeks have data |
| notable | `|z| ≥ 2` **and** at least 5 counts on the side that carries the signal: this week's count for a riser, the baseline mean for a faller (a platform that went from 20 papers a week to 0 is a faller worth seeing although its current count is 0) |
| new this week | an entity whose earliest `papers.first_seen` over all its mentions falls in the current week, i.e. its first appearance in the corpus. Archive instrument models have no ingestion timestamp, so their earliest `first_public` is used. On a **cold start** (nothing in the store was seen before the current week) every entity is new; the renderer says so and omits the list |
| conversion | a preprint whose `published_doi` resolves to another paper in the store (`papers.id = 'doi:' ‖ lower(published_doi)`), reported in the week in which the journal version was published or first seen. A re-upserted preprint (new version, overlap day of consecutive pulls) does **not** re-report an old pair. `lag_days` = journal `pub_date` − preprint `pub_date` |

### Entity families and metrics

| Family | Metric per week | Source |
|---|---|---|
| instruments (literature) | distinct papers mentioning the gazetteer entry | `mentions` with `kind='instrument'` |
| instruments (archives) | sequencing experiments, plus distinct studies | `archive_records` grouped by declared `instrument_model` (vendor / ticker / gazetteer id carried along) |
| companies | distinct papers, with a role split: `as_author` (affiliations), `as_funder_or_coi` (funding, coi, ack), `as_subject` (title, abstract, methods) | `mentions` with `kind='company'`; `unlisted:` ids are kept and flagged `unlisted` |
| subgroups | papers whose title or abstract contains a subgroup keyword phrase (FTS5 phrase match on `papers_fts`) or whose OpenAlex `primary_topic` is one of the subgroup's topic ids; plus `tracked_author_papers` from `tracked_author_hits` | `config/subgroups.yaml` (passed in as `subgroups=`); without it only tracked-author counts are produced |
| journals | papers, grouped case-insensitively by journal name | `papers.journal` |

The subgroup rule mirrors `Pipeline.assign_subgroups` (which is computed but not persisted). FTS
phrase matching is token-based with Porter stemming, so it is marginally broader than the
pipeline's substring test (`scRNA-seq` also matches `scRNA seq`).

### Why the partial-week rule exists

Without it, a short first week sits in the baseline for the whole `weeks` horizon. On a corpus whose
first 8-day pull put one boundary day (a few hundred papers) into its own bucket next to full weeks
of several thousand, an entity moving from 40 papers a week to 60 scores z = +20 against three full
weeks but only z = +1.5 (not notable) once the short week is mixed in: the spread it adds damps every
score for a quarter. The family total tells the two cases apart where an entity's own series cannot
(a slow week is 70 % of the median, not 8 %). The median is taken over the *other* weeks so that
with two weeks of history a 20 % boundary-day bucket is already separated from the full week next
to it (a plain median of the pair would be their average and let it through).

### Ranking of risers and fallers

`top_movers(trends, n)` takes every entity with at least `min_count` counts this week, last week or
on baseline average, and ranks it in one of two tiers that are never interleaved: rows with a `z`
first, ranked by `z`; then rows without one, ranked by the week-over-week change among themselves.
The scales are not comparable (z-scores move by units, archive experiment counts by thousands a
week), so when the paper families have a baseline and the archive family does not (or the other way
round), the tables are z-ranked and the family without a baseline only fills the places left below
the z-scored movers; the header says so and names `n`. Without this rule a store with 13 weeks of
paper history and one week of archive history showed twenty archive rows with `z n/a`, ordered by a
delta the table does not print, and none of the hundreds of notable paper movers it had just
announced. A row without a `z` takes part only when its family's 'last week' is a complete,
comparable bucket (see "Movers: two tiers, never a mixed scale" below); otherwise it is left out and
the header says why. The renderer bolds notable rows. On a cold start (no z-score anywhere) the
tables are the week-over-week tier alone, and the header states that z-scores are suppressed.

## Backfills: publication-date weeks, not ingestion

Every weekly series in this module is bucketed by the **event date** (`pub_date`, or `first_seen` only when a
record has no date), never by the ingestion timestamp. A backfill (`litradar backfill`, or any import that writes
papers with old publication dates) therefore fills the baseline weeks and makes z-scores available in the same run,
although every one of its rows was ingested today. Verified on a copy of a one-week store (3,562 papers, all
`first_seen` on one day): inserting 360 synthetic papers with `pub_date` spread over the twelve preceding weeks, 12 of
them per week naming one instrument, plus 40 current-week papers naming it, took `history.paper_weeks_with_data` from
1 to 13 and `z_available_papers` from false to true, and the instrument's row read `current 40, mean 11.08,
std 3.33, z +8.69, notable`. The partial-week rule did not drop any backfilled week: it compares each week with the
median of the *other* weeks, so a block of equally sized backfilled weeks is never "partial" next to one much larger
live week.

Two things do depend on how the rows arrived, and both are handled:

* **Coverage of "last week".** `history.previous_week_coverage` is computed from the earliest `runs.since`; without
  more information a corpus whose first run pulled 2026-10-10..17 looks as if it covered one day of the previous
  week and the week-over-week tables are suppressed or flagged. A recorded backfill is read from `backfill_progress`
  (its earliest month, as `backfill_from`), and coverage extends back to it, so the flag disappears. The sources are
  reported in `history.coverage_sources = {"runs_since", "backfill_from"}`. Rows inserted by other means carry no
  such record; their weeks still count in the baseline, but the coverage note then relies on the `runs` table alone.
* **"New this week".** First appearances are detected by `first_seen` (new to *this database*), which is meaningless
  right after a backfill because everything was ingested this week. The cold-start flag catches that case, and when
  earlier weeks nevertheless hold publication-dated papers the renderer prints a specific note ("every paper in this
  corpus was ingested this week although N earlier weeks hold publication-dated papers (a first run after a
  backfill) ...") instead of the first-week message; `history.ingested_this_week_only_with_history` carries the same
  fact for JSON consumers. The z-scores and movers above that note are by publication date and unaffected by the
  ingestion day — subject to the source-mix rule below.
* **Source mix (comparable weeks).** A backfill is typically a capped, single-source sample (one OpenAlex topic
  pull per month) while the live week is multi-source (bioRxiv, Crossref, Europe PMC, RSS, ...). Measured live on a
  13-week OpenAlex-only backfill under a 4,099-paper multi-source week, the corpus line read `baseline 759 ± 239,
  z +13.9` and 245 movers were "notable": every one of them the step in coverage, not a change in the literature.
  A paper baseline week is therefore **comparable** only when its source mix covers the current week's: a source is
  represented in the week when its share of that week's papers is at least `min_week_fraction` of its share of the
  current week (shares, not counts, so a uniformly small partial week stays with the partial-week rule), and the week
  is comparable when the sources represented in it supply at least `min_source_coverage` (default 0.75) of the
  current week's papers. Non-comparable weeks are left out of the paper baseline and listed in
  `history.paper_weeks_dropped_source_mix` (`{week, start, end, total, covered_share, missing}`), the current
  week's own mix in `history.current_week_sources`; the renderer prints a "Source mix:" note naming the step. With
  fewer than `min_baseline_weeks` comparable weeks the paper z-scores are suppressed (the header says "comparable
  weeks"); once the live sources have fed three weeks, the z returns, computed against those weeks only. A minor
  new source (below 25 % of the live week) never makes the baseline incomparable. `min_source_coverage=0` disables
  the rule. Archive records are not affected: they are fetched by the runs alone, and their 'last week' coverage is
  reported separately in `history.previous_week_coverage_archive` (from the earliest `runs.since`, never from the
  paper backfill).

## Movers: two tiers, never a mixed scale

`top_movers` ranks rows with a z first, by z, and rows without one after them, by week-over-week change among
themselves; the two tiers are never interleaved. An archive model's week-over-week delta is in thousands of
experiments and a paper z in standard deviations, so a family without a baseline is never ranked by its raw delta
next to z-scored rows (live, with one week of archive history, the risers and fallers tables were ten `n/a`-z
archive rows each, ranked by delta, and no paper row could appear): its rows only fill the places left below the
z-scored movers, and with `n` or more z-scored risers (or fallers) they do not appear at all. Rows without a z take
part only for families whose 'last week' is a complete, comparable bucket (`week_over_week_comparable(trends,
family)`: fully covered by the runs — or, for papers, by a recorded backfill — and not dropped from the baseline as
partial or for its source mix); a delta against a bucket the runs covered for three of seven days is an artefact.
The header says whether the family without a z is listed below the z-scored rows or left out, and why; when no
family has a z the tables are the week-over-week tier alone, and when every family is incomplete they are suppressed
with the reasons.

## Output structure (`compute_trends`)

```json
{
  "until": "2026-10-17", "week_start": "2026-10-11",
  "previous_week": {"start": "2026-10-04", "end": "2026-10-10"},
  "baseline": {"weeks": 13, "since": "2026-07-12", "end": "2026-10-10", "min_weeks_for_z": 3, "min_week_fraction": 0.25, "min_source_coverage": 0.75},
  "history": {"paper_weeks_with_data": 0, "paper_weeks_holding_papers": 1, "archive_weeks_with_data": 0, "z_available_papers": false, "z_available_archive": false,
              "paper_weeks_dropped_partial": [{"week": 1, "start": "2026-10-04", "end": "2026-10-10", "total": 282, "median": 3280.0}],
              "archive_weeks_dropped_partial": [],
              "paper_weeks_dropped_source_mix": [{"week": 2, "start": "2026-09-27", "end": "2026-10-03", "total": 975, "covered_share": 0.26, "missing": ["biorxiv", "crossref"]}],
              "current_week_sources": {"biorxiv": 1254, "openalex": 1083},
              "previous_week_partial_papers": true, "previous_week_partial_archive": false,
              "previous_week_coverage": {"covered_from": "2024-01-01", "days_covered": 7, "partial": false},
              "previous_week_coverage_archive": {"covered_from": "2026-10-10", "days_covered": 1, "partial": true},
              "coverage_sources": {"runs_since": "2026-10-10", "backfill_from": "2024-01-01"},
              "ingested_this_week_only_with_history": true},
  "thresholds": {"notable_z": 2.0, "min_count": 5, "std_floor": 1.0},
  "cold_start": true,
  "warnings": ["subgroup spatial: keyword query failed (...); keyword papers counted as 0"],
  "totals": {"papers": {...delta...}, "archive_experiments": {...delta...}},
  "instruments": {"papers": [row, ...], "experiments": [row, ...]},
  "companies": [row, ...], "subgroups": [row, ...], "journals": [row, ...],
  "new_entities": {"instrument": [...], "company": [...], "journal": [...], "archive_instrument": [...]},
  "conversions": [{"preprint_id", "preprint_doi", "preprint_title", "preprint_server", "preprint_date", "preprint_url",
                   "article_id", "article_doi", "article_title", "journal", "article_date", "article_url", "article_first_seen", "lag_days"}],
  "notable": [row + {"group": "instrument_papers" | "instrument_experiments" | "company" | "subgroup" | "journal"}]
}
```

Every `row` carries `id`, `name`, `kind`, `metric` and the delta fields `current`, `previous`,
`delta`, `pct_change`, `mean`, `std`, `z`, `notable`, `direction`, `baseline_weeks`, `series`
(weekly counts, oldest first, `weeks + 1` long). Company rows add `ticker`, `parent`, `unlisted` and
`roles` (a delta per role); archive rows add `vendor`, `ticker`, `entity_id` and `studies`;
subgroup rows add `tracked_author_papers`. Everything is JSON-serialisable.

## Rendered section

`render_trends_markdown(trends, top_n=10, heading_level=2, window_note=None)` produces:

* a header with the window, the amount of history available, the statement that weeks are
  seven-day buckets ending `until`, and the exact z / notable rule; then, when applicable, the
  optional `window_note` sentence, the partial weeks excluded from the baseline, and any
  `warnings`;
* a corpus line (papers and archive experiments: this week, last week, baseline, z);
* **Biggest risers** and **Biggest fallers** tables (entity, kind, this week, last week,
  baseline mean ± std, z, detail — company role split, archive study count or ticker);
* **New this week** (instruments, companies, journals, archive instrument models), or the
  cold-start note;
* **Preprints that became journal articles** with posting / publication dates, lag and the
  median lag.

## Wiring into the digest

`litradar/digest/render.py` is not modified by this change. To add the section after the top
innovations block:

```python
# litradar/digest/render.py
from .. import trends as trends_mod                         # near the other imports

    # ---- what changed (after the "Top N innovations" loop, before "Instruments named in this week's papers")
    tcfg = cfg.get("trends") or {}
    trend_data = None
    if tcfg.get("enabled", True):
        trend_data = trends_mod.compute_trends(
            store, until, weeks=int(tcfg.get("weeks", 13)), subgroups=trends_mod.load_subgroups(),
            min_week_fraction=float(tcfg.get("min_week_fraction", 0.25)),
        )
        md.append(trends_mod.render_trends_markdown(
            trend_data, top_n=int(tcfg.get("top_n", 10)), window_note=trends_mod.digest_window_note(since, until),
        ))
```

The shipped digest renderer (`_trends_section` in `litradar/digest/render.py`) does this and also passes
`conversions_note=...`: the digest prints its preprint-to-journal conversions once, under the top innovations
(`Store.preprint_conversions`, which covers the `published_doi` pairs this module finds plus title matches), and the
trends section prints that pointer instead of its own table, so one document never carries two answers. The
standalone `litradar trends` output keeps the table.

and in the `summary = {...}` dict at the end of `render()` add
`"trends": {"notable": trend_data["notable"], "new_entities": trend_data["new_entities"], "conversions": trend_data["conversions"], "totals": trend_data["totals"], "warnings": trend_data["warnings"]} if trend_data else None`.

**Counts differ from the digest tables.** The digest's own tables count the pipeline window
(`pipeline.window(days=7)` gives 8 inclusive days, `since..until`), while the trends section
counts the seven-day bucket `until − 6..until`. The same entity can therefore show, say, 46 papers
in "Instruments named in this week's papers" and 40 in "What changed" in the same document. The
`window_note` argument above prints the digest window and this caveat directly under the header.

Optional `config/radar.yaml` block (defaults apply when absent):

```yaml
trends:
  enabled: true
  weeks: 13
  top_n: 10
  min_week_fraction: 0.25
```

## Limitations

* Trends are computed on the current contents of the store. A paper published three weeks ago but
  ingested this week is counted in the week it was published, so earlier weeks' counts can rise
  retroactively. Weekly snapshots of `--json` output capture what the radar knew at the time.
* The first run of a corpus is a cold start: no baseline, every entity new. The section becomes
  informative from the second weekly run and reaches its full design from week fourteen.
* Trend weeks are seven-day buckets ending `until`; the digest tables use the pipeline window, which
  is eight inclusive days for `--days 7`. Counts for the same entity differ between the two parts of
  one document (see the wiring section); `window_note` makes the reason visible.
* A week whose family total is below 25 % of the median weekly total is treated as partial and left
  out of the baseline. A genuinely slow week is never that slow; but a corpus that is deliberately
  tiny (a handful of sources) with a bursty week can see the quiet weeks excluded. Set
  `min_week_fraction=0` to keep every week.
* A paper baseline week whose source mix does not cover the current week's (see "Source mix") is left
  out as well, so after a single-source backfill the paper z-scores are suppressed until the live
  sources have fed `min_baseline_weeks` weeks; the archive family is unaffected. Set
  `min_source_coverage=0` to compare against every week regardless of source.
* Movers come in two tiers, z-scored rows first and rows without a z after them, never interleaved;
  a family whose 'last week' is incomplete or non-comparable is left out of the second tier, with
  the reason printed.
* A `published_doi` back-filled onto a preprint whose journal version was already stored in an
  earlier week is not reported as a conversion (there is no persisted "already reported" state, so
  the alternative would be re-reporting old pairs every time a preprint is re-upserted).
* `first_seen` is an ingestion timestamp, so "new this week" means new to *this* database, not new
  to the world; after a backfill the list is omitted with a note until the next weekly run (see "Backfills").
* Conversions in this module depend on sources that expose a `published_doi` (bioRxiv / medRxiv) and
  on the journal version being in the store under its DOI id; conversions whose journal version was
  never ingested are not visible. The digest's own list (`Store.preprint_conversions`) adds title
  matches with a preprint-server and posting-date guard and is the one printed in the digest.
* Anything that silently degraded during the computation (a subgroup keyword query that failed, so
  its keyword papers count 0) is listed in `warnings` and printed under the header, so a zero is
  never mistaken for a measurement.
