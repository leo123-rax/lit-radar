"""KCI (Korea Citation Index, National Research Foundation of Korea) via its open OAI-PMH repository.

Verified live on 2026-10-17. Documentation (Korean):
https://www.kci.go.kr/kciportal/po/openapi/openDataOaiPmhView.kci  (OAI-PMH guide, base URL, sample calls)
https://www.kci.go.kr/kciportal/po/openapi/openDataPackGuide.kci?datasetBean.dtstTyCd=00  (data services overview)
Licence stated on the guide page: attribution required, no commercial use ("저작자표시-출처표시 ... 영리목적으로 사용불가능").

Base URL: https://open.kci.go.kr/oai/request   (Identify: repositoryName "KCI (Korea Citation Index) Open
Access Repository", earliestDatestamp 2013-03-21, granularity YYYY-MM-DD, deletedRecord no)
  verb=ListSets            -> ARTI (Article), ARTI_CONF (Conference Article), JOUR (Journal)
  verb=ListMetadataFormats -> oai_dc, oai_kci (schema http://www.kci.go.kr/kciportal/OAI/oai_kci.xsd)
  verb=ListRecords&set=ARTI&metadataPrefix=oai_kci&from=YYYY-MM-DD&until=YYYY-MM-DD
     -> 100 records per page; resumptionToken "from:until:pagesize:next_record_id" (the last field is the
        ARTI id the next page starts at, NOT a complete-list size; no total count is exposed)
  verb=GetRecord&identifier=oai:kci.go.kr:ARTI/<n>&metadataPrefix=oai_kci

oai_kci record: journalInfo{journal-name, pissn, eissn, publisher-name, pub-year, pub-mon, volume, issue}
  articleInfo@article-id (ART...){article-categories, title-group/article-title@lang=original|english,
  author-name/author{name, affiliation}, abstract-group/abstract@lang=original|english, language, fpage, lpage,
  doi (as http://dx.doi.org/...), uci, citation-count, url, verified}
  <language> carries the Korean NAME of the language, not a code: 한국어 on 200/200 records sampled on 2026-10-17
  (영어 = English, 일본어, 중국어, ... are the other values KCI uses). `KCI_LANG` maps those names to ISO 639-1;
  an unmapped or empty value falls back to the script of the native title (`script_lang.detect_script_lang`).
oai_dc record: dc:title@lang, dc:creator "name(affiliation); ...", dc:subject (category), dc:identifier@type
  (journalInfo with issn/eissn attributes, artiId, citedCnt, doi, uci), dc:publisher, dc:date YYYY-MM, dc:url.

Limits observed on 2026-10-17 (this is why the adapter is defensive):
  * The datestamp is the record's last processing date, not the publication date. A bulk re-processing
    on 2026-10-05 re-stamped a large part of the back catalogue: the first 500 records of the 30-day window
    were all published 2009-2014 (499 of them in one subject, 한의학), and the window's size is unknown
    because the protocol exposes no completeListSize. `fetch` therefore drops records whose pub-year is
    older than `min_pub_year`, and `max_pages` caps the harvest (100 records per page).
  * Paging works only by following the returned token verbatim (stateless; no cookie needed). Editing the
    token's third field does not move the cursor, so hand-built offsets are useless. `list_records` still
    stops as soon as a page repeats identifiers or a token repeats, so a misbehaving server cannot loop it.
  * Tokens do not encode the metadata format; the server defaults to oai_dc on token requests but
    accepts (non-standard) `metadataPrefix` next to the token, which this adapter sends.
  * ListIdentifiers never returned a resumptionToken in testing (use ListRecords).
  * `from`/`until` must be YYYY-MM-DD; other formats give noRecordsMatch rather than badArgument.
The keyword-search Open API (https://open.kci.go.kr/po/openapi/openApiSearch.kci?apiCode=articleSearch&key=...)
requires a key issued by KCI; without one it answers "필수 요청 파라미터가 없음 => key" / "등록되지 않은 key 입니다",
so its response schema could not be verified and it is not implemented here.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..http import Http
from ..models import Author, Paper, normalize_doi
from .script_lang import detect_script_lang, fold_text

log = logging.getLogger(__name__)
OAI = "https://open.kci.go.kr/oai/request"
DOC_URL = "https://www.kci.go.kr/kciportal/po/openapi/openDataOaiPmhView.kci"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "kci": "http://www.kci.go.kr/kciportal/OAI/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}
MIN_INTERVAL = 1.0
PAGE_SIZE = 100  # observed; not configurable
_CREATOR_RE = re.compile(r"^(?P<name>[^()]+?)\s*(?:\((?P<aff>[^)]*)\))?\s*$")
_ISO2_RE = re.compile(r"^[a-z]{2}$")
# KCI writes the language as its Korean name (<language>한국어</language>); map the names KCI uses to ISO 639-1.
KCI_LANG = {
    "한국어": "ko", "영어": "en", "일본어": "ja", "중국어": "zh", "독일어": "de", "프랑스어": "fr",
    "스페인어": "es", "러시아어": "ru", "korean": "ko", "english": "en",
}


def _pace(http: Http) -> None:
    mi = getattr(http, "min_interval", None)
    if isinstance(mi, dict):
        mi.setdefault("open.kci.go.kr", MIN_INTERVAL)


def _t(el: Optional[ET.Element], path: str) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.findtext(path, default="", namespaces=NS) or "").strip()


def _by_lang(el: Optional[ET.Element], path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if el is None:
        return out
    for x in el.findall(path, NS):
        out[(x.get("lang") or "original").lower()] = re.sub(r"\s+", " ", x.text or "").strip()
    return out


def _empty_record() -> Dict[str, Any]:
    return {
        "identifier": "", "datestamp": "", "article_id": "", "title_original": "", "title_english": "",
        "abstract_original": "", "abstract_english": "", "authors": [], "categories": [], "journal": "",
        "pissn": "", "eissn": "", "publisher": "", "pub_year": "", "pub_mon": "", "volume": "", "issue": "",
        "fpage": "", "lpage": "", "doi": "", "uci": "", "citation_count": "", "url": "", "language": "",
    }


def _parse_kci(md: ET.Element, rec: Dict[str, Any]) -> None:
    ji = md.find("kci:oai_kci/kci:journalInfo", NS)
    ai = md.find("kci:oai_kci/kci:articleInfo", NS)
    rec["journal"] = _t(ji, "kci:journal-name")
    rec["pissn"] = _t(ji, "kci:pissn")
    rec["eissn"] = _t(ji, "kci:eissn")
    rec["publisher"] = _t(ji, "kci:publisher-name")
    rec["pub_year"] = _t(ji, "kci:pub-year")
    rec["pub_mon"] = _t(ji, "kci:pub-mon")
    rec["volume"] = _t(ji, "kci:volume")
    rec["issue"] = _t(ji, "kci:issue")
    if ai is None:
        return
    rec["article_id"] = ai.get("article-id") or ""
    rec["categories"] = [c for c in (_t(ai, "kci:article-categories"),) if c]
    titles = _by_lang(ai, "kci:title-group/kci:article-title")
    rec["title_original"] = titles.get("original", "")
    rec["title_english"] = titles.get("english", "")
    abstracts = _by_lang(ai, "kci:abstract-group/kci:abstract")
    rec["abstract_original"] = abstracts.get("original", "")
    rec["abstract_english"] = abstracts.get("english", "")
    authors = []
    for a in ai.findall("kci:author-name/kci:author", NS):
        name = _t(a, "kci:name")
        if name:
            authors.append({"name": name, "affiliation": _t(a, "kci:affiliation")})
    if not authors:
        for a in ai.findall("kci:author-group/kci:author", NS):
            authors.extend(_split_creators(a.text or ""))
    rec["authors"] = authors
    rec["language"] = _t(ai, "kci:language")
    rec["fpage"] = _t(ai, "kci:fpage")
    rec["lpage"] = _t(ai, "kci:lpage")
    rec["doi"] = _t(ai, "kci:doi")
    rec["uci"] = _t(ai, "kci:uci")
    rec["citation_count"] = _t(ai, "kci:citation-count")
    rec["url"] = _t(ai, "kci:url")


def _split_creators(s: str) -> List[Dict[str, str]]:
    out = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        m = _CREATOR_RE.match(part)
        if m:
            out.append({"name": m.group("name").strip(), "affiliation": (m.group("aff") or "").strip()})
        else:
            out.append({"name": part, "affiliation": ""})
    return out


def _parse_dc(md: ET.Element, rec: Dict[str, Any]) -> None:
    dc = md.find("oai_dc:dc", NS)
    if dc is None:
        return
    titles = _by_lang(dc, "dc:title")
    rec["title_original"] = titles.get("original", "") or next(iter(titles.values()), "")
    rec["title_english"] = titles.get("english", "")
    rec["authors"] = _split_creators(_t(dc, "dc:creator"))
    rec["categories"] = [x.text.strip() for x in dc.findall("dc:subject", NS) if (x.text or "").strip()]
    for ident in dc.findall("dc:identifier", NS):
        kind = (ident.get("type") or "").lower()
        val = (ident.text or "").strip()
        if kind == "journalinfo":
            rec["journal"] = val.split(",")[0].strip()
            rec["pissn"] = ident.get("issn") or ""
            rec["eissn"] = ident.get("eissn") or ""
        elif kind == "artiid":
            rec["article_id"] = val
        elif kind == "citedcnt":
            rec["citation_count"] = val
        elif kind == "doi":
            rec["doi"] = val
        elif kind == "uci":
            rec["uci"] = val
    rec["publisher"] = _t(dc, "dc:publisher")
    date = _t(dc, "dc:date")
    rec["pub_year"], rec["pub_mon"] = date[:4], date[5:7]
    rec["url"] = _t(dc, "dc:url")


def parse_page(text: Optional[str]) -> Dict[str, Any]:
    """Parse one OAI-PMH response into {records, token, error, error_code}."""
    out: Dict[str, Any] = {"records": [], "token": "", "error": "", "error_code": ""}
    if not text:
        out["error"], out["error_code"] = "empty response", "HTTP_ERROR"
        return out
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as exc:
        out["error"], out["error_code"] = f"parse error: {exc}", "PARSE_ERROR"
        return out
    err = root.find("oai:error", NS)
    if err is not None:
        out["error"] = (err.text or "").strip()
        out["error_code"] = err.get("code") or "error"
        return out
    for r in root.findall(".//oai:ListRecords/oai:record", NS) + root.findall(".//oai:GetRecord/oai:record", NS):
        rec = _empty_record()
        rec["identifier"] = _t(r, "oai:header/oai:identifier")
        rec["datestamp"] = _t(r, "oai:header/oai:datestamp")
        md = r.find("oai:metadata", NS)
        if md is not None:
            if md.find("kci:oai_kci", NS) is not None:
                _parse_kci(md, rec)
            else:
                _parse_dc(md, rec)
        out["records"].append(rec)
    out["token"] = (root.findtext(".//oai:resumptionToken", default="", namespaces=NS) or "").strip()
    return out


def list_records(
    http: Http,
    since: str,
    until: str,
    metadata_prefix: str = "oai_kci",
    set_spec: str = "ARTI",
    max_pages: int = 10,
) -> Iterator[Dict[str, Any]]:
    """Harvest ListRecords for [since, until] (datestamp = KCI processing date), stopping on repeats."""
    params: Dict[str, Any] = {"verb": "ListRecords", "set": set_spec, "metadataPrefix": metadata_prefix, "from": since, "until": until}
    seen_ids: set = set()
    seen_tokens: set = set()
    for page_no in range(max(1, max_pages)):
        page = parse_page(http.get_text(OAI, params=params))
        if page["error"]:
            if page["error_code"] != "noRecordsMatch":
                log.warning("KCI OAI-PMH: %s (%s)", page["error"], page["error_code"])
            return
        fresh = [r for r in page["records"] if r["identifier"] and r["identifier"] not in seen_ids]
        if not fresh:
            if page["records"]:
                log.info("KCI OAI-PMH: page %d repeated already-seen records; stopping (server paging limit)", page_no + 1)
            return
        for r in fresh:
            seen_ids.add(r["identifier"])
            yield r
        token = page["token"]
        if not token or token in seen_tokens:
            return
        seen_tokens.add(token)
        params = {"verb": "ListRecords", "set": set_spec, "metadataPrefix": metadata_prefix, "resumptionToken": token}


def language_code(raw: str, native_title: str) -> str:
    """ISO 639-1 code for a KCI <language> value (a Korean language name, or occasionally a code); falls back to
    the script of `native_title` (Hangul -> ko, kana -> ja, Han-only -> ko for this source, Latin -> en)."""
    raw = (raw or "").strip()
    low = raw.lower()
    code = KCI_LANG.get(raw) or KCI_LANG.get(low) or (low if _ISO2_RE.match(low) else "")
    return code or detect_script_lang(native_title, default="en", han="ko")


def _date(year: str, mon: str) -> str:
    if not (year or "").isdigit() or len(year) != 4:
        return ""
    m = mon if (mon or "").isdigit() and 1 <= int(mon) <= 12 else "01"
    return f"{year}-{int(m):02d}-01"


def record_to_paper(r: Dict[str, Any]) -> Paper:
    doi = normalize_doi(r.get("doi"))
    title_en, title_orig = r.get("title_english") or "", r.get("title_original") or ""
    title = title_en or title_orig
    native = title_orig or title_en
    lang = language_code(r.get("language") or "", native)
    authors = [Author(name=a["name"], affiliation=a.get("affiliation", "")) for a in (r.get("authors") or []) if a.get("name")]
    affs = list(dict.fromkeys(a.affiliation for a in authors if a.affiliation))
    native_id = r.get("article_id") or r.get("identifier") or r.get("uci") or ""
    pages = "-".join(x for x in (r.get("fpage"), r.get("lpage")) if x)
    cited = r.get("citation_count") or "0"
    return Paper(
        id=Paper.make_id("kci", native_id, doi),
        source="kci",
        kind="article",
        title=title,
        doi=doi,
        abstract=r.get("abstract_english") or r.get("abstract_original") or "",
        journal=r.get("journal") or "",
        pub_date=_date(r.get("pub_year") or "", r.get("pub_mon") or ""),
        url=r.get("url") or (f"https://doi.org/{doi}" if doi else ""),
        authors=authors,
        affiliations=affs,
        cited_by_count=int(cited) if str(cited).isdigit() else 0,
        topics=list(r.get("categories") or []),
        extra={
            "lang": lang,
            "language_raw": (r.get("language") or "").strip(),
            "country": "KR",
            "title_native": native,
            "title_en": title_en,
            "categories": list(r.get("categories") or []),
            "kci_article_id": r.get("article_id") or "",
            "uci": r.get("uci") or "",
            "oai_identifier": r.get("identifier") or "",
            "oai_datestamp": r.get("datestamp") or "",
            "issn": [x for x in (r.get("pissn"), r.get("eissn")) if x],
            "publisher": r.get("publisher") or "",
            "volume": r.get("volume") or "",
            "issue": r.get("issue") or "",
            "pages": pages,
        },
    )


def fetch(
    http: Http,
    since: str,
    until: str,
    keywords: Sequence[str] = (),
    categories: Sequence[str] = (),
    min_pub_year: Optional[int] = None,
    metadata_prefix: str = "oai_kci",
    max_pages: int = 10,
    stats: Optional[Dict[str, int]] = None,
) -> Iterator[Paper]:
    """Yield KCI articles processed in [since, until] that pass the local filters.

    `keywords` (case- and accent-insensitive substrings of title + abstract, any language; see
    `script_lang.fold_text`), `categories` (KCI subject strings such as 의학 or 생물학), `min_pub_year`
    (default: window year - 1) drop the back-catalogue noise described in the module docstring.
    `stats` receives counters: records, kept.
    """
    _pace(http)
    min_year = int(since[:4]) - 1 if min_pub_year is None else int(min_pub_year)
    kws = [fold_text(k) for k in keywords if k]
    cats = {c for c in categories if c}
    counters = stats if stats is not None else {}
    counters.setdefault("records", 0)
    counters.setdefault("kept", 0)
    for r in list_records(http, since, until, metadata_prefix=metadata_prefix, max_pages=max_pages):
        counters["records"] += 1
        y = r.get("pub_year") or ""
        if y.isdigit() and int(y) < min_year:
            continue
        if cats and not (set(r.get("categories") or []) & cats):
            continue
        if kws:
            hay = fold_text(" ".join([r.get("title_original") or "", r.get("title_english") or "", r.get("abstract_english") or "", r.get("abstract_original") or ""]))
            if not any(k in hay for k in kws):
                continue
        counters["kept"] += 1
        yield record_to_paper(r)
