"""Read-only JSON API and search page over the SQLite store, plus a urllib client.

    litradar serve --port 8787 [--db PATH] [--cors ORIGIN]

Standard library only (http.server + threading). Every request opens the database with
SQLite's ``mode=ro`` URI and ``PRAGMA query_only``, so nothing served here can write.
No user-supplied SQL is ever executed: the FTS5 query string is bound as a parameter
through :meth:`litradar.store.Store.search`, and every other parameter is validated and
bounded before it reaches a query.

Privacy: author records may carry e-mail addresses when a source supplied them. The API never
returns them: ``authors[].email`` and ``extra.emails`` are removed from every paper payload
(``/papers``, ``/top``), and e-mail-like tokens in request paths are masked in the log.

The FTS5 query is passed to SQLite as written. A query that FTS5 cannot parse is a ``400`` carrying
SQLite's own message (``query not understood by FTS5: ...``); the API never silently rewrites it.

Endpoints (all GET; see ``/api`` for the machine-readable index and docs/search.md):

    /search?q=&since=&in=methods,coi&limit=     full-text search (FTS5 syntax allowed)
    /papers/{id}                                 one paper with mentions, score, tracked authors
    /entities/{kind}/{id}/papers?since=&limit=   papers mentioning one gazetteer entry
    /instruments?since=&until=&limit=            instrument counts in a window
    /companies?since=&until=&limit=              company counts by role in a window
    /archive/instruments?since=&until=&limit=    archive experiments per instrument model
    /top?since=&until=&limit=&full=1             top scored papers in a window (chips by default; full=1 for mention rows)
    /news?since=&company=&limit=                 news items, optionally for one company id
    /stats                                       table totals and recent runs
    /digest/latest                               the latest digest as Markdown text
    /                                            a minimal HTML search page

Other scripts can use :class:`RadarClient` instead of hand-rolling HTTP calls.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, unquote_plus, urlencode, urlsplit
from urllib.request import Request, pathname2url, urlopen

from . import __version__
from . import query as Q
from .store import FTS_COLUMNS, Store

# FTS_COLUMNS comes from the store (searchable columns, `mentions` included; docs/store.md section 2): one list, not a copy
ENTITY_KINDS: Tuple[str, ...] = ("instrument", "company")
MAX_LIMIT = 200  # per-paper endpoints (/search, /top, /entities)
MAX_COUNT_LIMIT = 500  # aggregate endpoints (/instruments, /companies, /archive, /news)
MAX_QUERY_LEN = 500
MAX_ID_LEN = 300
DEFAULT_WINDOW_DAYS = 7
TOP_SECTIONS = ("funding", "coi")  # the only sections /top carries unless full=1

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._+-]+(?:@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.I)
_ENTITY_PATH_RE = re.compile(r"^/entities/([a-z]+)/(.+)/papers$")

ENDPOINTS: List[Dict[str, str]] = [
    {"path": "/search", "params": "q (required), since, in, limit", "returns": "full-text hits with snippet, instrument and company chips"},
    {"path": "/papers/{id}", "params": "", "returns": "paper, mentions, score, tracked_authors"},
    {"path": "/entities/{kind}/{id}/papers", "params": "since, limit", "returns": "papers mentioning one instrument or company id"},
    {"path": "/instruments", "params": "since, until, limit", "returns": "instrument counts in the window"},
    {"path": "/companies", "params": "since, until, limit", "returns": "company counts by role in the window"},
    {"path": "/archive/instruments", "params": "since, until, limit", "returns": "archive experiments per instrument model"},
    {"path": "/top", "params": "since, until, limit, full", "returns": "top scored papers with score, reasons and entity chips (full=1: mention rows and all sections)"},
    {"path": "/news", "params": "since, company, limit", "returns": "news items, optionally filtered by company id"},
    {"path": "/stats", "params": "", "returns": "table totals, date range, recent runs"},
    {"path": "/digest/latest", "params": "", "returns": "latest digest as text/markdown"},
]


# ---- errors and responses --------------------------------------------------------------
class ApiError(Exception):
    """A client error that becomes a JSON ``{"error": ..., "status": ...}`` response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Response:
    __slots__ = ("status", "content_type", "body", "headers")

    def __init__(self, status: int, content_type: str, body: bytes, headers: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        self.status = status
        self.content_type = content_type
        self.body = body
        self.headers = list(headers or [])

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def json_response(payload: Any, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return Response(status, "application/json; charset=utf-8", body)


def text_response(text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200, headers=None) -> Response:
    return Response(status, content_type, text.encode("utf-8"), headers)


# ---- read-only store -------------------------------------------------------------------
class ReadOnlyStore(Store):
    """:class:`Store` opened read-only (``file:...?mode=ro``) without creating schema or WAL pragmas.

    All query methods of the parent class work; any write raises ``sqlite3.OperationalError``.
    """

    def __init__(self, path: Path) -> None:  # noqa: D107 - intentionally does not call super().__init__
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"database not found: {self.path}")
        uri = "file:" + pathname2url(str(self.path.resolve())) + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.execute("PRAGMA query_only=1")

    def probe(self) -> None:
        """Touch the schema once so a file that is not a radar database fails here, not on the first request."""
        try:
            self.conn.execute("SELECT 1 FROM papers LIMIT 1").fetchall()
        except sqlite3.DatabaseError as exc:
            raise sqlite3.DatabaseError(f"{self.path} is not a readable radar database: {exc}") from None

    def close(self) -> None:
        self.conn.close()


# ---- parameter validation --------------------------------------------------------------
def parse_date(value: Optional[str], name: str, today: Optional[date] = None) -> Optional[str]:
    """Accept ``YYYY-MM-DD`` or ``Nd`` (days back from today); return ISO date or None when blank.

    The same rule as the CLI (:func:`litradar.query.parse_date`); a bad value is a 400.
    """
    try:
        return Q.parse_date(value, name, today)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


def parse_flag(value: Optional[str], default: bool) -> bool:
    """``1/true/yes/on`` and ``0/false/no/off`` (any case); blank -> default; anything else is a 400."""
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ApiError(400, f"expected 0/1, true/false, yes/no, got {value!r}")


def parse_limit(value: Optional[str], default: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except ValueError:
        raise ApiError(400, "limit must be an integer") from None
    if n < 1:
        raise ApiError(400, "limit must be at least 1")
    return min(n, maximum)


def parse_sections(value: Optional[str]) -> Tuple[str, ...]:
    if not value:
        return ()
    parts: List[str] = []
    for raw in value.split(","):
        p = raw.strip().lower()
        if p and p not in parts:
            parts.append(p)
    bad = [p for p in parts if p not in FTS_COLUMNS]
    if bad:
        raise ApiError(400, f"unknown section(s) {', '.join(bad)}; choose from {', '.join(FTS_COLUMNS)}")
    return tuple(parts)


def parse_slug(value: Optional[str], name: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if not _SLUG_RE.match(value):
        raise ApiError(400, f"{name} may only contain letters, digits, '.', '_' and '-' (max 128 chars)")
    return value


def redact_path(raw_path: str, max_len: int = 300) -> str:
    """Request path for the log line: e-mail-like tokens in the query string are masked."""
    parts = urlsplit(raw_path)
    out = parts.path
    if parts.query:
        decoded = unquote_plus(parts.query)
        # match on the decoded form so %20-encoded text cannot hide an address; log that form only when masking
        out += "?" + (_EMAIL_RE.sub("[email]", decoded) if _EMAIL_RE.search(decoded) else parts.query)
    return out if len(out) <= max_len else out[: max_len - 1] + "…"


def _first_values(raw_query: str) -> Dict[str, str]:
    return {k: v[0] for k, v in parse_qs(raw_query, keep_blank_values=True).items()}


PRIVATE_AUTHOR_FIELDS = ("email",)
PRIVATE_EXTRA_FIELDS = ("emails", "email")


def strip_private_fields(out: Dict[str, Any]) -> Dict[str, Any]:
    """Remove personal data the sources sometimes supply: ``authors[].email`` and ``extra.emails``. In place."""
    authors = out.get("authors")
    if isinstance(authors, list):
        for a in authors:
            if isinstance(a, dict):
                for k in PRIVATE_AUTHOR_FIELDS:
                    a.pop(k, None)
    extra = out.get("extra")
    if isinstance(extra, dict):
        for k in PRIVATE_EXTRA_FIELDS:
            extra.pop(k, None)
    return out


def _parse_json_columns(row: Dict[str, Any]) -> Dict[str, Any]:
    """``*_json`` text columns become real objects; booleans become booleans; internal and private fields go."""
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if key == "_rowid":
            continue  # SQLite's internal row number: not part of the API
        if key.endswith("_json"):
            name = key[: -len("_json")]
            try:
                out[name] = json.loads(value) if value else None
            except (TypeError, ValueError):
                out[name] = value
        elif key == "fulltext_available":
            out[key] = bool(value)
        else:
            out[key] = value
    return strip_private_fields(out)


def _chips(mentions: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collapse mention rows into one chip per entity, split by kind."""
    seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for m in mentions:
        key = (m["kind"], m["entity_id"])
        chip = seen.get(key)
        if chip is None:
            chip = {
                "entity_id": m["entity_id"],
                "name": m.get("entity_name") or m["entity_id"],
                "vendor": m.get("vendor") or "",
                "ticker": m.get("ticker") or "",
                "sections": [],
            }
            seen[key] = chip
        if m.get("section") and m["section"] not in chip["sections"]:
            chip["sections"].append(m["section"])
    instruments = [c for (kind, _), c in seen.items() if kind == "instrument"]
    companies = [c for (kind, _), c in seen.items() if kind == "company"]
    return instruments, companies


def _split_concat(value: Any) -> List[str]:
    return [p for p in str(value or "").split(",") if p]


def _entity_papers(st: Store, kind: str, entity_id: str, since: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Like :meth:`Store.entity_papers` but one row per paper (sections collapsed) and filtered by kind."""
    sql = """SELECT p.id, p.doi, p.title, p.journal, p.pub_date, p.url, p.kind, p.source,
                    GROUP_CONCAT(DISTINCT m.section) AS sections, SUM(m.count) AS mentions, MIN(m.snippet) AS snippet
             FROM mentions m JOIN papers p ON p.id = m.paper_id
             WHERE m.kind = ? AND m.entity_id = ?"""
    params: List[Any] = [kind, entity_id]
    if since:
        sql += " AND (p.pub_date >= ? OR (p.pub_date = '' AND substr(p.first_seen, 1, 10) >= ?))"
        params += [since, since]
    sql += " GROUP BY p.id ORDER BY p.pub_date DESC, p.id LIMIT ?"
    params.append(limit)
    cur = st.conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["sections"] = _split_concat(r.get("sections"))
    return rows


# ---- the application -------------------------------------------------------------------
class RadarAPI:
    """Routes requests to read-only queries. Independent of the HTTP layer so it can be unit-tested directly."""

    def __init__(
        self,
        db_path: Path,
        digest_dir: Optional[Path] = None,
        cors_origin: Optional[str] = None,
        today: Optional[Callable[[], date]] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.digest_dir = Path(digest_dir) if digest_dir else None
        self.cors_origin = cors_origin or None
        self._today = today
        self.logger = logging.getLogger("litradar.api")
        st = self.open()  # fail fast on a missing, unreadable or non-SQLite database
        try:
            st.probe()
        finally:
            st.close()

    # -- helpers --
    def today(self) -> date:
        return self._today() if self._today else datetime.now(timezone.utc).date()

    def open(self) -> ReadOnlyStore:
        return ReadOnlyStore(self.db_path)

    def _window(self, q: Dict[str, str]) -> Tuple[str, str]:
        today = self.today()
        until = parse_date(q.get("until"), "until", today) or today.isoformat()
        since = parse_date(q.get("since"), "since", today)
        if since is None:
            since = (date.fromisoformat(until) - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()
        if since > until:
            raise ApiError(400, "since must not be after until")
        return since, until

    # -- dispatch --
    def dispatch(self, method: str, raw_path: str) -> Response:
        parts = urlsplit(raw_path)
        path = parts.path or "/"
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        query = _first_values(parts.query)
        if method == "OPTIONS":
            return Response(204, "text/plain; charset=utf-8", b"", [("Allow", "GET, HEAD, OPTIONS")])
        if method not in ("GET", "HEAD"):
            resp = json_response({"error": "read-only API: only GET is supported", "status": 405}, 405)
            resp.headers.append(("Allow", "GET, HEAD, OPTIONS"))
            return resp
        try:
            return self._route(path, query)
        except ApiError as exc:
            return json_response({"error": exc.message, "status": exc.status}, exc.status)
        except sqlite3.OperationalError as exc:
            msg = str(exc)
            if any(marker in msg.lower() for marker in ("fts5", "syntax", "no such column")):
                return json_response({"error": f"query not understood by FTS5: {msg}", "status": 400}, 400)
            self.logger.error("sqlite error on %s: %s", redact_path(raw_path), msg)
            return json_response({"error": "database error", "status": 503}, 503)
        except sqlite3.DatabaseError as exc:
            # "file is not a database", a corrupt or truncated file: the data layer is down, not the client's fault
            self.logger.error("database error on %s: %s", redact_path(raw_path), exc)
            return json_response({"error": "database error", "status": 503}, 503)
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            self.logger.exception("unhandled error on %s", redact_path(raw_path))
            return json_response({"error": "internal error", "status": 500}, 500)

    def _route(self, path: str, q: Dict[str, str]) -> Response:
        if path == "/":
            return self.page()
        if path == "/api":
            return json_response({"name": "lit-radar read-only API", "version": __version__, "endpoints": ENDPOINTS})
        if path == "/search":
            return self.search(q)
        if path == "/papers":
            return self.paper(q.get("id", ""))
        if path.startswith("/papers/"):
            return self.paper(unquote(path[len("/papers/"):]))
        m = _ENTITY_PATH_RE.match(path)
        if m:
            return self.entity_papers(m.group(1), unquote(m.group(2)), q)
        if path == "/instruments":
            return self.instruments(q)
        if path == "/companies":
            return self.companies(q)
        if path == "/archive/instruments":
            return self.archive_instruments(q)
        if path == "/top":
            return self.top(q)
        if path == "/news":
            return self.news(q)
        if path == "/stats":
            return self.stats()
        if path == "/digest/latest":
            return self.digest_latest()
        raise ApiError(404, f"no such endpoint: {path}; see /api")

    # -- endpoints --
    def search(self, q: Dict[str, str]) -> Response:
        query = (q.get("q") or "").strip()
        if not query:
            raise ApiError(400, "q is required")
        if len(query) > MAX_QUERY_LEN:
            raise ApiError(400, f"q is longer than {MAX_QUERY_LEN} characters")
        since = parse_date(q.get("since"), "since", self.today())
        sections = parse_sections(q.get("in"))
        limit = parse_limit(q.get("limit"), 50, MAX_LIMIT)
        with_chips = parse_flag(q.get("mentions"), True)
        st = self.open()
        try:
            # Store.search would retry an unparsable query as a phrase and return 200 with nothing; the API says
            # what FTS5 objected to instead, so a mistyped column name is visible to the client and the page.
            fts_error = Q.fts_syntax_error(st.conn, Q.with_sections(query, sections))
            if fts_error:
                raise ApiError(400, f"query not understood by FTS5: {fts_error}")
            try:
                rows = st.search(query, since=since, limit=limit, sections=sections)
            except sqlite3.OperationalError as exc:
                raise ApiError(400, f"query not understood by FTS5: {exc}") from None
            if with_chips:
                for r in rows:
                    r["instruments"], r["companies"] = _chips(st.mentions_for(r["id"]))
        finally:
            st.close()
        return json_response(
            {"query": query, "since": since, "sections": list(sections), "limit": limit, "count": len(rows), "results": rows}
        )

    def paper(self, paper_id: str) -> Response:
        if not paper_id:
            raise ApiError(400, "paper id is required: /papers/{id}")
        if len(paper_id) > MAX_ID_LEN:
            raise ApiError(400, f"paper id is longer than {MAX_ID_LEN} characters")
        st = self.open()
        try:
            row = st.get_paper(paper_id)
            if row is None:
                raise ApiError(404, "paper not found")
            out = _parse_json_columns(row)
            out["mentions"] = st.mentions_for(paper_id)
            score = st.get_score(paper_id)
            if score:
                score = _parse_json_columns(score)
            out["score"] = score
            out["tracked_authors"] = [
                {"author_name": r[0], "subgroup": r[1], "matched_by": r[2]}
                for r in st.conn.execute(
                    "SELECT author_name, subgroup, matched_by FROM tracked_author_hits WHERE paper_id=? ORDER BY subgroup, author_name",
                    (paper_id,),
                )
            ]
        finally:
            st.close()
        return json_response(out)

    def entity_papers(self, kind: str, entity_id: str, q: Dict[str, str]) -> Response:
        if kind not in ENTITY_KINDS:
            raise ApiError(400, f"kind must be one of {', '.join(ENTITY_KINDS)}")
        entity_id = parse_slug(entity_id, "entity id") or ""
        if not entity_id:
            raise ApiError(400, "entity id is required")
        since = parse_date(q.get("since"), "since", self.today())
        limit = parse_limit(q.get("limit"), 100, MAX_LIMIT)
        st = self.open()
        try:
            meta_row = st.conn.execute(
                "SELECT entity_name, vendor, ticker, category FROM mentions WHERE kind=? AND entity_id=? LIMIT 1",
                (kind, entity_id),
            ).fetchone()
            if not meta_row:
                # the API has no gazetteer; an id with no stored mention is unknown here (a typo, the wrong kind,
                # or an entry nothing has cited yet), and says so instead of answering 200 with nothing
                other = st.conn.execute("SELECT kind FROM mentions WHERE entity_id=? LIMIT 1", (entity_id,)).fetchone()
                hint = f"; it exists as a {other[0]}" if other else " (ids: `litradar entities`)"
                raise ApiError(404, f"unknown {kind} id {entity_id!r}: no mentions in this database{hint}")
            meta = {"name": meta_row[0], "vendor": meta_row[1] or "", "ticker": meta_row[2] or "", "category": meta_row[3] or ""}
            rows = _entity_papers(st, kind, entity_id, since, limit)
        finally:
            st.close()
        return json_response(
            {"kind": kind, "entity_id": entity_id, "entity": meta, "since": since, "limit": limit, "count": len(rows), "results": rows}
        )

    def instruments(self, q: Dict[str, str]) -> Response:
        since, until = self._window(q)
        limit = parse_limit(q.get("limit"), 60, MAX_COUNT_LIMIT)
        st = self.open()
        try:
            rows = st.instrument_counts(since, until, limit)
        finally:
            st.close()
        for r in rows:
            r["sections"] = _split_concat(r.get("sections"))
        return json_response({"since": since, "until": until, "limit": limit, "count": len(rows), "results": rows})

    def companies(self, q: Dict[str, str]) -> Response:
        since, until = self._window(q)
        limit = parse_limit(q.get("limit"), 60, MAX_COUNT_LIMIT)
        st = self.open()
        try:
            rows = st.company_counts(since, until, limit)
        finally:
            st.close()
        return json_response({"since": since, "until": until, "limit": limit, "count": len(rows), "results": rows})

    def archive_instruments(self, q: Dict[str, str]) -> Response:
        since, until = self._window(q)
        limit = parse_limit(q.get("limit"), 100, MAX_COUNT_LIMIT)
        st = self.open()
        try:
            rows = st.archive_instrument_counts(since, until)[:limit]
        finally:
            st.close()
        for r in rows:
            r["archives"] = _split_concat(r.get("archives"))
        return json_response({"since": since, "until": until, "limit": limit, "count": len(rows), "results": rows})

    def top(self, q: Dict[str, str]) -> Response:
        since, until = self._window(q)
        limit = parse_limit(q.get("limit"), 25, MAX_LIMIT)
        full = parse_flag(q.get("full"), False)
        st = self.open()
        try:
            rows = [_parse_json_columns(r) for r in st.top_papers(since, until, limit)]
        finally:
            st.close()
        if not full:
            # a ranked shortlist, not a dump: entity chips instead of every mention row, and only the short
            # disclosure sections; /papers/{id} (or full=1) has the rest
            for r in rows:
                mentions = r.pop("mentions", None) or []
                r["instruments"], r["companies"] = _chips(mentions)
                r["mention_rows"] = len(mentions)
                secs = r.get("sections")
                if isinstance(secs, dict):
                    r["sections"] = {k: v for k, v in secs.items() if k in TOP_SECTIONS}
        return json_response({"since": since, "until": until, "limit": limit, "full": full, "count": len(rows), "results": rows})

    def news(self, q: Dict[str, str]) -> Response:
        today = self.today()
        since = parse_date(q.get("since"), "since", today) or (today - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()
        company = parse_slug(q.get("company"), "company")
        limit = parse_limit(q.get("limit"), 100, MAX_COUNT_LIMIT)
        st = self.open()
        try:
            rows = st.news_in_window(since, limit=limit if not company else 5000)
        finally:
            st.close()
        if company:
            wanted = company.lower()
            rows = [r for r in rows if wanted in [c.lower() for c in r.get("companies", [])]][:limit]
        for r in rows:
            r.pop("companies_json", None)
        return json_response({"since": since, "company": company, "limit": limit, "count": len(rows), "results": rows})

    def stats(self) -> Response:
        st = self.open()
        try:
            counts = st.counts()
            lo, hi = st.conn.execute("SELECT MIN(pub_date), MAX(pub_date) FROM papers WHERE pub_date != ''").fetchone()
            runs = [
                {"id": r[0], "started": r[1], "finished": r[2], "since": r[3], "until": r[4], "counts": json.loads(r[5] or "{}")}
                for r in st.conn.execute("SELECT id, started, finished, since, until, counts_json FROM runs ORDER BY id DESC LIMIT 5")
            ]
        finally:
            st.close()
        return json_response(
            {
                "version": __version__,
                "counts": counts,
                "pub_date_range": {"min": lo, "max": hi},
                "runs": runs,
                "database": {"file": self.db_path.name, "bytes": self.db_path.stat().st_size},
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    def digest_latest(self) -> Response:
        path = (self.digest_dir / "latest.md") if self.digest_dir else None
        if path is None or not path.is_file():
            raise ApiError(404, "no digest has been rendered yet (digests/latest.md is missing)")
        return text_response(path.read_text(encoding="utf-8"), "text/markdown; charset=utf-8")

    def page(self) -> Response:
        nonce = secrets.token_urlsafe(16)
        csp = (
            f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'"
        )
        html = PAGE_HTML.replace("__NONCE__", nonce).replace("__VERSION__", __version__)
        return text_response(html, "text/html; charset=utf-8", headers=[("Content-Security-Policy", csp)])


# ---- HTTP layer ------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = f"litradar-api/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._serve("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._serve("OPTIONS")

    def do_POST(self) -> None:  # noqa: N802
        self._serve("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._serve("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._serve("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._serve("PATCH")

    def _serve(self, method: str) -> None:
        t0 = time.monotonic()
        app: RadarAPI = self.server.app  # type: ignore[attr-defined]
        resp = app.dispatch(method, self.path)
        body = b"" if method in ("HEAD", "OPTIONS") else resp.body
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(body) if method != "HEAD" else len(resp.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for key, value in resp.headers:
                self.send_header(key, value)
            if app.cors_origin:
                self.send_header("Access-Control-Allow-Origin", app.cors_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
                if app.cors_origin != "*":
                    self.send_header("Vary", "Origin")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass
        app.logger.info("%s %s %d %.1fms", method, redact_path(self.path), resp.status, (time.monotonic() - t0) * 1000)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return  # one line per request is emitted in _serve instead


class RadarServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], app: RadarAPI) -> None:
        super().__init__(address, _Handler)
        self.app = app


def make_server(app: RadarAPI, host: str = "127.0.0.1", port: int = 8787) -> RadarServer:
    """Bind (port 0 = ephemeral) without serving; call ``serve_forever`` yourself (tests do this in a thread)."""
    return RadarServer((host, port), app)


def start_in_thread(app: RadarAPI, host: str = "127.0.0.1", port: int = 0) -> Tuple[RadarServer, threading.Thread]:
    server = make_server(app, host, port)
    thread = threading.Thread(target=server.serve_forever, name="litradar-api", daemon=True)
    thread.start()
    return server, thread


def serve(app: RadarAPI, host: str = "127.0.0.1", port: int = 8787) -> int:
    try:
        server = make_server(app, host, port)
    except OSError as exc:
        # EADDRINUSE, a privileged port, an address that is not local: one line, not a traceback
        print(f"litradar serve: cannot listen on {host}:{port}: {exc.strerror or exc} (pass --port to pick another port)", file=sys.stderr, flush=True)
        return 1
    shown_host = "localhost" if host in ("0.0.0.0", "::", "127.0.0.1") else host
    print(f"lit-radar read-only API on http://{shown_host}:{server.server_address[1]}/  (database {app.db_path.name}; Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ---- Python client ---------------------------------------------------------------------
class RadarAPIError(Exception):
    """An error answer from the API (``status`` = HTTP status) or, with ``status == 0``, no answer at all
    (connection refused, DNS failure, timeout): ``message`` carries the reason."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if status else f"connection failed: {message}")
        self.status = status
        self.message = message


class RadarClient:
    """Tiny urllib client for the JSON API, for use from other scripts.

        from litradar.api import RadarClient
        radar = RadarClient("http://127.0.0.1:8787")
        for hit in radar.search("methods:Xenium", since="90d")["results"]:
            print(hit["pub_date"], hit["title"])
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8787", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path: str, params: Optional[Dict[str, Any]] = None, accept: str = "application/json") -> bytes:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        url = self.base_url + path + (("?" + urlencode(clean)) if clean else "")
        req = Request(url, headers={"Accept": accept, "User-Agent": f"litradar-client/{__version__}"})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            body = exc.read()
            try:
                message = json.loads(body.decode("utf-8")).get("error", body.decode("utf-8", "replace"))
            except ValueError:
                message = body.decode("utf-8", "replace") or exc.reason
            raise RadarAPIError(exc.code, str(message)) from None
        except URLError as exc:
            raise RadarAPIError(0, f"{exc.reason} ({url})") from None
        except OSError as exc:  # socket timeout and friends
            raise RadarAPIError(0, f"{exc} ({url})") from None

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return json.loads(self._request(path, params).decode("utf-8"))

    def search(self, q: str, since: Optional[str] = None, sections: Sequence[str] = (), limit: int = 50, mentions: bool = True) -> Dict[str, Any]:
        return self._get("/search", {"q": q, "since": since, "in": ",".join(sections) or None, "limit": limit, "mentions": None if mentions else "0"})

    def paper(self, paper_id: str) -> Dict[str, Any]:
        return self._get("/papers/" + quote(paper_id, safe=""))

    def entity_papers(self, kind: str, entity_id: str, since: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        return self._get(f"/entities/{quote(kind, safe='')}/{quote(entity_id, safe='')}/papers", {"since": since, "limit": limit})

    def instruments(self, since: Optional[str] = None, until: Optional[str] = None, limit: int = 60) -> Dict[str, Any]:
        return self._get("/instruments", {"since": since, "until": until, "limit": limit})

    def companies(self, since: Optional[str] = None, until: Optional[str] = None, limit: int = 60) -> Dict[str, Any]:
        return self._get("/companies", {"since": since, "until": until, "limit": limit})

    def archive_instruments(self, since: Optional[str] = None, until: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        return self._get("/archive/instruments", {"since": since, "until": until, "limit": limit})

    def top(self, since: Optional[str] = None, until: Optional[str] = None, limit: int = 25, full: bool = False) -> Dict[str, Any]:
        return self._get("/top", {"since": since, "until": until, "limit": limit, "full": "1" if full else None})

    def news(self, since: Optional[str] = None, company: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        return self._get("/news", {"since": since, "company": company, "limit": limit})

    def stats(self) -> Dict[str, Any]:
        return self._get("/stats")

    def endpoints(self) -> Dict[str, Any]:
        return self._get("/api")

    def digest_latest(self) -> str:
        return self._request("/digest/latest", accept="text/markdown").decode("utf-8")


# ---- the search page -------------------------------------------------------------------
PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Literature radar search</title>
<style nonce="__NONCE__">
:root { color-scheme: light dark;
  --bg: #fafaf9; --fg: #1c1917; --muted: #6b6560; --card: #ffffff; --line: #e7e5e4;
  --accent: #0f766e; --mark: #fde68a; --inst: #dbeafe; --inst-fg: #1e3a8a; --co: #fce7f3; --co-fg: #831843; }
@media (prefers-color-scheme: dark) { :root {
  --bg: #121212; --fg: #e7e5e4; --muted: #a8a29e; --card: #1c1c1c; --line: #2e2e2e;
  --accent: #5eead4; --mark: #92400e; --inst: #1e3a5f; --inst-fg: #bfdbfe; --co: #4a1d39; --co-fg: #fbcfe8; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 900px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 1.35rem; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: .9rem; margin: 0 0 18px; }
form { display: flex; gap: 8px; flex-wrap: wrap; }
#q { flex: 1 1 320px; font: inherit; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--fg); }
#q:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
select, button { font: inherit; padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--fg); }
button[type=submit] { background: var(--accent); color: var(--bg); border-color: var(--accent); cursor: pointer; font-weight: 600; }
.toggles { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 0; font-size: .85rem; color: var(--muted); align-items: center; }
.toggles label { border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px; cursor: pointer; user-select: none; }
.toggles input { display: none; }
.toggles input:checked + span { color: var(--accent); font-weight: 600; }
#status { margin: 18px 0 8px; color: var(--muted); font-size: .9rem; min-height: 1.2em; }
#status a { color: var(--accent); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
.card h2 { font-size: 1.02rem; margin: 0 0 4px; font-weight: 600; }
.card h2 a { color: inherit; text-decoration: none; }
.card h2 a:hover { text-decoration: underline; }
.meta { color: var(--muted); font-size: .85rem; margin-bottom: 6px; }
.kind { display: inline-block; font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; margin-right: 6px; }
.snip { font-size: .92rem; }
mark { background: var(--mark); color: inherit; padding: 0 1px; border-radius: 2px; }
.chips { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: .78rem; border-radius: 999px; padding: 1px 9px; cursor: pointer; border: none; }
.chip.inst { background: var(--inst); color: var(--inst-fg); }
.chip.co { background: var(--co); color: var(--co-fg); }
.links { margin-top: 8px; font-size: .82rem; }
.links a { color: var(--accent); margin-right: 12px; }
footer { margin-top: 28px; color: var(--muted); font-size: .82rem; }
footer a { color: var(--accent); }
code { font-size: .88em; background: var(--line); padding: 0 4px; border-radius: 3px; }
</style>
</head>
<body>
<main>
<h1>Literature radar search</h1>
<p class="sub" id="stats">Full-text search over stored papers: titles, abstracts, Methods, funding, competing interests, affiliations, open-access bodies and the entities attributed to each paper (<code>mentions</code>: names, aliases and ids from the gazetteer).</p>
<form id="f" autocomplete="off">
  <input id="q" type="search" placeholder='e.g. methods:Xenium  or  "spatial transcriptomics" NEAR(single-cell)  or  NovaSeq*' autofocus>
  <select id="since" title="published since">
    <option value="7d">7 days</option><option value="30d">30 days</option><option value="90d" selected>90 days</option>
    <option value="365d">1 year</option><option value="">all time</option>
  </select>
  <button type="submit">Search</button>
</form>
<div class="toggles" id="sections"><span>search in:</span></div>
<div id="status"></div>
<div id="results"></div>
<footer>FTS5 syntax: <code>methods:term</code>, <code>"exact phrase"</code>, <code>NEAR(a b, 10)</code>, <code>prefix*</code>, <code>AND</code> / <code>OR</code> / <code>NOT</code>.
JSON: <a href="/api">/api</a> · <a href="/stats">/stats</a> · <a href="/digest/latest">latest digest</a> · litradar __VERSION__</footer>
</main>
<script nonce="__NONCE__">
(function () {
  var COLS = ["title", "abstract", "methods", "funding", "coi", "affiliations", "body", "mentions"];
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  function snippetNode(text) {
    var span = el("span", { "class": "snip" }), re = /\\[([^\\[\\]]{1,80})\\]/g, last = 0, m;
    text = text || "";
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) span.appendChild(document.createTextNode(text.slice(last, m.index)));
      span.appendChild(el("mark", { text: m[1] }));
      last = re.lastIndex;
    }
    if (last < text.length) span.appendChild(document.createTextNode(text.slice(last)));
    return span;
  }
  var secBox = $("sections");
  COLS.forEach(function (c) {
    secBox.appendChild(el("label", null, [el("input", { type: "checkbox", value: c }), el("span", { text: c })]));
  });
  function selectedSections() {
    return Array.prototype.map.call(secBox.querySelectorAll("input:checked"), function (i) { return i.value; });
  }
  function setStatus(text, extra) { var s = $("status"); s.textContent = ""; s.appendChild(document.createTextNode(text)); if (extra) s.appendChild(extra); }
  function paperUrl(r) { return r.url || (r.doi ? "https://doi.org/" + r.doi : "/papers/" + encodeURIComponent(r.id)); }
  function card(r) {
    var chips = [];
    (r.instruments || []).forEach(function (c) { chips.push(el("button", { "class": "chip inst", type: "button", title: (c.vendor || "") + (c.ticker ? " · " + c.ticker : ""), text: c.name, onclick: function () { entity("instrument", c.entity_id, c.name); } })); });
    (r.companies || []).forEach(function (c) { chips.push(el("button", { "class": "chip co", type: "button", title: c.ticker || "", text: c.name, onclick: function () { entity("company", c.entity_id, c.name); } })); });
    return el("article", { "class": "card" }, [
      el("h2", null, [el("a", { href: paperUrl(r), target: "_blank", rel: "noopener", text: r.title || r.id })]),
      el("div", { "class": "meta" }, [
        el("span", { "class": "kind", text: r.kind || "paper" }),
        (r.journal || r.source || "") + (r.pub_date ? " · " + r.pub_date : "") + (r.section ? " · matched in " + r.section : "")
      ]),
      snippetNode(r.snippet),
      chips.length ? el("div", { "class": "chips" }, chips) : null,
      el("div", { "class": "links" }, [
        el("a", { href: paperUrl(r), target: "_blank", rel: "noopener", text: "paper" }),
        r.doi ? el("a", { href: "https://doi.org/" + r.doi, target: "_blank", rel: "noopener", text: "doi" }) : null,
        el("a", { href: "/papers/" + encodeURIComponent(r.id), target: "_blank", text: "json" })
      ])
    ]);
  }
  function render(rows) {
    var box = $("results"); box.textContent = "";
    rows.forEach(function (r) { box.appendChild(card(r)); });
  }
  function fetchJson(url, onOk) {
    fetch(url, { headers: { Accept: "application/json" } }).then(function (resp) {
      return resp.json().then(function (data) { if (!resp.ok) throw new Error(data.error || resp.statusText); return data; });
    }).then(onOk).catch(function (err) { setStatus("Error: " + err.message); $("results").textContent = ""; });
  }
  function search(push) {
    var q = $("q").value.trim(); if (!q) return;
    var params = new URLSearchParams({ q: q });
    var since = $("since").value; if (since) params.set("since", since);
    var secs = selectedSections(); if (secs.length) params.set("in", secs.join(","));
    if (push !== false) history.replaceState(null, "", "?" + params.toString());
    setStatus("Searching…");
    fetchJson("/search?" + params.toString() + "&limit=100", function (data) {
      setStatus(data.count + (data.count === 100 ? "+" : "") + " result" + (data.count === 1 ? "" : "s") + " for " + JSON.stringify(data.query) + (data.since ? " since " + data.since : "") + (data.sections.length ? " in " + data.sections.join(", ") : "") + " · ",
        el("a", { href: "/search?" + params.toString(), target: "_blank", text: "json" }));
      render(data.results);
    });
  }
  function entity(kind, id, name) {
    setStatus("Loading papers mentioning " + name + "…");
    var since = $("since").value;
    fetchJson("/entities/" + encodeURIComponent(kind) + "/" + encodeURIComponent(id) + "/papers?limit=100" + (since ? "&since=" + since : ""), function (data) {
      var back = el("a", { href: "#", text: "back to search", onclick: function (e) { e.preventDefault(); search(false); } });
      setStatus(data.count + " paper" + (data.count === 1 ? "" : "s") + " mention " + name + " (" + kind + " " + id + ")" + (data.since ? " since " + data.since : "") + " · ", back);
      render(data.results);
    });
  }
  $("f").addEventListener("submit", function (e) { e.preventDefault(); search(); });
  fetchJson("/stats", function (s) {
    var c = s.counts || {}, r = s.pub_date_range || {};
    $("stats").textContent = (c.papers || 0).toLocaleString() + " papers · " + (c.mentions || 0).toLocaleString() + " instrument and company mentions · " +
      (c.archive_records || 0).toLocaleString() + " archive experiments" + (r.min ? " · papers dated " + r.min + " to " + r.max : "");
  });
  var init = new URLSearchParams(location.search);
  if (init.get("q")) {
    $("q").value = init.get("q");
    if (init.has("since")) $("since").value = init.get("since");
    (init.get("in") || "").split(",").forEach(function (c) { var box = secBox.querySelector('input[value="' + c + '"]'); if (box) box.checked = true; });
    search(false);
  }
})();
</script>
</body>
</html>
"""
