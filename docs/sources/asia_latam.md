# Japan, Korea and Latin America: J-STAGE, KCI and SciELO

Metadata-only adapters for three regional platforms whose articles are under-represented in the Western
indexes (many carry Japanese, Korean, Portuguese or Spanish titles, and a fair share have no Crossref DOI).
All three were verified live on 2026-10-17 from a residential IP with the `lit-radar/0.1` User-Agent.
Everything below that is not marked as quoted from documentation was established by those calls.

| Source | Region | Adapter | Endpoint | Search | Dates | Status |
|---|---|---|---|---|---|---|
| **J-STAGE WebAPI** (JST) | Japan, ~3,900 journals | `litradar/sources/jstage.py` | `https://api.jstage.jst.go.jp/searchapi/do?service=3` | server-side: abstract, full text, title, keywords, author, affiliation, journal, ISSN, journal code | publication **year** only; per-article release date in `updated` | works, no key; **restrictive terms** (see below) |
| **SciELO ArticleMeta** | Brazil + 15 Latin American countries, Spain, Portugal, South Africa | `litradar/sources/scielo.py` | `https://articlemeta.scielo.org/api/v1/` | none server-side (sweep by processing date, filter locally) | processing date; real publication date inside the record | works, no key, no stated limits |
| **KCI OAI-PMH** (NRF Korea) | Korea, KCI-listed journals | `litradar/sources/kci.py` | `https://open.kci.go.kr/oai/request` | none (OAI-PMH sweep, filter locally) | datestamp = KCI processing date | works, no key; windows are dominated by re-processed back catalogue, no total count exposed |
| KCI Open API (keyword search) | Korea | not implemented | `https://open.kci.go.kr/po/openapi/openApiSearch.kci?apiCode=articleSearch` | title / author / journal | | needs a key issued by KCI; schema unverifiable without one |
| KoreaMed / KoreaMed Synapse | Korea (KAMJE journals) | not implemented | | | | no API, RSS or OAI found; the journals are in PubMed / Europe PMC / Crossref |

`litradar/sources/script_lang.py` is a tiny shared helper: `detect_script_lang` guesses `extra["lang"]` from the
writing system (Hangul, kana, Han, Cyrillic, ...) when a source does not state the language, and `fold_text`
gives the case- and accent-insensitive form used by every local keyword filter (NFKD, drop the Latin combining
marks U+0300-U+036F, NFC, casefold: `genômica`, `Genómica` and `GENOMICA` all match `genom`; Hangul and kana are
left unchanged).

Every adapter yields `Paper` objects with `source` (`jstage` / `scielo` / `kci`), the English title when the
source has one (the gazetteer and ranker are English), `extra["title_native"]` with the native-script title,
`extra["lang"]`, `extra["country"]`, DOI when present, `pub_date`, authors, affiliations where the API carries them,
journal, URL and `extra["issn"]` (a list, so `Pipeline.journal_tier` can map it to `config/journals.yaml`).

---

## J-STAGE WebAPI (Japan)

* Manual (Japanese, PDF, ver. 2.0 dated 2026-03-26): <https://www.jstage.jst.go.jp/static/files/ja/manual_api.pdf>
* Terms of use: <https://www.jstage.jst.go.jp/static/pages/WebAPI/-char/ja>
* Overview page: <https://www.jstage.jst.go.jp/static/pages/JstageServices/TAB3/-char/ja>

### Request

`GET https://api.jstage.jst.go.jp/searchapi/do?service=3&abst=genome&pubyearfrom=2025&pubyearto=2026&start=1&count=1000`

