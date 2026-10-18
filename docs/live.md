# Live mode: a daily corpus, a weekly digest

The original design was a weekly batch: one `litradar run --days 7` every Monday that fetched,
extracted, ranked, stored and rendered in a single pass. Live mode keeps every stage and changes
only the cadence. The database is updated every day (or every few hours on a laptop) by
`litradar update`; the digest and the dashboard are rendered from the database once a week, as
before. Nothing in the extraction, ranking or storage code changed; `update` is `run` without the
rendering, with a window computed from the database instead of a fixed look-back.

| Cadence | Command | What it does | Where it runs |
|---|---|---|---|
| daily, 06:20 UTC | `litradar update --overlap-days 2` | extends the corpus from the last recorded run forward; writes `digests/_work/last_update.json`; no digest | `.github/workflows/daily.yml`, or launchd/cron |
| after every update | `litradar status` | freshness and health; exit 1 when the data is stale or many sources failed | same job (fails it), or a cron alert |
| weekly, Monday 11:15 UTC | `litradar run --days 7` (digest + dashboard republish) | re-pulls the week (idempotent), renders `digests/<date>.md`, commits/e-mails/posts it | `.github/workflows/weekly.yml` |

The two scheduled workflows share the concurrency group `lit-radar-db`, so they never touch the
database at the same time. On GitHub the database travels between runs as the asset
`radar.sqlite.zst` on a rolling release tagged `db-latest`; on a laptop it is simply
`data/radar.sqlite`.

## How `update` chooses its window

`runs` is the ledger: every completed `run` or `update` appends a row with `since`, `until`,
`finished` and the report (`counts`, `errors`, `http`). `update` reads the newest row and works
forward from it.

| Situation | Window | Reported as |
|---|---|---|
| no run recorded yet (fresh database) | last 7 days, ending today (UTC) | `basis: first_run` |
| normal | from the last run's `until` **minus `--overlap-days`** (default 2) to today | `basis: last_run` |
| long gap (outage, paused schedule) | the same, but never longer than `--max-days` (default 14) | `capped: true` |
| `--since` and/or `--until` given | exactly as given; an explicit `--since` is never capped | `basis: override` |

Every source adapter treats both ends of the window as calendar dates, so "overlap 2" re-fetches
the last run's `until` day and the two days before it. Re-fetching is safe: papers are upserted
(richer fields win, nothing is blanked), mentions are replaced per paper, scores are recomputed,
archive records and news items are `INSERT OR IGNORE`. The cost of overlap is requests, not
duplicates.

The cap exists so that a schedule that was off for two months does not come back with one 60-day
pull of SRA (a week is roughly 120k experiments). Repair deliberately instead:
`litradar update --since 2026-08-15` (see "Backfilling" below).

`litradar update --dry-run` prints the window it would use, the last run it is continuing from,
and the sources, then exits without a single request and without touching the database: it reads an
existing `data/radar.sqlite` read-only and creates nothing when there is none (the store is opened
only on the fetching path).

## Why the overlap: measured lags (checked live on 2026-10-17)

Sources do not become complete on the day a paper or dataset is dated. The numbers below were
measured against the live APIs on 2026-10-17 with the queries noted, so they can be re-run.

### Europe PMC (PubMed records, `SRC:MED`)

Europe PMC exposes `firstPublicationDate` (`FIRST_PDATE`) and `firstIndexDate` / `CREATION_DATE`
(when the record entered Europe PMC). Because of the lag measured below, the pipeline's window
(`europepmc.date_clause`) collects records first published in the window OR created in the index in
the window with a publication date at most 30 days earlier (`INDEX_LAG_DAYS`), so a daily pull
collects what became available that day instead of the structurally thin leading edge: on
2026-10-18 `ISSN:"0028-0836"` over 2026-10-11..18 answered 0 records by `FIRST_PDATE` alone and 102
with the combined clause. Two cohorts, every record paged with `cursorMark`:

