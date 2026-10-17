"""J-STAGE WebAPI: article metadata from Japan's national journal platform (JST).

Verified live on 2026-10-17. Manual (Japanese, PDF, ver. 2.0 of 2026-03-26):
https://www.jstage.jst.go.jp/static/files/ja/manual_api.pdf  -- terms of use:
https://www.jstage.jst.go.jp/static/pages/WebAPI/-char/ja  -- overview:
https://www.jstage.jst.go.jp/static/pages/JstageServices/TAB3/-char/ja

Endpoint (article search is service=3):
  GET https://api.jstage.jst.go.jp/searchapi/do?service=3&abst=genome&pubyearfrom=2025&pubyearto=2026&start=1&count=1000

Parameters (all optional except service; at least one search field is required or ERR_012):
  pubyearfrom / pubyearto  4-digit publication year of the volume (there is NO day-level date filter)
  text   full text        abst   abstract        article  title        keyword  author keywords
  author / affil          material  journal title (partial match)      issn  XXXX-XXXX (exact)
  cdjournal  J-STAGE journal code (e.g. "jarq")   vol / no            start  1-based offset
  count  page size, max 1,000 (count=2000 is silently capped to 1,000; pages beyond 1,000 work via start)
  sortflg  1 = relevance (default); 2 = volume/issue/page, allowed only with material or issn (else ERR_014)
  Several words in one field are AND-ed when separated by a space; values are UTF-8 URL-encoded,
  so Japanese queries work (text=ゲノム returned 976 hits for 2026).

Response: Atom feed (xmlns Atom + prism + opensearch) with
  result/status   "0" ok; "WARN_002" more than 1,000 hits (data is still returned, page with start);
                  "ERR_001" zero hits (also when start > total); "ERR_003" concurrent-access limit;
                  ERR_004..ERR_014 parameter errors (see manual section 5)
  opensearch:totalResults / startIndex / itemsPerPage
  entry: article_title{en,ja}, article_link{en,ja}, author{en,ja}/name, cdjournal, material_title{en,ja},
         prism:issn, prism:eIssn, prism:volume, prism:number, prism:startingPage, prism:endingPage,
         pubyear (YYYY or YYYY-YYYY), joi, prism:doi, systemcode, systemname, title, link@href, id,
         updated (W3CDTF, +09:00) = the article's release date on J-STAGE.
  No abstract and no affiliations are returned by the search API.

Sample (text=genome, count=1000, pubyear 2026): status WARN_002, totalResults 1312, 1000 entries,
999 with a DOI; 169 of the 1000 had `updated` within the previous 30 days.

Terms that matter for a persistent corpus (verified on the terms page, 2026-10-17):
  * Article 3(5): API data may not be stored or cached in machine-readable form on a server or cloud
    for 24 hours or more ("本API提供情報を機械可読な状態でサーバもしくはクラウド等に24時間以上保存又は
    キャッシュしないものとします").
  * Article 4(10): no mass download by mechanical means.  Article 9: show "Powered by J-STAGE" with a
    link wherever the data is displayed.  Article 2: non-commercial use needs no application;
    commercial use (paid or free) requires prior approval from JST.
  Keep this adapter disabled by default and read the terms before enabling it on a hosted database.

Window strategy: the API filters by publication YEAR only, so `fetch` queries pubyearfrom = window
year - 1 (volumes lag their online release) through the window's end year, pages through the hits,
and keeps entries whose `updated` date falls inside [start, end].

Error handling: ERR_003 (concurrent-access limit) arrives as an HTTP 200 body, so the Http client's
retry/backoff never sees it; `search` sleeps RETRY_BACKOFF seconds (doubling, RETRY_MAX attempts) and
re-requests the same page before giving the query up. Every query that ends on a non-OK status is
counted in stats["errors"], and a query whose totalResults exceeds max_records is logged and counted
in stats["truncated"].
"""
from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..http import Http
from ..models import Author, Paper, normalize_doi
from .script_lang import detect_script_lang

log = logging.getLogger(__name__)
API = "https://api.jstage.jst.go.jp/searchapi/do"
MANUAL_URL = "https://www.jstage.jst.go.jp/static/files/ja/manual_api.pdf"
TERMS_URL = "https://www.jstage.jst.go.jp/static/pages/WebAPI/-char/ja"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
MAX_COUNT = 1000
MIN_INTERVAL = 1.0  # seconds between requests; the API has a concurrent-access limit (ERR_003)
RETRY_BACKOFF = 5.0  # seconds to wait after ERR_003 before re-requesting the same page (doubles each time)
RETRY_MAX = 2  # re-requests per page on ERR_003 before the query is abandoned
STATUS_OK = "0"
STATUS_OVER_CAP = "WARN_002"  # more than 1,000 hits: not an error, page with `start`
STATUS_NO_RESULTS = "ERR_001"  # zero hits, or `start` beyond the last hit
STATUS_BUSY = "ERR_003"  # concurrent-access limit, delivered in an HTTP 200 body
_sleep = time.sleep  # indirection so tests can stub the backoff
SEARCH_FIELDS = ("abst", "text", "article", "keyword", "author", "affil", "material", "issn", "cdjournal")
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _pace(http: Http) -> None:
    mi = getattr(http, "min_interval", None)
    if isinstance(mi, dict):
        mi.setdefault("api.jstage.jst.go.jp", MIN_INTERVAL)


