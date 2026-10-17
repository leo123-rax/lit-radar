"""SciELO (Latin America, Spain, Portugal, South Africa) via the ArticleMeta REST API.

Verified live on 2026-10-17. API root: https://articlemeta.scielo.org/ ("Articles Metadata API").
The ArticleMeta project documentation linked from https://scielo.readthedocs.io/en/latest/ (Developer APIs ->
ArticleMeta RESTful API) returned HTTP 404 that day, so every call below was established by live requests.

Base: https://articlemeta.scielo.org/api/v1
  GET /collection/identifiers/
      -> 36 collections: [{code, acron, name{en,...}, status (certified|development|diffusion|independent),
         type (journals|books|preprints|repositories), is_active}], e.g. scl Brazil, mex Mexico, arg Argentina,
         chl Chile, col Colombia, per Peru, cub Cuba, esp Spain, prt Portugal, sza South Africa, spa Public Health.
  GET /article/identifiers/?collection=scl&from=2026-09-17&until=2026-10-17&limit=1000&offset=0
      -> {"meta": {"limit", "offset", "filter", "total"}, "objects": [{"code", "collection", "processing_date", "doi"}]}
         `limit` is capped at 1,000 server-side (limit=5000 answers limit 1000); `collection` may be omitted to
         sweep every collection; from/until filter processing_date = the day the record entered or was
         reprocessed by SciELO, NOT the publication date (a 2024 article can carry a 2026 processing_date).
         30-day totals on 2026-10-17: all collections 4,186; scl 2,055; sza 650; arg 360; col 208; chl 164;
         per 131; esp 31; mex/cub/ven 0.
  GET /article/?collection=scl&code=S2317-15372026000101013&format=opac
      -> one document as JSON (~25 KB). format=json or xylose add `citations` (~170 KB); xmlrsps is JATS/SPS XML
         (~32 KB); xmlwos and xmlcrossref are deposit XML. An unknown code answers HTTP 200 with body `null`.
  GET /journal/?collection=scl&issn=2317-1537   -> journal record (v100 title, v441 subject areas, v350 languages,
         v310 country, v435 ISSNs). GET /journal/identifiers/?collection=scl -> 427 journals in scl.

Document shape (ISIS field notation "vNN": [{"_": value, "<subkey>": ...}], top level + "article" + "title"):
  top: code (SciELO PID "S" + ISSN + year + issue + order), collection, doi, processing_date, publication_date (year),
       publication_year, document_type (research-article, ...), fulltexts {pdf, html}{lang: url}, created_at, updated_at
  article.v12 titles [{l: lang, _: text}]      article.v83 abstracts [{a: text, l: lang}]   article.v85 keywords [{k, l}]
  article.v10 authors [{n: given names, s: surname, k: ORCID, 1: "aff1 aff2", r: role}]
  article.v70 affiliations [{i: id, _: institution, 1 / 2: divisions, c: city, s: state, p: country}]
  article.v240 normalised affiliations [{i, _, p: ISO country}]   article.v58 funding agencies   article.v102 acknowledgements
  article.v40 original language   article.v65 issue date YYYYMMDD (month/day may be 00)   article.v223 online (real) date
  article.v265 [{k: real|expected, v: date}]   article.v91 processing date   article.v237 DOI   article.v35 ISSN
  article.v100 journal   article.v30 abbreviation   article.v62 publisher   article.v880 PID   article.v71 document type code
  title.v441 subject areas: Health Sciences, Biological Sciences, Agricultural Sciences, Exact and Earth Sciences,
       Engineering, Human Sciences, Applied Social Sciences, Linguistics, Letters and Arts
  title.v854 Web of Science categories (e.g. PLANT SCIENCES)   title.v350 languages   title.v310 country   title.v690 site host

Not usable (tested 2026-10-17): search.scielo.org answers HTTP 403 with a JavaScript proof-of-work challenge even to a
browser User-Agent; www.scielo.br/oai/scielo-oai.php returns the HTML site (and /oai/ a 502); articlemeta.scielo.org
has no OAI-PMH (404); SciELO Preprints OAI-PMH (https://preprints.scielo.org/index.php/scielo/oai) answers Identify and
a bare ListIdentifiers (500 headers/page) but HTTP 500 to any from/until filter.

Window strategy: there is no server-side search, so `fetch` lists identifiers by processing_date, looks the journal up
once per ISSN (derived from the PID) to keep only the configured subject areas, fetches each remaining article and
applies the keyword filter locally. Expect roughly 150-500 article fetches per week for the life-science areas.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..http import Http
from ..models import Author, Paper, normalize_doi

log = logging.getLogger(__name__)
BASE = "https://articlemeta.scielo.org/api/v1"
MIN_INTERVAL = 0.25
PAGE_LIMIT = 1000  # server cap on /article/identifiers/
LIFE_SCIENCE_AREAS = ("Health Sciences", "Biological Sciences", "Agricultural Sciences")
_PID_RE = re.compile(r"^S(\d{4}-\d{3}[\dXx])")
_DATE8 = re.compile(r"^\d{8}$")
_DATE10 = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _pace(http: Http) -> None:
    mi = getattr(http, "min_interval", None)
    if isinstance(mi, dict):
        mi.setdefault("articlemeta.scielo.org", MIN_INTERVAL)


# ---- raw API calls -------------------------------------------------------------------------
def collections(http: Http) -> List[Dict[str, Any]]:
    data = http.get_json(f"{BASE}/collection/identifiers/")
    return data if isinstance(data, list) else []


def identifiers(http: Http, since: str, until: str, collection: Optional[str] = None, max_records: int = 50000) -> Iterator[Dict[str, Any]]:
    """Page through /article/identifiers/ (processing_date in [since, until]); omit `collection` for all."""
    offset = 0
    while offset < max_records:
        params: Dict[str, Any] = {"from": since, "until": until, "limit": min(PAGE_LIMIT, max_records - offset), "offset": offset}
        if collection:
            params["collection"] = collection
        data = http.get_json(f"{BASE}/article/identifiers/", params=params)
        if not isinstance(data, dict):
            break
        objs = data.get("objects") or []
        yield from objs
        total = int((data.get("meta") or {}).get("total") or 0)
        offset += len(objs)
        if not objs or offset >= total:
            break


def article(http: Http, collection: str, code: str, fmt: str = "opac") -> Optional[Dict[str, Any]]:
    data = http.get_json(f"{BASE}/article/", params={"collection": collection, "code": code, "format": fmt})
    return data if isinstance(data, dict) else None


def journal(http: Http, collection: str, issn: str) -> Optional[Dict[str, Any]]:
    data = http.get_json(f"{BASE}/journal/", params={"collection": collection, "issn": issn})
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


# ---- helpers --------------------------------------------------------------------------------
def issn_from_code(code: str) -> str:
    """The SciELO PID starts with 'S' + the journal's ISSN: S2317-1537 2026 0001 01013."""
    m = _PID_RE.match(code or "")
    return m.group(1).upper() if m else ""


