# Roadmap

*Ordered by readiness, not by ambition. Each item names the research note that motivates it
(`RESEARCH.md` lists the notes) and the adapter note in `docs/sources/` where one exists. Status
words: `done` / `wired` = in the code and in the weekly run; `built` = code and offline tests exist;
`specified` = the change is written down with the exact patch; `researched` = sources probed live,
no code. Statuses were re-checked against the code on 2026-10-18.*

## Near term (next few weeks)

1. **Wire the built adapters into the weekly run** — **done 2026-10-17.** GEO, PRIDE, CELLxGENE,
   ClinicalTrials.gov, NIH RePORTER, openFDA and Semantic Scholar run by default
   (`litradar/pipeline.py`; `config/radar.yaml` `sources:`); Semantic Scholar runs after the upserts
   with a 365-day look-back; GEO rows sit in the census table labelled as series × platform rather
   than in a separate table, PRIDE and CELLxGENE have their own sub-tables. *Motivation:*
   `instrument_archives.md` (recommended weekly pipeline), `researchers.md` (grants and trials as
   enrichment layers), `docs/sources/*.md`. Status: wired.
2. **Land the review fixes that change digits.** SRA double-unescape (4.7% of records lose
   fields); ENA exclusive upper bound (last day dropped); Crossref `from-created-date` (Cell Press
   and Lancet return nothing on day-range `from-pub-date`); OpenAlex `select` without `grants`; SEC
   User-Agent in EDGAR's `Name email` form; re-ingest no longer wiping stored Methods mentions;
   `rematch` re-tagging archive rows; FTS5 keyed by rowid. *Motivation:* `review_findings.json`
   (api_contracts, robustness, performance). Status: partly done — Crossref `from-created-date`
   (`DATE_FILTERS` in `sources/crossref.py`), the SEC `<tool> <contact e-mail>` User-Agent
   (`litradar/http.py`) and FTS5 keyed by rowid (`docs/store.md`) are in; check the commit history
   for the rest before re-doing any of it.
3. **Licence gate in front of every full-text fetch.** `policy.py` exists; apply it in
   `biorxiv.attach_fulltext` (block `cc_no`), add `OPEN_ACCESS:y` to the Europe PMC ISSN pull,
   default `biorxiv.fulltext: false` until the S3 route exists, store the licence level per record,
   and publish `LEGAL.md`. *Motivation:* `legal_tos.md` §6.2, `preprints.md` §9. Status: done —
   `Pipeline.fulltext_policy` / `apply_fulltext_policy` gate every full-text fetch, the Europe PMC
   pull carries `(OPEN_ACCESS:y)`, and `docs/LEGAL.md` is published.
4. **Google News RSS to opt-in; GenomeWeb's own feed in.** Require `GOOGLE_NEWS_RSS_OPTIN=1`,
   add `when:7d` to any remaining query, show encoded links, delete the Google versions of Nature
   and Science news and the dead `site:endpts.com` query, add the GenomeWeb Arc feed and section
   feeds, GEN, Inside Precision Medicine, Labiotech, BioCentury headlines, and Bing News `site:`
   fallbacks; poll PR Newswire every 4–6 h on a second schedule. *Motivation:*
   `gap-google-news-rss-news-layer.md`. Status: done in a different shape — Google News is off by
   default through `news.google_news: false` in `radar.yaml` (not an environment variable), GDELT
   is the per-company source and the GenomeWeb Arc feed is in `config/feeds.yaml`; the sub-daily
   PR Newswire schedule and the Bing fallbacks remain open.
5. **Feed-health footer.** Per-feed rolling baseline (items in 7 days, newest-item age) and the
   statuses DOWN / BLOCKED / MOVED / STALE / DECAYED / CHANGED printed in the digest; diff
   `news.google.com/robots.txt` each run. Silent feed decay is the failure mode that kills these
   systems. *Motivation:* `gap-google-news-rss-news-layer.md` §8, `ranking_delivery.md` §2.5.
   Status: researched.
6. **Persist the database outside the 7-day Actions cache.** Checkpoint and close the store before
   saving; upload `radar.sqlite.gz` (~31 MB) as a 90-day artifact every run and restore from the
   last successful artifact on a cache miss; two-job workflow (read-only run with keys, key-less
   publish to a `digests` branch); SHA-pinned actions; inputs via environment variables; hash-pinned
   requirements; log redaction of `api_key`/`mailto`. *Motivation:*
   `gap-security-hygiene-public-repo.md`, `ranking_delivery.md` §7.1, `review_findings.json`
   (performance, security). Status: done — the database travels as the `db-latest` / `db-YYYY-MM`
   release assets (not a 90-day artifact), checkpointed and integrity-checked before upload; the
   two-job workflow, SHA-pinned actions, inputs via `env:` and URL redaction are in place
   (`docs/deploy.md`, `SECURITY.md`).
