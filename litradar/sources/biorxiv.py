"""bioRxiv / medRxiv via the public API (https://api.biorxiv.org).

Endpoint: https://api.biorxiv.org/details/{server}/{YYYY-MM-DD}/{YYYY-MM-DD}/{cursor}
Returns **30 records per page** (``messages[0].count`` is 30 on every page observed live, 2026-10-17;
the ``cursor`` is a plain record offset) and ``messages[0].total`` gives the total for the interval,
so a one-week bioRxiv window of ~1,800 records is ~60 requests at the 0.5 s host pacing. Each record
carries a `jatsxml` URL (full-text XML) and, once a preprint is published, its journal DOI in
`published`. The interval lists every *version* posted, so we keep the latest per DOI.

Full text: the `jatsxml` URL points at www.biorxiv.org / www.medrxiv.org, which rate-limit scripted
clients (HTTP 429, Cloudflare error 1015) after a handful of requests. ``sources.biorxiv.fulltext`` is
therefore off by default in config/radar.yaml; the sanctioned bulk route is the requester-pays S3
bucket (see docs/sources/biorxiv.md). `attach_fulltext` stays available for forks that enable it.
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, Iterator, List, Optional

from ..extract.jats import parse_jats
from ..http import Http
from ..models import Author, Paper, normalize_doi, preprint_posting_date
from . import jats_abstract

log = logging.getLogger(__name__)
API = "https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}"


def _author_list(s: str, corresponding_inst: str = "") -> List[Author]:
    out = []
    for i, name in enumerate([a.strip() for a in (s or "").split(";") if a.strip()]):
        # the API only gives the corresponding author's institution; attach it to the last author
        aff = corresponding_inst if corresponding_inst and i == len(s.split(";")) - 1 else ""
        out.append(Author(name=name, affiliation=aff))
    return out


def fetch(
    http: Http,
    start: str,
    end: str,
    server: str = "biorxiv",
    categories: Optional[Iterable[str]] = None,
    max_records: int = 20000,
) -> Iterator[Paper]:
    cats = {c.lower().replace("_", " ") for c in categories} if categories else None
    cursor = 0
    seen: Dict[str, Paper] = {}
    while cursor < max_records:
        data = http.get_json(API.format(server=server, start=start, end=end, cursor=cursor))
        if not data:
            break
        coll = data.get("collection") or []
        msg = (data.get("messages") or [{}])[0]
        for item in coll:
            cat = (item.get("category") or "").lower().replace("_", " ")
            if cats and cat not in cats:
                continue
            doi = normalize_doi(item.get("doi"))
            if not doi:
                continue
            version = str(item.get("version") or "1")
            published = item.get("published")
            published_doi = normalize_doi(published) if published and published != "NA" else None
            # `date` is the date of the version listed; the DOI encodes the first posting (10.1101/2025.12.05.691730).
            # A weekly pull that only sees v2 must not date the preprint after its own journal version.
            version_date = item.get("date") or ""
            posted = preprint_posting_date(doi)
            pub_date = posted if posted and (not version_date or posted < version_date) else version_date
            p = Paper(
                id=f"doi:{doi}",
                source=server,
                kind="preprint",
                title=(item.get("title") or "").strip(),
                doi=doi,
                abstract=(item.get("abstract") or "").strip(),
                journal=server,
                pub_date=pub_date,
                url=f"https://www.{server}.org/content/{doi}v{version}",
                license=item.get("license") or "",
                category=cat,
                published_doi=published_doi,
                authors=_author_list(item.get("authors") or "", item.get("author_corresponding_institution") or ""),
                affiliations=[item["author_corresponding_institution"]] if item.get("author_corresponding_institution") else [],
                fulltext_url=item.get("jatsxml") or "",
                extra={"version": version, "version_date": version_date, "type": item.get("type"), "corresponding": item.get("author_corresponding")},
            )
            prev = seen.get(doi)
            if prev is None or int(version) >= int(prev.extra.get("version", "1")):
                seen[doi] = p
        count = int(msg.get("count") or len(coll))
        total = int(msg.get("total") or 0)
        cursor += count
        if count == 0 or cursor >= total:
            break
    if start and end:
        log.info("%s: %d preprints in %s..%s", server, len(seen), start, end)
    else:  # the by-DOI lookup (litradar add) feeds a canned single record through here: no date range to report
        log.debug("%s: %d version(s) kept for %s", server, len(seen), ", ".join(seen) or "no DOI")
    yield from seen.values()


def attach_fulltext(http: Http, paper: Paper, retries: int = 1) -> bool:
    """Download the JATS XML named in the record and fill paper.sections. Returns success.

    One retry by default: the host's 429 carries a 30 s Retry-After and a second one blocks the host
    for the run, so more attempts only add sleep."""
    if not paper.fulltext_url:
        return False
    resp = http.get(paper.fulltext_url, cache=True, retries=retries)
    if resp is None or resp.status_code != 200 or not resp.content:
        return False
    parsed = parse_jats(resp.content)
    if not parsed:
        return False
    for key in ("methods", "funding", "coi", "ack", "data_availability", "body"):
        if parsed.get(key):
            paper.sections[key] = parsed[key]
    if not paper.abstract:  # the API record had none: the JATS document carries it, at no extra request
        paper.abstract = jats_abstract(resp.content)
    if parsed.get("affiliations"):
        paper.affiliations = list(dict.fromkeys(paper.affiliations + parsed["affiliations"]))
    if parsed.get("authors"):
        paper.authors = [Author(**a) for a in parsed["authors"]]
    if parsed.get("emails"):
        paper.extra["emails"] = parsed["emails"]
    paper.fulltext_available = bool(parsed.get("methods") or parsed.get("body"))
    return paper.fulltext_available
