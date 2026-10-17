"""ChinaXiv (https://chinaxiv.org), the Chinese Academy of Sciences preprint server, via the OAI-PMH 2.0
interface of its global aggregator (https://global.chinaxiv.org). Metadata only; abstracts and titles
are mostly Chinese.

Verified live on 2026-10-17. The main site's own OAI endpoint (chinaxiv.org/oai/request) answers
"Sorry! You have no right to access this web." (IP allow-list) and there is no RSS feed (/rss,
/rss.xml, /feed -> 404), so the documented aggregator interface is used instead. It is advertised in the
aggregator's help page ("OAI-PMH 2.0 standard interface … http://global.chinaxiv.org/oaiapi/getdata",
contact eprint@mail.las.ac.cn):

  GET https://global.chinaxiv.org/oaiapi/getdata
      verb=ListRecords  metadataPrefix=oai_dc  source=chinaxiv
      startTime=2026-09-17T00:00:00Z  endTime=2026-10-17T23:59:59Z
  -> <OAI-PMH><ListRecords><record>
       <header><identifier>localIdentifier:chinaxiv_96799</identifier><datestamp>Oct 12, 2026 9:15:46 AM</datestamp></header>
       <metadata><title>…</title><author>…</author>*<keyword>…</keyword>*<subject>管理心理学</subject>
                 <abstract>…</abstract><article-id>202610.00074</article-id><source>chinaxiv</source>
                 <date>2026-10-12</date><type>articles</type><format>oai_dc</format>[<url>…</url>]</metadata>
     </record>…<resumptionToken expirationDate="…" completeListSize="163" cursor="100">1792268762149:100:163:OAI_DC</resumptionToken>
  continue with  verb=ListRecords&resumptionToken=<token>  (100 records per page; the last page has no token)

Quirks observed: the metadata elements are NOT namespaced (not real oai_dc); `date` can be non-padded
("2026-9-19"); Identify/ListSets answer badVerb; the standard from/until arguments are ignored (use
startTime/endTime); `source` accepts chinaxiv, biorxiv, medrxiv, arxiv (others -> badArgument). There is
no DOI element: ChinaXiv DOIs are minted as 10.12074/<article-id>, so the DOI here is DERIVED and flagged
(`extra["doi_derived"] = True`). Sample digits: 163 records (159 distinct ids) for 2026-09-17..10-17, of
which ~15 are life-science subjects (临床医学, 预防医学与公共卫生学, 农业基础学科, 植物生态学, 药物设计 …).
"""
from __future__ import annotations

import html as htmlmod
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterator, List, Optional

from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)

HOST = "global.chinaxiv.org"
OAI_URL = f"https://{HOST}/oaiapi/getdata"
DEFAULT_MIN_INTERVAL = 1.0
DOI_PREFIX = "10.12074"
ABS_URL = "https://chinaxiv.org/abs/{id}"
OAI_NS = "http://www.openarchives.org/OAI/2.0/"

# Chinese discipline labels (ChinaXiv's subject taxonomy) that mark a record as life-science / biomedical. Two or more
# characters each, so single characters such as 药 / 病 / 林 cannot fire on unrelated words. Matched against the
# record's `subject` labels only (keywords and titles are too noisy: a psychology paper on 生物运动 is not biology).
LIFE_SCIENCE_SUBJECT_TERMS = [
    "生物学", "生物工程", "生物信息", "生物医学", "生物技术", "生物化学", "基础医学", "临床医学", "医学", "药学", "药物", "护理学",
    "遗传学", "基因", "细胞", "分子生物", "微生物", "免疫", "神经科学", "肿瘤", "病理", "病毒", "流行病",
    "农业", "农学", "作物", "植物", "动物", "畜牧", "兽医", "林学", "水产", "生态学", "食品科学", "营养", "公共卫生", "预防医学", "中医", "中药",
]
# English fallbacks for aggregator sources (bioRxiv, medRxiv, arXiv q-bio) whose subjects are English; whole words only.
_LIFE_SCIENCE_EN_RE = re.compile(
    r"\b(bio\w*|medic\w*|clinic\w*|pharmac\w*|genom\w*|genetic\w*|cell(s|ular)?|neuro\w*|immun\w*|agricultur\w*|plant(s)?|ecolog\w*|health|oncolog\w*|q-bio\w*)\b",
    re.I,
)
_CJK_RE = re.compile(r"[一-鿿]")
_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_ARTICLE_ID_RE = re.compile(r"^\d{6}\.\d{5}(v\d+)?$")


