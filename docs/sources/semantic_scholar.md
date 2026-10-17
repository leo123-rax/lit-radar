# Semantic Scholar: citation enrichment

Module: `litradar/sources/semantic_scholar.py`. Tests: `tests/test_semantic_scholar_offline.py`.

Semantic Scholar does not discover papers for the radar; it adds **citation velocity** to the papers
the other adapters already stored. One POST per 500 DOIs is enough to refresh a whole week, and a
weekly rerun turns the stored counts into a time series: `papers.updated_at` moves **only when the
count rises**, while `extra_json.semantic_scholar.fetched_at` records every fetch.

Official documentation: https://api.semanticscholar.org/api-docs/graph (OpenAPI:
https://api.semanticscholar.org/graph/v1/swagger.json). Rate limits and keys:
https://www.semanticscholar.org/product/api.

## What was verified live (2026-10-17, no key)

### Batch lookup

```
POST https://api.semanticscholar.org/graph/v1/paper/batch?fields=citationCount,influentialCitationCount,publicationDate,externalIds,tldr
Content-Type: application/json
{"ids": ["DOI:10.1038/s41592-024-02201-0", "DOI:10.1038/s41586-020-2308-7", "DOI:10.9999/does-not-exist"]}
```

Response: HTTP 200, a JSON **list** in request order, `null` where the id is unknown:

| position | externalIds.DOI | citationCount | influentialCitationCount | publicationDate | tldr |
|---|---|---|---|---|---|
| 0 | 10.1038/s41592-024-02201-0 | 1462 | 133 | 2024-02-26 | `{"model": "tldr@v2.0.0", "text": "This study has constructed a foundation model for single-cell biology, scGPT, …"}` |
| 1 | 10.1038/s41586-020-2308-7 | 8756 | 1050 | 2020-05-01 | `{"model": "tldr@v2.0.0", "text": "A catalogue of predicted loss-of-function variants …"}` |
| 2 | (null) | | | | |

Notes from the call:

* `fields` is **one comma-separated query parameter**; the docs say explicitly it is not accepted in the
  POST body. Requests encodes the commas (`%2C`) and the service accepts that.
* Each element also carried `paperId`, `title`, `authors` and `openAccessPdf` although they were not
  requested; the adapter reads only the fields it asked for (plus `paperId`).
* Documented limits: **500 ids per call**, 10 MB per response, 9,999 citations per call (irrelevant here).
* `externalIds` also carried `CorpusId`, `PubMed`, `PubMedCentral`, `MAG` when known.
* **Matching is positional.** The list has one element per requested id, in request order, so the
  adapter keys each element by its position when the length matches, not by the DOI it echoes
  (see the merged-record caveat below). Only an off-contract length falls back to the echoed DOI.

### Re-verification (2026-10-17, 20:29 GMT, no key)

A second batch POST (`curl`, same URL and headers) with seven ids: the five Nature Methods 2025 DOIs
used in the adapter live test, the scGPT bioRxiv preprint, and an unknown DOI. HTTP 200 after one 429
(30 s earlier); `x-amzn-requestid c35811f0-6f9f-4395-a922-aaeecf3bccef`, 10,884 bytes, a list of 7
in request order:

| position | requested id | externalIds.DOI | paperId (prefix) | citationCount | influential | publicationDate |
|---|---|---|---|---|---|---|
| 0 | DOI:10.1038/s41592-025-02595-5 | same | f3968606 | 457 | 56 | 2024-02-25 |
| 1 | DOI:10.1038/s41592-025-02626-1 | same | c5fa0005 | 289 | 24 | 2023-12-23 |
| 2 | DOI:10.1038/s41592-025-02772-6 | same | 60999330 | 260 | 17 | 2025-08-01 |
| 3 | DOI:10.1038/s41592-025-02617-2 | same | 0f9c053f | 177 | 10 | 2023-02-14 |
| 4 | DOI:10.1038/s41592-025-02623-4 | same | 856b677a | 49 | 0 | 2025-03-13 |
| 5 | DOI:10.1101/2023.04.30.538439 | same | 275fb932 | 123 | 29 | 2023-07-02 |
| 6 | DOI:10.9999/does-not-exist | (null) | | | | |

