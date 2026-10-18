# Instrument usage time series (`litradar timeseries`)

**Question answered:** "how many PacBio (Nanopore, Illumina, ...) experiments and papers show up over
time, by model?" The command counts, per calendar month and per instrument model:

| source | what is counted | unit |
|---|---|---|
| `sra` | NCBI SRA experiments whose declared `instrument_model` is the model, by SRA publication date (`[PDAT]`) | experiments |
| `ena` | ENA `read_experiment` **rows** with that `instrument_model`, by `first_public`. The endpoint returns one row per experiment-run pair, so this is a count of **runs**, not experiments (see caveat 6) | runs |
| `epmc` | Europe PMC open-access papers whose **Methods** section names the model (any of the configured terms) | papers |

The unit is written into every output: a `unit` column in the CSV and the unit in each Markdown table
header. It does **not** count installed machines. Read the caveats before quoting a number.

```bash
python -m litradar timeseries --entity pacb-revio --entity pacb-sequel-ii --from 2019-01 --to 2026-10 \
    --sources sra,ena,epmc --out digests/timeseries/
python -m litradar timeseries --vendor pacb --from 2019-01            # every PacBio model
python -m litradar timeseries --vendor nanopore --vendor ilmn --dry-run  # how many requests would this take?
```

Options: `--all` (every entity in `config/archive_models.yaml`; what the weekly workflow runs), `--entity`
(repeatable; ids from `config/archive_models.yaml`), `--vendor` (repeatable; vendor name, ticker or id prefix
such as `pacb`, `ont`, `ilmn`, `mgi`), `--from/--to YYYY-MM` (default: the last 36 months), `--sources`
(default all three), `--out` (default `<out_dir>/timeseries`), `--refresh-months N` (re-fetch the current
month and the N months before it even when cached; default 2, so on 2026-10-17 months >= 2026-08 are always
re-fetched), `--force` (re-fetch and overwrite everything), `--no-chart`, `--dry-run`, `--json` (the whole
report as JSON; without it the command prints a few summary lines: selection, request arithmetic per source,
what the cache folding did, the output paths, then every warning and error verbatim).

```
$ litradar timeseries --all --from 2023-01 --to 2026-10 --refresh-months 2 --no-chart
timeseries: 27 entities, 74 series, 46 months (2023-01..2026-10), sources sra,ena,epmc
backfill: made 222 requests; 3182 cached, 0 pre-launch zeros, 0 failed, 0 kept; refresh cutoff 2026-08 (sra 72 requests / 1032 cached, ena 72 requests / 1032 cached, epmc 78 requests / 1118 cached)
cache layout: folded 3036 per-string rows into 736 entity rows (16 entity/source pairs; 0 months left to the backfill)
outputs: digests/timeseries/timeseries_2023-01_2026-10_all.csv (3404 rows), digests/timeseries/timeseries_2023-01_2026-10_all.md
```
(the run of 2026-10-18 against a cache that the earlier per-string version had filled; see *One series per
entity* below for what "folded" means).

`--from` / `--to` are checked against the current month: a `--to` in the future is clamped to the current
month with a warning, a `--from` in the future is an error before any request is spent (future months
cannot have deposits or papers).

Outputs in `--out`, named `timeseries_<from>_<to>_<selection>.*` where `<selection>` is the `--vendor` /
`--entity` tokens joined with `+` (`timeseries_2019-01_2026-09_pacb.csv`; a long selection is shortened to
its first token plus a hash of the resolved ids), so runs for different vendors into one directory keep
their files; a run that re-creates its own files says so in `warnings`:

