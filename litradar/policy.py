"""Data policy: what the radar may store from a given source.

The rule, in one sentence: metadata and abstracts are always stored; full text (Methods, funding,
competing-interest sections and the body) is stored only when the licence allows redistribution
or the item is in an open-access subset; for everything else we keep only derived facts
(gazetteer mentions with short snippets) and a link.

Levels returned by fulltext_storage_level():
  'full'        store sections and body (CC-BY, CC-BY-NC, CC-BY-ND, CC-BY-NC-ND, CC0, public domain, PMC OA subset)
  'facts_only'  parse the text for mentions/snippets but store no sections or body (all-rights-reserved preprints,
                author manuscripts, anything with an unknown licence that we could still read)
  'none'        do not fetch at all (paywalled; the source gave us no text)
"""
from __future__ import annotations

from typing import Optional

PERMISSIVE_PREFIXES = ("cc_by", "cc-by", "cc by", "cc0", "cc_0", "cc-0", "public domain", "publicdomain", "pd")
RESTRICTIVE_TOKENS = ("cc_no", "all rights reserved", "no reuse", "copyright", "proprietary")


def normalize_license(license_str: Optional[str]) -> str:
    s = (license_str or "").strip().lower()
    if s.startswith("http"):
        # e.g. https://creativecommons.org/licenses/by-nc/4.0/
        s = s.rstrip("/").split("creativecommons.org/licenses/")[-1].replace("/", "-")
        if "publicdomain" in license_str.lower():
            return "cc0"
        s = "cc-" + s if not s.startswith("cc") else s
    return s.replace(" ", "_")


def fulltext_storage_level(license_str: Optional[str], open_access: Optional[bool] = None, text_available: bool = True) -> str:
    """Decide how much of an item's full text may be kept.

    license_str: bioRxiv values (cc_by, cc_by_nc, cc_by_nc_nd, cc0, cc_no) or a CC URL / SPDX-ish string.
    open_access: Europe PMC isOpenAccess == 'Y' (PMC Open Access subset) when known.
    """
    if not text_available:
        return "none"
    lic = normalize_license(license_str)
    if lic:
        if any(lic.startswith(p.replace(" ", "_")) for p in PERMISSIVE_PREFIXES):
            return "full"
        if any(tok.replace(" ", "_") in lic for tok in RESTRICTIVE_TOKENS):
            return "facts_only"
    if open_access:
        return "full"
    return "facts_only"
