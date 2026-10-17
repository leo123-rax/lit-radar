# OpenAlex backfill: building the historical corpus

`litradar run` looks back one week. `litradar backfill` fills the same `papers` table with years of
history so trends (by subgroup, by country, by language, by venue) can be computed over time. Two routes
feed it, both metadata only (title, abstract, venue, authors, institutions, topics, country codes), both
resumable:

| Route | Command | Needs | Best for |
|---|---|---|---|
| API | `litradar backfill --from 2018-01 --to 2026-10 [--subgroup id ...] [--country CC ...] [--max-per-month N]` | a free OpenAlex key (`OPENALEX_API_KEY`) in practice | targeted pulls: one subgroup, one country, a few years |
| Snapshot | `litradar backfill --snapshot-dir DIR [--subgroup ...] [--country ...] [--from --to]` | disk space and `aws s3 sync` (no account) | the whole corpus, no API budget, repeatable |

Helpers: `--dry-run` lists the pending `(subgroup, month, country)` units; `--status [--json]` shows what
has been recorded. Code: `litradar/backfill.py`; tests: `tests/test_backfill.py` (offline, canned payloads
and a tiny gz fixture under `tests/fixtures/openalex_snapshot/`).

All statements below marked *verified* were checked live on 2026-10-17 from a residential IP without an
API key.

## 1. API route

### Request shape

```
GET https://api.openalex.org/works
    ?filter=primary_topic.id:T11289|T10222|T10621,
            title_and_abstract.search:"single-cell RNA" OR "scRNA-seq" OR "single-nucleus" ...,
            from_publication_date:2024-01-01,to_publication_date:2024-01-31
            [,institutions.country_code:cn]
    &per-page=200&cursor=*
    &select=id,doi,title,publication_date,type,language,primary_location,authorships,abstract_inverted_index,topics,primary_topic,cited_by_count,open_access,funders,biblio
    [&mailto=<LITRADAR_CONTACT_EMAIL>]
Authorization: Bearer <OPENALEX_API_KEY>        (when configured)
```

* One **unit of work** is `(subgroup, month, country)`. Topics and keywords come from
  `config/subgroups.yaml` (`openalex_topics`, `keywords`); the first 40 keywords are used because the
  request URL is limited to about 4 KB.
* Paging is the shared `litradar.sources.openalex.works()` cursor pager (200 per page) capped by
  `--max-per-month` (default 2000; the cap is checked between pages, so a unit may overshoot by up to one
  page). A unit that hits the cap is listed under `capped_units` in the report; split it with `--country`
  or raise the cap.
* A one-record **probe** request (`per-page=1`) is sent before the first unit so that an HTTP 400 (unknown
  filter or select field) is reported as a clear error instead of silently empty months.

### Documentation the filter names come from

The metered API could not be paged on the test day (see below), so the request shape follows the official
documentation rather than a sample response:

| Element | Source | Note |
|---|---|---|
| `from_publication_date`, `to_publication_date` | https://help.openalex.org/api/filtering/ | "convenience filters"; `,` = AND between filters, `\|` = OR inside one filter value |
| `institutions.country_code` | same page, example `filter=institutions.country_code:fr` | long form `authorships.institutions.country_code`; module constant `COUNTRY_FILTER` |
| `title_and_abstract.search` | https://help.openalex.org/api/searching/ | "Legacy: filter-based search … Deprecated. The `filter=field.search:` syntax still works but is no longer recommended. Use the `search` query parameter instead" (`search.title_and_abstract=`). Boolean `AND`/`OR`/`NOT` must be upper case, phrases quoted; that is how keyword expressions are built. The filter form is kept so the shared pager works unchanged; constant `SEARCH_FILTER`. |
| `primary_topic.id` | https://help.openalex.org/data/works/attributes/ | `primary_topic` = top-ranked topic, same as `topics[0]` |
| `select` list | same page | **`grants` was removed** ("funders … Replaces the removed grants property"; likewise `awards`). The shared `SELECT` in `sources/openalex.py` still names `grants`; `BACKFILL_SELECT` drops it and adds `funders` and `language`. |
| Authentication | https://help.openalex.org/api/authentication/ | free key, sent as `Authorization: Bearer` |

