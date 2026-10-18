"""Company intelligence over the store: profiles, window counts, CSV export and the co-mention graph.

The gazetteer (``config/companies.yaml``) says which companies exist; the ``mentions`` table says where
they appear. This module joins the two for one company at a time (``company_detail``), for every company
at once (``list_companies``, ``export_rows``) and for the pairs that share papers (``comention_graph``).
It reads ``papers``, ``mentions``, ``news_items`` and, when the table exists, ``candidate_companies``;
it never writes.

Roles follow the sections the extractor recorded a mention in:

* ``author``       ``affiliations``                      a company author
* ``funding_coi``  ``funding``, ``coi``, ``ack``         sponsor, employer or consulting relationship declared
* ``subject``      ``title``, ``abstract``               the paper is about the company or its product
* ``supplier``     ``methods``                           the company's product was used
* ``other``        anything else (``body``, ``data_availability``)

A paper is dated by ``pub_date``, falling back to the date part of ``first_seen`` — the rule the digest and
the dashboard already share, so every surface agrees on which papers fall in a window.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, TextIO, Tuple

from . import discovery

ROLES = ("author", "funding_coi", "subject", "supplier", "other")
ROLE_OF_SECTION = {
    "affiliations": "author",
    "funding": "funding_coi", "coi": "funding_coi", "ack": "funding_coi",
    "title": "subject", "abstract": "subject",
    "methods": "supplier",
}
ROLE_LABELS = {"author": "Author affiliation", "funding_coi": "Funding / COI", "subject": "Subject of the work",
               "supplier": "Supplier (Methods)", "other": "Other"}
TICKER_PLACEHOLDERS = {"", "private", "n/a", "na", "none", "null", "-", "--", "?"}
COMPANY_TYPES = ("tools", "diagnostics", "synthesis", "ai_bio", "pharma", "cro", "institute", "software")
DEFAULT_WINDOWS = (90, 365)
MONTHS_OF_HISTORY = 24
UNLISTED_PREFIX = "unlisted:"

EXPORT_COLUMNS = (
    "id", "name", "ticker", "public", "type", "parent", "papers_90d", "mentions_90d", "author_90d",
    "funding_coi_90d", "subject_90d", "supplier_90d", "other_90d", "news_90d", "candidates_linked",
    "last_paper", "domains", "aliases",
)

_ROLE_CASE = "CASE m.section " + " ".join(f"WHEN '{s}' THEN '{r}'" for s, r in ROLE_OF_SECTION.items()) + " ELSE 'other' END"


# ------------------------------------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------------------------------------


def is_public(ticker: Any) -> bool:
    """True when the gazetteer ticker names a listing rather than a placeholder (``private``, ``n/a``, ...)."""
    return str(ticker or "").strip().lower() not in TICKER_PLACEHOLDERS


def date_sql(alias: str = "p") -> str:
    """The date a paper is bucketed by: ``pub_date``, else the date part of ``first_seen``."""
    return f"COALESCE(NULLIF({alias}.pub_date,''), substr({alias}.first_seen,1,10))"


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def window(days: int, today: Optional[date] = None) -> Tuple[str, str]:
    """``(since, until)`` ISO dates for the last `days` days ending today (inclusive)."""
    t = today or today_utc()
    return (t - timedelta(days=int(days))).isoformat(), t.isoformat()


def month_axis(end: Optional[date] = None, months: int = MONTHS_OF_HISTORY) -> List[str]:
    """``['2024-11', ..., '2026-10']``: the last `months` calendar months ending with the month of `end`."""
    t = end or today_utc()
    y, m = t.year, t.month
    out: List[str] = []
    for _ in range(max(1, int(months))):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def page_slug(entity_id: str) -> str:
    """A file-system-safe slug for a company id (``companies/<slug>.html``)."""
    s = re.sub(r"[^a-z0-9._-]+", "-", str(entity_id or "").lower()).strip("-.")
    return s or "company"


def _conn(store_or_conn) -> sqlite3.Connection:
    return getattr(store_or_conn, "conn", store_or_conn)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


# ------------------------------------------------------------------------------------------------
# gazetteer entries
# ------------------------------------------------------------------------------------------------


def normalize_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
    """One gazetteer entry with every field present and typed (lists are lists, flags are bools)."""
    name = str(raw.get("name") or raw.get("id") or "")
    ticker = str(raw.get("ticker") or "private").strip() or "private"
    return {
        "id": str(raw.get("id") or discovery.slugify(name)),
        "name": name,
        "ticker": ticker,
        "public": is_public(ticker),
        "type": str(raw.get("type") or ""),
        "parent": str(raw.get("parent") or ""),
        "watch_news": bool(raw.get("watch_news", False)),
        "domains": [str(d).lower() for d in (raw.get("domains") or []) if d],
        "aliases": [str(a) for a in (raw.get("aliases") or []) if a],
        "notes": str(raw.get("notes") or ""),
    }


def load_entries(source: Any) -> List[Dict[str, Any]]:
    """Gazetteer entries from a ``Settings`` (reads ``companies.yaml``), a parsed YAML dict or a list of dicts."""
    data = source
    if hasattr(source, "yaml"):
        data = source.yaml("companies")
    items = data.get("entries") if isinstance(data, dict) else data
    return [normalize_entry(e) for e in (items or []) if isinstance(e, dict) and e.get("id")]


def resolve_entry(entries: Sequence[Dict[str, Any]], text: str) -> Optional[Dict[str, Any]]:
    """Entry by id, name, alias or ticker (case-insensitive); unique substring of id or name as a last resort."""
    t = (text or "").strip().lower()
    if not t:
        return None
    for e in entries:
        if e["id"].lower() == t or e["name"].lower() == t:
            return e
    for e in entries:
        if any(a.lower() == t for a in e["aliases"]):
            return e
    for e in entries:
        if e["public"] and e["ticker"].lower() == t:
            return e
    hits = [e for e in entries if t in e["id"].lower() or t in e["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise LookupError("ambiguous company; matches: " + ", ".join(sorted(h["id"] for h in hits)[:10]))
    return None


def entries_from_store(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Minimal entries for every company id the mentions table knows (name and ticker as stored).

    Lets a consumer without the YAML (the dashboard reads only the database) still profile companies;
    ``type`` and ``domains`` are blank in that case. ``unlisted:`` ids are left out."""
    rows = conn.execute(
        "SELECT entity_id, MAX(entity_name), MAX(ticker), MAX(vendor) FROM mentions WHERE kind='company' GROUP BY entity_id"
    ).fetchall()
    out = []
    for eid, name, ticker, parent in rows:
        if str(eid).startswith(UNLISTED_PREFIX):
            continue
        out.append(normalize_entry({"id": eid, "name": name or eid, "ticker": ticker or "private", "parent": parent or ""}))
    out.sort(key=lambda e: e["name"].lower())
    return out