* `.csv`: long format `source,entity_id,model,month,count,unit` (`unit` = `experiments`, `runs` or `papers`;
  `model` = the entity's archive strings or Europe PMC terms pipe-joined, one series per entity and source);
* `.md`: one table per source in the `--sources` order, rows = months, columns = the planned series in
  config order labelled with the gazetteer name and the archive string or term set
  (`Revio (revio)`, `Sequel II (Sequel II, Sequel IIe)`), numbers right-aligned with thousands separators,
  totals per column. A blank cell was never fetched; `fail` marks a request that failed in this run, so a
  failed month is never read as zero deposits;
* `.png`, when `matplotlib` happens to be importable (one panel per source, lines per model). matplotlib
  is never a dependency; without it the report says `"png": null`.

## Failures are named, and decide the exit code

Every failed count request is listed under `backfill.failures` with its key and the reason: the HTTP status
and the start of the body (`HTTP 400: {"error":"API key invalid"}`), `no answer (network failure, retries
exhausted or host rate-limited)`, `non-JSON body`, `esearch errorlist ...`, `Properties clause not
evaluated`, `unexpected payload`. `backfill.failed` keeps the bare keys. Nothing failed is cached, so an
older value survives and the next run retries. The run's verdict follows per source:

* every request of a source failed -> an entry in `errors` (`sra: all 24 count request(s) of this run
  failed: HTTP 400: ... (x24). Nothing cached for them; fix the cause and re-run`) and **exit code 1**;
* some requests failed -> an entry in `warnings` with the same reasons and exit code 0; the cells are `fail`.

`backfill.cutoff` is the first month inside the refresh window (`today` minus `--refresh-months` calendar
months, `2026-08` on 2026-10-17 with the default 2): months at or after it are fetched again on every run
because their counts are still growing; older cached months are not requested.

`NCBI_API_KEY` is sent only when it looks like a key (a plain alphanumeric token); a `.env` comment loaded as
the value used to earn HTTP 400 on every SRA request.

### Launch floors

An entity may carry `launch: YYYY-MM` (the announcement month) in `config/archive_models.yaml`. Months
before it are stored as 0 **without a request** and counted under `backfill.pre_launch`: nothing can deposit
from or name an instrument that did not exist, and Europe PMC phrase matches before a launch were only ever
false positives (the `Vega` and `Revio` cases below). The floor is the announcement month, which precedes
shipping, and is only set where the live audit showed a clear gap (Revio and Onso 2022-10, Vega 2024-11,
NovaSeq X 2022-09, AVITI 2022-03, AVITI 24 2024-01, UG 100 2022-05, MiSeq i100 2024-10); Sequel II has none
because early-access deposits reached SRA in 2019-03, before its April 2019 launch.

## Caching and politeness

Every `(source, entity_id, model, month)` count is stored in the `timeseries` table of the radar database
(`CREATE TABLE IF NOT EXISTS timeseries(...)` in `litradar/store.py`). A second run only requests months
that are missing or inside the refresh window, so a 2019-to-date backfill costs its request budget once:
live on 2026-10-17 the first Revio run made 135 requests (45 months x 3 sources), the second made 6
(2026-08 and 2026-09, the two months of the range inside the refresh window, x 3 sources) and read 129
from the cache. Failed calls are reported under `backfill.failed` and never cached, so an older value
survives and the next run retries.

Three safety rules protect the cache:

* **Every count is committed on its own.** A Ctrl-C, a crash or an exception half-way through a
  2,000-request backfill keeps everything fetched so far (verified live: a run interrupted with SIGINT after
  8 s left its completed months readable from a separate connection, and the next run asked only for the
  rest).
* **A fresh 0 never overwrites a positive cached count** (unless `--force`). Public deposits are not
  withdrawn wholesale, so a month that had 120 experiments and suddenly reports 0 means the archive
  renamed or dropped the vocabulary string (SRA then also answers `quotedphrasesnotfound`). Such keys are
  listed under `backfill.kept` and the report carries a warning; fix the string in
  `config/archive_models.yaml`, or `--force` if the zero is real.
* **SRA counts are accepted only when esearch really evaluated the `[Properties]` clause** (see the SRA
  section below); anything else is a failed call, not a number.

Rows whose series is no longer configured (a renamed model string or a changed `epmc_terms` set) stay in
the table but are excluded from the CSV, Markdown and chart, which only ever contain the series planned
for the current run. Prune the `timeseries` table by hand if you want them gone.

Request budget: **one request per entity per source per month**, whatever the vocabulary looks like (see the
next section). The 27 configured entities make 74 series (24 with SRA strings, 24 with ENA values, 26 with
Europe PMC terms), so:

| run | months requested | requests |
|---|---|---|
| warm cache, `--refresh-months 2` (the weekly step) | the current month and the two before it | 74 x 3 = **222** (SRA 72, ENA 72, Europe PMC 78); about a minute: SRA at 0.4 s per request without `NCBI_API_KEY`, the EBI hosts at 0.2 s |
| cold cache, default `--from` (36 months) | every month | **2,611** (SRA 850, ENA 850, Europe PMC 911; 53 months stored as 0 under launch floors, no request) - 12 to 15 minutes |
| cold cache, `--from 2023-01` (46 months on 2026-10-18) | every month | 3,404 - 123 pre-launch = 3,281 |

The warm-cache figure is inside the (entities x sources x 3) = 243 envelope by construction, because a
multi-string entity costs one request, not one per string. `--dry-run` prints the exact number before you
spend it (the numbers above are its output). Both hosts already have `DEFAULT_MIN_INTERVAL` entries in
`litradar/http.py`; no new host was added. Never run two NCBI loops at the same time: during testing, a
second concurrent process was enough to get HTTP 429 from E-utilities.

## One series per entity (layout since 2026-10-18)

An entity's archive strings are OR-ed into **one** count request per month, exactly as its Europe PMC terms
always were: SRA `2025/06/01:2025/06/30[PDAT] AND ("instrument sequel ii"[Properties] OR "instrument sequel
iie"[Properties])`, ENA `... AND (instrument_model="Sequel II" OR instrument_model="Sequel IIe")`. The series
key (the `model` column of the cache and the CSV) is the pipe-joined string list, `sequel ii|sequel iie`;
for a single string it is the string itself, so `revio` rows written before this change are the same rows.
Verified live on 2026-10-18 for 2025-06: the SRA OR gave 5,139 = 3,474 + 1,665 (the two single-string
counts, which the cache also held), the ENA OR gave 5,190 = 3,493 + 1,697, and when one phrase inside the OR
has no records (`vega|onso`) esearch drops it from the querytranslation, lists it under
`warninglist.quotedphrasesnotfound` and still evaluates the others under `[Properties]`, so the acceptance rule
of the SRA section holds. Whole-string matching makes the strings disjoint, which is why the union equals the
sum - and why the per-string history was not lost:

* **Folding.** Every run first calls `consolidate_cache`: for each selected entity with two or more strings
  for a source, a month whose strings are *all* cached becomes one entity row holding their sum (no request),
  the per-string rows are deleted in the same transaction, and the report says so (`consolidated`:
  `derived`, `pruned`, `partial` = months with some strings missing, left to the backfill). Entity rows that
  already exist are kept. A `--dry-run` folds nothing; it plans *as if* it had, so the number it prints is
  the number the real run makes. On 2026-10-18 the 5,704-row per-string cache (124 series) became a 3,404-row
  entity cache (74 series) with 0 partial months.
* **What you lose and where it still is.** The split of one gazetteer entity into its vocabulary variants
  (NovaSeq X vs NovaSeq X Plus, the nine HiSeq models, the eight Ion Torrent models) is no longer refreshed.
  The last per-string export, `digests/timeseries/timeseries_2023-01_2026-10_pacific-biosciences+27ids-e4dcee.csv`
  (2023-01..2026-10 as fetched on 2026-10-18), keeps that resolution as a file. Give a variant its own
  gazetteer entity if you want it as a series of its own.
* **Outputs.** The Markdown columns are labelled with the gazetteer name and the string set
  (`Sequel II (sequel ii, sequel iie)`); the dashboard page sums an entity's rows per month, which is exact
  under either layout.

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
  `vega`, `aviti 24`, `ug 200`, `dnbseq-g800`, all valid vocabulary with zero deposits so far); the
  `querytranslation` still carries the `[Properties]` clause. Treated as 0 (subject to the never-overwrite-
  a-positive-count rule above).
