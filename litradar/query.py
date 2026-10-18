"""User-input parsing shared by the CLI, the JSON API and the watchlist: dates and FTS5 query text.

Standard library only (the API module stays dependency-free). Two concerns live here so every
surface agrees on them:

* ``parse_since`` / ``parse_date``: a ``--since`` / ``--until`` value is ``YYYY-MM-DD`` or ``Nd``
  (N days before today). Anything else raises ``ValueError`` with one clear message instead of
  being compared against ``pub_date`` as a string and silently matching nothing.
* FTS5 helpers: ``auto_phrase`` turns a plain multi-word query (``liquid biopsy``) into a phrase
  (``"liquid biopsy"``) unless the text already uses FTS5 operators; ``fts_syntax_error`` asks
  SQLite whether a query parses, so callers can report a syntax error (API: HTTP 400) or say out
  loud that they are falling back to a phrase (CLI).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

SINCE_FORMAT = "YYYY-MM-DD or Nd (for example 90d)"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RELATIVE_RE = re.compile(r"^(\d{1,4})d$")
# FTS5 syntax the user may have written on purpose: quotes, column filters, grouping, prefix, NEAR,
# initial-token anchors and the upper-case boolean operators.
_FTS_OPERATOR_RE = re.compile(r'["(){}:*^]|(?:^|\s)(?:AND|OR|NOT|NEAR)(?:\s|$|\()')


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def parse_date(value: Optional[str], name: str = "--since", today: Optional[date] = None) -> Optional[str]:
    """``YYYY-MM-DD`` or ``Nd`` -> ISO date string; ``None``/blank -> ``None``. Raises ``ValueError``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _RELATIVE_RE.match(text)
    if m:
        base = today or today_utc()
        return (base - timedelta(days=int(m.group(1)))).isoformat()
    if _DATE_RE.match(text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"{name} is not a real calendar date: {text}") from None
        return text
    raise ValueError(f"{name} must be {SINCE_FORMAT}, got {text!r}")


def parse_since(value: Optional[str], today: Optional[date] = None) -> Optional[str]:
    """``parse_date`` for a ``--since`` flag."""
    return parse_date(value, "--since", today)


def _date_arg(text: str, name: str) -> str:
    try:
        return parse_date(text, name) or ""
    except ValueError as exc:
        msg = str(exc)
        # argparse prefixes the message with "argument --since: " itself
        raise argparse.ArgumentTypeError(msg[len(name) + 1:] if msg.startswith(name + " ") else msg) from None


def since_arg(text: str) -> str:
    """argparse ``type=`` for ``--since``: validates and resolves ``Nd`` at parse time (usage error, exit 2, on bad input)."""
    return _date_arg(text, "--since")


def until_arg(text: str) -> str:
    """argparse ``type=`` for ``--until`` (same format as ``--since``)."""
    return _date_arg(text, "--until")


def iso_date_arg(text: str) -> str:
    """argparse ``type=`` for flags that take a calendar date only (no ``Nd``)."""
    t = (text or "").strip()
    if not _DATE_RE.match(t):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    try:
        datetime.strptime(t, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a real calendar date: {text!r}") from None
    return t


# ---- FTS5 ---------------------------------------------------------------------------------
def has_fts_operators(text: str) -> bool:
    """True when the text uses FTS5 syntax (quotes, ``col:``, parentheses, ``*``, ``^``, AND/OR/NOT/NEAR)."""
    return bool(_FTS_OPERATOR_RE.search(text or ""))


def phrase_query(text: str) -> str:
    """Quote text as one FTS5 phrase (embedded quotes become spaces)."""
    return '"' + (text or "").replace('"', " ").strip() + '"'


def auto_phrase(text: str) -> str:
    """A plain multi-word query becomes a phrase; anything with FTS5 operators (or a single word) passes through.

    ``liquid biopsy`` -> ``"liquid biopsy"``; ``methods:Xenium``, ``Xenium OR Visium`` and ``NovaSeq*`` are unchanged.
    A single word is left alone so prefix/stem behaviour stays as documented. Hyphenated or dotted single
    tokens (``single-cell``, ``R10.4.1``) are quoted, because unquoted they are a column filter or a syntax error.
    """
    q = (text or "").strip()
    if not q or has_fts_operators(q):
        return q
    if len(q.split()) > 1 or re.search(r"[-./]", q):
        return phrase_query(q)
    return q


def with_sections(query: str, sections) -> str:
    """The exact wrapping ``Store.search`` applies for a section filter: ``{methods coi}: (query)``."""
    cols = " ".join(s for s in (sections or ()) if s)
    return f"{{{cols}}}: ({query})" if cols else query


def fts_syntax_error(conn: sqlite3.Connection, query: str, table: str = "papers_fts") -> Optional[str]:
    """SQLite's own message when ``query`` is not valid FTS5 for ``table``; ``None`` when it parses.

    Runs ``MATCH ... LIMIT 1`` and reads at most one row: FTS5 compiles the expression when the filter
    starts (with ``LIMIT 0`` SQLite never runs the filter, so nothing would be checked). A missing table
    also comes back as a message rather than an exception, so a caller can report it instead of crashing.
    """
    try:
        conn.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ? LIMIT 1", (query,)).fetchone()
    except sqlite3.OperationalError as exc:
        return str(exc)
    return None


__all__ = [
    "SINCE_FORMAT", "parse_date", "parse_since", "since_arg", "until_arg", "iso_date_arg", "today_utc",
    "has_fts_operators", "phrase_query", "auto_phrase", "with_sections", "fts_syntax_error",
]
