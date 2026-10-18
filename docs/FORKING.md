# Forking lit-radar: the checklist

This is the shortest path from "I found this repository" to "it runs for me", as a numbered list
with time estimates. Every GitHub step below was checked against `.github/workflows/*.yml`; every
local step was run on a fresh clone with an empty `.env`. The long-form reference is
`docs/deploy.md`; this page only tells you what to do, in order, and what to expect.

Times marked **measured** were taken on a laptop (Python 3.9, macOS) on a fresh clone with no keys;
times marked *estimate* depend on your connection, GitHub's queue and how fast you click.

## Part A: run it on your laptop first (about 10 minutes)

Do this before touching GitHub. It proves the install, shows you a real digest, and makes the
Actions setup a matter of copying names.

1. **Clone and install** (*estimate* 1 to 2 minutes; the checkout is about 8 MB):

   ```bash
   git clone https://github.com/<you>/lit-radar && cd lit-radar
   python3 -m venv .venv && source .venv/bin/activate
   python -m pip install --upgrade pip        # do not skip this line, see below
   python -m pip install -e ".[dev]"
   ```

   The pip upgrade is not optional on a stock macOS Python. Its pip (21.2.4) refuses a
   `pyproject.toml`-only editable install with
   `ERROR: File "setup.py" or "setup.cfg" not found. Directory cannot be installed in editable mode`
   (**measured**: that is the first thing you see if you skip the line). After the upgrade the
   editable install itself takes a few seconds (**measured**: 3 s with warm wheel caches; add
   download time the first time).

   Windows (PowerShell): `py -3 -m venv .venv; .venv\Scripts\Activate.ps1`, then the same three
   `python -m pip` lines.

2. **Check the install** (*estimate* 3 minutes, almost all of it the test suite):

   ```bash
   python -m litradar --help                   # ends with a "start here" block
   python -m litradar check-config             # validates config/*.yaml; exit 0 = good
   python -m pytest -p no:warnings             # offline, no network; prints "N passed"
   ```

   The suite needs no network and no keys (**measured**: 148 s on Python 3.9, all passed).
   `pyproject.toml` already passes `-q` to pytest; if you add another `-q` on the command line you
   get `-qq`, which hides the "N passed" summary line and shows only dots.

3. **Create `.env`** (*estimate* 1 minute with no keys; 10 to 15 minutes if you also get the two
   free keys worth having):

   ```bash
   cp .env.example .env
   ```

   Leaving every value blank is a valid configuration. If you want to spend ten minutes, the two
   entries that change the output most are `LITRADAR_CONTACT_EMAIL` (any address you read; it
   switches on the SEC 8-K feeds and the faster "polite" pools) and `OPENALEX_API_KEY` (free;
   without it OpenAlex contributes a handful of works at best). The table in Part C shows exactly
   what each missing key costs.

4. **A one-day smoke run** (**measured**: 112 s wall, 225 HTTP requests, no keys, a Saturday):

   ```bash
   python -m litradar run --days 1
   ```

   The command ends with a JSON report and `digest written to digests/<date>.md`. The report's
   `errors` list is normal reading, not a failure: with no keys you will see `sec: SEC 8-K feeds
   skipped (LITRADAR_CONTACT_EMAIL not set ...)` and usually `gdelt: host rate-limited (HTTP 429)`.
   The exit code is 0 as long as the pipeline itself completed; per-source problems are reported
   in the digest's "Coverage and method" section and in `litradar status`, never as a crash.

   Then the real thing, when you have the time: `python -m litradar run --days 7` (*estimate*
   10 to 30 minutes with keys; the SRA census alone is about 240 requests at 3 per second without
   `NCBI_API_KEY`).

