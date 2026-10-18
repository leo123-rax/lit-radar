"""OpenAlex backfill: fill the persistent corpus month by month (API) or from the public snapshot (JSONL.gz).

Two routes into the same ``papers`` table, both metadata only (title, abstract, venue, authors, institutions,
topics, country codes) and both resumable through the additive ``backfill_progress`` table created here:

1. API route: ``litradar backfill --from 2018-01 --to 2026-10 [--subgroup X ...] [--country CN ...] [--topics-only]``

   GET https://api.openalex.org/works
       filter=primary_topic.id:T11289|T10222|T10621,
              from_publication_date:2024-01-01,to_publication_date:2024-01-31
              [,institutions.country_code:cn]
       search.title_and_abstract="single-cell RNA" OR "scRNA-seq" OR ...        (omitted with --topics-only)
       per-page=200 & cursor=* & select=<BACKFILL_SELECT> [& mailto=<LITRADAR_CONTACT_EMAIL>]
       header  Authorization: Bearer <OPENALEX_API_KEY>   (when configured)

   One unit of work is (subgroup, month, country). A unit is written to ``backfill_progress`` ONLY when the pager
   reported that the API returned its last page (or the --max-per-month cap stopped it; the cap is stored with the
   row and a larger cap reopens the unit). Budget exhaustion, transport failures, non-JSON bodies, HTTP errors and
   persistent rate limits leave the unit without a row, so the next run picks it up again. Papers already upserted
   in an interrupted unit are kept (upserts are idempotent).

   Verified live on 2026-10-17 (20:35 UTC) without a key, with exactly the request shape above: HTTP 429
   ``{"error":"Rate limit exceeded","message":"Insufficient budget. This request has no API key, so it counts
   against the free daily budget shared by everyone on your network's IP address, and that budget is used up
   ($0 remaining; resets at midnight UTC). Use your own key instead ... Keys are free ...
   https://help.openalex.org/api/authentication/", "retryAfter":12246, "costUsd":0.001,
   "dailyRemainingUsd":0, "creditsRequired":10, "creditsRemaining":0}``.
   ``litradar.sources.openalex.works`` raises ``OpenAlexBudgetExhausted`` on that answer; this module saves the
   progress made so far and returns a report with ``stopped="budget"`` (CLI exit code 3) instead of a traceback.
   A rejected request (HTTP 400/404: invalid select field, malformed filter) raises ``OpenAlexRequestError`` with
   the API's message; the run stops the same way with ``stopped="request"`` because every later unit would fail
   identically.

   Because the metered API could not be paged, the request shape follows the official documentation (re-read
   2026-10-17):
     * https://help.openalex.org/api/searching/ : works accept the scoped query parameters ``search.title``,
       ``search.title_and_abstract`` and ``search.title_abstract_keywords`` (each once per request, combined with
       AND); "Use AND, OR, NOT (uppercase) for complex queries"; double quotes mark an exact phrase; "Words not
       separated by boolean operators are treated as AND". The legacy ``filter=<field>.search:`` syntax "still works
       but is no longer recommended"; the only filter form the page names is ``title_abstract_keywords.search``, and
       OR between quoted phrases inside a filter value is documented only for ``raw_author_name.search``. Hence the
       keyword intersection is sent as the ``search.title_and_abstract`` parameter (constant ``SEARCH_PARAM``);
       ``SEARCH_FILTER`` keeps the legacy filter name only as a fallback constant.
     * https://help.openalex.org/api/filtering/ : comma = AND between filters, ``|`` = OR inside one filter value
       (up to 100 values), ``!`` negates; examples ``from_publication_date:2022-01-01,to_publication_date:2022-01-26``
       and ``institutions.country_code:fr|gb`` (long alias ``authorships.institutions.country_code``).
     * https://help.openalex.org/data/works/attributes/ : the ``grants`` property was REMOVED (replaced by
       ``funders`` and ``awards``), so ``BACKFILL_SELECT`` drops it and adds ``funders`` and ``language``.
   Guards on the first unit of a run: (a) a per-page=1 probe turns an HTTP 400 (unknown filter, select field or
   query parameter) into a clear ``BackfillError``; (b) the boolean syntax is checked with set arithmetic that
   cannot false-alarm: count("kw1" OR "kw2" ...) must be >= count("kw1") alone, otherwise OR is not being honoured
   and the run stops with ``BackfillError`` instead of silently recording empty months.

2. Snapshot route: ``litradar backfill --snapshot-dir DIR [--topics-only]``

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
   The reader walks DIR recursively and streams every ``*.gz`` / ``*.jsonl`` / ``*.json`` file line by line with
   stdlib gzip + json (``manifest*`` and ``deleted_ids*`` are skipped). Lines are read as bytes and parsed only when
   they name one of the configured topic ids (``openalex.org/T#####"``; real parts are compact JSON with plain
   slashes, and escaped ``\\/`` is tolerated too), which skips the JSON decode for the vast majority of the
   464,765,485 records. A parsed work is kept when its ``primary_topic.id`` is in a subgroup's topic list AND
   (a subgroup keyword occurs in title+abstract, OR the work's ``language`` is not English, OR --topics-only),
   optionally restricted to a country list and a publication window; each finished file is recorded in
   ``backfill_progress``. Truncated, corrupted (zlib.error) or non-gzip files are reported and left for the next run.
   Record shape verified on real parts (updated_date=2016-06-24/part_0000.gz, 2025-10-30/part_0000.gz, head of
   2026-10-14/part_0159.gz): id, doi, title, publication_date, type, language, primary_topic{id,display_name,
   score,subfield,field,domain}, topics[], abstract_inverted_index, authorships[]{author{id,display_name,orcid},
   author_position, countries[], institutions[]{id,display_name,ror,country_code,type}, raw_affiliation_strings[],
   is_corresponding}, primary_location{source{display_name,issn,type},landing_page_url,license}, funders[],
   awards[], cited_by_count, countries_distinct_count, updated_date. There is no ``grants`` key.

Every paper written by either route carries, in ``extra_json``: ``countries`` (ISO 3166-1 alpha-2 codes of all
author institutions), ``first_author_countries``, ``language``, ``subgroups``, ``backfill_source`` and ``match``
(``"topic+keyword"`` or ``"topic"``), so country, language and subgroup trends can be computed later straight from
the ``papers`` table (see docs/backfill.md).
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .http import Http
from .models import Paper
from .sources import openalex
from .sources.openalex import OpenAlexBudgetExhausted, OpenAlexRequestError
from .store import Store

log = logging.getLogger(__name__)

WORKS = openalex.WORKS
COUNTRY_FILTER = "institutions.country_code"  # documented short form; long form is authorships.institutions.country_code
SEARCH_PARAM = "search.title_and_abstract"  # the documented scoped-search query parameter (help.openalex.org/api/searching/)
SEARCH_FILTER = "title_and_abstract.search"  # legacy filter form ("still works but is no longer recommended"); fallback only
# The shared SELECT in sources/openalex.py still names `grants`, which the 2026 works object no longer has.
BACKFILL_SELECT = ",".join(
    [
        "id", "doi", "title", "publication_date", "type", "language", "primary_location", "authorships",
        "abstract_inverted_index", "topics", "primary_topic", "cited_by_count", "open_access", "funders", "biblio",
    ]
)
MAX_KEYWORDS = 40  # the request URL is limited to ~4 KB
SNAPSHOT_PROGRESS_SUBGROUP = "_snapshot_file"  # progress rows for snapshot files: month column = relative path
MATCH_TOPIC_KEYWORD = "topic+keyword"
MATCH_TOPIC = "topic"

SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_progress (
  subgroup TEXT NOT NULL,
  month TEXT NOT NULL,
  country TEXT NOT NULL DEFAULT '',
  fetched INTEGER DEFAULT 0,
  done_at TEXT,
  cap INTEGER DEFAULT 0,
  PRIMARY KEY (subgroup, month, country)
);
"""

MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


class BackfillError(RuntimeError):
    """The API rejected the request shape (HTTP 400), the boolean syntax is not honoured, or the arguments are invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label(subgroup: str, month: str, country: str) -> str:
    return f"{subgroup} {month} {country or 'all'}"


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
_KEYWORD_STRIP = re.compile(r'[",|:]+')  # quote = phrase delimiter; , | : are OpenAlex filter syntax


def keyword_terms(keywords: Iterable[str], limit: int = MAX_KEYWORDS) -> List[str]:
    """Cleaned, de-duplicated keyword phrases (filter/phrase syntax characters replaced by spaces)."""
    terms: List[str] = []
    for k in keywords:
        k = " ".join(_KEYWORD_STRIP.sub(" ", k or "").split())
        if k and k not in terms:
            terms.append(k)
    return terms[:limit]


def keyword_expression(keywords: Iterable[str], limit: int = MAX_KEYWORDS) -> str:
    """'"kw one" OR "kw two"' in the documented search syntax (upper-case OR, quoted phrases)."""
    return " OR ".join(f'"{t}"' for t in keyword_terms(keywords, limit))


def subgroup_filters(group: Dict[str, Any], month: str, country: str = "", topics_only: bool = False) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(filters, extra query parameters) for one (subgroup, month, country) unit.

    Filters use OpenAlex filter names; the keyword intersection travels separately as the documented
    ``search.title_and_abstract`` parameter (empty dict with --topics-only or when the subgroup has no keywords)."""
    topics = [str(t) for t in (group.get("openalex_topics") or []) if t]
    if not topics:
        raise BackfillError(f"subgroup {group.get('id')!r} has no openalex_topics; nothing to intersect with")
    start, end = month_bounds(month)
    flt: Dict[str, str] = {
        "primary_topic.id": openalex.or_join(topics),
        "from_publication_date": start,
        "to_publication_date": end,
    }
    if country:
        flt[COUNTRY_FILTER] = country.strip().lower()
    params: Dict[str, str] = {}
    expr = "" if topics_only else keyword_expression(group.get("keywords") or [])
    if expr:
        params[SEARCH_PARAM] = expr
    return flt, params


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


