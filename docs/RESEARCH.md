# Research: how the evidence behind this design was produced and checked

*Index of the research notes that motivate `DESIGN.md`, `SOURCES.md`, `SUBSCRIPTIONS.md` and
`ROADMAP.md`, with the verification record. All work was done on 2026-10-17; counts and prices are
point-in-time. The note files live in the research folder that accompanies the repository; file
names below are theirs.*

## 1. Method

The research ran in four stages, each by agents that did not share state with the others:

1. **Nine domain sweeps.** One agent per domain, briefed to execute live requests rather than trust
   documentation, to tag every statement `[LIVE]`, `[DOC]`, `[3P]` (third-party) or `[UNVERIFIED]`,
   and to end with recommendations, load-bearing claims with source URLs, and open questions.
   Roughly 150 live API calls in the aggregator sweep alone; every feed URL in the brief was probed;
   every archive endpoint was called with the exact query later used in code.
2. **Independent fact-check of 54 load-bearing claims.** Six claims per sweep were handed, without
   the sweep's reasoning, to separate agents who re-ran the call or fetched the primary document
   and returned a verdict (`confirmed`, `partially_confirmed` with corrected wording, or `refuted`),
   an evidence URL and notes. Result: **26 confirmed, 28 partially confirmed, 0 refuted.** "Partially
   confirmed" means the numbers reproduced but the wording over-reached, omitted a qualifier, or
   drew a wrong inference; the corrected wording is what the design documents use (table in
   section 3).
3. **Adversarial critic.** One agent read all nine notes and the shipped repository and reported
   where the repo contradicted the research, where notes contradicted each other, which central
   claims remained unverified, and which gaps deserved a further brief. Its inter-note
   inconsistencies are reconciled in `DESIGN.md` section 3 and listed in section 4 below.
4. **Five gap fills**, each with its own live probing, on the topics the critic ranked highest:
   security hygiene for a public forkable repo; the Google News RSS dependency; FDA/CMS regulatory
   and reimbursement sources plus clinical-lab venues; retraction, correction and withdrawal
   tracking; ex-US sources, funders and CJK author disambiguation. Their 40 claims carry source URLs
   but were **not** put through the independent fact-check; they are marked as such wherever used.

In parallel, an adversarial **code review** of the repository ran in seven dimensions (security,
legal policy, robustness, digest quality, API contracts, performance, gazetteer precision); each
finding was then handed to a sceptic who tried to refute it by reproduction. The verified findings
and the fixes they specify are summarised in `DESIGN.md` (sections 5, 8–11, 13) and recorded in
`review_findings.json` alongside the research notes.

## 2. The notes

