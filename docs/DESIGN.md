# lit-radar: design

*Architecture dossier. State of the code and of the research as of 2026-10-17. Every live-measured
number carries that date unless another is given; prices are list prices read from the vendor's own
pages on that date. Companion documents: `SOURCES.md` (per-adapter catalog), `SUBSCRIPTIONS.md`
(what a reader might pay for), `RESEARCH.md` (how the evidence was produced and checked),
`ROADMAP.md` (what comes next), `LEGAL.md` (storage and access policy).*

## 1. What the radar is for

lit-radar is a weekly literature radar for a reader who follows life-science tools, genomics,
diagnostics and biotech: an investor, an analyst, a product manager, a core-facility director. It
answers five questions every week, from free public sources, with every number traceable to the
call that produced it:

1. **What was published?** Every new paper and preprint in a curated set of journals (by ISSN),
   bioRxiv/medRxiv, arXiv q-bio and the open-access share of Europe PMC.
2. **Which instruments did they use?** The platforms, kits and software named in Methods sections,
   resolved against a gazetteer of 335 entries / ~2,660 aliases with vendor and ticker.
3. **Which machines are producing public data?** A census of newly released sequencing experiments
   (NCBI SRA, ENA) by declared instrument model, independent of what gets written up, with
   study-level (GEO), proteomics (PRIDE) and single-cell/spatial assay (CELLxGENE) counterparts.
4. **Which companies are touched, and how?** As author (affiliation or e-mail domain), as funder or
   competing-interest disclosure, as subject, or as supplier of a named instrument; plus filings,
   trials, grants and trade-press headlines.
5. **Who is doing the work?** New output by tracked researchers in twelve subgroups (single-cell,
   spatial, long-read, proteomics, liquid biopsy, perturbation screens, AI-bio, gene editing,
   structural biology, synthetic biology, cell therapy, MCED).

It is growing from a weekly digest into a **persistent corpus**: every run upserts into one SQLite
file with full-text search, so "everything mentioning Xenium in the last 90 days" is a local query,
and monthly instrument time series can be backfilled to 2019.

Non-goals, stated so forks do not drift into them: no publisher logins, no HTML scraping of
publisher sites, no Google Scholar, no storage of paywalled full text, no redistribution of stored
text. The operator is assumed to be **commercial with no institutional licence**; every "academic"
or "non-commercial" door is treated as closed by default (see `LEGAL.md`).

## 2. Four layers

```
 INGEST  (litradar/sources/)                     EXTRACT  (litradar/extract/)
 ┌─────────────────────────────────────────┐     ┌─────────────────────────────────────────┐
 │ papers & preprints                      │     │ JATS section parser: Methods, funding,  │
 │   bioRxiv/medRxiv API, Europe PMC REST, │     │   COI, acknowledgements, data           │
 │   Crossref by ISSN, OpenAlex (keyed),   │ ──> │   availability, authors, e-mails, body  │
 │   arXiv q-bio, journal/subject RSS      │     │ gazetteer matcher: instruments,         │
 │ archives (census)                       │     │   companies, context rules              │
 │   NCBI SRA, ENA; GEO, PRIDE,            │     │ archive-model map (controlled vocab,    │
 │   CELLxGENE (built, not wired)          │     │   no context rules)                     │
 │ news & filings                          │     │ company gazetteer over headlines        │
 │   RSS, press wires, SEC 8-K Atom,       │     │                                         │
 │   Google News (opt-in pending);         │     │                                         │
 │   CT.gov, NIH RePORTER, openFDA (built) │     │                                         │
 └─────────────────────────────────────────┘     └─────────────────────────────────────────┘
                                                                     │
                                                                     v
 DELIVER / SEARCH  (litradar/digest/, store.py)  ENRICH / RANK  (litradar/rank/, policy.py)
 ┌─────────────────────────────────────────┐     ┌─────────────────────────────────────────┐
 │ Markdown digest (+ JSON summary)        │     │ subgroup assignment (OpenAlex topics +  │
 │ e-mail (SMTP), Slack webhook,           │     │   keywords); tracked-author hits        │
 │   committed to git                      │ <── │ heuristic score 0-10 with reasons       │
 │ SQLite + FTS5: litradar search,         │     │ optional LLM rubric (Claude, Batches),  │
 │   litradar entity, litradar timeseries  │     │   blend 0.4 / 0.6                       │
 │ planned: dashboard, Parquet export,     │     │ citation enrichment (Semantic Scholar)  │
 │   DuckDB                                │     │ data policy: full / facts_only / none   │
 └─────────────────────────────────────────┘     └─────────────────────────────────────────┘
```

**Ingest** is a set of dumb adapters: each yields normalised `Paper`, `ArchiveRecord` or `NewsItem`
objects and does no matching or scoring. Adapters share one HTTP client (`litradar/http.py`) that
carries an honest User-Agent with the operator's contact address, paces every host at or below its
published limit, retries with exponential backoff, honours `Retry-After`, and stops calling a host
for the rest of the run after two consecutive HTTP 429s (so one misbehaving loop cannot get the
operator's address blocked). Nine adapters are wired into the weekly run today (bioRxiv/medRxiv,
Europe PMC, Crossref, OpenAlex, arXiv, RSS, SRA, ENA, SEC 8-K via RSS); seven more are built, tested
offline and documented but not yet wired (GEO, PRIDE, CELLxGENE, ClinicalTrials.gov, NIH RePORTER,
openFDA, Semantic Scholar). Wiring them is the first roadmap item.

**Extract** pulls the sections that matter out of JATS XML (Methods, funding, competing interests,
acknowledgements, data availability, authors with affiliations and e-mail addresses) and runs the
two gazetteers. Everything the matcher finds is stored as a `Mention` with the section, a short
snippet and the alias that fired, so every count in the digest can be traced to a sentence.

**Enrich and rank** assigns subgroups, flags tracked authors, scores each paper with a legible
heuristic, optionally sends the shortlist to a language model with a fixed rubric and structured
output, and decides per record how much text may be kept (`policy.py`).

**Deliver and search** renders the digest, sends it, commits it, and leaves everything in SQLite
with an FTS5 index over title, abstract, Methods, funding, COI, affiliations and body.

The pipeline is idempotent: re-running a window updates records in place, and each stage logs counts
so the digest's coverage footer can say exactly what was pulled, what had full text, what was scored
and what failed.

## 3. Sources

Every source below was probed live on 2026-10-17 from a residential IP with the project's
User-Agent; the research notes record the raw calls. Where two notes disagreed, the value chosen
here is the one reproduced by the independent fact-check (see `RESEARCH.md`), and the disagreement
is named.

### 3.1 Papers and preprints

