# Getting the digest delivered

The weekly run writes `digests/YYYY-MM-DD.md` and commits it. That file is the product; delivery is
how it reaches a person who does not open the repository every Monday. Four channels exist. Two of
them need nothing you have to provision, store or rotate:

| Channel | Needs | How a reader subscribes | Where it is wired |
|---|---|---|---|
| **GitHub Issue** | nothing: the workflow's own `GITHUB_TOKEN` (`issues: write`) | **Watch -> Custom -> Issues** on the repository | `weekly.yml`, publish job, after the digest commit |
| **Atom feed** | nothing: a file on the Pages site | add `https://<owner>.github.io/<repository>/feed.xml` to a feed reader | `pages.yml`, after the dashboard build |
| E-mail (SMTP) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO` | nothing; the digest is sent | `litradar run --email` (the radar job passes it when `SMTP_HOST` is set) |
| Slack | `SLACK_WEBHOOK_URL` | join the channel | `litradar run --slack` (passed when the URL is set) |

`python -m litradar deliver --status` prints which of the four are usable from where you run it
(presence only; no value of any credential is ever printed).

## 1. The GitHub Issue

### What happens

After `digests/<date>.md` is committed and pushed, the publish job runs
`litradar deliver --issue --date <date>`:

1. `gh issue list` (open and closed) is searched for an issue titled exactly `Weekly digest <date>`.
2. **Found** -> `gh issue edit` replaces the body (and keeps the title). The issue's open/closed state
   is left alone. **Not found** -> `gh issue create`. The title is the identity, so re-running the
   workflow for the same date updates one issue instead of opening a second.
3. The label `digest` is added; the label is created first when the repository does not have it. Both
   are best effort: a failure is logged and the issue stands without the label.
4. Every still-open digest issue for an **earlier** date is closed with the comment
   `Superseded by #<n> (Weekly digest <date>): <url>`. Issues for a later date are never touched, so
   re-delivering an old digest cannot close the current one.

The body is: a line linking the full file in the repository, the trends dashboard and the feed; the
digest Markdown; a short footer explaining how to subscribe. GitHub caps an issue body at 65,536
characters, so the digest is cut at the last line break before **60,000** characters with a note
pointing at the complete file. A full weekly digest is typically 90-150k characters, so the issue
usually holds the top innovations, the platform tables and part of the company section; the
archive census, tracked researchers, news and coverage are in the linked file.

Nothing in the digest is altered. The `gh` calls are list-form subprocesses with the body on stdin
(`--body-file -`), so third-party text (titles, abstracts, headlines) never passes through a shell.

### Subscribing

On the repository page: **Watch -> Custom -> tick Issues -> Apply**. GitHub e-mails every new issue
and comment on it to you, from its own notification address, with the Markdown rendered. Each
week's issue is one e-mail; the close comment on the previous week's issue is a second, short one
(turn `close_previous` off if that bothers you, see below). Anyone with read access to the repository
can subscribe the same way, which is the point: the repository owner adds a reader by sharing the
link, not by editing a recipient list.

### Forks

The same workflow in a fork does the same thing, with two switches the fork owner controls:

* **Issues must be enabled.** GitHub turns Issues off in forks by default (*Settings -> General ->
  Features -> Issues*). Until then the step logs `::warning:: ... Issues are disabled` and the run
  stays green.
* **The digest must be committed.** The issue step runs only after the commit step pushed a digest,
  because the issue links to that file. In a fork that means the repository variable
  `LITRADAR_COMMIT_DIGESTS=true` (`docs/deploy.md` section 2). The reasoning is the same as for the
  commit: an issue is as public as the repository, and both reveal the watchlist.

### Configuration

All optional, under `digest.delivery` in `config/radar.yaml`; a missing key means the default:

```yaml
digest:
  delivery:
    github_issue: true      # false = `deliver --issue` prints "skipped" and exits 0 (the workflow step stays green)
    close_previous: true    # false = earlier digest issues stay open and get no comment
    label: digest           # label added to each digest issue (created when missing)
    feed_entries: 12        # digests in feed.xml
    pages_url: ""           # dashboard URL for the links; default https://<owner>.github.io/<repository>/
```

