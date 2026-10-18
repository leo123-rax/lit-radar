# OpenAlex backfill: building the historical corpus

`litradar run` looks back one week. `litradar backfill` fills the same `papers` table with years of
history so trends (by subgroup, by country, by language, by venue) can be computed over time. Two routes
feed it, both metadata only (title, abstract, venue, authors, institutions, topics, country codes), both
resumable:

| Route | Command | Needs | Best for |
|---|---|---|---|
| API | `litradar backfill --from 2018-01 --to 2026-10 [--subgroup id ...] [--country CC ...] [--max-per-month N] [--topics-only]` | a free OpenAlex key (`OPENALEX_API_KEY`) in practice | targeted pulls: one subgroup, one country, a few years |
| Snapshot | `litradar backfill --snapshot-dir DIR [--subgroup ...] [--country ...] [--from --to] [--topics-only]` | disk space and `aws s3 sync` (no account) | the whole corpus, no API budget, repeatable |

Helpers: `--dry-run` lists the pending `(subgroup, month, country)` units (or, with `--snapshot-dir`, the
pending files); `--status [--json]` shows what has been recorded; `--redo` forgets the recorded progress of
the selected units / files so they are pulled again. Code: `litradar/backfill.py`; tests:
`tests/test_backfill.py` (offline, canned payloads and a tiny gz fixture under
`tests/fixtures/openalex_snapshot/`).

All statements below marked *verified* were checked live on 2026-10-17 from a residential IP without an
API key.

## 1. API route

### Request shape

```
GET https://api.openalex.org/works
    ?filter=primary_topic.id:T11289|T10222|T10621,
            from_publication_date:2024-01-01,to_publication_date:2024-01-31
            [,institutions.country_code:cn]
    &search.title_and_abstract="single-cell RNA" OR "scRNA-seq" OR "single-nucleus" ...     (omitted with --topics-only)
    &per-page=200&cursor=*
    &select=id,doi,title,publication_date,type,language,primary_location,authorships,abstract_inverted_index,topics,primary_topic,cited_by_count,open_access,funders,biblio
    [&mailto=<LITRADAR_CONTACT_EMAIL>]
Authorization: Bearer <OPENALEX_API_KEY>        (when configured)
```

* One **unit of work** is `(subgroup, month, country)`. Topics and keywords come from
  `config/subgroups.yaml` (`openalex_topics`, `keywords`); the first 40 keywords are used because the
  request URL is limited to about 4 KB. Characters that are OpenAlex filter or phrase syntax (`"`, `,`,
  `|`, `:`) are replaced by spaces inside keywords.
* Paging is the shared `litradar.sources.openalex.works()` cursor pager (200 per page), which now reports
  how it ended (`state`): `complete` (last page received), `capped` (`--max-per-month` reached with a cursor
  still open) or `error` (transport failure, non-JSON body, HTTP error, persistent rate limit). Only
  `complete` and `capped` units are recorded as done.
* OpenAlex returns filter-only queries in **`cited_by_count` descending** order (verified live 2026-10-18: a
  one-month topic filter came back 286, 112, 102, 92, 79, 66 citations), so a unit that hits the cap keeps the
  month's most-cited works and drops the long tail. A first pass with a small cap (for example 300) is therefore a
  usable "top of the month" corpus; a later run with a larger cap reopens the capped units and adds the tail.
* `--max-per-month` (default 2000, must be >= 1) is checked between pages, so a unit may overshoot by up to
  one page. The cap is stored with the progress row; a capped unit is listed under `capped_units` in the
  report and **re-opened automatically by any later run with a larger `--max-per-month`**. Splitting by
  `--country` is the other way to get under the cap.
* **Probes before the first unit** (one record each, `per-page=1`): (a) the full request, so an HTTP 400
  (unknown filter, select field or query parameter) is reported as a clear error instead of silently empty
  months; (b) a boolean-syntax check that cannot false-alarm: `count("kw1" OR "kw2" ...)` must be at least
  `count("kw1")` alone (a union cannot be smaller than one of its terms). If it is smaller, OpenAlex is
  treating `OR` as `AND` (its documented default for bare words) and the run stops with a `BackfillError`
  before recording anything. The check repeats on following units while both counts are 0 (inconclusive)
  and is skipped with `--topics-only`. A third probe without the search parameter logs the topic-only count
  and warns when the keyword search did not narrow it at all.
