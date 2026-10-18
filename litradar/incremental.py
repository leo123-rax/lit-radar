"""Live mode: incremental corpus updates and health checks.

The weekly `run` fetches a fixed look-back window and renders a digest. Live mode splits that in
two: `litradar update` (daily, or every few hours on a laptop) extends the corpus from the last
recorded run forward, and the digest / dashboard are rendered from the database on their own
schedule. This module holds the pieces both commands share:

* window computation from the `runs` table (overlap for late-indexing sources, a cap so a long
  outage does not turn into a 90-day pull, explicit overrides);
* the JSON run summary written to `<out_dir>/_work/last_update.json`;
* the health report behind `litradar status` and its exit code, including the error classification
  (failed / degraded / configuration) that decides which sources count against `--max-failing-sources`.

Nothing here touches the network; everything is unit-testable against a seeded `runs` table.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_OVERLAP_DAYS = 2      # Europe PMC indexes most PubMed records within 1-2 days; archives set first_public late
DEFAULT_MAX_DAYS = 14         # cap: a long outage is repaired by hand with --since, not by a giant pull
FIRST_RUN_DAYS = 7            # no previous run: behave like the weekly run
STALE_AFTER_DAYS = 3          # `status` exits 1 when the last run finished more than this many days ago
MAX_FAILING_SOURCES = 2       # `status` exits 1 when the last run had FAILURES on more than this many sources (see classify_error)
RECENT_RUNS = 3               # per-source table depth in `status`
RATE_WINDOW_DAYS = 14         # papers/day and archive records/day are averaged over this many days
GAP_WINDOW_DAYS = 30          # days with zero new papers are reported over this many days

# As documented in docs/live.md and the two workflow files. Cron syntax restricted to "M H * * DOW".
SCHEDULES: Tuple[Tuple[str, str, str], ...] = (
    ("daily corpus update", "20 6 * * *", ".github/workflows/daily.yml"),
    ("weekly digest", "15 11 * * 1", ".github/workflows/weekly.yml"),
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(s: str) -> date:
    if not _DATE_RE.match(s or ""):
        raise ValueError(f"expected YYYY-MM-DD, got {s!r}")
    return date.fromisoformat(s)


def parse_timestamp(s: Optional[str]) -> Optional[datetime]:
    """Parse the store's 'YYYY-MM-DDTHH:MM:SSZ' (or a bare date) into an aware UTC datetime."""
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if "T" in s:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# runs table
# ---------------------------------------------------------------------------------------------
def _parse_run(row: Tuple[Any, ...]) -> Dict[str, Any]:
    run_id, started, finished, since, until, counts_json = row
    try:
        payload = json.loads(counts_json or "{}")
    except ValueError:
        payload = {}
    # Pipeline.run stores the whole report ({since, until, counts, errors, http}); older rows may hold bare counts
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {k: v for k, v in payload.items() if isinstance(v, (int, float))}
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    return {
        "id": run_id,
        "started": started,
        "finished": finished,
        "since": since,
        "until": until,
        "counts": counts or {},
        "errors": [str(e) for e in errors],
        "http": payload.get("http") if isinstance(payload.get("http"), dict) else {},
    }


def recent_runs(conn: sqlite3.Connection, n: int = RECENT_RUNS) -> List[Dict[str, Any]]:
    """Most recent runs first."""
    cur = conn.execute("SELECT id, started, finished, since, until, counts_json FROM runs ORDER BY id DESC LIMIT ?", (int(n),))
    return [_parse_run(r) for r in cur.fetchall()]


