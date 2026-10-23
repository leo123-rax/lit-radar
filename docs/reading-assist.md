# Personal subscription reading assist

For readers who pay for Nature or Science subscriptions and want the radar to help them read the
paywalled papers it surfaces. This is a reading tool at human scale, not a harvester.

## What it does

- `litradar login nature` / `litradar login science` opens the publisher's sign-in page in a real
  Chromium window that uses a private browser profile under `.cache/browser-profile/`. **You type your
  own credentials in that window.** The code never asks for, stores, submits or logs a password; it only
  waits until the session is live and then closes the tab. `litradar login --status` reports which
  publishers currently have a live session.
- `litradar read <doi|url> [...]` opens one article at a time through that signed-in profile, extracts
  the title, abstract, Methods, funding, competing-interest and acknowledgement sections from the page,
  runs the normal instrument and company extraction, and prints a reading card with the Methods text
  (`--full` for all of it, `--out cards.md` to save). `--no-store` shows the card without storing anything.
- Pacing and cap: 20 s between articles and 20 articles per day by default (`LITRADAR_READ_CAP`,
  `LITRADAR_READ_INTERVAL`, or `subscription:` in `config/radar.yaml`). Exceeding the cap exits with code 3.

## What gets stored

By default a read article is stored at policy level **facts_only**: the derived facts (instruments and
companies found, with short snippets) and the public metadata, but **no sections or body**, exactly like
any other paywalled item. `--keep-text` stores the sections locally as well, labelled
`fulltext_source: personal_subscription`; that text is your personal copy under your subscription terms
and must never be redistributed, committed, or exported. The weekly harvester and the GitHub Actions
jobs never use these sessions or see these credentials.

## Auto-run

`litradar read` is the manual door; `read-queue` is the same door opened once a day by a timer, and
`reads-import` is how the result reaches the hosted database.

**What runs where**

| Where | What | What it touches |
|---|---|---|
| your Mac, daily 07:30 | `litradar read-queue --limit 20` through a LaunchAgent | the signed-in browser profile, the local database, `reads/<date>.jsonl` |
| your Mac, right after | `git add reads && git commit && git pull --rebase && git push` | the repository (facts only; nothing is committed when nothing was read) |
| GitHub Actions | `python -m litradar reads-import`, one step in `daily.yml` and `weekly.yml` right after the database is restored | the hosted database |

**Privacy, stated plainly: the sessions and the article text never leave the Mac.** The browser profile,
the publisher cookies and every sentence of every article stay in `.cache/` on the machine that holds the
subscription. What is committed and what the hosted job sees is a *facts record* per article: DOI, title,
journal, publication date, publisher, the time of the read, how many characters of Methods the page had,
**which** sections were present (names only, never their text), and the gazetteer mentions — each with its
`kind`, `entity_id`, `section`, `count` and matched `alias`. No abstract, no Methods, no body, and no
snippets at all unless you pass `--snippets`, which caps each one at 240 characters. CI never signs in
anywhere and has no way to.

**Picking the queue.** Candidates come from the published `search-index.json` (the dashboard's own index:
the last 180 days of records, a few MB) rather than the 1 GB database, so the laptop downloads almost
nothing; `--source db` reads the local store instead. A candidate must be in a journal listed under
`reading.journals` in `config/radar.yaml`, published inside `--days`, and not read before. The list is
ordered by score, cut to `--limit` and to whatever the daily cap has left, and read one article at a time
with the usual 20-second pacing.

```yaml
reading:
  journals:
    nature: ["Nature"]          # the flagship journals only: that is what a personal subscription covers
    science: ["Science"]
```

The Nature Research titles (*Nature Methods*, *Nature Biotechnology*, *Nature Genetics*, …) and the Science
family (*Science Advances*, *Science Translational Medicine*, …) are **separate subscriptions**. The full
candidate lists are `NATURE_RESEARCH_TITLES` and `SCIENCE_FAMILY_TITLES` in `litradar/reader.py`: add the
ones your subscription actually includes. An unsubscribed title simply comes back paywalled and costs a
read against the daily cap.

**Not signed in.** Sessions expire. When the publisher a queued article needs has no live session, the run
reads nothing at all, says so, posts a macOS notification telling you to run `litradar login <publisher>`,
and exits 0. Nothing is committed and no cap is spent.

**Articles that are not ready.** A Nature *accelerated article preview* (the unedited manuscript, PDF-only
for days to weeks) has no Methods to extract: the queue writes one record with `status: "preview"` and no
mentions, and puts that DOI back in the queue a week later, up to three times. Pages whose Methods defer to
the Supplementary Information (common on science.org, where the HTML stub is a few hundred characters
against 20k–47k for a Nature page) are recorded with `status: "supplement_only"`, `methods_in_supplement:
true` and, when the page offers one, the `supplement_url` — recorded, never downloaded.