* A plain **HTTP 429** (per-second rate limit, no "budget" in the message) is slept on once
  (`retryAfter` from the body or the `Retry-After` header, at most 60 s) and the page is retried; if it
  persists the unit is left for the next run. Only budget answers (`"budget"` in the message, or HTTP 403)
  stop the run.

### Documentation the request shape comes from (re-read 2026-10-17)

The metered API could not be paged on the test day (see below), so the request shape follows the official
documentation rather than a sample response:

| Element | Source | Note |
|---|---|---|
| `from_publication_date`, `to_publication_date` | https://help.openalex.org/api/filtering/ | "convenience filters"; `,` = AND between filters, `\|` = OR inside one filter value (up to 100 values), `!` negates |
| `institutions.country_code` | same page, examples `institutions.country_code:fr|gb` | long alias `authorships.institutions.country_code`; module constant `COUNTRY_FILTER` |
| `search.title_and_abstract` | https://help.openalex.org/api/searching/ | the documented scoped search **query parameter** for works (with `search.title`, `search.title_abstract_keywords`; each once per request, combined with AND). "Use `AND`, `OR`, `NOT` (uppercase) for complex queries"; double quotes mark an exact phrase; "Words not separated by boolean operators are treated as `AND`". Constant `SEARCH_PARAM`. |
| legacy `filter=title_and_abstract.search:` | same page | "still works but is no longer recommended"; the page does not list `title_and_abstract.search` as a filter (only `title_abstract_keywords.search`), and documents `OR` between quoted phrases inside a filter value only for `raw_author_name.search`. Kept as the fallback constant `SEARCH_FILTER`; **not sent**. |
| `primary_topic.id` | https://help.openalex.org/data/works/attributes/ | `primary_topic` = top-ranked topic, same as `topics[0]` |
| `select` list | same page | **`grants` was removed** ("funders … Replaces the removed grants property"; likewise `awards`). The shared `SELECT` in `sources/openalex.py` still names `grants`; `BACKFILL_SELECT` drops it and adds `funders` and `language`. |
| Authentication | https://help.openalex.org/api/authentication/ | free key, sent as `Authorization: Bearer` |

### What happened live (verified)

```
GET https://api.openalex.org/works?filter=primary_topic.id:T11289|T10222,from_publication_date:2024-01-01,to_publication_date:2024-01-31
    &search.title_and_abstract="single-cell RNA" OR "scRNA-seq"&per-page=1&select=id,doi,title,publication_date,language,primary_topic,funders
HTTP 429   (2026-10-17 20:35 UTC)
{"error":"Rate limit exceeded",
 "message":"Insufficient budget. This request has no API key, so it counts against the free daily budget shared by
            everyone on your network's IP address, and that budget is used up ($0 remaining; resets at midnight UTC).
            Use your own key instead: add ?api_key=YOUR_KEY to the URL, or send it as an \"Authorization: Bearer YOUR_KEY\"
            header. Keys are free and have their own budget: https://help.openalex.org/api/authentication/ ...",
 "retryAfter":12246,"costUsd":0.001,"dailyRemainingUsd":0,"prepaidRemainingUsd":0,"creditsRequired":10,"creditsRemaining":0}
```

`litradar backfill --from 2024-01 --to 2024-01 --subgroup single_cell --max-per-month 50` (no key, fresh
database) therefore prints a report with `"stopped": "budget"`, `"done": 0`, an empty `backfill_progress`
table, the message *"OpenAlex refused the request (budget or rate limit): Insufficient budget … Re-run the
same command to resume; a free key (OPENALEX_API_KEY) has its own budget"*, exit code 3 and no traceback.
Counts for a successful month could not be collected on the test day; the first keyed run logs
`backfill: <subgroup> <month> <country> has N works after keywords (<first keyword> alone: K; topics only: T)`
from the probes' `meta.count`.

### Resumability

```sql
CREATE TABLE IF NOT EXISTS backfill_progress (
  subgroup TEXT NOT NULL, month TEXT NOT NULL, country TEXT NOT NULL DEFAULT '',
  fetched INTEGER DEFAULT 0, done_at TEXT, cap INTEGER DEFAULT 0, PRIMARY KEY (subgroup, month, country));
```