def _vals(rec: Dict[str, Any], field: str, sub: str = "_") -> List[str]:
    out = []
    for item in rec.get(field) or []:
        if isinstance(item, dict) and item.get(sub):
            out.append(str(item[sub]).strip())
    return out


def _first(rec: Dict[str, Any], field: str, sub: str = "_") -> str:
    v = _vals(rec, field, sub)
    return v[0] if v else ""


def subject_areas_of(journal_doc: Optional[Dict[str, Any]]) -> List[str]:
    if not journal_doc:
        return []
    return _vals(journal_doc, "v441")


def _fmt8(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def pub_date_of(doc: Dict[str, Any]) -> Tuple[str, str]:
    """Best publication date and its basis: online (v223 / v265 real) > issue (v65) > processing > year."""
    art = doc.get("article") or {}
    real = _first(art, "v223")
    if not real:
        for item in art.get("v265") or []:
            if isinstance(item, dict) and item.get("k") == "real" and item.get("v"):
                real = str(item["v"])
                break
    if _DATE8.match(real) and real[4:6] != "00" and real[6:8] != "00":
        return _fmt8(real), "online"
    v65 = _first(art, "v65")
    if _DATE8.match(v65) and v65[4:6] != "00":
        return (_fmt8(v65) if v65[6:8] != "00" else f"{v65[:4]}-{v65[4:6]}-01"), "issue"
    pd = doc.get("processing_date") or _first(art, "v91") or ""
    if _DATE10.match(pd):
        return pd, "processing"
    if _DATE8.match(pd):
        return _fmt8(pd), "processing"
    year = str(doc.get("publication_year") or v65[:4] or "")
    return (f"{year}-01-01", "year") if year.isdigit() and len(year) == 4 else ("", "")


def to_paper(doc: Dict[str, Any]) -> Paper:
    art = doc.get("article") or {}
    tdoc = doc.get("title") or {}
    code = doc.get("code") or _first(art, "v880")
    collection = doc.get("collection") or _first(art, "v992")
    doi = normalize_doi(doc.get("doi") or _first(art, "v237"))
    orig_lang = (_first(art, "v40") or "").lower()
    titles: Dict[str, str] = {}
    for t in art.get("v12") or []:
        if isinstance(t, dict) and t.get("_"):
            titles.setdefault((t.get("l") or orig_lang or "und").lower(), str(t["_"]).strip())
    abstracts: Dict[str, str] = {}
    for a in art.get("v83") or []:
        if isinstance(a, dict) and (a.get("a") or a.get("_")):
            abstracts.setdefault((a.get("l") or orig_lang or "und").lower(), str(a.get("a") or a.get("_")).strip())
    title = titles.get("en") or titles.get(orig_lang) or next(iter(titles.values()), "")
    native = titles.get(orig_lang) or next(iter(titles.values()), "") or title
    abstract = abstracts.get("en") or abstracts.get(orig_lang) or next(iter(abstracts.values()), "")
    affs_by_id: Dict[str, str] = {}
    affs: List[str] = []
    for a in art.get("v70") or []:
        if not isinstance(a, dict):
            continue
        s = ", ".join(str(a[k]).strip() for k in ("_", "1", "2", "c", "s", "p") if a.get(k))
        if not s:
            continue
        if a.get("i"):
            affs_by_id[str(a["i"])] = s
        affs.append(s)
    authors: List[Author] = []
    for a in art.get("v10") or []:
        if not isinstance(a, dict):
            continue
        name = " ".join(str(a[k]).strip() for k in ("n", "s") if a.get(k)) or str(a.get("_") or "").strip()
        if not name:
            continue
        ids = str(a.get("1") or "").split()
        authors.append(Author(name=name, affiliation="; ".join(affs_by_id[i] for i in ids if i in affs_by_id), orcid=str(a.get("k") or "")))
    funders = _vals(art, "v58")
    ack = _first(art, "v102")
    keywords = [k.strip() for k in _vals(art, "v85", "k") if k.strip()]
    areas = _vals(tdoc, "v441")
    wos = _vals(tdoc, "v854")
    pub_date, basis = pub_date_of(doc)
    html_urls = ((doc.get("fulltexts") or {}).get("html") or {}) if isinstance(doc.get("fulltexts"), dict) else {}
    url = ""
    if doi:
        url = f"https://doi.org/{doi}"
    elif html_urls:
        url = html_urls.get("en") or next(iter(html_urls.values()), "")
    elif _first(tdoc, "v690") and code:
        url = f"https://{_first(tdoc, 'v690')}/scielo.php?script=sci_arttext&pid={code}"
    p = Paper(
        id=Paper.make_id("scielo", code or title, doi),
        source="scielo",
        kind="article",
        title=title,
        doi=doi,
        abstract=abstract,
        journal=_first(art, "v100") or _first(tdoc, "v100"),
        pub_date=pub_date,
        url=url,
        category=doc.get("document_type") or "",
        authors=authors,
        affiliations=list(dict.fromkeys(affs)),
        topics=list(dict.fromkeys(areas + wos)),
        extra={
            "lang": orig_lang or next(iter(titles), "und"),
            "country": _first(tdoc, "v310"),
            "collection": collection,
            "scielo_pid": code,
            "title_native": native,
            "title_en": titles.get("en", ""),
            "titles": titles,
            "abstract_langs": sorted(abstracts),
            "keywords": keywords,
            "subject_areas": areas,
            "wos_categories": wos,
            "processing_date": doc.get("processing_date") or "",
            "pub_date_basis": basis,
            "document_type": doc.get("document_type") or "",
            "issn": list(dict.fromkeys(x for x in [_first(art, "v35")] + _vals(tdoc, "v435") if x)),
            "publisher": _first(art, "v62") or _first(tdoc, "v480"),
            "fulltext_html": html_urls,
        },
    )
    if funders:
        p.sections["funding"] = "; ".join(funders)
    if ack:
        p.sections["ack"] = ack
    return p


def fetch(
    http: Http,
    since: str,
    until: str,
    collections: Optional[Sequence[str]] = None,
    subject_areas: Optional[Sequence[str]] = LIFE_SCIENCE_AREAS,
    keywords: Sequence[str] = (),
    max_articles: int = 2000,
    fmt: str = "opac",
    stats: Optional[Dict[str, int]] = None,
) -> Iterator[Paper]:
    """Yield SciELO articles processed in [since, until] whose journal is in `subject_areas` (None/empty = all)
    and whose title, abstract or keywords contain any of `keywords` (empty = all). One request per identifier
    page, one per distinct journal ISSN, one per article; `max_articles` caps the article fetches.
    `stats` receives counters: identifiers, skipped_subject, fetched, missing, kept, capped."""
    _pace(http)
    areas = {a for a in (subject_areas or ()) if a}
    kws = [k.lower() for k in keywords if k]
    counters = stats if stats is not None else {}
    for k in ("identifiers", "skipped_subject", "fetched", "missing", "kept", "capped"):
        counters.setdefault(k, 0)
    journal_cache: Dict[Tuple[str, str], Optional[List[str]]] = {}
    for col in (list(collections) if collections else [None]):
        for ident in identifiers(http, since, until, collection=col):
            counters["identifiers"] += 1
            code = ident.get("code") or ""
            c = ident.get("collection") or col or ""
            if not code or not c:
                continue
            if areas:
                issn = issn_from_code(code)
                key = (c, issn)
                if key not in journal_cache:
                    jd = journal(http, c, issn) if issn else None
                    journal_cache[key] = subject_areas_of(jd) if jd else None
                known = journal_cache[key]
                if known is not None and not (set(known) & areas):
                    counters["skipped_subject"] += 1
                    continue
            if counters["fetched"] >= max_articles:
                counters["capped"] += 1
                continue
            counters["fetched"] += 1
            doc = article(http, c, code, fmt=fmt)
            if not doc:
                counters["missing"] += 1
                continue
            p = to_paper(doc)
            if kws:
                hay = " ".join([p.title, p.abstract] + list(p.extra.get("titles", {}).values()) + p.extra.get("keywords", [])).lower()
                if not any(k in hay for k in kws):
                    continue
            counters["kept"] += 1
            yield p
    if counters["capped"]:
        log.warning("SciELO: %d identifiers not fetched (max_articles=%d)", counters["capped"], max_articles)
    log.info("SciELO %s..%s: %s", since, until, dict(counters))
