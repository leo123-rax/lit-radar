# ENA Portal API — experiment-level instrument census

Adapter: `litradar/sources/ena.py` → `ArchiveRecord(archive="ena")`, one record per experiment accession.
Tests: `tests/test_sources_offline.py::test_ena_*` (canned payloads, no network).

## Endpoint

`GET https://www.ebi.ac.uk/ena/portal/api/search?result=read_experiment&query=first_public>="{start}" AND first_public<"{end+1 day}"&fields=...&format=json&limit=0`

Fields: `experiment_accession, study_accession, instrument_model, instrument_platform, library_strategy,
library_source, scientific_name, center_name, first_public, study_title`. Falls back to `result=read_run`
(keyed on `run_accession`) when the experiment query fails; `limit=0` means no limit.

## Date bound (verified live 2026-10-17)

The portal compares `first_public` with a bare date as midnight on that day, so `<=` behaves as `<`:

| Query (`result=read_experiment`) | `/count` |
|---|---|
| `first_public>="2026-10-15" AND first_public<="2026-10-15"` | **0** |
| `first_public>="2026-10-15" AND first_public<"2026-10-16"` | **420** |

The old query dropped the last day of every window (a week ending 2026-10-16 would have lost 1,107 of
1,807 records). The adapter now sends `>= start` and `< end + 1 day` (`ena.exclusive_end`), so a one-day
window `start == end` returns that day's rows. Live through the adapter: `ena.fetch(http, "2026-10-15",
"2026-10-15")` → 408 distinct experiments from 420 rows.

## Granularity

`result=read_experiment` is served one row per **run**: the portal appends `run_accession` as a primary
accession even when it is not requested (its `results` listing gives `read_experiment` the same
`recordCount` as `read_run`). The adapter de-duplicates on the key accession before yielding and logs
`ENA: <distinct> distinct experiment(s) from <rows> rows`, so the logged count is no longer inflated.

## Known limitations

* ENA mirrors NCBI SRA and DDBJ with a lag, so a record can appear in SRA first; the store de-duplicates on
  accession across archives only when both carry the SRX/ERX/DRX id.
* `instrument_model` is a controlled vocabulary but submitters still choose generic values
  (`unspecified`, `Illumina HiSeq 2500` for newer machines); the gazetteer maps what it can.