* Created additively by `backfill.ensure_schema()`; `litradar/store.py` is not modified. A table created by
  an earlier revision without `cap` gets the column added (`ALTER TABLE … ADD COLUMN`).
* A row is written only when the pager reported `complete` or `capped`. Budget exhaustion, transport
  failures, non-JSON bodies, HTTP errors and persistent rate limits leave the unit without a row (listed under
  `errors` in the report), so the next run picks it up again; papers already upserted in the interrupted
  unit are kept (upserts are idempotent).
* `cap` stores the `--max-per-month` the unit was fetched with. `progress_done()` treats a row with
  `fetched >= cap` as finished only while the requested cap is not larger; `--dry-run` applies the same rule.
* `--redo` runs `DELETE FROM backfill_progress WHERE subgroup=? AND month BETWEEN ? AND ? AND country=?`
  for the selected units (or the snapshot files under `--snapshot-dir`) before pulling; it is ignored with
  `--dry-run`.
* `country` is `''` for unrestricted units and the upper-case ISO code otherwise, so `--country CN` and an
  unrestricted pull of the same month are tracked separately.
* Exit codes: **0** finished (possibly with units listed under `errors` for the next run), **2** argument
  error, HTTP 400 or failed boolean-syntax check, **3** paused on budget (`"stopped": "budget"` in the
  report; re-run the same command to resume). `--status` shows `capped=` per subgroup.

## 2. Snapshot route

### Bucket layout (verified with anonymous `ListBucket` calls to `https://openalex.s3.amazonaws.com/`)

The complete OpenAlex database is published quarterly (second Wednesday of January, April, July and
October), **CC0**, in the public bucket `s3://openalex`. No AWS account is needed; the AWS Open Data
program covers transfer:

```
aws s3 sync "s3://openalex/data/jsonl/works" ./openalex-snapshot/data/jsonl/works --no-sign-request
```

```
s3://openalex/
├── README.txt  LICENSE.txt (CC0)  RELEASE_NOTES.txt (latest "RELEASE 2026-10-14")  browse.html
├── data/
│   ├── jsonl/
│   │   ├── manifest.json                              all entities, this format
│   │   ├── works/
│   │   │   ├── manifest.json                          works only (see below)
│   │   │   ├── deleted_ids.csv.gz                     deletion log (gzip CSV)
│   │   │   └── updated_date=YYYY-MM-DD/part_NNNN.gz   gzip JSON Lines, one work per line, <= 400,000 per part
│   │   └── authors/ sources/ institutions/ topics/ ...
│   └── parquet/ ...                                   the same data as Parquet (not read by lit-radar)
└── legacy-data/works/updated_date=.../part_NNN.gz     frozen pre-2026 flat layout (+ legacy-data/works/manifest, merged_ids/)
```

Works manifest, 2026-10-17 (`data/jsonl/works/manifest.json`, 357,013 bytes):

```json
{"date": "2026-10-14", "format": "jsonl", "entity": "works", "record_count": 464765485, "content_length": 675298563638,
 "files": [{"url": "s3://openalex/data/jsonl/works/updated_date=2016-06-24/part_0000.gz", "meta": {"content_length": 224592, "record_count": 489}},
           ...,
           {"url": "s3://openalex/data/jsonl/works/updated_date=2026-10-14/part_0159.gz", "meta": {"content_length": 663136079, "record_count": 315805}}]}
```

1,898 part files, ~615 GB compressed for works alone (several TB decompressed). The pre-2026 layout the
brief anticipated (`data/works/updated_date=.../part_000.gz`) now lives under `legacy-data/` and is
frozen; the reader handles both because it simply walks the directory. Official pages:
https://help.openalex.org/access/snapshot/ and https://help.openalex.org/access/sync/.

### Record shape (verified on real parts: `updated_date=2016-06-24/part_0000.gz`, `2025-10-30/part_0000.gz`, head of `2026-10-14/part_0159.gz`)

