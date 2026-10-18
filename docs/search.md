# Searching the radar: FTS5 syntax, the JSON API, Datasette, and a Python client

The store is one SQLite file with an FTS5 index over every paper. Three ways in:

| Way | Start | Good for |
|---|---|---|
| CLI | `litradar search "methods:Xenium" --since 90d` | quick checks at the terminal |
| JSON API + search page | `litradar serve --port 8787` then http://127.0.0.1:8787/ | sharing with people and scripts; no extra dependencies |
| Datasette | `litradar serve --datasette` then http://127.0.0.1:8001/ | browsing tables, facets, canned queries, ad-hoc SQL |
| Static search page | `litradar dashboard --out site/` (or `litradar search-index --out site/`), publish `site/` | a server-less search over the recent window on GitHub Pages; see section 7 |

The first three are read-only and all three run the same FTS5 query language against the same index. They
differ in one convenience: the **CLI** (and the watchlist) treats plain text as a phrase, the **API** and
**Datasette** take the query exactly as written. See "Plain text versus FTS5 syntax" below.

**What is searched: stored text plus the gazetteer's attributions.** The index has seven text columns (title,
abstract, Methods, funding, competing interests, affiliations, open-access body) and one derived column,
`mentions`, holding the entity name, the matched alias and the entity id of every gazetteer mention stored
for the paper (`docs/store.md` section 2). The matcher behind `litradar entity`, the digest and the
`/entities` endpoint resolves aliases (a paper that says "Next GEM 5' reagent kit" is attributed to
Chromium), so a plain search for an entity's name or id also finds papers whose text never spells it out:
`litradar search Chromium` returns that paper through the `mentions` column, and such a hit says so
(`section: mentions`, see section 2). On the 2026-10-18 snapshot, 334 papers matched `Chromium`, 190 of them
only there. `--in mentions` restricts a search to attributed papers;
`--in title,abstract,methods,funding,coi,affiliations,body` restricts it to the words themselves. Ask by
entity id (`litradar entity txg-chromium`, `/entities/instrument/txg-chromium/papers`) when you want the
matcher's view, with the distinct-paper count and the per-section breakdown; search when you want the words.

## 1. What is indexed

`papers_fts` (tokenizer `porter unicode61`) holds one row per paper with these searchable columns (seven
text columns and the derived `mentions` column; `store.FTS_COLUMNS` is the list the CLI and the API validate
`--in` / `in=` against):

| Column | Content | Typical use |
|---|---|---|
| `title` | title | `title:CRISPR` |
| `abstract` | abstract | `abstract:"cell atlas"` |
| `methods` | Methods / Materials section when full text was available | `methods:NovaSeq` - who *used* an instrument |
| `funding` | funding statement | `funding:NIH`, `funding:foundation` - who paid |
| `coi` | competing-interests / disclosures | `coi:Illumina` - employees, consultants, equity |
| `affiliations` | author affiliation strings | `affiliations:"Genomics Inc"` - industry authors |
| `body` | the rest of the open-access full text | everything else |
| `mentions` | **not paper text**: what the gazetteer attributed to the paper - every matched instrument and company as canonical name, matched alias and entity id, ` ; `-joined (`Chromium ; Next GEM ; txg-chromium`; derived from the `mentions` table, `docs/store.md` section 2) | `mentions:Chromium`, `mentions:"Next GEM"`, `mentions:txg-chromium` - papers *attributed* to an entity under any alias, by name, alias or id; `--in mentions` asks only the matcher |

