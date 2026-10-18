"""Pull the sections we care about out of JATS XML (bioRxiv, Europe PMC / PMC full text).

Returned dict keys: methods, funding, coi, ack, data_availability, abstract (the article's own
<abstract>, for records whose metadata carried none), plus authors (list of dicts), affiliations
(list of str), emails (list of str) and body (all body text, for FTS).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from lxml import etree

METHODS_RE = re.compile(r"method|materials|experimental procedure|star\W*methods|online methods|protocol", re.I)
FUNDING_RE = re.compile(r"funding|financial support|grant support", re.I)
COI_RE = re.compile(r"competing|conflict|declaration of interest|disclosure", re.I)
DATA_RE = re.compile(r"data (and|&) (code|materials) availability|data availability|availability of data|code availability", re.I)
ACK_RE = re.compile(r"acknowledg", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _text(el: Any) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", " ".join(el.itertext())).strip()


def _strip_ns(root: Any) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


_SKIPPED_ABSTRACT_TYPES = ("graphical", "teaser", "toc", "video", "web-summary", "key-points")
_ABSTRACT_LEAD_RE = re.compile(r"^(abstract|summary)\s*[:.\-]?\s*", re.I)


def extract_abstract(root: Any) -> str:
    """The article's abstract as plain text: the first plain <abstract> in <front>/<article-meta> (graphical,
    teaser and similar variants are skipped; sub-article abstracts are ignored), with a leading 'Abstract' label
    removed and whitespace collapsed. '' when the document has none."""
    meta = root.find("front/article-meta")
    scope = meta if meta is not None else root
    fallback = ""
    for ab in scope.iter("abstract"):
        atype = (ab.get("abstract-type") or "").lower()
        txt = _ABSTRACT_LEAD_RE.sub("", _text(ab))
        if not txt:
            continue
        if atype in _SKIPPED_ABSTRACT_TYPES:
            fallback = fallback or txt
            continue
        return txt
    return fallback


def parse_jats(xml: bytes) -> Dict[str, Any]:
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(xml, parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        return {}
    if root is None:
        return {}
    _strip_ns(root)
    out: Dict[str, Any] = {"methods": "", "funding": "", "coi": "", "ack": "", "data_availability": "", "body": "", "abstract": ""}
    out["abstract"] = extract_abstract(root)

    # ---- sections by title ----------------------------------------------------------------
    buckets: Dict[str, List[str]] = {"methods": [], "funding": [], "coi": [], "data_availability": [], "ack": []}
    for sec in root.iter("sec"):
        title_el = sec.find("title")
        title = _text(title_el) if title_el is not None else ""
        sec_type = (sec.get("sec-type") or "").lower()
        label = f"{sec_type} {title}"
        txt = _text(sec)
        if not txt:
            continue
        if "methods" in sec_type or (METHODS_RE.search(title) and not FUNDING_RE.search(title)):
            # skip tiny nested subsections when the parent already matched (handled by dedup below)
            buckets["methods"].append(txt)
        elif FUNDING_RE.search(label):
            buckets["funding"].append(txt)
        elif COI_RE.search(label):
            buckets["coi"].append(txt)
        elif DATA_RE.search(label):
            buckets["data_availability"].append(txt)
        elif ACK_RE.search(label):
            buckets["ack"].append(txt)

    # explicit JATS elements
    for fg in root.iter("funding-group"):
        buckets["funding"].append(_text(fg))
    for fs in root.iter("funding-statement"):
        buckets["funding"].append(_text(fs))
    for fn in root.iter("fn"):
        ft = (fn.get("fn-type") or "").lower()
        if ft in ("conflict", "coi-statement", "competing-interests") or "conflict" in ft:
            buckets["coi"].append(_text(fn))
        elif "financial" in ft or "funding" in ft:
            buckets["funding"].append(_text(fn))
    for ack in root.iter("ack"):
        buckets["ack"].append(_text(ack))
    for notes in root.iter("notes"):
        nt = (notes.get("notes-type") or "").lower()
        if "coi" in nt or "competing" in nt or "conflict" in nt:
            buckets["coi"].append(_text(notes))
        elif "data" in nt and "avail" in nt:
            buckets["data_availability"].append(_text(notes))

    for key, chunks in buckets.items():
        # drop chunks fully contained in another chunk (nested <sec>)
        chunks = sorted(set(chunks), key=len, reverse=True)
        kept: List[str] = []
        for c in chunks:
            if not any(c in k for k in kept):
                kept.append(c)
        out[key] = "\n\n".join(kept)

    # ---- authors / affiliations / emails -------------------------------------------------
    authors: List[Dict[str, str]] = []
    aff_by_id: Dict[str, str] = {}
    for aff in root.iter("aff"):
        aid = aff.get("id") or ""
        txt = _text(aff)
        txt = re.sub(r"^\s*\d+\s*", "", txt)
        if aid:
            aff_by_id[aid] = txt
    affiliations = list(dict.fromkeys(a for a in aff_by_id.values() if a))
    for contrib in root.iter("contrib"):
        if (contrib.get("contrib-type") or "author").lower() != "author":
            continue
        surname = _text(contrib.find(".//surname"))
        given = _text(contrib.find(".//given-names"))
        name = f"{given} {surname}".strip() or _text(contrib.find(".//string-name")) or _text(contrib.find(".//collab"))
        if not name:
            continue
        affs = []
        for xref in contrib.findall("xref"):
            if (xref.get("ref-type") or "") == "aff" and xref.get("rid") in aff_by_id:
                affs.append(aff_by_id[xref.get("rid")])
        for aff in contrib.findall("aff"):
            t = _text(aff)
            if t:
                affs.append(t)
                if t not in affiliations:
                    affiliations.append(t)
        orcid = ""
        for cid in contrib.findall("contrib-id"):
            if (cid.get("contrib-id-type") or "").lower() == "orcid":
                orcid = _text(cid)
        email = _text(contrib.find(".//email"))
        authors.append({"name": name, "affiliation": "; ".join(affs), "orcid": orcid, "email": email})

    body_el = root.find(".//body")
    out["body"] = _text(body_el)
    full_text = _text(root)
    out["emails"] = sorted(set(EMAIL_RE.findall(full_text)))
    out["authors"] = authors
    out["affiliations"] = affiliations
    return out
