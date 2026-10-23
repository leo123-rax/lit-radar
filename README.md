# lit-radar

[![ci](https://github.com/leo123-rax/lit-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/leo123-rax/lit-radar/actions/workflows/ci.yml) [![weekly digest](https://github.com/leo123-rax/lit-radar/actions/workflows/weekly.yml/badge.svg)](https://github.com/leo123-rax/lit-radar/actions/workflows/weekly.yml) [![dashboard](https://img.shields.io/badge/dashboard-GitHub%20Pages-blue)](https://leo123-rax.github.io/lit-radar/) [![latest digest](https://img.shields.io/badge/digest-latest-success)](digests/latest.md) [![licence: MIT](https://img.shields.io/badge/licence-MIT-lightgrey)](LICENSE)

A weekly **science-literature radar** for people who follow life-science tools, genomics,
diagnostics and biotech. Every week it:

1. **Pulls every new paper and preprint** from the journals you care about (Nature, Science, Cell,
   NEJM, Nature Methods, Genome Biology ... by ISSN), bioRxiv/medRxiv, arXiv q-bio, and open-access
   full text from Europe PMC.
2. **Finds the instruments in the Methods**: which papers ran a NovaSeq X, a Chromium, a Xenium,
   a Revio, a PromethION, an Orbitrap Astral, an Olink panel ... using a curated gazetteer of ~200
   platforms with the spellings that actually appear in Methods sections.
3. **Counts the machines behind newly public data**: NCBI SRA and ENA record the declared
   `instrument_model` for every sequencing experiment released, so you get a weekly census of what
   sequencers are producing data, independent of what gets written up.
4. **Maps everything to companies**: vendor tickers behind each instrument, company-affiliated
   authors (e-mail domains, OpenAlex institution typing), companies named in funding and
   competing-interest statements, plus company news (GDELT per-company queries, SEC 8-K filings,
   press wires; Google News RSS only if you opt in).
5. **Tracks researchers by subgroup** (single-cell, spatial, long-read, proteomics, liquid biopsy,
   perturbation screens, AI-bio, gene editing, structural biology, synthetic biology).
6. **Ranks "most interesting"** with a transparent heuristic, optionally blended with a Claude
   rubric score, and renders a Markdown digest you can e-mail, post to Slack, or just read on GitHub.
7. **Stores it all in SQLite with full-text search**, so you can ask
   `litradar search "Xenium" --since 90d` or `litradar entity pacb-revio --since 180d`.

Everything is a plain Python package with four small dependencies (`requests`, `PyYAML`, `lxml`,
`cssselect`). No servers, no accounts required to start. Fork it, add your own keys in `.env`, and
run it on a laptop or let the included GitHub Action run it every Monday (the fork checklist, with
time estimates and what a zero-key copy produces, is `docs/FORKING.md`).

## Quick start

Python 3.9 or newer with a modern pip. The package is `pyproject.toml`-only, so an editable install
needs pip >= 21.3; the pip that ships with macOS's command-line-tools Python (21.2.4) fails with
"editable mode currently requires a setuptools-based build" until you upgrade it, which is why the
upgrade line is in the snippet. (`python -m pip install .` without `-e` works on any pip.)

```bash
git clone https://github.com/<you>/lit-radar && cd lit-radar
python3 -m venv .venv && source .venv/bin/activate   # python3: stock macOS has no `python`
python -m pip install --upgrade pip                   # pip >= 21.3 for a pyproject-only editable install
python -m pip install -e .
cp .env.example .env          # optional: add keys (all optional; see below)
python -m litradar check-config
python -m litradar run --days 7      # 10-30 minutes on first run; the digest lands in digests/
python -m litradar search "spatial transcriptomics" --since 30d
python -m litradar entity txg-xenium --since 90d
python -m litradar trends --print   # what changed vs last week and the 13-week baseline
```

Trends (risers, fallers, new entities, preprint-to-journal conversions) are described in
`docs/trends.md`; the section becomes informative from the second weekly run.

Optional extras: `python -m pip install -e ".[llm]"` for Claude scoring, `python -m pip install markdown`
for HTML e-mail. The install also puts a `litradar` console script on the virtualenv's PATH, so
`litradar search ...` and `python -m litradar search ...` are the same command; the docs use both.

## What you need (and what you don't)

| Key | Needed for | Cost | Where |
|---|---|---|---|
| `LITRADAR_CONTACT_EMAIL` | polite-pool rate limits at OpenAlex, Crossref, Unpaywall; SEC EDGAR 8-K feeds are skipped without a contact | free | any address you own |
| `OPENALEX_API_KEY` | OpenAlex works/authors/institutions. **Effectively required:** anonymous traffic shares a tiny per-IP daily budget that runs out mid-pull | free | https://help.openalex.org/api/authentication/ |
| `NCBI_API_KEY` | lifts E-utilities from 3 to 10 requests/s (SRA and GEO census, PubMed) | free | NCBI account settings |
| `OPENFDA_API_KEY` | 510(k) / PMA source: 1,000 requests/day per IP without a key, 120,000/day with one | free | https://open.fda.gov/apis/authentication/ |
| `SEMANTIC_SCHOLAR_API_KEY` | citation enrichment (1 request/s with a key) | free | semanticscholar.org/product/api |
| `ANTHROPIC_API_KEY` | `--llm` rubric scoring of the weekly shortlist | pay-as-you-go (cost table in `litradar/rank/llm.py`; set a spend limit first) | the provider's console |
| SMTP / Slack | delivery | free tiers exist | your mail provider / Slack app |

Everything is optional; the pipeline runs with no keys at all. Copy `.env.example` to `.env` locally,
or add the same names as repository secrets on GitHub Actions (`docs/deploy.md`).

You do **not** need publisher logins, and `.env.example` has no slot for one. A personal journal
subscription is for *reading* the items the digest links to; publisher terms allow access only
through a normal web browser and grant no automated-download or text-mining rights, so the pipeline
never logs in anywhere. Full text is read only from open-licensed sources (the Europe PMC open-access
subset, preprints under open licences via their official API). The policy, the per-source terms and
how the code enforces them are in `docs/LEGAL.md`; the short version is `docs/ACCEPTABLE_USE.md`.

## What the digest contains

* **Top innovations**: ranked papers with venue, score and the reasons behind the score (journal
  tier, platforms and lab tools named, company mentions, company-affiliated author, watchlist
  terms, tracked author, novelty cues), the instruments and companies detected, and (with `--llm`)
  a one-line summary, why it matters, and novelty / commercial / hype flags.
* **Platforms named in this week's papers**: the instruments an investor tracks (sequencers,
  single-cell, spatial, proteomics, cryo-EM, arrays, synthesis, cell engineering) with vendor,
  ticker, paper count and an example; **lab infrastructure and consumables** (QC, library prep,
  PCR, automation, imaging, cytometry, bioprocess) in a separate table so they do not crowd it.
* **Instruments behind newly public sequencing data**: NCBI SRA + ENA experiments by declared
  instrument model, vendor share and library strategies, each row with the share its single largest
  study supplies (named at 50 % or more, with a footnote), so one 35,000-experiment submission is read
  as that study and not as a fleet-wide shift. These count experiments, not machines,
  and lag the experiment by months. The study-level GEO census (Series × platform) sits in the same
  table, flagged as studies; proteomics datasets per declared mass spectrometer (PRIDE) and
  single-cell / spatial assays (CELLxGENE) get their own sub-tables; native Chinese experiments from
  NGDC GSA and GSA-Human join the census when the run pulled them.
* **Companies in the literature**: how each company appears (as author, as funder or
  competing-interest disclosure, as subject, as supplier of a named instrument), typed so
  institutes and pharma disclosures are not read as vendors; first-time appearances by publication
  date, labelled with the channel they came from, and withheld for a channel (parsed Methods or COI
  text) whose own history is shorter than the comparison window.
* **Tracked researchers**: new papers and preprints by the people in `config/subgroups.yaml`,
  grouped by subgroup; name-only matches are marked provisional until an OpenAlex id is set.
* **Trade press & company news**: grouped by company, from the GDELT DOC 2.0 API (one query per
  watched company, built for programmatic use; it allows one request every 5 seconds, so a run that
  gets a 429 backs off and, if the 429 persists, skips the remaining companies and says so in the
  digest's error list), SEC 8-K filings, press wires and the trade-press feeds that allow scripted
  access. Google News RSS is **opt-in and off by default** (`news.google_news: false` in
  `config/radar.yaml`): its `robots.txt` disallows the `/rss/` path, so turning it on is the
  operator's decision (see `docs/LEGAL.md`).
* **Coverage and method**: exactly what was pulled per source, how many items had full text, how
  many were scored by the model, what failed, and database totals.

A real run's output is in `digests/`. The design behind each section is in `docs/DESIGN.md`.

## Command reference

Every command reads `config/` and `.env` from the repo root; `python -m litradar <command> --help` shows all flags.

| Command | What it does |
|---|---|
| `run --days 7 [--llm]` | full weekly pass: fetch, full text, extract, census, news, signals, rank, digest |
| `update [--overlap-days 2]` | incremental daily pass from the last run's end (no digest); `status` reports freshness and exits non-zero when stale |
| `digest --days 7` | re-render the digest from the database (all sections, no fetching) |
| `search "query" [--since 90d] [--in methods,coi,mentions] [--raw-fts]` · `entity <id>` · `show <id|name>` · `watch add|run` | search and entity dossiers over the corpus. A plain multi-word query is searched as one **phrase** (`search "minimal residual disease"` = those words adjacent, in order); `--raw-fts` sends the text to FTS5 as typed, so `search --raw-fts "minimal residual disease"` is the AND of three stemmed words, and FTS5 syntax (`methods:Xenium`, `Xenium OR Visium`, `NovaSeq*`) always passes through. A plain search also matches the `mentions` column: entities the gazetteer attributed to a paper under any alias. Syntax: `docs/search.md` |
| `serve [--datasette]` | read-only JSON API + search page on localhost; or launch Datasette with the bundled metadata |
| `add <doi|pmid|pmcid|arxiv|url> [--tag x]` · `tags` | add papers to the corpus by identifier |
| `track company|instrument|author|topic|term ...` · `untrack` · `prioritize <kind> <target> --weight W` | edit what is tracked and weighted without hand-editing YAML |
| `coverage --days 7` | where coverage is thin: sources, journals with zero items, watched entities with zero mentions |
| `concepts [--weeks 26] [--llm]` | emerging concepts: phrase bursts in titles, abstracts and Methods |
| `candidates [refresh|promote|ignore]` | companies spotted in the literature but not yet tracked, with evidence |
| `timeseries --entity pacb-revio --from 2023-01` | monthly SRA / ENA / Europe PMC counts per instrument (CSV, Markdown, optional PNG) |
| `trends` | week-over-week and 13-week-baseline movers for instruments, companies, subgroups, journals |
| `dashboard --out site/` | static trends site (Chart.js) for GitHub Pages |
| `backfill --from 2018-01 --to 2026-10` | grow the corpus from OpenAlex by topic and month (resumable; snapshot mode too) |
| `tag [--translate]` · `export --parquet DIR` · `rematch` · `check-config` · `stats` | language/country tags, exports, re-annotation after gazetteer edits, validation |
| `fetch <source> ...` · `fetch --list` · `browser-check` | raw access to every source client (24, one per adapter the pipeline runs, SEC 8-K and GDELT included) as JSON lines; the optional real-browser fetcher |
| `login nature\|science` · `read <doi>` · `read-queue [--limit 20]` · `reads-import` | **optional, local only**: read the paywalled papers *your own* subscription covers. You sign in yourself in a real browser window; `read-queue` then reads the day's best candidates unattended (capped at 20/day, 20 s apart) and commits **facts only** to `reads/<date>.jsonl` — sections present, Methods length, instrument and company mentions, never a sentence of text. `reads-import` folds those facts into the database on CI, which never signs in anywhere. `docs/reading-assist.md` |

Full docs: `docs/FORKING.md` (fork checklist), `docs/deploy.md` (GitHub Actions, Pages, laptop scheduling),
`docs/DESIGN.md` (architecture and cost model), `docs/SOURCES.md` (every source, verified), `docs/COOKBOOK.md`
(copy-paste recipes), `docs/search.md`, `docs/live.md`, `docs/track.md`, `docs/concepts.md`, `docs/discovery.md`,
`docs/dashboard.md`, `docs/gazetteer.md`, `docs/LEGAL.md`, `docs/SCALE.md`, `docs/ROADMAP.md`.

## Configuration

Everything lives in `config/` and is meant to be edited:

* `radar.yaml`: sources on/off, look-back window, watchlist terms, ranking weights, digest size.
* `instruments.yaml`: the instrument gazetteer. Each entry has aliases as they appear in Methods,
  the vendor and ticker, launch year, and context rules for ambiguous names
  (`Chromium` the element vs the 10x instrument, `G4` the quadruplex vs the sequencer).
* `companies.yaml`: company names, subsidiaries, tickers, e-mail domains; `watch_news: true` adds a
  GDELT news query (plus a Google News query only when `news.google_news` is on) and, for US
  tickers, an SEC 8-K feed.
* `subgroups.yaml`: research subgroups, their keywords, OpenAlex topic ids and tracked authors.
* `journals.yaml`: journals by ISSN with tier and RSS; publisher subject feeds.
* `feeds.yaml`: trade press and press-wire feeds.

`python -m litradar check-config` validates the YAML and reports duplicate ids and alias collisions.

## Scheduling

* **GitHub Actions** (recommended for forks): `.github/workflows/weekly.yml` runs every Monday. The
  job that holds your API keys can only read the repository; a second job with no keys stores the
  SQLite database as a GitHub release asset (`db-latest`, plus a `db-YYYY-MM` copy per month for
  roll-back) and commits the digest to `digests/`. Forks do not commit digests unless the repository
  variable `LITRADAR_COMMIT_DIGESTS=true` is set, because a committed digest reveals your watchlist.
  *Run workflow* also takes a `timeseries_vendor` input to produce a vendor time series on demand.
  Two more workflows: `daily.yml` extends the corpus every day at 06:20 UTC (`litradar update`, no
  digest) and `pages.yml` rebuilds the dashboard from the `db-latest` snapshot after each weekly
  run. The numbered fork checklist (what to click on GitHub, which secret names, how long each step
  takes): `docs/FORKING.md`. The full reference, including Pages for the dashboard: `docs/deploy.md`;
  live mode: `docs/live.md`.
* **Laptop or server**: `cron` or `launchd` calling `python -m litradar run --email`; examples in
  `docs/examples/`. Windows: Task Scheduler with the same command.

## Getting the digest

No mail server needed. After each weekly run the workflow opens a GitHub Issue titled
`Weekly digest YYYY-MM-DD` (label `digest`) whose body is the digest itself, and closes the previous
week's issue with a link to the new one; re-running a date updates the same issue. To receive it by
e-mail, open the repository, click **Watch -> Custom -> Issues**: GitHub then mails every digest to
you with no setup on your side. A fork gets the same for free once Issues are enabled in its
settings and digests are committed (`LITRADAR_COMMIT_DIGESTS=true`). The Pages workflow also
publishes an Atom feed of the last 12 digests at `https://<owner>.github.io/<repository>/feed.xml`
for any feed reader. E-mail (SMTP) and Slack remain as before (`run --email`, `run --slack`);
`python -m litradar deliver --status` shows which channels are configured. Details: `docs/delivery.md`.

## Security and privacy

* **Keys live in `.env` (git-ignored) or in GitHub Actions secrets, nowhere else**: never in
  `config/*.yaml`, never in a digest, never in an issue. CI fails if a database, WAL file, snapshot or
  `.env` is ever tracked; every workflow action is pinned to a full commit SHA and every
  `workflow_dispatch` input reaches the shell only through an environment variable and is validated.
  The first step of each scheduled job prints which optional secrets are present (names only, never a
  value) so a fork owner can see what is missing. Reporting and key rotation: `SECURITY.md`.
* **What a public repository exposes:** the committed digests (`digests/*.md`), the database snapshot
  in the `db-*` releases and the Pages dashboard all show which companies, instruments and people you
  watch. Options: keep a private duplicate instead of a public fork, leave `LITRADAR_COMMIT_DIGESTS`
  unset in a fork, or keep a personal watchlist in an overlay directory via `LITRADAR_CONFIG_DIR` and
  ship only the generic lists.
* **What the data is:** harvested public metadata, abstracts and open-licensed text, each record with
  its licence string; no credentials, no paywalled text, no logins. The MIT licence covers the code
  and the gazetteers only; third-party content keeps its own terms (`NOTICE`, `docs/LEGAL.md`).

## How ranking works

The heuristic is deliberately legible (ranking v2, `litradar/rank/heuristics.py::DEFAULT_WEIGHTS`):
a venue prior (journal tier A = 5.0, B = 3.0, C = 1.0, preprint = 1.0, unranked = 0.5; the A/B prior
is withheld when no topical signal fires), +1.0 for the first platform named (further platforms are
listed, not scored), +1.0 when a platform is named in the title or abstract, +0.2 per lab tool
(cap 3), +0.5 for the first commercial company named, +1.0 for a company-affiliated author, +1.0 per
watchlist term (cap 3), +3.0 for a tracked author matched by OpenAlex id (+1.5 by name only), +0.5
per novelty cue (cap 1.0), +1.5 when the text says the paper introduces a platform / method / assay,
age-aware citation points (cap 2.0), +1.0 for a preprint that has since been published, +1.0 for a
news co-mention, +1.0 for a concept's first appearance in the corpus, and −4.0 for correction /
retraction notices and for records that are not research papers (letters to the editor, editorials,
dataset / software deposits, dissertations), which also lose their venue prior. The raw sum is
scaled to 0-10 (16 raw points = 10) and every score carries a reasons string shown in the digest.
Weights are overridable in `config/radar.yaml` under `ranking.weights`; `docs/ranking.md` section 4
has the full table and the topical gate.

With `--llm`, the shortlist (default: heuristic >= 3, max 300 papers) is scored by Claude against a
fixed rubric (novelty, commercial implication, instrument role, clinical readiness, hype risk,
watchlist relevance, plus a one-line summary and the companies and instruments implicated) using
structured JSON output and the Message Batches API (50% off list price). The model sees title,
abstract, venue and the matcher's hits, never stored full text. The final score is
0.4 x heuristic + 0.6 x LLM composite. Cost in the current single-stage form is about $2 a week
for 300 abstracts on Claude Opus 5.5 (about $1 through Batches; cents on Haiku 5.5); the
recommended two-stage cascade (Haiku triage of every candidate, Opus deep scoring of the top ~250
with Methods text) is modelled at about $5.86 a week, roughly $305 a year. See `litradar/rank/`,
`docs/ranking.md` (the shipped heuristic, section 4) and `docs/DESIGN.md` section 8 (the LLM rubric
and the cost model).

## Data sources at a glance

| Source | What we take | Access | Status |
|---|---|---|---|
| bioRxiv / medRxiv API | all new preprint versions, abstracts, licence, published-DOI link (30 records per page; DOIs now mint under prefix 10.64898) | free, no key | wired |
| Europe PMC | new articles by ISSN; open-access full text (Methods, funding, COI); section-level search (`METHODS:`, `COMP_INT:`, `ACK_FUND:`) | free, no key | wired |
| Crossref | DOI-level journal feeds the day of registration; abstracts where deposited (0% Elsevier, 10-23% Nature-branded, 85-99% Science/PNAS/eLife/AACR) | free, `mailto` | wired |
| OpenAlex | topic feeds, company-typed institutions, tracked authors, abstracts | **free key required** (anonymous traffic shares a $0.10/day budget per network; a key gives $1/day) | wired |
| arXiv | q-bio categories | free, 3 s between requests | wired |
| NCBI SRA (E-utilities) | weekly census of sequencing experiments with declared instrument model (~121k experiments a week) | free; key lifts 3 to 10 req/s | wired |
| ENA portal API | ERX/DRX records with `instrument_model` and `first_public` (the ENA/DDBJ-native complement to SRA) | free | wired |
| Journal / subject RSS | tables of contents as a backstop | free | wired |
| SEC EDGAR Atom, PR Newswire, STAT, BioPharma Dive, MedTech Dive | company filings and trade-press headlines | free (EDGAR requires a `<tool> <contact e-mail>` User-Agent, sent from `LITRADAR_CONTACT_EMAIL`) | wired; the EDGAR feeds are skipped while no contact is configured |
| GDELT DOC 2.0 API | per-company news headlines and links (`"<company>" + <type clause>`, `sourcelang:english`) | free, no key; one request every 5 s, so ~75 companies take ~7 minutes | wired, on by default (`news.gdelt`); a persistent 429 skips the remaining companies and is listed in the digest's errors |
| Google News RSS | headlines for outlets and companies with no scriptable feed | free | wired but **opt-in, off by default** (`news.google_news: false`): `robots.txt` disallows `/rss/` |
| NCBI GEO | newly released Series and the platforms they ran on (study-level census) | free; key lifts rate | wired |
| PRIDE Archive | proteomics datasets per declared mass spectrometer | free | wired |
| CZ CELLxGENE Discover | single-cell and spatial assays behind newly public datasets | free | wired |
| ClinicalTrials.gov v2 | newly posted studies naming watched companies or assays | free | wired |
| NIH RePORTER v2 | S10 shared-instrumentation awards, SBIR/STTR, keyword awards | free | wired |
| openFDA device | 510(k) clearances, De Novo grants, PMA decisions | free; key lifts daily quota | wired |
| Semantic Scholar | citation counts and TL;DRs for stored papers | free key (required in practice) | wired |

Full notes, including what did *not* work (several publisher and investor-relations feeds block
scripts; bioRxiv full-text XML is rate-limited for non-browser clients; the OpenAlex anonymous
budget runs out), are in `docs/SOURCES.md`; the reasoning is in `docs/DESIGN.md`.

## Contributing

See `CONTRIBUTING.md`. The highest-value contributions are gazetteer entries and aliases, context
terms for ambiguous names, OpenAlex ids for tracked researchers, and new source adapters with
offline tests.

## License

MIT. The gazetteers are data about publicly marketed products and public companies; they contain no
proprietary information.
