# China coverage: GSA, GSA-Human, ChinaXiv, the CNKI/Wanfang/CQVIP boundary, and Chinese companies

Tested live on 2026-10-17 from a residential IP with the project's honest `lit-radar/0.1` User-Agent.
Everything below is anonymous access to public metadata; nothing logs in, nothing bypasses a browser check.

Why this matters for a global census: native GSA records (CRA/CRX/CRR) are **not** mirrored into INSDC
(SRA/ENA/DDBJ). In the 30 days to 2026-10-17 GSA released 33,480 native experiments, of which 11,014
(33%) were on MGI/BGI platforms, against 6,182-19,334 MGI experiments visible in SRA over the same
window (see the SRA section for why those two numbers differ). A census built on SRA + ENA alone
undercounts Chinese platforms by roughly half.

## Summary table

| Source | Status | Adapter | What it yields | Window digits (2026-09-17 .. 10-17) |
|---|---|---|---|---|
| **GSA search page** (`ngdc.cncb.ac.cn/gsa/search`) | works, HTML, no key | `litradar/sources/gsa.py` `platform_histogram()`, `fetch()` | per-experiment `ArchiveRecord(archive='gsa')` with CRX/CRA, platform **family**, strategy, exact release day; facet histogram in one request | 33,480 native (NGDC) experiments: ILLUMINA 21,891 / BGISEQ 11,014 / PACBIO_SMRT 451 / OXFORD_NANOPORE 86 / CAPILLARY 18 / GENEMIND 11 / CapitalBio 8 / ION_TORRENT 1. Including INSDC mirrors the same index holds 363,091. |
| **GSA statistics JSON** (`/gsa/statistics/statistics.json`) | works, JSON (~12 MB), no key | `gsa.fetch_statistics()` + `litradar/gsa_snapshots.py` | cumulative counts per instrument **model** (80 models), CRA created per month, organisms, sample types, funders, organizations by country, 17,850 dataset-linked publications (DOI + PMID + CRA) | DNBSEQ-T7 112,846 experiments all-time (#5 overall), MGISEQ-2000 23,398, BGISEQ-500 13,905, DNBSEQ-G99 2,179, GenoLab M 453, FASTASeq 300 268; 2,115,280 experiments total, 1,233,693 released |
| **GSA browse pages** (`/gsa/browse/**`) | **browser-gated** (nginx JavaScript cookie challenge) | not used | the only place the instrument model of a native CRX is shown | — |
| **GSA RunInfo export** (`/gsa/search/getRunInfo`) | works but ignores the ReleaseDate term; ~10k-row cap | not used | CSV with Run, ReleaseDate, Platform (family), BioProject, TaxID … | returns the first ~10k rows of the whole index regardless of the query |
| **GSA-Human** (`/gsa-human/browse/`, `/gsa-human/ajaxb/runinstudy`) | works, HTML list + JSON runs, no key | `litradar/sources/gsa_human.py` `fetch()` | per-experiment `ArchiveRecord(archive='gsa-human')` with **model-level** platform, HRA study, PRJCA, organization, release date, Homo sapiens | 243 studies released; first 40 studies = 3,581 experiments: DNBSEQ-T7 2,481 / NovaSeq 6000 911 / NovaSeq X Plus 108 / NextSeq 550 53 / BGISEQ-500 22 / HiSeq X Ten 6 (100% matched to the instrument gazetteer) |
| **ChinaXiv OAI-PMH** (`global.chinaxiv.org/oaiapi/getdata`) | works, documented, no key | `litradar/sources/chinaxiv.py` `fetch()` | `Paper(source='chinaxiv', kind='preprint')` with Chinese title/abstract/authors/keywords/subject, derived DOI, abs URL | 275 distinct records (3 pages): 天文学 122, 核物理学 19, 图书馆学 19, … life-science subjects 33 (临床医学 5, 预防医学与公共卫生学 5, 农业基础学科 2, 植物生态学 2, 药物设计 1 …); 99 Chinese-language titles |
| ChinaXiv main-site OAI (`chinaxiv.org/oai/request`) | **access-restricted** ("Sorry! You have no right to access this web.") | not used | — | — |
| ChinaXiv RSS | none (`/rss`, `/rss.xml`, `/feed` → 404); search pages answer 403 to scripts | not used | — | — |
| **CNKI / Wanfang / CQVIP** | licensed databases, no public API | not used (boundary) | — | see below |

## 1. GSA (Genome Sequence Archive, CNCB-NGDC)

Handbook and submission guide: https://ngdc.cncb.ac.cn/gsa/support/documents (there is no formal API
documentation; the endpoints below were read from the site's own front-end JavaScript and verified live).
robots.txt disallows only `/openlb/`.

### 1a. Search page = per-experiment feed with exact release day

```
GET https://ngdc.cncb.ac.cn/gsa/search
    searchTerm = "MM/DD/YYYY - MM/DD/YYYY"[ReleaseDate]     # advanced-search syntax; one day = same date twice
    pageSize   = 200                                          # honoured
    from       = <1-based page number>
    centerSelectedItem = NGDC   centerSelected = true         # facet: NGDC = native GSA; INSDC = mirrored SRA/ENA/DDBJ
```

Response is server-rendered HTML: `Total Items:&nbsp;N`, then one hit per
`<input class="checkbox_class" data-value="CRX…">` with fields *Title*, *Accession* (link
`browse/<CRA>/<CRX>`), *Platform name*, *Source name*, *Strategy name*, *Selection name*. The sidebar
facets (`platform_block`, `strategy_block`, `source_block`, `center_block`) carry counts for the whole
result set, so `gsa.platform_histogram()` costs one request. The *Platform name* is the **family**
(ILLUMINA, BGISEQ, OXFORD_NANOPORE, PACBIO_SMRT, GENEMIND, SALUS, ELEMENT, ULTIMA …), not the model.

`gsa.fetch(http, start, end)` sweeps the window one day at a time (so `first_public` is exact) and labels
`instrument_model` as `"<FAMILY> (model n/a)"`. The gazetteer resolves `BGISEQ (model n/a)` to the MGI
DNBSEQ entry (vendor MGI Tech, 688114.SS); `ILLUMINA (model n/a)` resolves to no model, so the digest's
vendor-share table falls back to the platform string, which is the honest outcome. Cost: a 200-hit page
is ~900 KB and 4-9 s; one day (2026-10-16) had 813 native experiments = 5 pages; a week ~10.5k = ~53
pages ≈ 48 MB. `max_records` (default 20,000) caps a sweep; the host is paced at one request per second.

Verified one-day sweep (2026-10-16, first 400 of 813): ILLUMINA 271 / BGISEQ 121 / OXFORD_NANOPORE 8;
38 CRA studies; strategies RNASeq 316 / Genome 53 / Other 23 / EpiGenomics 8; 14.6 s.

Other facet values seen in the 30-day NGDC window: strategies Genome 11,203 / RNASeq 10,380 / Other
11,057 / unspecified 4,695 / Exome 549 / EpiGenomics 291; sources METAGENOMIC 11,660 / GENOMIC 11,101 /
TRANSCRIPTOMIC 8,748 / TRANSCRIPTOMIC SINGLE CELL 759. (The strategy facet also lists library-selection
values such as PCR and RANDOM; they are reported as returned.)

### 1b. Statistics JSON = the instrument model, as cumulative totals

```
GET https://ngdc.cncb.ac.cn/gsa/statistics/statistics.json        # ~11.7 MB, ~60 s on a slow link
platforms.platformList[]  {platforms: "DNBSEQ-T7", experiments: 112846, runs: 119461, platformId: 64}
total.mouthArchiveList[]  [["2026-07", 1330], ["2026-08", 1151], ["2026-09", 1267], ["2026-10", 731]]
total.{archiveCount 36178, expCount 2115280, runCount 2143928, releasedExp 1233693, privateExp 881587}
organisms[], sampleType[], agency.agencyList[], organization.{organizationList[1250], countrySummaryList[35]}
article.publications[17850]  {doi, pubmedId, craAccession (HTML), proAccession (HTML), journal, pDate, articleTitle}
```

`litradar/gsa_snapshots.py` stores one snapshot per run in three additive tables
(`gsa_platform_snapshots`, `gsa_monthly_archives`, `gsa_totals`; `CREATE TABLE IF NOT EXISTS`, created
on first use via `store.conn`) and `platform_deltas()` diffs the latest snapshot against a baseline, which
turns the cumulative totals into a per-model flow (DNBSEQ-T7 vs NovaSeq 6000 vs GenoLab M week over week).
Deltas include retroactive releases, so they are a trend signal; the per-day search counts are the exact
release numbers. `gsa.publications()` exposes the dataset→paper bridge (DOI/PMID/CRA/PRJCA) for later use.

All-time top models in GSA on 2026-10-17: Illumina NovaSeq 6000 434,392; HiSeq X Ten 132,248; HiSeq 2500
128,413; MiSeq 116,399; **DNBSEQ-T7 112,846**; HiSeq 2000 42,489; HiSeq 4000 29,269; **MGISEQ-2000
23,398**; AB 3730xL 22,744; NovaSeq 5000 21,417. Chinese-vendor models: DNBSEQ-T7 112,846; MGISEQ-2000
23,398; BGISEQ-500 13,905; MGISEQ-200 8,492; MGISEQ-2000RS 6,797; BGISEQ-2000 5,593; DNBSEQ-G99 2,179;
DNBSEQ-T10×4 1,676; Gene+Seq-2000 752; BGISEQ-100 492; BGISEQ-1000 479; GenoLab M 453; DNBSEQ-T20×2 370;
FASTASeq 300 268; DNBSE-E25 106.

### 1c. What is deliberately not used

* `/gsa/browse/**` (CRA/CRX/CRR pages and the `browse/experiment/list` DataTables endpoint) answer every
  scripted request with a 1.6 KB "正在进行安全检查" page that sets a cookie from JavaScript and reloads.
  The challenge is trivial to compute, and that is exactly why the adapter does not: it is an explicit
  signal that the operator does not want scripted access to those pages. Model-level per-experiment data
  for native GSA therefore comes only from GSA-Human (below) and from the statistics snapshots.
* `/gsa/search/getRunInfo` (and `getAccessionList`, `getDatasetList`) are the search page's CSV exports.
  Verified: they honour the facet filters but ignore the `[ReleaseDate]` term and return the first ~10k
  rows of the index (release dates 2015-2025), so they cannot serve a weekly window.
* The bulk file area `https://download.cncb.ac.cn/gsa/` reset the connection to scripted clients and holds
  reads, not metadata.

## 2. GSA-Human (controlled-access human data; metadata open)

```
GET https://ngdc.cncb.ac.cn/gsa-human/browse/?pageNo=1&order=b.release_time&desc=desc&releaseTime=09/17/2026 - 10/17/2026
    -> HTML table, 10 rows per page, "from 1 to 10, Total 243"; columns Study (HRA), Title, Organization, DAC,
       Access, Requests, Valid Requests, Approved, Project (PRJCA), Strategy, Study Type, Disease, Individuals,
       Samples, Experiments, Release Time, Sharing Rank, Last Processed, Request
GET https://ngdc.cncb.ac.cn/gsa-human/ajaxb/runinstudy?accession=HRA000001&pageSize=500&pageNo=1
    -> {"totalCount": n, "runViews": [{"expName": "HRX000001", "platform": "Illumina HiSeq 2000", "runAcc": "HRR000001",
        "strategyName": "WGS", "biosampleAcc": "SAMC019696", "expTitle": "…", …}]}
```

`gsa_human.fetch(http, start, end, max_studies=400)` lists the studies released in the window (newest
first) and pulls each study's runs (one JSON request per study, `pageSize=500`), collapsing runs to
experiments. The `platform` string is model-level and resolves directly in the instrument gazetteer. The
list endpoint occasionally resets the connection; the shared client's retries cover it. Verified: 243
studies released in the 30-day window; the first 40 studies (3,581 experiments) were 69% DNBSEQ-T7.

## 3. ChinaXiv (CAS preprint server)

Main site: https://chinaxiv.org (article pages `https://chinaxiv.org/abs/YYYYMM.NNNNN`). The site's own
OAI endpoint `https://chinaxiv.org/oai/request?verb=Identify` answers `Sorry!You have no right to access
this web.` (IP allow-list; the "authorization" page says CAS and `.ac.cn`/`.edu.cn` accounts can register).
Search/list pages return 403 ("系统监测到您访问异常") to scripts, and there is no RSS.

The aggregator https://global.chinaxiv.org documents a public OAI-PMH 2.0 interface in its help page
(contact eprint@mail.las.ac.cn); it is what `litradar/sources/chinaxiv.py` uses:

```
GET https://global.chinaxiv.org/oaiapi/getdata?verb=ListRecords&metadataPrefix=oai_dc&source=chinaxiv
                                             &startTime=2026-09-17T00:00:00Z&endTime=2026-10-17T23:59:59Z
GET https://global.chinaxiv.org/oaiapi/getdata?verb=ListRecords&resumptionToken=1792268762149:100:163:OAI_DC
```

Record shape (elements are **not** namespaced, despite `metadataPrefix=oai_dc`): `title`, `author`*,
`keyword`*, `subject`, `abstract`, `article-id` (e.g. `202610.00074`), `source`, `date` (may be non-padded,
`2026-9-19`), `type`, `format`, optional `url` (journal landing page when the preprint was published).
100 records per page; `resumptionToken` carries `completeListSize` and `cursor`; the last page has no
token. `Identify` and `ListSets` answer `badVerb`; `ListMetadataFormats` lists `OAI_DC` only; the standard
`from`/`until` arguments are ignored (use `startTime`/`endTime`); `source` also accepts `biorxiv`,
`medrxiv`, `arxiv` (3-day counts 371 / 138 / 2,964) and rejects `chemrxiv`, `preprints.org`, `ssrn`.

Mapping to `Paper`: `id = doi:10.12074/<article-id>` (ChinaXiv mints DOIs as `10.12074/<article-id>`; the
DOI is **derived**, flagged `extra["doi_derived"]`, and is registered with neither Crossref nor DataCite,
so it will not deduplicate against those feeds); `url = https://chinaxiv.org/abs/<article-id>`;
`kind = preprint`, `journal = ChinaXiv`, `category` = first subject, `topics` = keywords,
`extra["subjects"]`, `extra["language"]` (zh/en), `extra["life_science"]` from the subject taxonomy
(`chinaxiv.LIFE_SCIENCE_SUBJECT_TERMS`; `fetch(..., life_science_only=True)` keeps only those). Duplicate
article-ids (new versions) keep the later `date`.

Verified 30-day pull: 275 distinct records in 3 requests (4.5 s); subjects led by astronomy (122) and
nuclear physics (19); 33 life-science records; 99 Chinese-language titles, the rest English. All 275 were
new to a fresh store and stored without error.

**Full-text search caveat:** the store's FTS5 table uses the `porter unicode61` tokenizer, which treats a
run of CJK characters as one token. `litradar search 临床` matches only where 临床 is delimited by
spaces/punctuation (verified: 0 hits across the 275 stored titles, 1 hit on a mixed-script test string).
Options for a follow-up outside this change: a parallel FTS table with `tokenize='trigram'` (SQLite ≥ 3.34;
queries need ≥ 3 characters) or pre-segmenting Chinese text (an optional `jieba` extra) into a dedicated
column. Keyword matching through the gazetteer is unaffected (see §5).

## 4. CNKI, Wanfang, CQVIP (维普): boundary, not scraped

* **CNKI** (中国知网, https://www.cnki.net): subscription database. Programmatic access is sold as the
  "CNKI Open API / 知网 API" to institutional licensees under contract; the public site has no documented
  anonymous API, requires JavaScript, rate-limits and fingerprints scripted clients, and its terms forbid
  automated downloading. The CNKI "Scholar" discovery layer (https://scholar.cnki.net) is likewise
  JavaScript-rendered with anti-bot measures. A number of Western libraries lost CNKI access in 2022-2023
  when export-control and data-security reviews narrowed the licensed catalogue; whatever a licence
  covers, it is reading rights, not text-mining rights.
* **Wanfang Data** (万方, https://www.wanfangdata.com.cn): subscription database; APIs exist for licensed
  institutions (document delivery, metadata feeds) and are not public.
* **CQVIP / 维普** (http://www.cqvip.com): subscription database; no public API; the site blocks scripts.
* **What is legitimately available instead:** (1) Chinese journals indexed in Crossref (most Chinese
  journals with DOIs now deposit metadata; `crossref.py` by ISSN covers them), Europe PMC (MEDLINE-indexed
  Chinese journals) and OpenAlex (which ingests Crossref plus CNKI-sourced records for some journals);
  (2) **CSTR/ChinaDOI**: ChinaXiv DOIs (`10.12074`) and many Chinese journal DOIs are registered through
  ISTIC's ChinaDOI (https://www.chinadoi.cn), which has no public query API comparable to Crossref's;
  (3) ChinaXiv via the aggregator OAI above; (4) GSA/GSA-Human for the data side. **Boundary:** the radar
  does not fetch CNKI/Wanfang/CQVIP pages, does not use institutional proxies, and does not store any
  content from them; a licensee who wants CNKI coverage should obtain the vendor's API contract and add an
  adapter under the same offline-test discipline. Nothing in this repository depends on those databases.

## 5. Company gazetteer: Chinese names, aliases, domains

`config/companies.yaml` gained a China section (GeneMind, Salus BioMed, Axbio, Vazyme, Berry Genomics,
Geneplus, HaploX, Genecast, AmoyDx, Daan Gene, Sansure, Hangzhou Biotest, Sinopharm (incl. China
National Biotec Group / Sinopharm Diagnostics), Biomarker Technologies, Annoroad, Biotree, Pharmaron,
WuXi Biologics, Hengrui, BeOne Medicines (BeiGene), Zai Lab, Innovent, Akeso, Legend Biotech) and Chinese
aliases plus domains on the existing MGI Tech, BGI Genomics, GenScript, Singleron, Novogene (ticker now
688315.SS), Genetron and WuXi AppTec entries. Tickers carry exchange suffixes (`.SS` Shanghai incl. STAR,
`.SZ` Shenzhen incl. ChiNext, `.HK`), bare tickers are US listings (BNR, ONC, ZLAB, LEGN). The leftover
`twist-dx-dup` entry (which was already Burning Rock) was renamed `burning-rock`; run `litradar rematch`
once after merging so stored mentions pick up the new id. Legend Biotech and WuXi Biologics are now their
own entries (removed as aliases of GenScript / WuXi AppTec), so `check-config` reports no alias collisions.

**Matcher behaviour with Chinese aliases (verified, see `tests/test_china_sources.py`):**

* Python's `\w` includes CJK ideographs, so the gazetteer's word boundaries treat a Chinese alias as a
  word only when it is delimited by spaces or punctuation. `深圳华大基因股份有限公司, 深圳 518083` → BGI
  Genomics; `康龙化成(北京)新药技术股份有限公司` → Pharmaron; `本研究由华大基因完成` (brand embedded in
  prose) → no match.
* Aliases of ≤ 5 characters are treated as ambiguous by `_is_ambiguous()` (CJK strings are `isalpha()`
  and already lower-case), so short brands such as 华大基因 or 诺禾致源 need the English name within
  250 characters (`BGI Genomics (华大基因)` matches; `诺禾致源、百迈客生物` alone does not). Registered
  names (> 5 characters) match on their own.
* A one-line follow-up in `litradar/extract/gazetteer.py` would lift both limits for CJK aliases: skip the
  `\w` look-arounds and the ambiguity heuristic when an alias contains CJK characters (two ideographs are
  already as specific as an English word). It is not part of this change because the module is shared.

## 6. SRA vocabulary check: Chinese platforms are already in the INSDC census

The INSDC experiment schema (`SRA.common.xsd` 1.5, fetched from
https://ftp.ebi.ac.uk/pub/databases/ena/doc/xsd/sra_1_5/SRA.common.xsd, 118 enumeration values) lists
`DNBSEQ-T7`, `DNBSEQ-G400`, `DNBSEQ-G800`, `DNBSEQ-G50`, `DNBSEQ-G400 FAST`, `DNBSEQ-T10x4RS`,
`MGISEQ-2000RS`, `BGISEQ-50`, `BGISEQ-500` (platform BGISEQ), `GenoLab M`, `FASTASeq 300` (platform
GENEMIND), plus `UG 100`, `Element AVITI`, `AVITI 24`, `Onso`, `Revio`. NCBI's live index is a superset
(it also carries `DNBSEQ-G99`, which is not in the 1.5 XSD).

Live SRA counts for 2026-09-17..10-17 via `"instrument <model>"[Properties]` (the existing
`sra.counts_by_instrument()`): DNBSEQ-T7 **19,334**, DNBSEQ-G400 3,953, MGISEQ-2000RS 2,512, BGISEQ-500
2,173, FASTASeq 300 417, DNBSEQ-G99 328, GenoLab M 146, DNBSEQ-T10x4RS 1; for scale NovaSeq X Plus 31,917
and Revio 2,484. `"platform bgiseq"[Properties]` returns only 6,182 for the same window, so the platform
property undercounts and per-model instrument properties should be used. The census (`sra.py` + `ena.py`)
therefore already counts Chinese platforms wherever submitters chose INSDC; what it misses is the native GSA
flow (11,014 BGISEQ-family experiments in the window), which `gsa.py` adds.

**Instrument-gazetteer resolution of archive spellings** (as of this branch; the instrument gazetteer is
maintained separately): `DNBSEQ-T7`, `DNBSEQ-G400`, `DNBSEQ-G99`, `DNBSEQ-T20×2`, `BGISEQ-500`,
`MGISEQ-2000` and the GSA family label `BGISEQ (model n/a)` resolve to MGI Tech. Not yet resolved and worth
adding as aliases of the matching entries: `MGISEQ-2000RS`, `MGISEQ-200`, `BGISEQ-2000`, `BGISEQ-50`,
`BGISEQ-100`, `BGISEQ-1000`, `DNBSEQ-T10x4RS` / `DNBSEQ-T10×4`, `DNBSE-E25` (GSA's spelling of DNBSEQ-E25),
`Gene+Seq-2000` (Geneplus-branded MGISEQ), and new entries for GeneMind (`GenoLab M`, `FASTASeq 300`),
Salus (`Salus Pro`/`Salus EVO`) and Axbio. Until then those rows appear in the digest with the model name
and no vendor.

## 7. Live-test record (2026-10-17)

* GSA histogram, 30 days, NGDC: 1 request, 11.1 s, total 33,480 (platform split in the summary table).
* GSA one-day sweep 2026-10-16: 813 experiments; first 400 fetched in 2 pages, 14.6 s; 121 BGISEQ rows
  resolved to MGI Tech by the gazetteer, 271 ILLUMINA rows carry the family only.
* GSA statistics JSON: 61.4 s, 80 model rows stored as a snapshot, 17,850 publications parsed.
* GSA-Human: 243 studies in the window; first 40 studies = 3,581 experiments in 60.2 s (41 requests),
  all models resolved by the gazetteer.
* ChinaXiv OAI: 275 records, 3 requests, 4.5 s; 275/275 stored as new papers.
* SRA Properties counts: 11 requests with the shared client's pacing; one 429 was retried once.
* Total: 71 HTTP requests, 1 retry, 0 failures.

## 8. Not done / open

* Native GSA instrument **model per experiment** (browser-gated) — covered indirectly by snapshots and
  GSA-Human only.
* GSA organism per experiment: the search hit has no organism field (facets have sample type only).
* ChinaXiv affiliations and e-mail addresses are not in the OAI metadata, so corporate authorship cannot
  be detected for ChinaXiv records; only title/abstract/keyword mentions feed the company gazetteer.
* Chinese full-text search in SQLite FTS5 (see §3).
* CNKI/Wanfang/CQVIP remain a boundary (see §4).