| Parameter | Meaning (manual section 3.2) | Verified |
|---|---|---|
| `service=3` | article search (`2` = volume/issue list, `1` = journal search) | yes |
| `pubyearfrom`, `pubyearto` | 4-digit publication year of the volume; **no month/day filter exists** | yes |
| `text` | full-text search (partial match, case-insensitive) | 3,029 hits for `genome` in 2025-26 |
| `abst` | abstract | 466 hits |
| `article` | title | 153 hits |
| `keyword` | author keywords | 72 hits (2026) |
| `author`, `affil` | author name, affiliation | `affil=RIKEN` 257 hits (2026) |
| `material`, `issn`, `cdjournal` | journal title (partial), ISSN `XXXX-XXXX` (exact), J-STAGE journal code | yes |
| `start`, `count` | 1-based offset and page size; `count` max 1,000 (`count=2000` is silently served as 1,000); `start=1001` works | yes |
| `sortflg` | `1` relevance (default), `2` volume/issue/page; `2` needs `material` or `issn`, else `ERR_014` | yes |

Several words in one field are AND-ed with a space; values are UTF-8 URL-encoded, so native-script queries
work (`text=ゲノム` returned 976 hits for 2026). At least one search field must be given (`ERR_012`).

### Response (Atom + PRISM + OpenSearch)

`result/status`: `0` ok; `WARN_002` the hit count exceeds 1,000 (data is still returned, keep paging with
`start`); `ERR_001` zero hits (also returned when `start` is beyond the last hit); `ERR_003` concurrent-access
limit; `ERR_004`-`ERR_014` parameter errors. `opensearch:totalResults`, `startIndex`, `itemsPerPage`.