def tag_paper(p: Paper, w: Dict[str, Any], subgroups: Sequence[str], source: str, match: str = MATCH_TOPIC_KEYWORD) -> Paper:
    """Add country / language / subgroup / match tags to a Paper built by openalex.to_paper (metadata only)."""
    allc, first = country_codes(w)
    p.extra["countries"] = allc
    p.extra["first_author_countries"] = first
    p.extra["language"] = w.get("language") or ""
    p.extra["subgroups"] = sorted(set(subgroups))
    p.extra["backfill_source"] = source
    p.extra["match"] = match
    p.extra["work_type"] = w.get("type") or ""
    funders = [f.get("display_name") for f in (w.get("funders") or []) if isinstance(f, dict) and f.get("display_name")]
    if funders and not p.sections.get("funding"):
        p.sections["funding"] = "; ".join(dict.fromkeys(funders))
        p.extra["grants"] = list(dict.fromkeys(funders))
    return p


def _merge_extra(store: Store, p: Paper) -> None:
    """upsert_paper replaces extra_json wholesale; keep earlier subgroup tags, the stronger match mode and keys we do not set."""
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
    if MATCH_TOPIC_KEYWORD in (old.get("match"), p.extra.get("match")):
        merged["match"] = MATCH_TOPIC_KEYWORD
    p.extra = merged


def upsert_work(store: Store, w: Dict[str, Any], subgroups: Sequence[str], source: str, match: str = MATCH_TOPIC_KEYWORD) -> bool:
    """Normalise one OpenAlex work, tag it, and upsert it. Returns True when the paper was new."""
    p = tag_paper(openalex.to_paper(w), w, subgroups, source, match)
    _merge_extra(store, p)
    return store.upsert_paper(p)


# ---------------------------------------------------------------- progress -------------------------------------
def ensure_schema(store: Store) -> None:
    store.conn.executescript(SCHEMA)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(backfill_progress)")}
    if "cap" not in cols:  # table created by an earlier revision of this module: additive column
        store.conn.execute("ALTER TABLE backfill_progress ADD COLUMN cap INTEGER DEFAULT 0")
    store.conn.commit()


def progress_done(store: Store, subgroup: str, month: str, country: str = "", max_per_month: Optional[int] = None) -> bool:
    """True when the unit is recorded as finished. A unit that stopped at its cap counts as finished only while the
    requested `max_per_month` is not larger than the cap it was fetched with; a larger cap reopens it."""
    row = store.conn.execute(
        "SELECT done_at, fetched, cap FROM backfill_progress WHERE subgroup=? AND month=? AND country=?", (subgroup, month, country or "")
    ).fetchone()
    if not row or not row[0]:
        return False
    fetched, cap = int(row[1] or 0), int(row[2] or 0)
    if max_per_month and cap and fetched >= cap and max_per_month > cap:
        return False
    return True