**Installing the timer** (do it yourself; the repository never installs anything):

```bash
scripts/reader_schedule.sh install      # writes and loads ~/Library/LaunchAgents/com.lit-radar.reader.plist
scripts/reader_schedule.sh status       # installed? loaded? last lines of the log
scripts/reader_schedule.sh run          # one run by hand, exactly what the agent does
scripts/reader_schedule.sh uninstall
```

The plist is generated from `scripts/com.lit-radar.reader.plist.template` with the repository path and the
Python interpreter *discovered*, never hard-coded (override with `LITRADAR_READER_PYTHON`,
`LITRADAR_READER_HOUR`, `LITRADAR_READER_MINUTE`, `LITRADAR_READER_LIMIT`). Logs go to `.cache/reader.log`.
A run is skipped while a `litradar login` window is open (the browser profile has one writer) and while
another run holds `.cache/reader.lock`.

**On the hosted side**, `litradar reads-import` folds `reads/*.jsonl` into whatever `LITRADAR_DB` points
at: for each DOI the database already holds, it sets `extra_json.fulltext_source = "personal_subscription"`
and `methods_chars`, records the sections present as keys with empty text, replaces that paper's full-text
mentions with the record's, and re-scores it. A DOI the database does not hold is counted
(`reads.unknown_doi`) and skipped — facts never create papers. The step is `continue-on-error: true`: a bad
facts file can never stop the daily update.

```
reads.imported=12 reads.skipped=1 reads.unknown_doi=3
```

To fold a hand-run batch into the same format:

```bash
litradar reads-import --from-read-json cards.jsonl --to reads/2026-10-23.jsonl   # drops every text field
```

## Terms, in plain language

A personal subscription licenses *you* to read articles in a browser. This tool keeps to that: your
browser, your session, one article at a time, at a pace a person reads. Systematic or bulk downloading of
paywalled text is outside personal-subscription terms at both publishers and can get an account
suspended; the cap, the pacing, the facts-only default and the explicit per-article command exist so the
tool cannot drift into that. Text-and-data-mining rights are a separate licence that institutions buy.
See `docs/LEGAL.md`.

## Setup

```bash
pip install 'lit-radar[browser]'
python -m playwright install chromium
litradar login nature        # a window opens; sign in; the command returns when the session is live
litradar login science
litradar login --status
litradar read 10.1038/s41592-026-03265-w --no-store
```

## Troubleshooting

- *Signed in, but a Nature Methods / Nature Biotechnology article still says paywalled*: a personal **Nature** subscription covers the flagship journal only; the Nature Research journals are separate subscriptions. The session probe therefore uses a Nature (flagship) article.

- *"paywall markers present"*: the session is not live for that publisher. Run `litradar login <publisher>`
  again; if the window shows a CAPTCHA or consent banner, complete it; the command keeps polling for up to
  five minutes (`--wait`).
- Two-factor prompts and institutional single sign-on work the same way: finish them in the window.
- Sessions expire after days to weeks; `login --status` tells you when to sign in again.
- The profile directory holds the publisher cookies. Delete `.cache/browser-profile/` to sign out everywhere.
- Only `nature.com` and `science.org` are routed through the subscription session; every other DOI goes
  through `litradar add` and the open sources.


### How the sign-in wait behaves (final form, 2026-10-23)

`litradar login nature|science` opens a window titled **Google Chrome for Testing** on the publisher's sign-in page
and then leaves it completely alone: no probe tab, no reload, no timer-driven navigation while you type (a probe
tab steals focus from the form, and the publisher's own "log in" button may open a tab of its own). Sign in
**inside that window** — a login in your everyday browser does not count, the window keeps its own cookies — on
whatever tab the publisher gives you, accept the cookie banner, and leave the window open.

The command watches the profile's cookies (read-only). About ten seconds after they change it fetches the
flagship article once in a tab of the *same* window; when the Methods come back it prints "signed in", saves the
publisher's cookies and the window's user agent to `.cache/browser-profile/<publisher>-state.json` (mode 600,
git-ignored) and closes the window itself. Publisher log-in cookies are session cookies, so this saved state is
what makes later headless `read` calls signed in: every `Session` re-adds the cookies and, headless, reuses the
window's user agent with the full Chromium build (a plain headless browser announces itself as "HeadlessChrome"
and science.org's bot wall challenges it again). Enter in an interactive terminal forces a check; closing the
window or the `--wait` timeout ends the wait with one final headless check.

Verified 2026-10-23: both publishers signed in within about 45 seconds each; a paywalled Nature article returned
29,807 characters of Methods and a Science article 38,161, headless, through the saved sessions.
