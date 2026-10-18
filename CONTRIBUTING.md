# Contributing

Thanks for helping build the radar. The project is deliberately small and dependency-light so that
anyone can fork it, add their own API keys, and run it on a laptop or on GitHub Actions.

## Dev setup

Python 3.9+ and pip >= 21.3 (the package is `pyproject.toml`-only; older pips, including the
21.2.4 shipped with macOS's command-line-tools Python, cannot do an editable install of it, hence
the upgrade line).

```bash
git clone <your fork>
cd lit-radar
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,llm]"
cp .env.example .env                   # fill in what you have; everything is optional
python -m litradar check-config         # validates the YAML gazetteers
python -m pytest -p no:warnings         # offline tests, no network needed (-q is already in pyproject.toml)
ruff check litradar tests
```

`pyproject.toml` sets `addopts = "-q"`, so `pytest -q` on the command line becomes `-qq` and hides the
`N passed` summary line; run `python -m pytest -p no:warnings` as above, or `-o addopts=` to override.

## Where things live

| Path | What |
|---|---|
| `config/instruments.yaml` | the instrument / platform / kit gazetteer (the heart of the project) |
| `config/companies.yaml` | company gazetteer: tickers, parents, aliases, e-mail domains |
| `config/subgroups.yaml` | research subgroups, keywords, tracked authors |
| `config/journals.yaml`, `config/feeds.yaml` | journals by ISSN/RSS; news and press feeds |
| `litradar/sources/` | one adapter per data source; they only normalise records |
| `litradar/extract/` | gazetteer matching and JATS section parsing |
| `litradar/rank/` | heuristic scoring and the optional LLM scorer |
| `litradar/digest/` | Markdown rendering and delivery |
| `litradar/store.py` | SQLite + FTS5 schema and queries |
| `litradar/policy.py` | licence -> storage level (`full` / `facts_only` / `none`); the allow-list lives here |
| `litradar/http.py` | the one HTTP client: User-Agent, per-host pacing, backoff, host blocking, disk cache |
| `docs/SOURCES.md`, `docs/sources/` | source catalog and per-source notes with verified limits |
| `docs/LEGAL.md`, `docs/ACCEPTABLE_USE.md` | data policy, per-source terms, what is never done |
| `docs/deploy.md` | running a fork on GitHub Actions, locally, or on a schedule |
| `SECURITY.md`, `NOTICE` | reporting, key rotation; third-party data acknowledgements and licence scope |

## The most useful contributions

1. **Gazetteer entries.** New instruments, new aliases as they appear in Methods sections, fixed
   launch years, ownership changes after acquisitions. Run `python -m litradar check-config` to catch
   duplicate ids and alias collisions, then the tests.
2. **Context terms for ambiguous names.** If a product name is also a common word, set
   `context_required: true` and list `context_terms`. The tests in `tests/test_gazetteer.py` show the
   pattern.
3. **New sources.** Add a module under `litradar/sources/` that yields `Paper`, `ArchiveRecord` or
   `NewsItem` objects, wire it into `litradar/pipeline.py`, add an offline test with a canned payload,
   and add a note under `docs/sources/` with the source's terms and documented rate limit. In the
   same change, add the source to the three policy documents and the catalog: a row in
   `docs/LEGAL.md` sections 2 and 4 (what is stored; terms, documented limit, pacing), an entry in
   `NOTICE` ("THIRD-PARTY DATA SOURCES", plus the attribution list when the source asks for credit),
   the route in `docs/ACCEPTABLE_USE.md` item 3, and a status row in `docs/SOURCES.md`. Only
   documented API endpoints and RSS feeds that serve scripts are accepted (see the ground rules).
4. **OpenAlex topic ids and author ids** in `config/subgroups.yaml`; these make researcher tracking exact.

## Ground rules

* **Never commit `.env`, database files, snapshots, or anything from behind a paywall.** The pipeline
  stores metadata, abstracts and open-licensed full text only (`docs/LEGAL.md`). CI fails if a
  database, WAL/SHM file, `.zst` snapshot, `.cache/` or `.env*` file is tracked.
* **No credentials in YAML, Markdown or digests**, and no slot for a publisher login anywhere: the
  pipeline does not log in (`SECURITY.md`, `docs/LEGAL.md`).
* **No HTML scrapers.** Adapters call documented APIs or RSS feeds. Pull requests that scrape
  publisher pages, Google Scholar, or any site whose `robots.txt` or `/.well-known/tdmrep.json`
  reserves rights will be closed. A feed that answers 403 is dropped, not worked around. The one
  standing exception is the CNCB-NGDC archive pair (`litradar/sources/gsa.py`, `gsa_human.py`),
  which reads server-rendered metadata pages because the archive publishes no API; `docs/LEGAL.md`
  section 3 states the four conditions it meets (public data archive, archive metadata only, paths
  allowed by `robots.txt`, paced and never circumventing a challenge). A new exception needs all
  four, a paragraph in that section, and a reviewer's agreement.
* **Keep adapters polite:** use the shared `Http` client so rate limits and User-Agent rules are
  honoured, and add a `DEFAULT_MIN_INTERVAL` entry for any new host at or below its documented limit
  (or register it from the adapter with `http.min_interval.setdefault(host, seconds)`, as the
  regional and NGDC adapters do).
* **Licence-aware storage:** anything that stores text must go through `litradar/policy.py` and keep
  the record's licence string.
* **Workflow changes** keep every third-party action pinned to a full commit SHA with the tag in a
  comment (the same SHA for the same action in all four workflows, `pages.yml` included), pass
  `workflow_dispatch` inputs through `env:` and validate them before use (never inline `${{ }}` in
  `run:`), keep the job that holds API keys on `contents: read` (the single-job `daily.yml` is the
  documented exception), and never print a secret's value, length or prefix (the presence step
  prints names only).
* Keep Python 3.9 compatibility (no `match`, no `X | Y` unions at runtime).
* Run `ruff check litradar tests` and `python -m pytest -p no:warnings` before opening a pull
  request. Security problems go to `SECURITY.md`, not the issue tracker.
