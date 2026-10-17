#!/usr/bin/env python3
"""Open-access papers whose Methods section names a term, with the sentence that names it. Europe PMC only, no pipeline.

    python examples/methods_search.py "NovaSeq X"                       # last 7 days, open-access, METHODS section
    python examples/methods_search.py Xenium "Visium HD" --days 30 --limit 10
    python examples/methods_search.py "Orbitrap Astral" --field BODY --no-fulltext

Europe PMC indexes the open-access subset section by section, so `METHODS:"NovaSeq X"` is answered
server-side; nothing is downloaded to find the papers. With full text on (default), each hit's JATS XML
is fetched once (cached on disk under .cache/) and the first sentence that names the term is printed.
Phrase search is prefix-inclusive at the token level ("NovaSeq X" also matches "NovaSeq X Plus").
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import date, timedelta

try:
    from litradar.http import Http
    from litradar.sources import europepmc
except ImportError:  # pragma: no cover - the examples only need the package importable
    raise SystemExit("lit-radar is not installed in this interpreter: run `pip install -e .` from the repository root (or `pip install git+<repo-url>`)") from None


def sentence_with(text: str, term: str, width: int = 240) -> str:
    """The sentence (or a window) around the first case-insensitive occurrence of `term`."""
    m = re.search(re.escape(term), text, re.I)
    if not m:
        return ""
    start = max(text.rfind(". ", 0, m.start()) + 2, m.start() - width // 2)
    end = text.find(". ", m.end())
    end = m.end() + width // 2 if end < 0 else min(end + 1, m.end() + width)
    return " ".join(text[start:end].split())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("terms", nargs="+", help="one or more phrases; OR-ed")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--field", default="METHODS", help="METHODS (default), BODY, COMP_INT, ACK_FUND, AFF or ABSTRACT")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--all-access", action="store_true", help="do not restrict to the open-access subset (full text then rarely available)")
    ap.add_argument("--no-fulltext", action="store_true", help="list the hits without downloading their XML")
    args = ap.parse_args()

    until = date.today()
    since = until - timedelta(days=args.days)
    http = Http(contact_email=os.environ.get("LITRADAR_CONTACT_EMAIL", ""), cache_dir=".cache")

    query = europepmc.section_query(args.field.upper(), args.terms, since.isoformat(), until.isoformat(), open_access_only=not args.all_access)
    total = europepmc.hit_count(http, query)
    print(f"query: {query}\nhits:  {total:,} (showing up to {args.limit})\n")

    for rec in europepmc.search(http, query, max_records=args.limit):
        paper = europepmc.to_paper(rec)
        line = f"{paper.pub_date or 'n.d.'}  {paper.title[:110]}\n    {paper.journal}  {paper.url}"
        if not args.no_fulltext and europepmc.attach_fulltext(http, paper):
            section = paper.sections.get("methods") or paper.sections.get("body") or ""
            for term in args.terms:
                snippet = sentence_with(section, term)
                if snippet:
                    line += f"\n    [{term}] ...{snippet}..."
                    break
        print(line)
    print(f"\nhttp: {http.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
