"""OpenAlex backfill: fill the persistent corpus month by month (API) or from the public snapshot (JSONL.gz).

Two routes into the same ``papers`` table, both metadata only (title, abstract, venue, authors, institutions,
topics, country codes) and both resumable through the additive ``backfill_progress`` table created here:

1. API route: ``litradar backfill --from 2018-01 --to 2026-10 [--subgroup X ...] [--country CN ...]``

   GET https://api.openalex.org/works
       filter=primary_topic.id:T11289|T10222|T10621,
              title_and_abstract.search:"single-cell RNA" OR "scRNA-seq" OR ...,
              from_publication_date:2024-01-01,to_publication_date:2024-01-31
              [,institutions.country_code:cn]
       per-page=200 & cursor=* & select=<BACKFILL_SELECT> [& mailto=<LITRADAR_CONTACT_EMAIL>]
       header  Authorization: Bearer <OPENALEX_API_KEY>   (when configured)

   One unit of work is (subgroup, month, country). A finished unit is written to ``backfill_progress`` and
   skipped on the next run, so the command can be interrupted, run out of API budget, and simply be re-run.

   Verified live on 2026-10-17 without a key: the very first request answered HTTP 429 with
   ``{"error":"Rate limit exceeded","message":"Insufficient budget. This request has no API key, so it counts
   against the free daily budget shared by everyone on your network's IP address, and that budget is used up
   ($0 remaining; resets at midnight UTC). Use your own key instead ... Keys are free ...
   https://help.openalex.org/api/authentication/", "retryAfter":13878, "costUsd":0.001,
   "dailyRemainingUsd":0, "creditsRequired":10, "creditsRemaining":0}``.
   ``litradar.sources.openalex.works`` raises ``OpenAlexBudgetExhausted`` on that answer; this module saves the
   progress made so far and returns a report with ``stopped="budget"`` instead of a traceback.

   Because the metered API could not be paged, filter names were taken from the official documentation:
     * https://help.openalex.org/api/filtering/ : ``from_publication_date`` / ``to_publication_date``; country
       filter documented as ``institutions.country_code:fr`` (long form ``authorships.institutions.country_code``);
       ``|`` is OR inside one filter value, ``,`` is AND between filters.
     * https://help.openalex.org/api/searching/ : ``filter=<field>.search:`` is marked deprecated but "still
       works"; the recommended form is the query parameter ``search.title_and_abstract=``. Boolean operators
       must be upper-case ``AND``/``OR``/``NOT`` and phrases quoted, which is how keyword expressions are built
       here. The filter form is kept so the shared ``works()`` pager can be reused unchanged.
     * https://help.openalex.org/data/works/attributes/ : the ``grants`` property was REMOVED (replaced by
       ``funders`` and ``awards``), so ``BACKFILL_SELECT`` drops it and adds ``funders`` and ``language``.
   A one-record probe request is sent before paging so that an HTTP 400 (unknown filter or select field)
   surfaces as a clear ``BackfillError`` rather than as silently empty months.

2. Snapshot route: ``litradar backfill --snapshot-dir DIR``

   The whole OpenAlex database is published quarterly (second Wednesday of Jan/Apr/Jul/Oct), CC0, in the public
   bucket ``s3://openalex``; no AWS account is needed:
       aws s3 sync "s3://openalex/data/jsonl/works" DIR --no-sign-request
   Layout verified on 2026-10-17 with anonymous ListBucket calls to https://openalex.s3.amazonaws.com/ (the
   bucket's own README.txt and https://help.openalex.org/access/snapshot/ describe the same tree):
       README.txt  LICENSE.txt (CC0)  RELEASE_NOTES.txt (latest release 2026-10-14)  browse.html
       data/jsonl/manifest.json                     all entities, this format
       data/jsonl/works/manifest.json               {"date":"2026-10-14","format":"jsonl","entity":"works",
                                                     "record_count":464765485,"content_length":675298563638,
                                                     "files":[{"url":"s3://openalex/data/jsonl/works/updated_date=2016-06-24/part_0000.gz",
                                                               "meta":{"content_length":224592,"record_count":489}}, ...]}  (1,898 files, ~615 GB)
       data/jsonl/works/deleted_ids.csv.gz          deletion log
       data/jsonl/works/updated_date=YYYY-MM-DD/part_NNNN.gz   gzip JSON Lines, one work per line, <= 400,000 per part
       data/parquet/works/updated_date=.../part_NNNN.parquet   the same data as Parquet (not read here)
       legacy-data/works/updated_date=.../part_NNN.gz          frozen pre-2026 flat layout (+ legacy-data/works/manifest)
   The reader walks DIR recursively, streams every ``*.gz`` / ``*.jsonl`` / ``*.json`` file line by line with
   stdlib gzip + json (``manifest*`` and ``deleted_ids*`` are skipped), keeps works whose ``primary_topic.id`` is
   in a subgroup's topic list AND whose title+abstract matches one of its keywords, optionally restricted to a
   country list and a publication window, and records each finished file in ``backfill_progress``.
   Record shape verified on real parts (updated_date=2016-06-24/part_0000.gz, 2025-10-30/part_0000.gz, head of
   2026-10-14/part_0159.gz): id, doi, title, publication_date, type, language, primary_topic{id,display_name,
   score,subfield,field,domain}, topics[], abstract_inverted_index, authorships[]{author{id,display_name,orcid},
   author_position, countries[], institutions[]{id,display_name,ror,country_code,type}, raw_affiliation_strings[],
   is_corresponding}, primary_location{source{display_name,issn,type},landing_page_url,license}, funders[],
   awards[], cited_by_count, countries_distinct_count, updated_date. There is no ``grants`` key.

Every paper written by either route carries, in ``extra_json``: ``countries`` (ISO 3166-1 alpha-2 codes of all
author institutions), ``first_author_countries``, ``language``, ``subgroups`` and ``backfill_source``, so country,
language and subgroup trends can be computed later straight from the ``papers`` table (see docs/backfill.md).
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .http import Http
from .models import Paper
from .sources import openalex
from .sources.openalex import OpenAlexBudgetExhausted
from .store import Store

log = logging.getLogger(__name__)

WORKS = openalex.WORKS
COUNTRY_FILTER = "institutions.country_code"  # documented short form; long form is authorships.institutions.country_code
SEARCH_FILTER = "title_and_abstract.search"  # deprecated-but-working filter form of search.title_and_abstract
# The shared SELECT in sources/openalex.py still names `grants`, which the 2026 works object no longer has.
BACKFILL_SELECT = ",".join(
    [
        "id", "doi", "title", "publication_date", "type", "language", "primary_location", "authorships",
        "abstract_inverted_index", "topics", "primary_topic", "cited_by_count", "open_access", "funders", "biblio",
    ]
)
MAX_KEYWORDS = 40  # the request URL is limited to ~4 KB
SNAPSHOT_PROGRESS_SUBGROUP = "_snapshot_file"  # progress rows for snapshot files: month column = relative path

SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_progress (
  subgroup TEXT NOT NULL,
  month TEXT NOT NULL,
  country TEXT NOT NULL DEFAULT '',
  fetched INTEGER DEFAULT 0,
  done_at TEXT,
  PRIMARY KEY (subgroup, month, country)
);
"""

MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


class BackfillError(RuntimeError):
    """The API rejected the request shape (HTTP 400) or the arguments are invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- months ---------------------------------------
def parse_month(s: str) -> Tuple[int, int]:
    m = MONTH_RE.match((s or "").strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise BackfillError(f"month must be YYYY-MM, got {s!r}")
    return int(m.group(1)), int(m.group(2))


def months(from_month: str, to_month: str) -> List[str]:
    """Inclusive list of 'YYYY-MM' strings from `from_month` to `to_month`."""
    y0, m0 = parse_month(from_month)
    y1, m1 = parse_month(to_month)
    if (y0, m0) > (y1, m1):
        raise BackfillError(f"--from {from_month} is after --to {to_month}")
    out: List[str] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_bounds(month: str) -> Tuple[str, str]:
    """('YYYY-MM-01', 'YYYY-MM-<last day>') for one month."""
    y, m = parse_month(month)
    first = date(y, m, 1)
    last = date(y + (m == 12), 1 if m == 12 else m + 1, 1).toordinal() - 1
    return first.isoformat(), date.fromordinal(last).isoformat()


# ---------------------------------------------------------------- filters --------------------------------------
def keyword_expression(keywords: Iterable[str], limit: int = MAX_KEYWORDS) -> str:
    """'"kw one" OR "kw two"' in the boolean syntax the search documentation specifies (upper-case OR, quoted phrases)."""
    terms: List[str] = []
    for k in keywords:
        k = (k or "").strip().replace('"', " ").strip()
        if k and k not in terms:
            terms.append(k)
    return " OR ".join(f'"{t}"' for t in terms[:limit])


def subgroup_filters(group: Dict[str, Any], month: str, country: str = "") -> Dict[str, str]:
    """The filter dict for one (subgroup, month, country) unit; keys are OpenAlex filter names."""
    topics = [str(t) for t in (group.get("openalex_topics") or []) if t]
    if not topics:
        raise BackfillError(f"subgroup {group.get('id')!r} has no openalex_topics; nothing to intersect with")
    start, end = month_bounds(month)
    flt: Dict[str, str] = {
        "primary_topic.id": openalex.or_join(topics),
        "from_publication_date": start,
        "to_publication_date": end,
    }
    expr = keyword_expression(group.get("keywords") or [])
    if expr:
        flt[SEARCH_FILTER] = expr
    if country:
        flt[COUNTRY_FILTER] = country.strip().lower()
    return flt


# ---------------------------------------------------------------- tagging --------------------------------------
def country_codes(w: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(all author-institution countries, first-author countries) as sorted upper-case ISO codes."""
    allc: Set[str] = set()
    first: Set[str] = set()
    for i, a in enumerate(w.get("authorships") or []):
        codes: Set[str] = set()
        for inst in a.get("institutions") or []:
            cc = inst.get("country_code") if isinstance(inst, dict) else None
            if cc:
                codes.add(str(cc).upper())
        for cc in a.get("countries") or []:
            if cc:
                codes.add(str(cc).upper())
        allc |= codes
        if i == 0 or a.get("author_position") == "first":
            first |= codes
    return sorted(allc), sorted(first)


