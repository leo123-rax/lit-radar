# Searching the radar: FTS5 syntax, the JSON API, Datasette, and a Python client

The store is one SQLite file with an FTS5 index over every paper. Three ways in:

| Way | Start | Good for |
|---|---|---|
| CLI | `litradar search "methods:Xenium" --since 90d` | quick checks at the terminal |
| JSON API + search page | `litradar serve --port 8787` then http://127.0.0.1:8787/ | sharing with people and scripts; no extra dependencies |
| Datasette | `litradar serve --datasette` then http://127.0.0.1:8001/ | browsing tables, facets, canned queries, ad-hoc SQL |

All three are read-only and all three accept the same FTS5 query language.

## 1. What is indexed

`papers_fts` (tokenizer `porter unicode61`) holds one row per paper with these searchable columns:

| Column | Content | Typical use |
|---|---|---|
| `title` | title | `title:CRISPR` |
| `abstract` | abstract | `abstract:"cell atlas"` |
| `methods` | Methods / Materials section when full text was available | `methods:NovaSeq` - who *used* an instrument |
| `funding` | funding statement | `funding:NIH`, `funding:foundation` - who paid |
| `coi` | competing-interests / disclosures | `coi:Illumina` - employees, consultants, equity |
| `affiliations` | author affiliation strings | `affiliations:"Genomics Inc"` - industry authors |
| `body` | the rest of the open-access full text | everything else |

Tokenizer facts that change how you write queries:

- **Case-insensitive, diacritics folded**: `xenium`, `Xenium` and `XENIUM` are the same token.
- **Porter stemming**: `sequenced`, `sequencing` and `sequence` all match `sequenc*`'s stem; a search for
  `sequencing` also finds "sequenced". Quotes do not switch stemming off.
- **Punctuation splits tokens**: `NovaSeq-X`, `NovaSeq X` and `NovaSeq/X` all become the tokens
  `novaseq` + `x`; `10x` stays one token; `R10.4.1` becomes `r10`, `4`, `1`; `single-cell` becomes
  `single` + `cell`. Search for such terms as **phrases**: `"NovaSeq X Plus"`, `"single-cell"`, `"R10.4.1"`.
- **Hyphens and other punctuation are syntax outside quotes**: FTS5 reads `single-cell` as the term
  `single` followed by a negated column filter `-cell:` and fails with "no such column: cell";
  `R10.4.1` unquoted is a syntax error. Always quote hyphenated or dotted terms.

## 2. FTS5 cheat-sheet for this schema

| You want | Write | Notes |
|---|---|---|
| all words, any column | `Xenium Visium` | implicit AND |
| either word | `Xenium OR Visium` | operators are upper-case |
| exclude | `Xenium NOT Visium` | binary: `a NOT b` |
| grouping | `(Xenium OR Visium) AND methods:NovaSeq` | |
| exact phrase | `"spatial transcriptomics"` | tokens must be adjacent and in order |
| one column | `methods:Revio` | column name, colon, term; no space |
| one column, phrase | `coi:"Oxford Nanopore"` | |
| several columns | `{methods coi funding}: Illumina` | braces, space-separated column names |
| several columns, whole expression | `{methods coi}: (Illumina OR "10x Genomics")` | |
| prefix | `NovaSeq*` | matches `novaseq`, `novaseqx`, ... ; stems still apply |
| proximity | `NEAR("spatial transcriptomics" "single-cell", 10)` | phrases inside `NEAR(...)`, max token distance after the comma (default 10) |
| term at start of a column | `^Xenium` | `title:^Xenium` = title begins with Xenium |
| phrase with a hyphen | `"long-read"` | unquoted, `-read` is parsed as a negated column filter and errors |
| phrase with a dot or slash | `"R10.4.1"`, `"HiSeq X/Ten"` | same rule |
| a literal quote inside a phrase | `"said ""hi"""` | double the quote character |

Ranking: results are ordered by `bm25(papers_fts)` (lower is better); the API returns it as `rank`.
Snippets mark matched tokens with `[` and `]`.

**Section filter versus column filter.** `--in methods,coi` (CLI) or `in=methods,coi` (API) wraps your
whole query as `{methods coi}: (query)`; writing `methods:` inside the query does the same for one
term. Combine them freely.

**Invalid syntax.** The CLI and the JSON API retry an unparsable query as a plain phrase (`"your text"`)
instead of failing; Datasette shows the FTS5 error so you can fix the query.

Worked examples:

| Question | Query |
|---|---|
| Who ran anything on a Revio last quarter? | `methods:Revio` with `since=90d` |
| Papers whose Methods name a 10x spatial product | `methods:(Xenium OR Visium OR "Visium HD")` |
| Disclosures that mention a sequencing vendor | `{coi funding}: (Illumina OR "Pacific Biosciences" OR "Oxford Nanopore")` |
| Industry-authored single-cell papers | `affiliations:(Inc OR Ltd OR GmbH) AND "single-cell"` |
| Protein-design papers not about antibodies | `"protein design" NOT antibod*` |
| Any nanopore kit chemistry | `methods:("R10.4.1" OR "Kit 14" OR LSK114)` |
| Cell atlas work funded by a foundation | `funding:foundation AND abstract:atlas` |
| Long-read assemblies with HiFi | `NEAR(assembl* HiFi, 15)` |

