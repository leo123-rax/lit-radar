# NCBI GEO (Gene Expression Omnibus) — study-level instrument census

Adapter: `litradar/sources/geo.py` → `ArchiveRecord(archive="geo")`, one record per (Series, Platform) pair.
Tests: `tests/test_geo_offline.py` (canned payloads, no network).

## Why

GEO is where most published expression, epigenomic, single-cell and spatial datasets land. Every GEO
Series (GSE) is tied to one or more Platform records (GPL) whose title names the instrument and the
organism, e.g. `Illumina NovaSeq 6000 (Homo sapiens)`, `Revio (Escherichia coli)`, `Element AVITI
(Brachypodium distachyon)`. Sweeping the Series released each week therefore gives a **study-level**
census of which machines are producing public data, complementary to the **experiment-level** SRA and
ENA sweeps (which count libraries, and where one study can contribute thousands of records).

## Official documentation

* E-utilities (esearch / esummary / einfo): https://www.ncbi.nlm.nih.gov/books/NBK25499/
* E-utilities usage policy (3 req/s without a key, 10/s with `api_key`): https://www.ncbi.nlm.nih.gov/books/NBK25497/
* GEO programmatic access overview: https://www.ncbi.nlm.nih.gov/geo/info/geo_paccess.html
* Search fields for `db=gds` (from `einfo.fcgi?db=gds`): `ETYP` entry type (DataSet / Series / Platform / Sample),
  `PDAT` publication date, `ACCN` GEO accession, `TITL` title words, `PTYP` platform technology type,
  `ORGN` organism, `NSAM` number of samples.

## Endpoints and parameters (verified live 2026-10-17)

| Step | Call | Key parameters | Returns |
|---|---|---|---|
| 1 | `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` | `db=gds`, `term=GSE[ETYP] AND 2026/10/10:2026/10/17[PDAT]`, `usehistory=y`, `retmax=0`, `retmode=json`, optional `api_key` | `esearchresult.count` (`"516"`), `webenv`, `querykey`; `querytranslation` = `GSE[ETYP] AND 2026/10/10[PDAT] : 2026/10/17[PDAT]` |
| 2 | `GET …/esummary.fcgi` | `db=gds`, `WebEnv`, `query_key`, `retstart`, `retmax=200`, `retmode=json` | `result.uids` + one DocSum per uid (GSE uid = 200000000 + series number) |
| 3 | `GET …/esummary.fcgi` | `db=gds`, `id=100024676,100016791,…` (GPL uid = 100000000 + platform number; ≤ 50 per call), `retmode=json` | one DocSum per platform |

### Series DocSum fields (step 2), as returned live

```json
{"uid": "200351130", "accession": "GSE351130", "title": "Single-cell RNA sequencing of …",
 "summary": "…", "gpl": "24247", "gse": "351130", "taxon": "Mus musculus", "entrytype": "GSE",
 "gdstype": "Expression profiling by high throughput sequencing", "ptechtype": "", "pdat": "2026/10/16",
 "suppfile": "MTX, TSV", "samples": [{"accession": "GSM10127937", "title": "…"}, …], "n_samples": 8,
 "platformtitle": "", "platformtaxa": "", "samplestaxa": "", "seriestitle": "", "pubmedids": [],
 "projects": [], "ftplink": "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE351nnn/GSE351130/", "geo2r": "no",
 "bioproject": "PRJNA1544684"}
```

* `gpl` is a bare number and is **semicolon-joined for multi-platform series** (`"21290;24676"`): 42 of the
  516 series in the test week used 2–5 platforms.
* `gdstype` is also semicolon-joined when mixed (`"Expression profiling by high throughput sequencing; Other"`).
* `platformtitle`, `platformtaxa`, `samplestaxa`, `seriestitle` were **empty on every GSE DocSum** tested, so the
  platform title requires step 3. (The brief anticipated it might be present; it is not.)
* No submitter / centre field exists in the DocSum; `center_name` is left empty.
* A 200-record page is ~0.75 MB (the per-sample list dominates); 516 series = 3 pages.

### Platform DocSum fields (step 3), as returned live

