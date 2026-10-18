"""Client-side search for the static dashboard: a compact JSON index, an entity vocabulary and one page.

`litradar dashboard` already publishes trends; this module adds the "find that paper" surface on top
of the same read-only store and the same site. Three files land in the site directory:

  search-index.json     one compact record per paper in the look-back window (default 180 days),
                        ranked by score then date, capped by record count AND by raw byte size
  search-entities.json  instrument / company / subgroup / source / tier / kind / country vocabulary
                        with per-value paper counts over the indexed records (facets, autocomplete)
  search.html           the page: plain JavaScript, no framework, nothing fetched but the two files
                        above (and the site's own stylesheet, script and pinned chart library that
                        every dashboard page shares)

Record format (short keys keep 20,000 records inside a few megabytes; the `schema` object in the
index file repeats this legend for consumers):

  id   paper id            t    title (markup stripped)       j    journal / venue
  d    YYYY-MM-DD           doi  DOI                           u    URL, only when it is not https://doi.org/<doi>
  k    article | preprint   src  source kind (biorxiv, crossref, europepmc, rss, ...)
  tier A | B | C | preprint | other (journal tier the ranker recorded)
  s    rank score           c    citation count (only when > 0)
  a    abstract excerpt     sg   subgroups       in   instrument ids    co   company ids    cc   country codes

Data policy (litradar/policy.py, docs/LEGAL.md): metadata and abstracts are storable at every policy
level; full text is not. The index therefore never reads `sections_json` or the FTS body, and the
only prose it carries is an excerpt of the stored abstract, cut at SNIPPET_CHARS (well inside the
digest's 420-character rule) and only for papers whose policy level is 'full' or 'facts_only'. A
paper recorded at level 'none' gets title-only treatment. The level is the stored
`extra_json.fulltext_policy`, else derived from the licence and open-access flag exactly as the
store does (`Store.stored_fulltext_level`).

Entity links: a record's instrument and company chips link to the dashboard's entity pages only
for entities that have a card in this build (the dashboard passes its card lists in); an entity
without a card gets a chip that filters the results, nothing more. ENTITY_PAGES maps a kind to the
page the chip links to; change it when the site grows per-entity pages.
"""
from __future__ import annotations

import html
import json
import logging
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import dashboard as _dash
from .store import Store

log = logging.getLogger(__name__)

INDEX_FILE = "search-index.json"
ENTITIES_FILE = "search-entities.json"
PAGE_FILE = "search.html"

DEFAULT_DAYS = 180
DEFAULT_MAX_RECORDS = 20_000
DEFAULT_MAX_BYTES = 4_000_000       # raw bytes of search-index.json; the tail of the ranking is dropped to fit
SNIPPET_CHARS = 240
SNIPPET_LEVELS = frozenset({"full", "facts_only"})   # policy levels at which an abstract excerpt may be shown
MAX_COUNTRIES = 5
ENTITY_PAGES = {"instrument": "instruments.html", "company": "companies.html"}

SCHEMA_LEGEND = {
    "id": "paper id", "t": "title", "j": "journal", "d": "date YYYY-MM-DD", "doi": "DOI",
    "u": "URL when not https://doi.org/<doi>", "k": "article|preprint", "src": "source kind",
    "tier": "A|B|C|preprint|other", "s": "rank score", "c": "citations (when > 0)",
    "a": "abstract excerpt", "sg": "subgroups", "in": "instrument ids", "co": "company ids", "cc": "country codes",
}

# a tag starts with a letter or a slash, so "p < 0.05" and "n > 30" in an abstract are left alone
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]{0,200}>")
_WS_RE = re.compile(r"\s+")
_TIER_RE = re.compile(r"journal tier ([ABC])\b")
_WEB_URL_RE = re.compile(r"^https?://", re.I)


# ---------------------------------------------------------------------------- text and policy
def clean_text(value: Any) -> str:
    """Strip markup (titles arrive with <sub>, <i>, newlines), unescape entities, collapse whitespace."""
    text = html.unescape(_TAG_RE.sub(" ", str(value or "")))
    return _WS_RE.sub(" ", text).strip()


def excerpt(text: Any, limit: int = SNIPPET_CHARS) -> str:
    """The opening of `text`, at most `limit` characters, cut at a word boundary with an ellipsis."""
    text = clean_text(text)
    limit = max(8, int(limit))
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-(") + "…"


def policy_level(license_str: Optional[str], extra: Dict[str, Any]) -> str:
    """The paper's data-policy level: the stored one, else derived as the store derives it."""
    return Store.stored_fulltext_level(license_str, extra)


def abstract_excerpt(abstract: Any, level: str, limit: int = SNIPPET_CHARS) -> str:
    """An abstract excerpt, or '' when the policy level does not allow one. Never reads full text."""
    if level not in SNIPPET_LEVELS or not abstract:
        return ""
    return excerpt(abstract, limit)


def source_kind(source: Any) -> str:
    """`rss:<feed>` collapses to `rss`; everything else is the source name as stored."""
    s = str(source or "").strip().lower()
    if not s:
        return "unknown"
    return "rss" if s.startswith("rss:") else s


def tier_of(reasons: Any, kind: Any) -> str:
    """Journal tier as the ranker recorded it in `scores.reasons` ('journal tier A'); preprints are their own tier."""
    match = _TIER_RE.search(str(reasons or ""))
    if match:
        return match.group(1)
    if str(kind or "").lower() == "preprint":
        return "preprint"
    return "other"


def _json_dict(text: Any) -> Dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _countries(extra: Dict[str, Any], column_value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(column_value, str) and column_value.strip():
        found.append(column_value)
    values = extra.get("countries")
    if isinstance(values, list):
        found.extend(v for v in values if isinstance(v, str) and v.strip())
    elif isinstance(extra.get("country"), str) and extra["country"].strip():
        found.append(extra["country"])
    out: List[str] = []
    for code in found:
        code = code.strip().upper()
        if code and code not in out:
            out.append(code)
    return out[:MAX_COUNTRIES]


def _web_url(value: Any) -> str:
    s = str(value or "").strip()
    return s if _WEB_URL_RE.match(s) else ""


# ---------------------------------------------------------------------------- collection
def _empty_result(today: date, days: int) -> Dict[str, Any]:
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"start": (today - timedelta(days=days)).isoformat(), "end": today.isoformat(), "days": days},
        "records": [],
        "entities": {k: [] for k in ("instruments", "companies", "subgroups", "sources", "tiers", "kinds", "countries")},
        "stats": {"records": 0, "total_in_window": 0, "dropped_by_cap": 0, "dropped_by_budget": 0,
                  "with_snippet": 0, "with_citations": 0, "max_records": 0, "max_bytes": 0},
    }