| Source | Role | Access | Cadence / volume | Cost | Caveats (reconciled) |
|---|---|---|---|---|---|
| **bioRxiv / medRxiv API** `api.biorxiv.org/details/{server}/{from}/{to}/{cursor}` | all new preprint versions: abstract, category, licence, published-DOI link, JATS URL | free, no key, no published rate limit (client paces 0.5 s) | ~1,350 new + ~450 revised bioRxiv and ~366 + ~107 medRxiv per week | $0 | **30 records per page** for `/details` (official docs; confirmed by cursor stepping 0/30/60; an earlier catalog row said 100 — wrong; `/pubs`, `/pub`, `/publisher`, `/funder` do return 100). Errors arrive as HTTP 200 with the message in `messages[0].status`. **DOI prefix moved from 10.1101 to 10.64898 (registrant openRxiv) for postings from 2025-12-01**, and Crossref transferred the whole legacy corpus to owner-prefix 10.64898 / member 54368 in January 2026, so filter Crossref by `member:54368`, never by DOI-string prefix. Licence values live: `cc_by` ~35%, `cc_by_nc_nd` ~23%, **`cc_no` ~22%** (all rights reserved), `cc_by_nc` 12%, `cc_by_nd` 7%, `cc0` 2%. The `jatsxml` URLs on `www.biorxiv.org` answer HTTP 429 "error code: 1015" (Cloudflare rate limiting) to every scripted client tried, with or without a browser User-Agent; the supported bulk route is the requester-pays S3 buckets (section 3.6). Server usage statistics exist only as whole-server monthly aggregates; there is no per-article metrics endpoint. |
| **Europe PMC REST** `/webservices/rest/search`, `/{PMCID}/fullTextXML` | the spine for journals by ISSN, open-access full text, **section-scoped search** (`METHODS:`, `COMP_INT:`, `ACK_FUND:`, `AFF:`, `DATA_AVAILABILITY:`), preprint records for 30+ servers under one id space (`PPR…`) | free, no key, no published limit (client paces 0.2 s on `www.ebi.ac.uk`) | ~210k records in a six-week window, ~71k with full text; `pageSize` max 1,000 with `cursorMark` | $0 | Section fields only match records with indexed full text. `METHODS:"NovaSeq"` = 1,727 hits vs 27 for `ABSTRACT:"NovaSeq"` over 2026-09-01..10-17. `fullTextXML` is documented for the OA subset only; non-OA author manuscripts return HTTP 500 (the server, not the code, enforces this today). PubMed-load lag ranged from 1 day to 7–8 days in the probe fortnight; use a 14-day creation window. **Preprint full text covers only ~6% of preprints** (76,331 of 1,264,541) by design (COVID-19 and funder-supported CC preprints), and **bioRxiv full text has been zero for every month since June 2026** (last record first published 2026-05-29); medRxiv and Research Square continued. Section search therefore cannot carry preprint instrument detection. |
| **Crossref REST** `/works?filter=…` and `/journals/{issn}/works` | DOI-level discovery per journal on the day of registration; preprint↔published relations; retraction/correction notices | free; send `mailto` | 331,745 new DOIs in a 7-day window across all of Crossref | $0 | **Polite-pool rate: the `X-Rate-Limit-Limit` header advertised 3 per second in one probe and 10 per second in another on the same day**; the historically documented 50/s no longer applies. Design value: read the header every run and plan on 3 req/s; the client's 0.2 s pacing is acceptable only because a weekly sweep is ~45 calls. Use `from-created-date` (deposit date), not `from-pub-date`: Cell Press and The Lancet deposit month-precision publication dates and return nothing for a day-range `from-pub-date` filter (the current adapter still uses `from-pub-date`; fix specified). Abstract coverage is publisher-dependent: 0% for every Elsevier title probed, 10–23% for Nature-branded research journals (Nature Communications 47%), 85–99% for Science, PNAS, NAR, eLife, AACR, JCO, Blood. `relation.type:is-preprint-of` returns 845,229 links, `has-preprint` 579,198: links are mirrored for most pairs but not guaranteed bidirectional. |
| **OpenAlex** `/works`, `/authors`, `/institutions`, `/topics` | topic feeds, company-typed institutions, tracked authors, abstracts (inverted index), journal backstop, merged preprint/published works | **free API key effectively required** | 4,516 topics in 4 domains / 26 fields / 252 subfields; ~36k `type:company` institutions (point-in-time, unverifiable on the day) | $0 with key ($1/day budget) | **Usage is metered in USD per day.** Keyless calls draw on a $0.10/day budget that the live API notice says is shared "with everyone on your network's IP address" (the docs only say "keyless budget"); it was exhausted by one afternoon of probing, and the `mailto` polite pool is a retired system that confers nothing. A free key gives $1/day (≈10,000 list calls at $0.0001 each; search $0.001; single-entity lookups free), 30 req/s, and is needed for cursor paging. `from_created_date` / `from_updated_date` are **paid sync filters (Member+ $10,000/yr, Partner from $20,000/yr)**; the free design polls `from_publication_date` with a rolling 21–28-day overlap and upserts on id. Batch DOI lookups 100 per call. `authorships.author_position` is not a filter (compute last/corresponding client-side; list payloads truncate at 100 authorships). The `grants` field is removed (`funders[]`, `awards[]`); the current adapter still selects `grants` and must drop it. Topic assignments changed on 2026-10-06 (new classifier; topic ids unchanged). |
| **arXiv API** `export.arxiv.org/api/query` | q-bio categories (GN, QM, BM, MN, CB, TO, SC) by `submittedDate` | free; one request per 3 s | ~81/week q-bio; ~1,435/week if cs.LG and stat.ML are added | $0 | ~39% of q-bio postings carry the default non-exclusive licence, under which e-prints must not be stored and served; store metadata and derived facts only. |
| **Journal and subject RSS** | tables of contents as a speed backstop; DOIs dedupe against ISSN pulls | free | Nature journal feeds ~8 items (RDF), 31 subject feeds × 30 items (RSS 2.0) | $0 | Nature journal feeds return 303 to an `idp.nature.com` cookie handshake on an edge-cache miss; follow redirects. Working: Nature portfolio, Science family (`showFeed`), Cell Press `current.rss`/`inpress.rss`, NEJM, Lancet, JAMA, PNAS, PLOS, eLife, OUP NAR, JMD, Annals of Oncology, JCO. Blocked or dead to scripts: Genome Biology (Cloudflare), Genome Research (HTTP 429), AACR and ASH (Silverchair 403), OUP Clinical Chemistry (404). ISSN pulls cover all of them. |

### 3.2 Instrument census (structured fields, no text mining)