```json
{"uid": "100024676", "accession": "GPL24676", "title": "Illumina NovaSeq 6000 (Homo sapiens)",
 "taxon": "Homo sapiens", "entrytype": "GPL", "ptechtype": "high-throughput sequencing", "pdat": "2018/03/02",
 "n_samples": 1166851, "gse": "281415;340645;…(28,769 series)…", "summary": "", "gdstype": ""}
```

* `ptechtype` values seen: `high-throughput sequencing`, `in situ oligonucleotide`, `oligonucleotide beads`,
  `spotted oligonucleotide`, `RT-PCR`, `other` (Xenium, CosMx). It is stored in `ArchiveRecord.platform`.
* The `gse` back-reference lists every series on the platform: ~200 KB for popular platforms, so a 50-id batch
  is ~1.1 MB / ~0.5 s. `efetch.fcgi?db=gds` returns a 13 KB text DocSum for the same 50 ids but is unstructured
  prose, so the adapter uses esummary.
* Unknown uids come back as `{"uid": "…", "error": "cannot get document summary"}` and are skipped.

### Platform-title conventions → `instrument_model`

GEO uses the sequencer vocabulary without a vendor prefix for PacBio and Oxford Nanopore. Seen live:

| GPL title | `instrument_model` |
|---|---|
| `Illumina NovaSeq 6000 (Homo sapiens)` | `Illumina NovaSeq 6000` |
| `Illumina NovaSeq X Plus (Anser anser)` | `Illumina NovaSeq X Plus` |
| `NextSeq 2000 (Mus musculus)` | `NextSeq 2000` |
| `Sequel II (Oxytricha trifallax)`, `Revio (Escherichia coli)`, `Onso (Homo sapiens)`, `PacBio RS II (Capra hircus)` | `Sequel II`, `Revio`, `Onso`, `PacBio RS II` |
| `PromethION (Drosophila yakuba)`, `MinION (…)` | `PromethION`, `MinION` |
| `Element AVITI (Escherichia coli; Escherichia phage MS2)` | `Element AVITI` |
| `Xenium In Situ Analyzer: Danio rerio` | `Xenium In Situ Analyzer` |
| `NanoString CosMx SMI (human)` | `NanoString CosMx SMI` |
| `Agilent-021169 Arabidopsis 4 Oligo Microarray (V4) (Feature Number version)` | unchanged (array design detail) |
| `[HuGene-2_0-st] Affymetrix Human Gene 2.0 ST Array [transcript (gene) version]` | unchanged |

Rule (`geo.model_from_title`): drop a trailing ` (taxon)` / `: taxon` that equals the GPL `taxon` field; otherwise
drop a trailing parenthetical that has no digits, is not a "version" note, and is either ≥ 2 words or a single
lower-case word. The gazetteer (`Gazetteer.match_archive_model`) then maps the model string to a vendor/ticker
exactly as it does for SRA and ENA strings.

## Record mapping

| `ArchiveRecord` field | GEO source |
|---|---|
| `archive` | `"geo"` |
| `accession` | `"GSE351130/GPL24247"` — one record per (Series, Platform); a series with no platform is keyed by the GSE alone |
| `study_accession` | `GSE351130` |
| `instrument_model` | platform title minus organism (table above) |
| `platform` | GPL `ptechtype`, e.g. `high-throughput sequencing` |
| `library_strategy` | `gdstype`, e.g. `Expression profiling by high throughput sequencing` |
| `first_public` | `pdat` → `2026-10-16` |
| `study_title` / `organism` | series `title` / series `taxon` |
| `center_name` | `""` (not available in the DocSum) |

`first_public` is the GEO release date, so the window query is exact and re-runs are idempotent
(`INSERT OR IGNORE` on `accession`).

## Live test, 2026-10-17 (window 2026-10-10 .. 2026-10-17, paced at 1 req/s)

* **516 GSE series released**; 574 (series, platform) records; 138 distinct platforms, all resolved;
  8 HTTP requests, 8.1 s (3 series pages + 3 GPL batches + 2 counts).
