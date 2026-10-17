# NCBI GEO (Gene Expression Omnibus) — study-level instrument census

Adapter: `litradar/sources/geo.py` → `ArchiveRecord(archive="geo")`, one record per (Series, Platform) pair.
Tests: `tests/test_geo_offline.py` (canned payloads, no network). Run them with
`python3 -m pytest -q -p no:warnings -o addopts="" tests/test_geo_offline.py` (the repo's `pyproject.toml`
already sets `addopts = "-q"`, so without `-o addopts=""` the summary line is suppressed).

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
* Search fields for `db=gds` (from `einfo.fcgi?db=gds`, 9,121,639 records, last update 2026/10/16): `ETYP` entry
  type (DataSet / Series / Platform / Sample), `PDAT` publication date, `ACCN` GEO accession, `TITL` title words,
  `PTYP` platform technology type, `ORGN` organism, `NSAM` number of samples.
* esearch `retmax` ceiling is 10,000; larger result sets are paged with `retstart` (verified live, below).

## Endpoints and parameters (verified live 2026-10-17)

| Step | Call | Key parameters | Returns |
|---|---|---|---|
| 1 | `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` | `db=gds`, `term=GSE[ETYP] AND 2026/10/10:2026/10/17[PDAT]`, `usehistory=y`, `retmax=0`, `retmode=json`, optional `api_key` | `esearchresult.count` (`"516"`), `webenv`, `querykey`; `querytranslation` = `GSE[ETYP] AND 2026/10/10[PDAT] : 2026/10/17[PDAT]` |
| 2 | `GET …/esummary.fcgi` | `db=gds`, `WebEnv`, `query_key`, `retstart`, `retmax=200`, `retmode=json` | `result.uids` + one DocSum per uid (GSE uid = 200000000 + series number) |
| 3 | `GET …/esummary.fcgi` | `db=gds`, `id=100024676,100016791,…` (GPL uid = 100000000 + platform number; ≤ 50 per call), `retmode=json` | one DocSum per platform |
| 3b | `GET …/esearch.fcgi` (fallback, only when step 3 resolves nothing) | `db=gds`, `term=(GPL24676[ACCN] OR GPL33484[ACCN]) AND GPL[ETYP]`, `retmax=<n ids>` | `idlist` `["100024676", "100033484"]` — the server's real uids, then step 3 is retried once with them |
| 4 | `GET …/esearch.fcgi` (platform time series) | `db=gds`, `term=GSE[ETYP] AND 2025/01/01:2025/12/31[PDAT] AND (GPL24676[ACCN] OR GPL24247[ACCN])`, `retmax=10000`, then `retstart=10000` | `count "11139"`; page 1 = 10,000 uids, page 2 = 1,139 uids, no overlap, union = 11,139 |

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
* `n_samples` may come back as a string (`"1"`); the adapter coerces it.
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
* **GPL uid = 100000000 + platform number** held for all 138 platforms of the test week and for GPL33484, but it is
  an observed convention, not a documented contract. When a batch resolves nothing (all-error or empty), the
  adapter recovers the real uids with step 3b and retries once; if that also fails it logs at ERROR and the
  affected records carry an empty `instrument_model`.

### Platform-title conventions → `instrument_model`

GEO uses the sequencer vocabulary without a vendor prefix for PacBio and Oxford Nanopore. Seen live:

| GPL title | `instrument_model` |
|---|---|
| `Illumina NovaSeq 6000 (Homo sapiens)` | `Illumina NovaSeq 6000` |
| `Illumina NovaSeq X Plus (Anser anser)` | `Illumina NovaSeq X Plus` |
| `NextSeq 2000 (Mus musculus)` | `NextSeq 2000` |
| `Sequel II (Oxytricha trifallax)`, `Revio (Escherichia coli)`, `Onso (Homo sapiens)`, `PacBio RS II (Capra hircus)` | `Sequel II`, `Revio`, `Onso`, `PacBio RS II` |
| `PromethION (Drosophila yakuba)`, `MinION (…)` | `PromethION`, `MinION` |
| `Oxford Nanopore MinION (Escherichia coli K-12)` | `Oxford Nanopore MinION` (strain names carry digits) |
| `Element AVITI (Escherichia coli; Escherichia phage MS2)` | `Element AVITI` |
| `Xenium In Situ Analyzer: Danio rerio` | `Xenium In Situ Analyzer` |
| `NanoString CosMx SMI (human)`, `NanoString CosMx SMI (Human)` | `NanoString CosMx SMI` |
| `Agilent-021169 Arabidopsis 4 Oligo Microarray (V4) (Feature Number version)` | unchanged (array design detail) |
| `[HuGene-2_0-st] Affymetrix Human Gene 2.0 ST Array [transcript (gene) version]` | unchanged |

