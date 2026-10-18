# ClinicalTrials.gov (API v2)

Adapter: `litradar/sources/clinicaltrials.py` · yields `NewsItem(feed='clinicaltrials')` · tests:
`tests/test_sources_clinicaltrials.py` (offline, canned payloads).

Newly registered clinical studies are a company signal the literature does not carry for months: a
diagnostics vendor appearing as sponsor or collaborator, or a named assay (an MRD test, a liquid-biopsy
panel, a proteomics panel, a sequencing platform) appearing as an intervention. This adapter pulls every
study **first posted** in the radar window whose sponsor/collaborator, intervention or free text matches a
watched term, and turns each into a news item whose summary carries the sponsor and collaborator names
exactly as the registry spells them, so the company gazetteer (`config/companies.yaml`) can match them.

## Endpoint (verified live 2026-10-17, API version 2.0.5)

* Documentation: https://clinicaltrials.gov/data-api/api · OpenAPI spec: https://clinicaltrials.gov/api/oas/v2
  (the `.json` variant of the spec URL returns 404; the YAML one works)
* Search areas: https://clinicaltrials.gov/data-api/about-api/search-areas · field (piece) names:
  https://clinicaltrials.gov/data-api/about-api/study-data-structure
* Essie query syntax: https://clinicaltrials.gov/find-studies/constructing-complex-search-queries

```
GET https://clinicaltrials.gov/api/v2/studies
    filter.advanced = AREA[StudyFirstPostDate]RANGE[2026-10-03,2026-10-17]     # MAX for an open end
    query.spons     = Guardant OR Natera OR "Twist Bioscience"                 # sponsor / collaborator area
    query.intr      = "circulating tumor DNA" OR Signatera                     # intervention / arm area
    query.term      = ...                                                      # "other terms" (BasicSearch) area
    fields          = NCTId|BriefTitle|OfficialTitle|StudyFirstPostDate|OverallStatus|StudyType|LeadSponsorName|
                      LeadSponsorClass|CollaboratorName|CollaboratorClass|InterventionName|InterventionType|
                      Condition|Phase|EnrollmentCount|EnrollmentType|BriefSummary
    pageSize        = 200          # anything above 1,000 is coerced down to 1,000
    countTotal      = true         # totalCount only comes back with the first page
    pageToken       = <nextPageToken from the previous page>; omit on page 1
```

Response shape:

```json
{"totalCount": 387,
 "studies": [{"protocolSection": {
     "identificationModule": {"nctId": "NCT07875829", "briefTitle": "Quality of Recovery After Colorectal Surgery (Robots vs Laparoscopic)"},
     "statusModule": {"overallStatus": "RECRUITING", "studyFirstPostDateStruct": {"date": "2026-10-15"}},
     "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Hospital Central do Funchal", "class": "OTHER"}},
     "descriptionModule": {"briefSummary": "Minimally invasive techniques for colorectal surgery include ..."},
     "conditionsModule": {"conditions": ["Recovery Outcomes"]},
     "designModule": {"studyType": "INTERVENTIONAL", "phases": ["NA"], "enrollmentInfo": {"count": 110, "type": "ESTIMATED"}},
     "armsInterventionsModule": {"interventions": [{"type": "PROCEDURE", "name": "Robotic vs laparoscopic surgery"}]}}}],
 "nextPageToken": "ZVNj7o2Elu8o3lpzXcinvarumpOQJJxuYfGl2vg"}
```

Behaviour confirmed by probing:

