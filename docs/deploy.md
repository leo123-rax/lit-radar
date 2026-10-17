# Deploying your own copy

Everything below assumes you want the radar to run itself every Monday on GitHub Actions, keep its
history, and publish a digest. The same package also runs on a laptop; that is section 6.

## 1. Make the repository yours

**Fork** when you want to pull upstream gazetteer updates with a button. Two consequences of a fork:
GitHub disables scheduled workflows in forks until you enable them (section 3), and this project's
weekly workflow does **not** commit digests in a fork unless you ask it to (section 4), because a
committed digest reveals your watchlist.

**Duplicate** (`git clone --bare <upstream>` then `git push --mirror <your new repo>`) when you want a
private copy: a public fork cannot be switched to private, and GitHub Pages sites are public even for
private repositories on most plans. A duplicate commits digests like any non-fork.

## 2. Add secrets and variables

*Settings -> Secrets and variables -> Actions*. Every secret is optional; the workflow reads what is
present. Add as **secrets** (names exactly as in `.env.example`):

| Secret | Why |
|---|---|
| `LITRADAR_CONTACT_EMAIL` | polite-pool rate limits at OpenAlex and Crossref; SEC EDGAR feeds are skipped without it |
| `OPENALEX_API_KEY` | free, effectively required: anonymous OpenAlex traffic shares a tiny per-IP daily budget |
| `NCBI_API_KEY` | lifts E-utilities from 3 to 10 requests/s (the SRA/GEO census is the longest step) |
| `OPENFDA_API_KEY` | 1,000 -> 120,000 requests/day for the 510(k)/PMA source |
| `SEMANTIC_SCHOLAR_API_KEY` | citation enrichment |
| `ANTHROPIC_API_KEY` | only if you will run with the `llm` input; set a spend limit at the provider first |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO` | e-mail delivery; use an app-specific password |
| `SLACK_WEBHOOK_URL` | Slack delivery; the URL is itself a credential |

Add as **variables** (non-secret):

| Variable | Effect |
|---|---|
| `LITRADAR_COMMIT_DIGESTS` | set to `true` in a fork to commit `digests/*.md` to the branch; unset or anything else = do not commit (the digest is still attached to the run as an artifact) |
| `LITRADAR_LLM_MODEL` | overrides the default model name; leave unset to use the code default |

Never put any of these in `config/*.yaml` or in the workflow file.

## 3. Enable Actions

*Actions* tab -> "I understand my workflows, go ahead and enable them". Then open **weekly-digest**
and, in a fork, click **Enable workflow** (schedules are off by default in forks). Two GitHub
behaviours to know: scheduled runs can be delayed on busy mornings, and GitHub disables schedules
after 60 days without any repository activity. The workflow declares its own token permissions, so
no change to *Settings -> Actions -> General -> Workflow permissions* is needed.

## 4. First manual run

*Actions -> weekly-digest -> Run workflow*, keep `days = 7`, leave `llm` off and `timeseries_vendor`
empty. Expect 15 to 40 minutes. What a good first run looks like:

1. Job **radar**: a notice "no db-latest release yet: starting from an empty database (first run)",
   then the pipeline log, then "Checkpoint and compress" printing the size of `data/radar.sqlite`
   and `radar.sqlite.zst`.
2. Job **publish**: two releases appear under *Releases*, `db-latest` and `db-YYYY-MM`, each holding
   `radar.sqlite.zst`. Then either a commit `digest: YYYY-MM-DD` on your branch or, in a fork without
   `LITRADAR_COMMIT_DIGESTS=true`, a notice that the digest was not committed. In both cases the
   digest is in the run's `radar-state` artifact.

Where the history lives, and why: the previous design kept the database in `actions/cache`, which
GitHub evicts after **7 idle days**, exactly the cadence of a weekly job, and which dropped SQLite's
`-wal`/`-shm` side files. Release assets have no idle eviction. `db-latest` is replaced on every
successful run; `db-YYYY-MM` is replaced on every run of that month, so at month end it holds the
last state of that month and you have one roll-back point per month. `.cache/` (raw HTTP bodies)
still goes through `actions/cache` because it is only an optimisation; losing it costs a few extra
requests.

**Size expectations.** One 7-day window is about 3.5k papers and about **120k archive rows** (SRA +
ENA experiments). After the first week the database measured about 118 MB raw and about 27 MB as
`.zst`; growth is roughly that per week of history. GitHub allows release assets up to 2 GiB each, so
at the measured rate the compressed asset has well over a year of headroom. When it gets large, roll
up `archive_records` older than a few months into monthly counts or `VACUUM` the database before
the publish step; both are ordinary SQLite operations on the restored file.

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

A separate dashboard workflow builds a static site into `site/` and publishes it with GitHub Pages.
*Settings -> Pages -> Build and deployment -> Source: GitHub Actions*. The site goes live on that
workflow's next run at `https://<owner>.github.io/<repository>/`. Pages sites are public, including
on private repositories unless your plan offers access control, so treat the dashboard like the
digest: it shows your watchlist.

## 6. Running locally

```bash
# macOS / Linux
git clone https://github.com/<you>/lit-radar && cd lit-radar
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[llm]" markdown
cp .env.example .env            # fill in what you have; everything is optional
python -m litradar check-config
python -m litradar run --days 7
```

```powershell
# Windows (PowerShell)
git clone https://github.com/<you>/lit-radar; cd lit-radar
py -3 -m venv .venv; .venv\Scripts\Activate.ps1
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

Actions in `.github/workflows/` are pinned to full commit SHAs. To have GitHub propose updates, add
`.github/dependabot.yml`:

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
| Digest footer says OpenAlex "Insufficient budget" | add `OPENALEX_API_KEY` (free) |
| No SEC 8-K items | add `LITRADAR_CONTACT_EMAIL`; the feeds are skipped without a contact |
| `llm` run shows "LLM-scored: 0" | `ANTHROPIC_API_KEY` missing, or the provider rejected the model name in `LITRADAR_LLM_MODEL` |
| Publish job fails with "new snapshot is N bytes, the published one is M bytes" | the run produced a much smaller database (usually a source outage or a wiped restore); inspect the `radar-state` artifact, then re-run or roll back (section 4) |
| Radar job fails `integrity_check` | the published snapshot is damaged; roll `db-latest` back from the newest `db-YYYY-MM` (section 4) |
| Digest not committed | you are in a fork; set the variable `LITRADAR_COMMIT_DIGESTS=true` if you want it committed |
