# News layer — publisher RSS, press wires, SEC filings, GDELT, and Google News (opt-in)

Code: `Pipeline.fetch_news` in `litradar/pipeline.py`; adapters `litradar/sources/rss.py` (any RSS/Atom) and
`litradar/sources/gdelt.py` (GDELT DOC 2.0). Config: `config/feeds.yaml`, `news:` and `sources.sec` in
`config/radar.yaml`, `watch_news` / `news_query` per company in `config/companies.yaml`.
Tests: `tests/test_news_pipeline.py`, `tests/test_gdelt_offline.py`, `tests/test_sources_offline.py::test_rss_parsers`.

## Why the layer was rebuilt

The previous default issued ~90 `news.google.com/rss/search` queries per run (one per watched company plus
`site:` queries for trade outlets). `https://news.google.com/robots.txt` serves `User-agent: *` /
`Disallow: /` with an allow-list that does not include `/rss`, and Google's Terms of Service prohibit
"using automated means to access content from any of our services in violation of the machine-readable
instructions on our web pages (for example, robots.txt files that disallow crawling ...)". Scripted polling
of that endpoint is therefore a terms violation regardless of how politely it is done, so it is now
**opt-in** (`news.google_news: false` by default) and the default layer is built from sources that permit
programmatic access.

## Layers

| Layer | Kind | Permission footing | What you get |
|---|---|---|---|
| Publisher RSS (`config/feeds.yaml`) | `news` | each publisher's own feed | headline + link + summary; STAT, BioPharma Dive, MedTech Dive, Science news (`science.org/rss/news_current.xml`, 11 items live), GenomeWeb Arc feed (robots-permitted with `Crawl-delay: 10`; see note) |
| Press wires | `press` | PR Newswire public RSS | ~20 items per list spanning roughly a day, so a weekly run sees only the last day; poll more often if wires matter |
| SEC EDGAR 8-K Atom | `sec` | SEC fair-access policy (`<tool> <e-mail>` User-Agent, ≤10 req/s) | material events for every company with a 1–5 letter US ticker; see docs/sources/sec.md |
| GDELT DOC 2.0 | `gdelt` | GDELT: "unlimited and unrestricted use for any academic, commercial, or governmental use ... without fee"; built for programmatic access | per-company articles from GDELT's global crawl; title + URL + date, no snippet |
| Google News RSS | `gnews` | robots.txt disallows `/rss`; Google ToS | **opt-in only**; titles + encoded `news.google.com/rss/articles/...` links, no snippet |

Every item passes `news.exclude_title_patterns` (insider-sale forms, stock-screener chatter). A per-company
item (GDELT or Google News) is kept only when the gazetteer finds that company in its text (`news.offtopic`
counts the drops), so a query for "GRAIL" cannot store "the holy grail of cooking".

## GDELT DOC 2.0

`GET https://api.gdeltproject.org/api/v2/doc/doc?query=<q>&mode=ArtList&format=json&timespan=7d&maxrecords=250&sort=DateDesc`

* `query` = `("<company A>" OR "<company B>" OR ...) <query_context[type]> sourcelang:<gdelt_language>`, e.g.
  `("Illumina" OR "10x Genomics" OR "Oxford Nanopore") (sequencing OR genomics OR proteomics OR "life sciences" OR instrument OR biotech) sourcelang:english`.
  **Companies are batched**: the watched companies that share a context clause are packed greedily into
  OR groups (`gdelt.batch_companies`) under the API's **250-character query limit** (`news.gdelt_max_query_chars`)
  and at most `news.gdelt_batch_size` (8) names per request, so ~70 companies are ~10 requests instead of 70.
  A batch of one is the bare phrase `"<company>"`, and a company with its own `news_query` in `companies.yaml`
  is always queried alone. OR groups **must** be parenthesised (the API otherwise answers `Queries containing
  OR'd terms must be surrounded by ().` as a 200 text/html body — treated as a rejected query). A quoted phrase
  under **five characters** is rejected the same way (`The specified phrase is too short.`) and voids the whole
  query it sits in, so a short company name is searched by its first alias of five or more characters
  (`"Seer"` → `"Seer Inc"`; the stored feed label is still the company name) and a company with no such alias is
  left out and counted in `news.gdelt_short_names`.