* **A misspelt field tag is not an error for NCBI.** Live on 2026-10-17, `"instrument revio"[Propertiez]`
  returned HTTP 200, `count: "851"`, an `errorlist` key (`{"phrasesnotfound": [], "fieldsnotfound": []}`)
  and a `querytranslation` ending in `"instrument revio"[All Fields]`: a free-text count, not a Properties
  count. `sra_count()` therefore returns a number only when `errorlist` is absent **and** `querytranslation`
  contains `[Properties]`; otherwise the call is reported as failed and nothing is cached.
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
* `instrument_model="Revio"` and `instrument_model=Revio` both work (quoted form used). The value must be
  the exact vocabulary string, but matching is case-insensitive: `"revio"` and `"REVIO"` also return 38.
  `"PacBio Revio"` returns 0 because it is not a vocabulary value, not because of case.
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

Each entity in the config maps to `sra` (lower-case strings), `ena` (exact strings), `epmc_terms` and an
optional `launch` month. `tests/test_timeseries.py` checks that every `ena` value is in this vocabulary, that
`sra` is its lower-case twin, that no model string is claimed by two entities, that every id exists in
`config/instruments.yaml` unless flagged `proposed: true`, and that no Europe PMC term is one of the bare
words that produced false positives live. No id is currently flagged `proposed` (both legacy PacBio ids,
`pacb-rs-ii` and `pacb-sequel`, landed in the gazetteer on 2026-10-17 and their flags were dropped);
`litradar check-config` prints `0 warnings`. The mechanism stays: a pre-registered id works in the time series
before its gazetteer entry exists, `check-config` warns about it and tells you when the flag has become
stale, and `litradar timeseries` repeats the warning in the `warnings` array of its report.

