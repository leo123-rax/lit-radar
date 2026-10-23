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


### How the sign-in wait behaves (changed 2026-10-22)

While the sign-in window is open the command does nothing at all: no probe tab, no reload, no timer-driven
navigation, because a probe tab steals focus from the form and the publisher's own "log in" button may open a
tab of its own. Sign in on whatever tab the publisher gives you, accept its cookie banner, then **close the
browser window** (or press Enter in the terminal). Only then is the window closed from the code side (the
profile keeps the cookies) and the flagship article fetched once, headless, to decide. If you sign in and walk
away, the check still runs when `--wait` elapses.