# ------------------------------------------------------------------------------------------------
# counts
# ------------------------------------------------------------------------------------------------


def _empty_roles() -> Dict[str, int]:
    return {r: 0 for r in ROLES}


def paper_counts(conn: sqlite3.Connection, since: str, until: str) -> Dict[str, Dict[str, Any]]:
    """Per company id in the window: distinct ``papers``, summed ``mentions``, distinct papers per role, ``last_paper``."""
    d = date_sql()
    out: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"papers": 0, "mentions": 0, "roles": _empty_roles(), "last_paper": ""})
    for eid, n, k, last in conn.execute(
        f"SELECT m.entity_id, COUNT(DISTINCT m.paper_id), SUM(m.count), MAX({d}) FROM mentions m JOIN papers p ON p.id=m.paper_id "
        f"WHERE m.kind='company' AND {d} >= ? AND {d} <= ? GROUP BY m.entity_id",
        (since, until),
    ).fetchall():
        out[eid].update({"papers": int(n or 0), "mentions": int(k or 0), "last_paper": last or ""})
    for eid, role, n in conn.execute(
        f"SELECT m.entity_id, {_ROLE_CASE} AS role, COUNT(DISTINCT m.paper_id) FROM mentions m JOIN papers p ON p.id=m.paper_id "
        f"WHERE m.kind='company' AND {d} >= ? AND {d} <= ? GROUP BY m.entity_id, role",
        (since, until),
    ).fetchall():
        out[eid]["roles"][role if role in ROLES else "other"] = int(n or 0)
    return dict(out)


def _news_date_sql() -> str:
    return "COALESCE(NULLIF(substr(published,1,10),''), substr(first_seen,1,10))"


