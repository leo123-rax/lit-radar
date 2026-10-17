"""Optional LLM scoring of the week's shortlist with Claude (Anthropic SDK).

Off by default. Turn on with `--llm` (needs ANTHROPIC_API_KEY). The shortlist is whatever the
heuristic ranked above `ranking.llm.min_heuristic`, capped at `ranking.llm.max_items`.

Cost control
------------
* Structured JSON output (`output_config.format`), so no parsing failures.
* The rubric lives in a cached system prompt (`cache_control`), so per-paper cost is the abstract.
* `use_batch=True` sends everything through the Message Batches API at a 50% discount and polls;
  `use_batch=False` scores synchronously (handy for a handful of papers).
* Refusals (`stop_reason == "refusal"`, possible on pathogen-heavy abstracts) are recorded as
  unscored rather than crashing the run.

Rough cost per 1,000 abstracts (≈600 input + 250 output tokens each, before caching / batch):
  claude-opus-5-5   ≈ $7.4    claude-sonnet-5-5 ≈ $3.7    claude-haiku-5-5 ≈ $0.19
Batch halves those numbers. Set LITRADAR_LLM_MODEL in .env to change the model.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "novelty": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1 incremental .. 5 field-changing"},
        "commercial_implication": {"type": "integer", "minimum": 1, "maximum": 5, "description": "relevance to tools/diagnostics/biotech vendors' revenue or competitive position"},
        "instrument_role": {"type": "string", "enum": ["none", "incidental", "central", "new_platform"]},
        "clinical_readiness": {"type": "string", "enum": ["basic", "preclinical", "early_clinical", "late_clinical", "approved_or_commercial"]},
        "hype_risk": {"type": "integer", "minimum": 1, "maximum": 5, "description": "5 = claims far ahead of evidence"},
        "watchlist_relevance": {"type": "integer", "minimum": 0, "maximum": 5},
        "one_line": {"type": "string", "description": "<= 30 words, plain language, what was shown"},
        "why_it_matters": {"type": "string", "description": "<= 60 words, for an investor in life-science tools and diagnostics"},
        "companies_implicated": {"type": "array", "items": {"type": "string"}, "description": "vendors / companies that gain or lose, named explicitly or by their product"},
        "instruments_implicated": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "novelty", "commercial_implication", "instrument_role", "clinical_readiness", "hype_risk",
        "watchlist_relevance", "one_line", "why_it_matters", "companies_implicated", "instruments_implicated",
    ],
    "additionalProperties": False,
}

SYSTEM_RUBRIC = """You score new scientific papers and preprints for a reader who follows life-science tools, genomics, diagnostics and biotech companies. You are shown a title, abstract, venue and any instruments / companies a text-matcher already found.

