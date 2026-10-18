# The trends dashboard

The weekly digest answers *what happened last week*. Once the store has been running for a while it
can answer a different question — *what is happening over time* — and that one wants charts, not
prose. `litradar dashboard` reads the SQLite store and writes a **self-contained static site**: seven
HTML pages, a stylesheet, one script, the data as JSON, and a client-side search engine (its index
and vocabulary as two more JSON files). No server, no build step, no framework.

```bash
litradar dashboard --out site/            # 52 weeks by default
litradar dashboard --out site/ --weeks 26 --db data/radar.sqlite
python3 -m http.server -d site 8000       # look at it locally
```

| Flag | Default | What it does |
|---|---|---|
| `--out` | `site` | output directory (created if missing) |
| `--weeks` | `52` | weeks of history to chart; the window ends with the current ISO week |
| `--db` | the configured store | database to read; **always opened read-only** |
| `--max-papers` | `4000` | cap on `data/papers.json` (most recent papers in the charted window; the search page has its own index, see below) |
| `--json` | off | print the full file manifest, with byte sizes, as JSON |

The search page's index is built by the same command with its own defaults (180 days, 20,000
records, a 4,000,000-byte budget, best rank score first). To change them, or to rebuild only the
search files, run `litradar search-index --out site/ [--days N] [--max N] [--max-bytes N]`
afterwards; it writes `search.html`, `search-index.json` and `search-entities.json` and nothing else.

The store is never written to. The module opens it with `file:…?mode=ro`; if the file is in WAL mode
and a read-only open is refused (a read-only connection cannot create the `-shm` companion), it
copies the database and its sidecars to a temporary directory, reads the copy, and deletes it.

## The pages

| Page | What it shows |
|---|---|
| `index.html` | papers per week; instruments named per week by vendor; archive experiments per week by vendor; top movers (instruments by papers, instruments by experiments, companies by mentions — ranked over **every** entity in the store, not only the carded ones); monthly series when the optional `timeseries` table exists |
| `instruments.html` | one card per instrument: papers naming it per week **and** archive experiments per week, in two separate charts, plus its most recent papers. Cards are the union of the 48 most-named instruments and the 48 with the most archive experiments, so a sequencer that archives see constantly but papers rarely name by model keeps its card; the lede states how many of the instruments with signal are shown |
| `timeseries.html` | the archive time series (`litradar timeseries --all`): one chart per vendor with its instrument models as monthly series — SRA experiments by default, switchable to ENA runs or Europe PMC papers, linear or log axis — the last two months shaded as incomplete, a dashed vendor total, a 12-month table per vendor, and links to the committed CSV and to `data/timeseries.json`. Details below |
| `companies.html` | one card per company: mentions per week split by role — author affiliation, funding / competing-interest statement, subject of the work — with the stored news count |
| `subgroups.html` | papers per week per research subgroup; every subgroup is in the table, the chart keeps the six largest and folds the rest into `Other` |
| `countries.html` | papers per week by country of first affiliation, or an explanation of how to enable it |
| `search.html` | client-side search over the last 180 days: instant word-prefix matching on title, abstract excerpt, venue and attributed entities; facets (instrument, company, subgroup, tier, kind, source, country, date range); sort by relevance, score, date or citations; shareable URL state. Reads `search-index.json` and `search-entities.json`, written by the same build; `litradar search-index` rebuilds just those. Details in `docs/search.md` section 7 |

Every chart ships a **data table** below it (`<details>` on the overview and entity pages, open on
the movers tables), so no value is reachable only by hovering, and the pages stay useful with
JavaScript charts disabled or on a screen reader.

## What it reads

