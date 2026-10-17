# Instrument usage time series (`litradar timeseries`)

**Question answered:** "how many PacBio (Nanopore, Illumina, ...) experiments and papers show up over
time, by model?" The command counts, per calendar month and per instrument model:

| source | what is counted | unit |
|---|---|---|
| `sra` | NCBI SRA experiments whose declared `instrument_model` is the model, by SRA publication date (`[PDAT]`) | experiments |
| `ena` | ENA `read_experiment` records with that `instrument_model`, by `first_public` | experiments |
| `epmc` | Europe PMC open-access papers whose **Methods** section names the model (any of the configured terms) | papers |

It does **not** count installed machines. Read the caveats before quoting a number.

```bash
python -m litradar timeseries --entity pacb-revio --entity pacb-sequel-ii --from 2019-01 --to 2026-10 \
    --sources sra,ena,epmc --out digests/timeseries/
python -m litradar timeseries --vendor pacb --from 2019-01            # every PacBio model
python -m litradar timeseries --vendor nanopore --vendor ilmn --dry-run  # how many requests would this take?
```

Options: `--entity` (repeatable; ids from `config/archive_models.yaml`), `--vendor` (repeatable; vendor
name, ticker or id prefix such as `pacb`, `ont`, `ilmn`, `mgi`), `--from/--to YYYY-MM` (default: the
last 36 months), `--sources` (default all three), `--out` (default `<out_dir>/timeseries`),
`--refresh-months N` (re-fetch the last N months even when cached; default 2), `--force`, `--no-chart`,
`--dry-run`.

Outputs in `--out`: `timeseries_<from>_<to>.csv` (long format `source,entity_id,model,month,count`),
`timeseries_<from>_<to>.md` (one table per source, rows = months, columns = entity/model, with totals) and,
when `matplotlib` happens to be importable, `timeseries_<from>_<to>.png` (one panel per source, lines per
model). matplotlib is never a dependency; without it the report says `"png": null`.

## Caching and politeness

Every `(source, entity_id, model, month)` count is stored in the `timeseries` table of the radar database
(`CREATE TABLE IF NOT EXISTS timeseries(...)` in `litradar/store.py`). A second run only requests months
that are missing or inside the refresh window, so a 2019-to-date backfill costs its request budget once:
live on 2026-10-17 the first Revio run made 135 requests (45 months x 3 sources), the second made 6
(the two most recent months x 3 sources) and read 129 from the cache. Failed calls are reported under
`backfill.failed` and never cached, so an older value survives and the next run retries.