7. **Week-over-week deltas and first appearances.** A `weekly_entity_counts` table (archive
   models, vendors, literature instruments, companies) written at the end of each run; "up/down vs
   last week" in the tables; first-appearance flags gated on history. *Motivation:*
   `review_findings.json` (digest_quality), `researchers.md` §13. Status: done as
   `litradar/trends.py` (the "What changed" digest section and `litradar trends`; `docs/trends.md`),
   computed from the stored rows without a `weekly_entity_counts` table.
8. **Gazetteer precision pass.** Case-sensitive plus context terms for capitalised dictionary-word
   aliases (`Evident`, `ENrich`, `TransIT`, `Myriad`, `Recursion`, `Ginkgo`, `WuXi`, `the Broad`);
   FlowJo and Octet as their own entries; a Sequel I entry; Revio context terms; a stoplist in the
   matcher; `check-config` flagging alias collisions across the two gazetteers; the PRIDE legacy MS
   aliases (autoflex, maXis, LTQ FT, Agilent Q-TOF, SCIEX legacy, Waters Xevo); the INSDC strings
   observed at DDBJ (GeneMind FASTASeq 300 / GenoLab M / SURFSeq; Salus Pro / EVO; DNBSEQ-T1+,
   T10x4RS, E25, T7+, G400 FAST; CycloneSEQ; AxiLona AXP-100). *Motivation:*
   `instrument_gazetteer.md` §3, `review_findings.json` (gazetteer_precision), `docs/sources/pride.md`,
   `gap-ex-us-sources-funders-and-cjk-author-disambiguation.md` §5. Status: specified.
9. **Ranking rebalance.** Section-weighted company mentions (Methods ×0), tier-A articles
   guaranteed a place in the LLM shortlist, revised preprints labelled and down-weighted,
   LLM-implicated companies rendered, company names instead of ids in news headings, escaping of
   untrusted text in Markdown and HTML, verified-TLS SMTP. *Motivation:* `review_findings.json`
   (digest_quality, security). Status: specified.
10. **LLM cascade and spend cap.** Haiku 5.5 triage over all candidates (`effort: low`, Batches,
    ~$1.11/week) then Opus 5.5 deep scoring of the top ~250 with Methods excerpts (~$4.75/week);
    `MAX_WEEKLY_LLM_USD` computed before submission and reported in the digest when exceeded; a
    default model when the variable is unset. *Motivation:* `ranking_delivery.md` §5. Status:
    researched.
11. **Dashboard (opt-in).** A static site rendered from the store showing ranked items and public
    metadata only; off by default because GitHub Pages sites are public even for private
    repositories and the watchlist must not be rendered. *Motivation:* `ranking_delivery.md` §6.6.
    Status: shipped — `litradar dashboard --out site/`, built by `pages.yml` from the `db-latest`
    asset (`docs/deploy.md` §5).
12. **Monthly backfill of instrument time series** as a scheduled step after the weekly run
    (`litradar timeseries --vendor pacb --vendor nanopore --vendor ilmn --from 2019-01`); the cache
    makes the weekly cost a few dozen requests. *Motivation:* `docs/sources/timeseries.md`. Status:
    done — `weekly.yml` refreshes the archive time series for every entity after the radar run and
    before the database is compressed, so the `timeseries` table travels in the release asset.

## Mid term (next quarter)

13. **DDBJ Search API as the primary INSDC census view**, with NCBI E-utilities kept for same-day
    releases and ENA demoted to the ENA-native complement; re-run each window a week later to catch
    the 1–4-day ingestion tail; add the new platform codes (`GENEMIND`, `SALUS`, `ULTIMA`,
    `SINGULAR_GENOMICS`, `TAPESTRI`, `GENEUS_TECH`, `VELA_DIAGNOSTICS`, `COMPLETE_GENOMICS`).
    *Motivation:* `gap-ex-us-sources-funders-and-cjk-author-disambiguation.md` §3. Status: researched.