| Publication day | Records | indexed same day | within 1 d | within 2 d | within 7 d | within 9 d | within 14 d | max lag |
|---|---|---|---|---|---|---|---|---|
| 2026-10-03 (14 days before the check) | 1,068 | 8.6 % | 83.4 % | 86.9 % | 86.9 % | 86.9 % | 100 % | 14 d |
| 2026-09-19 (28 days before) | 1,839 | 7.2 % | 54.1 % | 55.5 % | 78.1 % | 90.5 % | 96.4 % | 27 d |

Two features matter for the window:

* **Batches.** The 2026-10-03 cohort was flat between day 2 and day 10, then the remaining 13 %
  arrived at lag 11–14 days, i.e. in one load on 2026-10-16/17. The first 1,000 hits for
  `FIRST_PDATE:[2026-10-01 TO 2026-10-07]` on the day of the check had all been indexed that same
  day (lag 9–10 days).
* **The leading edge is thin.** Counts of `SRC:MED` records per `FIRST_PDATE` on 2026-10-17:
  2026-10-09 → 2,639; 2026-10-10 → 132; 2026-10-11 → 1; 2026-10-12 → 21; 2026-10-13 → 2;
  2026-10-14 → 5; 2026-10-15 → 1; 2026-10-16 → 2; 2026-10-17 → 0, against roughly 3,000–3,500
  per day for 2026-10-05..09 once settled. In other words, on the day of the check Europe PMC
  held essentially nothing dated within the previous seven days.
* **Dates get revised.** 14 of 1,068 and 8 of 1,839 records had `firstIndexDate` *before*
  `firstPublicationDate` (by up to 440 days): the publication date was changed after indexing,
  typically from an online-first date to an issue date. Such a record moves out of the window it
  was first fetched in; it is already stored, so nothing is lost, but it explains why a re-pull of
  a past week can return fewer records than the first pull.

Preprints in Europe PMC (`SRC:PPR`, 2026-10-03, 76 records) are faster: 43 % within 1 day, 92 %
within 2 days, 100 % within 3 days.

Consequence: a 2-day overlap catches the fast majority but not the tail, and the tail is a
material share (13 % of the 2026-10-03 cohort and 22 % of the 2026-09-19 cohort arrived more
than a week after their publication date). The
Monday `run --days 7` re-covers seven days, and a periodic deeper backfill (below) closes the
rest. `status` reports "gaps" (days with zero papers by publication date) so a thin day is
visible rather than silent.

### NCBI SRA (`[PDAT]` census)

The pipeline asks E-utilities for experiments whose publication date falls in the window
(`YYYY/MM/DD:YYYY/MM/DD[PDAT]`, inclusive on both ends) and reads `esummary` for the instrument
model. The summary also carries `CreateDate` and `UpdateDate`.

* For `2026/10/10[PDAT]` (20,424 experiments) a 500-record sample had `CreateDate` equal to the
  PDAT day for **500 of 500** and `UpdateDate` equal to it for 500 of 500. SRA's `CreateDate` is
  therefore the day the public record was created, not the submission date, and it carries no
  information about late arrival; the question "do records appear with a PDAT in the past?" has
  to be answered by re-counting a past day later (`status` makes the counts visible; see also the
  T0/T1 comparison below).
* Daily volume is very uneven: per-day `[PDAT]` counts over the 21 days to 2026-10-17 ranged from
  3,506 (the check day itself, at 21:16 UTC, i.e. a partial day) and 5,779 to 56,418
  (2026-10-01). Day-to-day variance swamps any lag signal, so SRA deltas in `status` should be
  read against the window length, not as growth.
* The run at 06:20 UTC always sees a partial "today"; the overlap re-fetches it tomorrow.

### ENA portal (`first_public`)

* **The upper bound is exclusive.** Verified on 2026-10-09: `first_public>=2026-10-09 AND
  first_public<=2026-10-09` returns 0 experiments; `... AND first_public<=2026-10-10` returns
  2,098, exactly the same as `... AND first_public<2026-10-10`. The ENA adapter uses the `<=`
  form with the window's `until`, so **the last day of a window is never fetched from ENA in the
  run that ends on it**; the next update's overlap (≥ 1 day) is what picks it up. Do not run
  `update --overlap-days 0` if ENA matters.
* Release is batched: at 21:30 UTC on the check day ENA reported 0 experiments with
  `first_public` = that day, and 0 for 2026-10-12, 13 and 14 as well, while 2026-10-08 carried
  72,858 and 2026-10-02 52,588. Zero-days are normal for ENA and are not gaps in the pipeline.
