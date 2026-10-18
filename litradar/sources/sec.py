"""SEC EDGAR 8-K Atom feeds for the watched companies: ticker -> CIK resolution and the feed URL.

EDGAR's legacy ``browse-edgar`` endpoint accepts ``CIK=<ticker>``, but it resolves the ticker through a table
that lags https://www.sec.gov/files/company_tickers.json: a recent listing or a renamed registrant comes back
as an HTTP 200 ``text/html`` page reading "No matching Ticker Symbol." (verified live 2026-10-18 for 8 of 46
watched tickers, all of them present in ``company_tickers.json``). The feeds are therefore requested by CIK,
looked up in ``company_tickers.json`` (downloaded and cached by ``litradar.track.sec_company_tickers``), and a
ticker that table does not know is reported in the run's error list instead of being requested.

The filings themselves are read by the generic Atom reader in ``litradar.sources.rss``; this module only
builds URLs, resolves tickers and recognises EDGAR's HTML answers.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HOST = "www.sec.gov"
BROWSE_URL = f"https://{HOST}/cgi-bin/browse-edgar"
# a plain US listing; foreign suffixes (ROG.SW, SRT.DE) and placeholders (private, n/a) never get a feed
TICKER_RE = re.compile(r"[A-Z]{1,5}")
# the one-line explanations EDGAR's HTML answer carries (verified live 2026-10-18 for the first two)
_EDGAR_MESSAGE_RE = re.compile(r"No matching (?:Ticker Symbol|CIK)\.?|Invalid CIK\.?|No records found\.?", re.I)


def is_plain_ticker(ticker: Optional[str]) -> bool:
    """True for a 1-5 upper-case-letter US ticker, the only kind an EDGAR feed is generated for."""
    return bool(TICKER_RE.fullmatch(ticker or ""))


def pad_cik(cik: object) -> str:
    """EDGAR's 10-digit CIK form ('1717115' -> '0001717115'); non-numeric input is returned stripped."""
    s = str(cik or "").strip()
    return s.zfill(10) if s.isdigit() else s


def feed_url(cik_or_ticker: str, form: str = "8-K", count: int = 20) -> str:
    """The Atom feed of one registrant's filings of one form type. Pass a CIK (padded or not; EDGAR accepts
    both); a ticker resolves only when EDGAR's legacy ticker table knows it."""
    return f"{BROWSE_URL}?action=getcompany&CIK={cik_or_ticker}&type={form}&dateb=&owner=include&count={count}&output=atom"


def cik_by_ticker(rows: Iterable[Mapping[str, object]]) -> Dict[str, str]:
    """{TICKER: 10-digit CIK} from ``company_tickers.json`` rows, in either shape: ``[{cik, ticker, title}]`` as
    ``track.sec_company_tickers`` returns them, or the SEC's own ``{cik_str, ticker, title}`` records."""
    out: Dict[str, str] = {}
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        ticker = str(r.get("ticker") or "").strip().upper()
        cik = r.get("cik") if r.get("cik") not in (None, "") else r.get("cik_str")
        if ticker and cik is not None and str(cik).strip().isdigit():
            out.setdefault(ticker, pad_cik(cik))
    return out


def resolve(tickers: Sequence[str], rows: Iterable[Mapping[str, object]]) -> Tuple[Dict[str, str], List[str]]:
    """``({ticker: CIK} for the tickers the table knows, sorted [tickers it does not])``."""
    table = cik_by_ticker(rows)
    resolved = {t: table[t.upper()] for t in tickers if t.upper() in table}
    unresolved = sorted({t for t in tickers if t.upper() not in table})
    return resolved, unresolved


def error_message(body: Optional[str]) -> str:
    """EDGAR's one-line explanation inside an HTML answer ('No matching Ticker Symbol.', 'No matching CIK.'),
    '' when the body carries none of the known ones."""
    m = _EDGAR_MESSAGE_RE.search(body or "")
    return m.group(0) if m else ""
