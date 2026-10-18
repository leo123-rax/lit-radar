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
| **GSA search page** (`ngdc.cncb.ac.cn/gsa/search`) | **wired** 2026-10-17 (`sources.gsa`, default on; §9), HTML, no key | `litradar/sources/gsa.py` `platform_histogram()`, `fetch()` | per-experiment `ArchiveRecord(archive='gsa')` with CRX/CRA, platform **family**, strategy, exact release day; facet histogram in one request | 33,480 native (NGDC) experiments: ILLUMINA 21,891 / BGISEQ 11,014 / PACBIO_SMRT 451 / OXFORD_NANOPORE 86 / CAPILLARY 18 / GENEMIND 11 / CapitalBio 8 / ION_TORRENT 1. Including INSDC mirrors the same index holds 363,091. |
| **GSA statistics JSON** (`/gsa/statistics/statistics.json`) | works, JSON (~12 MB), no key; opt-in per-run snapshot (`sources.gsa.statistics_snapshot: true`; §9) | `gsa.fetch_statistics()` + `litradar/gsa_snapshots.py` | cumulative counts per instrument **model** (80 models), CRA created per month, organisms, sample types, funders, organizations by country, 17,850 dataset-linked publications (DOI + PMID + CRA) | DNBSEQ-T7 112,846 experiments all-time (#5 overall), MGISEQ-2000 23,398, BGISEQ-500 13,905, DNBSEQ-G99 2,179, GenoLab M 453, FASTASeq 300 268; 2,115,280 experiments total, 1,233,693 released |
| **GSA browse pages** (`/gsa/browse/**`) | **browser-gated** (nginx JavaScript cookie challenge) | not used | the only place the instrument model of a native CRX is shown | — |
| **GSA RunInfo export** (`/gsa/search/getRunInfo`) | works but ignores the ReleaseDate term; ~10k-row cap | not used | CSV with Run, ReleaseDate, Platform (family), BioProject, TaxID … | returns the first ~10k rows of the whole index regardless of the query |
| **GSA-Human** (`/gsa-human/browse/`, `/gsa-human/ajaxb/runinstudy`) | **wired** 2026-10-17 (`sources.gsa_human`, default on; §9), HTML list + JSON runs, no key | `litradar/sources/gsa_human.py` `fetch()` | per-experiment `ArchiveRecord(archive='gsa-human')` with **model-level** platform, HRA study, PRJCA, organization, release date, Homo sapiens | 243 studies released; first 40 studies = 3,581 experiments: DNBSEQ-T7 2,481 / NovaSeq 6000 911 / NovaSeq X Plus 108 / NextSeq 550 53 / BGISEQ-500 22 / HiSeq X Ten 6 (100% matched to the instrument gazetteer) |
| **ChinaXiv OAI-PMH** (`global.chinaxiv.org/oaiapi/getdata`) | **wired** 2026-10-17 (`sources.chinaxiv`, default on with `life_science_only: true`; §9), verified live (no reachable vendor docs), no key | `litradar/sources/chinaxiv.py` `fetch()` | `Paper(source='chinaxiv', kind='preprint')` with Chinese title/abstract/authors/keywords/subject, derived DOI, abs URL | 275 distinct records (3 pages): 天文学 122, 核物理学 19, 图书馆学 19, … life-science subjects 33 (临床医学 5, 预防医学与公共卫生学 5, 农业基础学科 2, 植物生态学 2, 药物设计 1 …); 99 Chinese-language titles |
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
    pageSize   = N                                            # honoured up to at least 3,000 (verified)
    from       = <1-based page number>
    centerSelectedItem = NGDC   centerSelected = true         # facet: NGDC = native GSA; INSDC = mirrored SRA/ENA/DDBJ
    <facet>SelectedItem = <value>   <facet>Selected = true    # facet in platform | strategy | source | selection | fileType; combinable
```

Response is server-rendered HTML: `Total Items:&nbsp;N`, then one hit per
`<input class="checkbox_class" data-value="CRX…">` with fields *Title*, *Accession* (link
`browse/<CRA>/<CRX>`), *Platform name*, *Source name*, *Strategy name*, *Selection name*. The sidebar has
seven facet blocks, in page order `center_block`, `downloadable_block`, `fileType_block`,
`platform_block`, `source_block`, `strategy_block`, `selection_block`, each carrying counts for the whole
result set, so `gsa.platform_histogram()` costs one request. `parse_facets()` returns every block under its
own key (`center`, `downloadable`, `file_type`, `platform`, `source`, `strategy`, `selection`). The
*Platform name* is the **family** (ILLUMINA, BGISEQ, OXFORD_NANOPORE, PACBIO_SMRT, GENEMIND, SALUS,
ELEMENT, ULTIMA …), not the model.

Three page shapes come back with HTTP 200, and the adapter tells them apart (`gsa.check_search_page()`):
a results page carries `Total Items`; a day with no releases renders `No items found` (27.8 KB, no total,
no facet blocks; verified with a 2027 date) and is treated as zero; anything else, in particular the
1.3 KB JavaScript cookie challenge titled `正在进行安全检查` that `/gsa/browse/**` already serves, raises
`RuntimeError` so the pipeline records the failure in its error list instead of a plausible-looking zero.

**Pagination is unstable, so the adapter never walks pages.** The search has no sort parameter and the
Elasticsearch order changes between requests: the same page-1 request issued twice shared 156 of 200
accessions, pages 2-4 of one day overlapped each other, and five pages of 200 for the 813-hit day
2026-10-16 produced only 739 distinct accessions (9% lost; a 13-page day lost 27%). One page sized to the
result set is complete, because the instability is only across pages: `pageSize=1000` returned 813/813
distinct (3.5 MB, 20.5 s) and `pageSize=3000` returned 2,593/2,593 (11 MB, 53.5 s); server time grows at
~20-25 ms per hit.

`gsa.fetch(http, start, end)` therefore works day by day (so `first_public` is exact) as follows: one
probe request of `page_size` (200) hits, which is already complete for a small day and carries the day's
facet counts; otherwise ONE page sized to the day's total; and when the total exceeds `max_page_size`
(default 1,500, kept inside the client's 60 s read timeout) the day is partitioned on its facet counts,
platform first, then strategy, source, selection and file type, each partition again as one sized page
(a partition that is still too big is probed once for its own facet counts and split further). A
partition that no facet can split is truncated at `max_page_size` and logged, and a day that ends short
of its total for any reason is logged as `fetched n of N`; nothing is silently short. `instrument_model`
is labelled `"<FAMILY> (model n/a)"`; `center_name` is left empty because the hit names no submitting
centre and the NGDC/INSDC facet is a provenance filter, not a centre. The gazetteer resolves
`BGISEQ (model n/a)` to the MGI DNBSEQ entry (vendor MGI Tech, 688114.SS); `ILLUMINA (model n/a)`
resolves to no model, so the digest's vendor-share table falls back to the platform string, which is the
honest outcome. Cost: a typical day is 2 requests (probe + sized page); 2026-10-15 (2,593 experiments)
took 8 requests because ILLUMINA (2,125) had to be split by strategy; the two days together were 11
requests and 110.7 s with every record present. `max_records` (default 20,000) caps a sweep; the host is
paced at one request per second.

Verified complete two-day fetch (2026-10-17): 2026-10-15 = 2,593 experiments / 75 CRA studies, ILLUMINA
2,125 / BGISEQ 447 / OXFORD_NANOPORE 17 / PACBIO_SMRT 4, strategies Other 1,165 / RNASeq 931 / Genome 484 /
EpiGenomics 13; 2026-10-16 = 813 experiments / 41 CRA studies, ILLUMINA 559 / BGISEQ 240 /
OXFORD_NANOPORE 12 / PACBIO_SMRT 2, strategies RNASeq 623 / Genome 113 / Other 61 / EpiGenomics 16. Both
platform splits equal the facet histogram of the same day exactly.

Other facet values seen in the 30-day NGDC window: strategies Genome 11,203 / RNASeq 10,380 / Other
11,057 / unspecified 4,695 / Exome 549 / EpiGenomics 291; sources METAGENOMIC 11,660 / GENOMIC 11,101 /
TRANSCRIPTOMIC 8,748 / TRANSCRIPTOMIC SINGLE CELL 759. Library-selection values (PCR, RANDOM, RT-PCR,
PolyA, cDNA, size fractionation …) are a separate `selection` facet; one-day digits for 2026-10-16:
PCR 542 / RANDOM 110 / PolyA 88 / RT-PCR 50 / cDNA 14 / other 6 / ChIP 2 / size fractionation 1, and
file types fastq 810 / bam 3.

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

The aggregator https://global.chinaxiv.org exposes a public OAI-PMH 2.0 endpoint, which is what
`litradar/sources/chinaxiv.py` uses. It is **verified live, not documented**: no help page is reachable
to a scripted client (the home page is a 2 KB JavaScript shell; `/help`, `/help/index`, `/about`, `/oai`
answer HTTP 500; `/oaiapi` and `/api` answer 404; checked 2026-10-17), so the interface below is
described from its live behaviour:

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

**Parents and sisters never ride on a subsidiary's ticker.** Group-level names are separate, unlisted
entries so ticker joins stay clean: `bgi-group` (BGI Group, BGI, BGI-Shenzhen, BGI Research, Beijing
Genomics Institute, STOmics, 华大集团, 华大时空, 深圳华大基因研究院; also the shared `genomics.cn` / `bgi.com`
e-mail domains, which cannot distinguish the group from its listed arms) with `bgi-genomics` (300676.SZ)
and `mgi-tech` (688114.SS) pointing at it through `parent`; `sinopharm` (China National Pharmaceutical
Group, 国药集团, 中国医药集团有限公司; unlisted) with `sinopharm-group` (Sinopharm Group Co., 国药控股,
1099.HK, the distribution arm) and `cnbg` (China National Biotec Group, 中国生物技术股份有限公司; unlisted
vaccine / diagnostics arm) as children; `wuxi-xdc` (WuXi XDC, 药明合联, 2268.HK) separate from
`wuxi-biologics` (2269.HK). The matcher resolves overlapping aliases to the longest one, so
"Sinopharm Group Co., Ltd." hits 1099.HK while a bare "Sinopharm" hits the unlisted parent.

**Matcher behaviour with Chinese aliases (verified, see `tests/test_china_sources.py`):**

* Python's `\w` includes CJK ideographs, so the gazetteer's word boundaries treat a Chinese alias as a
  word only when it is delimited by spaces or punctuation. `深圳华大基因股份有限公司, 深圳 518083` → BGI
  Genomics; `康龙化成(北京)新药技术股份有限公司` → Pharmaron; `本研究由华大基因完成` (brand embedded in
  prose) → no match.
* Aliases of ≤ 5 characters are treated as ambiguous by `_is_ambiguous()` (CJK strings are `isalpha()`
  and already lower-case), so short brands such as 华大基因 or 诺禾致源 need the English name within
  250 characters (`BGI Genomics (华大基因)` matches; `诺禾致源、百迈客生物` alone does not). Registered
  names (> 5 characters) match on their own.
* The pre-existing bare alias `WuXi` on the WuXi AppTec entry is four mixed-case letters, so it matches
  case-insensitively and also hits the city name in affiliations ("Wuxi, Jiangsu"); it predates this
  change and is left as is, but a ticker join on WuXi AppTec mentions should expect that noise.
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

Second pass, 2026-10-17 (after review), 57 requests through the shared client across seven scratch
scripts, 0 retries, 0 failures:

* Page shapes: a 2027 date returns the 27.8 KB "No items found" page (no `Total Items`, no facet blocks);
  `/gsa/browse/CRA033661` returns the 1.3 KB "正在进行安全检查" challenge; the 2026-10-16 page has the seven
  facet blocks in the order center, downloadable, fileType, platform, source, strategy, selection.
* Pagination instability measured on 2026-10-16 (813 hits, pageSize 200): pages 1-5 = 813 hits, 739
  distinct; page 1 repeated = 156/200 shared; overlaps page 2/3 = 41, 2/4 = 6, 3/4 = 27. A page walk of
  2026-10-15..16 yielded 1,904 of 2,593 and 584 of 813.
* Sized pages: pageSize 1000 for 2026-10-16 = 813/813 distinct, 3,481,895 bytes, 20.5 s; pageSize 3000
  for 2026-10-15 = 2,593/2,593 distinct, 11,010,419 bytes, 53.5 s. Combined filter platform=ILLUMINA +
  strategy=RNASeq on 2026-10-16 = 381 with facets scoped to the filtered set.
* Restructured `fetch()` on 2026-10-15..16: 3,406 distinct experiments = both day totals, 11 requests,
  110.7 s, no shortfall warning (digits in §1a).
* 30-day histogram re-run: total 33,480, same platform split, plus selections PCR 17,492 / unspecified
  4,695 / RANDOM 4,049 / PolyA 2,751 / cDNA 1,198 / Hybrid Selection 995 / RANDOM PCR 611 / RT-PCR 472 /
  size fractionation 298, file types fastq 31,668 / bam 1,744 / 10x genomics 42 / ab1 18 / sra 7; 5.1 s.
* ChinaXiv aggregator: `Identify` answers an OAI-PMH 2.0 envelope with `<error code="badVerb">`;
  `ListRecords` 2026-10-10..17 = 51 distinct records in one page, 0.8 s; home page 2,027 bytes of
  JavaScript, `/help`, `/help/index`, `/about`, `/oai`, `/help/api` HTTP 500, `/oaiapi`, `/api` 404.

## 8. Not done / open

* Native GSA instrument **model per experiment** (browser-gated) — covered indirectly by snapshots and
  GSA-Human only.
* GSA organism per experiment: the search hit has no organism field (facets have sample type only).
* ChinaXiv affiliations and e-mail addresses are not in the OAI metadata, so corporate authorship cannot
  be detected for ChinaXiv records; only title/abstract/keyword mentions feed the company gazetteer.
* Chinese full-text search in SQLite FTS5 (see §3).
* CNKI/Wanfang/CQVIP remain a boundary (see §4).
* Digest: `SEQUENCING_ARCHIVES` in `litradar/digest/render.py` still lists SRA / ENA / GEO, so GSA and GSA-Human rows
  reach `New archive records` and `Store.archive_instrument_counts()` but not yet the sequencing census table (§9).

## 9. Pipeline wiring (applied 2026-10-17)

All three adapters now run in the weekly pipeline; every live endpoint was re-checked on the wiring day with one request
each through the shared client (0 retries, 0 failures):

* GSA histogram 2026-09-17..10-17, NGDC: total **33,480**; ILLUMINA 21,891 / BGISEQ 11,014 / PACBIO_SMRT 451 /
  OXFORD_NANOPORE 86 / CAPILLARY 18 / GENEMIND 11 / CapitalBio 8 / ION_TORRENT 1; strategies Genome 11,203 / Other 11,057 /
  RNASeq 10,380 / Exome 549 / EpiGenomics 291; 1 request, 10.2 s (same digits as the first live test).
* GSA-Human list page 1: **246** studies released in the window (243 on the first test day); `runinstudy` for HRA021718 =
  96 runs on DNBSEQ-T7, Illumina NovaSeq 6000 and Illumina NovaSeq X Plus; 2 requests, 4.1 s.
* ChinaXiv 2026-10-10..17: 51 distinct records in one page, 4 life-science subjects, 0 Chinese-language titles in that
  week's sample; 1 request, 4.1 s.

### Configuration (`config/radar.yaml`, `sources:`)

```yaml
gsa:
  enabled: true
  center: NGDC               # NGDC = native records; INSDC = the mirrored SRA/ENA/DDBJ copies (already counted by sra / ena)
  page_size: 200             # per-day probe
  max_page_size: 1500        # largest single page; bigger days are split by facet (never paged)
  max_records: 20000         # per run
  statistics_snapshot: false # true = also fetch statistics.json (~12 MB) and store a per-model snapshot (litradar/gsa_snapshots.py)
gsa_human:
  enabled: true
  max_studies: 400           # one JSON request per study
chinaxiv:
  enabled: true
  source: chinaxiv
  life_science_only: true    # subject-taxonomy filter (chinaxiv.LIFE_SCIENCE_SUBJECT_TERMS); false = every discipline
  max_records: 5000
```

### Where the records go

* `Pipeline.fetch_archives` stores GSA experiments as `ArchiveRecord(archive="gsa")` and GSA-Human experiments as
  `ArchiveRecord(archive="gsa-human")` through `_store_archive`, so every record passes `Gazetteer.match_archive_model`.
  The GSA family label `BGISEQ (model n/a)` resolves to the MGI entry (vendor MGI Tech, ticker 688114.SS); the GSA-Human
  model strings resolve directly; `ILLUMINA (model n/a)` and the other families stay unmapped and are labelled from the
  platform string by `store.archive_vendor_label` ("Illumina (unmapped model)"). Counters: `archive.gsa_new`,
  `archive.gsa_human_new`, and `archive.gsa_snapshot_models` when the snapshot is on. `--sources gsa` / `--sources gsa_human`
  run the archive stage alone.
* `Pipeline.fetch_papers` adds ChinaXiv records with the usual `add()` / `merge()` path (counter `fetched.chinaxiv`); the
  derived `10.12074/<article-id>` DOI never collides with Crossref or DataCite records, so no cross-source merge happens.
* Failures are isolated per source and reported as `gsa: …`, `gsa_human: …`, `chinaxiv: …` in the run's error list;
  `HOST_SOURCES` maps `ngdc.cncb.ac.cn` to `gsa` and `global.chinaxiv.org` to `chinaxiv` for the dead-host report.
* Offline wiring tests: `tests/test_pipeline_offline.py` (`test_run_reaches_global_corpus_sources_with_configured_caps`,
  `test_global_corpus_toggles_are_respected_and_jstage_is_opt_in`, `test_global_corpus_source_failures_are_isolated`).

* The digest's sequencing census (`SEQUENCING_ARCHIVES` / `SEQUENCING_ARCHIVE_LABELS` in `litradar/digest/render.py`)
  includes both archives under their **stored** ids, `gsa` and `gsa-human` (hyphen, as the adapter writes it; the
  source / counter name `gsa_human` is a label, not the stored id). A stored GSA-Human row therefore appears in the
  census table with `gsa-human` in its Archive column and "GSA-Human" in the section heading
  (`tests/test_digest_sections.py`).

### Still open after the wiring

* `litradar run --sources` help text in `litradar/cli.py` does not list the new ids yet (they work; only the help string lags).