def tag_paper(p: Paper, w: Dict[str, Any], subgroups: Sequence[str], source: str) -> Paper:
    """Add country / language / subgroup tags to a Paper built by openalex.to_paper (metadata only)."""
    allc, first = country_codes(w)
    p.extra["countries"] = allc
    p.extra["first_author_countries"] = first
    p.extra["language"] = w.get("language") or ""
    p.extra["subgroups"] = sorted(set(subgroups))
    p.extra["backfill_source"] = source
    p.extra["work_type"] = w.get("type") or ""
    funders = [f.get("display_name") for f in (w.get("funders") or []) if isinstance(f, dict) and f.get("display_name")]
    if funders and not p.sections.get("funding"):
        p.sections["funding"] = "; ".join(dict.fromkeys(funders))
        p.extra["grants"] = list(dict.fromkeys(funders))
    return p


def _merge_extra(store: Store, p: Paper) -> None:
    """upsert_paper replaces extra_json wholesale; keep earlier subgroup tags and keys we do not set."""
    existing = store.get_paper(p.id)
    if not existing:
        return
    try:
        old = json.loads(existing.get("extra_json") or "{}") or {}
    except ValueError:
        old = {}
    merged = dict(old)
    merged.update(p.extra)
    merged["subgroups"] = sorted(set(old.get("subgroups") or []) | set(p.extra.get("subgroups") or []))
    for key in ("countries", "first_author_countries"):
        if not p.extra.get(key) and old.get(key):
            merged[key] = old[key]
    p.extra = merged


def upsert_work(store: Store, w: Dict[str, Any], subgroups: Sequence[str], source: str) -> bool:
    """Normalise one OpenAlex work, tag it, and upsert it. Returns True when the paper was new."""
    p = tag_paper(openalex.to_paper(w), w, subgroups, source)
    _merge_extra(store, p)
    return store.upsert_paper(p)


