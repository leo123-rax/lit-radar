# SEC EDGAR — 8-K Atom feeds per watched company

Adapter: `litradar/sources/sec.py` (ticker → CIK resolution, feed URL, EDGAR's HTML answers) driven from
`Pipeline.fetch_news` (`litradar/pipeline.py`); the filings are parsed by the generic reader in
`litradar/sources/rss.py`. One `NewsItem(feed="SEC 8-K <TICKER>")` per filing, tagged with the company id.
Tests: `tests/test_news_pipeline.py` (offline: CIK URLs, unresolved tickers, HTML answers, table unavailable),
`tests/test_sources_offline.py` (helpers, reader outcome), `tests/test_http_policy.py` (User-Agent shape, 403 logging).

## Endpoint (verified live 2026-10-17, CIK form re-verified 2026-10-18)

`GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<10-digit CIK>&type=8-K&dateb=&owner=include&count=20&output=atom`

| Request | Result |
|---|---|
| `CIK=0001717115` (or `CIK=1717115`), User-Agent `lit-radar/0.1 noreply@example.org` | **200** `application/atom+xml`, ~35 KB, 20 `<entry>` elements |
| `CIK=TEM` (the ticker), same User-Agent | **200** `text/html`, 3,460 bytes: "No matching Ticker Symbol." — no feed |
| `CIK=0000000001` (no such registrant) | **200** `text/html`: "No matching CIK." |
| any CIK, User-Agent `lit-radar/0.1 (+https://github.com/...) noreply@example.org` | 403, 4,819-byte HTML "Your Request Originates from an Undeclared Automated Tool" |
| any CIK, User-Agent `lit-radar/0.1 (no contact configured)` | 403 (same page) |

The fair-access policy (https://www.sec.gov/os/accessing-edgar-data) asks for
`User-Agent: Sample Company Name AdminContact@<sample company domain>.com` and at most 10 requests per
second. Two things matter in practice: a bare e-mail address (no `mailto:` token) and **no URL anywhere in
the header** — a URL is enough to be refused even with a valid address next to it.

## Ticker → CIK (why the feeds are not requested by ticker)

`browse-edgar` accepts `CIK=<ticker>`, but resolves it through EDGAR's **legacy ticker table**, which lags the
published https://www.sec.gov/files/company_tickers.json (~10,400 rows, refreshed daily). On 2026-10-18 the
legacy lookup failed for 8 of the 46 watched US tickers — recent listings (TEM, GRAL, CAI, FRNM, BLLN, GENB,
ALMR) and one renamed registrant (RVTY, formerly PKI) — although every one of them is in `company_tickers.json`.
Each failure is an HTTP 200 HTML page, so the HTTP layer counted a success and the Atom reader logged one WARNING
and yielded zero items: the filings were silently never collected.

What the pipeline does now, per run:

1. `litradar.track.sec_company_tickers` downloads `company_tickers.json` once (cached in `<cache_dir>/sec_company_tickers.json`
   for 7 days; the request carries the same `<tool> <e-mail>` User-Agent).
2. `sec.resolve` maps every watched 1–5-letter ticker to its 10-digit CIK; the feed URL carries the CIK and the
   feed label keeps the ticker (`SEC 8-K TEM`).
3. A ticker the table does not know is **not requested** (EDGAR could not resolve it either) and is named in
   one line of the run's error list, mapped to `--sources sec` by `litradar status` / `coverage`:
   `sec: 1 of 46 watched ticker(s) not in the SEC ticker table (company_tickers.json), no 8-K feed collected for them: XYZ (a foreign, delisted or misspelled ticker in companies.yaml)`.
4. If the table cannot be downloaded, the feeds fall back to `CIK=<ticker>` (which works for most registrants)
   and the run's error list says so:
   `sec: SEC ticker table unavailable (...); 8-K feeds requested by ticker, which EDGAR cannot resolve for recent listings or renamed registrants`.
5. A feed that answers something other than Atom (an HTML page) is reported once per run with EDGAR's own
   explanation, e.g. `sec: 2 of 46 8-K feed(s) answered an HTML page instead of Atom, no filings collected for them: GRAL (No matching Ticker Symbol.), TEM (No matching CIK.)`.
   The reader records this in the `outcome` dict `rss.news_items` / `rss.fetch_items` accept (`fetched`, `parsed`,
   `error`, `body`); the other feed kinds get a `feed <name>: not XML (...)` line each.

Run counters: `news.sec_feeds` (feeds requested), `news.sec_unresolved` (tickers skipped), `news.sec_not_atom`
(feeds that answered HTML), `news.sec` (filings stored).

## What the client does

* `litradar/http.py` composes the User-Agent as `lit-radar/0.1 <contact e-mail>` when
  `LITRADAR_CONTACT_EMAIL` is set and `lit-radar/0.1 (no contact configured)` otherwise; any URL in a
  custom base string is stripped. The same header goes to every host (Crossref and OpenAlex also accept it).
* `www.sec.gov` is paced at 0.11 s between requests (the 10 req/s limit).
* Without a contact e-mail the pipeline skips the SEC feeds entirely (ticker table included) and records one
  line in the run's error list (`sec: SEC 8-K feeds skipped (LITRADAR_CONTACT_EMAIL not set ...)`) so the gap
  shows in the digest.
* A 403 is logged at WARNING once per host (with the User-Agent hint for `sec.gov`), then at INFO.
* An HTTP 200 whose body is not a feed is still counted as a success by the HTTP layer (it does not inspect
  `Content-Type`); the news layer's `outcome` check above is what turns it into a reported error.

## Known limitations

* Every entry's title is `8-K  - Current report`; the Item codes (2.02 results, 1.01 material agreement,
  5.02 officer changes, ...) are only in `<summary>`. The pipeline prefixes the company name and date; the
  renderer does not yet surface the Item codes.
* Only companies whose `ticker` is 1–5 upper-case letters get a feed (foreign listings and `private` do not).
  `company_tickers.json` lists exchange-listed SEC registrants only, so a ticker outside it (a foreign primary
  listing written without its suffix, a delisted or misspelled symbol) is reported, never guessed.
* A company whose ticker changed keeps its CIK, so a rename is covered as soon as `company_tickers.json` (and the
  7-day cache) carries the new symbol; until then the old symbol is "not in the SEC ticker table".
* The feed covers material events only; for product and partnership news see docs/sources/news.md.