Per `entry`: `article_title{en,ja}`, `article_link{en,ja}`, `author{en,ja}/name`, `cdjournal`,
`material_title{en,ja}`, `prism:issn`, `prism:eIssn`, `prism:volume`, `prism:number`, `prism:startingPage`,
`prism:endingPage`, `pubyear`, `joi`, `prism:doi`, `systemcode`, `systemname`, `title`, `link@href`, `id`,
`updated` (W3CDTF with `+09:00`; the manual calls it 記事の公開日, the article's release date).
Titles are CDATA with inline `<i>`, `<sub>`, `<sup>`; the adapter strips them. **No abstract and no
affiliations are returned**, so these papers only match the gazetteer on title text.

Sample (`text=genome`, pubyear 2026, `count=1000`): status `WARN_002`, 1,312 hits, 1,000 entries, 999 with a DOI
(JaLC-registered `10.xxxx/...`, most not in Crossref), 169 released in the previous 30 days.

### Adapter behaviour

`jstage.fetch(http, start, end, terms=[...], field="abst", journals=[cdjournal...], issns=[...], max_records=5000,
year_slack=1)` runs one query per term / journal code / ISSN with `pubyearfrom = start year - year_slack`
(volumes lag their online release), pages 1,000 at a time, keeps entries whose `updated` date falls inside
`[start, end]`, and de-duplicates by DOI. `jstage.hit_count(http, params)` sizes a query with one request.
Requests are paced at 1 per second (`MIN_INTERVAL`); the API has a concurrency limit (`ERR_003`).

Errors: `ERR_003` arrives in an HTTP 200 body, so the shared client's retry/backoff never sees it; `search` waits
`RETRY_BACKOFF` (5 s, doubling) and re-requests the **same** page up to `RETRY_MAX` (2) times before abandoning
the query. Any query that ends on another non-OK status (parameter errors, HTTP failure, unparsable body) is
abandoned with a warning. The `stats` dict passed to `fetch` reports `queries`, `entries`, `in_window`, `papers`,
`errors` (abandoned queries) and `truncated` (queries whose `totalResults` exceeded `max_records`, also logged
with both numbers), so a run report can show partial coverage; the adapter itself never raises.

### Terms that matter for a persistent corpus

Quoted from the terms page (2026-10-17):

* Article 3(5): 「利用者は、本API提供情報を機械可読な状態でサーバもしくはクラウド等に24時間以上保存又はキャッシュしないものとします。」
  (API-provided data may not be stored or cached in machine-readable form on a server or cloud for 24 hours or more.)
* Article 4(10): no mass download of listed information by mechanical means
  (「機械的及びそれに準じた手段を用いて本サービスに掲載する情報を大量にダウンロードする行為」).
* Article 9: display 「Powered by J-STAGE」 with a link to J-STAGE wherever the data is shown.
* Article 2: non-commercial use needs no application; commercial use, paid or free, needs prior approval from JST.
* Article 5: JST may limit access counts and times at any moment.

A SQLite corpus kept for months on a hosted runner (the GitHub Actions cache) sits uneasily with Article 3(5).
The adapter is therefore meant to ship **disabled by default**; enable it after reading the terms, for example
for laptop runs, for a pointer-only mode (keep DOI + URL and re-fetch titles on demand), or after asking JST.

### Live check (2026-10-17, window 2026-09-17..2026-10-17)

`abst` search for `genome` OR `genomics`, publication years 2025-2026: 529 hits scanned in 2 queries, 54 entries
released inside the window, **52 unique papers** (all 52 with a DOI; English title available for 33, Japanese-only
for 19). 8 requests, 14 s. Hit counts for the same years: `abst:genome` 466, `abst:genomics` 63, `text:genome` 3,029,
`text:genomics` 1,529, `article:genome` 153, `article:genomics` 19. Re-run the same day after the review fixes
(`max_records=1000`): the same 52 papers, `stats` `{queries 2, entries 529, in_window 54, papers 52, errors 0,
truncated 0}`, 2 requests, 0 failures, 10.6 s.

---

## SciELO via ArticleMeta (Latin America, Spain, Portugal, South Africa)

* API root: <https://articlemeta.scielo.org/> ("Articles Metadata API").
* Official specification (reStructuredText, Portuguese) in the project repository `scieloorg/articles_meta`:
  * <https://github.com/scieloorg/articles_meta/blob/master/docs/source/api/article_identifiers.rst>: `limit`
    max 1,000; `from` / `until` ISO dates (defaults 1500-01-01 / today) filtering the processing date; `offset`.
  * <https://github.com/scieloorg/articles_meta/blob/master/docs/source/api/article.rst>: `format` JSON by default,
    plus `xmldoaj`, `xmlpubmed`, `xmlcrossref`, `xmlrsps`, `xmlwos`; `body=true` adds the HTML full text.
  * <https://github.com/scieloorg/articles_meta/blob/master/docs/source/api/journal.rst>: `issn` required,
    `collection` optional; the response is a JSON **list** of journal records.
  The rendered copy (scielo.readthedocs.io, project `articlemeta`) answered HTTP 404 on 2026-10-17; use the
  repository files. Whatever is not covered there (collection list, response sizes, `format=opac`) comes from
  live calls.

### Endpoints verified

| Call | Returns |
|---|---|
| `GET /api/v1/collection/identifiers/` | 36 collections `[{code, acron, name{en,...}, status, type, is_active}]`: scl Brazil, arg, chl, col, cub, ecu, per, pry, ury, bol, cri, dom, mex, ven, wid West Indies, esp Spain, prt Portugal, sza South Africa, spa Public Health, plus books, preprints, data and several "diffusion" magazines |
| `GET /api/v1/article/identifiers/?collection=scl&from=2026-09-17&until=2026-10-17&limit=1000&offset=0` | `{"meta": {"limit", "offset", "filter", "total"}, "objects": [{"code", "collection", "processing_date", "doi"}]}`; `limit` is capped at 1,000 (`limit=5000` answers `limit: 1000`); omit `collection` to sweep every collection; `from`/`until` filter **processing_date**, not publication date |
| `GET /api/v1/article/?collection=scl&code=S2317-15372026000101013&format=opac` | one document, ~25 KB JSON. **`opac` is not in the documented format list** (observed to work on 2026-10-17: the documented JSON without `citations`, ~7x smaller for a well-cited article); the adapter retries once with the documented `format=json` when an `opac` call yields nothing. `format=json` / `xylose` add `citations` (~170 KB); `xmlrsps` JATS/SPS XML (~32 KB); `xmlwos`, `xmlcrossref` deposit XML. Unknown code: HTTP 200, body `null` |
| `GET /api/v1/journal/?collection=scl&issn=2317-1537` | journal record with `v100` title, `v441` subject areas, `v350` languages, `v310` country, `v435` ISSNs |
| `GET /api/v1/journal/identifiers/?collection=scl` | 427 journals in scl |

30-day identifier totals on 2026-10-17: all collections 4,186; scl 2,055; sza 650; arg 360; col 208; chl 164; per 131;
esp 31; mex, cub, ven 0. The `preprints` collection had 0 identifiers in the window.

### Document shape (ISIS field notation `"vNN": [{"_": value, "<subkey>": ...}]`)

Top level: `code` (SciELO PID = `S` + ISSN + year + issue + order; the adapter derives the journal ISSN from it),
`collection`, `doi`, `processing_date`, `publication_date` (year), `document_type`, `fulltexts{pdf,html}{lang: url}`.
`article`: `v12` titles `[{l, _}]`, `v83` abstracts `[{a, l}]`, `v85` keywords `[{k, l}]`, `v10` authors `[{n given,
s surname, k ORCID, 1 "aff1 aff2", r role}]`, `v70` affiliations `[{i id, _ institution, 1/2 divisions, c city, s state,
p country}]`, `v240` normalised affiliations, `v58` funding agencies, `v102` acknowledgements, `v40` original language,
`v65` issue date `YYYYMMDD` (month/day may be `00`), `v223` online date, `v265 [{k: real|expected, v}]`, `v91` processing
date, `v237` DOI, `v35` ISSN, `v100` journal, `v30` abbreviation, `v62` publisher, `v880` PID.
`title` (the journal): `v441` subject areas (Health Sciences, Biological Sciences, Agricultural Sciences, Exact and Earth
Sciences, Engineering, Human Sciences, Applied Social Sciences, Linguistics, Letters and Arts), `v854` Web of Science
categories, `v350` languages, `v310` country, `v690` site host.

### Adapter behaviour

`scielo.fetch(http, since, until, collections=None, subject_areas=("Health Sciences", "Biological Sciences",
"Agricultural Sciences"), keywords=[...], max_articles=2000)`:

1. lists identifiers by processing date (all collections unless `collections` is given). A failed identifier
   page (HTTP error, blocked host, non-JSON body) ends that sweep with a warning and is counted in
   `stats["identifier_errors"]`, so a partly swept window is visible in the run report instead of silent;
2. stops as soon as `max_articles` article fetches have been made: no further identifier pages and no further
   journal lookups; `stats["capped"]` = identifiers of the started sweep(s) left unfetched, computed from
   `meta.total` (collections not yet started are not counted);
3. looks each journal up once per ISSN and skips articles whose journal has none of the configured subject areas
   (journals the API does not know are kept);
4. fetches each remaining article (`format=opac`, falling back to `format=json`) and keeps it when any keyword
   appears in **any** title, **any** abstract (all languages, kept in `extra["abstracts"]`) or the author keyword
   list, matched case- and accent-insensitively via `fold_text` (empty `keywords` keeps everything);
5. `pub_date` = online date (`v223` / `v265 real`) > issue date (`v65`) > processing date > year, recorded in
   `extra["pub_date_basis"]`; `extra["processing_date"]` says when SciELO loaded the record.

Funding agencies land in `sections["funding"]`, acknowledgements in `sections["ack"]`, subject areas and WoS
categories in `topics`. Requests are paced at 4 per second; the service states no limits. Budget: one request per
1,000 identifiers, one per distinct journal, one per article fetched (expect 150-500 articles per week for the
life-science areas across all collections). `stats` keys: `identifiers`, `identifiers_total`, `identifier_errors`,
`skipped_subject`, `fetched`, `missing`, `kept`, `capped`.

SciELO lag is real: records enter ArticleMeta weeks to months after online publication, so a paper processed this
week may carry a `pub_date` from several months ago and fall outside a strict publication-date digest window.

### Not usable (tested 2026-10-17)

* `search.scielo.org` answers HTTP 403 with a JavaScript proof-of-work challenge, even to a browser User-Agent.
* `www.scielo.br/oai/scielo-oai.php` returns the HTML site; `/oai/` a 502; `articlemeta.scielo.org/oai/` a 404.
* SciELO Preprints OAI-PMH (`https://preprints.scielo.org/index.php/scielo/oai`) answers `Identify` and a bare
  `ListIdentifiers` (500 headers per page) but HTTP 500 to any `from`/`until`, date-only or full timestamp.
  A full-sweep adapter with local date filtering would work; not built here.

### Live check (2026-10-17, window 2026-09-17..2026-10-17)

`scielo.fetch(..., collections=None, keywords=["genom"], max_articles=900)` over all collections with the default
life-science subject areas: **4,186 identifiers** processed in the window; 1,847 skipped because the journal's subject
areas were outside Health / Biological / Agricultural Sciences; **900 articles fetched** (the cap; 1,439 eligible
identifiers were not fetched); 0 missing documents; **11 articles matched `genom`** in a title, abstract or keyword
(10 from `scl` Brazil, 1 from `col` Colombia; 10 English-titled, 1 Spanish; all 11 with a DOI; all dated from the
online-publication field, 10 of them inside the window). Extrapolating the hit rate to the 2,339 eligible identifiers
gives roughly 29 genomics papers for the month (estimate, not measured). 1,274 requests, 0 failures, 347 s at
4 requests per second.

Re-run the same day after the review fixes (`collections=["scl", "col", "mex"]`, life-science areas, `keywords=["genom"]`,
`max_articles=250`): 434 identifiers consumed of the 2,055 in `scl` before the cap, 183 skipped by subject area,
250 fetched, 0 missing, **6 kept** (all English-titled, all with a DOI, all 6 with `pub_date` inside the window),
`capped` 1,622 (= 2,055 - 434 + 1; the `col` and `mex` sweeps never started, so they are not counted),
`identifier_errors` 0; 358 requests, 0 failures, 98 s. No `format=json` retry was needed.

---

## Korea

### KCI OAI-PMH (implemented, with limits)

* Guide (Korean): <https://www.kci.go.kr/kciportal/po/openapi/openDataOaiPmhView.kci>; data-services overview:
  <https://www.kci.go.kr/kciportal/po/openapi/openDataPackGuide.kci?datasetBean.dtstTyCd=00>.
* Licence stated on the guide page: attribution required, no commercial use
  (「저작자표시-출처표시 ... 영리목적으로 사용불가능 합니다」).
* Base URL `https://open.kci.go.kr/oai/request`. `Identify`: repositoryName "KCI (Korea Citation Index) Open Access
  Repository", earliestDatestamp 2013-03-21, granularity `YYYY-MM-DD`. `ListSets`: `ARTI` Article, `ARTI_CONF`
  Conference Article, `JOUR` Journal. `ListMetadataFormats`: `oai_dc`, `oai_kci`
  (schema `http://www.kci.go.kr/kciportal/OAI/oai_kci.xsd`).
* `ListRecords&set=ARTI&metadataPrefix=oai_kci&from=2026-09-17&until=2026-10-17`: 100 records per page,
  `resumptionToken` of the form `from:until:pagesize:next_record_id` (e.g. `2026-09-17:2026-10-17:100:168917`
  means the next page starts at `oai:kci.go.kr:ARTI/168917`; the last field is **not** a complete-list size, and
  no total count is exposed).

`oai_kci` record: `journalInfo{journal-name, pissn, eissn, publisher-name, pub-year, pub-mon, volume, issue}`,
`articleInfo@article-id` (ART...) with `article-categories` (KCI subject, e.g. 의학, 생물학, 한의학),
`title-group/article-title@lang=original|english`, `author-name/author{name, affiliation}`,
`abstract-group/abstract@lang=original|english`, `language`, `fpage`, `lpage`, `doi` (as `http://dx.doi.org/...`),
`uci`, `citation-count`, `url`, `verified`. `oai_dc` records carry the same in Dublin Core (`dc:title@lang`,
`dc:creator` "name(affiliation); ...", `dc:subject`, `dc:identifier@type=journalInfo|artiId|citedCnt|doi|uci`,
`dc:date YYYY-MM`, `dc:url`); the adapter parses both.

`<language>` holds the **Korean name of the language, not a code**: `한국어` on 100/100 records of the 2026-10-10..17
window re-sampled on 2026-10-17 (`영어` English, `일본어`, `중국어`, ... are the other values KCI uses). `kci.KCI_LANG`
maps those names (and English names) to ISO 639-1; a bare two-letter code is accepted as-is; anything else, or an
empty element, falls back to the script of the native title (Hangul -> `ko`, kana -> `ja`, Han-only -> `ko` for this
source). The raw value is kept in `extra["language_raw"]`. (An earlier revision sliced the first two characters
of the raw value and produced `한국` for every live record.)

Licence: the OAI guide page says attribution and **no commercial use** (「출처표시 ... 영리목적으로 사용불가능
합니다」), while KCI's data-services page (`openDataPackGuide.kci?datasetBean.dtstTyCd=00`) shows the 공공누리
제1유형 badge (attribution; 「영리 목적으로 이용할 수 있습니다」). The two pages disagree (both re-read 2026-10-17);
this document and the adapter quote the stricter OAI page. Either way a source credit is required (wiring step 5).

Limits observed on 2026-10-17 (the adapter is defensive about each):

* **Datestamp = processing date.** A bulk re-processing on 2026-10-05 re-stamped a large part of the back
  catalogue: the first 500 records of the 30-day window were all published 2009-2014 (499 of 500 in one subject,
  한의학 Korean medicine; the 7-day window looked the same). The window's size is unknown because no
  complete-list size is exposed. `fetch` drops records whose `pub-year` is older than `min_pub_year` (default:
  window year - 1) and `max_pages` caps the harvest at 100 records per page.
* **Paging works only by following the returned token verbatim** (stateless, no cookie needed; verified with
  three consecutive pages starting at ARTI/18901, /168917, /169017). Editing the token's third field does not move
  the cursor, so hand-built offsets are useless. `list_records` stops when a page repeats identifiers or a token
  repeats, so a misbehaving server cannot loop it.
* Tokens do not encode the metadata format: the server falls back to `oai_dc` on token requests but accepts a
  (non-standard) `metadataPrefix` next to the token, which the adapter sends. `ListIdentifiers` never returned a
  token. Malformed dates (`2026/10/10`) answer `noRecordsMatch`, not `badArgument`.

While the re-stamped back catalogue dominates the datestamp index, this source yields few or no current papers
per run; raising `max_pages` buys more records at 100 per request (about 1 s each) without any guarantee of reaching
2026 publications. Day-by-day windows do not help, since the datestamp is the re-processing day.

`kci.fetch(http, since, until, keywords=[...], categories=[...], min_pub_year=None, max_pages=10)` matches
`keywords` against both titles and both abstracts case- and accent-insensitively (`fold_text`), so `genom` matches
`Genômica` and `전장유전체` matches itself; `stats` reports `records` and `kept`.

### KCI Open API (documented, not implemented)

`https://open.kci.go.kr/po/openapi/openApiSearch.kci?apiCode=articleSearch&title=genome&displayCount=10&key=...`
(also `apiCode=articleDetail&id=ART...`, `journalSearch`) returns XML `<MetaData><inputData>...<outputData><result>`.
Without a key the result is `필수 요청 파라미터가 없음 => key`; with an unregistered key `등록되지 않은 key 입니다`.
Keys are requested at `https://www.kci.go.kr/kciportal/po/openapi/openApiKeyRequest.kci?datasetBean.dtstTyCd=02`
(login required). The record schema could not be verified without a key, so no adapter was written; the Open API list
page (<https://www.kci.go.kr/kciportal/po/openapi/openApiList.kci>) advertises article basic info, article detail,
references and citation indicators.

### KoreaMed and KoreaMed Synapse (documented, not implemented)

`https://koreamed.org/` and `https://synapse.koreamed.org/` load (HTTP 200) but expose only an HTML search form
(`POST /advanced/`); no JSON, RSS, OpenSearch or OAI-PMH endpoint was found, and `/oai`, `/rss`, `/api/search`
answer 404. The KoreaMed homepage itself points to Crossref's text-and-data-mining API. KAMJE journals carry
Crossref DOIs and are indexed in PubMed / Europe PMC, so the existing `europepmc`, `crossref` and `openalex`
adapters cover them when their ISSNs are added to `config/journals.yaml`. KoreaScience (KISTI) is likewise an HTML
site (`/openapi/`, `/oai/`, `/search/rss.page` all 404).

### Live check (2026-10-17, window 2026-09-17..2026-10-17)

`kci.list_records(..., max_pages=5)`: 500 records in 5 pages (all distinct, 5 requests); publication years 2009-2014
only (2013: 67, 2011: 64, 2010: 63, 2009: 63, 2012: 62, 2014: 61); subjects 한의학 499, 한국어와문학 1.
Keyword `genom` / `유전체` matched **0** of the 500 in any year. `kci.fetch(...)` with the default `min_pub_year`
(2025) and the same keywords kept **0** papers. A 7-day window (3 pages, 300 records) showed the same 2011-2016
back-catalogue profile. 13 requests, 0 failures, 142 s. In other words the endpoint, the harvest and the parsing all
work, but the current datestamp index carries no recent Korean genomics literature to count.

Re-run the same day after the language fix (2 pages, 200 records, `min_pub_year=2000`): `<language>` values 한국어
195, 영어 4, empty 1 -> `extra["lang"]` `ko` 195, `en` 5 (the empty one has a Latin-script title); publication
years 2004-2016; the default filters (`min_pub_year` 2025, keywords `genom` / `유전체`) kept 0 of 200. 4 requests,
0 failures, 41 s.

---

## Wiring (not applied here; other adapters are built in parallel)

1. `litradar/pipeline.py`, imports:
   ```python
   from .sources import jstage as jstage_src
   from .sources import kci as kci_src
   from .sources import scielo as scielo_src
   ```
   In `fetch_papers`, after step 6 (RSS):
   ```python
   # 7. J-STAGE (Japan): keyword / journal-code queries, filtered to the window by release date
   if self._enabled("jstage", only):
       cfg = self._src("jstage")
       try:
           for p in jstage_src.fetch(self.http, since, until, terms=cfg.get("terms") or [], field=cfg.get("field", "abst"),
                                     journals=cfg.get("journals") or [], issns=cfg.get("issns") or [],
                                     max_records=int(cfg.get("max_records_per_query", 5000))):
               add(p, "jstage")
       except Exception as exc:  # noqa: BLE001
           self.errors.append(f"jstage: {exc}")
           log.exception("jstage failed")

   # 8. SciELO (Latin America, Spain, Portugal, South Africa): sweep by processing date, filter locally
   if self._enabled("scielo", only):
       cfg = self._src("scielo")
       try:
           for p in scielo_src.fetch(self.http, since, until, collections=cfg.get("collections") or None,
                                     subject_areas=cfg.get("subject_areas", list(scielo_src.LIFE_SCIENCE_AREAS)),
                                     keywords=cfg.get("keywords") or [], max_articles=int(cfg.get("max_articles", 2000))):
               add(p, "scielo")
       except Exception as exc:  # noqa: BLE001
           self.errors.append(f"scielo: {exc}")
           log.exception("scielo failed")

   # 9. KCI (Korea) OAI-PMH: capped harvest, filtered locally
   if self._enabled("kci", only):
       cfg = self._src("kci")
       try:
           for p in kci_src.fetch(self.http, since, until, keywords=cfg.get("keywords") or [], categories=cfg.get("categories") or [],
                                  min_pub_year=cfg.get("min_pub_year"), max_pages=int(cfg.get("max_pages", 10))):
               add(p, "kci")
       except Exception as exc:  # noqa: BLE001
           self.errors.append(f"kci: {exc}")
           log.exception("kci failed")
   ```
2. `config/radar.yaml`, under `sources:`:
   ```yaml
   jstage:
     enabled: false          # read https://www.jstage.jst.go.jp/static/pages/WebAPI/-char/ja first (24-hour caching clause)
     field: abst             # abst | text | article | keyword | author | affil
     terms: [genome, genomics, sequencing, single-cell, spatial transcriptomics, proteomics, CRISPR]
     journals: []            # J-STAGE journal codes (cdjournal), e.g. [plantbiotechnology, jsbbs]
     issns: []
     max_records_per_query: 5000
   scielo:
     enabled: true
     collections: []         # empty = all 36 collections; e.g. [scl, mex, arg, chl, col]
     subject_areas: [Health Sciences, Biological Sciences, Agricultural Sciences]
     keywords: []            # empty = keep every article in those areas
     max_articles: 2000      # article fetches per run
   kci:
     enabled: true
     keywords: []            # e.g. [genom, 유전체, sequencing]
     categories: []          # KCI subject strings, e.g. [의학, 생물학, 약학]
     min_pub_year: null      # default: window year - 1
     max_pages: 10           # 100 records per page, ~1 s each; the window has no known size
   ```
3. `litradar/http.py` needs no change: each adapter registers its own pacing in `http.min_interval`
   (`api.jstage.jst.go.jp` 1.0 s, `articlemeta.scielo.org` 0.25 s, `open.kci.go.kr` 1.0 s).
4. `docs/SOURCES.md`: add three rows to "Papers and preprints" pointing here; `litradar/cli.py` `--sources` help
   string: append `jstage,scielo,kci`.
5. `litradar/digest/render.py` (required by the sources' terms, not optional): after the paper sections, append an
   attribution footer built from the sources actually rendered:
   * when any rendered paper has `source == "jstage"`: `Powered by J-STAGE` linking to <https://www.jstage.jst.go.jp/>
     (J-STAGE terms Article 9: the credit with a link must appear wherever API data is displayed);
   * when any rendered paper has `source == "kci"`: `Source: KCI (Korea Citation Index), National Research Foundation
     of Korea` linking to <https://www.kci.go.kr/> (출처표시, source attribution, required by the OAI guide page);
   * SciELO states no credit requirement; a `Source: SciELO` line is courteous.
   `Paper.extra["lang"]` / `["country"]` (JP / KR / BR ...) are available for a language or country breakdown when
   the renderer grows one.

## Tests

`tests/test_sources_asia_latam.py` (offline, canned payloads in `tests/fixtures/jstage_articles.xml`,
`kci_oai_kci.xml` with `<language>한국어</language>` as the server sends it, `scielo_article_opac.json`): normal
records, empty results (`ERR_001`, `noRecordsMatch`, empty identifier pages), malformed payloads (non-XML, `None`,
non-dict JSON, `null` article with the `format=json` retry), pagination stop (J-STAGE `start`/`totalResults` and
`max_records` truncation count, SciELO `offset`/`total`, identifier-page failure counted mid-sweep, `max_articles`
stopping before further pages or journal lookups, KCI repeated pages and tokens, `max_pages`), J-STAGE `ERR_003`
retry on the same page then abandonment (stubbed sleep), list- and dict-shaped `/journal/` payloads, window /
keyword / category / subject-area filters with accent-insensitive keywords over every title and abstract,
de-duplication, KCI language-name mapping and script fallback, `fold_text`.
