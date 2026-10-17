# Source catalog: what works, what doesn't, and how each source is used

Tested live on 2026-10-17 from a residential IP with an honest `lit-radar/0.1` User-Agent.
"Works" means the adapter in `litradar/sources/` pulls it without credentials unless noted.

## Papers and preprints

| Source | Status | How it is used | Notes from testing |
|---|---|---|---|
| **bioRxiv / medRxiv details API** (`api.biorxiv.org/details/{server}/{from}/{to}/{cursor}`) | works, no key | all new preprint versions in the window; abstract, category, licence, `published` DOI, `jatsxml` URL | 1,803 bioRxiv records for 2026-10-10..17; 100 per page via cursor. The `jatsxml` full-text URL on `www.biorxiv.org` returned HTTP 429 (Cloudflare error 1015) to scripted requests even at one request per 3 s, so **preprint Methods are not scraped**. The official bulk route is the requester-pays S3 bucket (`s3://biorxiv-src-monthly`, MECA archives). Europe PMC indexes bioRxiv preprints (5,426 in the window) but had full text for none of them that week. |
| **Europe PMC REST** (`/webservices/rest/search`, `/{PMCID}/fullTextXML`) | works, no key | new articles by ISSN; **section-level search** `METHODS:"NovaSeq"`, `COMP_INT:"Illumina"`, `ACK_FUND:"10x Genomics"`; open-access full text as JATS XML | 91 open-access papers named NovaSeq or Chromium in their Methods in the test week; 3,626 OA articles with full text that week overall; `ISSN:` filter and `FIRST_PDATE:[a TO b]` confirmed. Cursor paging (`cursorMark`) up to 1,000 per page. |
| **Crossref REST** (`/journals/{issn}/works?filter=from-pub-date,until-pub-date`) | works, mailto | DOI-level feed per journal the day of registration; abstracts when the publisher deposits them | Nature Methods: 8 DOIs in the window. Abstracts are JATS-flavoured XML and are stripped. Science mostly does not deposit abstracts. |
| **OpenAlex** (`/works`, filters `primary_topic.id`, `authorships.institutions.type:company`, `authorships.author.id`, `primary_location.source.issn`) | **needs a free API key** | topic feeds, company-typed institutions, tracked authors, journals by ISSN | Anonymous traffic is now metered per IP ("Insufficient budget … resets at midnight UTC"); the adapter raises a clear error and the run continues without OpenAlex. Keys are free: https://help.openalex.org/api/authentication/ and are sent as `Authorization: Bearer`. |
| **arXiv API** (`export.arxiv.org/api/query`) | works, 3 s pacing | q-bio categories (`q-bio.GN`, `QM`, `BM`, `MN`, `CB`, `TO`, `SC`) by `submittedDate` | 36 results in q-bio.GN+QM for the test week. Query parameters must be URL-encoded (parentheses and brackets). |
| **Journal RSS tables of contents** | mixed | metadata backstop; DOIs in links dedupe against ISSN pulls | Working: Nature portfolio (`nature.com/<journal>.rss`), Science family (`science.org/action/showFeed…`), Cell Press (`cell.com/<journal>/current.rss`), NEJM, Lancet, JAMA, PNAS, PLOS Biology, eLife, OUP NAR, JMD, Annals of Oncology, JCO. Dead or blocked: BMC/Genome Biology (HTML bot page), AACR (404), CSHL Genome Research (429), ASH Blood (404), OUP Clinical Chemistry (404). ISSN pulls cover all of these anyway. |
| **Nature subject feeds** (`nature.com/subjects/<slug>.rss`) | works | cross-journal topic coverage (genomics, DNA sequencing, proteomics, …) | 30 items per feed. |

## Instrument census (structured, no text mining)

| Source | Status | How it is used | Notes |
|---|---|---|---|
| **NCBI SRA via E-utilities** | works; key lifts 3→10 req/s | `esearch db=sra term=YYYY/MM/DD:YYYY/MM/DD[PDAT] usehistory=y`, then `esummary` 500 at a time; `ExpXml` carries `<Platform instrument_model="…">`, study, organism, library strategy, submitting centre | **121,381 experiments** published in the test week; one 500-record page is ~0.75 MB and ~0.4 s. `"instrument illumina novaseq x plus"[Properties]` gives instant per-model counts (1,919 that week; NovaSeq 6000: 8,314). The property string must match SRA's own model spelling in lower case ("instrument revio", not "pacbio revio"). |
| **ENA portal API** (`/ena/portal/api/search?result=read_experiment&query=first_public>=…`) | works | ERX/DRX (ENA- and DDBJ-submitted) experiments with `instrument_model`, `instrument_platform`, `first_public` | 1,807 experiments in the test week, all ERX/DRX: NCBI-mirrored SRX records do not carry a `first_public` in the window, so ENA is the complement to SRA, not a substitute. `limit=0` returns everything; `/count` endpoint for sizing. |
| GEO (GPL platform ids), PRIDE / ProteomeXchange (instrument), CELLxGENE (assay ontology) | not yet wired | next adapters to add; all have public APIs with instrument or assay fields | see `docs/DESIGN.md` |

## Company and news signals

| Source | Status | How it is used | Notes |
|---|---|---|---|
| **Google News RSS** (`news.google.com/rss/search?q=…`) | works | one query per company with `watch_news: true`; `site:` queries for trade outlets that block scripts | GenomeWeb, 360Dx, Endpoints, Fierce Biotech, The Scientist, Drug Discovery News all returned 403 to direct feed requests; their headlines arrive via `site:` queries. |
| **SEC EDGAR Atom** (`browse-edgar?action=getcompany&CIK=<ticker>&type=8-K&output=atom`) | works | 8-K filings per watched US-listed company | SEC requires a contact in the User-Agent; the HTTP client adds `mailto:` when `LITRADAR_CONTACT_EMAIL` is set. |
| PR Newswire RSS (biotechnology, medical equipment) | works | press releases matched to the company gazetteer | GlobeNewswire and Business Wire RSS returned 403. |
| STAT News, BioPharma Dive, MedTech Dive RSS | works | trade press | |
| Investor-relations RSS (Illumina, 10x, PacBio, Thermo, Bruker, Twist, Guardant, Natera, …) | blocked (403/404) | replaced by Google News + EDGAR | |
| OpenAlex `institutions.type:company` | needs key | corporate authorship | see above |
| Author e-mail domains, JATS `<fn fn-type="conflict">`, `<funding-group>`, `<ack>` | works where full text is open | competing-interest and funding mentions | parsed by `litradar/extract/jats.py` |

## Not used, and why

* **Publisher logins / personal subscriptions**: reading rights only; no automated access. See `docs/LEGAL.md`.
* **Google Scholar**: no API; automated queries violate its terms.
* **Semantic Scholar**: reserved for citation-velocity enrichment (free key); not needed for the weekly pull.
* **Altmetric / Dimensions / Scopus / Web of Science**: paid or institution-gated; optional enrichments documented in `docs/DESIGN.md`.