**Europe PMC terms must be vendor-qualified or brand tokens.** The phrase search is case-insensitive and
prefix-inclusive, so an ordinary word is a false-positive machine: live on 2026-10-17 `METHODS:"Vega system"`
returned knee implants, MRI software and astronomy (all 15 hits before 2025 and 12 of 14 after were not
sequencers), bare `Revio` returned a robotics paper (REVIO odometry) as the model's "first mention", and
`UG 100` would match `10 ug 100 uL` in a buffer recipe. The config therefore uses `PacBio Vega` / `Vega
sequencer`, `PacBio Revio` / `Revio system` / `Revio sequencer` / `Revio platform`, `PacBio Sequel II` /
`Sequel II system` / ..., `UG100` / `Ultima UG 100` / `UG 100 sequencer`; tokens such as `NovaSeq`, `MinION`,
`DNBSEQ`, `AVITI` are brands and stay bare. The `epmc` series under-counts by design (a paper that writes
"the Revio (Pacific Biosciences)" is missed) rather than over-counting. Changing an entity's terms changes
its series key: cached months under the old term set stay in the table but leave the outputs.

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
4. **SRA and ENA are mirrors with different month allocation, and ENA-native submissions lead by months.**
   ENA mirrors SRA (and DDBJ), so totals agree over long windows for models whose projects are one-run-per-
   experiment (Revio 2023-01..2026-09: SRA 32,855 experiments vs ENA 31,820 runs, within 3%; across the
   audited models the SRA/ENA ratio was 0.98-1.05 except MGI, see caveat 6) but single months differ because
   SRA's `[PDAT]` and ENA's `first_public` are not the same timestamp (2024-04: SRA 10 vs ENA 138; 2024-07:
   SRA 597 vs ENA 194). Projects submitted to ENA itself (`PRJEB` studies) appear in ENA first and reach SRA
   **months later**: all 479 Sequel IIe runs of 2021-08..2022-05 were PRJEB studies that SRA only shows from
   2022-06, and a 2025-06 MGI `DNBSEQ-T10x4RS` project (ENA 600) surfaced in SRA in 2026-04, ten months later.
   So an SRA series can read "no deposits until mid-2022" for a model ENA has had since 2021. Compare within
   one archive, or sum to quarters. ENA also has months where a model is absent although SRA has it
   (`Illumina NovaSeq X Plus`, 2024-01: ENA 0, SRA 158): the ENA vocabulary gained that value later and older
   mirrored records were not relabelled.
5. **SRA Properties index vs esummary.** The `[Properties]` count is instant and exact on the whole model
   string, which makes it ideal for counting but blind to anything not in the controlled vocabulary.
   The weekly census (`litradar/sources/sra.py`) reads the same `instrument_model` from `esummary` ExpXml,
   which additionally gives study, organism, library strategy and submitter; the two agree on the model
   label (500/500 in the sample) but esummary is ~0.4 s per 500 records, so only the census uses it.
   `unspecified` is a legal value on every platform and is not counted anywhere.