| Probe | Result |
|---|---|
| `filter.advanced=AREA[StudyFirstPostDate]RANGE[2026-10-15,2026-10-17]` | HTTP 200, `totalCount` 387, `nextPageToken` present |
| same, `RANGE[2026-10-03,2026-10-17]` (14 days) | 1,754 studies |
| same, `RANGE[2026-10-03,MAX]` | 1,754 (open end works) |
| `pageToken=<token>` on page 2 | next studies returned, a fresh `nextPageToken`; last page has none |
| `pageToken=garbage` | HTTP **400**, plain-text body `Incorrect pageToken format` (the shared Http client returns `None`) |
| `pageSize=5000` | 1,000 studies returned (coerced) |
| `fields=NCTId\|BriefTitle\|...` (pipe-separated piece names) | only the requested modules come back |
| `query.spons=Natera` / `Guardant` (no date filter) | 77 / 28 studies overall |
| `query.spons=Guardant OR Natera` | 105 = 77 + 28, so Essie `OR` works per area |
| `query.spons="Bio-Rad Laboratories" OR "10x Genomics" OR "BD (Becton Dickinson)" OR "F. Hoffmann-La Roche"` | HTTP 200 (quoted punctuated names are fine) |
| `query.intr=Guardant360 OR Signatera OR "Guardant Reveal"`, 2026-01-01..2026-10-17 | 10 studies |

## Live test (2026-10-17, window 2026-10-03..2026-10-17, five terms in both the sponsor and the intervention area)

`new_studies(http, "2026-10-03", "2026-10-17", sponsor_terms=TERMS, intervention_terms=TERMS)` with
`TERMS = ["circulating tumor DNA", "Signatera", "Guardant", "whole genome sequencing", "Olink"]`:
**13 requests in 16.3 s** (1 sizing call, 10 per-term counts, 2 result pages), 0 retries, 0 failures, **5 studies**.

| Term | sponsor area | intervention area |
|---|---|---|
| circulating tumor DNA | 0 | 5 |
| Signatera | 0 | 0 |
| Guardant | 0 | 0 |
| whole genome sequencing | 0 | 0 |
| Olink | 0 | 0 |

The five matches, as the digest would list them (sponsor first in the summary):

| NCT | First posted | Title | Summary (head) |
|---|---|---|---|
| NCT07873086 | 2026-10-14 | MRD-Guided Aumolertinib-Based Adjuvant Therapy for Resected EGFR-Mutant NSCLC | Sponsor: Peking University People's Hospital (Other). Collaborators: Jiangsu Hansoh Pharmaceutical Group Co., Ltd. Interventions: Drug: Aumolertinib; Drug: Pemetrexed; Drug: Carboplatin. Conditions: Non-Small Cell Lung Cancer. Phase: N/A. Enrollment: 50 (estimated) … |
| NCT07878351 | 2026-10-16 | Prospective ctDNA Testing in Large B-Cell Lymphoma | Sponsor: Peking University People's Hospital (Other). Interventions: Drug: Standard-of-care first-line immunochemotherapy … Conditions: Diffuse Large B Cell Lymphoma (DLBCL); High Grade B Cell Lymphoma. Enrollment: 100 (estimated). Status: Recruiting. Type: Observational … |
| NCT07863986 | 2026-10-07 | Predictive Factors for Response to Lutetium-177 PSMA Radioligand Therapy in Patients With Metastatic Castration-Resistant Prostate Cancer | Sponsor: Lithuanian University of Health Sciences (Other). Interventions: Drug: [177Lu]Lu-PSMA-617 (Pluvicto, Novartis); Diagnostic Test: 68Ga-PSMA-11 PET/CT; …; Other: Circulating tumor DNA (ctDNA) quantification. Conditions: Prostate Cancer; … Enrollment: 70 (estimated) … |
| NCT07858253 | 2026-10-05 | CLEAR-Lung III: Circulating Tumor DNA-Guided Consolidation Chemotherapy and Immunotherapy | Sponsor: [individual investigator, name redacted here] (Other). Interventions: Drug: Consolidation Chemotherapy. Conditions: Lung Cancer. Phase: Phase 2. Enrollment: 43 (estimated). Status: Not Yet Recruiting. Type: Interventional … |
| NCT07870265 | 2026-10-13 | Detection of the BRAF V600E Mutation in Liquid Biopsy Using the CRISPR/Cas12a System | Sponsor: Masaryk Memorial Cancer Institute (Other). Interventions: Diagnostic Test: Novel CRISPR/Cas12a Electrochemical Bioassay; Diagnostic Test: Reference Standard Molecular Diagnostics (NGS and ddPCR); Procedure: Serial Peripheral Blood Sampling for Liquid Biopsy. Conditions: Unresectable Metastatic Melanoma. Phase: N/A. Enrollment: 17 (estimated) … |