* `first_public` − `first_created` had a median of 139 days (p90 167, max 967) over 74,956
  experiments released 2026-10-08/09: submissions sit under embargo for months and then appear
  with a `first_public` of the release day. `last_updated` − `first_public` was 0 for > 99.9 %
  of them (max 7 days), so records rarely change after release; the adapter's `INSERT OR IGNORE`
  is the right policy.

### Growth of past days between two counts on the check day

Per-day counts for the 21 days to 2026-10-17 were taken twice on the check day (21:16 UTC and
21:25 UTC, nine minutes apart) for Europe PMC `SRC:MED`, SRA `[PDAT]` and ENA `first_public`.
**No day changed for any of the three sources** in that interval (0 of 21 days each). The
interval is short, so this only says that no load landed in those minutes, not that no backfill
happens; the cohort curves above, which record when each Europe PMC record actually arrived,
are the evidence that it does. A longer-baseline version of the same check is what `status`
gives you for free over time: compare a past day's `fetched.*` and `archive.*_new` counts across
successive runs that covered it.

### Defaults that follow

* `--overlap-days 2` (daily): covers ENA's exclusive upper bound, SRA's partial current day, the
  Europe PMC fast cohort (≈ 55–85 % within a day) and preprints (100 % within 3 days).
* Weekly `run --days 7`: re-covers a week, catching most of the Europe PMC medium tail.
* Monthly backfill of 28 days for the metadata sources (`--sources europepmc,crossref,openalex`):
  catches the long tail (≈ 4 % of PubMed records arrive 14–27 days late). The archives do not
  need it.

## The daily workflow (`.github/workflows/daily.yml`)

1. **Restore.** `gh release view db-latest`; if the release does not exist it is created. It is meant
   to be a pre-release that is never marked "Latest" (`--prerelease --latest=false`), so the rolling
   snapshot does not show up as the repository's latest release; if a run created it with other flags
   (the weekly and daily workflows have not always agreed), `gh release edit db-latest --prerelease
   --latest=false` fixes the existing release in place. If it holds `radar.sqlite.zst`, the asset
   is downloaded and decompressed to `data/radar.sqlite`; a failed download fails the job so a good
   snapshot is never replaced by a 7-day one. No asset yet means a fresh start (first-run window).
2. **Update.** `python -m litradar update --overlap-days 2 --max-days 14`, with optional
   `since` and `sources` from `workflow_dispatch` inputs for repairs.
3. **Upload.** `PRAGMA wal_checkpoint(TRUNCATE)` + `PRAGMA integrity_check`, `zstd -19`, then
   `gh release upload db-latest radar.sqlite.zst --clobber`. This also runs after a failed update
   (SQLite stays consistent and upserts are idempotent, so partial progress is kept) but never
   after a failed restore.
4. **Artifact.** `digests/_work/last_update.json` is kept for 30 days as the artifact
   `last_update`, including on failure (the update command writes a failure summary).
5. **Freshness check.** `python -m litradar status` ends the job; its exit code is the job's.
   A red daily run therefore means one of: the update crashed, the last run is more than three days
   old, or more than two sources **failed** (configuration skips and rate limiting are reported but
   not counted; see "Which errors make `status` exit 1" below), so a keyless fork is green on a day
   when nothing upstream is actually down.