Rule (`geo.model_from_title`): drop a trailing ` (taxon)` / `: taxon` that equals the GPL `taxon` field. Otherwise
drop a trailing parenthetical unless it is an array design note — a version marker (`V4`, `v2.1`, `Version 2`), a
bare number (`2.0`), or a note containing "version", "array", "feature" or "custom" — and it is either several
words or a single alphabetic word. Single non-alphabetic tokens such as `(HG-U133A)` are kept. The gazetteer
(`Gazetteer.match_archive_model`, which ignores prose context rules for controlled vocabularies) then maps the
model string to a vendor/ticker exactly as it does for SRA and ENA strings. Checked against
`config/instruments.yaml` after merging main: `Revio`, `Onso`, `Vega`, `PacBio RS`, `PacBio RS II`, `Sequel`,
`Sequel II`, `Sequel IIe`, `Illumina NovaSeq 6000`, `Illumina NovaSeq X`, `Illumina NovaSeq X Plus`, `NextSeq 2000`,
`NextSeq 550`, `NextSeq 500`, `Illumina NextSeq 500`, `Illumina HiSeq 2500`, `PromethION`, `MinION`, `GridION`,
`Element AVITI`, `DNBSEQ-T7`, `NanoString CosMx SMI`, `Xenium In Situ Analyzer`, `Ultima Genomics UG 100`,
`Singular G4` all resolve to a vendor entry.

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

### Coverage bookkeeping

`fetch_series()` records the sweep in the module-level `geo.last_sweep` (`start`, `end`, `total`, `fetched`,
`complete`, `error`). `geo.sweep_error()` returns `""` after a complete sweep and one line otherwise:
`geo: esearch failed for …`, `geo: esearch found N series but returned no history for …`, or
`geo: partial sweep 300/516 series for …` (an esummary page never came back, e.g. retries exhausted or the eutils
host closed by the shared client after two 429s). A partial sweep is also logged at ERROR. The pipeline wiring
below appends `sweep_error()` to `Pipeline.errors`, so the digest's coverage section shows the gap instead of a
silently short week. The `max_records` cap is deliberate and is not reported as an error.

## Live test, 2026-10-17 (window 2026-10-10 .. 2026-10-17, paced by the shared `Http` client, no API key)

* **516 GSE series released**; 574 (series, platform) records; 138 distinct platforms, all resolved;
  `sweep_error() == ""`, `last_sweep = {total 516, fetched 516, complete True}`; 9 HTTP requests, 4.2 s
  (1 esearch + 3 series pages + 3 GPL batches + 1 count, plus the esearch for the summary).
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
  (the `sequencing_only` config switch) to skip arrays at esearch time (491 of the 516 series).
* `gdstype` mix: Expression profiling by HTS 366 · Genome binding/occupancy by HTS 57 · Other 50 · mixed types 45+.

### Per-platform counts without a sweep (`[ACCN]` check, de-duplicated)

`GSE[ETYP] AND GPL24676[ACCN] AND 2026/10/10:2026/10/17[PDAT]` returned **100**, exactly the number of
series on GPL24676 in the sweep. `geo.search_platforms(http, ["Revio", "Sequel", "PacBio", "Onso", "Vega"])`
returned 386 GPL records (the PacBio family; `Vega` matched none). `geo.series_count_by_platform` ORs the
accessions in chunks of 100, pages each chunk's uids with `retstart` (step 4) and **unions the uids**, so a
series on two platforms of the same family is counted once (the first implementation summed per-chunk counts
and over-counted by 3–6 series per year).

Measured 2026-10-17, GEO Series released on a PacBio-family platform (distinct series; the summed figure from
the first implementation in parentheses):

| Release year | Series |
|---|---|
| 2022 | 154 (157) |
| 2023 | 46 (48) |
| 2024 | 66 (68) |
| 2025 | 83 (84) |
| 2026 to 10/17 | 86 (92) |

