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
```

Optional extras: `pip install -e ".[llm]"` for Claude scoring, `pip install markdown` for HTML e-mail.

## What you need (and what you don't)

| Key | Needed for | Cost | Where |
|---|---|---|---|
| `LITRADAR_CONTACT_EMAIL` | polite-pool rate limits at OpenAlex, Crossref, Unpaywall; SEC EDGAR requires a contact in the User-Agent | free | any address you own |
| `OPENALEX_API_KEY` | OpenAlex works/authors/institutions (anonymous traffic is now metered per IP and runs out) | free | https://help.openalex.org/api/authentication/ |
| `NCBI_API_KEY` | lifts E-utilities from 3 to 10 requests/s (SRA census, PubMed) | free | NCBI account settings |
| `SEMANTIC_SCHOLAR_API_KEY` | reserved for citation-velocity enrichment | free | semanticscholar.org/product/api |
| `ANTHROPIC_API_KEY` | `--llm` rubric scoring of the weekly shortlist | pay-as-you-go (see cost table in `docs/DESIGN.md`) | console.anthropic.com |
| SMTP / Slack | delivery | free tiers exist | your mail provider / Slack app |

You do **not** need publisher logins. A personal Nature or Science subscription is for *reading*
the items the digest links to; it does not grant automated download or text-mining rights, so the
pipeline never logs in anywhere. Full text is read only from open-licensed sources (Europe PMC
open-access subset, preprints via their official routes). See `docs/LEGAL.md`.

## What the digest contains

* **Top innovations**: ranked papers with venue, score, the instruments and companies detected,
  and (with `--llm`) a one-line summary, why it matters, and novelty / commercial / hype flags.
* **Instruments named in this week's papers**: platform, vendor, ticker, paper count, example.
* **Instruments behind newly public sequencing data**: SRA + ENA experiments by instrument model,
  vendor share, library strategies.
* **Companies in the literature**: how each company appears (as author, as funder/COI, as subject),
  unknown company-type institutions worth adding to the gazetteer, first-time appearances.
* **Tracked researchers**: new papers and preprints by the people in `config/subgroups.yaml`.
* **Trade press & company news**: grouped by company, from Google News queries, SEC 8-K filings
  and the feeds that still allow scripted access.
* **Coverage and method**: exactly what was pulled, what had full text, what failed.

A real run's output is in `digests/`.

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

* **GitHub Actions** (recommended for forks): `.github/workflows/weekly.yml` runs every Monday,
  caches the SQLite database between runs, commits the digest to `digests/`, and e-mails or posts to
  Slack when the secrets are present. Add secrets under *Settings → Secrets and variables → Actions*.
* **Laptop**: `cron` or a `launchd` plist calling `python -m litradar run --email`.

## How ranking works

The heuristic is deliberately legible: journal tier, number of distinct instruments and companies
named, company-affiliated authorship, watchlist-term hits, tracked authors, novelty cues, early
citations and whether full text was parsed. Every score comes with a reasons string that is shown in
the digest. With `--llm`, the shortlist (default: heuristic ≥ 3, max 300 papers) is scored by Claude
against a fixed rubric (novelty, commercial implication, instrument role, clinical readiness, hype
risk, watchlist relevance) using structured JSON output and the Message Batches API; the final score
blends the two. See `litradar/rank/`.

## Data sources at a glance

| Source | What we take | Access |
|---|---|---|
| bioRxiv / medRxiv API | all new preprint versions, abstracts, licence, published-DOI link | free, no key |
| Europe PMC | new articles by ISSN; open-access full text (Methods, funding, COI); section-level search (`METHODS:`, `COMP_INT:`, `ACK_FUND:`) | free, no key |
| Crossref | DOI-level journal feeds the day of registration; abstracts where deposited | free, mailto |
| OpenAlex | topic feeds, company-typed institutions, tracked authors | free key required |
| arXiv | q-bio categories | free, 3 s between requests |
| NCBI SRA (E-utilities) | weekly census of sequencing experiments with declared instrument model | free; key lifts rate |
| ENA portal API | ERX/DRX records with `instrument_model` and `first_public` | free |
| Journal / subject RSS | tables of contents as a backstop | free |
| Google News RSS, SEC EDGAR Atom, PR Newswire | company and trade-press headlines | free |

Full notes, including what did *not* work (several publisher and IR feeds block scripts; bioRxiv
full-text XML is rate-limited for non-browser clients), are in `docs/SOURCES.md`.

## Contributing

See `CONTRIBUTING.md`. The highest-value contributions are gazetteer entries and aliases, context
terms for ambiguous names, OpenAlex ids for tracked researchers, and new source adapters with
offline tests.

## License

MIT. The gazetteers are data about publicly marketed products and public companies; they contain no
proprietary information.
