# Subscriptions: what to buy beyond Nature and Science, and what each adds

*Prices read from the vendors' own pages on 2026-10-17, in USD, excluding tax; "quote" means the
seller publishes no individual price. This document is about the human reader's access. Nothing in
it changes what the pipeline fetches.*

## The rule first

**No personal subscription grants text-mining rights.** Not Nature, not Science, not Nature+, not
Cell Press, not any trade outlet. Springer Nature grants TDM rights "via their institutions" for
non-commercial research and sells API access to everyone else; its website terms forbid copying,
downloading or storing "any content, files, feeds or data … to make or populate a database", and
nature.com publishes a machine-readable rights reservation (`/.well-known/tdmrep.json` →
`tdm-reservation: 1`). Elsevier's TDM licence covers "subscribers for non-commercial research
purposes" at subscribing institutions. Nature+ Personal is marked "For personal use only. Not for
business use." The pipeline therefore never logs in anywhere, never fetches a publisher HTML page, and
links out to `doi.org`. A subscription buys exactly one thing for this project: the ability to
**open the Methods section of a paywalled paper the digest has already surfaced and read it in a
browser**.

## What the free layer already covers

| Need | Free source the pipeline uses | What paying adds |
|---|---|---|
| New items within hours, per journal | publisher RSS, Crossref `from-created-date` | nothing |
| Title, DOI, authors, date | feeds, Crossref | nothing |
| Abstract | PubMed `efetch` (every title, including Cell Press, Lancet, NEJM); Crossref for AAAS/OUP/CSHL/eLife/PNAS/AACR/ASCO/ASH; Europe PMC | nothing |
| Author affiliations | PubMed, Europe PMC, Crossref (AAAS/OUP/Silverchair only) | nothing |
| Funding and competing interests | PubMed `GrantList` and `CoiStatement`; Europe PMC `ACK_FUND:` / `COMP_INT:` | the free-text paragraph in the paywalled ~70–99% of Nature/Cell/Lancet/NEJM papers, for a reader |
| Instruments and kits named in Methods | Europe PMC `fullTextXML` and `METHODS:` search for the open-access share (1% of Science and Nature Biotechnology; 85–96% of Nature Communications, Science Advances, Genome Biology, NAR, eLife, PLOS Biology) | **the one place a subscription matters, and only for a human**: paywalled Methods of Nature-portfolio, Cell Press, Science, Lancet, NEJM, JAMA, AACR, JCO and Blood papers |
| Preprints | bioRxiv/medRxiv API, Europe PMC `SRC:PPR` | nothing |
| Vendor and company news | BioPharma Dive, MedTech Dive, STAT (free tier), GEN, Inside Precision Medicine, Labiotech, BioCentury headlines, GenomeWeb's Arc feed, Nature Briefing, EDGAR 8-K | paywalled analysis: GenomeWeb Premium, STAT+, Endpoints Insider, BioCentury |

## Ranked: journals and reading access

Ranked by expected paywalled tier-A Methods sections the reader will actually open per month,
divided by price.