### What happened live (verified)

```
GET https://api.openalex.org/works?filter=primary_topic.id:T11289,title_and_abstract.search:...,from_publication_date:2024-01-01,to_publication_date:2024-01-31&per-page=2
HTTP 429
{"error":"Rate limit exceeded",
 "message":"Insufficient budget. This request has no API key, so it counts against the free daily budget shared by
            everyone on your network's IP address, and that budget is used up ($0 remaining; resets at midnight UTC).
            Use your own key instead: add ?api_key=YOUR_KEY to the URL, or send it as an \"Authorization: Bearer YOUR_KEY\"
            header. Keys are free and have their own budget: https://help.openalex.org/api/authentication/ ...",
 "retryAfter":13878,"costUsd":0.001,"dailyRemainingUsd":0,"prepaidRemainingUsd":0,"creditsRequired":10,"creditsRemaining":0}
```

`litradar backfill --from 2024-01 --to 2024-01 --subgroup single_cell --max-per-month 50` (no key, fresh
database) therefore printed a report with `"stopped": "budget"`, `"done": 0`, an empty `backfill_progress`
table, and the message *"OpenAlex budget exhausted (…). Progress saved … Re-run the same command to
resume; a free key (OPENALEX_API_KEY) has its own budget"*, exit code 0 and no traceback. Counts for a
successful month could not be collected on the test day; the first keyed run will log
`backfill: <subgroup> <month> <country> has N works (cap M)` from the probe's `meta.count`.

### Resumability

```sql
CREATE TABLE IF NOT EXISTS backfill_progress (
  subgroup TEXT NOT NULL, month TEXT NOT NULL, country TEXT NOT NULL DEFAULT '',
  fetched INTEGER DEFAULT 0, done_at TEXT, PRIMARY KEY (subgroup, month, country));
```

* Created additively by `backfill.ensure_schema()`; `litradar/store.py` is not modified.
* A row is written only when a unit finished (the pager returned its last page or the cap was reached).
  Budget exhaustion, transport failures and HTTP 400 leave the unit without a row, so the next run picks
  it up again; papers already upserted in the interrupted unit are kept (upserts are idempotent).
* `country` is `''` for unrestricted units and the upper-case ISO code otherwise, so `--country CN` and an
  unrestricted pull of the same month are tracked separately.
* Exit codes: 0 when finished or paused on budget (the report says which), 2 for argument errors or an
  HTTP 400 from the API.

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

Authorship: `{"author": {"id", "display_name", "orcid"}, "author_position": "first|middle|last",
"countries": ["JP"], "institutions": [{"id", "display_name", "ror", "country_code": "JP", "type"}],
"raw_affiliation_strings": [...], "is_corresponding": bool}`.

### What the reader does

* Walks `DIR` recursively (or reads a single file); takes `*.gz`, `*.jsonl`, `*.json`; skips `manifest*` and
  `deleted_ids*`; streams line by line with stdlib `gzip` + `json` (no extra dependency, constant memory).
* Keeps a work when `primary_topic.id` is in a subgroup's `openalex_topics` **and** a subgroup keyword
  occurs in title + reconstructed abstract (case-insensitive, hyphen/space-insensitive, word-bounded), the
  offline equivalent of the API filters. `--country` keeps works with at least one author institution in
  the list; `--from/--to` restrict `publication_date`.
* Records each finished file in `backfill_progress` as `(subgroup='_snapshot_file', month=<relative path>,
  fetched=<kept>)`, so a re-run over the same directory skips finished files. Unreadable files (truncated
  download, not gzip) are reported under `errors` and left for the next run. Malformed lines are counted
  and skipped.

Live run over three real part files (two complete, one deliberately truncated by a ranged download):