## 3. JSON API

```bash
litradar serve                          # http://127.0.0.1:8787/, database from radar.yaml / LITRADAR_DB
litradar serve --port 8787 --db data/radar.sqlite --digests digests --cors '*'
litradar serve --host 0.0.0.0           # expose on the LAN (read-only, but still put it behind something you trust)
```

Properties:

- **Read-only by construction.** Every request opens the file with SQLite's `mode=ro` URI and
  `PRAGMA query_only`; `POST`/`PUT`/`DELETE` get `405`. No user text is ever interpolated into SQL;
  the FTS5 query is a bound parameter.
- **Bounded parameters.** `limit` is capped (200 for paper lists, 500 for count tables); dates must
  be `YYYY-MM-DD` or `Nd`; ids and section names are validated. Bad input is a `400` with
  `{"error": "...", "status": 400}`; unknown paths are `404`.
- **Windows.** `since`/`until` default to the last 7 days ending today (UTC) on the count endpoints.
  `/search` and `/entities` take `since` only (no upper bound), like the CLI.
- **CORS** is off unless `--cors ORIGIN` is given (`*` or one origin). `OPTIONS` preflight is answered.
- **Logging**: one line per request (`GET /search?q=... 200 4.1ms`); e-mail addresses in query strings
  are masked.
- **Security headers** on every response; the HTML page uses a per-response CSP nonce.
- Dependency-free: Python standard library only (`http.server`, threading, `sqlite3`, `urllib`).

### Endpoints

`GET /api` returns this list as JSON.

| Endpoint | Parameters | Returns |
|---|---|---|
| `/search` | `q` (required, FTS5), `since`, `in` (comma list of columns), `limit` (default 50, max 200), `mentions=0` to skip chips | `{query, since, sections, limit, count, results[]}`; each hit: `id, doi, title, journal, pub_date, url, kind, source, snippet, rank, instruments[], companies[]` |
| `/papers/{id}` or `/papers?id=` | id may be URL-encoded (`doi%3A10.1000%2Fabc`) or raw | the paper with `authors[]`, `affiliations[]`, `sections{}`, `topics[]`, `extra{}`, plus `mentions[]`, `score{heuristic, llm, final, reasons}`, `tracked_authors[]` |
| `/entities/{kind}/{id}/papers` | `kind` = `instrument` or `company`; `since`; `limit` (max 200) | `{kind, entity_id, entity{name, vendor, ticker, category}, since, limit, count, results[]}`; one row per paper with `sections[]`, `mentions`, `snippet` |
| `/instruments` | `since`, `until`, `limit` (default 60, max 500) | instrument counts: `entity_id, entity_name, vendor, ticker, category, papers, mentions, sections[], example_title, example_url` |
| `/companies` | `since`, `until`, `limit` | company counts by role: `papers, as_author, as_funder_or_coi, as_subject, as_supplier` |
| `/archive/instruments` | `since`, `until`, `limit` (default 100) | archive experiments per declared instrument model: `instrument_model, platform, vendor, ticker, experiments, studies, archives[]` |
| `/top` | `since`, `until`, `limit` (default 25, max 200) | top scored papers with `final`, `reasons`, `mentions[]` and parsed JSON fields |
| `/news` | `since`, `company` (gazetteer id), `limit` (default 100) | news items with `companies[]` |
| `/stats` | | `counts{}`, `pub_date_range{min,max}`, `runs[]`, `database{file, bytes}` |
| `/digest/latest` | | the latest digest as `text/markdown` |
| `/` | | the HTML search page (vanilla JS over these endpoints) |

Chips in `/search` results: `instruments[]` and `companies[]` each hold
`{entity_id, name, vendor, ticker, sections[]}`, collapsed from the `mentions` table, so a result
card can show what the paper used and who it touches without a second request.

### curl examples

```bash
B=http://127.0.0.1:8787

# full text: anything whose Methods name Xenium in the last 90 days
curl -s "$B/search?q=methods:Xenium&since=90d&limit=20" | python3 -m json.tool

# proximity query, restricted to abstract + body, no chips
curl -s -G "$B/search" --data-urlencode 'q=NEAR("spatial transcriptomics" "single-cell", 10)' \
     --data-urlencode 'in=abstract,body' --data-urlencode 'mentions=0'

# one paper, everything the store knows
curl -s "$B/papers/doi%3A10.1126%2Fscience.aeh2982"

# papers mentioning one instrument (gazetteer id from `litradar entities`)
curl -s "$B/entities/instrument/pacb-revio/papers?since=180d"

# count tables for a window
curl -s "$B/instruments?since=2026-10-10&until=2026-10-17&limit=40"
curl -s "$B/companies?since=2026-10-10&until=2026-10-17"
curl -s "$B/archive/instruments?since=2026-10-10&until=2026-10-17"

# ranked shortlist, news for one company, totals, digest
curl -s "$B/top?since=7d&limit=10"
curl -s "$B/news?company=illumina&since=30d"
curl -s "$B/stats"
curl -s "$B/digest/latest" > latest.md

# validation errors come back as JSON
curl -s "$B/search?q=Xenium&since=yesterday"
# {"error": "since must be YYYY-MM-DD or Nd (for example 90d)", "status": 400}
```