def collect_index(
    conn: sqlite3.Connection,
    days: int = DEFAULT_DAYS,
    today: Optional[date] = None,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_bytes: Optional[int] = DEFAULT_MAX_BYTES,
    snippet_chars: int = SNIPPET_CHARS,
    carded: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, Any]:
    """Read the window and build records, entity vocabulary and stats. Only metadata and abstracts are read.

    `carded` maps 'instrument' / 'company' to the entity ids that have a card on the dashboard; those
    entities get a `page` link in the vocabulary. A missing optional table (scores, mentions,
    tracked_author_hits) empties that part of each record; a missing `papers` table yields an empty index.
    """
    today = today or datetime.now(timezone.utc).date()
    days = max(1, int(days))
    max_records = max(0, int(max_records))
    result = _empty_result(today, days)
    start = result["window"]["start"]
    tables = _dash._tables(conn)
    if "papers" not in tables:
        return result

    expr = _dash._paper_date_sql()
    columns = set(_dash._columns(conn, "papers"))
    country_col = "p.country" if "country" in columns else "NULL"
    cited_col = "p.cited_by_count" if "cited_by_count" in columns else "0"
    has_scores = "scores" in tables
    score_cols = "s.final, s.reasons" if has_scores else "NULL, NULL"
    score_join = "LEFT JOIN scores s ON s.paper_id = p.id" if has_scores else ""
    rows = _dash._q(
        conn,
        f"SELECT p.id, p.doi, p.title, p.abstract, p.journal, {expr} AS d, p.url, p.kind, p.source, p.license, "
        f"{cited_col}, p.extra_json, {country_col}, {score_cols} "
        f"FROM papers p {score_join} WHERE {expr} >= ?",
        (start,),
    )

    instruments: Dict[str, List[str]] = defaultdict(list)
    companies: Dict[str, List[str]] = defaultdict(list)
    entity_meta: Dict[str, Dict[str, Dict[str, str]]] = {"instrument": {}, "company": {}}
    if "mentions" in tables:
        for paper_id, kind, eid, name, vendor, ticker, category in _dash._q(
            conn,
            f"SELECT m.paper_id, m.kind, m.entity_id, MAX(m.entity_name), MAX(m.vendor), MAX(m.ticker), MAX(m.category) "
            f"FROM mentions m JOIN papers p ON p.id = m.paper_id WHERE {expr} >= ? "
            f"GROUP BY m.paper_id, m.kind, m.entity_id",
            (start,),
        ):
            if not eid or kind not in entity_meta:
                continue
            bucket = instruments if kind == "instrument" else companies
            if eid not in bucket[paper_id]:
                bucket[paper_id].append(eid)
            entity_meta[kind].setdefault(eid, {
                "name": clean_text(name) or eid, "vendor": clean_text(vendor), "ticker": _dash._ticker(ticker),
                "category": clean_text(category),
            })

    tracked_subgroups: Dict[str, List[str]] = defaultdict(list)
    if "tracked_author_hits" in tables:
        for paper_id, group in _dash._q(
            conn,
            f"SELECT DISTINCT t.paper_id, t.subgroup FROM tracked_author_hits t JOIN papers p ON p.id = t.paper_id "
            f"WHERE {expr} >= ? AND t.subgroup IS NOT NULL AND t.subgroup != ''",
            (start,),
        ):
            tracked_subgroups[paper_id].append(str(group))

    records: List[Dict[str, Any]] = []
    for pid, doi, title, abstract, journal, d, url, kind, source, license_str, cited, extra_json, country, final, reasons in rows:
        extra = _json_dict(extra_json)
        rec: Dict[str, Any] = {"id": pid, "t": clean_text(title) or "(untitled)", "d": str(d or "")}
        journal = clean_text(journal)
        if journal:
            rec["j"] = journal
        doi = str(doi or "").strip()
        if doi:
            rec["doi"] = doi
        web = _web_url(url)
        if web and web.lower() != f"https://doi.org/{doi}".lower():
            rec["u"] = web
        rec["k"] = "preprint" if str(kind or "").lower() == "preprint" else "article"
        rec["src"] = source_kind(source)
        rec["tier"] = tier_of(reasons, rec["k"])
        if final is not None:
            try:
                rec["s"] = round(float(final), 2)
            except (TypeError, ValueError):
                pass
        try:
            cites = int(cited or 0)
        except (TypeError, ValueError):
            cites = 0
        if cites > 0:
            rec["c"] = cites
        snippet = abstract_excerpt(abstract, policy_level(license_str, extra), snippet_chars)
        if snippet:
            rec["a"] = snippet
        groups = list(dict.fromkeys(_dash._extra_subgroups(extra_json) + tracked_subgroups.get(pid, [])))
        if groups:
            rec["sg"] = groups
        if instruments.get(pid):
            rec["in"] = instruments[pid]
        if companies.get(pid):
            rec["co"] = companies[pid]
        codes = _countries(extra, country)
        if codes:
            rec["cc"] = codes
        records.append(rec)

    total = len(records)
    # score desc, then date desc: two stable sorts, newest first then best first
    records.sort(key=lambda r: r["d"], reverse=True)
    records.sort(key=lambda r: r.get("s") if r.get("s") is not None else float("-inf"), reverse=True)
    records = records[:max_records]
    dropped_by_cap = total - len(records)
    records, dropped_by_budget = fit_budget(records, max_bytes, result["window"])
    with_snippet = sum(1 for r in records if r.get("a"))

    result["records"] = records
    result["entities"] = build_entities(records, entity_meta, carded or {})
    result["stats"] = {
        "records": len(records),
        "total_in_window": total,
        "dropped_by_cap": dropped_by_cap,
        "dropped_by_budget": dropped_by_budget,
        "with_snippet": with_snippet,
        "with_citations": sum(1 for r in records if r.get("c")),
        "max_records": max_records,
        "max_bytes": int(max_bytes or 0),
    }
    return result


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def fit_budget(records: List[Dict[str, Any]], max_bytes: Optional[int], window: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """Keep the longest prefix of `records` whose serialized index stays within `max_bytes`.

    Records arrive best-first, so the budget trims the tail of the ranking. Returns (kept, dropped).
    No budget (None or <= 0) keeps everything.
    """
    if not max_bytes or int(max_bytes) <= 0:
        return records, 0
    envelope = _index_envelope(window, [], {"records": 0, "total_in_window": 0, "dropped_by_cap": 0, "dropped_by_budget": 0})
    total = len(_dumps(envelope).encode("utf-8")) + 64   # the stats block grows by a few digits at most
    kept = 0
    for rec in records:
        size = len(_dumps(rec).encode("utf-8")) + 1
        if total + size > int(max_bytes):
            break
        total += size
        kept += 1
    return records[:kept], len(records) - kept


def build_entities(records: Sequence[Dict[str, Any]], meta: Dict[str, Dict[str, Dict[str, str]]],
                   carded: Dict[str, Set[str]]) -> Dict[str, List[Dict[str, Any]]]:
    """Facet vocabulary with per-value paper counts over the indexed records only, so facets and index agree."""
    counts: Dict[str, Dict[str, int]] = {k: defaultdict(int) for k in
                                         ("instruments", "companies", "subgroups", "sources", "tiers", "kinds", "countries")}
    for rec in records:
        for eid in rec.get("in", ()):
            counts["instruments"][eid] += 1
        for eid in rec.get("co", ()):
            counts["companies"][eid] += 1
        for group in rec.get("sg", ()):
            counts["subgroups"][group] += 1
        for code in rec.get("cc", ()):
            counts["countries"][code] += 1
        counts["sources"][rec.get("src", "unknown")] += 1
        counts["tiers"][rec.get("tier", "other")] += 1
        counts["kinds"][rec.get("k", "article")] += 1

    def ranked(kind: str) -> List[Tuple[str, int]]:
        return sorted(counts[kind].items(), key=lambda kv: (-kv[1], kv[0]))

    out: Dict[str, List[Dict[str, Any]]] = {}
    for kind, key in (("instrument", "instruments"), ("company", "companies")):
        rows = []
        for eid, n in ranked(key):
            info = meta.get(kind, {}).get(eid, {})
            row: Dict[str, Any] = {"id": eid, "name": info.get("name") or eid, "papers": n}
            for field in ("vendor", "ticker", "category"):
                if info.get(field):
                    row[field] = info[field]
            if eid in carded.get(kind, set()) and ENTITY_PAGES.get(kind):
                row["page"] = ENTITY_PAGES[kind]
            rows.append(row)
        out[key] = rows
    for key in ("subgroups", "sources", "tiers", "kinds", "countries"):
        out[key] = [{"id": value, "papers": n} for value, n in ranked(key)]
    return out


# ---------------------------------------------------------------------------- writing
def _index_envelope(window: Dict[str, Any], records: Sequence[Dict[str, Any]], stats: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": window,
        "count": len(records),
        "total_in_window": stats.get("total_in_window", len(records)),
        "dropped": {"by_cap": stats.get("dropped_by_cap", 0), "by_budget": stats.get("dropped_by_budget", 0)},
        "snippet_chars": SNIPPET_CHARS,
        "schema": SCHEMA_LEGEND,
        "records": list(records),
    }


def write_files(result: Dict[str, Any], out_dir: Path) -> Dict[str, int]:
    """Write the index, the vocabulary and the page into `out_dir`. Returns {relative path: bytes}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, int] = {}

    def put(rel: str, text: str) -> None:
        path = out_dir / rel
        path.write_text(text, encoding="utf-8")
        written[rel] = path.stat().st_size

    put(INDEX_FILE, _dumps(_index_envelope(result["window"], result["records"], result["stats"])))
    put(ENTITIES_FILE, _dumps({
        "generated": result["generated"], "window": result["window"], "count": len(result["records"]),
        **result["entities"],
    }))
    put(PAGE_FILE, render_page(result))
    return written


def carded_ids(data: Optional[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Entity ids that have a card in a dashboard build (its `instruments` / `companies` lists)."""
    if not data:
        return {}
    return {
        "instrument": {str(e.get("id")) for e in data.get("instruments") or [] if e.get("id")},
        "company": {str(e.get("id")) for e in data.get("companies") or [] if e.get("id")},
    }


def write_search_site(
    db_path: Path,
    out_dir: Path,
    data: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
    days: int = DEFAULT_DAYS,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_bytes: Optional[int] = DEFAULT_MAX_BYTES,
    snippet_chars: int = SNIPPET_CHARS,
) -> Dict[str, int]:
    """Open the store read-only, build the index and write the three files. Returns {relative path: bytes}.

    `data` is the dashboard's collected dataset when called from `dashboard.build` (for card-aware
    entity links); the standalone command passes nothing.
    """
    conn, tmp = _dash.connect_readonly(Path(db_path))
    try:
        result = collect_index(conn, days=days, today=today, max_records=max_records, max_bytes=max_bytes,
                               snippet_chars=snippet_chars, carded=carded_ids(data))
    finally:
        conn.close()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    files = write_files(result, Path(out_dir))
    log.info("search index: %d records (%d in window), %d bytes", result["stats"]["records"],
             result["stats"]["total_in_window"], files.get(INDEX_FILE, 0))
    return files


def build_search(db_path: Path, out_dir: Path, **kwargs: Any) -> Dict[str, Any]:
    """The standalone command's entry point: files written plus the index statistics."""
    conn, tmp = _dash.connect_readonly(Path(db_path))
    try:
        result = collect_index(conn, **kwargs)
    finally:
        conn.close()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    files = write_files(result, Path(out_dir))
    return {
        "out": str(Path(out_dir)),
        "generated": result["generated"],
        "window": result["window"],
        "stats": result["stats"],
        "entities": {k: len(v) for k, v in result["entities"].items()},
        "files": files,
        "bytes": sum(files.values()),
    }


# ---------------------------------------------------------------------------- the page
def render_page(result: Dict[str, Any]) -> str:
    """The search page, in the dashboard's shared shell (header, nav, theme, footer)."""
    stats, window = result["stats"], result["window"]
    cap_note = ""
    dropped = int(stats.get("dropped_by_cap", 0)) + int(stats.get("dropped_by_budget", 0))
    if dropped:
        cap_note = (f" The {dropped:,} lowest-ranked papers in the window are not indexed, to keep the index small; "
                    "the full store answers through the CLI or the JSON API.")
    lede = (
        f'{stats["records"]:,} papers from {window["start"]} to {window["end"]} ({stats["total_in_window"]:,} in the '
        f'window), searched in the browser. Each keystroke matches word prefixes in titles, abstract excerpts, venues '
        f'and the instruments, companies and subgroups attributed to each paper; facets narrow the list; the address '
        f'bar carries the search, so a result list is a shareable link. Nothing leaves the page.{cap_note}'
    )
    payload = {
        "index": INDEX_FILE, "entities": ENTITIES_FILE, "window": window,
        "count": stats["records"], "total_in_window": stats["total_in_window"], "generated": result["generated"],
    }
    return _dash._page("Search", PAGE_FILE, lede, PAGE_BODY, payload, PAGE_SCRIPT, result["generated"])


PAGE_BODY = """  <style>
  .search-top .filters { gap: 10px 12px; }
  .search-top input[type="search"] { flex: 1 1 320px; }
  .search-top select { flex: 0 1 180px; }
  .search-layout { display: grid; grid-template-columns: 272px minmax(0, 1fr); gap: 16px; align-items: start; }
  @media (max-width: 860px) { .search-layout { grid-template-columns: minmax(0, 1fr); } .search-facets { order: 2; } }
  .facet { margin: 0 0 12px; padding: 10px 14px; }
  .facet summary { font-weight: 600; color: var(--text-primary); font-size: 14px; }
  .facet ul { list-style: none; margin: 8px 0 0; padding: 0; max-height: 264px; overflow-y: auto; }
  .facet li label { display: flex; gap: 8px; align-items: baseline; font-size: 13px; padding: 2px 0; cursor: pointer; color: var(--text-primary); }
  .facet li .name { overflow-wrap: anywhere; }
  .facet .count { margin-left: auto; color: var(--text-muted); font-variant-numeric: tabular-nums; }
  .facet input[type="search"], .facet input[type="date"] {
    font: inherit; font-size: 13px; width: 100%; padding: 4px 7px; margin-top: 8px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--page); color: var(--text-primary);
  }
  .facet .more { font: inherit; font-size: 12px; margin-top: 6px; background: none; border: 0; color: var(--series-1); cursor: pointer; padding: 0; }
  .search-main { min-width: 0; }
  .hit { padding: 12px 0; border-bottom: 1px solid var(--grid); }
  .hit:last-child { border-bottom: 0; }
  .hit h3 { font-size: 15px; margin: 0 0 4px; font-weight: 600; overflow-wrap: anywhere; line-height: 1.4; }
  .hit .meta { margin: 0 0 5px; font-size: 12.5px; color: var(--text-secondary); display: flex; flex-wrap: wrap; gap: 3px 10px; align-items: baseline; }
  .hit .snip { margin: 0 0 7px; font-size: 13.5px; color: var(--text-secondary); overflow-wrap: anywhere; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .chip { font: inherit; font-size: 12px; border: 1px solid var(--border); border-radius: 999px; padding: 1px 9px;
          background: transparent; color: var(--text-secondary); cursor: pointer; max-width: 100%; overflow-wrap: anywhere; text-align: left; }
  .chip.in { border-color: var(--series-1); } .chip.co { border-color: var(--series-5); } .chip.sg { border-color: var(--series-3); }
  .chip.on, .chip:hover { color: var(--text-primary); background: var(--page); }
  .chip.on { border-width: 2px; padding: 0 8px; }
  .chip-link { font-size: 12px; text-decoration: none; margin-left: -3px; }
  .badge { border: 1px solid var(--border); border-radius: 4px; padding: 0 5px; font-size: 11.5px; white-space: nowrap; }
  .badge.tier-A { border-color: var(--series-4); } .badge.tier-B { border-color: var(--series-7); }
  .active { margin: 0 0 10px; }
  .active:empty { display: none; }
  .status { display: flex; flex-wrap: wrap; gap: 4px 12px; }
  .hl { background: transparent; color: inherit; font-weight: 600; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  </style>
  <section class="card search-top">
    <h2>Search the window</h2>
    <div class="filters">
      <label for="sq">Query</label>
      <input type="search" id="sq" list="sq-suggest" placeholder="word prefixes, e.g. xenium spatial, novaseq, deepmind" autocomplete="off" autocapitalize="off" spellcheck="false">
      <datalist id="sq-suggest"></datalist>
      <label for="ssort">Sort</label>
      <select id="ssort">
        <option value="relevance">relevance</option>
        <option value="score">rank score</option>
        <option value="date">date</option>
        <option value="cites" id="ssort-cites" hidden>citations</option>
      </select>
      <button type="button" id="sclear" class="theme-toggle">Clear</button>
    </div>
    <p class="note status" id="sstatus" aria-live="polite">Loading the index…</p>
  </section>
  <div class="search-layout">
    <aside class="search-facets" id="sfacets" aria-label="Filters"></aside>
    <section class="search-main">
      <div class="chips active" id="sactive"></div>
      <div id="sresults"></div>
      <p><button type="button" id="smore" class="theme-toggle" hidden>Show more</button></p>
    </section>
  </div>
"""


PAGE_SCRIPT = r"""(function () {
  "use strict";
  /* lit-radar search page: a prefix-token index built in the browser from search-index.json.
     Everything from the data files is written with textContent or validated attributes; no HTML
     string is ever assembled from data. */
  var cfg = {};
  try { cfg = JSON.parse(document.getElementById("page-data").textContent) || {}; } catch (e) { cfg = {}; }

  var FACETS = [
    { key: "in", label: "Instruments", field: "in", vocab: "instruments", list: true, filter: true },
    { key: "co", label: "Companies", field: "co", vocab: "companies", list: true, filter: true },
    { key: "sg", label: "Subgroups", field: "sg", vocab: "subgroups", list: true },
    { key: "tier", label: "Journal tier", field: "tier", vocab: "tiers" },
    { key: "kind", label: "Kind", field: "k", vocab: "kinds" },
    { key: "src", label: "Source", field: "src", vocab: "sources" },
    { key: "cc", label: "Country", field: "cc", vocab: "countries", list: true, filter: true }
  ];
  var TIER_LABELS = { A: "tier A", B: "tier B", C: "tier C", preprint: "preprint", other: "unranked venue" };
  var SHORT_LIST = 12, PAGE = 50, MORE = 100, MAX_EXPAND = 600;

  var docs = [], names = {}, pages = {}, vocabCounts = {}, post = Object.create(null), vocab = [];
  var facetCounts = {};
  var state = { q: "", sort: "relevance", f: {}, from: "", to: "", shown: PAGE, expanded: {} };
  FACETS.forEach(function (f) { state.f[f.key] = []; });
  var matches = [];
  var hasCites = false;

  var $ = function (id) { return document.getElementById(id); };
  function el(tag, text, attrs) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    if (attrs) { Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); }); }
    return node;
  }
  function isWebUrl(href) { return typeof href === "string" && /^https?:\/\//i.test(href); }
  /* a page link from the vocabulary is a sibling .html file, never a scheme or a path */
  function isLocalPage(href) { return typeof href === "string" && /^[\w.-]+\.html(#[\w.:%-]*)?$/.test(href); }
  function doiUrl(doi) {
    return "https://doi.org/" + String(doi).split("/").map(encodeURIComponent).join("/");
  }
  function fold(s) {
    s = String(s === undefined || s === null ? "" : s).toLowerCase();
    try { s = s.normalize("NFKD").replace(/[̀-ͯ]/g, ""); } catch (e) { /* no normalize: fine */ }
    return s;
  }
  var splitter, keepSeparators;
  try { splitter = new RegExp("[^\\p{L}\\p{N}]+", "u"); } catch (e) { splitter = /[^a-z0-9]+/; }
  keepSeparators = new RegExp("(" + splitter.source + ")", splitter.flags || "");
  function tokens(s) { return fold(s).split(splitter).filter(Boolean); }

  /* ------------------------------------------------------------- index */
  function buildIndex() {
    docs.forEach(function (d, i) {
      var w = new Map();
      function add(text, weight) {
        tokens(text).forEach(function (t) { w.set(t, (w.get(t) || 0) + weight); });
      }
      add(d.t, 3);
      add(d.j, 1.5);
      add(d.a, 1);
      (d.sg || []).forEach(function (g) { add(g, 2); });
      (d.in || []).forEach(function (id) { add(names.instrument[id] || id, 2.5); add(id, 1.5); });
      (d.co || []).forEach(function (id) { add(names.company[id] || id, 2.5); add(id, 1.5); });
      (d.cc || []).forEach(function (c) { add(c, 1); });
      w.forEach(function (weight, tok) { (post[tok] || (post[tok] = [])).push(i, weight); });
    });
    vocab = Object.keys(post).sort();
  }
  function lowerBound(prefix) {
    var lo = 0, hi = vocab.length;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (vocab[mid] < prefix) { lo = mid + 1; } else { hi = mid; } }
    return lo;
  }
  function textSearch(q) {
    var terms = tokens(q);
    if (!terms.length) { return null; }
    terms = terms.filter(function (t, i) { return terms.indexOf(t) === i; });
    var n = docs.length, scores = new Float64Array(n), hits = new Uint8Array(n);
    terms.forEach(function (term) {
      var seen = new Uint8Array(n), expanded = 0;
      for (var v = lowerBound(term); v < vocab.length && expanded < MAX_EXPAND; v++, expanded++) {
        var tok = vocab[v];
        if (tok.slice(0, term.length) !== term) { break; }
        var factor = tok === term ? 1.25 : 0.45 + 0.55 * (term.length / tok.length);
        var arr = post[tok];
        for (var k = 0; k < arr.length; k += 2) { scores[arr[k]] += arr[k + 1] * factor; seen[arr[k]] = 1; }
      }
      for (var d = 0; d < n; d++) { if (seen[d]) { hits[d] += 1; } }
    });
    var out = [];
    for (var i = 0; i < n; i++) { if (hits[i] === terms.length) { out.push([i, scores[i]]); } }
    return { rows: out, terms: terms };
  }

  /* ------------------------------------------------------------- filters, sort */
  function passes(d) {
    for (var i = 0; i < FACETS.length; i++) {
      var f = FACETS[i], wanted = state.f[f.key];
      if (!wanted.length) { continue; }
      var value = d[f.field];
      if (f.list) {
        var any = false;
        for (var j = 0; j < (value || []).length; j++) { if (wanted.indexOf(value[j]) !== -1) { any = true; break; } }
        if (!any) { return false; }
      } else if (wanted.indexOf(value || "") === -1) { return false; }
    }
    if (state.from && (d.d || "") < state.from) { return false; }
    if (state.to && (d.d || "") > state.to) { return false; }
    return true;
  }
  function run() {
    var t0 = (window.performance && performance.now) ? performance.now() : 0;
    var text = textSearch(state.q), rel = null, pool;
    if (text) {
      rel = new Float64Array(docs.length);
      pool = text.rows.filter(function (r) { rel[r[0]] = r[1]; return passes(docs[r[0]]); }).map(function (r) { return r[0]; });
    } else {
      pool = [];
      for (var i = 0; i < docs.length; i++) { if (passes(docs[i])) { pool.push(i); } }
    }
    var sort = state.sort;
    if (sort === "relevance" && !text) { sort = "score"; }
    if (sort === "cites" && !hasCites) { sort = "score"; }
    pool.sort(function (a, b) {
      var A = docs[a], B = docs[b], diff;
      if (sort === "relevance") { diff = rel[b] - rel[a]; if (diff) { return diff; } }
      if (sort === "cites") { diff = (B.c || 0) - (A.c || 0); if (diff) { return diff; } }
      if (sort === "date") { if (A.d !== B.d) { return A.d < B.d ? 1 : -1; } }
      diff = (B.s === undefined ? -1e9 : B.s) - (A.s === undefined ? -1e9 : A.s);
      if (diff) { return diff; }
      if (A.d !== B.d) { return A.d < B.d ? 1 : -1; }
      return a - b;
    });
    matches = pool;
    renderResults(text ? text.terms : []);
    renderFacets();
    renderActive();
    var ms = t0 ? Math.round(((performance.now() - t0) + Number.EPSILON) * 10) / 10 : null;
    var status = $("sstatus");
    status.textContent = "";
    status.appendChild(el("span", matches.length.toLocaleString() + " of " + docs.length.toLocaleString() + " indexed papers match"));
    if (ms !== null) { status.appendChild(el("span", ms + " ms", { "class": "muted" })); }
    if (cfg.total_in_window > docs.length) {
      status.appendChild(el("span", (cfg.total_in_window - docs.length).toLocaleString() + " more in the window are not indexed", { "class": "muted" }));
    }
    writeHash();
  }

  /* ------------------------------------------------------------- rendering */
  function highlighter(terms) {
    if (!terms.length) { return function (parent, text) { parent.appendChild(document.createTextNode(text)); }; }
    return function (parent, text) {
      /* split with a capturing group keeps separators; mark the words whose folded form starts with a term */
      String(text || "").split(keepSeparators).forEach(function (piece) {
        if (!piece) { return; }
        var f = fold(piece), hit = false;
        for (var i = 0; i < terms.length; i++) { if (f.slice(0, terms[i].length) === terms[i]) { hit = true; break; } }
        parent.appendChild(hit ? el("mark", piece, { "class": "hl" }) : document.createTextNode(piece));
      });
    };
  }
  function chip(kind, id, label, extraClass) {
    var on = state.f[kind].indexOf(id) !== -1;
    var b = el("button", label, { type: "button", "class": "chip " + (extraClass || kind) + (on ? " on" : ""),
                                  "aria-pressed": on ? "true" : "false", title: (on ? "Remove filter: " : "Filter by ") + label });
    b.addEventListener("click", function () { toggle(kind, id); });
    return b;
  }
  function renderResults(terms) {
    var host = $("sresults"), more = $("smore");
    host.textContent = "";
    var mark = highlighter(terms);
    if (!matches.length) {
      host.appendChild(el("p", docs.length ? "No matches. Try a shorter prefix or clear a filter." : "The index is empty.", { "class": "empty" }));
      more.hidden = true;
      return;
    }
    var limit = Math.min(matches.length, state.shown);
    for (var i = 0; i < limit; i++) {
      var d = docs[matches[i]];
      var art = el("article", null, { "class": "hit" });
      var h = el("h3");
      var href = isWebUrl(d.u) ? d.u : (d.doi ? doiUrl(d.doi) : "");
      if (href) {
        var a = el("a", null, { href: href, target: "_blank", rel: "noopener noreferrer" });
        mark(a, d.t);
        h.appendChild(a);
      } else { mark(h, d.t); }
      art.appendChild(h);

      var meta = el("p", null, { "class": "meta" });
      if (d.j) { var jn = el("span"); mark(jn, d.j); meta.appendChild(jn); }
      if (d.d) { meta.appendChild(el("span", d.d)); }
      meta.appendChild(el("span", d.k === "preprint" ? "preprint" : "article", { "class": "badge" }));
      if (d.tier && d.tier !== "preprint" && d.tier !== "other") { meta.appendChild(el("span", TIER_LABELS[d.tier] || d.tier, { "class": "badge tier-" + d.tier })); }
      if (d.s !== undefined) { meta.appendChild(el("span", "score " + d.s, { title: "rank score" })); }
      if (d.c) { meta.appendChild(el("span", d.c + " citations")); }
      if (d.cc) { meta.appendChild(el("span", d.cc.join(" "), { title: "country of affiliation" })); }
      if (d.doi) { meta.appendChild(el("a", "DOI", { href: doiUrl(d.doi), target: "_blank", rel: "noopener noreferrer" })); }
      if (d.src) { meta.appendChild(el("span", d.src, { "class": "muted", title: "source" })); }
      art.appendChild(meta);

      if (d.a) { var p = el("p", null, { "class": "snip" }); mark(p, d.a); art.appendChild(p); }

      var chips = el("div", null, { "class": "chips" });
      (d.in || []).forEach(function (id) { chips.appendChild(chip("in", id, names.instrument[id] || id)); if (isLocalPage(pages.instrument[id])) { chips.appendChild(el("a", "↗", { href: pages.instrument[id], "class": "chip-link", title: "trend card" })); } });
      (d.co || []).forEach(function (id) { chips.appendChild(chip("co", id, names.company[id] || id)); if (isLocalPage(pages.company[id])) { chips.appendChild(el("a", "↗", { href: pages.company[id], "class": "chip-link", title: "trend card" })); } });
      (d.sg || []).forEach(function (g) { chips.appendChild(chip("sg", g, g)); });
      if (chips.childNodes.length) { art.appendChild(chips); }
      host.appendChild(art);
    }
    more.hidden = limit >= matches.length;
    more.textContent = "Show more (" + (matches.length - limit).toLocaleString() + " remaining)";
  }
  function labelFor(f, id) {
    if (f.vocab === "instruments") { return names.instrument[id] || id; }
    if (f.vocab === "companies") { return names.company[id] || id; }
    if (f.vocab === "tiers") { return TIER_LABELS[id] || id; }
    return id;
  }
  function countFacets() {
    facetCounts = {};
    FACETS.forEach(function (f) { facetCounts[f.key] = Object.create(null); });
    matches.forEach(function (i) {
      var d = docs[i];
      FACETS.forEach(function (f) {
        var v = d[f.field], c = facetCounts[f.key];
        if (f.list) { (v || []).forEach(function (x) { c[x] = (c[x] || 0) + 1; }); }
        else { c[v || ""] = (c[v || ""] || 0) + 1; }
      });
    });
  }
  /* the facet containers (details, filter box, date inputs) are built once; only the lists re-render */
  function buildFacetShells() {
    var host = $("sfacets");
    host.textContent = "";
    var dates = el("details", null, { "class": "card facet", "data-key": "date", open: "" });
    dates.appendChild(el("summary", "Date range"));
    var from = el("input", null, { type: "date", id: "sfrom", "aria-label": "from date" });
    var to = el("input", null, { type: "date", id: "sto", "aria-label": "to date" });
    from.addEventListener("change", function () { state.from = from.value; state.shown = PAGE; run(); });
    to.addEventListener("change", function () { state.to = to.value; state.shown = PAGE; run(); });
    dates.appendChild(from); dates.appendChild(to);
    host.appendChild(dates);
    FACETS.forEach(function (f) {
      if (!(vocabCounts[f.vocab] || []).length) { return; }
      var det = el("details", null, { "class": "card facet", "data-key": f.key });
      /* open by default: anything with an active filter, the short facets, instruments and subgroups;
         companies and countries start folded (long lists) */
      det.open = state.f[f.key].length > 0 || !f.list || f.key === "in" || f.key === "sg";
      det.appendChild(el("summary", f.label));
      if (f.filter) {
        var inp = el("input", null, { type: "search", placeholder: "filter " + f.label.toLowerCase(), "aria-label": "filter " + f.label });
        inp.addEventListener("input", function () { renderFacetList(f); });
        det.appendChild(inp);
      }
      det.appendChild(el("ul"));
      det.appendChild(el("div", null, { "class": "more-slot" }));
      host.appendChild(det);
    });
  }
  function renderFacetList(f) {
    var det = $("sfacets").querySelector('details[data-key="' + f.key + '"]');
    if (!det) { return; }
    var inp = det.querySelector("input[type=search]"), typed = inp ? inp.value : "";
    var ul = det.querySelector("ul"), slot = det.querySelector(".more-slot");
    var counts = facetCounts[f.key] || {};
    var rows = (vocabCounts[f.vocab] || []).map(function (v) {
      return { id: v.id, label: labelFor(f, v.id), n: counts[v.id] || 0, on: state.f[f.key].indexOf(v.id) !== -1 };
    }).filter(function (r) { return (r.on || r.n > 0) && (!typed || fold(r.label + " " + r.id).indexOf(fold(typed)) !== -1); });
    rows.sort(function (a, b) { return (b.on - a.on) || (b.n - a.n) || (a.label < b.label ? -1 : 1); });
    var showAll = state.expanded[f.key] || !!typed;
    ul.textContent = "";
    (showAll ? rows : rows.slice(0, SHORT_LIST)).forEach(function (r) {
      var li = el("li"), lab = el("label"), cb = el("input", null, { type: "checkbox" });
      cb.checked = r.on;
      cb.addEventListener("change", function () { toggle(f.key, r.id); });
      lab.appendChild(cb);
      lab.appendChild(el("span", r.label, { "class": "name" }));
      lab.appendChild(el("span", r.n.toLocaleString(), { "class": "count" }));
      li.appendChild(lab);
      ul.appendChild(li);
    });
    if (!rows.length) { ul.appendChild(el("li", "none in the current results", { "class": "muted" })); }
    slot.textContent = "";
    if (rows.length > SHORT_LIST && !typed) {
      var more = el("button", showAll ? "show fewer" : "show all " + rows.length, { type: "button", "class": "more" });
      more.addEventListener("click", function () { state.expanded[f.key] = !showAll; renderFacetList(f); });
      slot.appendChild(more);
    }
  }
  function renderFacets() {
    countFacets();
    var from = $("sfrom"), to = $("sto");
    if (from && from.value !== state.from) { from.value = state.from; }
    if (to && to.value !== state.to) { to.value = state.to; }
    FACETS.forEach(renderFacetList);
  }
  function renderActive() {
    var host = $("sactive");
    host.textContent = "";
    FACETS.forEach(function (f) {
      state.f[f.key].forEach(function (id) {
        var label = f.vocab === "instruments" ? (names.instrument[id] || id)
                  : f.vocab === "companies" ? (names.company[id] || id)
                  : f.vocab === "tiers" ? (TIER_LABELS[id] || id) : id;
        host.appendChild(chip(f.key, id, f.label + ": " + label + " ×"));
      });
    });
    if (state.from || state.to) {
      var b = el("button", "Dates: " + (state.from || "…") + " to " + (state.to || "…") + " ×", { type: "button", "class": "chip" });
      b.addEventListener("click", function () { state.from = ""; state.to = ""; state.shown = PAGE; run(); });
      host.appendChild(b);
    }
  }
  function toggle(kind, id) {
    var list = state.f[kind], at = list.indexOf(id);
    if (at === -1) { list.push(id); } else { list.splice(at, 1); }
    state.shown = PAGE;
    run();
  }

  /* ------------------------------------------------------------- URL hash state */
  function writeHash() {
    var parts = [];
    if (state.q) { parts.push("q=" + encodeURIComponent(state.q)); }
    FACETS.forEach(function (f) {
      if (state.f[f.key].length) { parts.push(f.key + "=" + state.f[f.key].map(encodeURIComponent).join(",")); }
    });
    if (state.from) { parts.push("from=" + encodeURIComponent(state.from)); }
    if (state.to) { parts.push("to=" + encodeURIComponent(state.to)); }
    if (state.sort !== "relevance") { parts.push("sort=" + encodeURIComponent(state.sort)); }
    var hash = parts.length ? "#" + parts.join("&") : "";
    if (hash !== location.hash) {
      try { history.replaceState(null, "", location.pathname + location.search + hash); } catch (e) { location.hash = hash; }
    }
  }
  function readHash() {
    var raw = location.hash.replace(/^#/, "");
    state.q = ""; state.from = ""; state.to = ""; state.sort = "relevance"; state.shown = PAGE;
    FACETS.forEach(function (f) { state.f[f.key] = []; });
    if (!raw) { return; }
    raw.split("&").forEach(function (pair) {
      var eq = pair.indexOf("="), key = eq === -1 ? pair : pair.slice(0, eq), value = eq === -1 ? "" : pair.slice(eq + 1);
      var dec = function (s) { try { return decodeURIComponent(s); } catch (e) { return ""; } };
      if (key === "q") { state.q = dec(value).slice(0, 200); }
      else if (key === "from" || key === "to") { var v = dec(value); if (/^\d{4}-\d{2}-\d{2}$/.test(v)) { state[key] = v; } }
      else if (key === "sort") { var s = dec(value); if (["relevance", "score", "date", "cites"].indexOf(s) !== -1) { state.sort = s; } }
      else if (state.f[key]) { state.f[key] = value.split(",").map(dec).filter(Boolean).slice(0, 50); }
    });
  }
  function syncControls() {
    $("sq").value = state.q;
    $("ssort").value = state.sort;
    if ($("ssort").value !== state.sort) { $("ssort").value = "relevance"; state.sort = "relevance"; }
  }

  /* ------------------------------------------------------------- theme toggle (shared shell) */
  function wireTheme() {
    var button = $("theme-toggle");
    if (!button || button.dataset.wired) { return; }
    button.dataset.wired = "1";
    var order = ["auto", "light", "dark"];
    function label() { button.textContent = "Theme: " + (document.documentElement.getAttribute("data-theme") || "auto"); }
    button.addEventListener("click", function () {
      var now = document.documentElement.getAttribute("data-theme") || "auto";
      var next = order[(order.indexOf(now) + 1) % order.length];
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("litradar-theme", next); } catch (e) { /* private mode */ }
      label();
    });
    label();
  }

  /* ------------------------------------------------------------- boot */
  function fail(message) {
    var status = $("sstatus");
    status.textContent = "";
    status.appendChild(el("span", message));
    status.appendChild(el("span", "This page reads " + cfg.index + " next to it, so it needs to be served over http(s): from a local copy run python3 -m http.server -d site 8000 and open http://localhost:8000/search.html."));
  }
  function start(index, entities) {
    docs = Array.isArray(index.records) ? index.records : [];
    names = { instrument: {}, company: {} };
    pages = { instrument: {}, company: {} };
    (entities.instruments || []).forEach(function (e) { names.instrument[e.id] = e.name || e.id; if (e.page) { pages.instrument[e.id] = e.page; } });
    (entities.companies || []).forEach(function (e) { names.company[e.id] = e.name || e.id; if (e.page) { pages.company[e.id] = e.page; } });
    vocabCounts = entities;
    hasCites = docs.some(function (d) { return d.c > 0; });
    $("ssort-cites").hidden = !hasCites;
    var dl = $("sq-suggest");
    [].concat(entities.instruments || [], entities.companies || []).slice(0, 400).forEach(function (e) {
      dl.appendChild(el("option", null, { value: e.name || e.id }));
    });
    buildIndex();
    readHash();
    syncControls();
    buildFacetShells();
    run();
    var pending = null;
    $("sq").addEventListener("input", function () {
      state.q = $("sq").value; state.shown = PAGE;
      if (pending) { cancelAnimationFrame(pending); }
      pending = requestAnimationFrame(function () { pending = null; run(); });
    });
    $("ssort").addEventListener("change", function () { state.sort = $("ssort").value; run(); });
    $("sclear").addEventListener("click", function () {
      try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { location.hash = ""; }
      readHash(); syncControls(); run(); $("sq").focus();
    });
    $("smore").addEventListener("click", function () { state.shown += MORE; renderResults(tokens(state.q)); });
    window.addEventListener("hashchange", function () { readHash(); syncControls(); run(); });
  }
  wireTheme();
  if (!window.fetch) { fail("This browser cannot load the index (no fetch)."); return; }
  Promise.all([fetch(cfg.index, { cache: "no-cache" }), fetch(cfg.entities, { cache: "no-cache" })]).then(function (responses) {
    responses.forEach(function (r) { if (!r.ok) { throw new Error(r.status + " " + r.statusText + " for " + r.url); } });
    return Promise.all(responses.map(function (r) { return r.json(); }));
  }).then(function (parsed) { start(parsed[0] || {}, parsed[1] || {}); })
    .catch(function (err) { fail("Could not load the search index: " + (err && err.message ? err.message : err) + "."); });
})();
"""