Top-level keys: `id, doi, title, display_name, publication_date, publication_year, type, language,
primary_location, locations, best_oa_location, open_access, authorships, institutions, countries_distinct_count,
institutions_distinct_count, corresponding_author_ids, corresponding_institution_ids, abstract_inverted_index,
topics, primary_topic, keywords, concepts, mesh, funders, awards, cited_by_count, counts_by_year, fwci,
citation_normalized_percentile, referenced_works, related_works, biblio, is_retracted, is_paratext, has_content,
has_fulltext, indexed_in, study_designs, sustainable_development_goals, created_date, updated_date` (no `grants`).
Lines are compact JSON with plain slashes (`"primary_topic":{"id":"https://openalex.org/T12454",...`).

Authorship: `{"author": {"id", "display_name", "orcid"}, "author_position": "first|middle|last",
"countries": ["JP"], "institutions": [{"id", "display_name", "ror", "country_code": "JP", "type"}],
"raw_affiliation_strings": [...], "is_corresponding": bool}`.

### What the reader does

* Walks `DIR` recursively (or reads a single file); takes `*.gz`, `*.jsonl`, `*.json`; skips `manifest*` and
  `deleted_ids*`; streams line by line with stdlib `gzip` + `json` (no extra dependency, constant memory).
* **Pre-filters before parsing**: lines are read as bytes and JSON-decoded only when they contain one of the
  configured topic ids (`openalex.org/T#####"`, escaped `\/` tolerated). With ~35 tracked topics among
  ~4,500 this skips the decode for the vast majority of the 464,765,485 records, which is the dominant cost of
  a multi-day import. Skipped lines are counted as `prefiltered`; the exact match below stays the authority.
* Keeps a work when `primary_topic.id` is in a subgroup's `openalex_topics` **and** one of: a subgroup
  keyword occurs in title + reconstructed abstract (case-insensitive, hyphen/space-insensitive, word-bounded),
  the work's `language` is not English (see section 3), or `--topics-only` was given. `--country` keeps works
  with at least one author institution in the list; `--from/--to` restrict `publication_date`.
* Records each finished file in `backfill_progress` as `(subgroup='_snapshot_file', month=<relative path>,
  fetched=<kept>)`, so a re-run over the same directory skips finished files; `--dry-run` lists files with
  their done/pending state and `--redo` clears them. Unreadable files (truncated download: `EOFError`;
  corrupted body: `zlib.error`; not gzip: `OSError`) are reported under `errors` and left for the next run.
  Malformed lines are counted and skipped.

Live run over three real part files (two complete, one deliberately truncated by a ranged download), before
the pre-filter existed:

```
backfill snapshot: part_2016.gz -> 0 of 489 records kept
backfill snapshot: part_2025.gz -> 0 of 1 records kept
backfill snapshot: part_2026_head.gz unreadable (Compressed file ended before the end-of-stream marker was reached); left for the next run
{"files": 3, "files_done": 2, "lines": 490, "malformed": 0, "matched": 0, "errors": ["part_2026_head.gz: EOFError: ..."]}
```

(0 kept is the expected outcome for arbitrary partitions: these parts hold linguistics, public policy and
fusion-physics records. A full sync keeps only life-science works in the configured subgroups.)

## 3. Country, language and match tags

Every paper written by either route carries in `extra_json`:

| Key | Meaning |
|---|---|
| `countries` | sorted ISO 3166-1 alpha-2 codes of all author institutions (`authorships[].institutions[].country_code` ∪ `authorships[].countries`) |
| `first_author_countries` | the same for the first authorship |
| `language` | OpenAlex `language` (ISO 639-1, auto-detected from title/abstract; `zh` for Chinese-language metadata) |
| `subgroups` | every subgroup that matched; merged across runs, since `upsert_paper` otherwise replaces `extra_json` |
| `backfill_source` | `api` or `snapshot` |
| `match` | `topic+keyword` (a subgroup keyword occurred in title/abstract) or `topic` (accepted on the OpenAlex topic alone); when both routes touch a paper the stronger `topic+keyword` is kept |
| `work_type` | OpenAlex `type` (`article`, `preprint`, `review`, `dataset`, ...) |
| `grants` / `sections.funding` | funder display names from `funders[]` (the replacement for the removed `grants`) |

