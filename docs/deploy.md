# Deploying your own copy

Everything below assumes you want the radar to run itself every Monday on GitHub Actions, keep its
history, and publish a digest. The same package also runs on a laptop; that is section 6.

Short on time? `docs/FORKING.md` is the same material as a numbered checklist with time estimates,
the exact secret and variable names, and a table of what a copy with zero keys produces. This page
is the reference behind it.

## 1. Make the repository yours

**Fork** when you want to pull upstream gazetteer updates with a button. Two consequences of a fork:
GitHub disables scheduled workflows in forks until you enable them (section 3), and this project's
weekly workflow does **not** commit digests in a fork unless you ask it to (section 4), because a
committed digest reveals your watchlist.

**Duplicate** (`git clone --bare <upstream>` then `git push --mirror <your new repo>`) when you want a
private copy: a public fork cannot be switched to private, and GitHub Pages sites are public even for
private repositories on most plans. A duplicate commits digests like any non-fork.

## 2. Add secrets and variables

*Settings -> Secrets and variables -> Actions*. The workflows run with no secrets at all, but each
secret is **required for a feature**: without it that feature is silently thin or skipped, and a
public demo deployed with zero secrets omits a core source (OpenAlex) and the SEC filings. The first
step of every weekly and daily run prints which of these are present (**names only, never a
value**) to the log and the run summary, and raises a warning annotation when `OPENALEX_API_KEY` or
`LITRADAR_CONTACT_EMAIL` is missing, so you can see what a fork lacks without opening a digest.
Add as **secrets** (names exactly as in `.env.example`):