def mark_progress(store: Store, subgroup: str, month: str, country: str, fetched: int, cap: int = 0) -> None:
    store.conn.execute(
        """INSERT INTO backfill_progress (subgroup, month, country, fetched, done_at, cap) VALUES (?,?,?,?,?,?)
           ON CONFLICT(subgroup, month, country) DO UPDATE SET fetched=excluded.fetched, done_at=excluded.done_at, cap=excluded.cap""",
        (subgroup, month, country or "", int(fetched), _now(), int(cap or 0)),
    )
    store.conn.commit()


def clear_progress(store: Store, subgroup_ids: Sequence[str], from_month: str, to_month: str, countries: Sequence[str] = ()) -> int:
    """Forget the progress rows of the selected API units (--redo) so the next run pulls them again. Returns rows removed."""
    ensure_schema(store)
    months(from_month, to_month)  # validates the window
    cs = [c.strip().upper() for c in countries if c and c.strip()] or [""]
    n = 0
    for sg in subgroup_ids:
        for c in cs:
            cur = store.conn.execute(
                "DELETE FROM backfill_progress WHERE subgroup=? AND month BETWEEN ? AND ? AND country=?", (sg, from_month, to_month, c)
            )
            n += cur.rowcount
    store.conn.commit()
    return n


def clear_snapshot_progress(store: Store, root: Path) -> int:
    """Forget the progress rows of the snapshot files under `root` (--redo with --snapshot-dir). Returns rows removed."""
    ensure_schema(store)
    n = 0
    for rel, _done in snapshot_plan(store, root):
        cur = store.conn.execute("DELETE FROM backfill_progress WHERE subgroup=? AND month=?", (SNAPSHOT_PROGRESS_SUBGROUP, rel))
        n += cur.rowcount
    store.conn.commit()
    return n