Every element carried `tldr: {model, text}`; `externalIds` on the journal records also listed
`PubMed`, `PubMedCentral`, `CorpusId`, on the preprint only `CorpusId`. Row 5 is the preprint of the
scGPT paper whose journal DOI (10.1038/s41592-024-02201-0) was looked up in the first verification:
Semantic Scholar keeps those two as **separate records** (123 vs 1462 citations), whereas row 3 shows a
journal DOI resolving to a record that carries the **preprint's** venue and date. Both behaviours exist;
see the merged-records caveat.

Adapter live run (20:31-20:32 GMT, real `Http`, temp SQLite store seeded with the same seven DOIs,
`retries=3, retry_wait=30`): pass 1 answered 200 on the first POST, `matched 7 / updated 7 /
unmatched 0 / failed_batches 0`, every row's `cited_by_count`, `paper_id`, `influential_citation_count`
and `tldr` landed on the row at its request position (the scGPT preprint got 123 / 29 / paperId
275fb932…, the scGPT journal DOI 1462 / 133 / 13dc81fc…). Pass 2 with unchanged counts met two 429s,
succeeded on the third attempt, reported `updated 0` and left every `updated_at` untouched. One
`paper_details()` GET answered 200 (177 / 10, `venue: "bioRxiv"`, `publicationDate: 2023-02-14`).
Client stats: 5 requests, 2 failures (the 429s).

### Single paper

```
GET https://api.semanticscholar.org/graph/v1/paper/DOI:10.1038/s41592-024-02201-0?fields=title,venue,year,citationCount,influentialCitationCount,publicationDate,externalIds,tldr
```

HTTP 200 with the same shape as one batch element (`venue: "Nature Methods"`, `year: 2024`). An
unknown DOI is documented as HTTP 404 "Bad paper id"; `paper_details()` returns `None` for it.

### Rate limits

From the product page (quoted 2026-10-17): public endpoints are *"rate-limited to 1000 requests per
second shared among all unauthenticated users. Requests may also be further throttled during periods
of heavy use"*; *"The introductory rate limit for an API key is 1 RPS on all endpoints."* The key is sent
in the `x-api-key` header.

Observed without a key: of four batch POSTs 4 s apart, two answered
`429 {"message": "Too Many Requests. Please wait and try again or apply for a key …", "code": "429"}`
with **no `Retry-After` header**, then a 200; a later run of the adapter saw three 429s in a row
(5 s apart) and gave up for that run. The shared pool is best-effort, so the adapter:

* paces at 1 request/s through the shared `Http` client (`DEFAULT_MIN_INTERVAL["api.semanticscholar.org"] = 1.0`,
  unchanged with a key because the introductory key limit is also 1 RPS);
* on a failed batch waits `retry_wait` (default 10 s) and retries up to `retries` (default 3) times;
* then **skips that batch and carries on** with the next one, so a single bad batch (the client
  collapses 400/429/5xx and non-JSON bodies to the same `None`) cannot block every older paper on
  every weekly run;
* after **two failed batches in a row** (`MAX_CONSECUTIVE_FAILURES`) it stops for the run rather than
  hammering a throttled pool. Un-enriched papers are simply picked up next week.

Set `SEMANTIC_SCHOLAR_API_KEY` in `.env` (already read into `Settings.semantic_scholar_api_key`) to leave
the shared pool; keys are free.

## What the adapter writes

For every stored paper with a DOI (optionally only those published or first seen on/after `since`):

