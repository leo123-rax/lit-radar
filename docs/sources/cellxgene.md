# CZ CELLxGENE Discover: single-cell and spatial assays behind newly public datasets

Adapter: `litradar/sources/cellxgene.py` · records: `ArchiveRecord(archive='cellxgene')` · tests:
`tests/test_sources_cellxgene.py` · verified live 2026-10-17 from a residential IP with the
`lit-radar/0.1` User-Agent, no credentials.

## Why it is in the radar

SRA and ENA tell you which **sequencer** produced newly public reads; they almost never tell you the
single-cell or spatial **assay** (the Chromium chemistry, Visium, Slide-seq, Parse Evercode, BD Rhapsody ...).
CELLxGENE Discover is the largest curated corpus of such datasets and every dataset is annotated against the
CELLxGENE schema, so the assay is an EFO ontology term rather than free text. The adapter turns that into the
same `archive_records` rows the digest already aggregates, so the "instruments behind newly public data" table
gains a single-cell / spatial dimension: datasets per assay per week, with cell counts, mapped to the gazetteer
(10x Genomics, Parse, BD, Curio, Scale, Singleron, Honeycomb, Illumina/Fluent, Takara ...).

What it is **not**: a sequencer census (CELLxGENE does not record the instrument that read the libraries) and
not a real-time feed (datasets appear when a submitter and a CZI curator publish them, typically weeks to
months after the paper).

## API, as verified live

Official documentation: Swagger UI https://api.cellxgene.cziscience.com/curation/ui/ (OpenAPI 3.0 spec at
https://api.cellxgene.cziscience.com/curation/openapi.json, title "Chan Zuckerberg Initiative CELLxGENE
Discover API", version 1.0.0); worked examples
https://github.com/chanzuckerberg/single-cell-curation/tree/main/notebooks/curation_api/python_raw ;
portal documentation https://cellxgene.cziscience.com/docs/ . Public GET endpoints accept anonymous
requests (the spec lists the lenient bearer scheme *or* no auth). **No rate limit is published**; the shared
`Http` client paces `api.cellxgene.cziscience.com` at one request per second and a weekly run is one request.

| Call | Result on 2026-10-17 | Used for |
|---|---|---|
| `GET /curation/v1/datasets?visibility=PUBLIC` | HTTP 200, JSON array of **2,243** `dataset_index` objects, 9,797,284 bytes, 6-11 s, **no pagination** (no limit/offset parameters; unknown parameters are ignored). Spec parameters: `visibility`, `analysis=pre-analysis\|post-analysis`, `schema_version` | primary route: one request per run |
| `GET /curation/v1/collections?visibility=PUBLIC` | HTTP 200, JSON array of **401** `collection_list` objects, 3,193,904 bytes. Each has `collection_id, name, doi, published_at, revised_at, created_at, visibility, consortia, publisher_metadata, datasets[]`; the embedded dataset summaries carry `assay, organism, tissue, disease, suspension_type` but **not** `cell_count` or a dataset-level `published_at` | fallback route, step 1 |
| `GET /curation/v1/collections/{collection_id}` | HTTP 200, one collection (21,606 bytes for a 10-dataset collection) whose `datasets[]` **do** carry `cell_count, primary_cell_count, published_at, revised_at, schema_version, is_primary_data` | fallback route, step 2, capped by `max_collection_details` |
| `GET /curation/v1/openapi.json`, `/v1/swagger.json` | 404 (`application/problem+json`); the spec lives at `/curation/openapi.json` | documentation |

Fields of a `dataset_index` object, with live sample values:

```
dataset_id          "8f3cb9c4-38d5-4b9c-b309-bcccd76d0075"     dataset_version_id   "b840a9b5-0dbf-4760-969f-8bb73c19b87b"
collection_id       "384e373e-4db1-4605-812f-d9da7cfd246f"     collection_version_id "d6481c1b-..."
collection_name     "A Single-Cell and Spatial Transcriptomic Atlas Characterising ..."
collection_doi      "10.1002/mco2.71022"                       collection_doi_label "..."
title               "Brain tumour single-cell atlas (glioma cohort, 66 samples)"
assay               [{"label": "10x 3' v3", "ontology_term_id": "EFO:0009922"}]      # 1-11 entries; 23% of datasets list >1
organism            [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}] # always exactly one in the live corpus
cell_count          638930                                     primary_cell_count   638930
published_at        "2026-10-16T16:35:23+00:00"                revised_at           null | ISO-8601
spatial             null | {"is_single": true, "has_fullres": false}                 # set on 664 of 2,243 datasets
suspension_type     ["cell", "nucleus"]      schema_version "7.1.0"      visibility "PUBLIC"      tombstone false
processing_status   "SUCCESS"                mean_genes_per_cell 2238.4   is_primary_data [true]
explorer_url, assets[{filetype: H5AD, filesize, url}], citation, cell_type[], tissue[], disease[], sex[], donor_id[], ...
```

All 2,243 public datasets were `visibility=PUBLIC`, `tombstone=false`, `processing_status=SUCCESS`,
`schema_version=7.1.0`; `published_at` ranged 2020-08-26 to 2026-10-16 and was never null.

## How the adapter maps it

| ArchiveRecord field | Value | Note |
|---|---|---|
| `archive` | `cellxgene` | |
| `accession` | `dataset_id`, or `dataset_id#<EFO id>` when the dataset declares several assays | one row per (dataset, assay); the `archive_records` primary key is the accession |
| `study_accession` | `collection_id` | the digest's "studies" column counts distinct collections |
| `study_title` | `<collection name> \| <dataset title> \| <cell_count> cells` | the cell count rides along here (the model has no numeric field for it) |
| `instrument_model` | assay `label`, verbatim (`10x 3' v3`, `Visium Spatial Gene Expression V1`, `Slide-seqV2` ...) | 56 distinct labels in the corpus |
| `library_strategy` | assay `ontology_term_id` (`EFO:0009922`) | stable key; labels get edited |
| `platform` | `spatial` when the dataset's `spatial` object is set or the label names a spatial product, else `single-cell` | |
| `first_public` | `published_at[:10]` | `include_revisions=True` also selects datasets whose `revised_at` is in the window, but `first_public` stays the original date |
| `organism` | organism label(s), `; `-joined | |
| `center_name` | empty | CELLxGENE has no submitting-centre field; contact names are deliberately not stored |
| `entity_id`, `vendor`, `ticker` | from the assay -> gazetteer map below | the pipeline's gazetteer matcher runs afterwards (see wiring) |

### Assay -> gazetteer map

Keyed by EFO id (`ASSAY_ENTITY`), with label regex rules (`_LABEL_RULES`) for terms not yet listed. Every id
below exists in `config/instruments.yaml` with the same vendor and ticker (checked by a test).