* Release-day spread: 10/10 49 · 10/11 50 · 10/12 72 · 10/13 102 · 10/14 114 · 10/15 131 · 10/16 56 (no 10/17 yet;
  the index lagged the calendar by a day at run time, so a weekly run should include the previous day).
* **Top 10 platform titles** (records):

  | # | Platform title | n |
  |---|---|---|
  | 1 | Illumina NovaSeq 6000 (Homo sapiens) | 100 |
  | 2 | Illumina NovaSeq 6000 (Mus musculus) | 89 |
  | 3 | Illumina NovaSeq X Plus (Homo sapiens) | 43 |
  | 4 | Illumina NovaSeq X Plus (Mus musculus) | 42 |
  | 5 | Illumina NovaSeq X (Mus musculus) | 20 |
  | 6 | NextSeq 2000 (Mus musculus) | 16 |
  | 7 | NextSeq 2000 (Homo sapiens) | 14 |
  | 8 | Illumina NovaSeq X (Homo sapiens) | 12 |
  | 9 | NextSeq 550 (Homo sapiens) | 11 |
  | 10 | Illumina NovaSeq 6000 (Rattus norvegicus) | 8 |

* **Top instrument models** (organism folded): NovaSeq 6000 233 · NovaSeq X Plus 103 · NextSeq 2000 35 ·
  NovaSeq X 35 · DNBSEQ-T7 18 · Element AVITI 16 · NextSeq 550 16 · Illumina NextSeq 500 13 · HiSeq 2500 10 ·
  NextSeq 500 10. Long-read in GEO that week: MinION 4, PacBio RS II 1 (GEO is expression-centric; most
  long-read genome data goes straight to SRA/ENA, so GEO under-represents PacBio/ONT usage).
* Technology types: high-throughput sequencing 546 · in situ oligonucleotide 11 · other 8 · oligonucleotide
  beads 6 · spotted oligonucleotide 2 · RT-PCR 1. Pass `extra_term='"high-throughput sequencing"[PTYP]'`
  to skip arrays at esearch time (491 of the 516 series).
* `gdstype` mix: Expression profiling by HTS 366 · Genome binding/occupancy by HTS 57 · Other 50 · mixed types 45+.

### Per-platform counts without a sweep (`[ACCN]` check)

`GSE[ETYP] AND GPL24676[ACCN] AND 2026/10/10:2026/10/17[PDAT]` returned **100**, exactly the number of
series on GPL24676 in the sweep. So `geo.search_platforms(http, ["Revio", "Sequel", "PacBio", "Onso"])`
(→ 386 GPL records) plus `geo.series_count_by_platform(http, gpls, start, end)` gives a vendor-level
time series with a handful of esearch calls. Measured: GEO series on a PacBio-family platform released
in 2022: 157 · 2023: 48 · 2024: 68 · 2025: 84 · 2026 (to 10/17): 92. Caveats: `search_platforms` is a
title-word search (check the titles it returns before trusting a family), a series on two platforms of the
same family can be counted twice when the id list spans chunks, and GEO undercounts long-read usage
relative to SRA/ENA.

## Politeness and cost

* Same host as SRA (`eutils.ncbi.nlm.nih.gov`), already paced by `litradar/http.py` (0.34 s without
  `NCBI_API_KEY`, 0.11 s with). No new host, so no new `DEFAULT_MIN_INTERVAL` entry.
* A weekly run is ~8 requests and ~5 MB. Parallel bursts above 3 req/s draw HTTP 429 quickly (observed
  while probing); the shared client treats two consecutive 429s as a closed host for the rest of the run,
  which would also silence the SRA sweep, so never call this adapter from several threads.
* `fetch(..., gpl_cache=dict)` lets a long run or a backfill reuse platform titles across windows
  (GPL metadata is stable).

## Known gaps

* `Onso` needs a context term in the instrument gazetteer (`context_required: true`), so the bare GEO/SRA
  string `Onso` does not map to a vendor; `PacBio RS` / `PacBio RS II` have no gazetteer entry. Both are
  gazetteer additions, not adapter changes.
* Per-sample platform assignment inside multi-platform series would need one GSM DocSum per sample; not done.
* GEO has no instrument field for arrays beyond the design name; array records carry the full title.