The 2022 figure is a batch artefact, not a usage trend: 104 of the 154 series were released on a single day,
2022-12-04, all titled `long read RNA-seq from <tissue or cell line>` on `Sequel II (Mus musculus)` (GPL29177, 57)
and `Sequel II (Homo sapiens)` (GPL28352, 40), and all citing the same PubMed id (22955616) — one consortium's
long-read RNA-seq release. Excluding that day, 2022 had ~50 PacBio-family series, in line with 2023–2024.
Read GEO platform time series with this in mind: a consortium can release a hundred Series at once, so look at
release-day spread (or count distinct BioProjects / PubMed ids) before calling a jump a trend.

Caveats: `search_platforms` is a title-word search (check the titles it returns before trusting a family), and
GEO undercounts long-read usage relative to SRA/ENA, which remain the primary census for a "machines in use
over time" question; GEO is the study-level complement.

## Politeness and cost

* Same host as SRA (`eutils.ncbi.nlm.nih.gov`), already paced by `litradar/http.py` (0.34 s without
  `NCBI_API_KEY`, 0.11 s with). No new host, so no new `DEFAULT_MIN_INTERVAL` entry.
* A weekly run is ~8 requests and ~5 MB. The GPL fallback adds at most two requests per failed batch. A
  five-year platform time series for a 386-platform family is 4 chunks × 5 windows = 20 esearch calls (~120 KB each
  when a chunk returns 10,000 uids).
* Parallel bursts above 3 req/s draw HTTP 429 quickly (observed while probing); the shared client treats two
  consecutive 429s as a closed host for the rest of the run, which would also silence the SRA sweep, so never
  call this adapter from several threads.
* `fetch(..., gpl_cache=dict)` lets a long run or a backfill reuse platform titles across windows
  (GPL metadata is stable).

## Wiring (for the maintainer; nothing below is in this branch)

1. `litradar/pipeline.py` — import next to the other source imports:
   ```python
   from .sources import geo as geo_src
   ```
2. `litradar/pipeline.py`, `Pipeline.fetch_archives()` — after the ENA block, before `return n`:
   ```python
   if self._enabled("geo", only):
       cfg = self._src("geo")
       try:
           extra = '"high-throughput sequencing"[PTYP]' if cfg.get("sequencing_only", True) else ""
           n += self._store_archive(
               geo_src.fetch(self.http, since, until, api_key=self.settings.ncbi_api_key,
                             max_records=int(cfg.get("max_records", 20000)), extra_term=extra),
               "geo",
           )
           if geo_src.sweep_error():
               self.errors.append(geo_src.sweep_error())
       except Exception as exc:  # noqa: BLE001
           self.errors.append(f"geo: {exc}")
           log.exception("geo failed")
   ```
   (`_store_archive` already runs `Gazetteer.match_archive_model` on `instrument_model` and records `counts["archive.geo_new"]`.)
3. `litradar/pipeline.py`, `Pipeline.run()` — widen the archive gate so `--sources geo` works:
   ```python
   if only is None or any(s in only for s in ("ena", "sra", "geo")):
       self.fetch_archives(since, until, only=only)
   ```
4. `config/radar.yaml`, under `sources:` next to `sra` / `ena`:
   ```yaml
   geo:
     enabled: true           # NCBI GEO: newly released Series (GSE) and the platform (GPL) each ran on; study-level census
     max_records: 20000      # a week is ~500 series = 3 esummary pages + ~3 platform lookups (~8 requests, ~5 MB)
     sequencing_only: true   # adds "high-throughput sequencing"[PTYP] to the esearch; set false to include microarray / in-situ series
   ```
5. `docs/SOURCES.md` — replace the `GEO (GPL platform ids) … not yet wired` cell with:
   ```
   | **NCBI GEO via E-utilities** (`esearch db=gds GSE[ETYP] AND <PDAT range>` -> paged `esummary` -> batched GPL `esummary`) | works; key lifts 3->10 req/s | one record per Series x Platform with the platform title folded to an instrument model, technology type, gdstype, release date; `sweep_error()` reports partial weeks | 516 series / 574 records / 138 platforms in the test week, 9 requests. GSE DocSums carry no platform title, so GPL lookups are required. See `docs/sources/geo.md`. |
   ```
