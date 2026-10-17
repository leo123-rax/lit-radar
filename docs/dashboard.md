# The trends dashboard

The weekly digest answers *what happened last week*. Once the store has been running for a while it
can answer a different question — *what is happening over time* — and that one wants charts, not
prose. `litradar dashboard` reads the SQLite store and writes a **self-contained static site**: six
HTML pages, a stylesheet, one script, and the data as JSON. No server, no build step, no framework.

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
| `--max-papers` | `4000` | cap on the client-side search index (most recent papers in the window) |
| `--json` | off | print the full file manifest, with byte sizes, as JSON |

The store is never written to. The module opens it with `file:…?mode=ro`; if the file is in WAL mode
and a read-only open is refused (a read-only connection cannot create the `-shm` companion), it
copies the database and its sidecars to a temporary directory, reads the copy, and deletes it.

## The pages

| Page | What it shows |
|---|---|
| `index.html` | papers per week; instruments named per week by vendor; archive experiments per week by vendor; top movers (instruments by papers, instruments by experiments, companies by mentions); monthly series when the optional `timeseries` table exists |
| `instruments.html` | one card per instrument: papers naming it per week **and** archive experiments per week, in two separate charts, plus its most recent papers |
| `companies.html` | one card per company: mentions per week split by role — author affiliation, funding / competing-interest statement, subject of the work — with the stored news count |
| `subgroups.html` | papers per week per research subgroup |
| `countries.html` | papers per week by country of first affiliation, or an explanation of how to enable it |
| `search.html` | client-side search over the window: title, venue, date, instruments, companies, link |

Every chart ships a **data table** below it (`<details>` on the overview and entity pages, open on
the movers tables), so no value is reachable only by hovering, and the pages stay useful with
JavaScript charts disabled or on a screen reader.

## What it reads

All of these are optional. A missing table or column empties one section; it never fails the build.

| Table | Used for |
|---|---|
| `papers` | papers per week, the search index, subgroup and country assignment from `extra_json` |
| `mentions` | instruments and companies per week, by vendor and by role |
| `scores` | the `final` score shown in the search index |
| `archive_records` | experiments per week, by vendor and per instrument entity |
| `news_items` | per-company news counts (matched on the stored company id, else the display name) |
| `tracked_author_hits` | subgroup counts |
| `timeseries` *(optional)* | monthly pre-aggregated counts: `source, entity_id, model, month, count` |

**Week buckets** are ISO weeks keyed by the Monday, in UTC. A paper is bucketed by `pub_date`,
falling back to the date part of `first_seen` for feeds that carry no publication date — the same
rule the digest uses, so the two never disagree.

**Top movers** compare the last four complete weeks against the four before them, and report the
raw counts beside the change. A move from zero has no percentage: it is shown as `n/a`, never as an
infinite or invented growth rate.

**Vendor labels in the archive chart.** Records whose declared instrument model is not in the
gazetteer carry no vendor, so the archive's own `instrument_platform` value stands in. That
vocabulary is upper-case (`ILLUMINA`) where the gazetteer is mixed-case (`Illumina`), so labels are
merged case-insensitively with the mixed-case spelling winning. Platform names that are *different
words* for the same vendor (a sequencer platform code versus the company name) still appear as two
series; that is a gazetteer gap, and adding the model to `config/instruments.yaml` fixes it at the
source rather than in the dashboard.

### Enabling the countries page

Neither of these is written by the base pipeline, which is why the page ships with an explanation
instead of a chart:

1. a `country` column on `papers` — an adapter may add one with `ALTER TABLE papers ADD COLUMN
   country TEXT` after the schema has been created; or
2. a `country` string (or a `countries` list, first element used) in each paper's `extra_json`.

Store ISO 3166-1 alpha-2 codes, derived from the first author's affiliation or from an institution
record. The page fills in on the next build with no configuration.

### Enabling the subgroups page

Counts come from `tracked_author_hits` (configure authors in `config/subgroups.yaml`) and, when a
paper's `extra_json` carries `subgroups: ["single_cell", …]` or `subgroup: "single_cell"`, from that
keyword assignment. Either source alone is enough.

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

## Size

The site is data-dependent. For a one-week corpus of ~3,500 papers, ~3,800 mentions and ~123,000
archive records, a 52-week build came to **2.8 MiB in 15 files**: `index.html` 11 KB,
`instruments.html` 126 KB, `companies.html` 41 KB, `subgroups.html` 4 KB, `countries.html` 3 KB,
`search.html` 1.27 MB, `assets/app.js` 20 KB, `assets/styles.css` 5 KB, and the same payloads again
as `data/*.json`.

`search.html` dominates, because the search index is embedded in the page (which keeps the site
working from a `file://` path, where `fetch()` of a sibling JSON file is blocked by the browser).
The same index is written to `data/papers.json` for anything else that wants it. Lower
`--max-papers` for a smaller page; the cap takes the most recent papers in the window.

## Publishing to GitHub Pages

`.github/workflows/pages.yml` builds and deploys the site.

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.** (Not "Deploy from a
   branch" — nothing is committed to the repository; the site is uploaded as an artifact.)
2. Run `weekly-digest` at least once, so there is a cached database to build from.
3. The `pages` workflow then runs automatically whenever `weekly-digest` **completes successfully**
   (`workflow_run`), and can be run by hand from the Actions tab (`workflow_dispatch`, with an
   optional `weeks` input).

The workflow restores the database from the same Actions cache the digest workflow writes
(`restore-keys: radar-db-`), runs `litradar dashboard --db data/radar.sqlite --out site`, then
uploads and deploys with `actions/upload-pages-artifact` and `actions/deploy-pages`. It needs
`pages: write` and `id-token: write`, which are declared in the workflow. If no cached database is
found — a fresh fork that has not run the digest yet — the workflow posts a notice and finishes
green without deploying, rather than failing.

Notes and limits:

* `workflow_run` triggers only fire for workflow files on the **default branch**; a pages workflow
  on a feature branch will not be started by a digest run.
* Actions caches are branch-scoped: the digest and the pages workflow must run on the same branch
  (the default branch) for the restore to hit.
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
optional `timeseries` table, the series-folding rule, and a check that the source database is
byte-identical after a build.