| File | Domain | Words | What it establishes |
|---|---|---|---|
| `journals.md` | journals and feeds | ~9,500 | Every journal in scope has a free table-of-contents feed; which publishers deposit abstracts to Crossref (Elsevier 0%, Nature-branded 10–23%, AAAS/OUP/CSHL/eLife/PNAS/AACR/ASCO/ASH 85–100%); Europe PMC OA share per journal; personal-subscription prices and terms; no personal subscription grants TDM rights. |
| `aggregators.md` | bibliographic aggregator APIs | ~8,500 | Fifteen APIs with endpoints, fields, filters, limits and prices; the minimum free stack (Crossref + PubMed + Europe PMC + keyed OpenAlex); Europe PMC section-scoped search; OpenAlex metering; Semantic Scholar key requirement; Scopus/WoS/Dimensions/Lens excluded. |
| `preprints.md` | preprint servers | ~8,300 | bioRxiv API behaviour (30 per page, HTTP 200 errors, prefix 10.64898, licence distribution, Cloudflare on `jatsxml`, S3 route); Europe PMC as the spine over 30+ servers; preprint→published matching via Crossref relations, bioRxiv `/pubs`, OpenAlex merged works, Europe PMC `commentCorrectionList`; licensing policy per preprint. |
| `instrument_archives.md` | data archives | ~6,100 | Live-tested SRA, ENA, GEO, BioStudies, PRIDE/ProteomeXchange/MassIVE, MetaboLights, single-cell portals, controlled-access archives; the INSDC vocabulary strings; freshness per archive; GEO GPL conventions; the recommended weekly instrument pipeline. |
| `instrument_gazetteer.md` | instrument gazetteer | ~25,600 | A 247-entry, 3,404-alias YAML gazetteer with vendor, ticker, launch year, status and spelling variants; the corporate ownership map with deal dates and sources; the high-ambiguity token table; verification log. |
| `researchers.md` | researcher tracking | ~10,200 | OpenAlex as backbone (topic ids per subgroup, author resolution rules, paid sync filters); ORCID, Semantic Scholar, Europe PMC and Crossref as bridges; NIH RePORTER, ClinicalTrials.gov, patents, conferences, rosters; metrics for leading and rising researchers; Google Scholar excluded; illustrative seed lists. |
| `ranking_delivery.md` | ranking, storage, delivery, scheduling | ~11,800 | The two-stage LLM cascade and its cost (~$5.86/week); OpenAlex metering as a design constraint; which RSS feeds work; dedupe linkage sources; SQLite + FTS5 schema; LLM pricing and Batches limits; delivery options; GitHub Actions caveats; MVP vs v2. |
| `companies.md` | company mapping | ~8,600 | Gazetteer-first entity resolution with ROR/OpenAlex/Wikidata as recall engines; ROR's failures on company strings and M&A lag; free-text funding/COI sections; Europe PMC fields for Methods/funding/COI; SEC EDGAR full-text search; free identifier crosswalks; new-entrant candidate sources. |
| `legal_tos.md` | legal and terms of service | ~10,500 | The operator is commercial; every non-commercial door is shut; publisher terms (Springer Nature, AAAS, Elsevier, Wiley), machine-readable rights reservations, PMC/Europe PMC/bioRxiv/arXiv routes; the licence-to-storage decision function; rate-limit defaults; licence and NOTICE recommendations; genuine uncertainties. |
| `gap-security-hygiene-public-repo.md` | security | ~9,900 | Which credentials belong in `.env` and CI secrets (publisher logins never); verified repo findings; GitHub-side controls; corrected `.env.example` and a two-job workflow; redaction module; pre-commit scanners; incident runbook. |
| `gap-google-news-rss-news-layer.md` | news layer | ~8,400 | Verdict: replace Google News RSS as primary, keep as opt-in; robots/ToS analysis; 72-request reliability measurement; link decoding; recall of `site:` queries vs GenomeWeb's own feed; alternatives (Bing, GDELT, Yahoo, CC-NEWS, NewsAPI, Inoreader, Feedly); feed-health policy. |
| `gap-fda-cms-reimbursement-and-clinical-lab-venues.md` | regulatory and reimbursement | ~9,700 | FDA freshness tiers (De Novo DB weekly, bulk zips monthly, openFDA monthly); CDx list structure; LDT rule status; CMS Coverage API and MCD zips; CLFS/PLA cycles; clinical-lab journals and supplement shapes; EUDAMED/NICE/PMDA/NMPA; digest sections and weights. |
| `gap-retraction-correction-withdrawal-tracking.md` | integrity signals | ~7,800 | Crossref as the same-day alert feed; Retraction Watch CSV as the slow reasons layer; PubMed's renamed publication type; Europe PMC status monitor; preprint withdrawal representation; `update_event` schema, re-surfacing rule and demotion ladder; licensing per source. |
| `gap-ex-us-sources-funders-and-cjk-author-disambiguation.md` | ex-US coverage | ~7,300 | The literature gap is smaller than assumed (Europe PMC OA carries most China-affiliated instrument evidence); GSA is not INSDC and has no API; DDBJ Search is the best new census adapter; nine missing Chinese-published journals; funder APIs; name-ambiguity measurements and resolution rules; translation cost. |

Machine-readable companions: `verification.json` (per-domain summaries, recommendations, all 116
claims with the 54 verification blocks, gap summaries, critic assessment) and
`review_findings.json` (code-review findings with sceptic verdicts).

