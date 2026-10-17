"""arXiv API (Atom) for q-bio and AI-bio preprints. One request per 3 seconds, per arXiv's terms."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Iterable, Iterator, List

from ..http import Http
from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)
API = "http://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def fetch(http: Http, start: str, end: str, categories: Iterable[str], max_records: int = 2000) -> Iterator[Paper]:
    cats = " OR ".join(f"cat:{c}" for c in categories)
    s = start.replace("-", "") + "0000"
    e = end.replace("-", "") + "2359"
    query = f"({cats}) AND submittedDate:[{s} TO {e}]"
    offset = 0
    page = 200
    while offset < max_records:
        text = http.get_text(API, params={"search_query": query, "start": offset, "max_results": page, "sortBy": "submittedDate", "sortOrder": "descending"})
        if not text:
            break
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            break
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