Request budget: one request per model string per month per source. For all PacBio models (8 archive
strings + 6 Europe PMC term sets) over 2019-01..2026-10 (94 months) that is 8 x 94 x 2 + 6 x 94 = 2,068
requests: about 4 minutes of SRA at 3 requests/s (NCBI's limit without a key; 10/s with `NCBI_API_KEY`),
plus about 3 minutes of ENA and Europe PMC at the 0.2 s pacing already configured for `www.ebi.ac.uk`.
`--dry-run` prints the exact number before you spend it. Both hosts already have `DEFAULT_MIN_INTERVAL`
entries in `litradar/http.py`; no new host was added. Never run two NCBI loops at the same time: during
testing, a second concurrent process was enough to get HTTP 429 from E-utilities.

## Endpoints, verified live on 2026-10-17

### NCBI SRA, E-utilities `esearch`

Documentation: https://www.ncbi.nlm.nih.gov/books/NBK25499/ (esearch), https://www.ncbi.nlm.nih.gov/books/NBK25497/ (usage policy, API keys).

```
GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi
    ?db=sra&retmax=0&retmode=json
    &term=2025/09/01:2025/09/30[PDAT] AND "instrument revio"[Properties]
-> {"header": {"type": "esearch", "version": "0.3"},
    "esearchresult": {"count": "851", "retmax": "0", "retstart": "0", "idlist": [], "translationset": [], ...}}
```

* The Properties phrase is `"instrument " + <SRA instrument_model spelling in lower case>`. Spelling matters:
  `"instrument revio"` works, `"instrument pacbio revio"` does not exist.
* The match is on the **whole string**, not a prefix: `"instrument illumina novaseq x"[Properties]` returned
  18,126 for 2025-09 with or without `NOT "instrument illumina novaseq x plus"[Properties]`, and 500 of 500
  sampled `esummary` ExpXml records for that query carried exactly `instrument_model="Illumina NovaSeq X"`.
  So list every sub-model you want counted (the config does).
* The index is exhaustive: for 2025-09 the eight PacBio strings summed to 4,693, the same figure as
  `"pacbio smrt"[Platform]`.
* A string with no records returns `count: "0"` with `warninglist.quotedphrasesnotfound` (seen for
  `vega`, `aviti 24`, `ug 200`, `dnbseq-g800`, all valid vocabulary with zero deposits so far). Treated as 0.
* `[PDAT]` is the date the SRA record became public, formatted `YYYY/MM/DD:YYYY/MM/DD`, both ends inclusive.
* Pacing: 3 requests/s without an API key, 10/s with one; `Http` enforces 0.34 s / 0.11 s.

### ENA portal API, `count`

Documentation: https://www.ebi.ac.uk/ena/portal/api/ (Swagger) and
https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/advanced-search.html .

```
GET https://www.ebi.ac.uk/ena/portal/api/count
    ?result=read_experiment&format=json
    &query=first_public>=2024-01-01 AND first_public<=2024-01-31 AND instrument_model="Revio"
-> {"count":"38"}
```

* Without `format=json` the body is `text/plain` with two lines, `count` and `38`. The number is a string.
* `instrument_model="Revio"` and `instrument_model=Revio` both work (quoted form used). Values are exact and
  case-sensitive: `"PacBio Revio"` returns 0.
* `returnFields?result=read_experiment` confirms the field names `first_public`, `instrument_model`,
  `instrument_platform`.
* All-time counts on 2026-10-17 for a few models: `Illumina HiSeq 2500` 4,366,070; `MinION` 423,568;
  `Illumina NovaSeq X Plus` 373,878; `PromethION` 203,282; `DNBSEQ-T7` 203,268; `Element AVITI` 92,404;
  `PacBio RS II` 85,188; `Sequel IIe` 33,661; `UG 100` 2,887; `Onso` 448; `Vega` 0.

### Europe PMC REST `search`

Documentation: https://europepmc.org/RestfulWebService and https://europepmc.org/searchsyntax .

```
GET https://www.ebi.ac.uk/europepmc/webservices/rest/search
    ?format=json&resultType=idlist&pageSize=1
    &query=(METHODS:"Revio" OR METHODS:"Sequel II") AND (FIRST_PDATE:[2024-01-01 TO 2024-01-31])
-> {"version": "6.9", "hitCount": 117, "nextCursorMark": "...", "resultList": {"result": [{"id": "38168995", "source": "MED", "pmid": "38168995", "pmcid": "PMC11921810", ...}]}}
```

* `hitCount` counts **papers** matching any term (a paper naming Revio twenty times counts once). The
  terms of one entity are OR-ed into one query, so the `model` column of an `epmc` row is the pipe-joined
  term set (`Sequel II|Sequel IIe`).
* `METHODS:` is a section field and only exists for articles with indexed full text, in practice the
  open-access subset. A zero-hit query returns `hitCount: 0` and an empty `resultList`.
* Phrase search is token-prefix inclusive: `METHODS:"NovaSeq X"` also matches "NovaSeq X Plus", and
  `"Sequel"` would match "Sequel II", which is why the first-generation Sequel entity has no Europe PMC terms.

## Vocabulary: `config/archive_models.yaml`

SRA and ENA share one controlled vocabulary, the `instrument_model` enumerations of `SRA.common.xsd`
(https://github.com/enasequence/schema/blob/master/src/main/resources/uk/ac/ebi/ena/sra/schema/SRA.common.xsd,
fetched 2026-10-17). The relevant platforms:

| platform | instrument_model values |
|---|---|
| PacBio | `Onso`, `PacBio RS`, `PacBio RS II`, `Revio`, `Sequel`, `Sequel II`, `Sequel IIe`, `Vega` |
| Oxford Nanopore | `MinION`, `GridION`, `PromethION` (no sub-models, no Flongle) |
| Illumina | `HiSeq X Five`, `HiSeq X Ten`, `Illumina HiSeq 1000/1500/2000/2500/3000/4000`, `Illumina HiSeq X`, `Illumina iSeq 100`, `Illumina MiSeq`, `Illumina MiniSeq`, `Illumina NovaSeq 6000`, `Illumina NovaSeq X`, `Illumina NovaSeq X Plus`, `NextSeq 500/550/1000/2000`, Genome Analyzer family |
| Element | `Element AVITI`, `AVITI 24` |
| Ultima | `UG 100`, `UG 200` |
| MGI / BGI | `DNBSEQ-T7`, `DNBSEQ-G400`, `DNBSEQ-G400 FAST`, `DNBSEQ-G50`, `DNBSEQ-G800`, `DNBSEQ-T10x4RS`, `MGISEQ-2000RS`, `BGISEQ-500`, `BGISEQ-50` |
| Ion Torrent | `Ion Torrent PGM`, `Ion Torrent Proton`, `Ion Torrent S5`, `Ion Torrent S5 XL`, `Ion Torrent Genexus`, `Ion GeneStudio S5`, `Ion GeneStudio S5 Prime`, `Ion GeneStudio S5 Plus` |

Each entity in the config maps to `sra` (lower-case strings), `ena` (exact strings) and `epmc_terms`.
`tests/test_timeseries.py` checks that every `ena` value is in this vocabulary, that `sra` is its lower-case
twin, that no model string is claimed by two entities, and that every id exists in `config/instruments.yaml`
unless flagged `proposed: true`. Two legacy PacBio ids are pre-registered that way until the gazetteer gains
entries for them: `pacb-rs-ii` (PacBio RS, RS II) and `pacb-sequel` (first-generation Sequel). They work
in the time series today; `check` warns until the gazetteer catches up.

Not coverable from the archives (no `instrument_model` value): Nanopore sub-models and Flongle, Illumina
MiSeq i100, MGI DNBSEQ-G99 and T20, Singular G4, Roche SBX. Those entities carry only `epmc_terms`.
Vocabulary with no gazetteer id and therefore not in the config: Illumina MiniSeq, Ultima UG 200, Genapsys,
GeneMind, Vela Sentosa, capillary and 454 instruments.

## Caveats (read before quoting a number)

1. **Experiments and papers, not installed machines.** One instrument can produce thousands of SRA
   experiments a month (a NovaSeq X running population cohorts) or none (a clinical lab). The series tracks
   *public research output attributed to a model*, which is a lagging, biased proxy for the installed base.
2. **Deposit lag.** Data is released when the paper is accepted or the embargo lifts, typically 6 to 24
   months after sequencing. Recent months are always under-counted and keep growing for years; this is why
   the last `--refresh-months` months are re-fetched on every run. Never read the last 2 to 3 months as a
   trend break.
3. **Clinical and commercial sequencing never deposits.** Diagnostic labs, pharma, agrigenomics services
   and most direct-to-consumer and population programmes do not submit to SRA or ENA, and their papers (if
   any) rarely name instruments in open-access Methods. A vendor whose growth is clinical can be flat here
   while its consumables revenue grows.
4. **SRA and ENA are mirrors with different month allocation.** ENA mirrors SRA (and DDBJ), so totals agree
   over long windows (Revio 2023-01..2026-09: SRA 32,855 vs ENA 31,820, within 3%) but single months differ
   because SRA's `[PDAT]` and ENA's `first_public` are not the same timestamp (2024-04: SRA 10 vs ENA 138;
   2024-07: SRA 597 vs ENA 194). Compare within one source, or sum to quarters. ENA also has months where a
   model is absent although SRA has it (`Illumina NovaSeq X Plus`, 2024-01: ENA 0, SRA 158): the ENA
   vocabulary gained that value later and older mirrored records were not relabelled.
5. **SRA Properties index vs esummary.** The `[Properties]` count is instant and exact on the whole model
   string, which makes it ideal for counting but blind to anything not in the controlled vocabulary.
   The weekly census (`litradar/sources/sra.py`) reads the same `instrument_model` from `esummary` ExpXml,
   which additionally gives study, organism, library strategy and submitter; the two agree on the model
   label (500/500 in the sample) but esummary is ~0.4 s per 500 records, so only the census uses it.
   `unspecified` is a legal value on every platform and is not counted anywhere.
6. **Experiments are not runs or samples.** An SRA/ENA experiment is a library-on-instrument record; one
   sample can have several, one run can hold many. ENA's `read_run` result type counts runs instead
   (`result=read_run` on the same endpoint) if that is the quantity you need.
7. **Europe PMC counts open-access papers only**, with a tokenised phrase match that is case-insensitive
   and prefix-inclusive. Terms are chosen to be specific (`PacBio Vega`, not `Vega`; nothing for the
   first-generation Sequel), so the `epmc` series under-counts by design rather than over-counting.
8. **Illumina is grouped by family** because the gazetteer is (`ilmn-hiseq` carries nine HiSeq models). The
   CSV keeps one row per model string, so per-model series are preserved; only the Markdown column label
   groups them under the entity.

## Live run on 2026-10-17: Revio, monthly, 2023-01 to 2026-09

`python -m litradar timeseries --entity pacb-revio --from 2023-01 --to 2026-09 --sources sra,ena,epmc`
(135 requests, 0 failures; second run: 6 requests, 129 cached). Revio began shipping in early 2023, which is
visible as the first deposits in 2023-08 and the first Methods mentions in 2023-01/02 (early-access sites).

| month | SRA experiments | ENA experiments | Europe PMC papers |
|---|---:|---:|---:|
| 2023-01 | 0 | 0 | 1 |
| 2023-02 | 0 | 0 | 1 |
| 2023-03 | 0 | 0 | 0 |
| 2023-04 | 0 | 0 | 0 |
| 2023-05 | 0 | 0 | 0 |
| 2023-06 | 0 | 0 | 0 |
| 2023-07 | 0 | 0 | 0 |
| 2023-08 | 9 | 27 | 2 |
| 2023-09 | 38 | 33 | 1 |
| 2023-10 | 32 | 18 | 1 |
| 2023-11 | 94 | 95 | 1 |
| 2023-12 | 51 | 51 | 3 |
| 2024-01 | 38 | 38 | 7 |
| 2024-02 | 156 | 151 | 2 |
| 2024-03 | 54 | 163 | 5 |
| 2024-04 | 10 | 138 | 2 |
| 2024-05 | 109 | 184 | 5 |
| 2024-06 | 456 | 513 | 7 |
| 2024-07 | 597 | 194 | 15 |
| 2024-08 | 373 | 299 | 9 |
| 2024-09 | 404 | 399 | 11 |
| 2024-10 | 547 | 600 | 16 |
| 2024-11 | 369 | 346 | 23 |
| 2024-12 | 213 | 201 | 19 |
| 2025-01 | 596 | 521 | 47 |
| 2025-02 | 485 | 453 | 30 |
| 2025-03 | 631 | 704 | 35 |
| 2025-04 | 793 | 783 | 46 |
| 2025-05 | 1149 | 1099 | 43 |
| 2025-06 | 967 | 794 | 31 |
| 2025-07 | 1288 | 1348 | 49 |
| 2025-08 | 1126 | 1126 | 46 |
| 2025-09 | 851 | 797 | 51 |
| 2025-10 | 625 | 646 | 58 |
| 2025-11 | 1384 | 962 | 59 |
| 2025-12 | 1026 | 1287 | 50 |
| 2026-01 | 1087 | 1135 | 126 |
| 2026-02 | 1211 | 923 | 64 |
| 2026-03 | 3175 | 2246 | 66 |
| 2026-04 | 1091 | 1741 | 68 |
| 2026-05 | 2729 | 2580 | 84 |
| 2026-06 | 1443 | 1647 | 79 |
| 2026-07 | 1149 | 1198 | 53 |
| 2026-08 | 4418 | 4444 | 51 |
| 2026-09 | 2081 | 1936 | 41 |
| **total** | **32855** | **31820** | **1308** |

Yearly SRA experiments: 2023 = 224; 2024 = 3,326; 2025 = 10,921; 2026 (Jan-Sep) = 18,384. The last two to
three months will still grow (caveat 2); the 2026-08 spike is a handful of large cohort releases, not a
step change in the installed base (caveat 1).

## Wiring into the weekly pipeline (optional)

The command is standalone and needs no changes to `litradar/pipeline.py`. To refresh a standing series
every week, add a step after `python -m litradar run` in the scheduler, for example
`python -m litradar timeseries --vendor pacb --vendor nanopore --from 2019-01 --out digests/timeseries/`:
the cache makes the weekly cost a few dozen requests.
