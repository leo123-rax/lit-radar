"""arXiv API (Atom) for q-bio and AI-bio preprints. One request per 3 seconds, per arXiv's terms."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Iterable, Iterator, List

from ..http import Http
from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)
API = "http://export.arxiv.org/api/query"
HOST = "export.arxiv.org"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


class ArxivUnavailable(RuntimeError):
    """arXiv answered nothing usable: the host was rate-limited (HTTP 429/503) and the shared client has skipped it
    for the rest of the run, the retries were exhausted, or the body was not Atom. Raised so the run records the
    source as errored (pages already yielded are kept by the caller) instead of logging a quiet partial result."""


def _unavailable(http: Http, pages_done: int, offset: int, detail: str) -> ArxivUnavailable:
    blocked = HOST in (getattr(http, "blocked_hosts", None) or set())
    where = f"after {pages_done} page(s), {offset} record(s)" if pages_done else "on the first page"
    if blocked:
        return ArxivUnavailable(f"{HOST} rate-limited (HTTP 429/503 twice in a row) and skipped for the rest of the run {where}; "
                                "the window is incomplete, re-run later")
    return ArxivUnavailable(f"{HOST} gave no usable answer {where} ({detail}); the window is incomplete, re-run later")


def fetch(http: Http, start: str, end: str, categories: Iterable[str], max_records: int = 2000) -> Iterator[Paper]:
    """Yield arXiv preprints in the categories submitted in [start, end]. Stops and raises ArxivUnavailable as soon
    as the host is blocked or a page is unusable; it never spends the remaining pages on a dead host."""
    cats = " OR ".join(f"cat:{c}" for c in categories)
    s = start.replace("-", "") + "0000"
    e = end.replace("-", "") + "2359"
    query = f"({cats}) AND submittedDate:[{s} TO {e}]"
    offset = 0
    page = 200
    pages_done = 0
    while offset < max_records:
        if HOST in (getattr(http, "blocked_hosts", None) or set()):
            raise _unavailable(http, pages_done, offset, "host blocked")
        text = http.get_text(API, params={"search_query": query, "start": offset, "max_results": page, "sortBy": "submittedDate", "sortOrder": "descending"})
        if not text:
            raise _unavailable(http, pages_done, offset, "no response after the retries")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise _unavailable(http, pages_done, offset, f"body is not Atom XML: {exc}") from None
        pages_done += 1
        entries = root.findall("a:entry", NS)
        for en in entries:
            aid = (en.findtext("a:id", default="", namespaces=NS) or "").rsplit("/abs/", 1)[-1]
            title = " ".join((en.findtext("a:title", default="", namespaces=NS) or "").split())
            summary = " ".join((en.findtext("a:summary", default="", namespaces=NS) or "").split())
            published = (en.findtext("a:published", default="", namespaces=NS) or "")[:10]
            doi = normalize_doi(en.findtext("arxiv:doi", default="", namespaces=NS))
            authors: List[Author] = []
            for au in en.findall("a:author", NS):
                name = au.findtext("a:name", default="", namespaces=NS)
                aff = au.findtext("arxiv:affiliation", default="", namespaces=NS)
                if name:
                    authors.append(Author(name=name, affiliation=aff or ""))
            primary = en.find("arxiv:primary_category", NS)
            cat = primary.get("term") if primary is not None else ""
            yield Paper(
                id=Paper.make_id("arxiv", aid, doi),
                source="arxiv",
                kind="preprint",
                title=title,
                doi=doi,
                abstract=summary,
                journal="arXiv",
                pub_date=published,
                url=f"https://arxiv.org/abs/{aid}",
                category=cat or "",
                authors=authors,
                extra={"arxiv_id": aid},
            )
        if len(entries) < page:
            break
        offset += page