6. **SRA counts experiments, ENA counts runs.** An SRA experiment is a library-on-instrument record (SRX);
   one sample can have several, and one experiment can have many runs (lanes, re-runs). ENA's
   `read_experiment` count endpoint returns one row per experiment-**run** pair, verified live on
   2026-10-17: study `PRJNA1152220` counts 38,728 with `result=read_experiment` and 38,728 with
   `result=read_run` while holding 341 distinct experiments (`SRX25823193` appears once per `SRR`). The
   two series agree only when each experiment has one run; multi-lane projects inflate ENA against SRA
   (MGISEQ-2000RS 2019-01..2026-09: SRA 79,138 experiments vs ENA 149,620 runs, ratio 0.53; 2026-04 alone
   ENA 33,436 vs SRA 1,087 from one project). The CSV `unit` column and the Markdown headers say which is
   which; never add an `sra` and an `ena` figure together. Counting distinct ENA experiments would mean
   paging `experiment_accession` for every month (thousands of rows per request) and is not done.
7. **Europe PMC counts open-access papers only**, with a tokenised phrase match that is case-insensitive
   and prefix-inclusive. Terms are vendor-qualified or brand tokens (`PacBio Vega` and `Vega sequencer`,
   never `Vega`; `PacBio Revio` / `Revio system`, never `Revio`; nothing for the first-generation Sequel), so
   the `epmc` series under-counts by design rather than over-counting; see the vocabulary section above for
   the live false positives that forced this. Months before an entity's `launch` are 0 without a request.
8. **Illumina is grouped by family** because the gazetteer is (`ilmn-hiseq` carries nine HiSeq models). The
   CSV keeps one row per model string, so per-model series are preserved; only the Markdown column label
   groups them under the entity.

## Live run on 2026-10-17: Revio, monthly, 2023-01 to 2026-09

`python -m litradar timeseries --entity pacb-revio --from 2023-01 --to 2026-09 --sources sra,ena,epmc`
(135 requests, 0 failures, 0 kept; second run: 6 requests, 129 cached; re-run unchanged after the review
fixes on the same day). Revio began shipping in early 2023, which is visible as the first deposits in
2023-08 and the first Methods mentions in 2023-01/02 (early-access sites). The Europe PMC column below was
produced with the bare `Revio` term of that day (1,308 papers; a later audit found one 2022-10 robotics false
positive); the vendor-qualified terms now configured will give lower, cleaner counts on the next run. ENA
figures are runs (caveat 6); for Revio they track SRA experiments closely because its projects are mostly
one run per experiment.

| month | SRA experiments | ENA runs | Europe PMC papers |
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

Yearly SRA experiments: 2023 = 224; 2024 = 3,326; 2025 = 10,921; 2026 (Jan-Sep) = 18,384. Yearly Europe PMC
papers: 2023 = 10; 2024 = 121; 2025 = 545; 2026 (Jan-Sep) = 632. The last two to three months will still
grow (caveat 2). The 2026-08 spike is a handful of large releases, not a step change in the installed base
(caveat 1): a 1,000-record `esummary` sample of the 4,418 experiments spans 61 studies, and the three largest
make up 73% of the sample.

## Wiring into the weekly pipeline and the dashboard

The command is standalone and needs no changes to `litradar/pipeline.py`. `.github/workflows/weekly.yml`
runs it in the `radar` job right after `python -m litradar run` and before the database is compressed:

```
timeout -s INT -k 60 15m python -m litradar timeseries --all --refresh-months 2 --no-chart
```

with `continue-on-error: true`, so a source outage or the timeout never fails the digest. Because the step
precedes compression, the refreshed `timeseries` table travels in the `radar.sqlite.zst` snapshot the publish
job stores, and `pages.yml` (which runs after the weekly digest) charts it on `timeseries.html`. Weekly
budget: **222 requests** with a warm cache (the table above). A fork whose snapshot has no cache yet fetches
36 months on its first run (2,611 requests, 12-15 minutes); if the 15-minute `timeout` cuts that short the
run exits non-zero, keeps every count it committed, and finishes the rest the following week. The CSV and
Markdown the step writes under `digests/timeseries/` stay in the runner (the publish job commits
`digests/*.md` only); refresh the committed `timeseries_<from>_<to>_all.*` pair by hand when you want the
file to catch up with the cache.

The dashboard page (`litradar dashboard`, `timeseries.html`; docs/dashboard.md, *The time-series page*)
draws one chart per vendor with the vendor's models as series, a measure switch (SRA experiments, ENA runs,
Europe PMC papers), a linear/log switch, the last two months shaded as incomplete, a vendor total, a
12-month table per vendor, and links to the committed CSV and to `data/timeseries.json`.