def _news_sets(conn: sqlite3.Connection, since: str, until: str) -> Dict[str, Set[str]]:
    """News item ids per company key (the stored ``companies_json`` strings: gazetteer ids, sometimes names)."""
    if not _has_table(conn, "news_items"):
        return {}
    d = _news_date_sql()
    sets: Dict[str, Set[str]] = defaultdict(set)
    for nid, raw in conn.execute(f"SELECT id, companies_json FROM news_items WHERE {d} >= ? AND {d} <= ?", (since, until)).fetchall():
        try:
            items = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(items, list):
            for c in {str(c) for c in items if isinstance(c, str)}:
                sets[c].add(str(nid))
    return dict(sets)


def news_counts(conn: sqlite3.Connection, since: str, until: str) -> Dict[str, int]:
    """News items per company key in the window (``{'illumina': 5, 'Acme Bio': 1}``)."""
    return {k: len(v) for k, v in _news_sets(conn, since, until).items()}


def _news_for(entry: Dict[str, Any], sets: Dict[str, Set[str]]) -> int:
    """Distinct news items tagged with the entry's id or its display name (an item tagged with both counts once)."""
    return len(sets.get(entry["id"], set()) | sets.get(entry["name"], set()))


def recent_news(conn: sqlite3.Connection, entry: Dict[str, Any], since: str, until: str, limit: int = 10) -> List[Dict[str, Any]]:
    """The newest news items tagged with the company (by id or name), newest first."""
    if not _has_table(conn, "news_items"):
        return []
    d = _news_date_sql()
    keys = {entry["id"], entry["name"]}
    out = []
    for title, url, feed, day, raw in conn.execute(
        f"SELECT title, url, feed, {d}, companies_json FROM news_items WHERE {d} >= ? AND {d} <= ? ORDER BY {d} DESC, title",
        (since, until),
    ).fetchall():
        try:
            items = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(items, list) and keys & {str(c) for c in items if isinstance(c, str)}:
            out.append({"title": title or "(untitled)", "url": url or "", "feed": feed or "", "date": day or ""})
            if len(out) >= limit:
                break
    return out


