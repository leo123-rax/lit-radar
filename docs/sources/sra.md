# NCBI SRA — weekly census of newly public sequencing experiments

Adapter: `litradar/sources/sra.py` → `ArchiveRecord(archive="sra")`, one record per experiment (SRX/ERX/DRX).
Tests: `tests/test_sources_offline.py::test_sra_*` (canned payloads, no network).

## Endpoints

| Step | Call | Key parameters |
|---|---|---|
| 1 | `esearch.fcgi` | `db=sra`, `term=YYYY/MM/DD:YYYY/MM/DD[PDAT]`, `usehistory=y`, `retmax=0`, `retmode=json` → count, WebEnv, query_key |
| 2 | `esummary.fcgi` | `db=sra`, `WebEnv`, `query_key`, `retstart`, `retmax=500` (JSON ceiling), `retmode=json` → one DocSum per uid with `expxml` |
| alt | `esearch.fcgi` | `"instrument illumina novaseq x plus"[Properties]` for instant per-model counts |

A full week is ~120k experiments ≈ 240 esummary pages (~190 MB). `sources.sra.max_records` caps the sweep.

## Pacing and the circuit breaker

NCBI's nominal limit is 3 req/s without an API key, 10/s with one, but 0.34 s already drew HTTP 429s in a
243-page sweep. `litradar/http.py` now paces `eutils.ncbi.nlm.nih.gov` at **0.4 s** without a key and
**0.12 s** with `NCBI_API_KEY`, backs off at least 2 s (or `Retry-After`) on a 429, and only gives up on the
host after **6 consecutive** 429s (other hosts: 2). When the esummary loop does stop early the adapter logs
`census truncated at <n> of <total> records` at WARNING, so a short census is never silent. `api_key=`
values are redacted from every logged URL.

## ExpXml parsing (fixed 2026-10-17)

With `retmode=json` the `expxml` string is already well-formed XML; `&amp;` inside it is a correctly
escaped ampersand. The previous parser ran `html.unescape()` first, which turned `&amp;` into a bare `&`
and made the parse fail for every record whose centre/study/title contains an ampersand (4.7% of a live
week: 5,755 of 121,381 rows, e.g. `center_name="Agriculture &amp; Agri-Food Canada"`). Those records fell
into a regex fallback that kept only the instrument model and stored a synthetic accession `SRA-UID-<uid>`,
so they could not de-duplicate against ENA.

Now: parse raw first; if that yields only a text node that looks like markup (the double-escaped
`retmode=xml` form) or fails, unescape once and retry; as a last resort read experiment/study accession,
strategy, organism and centre with regexes. A synthetic accession is used only when the record truly has
none, and the count of such records is logged.

**Repairing an existing database**: rows written by the old parser must be deleted and the window re-run:

```sql
DELETE FROM archive_records WHERE accession LIKE 'SRA-UID-%';
```

then `litradar run --since <start> --until <end> --only sra` for the affected weeks.

## Known limitations

* `createdate` (the DocSum field used as `first_public`) is the SRA record creation date, which can differ
  from the ENA `first_public` date by a day.
* `retmode=xml` with `retmax` up to 10,000 would cut the request count ~20x; not used yet because the JSON
  path is what the tests and the time-series module exercise.