```
backfill snapshot: part_2016.gz -> 0 of 489 records kept
backfill snapshot: part_2025.gz -> 0 of 1 records kept
backfill snapshot: part_2026_head.gz unreadable (Compressed file ended before the end-of-stream marker was reached); left for the next run
{"files": 3, "files_done": 2, "lines": 490, "malformed": 0, "matched": 0, "errors": ["part_2026_head.gz: EOFError: ..."]}
```

(0 kept is the expected outcome for arbitrary partitions: these parts hold linguistics, public policy and
fusion-physics records. A full sync keeps only life-science works in the configured subgroups.)

## 3. Country and language tags

Every paper written by either route carries in `extra_json`:

| Key | Meaning |
|---|---|
| `countries` | sorted ISO 3166-1 alpha-2 codes of all author institutions (`authorships[].institutions[].country_code` ∪ `authorships[].countries`) |
| `first_author_countries` | the same for the first authorship |
| `language` | OpenAlex `language` (ISO 639-1, auto-detected from title/abstract; `zh` for Chinese-language metadata) |
| `subgroups` | every subgroup that matched; merged across runs, since `upsert_paper` otherwise replaces `extra_json` |
| `backfill_source` | `api` or `snapshot` |
| `work_type` | OpenAlex `type` (`article`, `preprint`, `review`, `dataset`, ...) |
| `grants` / `sections.funding` | funder display names from `funders[]` (the replacement for the removed `grants`) |

Example trend queries (SQLite `json1`, present in Python's bundled SQLite):

```sql
-- papers per month and country for one subgroup
SELECT substr(p.pub_date, 1, 7) AS month, c.value AS country, COUNT(*) AS papers
FROM papers p, json_each(p.extra_json, '$.countries') c, json_each(p.extra_json, '$.subgroups') s
WHERE s.value = 'single_cell' AND p.pub_date >= '2018-01-01'
GROUP BY month, country ORDER BY month, papers DESC;

-- share of first-author papers from one country per year
SELECT substr(pub_date, 1, 4) AS year,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM json_each(extra_json, '$.first_author_countries') WHERE value = 'CN') THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS share
FROM papers WHERE json_extract(extra_json, '$.backfill_source') IS NOT NULL GROUP BY year;
```

## 4. Wiring

Nothing in `litradar/pipeline.py`, `litradar/digest/render.py` or `config/radar.yaml` was changed. The
feature is reachable through the new `backfill` subcommand in `litradar/cli.py`. Optional follow-ups for
whoever owns those files:

1. `config/radar.yaml`: add a `backfill:` block (`max_per_month: 2000`, `countries: []`) and read it in
   `cmd_backfill` as defaults for the flags.
2. `litradar/digest/render.py`: a "corpus" line (`SELECT COUNT(*), MIN(pub_date) FROM papers WHERE
   json_extract(extra_json,'$.backfill_source') IS NOT NULL`) and a country table from the first query
   above.
3. `litradar rematch` already re-annotates stored papers with the gazetteers, so after a backfill run
   `litradar rematch` populates `mentions` for the historical corpus (abstract-level mentions only; no
   full text is fetched by the backfill).
4. The weekly `openalex` adapter still selects the removed `grants` field; switching `SELECT` in
   `litradar/sources/openalex.py` to `BACKFILL_SELECT` (or to `funders`) is a one-line change once a keyed
   call confirms the behaviour.

## 5. Limits and honest caveats

* No keyed API call succeeded on the test day, so filter names rest on the current documentation, not on
  a sample page; the probe request turns any mismatch into a clear error on the first run.
* `title_and_abstract.search` is a deprecated-but-working filter form; if it is ever withdrawn, switch the
  pager to the `search.title_and_abstract` query parameter (requires a small change to `works()`).
* The API intersects the topic list with keywords (OpenAlex stemming); the snapshot matcher uses literal,
  hyphen-tolerant keyword matching, so the two routes can differ slightly at the margin.
* The budget message is truncated by the shared adapter at 200 characters.
* The snapshot is ~615 GB for works; sync a subset of `updated_date=` partitions first (recent partitions
  carry the most recently changed records, not the most recently published ones).