`mentions` is maintained from the mentions table whenever a paper is (re)matched, and it is searched by
default like every other column: a plain `Chromium` finds a paper that only says "Next GEM", because the
canonical name is indexed. `--in title,abstract` (CLI) or `in=title,abstract` (API) keeps a search to paper
text; `--in mentions` asks only the matcher.

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
| attributed to an entity, any alias | `mentions:Chromium` or `mentions:txg-chromium` | the derived column of section 1; a plain query searches it too |
| prefix | `NovaSeq*` | matches `novaseq`, `novaseqx`, ... ; stems still apply |
| proximity | `NEAR("spatial transcriptomics" "single-cell", 10)` | phrases inside `NEAR(...)`, max token distance after the comma (default 10) |
| term at start of a column | `^Xenium` | `title:^Xenium` = title begins with Xenium |
| phrase with a hyphen | `"long-read"` | unquoted, `-read` is parsed as a negated column filter and errors |
| phrase with a dot or slash | `"R10.4.1"`, `"HiSeq X/Ten"` | same rule |
| a literal quote inside a phrase | `"said ""hi"""` | double the quote character |

**Ordering.** Research papers come first; records that are not papers follow, each with a `record_type`
saying why (`letter`, `editorial`, `dataset`, `software`, `deposit` for a general repository such as Zenodo or
figshare, `protocol`, `supplement`, `dissertation`, `peer_review`, `paratext`; `null` for a paper). The rule is
`litradar/worktype.py` (OpenAlex `work_type`, repository DOI prefixes and venues, NEJM DOI codes, "To the
Editor:" / "Reply to" / "Supplementary ... from" cues), applied as one SQL expression. Within each group the
`sort` decides: `relevance` (default) is `bm25(papers_fts, ...)` with the title weighted 4, abstract and entity
mentions 2, Methods / funding / COI / affiliations 1 and body 0.5 (lower `rank` is better); `score` is the stored
rank score (`score`; unscored rows last); `date` is newest first. bm25 normalises by document length, so a
title-only deposit record would otherwise outrank a full-text paper that names the same platform in its title:
that is why the papers-first rule exists rather than a weight. Rows that share a title (a dataset deposited in
24 versions) fold into the best-placed one, whose `duplicates` counts the rows folded; `--keep-duplicates`
(CLI) or `collapse=0` (API) keeps them apart.

**Snippets.** Each hit's `snippet` is an 18-token excerpt with the matched tokens in `[` and `]`, cut from the
paper's own text: among `title`, `abstract`, `methods`, `funding`, `coi`, `affiliations` and `body`, the column
whose best window holds the most matches wins, the earlier column on a tie, and `section` names it (the CLI
prints it as `[methods]` before the excerpt, the search page as "matched in methods"). Only a paper that
matched through the `mentions` column alone - nothing in its text matched, the gazetteer attributed the entity
through an alias - shows that column's `;`-joined entries, with `section: mentions`. Snippets are computed for
the returned rows only, after ranking and title folding, so a broad query costs no more than a narrow one.

**Section filter versus column filter.** `--in methods,coi` (CLI) or `in=methods,coi` (API) wraps your
whole query as `{methods coi}: (query)`; writing `methods:` inside the query does the same for one
term. Combine them freely. `mentions` is a valid section name in both places (`--in mentions`,
`in=mentions`); an unknown name is a usage error (CLI) or a `400` (API) listing the valid ones.

### Plain text versus FTS5 syntax

`litradar search "liquid biopsy"` (two words, no FTS5 syntax) is a **phrase** search: the tokens must be
adjacent and in order, as if you had typed `'"liquid biopsy"'`. Without that rule the same text is an
AND of two stemmed words anywhere in the paper, and `residual` happily matches `residues`. The rule:

- a query that contains FTS5 syntax (`"`, `:`, `(`, `)`, `*`, `^`, `{}`, or an upper-case `AND`/`OR`/`NOT`/
  `NEAR`) is passed through exactly as typed;
- otherwise a multi-word query, or a single token with `-`, `.` or `/` in it (`single-cell`, `R10.4.1`), is
  quoted into one phrase (the CLI says so on stderr);
- a plain single word is left alone (stemming and prefix behaviour as documented above);
- `--raw-fts` switches the quoting off and sends the text to FTS5 verbatim.

`watch add "liquid biopsy"` follows the same rule when the term is evaluated. The JSON API and Datasette
do **not** rewrite the query: quote phrases yourself (`q="liquid biopsy"`).

**Invalid syntax.** FTS5 errors (`no such column: nosuchcol` for a mistyped column filter, `syntax error
near ""` for an unclosed parenthesis) are reported, never swallowed: the **JSON API** answers `400` with
SQLite's message (`query not understood by FTS5: ...`), Datasette shows the error on the page, and the
**CLI** prints a one-line note on stderr saying it is retrying the text as a plain phrase, then does so.

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
  be `YYYY-MM-DD` or `Nd`; ids, kinds and section names are validated. Bad input is a `400` with
  `{"error": "...", "status": 400}`; unknown paths, unknown paper ids and entity ids with no stored
  mention are `404`.
- **Privacy.** Author records sometimes arrive with e-mail addresses. The API strips `authors[].email`
  and `extra.emails` from every paper payload (`/papers/{id}`, `/top`, with or without `full=1`), and
  the request log masks e-mail-like tokens in query strings. Nothing else is redacted: titles, abstracts,
  affiliations and the stored sections are returned as stored.
- **Windows.** `since`/`until` default to the last 7 days ending today (UTC) on the count endpoints.
  `/search` and `/entities` take `since` only (no upper bound), like the CLI.
- **Search ordering.** `/search` takes `sort=relevance|score|date` (default `relevance`; anything else is a
  `400`) and `collapse=1|0` (default `1`: rows sharing a title fold into one, `duplicates` counting the rest).
  Research papers always precede letters, editorials and dataset / software deposits; `record_type` says why a
  row is in the second group (`null` for a paper). The response echoes `sort` and `collapse`.
- **Database checks at startup.** A missing file, a file that is not SQLite, or a database without the
  radar schema stops `litradar serve` with one line and exit 2; a busy port is one line and exit 1. A
  database error during a request is a `503` (`database error`), never a traceback to the client.
- **CORS** is off unless `--cors ORIGIN` is given (`*` or one origin). `OPTIONS` preflight is answered.
- **Logging**: one line per request (`GET /search?q=... 200 4.1ms`); e-mail addresses in query strings
  are masked.
- **Security headers** on every response; the HTML page uses a per-response CSP nonce.
- Dependency-free: Python standard library only (`http.server`, threading, `sqlite3`, `urllib`).

### Endpoints

`GET /api` returns this list as JSON.

| Endpoint | Parameters | Returns |
|---|---|---|
| `/search` | `q` (required, FTS5 as written), `since`, `in` (comma list of columns, `mentions` included), `limit` (default 50, max 200), `sort` (`relevance` default, `score`, `date`), `collapse` (`1` default; `0` keeps rows that share a title), `mentions=0` (also `false`/`no`) to skip chips | `{query, since, sections, limit, sort, collapse, count, results[]}`; each hit: `id, doi, title, journal, pub_date, url, kind, source, snippet, section, rank, score, record_type, duplicates, instruments[], companies[]` (`section` = the column the snippet was cut from, section 2; `record_type` = why the row is not a research paper, `null` for a paper; `duplicates` = rows with the same title folded into this one); an FTS5 syntax error is a `400` with SQLite's message |
| `/papers/{id}` or `/papers?id=` | id may be URL-encoded (`doi%3A10.1000%2Fabc`) or raw | the paper with `authors[]` (no e-mail field), `affiliations[]`, `sections{}`, `topics[]`, `extra{}` (no `emails`), plus `mentions[]`, `score{heuristic, llm, final, reasons}`, `tracked_authors[]`; unknown id: `404` |
| `/entities/{kind}/{id}/papers` | `kind` = `instrument` or `company` (anything else: `400`); `since`; `limit` (max 200) | `{kind, entity_id, entity{name, vendor, ticker, category}, since, limit, count, results[]}`; one row per paper with `sections[]`, `mentions`, `snippet`. An id with no stored mention is `404 unknown instrument id '...'` (the message says when the id exists under the other kind); the API has no gazetteer, so a valid entry nothing has cited yet is also a 404 |
| `/instruments` | `since`, `until`, `limit` (default 60, max 500) | instrument counts: `entity_id, entity_name, vendor, ticker, category, papers, mentions, sections[], example_title, example_url` |
| `/companies` | `since`, `until`, `limit` | company counts by role: `papers, as_author, as_funder_or_coi, as_subject, as_supplier` |
| `/archive/instruments` | `since`, `until`, `limit` (default 100) | archive experiments per declared instrument model: `instrument_model, platform, vendor, ticker, experiments, studies, archives[]` |
| `/top` | `since`, `until`, `limit` (default 25, max 200), `full=1` | ranked shortlist: each paper with `final`, `reasons`, parsed JSON fields, entity chips `instruments[]` / `companies[]`, `mention_rows` (count) and only the `funding` / `coi` sections, so the default 25 rows are tens of KB, not hundreds. `full=1` returns the `mentions[]` rows and every stored section instead (what `/papers/{id}` shows) |
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
# {"error": "since must be YYYY-MM-DD or Nd (for example 90d), got 'yesterday'", "status": 400}
curl -s "$B/search?q=nosuchcol:Xenium"
# {"error": "query not understood by FTS5: no such column: nosuchcol", "status": 400}
curl -s "$B/entities/instrument/no-such-thing/papers"
# {"error": "unknown instrument id 'no-such-thing': no mentions in this database (ids: `litradar entities`)", "status": 404}
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

# 5. Errors are exceptions with the HTTP status; status 0 means no server answered at all
try:
    radar.search("Xenium", since="never")
except RadarAPIError as exc:
    print(exc.status, exc.message)                   # 400 since must be YYYY-MM-DD or Nd ...
try:
    RadarClient("http://127.0.0.1:1", timeout=2).stats()
except RadarAPIError as exc:
    print(exc.status, exc.message)                   # 0 [Errno 61] Connection refused (http://127.0.0.1:1/stats)
```

Into pandas, if you have it:

```python
import pandas as pd
df = pd.DataFrame(radar.instruments(since="90d", limit=500)["results"])
df[["entity_name", "vendor", "papers", "mentions"]].head(20)
```

Method reference: `search(q, since=None, sections=(), limit=50, mentions=True)`, `paper(id)`,
`entity_papers(kind, id, since=None, limit=100)`, `instruments(since, until, limit)`,
`companies(...)`, `archive_instruments(...)`, `top(since, until, limit, full=False)`, `news(since, company, limit)`,
`stats()`, `endpoints()`, `digest_latest()`. Every method returns the decoded JSON (a `dict`) except
`digest_latest()`, which returns the Markdown string. Connection failures (server down, DNS, timeout)
raise `RadarAPIError` with `status == 0`, so one `except` clause covers both kinds of failure.

## 5. Datasette

```bash
pip install 'lit-radar[serve]'       # adds datasette>=0.64
litradar serve --datasette           # = datasette -i data/radar.sqlite -m datasette/metadata.yaml --setting sql_time_limit_ms 5000 --port 8001
```

Then:

- `/radar` lists tables and the canned queries.
- `/radar/search?query=methods:Xenium` is full-text search (FTS5 syntax as above, taken verbatim; quote
  phrases and hyphenated terms yourself; `NOT` is binary, there is no `AND NOT`).
- `since` / `until` parameters of the canned queries are calendar dates, `YYYY-MM-DD`; a blank parameter
  is an open bound. The `Nd` shorthand is a CLI/API convenience that SQL does not understand (it would
  compare `pub_date >= '90d'` and return nothing).
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
litradar search "Xenium" --sort score                    # by stored rank score; --sort date = newest first; default = relevance
litradar search "Visium" --keep-duplicates               # keep every dataset version instead of folding repeated titles
litradar search "liquid biopsy" --since 30d              # plain text = phrase (see above); --json adds each paper's score
litradar search "liquid biopsy" --raw-fts                # verbatim FTS5: AND of the stemmed words
litradar search "Illumina" --in coi,funding --since 30d
litradar entity pacb-revio --since 180d                  # distinct papers; "N papers mention ... (showing k)" when --limit cuts the list
litradar entities --kind instrument --grep nanopore      # gazetteer ids for /entities/... and canned queries
litradar stats [--json]                                  # --json: one object {counts, runs}
```

`--since` / `--until` are validated everywhere (`YYYY-MM-DD` or `Nd`; anything else is a usage error, exit 2,
never a silent empty result), `--limit` must be at least 1, and output piped into `head` ends quietly.

## 7. The static search page (GitHub Pages)

The three ways above need a running process. The dashboard site has none, so it carries its own
search engine: `litradar dashboard --out site/` writes, next to the trend pages,

| File | Content |
|---|---|
| `search.html` | the page: query box, facets, sort, result rows; plain JavaScript in the dashboard's shared shell |
| `search-index.json` | one compact record per paper in the look-back window (default 180 days), best rank score first |
| `search-entities.json` | instrument, company, subgroup, source, tier, kind and country vocabulary with per-value paper counts |

`litradar search-index --out site/ [--days 180] [--max 20000] [--max-bytes 4000000] [--db PATH] [--json]`
rebuilds just those three files (a wider window, a different cap, a look at the sizes). The database
is opened read-only, as for the dashboard.

**How a query matches.** The page builds a prefix-token index in the browser from the records:
every keystroke is split into words, each word matches as a *prefix* of indexed words (`xen` finds
Xenium, `novaseq` finds NovaSeq X), and a paper must match every word (AND). Fields are weighted —
title 3, instrument and company names 2.5, subgroups 2, venue 1.5, abstract excerpt 1 — and an
exact word outranks a prefix. Matching is case- and accent-insensitive. There is no FTS5 syntax
here: no quotes, no `OR`, no column filters; for those use the CLI, the JSON API or Datasette.

**Facets and sort.** Instruments, companies, subgroups, journal tier (A / B / C as the ranker
recorded it, preprint, unranked venue), kind, source kind (bioRxiv, Crossref, Europe PMC, RSS, ...),
country of affiliation when the store carries one, and a date range. Facet counts are computed over
the current result list; several values inside one facet are OR-ed, facets are AND-ed. Sort by
relevance (default while a query is typed), rank score, date, or citations when any record carries
a count. Clicking a chip under a result adds that entity as a filter; the arrow next to a chip opens
the entity's trend card where the dashboard has one.

**Shareable searches.** The state lives in the URL fragment (`search.html#q=xenium&in=txg-xenium&sort=date`),
written as you type and read back on load, so a result list can be pasted into a message.

**What the index holds, and what it does not.** Per paper: id, title, venue, date, DOI, URL (only
when it is not the DOI resolver), kind, source kind, tier, rank score, citation count when stored,
subgroups, instrument and company ids, country codes, and an excerpt of the stored **abstract** of
at most 240 characters. The index never reads parsed sections or the full-text body, and no
excerpt is written for a paper whose data-policy level is `none` (see `docs/LEGAL.md` and
`litradar/policy.py`: metadata and abstracts are storable at every level, full text is not; the
digest's 420-character rule is the precedent for excerpts). Everything from the data files is
written into the page with `textContent`; links are made only from `http(s)` URLs, DOIs and
sibling `.html` pages.

**Size.** Records are ranked by score then date, capped at `--max` records (20,000) and then at
`--max-bytes` raw bytes (4,000,000) by dropping the tail of the ranking; the page says how many
papers in the window are not indexed. A one-week corpus of 3,562 papers gave a 1.79 MB index
(about 500 bytes per record, 74% of them with an excerpt) and a 43 KB vocabulary; at that density
the byte budget binds near 7,900 records. The files are fetched by the page, not embedded, so a
local copy must be served over HTTP (`python3 -m http.server -d site 8000`); the page says so when
the fetch fails from a `file://` URL.