Reading the numbers: in a two-week window, named vendors and assays rarely appear as *sponsor* or as an
*intervention name*; they appear in the free text (the same five terms in `query.term` returned 11 studies).
Pass `terms=` as well when recall matters more than precision, and keep the brand names in the sponsor
list for the weeks a vendor does sponsor or collaborate (28 Guardant- and 77 Natera-sponsored studies exist
in total).

## Etiquette

* The API documentation asks clients to stay around **50 requests per minute per IP**. The About API page is
  rendered client-side, so that sentence could not be re-read by script on 2026-10-17; treat the figure as the
  documented ceiling, not a measured one. `litradar/http.py` paces `clinicaltrials.gov` at **1.25 s per
  request** (~48/min) and the per-host circuit breaker applies as everywhere else.
* One request chain per (search area, chunk of 40 terms): the full company gazetteer (170 entries, 74 with
  `watch_news: true`) costs 2 sponsor chains per week plus one per 40 intervention terms, each usually a single
  page at `pageSize=200`.
* `countTotal=true` is sent on the first page only; `fields` trims each study to the pieces used.
* No key, no login; the registry's data are public domain.

## Module API

| Function | Purpose |
|---|---|
| `new_studies(http, start, end=None, sponsor_terms=None, intervention_terms=None, terms=None, chunk=40, page_size=200, max_records=2000)` | entry point: `NewsItem`s for studies first posted in `[start, end]` matching any term in the given area(s); de-duplicated by NCT id; with no terms, every study in the window |
| `search(http, start, end, term=, spons=, intr=, page_size=, max_records=, fields=)` | raw study dicts with `pageToken` paging; stops on a missing/repeated token, an empty page, a failed page or `max_records` |
| `count(http, start, end, term=, spons=, intr=)` | one cheap sizing request (`pageSize=1`, `countTotal=true`) |
| `to_news_item(study)` / `study_summary(protocol_section)` | normalisation: `id='ctgov:<NCT>'`, `url=https://clinicaltrials.gov/study/<NCT>`, `published=<StudyFirstPostDate>`, summary = `Sponsor: … (class). Collaborators: … Interventions: <Type>: <name>; … Conditions: … Phase: … Enrollment: n (estimated/actual). Status: … Type: … Summary: <brief summary, truncated to a 1,500-character total>` |
| `essie_term`, `essie_or`, `date_range_filter`, `phase_label` | query building helpers (multi-word or punctuated terms are double-quoted; `PHASE2`+`PHASE3` renders `Phase 2/3`) |

## Wiring (as applied in `litradar/pipeline.py`, 2026-10-17)

The `radar.yaml` block below is shipped (`sources.clinicaltrials`, on by default) and `Pipeline.fetch_signals()`
calls `ctgov_src.new_studies(...)`, fills `companies` from the company gazetteer and upserts the items; they render
in the digest's "Grants, trials and clearances" section together with NIH RePORTER awards and openFDA decisions
(not in `fetch_news`, as first sketched). `--sources clinicaltrials` runs the stage alone.

`config/radar.yaml`:

```yaml
sources:
  clinicaltrials:
    enabled: true
    # Essie terms matched against sponsor / collaborator names. Empty = every company with watch_news: true.
    sponsor_terms: []
    # terms matched against intervention / arm names (assays, panels, platforms)
    intervention_terms: [ctDNA, "circulating tumor DNA", "minimal residual disease", "liquid biopsy",
                         "whole genome sequencing", "whole-genome sequencing", "exome sequencing",
                         "single-cell", "spatial transcriptomics", proteomics, Olink, SomaScan, NULISA]
    # optional free-text ("other terms") area; higher recall, lower precision
    terms: []
    max_records: 2000
    page_size: 200
```

`litradar/pipeline.py` (as applied): `from .sources import clinicaltrials as ctgov_src`; `fetch_signals(since,
until, only)` calls `ctgov_src.new_studies(...)`, runs `self.companies.find(f"{it.title} {it.summary}")` on each
item to fill `it.companies`, and upserts with `self.store.upsert_news`.