Repository, branch and dashboard URL are resolved from the Actions environment (`GH_REPO` /
`GITHUB_REPOSITORY`, `GITHUB_REF_NAME`, `GITHUB_SERVER_URL`) and can be overridden with
`--repo owner/name`, `--branch`, `--pages-url` or the `LITRADAR_PAGES_URL` environment variable
(set it as a repository **variable** of that name when the dashboard has a custom domain; both
workflows pass it through).

### Permissions and failure handling

The publish job asks for `contents: write` (release assets, the digest commit) and now
`issues: write`; it still holds no API key. The radar job, which holds the keys, is unchanged and
cannot write anything. The delivery step is `continue-on-error: true` and the command itself prints
`::warning::` and exits 1 on any problem (no `gh`, no token, Issues disabled, API error), so a
delivery hiccup shows as a warning annotation on the run and never undoes the commit or the release
upload.

### By hand

```bash
python -m litradar deliver --issue --dry-run            # newest digest: what would be created or updated, body preview
python -m litradar deliver --issue --date 2026-10-18    # deliver one specific digest
python -m litradar deliver --issue --repo owner/name    # when the checkout has no GitHub remote
```

Locally `gh` must be logged in (`gh auth status`) or `GH_TOKEN` set; the token needs the `repo`
scope (or `issues: write` on a fine-grained token).

## 2. The Atom feed

`litradar deliver --feed site/feed.xml` writes an Atom 1.0 feed of the newest `digests/YYYY-MM-DD.md`
files (12 by default, newest first). Each entry carries the digest's title (its H1), a link to the
file in the repository (`rel="alternate"`), a link to the dashboard (`rel="related"`), the
publication date (midnight UTC of the file's date), the render time from the digest's own
`_Rendered YYYY-MM-DD HH:MM UTC_` line as `updated`, and the first 2,000 characters of the Markdown
as `content type="text"`. Dates are RFC 3339 (`2026-10-18T01:29:00Z`). The document is built with the
standard library's ElementTree, so every character is escaped and the file always parses; with no
digests it is a valid empty feed.

The Pages workflow runs the command after `litradar dashboard` has written `site/` and before the
site is uploaded. The dashboard build adds files to `site/` and never clears the directory, so the
order does not matter for correctness; the feed step is placed after it anyway and is
`continue-on-error`, so a feed problem cannot keep the dashboard from deploying. The digests come
from the repository checkout (`persist-credentials: false`, read-only), not from the database, so a
fork that does not commit digests publishes an empty feed. Subscribe at
`https://<owner>.github.io/<repository>/feed.xml`.

## 3. E-mail and Slack (unchanged)

`litradar run --email` sends the whole digest as text plus sanitised HTML over verified-TLS SMTP;
`litradar run --slack` posts the top ten to a webhook. Both read their settings from `.env` locally
or from repository secrets on Actions (`docs/deploy.md` section 2; the radar job adds the flags when
`SMTP_HOST` / `SLACK_WEBHOOK_URL` are present). A delivery failure is logged and the run continues;
the digest file is always on disk first.

## 4. Command reference

```
litradar deliver --issue [--date YYYY-MM-DD] [--dry-run] [--repo owner/name] [--branch main] [--pages-url URL]
litradar deliver --feed PATH [--entries 12]
litradar deliver --status [--json]
```

Flags combine (`--status --feed site/feed.xml --issue`). Exit 0 on success or a configured skip, 1 when
a requested delivery failed, 2 on a usage error.

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No issue after a green weekly run | the commit step did not push a digest (fork without `LITRADAR_COMMIT_DIGESTS=true`, or "digest unchanged"); the issue step runs only after a push |
| Warning annotation `GitHub Issue delivery failed: gh issue create exited 1: ... Issues are disabled` | enable Issues in *Settings -> General -> Features* |
| Warning `... Resource not accessible by integration` | the publish job lacks `issues: write` (an older copy of `weekly.yml`), or the organisation restricts `GITHUB_TOKEN` permissions; grant them under *Settings -> Actions -> General* |
| Two issues for one date | the titles differ (edited by hand); the newer one is the live one, close the other |
| Label missing on the issue | label creation failed (no permission to create labels); the issue is fine, add the label once by hand |
| `feed.xml` is empty | no `digests/*.md` in the checkout the Pages job ran from (fork not committing digests) |
| Issue body ends mid-digest | expected: cut at 60,000 characters with a link to the complete file |