Score strictly and consistently:
- novelty: 1 = incremental or confirmatory; 3 = solid advance within a known approach; 5 = opens a new capability or market.
- commercial_implication: how much this changes demand, pricing power or competitive position for identifiable vendors (sequencers, single-cell and spatial platforms, proteomics, synthesis, diagnostics, software). 1 = none; 5 = direct, near-term.
- instrument_role: 'central' when the result depends on a specific named platform; 'new_platform' when the paper itself introduces an instrument / assay / platform; 'incidental' when a platform is merely used; 'none' otherwise.
- clinical_readiness: where the work sits on the path to patients.
- hype_risk: 5 when the framing outruns the evidence (small n, no validation cohort, simulation-only claims stated as clinical).
- watchlist_relevance: 0-5 against the user's watchlist terms supplied in the message.
- one_line / why_it_matters: plain language, no jargon that an informed generalist would not know, no hedging filler.
- companies_implicated: real company names only (e.g. vendors of the platforms used, or companies whose product is the subject). Empty list if none.
Return only the JSON object."""


def _composite(d: Dict[str, Any]) -> float:
    role = {"none": 0.0, "incidental": 0.5, "central": 1.0, "new_platform": 1.5}.get(d.get("instrument_role"), 0.0)
    readiness = {"basic": 0.0, "preclinical": 0.25, "early_clinical": 0.5, "late_clinical": 0.75, "approved_or_commercial": 1.0}.get(d.get("clinical_readiness"), 0.0)
    raw = (
        0.30 * d.get("novelty", 1)
        + 0.30 * d.get("commercial_implication", 1)
        + 0.20 * d.get("watchlist_relevance", 0)
        + 0.6 * role
        + 0.4 * readiness
        - 0.15 * (d.get("hype_risk", 1) - 1)
    )
    # raw range ≈ [0.45, 5.9]; map to 0-10
    return round(max(0.0, min(10.0, (raw - 0.45) / (5.9 - 0.45) * 10.0)), 2)


def _user_message(item: Dict[str, Any], watchlist_terms: Sequence[str]) -> str:
    return (
        f"WATCHLIST TERMS: {', '.join(watchlist_terms) or '(none)'}\n\n"
        f"VENUE: {item.get('journal') or '?'} ({item.get('kind') or 'article'}, {item.get('pub_date') or '?'})\n"
        f"TITLE: {item.get('title')}\n\n"
        f"ABSTRACT: {(item.get('abstract') or '')[:6000]}\n\n"
        f"INSTRUMENTS FOUND BY MATCHER: {', '.join(item.get('instruments') or []) or '(none)'}\n"
        f"COMPANIES FOUND BY MATCHER: {', '.join(item.get('companies') or []) or '(none)'}"
    )


def estimate_cost_usd(n_items: int, model: str, use_batch: bool) -> float:
    prices = {  # $ per million tokens (input, output)
        "claude-opus-5-5": (4.0, 20.0),
        "claude-sonnet-5-5": (2.0, 10.0),
        "claude-haiku-5-5": (0.10, 0.50),
        "claude-fable-5-1": (10.0, 50.0),
    }
    pin, pout = prices.get(model, (4.0, 20.0))
    cost = n_items * (600 * pin + 250 * pout) / 1e6
    return round(cost * (0.5 if use_batch else 1.0), 2)


def score_items(
    items: Sequence[Dict[str, Any]],
    watchlist_terms: Sequence[str],
    model: str = "claude-opus-5-5",
    api_key: str = "",
    use_batch: bool = True,
    poll_seconds: int = 30,
    max_wait_seconds: int = 3600,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Return {item_id: scores dict (with 'composite') or None if refused/failed}."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pip install 'lit-radar[llm]' to enable LLM scoring") from exc

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    system = [{"type": "text", "text": SYSTEM_RUBRIC, "cache_control": {"type": "ephemeral"}}]
    output_config = {"format": {"type": "json_schema", "schema": SCHEMA}}
    results: Dict[str, Optional[Dict[str, Any]]] = {}

    def _parse(message: Any) -> Optional[Dict[str, Any]]:
        if getattr(message, "stop_reason", None) == "refusal":
            return None
        text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            return None
        data["composite"] = _composite(data)
        return data

    if not use_batch or len(items) <= 3:
        for it in items:
            try:
                msg = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=system,
                    messages=[{"role": "user", "content": _user_message(it, watchlist_terms)}],
                    output_config=output_config,
                )
                results[it["id"]] = _parse(msg)
            except anthropic.RateLimitError:
                time.sleep(20)
                results[it["id"]] = None
            except anthropic.APIStatusError as exc:
                log.warning("LLM scoring failed for %s: %s", it["id"], exc)
                results[it["id"]] = None
        return results

    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    id_by_custom: Dict[str, str] = {}
    requests: List[Request] = []
    for i, it in enumerate(items):
        cid = f"paper-{i}"
        id_by_custom[cid] = it["id"]
        requests.append(
            Request(
                custom_id=cid,
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=1024,
                    system=system,
                    messages=[{"role": "user", "content": _user_message(it, watchlist_terms)}],
                    output_config=output_config,
                ),
            )
        )
    batch = client.messages.batches.create(requests=requests)
    log.info("submitted batch %s with %d requests", batch.id, len(requests))
    waited = 0
    while waited < max_wait_seconds:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(poll_seconds)
        waited += poll_seconds
    if batch.processing_status != "ended":
        log.warning("batch %s still %s after %ss; results will be missing", batch.id, batch.processing_status, waited)
        return {it["id"]: None for it in items}
    for res in client.messages.batches.results(batch.id):
        pid = id_by_custom.get(res.custom_id)
        if not pid:
            continue
        if res.result.type == "succeeded":
            results[pid] = _parse(res.result.message)
        else:
            results[pid] = None
    for it in items:
        results.setdefault(it["id"], None)
    return results