| Assay labels (EFO ids) | Gazetteer id | Vendor / ticker |
|---|---|---|
| 10x 3' v1/v2/v3/v4, 10x 5' v1/v2/v3, 10x 3'/5'/plain transcription profiling, 10x scATAC-seq (EFO:0009901, 0009899, 0009922, 0022604, 0011025, 0009900, 0022605, 0030003, 0030004, 0030080, 0030007) | `txg-chromium` | 10x Genomics / TXG |
| 10x multiome (EFO:0030059) | `txg-multiome` | 10x Genomics / TXG |
| 10x gene expression flex, 10x Next GEM Flex v1, 10x GEM-X Flex v1, 10x Flex Apex (EFO:0022606, 0920087, 0920088, 0920089) | `txg-chromium-flex` | 10x Genomics / TXG |
| Visium Spatial Gene Expression V1 (EFO:0022857); label rules: `visium` -> `txg-visium`, `visium hd` -> `txg-visium-hd`, `xenium` -> `txg-xenium` | `txg-visium` / `txg-visium-hd` / `txg-xenium` | 10x Genomics / TXG |
| Parse Evercode Whole Transcriptome v2, SPLiT-seq (EFO:0022601, 0009919) | `parse-evercode` | Parse Biosciences / private |
| BD Rhapsody Whole Transcriptome Analysis, BD Rhapsody Targeted mRNA (EFO:0700003, 0700004) | `bd-rhapsody` | BD / BDX |
| Slide-seqV2, curio biosciences seeker, 3mm (EFO:0030062, 0920002) | `curio-seeker` | Curio Bioscience / private |
| ScaleBio single cell RNA sequencing (EFO:0022490) | `scale-bio` | Scale Biosciences / private |
| GEXSCOPE technology (EFO:0700011) | `singleron` | Singleron / private |
| HIVE CLX Single-Cell RNAseq Solution (EFO:0900002) | `honeycomb-hive` | Honeycomb / private |
| particle-templated instant partition sequencing = PIP-seq (EFO:0900000) | `fluent-pipseq` | Illumina / ILMN |
| Smart-seq v4, Fluidigm C1-based SMARTer library preparation (EFO:0700016, 0010058) | `takara-smart-seq` | Takara Bio / 4974.T |
| label rules only (not yet seen in the corpus): Tapestri, ICELL8, Stereo-seq, MERSCOPE, CosMx, GeoMx | `missionbio-tapestri`, `takara-icell8`, `bgi-stereo-seq`, `vizgen-merscope`, `bruker-cosmx`, `bruker-geomx` | |
| Drop-seq, inDrop, CEL-seq/CEL-seq2, sci-RNA-seq3, sci-Plex, Seq-Well, MERFISH, snmC-seq2, snm3C-seq, Patch-seq, STRT-seq, MARS-seq, Quartz-seq, SORT-seq, microwell-seq, TruDrop, SCRB-seq, DroNc-seq, mCT-seq, Smart-seq2/3, Asteria scRNA-seq kit, scATAC-seq | *(unmapped)* | academic protocols or products not in the gazetteer; the label is still stored in `instrument_model` |

