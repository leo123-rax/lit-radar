"""GDELT DOC 2.0 API: per-company news without scraping a search engine.

Endpoint
    GET https://api.gdeltproject.org/api/v2/doc/doc?query=<q>&mode=ArtList&format=json&timespan=7d
        &maxrecords=250&sort=DateDesc

    * ``query`` is a free-text expression: quoted phrases, ``OR`` groups **must** be wrapped in
      parentheses (the API answers ``Queries containing OR'd terms must be surrounded by ().`` as a
      200 text/html body otherwise), plus operators such as ``sourcelang:english``,
      ``domain:example.com``, ``domainis:``, ``sourcecountry:``, ``theme:``.
    * ``timespan`` accepts minutes/hours/days/weeks/months (``15min`` minimum, ``3m`` maximum window).
    * ``maxrecords`` defaults to 75 and caps at 250.
    * A result is ``{"articles": [{url, url_mobile, title, seendate (YYYYMMDDTHHMMSSZ), socialimage,
      domain, language, sourcecountry}, ...]}``; an empty result is the literal body ``{}``.

Terms
    GDELT states its data are "available for unlimited and unrestricted use for any academic,
    commercial, or governmental use of any kind without fee" and asks for a citation when
    redistributed; the DOC API is built for programmatic access. The rate limit is one request every
    five seconds per client (HTTP 429 with a plain-text body otherwise); the shared Http client paces
    ``api.gdeltproject.org`` at 8 s, backs off 10 s on a 429 and blocks the host after four 429s in a
    row. Bursting the endpoint puts the client IP in a penalty state that outlasts the burst by many
    minutes, so requests must never be parallelised.

Batching
    Several companies that share a context clause go into one request as an OR group,
    ``("Illumina" OR "10x Genomics" OR "Oxford Nanopore") (sequencing OR genomics) sourcelang:english``,
    packed greedily under :data:`MAX_QUERY_CHARS` (250, the DOC API's query-length limit) and
    :data:`DEFAULT_BATCH_SIZE` names. A 70-company watchlist is therefore about ten requests instead of
    seventy, which is what makes the layer survive an endpoint that sheds one request in three. The
    pipeline attributes each article to the batch member(s) its gazetteer finds in the title, so a
    batch never stores an article that names none of its companies. Trade-off: ``maxrecords`` (250)
    is shared by the batch, so on a 7-day window a very newsworthy member can crowd out a quiet one;
    a company with its own ``news_query`` in companies.yaml is always queried alone.

Coverage caveat
    GDELT crawls mainstream and regional news in 65 languages; paywalled trade titles are mostly
    absent, so this layer complements (does not replace) publisher RSS, press-wire RSS and SEC EDGAR.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..http import Http
from ..models import NewsItem

log = logging.getLogger(__name__)
DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
HOST = DOC_API.split("/")[2]
MAX_RECORDS = 250
MAX_QUERY_CHARS = 250     # the DOC API's query-length limit; a batch is packed under it
DEFAULT_BATCH_SIZE = 8    # names per OR group at most, so maxrecords is not shared too thinly
# The API answers `The specified phrase is too short.` (a 200 text/html body that voids the WHOLE query) for a quoted
# phrase under five characters; observed live 2026-10-18 with "Seer" in a batch of eight, while "Roche" (5) passed.
MIN_PHRASE_CHARS = 5
_OR_GROUP_RE = re.compile(r"\bOR\b")


def too_short(name: str, min_chars: int = MIN_PHRASE_CHARS) -> bool:
    """Would GDELT reject this company name as a phrase? Such a name must be kept out of every query, alone or batched."""
    return len((name or "").strip()) < min_chars


def _phrase(name: str) -> str:
    return f'"{name.strip()}"'


def _context_clause(context: str) -> str:
    """The sector clause as the API wants it: an OR group must be parenthesised."""
    ctx = (context or "").strip()
    if ctx and _OR_GROUP_RE.search(ctx) and not (ctx.startswith("(") and ctx.endswith(")")):
        ctx = f"({ctx})"
    return ctx


def build_query(company: str, context: str = "", language: str = "english", extra: str = "") -> str:
    """Compose a DOC API query: the company name as a phrase, the sector context clause, a language filter.

    The context clause from ``news.query_context`` in radar.yaml is already parenthesised; a bare
    ``a OR b`` clause is wrapped so the API accepts it.
    """
    names = [company] if company and company.strip() else []
    return build_batch_query(names, context, language=language, extra=extra)


def build_batch_query(companies: Sequence[str], context: str = "", language: str = "english", extra: str = "") -> str:
    """One query for several companies: ``("A" OR "B") <context> sourcelang:<language>``.

    A single name is emitted as a bare phrase (identical to :func:`build_query`), so a batch of one
    reads exactly as the per-company query always did.
    """
    names = [n.strip() for n in companies if n and n.strip()]
    parts: List[str] = []
    if len(names) == 1:
        parts.append(_phrase(names[0]))
    elif names:
        parts.append("(" + " OR ".join(_phrase(n) for n in names) + ")")
    ctx = _context_clause(context)
    if ctx:
        parts.append(ctx)
    if extra and extra.strip():
        parts.append(extra.strip())
    if language:
        parts.append(f"sourcelang:{language}")
    return " ".join(parts)


def batch_companies(
    companies: Sequence[Tuple[str, str]],
    context: str = "",
    language: str = "english",
    max_chars: int = MAX_QUERY_CHARS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> List[List[Tuple[str, str]]]:
    """Pack ``(name, id)`` pairs that share one context into OR groups whose query stays within ``max_chars``.

    Greedy and order-preserving: a name is appended to the open batch while the batch has fewer than
    ``batch_size`` members and the resulting query fits; otherwise it opens a new batch. A single name
    whose query alone exceeds the limit still gets a batch of its own (the API, not this function,
    decides what to do with it). ``batch_size <= 0`` means no member cap. Blank names and names the
    API would reject as too short (:func:`too_short`) are left out, because one of them voids the
    whole batch; the caller reports them.
    """
    batches: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    for name, cid in companies:
        if not (name and name.strip()) or too_short(name):
            continue
        trial = current + [(name, cid)]
        fits = len(build_batch_query([n for n, _ in trial], context, language=language)) <= max_chars
        within_size = batch_size <= 0 or len(trial) <= batch_size
        if current and not (fits and within_size):
            batches.append(current)
            current = [(name, cid)]
        else:
            current = trial
    if current:
        batches.append(current)
    return batches


def _seen_date(s: str) -> str:
    """'20261015T143000Z' -> '2026-10-15' (UTC); anything unparseable -> ''."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        pass
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def search(http: Http, query: str, timespan: str = "7d", max_records: int = MAX_RECORDS) -> Optional[List[Dict[str, Any]]]:
    """One DOC API call. Returns the article list ([] when GDELT has none) or None on transport/API failure."""
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "timespan": timespan,
        "maxrecords": max(1, min(int(max_records), MAX_RECORDS)),
        "sort": "DateDesc",
    }
    resp = http.get(DOC_API, params=params, headers={"Accept": "application/json"})
    if resp is None or resp.status_code != 200:
        return None
    body = (resp.text or "").strip()
    if not body or body == "{}":
        return []
    try:
        data = resp.json()
    except ValueError:
        # the API reports query-syntax problems as a 200 text/html one-liner
        log.warning("GDELT rejected query %r: %s", query, body[:160])
        return None
    if not isinstance(data, dict):
        return None
    arts = data.get("articles") or []
    return [a for a in arts if isinstance(a, dict)]