def progress_summary(store: Store) -> List[Dict[str, Any]]:
    ensure_schema(store)
    cur = store.conn.execute(
        """SELECT subgroup, country, COUNT(*) AS units, SUM(fetched) AS fetched, MIN(month) AS first_month,
                  MAX(month) AS last_month, MAX(done_at) AS last_done,
                  SUM(CASE WHEN cap > 0 AND fetched >= cap THEN 1 ELSE 0 END) AS capped_units
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


def probe(
    http: Http,
    filters: Dict[str, str],
    mailto: str = "",
    api_key: str = "",
    select: str = BACKFILL_SELECT,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """One per-page=1 request. Returns meta.count; raises BackfillError on HTTP 400 and OpenAlexBudgetExhausted on
    budget answers (HTTP 403 or "budget" in the message); a plain HTTP 429 is retried once after `retryAfter`
    seconds; None when the response is unusable (network failure, non-JSON, persistent rate limit)."""
    params: Dict[str, Any] = {"filter": ",".join(f"{k}:{v}" for k, v in filters.items()), "per-page": 1, "select": select}
    if mailto:
        params["mailto"] = mailto
    params.update(extra_params or {})
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for attempt in range(2):
        resp = http.get(WORKS, params=params, headers=headers, ok_statuses=(200, 400, 403, 429))
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        msg = str(data.get("message") or "") if isinstance(data, dict) else ""
        if resp.status_code == 400:
            raise BackfillError(f"OpenAlex rejected the request (HTTP 400): {msg[:300] or 'no message'}")
        if resp.status_code == 403 or "budget" in msg.lower():
            raise OpenAlexBudgetExhausted(msg[:300] or f"HTTP {resp.status_code}")
        if resp.status_code == 429:
            if attempt == 0:
                wait = openalex.retry_after_seconds(resp, data)
                log.warning("OpenAlex rate limit on probe (HTTP 429); waiting %.0fs and retrying once", wait)
                openalex._sleep(wait)
                continue
            return None
        if resp.status_code != 200 or not isinstance(data, dict):
            return None
        return int(((data.get("meta") or {}).get("count")) or 0)
    return None


def verify_keyword_syntax(
    http: Http, flt: Dict[str, str], search: Dict[str, str], mailto: str, api_key: str, select: str, label: str
) -> Optional[bool]:
    """Check on one unit that the API honours boolean OR in `search.title_and_abstract`.

    Set arithmetic that cannot false-alarm: count(all keywords OR-ed) must be >= count(first keyword alone).
    Returns True when verified, None when inconclusive (both counts 0 or a probe failed), and raises BackfillError
    when the OR-ed expression returns fewer works than one of its own terms (OR treated as AND, or the parameter
    mis-parsed). The first probe also surfaces HTTP 400 for an unknown filter / select / parameter name."""
    expr = search.get(SEARCH_PARAM, "")
    count_all = probe(http, flt, mailto, api_key, select, extra_params=search or None)
    if count_all is None:
        return None
    if not expr:
        log.info("backfill: %s has %d works (topic-only request)", label, count_all)
        return True
    first_term = expr.split(" OR ")[0]
    count_one = probe(http, flt, mailto, api_key, select, extra_params={SEARCH_PARAM: first_term})
    count_topic = probe(http, flt, mailto, api_key, select)
    if count_one is None:
        return None
    log.info(
        "backfill: %s has %d works after keywords (%s alone: %d; topics only: %s)",
        label, count_all, first_term, count_one, "n/a" if count_topic is None else count_topic,
    )
    if count_one > count_all:
        raise BackfillError(
            f"{label}: the OR-ed keyword expression returned {count_all} works but its first term {first_term} alone "
            f"returned {count_one}; OpenAlex is not honouring boolean OR in {SEARCH_PARAM} (words default to AND). "
            f"Nothing was recorded. Check the keyword expression or run with --topics-only"
        )
    if count_topic is not None and count_topic > 0 and count_all == count_topic == count_one:
        log.warning("backfill: %s: keyword search did not narrow the topic count (%d); is %s being applied?", label, count_topic, SEARCH_PARAM)
    return True if (count_all > 0 or count_one > 0) else None


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
    topics_only: bool = False,
) -> Dict[str, Any]:
    """Pull every (subgroup, month, country) unit not yet marked done. Never raises for budget exhaustion."""
    if max_per_month < 1:
        raise BackfillError(f"--max-per-month must be at least 1, got {max_per_month}")
    ensure_schema(store)
    by_id = {g["id"]: g for g in groups}
    units = plan(groups, from_month, to_month, countries)
    report: Dict[str, Any] = {
        "units": len(units), "skipped": 0, "done": 0, "papers_fetched": 0, "papers_new": 0, "papers_updated": 0,
        "capped_units": [], "errors": [], "stopped": None, "message": "",
    }
    verified = not do_probe  # the probe / boolean-syntax check runs on units until it is conclusive
    match = MATCH_TOPIC if topics_only else MATCH_TOPIC_KEYWORD
    for subgroup, month, country in units:
        if progress_done(store, subgroup, month, country, max_per_month):
            report["skipped"] += 1
            continue
        label = _label(subgroup, month, country)
        flt, search = subgroup_filters(by_id[subgroup], month, country, topics_only)
        fetched = new = 0
        state: Dict[str, Any] = {}
        try:
            if not verified:
                verified = bool(verify_keyword_syntax(http, flt, search, mailto, api_key, select, label))
            for w in openalex.works(
                http, flt, mailto, api_key, max_records=max_per_month, select=select, extra_params=search or None, state=state
            ):
                fetched += 1
                if upsert_work(store, w, [subgroup], "api", match):
                    new += 1
                if fetched % 200 == 0:
                    store.commit()
        except OpenAlexRequestError as exc:
            # the request shape is wrong (select/filter/parameter): every later unit would fail the same way
            store.commit()
            report["papers_fetched"] += fetched
            report["papers_new"] += new
            report["papers_updated"] += fetched - new
            report["stopped"] = "request"
            report["message"] = (
                f"OpenAlex rejected the request ({exc}). Nothing to retry until the query is fixed; "
                f"{report['done']} unit(s) finished this run, {label} left incomplete."
            )
            report["errors"].append(f"{label}: {exc}")
            log.warning(report["message"])
            return report
        except OpenAlexBudgetExhausted as exc:
            store.commit()
            report["papers_fetched"] += fetched
            report["papers_new"] += new
            report["papers_updated"] += fetched - new
            report["stopped"] = "budget"
            report["message"] = (
                f"OpenAlex refused the request (budget or rate limit): {str(exc)[:300]}. Progress saved: {report['done']} unit(s) "
                f"finished this run, {label} left incomplete. Re-run the same command to resume; "
                f"a free key (OPENALEX_API_KEY) has its own budget: https://help.openalex.org/api/authentication/"
            )
            log.warning(report["message"])
            return report
        store.commit()
        report["papers_fetched"] += fetched
        report["papers_new"] += new
        report["papers_updated"] += fetched - new
        if not state.get("complete") and not state.get("capped"):
            reason = state.get("error") or "pager stopped early"
            report["errors"].append(f"{label}: {reason}; {fetched} fetched, unit left for the next run")
            log.warning("backfill: %s: %s; %d fetched, unit left for the next run", label, reason, fetched)
            continue
        mark_progress(store, subgroup, month, country, fetched, cap=max_per_month)
        report["done"] += 1
        if state.get("capped"):
            report["capped_units"].append(label)
            log.warning(
                "backfill: %s hit --max-per-month %d (%d fetched); re-run with a larger --max-per-month to reopen it, or split by --country",
                label, max_per_month, fetched,
            )
    report["message"] = f"backfill complete: {report['done']} unit(s) fetched, {report['skipped']} already done, {report['papers_new']} new papers"
    if report["errors"]:
        report["message"] += f"; {len(report['errors'])} unit(s) left for the next run"
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


def topic_hint(topic_ids: Iterable[str]) -> Optional[re.Pattern[bytes]]:
    """Byte-level pre-filter: a snapshot line can only match when it names one of the topic ids."""
    ids = sorted({str(t) for t in topic_ids if t})
    if not ids:
        return None
    alts = b"|".join(re.escape(t.encode("utf-8")) for t in ids)
    return re.compile(rb'openalex\.org(?:/|\\/)(?:' + alts + rb')"')


class SnapshotMatcher:
    """Offline equivalent of the API request: primary_topic in a subgroup's topics AND (a keyword in title+abstract,
    or non-English metadata, or --topics-only)."""

    def __init__(
        self, groups: Sequence[Dict[str, Any]], countries: Sequence[str] = (), from_month: str = "", to_month: str = "", topics_only: bool = False
    ) -> None:
        self.groups: List[Tuple[str, Set[str], Optional[re.Pattern[str]]]] = []
        for g in groups:
            topics = {str(t) for t in (g.get("openalex_topics") or []) if t}
            if g.get("id") and topics:
                self.groups.append((g["id"], topics, _keyword_regex(g.get("keywords") or [])))
        self.topic_ids: Set[str] = set().union(*[t for _, t, _ in self.groups]) if self.groups else set()
        self.countries = {c.strip().upper() for c in countries if c and c.strip()}
        self.start = month_bounds(from_month)[0] if from_month else ""
        self.end = month_bounds(to_month)[1] if to_month else ""
        self.topics_only = topics_only

    def match(self, w: Dict[str, Any]) -> List[str]:
        return self.match_detail(w)[0]

    def match_detail(self, w: Dict[str, Any]) -> Tuple[List[str], str]:
        """(matching subgroup ids, mode). Mode is 'topic+keyword' when a subgroup keyword occurred in title+abstract,
        'topic' when the work was accepted on its topic alone: --topics-only, a subgroup without keywords, or metadata
        whose `language` is not English (English keywords cannot be expected in Chinese-only titles/abstracts)."""
        topic = ((w.get("primary_topic") or {}).get("id") or "").rsplit("/", 1)[-1]
        if not topic:
            return [], ""
        candidates = [(gid, rx) for gid, topics, rx in self.groups if topic in topics]
        if not candidates:
            return [], ""
        pub = w.get("publication_date") or ""
        if self.start and (not pub or pub < self.start):
            return [], ""
        if self.end and (not pub or pub > self.end):
            return [], ""
        if self.countries:
            allc, _ = country_codes(w)
            if not self.countries & set(allc):
                return [], ""
        keyword_free = self.topics_only or (str(w.get("language") or "en").lower() != "en")
        text = ""
        hits: List[str] = []
        by_keyword = False
        for gid, rx in candidates:
            if rx is not None and not self.topics_only:
                if not text:
                    text = (w.get("title") or "") + "\n" + openalex.reconstruct_abstract(w.get("abstract_inverted_index"))
                if rx.search(text):
                    hits.append(gid)
                    by_keyword = True
                    continue
            if rx is None or keyword_free:
                hits.append(gid)
        return hits, (MATCH_TOPIC_KEYWORD if by_keyword else MATCH_TOPIC) if hits else ""


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


def _relative(root: Path, path: Path) -> str:
    return str(path.relative_to(root)) if root.is_dir() else path.name


def snapshot_plan(store: Store, root: Path) -> List[Tuple[str, bool]]:
    """(relative path, already done) for every data file under `root`, in read order."""
    root = Path(root)
    if not root.exists():
        raise BackfillError(f"snapshot path does not exist: {root}")
    ensure_schema(store)
    return [(_relative(root, p), progress_done(store, SNAPSHOT_PROGRESS_SUBGROUP, _relative(root, p))) for p in snapshot_files(root)]


def iter_jsonl(path: Path, counters: Optional[Dict[str, int]] = None, hint: Optional[re.Pattern[bytes]] = None) -> Iterator[Dict[str, Any]]:
    """Stream dict records from a JSON Lines file (.gz or plain), reading bytes. Lines that do not contain `hint`
    are counted (`prefiltered`) and never JSON-decoded; malformed lines are counted (`malformed`) and skipped."""
    opener = gzip.open if str(path).lower().endswith(".gz") else open
    with opener(path, "rb") as fh:  # type: ignore[arg-type]
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if counters is not None:
                counters["lines"] = counters.get("lines", 0) + 1
            if hint is not None and hint.search(line) is None:
                if counters is not None:
                    counters["prefiltered"] = counters.get("prefiltered", 0) + 1
                continue
            try:
                rec = json.loads(line)
            except ValueError:  # includes UnicodeDecodeError
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
    topics_only: bool = False,
) -> Dict[str, Any]:
    """Filter snapshot works files into the store. Each finished file is recorded so a re-run skips it."""
    ensure_schema(store)
    root = Path(snapshot_dir)
    if not root.exists():
        raise BackfillError(f"snapshot path does not exist: {root}")
    matcher = SnapshotMatcher(groups, countries, from_month, to_month, topics_only)
    if not matcher.groups:
        raise BackfillError("no subgroup with openalex_topics to filter on")
    hint = topic_hint(matcher.topic_ids)
    files = snapshot_files(root)
    report: Dict[str, Any] = {
        "files": len(files), "files_done": 0, "files_skipped": 0, "lines": 0, "prefiltered": 0, "malformed": 0,
        "matched": 0, "papers_new": 0, "papers_updated": 0, "errors": [],
    }
    for path in files:
        rel = _relative(root, path)
        if progress_done(store, SNAPSHOT_PROGRESS_SUBGROUP, rel):
            report["files_skipped"] += 1
            continue
        counters: Dict[str, int] = {}
        matched = new = 0
        try:
            for w in iter_jsonl(path, counters, hint):
                hits, mode = matcher.match_detail(w)
                if not hits:
                    continue
                matched += 1
                if upsert_work(store, w, hits, "snapshot", mode):
                    new += 1
                if matched % commit_every == 0:
                    store.commit()
        except (OSError, EOFError, UnicodeDecodeError, zlib.error) as exc:  # truncated, corrupted, not gzip, unreadable
            store.commit()
            report["errors"].append(f"{rel}: {exc.__class__.__name__}: {str(exc)[:120]}")
            log.warning("backfill snapshot: %s unreadable (%s); left for the next run", rel, exc)
            continue
        store.commit()
        mark_progress(store, SNAPSHOT_PROGRESS_SUBGROUP, rel, "", matched)
        report["files_done"] += 1
        report["lines"] += counters.get("lines", 0)
        report["prefiltered"] += counters.get("prefiltered", 0)
        report["malformed"] += counters.get("malformed", 0)
        report["matched"] += matched
        report["papers_new"] += new
        report["papers_updated"] += matched - new
        log.info("backfill snapshot: %s -> %d of %d records kept (%d skipped before parsing)", rel, matched, counters.get("lines", 0), counters.get("prefiltered", 0))
    report["message"] = f"snapshot import: {report['files_done']} file(s) read, {report['files_skipped']} already done, {report['matched']} works kept, {report['papers_new']} new"
    return report