# ---------------------------------------------------------------- progress -------------------------------------
def ensure_schema(store: Store) -> None:
    store.conn.executescript(SCHEMA)
    store.conn.commit()


def progress_done(store: Store, subgroup: str, month: str, country: str = "") -> bool:
    row = store.conn.execute(
        "SELECT done_at FROM backfill_progress WHERE subgroup=? AND month=? AND country=?", (subgroup, month, country or "")
    ).fetchone()
    return bool(row and row[0])


def mark_progress(store: Store, subgroup: str, month: str, country: str, fetched: int) -> None:
    store.conn.execute(
        """INSERT INTO backfill_progress (subgroup, month, country, fetched, done_at) VALUES (?,?,?,?,?)
           ON CONFLICT(subgroup, month, country) DO UPDATE SET fetched=excluded.fetched, done_at=excluded.done_at""",
        (subgroup, month, country or "", int(fetched), _now()),
    )
    store.conn.commit()


def progress_summary(store: Store) -> List[Dict[str, Any]]:
    ensure_schema(store)
    cur = store.conn.execute(
        """SELECT subgroup, country, COUNT(*) AS units, SUM(fetched) AS fetched, MIN(month) AS first_month,
                  MAX(month) AS last_month, MAX(done_at) AS last_done
           FROM backfill_progress GROUP BY subgroup, country ORDER BY subgroup, country"""
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------- API route ------------------------------------
def select_subgroups(all_groups: Sequence[Dict[str, Any]], wanted: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    if not wanted:
        return [g for g in all_groups if g.get("id")]
    by_id = {g.get("id"): g for g in all_groups}
    missing = [w for w in wanted if w not in by_id]
    if missing:
        raise BackfillError(f"unknown subgroup id(s): {', '.join(missing)}; known: {', '.join(sorted(k for k in by_id if k))}")
    return [by_id[w] for w in wanted]


def plan(groups: Sequence[Dict[str, Any]], from_month: str, to_month: str, countries: Sequence[str] = ()) -> List[Tuple[str, str, str]]:
    """Every (subgroup id, month, country) unit for the request, in execution order."""
    cs = [c.strip().upper() for c in countries if c and c.strip()] or [""]
    return [(g["id"], m, c) for g in groups for m in months(from_month, to_month) for c in cs]


def probe(http: Http, filters: Dict[str, str], mailto: str = "", api_key: str = "", select: str = BACKFILL_SELECT) -> Optional[int]:
    """One per-page=1 request. Returns meta.count, raises BackfillError on HTTP 400 and OpenAlexBudgetExhausted on
    budget / rate-limit answers; None when the response is unusable (network failure, non-JSON)."""
    params: Dict[str, Any] = {"filter": ",".join(f"{k}:{v}" for k, v in filters.items()), "per-page": 1, "select": select}
    if mailto:
        params["mailto"] = mailto
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = http.get(WORKS, params=params, headers=headers, ok_statuses=(200, 400, 403, 429))
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    msg = (data or {}).get("message", "") if isinstance(data, dict) else ""
    if resp.status_code == 400:
        raise BackfillError(f"OpenAlex rejected the request (HTTP 400): {msg[:300] or 'no message'}")
    if resp.status_code in (403, 429) or "budget" in msg.lower():
        raise OpenAlexBudgetExhausted(msg[:300] or f"HTTP {resp.status_code}")
    if resp.status_code != 200 or not isinstance(data, dict):
        return None
    return int(((data.get("meta") or {}).get("count")) or 0)


def run_api(
    store: Store,
    http: Http,
    groups: Sequence[Dict[str, Any]],
    from_month: str,
    to_month: str,
    countries: Sequence[str] = (),
    max_per_month: int = 2000,
    mailto: str = "",
    api_key: str = "",
    select: str = BACKFILL_SELECT,
    do_probe: bool = True,
) -> Dict[str, Any]:
    """Pull every (subgroup, month, country) unit not yet marked done. Never raises for budget exhaustion."""
    ensure_schema(store)
    by_id = {g["id"]: g for g in groups}
    units = plan(groups, from_month, to_month, countries)
    report: Dict[str, Any] = {
        "units": len(units), "skipped": 0, "done": 0, "papers_fetched": 0, "papers_new": 0, "papers_updated": 0,
        "capped_units": [], "errors": [], "stopped": None, "message": "",
    }
    probed = not do_probe
    for subgroup, month, country in units:
        if progress_done(store, subgroup, month, country):
            report["skipped"] += 1
            continue
        flt = subgroup_filters(by_id[subgroup], month, country)
        stats = getattr(http, "stats", None) or {}
        failures_before = int(stats.get("failures", 0))
        fetched = new = 0
        try:
            if not probed:
                count = probe(http, flt, mailto, api_key, select)
                probed = True
                if count is not None:
                    log.info("backfill: %s %s %s has %d works (cap %d)", subgroup, month, country or "all", count, max_per_month)
            for w in openalex.works(http, flt, mailto, api_key, max_records=max_per_month, select=select):
                fetched += 1
                if upsert_work(store, w, [subgroup], "api"):
                    new += 1
                if fetched % 200 == 0:
                    store.commit()
        except OpenAlexBudgetExhausted as exc:
            store.commit()
            report["papers_fetched"] += fetched
            report["papers_new"] += new
            report["papers_updated"] += fetched - new
            report["stopped"] = "budget"
            report["message"] = (
                f"OpenAlex budget exhausted ({str(exc)[:300]}). Progress saved: {report['done']} unit(s) finished this run, "
                f"{subgroup} {month} {country or 'all countries'} left incomplete. Re-run the same command to resume; "
                f"a free key (OPENALEX_API_KEY) has its own budget: https://help.openalex.org/api/authentication/"
            )
            log.warning(report["message"])
            return report
        store.commit()
        failures_after = int((getattr(http, "stats", None) or {}).get("failures", failures_before))
        if fetched == 0 and failures_after > failures_before:
            report["errors"].append(f"{subgroup} {month} {country or 'all'}: request failed; unit left for the next run")
            continue
        mark_progress(store, subgroup, month, country, fetched)
        report["done"] += 1
        report["papers_fetched"] += fetched
        report["papers_new"] += new
        report["papers_updated"] += fetched - new
        if fetched >= max_per_month:
            report["capped_units"].append(f"{subgroup} {month} {country or 'all'}")
            log.warning("backfill: %s %s %s hit --max-per-month %d; split by --country or raise the cap", subgroup, month, country or "all", max_per_month)
    report["message"] = f"backfill complete: {report['done']} unit(s) fetched, {report['skipped']} already done, {report['papers_new']} new papers"
    return report


# ---------------------------------------------------------------- snapshot route -------------------------------
def _keyword_regex(keywords: Iterable[str]) -> Optional[re.Pattern[str]]:
    parts = []
    for k in keywords:
        k = (k or "").strip()
        if not k:
            continue
        tokens = [re.escape(t) for t in re.split(r"[\s\-]+", k) if t]
        if tokens:
            parts.append(r"[\s\-]*".join(tokens))
    if not parts:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])", re.I)


