# Semantic Scholar: citation enrichment

Module: `litradar/sources/semantic_scholar.py`. Tests: `tests/test_semantic_scholar_offline.py`.

Semantic Scholar does not discover papers for the radar; it adds **citation velocity** to the papers
the other adapters already stored. One POST per 500 DOIs is enough to refresh a whole week, and a
weekly rerun turns the stored counts into a time series (`updated_at` moves whenever the count rises).

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
* then **stops for the run** rather than hammering. Un-enriched papers are simply picked up next week.

Set `SEMANTIC_SCHOLAR_API_KEY` in `.env` (already read into `Settings.semantic_scholar_api_key`) to leave
the shared pool; keys are free.

## What the adapter writes

For every stored paper with a DOI (optionally only those published or first seen on/after `since`):

* `papers.cited_by_count = MAX(existing, citationCount)`: counts never go down, and OpenAlex counts
  already present are kept when higher.
* `papers.extra_json["semantic_scholar"] = {paper_id, citation_count, influential_citation_count,
  publication_date, tldr, fetched_at}`; other keys in `extra_json` are preserved.
* `papers.updated_at` is set to the fetch time.

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

## Caveats

* The store's `upsert_paper` replaces `extra_json` wholesale when a source re-delivers the same paper,
  so the `semantic_scholar` block is refreshed, not accumulated: run the enrichment **after** the upserts
  in a pipeline run (the wiring below does), and read history from the weekly `runs` table or by
  diffing digests rather than from `extra_json`.
* Semantic Scholar indexes preprints and journal versions as separate papers; a bioRxiv DOI and its
  published DOI may carry different counts. The adapter looks up whatever DOI the store holds.
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
            since=None if cfg.get("whole_store") else since,
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
    max_papers: 5000        # cap per run; newest first
```