* `papers.cited_by_count = MAX(COALESCE(existing, 0), citationCount)`: counts never go down, OpenAlex
  counts already present are kept when higher, and a NULL left by an import path is treated as 0
  (SQLite's multi-argument `MAX` would otherwise return NULL).
* `papers.extra_json["semantic_scholar"] = {paper_id, citation_count, influential_citation_count,
  publication_date, tldr, fetched_at}`, refreshed on every fetch; other keys in `extra_json` are preserved.
* `papers.updated_at` is set to the fetch time **only for rows whose count rose**, so it remains a
  change marker (note that `Store.upsert_paper` also moves it whenever a source re-delivers the paper).

`enrich_citations()` returns `{"candidates", "requested", "matched", "updated", "unmatched",
"batches", "failed_batches", "citations": {paper_id: count}}`; `apply_to_papers(papers, citations)`
raises `cited_by_count` on in-memory `Paper` objects so the same run's heuristic ("early citations")
can use them.

## API

```python
from litradar.sources import semantic_scholar as s2

summary = s2.enrich_citations(http, store, since="2026-10-10", api_key=settings.semantic_scholar_api_key)
details = s2.paper_details(http, "10.1038/s41592-024-02201-0", api_key=settings.semantic_scholar_api_key)
```

Parameters of `enrich_citations`: `since` (YYYY-MM-DD or None for the whole store), `api_key`,
`limit` (cap on candidate rows, newest first), `batch_size` (clamped to 500), `retries`, `retry_wait`.

`window_start(since, lookback_days=365)` pushes the run's `since` back by `lookback_days` (None stays
None, a non-positive look-back returns `since` unchanged). Use it for the `since` argument: papers only
days old almost always return `citationCount: 0`, so enriching just the 7-day run window is near-inert;
the value is velocity on papers weeks to months old.

## Caveats

* The store's `upsert_paper` replaces `extra_json` wholesale when a source re-delivers the same paper,
  so the `semantic_scholar` block is refreshed, not accumulated: run the enrichment **after** the upserts
  in a pipeline run (the wiring below does), and read history from the weekly `runs` table or by
  diffing digests rather than from `extra_json`.
* Preprint vs journal version is handled **per record** by Semantic Scholar. Some pairs are **merged**
  into one record: `publicationDate` / `venue` in the response are then the preprint's while the
  requested DOI is the journal's (live: journal DOI 10.1038/s41592-025-02617-2 answered
  `venue: "bioRxiv"`, `publicationDate: 2023-02-14`), the counts are the merged total, and
  `externalIds.DOI` echoes only one of the two DOIs. Other pairs stay **separate records** with separate
  counts (live: scGPT preprint 10.1101/2023.04.30.538439 = 123 citations vs journal
  10.1038/s41592-024-02201-0 = 1462). This is why the adapter maps by position, not by the echoed DOI:
  each stored DOI gets whatever record it resolves to, and when the store holds both DOIs of a merged
  pair (bioRxiv adapter + Crossref) both rows receive the same counts and neither is reported
  "unmatched". Read `publication_date` in `extra_json` as "Semantic Scholar's date for the record", not
  the journal date (`papers.pub_date` keeps that).
* Fresh papers (days old) almost always return `citationCount: 0`; the signal is for the look-back and
  for re-ranking older items, not for the week's newest preprints.
* `tldr` is model-generated text from Semantic Scholar (tldr@v2.0.0), not the authors' abstract.

## Wiring (not applied here; see `litradar/pipeline.py`)

```python
from .sources import semantic_scholar as s2_src

# in Pipeline.run, after fetch_news() and before self.rank(...):
if (only is None or "semantic_scholar" in only) and self._src("semantic_scholar").get("enabled", True):
    cfg = self._src("semantic_scholar")
    try:
        s2 = s2_src.enrich_citations(
            self.http, self.store,
            since=None if cfg.get("whole_store") else s2_src.window_start(since, int(cfg.get("lookback_days", 365))),
            api_key=self.settings.semantic_scholar_api_key,
            limit=int(cfg.get("max_papers", 5000)) or None,
        )
        for k in ("requested", "matched", "updated", "failed_batches"):
            self.counts[f"semantic_scholar.{k}"] = s2[k]
        s2_src.apply_to_papers(papers.values(), s2["citations"])
    except Exception as exc:  # noqa: BLE001
        self.errors.append(f"semantic_scholar: {exc}")
        log.exception("semantic scholar enrichment failed")
```

```yaml
# config/radar.yaml, under sources:
  semantic_scholar:
    enabled: true           # citation counts + influential citations + TLDR for stored papers with DOIs
    whole_store: false      # true = refresh every stored paper each run (1 request per 500 DOIs)
    lookback_days: 365      # enrich papers published/first seen within this many days before the run window
    max_papers: 5000        # cap per run; newest first (1 request per 500 DOIs -> at most 10 requests)
```