class SnapshotMatcher:
    """Offline equivalent of the API filters: primary_topic in topics AND keyword in title+abstract, per subgroup."""

    def __init__(self, groups: Sequence[Dict[str, Any]], countries: Sequence[str] = (), from_month: str = "", to_month: str = "") -> None:
        self.groups: List[Tuple[str, Set[str], Optional[re.Pattern[str]]]] = []
        for g in groups:
            topics = {str(t) for t in (g.get("openalex_topics") or []) if t}
            if g.get("id") and topics:
                self.groups.append((g["id"], topics, _keyword_regex(g.get("keywords") or [])))
        self.countries = {c.strip().upper() for c in countries if c and c.strip()}
        self.start = month_bounds(from_month)[0] if from_month else ""
        self.end = month_bounds(to_month)[1] if to_month else ""

    def match(self, w: Dict[str, Any]) -> List[str]:
        topic = ((w.get("primary_topic") or {}).get("id") or "").rsplit("/", 1)[-1]
        if not topic:
            return []
        candidates = [(gid, rx) for gid, topics, rx in self.groups if topic in topics]
        if not candidates:
            return []
        pub = w.get("publication_date") or ""
        if self.start and (not pub or pub < self.start):
            return []
        if self.end and (not pub or pub > self.end):
            return []
        if self.countries:
            allc, _ = country_codes(w)
            if not self.countries & set(allc):
                return []
        text = ""
        hits: List[str] = []
        for gid, rx in candidates:
            if rx is None:
                hits.append(gid)
                continue
            if not text:
                text = (w.get("title") or "") + "\n" + openalex.reconstruct_abstract(w.get("abstract_inverted_index"))
            if rx.search(text):
                hits.append(gid)
        return hits


