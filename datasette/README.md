# Datasette front-end for the radar database

[Datasette](https://datasette.io) turns the SQLite store into a browsable, queryable website with a
JSON API for every page. The metadata in this directory adds the parts Datasette cannot infer:
full-text search, facets, column descriptions and nine canned queries.

Datasette is optional. `litradar serve` (no flags) runs a dependency-free JSON API and search page
instead; see `docs/search.md` for both.

## Run it locally

```bash
pip install 'lit-radar[serve]'          # or: pip install 'datasette>=0.64'
litradar serve --datasette               # wraps the command below; http://127.0.0.1:8001/
```

What that runs:

```bash
datasette -i data/radar.sqlite -m datasette/metadata.yaml --setting sql_time_limit_ms 5000 --port 8001
```

- `-i PATH` (`--immutable`) tells Datasette the file will not change while served, which enables
  count caching and HTTP caching. The pipeline rewrites the database on every `litradar run`, so
  either serve a copy, or use `litradar serve --datasette --no-immutable` while the pipeline may
  write (Datasette still opens the file read-only).
- `sql_time_limit_ms 5000` caps every query at five seconds; the canned queries below finish in well
  under 100 ms on a one-week database (3.6k papers, 123k archive records).
- `-m` loads `metadata.yaml`. Datasette derives the database name from the file stem (`radar`); if
  you serve a copy with another file name, rename the key under `databases:` to match.

## Full-text search

`papers_fts` is an FTS5 table keyed by the text column `id`, not by `rowid`, so Datasette's
table-level `fts_table` integration (which joins on rowid) does not apply. The **search** canned
query performs the join and accepts raw FTS5 syntax:

```
/radar/search?query=methods:Xenium
/radar/search?query="spatial transcriptomics" AND NOT Visium&since=2026-10-01
/radar/search?query={methods coi}: Illumina
/radar/search?query=NovaSeq*
/radar/search?query=NEAR("spatial transcriptomics" "single-cell", 10)
```

Append `.json` to any page for the data (`?_shape=array` gives a plain list of objects). Hyphenated
terms must be quoted (`"single-cell"`): FTS5 reads an unquoted `-cell` as a negated column filter and
Datasette shows the resulting "no such column" error. The full syntax cheat-sheet is in `docs/search.md`.

## Canned queries

| Query | Parameters | Answers |
|---|---|---|
| `search` | `query`, `since` | full-text search with snippet and bm25 rank |
| `papers_naming_instrument_in_methods` | `instrument` (gazetteer id or display name), `since` | which papers used a given instrument, from the Methods section |
| `companies_by_role` | `since`, `until` | how each watched company appears: author affiliation, funder/COI, subject, supplier |
| `archive_experiments_per_model_per_week` | `since`, `until`, `model` | SRA/ENA/GEO/PRIDE deposits per instrument model per week |
| `tracked_author_hits` | `subgroup`, `since` | papers by watch-listed authors |
| `news_by_company` | `company` (gazetteer id), `since` | headlines tagged with a company |
| `preprint_to_publication_pairs` | none | preprints with a journal DOI, paired with the article when stored |
| `top_scored_papers` | `since`, `until` | highest final scores with the signals behind them |
| `unlisted_company_institutions` | `since` | corporate-looking affiliations not matched by the company gazetteer (candidates for `companies.yaml`) |

Blank parameters mean "no filter" everywhere. Each query also has a JSON form:

```bash
curl 'http://127.0.0.1:8001/radar/companies_by_role.json?since=2026-10-10&until=2026-10-17&_shape=array'
```

## Tables, facets, descriptions

`papers` is faceted by `journal`, `kind` and `source`, sorted newest first; `archive_records` by
`archive`, `platform`, `library_strategy` and `vendor`; `mentions` by `kind`, `section` and `vendor`;
`news_items` by `feed`; `tracked_author_hits` by `subgroup`. Every column has a description that
Datasette shows on the table page. The FTS shadow tables are hidden from the index. A `timeseries`
entry is included for the optional monthly-counts table that `litradar timeseries` creates; it is
ignored when the table does not exist.

## Publishing a public copy

Datasette has first-party and plugin publishers. Every option bakes the database file into the
deployment, so a refreshed database means a redeploy; all of them are read-only by construction.

| Target | How | Caveats |
|---|---|---|
| Google Cloud Run | `datasette publish cloudrun data/radar.sqlite -m datasette/metadata.yaml --service radar` | needs the `gcloud` CLI and a project with billing enabled (a free tier exists, but billing must be on); cold starts of a few seconds; image rebuilt on every publish |
| Vercel | `pip install datasette-publish-vercel` then `datasette publish vercel data/radar.sqlite -m datasette/metadata.yaml --project radar` | serverless: the database counts against the function bundle size limit and requests against the function duration limit (check the current limits for your plan; a ~120 MB file is near them); keep `sql_time_limit_ms` low |
| Fly.io | `pip install datasette-publish-fly` then `datasette publish fly data/radar.sqlite -m datasette/metadata.yaml --app radar` | always-on VM, no cold starts; `--create-volume` lets you replace the database without rebuilding; account needs a payment method (check current pricing) |
| Datasette Lite (no server) | host the `.sqlite` file on any static host and open `https://lite.datasette.io/?url=<file-url>&metadata=<metadata-url>` | runs Datasette in the browser via WebAssembly; the whole file downloads to each visitor; FTS5 availability depends on the Pyodide SQLite build, so test the `search` query first |

Hardening settings worth adding for anything public:

```bash
--setting default_allow_sql off     # canned queries only; no arbitrary SQL
--setting allow_download off        # no one-click download of the whole file
--setting sql_time_limit_ms 2000
--setting max_returned_rows 1000
--setting default_cache_ttl 3600
```

Before publishing, make a copy and strip anything you do not want public. Author records can carry
e-mail addresses when a source supplied them; on the **copy** (never the live store):

```sql
UPDATE papers SET authors_json = (
  SELECT json_group_array(json(json_remove(a.value, '$.email'))) FROM json_each(papers.authors_json) a
) WHERE authors_json LIKE '%"email": "%@%';
VACUUM;
```

Full-text bodies of open-access papers are stored under their licences; check `docs/LEGAL.md` and the
data-policy settings before redistributing them.

## Datasette versions

This metadata targets the Datasette 0.6x stable series (verified with 0.65.4). The 1.0 alpha series
moves `queries`, `facets`, `sort`, `hidden` and similar keys out of metadata into a configuration file
passed with `-c`; if you install an alpha, split the file accordingly.