def candidate_links(conn: sqlite3.Connection, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rows of ``candidate_companies`` that are this company under another spelling: same normalised name
    as the name or an alias, a shared e-mail domain, or a promotion that produced this entry's id."""
    if not _has_table(conn, "candidate_companies"):
        return []
    keys = {discovery.normalize_name(entry["name"])} | {discovery.normalize_name(a) for a in entry["aliases"]}
    keys.discard("")
    domains = set(entry["domains"])
    out = []
    for name_norm, display, status, score, papers, raw in conn.execute(
        "SELECT name_norm, display_name, status, score, papers, evidence_json FROM candidate_companies"
    ).fetchall():
        payload = discovery._loads(raw, {})
        payload = payload if isinstance(payload, dict) else {}
        cand_domains = {str(d).lower() for d in (payload.get("domains") or [])}
        promoted_id = ((payload.get("promotion") or {}).get("entry") or {}).get("id") if payload.get("promotion") else None
        variants = {discovery.normalize_name(v) for v in (payload.get("variants") or [])}
        if name_norm in keys or (variants & keys) or (cand_domains & domains) or promoted_id == entry["id"]:
            out.append({"name_norm": name_norm, "display_name": display or name_norm, "status": status,
                        "score": float(score or 0), "papers": int(papers or 0)})
    out.sort(key=lambda r: (-r["score"], r["display_name"]))
    return out


# ------------------------------------------------------------------------------------------------
# listings
# ------------------------------------------------------------------------------------------------


def list_companies(store_or_conn, entries: Sequence[Dict[str, Any]], since: str, until: str,
                   type_: Optional[str] = None, public: Optional[bool] = None,
                   with_candidates: bool = True) -> List[Dict[str, Any]]:
    """One row per gazetteer entry with its counts in ``[since, until]``, most-mentioned first.

    `type_` keeps one gazetteer type; `public=True` keeps listed companies, `public=False` the private ones."""
    conn = _conn(store_or_conn)
    counts = paper_counts(conn, since, until)
    news = _news_sets(conn, since, until)
    rows = []
    for e in entries:
        if type_ and e["type"] != type_:
            continue
        if public is not None and e["public"] != bool(public):
            continue
        c = counts.get(e["id"]) or {"papers": 0, "mentions": 0, "roles": _empty_roles(), "last_paper": ""}
        rows.append({
            **{k: e[k] for k in ("id", "name", "ticker", "public", "type", "parent", "domains", "aliases", "watch_news")},
            "papers": c["papers"], "mentions": c["mentions"], "roles": dict(c["roles"]), "last_paper": c["last_paper"],
            "news": _news_for(e, news),
            "candidates": len(candidate_links(conn, e)) if with_candidates else 0,
            "since": since, "until": until,
        })
    rows.sort(key=lambda r: (-r["papers"], -r["mentions"], r["name"].lower()))
    return rows


def monthly_series(conn: sqlite3.Connection, entity_id: str, months: Sequence[str]) -> List[Dict[str, Any]]:
    """Distinct papers and summed mentions per calendar month for one company, zero-filled over `months`."""
    if not months:
        return []
    d = date_sql()
    lo, hi = f"{months[0]}-01", f"{months[-1]}-31"
    got = {
        m: (int(n or 0), int(k or 0))
        for m, n, k in conn.execute(
            f"SELECT substr({d},1,7) AS month, COUNT(DISTINCT m.paper_id), SUM(m.count) FROM mentions m JOIN papers p ON p.id=m.paper_id "
            f"WHERE m.kind='company' AND m.entity_id=? AND {d} >= ? AND {d} <= ? GROUP BY month",
            (entity_id, lo, hi),
        ).fetchall()
    }
    return [{"month": m, "papers": got.get(m, (0, 0))[0], "mentions": got.get(m, (0, 0))[1]} for m in months]


def comentioned(conn: sqlite3.Connection, entity_id: str, kind: str, since: str, until: str, limit: int = 12) -> List[Dict[str, Any]]:
    """Entities of `kind` (``instrument`` or ``company``) named in the same papers as the company, by shared papers."""
    d = date_sql()
    rows = conn.execute(
        f"SELECT b.entity_id, MAX(b.entity_name), MAX(b.vendor), MAX(b.ticker), COUNT(DISTINCT a.paper_id) AS n "
        f"FROM mentions a JOIN mentions b ON b.paper_id=a.paper_id AND b.kind=? AND b.entity_id<>a.entity_id "
        f"JOIN papers p ON p.id=a.paper_id "
        f"WHERE a.kind='company' AND a.entity_id=? AND {d} >= ? AND {d} <= ? "
        f"GROUP BY b.entity_id ORDER BY n DESC, b.entity_id LIMIT ?",
        (kind, entity_id, since, until, int(limit)),
    ).fetchall()
    return [{"id": eid, "name": name or eid, "vendor": vendor or "", "ticker": ticker if is_public(ticker) else "",
             "papers": int(n or 0), "unlisted": str(eid).startswith(UNLISTED_PREFIX)} for eid, name, vendor, ticker, n in rows]


def recent_papers(conn: sqlite3.Connection, entity_id: str, since: str, until: str, limit: int = 15) -> List[Dict[str, Any]]:
    """Newest papers mentioning the company in the window, with the roles it plays in each."""
    d = date_sql()
    rows = conn.execute(
        f"SELECT p.id, p.title, p.journal, {d} AS day, p.url, p.kind, GROUP_CONCAT(DISTINCT m.section), SUM(m.count) "
        f"FROM mentions m JOIN papers p ON p.id=m.paper_id "
        f"WHERE m.kind='company' AND m.entity_id=? AND {d} >= ? AND {d} <= ? "
        f"GROUP BY p.id ORDER BY day DESC, p.title LIMIT ?",
        (entity_id, since, until, int(limit)),
    ).fetchall()
    out = []
    for pid, title, journal, day, url, kind, sections, n in rows:
        secs = sorted({s for s in str(sections or "").split(",") if s})
        roles = sorted({ROLE_OF_SECTION.get(s, "other") for s in secs}, key=ROLES.index)
        out.append({"id": pid, "title": title or "(untitled)", "journal": journal or "", "date": day or "",
                    "url": url or "", "kind": kind or "", "sections": secs, "roles": roles, "mentions": int(n or 0)})
    return out


def company_detail(store_or_conn, entry: Dict[str, Any], today: Optional[date] = None,
                   windows: Sequence[int] = DEFAULT_WINDOWS, months: int = MONTHS_OF_HISTORY,
                   papers_limit: int = 15, top: int = 12) -> Dict[str, Any]:
    """Everything the ``show`` command and the dashboard detail page print for one company.

    `entry` is a normalised gazetteer entry (``normalize_entry``) or a minimal ``{id, name, ticker}`` dict.
    Counts are given per window in `windows` (days, default 90 and 365); the monthly series covers the last
    `months` calendar months; co-mentions and recent papers use the longest window."""
    conn = _conn(store_or_conn)
    entry = normalize_entry(entry) if "public" not in entry else entry
    today = today or today_utc()
    wins: Dict[str, Dict[str, Any]] = {}
    longest = max(int(w) for w in windows) if windows else DEFAULT_WINDOWS[-1]
    for days in windows:
        since, until = window(days, today)
        c = paper_counts(conn, since, until).get(entry["id"]) or {"papers": 0, "mentions": 0, "roles": _empty_roles(), "last_paper": ""}
        wins[f"{int(days)}d"] = {"since": since, "until": until, "papers": c["papers"], "mentions": c["mentions"],
                                 "roles": dict(c["roles"]), "last_paper": c["last_paper"],
                                 "news": _news_for(entry, _news_sets(conn, since, until))}
    since, until = window(longest, today)
    axis = month_axis(today, months)
    return {
        **entry,
        "slug": page_slug(entry["id"]),
        "today": today.isoformat(),
        "windows": wins,
        "monthly": monthly_series(conn, entry["id"], axis),
        "instruments": comentioned(conn, entry["id"], "instrument", since, until, top),
        "companies": comentioned(conn, entry["id"], "company", since, until, top),
        "recent_papers": recent_papers(conn, entry["id"], since, until, papers_limit),
        "news": recent_news(conn, entry, since, until, limit=10),
        "candidates": candidate_links(conn, entry),
    }


# ------------------------------------------------------------------------------------------------
# export
# ------------------------------------------------------------------------------------------------


def export_rows(store_or_conn, entries: Sequence[Dict[str, Any]], since: str, until: str) -> List[Dict[str, Any]]:
    """One flat row per company (``EXPORT_COLUMNS``) with the window's counts; the window defaults to 90 days."""
    out = []
    for r in list_companies(store_or_conn, entries, since, until):
        roles = r["roles"]
        out.append({
            "id": r["id"], "name": r["name"], "ticker": r["ticker"], "public": "yes" if r["public"] else "no",
            "type": r["type"], "parent": r["parent"], "papers_90d": r["papers"], "mentions_90d": r["mentions"],
            "author_90d": roles["author"], "funding_coi_90d": roles["funding_coi"], "subject_90d": roles["subject"],
            "supplier_90d": roles["supplier"], "other_90d": roles["other"], "news_90d": r["news"],
            "candidates_linked": r["candidates"], "last_paper": r["last_paper"],
            "domains": " ".join(r["domains"]), "aliases": len(r["aliases"]),
        })
    return out


def write_csv(rows: Iterable[Dict[str, Any]], fh: TextIO) -> int:
    """Write export rows as CSV with the fixed ``EXPORT_COLUMNS`` header. Returns the number of data rows."""
    w = csv.DictWriter(fh, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    n = 0
    for r in rows:
        w.writerow(r)
        n += 1
    return n


# ------------------------------------------------------------------------------------------------
# co-mention graph
# ------------------------------------------------------------------------------------------------


def comention_graph(store_or_conn, since: str, until: str, entries: Optional[Sequence[Dict[str, Any]]] = None,
                    min_weight: int = 1, include_unlisted: bool = False, keep_isolated: bool = False) -> Dict[str, Any]:
    """Companies that appear in the same papers: ``{nodes: [{id, name, ticker, type, papers}], links: [{source, target, weight}]}``.

    ``weight`` is the number of distinct papers naming both companies in the window. Nodes carry the
    gazetteer type when `entries` is given (else ``""``); ``unlisted:`` institutions are left out unless
    `include_unlisted`. Isolated companies (no shared paper) are dropped unless `keep_isolated`."""
    conn = _conn(store_or_conn)
    d = date_sql()
    meta = {e["id"]: e for e in (entries or [])}
    counts = paper_counts(conn, since, until)
    links = []
    for a, b, n in conn.execute(
        f"SELECT a.entity_id, b.entity_id, COUNT(DISTINCT a.paper_id) AS n FROM mentions a "
        f"JOIN mentions b ON b.paper_id=a.paper_id AND b.kind='company' AND a.entity_id < b.entity_id "
        f"JOIN papers p ON p.id=a.paper_id "
        f"WHERE a.kind='company' AND {d} >= ? AND {d} <= ? GROUP BY a.entity_id, b.entity_id HAVING n >= ? ORDER BY n DESC, a.entity_id, b.entity_id",
        (since, until, max(1, int(min_weight))),
    ).fetchall():
        if not include_unlisted and (str(a).startswith(UNLISTED_PREFIX) or str(b).startswith(UNLISTED_PREFIX)):
            continue
        links.append({"source": a, "target": b, "weight": int(n)})
    linked = {l_["source"] for l_ in links} | {l_["target"] for l_ in links}
    names = {
        eid: (name or eid, ticker or "")
        for eid, name, ticker in conn.execute("SELECT entity_id, MAX(entity_name), MAX(ticker) FROM mentions WHERE kind='company' GROUP BY entity_id").fetchall()
    }
    nodes = []
    for eid, c in counts.items():
        if eid not in linked and not keep_isolated:
            continue
        if not include_unlisted and str(eid).startswith(UNLISTED_PREFIX):
            continue
        name, ticker = names.get(eid, (eid, ""))
        e = meta.get(eid)
        nodes.append({"id": eid, "name": e["name"] if e else name, "ticker": (e["ticker"] if e else ticker) if is_public(e["ticker"] if e else ticker) else "",
                      "type": e["type"] if e else ("unlisted" if str(eid).startswith(UNLISTED_PREFIX) else ""),
                      "papers": c["papers"], "degree": sum(1 for l_ in links if eid in (l_["source"], l_["target"]))})
    nodes.sort(key=lambda n_: (-n_["papers"], n_["name"].lower()))
    return {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "window": {"since": since, "until": until},
            "min_weight": int(min_weight), "nodes": nodes, "links": links}


# ------------------------------------------------------------------------------------------------
# vetting table (docs) and SEC ticker proposals
# ------------------------------------------------------------------------------------------------

VETTING_COLUMNS = ("candidate", "verdict", "name", "type", "ticker", "hq", "what", "domain", "action")


def _md(s: Any) -> str:
    return str("" if s is None else s).replace("|", "/").replace("\n", " ").strip()


def render_vetting_table(rows: Iterable[Dict[str, Any]]) -> str:
    """A Markdown table of vetting decisions (``VETTING_COLUMNS``) for ``docs/companies.md``."""
    lines = ["| " + " | ".join(VETTING_COLUMNS) + " |", "|" + "---|" * len(VETTING_COLUMNS)]
    for r in rows:
        lines.append("| " + " | ".join(_md(r.get(c, "")) for c in VETTING_COLUMNS) + " |")
    return "\n".join(lines)


def sec_ticker_proposals(entries: Sequence[Dict[str, Any]], tickers: Sequence[Tuple[str, str, str]]) -> List[Dict[str, Any]]:
    """Exact-brand SEC matches for entries whose ticker is a placeholder: ``[{id, name, ticker, title, match}]``.

    `tickers` is ``discovery.load_sec_tickers()`` output (``(normalised_title, ticker, title)``). Only an exact
    normalised match (``match == 1.0``) is proposed — the whole name, not a shared first word — because
    the proposals are meant to be applied, not browsed. A company whose ticker already names a listing is
    skipped. Anything else stays a hint for ``litradar candidates --hints``."""
    out = []
    for e in entries:
        if is_public(e.get("ticker")):
            continue
        hint = discovery.sec_ticker_hint(e["name"], tickers)
        if hint and hint.get("match", 0) >= 1.0:
            out.append({"id": e["id"], "name": e["name"], "ticker": hint["ticker"], "title": hint["title"], "match": hint["match"]})
    return out


__all__ = [
    "ROLES", "ROLE_OF_SECTION", "ROLE_LABELS", "COMPANY_TYPES", "EXPORT_COLUMNS", "VETTING_COLUMNS",
    "is_public", "date_sql", "window", "month_axis", "page_slug", "normalize_entry", "load_entries", "resolve_entry",
    "entries_from_store", "paper_counts", "news_counts", "recent_news", "candidate_links", "list_companies",
    "monthly_series", "comentioned", "recent_papers", "company_detail", "export_rows", "write_csv",
    "comention_graph", "render_vetting_table", "sec_ticker_proposals",
]