**Non-English metadata.** Many Chinese-journal / CNKI records carry Chinese-only titles and abstracts;
English keywords cannot occur in them, so a keyword gate would silently drop exactly the works a global
corpus needs. The snapshot route therefore accepts a work in a tracked topic on the topic alone whenever its
`language` is set and is not `en`, and tags it `match: "topic"`; works without a `language` value are
treated as English. The API route cannot apply a per-work rule, so for country pulls where this matters
(for example `--country CN`) run it with `--topics-only`, which drops the `search.title_and_abstract`
parameter for every unit and tags everything `match: "topic"`. Trend queries can filter on `match` to
compare like with like.

Example trend queries (SQLite `json1`, present in Python's bundled SQLite):

```sql
-- papers per month and country for one subgroup
SELECT substr(p.pub_date, 1, 7) AS month, c.value AS country, COUNT(*) AS papers
FROM papers p, json_each(p.extra_json, '$.countries') c, json_each(p.extra_json, '$.subgroups') s
WHERE s.value = 'single_cell' AND p.pub_date >= '2018-01-01'
GROUP BY month, country ORDER BY month, papers DESC;

-- share of first-author papers from one country per year, keyword-confirmed matches only
SELECT substr(pub_date, 1, 4) AS year,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM json_each(extra_json, '$.first_author_countries') WHERE value = 'CN') THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS share
FROM papers WHERE json_extract(extra_json, '$.backfill_source') IS NOT NULL
  AND json_extract(extra_json, '$.match') = 'topic+keyword' GROUP BY year;

-- language mix of the corpus per year
SELECT substr(pub_date, 1, 4) AS year, json_extract(extra_json, '$.language') AS language, COUNT(*) AS papers
FROM papers WHERE json_extract(extra_json, '$.backfill_source') IS NOT NULL GROUP BY year, language ORDER BY year, papers DESC;
```

## 4. Wiring

Nothing in `litradar/pipeline.py`, `litradar/digest/render.py` or `config/radar.yaml` was changed. The
feature is reachable through the new `backfill` subcommand in `litradar/cli.py`. The shared pager in
`litradar/sources/openalex.py` gained two optional, backward-compatible keyword parameters (`extra_params`,
`state`) and the 429 handling described above; its five positional callers in `pipeline.py` are unchanged.
Optional follow-ups for whoever owns those files:

1. `config/radar.yaml`: add a `backfill:` block (`max_per_month: 2000`, `countries: []`,
   `topics_only: false`) and read it in `cmd_backfill` as defaults for the flags.
2. `litradar/digest/render.py`: a "corpus" line (`SELECT COUNT(*), MIN(pub_date) FROM papers WHERE
   json_extract(extra_json,'$.backfill_source') IS NOT NULL`) and a country table from the first query
   above.
3. `litradar rematch` already re-annotates stored papers with the gazetteers, so after a backfill run
   `litradar rematch` populates `mentions` for the historical corpus (abstract-level mentions only; no
   full text is fetched by the backfill).
4. `litradar/pipeline.py` still sends the weekly keyword intersection as the deprecated
   `title_and_abstract.search` filter with `|` between keywords, and the weekly `SELECT` still names the
   removed `grants` field; switching to `extra_params={backfill.SEARCH_PARAM: backfill.keyword_expression(kws)}`
   and `backfill.BACKFILL_SELECT` is a small change once a keyed call confirms the behaviour.

## 5. Limits and honest caveats

* No keyed API call succeeded on the test day, so the request shape rests on the current documentation,
  not on a sample page. Two probes on the first run turn a mismatch into a clear error: HTTP 400 for an
  unknown name, `BackfillError` when the OR-ed keyword expression returns fewer works than one of its terms.
  A silently ignored search parameter would not be caught (only warned about when the keyword count equals
  the topic-only count).
* The API intersects the topic list with keywords (OpenAlex stemming); the snapshot matcher uses literal,
  hyphen-tolerant keyword matching, so the two routes can differ slightly at the margin. The snapshot route
  additionally accepts non-English-metadata works on the topic alone; the API route needs `--topics-only`
  for the same effect, so a corpus built from both routes should be compared on `match`.
* The budget message is truncated at 300 characters.
* The snapshot is ~615 GB for works; sync a subset of `updated_date=` partitions first (recent partitions
  carry the most recently changed records, not the most recently published ones).
* `Store.upsert_paper` replaces `extra_json` wholesale; the backfill merges `subgroups`, keeps earlier
  country tags and the stronger `match`, but other keys set by both the weekly pipeline and the backfill are
  overwritten by the later writer.
