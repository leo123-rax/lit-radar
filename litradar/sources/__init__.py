"""Source adapters. Each exposes functions that yield Paper / ArchiveRecord / NewsItem objects.

Adding a source: write a module here with a `fetch(...)` generator and register it in
litradar/pipeline.py. Keep adapters dumb: no matching, no scoring, just normalised records.

Two helpers shared by several adapters live here so they do not import each other:
``clean_text`` (publisher metadata carries JATS/HTML markup and multi-line whitespace in titles) and
``jats_abstract`` (the abstract inside a JATS full-text document, used when the search record had none).
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Optional, Union

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value: Optional[str]) -> str:
    """Plain text from a metadata string: tags stripped (``<i>de novo</i>`` -> ``de novo``), entities decoded,
    whitespace collapsed. Idempotent on clean input."""
    s = value or ""
    if "<" in s or "&" in s:
        s = html.unescape(s)
        s = _TAG_RE.sub(" ", s)
        s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def jats_abstract(xml: Union[bytes, str, None]) -> str:
    """The article abstract from a JATS document as plain text ('' when absent or unparseable).

    The first ``<abstract>`` without an ``abstract-type`` of graphical/teaser/summary wins (the ``<front>``
    one in practice); section titles inside structured abstracts are kept as sentence lead-ins."""
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml if isinstance(xml, bytes) else xml.encode("utf-8"))
    except ET.ParseError:
        return ""
    for el in root.iter():
        if _local(el.tag) != "abstract":
            continue
        if (el.get("abstract-type") or "").lower() in ("graphical", "teaser", "toc", "author-highlights", "key-points"):
            continue
        text = _WS_RE.sub(" ", " ".join(t for t in el.itertext() if t)).strip()
        if text:
            return text
    return ""