| Source | Role | Access | Cadence / volume | Cost | Caveats (reconciled) |
|---|---|---|---|---|---|
| **NCBI SRA via E-utilities** `esearch db=sra …[PDAT]` → `esummary` (500/page) | experiment-level census: `ExpXml` carries `<Platform instrument_model="…">`, study, organism, library strategy, submitting centre | free; 3 req/s, **10 req/s with a free key** (one legal-note table said "3 with key"; wrong) | **121,381 experiments** released 2026-10-10..17 = ~243 `esummary` pages, ~190 MB, ~2.7 min | $0 | Same-day freshness. The instrument model **is** searchable server-side as `"instrument <model>"[Properties]` (e.g. `"instrument illumina novaseq x plus"[Properties]` → 372,570 all-time), which the time-series command uses; `[Platform]` indexes only the family. A parsing bug (double HTML-unescape) lost accession/study/strategy on 5,755 of 121,381 records (4.7%) in the first live run; fix specified. Parallel bursts above 3 req/s draw 429s and trip the circuit breaker, which would silently truncate the census, so SRA and GEO never run concurrently. |
| **ENA Portal API** `/search?result=read_experiment&query=first_public>=…` and `/count?field=instrument_model` | ENA- and DDBJ-submitted experiments (ERX/DRX) with a clean controlled vocabulary; one-call distribution | free | 1,807 ERX/DRX experiments in the test week | $0 | **Not a steady "~10-day" mirror lag**: through early October ENA published NCBI-origin SRR runs 1–2 days after NCBI, then the SRR import stalled on 2026-10-07 while ERR/DRR kept flowing, so on the probe day the gap was 10 days and growing. Design: query NCBI directly for freshness, run ENA with a 14-day lag for the mirrored records, and de-duplicate on study accession. Vocabulary strings carry no vendor prefix (`Revio`, `PromethION`, `Element AVITI`, `UG 100`, `Onso`), and `Illumina NovaSeq X` and `Illumina NovaSeq X Plus` are two values of comparable size (373,566 vs 373,878 runs); the `controlledVocab` endpoint is stale. The upper date bound is exclusive in the current query; fix specified. `read_experiment` returns one row per run, so logged counts are inflated until de-duplicated. |
| **NCBI GEO** (`db=gds`, `GSE[ETYP] AND …[PDAT]`, GPL title lookup) | study-level census: Series × Platform, platform title → instrument model | free; same E-utilities limits | 516 Series / 574 Series×Platform / 138 platforms in the test week; 9 requests | $0 | Built, not yet wired. GSE DocSums carry no platform title, so a batched GPL lookup is required (GPL uid = 100000000 + number, an observed convention with a fallback). GPL34284 is not the only human NovaSeq X Plus platform (GPL37515 duplicates it; mixed-organism GPLs exist); no GPL mentions Visium. GEO under-represents long-read usage (most genome data goes straight to SRA/ENA). A consortium can release a hundred Series in one day (104 of 154 PacBio Series in 2022 landed on 2022-12-04), so read release-day spread before calling a jump a trend. |
| **PRIDE Archive v3** `/search/projects?filter=publicationDate==YYYY-MM-DD` and `/facet/projects` | proteomics datasets per declared mass spectrometer (PSI-MS CV terms) | free; no published limit | 513 datasets / 597 instrument declarations / 67 instrument names in 30 days; 7 requests per week | $0 | Built, not yet wired. Date ranges are not supported in the filter grammar (one request per day); `pageSize` capped at 100. 552 of 597 declarations mapped to a vendor with the current gazetteer (Thermo Fisher 413, Bruker 113, Danaher/SCIEX 21, Waters 5). ProteomeXchange's `GetDataset?action=list` dump (57,301 datasets across six repositories, ~35 MB, invalid UTF-8 bytes) is the cross-repository alternative. |
| **CZ CELLxGENE Discover** `/curation/v1/datasets?visibility=PUBLIC` | single-cell and spatial **assay** (EFO term) behind curated datasets: Chromium chemistry versions, Visium, Slide-seq, Parse, BD Rhapsody, Curio | free; no published limit (client paces 1 s); one ~10 MB request | 2,243 public datasets all-time; 3–5 per week; 27 datasets / 44 assay rows in 60 days | $0 | Built, not yet wired. Records the assay, never the sequencer; curation lags papers by weeks to months, so the 60-day window is the useful cadence. Multi-assay datasets count once per assay (23% list more than one). |
| Planned: **DDBJ Search API** (`/search/api/facets/sra-experiment?…&facets=platform,instrumentModel`) | INSDC-wide census with a 1–4-day lag (99.6% of NCBI's count for a 10-day-old week) and China-vendor platform codes (`GENEMIND` 1,623 experiments 2026 YTD, `SALUS` 72) | free | — | $0 | Recommended replacement for ENA as the primary INSDC view; see ex-US research note. |

### 3.3 Researchers, grants, trials

| Source | Role | Access | Caveats |
|---|---|---|---|
| **OpenAlex authors/topics** | author ids, `counts_by_year`, `summary_stats`, topic membership; works by author/topic/keyword | free key | Only ~1 in 10 profiles carries an ORCID; split and merged profiles are common (one 38,629-work merged profile seen). `/autocomplete/authors` is a display-name prefix match and missed initial-bearing names; resolve by `filter=orcid:` or topic-filtered `/authors?search=`. |
| **ORCID public API** | identity bridge; expanded search with `affiliation-org-name` | free; 12 req/s, 100k reads/day | `Fei Chen` + `Broad Institute` resolved to exactly one record; bare common CJK names return hundreds to thousands (4,254 "Wei Wang"). |
| **NIH RePORTER v2** `POST /v2/projects/search` | S10 shared-instrumentation awards (installed-base signal), SBIR/STTR, keyword hits | free, no key; ≤1 req/s; limit 500, offset ≤14,999 | Built, not yet wired. Records are added in a Saturday weekly batch (three consecutive weeks observed; not officially stated). Camel-case criteria keys are silently dropped and the query becomes "everything". 10 of 57 S10 titles in a quarter named a gazetteer instrument. |
| **ClinicalTrials.gov v2** `GET /api/v2/studies` with Essie filters | newly posted studies whose sponsor, collaborator or intervention names a watched company or assay | free, no key; no official rate limit (~50/min is third-party) | Built, not yet wired. Vendors rarely appear as sponsor or intervention in a two-week window (5 hits); free-text search doubles recall at the cost of precision. |
| **Semantic Scholar Graph** `POST /paper/batch` (500 DOIs) | citation count, influential citations, TL;DR for stored papers | free key **required in practice** (keyless pool answered 429 on most calls) | Built, not yet wired. Positional batch mapping (merged preprint/journal records echo only one DOI). Fresh papers return 0 citations; the signal is velocity on papers weeks to months old. Licence for commercial use not read; check before relying on it. |
| Excluded: **Google Scholar** | — | — | `robots.txt` disallows `/scholar` and `/citations?`; Google's Terms forbid automated access that violates it. No code path. |

### 3.4 Companies, filings, regulatory

| Source | Role | Access | Caveats |
|---|---|---|---|
| **Company gazetteer** (`config/companies.yaml`, 170 entries, 73 with `watch_news`) | the authority: canonical name, aliases, former names, parent, ticker, e-mail domains, type (tools / diagnostics / synthesis / ai_bio / pharma / cro / institute) | local | ROR and OpenAlex are recall engines, never the authority: ROR's affiliation matcher returned ITER, Illumina (Singapore) and UC San Diego for a textbook Illumina San Diego address, none `chosen`; NanoString's ROR record has no Bruker parent (last modified 2024-12-11); Cytiva has no ROR record; Thermo Fisher's ROR children are 14 country entities. |
| **SEC EDGAR** 8-K Atom per CIK; `efts` full-text search | material events and press releases (8-K Exhibit 99.1) for listed names | free; contact in the User-Agent required; 10 req/s | **The current client's User-Agent (`lit-radar/0.1 (+url); mailto:x`) is rejected with 403**; EDGAR wants `Name email`. The feed path yielded 0 rows in the live run until that is fixed (fix specified; the run now skips SEC when no contact address is set). `efts` returns ticker-tagged hits for instrument terms (`"Xenium"` → 23 hits in 2026). 8-Ks cover material events only: 0 filings for 8 of 10 sample names in 30 days. |
| **openFDA** `device/510k.json`, `device/pma.json` | 510(k) clearances, De Novo grants (rows of the 510k dataset with `DEN` numbers), PMA originals and supplements | free; 240 req/min; 1,000/day keyless, 120,000/day with a free key | Built, not yet wired. 510(k)/PMA refresh **monthly** (index 15 days to 6 weeks behind), so the look-back must be ≥45 days; event/recall/UDI refresh weekly. The weekly De Novo web database and the monthly bulk zips are fresher. |
| **Press wires** | PR Newswire health lists (biotechnology, medical equipment) | free RSS | Works (verified in the live run and the news-layer note; one company-mapping note had marked the biotechnology list URL "unconfirmed"). Each list holds ~20 items spanning 1–31 hours, so a weekly poll misses most of it; poll every 4–6 h on a second schedule. GlobeNewswire (Akamai) and the Business Wire portal answer 403; `feed.businesswire.com` custom feeds need a site-captured token. |
| **Trade press RSS** | STAT, BioPharma Dive, MedTech Dive, GEN, Inside Precision Medicine, Labiotech, BioCentury headlines, Nature/Science news | free | GenomeWeb exposes a robots-permitted full-text feed at `/arc/outboundfeeds/rss/?outputType=xml` (100 items, median ~1,950 characters of body) and a news sitemap; its home paths answer 403. Fierce (Cloudflare), The Scientist and Drug Discovery News have no scriptable feed. 360Dx now redirects to GenomeWeb. |
| **Google News RSS** `news.google.com/rss/search?q=…` | 12 of 17 configured feeds plus one query per watched company (~85 requests per run) | works technically (72 of 72 requests HTTP 200, no consent page, 100-item cap) | **Status: on by default in the current code; research verdict is "replace as primary, keep as off-by-default opt-in".** `news.google.com/robots.txt` (last modified 2026-10-06) disallows every path for generic agents except an allow-list that omits `/rss`, and Google's Terms (effective 2026-07-30) prohibit automated access in violation of robots.txt — the same footing on which Google Scholar was excluded. Results are relevance-ranked across years (28–53% from the last 7 days unless `when:7d` is inside `q`); the configured `site:endpts.com` query has been dead since the publisher moved to `endpoints.news`; per-company queries for listed names were 64% finance-aggregator recycling; link decoding now needs a ~0.6 MB page fetch plus an internal RPC, both disallowed, so encoded links are shown as-is. |
| **GDELT 2.0 DOC API** | default company-news source (`litradar/sources/gdelt.py`); Google News is opt-in | free | Returned HTTP 429 ("one every 5 seconds") on 10 of 12 calls and persisted >10 minutes after a four-request burst; `domain:` queries for the trade outlets returned empty. Treated as unavailable until re-probed from another IP. |
| **Bing News RSS** | robots-clean fallback for Fierce, Endpoints, The Scientist, DDN | free | Works for `site:` queries; bare brand queries (`q=Illumina`) return 0 items. The Microsoft Services Agreement's "noncommercial, personal use" clause has not been ruled on. |
| Planned: **CMS Coverage API**, **CLFS/PLA files**, **FDA CDx list**, **EUDAMED** | reimbursement and regulatory deltas | free | See `ROADMAP.md` and the FDA/CMS research note. |

### 3.5 Full text: where it may and may not come from

Full text is fetched only through designated machine routes and stored only when the record's own
licence allows (section 10). Routes in order of preference for a paper: Europe PMC `fullTextXML`
(OA subset); PMC's public S3 bucket `pmc-oa-opendata` (per-article JSON licence metadata, since the
August 2026 removal of the FTP datasets and OA Web Service); bioRxiv/medRxiv requester-pays S3
`.meca` archives (section 3.6); a bioRxiv preprint version of a paywalled paper via the `/pubs`
mapping; GEO/SRA accession metadata for the instrument alone; abstract only. Paywalled text is
linked, never fetched: nature.com publishes a TDMRep reservation (`/.well-known/tdmrep.json` →
`tdm-reservation: 1`, consent required for every purpose), Springer Nature's terms forbid copying
feeds or data "to make or populate a database", and AAAS's terms ban robots and systematic retrieval.