| Rank | Item | Annual cost | What it adds | Alternative |
|---|---|---|---|---|
| 1 | **Nature+ Personal** | $32.99 per 30 days, rolling (≈ **$401/yr**); €27.99 rest of world | One login for Nature plus 54 specialist Nature journals (55 total): Nature Methods, Nature Biotechnology, Nature Genetics, Nature Medicine, Nature Cancer, Nature Biomedical Engineering, Nature Machine Intelligence, Nature Computational Science and **21 of the 27 Nature Reviews titles** (Biodiversity, Bioengineering, Clean Technology, Computing, Electrical Engineering and Psychology are not included). Content since 2017; 30 PDF downloads per 30 days plus unlimited online reading; cancel any month. Replaces a $199 Nature print-and-online subscription once more than about three paywalled Nature-portfolio Methods sections a month are being opened. | À la carte online-only: $119 each for Nature Methods, Nature Biotechnology, Nature Medicine, Nature Cancer and most others; $99 for Nature Genetics, Nature Reviews Genetics, Nature Reviews Drug Discovery, NSMB. Three titles = $357; break-even against Nature+ is four. |
| 2 | **Cell Press: Cell** (and Cancer Cell if oncology diagnostics is a lane) | not published; quote from Elsevier customer service; personal subscriptions must be paid personally, not by an employer | Cell, Cancer Cell, Cell Systems, Molecular Cell and Cell Stem Cell are the paywalled Cell Press sources; Cell Genomics, Cell Reports, Cell Reports Methods and Cell Reports Medicine are already open. Roughly half of recent Cell research articles are CC-licensed and reachable through Europe PMC. | Wait for the backfile to open (publisher practice, 12 months; not re-verified) or read the OA half via Europe PMC. Decide only once the price is quoted and compared with Nature+. |
| 3 | **AACR: Cancer Discovery** (+ Clinical Cancer Research) | $120 online as an Active member ($315 dues, which include one complimentary digital journal, so Cancer Discovery is effectively $0 and CCR +$100) or $150 as an Affiliate member ($135 dues); 2024 order form, non-member price not published | Liquid-biopsy, MRD and ctDNA platform papers. CCR has 98% of abstracts free in Crossref, so abstracts alone go far. | PubMed abstracts plus Europe PMC (15–29% OA). |
| 4 | **NEJM** | $219 print + digital ("Special Price", 48 issues); online-only price not shown without region/category | Pivotal diagnostic-validation trials (MCED, MRD, screening). Abstracts are free and most investor-relevant content sits in the abstract and tables. | Skip; read via the PubMed abstract. |
| 5 | **Journal of Clinical Oncology** | $854 individual, print + online (publisher shop); ASCO member access not verified | Not worth it: 97% of abstracts free in Crossref; PubMed covers all. | Skip. |
| 6 | Lancet / Lancet Oncology / JAMA Oncology | not published | Same logic as NEJM. | Skip. |
| 7 | PNAS; Genome Research | $235 online-only (2020 rate page; current page 403); $85 online-only (2014 front matter; current page 429) | 85–98% free abstracts, 34–45% OA full text; both publishers historically open their backfiles after six months (not re-verified). | Skip. |
| — | Blood | via ASH international membership $400/yr (+$100 print) | Only if haematological-malignancy MRD is a core theme. | Skip. |
| — | Science (AAAS membership, typically already held) | regular 2-year membership with Science Digital billed $207 on a late-2025 receipt (≈$103.50/yr); Sigma Xi discounted rates $75 digital / $132 print; list prices by tier not published on reachable pages | Science weekly and archive. Science Translational Medicine, Science Immunology, Science Robotics and Science Signaling need a higher membership tier; Science Advances is open. | — |

## Ranked: trade press

| Rank | Title | Individual price | Paywall | Scriptable feed? | Verdict |
|---|---|---|---|---|---|
| 1 | **GenomeWeb / 360Dx Premium** | **quote** ("based on the number of users"); $95 non-refundable 90-day trial for new subscribers; non-profits may qualify for free access | hard paywall on most reporting | the robots-permitted Arc feed `/arc/outboundfeeds/rss/?outputType=xml` carries headlines and body text for matching; home-path feeds answer 403 | The only outlet reporting instrument placements, sequencer and spatial-platform pricing, lab-tools earnings detail and private tools start-ups week after week. The paid item most likely to justify itself for a tools reader; request the quote. |
| 2 | **STAT+** | **$399/yr** or $39/month; group $300 per person; 30-day refund | free articles plus paywalled "STAT+" stories; free newsletters | yes (`statnews.com/feed/`; STAT+ items are prefixed and tagged, so the digest can label them) | Best general biotech/pharma analysis at a published price. Buy if general biotech/pharma is in scope; otherwise the free feed suffices. |
| 3 | Endpoints News Insider | shown only after login (third-party reports of ~$200/yr pre-2021 could not be verified) | metered | `endpoints.news/feed/` works with a browser User-Agent; the publisher moved domain from `endpts.com` | Strong on deals, financings and pipeline; the free feed is enough for the company-news layer. |
| 4 | BioCentury | quote ("Publications + Archives", "+ BCIQ") | hard | headline feed `biocentury.com/rss/BioCentury.xml` | Enterprise-priced; not justified for a tools radar. |
| 5–12 | Fierce Biotech/Pharma/Medtech, BioPharma Dive, MedTech Dive, GEN, Inside Precision Medicine, Nature Briefing, Science news, The Scientist, Labiotech | free (registration for newsletters) | none | Dive, GEN, Inside Precision Medicine, Labiotech, Science news: yes; Fierce (Cloudflare) and The Scientist (client-rendered feed page): no | Free. GEN is the closest free substitute for GenomeWeb's tools coverage. |