All of these are optional, and *optional* means exactly one thing: a missing table or column
(SQLite's `no such table` / `no such column`). Such a query is skipped, logged, and listed in the
report as `skipped_queries` (the CLI prints each one as a warning). Every other database error — a
locked file, a corrupt image, a torn WAL copy — is raised, the command exits non-zero and nothing
is written, so a broken store can never publish an all-zero site as if it were news.

| Table | Used for |
|---|---|
| `papers` | papers per week, the search index (title, abstract excerpt, venue, DOI, URL, licence for the data-policy level), subgroup and country assignment from `extra_json` |
| `mentions` | instruments and companies per week, by vendor and by role; the entity ids and names on each search record |
| `scores` | the `final` score shown in the search index and its ranking; the journal tier the ranker recorded in `reasons` |
| `archive_records` | experiments per week, by vendor and per instrument entity |
| `news_items` | per-company news counts (matched on the stored company id, else the display name) |
| `tracked_author_hits` | subgroup counts |
| `timeseries` *(optional)* | monthly pre-aggregated counts: `source, entity_id, model, month, count` (`fetched_at` when present); the overview's monthly card and the whole of `timeseries.html` |

**Week buckets** are ISO weeks keyed by the Monday, in UTC. A paper is bucketed by `pub_date`,
falling back to the date part of `first_seen` for feeds that carry no publication date — the same
rule the digest uses, so the two never disagree.

**Top movers** compare the last four *complete* weeks against the four before them, and report the
raw counts beside the change. The axis ends with the ISO week containing the build date; unless
that is a Sunday the week is still in progress, so it is left out of the comparison and the page
says so ("The current, unfinished week is left out."). With fewer than eight complete weeks the
span shrinks (down to one week against one), and the table headers and the note always name the
exact weeks compared. The ranking covers every instrument and company with any signal in the
window, not only those with a card. A move from zero has no percentage: it is shown as `n/a`,
never as an infinite or invented growth rate.

**Vendor labels in the archive chart.** Records whose declared instrument model is not in the
gazetteer carry no vendor, so the archive's own `instrument_platform` value stands in. Where that
code is a known vendor's, it is translated to the gazetteer's spelling through a small map
(`PLATFORM_VENDORS`: `ILLUMINA`, `BGISEQ`/`DNBSEQ`, `PACBIO_SMRT`, `OXFORD_NANOPORE`,
`ION_TORRENT`, `ELEMENT`, `ULTIMA`), so a platform code and the company name are one stacked
series rather than two. Codes with no gazetteer vendor (`GENEMIND`, `GENAPSYS`, `ABI_SOLID`,
`LS454`, `CAPILLARY`) are left as they are — honest, if ugly — and remaining upper-case codes are
still merged case-insensitively with any mixed-case spelling. The map is only a display fix: the
records themselves still carry no `entity_id`, so adding the model to `config/instruments.yaml`
is what makes them count per instrument.

### The countries page

The page reads either of two places and fills in with no configuration when one of them is populated:

1. a `country` column on `papers` — an adapter may add one with `ALTER TABLE papers ADD COLUMN
   country TEXT` after the schema has been created; or
2. a `country` string (or a `countries` list, first element used) in each paper's `extra_json`.

The language/country tagger (`litradar/i18n.py`, `tag_store`, run at the end of every `litradar run`)
writes ISO 3166-1 alpha-2 codes into `extra_json.countries` (first author first) from institution
records or affiliation strings, so a store built by the pipeline carries the chart out of the box (the
hosted overview showed US 709 / CN 278 / GB 112 for one week). Only a store whose papers were never
tagged (an import that skipped the step, or a run that failed before it) shows the explanation instead
of a chart.

### Enabling the subgroups page

Counts come from `tracked_author_hits` (configure authors in `config/subgroups.yaml`) and, when a
paper's `extra_json` carries `subgroups: ["single_cell", …]` or `subgroup: "single_cell"`, from that
keyword assignment. Either source alone is enough.

## The time-series page

`timeseries.html` answers *which sequencers are being used over time*, from the `timeseries` table that
`litradar timeseries --all` fills (monthly counts per instrument model from NCBI SRA, ENA and Europe PMC;
method and caveats in `docs/sources/timeseries.md`). The weekly workflow refreshes that table before the
database snapshot is stored, so the published page follows the archives with a one-week cadence.

* **One card per vendor, its models as series.** Vendors appear in the order of `config/archive_models.yaml`,
  models in configuration order; names and vendors come from `config/instruments.yaml`, then
  `archive_models.yaml`, then the store's `mentions` table, then the raw id. An entity's rows are summed per
  month, which is exact whether the cache holds one row per entity (the current layout) or one per
  vocabulary string (the layout before 2026-10-18). A dashed grey line gives the vendor total when a vendor
  has more than one model.
* **Measure and axis are page-wide switches**: SRA experiments (default), ENA runs, Europe PMC papers;
  linear or logarithmic. Every chart and table re-renders on a change; a logarithmic axis has no zero, so a
  zero month is drawn as a gap and the page says so.
* **The last two months are shaded "incomplete (deposit lag)"** on every chart, marked `*` in the tables and
  named in the tooltip. Archives index deposits and papers for weeks after a month ends, and the weekly
  refresh re-fetches the current month and the two before it (`--refresh-months 2`), so those values will
  still grow. The count comes from `TIMESERIES_INCOMPLETE_MONTHS` in `litradar/dashboard.py`.
* **A 12-month table under each chart** (model × month for the selected measure, plus the total row),
  `null` months blank, numbers with thousands separators. The table scrolls inside its card at phone width;
  the page never scrolls sideways.
* **Downloads.** The page links the committed CSV by its repository path
  (`digests/timeseries/timeseries_<from>_<to>_all.csv`) and the page's own data as `data/timeseries.json`.
  The build copies that CSV into the site under the **same relative path**, so the link resolves both in a
  checkout and on the published site. `litradar dashboard` looks for it in `<out_dir>/timeseries`
  (`digests/timeseries` by default; the `_all` selection first, then the latest `--to`, then the widest
  range); the Python API takes `build(..., config_dir=..., timeseries_dir=...)`, and both are optional.
* **Nothing cached yet?** The page says so and tells you what to run — *run `litradar timeseries --all`
  first* — instead of rendering empty charts; a database from before the table existed behaves the same way.
* **Everything is escaped.** Model and vendor names are written through `html.escape` into the page and
  through the `<`/`>`/`&`-free JSON blob into the script; the script creates text nodes only.

`data/timeseries.json` is `{available, months, incomplete, sources[{id,label,unit,note}], vendors[{name,
slug, entities[{id, name, series{source: [count|null per month]}}]}], entities, series, rows, fetched_at,
csv, json, generated}`. For 27 models over 46 months (74 series) the page is 28 KB and the JSON 21 KB; the
copied CSV (3,404 rows) is 232 KB.

## Charting

Chart.js **v4.5.1**, pinned by exact version with a Subresource Integrity hash and
`crossorigin="anonymous"`, from cdnjs:

```
https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js
sha384-jb8JQMbMoBUzgWatfe6COACi2ljcDdZQ2OxczGA3bGNeWe+6DChMTBJemed7ZnvJ
```

Both the URL and the hash were verified live on 2026-10-17 (HTTP 200, 208,522 bytes). The identical
bytes are on jsdelivr at `https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js`, so
the URL in `litradar/dashboard.py` can be swapped without changing the integrity hash. Everything
else — the page script, the stylesheet, the data — is served from the site itself.

Chart choices worth knowing about, because they are deliberate:

* **No dual-axis charts.** Papers per week and archive experiments per week differ by orders of
  magnitude; sharing a second y-axis would invent a correlation. They are drawn as small multiples —
  two charts side by side on the instrument card — on one axis each.
* **A series cap with an honest tail.** Stacked charts keep the six largest series and sum the rest
  into a grey `Other`; a seventh and eighth hue would be indistinguishable under colour-vision
  deficiency.
* **Colour follows the entity.** Filtering the instrument or company list hides cards; it never
  re-colours what is left.
* **Light and dark are both selected**, each validated against its own surface, and the theme button
  cycles auto → light → dark, remembering the choice in `localStorage` (wrapped in `try/catch`, so a
  private window simply falls back to auto).
* **Axis labels are abbreviated (`10-12`), tooltips are not** (`Week of 2026-10-12`), so a
  52-week window spanning two calendar years is never ambiguous.
* **Only `http(s)` URLs become links.** Paper and news URLs come from feeds and archives the site
  does not control and the published page is public, so anything with another scheme
  (`javascript:`, `data:`, a bare DOI) is rendered as plain text. Everything else on the pages is
  written with `textContent`, never as HTML.

## Size

The site is data-dependent. For a one-week corpus of ~3,500 papers, ~4,000 mentions and ~123,000
archive records, a 52-week build came to **2.9 MiB in 15 files**: `index.html` 12 KB,
`instruments.html` 157 KB (75 cards), `companies.html` 41 KB, `subgroups.html` 6 KB,
`countries.html` 3 KB, `search.html` 1.28 MB, `assets/app.js` 23 KB, `assets/styles.css` 5 KB, and
the same payloads again as `data/*.json`. The build itself takes well under a second.

That figure predates the search engine. With it, the same corpus built to **3.5 MiB in 17 files**:
`search.html` is now a 28 KB page that fetches `search-index.json` (1.79 MB for 3,562 records,
about 500 bytes per record, 74% carrying an abstract excerpt) and `search-entities.json` (43 KB:
251 instruments, 127 companies, 9 subgroups). `data/papers.json` (1.3 MB at `--max-papers 4000`)
is still written for anything else that wants a plain list. Because the search files are fetched
rather than embedded, a local copy of the site must be served over HTTP (`python3 -m http.server
-d site 8000`); from a `file://` path the search page explains why it is empty while the chart
pages still work.

## Publishing to GitHub Pages

`.github/workflows/pages.yml` builds and deploys the site.

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.** (Not "Deploy from a
   branch" — nothing is committed to the repository; the site is uploaded as an artifact.)
2. Run `weekly-digest` at least once, so the `db-latest` release carries a database snapshot
   (`radar.sqlite.zst`) to build from.
3. The `pages` workflow then runs automatically whenever `weekly-digest` **completes successfully**
   (`workflow_run`), and can be run by hand from the Actions tab (`workflow_dispatch`, with an
   optional `weeks` input).

The workflow downloads `radar.sqlite.zst` from the repository's `db-latest` release (the state release
the weekly digest publishes; `gh release download db-latest --pattern 'radar.sqlite.zst'`), decompresses
it, runs `PRAGMA integrity_check`, then `litradar dashboard --db data/radar.sqlite --out site`, and
uploads and deploys with `actions/upload-pages-artifact` and `actions/deploy-pages`. It needs
`pages: write` and `id-token: write`, which are declared in the workflow. If no snapshot exists yet —
a fresh fork that has not run the digest — the workflow posts a notice and finishes green without
deploying, rather than failing. If the snapshot is unreadable the build step fails red (see *What it
reads*), and the previous deployment stays up. The `weeks` dispatch input reaches the shell through an
environment variable (`WEEKS`), never by interpolation into the command line.

Notes and limits:

* `workflow_run` triggers only fire for workflow files on the **default branch**; a pages workflow
  on a feature branch will not be started by a digest run.
* The `db-latest` release is repository-wide, so a manually dispatched pages run on a feature branch
  builds from the same snapshot the default branch publishes; there is no per-branch state to restore.
* The footer of every page names the build time; the counts are those of the snapshot the site was
  built from, not of a local checkout.
* The published site is **public if the repository is public**. The corpus is metadata, abstracts
  and open-licensed text (see `docs/LEGAL.md`), but check what your gazetteers and watchlist terms
  say about your interests before publishing, and keep the repository private if that matters —
  Pages supports private publishing on paid plans.
* GitHub Pages serves up to 1 GB per site with a soft 100 GB/month bandwidth limit; a build of a few
  megabytes is far inside both.

## Offline tests

`tests/test_dashboard_offline.py` seeds a temporary SQLite store through the normal `Store` API and
builds a site from it — no network, no live database. It covers a normal store, an empty one,
malformed `extra_json` and `companies_json`, unparseable dates, a window containing nothing, the
optional `timeseries` table, the series-folding rule, the instrument card cap (an archive-heavy
instrument with no paper mentions keeps its card and its place in the movers), the complete-weeks
rule for movers (Saturday versus Sunday builds), optional-versus-fatal database errors (a missing
column is skipped and counted; a syntax error or a corrupt file raises and writes nothing),
archive platform-code merging, subgroup folding, and a check that the source database is
byte-identical after a build. `tests/test_dashboard_search.py` covers the search index the same way:
record fields and ordering, the data-policy gate on abstract excerpts (never full text, nothing at
level `none`), markup stripping and escaping, the window, the record cap and byte budget, the
vocabulary with card-aware links, JSON validity, the page shell, the build hook and the
`search-index` command.