Response fields that come from JSON columns in SQLite (`authors_json`, `sections_json`, ...) are
returned parsed, without the `_json` suffix. Full-text bodies are never returned by the API (they
live only in the FTS index); `sections.methods`, `sections.funding` and `sections.coi` are.

### The search page

`/` is a single HTML page: one input box, a "since" selector, column toggles, results with title,
venue, date, kind, snippet (matches highlighted), instrument chips (blue) and company chips (pink),
and links to the paper, its DOI and its JSON. Clicking a chip lists the papers mentioning that entity.
Searches are reflected in the URL (`/?q=...&since=...&in=...`), so a search is a shareable link. It
follows the system light/dark preference and has no external assets.

## 4. Using it from a script: `RadarClient`

`litradar.api.RadarClient` wraps the endpoints with `urllib`; no third-party packages. Either install
the package (`pip install -e .` from a clone) or copy the `RadarClient` class (about 60 lines, standard
library only) into your own script.

```python
from litradar.api import RadarClient, RadarAPIError

radar = RadarClient("http://127.0.0.1:8787")       # or a friend's host

# 1. Weekly watch: who used a long-read instrument, and where did they say so?
for hit in radar.search("methods:(Revio OR PromethION OR \"Sequel II\")", since="7d", limit=200)["results"]:
    chips = ", ".join(c["name"] for c in hit["instruments"])
    print(hit["pub_date"], hit["journal"], "|", hit["title"], "|", chips)

# 2. Everything about one paper
paper = radar.paper("doi:10.1126/science.aeh2982")
print(paper["score"]["final"], paper["score"]["reasons"])
print([a["name"] for a in paper["authors"]][:5])

# 3. Entity history and count tables
xenium = radar.entity_papers("instrument", "txg-xenium", since="365d")
counts = radar.instruments(since="2026-10-10", until="2026-10-17", limit=100)
roles = radar.companies(since="2026-10-10", until="2026-10-17")
archive = radar.archive_instruments(since="2026-10-01", until="2026-10-17")

# 4. News and digest
for item in radar.news(company="illumina", since="30d")["results"]:
    print(item["published"], item["title"], item["url"])
open("latest.md", "w").write(radar.digest_latest())

# 5. Errors are exceptions with the HTTP status
try:
    radar.search("Xenium", since="never")
except RadarAPIError as exc:
    print(exc.status, exc.message)                   # 400 since must be YYYY-MM-DD or Nd ...
```

Into pandas, if you have it:

```python
import pandas as pd
df = pd.DataFrame(radar.instruments(since="90d", limit=500)["results"])
df[["entity_name", "vendor", "papers", "mentions"]].head(20)
```

Method reference: `search(q, since=None, sections=(), limit=50, mentions=True)`, `paper(id)`,
`entity_papers(kind, id, since=None, limit=100)`, `instruments(since, until, limit)`,
`companies(...)`, `archive_instruments(...)`, `top(since, until, limit)`, `news(since, company, limit)`,
`stats()`, `endpoints()`, `digest_latest()`. Every method returns the decoded JSON (a `dict`) except
`digest_latest()`, which returns the Markdown string.

## 5. Datasette

```bash
pip install 'lit-radar[serve]'       # adds datasette>=0.64
litradar serve --datasette           # = datasette -i data/radar.sqlite -m datasette/metadata.yaml --setting sql_time_limit_ms 5000 --port 8001
```

Then:

- `/radar` lists tables and the canned queries.
- `/radar/search?query=methods:Xenium` is full-text search (FTS5 syntax as above; hyphens quoted).
- `/radar/papers?_facet=journal&_facet=kind&_facet=source` browses with facets (configured by default).
- `/radar/papers_naming_instrument_in_methods?instrument=txg-xenium`, `/radar/companies_by_role?since=...&until=...`,
  `/radar/archive_experiments_per_model_per_week?model=novaseq`, `/radar/tracked_author_hits`,
  `/radar/news_by_company?company=illumina`, `/radar/preprint_to_publication_pairs`,
  `/radar/top_scored_papers`, `/radar/unlisted_company_institutions`.
- Add `.json` (and `?_shape=array`) to any of them for data; `/radar?sql=select ...` runs ad-hoc SQL
  under the time limit.

`datasette/README.md` covers the metadata layout, the `-i` immutability caveat, publishing to Cloud Run,
Vercel, Fly.io or Datasette Lite, and the settings to add before making a copy public.

## 6. CLI equivalents

```bash
litradar search "methods:Xenium" --since 90d --limit 20 [--json]
litradar search "Illumina" --in coi,funding --since 30d
litradar entity pacb-revio --since 180d
litradar entities --kind instrument --grep nanopore      # gazetteer ids for /entities/... and canned queries
litradar stats
```
