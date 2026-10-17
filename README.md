# lit-radar

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
   competing-interest statements, plus company news (Google News, SEC 8-K filings, press wires).
5. **Tracks researchers by subgroup** (single-cell, spatial, long-read, proteomics, liquid biopsy,
   perturbation screens, AI-bio, gene editing, structural biology, synthetic biology).
6. **Ranks "most interesting"** with a transparent heuristic, optionally blended with a Claude
   rubric score, and renders a Markdown digest you can e-mail, post to Slack, or just read on GitHub.
7. **Stores it all in SQLite with full-text search**, so you can ask
   `litradar search "Xenium" --since 90d` or `litradar entity pacb-revio --since 180d`.

Everything is a plain Python package with three dependencies (`requests`, `PyYAML`, `lxml`).
No servers, no accounts required to start. Fork it, add your own keys in `.env`, and run it on a
laptop or let the included GitHub Action run it every Monday.

## Quick start

```bash
git clone https://github.com/<you>/lit-radar && cd lit-radar
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # optional: add keys (all optional; see below)
python -m litradar check-config
python -m litradar run --days 7      # 10-30 minutes on first run; the digest lands in digests/
python -m litradar search "spatial transcriptomics" --since 30d
python -m litradar entity txg-xenium --since 90d
python -m litradar trends --print   # what changed vs last week and the 13-week baseline
```

Trends (risers, fallers, new entities, preprint-to-journal conversions) are described in
`docs/trends.md`; the section becomes informative from the second weekly run.

Optional extras: `pip install -e ".[llm]"` for Claude scoring, `pip install markdown` for HTML e-mail.

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
  instrument model, vendor share and library strategies. These count experiments, not machines,
  and lag the experiment by months; study-level GEO, proteomics (PRIDE) and single-cell assay
  (CELLxGENE) counterparts are built and being wired in.
* **Companies in the literature**: how each company appears (as author, as funder or
  competing-interest disclosure, as subject, as supplier of a named instrument), typed so
  institutes and pharma disclosures are not read as vendors; first-time appearances.
* **Tracked researchers**: new papers and preprints by the people in `config/subgroups.yaml`,
  grouped by subgroup; name-only matches are marked provisional until an OpenAlex id is set.
* **Trade press & company news**: grouped by company, from the feeds that allow scripted access,
  SEC 8-K filings and press wires. Google News queries are still on by default in this version;
  the research verdict is to make them opt-in (see `docs/ROADMAP.md`).
* **Coverage and method**: exactly what was pulled per source, how many items had full text, how
  many were scored by the model, what failed, and database totals.

A real run's output is in `digests/`. The design behind each section is in `docs/DESIGN.md`.

## Configuration

Everything lives in `config/` and is meant to be edited:

* `radar.yaml`: sources on/off, look-back window, watchlist terms, ranking weights, digest size.
* `instruments.yaml`: the instrument gazetteer. Each entry has aliases as they appear in Methods,
  the vendor and ticker, launch year, and context rules for ambiguous names
  (`Chromium` the element vs the 10x instrument, `G4` the quadruplex vs the sequencer).
* `companies.yaml`: company names, subsidiaries, tickers, e-mail domains; `watch_news: true` adds a
  Google News query and (for US tickers) an SEC 8-K feed.
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
  Step by step, including Pages for the dashboard: `docs/deploy.md`.
* **Laptop or server**: `cron` or `launchd` calling `python -m litradar run --email`; examples in
  `docs/examples/`. Windows: Task Scheduler with the same command.

## Security and privacy

* **Keys live in `.env` (git-ignored) or in GitHub Actions secrets, nowhere else**: never in
  `config/*.yaml`, never in a digest, never in an issue. CI fails if a database, WAL file, snapshot or
  `.env` is ever tracked; workflow actions are pinned to commit SHAs and workflow inputs reach the
  shell only through environment variables. Reporting and key rotation: `SECURITY.md`.
* **What a public repository exposes:** the committed digests (`digests/*.md`), the database snapshot
  in the `db-*` releases and the Pages dashboard all show which companies, instruments and people you
  watch. Options: keep a private duplicate instead of a public fork, leave `LITRADAR_COMMIT_DIGESTS`
  unset in a fork, or keep a personal watchlist in an overlay directory via `LITRADAR_CONFIG_DIR` and
  ship only the generic lists.
* **What the data is:** harvested public metadata, abstracts and open-licensed text, each record with
  its licence string; no credentials, no paywalled text, no logins. The MIT licence covers the code
  and the gazetteers only; third-party content keeps its own terms (`NOTICE`, `docs/LEGAL.md`).

## How ranking works

The heuristic is deliberately legible: journal tier (A = 4, B = 2.5, C = 1, preprint = 1),
+1.0 per distinct platform named (cap 3), +0.2 per lab tool (cap 3), +0.5 per company mentioned
(cap 3), +1.0 for a company-affiliated author, +1.0 per watchlist term (cap 3), +3.0 for a tracked
author, +0.5 per novelty cue (cap 1.0), +1.0 for early citations; the sum is scaled to 0-10 and
every score carries a reasons string shown in the digest. Weights are overridable in
`config/radar.yaml` under `ranking.weights`.

With `--llm`, the shortlist (default: heuristic >= 3, max 300 papers) is scored by Claude against a
fixed rubric (novelty, commercial implication, instrument role, clinical readiness, hype risk,
watchlist relevance, plus a one-line summary and the companies and instruments implicated) using
structured JSON output and the Message Batches API (50% off list price). The model sees title,
abstract, venue and the matcher's hits, never stored full text. The final score is
0.4 x heuristic + 0.6 x LLM composite. Cost in the current single-stage form is about $2 a week
for 300 abstracts on Claude Opus 5.5 (about $1 through Batches; cents on Haiku 5.5); the
recommended two-stage cascade (Haiku triage of every candidate, Opus deep scoring of the top ~250
with Methods text) is modelled at about $5.86 a week, roughly $305 a year. See `litradar/rank/`
and `docs/DESIGN.md` section 8.

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
| SEC EDGAR Atom, PR Newswire, STAT, BioPharma Dive, MedTech Dive | company filings and trade-press headlines | free (EDGAR needs a contact address in the User-Agent) | wired; EDGAR User-Agent fix pending |
| Google News RSS | headlines for outlets and companies with no scriptable feed | free | wired; robots.txt disallows it, so it is moving to opt-in |
| NCBI GEO | newly released Series and the platforms they ran on (study-level census) | free; key lifts rate | built, wiring pending |
| PRIDE Archive | proteomics datasets per declared mass spectrometer | free | built, wiring pending |
| CZ CELLxGENE Discover | single-cell and spatial assays behind newly public datasets | free | built, wiring pending |
| ClinicalTrials.gov v2 | newly posted studies naming watched companies or assays | free | built, wiring pending |
| NIH RePORTER v2 | S10 shared-instrumentation awards, SBIR/STTR, keyword awards | free | built, wiring pending |
| openFDA device | 510(k) clearances, De Novo grants, PMA decisions | free; key lifts daily quota | built, wiring pending |
| Semantic Scholar | citation counts and TL;DRs for stored papers | free key (required in practice) | built, wiring pending |

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
