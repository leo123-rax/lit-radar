#!/usr/bin/env python3
"""Headlines for a list of companies from Google News RSS and SEC EDGAR 8-K feeds. No pipeline, no database.

    python examples/company_news.py --company "Example Genomics" --company "Example Diagnostics:EXDX" --days 7
    python examples/company_news.py --company "Example Instruments" --context "(proteomics OR mass spectrometry)"

Give each company as "Name" or "Name:TICKER". Google News is queried as "<name>" plus a context clause so
bare names do not pull unrelated stories; a US ticker adds that company's 8-K filings from the SEC EDGAR
Atom feed. The SEC requires a contact in the User-Agent, so set LITRADAR_CONTACT_EMAIL or the EDGAR feed
is skipped (and said so). Several trade outlets block direct feed access (HTTP 403); their headlines
arrive through Google News instead. See docs/SOURCES.md for what works and what does not.
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import date, timedelta
from urllib.parse import quote_plus

try:
    from litradar.http import Http
    from litradar.sources import rss
except ImportError:  # pragma: no cover - the examples only need the package importable
    raise SystemExit("lit-radar is not installed in this interpreter: run `pip install -e .` from the repository root (or `pip install git+<repo-url>`)") from None

DEFAULT_CONTEXT = '(sequencing OR genomics OR proteomics OR "life sciences" OR instrument OR biotech)'
GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
EDGAR = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=8-K&dateb=&owner=include&count=20&output=atom"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--company", action="append", required=True, help='"Name" or "Name:TICKER"; repeatable')
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--context", default=DEFAULT_CONTEXT, help="clause appended to each Google News query")
    ap.add_argument("--limit", type=int, default=10, help="headlines per company per feed")
    args = ap.parse_args()

    since = (date.today() - timedelta(days=args.days)).isoformat()
    contact = os.environ.get("LITRADAR_CONTACT_EMAIL", "")
    http = Http(contact_email=contact)
    if not contact:
        print("note: LITRADAR_CONTACT_EMAIL is not set; SEC EDGAR feeds are skipped (the SEC answers 403 without a contact)\n")

    for spec in args.company:
        name, _, ticker = spec.partition(":")
        name, ticker = name.strip(), ticker.strip().upper()
        print(f"== {name}" + (f" ({ticker})" if ticker else ""))
        url = GNEWS.format(q=quote_plus(f'"{name}" {args.context}'))
        shown = 0
        for item in rss.news_items(http, f"news:{name}", url, since=since):
            if name.lower() not in f"{item.title} {item.summary}".lower():
                continue  # a query that does not actually name the company (common with short names)
            print(f"  {item.published or 'n.d.'}  {item.title}\n      {item.url}")
            shown += 1
            if shown >= args.limit:
                break
        if not shown:
            print("  (no Google News headlines in the window)")
        if ticker and contact and re.fullmatch(r"[A-Z]{1,5}", ticker):
            n = 0
            for item in rss.news_items(http, f"SEC 8-K {ticker}", EDGAR.format(ticker=ticker), since=since):
                print(f"  {item.published or 'n.d.'}  8-K: {item.title}\n      {item.url}")
                n += 1
                if n >= args.limit:
                    break
            if not n:
                print("  (no 8-K filings in the window)")
        print()
    print(f"http: {http.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