def _pace(http: Any) -> None:
    mi = getattr(http, "min_interval", None)
    if isinstance(mi, dict):
        mi.setdefault(HOST, DEFAULT_MIN_INTERVAL)


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def normalize_date(s: str) -> str:
    """'2026-9-19' -> '2026-09-19'; anything unparseable -> ''."""
    m = _DATE_RE.search(s or "")
    if not m:
        return ""
    y, mo, d = (int(g) for g in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _clean_text(s: Optional[str]) -> str:
    t = htmlmod.unescape(s or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def is_life_science(subjects: List[str], keywords: Optional[List[str]] = None) -> bool:
    """True when a subject label is a life-science discipline (Chinese taxonomy terms, or English whole words).

    `keywords` is accepted for call-site compatibility but ignored: ChinaXiv keywords are free text and fire on
    unrelated papers (生物运动 in psychology, "implantation" in accelerator physics).
    """
    hay = " ".join(subjects)
    if any(term in hay for term in LIFE_SCIENCE_SUBJECT_TERMS):
        return True
    return bool(_LIFE_SCIENCE_EN_RE.search(hay))


def parse_records(xml_bytes: bytes) -> Dict[str, Any]:
    """Parse one ListRecords page -> {'records': [dict, …], 'token': str|None, 'error': str|None, 'complete_list_size': int|None}."""
    out: Dict[str, Any] = {"records": [], "token": None, "error": None, "complete_list_size": None}
    if not xml_bytes:
        out["error"] = "empty response"
        return out
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        out["error"] = f"malformed XML: {exc}"
        return out
    for el in root.iter():
        if _local(el.tag) == "error":
            out["error"] = f"{el.get('code') or 'error'}: {(el.text or '').strip()}"
    for rec in root.iter():
        if _local(rec.tag) != "record":
            continue
        d: Dict[str, Any] = {"identifier": "", "datestamp": "", "title": "", "abstract": "", "article_id": "", "source": "", "date": "",
                             "type": "", "url": "", "authors": [], "keywords": [], "subjects": []}
        for child in rec.iter():
            name = _local(child.tag)
            text = _clean_text(child.text)
            if not text:
                continue
            if name == "identifier":
                d["identifier"] = text
            elif name == "datestamp":
                d["datestamp"] = text
            elif name == "title":
                d["title"] = text
            elif name == "abstract":
                d["abstract"] = text
            elif name in ("article-id", "article_id", "articleId"):
                d["article_id"] = text
            elif name == "source":
                d["source"] = text
            elif name == "date":
                d["date"] = normalize_date(text)
            elif name == "type":
                d["type"] = text
            elif name == "url":
                d["url"] = text
            elif name in ("author", "creator"):
                d["authors"].append(text)
            elif name == "keyword":
                d["keywords"].append(text)
            elif name == "subject":
                d["subjects"].append(text)
        if d["title"] or d["article_id"]:
            out["records"].append(d)
    for el in root.iter():
        if _local(el.tag) == "resumptionToken":
            out["token"] = (el.text or "").strip() or None
            try:
                out["complete_list_size"] = int(el.get("completeListSize") or 0) or None
            except ValueError:
                out["complete_list_size"] = None
    return out


def to_paper(d: Dict[str, Any]) -> Paper:
    aid = d.get("article_id") or ""
    doi = normalize_doi(f"{DOI_PREFIX}/{aid}") if _ARTICLE_ID_RE.match(aid) else None
    native = aid or (d.get("identifier") or "").rsplit(":", 1)[-1]
    url = ABS_URL.format(id=aid) if aid else (d.get("url") or "")
    title = d.get("title") or ""
    subjects = list(d.get("subjects") or [])
    return Paper(
        id=Paper.make_id("chinaxiv", native, doi),
        source="chinaxiv",
        kind="preprint",
        title=title,
        doi=doi,
        abstract=d.get("abstract") or "",
        journal="ChinaXiv",
        pub_date=d.get("date") or "",
        url=url,
        category=subjects[0] if subjects else "",
        authors=[Author(name=a) for a in d.get("authors") or []],
        topics=list(d.get("keywords") or []),
        extra={
            "article_id": aid,
            "oai_identifier": d.get("identifier") or "",
            "oai_datestamp": d.get("datestamp") or "",
            "subjects": subjects,
            "aggregator_source": d.get("source") or "",
            "landing_url": d.get("url") or "",
            "doi_derived": bool(doi),
            "language": "zh" if _CJK_RE.search(title + (d.get("abstract") or "")) else "en",
            "life_science": is_life_science(subjects, d.get("keywords") or []),
        },
    )


def fetch(http: Any, start: str, end: str, source: str = "chinaxiv", max_records: int = 5000, life_science_only: bool = False) -> Iterator[Paper]:
    """Yield one Paper per distinct article-id released in [start, end] (YYYY-MM-DD, inclusive, UTC).

    Pages via resumptionToken; stops on a missing/repeated token, an empty page, an OAI error, or
    `max_records`. When the same article-id appears twice (versions), the record with the later `date` wins.
    """
    _pace(http)
    params: Dict[str, Any] = {"verb": "ListRecords", "metadataPrefix": "oai_dc", "source": source,
                              "startTime": f"{start}T00:00:00Z", "endTime": f"{end}T23:59:59Z"}
    best: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    seen_tokens: set = set()
    pages = 0
    while len(best) < max_records:
        text = http.get_text(OAI_URL, params=params)
        pages += 1
        if not text:
            log.warning("ChinaXiv OAI: no response (page %d) for %s..%s", pages, start, end)
            break
        page = parse_records(text.encode("utf-8") if isinstance(text, str) else text)
        if page["error"] and not page["records"]:
            level = log.info if "noRecordsMatch" in page["error"] else log.warning
            level("ChinaXiv OAI: %s (source=%s, %s..%s)", page["error"], source, start, end)
            break
        for d in page["records"]:
            key = d.get("article_id") or d.get("identifier") or d.get("title")
            if not key:
                continue
            if key not in best:
                order.append(key)
                best[key] = d
            elif (d.get("date") or "") >= (best[key].get("date") or ""):
                best[key] = d
        tok = page["token"]
        if not page["records"] or not tok or tok in seen_tokens:
            break
        seen_tokens.add(tok)
        params = {"verb": "ListRecords", "resumptionToken": tok}
    log.info("ChinaXiv OAI: %d distinct records (%d pages) for %s..%s source=%s", len(best), pages, start, end, source)
    n = 0
    for key in order:
        p = to_paper(best[key])
        if life_science_only and not p.extra.get("life_science"):
            continue
        yield p
        n += 1
        if n >= max_records:
            break