## 3. The 28 partially confirmed claims and their corrected wording

Each row: what the sweep claimed, what the independent check found, and the wording now used.

| # | Domain | Claim as written | Correction (wording now used) |
|---|---|---|---|
| 1 | ranking | Europe PMC links preprints to published versions via `commentCorrectionList` type "Preprint of" and `versionList`; 451,513 hits for `SRC:PPR AND HAS_PUBLISHED_VERSION:Y`; records carry the note "Link created based on a title-first author match". | Counts and fields reproduce. The journal side carries the reciprocal "Preprint in"; `versionList` is absent on about half of linked records; the provenance note takes at least three values (title-first-author match ~74%, "CrossRef Pre-print loader" ~23%, "another matching version" ~3%); links exist only for published articles with a PMID. |
| 2 | preprints | bioRxiv/medRxiv changed DOI prefix to 10.64898 from 2025-12-01; Crossref `prefix:10.1101` returns 0 recent results because of the cutover. | Prefix and date confirmed (first 10.64898 postings carry 2025-12-02). The zero for `prefix:10.1101` reflects Crossref's **ownership transfer of the entire ~446k legacy corpus** to owner-prefix 10.64898 / member 54368 (January 2026), not the cutover; filter by member or owner prefix, never by DOI string. |
| 3 | preprints | Europe PMC has 1,264,541 preprints, 76,331 with full text (~6%); bioRxiv full text for 2026-09-01..10-17 is zero against 19,920 all-time. | Figures reproduce, but the framing is wrong: bioRxiv full text is zero for **every month since June 2026** (last record 2026-05-29), while medRxiv and Research Square continued through September; all servers show zero for October because full text lags ~5 weeks. By design full text covers only COVID-19 and funder-supported CC preprints. |
| 4 | preprints | Europe PMC section search (`METHODS:"Xenium"` 720 / 47 preprints; `METHODS:"Chromium Single Cell"` 10,063) lets instrument detection be pushed into the query; `/{PPRid}/fullTextXML` returns JATS. | Counts reproduce and PPR ids work, but `fullTextXML` is documented for PMC ids only, and section queries match only the ~6% of preprints with full text: of 280 Xenium preprints only 67 have full text, so Methods search on preprints misses most of them. |
| 5 | preprints | Crossref preprint links are bidirectional and `relation.type` is filterable (845,229 `is-preprint-of`); bioRxiv `/pubs` returned 3 links in a week against ~1,350 new preprints. | Filter and the sample pair confirmed; `has-preprint` totals 579,198, so bidirectionality is not guaranteed. The `/pubs` interval filters on **published** date, so 3 vs 1,353 compares different events; the same week in 2025 returned 445. bioRxiv DOIs sit under member 54368 in Crossref. |
| 6 | companies | OpenAlex works filter by `authorships.institutions.type:company` (15,990 since 2026-10-01), `raw_affiliation_strings.search`, `lineage`, `ror:https://ror.org/01sxvbm95`, `corresponding_institution_ids`, `funders.id`; `grants.funder` returns 400. | `funders.id`, `lineage` and `corresponding_institution_ids` are documented; `type` and `ror` are filterable institution attributes, so the compound filters are plausible but not explicitly listed; `raw_affiliation_strings.search` is not in current docs; `grants` is removed and "errors" (exact 400 unconfirmed). **ror.org/01sxvbm95 is 10x Genomics, not Illumina** (Illumina is 05k34t975). The 15,990 count could not be re-run (budget 429). |
| 7 | companies | OpenAlex has 36,583 `type:company` institutions; the institution object carries `ror`, `lineage`, `associated_institutions[].relationship`, name alternatives, acronyms, `created_date`. | Schema confirmed (8 institution types; 5 relationship values). The count is point-in-time and could not be re-verified (429); ROR reports 36,184 company organisations, so "~36k" is the usable figure. |
| 8 | companies | In five 2026 OA JATS files only one had `<funding-source>` and `sec-type="COI-statement"`; all five expressed funding and COI as ordinary titled sections. | For the one cited file the tags exist but the funding source is an inline tag inside `<ack>` and the COI section has **no title**; the other four PMCIDs were not supplied, so "all five used titled sections" is unverifiable. Design consequence unchanged: key on section titles and on the structured tags when present. |
| 9 | legal | Springer Nature Website Terms (15 Dec 2025) §6.9 allow access only "through a normal web browser", §5.4 forbid sharing logins, §3.7.3 forbid copy/download/store of "any content, files, feeds or data". | Sections and quotes exact, but §6.9's browser language is attached to unauthorised-access/denial-of-service conduct, not a blanket ban on non-browser access, and §3.7.3 forbids copying "to make or populate a database or publication", with a fair-dealing carve-out for insubstantial parts. For a literature monitor that fills a database, the database qualifier is the operative risk. |
| 10 | legal | bioRxiv `/details` returns `license` and `jatsxml`; licence values cc_by, cc_no, cc_by_nc_nd, cc_by_nc (medRxiv also cc_by_nd); robots.txt sets Crawl-delay 7 and blocks python-requests, wget, curl and AI crawlers. | Fields confirmed. `cc_by_nd`, `cc0` and `cc0_ng` occur on bioRxiv too. The robots block is on the versioned token `python-requests/2.32.3`, not generic python-requests; medRxiv's robots.txt has Crawl-delay 7 but no agent blocks; `api.biorxiv.org` has no robots.txt. |
| 11 | researchers | OpenAlex meters usage in USD: list calls $0.10/1,000, search $1/1,000, lookups free; keyless $0.10/day **per IP** (mailto does not help); free key $1/day; 10/30/50 rps. | All prices, budgets, reset time and rps confirmed. No official page says the keyless budget is scoped "per IP" (only the live API notice does); the mailto polite pool is a retired system. Extra limits: semantic search 1 rps; >5-operator searches 1/5/25 rps; content download $10/1,000. |
| 12 | researchers | `authorships.author_position` is not a valid filter; the API lists ~200 valid filters; list responses include only the first 100 authorships. | Confirmed from the API source (230 filter params; the error lists 229 names; HTTP 400); not live-tested because the budget was exhausted. Single-work endpoint returns up to 5,000 authorships; `is_corresponding` and related filters check only the first 100. |
| 13 | aggregators | bioRxiv is registered under 10.64898 with publisher openRxiv and institution bioRxiv; `type:posted-content,prefix:10.1101` since 2026-09-17 → 0, `prefix:10.64898` → 7,269. | Counts reproduce; the 7,269 split 5,702 bioRxiv + 1,567 **medRxiv** (institution field distinguishes them); the legacy corpus was transferred to owner-prefix 10.64898 in January 2026; there is no Crossref filter on institution, so server selection is client-side. |
| 14 | aggregators | OpenAlex charges per request from a daily budget: keyless $0.10 shared per IP, key $1/day; `from_created_date`/`from_updated_date` need Member+ ($10,000/yr) or Partner. | Confirmed except "shared per IP" (undocumented); Member ($5,000/yr) also lacks the sync filters; reranked search $2/1,000. |
| 15 | aggregators | PubMed E-utilities allow 3 req/s per IP, 10 per key; ESearch retmax caps at 100,000; ESummary/EFetch 10,000 per call; datetype crdt/edat/pdat/mhda. | Rates confirmed (live 429 payload `"limit":"3"`). For `db=pubmed` **ESearch caps at 9,999 UIDs** and rejects `retstart > 9,998`; ESummary/EFetch return at most ~10,000 (9,999 observed). The 100,000 figure comes from a guide no longer updated. |
| 16 | gazetteer | Illumina completed the SomaLogic acquisition from Standard BioTools on 30 Jan 2026 for $350M cash plus earn-outs; Standard BioTools merged with SomaLogic 5 Jan 2024 and retains CyTOF/Hyperion/Biomark. | Dates and consideration confirmed (plus up to $75M milestones and royalties; Sengenics included). "Retains" is stale: Illumina bought out the earn-out for ~$30M (2026-07-28); mass cytometry is under agreement to Element Biosystems ($5.5M, announced 2026-09-30) and microfluidics to ARCHIMED (announced 2026-10-05). |
| 17 | gazetteer | Oxford Nanopore's R9.4.1 kits and flow cells were discontinued June 2023–July 2024 (phase-out from February 2023); V14 kits run only on R10.4.1 flow cells. | Compatibility confirmed from the vendor FAQ (add Flongle FLO-FLG114). Month-level dates come from peer-reviewed reports, not a vendor page; the February 2023 start is unverified; the cited vendor URL is dead. |
| 18 | gazetteer | 10x Genomics announced Atera on 18 Apr 2026 (pre-orders open, shipping 2H 2026); Xenium first shipped 8 Dec 2022; Visium HD began shipping 26 Mar 2024. | Atera and Xenium confirmed; **Atera is now shipping** (first commercial shipments announced 2026-09-29). Visium HD pre-orders opened January 2024 and shipping began at the end of March 2024; the day-level date is unverified. |
| 19 | gazetteer | Roche launched AXELIOS 1 (Sequencing by Expansion) on 29 Jun 2026, shipping library prep kits and the open-source XOOS suite. | Launch, chemistry and CMOS sensor confirmed; it is Research Use Only; Roche "will be offering" kits (none listed); XOOS is source-available under a custom field-of-use licence with two binary-only modules, not OSI open source. |
| 20 | archives | ENA `instrument_model` values are exactly "Illumina NovaSeq X Plus", "Revio", "PromethION", "Element AVITI", "UG 100", "Onso"; vendor-prefixed strings return 0. | Confirmed, but the set is not "exactly" those: "Illumina NovaSeq X" (no Plus) is a distinct value of comparable size (373,566 vs 373,878 runs), and "AVITI 24" and "Vega" exist; the `controlledVocab` endpoint is stale. Query both NovaSeq X spellings. |
| 21 | archives | Latest ENA `first_public` for NCBI-origin SRR runs was 2026-10-07 while NCBI showed 121,381 runs for 2026-10-10..17; ENA mirrors NCBI ~10 days late. | Figures reproduce, but the lag is not steady: through early October ENA published NCBI releases 1–2 days later; the SRR import **stalled on 2026-10-07** while ERR/DRR continued, so the gap was 10 days and growing. |
| 22 | archives | In SRA E-utilities `[Platform]` indexes only the family; the instrument model is not searchable in any field, only parsed from `esummary` or `runinfo`. | `[Platform]` part confirmed, but the model **is** searchable via `"instrument <model>"[Properties]` (documented by NCBI; `"instrument illumina novaseq x plus"[Properties]` → ~372k). Design impact: filter server-side instead of post-filtering `ExpXml`. |
| 23 | archives | GEO Series are indexed by platform accession in `[ACCN]`; 43 new human NovaSeq X Plus Series in the week (GPL34284, 38,049 samples); Xenium human = GPL33762, CosMx = GPL33484, Revio = GPL34382; no Visium GPL. | Confirmed, with the caveat that GPL34284 is not the only human NovaSeq X Plus platform (GPL37515 duplicates it; ten mixed-organism GPLs include human); the listed `RGPL` field is unpopulated. |
| 24 | archives | ProteomeXchange `GetDataset?action=list&outputMode=json` returns the complete list (57,301 rows across PRIDE, iProX, MassIVE, jPOST, PanoramaPublic); 191 announced in the prior 7 days, 20 on Orbitrap Astral. | Six repositories (PeptideAtlas 140 was omitted); 191 is an 8-day inclusive window (183 or 188 for strict 7 days); 20 counts exact-string Astral only (25 including multi-instrument entries). Undocumented legacy CGI; payload has invalid UTF-8 bytes. |
| 25 | journals | Nature journal feeds `nature.com/{code}.rss` (RSS 1.0/RDF) and subject feeds `/subjects/{slug}.rss` return 30 items each and served HTTP 200 to plain curl. | Journal feeds carry ~8 items (RDF); subject feeds are RSS 2.0 with 30 items; journal feeds answer 200 only on a warm edge cache and otherwise 303 to the `idp.nature.com` cookie handshake. Use a redirect-following client. |
| 26 | journals | PubMed XML for a current Nature Methods article contained 82 Affiliation elements, 40 Grant elements and a CoiStatement; 3 req/s keyless, 10 per key. | 41 `Affiliation` elements (82 counted the `AffiliationInfo` wrappers); grants, COI and rate limits confirmed. |
| 27 | journals | Springer Nature grants TDM rights "via their institutions" for non-commercial research, asks for 1 request/s, limits the TDM API to 150 requests/minute, charges for the API, treats non-subscriber requests case by case. | All quotes confirmed; the 1 req/s applies to direct platform downloads, not the API; the developer portal's rate table gives the Full Text API 200/min (500/day Basic, 20,000/day Premium, "Depends on agreement"), so 150/min is the policy page's possibly stale wording. |
| 28 | journals | Nature+ Personal costs $32.99 per 30 days, includes Nature plus 54 specialist journals **including all Nature Reviews**, content from 2017, 30 PDFs per 30 days, "For personal use only", "Not for business use". | Confirmed except "all Nature Reviews": **21 of the 27** Nature Reviews titles are included (Biodiversity, Bioengineering, Clean Technology, Computing, Electrical Engineering and Psychology are not). |

