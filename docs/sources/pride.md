# PRIDE Archive: mass spectrometers behind newly public proteomics data

Adapter: `litradar/sources/pride.py` · Tests: `tests/test_sources_pride.py` · Verified live 2026-10-17.

PRIDE is the EMBL-EBI node of ProteomeXchange. Every public dataset (PXD accession) declares its
instruments as PSI-MS controlled-vocabulary terms, its experiment types, organisms and a publication
date. The adapter turns each (dataset, declared instrument) pair into an `ArchiveRecord`, so the weekly
census of sequencers (SRA, ENA) gets a proteomics counterpart: how many datasets per Orbitrap Astral,
timsTOF Pro 2, Q Exactive HF were released this week.

## Official documentation

* REST API page: https://www.ebi.ac.uk/pride/markdownpage/pridearchiverestapi
* OpenAPI 3 spec (v3): https://www.ebi.ac.uk/pride/ws/archive/v3/v3/api-docs
* Swagger UI (v3): https://www.ebi.ac.uk/pride/ws/archive/v3/webjars/swagger-ui/index.html
  (`/v3/swagger-ui/index.html` is a 404; the `webjars` path is the live one)

## Endpoints used (as verified, not as remembered)

### Search, one calendar day at a time

```
GET https://www.ebi.ac.uk/pride/ws/archive/v3/search/projects
    ?filter=publicationDate==YYYY-MM-DD
    &pageSize=100&page=N
    &sortFields=publicationDate&sortDirection=DESC
```

* Response: a JSON **array** of `ElasticPrideProject`; the hit count is in the response header
  `total_records`, not in the body. A page past the end is `[]`.
* `pageSize` is **capped at 100** server-side: 200, 500 and 1000 all returned 100 records.
* The `filter` grammar is `field==value` pairs joined by commas (AND). **Date ranges are not supported**:
  `publicationDate==[2026-09-17 TO 2026-10-17]` returns 0 hits; `publicationDate=range[a TO b]` and
  `publication_date==[a TO b]` are ignored and return the whole archive (42,133 projects). A second
  `publicationDate==` clause ANDs to 0 hits. The only date filter that works is equality on one day,
  which is exactly what the adapter uses: one request per day in the window.
* Cross-check: the per-day counts from the equality filter (2026-10-10: 8, 10-11: 1, 10-12: 5,
  10-13: 0, 10-14: 0, 10-15: 45, 10-16: 37, 10-17: 3) match the counts obtained by paging the
  whole archive sorted by `publicationDate DESC` (6 pages of 100 for the test month).
* Cost: window length in days, independent of how far back the window lies (a 2024 backfill costs 365
  requests, not a walk through everything newer). A second page is only needed on a day with more than
  100 releases; the busiest day in the test month had 53.

Sample record (fields the adapter reads; others omitted):

```json
{
  "accession": "PXD085732",
  "title": "quantmsdiann reanalysis of public single-cell, spatial, phosphoproteomics and bulk DIA datasets",
  "publicationDate": "2026-10-16",
  "submissionDate": "2026-10-16",
  "submissionType": "PARTIAL",
  "instruments": ["Orbitrap Eclipse", "timsTOF Pro", "TripleTOF 5600", "Orbitrap Astral", "ZenoTOF 8600", "timsTOF HT", "Q Exactive Plus", "Orbitrap Exploris 480", "timsTOF SCP", "Q Exactive"],
  "experimentTypes": ["Data-independent acquisition"],
  "organisms": ["Homo sapiens (human)", "Saccharomyces cerevisiae (baker's yeast)", "Escherichia coli"],
  "affiliations": ["European Molecular Biology Laboratory, European Bioinformatics Institute (EMBL-EBI)"]
}
```

Other fields present in the search record: `doi`, `keywords`, `projectTags`, `softwares`,
`quantificationMethods`, `organismsPart`, `diseases`, `labPIs`, `submitters`, `references`,
`sampleProcessingProtocol`, `dataProcessingProtocol`, `projectFileNames`, `updatedDate`, `highlights`.

### Facets: a free histogram per day

```
GET https://www.ebi.ac.uk/pride/ws/archive/v3/facet/projects?filter=publicationDate==YYYY-MM-DD&facetPageSize=100&facetPage=0
-> {"instruments": {"Orbitrap Exploris 480": 9, "Q Exactive HF": 7, ...},
    "experimentTypes": {"Bottom-up proteomics": 27, ...}, "organisms": {...}, "submissionType": {"COMPLETE": 5, "PARTIAL": 40}, ...}
```

`pride.instrument_counts(http, start, end)` sums these per day. It fetches no project records and is the
quick way to get "datasets per instrument" for any window; in the live test it agreed with the
per-record histogram on every one of the 67 instrument names.

### Single project (for reference)

`GET https://www.ebi.ac.uk/pride/ws/archive/v3/projects/{accession}` returns the full record. Here CV
terms are objects, e.g. `{"@type": "CvParam", "cvLabel": "MS", "accession": "MS:1001910", "name": "LTQ Orbitrap Elite"}`.
The adapter's `_names()` accepts both the string form (search) and the object form (detail).

### v2 fallback

`https://www.ebi.ac.uk/pride/ws/archive/v2/search/projects` is still live and behaves the same way
(same filter grammar, same 0 hits for a range), so v2 offers no date-range advantage; v3 is used.

## Mapping to `ArchiveRecord`