### 3.6 bioRxiv full text via S3 (optional, costed)

`s3://biorxiv-src-monthly` and `s3://medrxiv-src-monthly` (us-east-1, requester pays, AWS account
needed) hold monthly `.meca` ZIP packages with the PDF and full-text JATS XML, deposited a few days
into each month. Terms: text-and-data-mining only, no redistribution or re-hosting, link back to the
server. Cost drivers are requests plus egress at the standard list price of about $0.09/GB (a
third-party figure the research did not re-verify); the bucket's total size (~6 TB third-party) and
the monthly increment have not been measured, so the "tens of dollars per month incremental, ~$500
for a full backfill" estimate is **unverified**. A GitHub Actions runner is not inside AWS, so egress
applies. Until Europe PMC resumes bioRxiv full text, this is the only complete route to preprint
Methods sections; it is off by default.

## 4. The instrument census: what the counts mean

The census counts **declared instrument models on newly public data records**, not machines. Four
rules govern how the numbers may be read:

1. **Experiments are not instruments.** One sequencer running population cohorts can produce
   thousands of SRA experiments in a month; a clinical laboratory produces none. A Revio series that
   jumps from 1,149 experiments (2026-07) to 4,418 (2026-08) is a handful of large releases, not a
   step change in the installed base: a 1,000-record sample of that month spanned 61 studies and the
   three largest made up 73% of it.
2. **Deposit lag.** Data becomes public when a paper is accepted or an embargo lifts, typically 6 to
   24 months after sequencing. Recent months are always under-counted and keep growing for years; the
   time-series command re-fetches the last two months on every run and never lets a fresh zero
   overwrite a positive cached count (a sudden zero means the archive renamed the vocabulary string).
3. **No clinical, commercial or consumer data.** Diagnostic labs, pharma, agrigenomics services and
   population programmes do not deposit to SRA or ENA. A vendor whose growth is clinical can be flat
   here while its consumables revenue grows.
4. **Mirrors allocate months differently.** SRA `[PDAT]` and ENA `first_public` agree over long
   windows (Revio 2023-01..2026-09: 32,855 vs 31,820) but not per month (2024-04: 10 vs 138). Compare
   within one source or sum to quarters.

Structured fields (tier 1) are the backbone: SRA/ENA `instrument_model`, PRIDE `instruments` (PSI-MS
CV), CELLxGENE assay EFO terms, GEO platform titles. Text mining (tier 2) attaches instruments to
*papers* where tier 1 attaches them to *datasets*: Methods sections, Key Resources Tables and
catalog numbers (10x `PN 1000xxx`, Illumina `2002xxxx`, ONT `SQK-`/`FLO-`), which disambiguate a
vendor with near-zero false positives and never appear in abstracts. The two tiers are stored in the
same `archive_records` and `mentions` tables so a question like "Xenium in 90 days" draws on both.