Secrets and variables are the same set as the weekly workflow (`LITRADAR_CONTACT_EMAIL`,
`NCBI_API_KEY`, `OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `ANTHROPIC_API_KEY`,
`LITRADAR_LLM_MODEL`, the SMTP/Slack delivery settings). Permissions are `contents: write` only,
which is what uploading to a release needs. Actions are pinned to full commit SHAs with the
version in a comment. GitHub may delay or skip scheduled runs on busy mornings and disables
schedules on repositories with no activity for 60 days; the `workflow_dispatch` trigger runs the
job by hand at any time.

## Laptop setup (every 6 hours)

Live mode on a laptop is the same command on a timer. Six hours is a good interval: Europe PMC
and bioRxiv load in batches, SRA/ENA release daily, and four runs a day cost little because the
overlap window is only a few days. The SRA census dominates: measured per-day `[PDAT]` counts in
October 2026 ranged from 3,506 to 56,418 experiments (median of the 21 days: 13,058), i.e. about
7–115 `esummary` requests of 500 records per window day; paper sources are a few hundred
requests and repeat requests inside a day are served from the on-disk cache.

**macOS (launchd).** Save as `~/Library/LaunchAgents/litradar.update.plist`, edit the two paths,
then `launchctl load ~/Library/LaunchAgents/litradar.update.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>litradar.update</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd /PATH/TO/lit-radar &amp;&amp; .venv/bin/python -m litradar update --overlap-days 2 &amp;&amp; .venv/bin/python -m litradar status</string>
  </array>
  <key>StartInterval</key><integer>21600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/PATH/TO/lit-radar/digests/_work/update.out.log</string>
  <key>StandardErrorPath</key><string>/PATH/TO/lit-radar/digests/_work/update.err.log</string>
</dict>
</plist>
```

`StartInterval` runs every 21,600 s while the machine is awake; launchd runs a missed job once on
wake. Keep the weekly digest on its own calendar entry (`docs/examples/launchd.litradar.plist`).

**Linux / macOS (cron).** Every six hours at :20, plus a Monday digest and a monthly backfill:

```
20 */6 * * *  cd /PATH/TO/lit-radar && .venv/bin/python -m litradar update --overlap-days 2 >> digests/_work/update.log 2>&1 && .venv/bin/python -m litradar status >> digests/_work/update.log 2>&1
30 7   * * 1  cd /PATH/TO/lit-radar && .venv/bin/python -m litradar run --days 7 --email >> digests/_work/cron.log 2>&1
45 3   1 * *  cd /PATH/TO/lit-radar && .venv/bin/python -m litradar update --since $(date -u -v-28d +\%F 2>/dev/null || date -u -d '28 days ago' +\%F) --sources europepmc,crossref,openalex >> digests/_work/backfill.log 2>&1
```

`status` exits 1 when the corpus is stale or many sources failed, so with `MAILTO` set cron will
e-mail you exactly when something needs attention. (`\%` is how cron escapes `%`; the `date` line
works on both BSD and GNU `date`.)

Both schedulers need `.env` (or exported variables) for the API keys; see `.env.example`. The
first run on an empty database behaves like `run --days 7` without the digest.

## Running things by hand

```
python -m litradar update --dry-run                 # what would be fetched, no requests
python -m litradar update                           # extend the corpus (overlap 2, cap 14)
python -m litradar update --sources sra,ena         # archives only
python -m litradar update --since 2026-10-01        # explicit start (not capped), until today
python -m litradar update --since 2026-09-01 --until 2026-09-30   # a fixed past window
python -m litradar update --llm                     # also score the shortlist with the LLM
python -m litradar status                           # human-readable health; exit 1 = attention
python -m litradar status --json                    # for dashboards and alerts
python -m litradar digest --days 7                  # render the weekly digest from the database (no fetching)
python -m litradar digest --since 2026-10-06 --until 2026-10-12 --print
python -m litradar run --days 7 --email             # the full weekly pass: fetch + render + deliver
```

`digest` renders what the database holds. Before the first run there is nothing to render, so it
prints `no runs yet; run \`litradar run --days 7\` first` and exits 1 instead of creating an empty
database and writing a digest full of "nothing in this window"; `--allow-empty` restores the old
behaviour for a deliberately empty render.

`update` prints one paragraph: the window and its basis, papers fetched / new to the store, new
archive records per archive, new news items (inserted rows only; news queries the run skipped, for
example GDELT after a 429 or Google News while it is opt-in, are listed separately in parentheses and
never counted as items), errors by source (with the sources that are only degraded or skipped by
configuration named as such), request counts and database size.
The same facts are in `digests/_work/last_update.json`:

```json
{
 "started": "2026-10-17T06:20:03Z", "finished": "2026-10-17T06:41:50Z",
 "since": "2026-10-14", "until": "2026-10-17",
 "window": {"days": 3, "basis": "last_run", "capped": false, "last_until": "2026-10-16", "overlap_days": 2, "max_days": 14},
 "sources": "all",
 "papers_fetched": 2210, "papers_new": 1480, "fetched_by_source": {"biorxiv": 611, "europepmc.journals": 402, "...": 0},
 "archive_new": 61834, "archive_new_by_source": {"ena": 2098, "sra": 59736},
 "news_new": 57, "news_new_by_source": {"gdelt": 51, "press": 6}, "news_queries_skipped": {"gnews": 8},
 "fulltext": {"europepmc": 388, "biorxiv_failed": 2},
 "mentions": 4120, "tracked_hits": 9, "llm_scored": 0,
 "errors": ["rss Example Journal: HTTP 404"], "error_sources": ["rss"],
 "sources_by_class": {"failed": ["rss"], "degraded": [], "config": []},
 "http": {"requests": 1543, "cache_hits": 920, "retries": 3, "failures": 1, "blocked_skips": 0},
 "db_path": "data/radar.sqlite", "db_bytes": 734003200
}
```

(Illustrative values.) When the update itself crashes the file instead carries `"failed":
"<ExceptionType>: <message>"` and the per-source errors collected before the crash; no row is
added to `runs`, so the next update re-covers the same window.

### Reading `status`

```
lit-radar status  (2026-10-17T07:02:11Z)  OK
last update (the most recent run only): finished 2026-10-17T06:41:50Z (0.01 days ago), window 2026-10-14..2026-10-17 (3 days), 2210 papers fetched / 1480 new
  errors in last run (2, sources: rss, sec):
    - rss Example Journal: HTTP 404
    - sec: SEC 8-K feeds skipped (LITRADAR_CONTACT_EMAIL not set; ...)  [configuration, not counted]
  failing sources counted against the limit: rss; configuration skips: sec
  news queries skipped in last run (not inserted items): gnews 8
  earlier run 40 (finished 2026-10-16T06:41:12Z, window 2026-10-13..2026-10-16) had 1 error(s) on openalex:
    - openalex: HTTP 429: Insufficient budget
papers/day (last 14 days, by first_seen): 612.4  (8574 total)
archive records/day (last 14 days, by first_public): 18311.9  (256366 total)
per-source counts, most recent first (run 41 2026-10-14..2026-10-17  run 40 2026-10-13..2026-10-16  run 39 2026-10-12..2026-10-15); delta = latest - previous, n/a when the latest run did not pull the source:
  archive.ena_new               2098     1107      420  (+991)
  archive.sra_new              59736    68849    72500  (-9113)
  fetched.biorxiv                611      598      640  (+13)
  fetched.openalex                 0      402      380  (n/a: not in latest run)
  ...
sources disabled in config: none
keys configured: LITRADAR_CONTACT_EMAIL=yes, NCBI_API_KEY=yes, OPENALEX_API_KEY=no, OPENFDA_API_KEY=no, ...
database: 734.0 MB at data/radar.sqlite; papers 48211, archive_records 1903377, news_items 6120, mentions 211904, runs 41
gaps (days with zero papers by pub_date in the last 30 days, 30 checked from 2026-09-01): none
next scheduled runs (UTC, as documented): daily corpus update 2026-10-18T06:20Z [20 6 * * *]; weekly digest 2026-10-19T11:15Z [15 11 * * 1]
```

* *last update* describes the most recent run only. Errors from the other recent runs (the three the
  per-source table shows) are listed under it as *earlier run ...*, so an rss-only pass does not hide
  yesterday's OpenAlex failure; the exit code still follows the latest run.
* *news queries skipped* are queries the run did not send (a host rate-limited mid-run, Google News
  while it is opt-in). They are never counted as new items.
* *papers/day* counts by `first_seen` (what the updater brought in), *archive records/day* by
  the archives' own `first_public`.
* *per-source* values come from the `counts` stored with each run; compare them against the
  window length shown in the header, since a 7-day weekly run will always dwarf a 3-day update.
  A source the latest run did not pull (a `--sources` subset) shows `n/a` instead of a large negative delta.
* *gaps* are days with zero papers by **publication date**, checked only from the first day any
  run ever covered; a day fetched late is not a gap, a day never covered is. Expect the most
  recent day or two to be thin for Europe PMC (see the lag section); a zero inside the window
  means a source problem.
* *keys configured* is presence only, and "yes" means a non-empty value: a key left blank in `.env`
  (`NCBI_API_KEY=`), even with a comment after it, is "no". The list mirrors the keys in `.env.example`
  that a source reads (`OPENFDA_API_KEY` included; the reserved Altmetric key is not listed). Values never
  appear in `status` output or in `last_update.json`.
* Exit code 1 when the last run finished more than `--stale-days` (3) ago or had **failures** on
  more than `--max-failing-sources` (2) distinct sources. Per-feed RSS failures count as one source
  (`rss`), so a handful of dead journal feeds does not page anyone. Identical error lines are
  collapsed with a count (`(x7)`), and each line is tagged when it is not counted.
* The *next scheduled runs* line is computed from the two cron expressions as documented in the
  workflow files; it does not query GitHub.

### Which errors make `status` exit 1

Every error line in a run report starts with the source that produced it. `status` sorts the lines
into three classes (`incremental.classify_error`) and counts only the first against the limit:

| Class | Recognised by | Examples | Counted? | Why |
|---|---|---|---|---|
| **failed** | everything else | `europepmc: HTTP 500`, `nih_reporter: system unavailable`, `rss: all 3 request(s) to www.example.com failed` | yes | the source was asked and did not deliver; data for the window may be missing until a re-run succeeds |
| **degraded** (rate-limited) | `rate-limited`, `HTTP 429`, `too many requests` | `gdelt: host rate-limited (HTTP 429) during the run; 9 of 10 quer(y/ies) skipped`, `arxiv: host export.arxiv.org rate-limited (HTTP 429 2 times in a row)` | no | the host throttled this client and the pipeline backed off deliberately, reported exactly what it skipped and kept the rest of the run; the next scheduled run retries. An alarm that fires on every throttle trains people to ignore the alarm |
| **configuration** | `not set`, `not configured`, `api key`, `contact_email`, `LITRADAR_`, `budget`, `insufficient` | `sec: SEC 8-K feeds skipped (LITRADAR_CONTACT_EMAIL not set ...)`, `openalex: HTTP 429: Insufficient budget` | no | nothing upstream is down and no re-run changes it; the fix is a key or an e-mail address, which `keys configured` already shows and `litradar coverage` tags "(configuration, not an outage)" |

Two more rules keep the count honest. A source is counted once however many lines it produced
(`rss Nature: 404` and `rss Cell: timeout` are one failing source). And a host the HTTP client gave up
on is labelled by the **source that owns it**, not by its hostname: the pipeline maps API hosts
through `pipeline.HOST_SOURCES` and every configured feed host to `rss`, so a dead publisher CDN is
one `rss` failure and never a source of its own (`coverage` prints an unknown host plainly rather
than inventing a `--sources www` value from it).

The consequence for a keyless fork: a run whose only errors are the SEC skip and a GDELT 429 is
green. `--max-failing-sources` still exists for installations that want a different tolerance for
real failures, and the degraded and configuration lines stay in the output, tagged, so they are
never hidden; they just do not page anyone.

## Backfilling and repairs

* **Missed days** (schedule off for a while): the next `update` is capped at 14 days and says
  `capped: true`. Run `python -m litradar update --since <first missed day>` once; it is not
  capped and may take a while for SRA. Then let the schedule continue.
* **Europe PMC long tail:** once a month,
  `python -m litradar update --since <28 days ago> --sources europepmc,crossref,openalex`
  (also available as the `since` + `sources` inputs of the daily workflow's manual trigger).
* **A source was down** for several days (visible as that source's errors in `status` and as
  zero deltas): `python -m litradar update --since <first bad day> --sources <that source>`.
* **Gazetteer edits** do not need a refetch: `python -m litradar rematch --since 90d`.
* **The database on GitHub is wrong or corrupt:** delete the asset
  (`gh release delete-asset db-latest radar.sqlite.zst`) and dispatch the daily workflow; it
  starts from a 7-day window. To seed it from a laptop instead:
  `zstd -19 data/radar.sqlite -o radar.sqlite.zst && gh release upload db-latest radar.sqlite.zst --clobber`.