| Secret | Required for | Without it |
|---|---|---|
| `LITRADAR_CONTACT_EMAIL` | **SEC EDGAR 8-K feeds** (EDGAR requires a contact in the User-Agent); the polite pools at OpenAlex, Crossref and Unpaywall | every run logs `SEC 8-K feeds skipped: LITRADAR_CONTACT_EMAIL not set`; OpenAlex/Crossref run on the shared anonymous pool |
| `OPENALEX_API_KEY` | **the OpenAlex source**: topic feeds, tracked authors by OpenAlex id, company-typed author affiliations (free key) | OpenAlex contributes 0 or near-0 papers (the anonymous per-IP budget is spent within minutes), tracked authors match by name only, and the companies page's "author affiliation" series is empty |
| `NCBI_API_KEY` | faster SRA/GEO census (E-utilities 3 -> 10 requests/s); optional | the census runs at 3 requests/s and is the longest step |
| `OPENFDA_API_KEY` | the 510(k)/PMA source at volume (1,000 -> 120,000 requests/day); weekly job only | works on the per-IP quota |
| `SEMANTIC_SCHOLAR_API_KEY` | **citation enrichment** (1 request/s with a key) | enrichment is rate-limited to nothing in practice |
| `ANTHROPIC_API_KEY` | **the `llm` input** (rubric scoring); set a spend limit at the provider first | `--llm` is not passed; "LLM-scored: 0" |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO` | **e-mail delivery**; use an app-specific password | the digest is only committed / attached |
| `SLACK_WEBHOOK_URL` | **Slack delivery**; the URL is itself a credential | no Slack post |

Add as **variables** (non-secret):

| Variable | Effect |
|---|---|
| `LITRADAR_COMMIT_DIGESTS` | set to `true` in a fork to commit `digests/*.md` to the branch; unset or anything else = do not commit (the digest is still attached to the run as an artifact) |
| `LITRADAR_LLM_MODEL` | overrides the default model name; leave unset to use the code default |
| `LITRADAR_PAGES_URL` | public URL of the Pages site, used in the digest issue and the Atom feed links; leave unset to derive `https://<owner>.github.io/<repo>/` |

Never put any of these in `config/*.yaml` or in the workflow file.

## 3. Enable Actions

*Actions* tab -> "I understand my workflows, go ahead and enable them". Then open **weekly-digest**
and **daily-update** and, in a fork, click **Enable workflow** on each (schedules are off by
default in forks; **pages** has no schedule and runs after each weekly run). Two GitHub
behaviours to know: scheduled runs can be delayed on busy mornings, and GitHub disables schedules
after 60 days without any repository activity. The workflow declares its own token permissions, so
no change to *Settings -> Actions -> General -> Workflow permissions* is needed.

## 4. First manual run

*Actions -> weekly-digest -> Run workflow*, keep `days = 7`, leave `llm` off and `timeseries_vendor`
empty. Expect 15 to 40 minutes. What a good first run looks like:

1. Job **fetch, extract, rank, render** (`radar` in the file): the secrets-presence step
   (`present: NAME` / `MISSING: NAME`, names only), a notice "no db-latest release yet: starting
   from an empty database (first run)", then the pipeline log, then "Optimize, checkpoint and
   compress" printing `ok` and the size of `data/radar.sqlite` and `radar.sqlite.zst`.
2. Job **store the database, commit the digest** (`publish` in the file): two releases appear under
   *Releases*, `db-latest` and `db-YYYY-MM`, each holding `radar.sqlite.zst`, both created as
   pre-releases with "set as latest" off so a rolling snapshot never shows as the repository's
   *Latest release*. Then either a commit `digest: YYYY-MM-DD` on your branch or, in a fork without
   `LITRADAR_COMMIT_DIGESTS=true`, a notice that the digest was not committed. In both cases the
   digest is in the run's `radar-state` artifact.
3. Workflow **pages** starts by itself when the weekly run succeeds (section 5).

Nothing has to be created by hand before this run: the `db-latest` and `db-YYYY-MM` releases (and
their tags) are created by the publish job when they do not exist yet, and the daily job creates
`db-latest` the same way. **Branch protection:** the digest commit is a direct `git push` to your
default branch by `github-actions[bot]`. A protection rule or ruleset that requires pull requests
or blocks direct pushes makes that step fail (the database upload before it still succeeds). Either
leave the branch unprotected, allow the GitHub Actions app to bypass the rule, or keep
`LITRADAR_COMMIT_DIGESTS` unset and read the digest from the `radar-state` artifact instead.

**The daily update** (`.github/workflows/daily.yml`, 06:20 UTC, one job named `update`) does
not render a digest. It restores `radar.sqlite.zst` from `db-latest` (creating the release on a
first run), runs `python -m litradar update --overlap-days 2 --max-days 14` (re-fetch from two
days before the last recorded run's end, capped at 14 days; a `since` date or a `sources` subset
can be passed from *Run workflow* for repairs), then merges the FTS index, VACUUMs, checkpoints,
compresses and uploads the snapshot back to `db-latest` with `--clobber`. The upload also runs
after a failed update (SQLite stays consistent and upserts are idempotent) but never after a failed
restore. `digests/_work/last_update.json` is attached as the `last_update` artifact for 30 days,
and the job ends with `python -m litradar status`, whose exit code is the job's: red means the
update crashed, the last run is more than three days old, or more than two sources failed. Unlike
the weekly workflow this is a single job that holds both the API keys and a `contents: write`
token; the trade-off and its mitigations are stated in the file's header, and `SECURITY.md` lists
it as a known limitation. The daily job does not trigger a Pages rebuild; dispatch **pages** by
hand if you want the dashboard to reflect a weekday update.

If your `db-latest` was created by an earlier version of the workflows it may be marked as the
repository's *Latest release*; fix it once with
`gh release edit db-latest --prerelease --latest=false`.

Where the history lives, and why: the previous design kept the database in `actions/cache`, which
GitHub evicts after **7 idle days**, exactly the cadence of a weekly job, and which dropped SQLite's
`-wal`/`-shm` side files. Release assets have no idle eviction. `db-latest` is replaced on every
successful run; `db-YYYY-MM` is replaced on every run of that month, so at month end it holds the
last state of that month and you have one roll-back point per month. `.cache/` (raw HTTP bodies)
still goes through `actions/cache` because it is only an optimisation; losing it costs a few extra
requests.

**Size expectations.** One 7-day window is about 3.5k papers and about **120k archive rows** (SRA +
ENA experiments). After the first week the database measured about 118 MB raw and about 23 MB as
`.zst`; growth should be roughly proportional to new content. It was not, at first: every daily
overlap re-pull re-indexed the overlapping papers in the FTS5 table and the new segments were never
merged, so one update that added 3 papers grew the snapshot by about 16 MB raw / 6 MB `.zst` (+26%).
Both workflows therefore run `INSERT INTO papers_fts(papers_fts) VALUES('optimize')` and `VACUUM`
before compressing. The post-fix growth rate has not been measured yet; if the asset still grows by
megabytes on a day with a handful of new papers, look at `papers_fts_data` with `dbstat` first.
GitHub allows release assets up to 2 GiB each. When the file gets large, roll up `archive_records`
older than a few months into monthly counts; that is an ordinary SQLite operation on the restored
file.

**Safety rails.** The radar job refuses to continue if a restored snapshot fails `PRAGMA
integrity_check`. The publish job refuses to overwrite `db-latest` with a file less than half the
size of the published one. In both cases the run fails loudly and the new file stays attached to the
run as the `radar-state` artifact, so nothing is lost while you look.

**Rolling back.** Download the asset from the monthly release you want, then re-publish it:

```bash
gh release download db-2026-09 --pattern radar.sqlite.zst --dir restore
gh release upload db-latest restore/radar.sqlite.zst --clobber
```

To start over instead, delete the `db-latest` release (and its tag) in the GitHub UI; the next run
starts from an empty database and recreates it.

## 5. Enable GitHub Pages (dashboard)

A separate dashboard workflow (`.github/workflows/pages.yml`) builds a static site into `site/` and
publishes it with GitHub Pages. *Settings -> Pages -> Build and deployment -> Source: GitHub
Actions*. The site goes live on that workflow's next run at `https://<owner>.github.io/<repository>/`.
Pages sites are public, including on private repositories unless your plan offers access control,
so treat the dashboard like the digest: it shows your watchlist. Do this *before* the first weekly
run finishes if you can: **pages** is triggered by that run, and its *Configure Pages* step fails
when the repository's Pages source is not yet set to GitHub Actions (the workflow does not enable
Pages for you). Nothing is lost, and no site is deployed; set the source and re-run **pages** from
the Actions tab.

What the workflow does: it runs after every successful **weekly-digest** run (and on *Run
workflow*, with an integer `weeks` input, default 52), downloads `radar.sqlite.zst` from the
`db-latest` release (the same snapshot the weekly and daily jobs publish; there is no Actions-cache
step any more), checks `PRAGMA integrity_check`, runs `python -m litradar dashboard`, and deploys
`site/`. It fetches nothing from any API and never writes to the repository. If the release does
not exist yet or holds no asset, the run ends green with a notice and deploys nothing; a failed
download, a corrupt archive or a failed integrity check fails the run instead of deploying an empty
or broken site. The build is not serialised with the daily job, so a hand-dispatched run that
collides with the daily upload can fail that way; re-run it.

## 6. Running locally

Python 3.9 or newer, with pip >= 21.3: the package is `pyproject.toml`-only, and older pips (the
21.2.4 that ships with macOS's command-line-tools Python, for one) refuse `pip install -e .` with
"editable mode currently requires a setuptools-based build". Upgrade pip inside the virtualenv
first, as below, or install without `-e` (`python -m pip install ".[llm]"`), which any pip can do.

```bash
# macOS / Linux
git clone https://github.com/<you>/lit-radar && cd lit-radar
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[llm]" markdown
cp .env.example .env            # fill in what you have; everything is optional
python -m litradar check-config
python -m litradar run --days 7
```

```powershell
# Windows (PowerShell)
git clone https://github.com/<you>/lit-radar; cd lit-radar
py -3 -m venv .venv; .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[llm]" markdown
Copy-Item .env.example .env
python -m litradar check-config
python -m litradar run --days 7
```

The digest lands in `digests/`, the database in `data/radar.sqlite`. Override paths with
`LITRADAR_DB`, `LITRADAR_OUT_DIR`, `LITRADAR_CACHE_DIR`; point `LITRADAR_CONFIG_DIR` at a private
overlay directory to keep a personal watchlist out of a public repository.

To seed a laptop with the history your Actions copy has built:

```bash
gh release download db-latest --pattern radar.sqlite.zst --dir state
zstd -d state/radar.sqlite.zst -o data/radar.sqlite
```

## 7. Scheduling on a laptop or server

* **cron (Linux, macOS):** `docs/examples/crontab.txt` runs Mondays 07:30 and appends to a log under
  `digests/_work/`.
* **launchd (macOS):** copy `docs/examples/launchd.litradar.plist` to `~/Library/LaunchAgents/`,
  edit the two paths, `launchctl load` it.
* **Windows:** Task Scheduler, weekly trigger, action `.venv\Scripts\python.exe -m litradar run
  --days 7 --email` with the repository as the start-in directory.

Logs may contain request URLs; keep them private (see `SECURITY.md`).

## 8. Time series on demand

*Run workflow* with `timeseries_vendor` set to a vendor name, ticker or gazetteer id prefix (for
example `pacb`). The workflow then runs `litradar timeseries --vendor <value>` **instead of** the
digest and attaches the CSV and Markdown as the `timeseries-<run id>` artifact. Nothing is committed
and the state release is not touched. Locally: `python -m litradar timeseries --vendor pacb
--from 2024-01`.

## 9. Keeping the action pins current

Every `uses:` in `.github/workflows/` is pinned to a full commit SHA with the release tag in a
comment, and the same action uses the same SHA in every workflow (checkout, setup-python,
upload-artifact and the three Pages actions included), so a pin bump is a search-and-replace across
the four files. To resolve a tag yourself:
`gh api repos/actions/checkout/tags --jq '.[] | "\(.name) \(.commit.sha)"'`. To have GitHub
propose updates, add `.github/dependabot.yml`:

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The Monday run never happens | fork: enable the workflow (section 3); 60 idle days: push any commit or run it by hand |
| First step prints `MISSING: OPENALEX_API_KEY` (warning annotation on the run) | add the free key as a secret (section 2); until then OpenAlex contributes nothing and tracked authors match by name only |
| Digest footer says OpenAlex "Insufficient budget", or OpenAlex contributed 0 papers | add `OPENALEX_API_KEY` (free) |
| No SEC 8-K items; log says `SEC 8-K feeds skipped: LITRADAR_CONTACT_EMAIL not set` | add `LITRADAR_CONTACT_EMAIL`; the feeds are skipped without a contact |
| `db-latest` shows as the repository's "Latest release" | created by an older workflow version; run `gh release edit db-latest --prerelease --latest=false` once |
| **pages** run is red at "Restore the database" | the `db-latest` asset was being replaced or the download was cut short; re-run. A green run with "No database snapshot yet" means the release has no asset yet |
| Daily run is red at "Update the corpus" with "must be an integer" / "must be YYYY-MM-DD" | a *Run workflow* input failed validation; fix the input (digits only; `since` as `YYYY-MM-DD`; `sources` as a comma-separated list of ids) |
| `.zst` asset grows by megabytes on a day with few new papers | the FTS optimize/VACUUM step did not run or was skipped; check the "Upload the database snapshot" log for `FTS optimize skipped` and `ok` (section 4, size expectations) |
| `llm` run shows "LLM-scored: 0" | `ANTHROPIC_API_KEY` missing, or the provider rejected the model name in `LITRADAR_LLM_MODEL` |
| Publish job fails with "new snapshot is N bytes, the published one is M bytes" | the run produced a much smaller database (usually a source outage or a wiped restore); inspect the `radar-state` artifact, then re-run or roll back (section 4) |
| Radar job fails `integrity_check` | the published snapshot is damaged; roll `db-latest` back from the newest `db-YYYY-MM` (section 4) |
| Digest not committed | you are in a fork; set the variable `LITRADAR_COMMIT_DIGESTS=true` if you want it committed |