14. **China, Japan, Korea literature.** Nine Chinese-published English journals by ISSN (abstracts
    for SCLS and JGG via PubMed/Europe PMC; hLife Crossref-only); a weekly Europe PMC `METHODS:`
    sweep for every China-vendor alias (where DNBSEQ, Stereo-seq, Singleron, GenoLab, Salus and
    QNome evidence lives — 90% of DNBSEQ-T7 Methods mentions are China-affiliated); KCI Open API
    (free key, English abstracts, ORCIDs); J-STAGE for DOI discovery only plus CiNii for abstracts.
    GSA: search page and statistics JSON (the browse pages behind the cookie gate are not touched;
    `LEGAL.md` §3); ChinaXiv through the global aggregator's OAI-PMH, since the main-site OAI is
    allow-listed. *Motivation:* same note, §§1–3. Status: partly done — GSA, GSA-Human, ChinaXiv,
    KCI OAI-PMH and SciELO are wired and J-STAGE is built and opt-in (`docs/sources/china.md`,
    `asia_latam.md`); the Chinese-published English journals by ISSN, the Europe PMC `METHODS:`
    sweep and CiNii remain open.
15. **Non-US funders.** OpenAIRE Graph (registered client, 7,200 req/h) and UKRI Gateway to Research
    as live adapters; CORDIS monthly zips, Wellcome 360Giving, ANR, ARC `lief-register`
    (equipment), CIHR/NSERC sheets at low frequency; KAKEN with an app id. Instrument-purchase
    signal comparable to NIH S10 comes from GtR infrastructure grants and ARC LIEF. *Motivation:*
    same note, §1 and §3. Status: researched.
16. **Translation layer.** Non-English core-topic volume is ~3 abstracts/week, so translate only
    items that pass the entity pre-filter, in the triage stage, keeping originals; even 500/week
    costs $0.13–$5.20 depending on model. *Motivation:* same note, §6. Status: researched.
17. **Retraction, correction and withdrawal tracking.** `update_event` and `paper_status` tables;
    Crossref `is-update:true` since the last run as the alert feed plus `updated-by[]` on every
    stored DOI; Retraction Watch CSV diffed daily on `Record ID` for reasons and reinstatements;
    PubMed `"Retraction Notice"[pt]` and `"Retracted Publication"[pt] AND …[lr]`; Europe PMC
    `/status-update-search` over stored ids in batches of ≤100; bioRxiv/medRxiv `type: withdrawn`
    and version>1 diffs on Methods; a "Corrections and retractions affecting earlier digests"
    section; demotion ladder (retraction/removal/withdrawal → 0; partial ×0.3; EoC ×0.5;
    correction ×0.9 or ×0.6 when Methods/data affected); count-only author-integrity tooltip, never
    exclusion; PubPeer optional behind a key; scite not persisted. *Motivation:*
    `gap-retraction-correction-withdrawal-tracking.md` §4. Status: researched.
18. **Regulatory and reimbursement block.** Weekly De Novo database GET by decision date plus
    openFDA date-range queries, monthly reconcile against the bulk zips; CDx list rows hashed and
    joined to PMA supplements and drug labels; CMS Coverage API `whats-new/local` for MolDX LCD
    deltas with the Thursday zip as full text; quarterly PLA releases and the September/November
    CLFS determinations as first-class "new test" sources (LDTs never reach FDA databases); EUDAMED
    via the legacy JSON endpoint; clinical-lab journals and the four meeting-supplement detectors;
    event weights above papers (PMA original/De Novo IVD 40, CDx expansion 35, new MolDX LCD 30,
    ADLT 25 …). *Motivation:* `gap-fda-cms-reimbursement-and-clinical-lab-venues.md` §§9–10,
    `docs/sources/openfda.md`. Status: openFDA built; rest researched.
19. **OpenAlex backfill and snapshot; Parquet export; DuckDB.** Batched 100-DOI lookups within the
    $1/day budget for history; the CC0 snapshot for the full corpus, loaded to Parquet; a weekly
    `exports/papers_{YYYY-WW}.parquet` partitioned by month; DuckDB documented as the escape hatch
    above 5–10M rows (reads SQLite and Parquet, has its own FTS). *Motivation:*
    `ranking_delivery.md` §4.4, `researchers.md` §1. Status: partly done — `litradar backfill`
    (OpenAlex by topic and month, resumable) and `litradar export` (Parquet) exist; the CC0 snapshot
    route and the DuckDB documentation beyond `docs/SCALE.md` remain.