| ArchiveRecord field | PRIDE source |
|---|---|
| `archive` | `"pride"` |
| `accession` | `"<PXD>:<instrument slug>"`, e.g. `PXD085732:orbitrap-astral` (the store's primary key is `accession`, so multi-instrument datasets need distinct rows) |
| `study_accession` | `accession` (PXD) |
| `instrument_model` | each entry of `instruments`, as submitted ("Orbitrap Astral"); blank when none is declared (one row is still emitted so the dataset is counted) |
| `platform` | `"MS"`, or `"AFFINITY"` when `submissionType == "AFFINITY"` (Olink panels appear in `instruments`) |
| `library_strategy` | `experimentTypes` joined with `"; "` |
| `first_public` | `publicationDate` (YYYY-MM-DD) |
| `center_name` | first `affiliations` entry |
| `study_title` | `title` |
| `organism` | `organisms` joined with `"; "` |
| `entity_id`, `vendor`, `ticker` | filled by the pipeline via `Gazetteer.match_archive_model` |

## Live test (2026-09-17 .. 2026-10-17, through the adapter and the shared `Http` client)

* 31 requests, 0 retries, 16.4 s. **513 datasets, 597 instrument declarations, 67 distinct instrument
  names**; 47 datasets declare more than one instrument; none declares zero. Platform split: 593 MS, 4 AFFINITY.
* Instrument histogram (datasets per declared instrument):

| Instrument | Datasets | | Instrument | Datasets |
|---|---:|---|---|---:|
| Orbitrap Exploris 480 | 81 | | LTQ Orbitrap Elite | 15 |
| Orbitrap Fusion Lumos | 63 | | Q Exactive Plus | 12 |
| Q Exactive HF | 61 | | timsTOF SCP | 11 |
| Q Exactive | 41 | | LTQ Orbitrap | 9 |
| Orbitrap Astral | 34 | | TripleTOF 5600 | 9 |
| timsTOF Pro | 31 | | Orbitrap Exploris 240 | 8 |
| timsTOF Pro 2 | 31 | | Bruker Daltonics timsTOF series | 7 |
| Orbitrap Eclipse | 29 | | Orbitrap Ascend | 7 |
| Orbitrap Fusion | 27 | | autoflex | 7 |
| timsTOF HT | 25 | | Q Exactive HF-X | 7 |

* Experiment types: Bottom-up proteomics 304, Data-independent acquisition 155, Data-dependent
  acquisition 148, Affinity purification MS 25, Shotgun proteomics 21, Top-down 16, Gel-based 14,
  diaPASEF 14, SWATH MS 13, Immunopeptidomics 9, Cross-linking MS 8, Proteogenomics 6, Olink affinity
  proteomics 4, SRM/MRM 3.
* Gazetteer coverage with the current `config/instruments.yaml`: 552 of 597 records map to a vendor
  (Thermo Fisher Scientific 413, Bruker 113, Danaher/SCIEX 21, Waters 5). The 45 unmatched records are
  legacy or non-proteomics-flagship models: `autoflex` (7), `maXis` (4), `LTQ FT` (3), `6545XT Q-TOF LC/MS`,
  `LCQ Classic`, `ultraflex`, `LTQ`, `6520A Quadrupole Time-of-Flight LC/MS`, `4800 Proteomics Analyzer`
  (2 each), and singletons (`microflex`, `rapifleX`, `impact II`, `timsUltra AIP`, `Xevo G2-XS QTof`,
  `Q-Tof Ultima`, `nanoACQUITY UPLC`, `6495C Triple Quadrupole LC/MS`, `6410`, `6340`, `6220`, `Q TRAP`,
  `QSTAR`, `Triple Quad 6500+`, `4700 Proteomics Analyzer`, `EASY-nLC 1200`, `Vanquish`, and two
  non-instrument CV terms, `electrospray ionization` and `electron multiplier`). Suggested gazetteer
  aliases (not applied here to avoid conflicts with parallel gazetteer work): Bruker MALDI/QTOF
  (`autoflex`, `ultraflex`, `microflex`, `rapifleX`, `maXis`, `impact II`, `timsUltra AIP`), Thermo legacy
  ion traps (`LTQ`, `LTQ FT`, `LCQ`), Agilent LC/MS (`6545XT`, `6520A`, `6495C`, `6410`, `6340`, `6220`),
  SCIEX legacy (`QSTAR`, `Q TRAP`, `Triple Quad 6500+`, `4700/4800 Proteomics Analyzer`), Waters
  (`Xevo G2-XS QTof`, `Q-Tof Ultima`, `nanoACQUITY UPLC`).

## Etiquette

PRIDE publishes no numeric rate limit. The adapter goes through the shared `Http` client, which already
paces `www.ebi.ac.uk` (the host PRIDE shares with the ENA portal API, entry present in
`DEFAULT_MIN_INTERVAL`), identifies itself with the project User-Agent, retries with backoff and stops
for the run after repeated 429s. A weekly run is 7 requests; a month is ~31.

## Caveats

* `publicationDate` is the day the dataset became public, which can be years after `submissionDate`
  (e.g. submitted 2025-05-30, public 2026-10-17). It is the right field for "newly public" but not for
  "when the experiment was run".
* Instruments are self-declared from the PSI-MS CV; a few submitters pick generic terms
  (`orbitrap`, `instrument model`, `electrospray ionization`). The 524 all-time `instrument model`
  placeholders are a known artefact of the archive, not of the adapter.
* Counts are datasets, not runs or files; one dataset listing eleven instruments (a benchmark
  reanalysis) counts once for each.
* Days with zero releases are normal (PRIDE publishes in batches; 2026-10-13 and 10-14 had none).