Vocabulary normalisation is mandatory and lives in `config/archive_models.yaml` (27 entities), which
maps each gazetteer id to SRA lower-case `[Properties]` strings, exact ENA strings and Europe PMC
Methods phrases. Models with no archive value (Nanopore sub-models and Flongle, MiSeq i100, DNBSEQ
G99/T20, Singular G4, Roche SBX) are reachable only through Methods text.

## 5. The gazetteer and its precision rules

`config/instruments.yaml` carries 335 entries across 19 categories (short-read sequencers 38,
automation 36, imaging 32, library prep 28, spatial 25, software 19, MS proteomics 18, bioprocess
17, single-cell 16, synthesis 15, cell engineering 15, cryo-EM 14, QC 12, PCR 11, cytometry 11,
long-read 10, affinity proteomics 10, reagent 5, array 3). Each entry has a stable id, canonical
name, vendor as the brand appears in papers, the ticker of the current owner, launch year, status,
and aliases as they are actually spelled in Methods. The research gazetteer behind it (247 entries,
3,404 aliases, 82 verified against primary or trade sources) is in the research folder; the repo
version was merged with the archive vocabulary and the review's corrections.

Matching rules, in order:

- Aliases become word-bounded regexes; spaces and hyphens inside an alias match any mix of
  whitespace, hyphen or nothing (`NovaSeq X Plus`, `NovaSeq-X Plus`, `NovaSeqX Plus`).
- Overlapping matches resolve to the longest alias, so `NovaSeq X Plus` is not also counted as
  `NovaSeq X`.
- **Context gating.** Entries flagged `context_required` (19) fire only when a context term (the
  vendor by default) appears within ~250 characters; `case_sensitive_aliases` (89 entries) must match
  case exactly. The false-positive table in the research note drives these: `Chromium` the element,
  `G4` the quadruplex, `Fusion` the gene event, `Falcon` the tube, `Aurora` the kinase, `STAR` the
  aligner, `Astral` the microtubule, `MSD` the pharma company in COI text, `Platinum` the polymerase,
  `Vega` the surname, `Stellaris` the FISH probe, `COMET` the assay.
- **Trademark-as-technique words** (ddPCR, FACS, CyTOF, MERFISH, Slide-seq, CODEX, HiFi, lattice
  light-sheet) are method mentions unless a model or vendor is adjacent; two "method (no vendor)"
  entries keep academic protocols from being attributed to companies.
- **Archive strings skip the context rules.** Controlled-vocabulary values (`Revio`, `Sequel`,
  `PacBio RS II`) are unambiguous; matching them with prose rules left 1,120 of 3,380 PacBio
  experiments (33%) unattributed in the first live run and split vendors into `Pacific Biosciences`
  and `PACBIO_SMRT`. `match_archive_model` now ignores context, and `rematch` re-tags stored archive
  rows with the current gazetteer.
- **Ownership as of publication date vs today.** The gazetteer stores the brand on the box and the
  owner today; an ownership-history table with effective dates is planned so a 2021 "NanoString
  GeoMx" paper rolls up to BRKR today but to NSTG at publication. Verified deal dates: NanoString →
  Bruker 2024-05-06 (Section 363 sale, ~$392.6M cash); Olink → Thermo Fisher 2024-07-10; Fluent →
  Illumina 2024-07-09; Akoya → Quanterix 2025-07-07; Stilla → Bio-Rad 2025-06-30; SomaLogic →
  Illumina **2026-01-30** ($350M cash plus up to $75M milestones; Illumina bought out the earn-out
  for ~$30M on 2026-07-28, and Standard BioTools has since agreed to sell mass cytometry to Element
  Biosystems and microfluidics to ARCHIMED, so "Standard BioTools retains CyTOF/Hyperion/Biomark" is
  stale); BD Biosciences → Waters **2026-02-09** (Reverse Morris Trust; products keep the BD prefix).
  Unverified: 10x Genomics–Scale Biosciences, QIAGEN–Parse Biosciences (no closing found); Complete
  Genomics → Swiss Rockets AG pending (agreed February 2026).
- **Announced-but-unshipped platforms** (VITARI, UG 200, Simoa ONE, Nautilus, Tribrid Apex) are a
  separate high-interest class: a Methods mention before commercial shipping implies early access.
  Atera moved from announced (2026-04-18) to shipping (2026-09-29) during the research.

The review's precision pass on 25 real full texts found the remaining systematic failure: the
matcher's ambiguity heuristic only case-gates lower-case tokens of five letters or fewer, so any
capitalised dictionary word (`Evident`, `ENrich`, `TransIT`, `Myriad`, `Recursion`, `Ginkgo`, `WuXi`)
matched without context. Those aliases are being made case-sensitive with context terms; a stoplist
is the code-level follow-up. Bare company names and consumable brands (Promega, QIAGEN, Gibco,
Lipofectamine) have been removed from instrument aliases so they no longer top the instrument table.

## 6. Researcher tracking

OpenAlex is the backbone: a stable author object, `counts_by_year`, `summary_stats`, topics and
affiliations, and a works endpoint filterable by author, topic, keyword, institution, type and
publication date. Each of the twelve subgroups in `config/subgroups.yaml` is defined as topic ids
intersected with keyword slugs (33 distinct topic ids; T11289 "Single-cell and spatial
transcriptomics" is the only topic covering either field; no dedicated topic exists for
perturbation screens, protein design, long-read or MCED), plus a seed list of 160 tracked
researchers, 123 of them resolved to OpenAlex author ids.

Rules that came out of the research:

- Resolve people by ORCID (`filter=orcid:`) or by topic-filtered `/authors?search=`, never by
  `/autocomplete/authors` alone; store **all** accepted profile ids per person (split profiles are
  normal), and guard against merged profiles with a works-count outlier check (>3× the subgroup
  median) and a human queue.
- Compute last-author and corresponding status client-side from `authorships[].author_position` and
  `is_corresponding`; refetch single works when `authors_count > 100`.
- Always add `type:article|preprint` to avoid dataset deposits polluting leaderboards.
- Poll `from_publication_date` with a 21–28-day overlap (the right width is an untested guess to be
  measured in the first month); never depend on the paid sync filters.