## 4. Inconsistencies the critic flagged, and how they were resolved

| Topic | Positions | Resolution used in the documents |
|---|---|---|
| bioRxiv `/details` page size | 30 (preprints sweep, cursor-stepped) vs 100 (earlier catalog and adapter docstring) | **30** (official docs and two independent reproductions); the adapter advances by the returned `count`, so code is safe, docstring to be corrected |
| Crossref polite-pool rate | 3 req/s observed (aggregators, journals) vs 10 req/s default (legal) vs 10 observed by the code review | Report the live header range **3–10/s**, plan on 3, read the header every run; historic 50/s withdrawn |
| NCBI with key | "3 req/s with key" (legal table) vs 10 | **10 req/s with a free key**, 3 without |
| Deal statuses | companies sweep flagged SomaLogic→Illumina and Akoya→Quanterix "verify"; gazetteer verified both | **SomaLogic→Illumina closed 2026-01-30; Akoya→Quanterix closed 2025-07-07** (SEC/wire sources); 10x–Scale and QIAGEN–Parse remain unverified; Standard BioTools' retained businesses are being divested |
| PR Newswire biotechnology feed | unconfirmed (companies) vs works (catalog) | **works** (live run and news-layer note) |
| ENA mirroring lag | ~10 days (archives) vs 1–2 days then a stall (fact-check) | normally 1–2 days; **stall from 2026-10-07**; design lags ENA 14 days regardless |
| Europe PMC bioRxiv full text | zero for six weeks (preprints) vs zero since June 2026 (fact-check) | **zero since June 2026** |
| OpenAlex keyless scope | "per IP" (several sweeps) vs undocumented (fact-check) | "shared behind one IP" attributed to the live API notice; docs say only "keyless budget" |
| SRA instrument-model searchability | not searchable (archives) vs `[Properties]` works (fact-check, time-series builder) | **searchable via `"instrument <model>"[Properties]`** |

## 5. Central claims still unverified

Re-measure rather than re-brief: permanence of the Europe PMC bioRxiv full-text halt; whether
bioRxiv `jatsxml` URLs are reachable from a GitHub Actions egress; bioRxiv S3 bucket size and
incremental cost; Altmetric keyless 403 (closed vs blocked IP); prompt-cache hit rate inside Message
Batches; the Europe PMC PubMed-load lag; the Crossref rate header over several days; OpenAlex
publication-date back-dating (sets the overlap window); the 37 unresolved tracked researchers.
