"""Semantic Scholar Academic Graph API: citation enrichment for papers already in the store.

Semantic Scholar is not a *pull* source (nothing here discovers papers); it adds citation velocity
to papers the other adapters found. Everything below was verified live on 2026-10-17 against the
documented endpoints (https://api.semanticscholar.org/api-docs/graph, OpenAPI at
https://api.semanticscholar.org/graph/v1/swagger.json; rate limits at
https://www.semanticscholar.org/product/api).

Batch lookup (one request for up to 500 papers):

    POST https://api.semanticscholar.org/graph/v1/paper/batch?fields=citationCount,influentialCitationCount,publicationDate,externalIds,tldr
    body: {"ids": ["DOI:10.1038/s41592-024-02201-0", "DOI:10.1038/s41586-020-2308-7", "DOI:10.9999/does-not-exist"]}

    -> 200, a JSON *list* in the order of the ids; an unknown id is `null` at its position:
    [
      {"paperId": "13dc81fc…", "externalIds": {"DOI": "10.1038/s41592-024-02201-0", "CorpusId": 268028472, "PubMed": "38409223"},
       "title": "scGPT: toward building a foundation model …", "citationCount": 1462, "influentialCitationCount": 133,
       "publicationDate": "2024-02-26", "tldr": {"model": "tldr@v2.0.0", "text": "This study has constructed …"},
       "openAccessPdf": {…}, "authors": [{"authorId": "…", "name": "…"}, …]},
      {… "citationCount": 8756, "influentialCitationCount": 1050, "publicationDate": "2020-05-01" …},
      null
    ]

    * `fields` is a single comma-separated query parameter (documented: "not to be submitted in the POST body").
    * Documented limits: 500 ids per call, 10 MB per response.
    * The server returned more keys than requested (`title`, `authors`, `openAccessPdf`); we read only what we need.
    * `tldr` is a dict {"model", "text"} or null; `publicationDate` is "YYYY-MM-DD" or null.

Single paper:

    GET https://api.semanticscholar.org/graph/v1/paper/DOI:10.1038/s41592-024-02201-0?fields=title,citationCount,…
    -> 200 with the same shape as one batch element; 404 ("Bad paper id") for an unknown DOI.

Rate limits (product page, 2026-10-17): public endpoints are "rate-limited to 1000 requests per second
shared among all unauthenticated users" and "may also be further throttled during periods of heavy
use"; an introductory API key gets "1 RPS on all endpoints". The key goes in the `x-api-key` header.
Live without a key, 2 of 4 paced attempts (4 s apart) answered 429 with no Retry-After header before a
200, and a later run saw three 429s in a row, so the shared pool is best-effort: this module paces at
1 req/s through the shared Http client (DEFAULT_MIN_INTERVAL["api.semanticscholar.org"]), waits
DEFAULT_RETRY_WAIT seconds and retries a failed batch DEFAULT_RETRIES times, then *skips that batch*
and carries on (so one poison batch cannot block every older paper on every run); after
MAX_CONSECUTIVE_FAILURES failed batches in a row it stops for the run rather than hammering.
Whatever was not enriched is picked up next week.

Matching is positional. The contract (documented, and confirmed live twice on 2026-10-17: a 3-id and
a 7-id batch, both answered in request order with `null` at the unknown id's position) is one element
per requested id, so when the list has the expected length each element is keyed by its position,
whatever DOI it echoes in externalIds. That matters because Semantic Scholar's preprint/journal
handling is per record: it CAN merge a preprint and its journal version into one record and echo only
one of the two DOIs (live: journal DOI 10.1038/s41592-025-02617-2 resolves to a record with venue
"bioRxiv", publicationDate 2023-02-14, i.e. the preprint's metadata), or keep them as separate records
with separate counts (live: scGPT preprint 10.1101/2023.04.30.538439 -> paperId 275fb932…, 123
citations, 2023-07-02; its journal DOI 10.1038/s41592-024-02201-0 -> paperId 13dc81fc…, 1462 citations,
2024-02-26). Positional mapping is right in both cases: each requested DOI gets whatever record it
resolves to, and two DOIs resolving to one merged record both receive its counts. Only when the list
length is off-contract does the parser fall back to the echoed DOI.

Store effect: `papers.cited_by_count = MAX(COALESCE(existing, 0), citationCount)`; `papers.updated_at`
moves only when that count rises (so it works as a change marker for a weekly time series);
`papers.extra_json` gains a `semantic_scholar` object {paper_id, citation_count,
influential_citation_count, publication_date, tldr, fetched_at}, refreshed on every fetch. Other
keys in extra_json are preserved.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..http import Http
from ..models import normalize_doi
from ..store import Store

log = logging.getLogger(__name__)

API = "https://api.semanticscholar.org/graph/v1"
BATCH_URL = f"{API}/paper/batch"
PAPER_URL = f"{API}/paper/"
FIELDS = "citationCount,influentialCitationCount,publicationDate,externalIds,tldr"
DETAIL_FIELDS = "title,venue,year," + FIELDS
BATCH_LIMIT = 500  # documented hard limit per POST /paper/batch
EXTRA_KEY = "semantic_scholar"
DEFAULT_LOOKBACK_DAYS = 365  # papers days old are ~always at 0 citations; the signal is on older items


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers(api_key: str = "") -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


def _chunks(seq: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    size = max(1, int(size))
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_item(item: Any) -> Optional[Dict[str, Any]]:
    """Normalise one element of a batch response (or one /paper/{id} body). None for null / junk."""
    if not isinstance(item, dict):
        return None
    ext = item.get("externalIds")
    if not isinstance(ext, dict):
        ext = {}
    tldr = item.get("tldr")
    if isinstance(tldr, dict):
        tldr_text = str(tldr.get("text") or "").strip()
    elif isinstance(tldr, str):
        tldr_text = tldr.strip()
    else:
        tldr_text = ""
    pub = item.get("publicationDate")
    return {
        "paper_id": str(item.get("paperId") or ""),
        "doi": normalize_doi(ext.get("DOI")) if isinstance(ext.get("DOI"), str) else None,
        "citation_count": _int(item.get("citationCount")),
        "influential_citation_count": _int(item.get("influentialCitationCount")),
        "publication_date": str(pub)[:10] if isinstance(pub, str) else "",
        "tldr": tldr_text,
        "title": str(item.get("title") or ""),
        "venue": str(item.get("venue") or ""),
        "external_ids": {k: v for k, v in ext.items() if isinstance(k, str)},
    }


def parse_batch(data: Any, dois: Sequence[str]) -> Optional[Dict[str, Dict[str, Any]]]:
    """Map a batch response onto the requested DOIs.

    Returns {requested_doi: parsed} for the ids the service knew, or None when the payload is not
    the documented list (so the caller can treat it as a failed request, not an empty result).

    Matching is strictly positional when the list has one element per requested id (the documented,
    live-confirmed contract: request order, `null` for unknown ids). The DOI echoed in externalIds is
    deliberately NOT preferred: Semantic Scholar can merge a preprint and its journal version into one
    record that echoes only one of the two DOIs, so keying on it would hand both entries to one row and
    leave the other requested DOI "unmatched". Only when the list length is off-contract do we fall
    back to the echoed DOI (position is then meaningless), dropping elements without a known DOI.
    """
    if not isinstance(data, list):
        return None
    out: Dict[str, Dict[str, Any]] = {}
    positional = len(data) == len(dois)
    if not positional:
        log.warning("Semantic Scholar: batch returned %d elements for %d ids; matching by echoed DOI only", len(data), len(dois))
    wanted = set(dois)
    for pos, raw in enumerate(data):
        parsed = parse_item(raw)
        if parsed is None:
            continue
        if positional:
            key = dois[pos]
        elif parsed["doi"] in wanted:
            key = parsed["doi"]
        else:
            continue
        out[key] = parsed
    return out


DEFAULT_RETRIES = 3  # live, the unauthenticated pool answered 429 up to three times running before a 200
DEFAULT_RETRY_WAIT = 10.0  # seconds between attempts; no Retry-After header is sent, so this is a guess on the polite side
MAX_CONSECUTIVE_FAILURES = 2  # a single failed batch is skipped; this many in a row ends the run (throttled, not poisoned)


def fetch_batch(
    http: Http, dois: Sequence[str], api_key: str = "", retries: int = DEFAULT_RETRIES, retry_wait: float = DEFAULT_RETRY_WAIT
) -> Optional[Dict[str, Dict[str, Any]]]:
    """One POST /paper/batch for up to 500 DOIs. None when the request failed (after retries)."""
    ids = [f"DOI:{d}" for d in dois]
    url = f"{BATCH_URL}?fields={FIELDS}"
    for attempt in range(int(retries) + 1):
        data = http.post_json(url, {"ids": ids}, headers=_headers(api_key))
        if data is not None:
            parsed = parse_batch(data, dois)
            if parsed is None:
                log.warning("Semantic Scholar: unexpected batch payload (%s); giving up on this batch", type(data).__name__)
            return parsed
        if attempt < retries:
            log.info("Semantic Scholar batch failed (shared pool throttling?); retrying in %.0fs", retry_wait)
            if retry_wait > 0:
                time.sleep(retry_wait)
    return None


def paper_details(http: Http, doi: str, api_key: str = "", fields: str = DETAIL_FIELDS) -> Optional[Dict[str, Any]]:
    """GET /paper/DOI:<doi>. None when the DOI is unknown to Semantic Scholar (404) or the call failed."""
    nd = normalize_doi(doi)
    if not nd:
        return None
    resp = http.get(f"{PAPER_URL}DOI:{nd}", params={"fields": fields}, headers=_headers(api_key))
    if resp is None or resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return parse_item(data)


def window_start(since: Optional[str], lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> Optional[str]:
    """Start of the enrichment window: the run's `since` (YYYY-MM-DD) pushed back by `lookback_days`.

    Papers only days old almost always return citationCount 0, so enriching just the run window is
    near-inert; the value is velocity on papers weeks to months old. None (= whole store) stays None;
    a non-positive look-back or an unparsable date returns `since` unchanged.
    """
    if not since:
        return None
    try:
        start = date.fromisoformat(str(since)[:10])
    except ValueError:
        log.warning("Semantic Scholar: cannot parse since=%r; using it as-is", since)
        return since
    days = int(lookback_days or 0)
    if days <= 0:
        return start.isoformat()
    return (start - timedelta(days=days)).isoformat()


def _candidates(store: Store, since: Optional[str], limit: Optional[int]) -> List[Tuple[str, str, int, str]]:
    sql = "SELECT id, doi, cited_by_count, extra_json FROM papers WHERE doi IS NOT NULL AND doi != ''"
    params: List[Any] = []
    if since:
        sql += " AND (pub_date >= ? OR (pub_date = '' AND substr(first_seen,1,10) >= ?))"
        params += [since, since]
    sql += " ORDER BY pub_date DESC, first_seen DESC, id"  # id tiebreak keeps batch membership deterministic
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [(r[0], r[1], int(r[2] or 0), r[3] or "") for r in store.conn.execute(sql, params).fetchall()]


def enrich_citations(
    http: Http,
    store: Store,
    since: Optional[str] = None,
    api_key: str = "",
    limit: Optional[int] = None,
    batch_size: int = BATCH_LIMIT,
    retries: int = DEFAULT_RETRIES,
    retry_wait: float = DEFAULT_RETRY_WAIT,
) -> Dict[str, Any]:
    """Look up every stored paper with a DOI (optionally only those published / first seen on or after
    `since`) in batches of up to 500 and write citation counts back.

    Returns a summary: candidates, requested, matched, updated (cited_by_count rose), unmatched,
    batches, failed_batches, and `citations` = {paper_id: citationCount} for the matched papers so a
    caller can refresh in-memory objects before ranking.
    """
    rows = _candidates(store, since, limit)
    by_doi: Dict[str, List[Tuple[str, int, str]]] = {}
    for pid, doi, cited, extra_json in rows:
        nd = normalize_doi(doi)
        if nd:
            by_doi.setdefault(nd, []).append((pid, cited, extra_json))
    dois = list(by_doi)
    summary: Dict[str, Any] = {
        "candidates": len(rows), "requested": 0, "matched": 0, "updated": 0, "unmatched": 0,
        "batches": 0, "failed_batches": 0, "citations": {},
    }
    if not dois:
        return summary
    size = min(int(batch_size) if batch_size else BATCH_LIMIT, BATCH_LIMIT)
    fetched_at = _now()
    consecutive_failures = 0
    for chunk in _chunks(dois, size):
        result = fetch_batch(http, chunk, api_key=api_key, retries=retries, retry_wait=retry_wait)
        summary["batches"] += 1
        if result is None:
            summary["failed_batches"] += 1
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.warning("Semantic Scholar: %d batches failed in a row; stopping enrichment for this run (%d of %d DOIs requested)",
                            consecutive_failures, summary["requested"], len(dois))
                break
            log.warning("Semantic Scholar: batch %d failed; skipping it and continuing", summary["batches"])
            continue
        consecutive_failures = 0
        summary["requested"] += len(chunk)
        for doi in chunk:
            hit = result.get(doi)
            if not hit:
                summary["unmatched"] += 1
                continue
            summary["matched"] += 1
            for pid, cited, extra_json in by_doi[doi]:
                try:
                    extra = json.loads(extra_json) if extra_json else {}
                except ValueError:
                    extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                extra[EXTRA_KEY] = {
                    "paper_id": hit["paper_id"],
                    "citation_count": hit["citation_count"],
                    "influential_citation_count": hit["influential_citation_count"],
                    "publication_date": hit["publication_date"],
                    "tldr": hit["tldr"],
                    "fetched_at": fetched_at,
                }
                # COALESCE: SQLite's MAX(NULL, n) is NULL, and the column is nullable. updated_at moves only when the
                # count actually rises so it stays usable as a change marker (the fetch time lives in extra_json).
                store.conn.execute(
                    """UPDATE papers
                       SET cited_by_count = MAX(COALESCE(cited_by_count, 0), ?),
                           extra_json = ?,
                           updated_at = CASE WHEN ? > COALESCE(cited_by_count, 0) THEN ? ELSE updated_at END
                       WHERE id = ?""",
                    (hit["citation_count"], json.dumps(extra, ensure_ascii=False, default=str),
                     hit["citation_count"], fetched_at, pid),
                )
                if hit["citation_count"] > cited:
                    summary["updated"] += 1
                summary["citations"][pid] = max(cited, hit["citation_count"])
        store.commit()
    log.info("Semantic Scholar: %d/%d DOIs matched, %d citation counts raised, %d batch(es), %d failed",
             summary["matched"], summary["requested"], summary["updated"], summary["batches"], summary["failed_batches"])
    return summary


def apply_to_papers(papers: Iterable[Any], citations: Dict[str, int]) -> int:
    """Raise `cited_by_count` on in-memory Paper objects from an enrich_citations() summary. Returns how many changed."""
    n = 0
    for p in papers:
        c = citations.get(getattr(p, "id", ""))
        if c is not None and c > int(getattr(p, "cited_by_count", 0) or 0):
            p.cited_by_count = c
            n += 1
    return n