20. **Leading and rising researcher scores.** Senior-author output, subgroup share, preprint
    velocity, citation velocity (snapshotted `cited_by_count` per run, because `counts_by_year` is
    yearly), field-normalised impact, new grants, trials; z-scores within subgroups of ≥30 people,
    rank-based otherwise; new-entrant detection from topic leaderboards; ORCID-first resolution of
    the 37 unresolved seed names with the works-count outlier guard. *Motivation:* `researchers.md`
    §13, ex-US note §4. Status: researched.
21. **Ownership history with effective dates** so a 2021 "NanoString GeoMx" paper rolls up to BRKR
    today and NSTG at publication; `company_relation` with `valid_from`/`valid_to` seeded from the
    verified deal table (NanoString→Bruker 2024-05-06, Olink→Thermo 2024-07-10, Fluent→Illumina
    2024-07-09, Akoya→Quanterix 2025-07-07, Stilla→Bio-Rad 2025-06-30, SomaLogic→Illumina
    2026-01-30, BD Biosciences→Waters 2026-02-09). *Motivation:* `instrument_gazetteer.md` §2,
    `companies.md` §2. Status: researched.
22. **Preprint→published linking and dedupe clusters.** Crossref `relation` as primary, bioRxiv
    `/pubs` as backfill, OpenAlex merged locations, Europe PMC `commentCorrectionList`; fuzzy title
    fallback flagged, never auto-merged below 0.8. *Motivation:* `preprints.md` §6,
    `ranking_delivery.md` §3. Status: researched.
23. **Accession-mining bridge.** Parse Data Availability statements (Europe PMC
    `DATA_AVAILABILITY:` and `tmAccessionTypeList`) for GEO/SRA/PRIDE accessions and attach the
    archive's declared instrument model to paywalled papers whose Methods cannot be read; measure
    recall on Nature and Cell papers before relying on it. *Motivation:* critic's runners-up,
    `legal_tos.md` §6.3 fallback order. Status: idea, unmeasured.

## Ideas (not scheduled)

- **AI-bio model and code release feeds.** Protein design and AI-bio are two of the twelve
  subgroups and their main output channel is weights and repositories, not papers: Hugging Face
  Hub API (models by tag and date), GitHub releases for tracked organisations, Zenodo REST (new
  records by community), bio.tools. Zero coverage today. *Motivation:* critic's runners-up.
- **bioRxiv/medRxiv S3 `.meca` ingestion** behind `AWS_*` keys, off by default, as the only
  complete preprint full-text route while Europe PMC carries no bioRxiv text; measure bucket size
  and monthly egress first. *Motivation:* `preprints.md` §1 and §10.
- **Vendor publication feeds** where a vendor exposes one: Bruker Spatial Biology and Parse
  Biosciences WordPress REST `publication` endpoints (7,950 and 470 curated publications with
  product taxonomies), polled weekly with `after=<ISO date>`. *Motivation:* `instrument_archives.md` §9.
- **Key Resources Tables and Reporting Summaries** as catalog-number-level instrument evidence
  (Cell Press KRT rows in OA JATS; Nature Portfolio Reporting Summary PDFs, subject to terms).
  *Motivation:* `instrument_archives.md` §0, `instrument_gazetteer.md` recommendations.
- **Bluesky** bioRxiv subject bots and tracked accounts via Jetstream or `searchPosts` (public
  endpoint returned 403 from the probe egress; a persistent sidecar costs ~$2.19/month).
  *Motivation:* `ranking_delivery.md` §2.7, `researchers.md` §12.
- **Conference abstract supplements** (AACR, ASCO, ESMO, ASHG, AGBT, ASMS, ESHG, ISMB, AMP, ADLM,
  USCAP, ACMG) recognised in Crossref by issue and title patterns. *Motivation:* `researchers.md`
  §9, FDA/CMS note §7.
- **Patents** via Google Patents on BigQuery (1 TiB/month free) or EPO OPS once PatentsView
  returns; Lens only under a contract. *Motivation:* `researchers.md` §8, `companies.md` §1.
- **Feedback loop**: a thumbs-up/down per digest item written back to `scores` and used to re-fit
  the heuristic weights. *Motivation:* `ranking_delivery.md` §8.
- **Local "ask the corpus" UI** over the SQLite store. *Motivation:* `ranking_delivery.md` §6.7.
- **Apache-2.0 relicensing** of the code for the patent grant, plus `NOTICE` and `ACCEPTABLE_USE.md`.
  *Motivation:* `legal_tos.md` §6.6–6.7 (a licence decision for the maintainers, not a code change).