def _clean(s: Optional[str]) -> str:
    """Titles arrive as CDATA with inline <i>, <sub>, <sup> markup."""
    s = html.unescape(_TAG_RE.sub("", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _text(el: ET.Element, path: str) -> str:
    return _clean(el.findtext(path, default="", namespaces=NS))


def _int(s: Optional[str]) -> int:
    try:
        return int((s or "").strip())
    except ValueError:
        return 0


def _entry(en: ET.Element) -> Dict[str, Any]:
    link_el = en.find("a:link", NS)
    return {
        "title_en": _text(en, "a:article_title/a:en"),
        "title_ja": _text(en, "a:article_title/a:ja"),
        "link_en": _text(en, "a:article_link/a:en"),
        "link_ja": _text(en, "a:article_link/a:ja"),
        "link": (link_el.get("href") if link_el is not None else "") or _text(en, "a:id"),
        "authors_en": [_clean(n.text) for n in en.findall("a:author/a:en/a:name", NS) if _clean(n.text)],
        "authors_ja": [_clean(n.text) for n in en.findall("a:author/a:ja/a:name", NS) if _clean(n.text)],
        "cdjournal": _text(en, "a:cdjournal"),
        "journal_en": _text(en, "a:material_title/a:en"),
        "journal_ja": _text(en, "a:material_title/a:ja"),
        "issn": _text(en, "prism:issn"),
        "eissn": _text(en, "prism:eIssn"),
        "volume": _text(en, "prism:volume"),
        "number": _text(en, "prism:number"),
        "start_page": _text(en, "prism:startingPage"),
        "end_page": _text(en, "prism:endingPage"),
        "pubyear": _text(en, "a:pubyear"),
        "joi": _text(en, "a:joi"),
        "doi": _text(en, "prism:doi"),
        "updated": _text(en, "a:updated"),
    }


def parse_feed(text: Optional[str]) -> Dict[str, Any]:
    """Parse one Atom response into {status, message, total, start, per_page, entries}."""
    out: Dict[str, Any] = {"status": "", "message": "", "total": 0, "start": 0, "per_page": 0, "entries": []}
    if not text:
        out["status"] = "HTTP_ERROR"
        return out
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        out["status"] = "PARSE_ERROR"
        return out
    out["status"] = (root.findtext("a:result/a:status", default="", namespaces=NS) or "").strip()
    out["message"] = (root.findtext("a:result/a:message", default="", namespaces=NS) or "").strip()
    out["total"] = _int(root.findtext("os:totalResults", default="", namespaces=NS))
    out["start"] = _int(root.findtext("os:startIndex", default="", namespaces=NS))
    out["per_page"] = _int(root.findtext("os:itemsPerPage", default="", namespaces=NS))
    out["entries"] = [_entry(en) for en in root.findall("a:entry", NS)]
    return out


def search_page(http: Http, params: Dict[str, Any], start: int = 1, count: int = MAX_COUNT) -> Dict[str, Any]:
    q: Dict[str, Any] = dict(params)
    q["service"] = 3
    q["start"] = start
    q["count"] = max(1, min(int(count), MAX_COUNT))
    return parse_feed(http.get_text(API, params=q))


def search(http: Http, params: Dict[str, Any], max_records: int = 5000, stats: Optional[Dict[str, int]] = None) -> Iterator[Dict[str, Any]]:
    """Page through an article search (`params` = search fields + pubyearfrom/pubyearto).

    ERR_003 (concurrent-access limit) is retried on the same page after RETRY_BACKOFF s (doubling), up to
    RETRY_MAX times. `stats` receives counters: errors (queries abandoned on a non-OK status, HTTP failures
    and unparsable bodies included), truncated (queries with more hits than max_records).
    """
    counters = stats if stats is not None else {}
    counters.setdefault("errors", 0)
    counters.setdefault("truncated", 0)
    start = 1
    retries = 0
    backoff = RETRY_BACKOFF
    while start <= max_records:
        want = min(MAX_COUNT, max_records - start + 1)
        page = search_page(http, params, start=start, count=want)
        status = page["status"]
        if status == STATUS_NO_RESULTS:
            return
        if status == STATUS_BUSY and retries < RETRY_MAX:
            retries += 1
            log.info("J-STAGE: ERR_003 (concurrent-access limit) for %s start=%d; retry %d/%d in %.0fs", params, start, retries, RETRY_MAX, backoff)
            _sleep(backoff)
            backoff *= 2
            continue
        if status not in (STATUS_OK, STATUS_OVER_CAP):
            counters["errors"] += 1
            log.warning("J-STAGE: status %s %s for %s (start=%d); query abandoned", status, page["message"], params, start)
            return
        retries = 0
        backoff = RETRY_BACKOFF
        entries = page["entries"]
        total = page["total"]
        if start == 1 and total > max_records:
            counters["truncated"] += 1
            log.warning("J-STAGE: %d hits for %s but max_records=%d; only the first %d are scanned", total, params, max_records, max_records)
        yield from entries
        if not entries:
            return
        start += len(entries)
        if total:
            if start > total:  # totalResults is authoritative; a short page before the end still has more behind it
                return
        elif len(entries) < want:  # no total reported: a short page is the last one
            return


def hit_count(http: Http, params: Dict[str, Any]) -> int:
    """Total hits for a query (one request, count=1). ERR_001 means zero."""
    page = search_page(http, params, start=1, count=1)
    return page["total"] if page["status"] in (STATUS_OK, STATUS_OVER_CAP) else 0


def entry_to_paper(e: Dict[str, Any]) -> Paper:
    doi = normalize_doi(e.get("doi"))
    title_en, title_ja = e.get("title_en") or "", e.get("title_ja") or ""
    title = title_en or title_ja
    native = title_ja or title_en
    lang = detect_script_lang(title_ja, default="en", han="ja") if title_ja else "en"
    url = e.get("link_en") or e.get("link") or e.get("link_ja") or ""
    native_id = e.get("joi") or url or f"{e.get('cdjournal')}/{e.get('volume')}/{e.get('number')}/{e.get('start_page')}"
    authors: List[Author] = [Author(name=n) for n in (e.get("authors_en") or [])] or [Author(name=n) for n in (e.get("authors_ja") or [])]
    updated = e.get("updated") or ""
    pubyear = (e.get("pubyear") or "")[:4]
    if _DATE_RE.match(updated):
        pub_date = updated[:10]
    elif pubyear.isdigit():
        pub_date = f"{pubyear}-01-01"
    else:
        pub_date = ""
    pages = "-".join(x for x in (e.get("start_page"), e.get("end_page")) if x)
    return Paper(
        id=Paper.make_id("jstage", native_id, doi),
        source="jstage",
        kind="article",
        title=title,
        doi=doi,
        journal=e.get("journal_en") or e.get("journal_ja") or "",
        pub_date=pub_date,
        url=url or (f"https://doi.org/{doi}" if doi else ""),
        authors=authors,
        extra={
            "lang": lang,
            "country": "JP",
            "title_native": native,
            "title_en": title_en,
            "title_ja": title_ja,
            "journal_ja": e.get("journal_ja") or "",
            "authors_ja": e.get("authors_ja") or [],
            "cdjournal": e.get("cdjournal") or "",
            "issn": [x for x in (e.get("issn"), e.get("eissn")) if x],
            "volume": e.get("volume") or "",
            "number": e.get("number") or "",
            "pages": pages,
            "pubyear": e.get("pubyear") or "",
            "joi": e.get("joi") or "",
            "jstage_updated": updated,
        },
    )


def fetch(
    http: Http,
    start: str,
    end: str,
    terms: Sequence[str] = (),
    field: str = "abst",
    journals: Sequence[str] = (),
    issns: Sequence[str] = (),
    max_records: int = 5000,
    year_slack: int = 1,
    stats: Optional[Dict[str, int]] = None,
) -> Iterator[Paper]:
    """Yield articles released on J-STAGE in [start, end] (YYYY-MM-DD) that match any term, journal code or ISSN.

    `field` is the search field for `terms` (abst, text, article, keyword, author, affil, material).
    `journals` are J-STAGE journal codes (cdjournal), `issns` are XXXX-XXXX strings; each is one query.
    `stats` (optional dict) receives counters: queries, entries, in_window, papers, errors (queries abandoned
    on a non-OK status), truncated (queries with more hits than max_records).
    """
    if field not in SEARCH_FIELDS:
        raise ValueError(f"field must be one of {SEARCH_FIELDS}")
    _pace(http)
    years = {"pubyearfrom": str(max(1, int(start[:4]) - year_slack)), "pubyearto": end[:4]}
    queries: List[Dict[str, Any]] = []
    queries += [dict(years, **{field: t}) for t in terms if t]
    queries += [dict(years, cdjournal=j) for j in journals if j]
    queries += [dict(years, issn=i) for i in issns if i]
    seen = set()
    counters = stats if stats is not None else {}
    counters.setdefault("queries", 0)
    counters.setdefault("entries", 0)
    counters.setdefault("in_window", 0)
    counters.setdefault("papers", 0)
    counters.setdefault("errors", 0)
    counters.setdefault("truncated", 0)
    for q in queries:
        counters["queries"] += 1
        n_all = n_in = 0
        for e in search(http, q, max_records=max_records, stats=counters):
            n_all += 1
            upd = (e.get("updated") or "")[:10]
            if not (start <= upd <= end):
                continue
            n_in += 1
            p = entry_to_paper(e)
            if p.id in seen:
                continue
            seen.add(p.id)
            counters["papers"] += 1
            yield p
        counters["entries"] += n_all
        counters["in_window"] += n_in
        log.info("J-STAGE %s: %d hits scanned, %d released %s..%s", {k: v for k, v in q.items() if not k.startswith("pubyear")}, n_all, n_in, start, end)
