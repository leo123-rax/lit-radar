# Crossref REST API — per-journal DOI feeds

Adapter: `litradar/sources/crossref.py` → `Paper(source="crossref")`.
Tests: `tests/test_sources_offline.py::test_crossref_*` (canned payloads, no network).

## Endpoint

`GET https://api.crossref.org/journals/{ISSN}/works?filter=from-created-date:{start},until-created-date:{end}&rows=200&cursor=*&select=...&mailto=...`

`select` = `DOI,title,abstract,author,container-title,published,published-online,created,URL,type,funder,ISSN,subject,license`.
Paging is cursor-based (`message.next-cursor`); the client paces `api.crossref.org` at 0.2 s and sends the
contact e-mail as `mailto` (polite pool) and in the User-Agent.

## Why `created`, not `pub-date` (verified live 2026-10-17, window 2026-10-03..2026-10-17)

| Journal (ISSN) | `from-pub-date` | `from-created-date` | `from-online-pub-date` |
|---|---|---|---|
| Cell (0092-8674) | 0 | **30** | 0 |
| The Lancet (0140-6736) | 0 | **66** | 0 |
| Nature Methods (1548-7091) | 12 | 18 | 18 |

Several publishers deposit `published` at month precision (`{"date-parts": [[2026, 10]]}`), so a week-long
`from-pub-date` window never matches them. `from-created-date` filters on the day the DOI was registered,
which is what "new this week" means for a radar. If created-date returns nothing for an ISSN the adapter
retries once with `from-online-pub-date` (helps publishers that register DOIs long before release).

## Stored date

`Paper.pub_date` keeps the deposited publication date when it has day precision (`published`, then
`published-online`). When only a month is deposited the DOI registration day (`created.date-time`) is
used, so a Cell paper registered on 2026-10-15 is stamped 2026-10-15 rather than 2026-10-01 (which would
fall outside the window downstream). `extra["published_parts"]` keeps the raw deposited parts and
`extra["created"]` the registration day, so either can be recovered. `extra["issn"]` carries the record's
ISSNs for the journal-tier lookup. `_date()` tolerates `null` parts and string parts.

## Metadata hygiene

Crossref titles arrive with JATS/HTML markup and the publisher's line breaks (`<i>de novo</i>`,
`mouse\n   <i>Malat1</i>\n   long noncoding RNA`), and one record can list the same funder once per award
(24 copies of one NIH institute were seen live). `to_paper` runs title, container title and abstract through
`litradar.sources.clean_text` (tags stripped, entities decoded, whitespace collapsed) and keeps each funder
name once, in order, in both `extra["funders"]` and the `funding` section.

## Known limitations

* Abstracts are present only when the publisher deposits them (Nature portfolio and Cell Press mostly do;
  Science mostly does not).
* `until-created-date` is inclusive; a window that ends today will gain items on the same day as the run.