def last_run(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    runs = recent_runs(conn, 1)
    return runs[0] if runs else None


def error_sources(errors: Sequence[str]) -> List[str]:
    """Distinct source labels from Pipeline error strings ('europepmc: ...', 'rss Nature: ...', 'feed X: ...').

    Multi-word prefixes collapse to their first word, so twenty dead RSS feeds count as one source.
    """
    out: List[str] = []
    for e in errors:
        prefix = str(e).split(":", 1)[0].strip()
        if not prefix:
            continue
        key = prefix.split()[0].lower()
        if key not in out:
            out.append(key)
    return out


# Error text that points at this installation's configuration, not at the remote source: a re-run changes nothing,
# and the fork owner already sees it in `keys configured` and in coverage's "(configuration, not an outage)" tag.
CONFIG_ERROR_MARKERS = ("not set", "not configured", "api key", "api_key", "contact_email", "litradar_", "budget", "insufficient")
# Error text that says the remote host throttled us (HTTP 429 or the client's own circuit breaker): the run went on
# without that host, the pipeline reports exactly what it skipped, and tomorrow's run retries. Degraded, not failed.
RATE_LIMIT_MARKERS = ("rate-limited", "rate limited", "http 429", "too many requests")

ERROR_CLASSES = ("failed", "degraded", "config")


def classify_error(message: str) -> str:
    """'config' (missing key / identity / budget), 'degraded' (rate-limited, host skipped after 429s) or 'failed'
    (everything else: outages, parse errors, HTTP 5xx, dead feeds). Configuration wins over rate limiting because an
    anonymous-pool 429 ("Insufficient budget") is fixed by a key, not by waiting."""
    t = (message or "").lower()
    if any(m in t for m in CONFIG_ERROR_MARKERS):
        return "config"
    if any(m in t for m in RATE_LIMIT_MARKERS):
        return "degraded"
    return "failed"


def classify_errors(errors: Sequence[str]) -> Dict[str, List[str]]:
    """Split error strings into the three classes, order preserved: {'failed': [...], 'degraded': [...], 'config': [...]}."""
    out: Dict[str, List[str]] = {c: [] for c in ERROR_CLASSES}
    for e in errors:
        out[classify_error(str(e))].append(str(e))
    return out


def sources_by_class(errors: Sequence[str]) -> Dict[str, List[str]]:
    """Distinct source labels per error class. A source with both a failure and a 429 is listed under both; the
    `status` exit rule reads only 'failed'."""
    return {c: error_sources(msgs) for c, msgs in classify_errors(errors).items()}


def dedupe_errors(errors: Sequence[str]) -> List[Tuple[str, int]]:
    """Collapse identical error strings into (message, count), first occurrence order: seven identical NIH RePORTER
    outage lines become one line with a count of 7."""
    counts: Dict[str, int] = {}
    for e in errors:
        s = str(e)
        counts[s] = counts.get(s, 0) + 1
    return list(counts.items())


def format_error_line(message: str, count: int, width: int = 160) -> str:
    return f"{message[:width]}" + (f"  (x{count})" if count > 1 else "")


def open_readonly(db_path: Path) -> Optional[sqlite3.Connection]:
    """Open an existing database read-only; None when the file does not exist (never creates it).

    A WAL-mode file copied without its -shm sidecar cannot be opened with `mode=ro` by some SQLite builds; the
    fallback is a plain connection, which is still a read, and the main file already exists either way.
    """
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return conn
    except sqlite3.OperationalError:
        return sqlite3.connect(str(p))


# ---------------------------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------------------------
def compute_window(
    last_until: Optional[str],
    today: Optional[date] = None,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    max_days: int = DEFAULT_MAX_DAYS,
    first_run_days: int = FIRST_RUN_DAYS,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Dict[str, Any]:
    """Decide what [since, until] the next incremental pull should cover.

    * `--since`/`--until` override everything; an explicit `since` is honoured as given (no cap), and an
      explicit `since` after the window end is a ValueError (never a silent 0-day window).
    * No previous run: the last `first_run_days` days.
    * Otherwise: from the last run's `until` minus `overlap_days` (sources backfill late), capped at
      `max_days` so an outage is repaired deliberately with --since rather than by one giant pull. A
      recorded `until` in the future (clock skew, a run made with --until ahead) is clamped to the end.

    Dates are inclusive on both ends in every source adapter, so an overlap of N days re-fetches N+1
    calendar days including the previous `until` itself.
    """
    if overlap_days < 0:
        raise ValueError("overlap_days must be >= 0")
    if max_days < 1:
        raise ValueError("max_days must be >= 1")
    today = today or utc_now().date()
    end = parse_date(until) if until else today
    capped = False
    if since:
        start, basis = parse_date(since), "override"
    elif last_until:
        start, basis = parse_date(last_until) - timedelta(days=overlap_days), "last_run"
    else:
        start, basis = end - timedelta(days=first_run_days), "first_run"
    if start > end:
        if basis == "override":
            raise ValueError(f"--since {start.isoformat()} is after --until {end.isoformat()}")
        start = end
    if (end - start).days > max_days and basis != "override":
        start, capped = end - timedelta(days=max_days), True
    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "days": (end - start).days,
        "basis": basis,
        "capped": capped,
        "last_until": last_until,
        "overlap_days": overlap_days,
        "max_days": max_days,
    }


# ---------------------------------------------------------------------------------------------
# run summary (digests/_work/last_update.json)
# ---------------------------------------------------------------------------------------------
# `news.*` counters that are not inserted items: filtered-out headlines and queries the run never sent
NEWS_NON_INSERT = ("news.excluded", "news.offtopic")


def is_news_insert_counter(key: str) -> bool:
    """`news.gdelt`, `news.press`, `news.nih-reporter` count stored items; `news.gdelt_skipped` /
    `news.gnews_skipped` count queries that were skipped, `news.excluded` / `news.offtopic` count rejects."""
    return key.startswith("news.") and key not in NEWS_NON_INSERT and not key.endswith("_skipped")


def news_skipped_counters(counts: Dict[str, Any]) -> Dict[str, int]:
    """`{'gdelt': 10, 'gnews': 8}` from the `news.<source>_skipped` counters (queries never sent). The companion
    `news.<source>_companies_skipped` counter is not a source and is read by `news_skipped_companies`."""
    return {
        k[len("news."):-len("_skipped")]: int(v or 0)
        for k, v in counts.items()
        if k.startswith("news.") and k.endswith("_skipped") and not k.endswith("_companies_skipped") and v
    }


def news_skipped_companies(counts: Dict[str, Any]) -> Dict[str, int]:
    """`{'gdelt': 44}` from the `news.<source>_companies_skipped` counters: companies whose batched queries were never sent."""
    suffix = "_companies_skipped"
    return {k[len("news."):-len(suffix)]: int(v or 0) for k, v in counts.items() if k.startswith("news.") and k.endswith(suffix) and v}


def format_skipped(skipped: Dict[str, int], companies: Optional[Dict[str, int]] = None) -> str:
    """'gdelt 10 queries skipped (44 companies), gnews 8 queries skipped'."""
    companies = companies or {}
    parts = []
    for k, v in sorted(skipped.items()):
        part = f"{k} {v} quer{'y' if v == 1 else 'ies'} skipped"
        if companies.get(k):
            part += f" ({companies[k]} compan{'y' if companies[k] == 1 else 'ies'})"
        parts.append(part)
    return ", ".join(parts)


def summarize_update(report: Dict[str, Any], plan: Dict[str, Any], started: str, finished: str, db_path: Path, only: Optional[Sequence[str]]) -> Dict[str, Any]:
    counts: Dict[str, Any] = report.get("counts") or {}
    errors: List[str] = [str(e) for e in (report.get("errors") or [])]
    archive = {k[len("archive."):-len("_new")]: v for k, v in counts.items() if k.startswith("archive.") and k.endswith("_new")}
    fetched = {k[len("fetched."):]: v for k, v in counts.items() if k.startswith("fetched.")}
    news = {k[len("news."):]: v for k, v in counts.items() if is_news_insert_counter(k)}
    fulltext = {k[len("fulltext."):]: v for k, v in counts.items() if k.startswith("fulltext.")}
    return {
        "started": started,
        "finished": finished,
        "since": report.get("since", plan["since"]),
        "until": report.get("until", plan["until"]),
        "window": {k: plan[k] for k in ("days", "basis", "capped", "last_until", "overlap_days", "max_days")},
        "sources": list(only) if only else "all",
        "papers_fetched": int(counts.get("papers.unique", 0) or 0),
        "papers_new": int(counts.get("papers.new", 0) or 0),
        "fetched_by_source": fetched,
        "archive_new": int(sum(int(v or 0) for v in archive.values())),
        "archive_new_by_source": archive,
        "news_new": int(sum(int(v or 0) for v in news.values())),
        "news_new_by_source": news,
        "news_queries_skipped": news_skipped_counters(counts),
        "news_companies_skipped": news_skipped_companies(counts),
        "fulltext": fulltext,
        "mentions": int(counts.get("mentions", 0) or 0),
        "tracked_hits": int(counts.get("tracked_hits", 0) or 0),
        "llm_scored": int(counts.get("llm.scored", 0) or 0),
        "errors": errors,
        "error_sources": error_sources(errors),
        "sources_by_class": sources_by_class(errors),
        "http": report.get("http") or {},
        "db_path": str(db_path),
        "db_bytes": db_size(Path(db_path)),
    }


def write_summary(summary: Dict[str, Any], out_dir: Path) -> Path:
    work = Path(out_dir) / "_work"
    work.mkdir(parents=True, exist_ok=True)
    path = work / "last_update.json"
    path.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    return path


def read_summary(out_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(out_dir) / "_work" / "last_update.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _skipped_suffix(skipped: Dict[str, int], companies: Optional[Dict[str, int]] = None) -> str:
    """' (gdelt 10 queries skipped (44 companies), gnews 8 queries skipped)' or '' when nothing was skipped."""
    if not skipped:
        return ""
    return " (" + format_skipped(skipped, companies) + ")"


def summary_paragraph(summary: Dict[str, Any]) -> str:
    w = summary.get("window", {})
    basis = {"first_run": "first run", "last_run": "continuing from the last run", "override": "explicit --since/--until"}.get(w.get("basis"), w.get("basis"))
    bits = [
        f"Updated {summary['since']}..{summary['until']} ({w.get('days')} days, {basis}{', capped' if w.get('capped') else ''}):",
        f"{summary['papers_fetched']} papers fetched, {summary['papers_new']} new to the store;",
        f"{summary['archive_new']} new archive records" + (f" ({', '.join(f'{k} {v}' for k, v in sorted(summary['archive_new_by_source'].items()))})" if summary.get("archive_new_by_source") else "") + ";",
        f"{summary['news_new']} new news items" + _skipped_suffix(summary.get("news_queries_skipped") or {}, summary.get("news_companies_skipped") or {}) + ";",
    ]
    errs = summary.get("errors") or []
    if errs:
        by_class = summary.get("sources_by_class") or sources_by_class(errs)
        detail = "; ".join(
            f"{label}: {', '.join(by_class[c])}" for c, label in (("degraded", "degraded by rate limiting"), ("config", "configuration skips"))
            if by_class.get(c)
        )
        bits.append(f"{len(errs)} error(s) on {len(summary.get('error_sources') or [])} source(s): {', '.join(summary.get('error_sources') or [])}"
                    + (f" ({detail}; not counted by `status`)" if detail else "") + ".")
    else:
        bits.append("no source errors.")
    http = summary.get("http") or {}
    if http:
        bits.append(f"HTTP: {http.get('requests', 0)} requests, {http.get('cache_hits', 0)} cache hits, {http.get('failures', 0)} failures.")
    bits.append(f"Database {summary.get('db_bytes', 0) / 1e6:.1f} MB at {summary.get('db_path')}.")
    return " ".join(bits)


# ---------------------------------------------------------------------------------------------
# health / status
# ---------------------------------------------------------------------------------------------
def db_size(db_path: Path) -> int:
    """Main file plus WAL/SHM side files, in bytes (0 when the database does not exist yet)."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def _count_by_day(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> Dict[str, int]:
    return {str(d): int(n) for d, n in conn.execute(sql, params).fetchall() if d}


def papers_per_day(conn: sqlite3.Connection, today: date, days: int = RATE_WINDOW_DAYS) -> Dict[str, Any]:
    """New papers per day by first_seen (what the updater brought in), averaged over the window."""
    start = (today - timedelta(days=days - 1)).isoformat()
    by_day = _count_by_day(conn, "SELECT substr(first_seen,1,10) AS d, COUNT(*) FROM papers WHERE substr(first_seen,1,10) >= ? GROUP BY d ORDER BY d", (start,))
    total = sum(by_day.values())
    return {"window_days": days, "total": total, "per_day": round(total / days, 1), "by_day": by_day}


def archive_per_day(conn: sqlite3.Connection, today: date, days: int = RATE_WINDOW_DAYS) -> Dict[str, Any]:
    """Archive records per day by first_public (the archives' own release date)."""
    start = (today - timedelta(days=days - 1)).isoformat()
    by_day = _count_by_day(conn, "SELECT substr(first_public,1,10) AS d, COUNT(*) FROM archive_records WHERE substr(first_public,1,10) >= ? GROUP BY d ORDER BY d", (start,))
    total = sum(by_day.values())
    return {"window_days": days, "total": total, "per_day": round(total / days, 1), "by_day": by_day}


def paper_gaps(conn: sqlite3.Connection, today: date, days: int = GAP_WINDOW_DAYS) -> Dict[str, Any]:
    """Days in the last `days` with zero papers by publication date, within the span the runs ever covered.

    Publication date (not first_seen) is used so a day that was fetched late but fetched is not a gap,
    and a day the windows never covered is.
    """
    start = today - timedelta(days=days - 1)
    first_since = conn.execute("SELECT MIN(since) FROM runs").fetchone()[0]
    coverage_start = parse_date(first_since) if first_since and _DATE_RE.match(first_since) else None
    if coverage_start is None:  # no run yet: nothing was ever meant to be covered, so nothing is a gap
        return {"window_days": days, "days_checked": 0, "coverage_start": None, "gaps": []}
    by_day = _count_by_day(conn, "SELECT pub_date AS d, COUNT(*) FROM papers WHERE pub_date >= ? AND pub_date <= ? GROUP BY d", (start.isoformat(), today.isoformat()))
    gaps: List[str] = []
    checked = 0
    for i in range(days):
        d = start + timedelta(days=i)
        if d < coverage_start:
            continue
        checked += 1
        if by_day.get(d.isoformat(), 0) == 0:
            gaps.append(d.isoformat())
    return {"window_days": days, "days_checked": checked, "coverage_start": coverage_start.isoformat() if coverage_start else None, "gaps": gaps}


def per_source_table(runs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-source counts across the given runs (most recent first) with latest-minus-previous deltas.

    A source absent from the latest run's counters (a `--sources` subset, or a source that never got
    to count anything) gets `delta: None` and `in_latest: False` rather than a large negative number.
    """
    keys: List[str] = []
    for r in runs:
        for k in r.get("counts", {}):
            if (k.startswith("fetched.") or (k.startswith("archive.") and k.endswith("_new")) or is_news_insert_counter(k)) and k not in keys:
                keys.append(k)
    rows = []
    latest_counts = runs[0].get("counts", {}) if runs else {}
    for k in sorted(keys):
        values = [int(r.get("counts", {}).get(k, 0) or 0) for r in runs]
        in_latest = k in latest_counts
        delta = (values[0] - values[1]) if (in_latest and len(values) >= 2) else None
        rows.append({"source": k, "values": values, "delta": delta, "in_latest": in_latest})
    return rows


def next_scheduled(now: Optional[datetime] = None, schedules: Sequence[Tuple[str, str, str]] = SCHEDULES) -> List[Dict[str, Any]]:
    now = now or utc_now()
    out = []
    for name, cron, where in schedules:
        nxt = next_cron(cron, now)
        out.append({"name": name, "cron": cron, "defined_in": where, "next_utc": nxt.strftime("%Y-%m-%dT%H:%MZ") if nxt else None})
    return out


def _cron_field(field: str, lo: int, hi: int) -> Optional[set]:
    if field == "*":
        return None
    vals = set()
    for part in field.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            vals.update(range(int(a), int(b) + 1))
        else:
            vals.add(int(part))
    if any(v < lo or v > hi for v in vals):
        raise ValueError(f"cron field {field!r} out of range {lo}-{hi}")
    return vals


def next_cron(cron: str, now: datetime) -> Optional[datetime]:
    """Next fire time (UTC) for a 5-field cron limited to minute, hour and day-of-week (0/7 = Sunday).

    Day-of-month and month must be '*'; that is all the two workflow schedules use.
    """
    parts = cron.split()
    if len(parts) != 5 or parts[2] != "*" or parts[3] != "*":
        raise ValueError(f"unsupported cron expression {cron!r}")
    minutes = _cron_field(parts[0], 0, 59) or set(range(60))
    hours = _cron_field(parts[1], 0, 23) or set(range(24))
    dows = _cron_field(parts[4], 0, 7)
    if dows is not None and 7 in dows:
        dows = (dows - {7}) | {0}
    t = now.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 8):
        cron_dow = (t.weekday() + 1) % 7  # python Monday=0 -> cron Sunday=0
        if t.minute in minutes and t.hour in hours and (dows is None or cron_dow in dows):
            return t
        t += timedelta(minutes=1)
    return None


def _set(value: Any) -> bool:
    return bool(str(value or "").strip())


def key_presence(settings: Any) -> Dict[str, bool]:
    """Which optional credentials are configured (non-empty). Presence only; values never leave this function.

    Mirrors the keys in .env.example that a source actually reads; the reserved ALTMETRIC_API_KEY is not
    listed because nothing uses it yet. OPENFDA_API_KEY is read from the environment by the openFDA adapter.
    """
    return {
        "LITRADAR_CONTACT_EMAIL": _set(getattr(settings, "contact_email", "")),
        "NCBI_API_KEY": _set(getattr(settings, "ncbi_api_key", "")),
        "OPENALEX_API_KEY": _set(getattr(settings, "openalex_api_key", "")),
        "OPENFDA_API_KEY": _set(getattr(settings, "openfda_api_key", None) or os.environ.get("OPENFDA_API_KEY", "")),
        "SEMANTIC_SCHOLAR_API_KEY": _set(getattr(settings, "semantic_scholar_api_key", "")),
        "ANTHROPIC_API_KEY": _set(getattr(settings, "anthropic_api_key", "")),
        "SMTP_HOST": _set(getattr(settings, "smtp_host", "")),
        "SLACK_WEBHOOK_URL": _set(getattr(settings, "slack_webhook_url", "")),
    }


def disabled_sources(radar_cfg: Dict[str, Any]) -> List[str]:
    src = (radar_cfg or {}).get("sources") or {}
    return sorted(name for name, cfg in src.items() if isinstance(cfg, dict) and cfg.get("enabled", True) is False)


def health(
    conn: sqlite3.Connection,
    db_path: Path,
    radar_cfg: Optional[Dict[str, Any]] = None,
    settings: Any = None,
    now: Optional[datetime] = None,
    last_summary: Optional[Dict[str, Any]] = None,
    stale_after_days: int = STALE_AFTER_DAYS,
    max_failing_sources: int = MAX_FAILING_SOURCES,
) -> Dict[str, Any]:
    """Everything `litradar status` prints, as one dict. `exit_code()` turns it into 0/1.

    The failing-sources rule counts a source only for errors classified 'failed' (see `classify_error`):
    configuration skips (SEC without a contact e-mail, OpenAlex without a key) and rate limiting (GDELT's 429s,
    a host the client stopped calling) are printed as their own lines but never make the exit code 1, because
    neither is an outage a re-run would repair and the pipeline degraded deliberately. Host labels are already
    mapped to their owning source by the pipeline (an RSS host is `rss`), so one dead publisher host is one
    source, not one per host.
    """
    now = now or utc_now()
    today = now.date()
    runs = recent_runs(conn, RECENT_RUNS)
    last = runs[0] if runs else None
    finished = parse_timestamp(last["finished"]) if last else None
    age_days: Optional[float] = round((now - finished).total_seconds() / 86400, 2) if finished else None
    all_sources = error_sources(last["errors"]) if last else []
    by_class = sources_by_class(last["errors"]) if last else {c: [] for c in ERROR_CLASSES}
    failing = by_class["failed"]
    problems: List[str] = []
    if last is None:
        problems.append("no runs recorded yet")
    elif age_days is None:
        problems.append("last run has no parseable finish time")
    elif age_days > stale_after_days:
        problems.append(f"last update finished {age_days:.1f} days ago (limit {stale_after_days})")
    if len(failing) > max_failing_sources:
        not_counted = "; ".join(
            f"{label}: {', '.join(by_class[c])}" for c, label in (("degraded", "not counted, rate-limited"), ("config", "not counted, configuration")) if by_class[c]
        )
        problems.append(f"last run had errors on {len(failing)} sources (limit {max_failing_sources}): {', '.join(failing)}" + (f" ({not_counted})" if not_counted else ""))
    totals = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("papers", "archive_records", "news_items", "mentions", "runs")}
    # errors from the other recent runs: a clean rss-only pass must not hide yesterday's OpenAlex failure
    earlier_errors = [
        {"id": r["id"], "finished": r["finished"], "since": r["since"], "until": r["until"], "errors": r["errors"], "error_sources": error_sources(r["errors"])}
        for r in runs[1:] if r["errors"]
    ]
    return {
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": not problems,
        "problems": problems,
        "last_update": None if last is None else {
            "started": last["started"],
            "finished": last["finished"],
            "age_days": age_days,
            "since": last["since"],
            "until": last["until"],
            "window_days": ((parse_date(last["until"]) - parse_date(last["since"])).days if _DATE_RE.match(last["since"] or "") and _DATE_RE.match(last["until"] or "") else None),
            "papers_new": int(last["counts"].get("papers.new", 0) or 0),
            "papers_fetched": int(last["counts"].get("papers.unique", 0) or 0),
            "errors": last["errors"],
            "errors_deduped": [{"error": m, "count": n, "class": classify_error(m)} for m, n in dedupe_errors(last["errors"])],
            "error_sources": all_sources,
            "failing_sources": failing,
            "degraded_sources": by_class["degraded"],
            "config_skipped_sources": by_class["config"],
            "news_queries_skipped": news_skipped_counters(last["counts"]),
            "news_companies_skipped": news_skipped_companies(last["counts"]),
            "http": last["http"],
        },
        "earlier_errors": earlier_errors,
        "last_update_json": last_summary,
        "papers": papers_per_day(conn, today),
        "archive": archive_per_day(conn, today),
        "per_source": {"runs": [{"id": r["id"], "finished": r["finished"], "since": r["since"], "until": r["until"]} for r in runs], "rows": per_source_table(runs)},
        "sources_disabled": disabled_sources(radar_cfg or {}),
        "keys_configured": key_presence(settings) if settings is not None else {},
        "db": {"path": str(db_path), "bytes": db_size(Path(db_path)), "totals": totals},
        "gaps": paper_gaps(conn, today),
        "next_scheduled": next_scheduled(now),
        "thresholds": {"stale_after_days": stale_after_days, "max_failing_sources": max_failing_sources},
    }


def exit_code(report: Dict[str, Any]) -> int:
    return 0 if report.get("ok") else 1


def format_status(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lu = report.get("last_update")
    lines.append(f"lit-radar status  ({report['checked_at']})  {'OK' if report['ok'] else 'ATTENTION'}")
    for p in report.get("problems", []):
        lines.append(f"  ! {p}")
    if lu:
        lines.append(f"last update (the most recent run only): finished {lu['finished']} ({lu['age_days']} days ago), window {lu['since']}..{lu['until']} ({lu['window_days']} days), {lu['papers_fetched']} papers fetched / {lu['papers_new']} new")
        if lu["errors"]:
            deduped = lu.get("errors_deduped") or [{"error": m, "count": n, "class": classify_error(m)} for m, n in dedupe_errors(lu["errors"])]
            lines.append(f"  errors in last run ({len(lu['errors'])}, sources: {', '.join(lu['error_sources'])}):")
            for d in deduped[:10]:
                tag = {"degraded": "  [degraded: rate-limited, not counted]", "config": "  [configuration, not counted]"}.get(d["class"], "")
                lines.append(f"    - {format_error_line(d['error'], d['count'])}{tag}")
            if len(deduped) > 10:
                lines.append(f"    ... {len(deduped) - 10} more distinct")
            counted = lu.get("failing_sources")
            if counted is not None:
                lines.append(f"  failing sources counted against the limit: {', '.join(counted) or 'none'}"
                             + (f"; degraded (rate-limited): {', '.join(lu['degraded_sources'])}" if lu.get("degraded_sources") else "")
                             + (f"; configuration skips: {', '.join(lu['config_skipped_sources'])}" if lu.get("config_skipped_sources") else ""))
        else:
            lines.append("  errors in last run: none")
        skipped = lu.get("news_queries_skipped") or {}
        if skipped:
            companies = lu.get("news_companies_skipped") or {}
            lines.append("  news queries skipped in last run (not inserted items): "
                         + ", ".join(f"{k} {v}" + (f" ({companies[k]} companies)" if companies.get(k) else "") for k, v in sorted(skipped.items())))
    else:
        lines.append("last update: none recorded")
    for er in report.get("earlier_errors") or []:
        lines.append(f"  earlier run {er['id']} (finished {er['finished']}, window {er['since']}..{er['until']}) had {len(er['errors'])} error(s) on {', '.join(er['error_sources'])}:")
        for m, n in dedupe_errors(er["errors"])[:3]:
            lines.append(f"    - {format_error_line(m, n)}")
    p, a = report["papers"], report["archive"]
    lines.append(f"papers/day (last {p['window_days']} days, by first_seen): {p['per_day']}  ({p['total']} total)")
    lines.append(f"archive records/day (last {a['window_days']} days, by first_public): {a['per_day']}  ({a['total']} total)")
    ps = report["per_source"]
    if ps["rows"]:
        hdr = "  ".join(f"run {r['id']} {r['since']}..{r['until']}" for r in ps["runs"])
        lines.append(f"per-source counts, most recent first ({hdr}); delta = latest - previous, n/a when the latest run did not pull the source:")
        for row in ps["rows"]:
            vals = "  ".join(f"{v:>7}" for v in row["values"])
            if row["delta"] is not None:
                delta = f"  ({row['delta']:+d})"
            elif not row.get("in_latest", True):
                delta = "  (n/a: not in latest run)"
            else:
                delta = ""
            lines.append(f"  {row['source']:<28}{vals}{delta}")
    lines.append(f"sources disabled in config: {', '.join(report['sources_disabled']) or 'none'}")
    keys = report.get("keys_configured") or {}
    lines.append("keys configured: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in keys.items()))
    db = report["db"]
    lines.append(f"database: {db['bytes'] / 1e6:.1f} MB at {db['path']}; " + ", ".join(f"{k} {v}" for k, v in db["totals"].items()))
    g = report["gaps"]
    lines.append(f"gaps (days with zero papers by pub_date in the last {g['window_days']} days, {g['days_checked']} checked from {g['coverage_start'] or 'n/a'}): {', '.join(g['gaps']) or 'none'}")
    lines.append("next scheduled runs (UTC, as documented): " + "; ".join(f"{s['name']} {s['next_utc']} [{s['cron']}]" for s in report["next_scheduled"]))
    return "\n".join(lines)


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the WAL into the main file so a copy of radar.sqlite alone is complete (used before upload)."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except sqlite3.DatabaseError:
        pass


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")