Interaction with the pipeline's own matcher (`Gazetteer.match_archive_model`, run in `_store_archive`): on the
56 live labels it fires on 17, never with a different vendor than the adapter. It maps `Smart-seq`,
`Smart-seq2`, `Smart-seq3` to `takara-smart-seq` (the gazetteer's existing choice) and the two GEM-X / Next GEM
Flex labels to `txg-chromium` instead of `txg-chromium-flex`; the plain `10x 3' v3`-style labels are matched
only by the adapter. See the wiring note for the one-line priority rule.

## Live check, 2026-10-17

Three requests in total (`/datasets` twice, `/collections` once), 0 retries, 0 failures.

* Window **2026-08-18 .. 2026-10-17 (60 days)**: **27 datasets** in **14 collections**, 5,479,667 cells,
  **44 assay records** (36 `txg-chromium`, 4 `curio-seeker`, 1 `txg-chromium-flex`, 3 unmapped: CEL-seq2 x2,
  Drop-seq x1); 40 single-cell / 4 spatial rows.
* Collections whose own `published_at` falls in those 60 days: **13** (plus 28 older collections revised in
  the window; one of them gained the 27th dataset, hence 14 collections at dataset level).
* Window 2026-10-10 .. 2026-10-17 (7 days): 3 datasets, 3 collections, 1,209,552 cells.
* Assay histogram, 60 days (datasets / cells): 10x 3' v3 12 / 3,594,900 · 10x 5' v2 12 / 3,420,138 ·
  10x 3' v2 4 / 2,343,380 · curio biosciences seeker, 3mm 4 / 19,366 · 10x 5' transcription profiling 2 /
  2,066,108 · 10x 5' v1 2 / 2,066,108 · 10x 5' v3 2 / 731,625 · CEL-seq2 2 / 4,090 · 10x GEM-X Flex v1 1 /
  1,533,956 · 10x 3' v4 1 / 190,706 · 10x 3' v1 1 / 159,976 · Drop-seq 1 / 106,436.
* Assay histogram, all time (top 10 of 56 labels, datasets): 10x 3' v3 1,061 · 10x 3' v2 488 · Visium Spatial
  Gene Expression V1 350 · Slide-seqV2 310 · 10x 5' v1 153 · 10x 5' v2 134 · 10x multiome 107 · Smart-seq2 102
  · sci-RNA-seq3 81 · 10x 5' transcription profiling 45. Rows with cells for every label are produced by
  `cellxgene.assay_histogram(datasets)`.

## Caveats

* **Assay, not instrument.** The 10x rows say "a Chromium chemistry was used", not which sequencer read it.
  Combine with SRA/ENA for the sequencer side.
* **Curation lag and selection.** Only datasets a submitter chose to deposit and a curator published appear;
  weekly counts are small (3-5 datasets) and lumpy. The 60-day window is the useful cadence for trends.
* **Multi-assay datasets count once per assay** (23% of datasets list more than one, up to 11). The digest's
  "experiments" column therefore counts assay declarations, not datasets; `window_summary()` gives the
  dataset count.
* **Cell counts are per dataset** and the same cells are repeated on every assay row of a multi-assay dataset;
  sum cells over datasets (`assay_histogram` / `window_summary`), not over archive rows.
* **Revisions.** `revised_at` moves when a collection is re-published (new datasets, new schema). Default is
  first publication only; `include_revisions: true` widens the selection without changing `first_public`.
* **Payload size.** The primary route is one ~10 MB response per run. The fallback (collections list + one
  detail call per candidate collection, newest first, capped at `max_collection_details`) is only used when
  `/datasets` is unavailable or malformed.
* **Contact names** present in the collections payload are not stored anywhere.

## Configuration (shipped in `config/radar.yaml`)

```yaml
  cellxgene:
    enabled: true               # CZ CELLxGENE Discover: curated single-cell / spatial datasets with the assay (one ~10 MB request)
    include_revisions: false    # also count datasets whose latest revision landed in the window
    max_records: 0              # 0 = all assay rows in the window
    max_collection_details: 50  # fallback route only: cap on per-collection detail requests
```

## Wiring (as applied in `litradar/pipeline.py`, 2026-10-17)

`fetch_archives()` runs CELLxGENE after GEO and PRIDE exactly as below, `_store_archive()` keeps an adapter-supplied
gazetteer id, and `--sources cellxgene` works. The digest renders the rows in their own sub-table, "Single-cell and
spatial assays behind new datasets (CELLxGENE)".

```python
from .sources import cellxgene as cellxgene_src
# in fetch_archives(), after the ENA block:
        if self._enabled("cellxgene", only):
            cfg = self._src("cellxgene")
            try:
                n += self._store_archive(
                    cellxgene_src.fetch(
                        self.http, since, until,
                        include_revisions=bool(cfg.get("include_revisions", False)),
                        max_records=int(cfg.get("max_records", 0)),
                        max_collection_details=int(cfg.get("max_collection_details", 50)),
                    ),
                    "cellxgene",
                )
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"cellxgene: {exc}")
                log.exception("cellxgene failed")
# in run(): let --sources cellxgene reach fetch_archives
        if only is None or "ena" in only or "sra" in only or "cellxgene" in only:
# in _store_archive(): keep an adapter-supplied gazetteer id when there is one
            if e and not r.entity_id:
                r.entity_id, r.vendor, r.ticker = e.id, e.vendor, e.ticker
```

The `run --sources` help in `litradar/cli.py` lists `cellxgene`. The rows carry `platform` = `single-cell` /
`spatial` and `archive` = `cellxgene`, which is how the digest's separate sub-table selects them.
