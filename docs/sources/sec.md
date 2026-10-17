# SEC EDGAR — 8-K Atom feeds per watched company

Adapter: generated inside `Pipeline.fetch_news` (`litradar/pipeline.py`), parsed by the generic reader in
`litradar/sources/rss.py`; one `NewsItem(feed="SEC 8-K <TICKER>")` per filing, tagged with the company id.
Tests: `tests/test_news_pipeline.py` (offline), `tests/test_http_policy.py` (User-Agent shape, 403 logging).

## Endpoint (verified live 2026-10-17)

`GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<ticker>&type=8-K&dateb=&owner=include&count=20&output=atom`

| User-Agent sent | Result |
|---|---|
| `lit-radar/0.1 noreply@example.org` | **200** `application/atom+xml`, 34,808 bytes, 20 `<entry>` elements |
| `lit-radar/0.1 (+https://github.com/...) noreply@example.org` | 403, 4,819-byte HTML "Your Request Originates from an Undeclared Automated Tool" |
| `lit-radar/0.1 (no contact configured)` | 403 (same page) |

The fair-access policy (https://www.sec.gov/os/accessing-edgar-data) asks for
`User-Agent: Sample Company Name AdminContact@<sample company domain>.com` and at most 10 requests per
second. Two things matter in practice: a bare e-mail address (no `mailto:` token) and **no URL anywhere in
the header** — a URL is enough to be refused even with a valid address next to it.

## What the client does

* `litradar/http.py` composes the User-Agent as `lit-radar/0.1 <contact e-mail>` when
  `LITRADAR_CONTACT_EMAIL` is set and `lit-radar/0.1 (no contact configured)` otherwise; any URL in a
  custom base string is stripped. The same header goes to every host (Crossref and OpenAlex also accept it).
* `www.sec.gov` is paced at 0.11 s between requests (the 10 req/s limit).
* Without a contact e-mail the pipeline skips the SEC feeds entirely and records one line in the run's
  error list (`SEC 8-K feeds skipped: LITRADAR_CONTACT_EMAIL not set ...`) so the gap shows in the digest.
* A 403 is logged at WARNING once per host (with the User-Agent hint for `sec.gov`), then at INFO.

## Known limitations

* Every entry's title is `8-K  - Current report`; the Item codes (2.02 results, 1.01 material agreement,
  5.02 officer changes, ...) are only in `<summary>`. The pipeline prefixes the company name and date; the
  renderer does not yet surface the Item codes.
* Only companies whose `ticker` is 1–5 upper-case letters get a feed (foreign listings and `private` do not).
* The feed covers material events only; for product and partnership news see docs/sources/news.md.
