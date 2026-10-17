# The browser fetcher (opt-in)

`litradar/browser.py` drives a real Chromium through [Playwright](https://playwright.dev/python/) for
the few hosts that **block scripted clients but serve open-licensed content**. The motivating case is
bioRxiv / medRxiv full text: every preprint record from `api.biorxiv.org` carries a `jatsxml` URL on
`www.biorxiv.org`, and that host answers HTTP 429 (Cloudflare error 1015) to anything that is not a
browser, even at one request every three seconds (`docs/SOURCES.md`). The preprints are CC-licensed;
the block is an anti-bot measure, not a licence term.

It is **off by default**, needs an optional extra, and refuses to touch anything outside a short
allow-list. Read this page and `docs/LEGAL.md` before switching it on.

## What it must never be used for

* **Paywalled publisher pages.** A personal or institutional subscription grants *reading* rights
  through the publisher's site; it does not grant automated download or text-mining rights. The fetcher
  therefore refuses, for every host, any URL whose path or query looks like a login, sign-in, account,
  subscriber, institutional-access, token or checkout flow (`FORBIDDEN_URL_RE`). Adding a publisher's
  host to the allow-list does not change that, and you should not add one unless its content is
  openly licensed and its terms allow automated access.
* **Anything your session is logged into.** In CDP mode (below) the fetcher opens tabs in a Chrome you
  run. Use a profile that is not signed in to any publisher. The pipeline never logs in anywhere
  (`README.md`, "What you need (and what you don't)").
* **Bulk harvesting.** Fifty pages per run at one page per five seconds per host is the ceiling by
  default. For whole-corpus access use the official bulk route at the end of this page.

## Install

```bash
pip install -e ".[browser]"            # adds playwright>=1.40; nothing else changes
python -m playwright install chromium  # only needed for the launch fallback (mode 2 below)
litradar browser-check                 # reports: extra installed? DevTools endpoint listening? allow-list, pacing
```

Without the extra, `litradar fetch --browser` and `browser.enabled: true` fail immediately with
`the browser fetcher needs Playwright: pip install 'lit-radar[browser]' && python -m playwright install chromium`.

## Two ways to get a browser

1. **Your own Chrome over the DevTools protocol (preferred).** Start Chrome with a remote-debugging
   port; the fetcher connects with `playwright.chromium.connect_over_cdp("http://localhost:9222")`,
   opens each URL in a new tab of the existing context, reads the response body, and closes the tab.
   Closing the fetcher detaches; it never quits your Chrome.

   Chrome 136 and later ignore `--remote-debugging-port` on the *default* profile directory, so give
   it a dedicated `--user-data-dir`. One command per platform:

   ```bash
   # macOS
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="$HOME/.cache/lit-radar/chrome-profile" &
   ```
   ```powershell
   # Windows (PowerShell)
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:LOCALAPPDATA\lit-radar\chrome-profile"
   ```
   ```bash
   # Linux
   google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.cache/lit-radar/chrome-profile" &
   ```
   Check it answers: `curl -s http://localhost:9222/json/version` (or `litradar browser-check`).
   Chromium, Edge and Brave accept the same flags.

2. **A persistent Chromium of our own (fallback).** When nothing answers at the CDP URL, the fetcher
   calls `launch_persistent_context(<cache_dir>/browser-profile, headless=True)`. The profile is
   empty and stays empty; it exists so cookies the host sets survive between runs.

The mode in use is in every log line (`cdp` or `launched`).

## Rules the fetcher enforces

| Rule | Default | Where |
|---|---|---|
| Allow-list of hosts; anything else raises `BrowserRefused` | `www.biorxiv.org`, `www.medrxiv.org` | `allowed_hosts` |
| Login / subscriber / account / token / checkout paths raise `BrowserRefused` on every host | always on | `FORBIDDEN_URL_RE` |
| Minimum gap between fetches to the same host | 5 s | `min_interval` |
| Maximum pages per process; the next fetch raises `BrowserBudgetExhausted` | 50 | `max_pages` |
| One log line per fetch: URL, status, bytes, seconds, page k/N, mode | always on | `litradar.browser` logger at INFO |
| Non-200 answers and navigation errors return `None` (still paced, still counted) | always on | `BrowserFetcher.get` |

```
INFO litradar.browser: browser GET https://www.biorxiv.org/content/biorxiv/early/2026/10/14/2026.10.14.756197.source.xml -> 200, 184233 bytes in 2.3 s (page 1/50, cdp)
```

## Using it

**From the shell**, for one source, without the pipeline:

```bash
litradar fetch biorxiv --days 7 --categories genomics --fulltext --browser --out preprints.jsonl
```

`--fulltext` asks the adapter to download each record's JATS XML; `--browser` makes the client route
`www.biorxiv.org` / `www.medrxiv.org` through the browser and everything else (the API host, Europe
PMC, ...) through plain `requests` as usual. Every downloaded document is cached on disk like any other
response, so re-running the command does not touch the browser again.

**From Python:**

```python
from litradar.browser import BrowserFetcher, BrowserHttp
from litradar.sources import biorxiv

http = BrowserHttp(cache_dir=".cache", browser_options={"max_pages": 20})
for paper in biorxiv.fetch(http, "2026-10-10", "2026-10-17", categories=["genomics"]):
    if biorxiv.attach_fulltext(http, paper):            # goes through the browser for www.biorxiv.org
        print(paper.doi, len(paper.sections.get("methods", "")))
http.close()

with BrowserFetcher(allowed_hosts=["www.biorxiv.org"]) as b:   # the raw fetcher, bytes in, no HTTP client
    xml = b.get("https://www.biorxiv.org/content/biorxiv/early/2026/10/14/2026.10.14.756197.source.xml")
```

**In the weekly pipeline.** Add a `browser:` block to `config/radar.yaml`:

```yaml
browser:
  enabled: true                      # default false
  cdp_url: http://localhost:9222     # your Chrome; falls back to a launched Chromium when nothing answers
  allowed_hosts: [www.biorxiv.org, www.medrxiv.org]
  min_interval: 5                    # seconds between fetches to the same host
  max_pages: 50                      # per run
  headless: true                     # launch fallback only
  # user_data_dir: ~/.cache/lit-radar/browser-profile   # launch fallback only
```

and make `Pipeline.__init__` build its client through `litradar.browser.make_http`, which returns a
`BrowserHttp` when `browser.enabled` is true and a plain `Http` otherwise. The one-line change in
`litradar/pipeline.py`:

```python
from .browser import make_http
# ...
self.http = http or make_http(settings, contact_email=settings.contact_email, cache_dir=settings.cache_dir, ncbi_api_key=settings.ncbi_api_key)
```

Nothing else in the pipeline changes: `biorxiv.attach_fulltext` already calls `http.get(paper.fulltext_url, cache=True)`,
and `BrowserHttp.get` decides per host. Keep `sources.biorxiv.fulltext_max` in `radar.yaml` at or below
`browser.max_pages`; the budget wins when they disagree (further fetches return `None` and are counted
as `fulltext.biorxiv_failed`, exactly like a 429 today).

## Testing

`tests/test_browser.py` replaces Playwright with a fake driver: no browser is launched, no network is
touched and no sleeping happens in CI. The tests cover the allow-list, the forbidden paths, CDP-vs-launch
selection, pacing, the page budget, the per-fetch log line, and `BrowserHttp`'s delegation and caching.

## The official bulk alternative

For anything beyond a few dozen documents a week, use the routes the preprint servers provide:

* **bioRxiv / medRxiv requester-pays S3 buckets**: full-text MECA archives (JATS XML plus figures) for
  every preprint, updated monthly: `s3://biorxiv-src-monthly` and `s3://medrxiv-src-monthly`
  (`aws s3 ls --request-payer requester s3://biorxiv-src-monthly/Current_Content/`). You pay the
  transfer, bioRxiv publishes the layout in its [TDM documentation](https://www.biorxiv.org/tdm).
* **Europe PMC** indexes bioRxiv and medRxiv preprints and serves open-access full text through
  `europepmc.fulltext_xml(http, pmcid)` when the authors' licence allows it (coverage lags the servers;
  see `docs/SOURCES.md`).
* **The details API** (`api.biorxiv.org`) remains the right source for metadata and abstracts; it is not
  blocked and the `biorxiv` adapter uses it without a browser.