* Attribution: every article from a batch runs through the company gazetteer; it is stored only when the
  title names at least one batch member (`news.offtopic` counts the rest), tagged with every company found,
  and filed under `gdelt:<first matched batch member>`. Trade-off: `maxrecords` (250, sorted newest first)
  is shared by the batch, so over a 7-day window a very newsworthy member can crowd out a quiet one; a
  smaller `gdelt_batch_size` trades requests for completeness.
* `timespan` accepts `15min` .. `3m`; keep `news.gdelt_timespan` aligned with `window_days`.
* Response: `{"articles": [{url, url_mobile, title, seendate: "20261015T143000Z", socialimage, domain,
  language, sourcecountry}]}`; the literal body `{}` means no results.
* **Rate limit: one request every 5 seconds** (HTTP 429 with a plain-text body otherwise). The client paces
  `api.gdeltproject.org` at 8 s between request starts (an answer takes 2–12 s, so 5.5 s left no margin on the
  slow path), backs off 10 s / 20 s / 30 s on consecutive 429s and blocks the host only after **four** in a row
  (default hosts: two), because GDELT's 429s arrive in bursts that often clear within a minute. When the host is
  blocked the remaining batches are skipped without a request and the run records one line,
  `gdelt: host rate-limited (HTTP 429) during the run; N of M quer(y/ies) skipped (K companies not searched)`,
  logged once as `news degraded: ...`; `litradar status` files it under *degraded*, not *failed*
  (docs/live.md, "Which errors make `status` exit 1"). Counters: `news.gdelt_queries` (batches planned),
  `news.gdelt` (items stored, written as 0 when GDELT was asked and nothing came back), `news.gdelt_skipped`
  (batches never sent), `news.gdelt_companies_skipped`, `news.gdelt_failed_queries` (sent, no usable answer).
* Feed name is `gdelt:<company name>`; `NewsItem.summary` is empty (the digest links out).

### Live observations (2026-10-17, from one egress IP)

15 requests over about 90 minutes, honest User-Agent, never two within 5 s, including one series spaced
65 s apart: **12 answered HTTP 429** ("Please limit requests to one every 5 seconds ...") and each 429 took
7–9 s to arrive; the three 200s were 6 articles for `"Thermo Fisher" sourcelang:english` (3 of 6 titles
named the company; domains 247wallst.com, ipwatchdog.com, securityfocus.com ...), `{}` (0 articles) for
`"10x Genomics"` + context, and the syntax rejection for a deliberately un-parenthesised OR test. Through
the project's own client (`gdelt.company_news` for Illumina, 10x Genomics, Thermo Fisher Scientific, two
runs) the result both times was 429, a 5 s back-off, 429 again, host blocked for the run, **0 articles
stored**. The 65 s series rules out a simple per-request interval from this IP; the slow 429s look like
server-side load shedding or a penalty state. The research notes for this layer saw the same pattern
earlier the same day. The adapter is therefore built to degrade: a
rate-limited run stores nothing from GDELT, says so in the digest, and the other layers still run. Re-probe
from a CI runner or another network before relying on GDELT volume; the numbers above are what this
machine saw, not a measurement of the API's normal behaviour.

### Live observations (2026-10-18, same egress IP; before and after batching)

* **The IP was still being shed.** The first request after more than seven hours of silence answered 429 after
  11.5 s; a second one 10 s later, 429 after 11.4 s; a single request after a further six minutes of silence,
  429 after 7.0 s. No `Retry-After` header. Pacing alone cannot fix this from here: the limit is not a simple
  per-request interval from this address, and the same body (`Please limit requests to one every 5 seconds`) is
  returned whether or not the client was fast.
