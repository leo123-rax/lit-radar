# Europe PMC REST API — journals by ISSN, section search, open-access full text

Adapter: `litradar/sources/europepmc.py` → `Paper(source="europepmc")`.
Tests: `tests/test_sources_offline.py::test_europepmc_*` (canned payloads, no network).

## Endpoints

* Search: `GET https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=...&format=json&pageSize=1000&cursorMark=*&resultType=core`
* Full text: `GET https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML` (open-access subset only)

Query builders: `journal_query(issns, start, end)`, `section_query(field, terms, start, end, open_access_only=True)`
with `field` in METHODS / BODY / COMP_INT / ACK_FUND / AFF / ABSTRACT, and `date_clause(start, end)` =
`((FIRST_PDATE:[start TO end]) OR (CREATION_DATE:[start TO end] AND FIRST_PDATE:[start-30d TO end]))`: records first
published in the window, plus records that entered the index in the window with a publication date up to
`INDEX_LAG_DAYS` (30) earlier. PubMed records reach Europe PMC 1-14 days after publication (median ~8, docs/live.md),
so a publication-date window over the last day or two is structurally empty (`SRC:MED` 1-day: 27 records by
`FIRST_PDATE` vs 2,831 by `CREATION_DATE` on 2026-10-18) and the daily `update` would miss most Methods / COI
mentions without the second arm; the 30-day floor keeps legacy back-files out. `index_window=False` gives the plain
publication-date clause for historical pulls.

## Record shapes (verified live 2026-10-17, `resultType=core`)

| Record type | Where the venue name is | `license` | `isOpenAccess` |
|---|---|---|---|
| Journal article (`source=MED`) | `journalInfo.journal.title` (+ `issn`, `essn`, `medlineAbbreviation`) | `"cc by"`, `"cc by-nc-nd"`, ... for the OA subset; `null` otherwise | `Y` / `N` |
| Preprint (`source=PPR`) | **`bookOrReportDetails.publisher`** (`bioRxiv`, `medRxiv`, `Research Square`, `Preprints.org`, ...); `journalInfo` is `null`; there is **no top-level `publisher` key** | `null` on all 200 sampled | `N` on all sampled |

Sample of 200 PPR records first published 2026-10-10..17: Research Square 87, bioRxiv 73, Preprints.org 17,
medRxiv 12, PsyArXiv 3, Authorea 3, plus singletons. Before the fix every one of them was stored with
`journal = "preprint"` because the code read a top-level `publisher` that does not exist; `preprint_server()`
now reads the nested field (falling back to `journalInfo`, then the literal `preprint`).

## Licence and open-access flags

`to_paper` sets `Paper.license` from the search `license` field and keeps both `license` and
`is_open_access` in `Paper.extra` so the pipeline's data policy (`litradar/policy.py`) can decide how much
full text to store. `is_open_access(paper)` returns True/False/None. `attach_fulltext` returns False
**without a request** when the record is flagged `isOpenAccess: N`: `fullTextXML` answers HTTP 500 for
non-OA PMCIDs, and the default retry policy used to spend 1+2+4+8+16 = 31 s of sleep per such id.
`fulltext_xml` / `attach_fulltext` take `retries=1` by default for the same reason (`Http.get(retries=...)`).

## Abstract fallback from the full text

Around 15% of Europe PMC search records (Science family titles among them) carry no `abstractText`, and
those papers used to reach the digest as a title plus entity lists. When `attach_fulltext` has downloaded
the JATS document anyway, it now fills an empty `Paper.abstract` from the document's `<abstract>` element
(`litradar.sources.jats_abstract`; graphical/teaser abstracts are skipped) at no extra request. An abstract
the search record already had is never overwritten. Titles and abstracts from the search API are run
through `litradar.sources.clean_text` (JATS/HTML tags stripped, entities decoded, whitespace collapsed).

## Known limitations

* `isOpenAccess` is `N` for preprint records even when the server licence is CC; preprint full text is
  therefore not fetched from Europe PMC (only records with a PMCID were ever fetched).
* The JATS `<license>` element is not yet read back into `Paper.license` when the search field is empty.