- **Name matches are provisional.** The digest marks a name-only match ("add the OpenAlex id to make
  it exact"); common names (`Fei Chen`, `Xiao Wang`) stay unresolved until an ORCID or an
  affiliation-qualified search pins them. ORCID expanded search with `affiliation-org-name` resolved
  the hardest seed case to a single record.
- Metrics for "leading" and "rising" are defined in the researchers note (senior-author output,
  subgroup share, preprint velocity, citation velocity, field-normalised impact, new grants, trials)
  and are a roadmap item; z-scores need ~30 people per subgroup.

## 7. Company mapping

A company can touch a paper in four roles, and the digest keeps them apart because they mean
different things to a reader:

| Role | Evidence | Where it is found |
|---|---|---|
| **Author** | an author's affiliation string or e-mail domain belongs to the company; OpenAlex `authorships.institutions.type:company` or `lineage:<id>` | PubMed `AffiliationInfo`, Europe PMC `authorAffiliationDetailsList`, JATS `<aff>` and `<email>`, OpenAlex |
| **Funding / competing interest** | the company is named in a funding, COI or acknowledgement section | PubMed `CoiStatement` (the only aggregator exposing COI text; `COIS` search field) and `GrantList`; Europe PMC `ACK_FUND:` / `COMP_INT:` (search only); JATS sections located by **title regex**, because only 1 of 5 sampled OA files used `<funding-source>` or `sec-type="COI-statement"` |
| **Subject** | the company or its product is what the paper is about (title/abstract) | gazetteer over title and abstract |
| **Supplier** | a company's instrument or kit is named in Methods | the instrument gazetteer; credited through the platform entity, not double-counted as a company mention |

The company table in the digest is typed (tools / diagnostics / pharma / institute …) so that
institutes and pharma COI disclosures do not read as vendors, and "we used ChatGPT for language
polishing" does not make an AI company a supplier. Institution typing comes from OpenAlex (8 types)
and from the local gazetteer; e-mail domains (`illumina.com`, `10xgenomics.com`, …) are the
highest-precision corporate-author signal and are stored per company.

Identifier crosswalks for listed names are free: SEC `company_tickers.json` (10,443 rows), OpenFIGI
(25 req/min keyless), GLEIF LEI (parent/child links), Wikidata SPARQL (ROR ↔ exchange listing, 3,077
items). Commercial startup databases are not needed and have no self-serve API tier. New-entrant
candidates come from the Methods parenthetical convention `(Vendor, City, Country)`, ROR records
created recently with `types:company`, first SBIR/STTR awards, first `INDUSTRY` trial sponsors and
first openFDA applicants.

## 8. Ranking

### 8.1 Heuristic (always on, legible)

Every point has a reason string, printed under each digest item.

| Component | Default weight |
|---|---|
| journal tier | A = 4, B = 2.5, C = 1, preprint = 1, unknown = 0.5 |
| platforms named (sequencers, single-cell, spatial, proteomics, cryo-EM, arrays, synthesis, cell engineering) | +1.0 per distinct entity, cap 3 |
| lab tools named (QC, library prep, PCR, automation, imaging, cytometry, bioprocess) | +0.2 per distinct entity, cap 3 |
| companies named | +0.5 per distinct entity, cap 3 |
| company-affiliated author | +1.0 |
| watchlist terms (`config/radar.yaml`) | +1.0 per term, cap 3 |
| tracked author | +3.0 |
| novelty language | +0.5 per cue, cap 1.0 |
| early citations (`cited_by_count > 0` inside the window) | +1.0 |
| full text parsed | 0 by default (configurable) |

The sum is scaled to 0–10. The review found that with these weights only 42 of 220 tier-A journal
articles reached the LLM shortlist and 33 of the top 50 were among the 12% of papers with parsed
full text (Nature Methods' best rank was 106): parsed Methods inflate mention counts. The fix being
specified weights company mentions by section (title/abstract ×1, funding/COI ×0.5, Methods ×0) and
guarantees tier-A articles a place in the shortlist.

### 8.2 LLM rubric (optional, `--llm`)

The shortlist (heuristic ≥ 3.0, at most 300 papers) is sent to Claude with a cached system rubric
and a strict JSON schema (`output_config.format`, `additionalProperties: false`; numeric and
string-length constraints are not supported by the API and must live in descriptions). Dimensions:
`novelty` 1–5, `commercial_implication` 1–5, `instrument_role` ∈ {none, incidental, central,
new_platform}, `clinical_readiness` ∈ {basic … approved_or_commercial}, `hype_risk` 1–5 (inverse),
`watchlist_relevance` 0–5, `one_line`, `why_it_matters`, `companies_implicated`,
`instruments_implicated`. The model sees title, abstract (≤6,000 characters), venue and the
matcher's hits — never stored full text. Refusals are recorded as unscored. Composite = 0.30
novelty + 0.30 commercial + 0.20 watchlist + 0.6 role + 0.4 readiness − 0.15 (hype − 1), mapped to
0–10; **final = 0.4 × heuristic + 0.6 × LLM**. Rubric and schema must stay byte-stable through a run:
a whitespace change invalidates both the prompt cache and the 24-hour grammar cache.

### 8.3 Cost model

Measured and list-price figures; "estimate" marks a derived number.

| Line | Per week | Per year | Basis |
|---|---|---|---|
| OpenAlex, free key | $0 (inside the $1/day budget; a batched week is ~50–200 list calls at $0.0001) | $0 | official pricing, 2026-10-17 |
| NCBI E-utilities, Europe PMC, Crossref, bioRxiv API, ENA, GEO, PRIDE, CELLxGENE, CT.gov, RePORTER, openFDA, SEC | $0 | $0 | free APIs; keys only lift rate limits |
| GitHub Actions | $0 on a public repository (standard hosted runners are free); on a private repository a ~25–35-minute weekly Linux run is ~87–150 minutes/month inside the 2,000 free minutes (Linux 2-core overage $0.006/min) | $0 | official billing page; measured run 14 min 17 s with OpenAlex and bioRxiv full text blocked, estimated 25–35 min healthy |
| LLM scoring, **current code** (single stage over ≤300 items, ~600 input + 250 output tokens each) | Opus 5.5 ≈ $2.2 ($1.1 via Batches); Sonnet 5.5 ≈ $1.1 ($0.55); Haiku 5.5 ≈ $0.06 ($0.03) | ≈ $58 / $29 / $1.5 at batch prices | list prices $4/$20, $2/$10, $0.10/$0.50 per MTok; Batches 50% off (estimate) |
| LLM scoring, **recommended cascade** (Haiku 5.5 triage over ~5,000 items, `effort: low`; Opus 5.5 deep scoring of the top ~250 with ~4,000-token Methods excerpts) | ≈ $1.11 + $4.75 = **$5.86** | **≈ $305** | research cost model at Batches prices, no cache credit assumed (batch requests run in parallel, so cache hits are upside); a single-stage Opus pass over everything would be ~$44.50/week (~$2,300/yr) |
| bioRxiv S3 requester-pays (optional) | tens of dollars/month incremental (unverified); ~$500 one-off full backfill at ~$0.09/GB (third-party, unverified) | — | section 3.6 |
| Subscriptions (optional, for the human reader; none grants text-mining rights) | Nature+ Personal $32.99/30 days ≈ $401/yr; STAT+ $399/yr; GenomeWeb Premium by quote ($95 90-day trial); scite Pro $40/month billed yearly; Altmetric commercial by contract | — | `SUBSCRIPTIONS.md` |

A `MAX_WEEKLY_LLM_USD` cap, computed before submission and reported in the digest when exceeded, is
specified but not yet enforced in code.

## 9. Delivery and persistence

The digest is Markdown with a JSON summary: top innovations with reasons; platforms named this
week; lab infrastructure and consumables named; instruments behind newly public sequencing data
(SRA + ENA, vendor share, library strategies); companies in the literature by role; tracked
researchers by subgroup; trade press and company news grouped by company; a coverage footer. It is
e-mailed (SMTP; STARTTLS must use a verified default context, a fix the review specified), posted
to Slack, and committed to `digests/`. Scheduling is a GitHub Actions cron on Mondays at an
off-the-hour minute (the `schedule` event is delayed at the top of the hour, runs only on the
default branch, and is disabled on public repositories after 60 days without activity; the weekly
commit is the activity).

**Persistence is the known weak point.** The SQLite database (117.7 MB after one 7-day run, ~31 MB
gzipped) lives only in the Actions cache, which GitHub evicts after 7 days without access, exactly
the interval of a weekly job: one skipped Monday resets the time series. The database cannot be
committed (GitHub blocks files over 100 MiB). The specified fix uploads a compressed copy as a 90-day
workflow artifact every run and restores from the last successful artifact on a cache miss; a
committed Parquet export is the longer-term answer (section 12). Related fixes: checkpoint and close
the store before the cache is saved (a timed-out run otherwise saves a database missing its WAL),
split the workflow into a read-only run job holding the API keys and a key-less publish job writing
to a `digests` branch, SHA-pin actions, pass `workflow_dispatch` inputs through environment
variables, and redact `api_key`/`mailto` parameters from logged URLs and exception text.

## 10. Data policy in one table

`litradar/policy.py` returns one of three storage levels per record; `LEGAL.md` is the full policy.

| Record licence / route | Store full text | Store derived facts (mentions, snippets, offsets) | Digest |
|---|---|---|---|
| CC0, CC BY, CC BY-SA, CC BY-ND, PMC Open Access subset (`isOpenAccess = Y`), PMC author manuscript | yes (`full`) | yes | link + snippets with attribution |
| CC BY-NC, CC BY-NC-SA, CC BY-NC-ND (commercial operator) | configurable; default **no** (`facts_only`) | yes | link + short snippets |
| bioRxiv `cc_no`, arXiv non-exclusive licence, unknown or publisher-specific licence | no (`facts_only` at most) | yes | title, abstract, link |
| paywalled, or no machine-readable text | never (`none`) | — | DOI link; "Methods not machine-readable" flag |

The review found that the first live run cached bioRxiv full text without a licence gate (four
`cc_no` preprints with 54–133k characters of body each), that the Europe PMC ISSN pull had no
`OPEN_ACCESS:y` filter (the server's HTTP 500 for non-OA PMCIDs was the only guard), and that
`docs/LEGAL.md`, cited in four places, did not exist. The policy module landed; wiring it in front
of every fetch, the OA filter, and the legal document are being done by the code and legal owners.

## 11. Security posture

Credentials live in two places only: `.env` on the operator's machine and GitHub Actions repository
secrets for CI; the repository ships `.env.example` with empty values. What belongs there: the
contact e-mail; free API keys (NCBI, OpenAlex, Semantic Scholar, openFDA, OpenFIGI, ORCID public
client); an LLM key with an expiry and a spend cap; an SMTP app password; a Slack webhook; a
least-privilege AWS key for the requester-pays bucket. **Publisher logins never, not even as a
commented placeholder** (the shipped `.env.example` had `PUBLISHER_*` slots and a wrong "paid tier"
comment on the OpenAlex key; both are being removed). Only five of ~20 credential shapes are
recognised by GitHub secret scanning (Anthropic `sk-ant-`, AWS, Slack webhooks, Google service
accounts), so structure, not scanning, is the control: `permissions: {}` at workflow level,
`persist-credentials: false`, hash-pinned dependencies, pre-commit secret scanners as a backstop,
and a 60-minute incident runbook (revoke, `git filter-repo`, force-push, contact support for cached
views). The watchlist (`companies.yaml`, `subgroups.yaml`, `watchlist_terms`) is public by
construction in a public fork; a private overlay directory is the specified mechanism for a personal
list.

## 12. The global-corpus path (planned and in progress)

The weekly digest is the first product; the corpus is the second. In order of readiness:

1. **Wire the built adapters** (GEO, PRIDE, CELLxGENE, ClinicalTrials.gov, NIH RePORTER, openFDA,
   Semantic Scholar): each has a documented config block and pipeline patch in `docs/sources/`.
   Keep GEO rows (studies) out of the SRA/ENA experiment totals.
2. **Week-over-week deltas and trends.** A `weekly_entity_counts` table written at the end of each
   run (archive models, vendors, literature instruments, companies) feeds "up/down vs last week" and
   first-appearance flags; the `timeseries` command already backfills monthly SRA/ENA/Europe PMC
   counts per model to 2019 (Revio: 2,068 requests for all PacBio models over 94 months, cached per
   count so an interrupted backfill keeps what it fetched).
3. **OpenAlex backfill and snapshot.** For history beyond the weekly window, the free key's $1/day
   budget batches 100 DOIs per call; for the full corpus the OpenAlex snapshot (CC0) is the route,
   loaded into Parquet rather than SQLite.
4. **Parquet export, DuckDB escape hatch.** SQLite stays the store (upsert-heavy, transactional,
   FTS5); a weekly `exports/papers_{YYYY-WW}.parquet` partitioned by month is the analytics
   sidecar, 5–10× smaller than CSV; DuckDB reads both and has its own FTS. Migration pressure
   realistically begins above 5–10M rows, years away for a weekly radar. FTS5 lookups by the
   unindexed id column are currently full scans (upsert and rematch are O(N²)); keying the FTS table
   by rowid is the fix.
5. **Dashboard.** A static site rendered from the store, opt-in and off by default, showing ranked
   items and public metadata only: GitHub Pages sites are public even for private repositories, so
   the watchlist must never be rendered into it.
6. **China via GSA and ChinaXiv; Japan; Korea; Latin America.** GSA (CNCB-NGDC) is not an INSDC
   member, mirrors INSDC into itself (42.19M of 43.43M experiments), holds ~1.23M native experiments
   and has no public API behind a script-blocking cookie gate: request sanctioned access, do not
   script around it; the MGI-share miss is a 21–41% range, not a measurement. The practical adapter
   is DDBJ Search (section 3.2). Nine Chinese-published English journals (Cell Research, Protein &
   Cell, Science China Life Sciences, National Science Review, GPB, STTT, Cell Discovery, JGG, hLife)
   are missing from the ISSN list; three deposit no Crossref abstracts and hLife is Crossref-only.
   ChinaXiv's OAI endpoint is IP-allow-listed and has zero coverage in Europe PMC. Japan: J-STAGE
   for DOI discovery only (24-hour machine-readable caching ban) plus CiNii for abstracts; Korea:
   KCI Open API (free key, English abstracts and ORCIDs); Europe PMC already indexes SciELO
   Preprints. Non-US funders with instrument-placement signal: UKRI Gateway to Research (open JSON,
   infrastructure grant category), OpenAIRE Graph (register a client: 7,200 req/h), CORDIS monthly
   dumps, ARC `lief-register`, Wellcome 360Giving.
7. **Translation layer.** Measured non-English core-topic volume is ~3 abstracts per week (PubMed
   `chi[la]` proxy: 5 records in 17 days), so translation is a rounding error: even 500/week costs
   about $0.13 (Haiku 5.5) to $5.20 (Opus 5.5) per week at Batches prices. Translate only items that
   pass the entity pre-filter and keep originals.
8. **Retraction and correction tracking** (`update_event` table, re-surfacing section, score
   demotion ladder): Crossref `is-update:true` as the same-day alert feed (1,094 notices in the test
   week: 518 corrections, 329 errata, 89 retractions, 19 expressions of concern), the Retraction
   Watch CSV (CC0, GitLab, 73,283 rows, newest row 8 days old) as the reasons layer, PubMed
   `"Retraction Notice"[pt]` (the type was renamed in 2025; the old string returns 0), Europe PMC
   `POST /status-update-search` as confirmation, and bioRxiv/medRxiv `type: withdrawn` versions.
9. **Regulatory and reimbursement block**: openFDA plus the weekly De Novo database and monthly
   bulk zips; the FDA CDx list hashed by row; CMS Coverage API `/v1/reports/whats-new/local` for
   MolDX LCD deltas; quarterly PLA code releases and the September/November CLFS determinations
   (LDTs appear in no FDA database since the LDT rule was rescinded on 2025-09-19); EUDAMED for EU
   IVD registrations.

## 13. What did not work, and why

| Attempt | Result | Consequence |
|---|---|---|
| Fetching bioRxiv/medRxiv JATS from the `jatsxml` URLs | HTTP 429 "error code: 1015" (Cloudflare rate limit) from every client, UA-independent; 570 of 600 attempts failed in the live run, costing a 30 s `Retry-After` sleep | preprint Methods come only from S3 requester-pays or Europe PMC; the default `fulltext: true` for bioRxiv wastes requests and should be off until the S3 route exists |
| Vendor investor-relations RSS (27 companies) | 25 of 27 returned 403/404 | vendor news comes from the wires, EDGAR 8-K exhibits and an alias table, not IR sites |
| OpenAlex without a key | the $0.10/day budget shared behind one IP was exhausted mid-research; several verifications could not be re-run live | a free key is mandatory for every fork; keyless forks fail silently |
| Google News RSS as the news layer | technically fine (72/72 HTTP 200) but robots-disallowed and ToS-conflicting; one query dead for a year (`site:endpts.com`), one outlet redirected (360Dx), per-company results mostly finance aggregators | opt-in only; GenomeWeb's own Arc feed, direct trade RSS, EDGAR and sub-daily wire polling replace it |
| Trade-press feeds: GenomeWeb home paths, Fierce, GlobeNewswire, Business Wire portal, The Scientist, Drug Discovery News | 403 (Akamai/Cloudflare) or no feed | GenomeWeb Arc outbound feed; Bing News `site:` as robots-clean fallback; newsletters via the owner's mailbox (unverified) |
| Journal RSS: Genome Biology, Genome Research, AACR, ASH, OUP Clinical Chemistry | Cloudflare bot page / 429 / Silverchair 403 / 404 | ISSN pulls through Crossref and Europe PMC cover them |
| SEC EDGAR Atom with the client's User-Agent | 403 for all 44 feeds in the live run | send `Name email` as EDGAR prescribes; skip SEC when no contact is configured |
| ROR affiliation matcher for company addresses | wrong top hit, nothing `chosen` for a textbook Illumina address | local alias gazetteer first; ROR as id source only |
| Europe PMC as the preprint full-text source | bioRxiv full text zero since June 2026; 6% of preprints overall | section search cannot carry preprint instrument detection |
| Crossref `prefix:10.1101` for new preprints | 0 results: ownership moved to 10.64898 / member 54368 | filter by member or `type:posted-content`, never by DOI string |
| EMBO Early Evidence Base DocMaps API; Europe PMC `HAS_EVALUATIONS:Y` | 404; 0 hits (field apparently retired) | refereed-preprint signals unavailable via a verified free endpoint |
| SBIR.gov awards API; PatentsView PatentSearch API | 403 "undergoing maintenance"; paused since 2026-03-20 | monthly SBIR CSV plus NIH RePORTER; Google Patents on BigQuery (1 TiB/month free) or EPO OPS for patents |
| GDELT DOC API | persistent 429 after a four-request burst from one residential IP; empty `domain:` results for trade outlets | wired as the default with 5 s pacing and a per-run circuit breaker; the digest reports how many company queries were skipped when the host rate-limits; re-probe from the CI runner's IP |
| 10x Genomics, Illumina, Olink, PacBio, Oxford Nanopore publication pages | Vercel checkpoint (429) or JavaScript-only, no feed | Bruker Spatial Biology and Parse Biosciences WordPress REST `publication` endpoints are the only vendor feeds that answer |
| Altmetric keyless `/v1/doi/{doi}` | 403 from this egress, same session in which other WAF-protected hosts also returned 403 | ambiguous (closed vs blocked IP); the free tier is for university scientometrics researchers only, so a commercial operator needs a contract or skips it |
| GSA (CNCB-NGDC), ChinaXiv OAI, NMPA database | JavaScript cookie gate (host stopped answering after ~40 requests); IP allow-list; WAF 412 | request sanctioned access; do not script around |
| Scopus, Web of Science, Dimensions, Lens | institution-licensed; free tiers exclude commercial and investment use; no self-serve price | not budgeted |

## 14. Open questions

1. Is bioRxiv's absence from Europe PMC full text since June 2026 permanent? If so the S3 route is
   mandatory and its cost must be measured (`aws s3 ls --summarize --request-payer requester`).
2. Are the `jatsxml` URLs fetchable from a GitHub Actions (Azure) egress IP, or only blocked from
   the probe network? Likewise Altmetric keyless access and Google News consent pages from CI.
3. Crossref polite-pool rate: 3 or 10 per second, and is the header stable across days?
4. Is the ENA SRR import stall of 2026-10-07 an incident or a change in mirroring?
5. How much publication-date back-dating does OpenAlex apply to journal articles? This sets the
   `from_publication_date` overlap window.
6. Prompt-cache hit rate inside Message Batches (upside only; not budgeted).
7. Which of the 37 unresolved tracked researchers can be pinned by ORCID, and how many OpenAlex
   profiles are merged?
8. Deal statuses for 10x–Scale Biosciences and QIAGEN–Parse Biosciences; closing of the Complete
   Genomics sale.
9. Whether storing non-OA abstracts from Crossref/PubMed is itself restricted for a commercial
   operator (both say abstracts "may be subject to copyright"; neither prohibits storage).
10. Whether an internal weekly digest built on Bing News RSS is "noncommercial, personal use" under
    the Microsoft Services Agreement.
11. Semantic Scholar's API licence for use inside a commercial workflow.
12. The platform mix of GSA-native experiments, which decides how much MGI output the census misses.

## Appendix: the measured week (2026-10-10 to 2026-10-17)

3,562 new papers and preprints; 429 with parsed full text; 202 instrument entities and 109
companies detected; 123,164 new archive records (121,381 SRA experiments + ENA); 504 news items;
~1,000 HTTP requests (28 cache hits, 10 retries, 51 failures, 562 blocked skips); 14 min 17 s
wall-clock; peak RSS 252 MB; database 117.7 MB. The gazetteer matcher was the CPU hot spot
(~3.4 min of the run; a literal-token prefilter gives 7–10× with identical results). Weekly request
budget by host: Europe PMC ~430 (26 searches + 400 full texts + 2 ENA), NCBI ~244, Google News ~85,
SEC 44, journal/subject RSS 43, Crossref ~45, bioRxiv ~21 pages, OpenAlex 21–210 when keyed,
arXiv 1.