## Data and analytics services

| Service | Price | Status for a commercial operator | Verdict |
|---|---|---|---|
| **Altmetric** Details Page API | commercial contract, price by quote; free research access only for "university-affiliated scientometrics researchers", non-commercial, maximum six months | the documentation conflicts on keyless use (one page says a key is required for all endpoints; the help centre allows ≤1 call/s keyless); the live probe got 403 from an egress that was also 403'd elsewhere | contract or skip; for this reader the attention signal probably does not justify a contract |
| **scite** Pro | $40/month billed yearly (self-serve API key; 2,500 MCP credits/month) | the only paid enrichment with a clear self-serve path; its terms forbid persisting and redistributing content | only if supporting/contradicting citation tallies are used in ranking; do not store its data |
| **Dimensions** (Digital Science) | institutional subscription; free only for non-commercial scientometric research by application | "not intended for bulk data or for powering dashboards or other derivative products" | not usable without an institutional contract |
| **Scopus** (Elsevier) | institutional entitlement; "private sector and commercial researchers need an API license and subscription" | an API key alone grants nothing; entitlement is tied to institutional IP range or token | not available |
| **Web of Science** (Clarivate) | Starter API free trial 50 req/day without times-cited; Expanded API "requires a paid license", institution-linked | Starter returns no abstract field | not purchasable individually; adds little over OpenAlex |
| **Lens.org** | institutional or custom contract; trial 14 days non-commercial; individual commercial-use website agreement reported at $1,000/yr (not API) | "investment decisions" named explicitly as commercial use | quote only |
| OpenAlex paid tiers | Member $5,000/yr ($20/day), Member+ $10,000/yr ($100/day, includes the daily-sync filters), Partner from $20,000/yr | the free key ($1/day) is enough for the weekly radar if calls are batched | not needed |
| Springer Nature Full Text / TDM API | "depends on agreement"; price unpublished; Basic free tier 500 requests/day covers metadata only | the lawful route to Nature-portfolio Methods text at scale for a commercial operator | budget line item if that need materialises; not a workaround |

## Decision list

Pay for, in this order and only when the trigger is met:

1. **Nature+ Personal (≈$401/yr)** once the reader opens more than about three paywalled
   Nature-portfolio Methods sections a month. It is the only product that bundles Nature Methods,
   Nature Biotechnology, Nature Genetics, Nature Medicine and Nature Cancer, which carry most of
   the instrument-level detail the free abstract layer cannot see.
2. **GenomeWeb/360Dx Premium (quote; $95 trial)** for vendor- and platform-level reporting that no
   journal and no free feed replicates.
3. **STAT+ ($399/yr)** if general biotech/pharma analysis is part of the brief.

Do not pay for: any journal whose abstracts are already 85–100% in Crossref and whose Methods will
not be read (JCO, NEJM, Lancet, JAMA, PNAS, Blood, Genome Research); Cell Press until a price is
quoted and compared with Nature+; Scopus, Web of Science, Dimensions or Lens; and any paid tier in
the belief that it unlocks text mining. None does for an individual or a commercial user.

## Open items

Cell Press, Lancet family, JAMA, current PNAS and Genome Research, NEJM online-only, ASCO-member JCO
and non-member Blood prices could not be read from the publishers' pages (403 or institutional-only
listings) and need a browser visit or a quote. Endpoints Insider shows its price only after login;
GenomeWeb and BioCentury quote per seat; Labiotech's membership page was unreachable. AAAS list
prices by membership tier were not on any reachable page.
