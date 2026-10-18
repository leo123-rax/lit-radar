"""Coverage report: where the index is thin.

Answers, from the store alone (no network): which sources contributed what in the window and how
much of it has full text; which configured journals produced nothing (and when they were last
seen); which watched entities (companies with `watch_news`, instrument platforms, tracked authors,
watchlist terms) had zero mentions against their trailing 13-week average; which sources errored
in the last pipeline run; and concrete suggestions. Everything is a plain dict so it can be
printed as Markdown, dumped as JSON or embedded in the digest.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Settings
from .rank.heuristics import is_platform
from .store import Store

TRAILING_WEEKS = 13
MAX_LISTED = 40  # longest per-table listing in the Markdown; the JSON carries everything

# the digest's window semantics: dated papers by pub_date, undated ones by first_seen
_IN_WINDOW = "((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date = '' AND substr(p.first_seen,1,10) >= ?))"
_IN_RANGE = "((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date = '' AND substr(p.first_seen,1,10) >= ? AND substr(p.first_seen,1,10) <= ?))"

PAPER_SOURCES = ("biorxiv", "europepmc", "openalex", "crossref", "arxiv", "rss")
# errors whose text points at configuration, not at the remote source: a re-run changes nothing
_CONFIG_ERROR_MARKERS = ("not set", "not configured", "api key", "api_key", "contact_email", "litradar_", "budget", "insufficient")


def _trailing(since: str) -> Tuple[str, str]:
    d = date.fromisoformat(since)
    return (d - timedelta(days=7 * TRAILING_WEEKS)).isoformat(), (d - timedelta(days=1)).isoformat()


def _base_source(source: str) -> str:
    return (source or "").split(":", 1)[0].split(".", 1)[0] or "unknown"


_SOURCE_LABEL_ALIASES = {"feed": "rss", "feeds": "rss", "subject": "rss", "news": "rss", "gnews": "rss"}


def error_source_id(label: str) -> str:
    """The source id behind a pipeline error label: 'SEC 8-K feeds skipped' -> 'sec', 'rss Nature Methods' -> 'rss',
    'feed Example Feed' -> 'rss', 'europepmc' -> 'europepmc'. The same first-word rule `litradar status` uses, so
    suggestions name a real `--sources` value instead of echoing the whole label."""
    first = (label or "").strip().split()
    key = (first[0].lower().split(":", 1)[0].split(".", 1)[0] if first else "") or "unknown"
    return _SOURCE_LABEL_ALIASES.get(key, key)


def is_config_error(text: str) -> bool:
    """Does an error message describe a missing key/identity/budget rather than a source outage?"""
    t = (text or "").lower()
    return any(m in t for m in _CONFIG_ERROR_MARKERS)


# ----------------------------------------------------------------------------- store queries
def _last_run(store: Store) -> Optional[Dict[str, Any]]:
    row = store.conn.execute("SELECT started, finished, since, until, counts_json FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[4] or "{}")
    except ValueError:
        payload = {}
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else payload
    return {
        "started": row[0], "finished": row[1], "since": row[2], "until": row[3],
        "counts": counts or {}, "errors": list(payload.get("errors") or []), "http": payload.get("http") or {},
    }


def _sources(store: Store, since: str, until: str, last_run: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = store.conn.execute(
        f"SELECT p.source, COUNT(*), SUM(COALESCE(p.fulltext_available, 0)) FROM papers p WHERE {_IN_WINDOW} GROUP BY p.source",
        (since, until, since),
    ).fetchall()
    agg: Dict[str, Dict[str, Any]] = {}
    for source, n, ft in rows:
        key = "rss" if str(source or "").startswith("rss:") else (source or "unknown")
        a = agg.setdefault(key, {"source": key, "papers": 0, "fulltext": 0, "feeds": 0})
        a["papers"] += int(n or 0)
        a["fulltext"] += int(ft or 0)
        if key == "rss":
            a["feeds"] += 1
    pulled: Dict[str, int] = {}
    for k, v in ((last_run or {}).get("counts") or {}).items():
        if str(k).startswith("fetched."):
            label = str(k).split(".", 1)[1]
            pulled[_base_source(label)] = pulled.get(_base_source(label), 0) + int(v or 0)
    total = sum(a["papers"] for a in agg.values()) or 1
    out = []
    for a in sorted(agg.values(), key=lambda x: -x["papers"]):
        a["share"] = round(a["papers"] / total, 3)
        a["fulltext_share"] = round(a["fulltext"] / a["papers"], 3) if a["papers"] else 0.0
        a["pulled_last_run"] = pulled.get(_base_source(a["source"]))
        out.append(a)
    return out


def _journal_counts(store: Store, cond: str, params: Tuple[str, ...]) -> Dict[str, int]:
    rows = store.conn.execute(f"SELECT lower(p.journal), COUNT(*) FROM papers p WHERE {cond} GROUP BY lower(p.journal)", params).fetchall()
    return {str(j or ""): int(n) for j, n in rows}


def _issn_count(store: Store, issns: Sequence[str], cond: str, params: Tuple[str, ...]) -> int:
    if not issns:
        return 0
    clauses = " OR ".join("p.extra_json LIKE ?" for _ in issns)
    row = store.conn.execute(f"SELECT COUNT(*) FROM papers p WHERE {cond} AND ({clauses})", params + tuple(f'%"{i}"%' for i in issns)).fetchone()
    return int(row[0] if row else 0)


def _journals(store: Store, since: str, until: str, journals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    t_since, t_until = _trailing(since)
    in_window = _journal_counts(store, _IN_WINDOW, (since, until, since))
    trailing = _journal_counts(store, _IN_RANGE, (t_since, t_until, t_since, t_until))
    last_seen = {
        str(j or ""): d
        for j, d in store.conn.execute(
            "SELECT lower(journal), MAX(CASE WHEN pub_date != '' THEN pub_date ELSE substr(first_seen,1,10) END) FROM papers GROUP BY lower(journal)"
        ).fetchall()
    }
    zero = []
    for j in journals:
        name = str(j.get("name") or "")
        key = name.lower()
        issns = [str(i) for i in (j.get("issn") or [])]
        n = in_window.get(key, 0)
        if n == 0 and issns:
            n = _issn_count(store, issns, _IN_WINDOW, (since, until, since))
        if n:
            continue
        tr = trailing.get(key, 0)
        if tr == 0 and issns:
            tr = _issn_count(store, issns, _IN_RANGE, (t_since, t_until, t_since, t_until))
        zero.append({
            "name": name, "tier": j.get("tier", ""), "issn": issns, "rss": bool(j.get("rss")),
            "last_seen": last_seen.get(key), "trailing_13w_avg": round(tr / TRAILING_WEEKS, 2),
        })
    zero.sort(key=lambda r: (str(r["tier"]), r["name"]))
    return {"total": len(journals), "zero": zero}


def _mention_counts(store: Store, kind: str, cond: str, params: Tuple[str, ...]) -> Dict[str, int]:
    rows = store.conn.execute(
        f"SELECT m.entity_id, COUNT(DISTINCT m.paper_id) FROM mentions m JOIN papers p ON p.id = m.paper_id WHERE m.kind = ? AND {cond} GROUP BY m.entity_id",
        (kind,) + params,
    ).fetchall()
    return {str(e): int(n) for e, n in rows}


def _archive_counts(store: Store, since: str, until: str) -> Dict[str, int]:
    rows = store.conn.execute(
        "SELECT entity_id, COUNT(*) FROM archive_records WHERE first_public >= ? AND first_public <= ? AND entity_id != '' GROUP BY entity_id",
        (since, until),
    ).fetchall()
    return {str(e): int(n) for e, n in rows}


def _news_counts(store: Store, since: str, until: str, company_ids: Sequence[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for cid in company_ids:
        row = store.conn.execute(
            """SELECT COUNT(*) FROM news_items WHERE companies_json LIKE ?
               AND ((substr(first_seen,1,10) >= ? AND substr(first_seen,1,10) <= ?) OR (substr(published,1,10) >= ? AND substr(published,1,10) <= ?))""",
            (f'%"{cid}"%', since, until, since, until),
        ).fetchone()
        out[cid] = int(row[0] if row else 0)
    return out


def _author_hits(store: Store, cond: str, params: Tuple[str, ...]) -> Dict[Tuple[str, str], int]:
    rows = store.conn.execute(
        f"SELECT t.subgroup, lower(t.author_name), COUNT(DISTINCT t.paper_id) FROM tracked_author_hits t JOIN papers p ON p.id = t.paper_id WHERE {cond} GROUP BY t.subgroup, lower(t.author_name)",
        params,
    ).fetchall()
    return {(str(g), str(a)): int(n) for g, a, n in rows}


def _term_count(store: Store, term: str, cond: str, params: Tuple[str, ...]) -> int:
    row = store.conn.execute(
        f"SELECT COUNT(*) FROM papers p WHERE {cond} AND instr(lower(COALESCE(p.title,'') || ' ' || COALESCE(p.abstract,'')), lower(?)) > 0",
        params + (term,),
    ).fetchone()
    return int(row[0] if row else 0)


def _watched(store: Store, since: str, until: str, settings: Settings) -> Dict[str, Any]:
    t_since, t_until = _trailing(since)
    win = (since, until, since)
    tr = (t_since, t_until, t_since, t_until)
    companies = list((settings.yaml("companies") or {}).get("entries") or [])
    instruments = list((settings.yaml("instruments") or {}).get("entries") or [])
    groups = list((settings.yaml("subgroups") or {}).get("subgroups") or [])
    terms = [str(t) for t in (settings.radar or {}).get("watchlist_terms") or []]

    comp_now = _mention_counts(store, "company", _IN_WINDOW, win)
    comp_tr = _mention_counts(store, "company", _IN_RANGE, tr)
    watched_companies = [e for e in companies if e.get("watch_news") and e.get("id")]
    zero_ids = [str(e["id"]) for e in watched_companies if comp_now.get(str(e["id"]), 0) == 0]
    news = _news_counts(store, since, until, zero_ids)
    companies_zero = [
        {
            "id": str(e["id"]), "name": e.get("name"), "ticker": e.get("ticker"), "news_items": news.get(str(e["id"]), 0),
            "trailing_13w_avg": round(comp_tr.get(str(e["id"]), 0) / TRAILING_WEEKS, 2), "aliases": len(e.get("aliases") or []),
        }
        for e in watched_companies if str(e["id"]) in zero_ids
    ]

    inst_now = _mention_counts(store, "instrument", _IN_WINDOW, win)
    inst_tr = _mention_counts(store, "instrument", _IN_RANGE, tr)
    arch_now = _archive_counts(store, since, until)
    arch_tr = _archive_counts(store, t_since, t_until)
    platforms = [e for e in instruments if e.get("id") and is_platform(str(e.get("category") or "")) and str(e.get("status") or "current") != "discontinued"]
    platforms_zero = [
        {
            "id": str(e["id"]), "name": e.get("name"), "vendor": e.get("vendor"), "ticker": e.get("ticker"),
            "trailing_13w_avg": round(inst_tr.get(str(e["id"]), 0) / TRAILING_WEEKS, 2),
            "archive_experiments_13w": arch_tr.get(str(e["id"]), 0),
        }
        for e in platforms if inst_now.get(str(e["id"]), 0) == 0 and arch_now.get(str(e["id"]), 0) == 0
    ]

    hits_now = _author_hits(store, _IN_WINDOW, win)
    hits_tr = _author_hits(store, _IN_RANGE, tr)
    authors_all: List[Dict[str, Any]] = []
    for g in groups:
        for a in g.get("tracked_authors") or []:
            if isinstance(a, dict) and a.get("name"):
                authors_all.append({"subgroup": str(g.get("id")), "name": str(a["name"]), "has_id": bool(a.get("openalex_id"))})
    authors_zero = [
        dict(a, trailing_13w_avg=round(hits_tr.get((a["subgroup"], a["name"].lower()), 0) / TRAILING_WEEKS, 2))
        for a in authors_all if hits_now.get((a["subgroup"], a["name"].lower()), 0) == 0
    ]

    terms_zero = []
    for t in terms:
        if _term_count(store, t, _IN_WINDOW, win) == 0:
            terms_zero.append({"term": t, "trailing_13w_avg": round(_term_count(store, t, _IN_RANGE, tr) / TRAILING_WEEKS, 2)})

    return {
        "companies": {"total": len(watched_companies), "zero": companies_zero},
        "platforms": {"total": len(platforms), "zero": platforms_zero},
        "authors": {"total": len(authors_all), "with_openalex_id": sum(1 for a in authors_all if a["has_id"]), "zero": authors_zero},
        "terms": {"total": len(terms), "zero": terms_zero},
    }


# ----------------------------------------------------------------------------- suggestions
def _suggestions(report: Dict[str, Any], settings: Settings) -> List[str]:
    out: List[str] = []
    radar = settings.radar or {}
    src_cfg = radar.get("sources") or {}
    last = report.get("last_run")
    if not last:
        out.append("No pipeline run is recorded in this database yet: run `litradar run --days 7` before reading the rest.")
    for e in report.get("errored_sources") or []:
        if e.get("config"):
            out.append(f"{e['source']} did not run in the last run because of configuration ({e['error'][:120]}): fix .env (see .env.example), then re-run.")
        else:
            out.append(f"{e['source']} errored in the last run ({e['error'][:90]}): re-run it alone with `litradar run --sources {e['source_id']} -v`.")
    if bool((src_cfg.get("openalex") or {}).get("enabled", True)) and not settings.openalex_api_key:
        out.append(
            "No OpenAlex key configured: OPENALEX_API_KEY is empty and anonymous OpenAlex traffic is metered per IP, so the topic, "
            "tracked-author and company-author pulls may return nothing (free key: https://help.openalex.org/api/authentication/)."
        )
    if not settings.contact_email:
        out.append("LITRADAR_CONTACT_EMAIL is empty: SEC 8-K feeds are skipped and Crossref/OpenAlex serve you from the slow anonymous pool.")
    present = {_base_source(s["source"]) for s in report.get("sources") or []}
    pulled_last_run = {_base_source(str(k).split(".", 1)[1]) for k in ((last or {}).get("counts") or {}) if str(k).startswith("fetched.")}
    for name in PAPER_SOURCES:
        if bool((src_cfg.get(name) or {}).get("enabled", True)) and name not in present and last:
            if pulled_last_run and name not in pulled_last_run:
                out.append(f"Source {name} is enabled but contributed 0 papers in the window and was not pulled in the last run "
                           f"(a `--sources` subset, or it failed before returning anything): `litradar run --sources {name} -v` pulls it alone.")
            else:
                out.append(f"Source {name} is enabled but contributed 0 papers in the window: check it with `litradar run --sources {name} -v`.")
    for s in report.get("sources") or []:
        if s["source"] in ("biorxiv", "medrxiv") and s["papers"] >= 20 and s["fulltext"] == 0:
            out.append(f"{s['source']}: {s['papers']} preprints but no parsed full text (the JATS host rate-limits scripts; see docs/SOURCES.md).")
    jz = (report.get("journals") or {}).get("zero") or []
    no_issn = [j["name"] for j in jz if not j["issn"] and not j["rss"]]
    if no_issn:
        out.append("Journals with neither ISSN nor RSS, so nothing can pull them: " + ", ".join(no_issn[:8]) + ".")
    never = [j for j in jz if j["issn"] and j["last_seen"] is None]
    if never and _journal_pull_was_complete(last):
        out.append(
            "ISSN pull returned 0 for journals never seen in the store: check the ISSN for "
            + ", ".join(f"{j['name']} ({j['issn'][0]})" for j in never[:6]) + ("..." if len(never) > 6 else "") + "."
        )
    elif never:
        out.append(
            f"{len(never)} configured journals have never been seen in the store, but the last run did not pull the ISSN sources "
            "over a full week (a `--sources` subset or a short window), so their ISSNs cannot be judged yet."
        )
    stale = [j for j in jz if j["issn"] and j["last_seen"] is not None and j["trailing_13w_avg"] == 0]
    if stale:
        out.append("Journals seen before but silent for 13+ weeks: " + ", ".join(f"{j['name']} (last {j['last_seen']})" for j in stale[:6]) + ".")
    w = report.get("watched") or {}
    cz = (w.get("companies") or {}).get("zero") or []
    no_alias = [c for c in cz if c["aliases"] == 0 and c["news_items"] == 0 and c["trailing_13w_avg"] == 0]
    if no_alias:
        out.append(
            "Watched companies with no paper mentions, no news and no aliases configured: add an alias for "
            + ", ".join(c["name"] for c in no_alias[:8])
            + " by editing the entry's `aliases:` list in companies.yaml (keeps ticker/type/watch_news), then `litradar rematch`."
        )
    silent = [c for c in cz if c["aliases"] > 0 and c["trailing_13w_avg"] == 0 and c["news_items"] == 0]
    if silent:
        out.append("Watched companies never seen in papers or news over 13 weeks (check spelling/aliases or whether they belong on the list): " + ", ".join(c["name"] for c in silent[:8]) + ".")
    a = w.get("authors") or {}
    missing = a.get("total", 0) - a.get("with_openalex_id", 0)
    if missing:
        out.append(
            f"{missing} of {a.get('total', 0)} tracked authors have no OpenAlex id (name matching only): resolve them with "
            "`litradar track author \"<name>\" --subgroup <id>` (with OPENALEX_API_KEY set) or pass `--openalex A...`."
        )
    tz = [t for t in ((w.get("terms") or {}).get("zero") or []) if t["trailing_13w_avg"] == 0]
    if tz:
        out.append("Watchlist terms that never matched a title/abstract in 13 weeks: " + ", ".join(f"'{t['term']}'" for t in tz[:8]) + " (consider a broader spelling).")
    pz = (w.get("platforms") or {}).get("zero") or []
    if pz and report.get("papers_in_window", 0) > 0:
        quiet = [p for p in pz if p["trailing_13w_avg"] == 0 and p["archive_experiments_13w"] == 0]
        if quiet:
            out.append(f"{len(quiet)} current instrument platforms have no paper mention and no archive deposit in 13 weeks (expected for new launches; otherwise check aliases).")
    return out


# ----------------------------------------------------------------------------- public API
def _journal_pull_was_complete(last: Optional[Dict[str, Any]]) -> bool:
    """Did the last run pull the ISSN-driven sources over at least a week? Only then is a silent ISSN suspicious."""
    if not last:
        return False
    try:
        days = (date.fromisoformat(str(last.get("until"))) - date.fromisoformat(str(last.get("since")))).days
    except (TypeError, ValueError):
        return False
    counts = last.get("counts") or {}
    pulled = any(str(k).startswith(("fetched.europepmc", "fetched.crossref")) for k in counts)
    return days >= 7 and pulled


def coverage_report(store: Store, since: str, until: str, settings: Settings) -> Dict[str, Any]:
    """Everything `litradar coverage` prints, as a JSON-serialisable dict.

    `last_run` and `errored_sources` describe the most recent pipeline run only (its counters are not a
    window aggregate); the Markdown says so next to the numbers.
    """
    last = _last_run(store)
    errored = []
    for msg in (last or {}).get("errors") or []:
        src, _, rest = str(msg).partition(":")
        label = src.strip() or "unknown"
        errored.append({"source": label, "source_id": error_source_id(label), "error": rest.strip() or str(msg), "config": is_config_error(str(msg))})
    journals = list((settings.yaml("journals") or {}).get("journals") or [])
    n_window = store.conn.execute(f"SELECT COUNT(*) FROM papers p WHERE {_IN_WINDOW}", (since, until, since)).fetchone()[0]
    report: Dict[str, Any] = {
        "since": since,
        "until": until,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "papers_in_window": int(n_window),
        "sources": _sources(store, since, until, last),
        "last_run": last,
        "errored_sources": errored,
        "journals": _journals(store, since, until, journals),
        "watched": _watched(store, since, until, settings),
    }
    report["suggestions"] = _suggestions(report, settings)
    return report


def _row(cells: Sequence[Any]) -> str:
    return "| " + " | ".join(str("" if c is None else c).replace("|", "/").replace("\n", " ") for c in cells) + " |"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], empty: str = "_none_") -> str:
    if not rows:
        return empty + "\n"
    out = [_row(headers), "|" + "|".join("---" for _ in headers) + "|"] + [_row(r) for r in rows]
    return "\n".join(out) + "\n"


def _pct(x: float) -> str:
    return f"{100.0 * x:.0f}%"


def render_coverage_markdown(store: Store, since: str, until: str, settings: Settings, report: Optional[Dict[str, Any]] = None) -> str:
    """Markdown section for the digest (or the terminal). Pass a precomputed `report` to avoid re-querying."""
    r = report or coverage_report(store, since, until, settings)
    md: List[str] = [f"## Coverage: {since} to {until}\n"]
    last = r.get("last_run")
    head = f"{r['papers_in_window']} papers in the window from {len(r['sources'])} source(s)"
    if last:
        head += (f"; last run {str(last.get('finished') or last.get('started') or '')[:16]} (window {last.get('since')}..{last.get('until')}) "
                 f"with {len(r['errored_sources'])} source error(s). Run counters below ('Pulled in last run', 'Sources that errored') describe "
                 "that run only, not every run behind the window")
    else:
        head += "; no pipeline run recorded"
    md.append(head + ".\n")

    md.append("### Records per source\n")
    md.append(_table(
        ["Source", "Papers", "Share", "With full text", "Full-text share", "Pulled in last run"],
        [[s["source"] + (f" ({s['feeds']} feeds)" if s.get("feeds") else ""), s["papers"], _pct(s["share"]), s["fulltext"], _pct(s["fulltext_share"]),
          "" if s.get("pulled_last_run") is None else s["pulled_last_run"]] for s in r["sources"]],
        empty="_no papers in the window_",
    ))

    jz = r["journals"]["zero"]
    md.append(f"### Journals with zero items in the window ({len(jz)} of {r['journals']['total']})\n")
    md.append(_table(
        ["Journal", "Tier", "ISSN", "Last seen", f"Trailing {TRAILING_WEEKS}-wk avg/wk"],
        [[j["name"], j["tier"], ", ".join(j["issn"]) or ("rss only" if j["rss"] else "none"), j["last_seen"] or "never", j["trailing_13w_avg"]] for j in jz[:MAX_LISTED]],
        empty="_every configured journal produced at least one item_",
    ))
    if len(jz) > MAX_LISTED:
        md.append(f"_... and {len(jz) - MAX_LISTED} more (see --json)_\n")

    w = r["watched"]
    md.append("### Watched entities with zero paper mentions in the window\n")
    cz = w["companies"]["zero"]
    with_news = sum(1 for c in cz if c["news_items"])
    md.append(f"**Companies with watch_news** ({len(cz)} of {w['companies']['total']} with no paper mention"
              + (f"; {with_news} of them had news items in the window, listed first" if with_news else "") + ")\n")
    ordered = sorted(cz, key=lambda c: (-int(c["news_items"] or 0), str(c["name"])))
    md.append(_table(
        ["Company", "Ticker", "News items", f"Trailing {TRAILING_WEEKS}-wk avg papers/wk", "Aliases"],
        [[c["name"], c["ticker"], c["news_items"], c["trailing_13w_avg"], c["aliases"]] for c in ordered[:MAX_LISTED]],
        empty="_every watched company was mentioned in at least one paper_",
    ))
    pz = w["platforms"]["zero"]
    md.append(f"**Instrument platforms** ({len(pz)} of {w['platforms']['total']} with no paper mention and no archive deposit)\n")
    md.append(_table(
        ["Platform", "Vendor", f"Trailing {TRAILING_WEEKS}-wk avg papers/wk", f"Archive experiments ({TRAILING_WEEKS} wk)"],
        [[p["name"], p["vendor"], p["trailing_13w_avg"], p["archive_experiments_13w"]] for p in pz[:MAX_LISTED]],
        empty="_every current platform appeared somewhere_",
    ))
    if len(pz) > MAX_LISTED:
        md.append(f"_... and {len(pz) - MAX_LISTED} more (see --json)_\n")
    a = w["authors"]
    md.append(f"**Tracked authors**: {len(a['zero'])} of {a['total']} had no new paper in the window; {a['total'] - a['with_openalex_id']} have no OpenAlex id (name matching only).\n")
    no_id = [x["name"] for x in a["zero"] if not x["has_id"]]
    if no_id:
        md.append("Silent and without an id: " + ", ".join(no_id[:MAX_LISTED]) + ("..." if len(no_id) > MAX_LISTED else "") + "\n")
    tz = w["terms"]["zero"]
    md.append(f"**Watchlist terms** ({len(tz)} of {w['terms']['total']} unmatched)\n")
    md.append(_table(["Term", f"Trailing {TRAILING_WEEKS}-wk avg papers/wk"], [[t["term"], t["trailing_13w_avg"]] for t in tz], empty="_every watchlist term matched at least one paper_"))

    md.append("### Sources that errored in the last run\n")
    if r["errored_sources"]:
        for e in r["errored_sources"]:
            tag = " _(configuration, not an outage)_" if e.get("config") else ""
            md.append(f"- **{e['source']}**: {e['error'][:200]}{tag}")
        md.append("")
    else:
        md.append("_none_\n" if last else "_no run recorded_\n")

    md.append("### Suggestions\n")
    if r["suggestions"]:
        md.extend(f"- {s}" for s in r["suggestions"])
        md.append("")
    else:
        md.append("_nothing to suggest_\n")
    return "\n".join(md)