* **Before** (per-company queries, 5.5 s pacing, two 429s block): `run --days 1 --sources rss` sent **2** GDELT
  requests, both **429**, host blocked, **0** news items, `72 company quer(y/ies) skipped`.
* **After** (batched, 8 s pacing, 10/20/30 s back-offs, four 429s block): the same command planned **14**
  queries for the 72 watched companies and sent **8** requests: three answered 200 (6 companies → 0 articles;
  8 companies → 3 articles, 1 stored after gazetteer gating; 8 companies → rejected, `The specified phrase is too
  short.`, the "Seer" case now handled by the alias rule), then one dropped connection and **4 × 429** on the
  fourth query, host blocked, `10 of 14 quer(y/ies) skipped (44 companies not searched)`, **1** news item
  stored. Not a volume you can plan on, but the layer is no longer dead by construction: on a network GDELT
  serves normally the whole watchlist is ~14 requests (about two minutes), and `status` treats a shed run as
  degraded rather than failed.

### Coverage caveat

GDELT crawls mainstream and regional outlets in 65 languages; paywalled trade titles are largely absent
(`domain:` queries for two trade sites returned `{}` in the research probe). It complements publisher RSS,
wires and EDGAR; it does not replace them.

## GenomeWeb note

`https://www.genomeweb.com/arc/outboundfeeds/rss/?outputType=xml` is robots-permitted (`Crawl-delay: 10`)
and carries full article text in `content:encoded` (research probe: 100 items, median ~1,950 characters).
From the egress used on 2026-10-17 its CDN (Akamai) answered **403 to every non-browser client**, including
for `/robots.txt`, with four User-Agent/Accept variants. The entry is kept because a 403 costs one request
and one WARNING line, and the feed is the right one when it answers. Section feeds:
`/arc/outboundfeeds/rss/category/business-news/?outputType=xml`, `/category/diagnostics/?outputType=xml`.
Do not spoof a browser User-Agent to get around the 403.

## Google News (opt-in) details

When `news.google_news: true`: one query per watched company (`"<name>" <context> when:7d`) plus the
`gnews` entries in `feeds.yaml`. `when:7d` must be inside `q=` (a `when=` URL parameter is ignored) and
raises the share of in-window items materially. Items carry encoded links; decoding them requires fetching
a ~0.6 MB page per article plus an internal RPC, both robots-disallowed, so links are stored as served.
Enabling this is the fork owner's decision and risk.

## Changes to the default feed list (2026-10-17)

* Removed: a `site:360dx.com` query (the domain redirects to genomeweb.com), Google mirrors of Nature and
  Science news (the journal RSS feeds are already in `config/journals.yaml`), and a `site:endpts.com` query
  (the outlet moved to `endpoints.news`; its own `/feed/` answers 403, so it remains Google-only and opt-in).
* Added: GenomeWeb Arc feed (see note), Science news RSS.
* Kept: STAT, BioPharma Dive, MedTech Dive, PR Newswire biotechnology and medical-devices lists.

## Feed errors and link hygiene

* A feed body that is not XML (an HTML error page, a bot challenge, a truncated download) is reported
  **once per feed URL** at WARNING as `feed parse error in <feed name> (<url>): <parser error>; body starts
  '<!doctype html> ...'`; repeats within the run go to DEBUG. `litradar.sources.rss.bad_feeds()` returns the
  list for run reports.
* Article links lose feed-reader tracking parameters before they are stored (`af=R` on ascopubs,
  `dgcid=rss_...` on Nature, the `utm_*` family, Mailchimp/Facebook/Google click ids), so the digest and the
  dashboard carry canonical URLs. Every other query parameter is kept exactly as the feed served it.
* GDELT: once the host answers 429 four times in a row the shared client blocks it; the remaining batched
  queries are skipped without a request and the run records one error line, `gdelt: host rate-limited
  (HTTP 429) during the run; N of M quer(y/ies) skipped (K companies not searched)`, which the digest's
  coverage block lists under the errored sources tagged "(rate-limited: degraded, not an outage)".