def to_news_item(article: Dict[str, Any], feed: str, company_id: str = "") -> Optional[NewsItem]:
    url = (article.get("url") or "").strip()
    title = re.sub(r"\s+", " ", (article.get("title") or "")).strip()
    if not url or not title:
        return None
    return NewsItem(
        id=hashlib.sha1(url.encode()).hexdigest()[:20],
        feed=feed,
        title=title,
        url=url,
        published=_seen_date(article.get("seendate") or ""),
        summary="",  # the DOC API returns no snippet; the digest links out
        companies=[company_id] if company_id else [],
    )


def company_news(
    http: Http,
    company_name: str,
    company_id: str = "",
    context: str = "",
    timespan: str = "7d",
    language: str = "english",
    since: Optional[str] = None,
    max_records: int = MAX_RECORDS,
) -> Iterator[NewsItem]:
    """Yield NewsItem(feed='gdelt:<company>') for one company. Empty when GDELT has nothing or is rate-limiting."""
    yield from batch_news(http, [(company_name, company_id)], context=context, timespan=timespan, language=language,
                          since=since, max_records=max_records)


def batch_news(
    http: Http,
    companies: Sequence[Tuple[str, str]],
    context: str = "",
    timespan: str = "7d",
    language: str = "english",
    since: Optional[str] = None,
    max_records: int = MAX_RECORDS,
    outcome: Optional[Dict[str, Any]] = None,
) -> Iterator[NewsItem]:
    """One request for a batch of ``(name, id)`` pairs sharing a context; yields every in-window article.

    A batch of one yields items exactly as :func:`company_news` does (feed ``gdelt:<name>``, the company
    attached). For a larger batch the feed is ``gdelt`` and ``companies`` is empty: the caller decides
    which member(s) an article belongs to (the pipeline runs its gazetteer over the title and drops
    articles that name none of them). ``outcome``, when given, receives ``answered`` (bool) and
    ``articles`` (count before the window filter) so the caller can tell "no answer" from "no news".
    """
    names = [n for n, _ in companies if n and n.strip()]
    if not names:
        return
    query = build_batch_query(names, context, language=language)
    single = len(names) == 1
    feed = f"gdelt:{names[0]}" if single else "gdelt"
    company_id = (companies[0][1] or "") if single else ""
    arts = search(http, query, timespan=timespan, max_records=max_records)
    if outcome is not None:
        outcome["answered"] = arts is not None
        outcome["articles"] = len(arts or [])
    if arts is None:
        log.info("GDELT: no answer for %s", repr(names[0]) if single else f"{len(names)} companies ({names[0]} ...)")
        return
    n = 0
    for a in arts:
        item = to_news_item(a, feed, company_id)
        if item is None:
            continue
        if since and item.published and item.published < since:
            continue
        n += 1
        yield item
    log.debug("GDELT: %d article(s) for %s", n, names if not single else repr(names[0]))
