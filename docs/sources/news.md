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

* `query` = `"<company name>" <query_context[type]> sourcelang:<gdelt_language>`, e.g.
  `"Illumina" (sequencing OR genomics OR proteomics OR "life sciences" OR instrument OR biotech) sourcelang:english`.
  OR groups **must** be parenthesised (the API otherwise answers `Queries containing OR'd terms must be
  surrounded by ().` as a 200 text/html body — treated as a rejected query). Per-company overrides go in
  `companies.yaml` → `news_query`.
* `timespan` accepts `15min` .. `3m`; keep `news.gdelt_timespan` aligned with `window_days`.
* Response: `{"articles": [{url, url_mobile, title, seendate: "20261015T143000Z", socialimage, domain,
  language, sourcecountry}]}`; the literal body `{}` means no results.
* **Rate limit: one request every 5 seconds** (HTTP 429 with a plain-text body otherwise). The client paces
  `api.gdeltproject.org` at 5.5 s and backs off 5 s on a 429; two consecutive 429s block the host for the run
  and the pipeline records `gdelt: host rate-limited (HTTP 429) ...; N company queries skipped` in the
  digest's error list rather than failing silently. ~75 watched companies take about seven minutes.
* Feed name is `gdelt:<company name>`; `NewsItem.summary` is empty (the digest links out).

### Live observations (2026-10-17, from one egress IP)

Across three sessions spaced minutes apart (8 requests in all, 6–7 s between requests within a session,
honest User-Agent), the DOC API answered **429 on 6 of 8 requests, including the first request of each
session**; the two 200s were `{}` (0 articles) for `"10x Genomics"` + context with `timespan=7d`, and the
syntax rejection for a deliberately un-parenthesised OR test. Through the project's own client
(`gdelt.company_news` for three companies) the result was 429, a 5 s back-off, 429 again, host blocked for
the run, 0 articles stored. The research notes for this layer saw the same pattern earlier the same day
and read it as a per-IP penalty state that outlasts a burst by more than ten minutes. The adapter is therefore built to degrade: a
rate-limited run stores nothing from GDELT, says so in the digest, and the other layers still run. Re-probe
from a CI runner or another network before relying on GDELT volume; the numbers above are what this
machine saw, not a measurement of the API's normal behaviour.

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