5. **Look at what you got** (**measured**: each command under 2 seconds):

   ```bash
   python -m litradar status                  # exit 0 once a run exists; see below for what makes it exit 1
   python -m litradar digest --days 1         # re-render from the database, no fetching
   python -m litradar dashboard --out site    # static site, open site/index.html
   python -m litradar serve                   # http://127.0.0.1:8787 ; Ctrl-C to stop
   ```

   With the server running, `curl http://127.0.0.1:8787/api` lists every JSON route;
   `/stats`, `/top?limit=5`, `/instruments`, `/search?q=sequencing` and `/digest/latest` all answer
   from the database you just built. `serve` exits 2 with `database not found` if you run it
   before step 4.

   **About `status` exiting 1.** `litradar status` is written for cron and the daily workflow: it
   exits 1 when there is no run yet, when the last run is older than three days, or when the last
   run had errors on more than two sources that genuinely failed. Configuration skips (the SEC feed
   without `LITRADAR_CONTACT_EMAIL`) and rate-limited hosts (GDELT's 429) are still listed, tagged
   `[configuration, not counted]` / `[degraded: rate-limited, not counted]`, so a keyless laptop
   normally gets **exit 0 right after its first successful run**; `--max-failing-sources N` adjusts the
   limit and docs/live.md spells out the rule.

6. **Optional: a private watchlist.** Point `LITRADAR_CONFIG_DIR` at a directory outside the
   repository holding your own `companies.yaml` / `subgroups.yaml` and keep the shipped lists
   generic; otherwise a public fork, its digests and its dashboard show what you watch.

## Part B: let GitHub Actions run it every Monday (about 30 minutes, mostly waiting)

Everything here was read off the workflow files. Nothing has to be created by hand on GitHub except
secrets and one Pages setting: the workflows create their own releases.

1. **Fork or duplicate** (*estimate* 1 minute). Fork for one-click upstream updates; duplicate
   (`git clone --bare` + `git push --mirror` into a new private repository) when the copy must be
   private, because a public fork cannot be made private later. Two things are different in a fork:
   scheduled workflows start disabled, and the weekly job does not commit digests unless you set a
   variable (step 4).

2. **Enable Actions** (*estimate* 2 minutes). *Actions* tab -> "I understand my workflows, go ahead
   and enable them". Then, in a fork, open **weekly-digest** and **daily-update** and click
   *Enable workflow* on each: GitHub keeps `schedule:` triggers off in forks until you do. **pages**
   has no schedule (it runs after each successful weekly run) and **ci** runs on pushes and pull
   requests; neither needs this extra click. No change to *Settings -> Actions -> General ->
   Workflow permissions* is needed: every workflow starts from `permissions: {}` and each job
   declares what it needs.

3. **Add secrets and variables** (*estimate* 5 minutes of clicking, plus however long the keys take
   to obtain). *Settings -> Secrets and variables -> Actions*. Names must match exactly; all are
   optional and the first step of every weekly and daily run prints which ones are present (names
   only, never a value) and raises a warning annotation when the two important ones are missing.

   | Add as **secret** | Read by | Effect when present |
   |---|---|---|
   | `LITRADAR_CONTACT_EMAIL` | weekly, daily, time-series | SEC 8-K feeds run; OpenAlex / Crossref / Unpaywall use the polite pool |
   | `OPENALEX_API_KEY` | weekly, daily | OpenAlex topic feeds, tracked authors by id and company-typed affiliations actually return papers |
   | `NCBI_API_KEY` | weekly, daily, time-series | SRA / GEO census at 10 requests/s instead of 3 (the longest step) |
   | `OPENFDA_API_KEY` | weekly only | 510(k) / PMA source quota 120,000/day instead of 1,000/day per IP |
   | `SEMANTIC_SCHOLAR_API_KEY` | weekly, daily | citation enrichment at 1 request/s instead of the shared anonymous pool |
   | `ANTHROPIC_API_KEY` | weekly (`llm` input) | the weekly job adds `--llm` when the input is ticked *and* the key is present (set a spend limit at the provider first); the daily job receives the key but never scores |
   | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO` | weekly | the weekly job adds `--email` when `SMTP_HOST` is set; the daily job renders no digest, so it never mails |
   | `SLACK_WEBHOOK_URL` | weekly | the weekly job adds `--slack` when it is set |

   | Add as **variable** | Effect |
   |---|---|
   | `LITRADAR_COMMIT_DIGESTS` | `true` = commit `digests/*.md` to your default branch even in a fork; anything else or unset = the digest is only attached to the run (as the `radar-state` artifact, 7 days) |
   | `LITRADAR_LLM_MODEL` | overrides the model name used with `--llm`; leave unset for the code default |

4. **Set the Pages source** (*estimate* 1 minute, do it now rather than later). *Settings -> Pages
   -> Build and deployment -> Source: GitHub Actions*. The **pages** workflow is triggered by the
   first successful weekly run and fails at its *Configure Pages* step if this is still "Deploy
   from a branch"; nothing is lost if that happens, set the source and re-run **pages** by hand.
   Pages sites are public (private publishing needs a paid plan): the dashboard shows your
   watchlist.

5. **First manual run** (*estimate* 15 to 40 minutes of waiting). *Actions -> weekly-digest -> Run
   workflow*, defaults (`days = 7`, `llm` off, `timeseries_vendor` empty). What the two jobs do:

   * `radar` (holds your secrets, can only read the repository): prints the secrets-presence list,
     finds no `db-latest` release yet ("starting from an empty database (first run)"), runs
     `python -m litradar run --days 7`, then optimises, checkpoints and compresses the database.
   * `publish` (can write the repository, sees no secret): **creates** the releases `db-latest`
     and `db-YYYY-MM` if they do not exist (pre-releases, never marked "Latest"), uploads
     `radar.sqlite.zst` to both, then commits `digest: YYYY-MM-DD` to your default branch, or, in a
     fork without `LITRADAR_COMMIT_DIGESTS=true`, prints a notice and commits nothing.

   You do not create the release, the tag or any branch. When the run is green, **pages** starts on
   its own and the dashboard appears at `https://<you>.github.io/lit-radar/` a few minutes later.

6. **Branch protection** (*estimate* 0 minutes if you have none). The digest commit is a direct
   `git push` by `github-actions[bot]`. A protection rule or ruleset that requires pull requests or
   blocks direct pushes fails that step (the database upload before it still succeeds). Either leave
   the default branch unprotected, let the GitHub Actions app bypass the rule, or keep
   `LITRADAR_COMMIT_DIGESTS` unset and read digests from the run artifact.

7. **The daily job from tomorrow** (06:20 UTC). `daily-update` restores `db-latest` (creating it if
   the weekly run has not happened yet), runs `litradar update --overlap-days 2 --max-days 14`,
   uploads the snapshot back and ends with `python -m litradar status`, whose exit code is the
   job's. That step stays **green on a keyless fork**: the SEC skip is tagged as configuration and
   GDELT's 429 as degraded, and neither counts against the limit of two genuinely failed sources.
   It goes red only when upstream sources really fail; edit the last step of `daily.yml` to
   `python -m litradar status --max-failing-sources 4` if you prefer a quieter signal.

8. **Keep it alive.** GitHub switches scheduled workflows off after 60 days without activity in the
   repository. A commit resets that clock, and with `LITRADAR_COMMIT_DIGESTS=true` the weekly digest
   commit does it for you; a fork that never commits should be checked every couple of months
   (*Actions -> weekly-digest*: a yellow banner and an *Enable workflow* button mean it was switched
   off).

## Part C: what you get with zero keys

Measured on a one-day window (`run --days 1`, no keys, a Saturday, 112 s, 225 requests). A seven-day
window scales these counts up roughly sevenfold for the paper sources and the archives; the
"skipped" rows do not change until the key is added.

| Source | Runs without a key? | This run produced | What a key changes |
|---|---|---|---|
| bioRxiv / medRxiv | yes | 44 + 3 preprints | nothing (no key exists) |
| Europe PMC (journals by ISSN, Methods search) | yes | 0 papers in a 1-day window: Europe PMC indexes most records 1 to 2 days after publication, a 7-day window fills in | nothing |
| Crossref | yes, anonymous pool | 53 records | `LITRADAR_CONTACT_EMAIL` moves you to the polite pool |
| OpenAlex | runs, but nearly empty | 3 works | `OPENALEX_API_KEY` is the single most valuable key: topics, tracked authors, company-affiliated authors |
| arXiv q-bio | yes | 0 (weekend, 1-day window) | nothing |
| Journal / trade RSS | yes | 197 items; one trade feed answered 403 to scripts and is reported as a source error | nothing |
| NCBI SRA census | yes, 3 requests/s with 429 back-off | 3,780 experiments | `NCBI_API_KEY`: 10 requests/s, the longest step gets much shorter |
| ENA | yes | 599 records | nothing |
| NCBI GEO | yes | 12 series | `NCBI_API_KEY` as above |
| PRIDE | yes | 5 datasets | nothing |
| CZ CELLxGENE | yes | 0 in the window (nothing released that day) | nothing |
| ClinicalTrials.gov | yes | 0 matches in the window | nothing |
| openFDA 510(k) / PMA | yes, 1,000 requests/day per IP | 10 clearances + 7 PMA decisions (+ 23 for unwatched companies) | `OPENFDA_API_KEY`: 120,000/day |
| NIH RePORTER | yes | 0: the API answered with its "system unavailable" page for the whole run (upstream maintenance; the same error appears in a keyed run that day) | nothing; retry another day |
| Semantic Scholar | shared anonymous pool | 280 DOIs looked up, 187 matched, 42 citation counts raised; a second identical run 15 minutes later got a 429 and matched nothing, so expect either outcome | `SEMANTIC_SCHOLAR_API_KEY`: 1 request/s, predictable |
| GDELT company news | yes, 1 request per 5 s | 0: two consecutive 429s block the host for the rest of the run, 72 company queries skipped; listed in the digest's errors | nothing (no key exists); it is a shared public endpoint |
| SEC EDGAR 8-K | **no: skipped** | 0, with the message `SEC 8-K feeds skipped (LITRADAR_CONTACT_EMAIL not set ...)` | `LITRADAR_CONTACT_EMAIL` turns it on |
| Google News RSS | off by default | 8 configured feeds skipped (opt-in; see `docs/LEGAL.md`) | `news.google_news: true` in `config/radar.yaml`, your decision |
| LLM rubric scoring | off | `LLM-scored: 0` | `ANTHROPIC_API_KEY` plus `--llm` (or the `llm` workflow input) |
| E-mail / Slack delivery | off | the digest is written to `digests/` only | `SMTP_*` / `SLACK_WEBHOOK_URL` |

Net result of that keyless run: 289 papers, 12 instrument mentions, 4,396 archive records, 28 news
items, a complete digest with every section rendered (the empty ones say so) and a 307 KiB
dashboard in 15 files.

## Part D: the first ten minutes, troubleshooting

| What you see | Why, and what to do |
|---|---|
| `editable mode currently requires a setuptools-based build` | stock pip is older than 21.3; `python -m pip install --upgrade pip` inside the venv, then retry |
| `litradar status` exits 1 with `! no runs recorded yet` | expected before the first `run`; not an error |
| `litradar status` exits 1 after a successful run | more than two sources errored (SEC skip, GDELT 429, an outage); read the `!` line; see Part A step 5 |
| `litradar serve` exits 2: `database not found` | run `litradar run --days 1` first, or pass `--db PATH` |
| `litradar dashboard` exits 1: `no database at ...` | same cause; run the pipeline once |
| `litradar digest` before any `run` exits 1 with `no runs yet; run litradar run --days 7 first` | expected: there is nothing to render yet; `--allow-empty` forces an empty digest |
| `pytest -q` shows dots but no "N passed" | `-q` is already in `pyproject.toml`; run `python -m pytest -p no:warnings` (what CI does) |
| the digest's news section is empty | GDELT answered 429 (see its row above); SEC needs `LITRADAR_CONTACT_EMAIL`; Google News is opt-in |
| weekly run green, **pages** red at *Configure Pages* | the Pages source is not "GitHub Actions" yet (Part B step 4); set it and re-run **pages** |
| **daily-update** red at *Freshness check* every day | Part B step 7 |