def snapshot_files(root: Path) -> List[Path]:
    """Data files under `root` (a directory or a single file), sorted; manifests and deletion logs excluded."""
    root = Path(root)
    if root.is_file():
        return [root]
    out: List[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.startswith("manifest") or name.startswith("deleted_ids"):
            continue
        if name.endswith(".gz") or name.endswith(".jsonl") or name.endswith(".json"):
            out.append(p)
    return out


def iter_jsonl(path: Path, counters: Optional[Dict[str, int]] = None) -> Iterator[Dict[str, Any]]:
    """Stream dict records from a JSON Lines file (.gz or plain); malformed lines are counted and skipped."""
    opener = gzip.open if str(path).lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:  # type: ignore[arg-type]
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if counters is not None:
                counters["lines"] = counters.get("lines", 0) + 1
            try:
                rec = json.loads(line)
            except ValueError:
                if counters is not None:
                    counters["malformed"] = counters.get("malformed", 0) + 1
                continue
            if isinstance(rec, dict):
                yield rec
            elif counters is not None:
                counters["malformed"] = counters.get("malformed", 0) + 1


def run_snapshot(
    store: Store,
    snapshot_dir: Path,
    groups: Sequence[Dict[str, Any]],
    countries: Sequence[str] = (),
    from_month: str = "",
    to_month: str = "",
    commit_every: int = 500,
) -> Dict[str, Any]:
    """Filter snapshot works files into the store. Each finished file is recorded so a re-run skips it."""
    ensure_schema(store)
    root = Path(snapshot_dir)
    if not root.exists():
        raise BackfillError(f"snapshot path does not exist: {root}")
    matcher = SnapshotMatcher(groups, countries, from_month, to_month)
    if not matcher.groups:
        raise BackfillError("no subgroup with openalex_topics to filter on")
    files = snapshot_files(root)
    report: Dict[str, Any] = {"files": len(files), "files_done": 0, "files_skipped": 0, "lines": 0, "malformed": 0, "matched": 0, "papers_new": 0, "papers_updated": 0, "errors": []}
    for path in files:
        rel = str(path.relative_to(root)) if root.is_dir() else path.name
        if progress_done(store, SNAPSHOT_PROGRESS_SUBGROUP, rel):
            report["files_skipped"] += 1
            continue
        counters: Dict[str, int] = {}
        matched = new = 0
        try:
            for w in iter_jsonl(path, counters):
                hits = matcher.match(w)
                if not hits:
                    continue
                matched += 1
                if upsert_work(store, w, hits, "snapshot"):
                    new += 1
                if matched % commit_every == 0:
                    store.commit()
        except (OSError, EOFError, UnicodeDecodeError) as exc:  # truncated download, not gzip, unreadable
            store.commit()
            report["errors"].append(f"{rel}: {exc.__class__.__name__}: {str(exc)[:120]}")
            log.warning("backfill snapshot: %s unreadable (%s); left for the next run", rel, exc)
            continue
        store.commit()
        mark_progress(store, SNAPSHOT_PROGRESS_SUBGROUP, rel, "", matched)
        report["files_done"] += 1
        report["lines"] += counters.get("lines", 0)
        report["malformed"] += counters.get("malformed", 0)
        report["matched"] += matched
        report["papers_new"] += new
        report["papers_updated"] += matched - new
        log.info("backfill snapshot: %s -> %d of %d records kept", rel, matched, counters.get("lines", 0))
    report["message"] = f"snapshot import: {report['files_done']} file(s) read, {report['files_skipped']} already done, {report['matched']} works kept, {report['papers_new']} new"
    return report