6. Platform time series (no pipeline change needed), e.g. in a notebook or a future subcommand:
   ```python
   from litradar.sources import geo
   gpls = geo.search_platforms(http, ["Revio", "Sequel", "PacBio", "Onso", "Vega"])   # GPL accessions for a vendor family
   rows = geo.series_counts_by_window(http, gpls, [(f"{y}-01-01", f"{y}-12-31") for y in range(2022, 2027)])
   titles = geo.platform_titles(http, ["GPL24676", 16791])                            # batch GPL id -> title
   ```
   Pass one dict as `gpl_cache=` to `geo.fetch()` across windows in a backfill so platform titles are fetched once.
7. **Keep the digest's experiment-level numbers experiment-level.** GEO rows are (Series, Platform) pairs, i.e.
   studies, and with `sequencing_only: false` they include microarray / in-situ platforms. `Store.archive_instrument_counts`
   and `Store.archive_strategy_counts` currently group every archive together, and `render.py` prints
   `sum(experiments)` as "new sequencing experiments in public archives" and derives vendor shares from the same rows.
   In `litradar/store.py` (add `Sequence` to the `typing` import if it is not there yet):
   ```python
   def archive_instrument_counts(self, since: str, until: str, archives: Sequence[str] = ("sra", "ena")) -> List[Dict[str, Any]]:
       marks = ",".join("?" * len(archives))
       cur = self.conn.execute(
           f"""SELECT instrument_model, platform, vendor, ticker, COUNT(*) AS experiments, COUNT(DISTINCT study_accession) AS studies,
                      GROUP_CONCAT(DISTINCT archive) AS archives
               FROM archive_records WHERE first_public >= ? AND first_public <= ? AND archive IN ({marks})
               GROUP BY instrument_model ORDER BY experiments DESC""",
           (since, until, *archives),
       )
       return _rows(cur)

   def archive_strategy_counts(self, since: str, until: str, limit: int = 15, archives: Sequence[str] = ("sra", "ena")) -> List[Dict[str, Any]]:
       marks = ",".join("?" * len(archives))
       cur = self.conn.execute(
           f"""SELECT library_strategy, COUNT(*) AS experiments FROM archive_records
               WHERE first_public >= ? AND first_public <= ? AND archive IN ({marks}) GROUP BY library_strategy ORDER BY experiments DESC LIMIT ?""",
           (since, until, *archives, limit),
       )
       return _rows(cur)

   def geo_platform_counts(self, since: str, until: str, limit: int = 30) -> List[Dict[str, Any]]:
       """GEO Series released in the window per platform (study-level; one Series on two platforms counts for both)."""
       cur = self.conn.execute(
           """SELECT instrument_model, platform, vendor, ticker, COUNT(DISTINCT study_accession) AS series, COUNT(*) AS records
              FROM archive_records WHERE archive='geo' AND first_public >= ? AND first_public <= ?
              GROUP BY instrument_model ORDER BY series DESC LIMIT ?""",
           (since, until, limit),
       )
       return _rows(cur)
   ```
8. `litradar/digest/render.py` — after the "Library strategies" line of the archive section, a separate GEO table:
   ```python
   geo_rows = store.geo_platform_counts(since, until, limit=int(dcfg.get("archive_table_n", 30)))
   if geo_rows:
       md.append("### Platforms behind newly released GEO Series\n")
       md.append("Study-level census from NCBI GEO: one row per platform, counting the Series (GSE) released in the window that ran on it. These are studies, not experiments, so they are kept out of the SRA/ENA totals and vendor shares above.\n")
       md.append(_table(
           ["Platform (instrument model)", "Technology", "Vendor", "Ticker", "Series"],
           [[r["instrument_model"] or "(unresolved)", r.get("platform") or "", r.get("vendor") or "", r.get("ticker") or "", r["series"]] for r in geo_rows],
       ))
   ```
   Run after wiring: `python3 -m pytest -q -p no:warnings -o addopts="" && python3 -m ruff check litradar tests`.

## Known gaps

* Per-sample platform assignment inside multi-platform series would need one GSM DocSum per sample; not done.
* GEO has no instrument field for arrays beyond the design name; array records carry the full title.
* The GPL uid convention (step 3) is undocumented; the step 3b fallback covers a change, at the cost of two extra
  requests per batch, and logs at ERROR if even that fails.
* Vendor/ticker annotation depends on `config/instruments.yaml`; the strings listed under "Platform-title
  conventions" were verified against the gazetteer on main as of 2026-10-17 — re-check after large gazetteer edits.
