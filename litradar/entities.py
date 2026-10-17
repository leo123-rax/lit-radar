"""Entity-centric views over the store: one dossier per instrument, company or person, plus a watchlist.

    litradar show <entity_id | company_id | name | alias | ticker | "First Last"> [--since 365d] [--json]
    litradar watch add|remove|list <term> [--kind term|entity|author] [--json]
    litradar watch run [--since 7d] [--limit 20] [--json]

Everything here is read from the SQLite database the weekly run fills (litradar/store.py); there is
no network access and nothing beyond the standard library. The module only *adds* one table:

    watchlist(term TEXT PRIMARY KEY COLLATE NOCASE, kind TEXT, added_at TEXT)

created with CREATE TABLE IF NOT EXISTS the first time the watchlist is touched. On a read-only
database the watchlist is simply unavailable; `show` keeps working because its scratch space is a
TEMP table, which SQLite allows on read-only connections.

What a dossier contains
-----------------------
* identity: the gazetteer entry (vendor / ticker / category / launch year / aliases for an
  instrument; ticker / parent / e-mail domains for a company) or, for a person, the subgroups that
  track them in config/subgroups.yaml;
* papers per month over the window, zero-filled, with an ASCII sparkline and a 30-day-vs-prior-30-day
  trend;
* which sections the entity was found in (title / abstract / methods / funding / coi / ack /
  affiliations) with paper counts;
* the most recent papers, each with venue, the sections it was found in and a snippet;
* co-mentioned entities: instruments and companies appearing in the same papers, ranked by papers;
* instrument: SRA/ENA experiments per month declaring that instrument (matched by gazetteer id, or
  by model-name substring when the archive string never resolved to an id, which is reported);
* company: archive experiments for the vendor (by ticker, else vendor name), that vendor's instruments
  seen in the window ("portfolio"), and news items tagged with the company id;
* person: tracked-author hits with the subgroup and how the match was made (openalex_id or name),
  plus papers whose author list matches the name (see `name_key` for the matching rule: Western
  "First Last" / "Last, First", PubMed-style "Last FI" as Europe PMC stores it, leading initials
  "F.I. Last", romanised East Asian names in either order when both parts are spelled out, and
  CJK / other non-Latin names matched exactly, including against a CJK gloss such as "Wenxin Hu (胡文欣)").

Counts are never capped by --limit: `watch run` reports `matches` (the full count in the window) and
`shown` (the rows printed, newest first); the dossier's trend is measured only inside the window and
says n/a when the prior 30-day period lies outside it; months outside the window are summed into
`papers_out_of_range` / `experiments_out_of_range` instead of being drawn on the sparkline.

Resolution order for the query string: gazetteer id -> canonical name -> alias -> company ticker ->
an entity id that exists only in the mentions table (e.g. `unlisted:<slug>` institutions) -> a person
(tracked author in config, tracked_author_hits, or any paper's author list). Ties return the best match
and list the others under `candidates`.

Full-text watch terms use the FTS5 index (`papers_fts`, porter + unicode61 tokenizer). Terms containing
CJK characters bypass FTS (unicode61 does not segment CJK text) and are matched as exact substrings of
title / abstract / methods / body instead.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .extract.gazetteer import Entry, Gazetteer
from .store import Store

WATCHLIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
  term TEXT NOT NULL COLLATE NOCASE PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'term',
  added_at TEXT NOT NULL
);
"""
WATCH_KINDS = ("term", "entity", "author")
ENTITY_KINDS = ("instrument", "company")
SPARK_LEVELS = "_.:-=+*#%@"  # zero, then nine ascending levels
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
_CJK_RUN = re.compile(CJK_RE.pattern + "+")
_INITIALS = re.compile(r"[A-Z](?:[.\-]?[A-Z]){0,3}\.?")  # "M", "FJ", "E.E.", "J-P": an initials token
_SCRATCH = "_litradar_entity_papers"
# same window rule as litradar/store.py: published inside the window, or undated and first seen since `since`
_WINDOW = "((p.pub_date >= ? AND p.pub_date <= ?) OR (p.pub_date = '' AND substr(p.first_seen, 1, 10) >= ?))"
_EFFECTIVE_DATE = "CASE WHEN p.pub_date = '' THEN substr(p.first_seen, 1, 10) ELSE p.pub_date END"
_MONTH = "CASE WHEN p.pub_date = '' THEN substr(p.first_seen, 1, 7) ELSE substr(p.pub_date, 1, 7) END"
_SQL_CHUNK = 400  # stay well under SQLite's historical 999 bound-parameter limit


# ---------------------------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _rows(cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _words(s: str) -> List[str]:
    return re.findall(r"[a-z]+", _fold(s))


def _compact(s: str) -> str:
    """NFKC-normalised text with spaces and name punctuation removed: the key for non-Latin names."""
    return re.sub(r"[\s,.\-·・()（）]+", "", unicodedata.normalize("NFKC", s or ""))


def _script_keys(name: str) -> set:
    """Compact non-Latin forms present in a stored name: each CJK run and all runs joined, so
    "Wenxin Hu (胡文欣)" yields {"胡文欣"} and "张 锋" yields {"张", "锋", "张锋"}."""
    s = unicodedata.normalize("NFKC", name or "")
    runs = _CJK_RUN.findall(s)
    keys = set(runs)
    if len(runs) > 1:
        keys.add("".join(runs))
    if not _words(s):
        keys.add(_compact(s))
    return {k for k in keys if k}


def _initials_like(token: str) -> bool:
    return bool(_INITIALS.fullmatch(token))


def month_range(since: str, until: str) -> List[str]:
    """Inclusive list of YYYY-MM between two ISO dates (capped at 50 years)."""
    try:
        y, m = int(since[:4]), int(since[5:7])
        ey, em = int(until[:4]), int(until[5:7])
    except ValueError:
        return []
    out: List[str] = []
    while (y, m) <= (ey, em) and len(out) < 600:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def sparkline(values: Sequence[int], levels: str = SPARK_LEVELS) -> str:
    """ASCII sparkline: zero -> levels[0], otherwise scaled onto levels[1:] with the maximum at the top."""
    if not values:
        return ""
    vmax = max(values)
    steps = len(levels) - 2
    out = []
    for v in values:
        if v <= 0 or vmax <= 0:
            out.append(levels[0])
        else:
            out.append(levels[1 + int(round(v / vmax * steps))])
    return "".join(out)


def name_key(name: str) -> Optional[Tuple[str, str]]:
    """Reduce a person name to (first, last), ASCII-folded and lower-cased.

    Western forms: "Feng Zhang" -> ("feng", "zhang"); "Zhang, F." -> ("f", "zhang"); "Alexander van Oudenaarden"
    -> ("alexander", "oudenaarden"). PubMed / Europe PMC form with trailing initials: "Krokidis M" -> ("m", "krokidis"),
    "Zhang FJ" -> ("f", "zhang"), "van der Berg J" -> ("j", "berg"); leading initials "F.J. Zhang" -> ("f", "zhang").
    A Latin name with a CJK gloss, "Wenxin Hu (胡文欣)", keys on the Latin part (the gloss is matched by
    `_name_matches`). Names without Latin letters (CJK, Cyrillic, ...) return ("*", compact form), e.g.
    "张锋" -> ("*", "张锋"), and match exactly only. Returns None for empty strings.
    """
    raw = (name or "").strip()
    if not raw:
        return None
    if not _words(raw):
        key = _compact(raw)
        return ("*", key) if key else None
    if CJK_RE.search(raw):
        raw = re.sub(r"[()（）]", " ", _CJK_RUN.sub(" ", raw)).strip()
    if "," in raw:
        last_part, first_part = raw.split(",", 1)
        last_tokens, first_tokens = _words(last_part), _words(first_part)
    else:
        tokens = raw.split()
        head_init, tail_init = _initials_like(tokens[0]), _initials_like(tokens[-1])
        if len(tokens) >= 2 and tail_init and (not head_init or len(tokens[-1]) <= len(tokens[0])):
            # "Surname I" / "Surname IJ" (PubMed, Europe PMC): the trailing token is initials
            last_tokens, first_tokens = _words(" ".join(tokens[:-1])), [tokens[-1][0].lower()]
        elif len(tokens) >= 2 and head_init and not tail_init:
            # "IJ Surname": leading initials
            last_tokens, first_tokens = _words(" ".join(tokens[1:])), [tokens[0][0].lower()]
        else:
            words = _words(raw)
            last_tokens, first_tokens = words[-1:], words[:-1]
    if not last_tokens:
        return None
    return (first_tokens[0] if first_tokens else "", last_tokens[-1])


def names_match(a: Optional[Tuple[str, str]], b: Optional[Tuple[str, str]]) -> bool:
    """Same surname and compatible first name (equal, or one side is an initial that agrees).

    Non-Latin keys ("*", compact) match exactly only. Two spelled-out names also match with the order
    swapped ("Zhang Feng" vs "Feng Zhang"), because romanised East Asian names occur in both orders;
    initials never swap, so "Zhang F" does not match "Feng Z".
    """
    if not a or not b:
        return False
    if a[0] == "*" or b[0] == "*":
        return a == b
    fa, la = a
    fb, lb = b
    if la == lb:
        if fa == fb:
            return True
        if not fa or not fb:
            return False
        if len(fa) == 1 or len(fb) == 1:
            return fa[0] == fb[0]
        return False
    return len(fa) > 1 and len(fb) > 1 and fa == lb and la == fb


def _name_matches(key: Optional[Tuple[str, str]], name: str) -> bool:
    """Does a query key match a stored display name? Adds the CJK-gloss rule to `names_match`."""
    if not key or not name:
        return False
    if names_match(key, name_key(name)):
        return True
    return key[0] == "*" and key[1] in _script_keys(name)


def _like_needle(key: Tuple[str, str]) -> str:
    """SQL LIKE pre-filter for authors_json: the folded surname, or the first character of a non-Latin key
    (SQLite LIKE is exact on non-ASCII text, so a single character is the safe narrowing)."""
    return key[1][:1] if key[0] == "*" else key[1]


def _authors_match(authors_json: Optional[str], key: Tuple[str, str]) -> Optional[str]:
    """Return the author display name in a stored authors_json that matches `key`, else None."""
    try:
        authors = json.loads(authors_json or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(authors, list):
        return None
    for a in authors:
        name = a.get("name") if isinstance(a, dict) else (a if isinstance(a, str) else "")
        if name and _name_matches(key, str(name)):
            return str(name)
    return None


def _chunks(items: Sequence[Any], n: int = _SQL_CHUNK) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


# ---------------------------------------------------------------------------------- resolution
@dataclass
class Resolution:
    query: str
    kind: str = "unknown"  # instrument | company | person | unknown
    entity_id: str = ""
    name: str = ""
    matched_by: str = ""  # id | name | alias | ticker | mentions | tracked_author | authors
    identity: Dict[str, Any] = field(default_factory=dict)
    candidates: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _entry_identity(e: Entry) -> Dict[str, Any]:
    d: Dict[str, Any] = {"id": e.id, "name": e.name, "kind": e.kind, "ticker": e.ticker,
                         "aliases": [a for a in e.aliases if a != e.name]}
    if e.kind == "instrument":
        d.update({"vendor": e.vendor, "category": e.category, "launched": e.launched, "status": e.status})
    else:
        d.update({"parent": e.parent, "domains": list(e.domains)})
    if e.notes:
        d["notes"] = e.notes
    return d


def _candidate(e: Entry, how: str) -> Dict[str, Any]:
    return {"kind": e.kind, "id": e.id, "name": e.name, "vendor": e.vendor or e.parent, "ticker": e.ticker, "matched_by": how}


def _gazetteer_matches(query: str, instruments: Optional[Gazetteer], companies: Optional[Gazetteer]) -> List[Tuple[int, int, str, Entry]]:
    ql = query.strip().lower()
    out: List[Tuple[int, int, str, Entry]] = []
    for gz, kind_rank in ((companies, 0), (instruments, 1)):
        if gz is None:
            continue
        for e in gz.entries:
            if e.id.lower() == ql:
                out.append((0, kind_rank, "id", e))
            elif e.name.lower() == ql:
                out.append((1, kind_rank, "name", e))
            elif any(a.lower() == ql for a in e.aliases):
                out.append((2, kind_rank, "alias", e))
            elif e.kind == "company" and e.ticker and e.ticker.lower() == ql and e.ticker.lower() != "private":
                out.append((3, kind_rank, "ticker", e))
    # best rank first; companies before instruments; a parent company before the subsidiaries that share its ticker
    out.sort(key=lambda t: (t[0], t[1], 1 if t[3].parent else 0, t[3].name.lower()))
    return out


def _suggest(store: Store, query: str, instruments: Optional[Gazetteer], companies: Optional[Gazetteer], limit: int = 10) -> List[Dict[str, Any]]:
    ql = query.strip().lower()
    out: List[Dict[str, Any]] = []
    seen = set()
    if ql:
        for gz in (companies, instruments):
            for e in (gz.entries if gz else []):
                hay = [e.id, e.name, e.vendor, e.parent] + list(e.aliases)
                if any(ql in (h or "").lower() for h in hay) and e.id not in seen:
                    seen.add(e.id)
                    out.append(_candidate(e, "substring"))
        try:
            cur = store.conn.execute(
                "SELECT kind, entity_id, entity_name, vendor, ticker, COUNT(DISTINCT paper_id) AS papers FROM mentions "
                "WHERE entity_id LIKE ? OR entity_name LIKE ? GROUP BY kind, entity_id ORDER BY papers DESC LIMIT ?",
                (f"%{ql}%", f"%{ql}%", limit),
            )
            for r in _rows(cur):
                if r["entity_id"] not in seen:
                    seen.add(r["entity_id"])
                    out.append({"kind": r["kind"], "id": r["entity_id"], "name": r["entity_name"], "vendor": r["vendor"], "ticker": r["ticker"], "matched_by": "mentions"})
        except sqlite3.OperationalError:
            pass
    return out[:limit]


def _resolve_person(store: Store, query: str, subgroups: Optional[Sequence[Dict[str, Any]]]) -> Optional[Resolution]:
    key = name_key(query)
    if not key or not key[0]:
        return None  # a person needs a first and a last name (or a full non-Latin name); surname-only queries are too ambiguous
    identity: Dict[str, Any] = {"name": query, "tracked": False, "subgroups": [], "name_variants": []}
    display = query
    for g in subgroups or []:
        for a in g.get("tracked_authors") or []:
            cfg_name = str(a.get("name") or "")
            if cfg_name and _name_matches(key, cfg_name):
                identity["tracked"] = True
                identity["subgroups"].append({"subgroup": g.get("id"), "institution": a.get("institution", ""), "openalex_id": a.get("openalex_id", "")})
                if cfg_name not in identity["name_variants"]:
                    identity["name_variants"].append(cfg_name)
                display = cfg_name
    hit_names = [r[0] for r in store.conn.execute("SELECT DISTINCT author_name FROM tracked_author_hits") if _name_matches(key, r[0] or "")]
    for n in hit_names:
        if n not in identity["name_variants"]:
            identity["name_variants"].append(n)
    identity["hit_names"] = hit_names
    if identity["tracked"] or hit_names:
        if not identity["tracked"] and hit_names:
            display = hit_names[0]
        return Resolution(query=query, kind="person", entity_id="", name=display, matched_by="tracked_author", identity=identity)
    cur = store.conn.execute("SELECT authors_json FROM papers WHERE authors_json LIKE ? LIMIT 20000", (f"%{_like_needle(key)}%",))
    for (aj,) in cur:
        hit = _authors_match(aj, key)
        if hit:
            identity["name_variants"].append(hit)
            return Resolution(query=query, kind="person", entity_id="", name=query, matched_by="authors", identity=identity)
    return None


def resolve(store: Store, query: str, instruments: Optional[Gazetteer] = None, companies: Optional[Gazetteer] = None,
            subgroups: Optional[Sequence[Dict[str, Any]]] = None) -> Resolution:
    """Work out what a free-form query refers to. See the module docstring for the order of precedence."""
    q = (query or "").strip()
    res = Resolution(query=q)
    if not q:
        return res
    matches = _gazetteer_matches(q, instruments, companies)
    if matches:
        _, _, how, e = matches[0]
        res.kind, res.entity_id, res.name, res.matched_by, res.identity = e.kind, e.id, e.name, how, _entry_identity(e)
        res.candidates = [_candidate(m[3], m[2]) for m in matches[1:11]]
        return res
    row = store.conn.execute(
        "SELECT kind, entity_id, entity_name, vendor, ticker, category FROM mentions WHERE entity_id = ? COLLATE NOCASE LIMIT 1", (q,)
    ).fetchone()
    if row:
        res.kind, res.entity_id, res.name, res.matched_by = row[0], row[1], row[2] or row[1], "mentions"
        res.identity = {"id": row[1], "name": row[2], "kind": row[0], "vendor": row[3], "ticker": row[4], "category": row[5],
                        "note": "not in the gazetteer; identity taken from stored mentions"}
        return res
    person = _resolve_person(store, q, subgroups)
    if person:
        return person
    res.candidates = _suggest(store, q, instruments, companies)
    return res


# ---------------------------------------------------------------------------------- dossier
def _fill_scratch(store: Store, res: Resolution, since: str, until: str, subgroups: Optional[Sequence[Dict[str, Any]]]) -> None:
    conn = store.conn
    conn.execute(f"DROP TABLE IF EXISTS temp.{_SCRATCH}")
    conn.execute(f"CREATE TEMP TABLE {_SCRATCH} (paper_id TEXT PRIMARY KEY, via TEXT)")
    if res.kind in ENTITY_KINDS:
        conn.execute(
            f"INSERT OR IGNORE INTO {_SCRATCH} (paper_id, via) SELECT DISTINCT m.paper_id, NULL FROM mentions m "
            f"JOIN papers p ON p.id = m.paper_id WHERE m.kind = ? AND m.entity_id = ? AND {_WINDOW}",
            (res.kind, res.entity_id, since, until, since),
        )
    elif res.kind == "person":
        found = _person_papers(store, res, since, until)
        conn.executemany(f"INSERT OR IGNORE INTO {_SCRATCH} (paper_id, via) VALUES (?, ?)", list(found.items()))


def _person_papers(store: Store, res: Resolution, since: str, until: str) -> Dict[str, str]:
    """paper id -> author string that matched, for papers in the window."""
    key = name_key(res.query) or name_key(res.name)
    out: Dict[str, str] = {}
    if not key:
        return out
    cur = store.conn.execute(
        f"SELECT p.id, p.authors_json FROM papers p WHERE {_WINDOW} AND p.authors_json LIKE ?", (since, until, since, f"%{_like_needle(key)}%")
    )
    for pid, aj in cur:
        hit = _authors_match(aj, key)
        if hit:
            out[pid] = hit
    hit_names = list(res.identity.get("hit_names") or [])
    for chunk in _chunks(hit_names):
        marks = ",".join("?" for _ in chunk)
        cur = store.conn.execute(
            f"SELECT t.paper_id, t.author_name FROM tracked_author_hits t JOIN papers p ON p.id = t.paper_id "
            f"WHERE t.author_name IN ({marks}) AND {_WINDOW}",
            (*chunk, since, until, since),
        )
        for pid, name in cur:
            out.setdefault(pid, name)
    return out


def _zero_filled(months: List[str], counts: Dict[str, int], key: str) -> Tuple[List[Dict[str, Any]], str, int]:
    """Series over exactly the window months plus the sum of rows whose month falls outside them
    (undated items first seen after `until`, or malformed dates), so totals still reconcile."""
    series = [{"month": m, key: int(counts.get(m, 0))} for m in months]
    out_of_range = sum(int(v) for m, v in counts.items() if m not in months)
    return series, sparkline([s[key] for s in series]), out_of_range


def _iso_date(s: str) -> Optional[date]:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _trend(conn: sqlite3.Connection, since: str, until: str) -> Dict[str, Any]:
    """Papers in the last 30 days of the window vs the 30 days before, measured only inside [since, until]:
    the recent period is clipped to `since` (recent_days says how long it really is) and the prior period is
    reported as None, with a note, when it would start before the window."""
    end = _iso_date(until) or datetime.now(timezone.utc).date()
    start = _iso_date(since)
    r0, r1 = end - timedelta(days=29), end
    p0, p1 = end - timedelta(days=59), end - timedelta(days=30)
    if start and start > r0:
        r0 = min(start, r1)
    sql = f"SELECT COUNT(*) FROM {_SCRATCH} e JOIN papers p ON p.id = e.paper_id WHERE ({_EFFECTIVE_DATE}) BETWEEN ? AND ?"
    recent = conn.execute(sql, (r0.isoformat(), r1.isoformat())).fetchone()[0]
    out: Dict[str, Any] = {"recent_30d": recent, "prior_30d": None, "change_pct": None, "recent_from": r0.isoformat(),
                           "recent_days": (r1 - r0).days + 1, "prior_from": p0.isoformat(), "prior_to": p1.isoformat()}
    if start and p0 < start:
        out["note"] = f"prior period would start before the window ({since}); widen --since to at least 60 days to measure a trend"
        return out
    prior = conn.execute(sql, (p0.isoformat(), p1.isoformat())).fetchone()[0]
    out["prior_30d"] = prior
    out["change_pct"] = round(100.0 * (recent - prior) / prior, 1) if prior else None
    return out


def _archive_series(conn: sqlite3.Connection, where: str, params: Sequence[Any], since: str, until: str, months: List[str]) -> Dict[str, Any]:
    base = f"FROM archive_records WHERE {where} AND first_public >= ? AND first_public <= ?"
    p = (*params, since, until)
    per_month = {r[0]: (r[1], r[2]) for r in conn.execute(f"SELECT substr(first_public, 1, 7) AS m, COUNT(*), COUNT(DISTINCT study_accession) {base} GROUP BY m", p)}
    series = [{"month": m, "experiments": per_month.get(m, (0, 0))[0], "studies": per_month.get(m, (0, 0))[1]} for m in months]
    models = _rows(conn.execute(
        f"SELECT instrument_model, GROUP_CONCAT(DISTINCT archive) AS archives, COUNT(*) AS experiments, COUNT(DISTINCT study_accession) AS studies "
        f"{base} GROUP BY instrument_model ORDER BY experiments DESC LIMIT 12", p))
    total = sum(v[0] for v in per_month.values())
    out_of_range = sum(v[0] for m, v in per_month.items() if m not in months)
    return {"experiments_total": total, "experiments_out_of_range": out_of_range, "per_month": series,
            "sparkline": sparkline([s["experiments"] for s in series]), "models": models}


def _archive_for_instrument(conn: sqlite3.Connection, res: Resolution, since: str, until: str, months: List[str]) -> Dict[str, Any]:
    out = _archive_series(conn, "entity_id = ?", (res.entity_id,), since, until, months)
    out["matched_by"] = "entity_id"
    if out["experiments_total"] == 0 and res.name:
        alt = _archive_series(conn, "instrument_model LIKE ?", (f"%{res.name}%",), since, until, months)
        if alt["experiments_total"]:
            alt["matched_by"] = "model_name"
            alt["note"] = f"no archive record carries entity_id={res.entity_id!r}; counted instrument_model strings containing {res.name!r} instead"
            return alt
    return out


def _archive_for_company(conn: sqlite3.Connection, res: Resolution, since: str, until: str, months: List[str]) -> Dict[str, Any]:
    ticker = (res.identity.get("ticker") or "").strip()
    if ticker and ticker.lower() != "private":
        out = _archive_series(conn, "ticker = ?", (ticker,), since, until, months)
        out["matched_by"] = "ticker"
        if out["experiments_total"]:
            return out
    out = _archive_series(conn, "vendor = ? COLLATE NOCASE", (res.name,), since, until, months)
    out["matched_by"] = "vendor"
    return out


def _portfolio(conn: sqlite3.Connection, res: Resolution, instruments: Optional[Gazetteer], since: str, until: str) -> List[Dict[str, Any]]:
    if instruments is None:
        return []
    names = {res.name.lower()} | {a.lower() for a in res.identity.get("aliases") or []}
    ticker = (res.identity.get("ticker") or "").lower()
    owned = [e for e in instruments.entries
             if (e.vendor or "").lower() in names or (e.parent or "").lower() in names
             or (ticker and ticker != "private" and (e.ticker or "").lower() == ticker)]
    if not owned:
        return []
    ids = [e.id for e in owned]
    papers: Dict[str, int] = {}
    experiments: Dict[str, int] = {}
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for eid, n in conn.execute(
            f"SELECT m.entity_id, COUNT(DISTINCT m.paper_id) FROM mentions m JOIN papers p ON p.id = m.paper_id "
            f"WHERE m.kind = 'instrument' AND m.entity_id IN ({marks}) AND {_WINDOW} GROUP BY m.entity_id",
            (*chunk, since, until, since),
        ):
            papers[eid] = n
        for eid, n in conn.execute(
            f"SELECT entity_id, COUNT(*) FROM archive_records WHERE entity_id IN ({marks}) AND first_public >= ? AND first_public <= ? GROUP BY entity_id",
            (*chunk, since, until),
        ):
            experiments[eid] = n
    rows = [{"entity_id": e.id, "name": e.name, "category": e.category, "papers": papers.get(e.id, 0), "experiments": experiments.get(e.id, 0)} for e in owned]
    rows = [r for r in rows if r["papers"] or r["experiments"]]
    rows.sort(key=lambda r: (-r["papers"], -r["experiments"], r["name"]))
    return rows


def _news(conn: sqlite3.Connection, entity_id: str, since: str, limit: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT id, feed, title, url, published, first_seen FROM news_items WHERE companies_json LIKE ? "
        "AND (substr(published, 1, 10) >= ? OR substr(first_seen, 1, 10) >= ?) ORDER BY published DESC, first_seen DESC LIMIT ?",
        (f'%"{entity_id}"%', since, since, limit),
    )
    return _rows(cur)


def _tracked_hits(conn: sqlite3.Connection, hit_names: Sequence[str], since: str, until: str, limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for chunk in _chunks(list(hit_names)):
        marks = ",".join("?" for _ in chunk)
        out += _rows(conn.execute(
            f"SELECT t.paper_id AS id, t.author_name, t.subgroup, t.matched_by, p.title, p.pub_date, p.journal, p.url, p.kind "
            f"FROM tracked_author_hits t JOIN papers p ON p.id = t.paper_id WHERE t.author_name IN ({marks}) AND {_WINDOW} "
            f"ORDER BY ({_EFFECTIVE_DATE}) DESC LIMIT ?",
            (*chunk, since, until, since, limit),
        ))
    out.sort(key=lambda r: r.get("pub_date") or "", reverse=True)
    return out[:limit]


def dossier(store: Store, res: Resolution, since: str, until: Optional[str] = None, limit: int = 15,
            instruments: Optional[Gazetteer] = None, subgroups: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Build the full entity view as plain data (see the module docstring for the sections)."""
    until = until or _today()
    conn = store.conn
    out: Dict[str, Any] = dict(res.to_dict())
    out["window"] = {"since": since, "until": until}
    if res.kind not in ENTITY_KINDS and res.kind != "person":
        out["papers_total"] = 0
        return out
    months = month_range(since, until)
    owns_transaction = not conn.in_transaction  # a caller mid-transaction keeps control of its own commit
    _fill_scratch(store, res, since, until, subgroups)
    try:
        out["papers_total"] = conn.execute(f"SELECT COUNT(*) FROM {_SCRATCH}").fetchone()[0]
        per_month = {r[0]: r[1] for r in conn.execute(f"SELECT {_MONTH} AS m, COUNT(*) FROM {_SCRATCH} e JOIN papers p ON p.id = e.paper_id GROUP BY m")}
        out["papers_per_month"], out["sparkline"], out["papers_out_of_range"] = _zero_filled(months, per_month, "papers")
        out["trend"] = _trend(conn, since, until)
        if res.kind in ENTITY_KINDS:
            out["sections"] = _rows(conn.execute(
                f"SELECT m.section, COUNT(DISTINCT m.paper_id) AS papers, SUM(m.count) AS mentions FROM {_SCRATCH} e "
                "JOIN mentions m ON m.paper_id = e.paper_id WHERE m.kind = ? AND m.entity_id = ? GROUP BY m.section ORDER BY papers DESC",
                (res.kind, res.entity_id)))
        recent = _rows(conn.execute(
            f"SELECT p.id, p.pub_date, p.title, p.journal, p.source, p.kind, p.url, e.via FROM {_SCRATCH} e JOIN papers p ON p.id = e.paper_id "
            f"ORDER BY ({_EFFECTIVE_DATE}) DESC, p.id LIMIT ?", (limit,)))
        for r in recent:
            r["venue"] = (r.get("journal") or r.get("source") or "") + (" (preprint)" if r.get("kind") == "preprint" else "")
            r["sections"], r["snippet"] = [], ""
            if res.kind in ENTITY_KINDS:
                ms = _rows(conn.execute("SELECT section, snippet, count FROM mentions WHERE paper_id = ? AND kind = ? AND entity_id = ? ORDER BY count DESC, section",
                                        (r["id"], res.kind, res.entity_id)))
                r["sections"] = [m["section"] for m in ms]
                r["snippet"] = (ms[0]["snippet"] or "") if ms else ""
            else:
                r["sections"] = ["authors"]
                r["snippet"] = f"author: {r.get('via') or ''}"
            r.pop("via", None)
        out["recent_papers"] = recent
        co: Dict[str, List[Dict[str, Any]]] = {}
        for kind, label in (("instrument", "instruments"), ("company", "companies")):
            co[label] = _rows(conn.execute(
                f"SELECT m.entity_id, m.entity_name, m.vendor, m.ticker, COUNT(DISTINCT m.paper_id) AS papers, SUM(m.count) AS mentions "
                f"FROM {_SCRATCH} e JOIN mentions m ON m.paper_id = e.paper_id WHERE m.kind = ? AND NOT (m.kind = ? AND m.entity_id = ?) "
                "GROUP BY m.entity_id ORDER BY papers DESC, mentions DESC LIMIT ?",
                (kind, res.kind, res.entity_id, limit)))
        out["co_mentioned"] = co
        if res.kind == "instrument":
            out["archive"] = _archive_for_instrument(conn, res, since, until, months)
        elif res.kind == "company":
            out["archive"] = _archive_for_company(conn, res, since, until, months)
            out["portfolio"] = _portfolio(conn, res, instruments, since, until)
            out["news"] = _news(conn, res.entity_id, since, limit)
        else:
            out["tracked_hits"] = _tracked_hits(conn, res.identity.get("hit_names") or [], since, until, limit)
    finally:
        conn.execute(f"DROP TABLE IF EXISTS temp.{_SCRATCH}")
        if owns_transaction:
            conn.commit()  # the scratch INSERT opened an implicit transaction; do not leave it pinning a WAL snapshot
    return out


# ---------------------------------------------------------------------------------- rendering
def _fmt_count(label: str, rows: List[Dict[str, Any]], name_key_: str = "entity_name", n: int = 12) -> str:
    if not rows:
        return f"  {label} none"
    return f"  {label} " + ", ".join(f"{r[name_key_]} ({r['papers']})" for r in rows[:n])


def render_dossier(d: Dict[str, Any]) -> str:
    """Plain-text dossier for a terminal (no colour, no Unicode art)."""
    ident = d.get("identity") or {}
    lines: List[str] = []
    head = f"{d.get('name') or d.get('query')}  [{d.get('kind')}"
    head += f" {d['entity_id']}]" if d.get("entity_id") else "]"
    lines.append(head + f"  matched by {d.get('matched_by') or 'n/a'}")
    if d.get("kind") == "instrument":
        bits = [f"vendor: {ident.get('vendor') or '?'} ({ident.get('ticker') or '?'})", f"category: {ident.get('category') or '?'}"]
        if ident.get("launched"):
            bits.append(f"launched {ident['launched']}")
        if ident.get("status") and ident["status"] != "current":
            bits.append(ident["status"])
        lines.append("  " + " · ".join(bits))
    elif d.get("kind") == "company":
        bits = [f"ticker: {ident.get('ticker') or '?'}"]
        if ident.get("parent"):
            bits.append(f"parent: {ident['parent']}")
        if ident.get("domains"):
            bits.append("domains: " + ", ".join(ident["domains"]))
        lines.append("  " + " · ".join(bits))
    elif d.get("kind") == "person":
        groups = ident.get("subgroups") or []
        if groups:
            lines.append("  tracked in: " + ", ".join(f"{g['subgroup']}" + (f" ({g['institution']})" if g.get("institution") else "") for g in groups))
        else:
            lines.append("  not a tracked author (matched from stored author lists)")
        if ident.get("name_variants"):
            lines.append("  name forms: " + ", ".join(ident["name_variants"][:8]))
    if ident.get("aliases"):
        lines.append("  aliases: " + ", ".join(ident["aliases"][:12]))
    if ident.get("note"):
        lines.append(f"  note: {ident['note']}")
    if d.get("candidates"):
        lines.append("  other matches: " + ", ".join(f"{c['id']} ({c['kind']}, by {c['matched_by']})" for c in d["candidates"][:6]))
    w = d.get("window") or {}
    total = d.get("papers_total", 0)
    sec = d.get("sections") or []
    found = (" · found in: " + ", ".join(f"{s['section']} {s['papers']}" for s in sec)) if sec else ""
    lines.append(f"  window: {w.get('since')} .. {w.get('until')} · {total} papers{found}")
    if "papers_per_month" not in d:
        return "\n".join(lines)

    pm = d["papers_per_month"]
    lines.append("")
    lines.append("Papers per month")
    if pm:
        lines.append(f"  {pm[0]['month']} .. {pm[-1]['month']}   {d['sparkline']}   (max {max(s['papers'] for s in pm)}/month)")
    t = d.get("trend") or {}
    chg = f" ({t['change_pct']:+.1f}%)" if t.get("change_pct") is not None else ""
    prior = f"prior 30 days {t['prior_30d']}" if t.get("prior_30d") is not None else f"prior 30 days n/a (window starts {w.get('since')})"
    lines.append(f"  trend: last {t.get('recent_days', 30)} days {t.get('recent_30d', 0)} vs {prior}{chg}")
    if d.get("papers_out_of_range"):
        lines.append(f"  note: {d['papers_out_of_range']} paper(s) dated outside the window months (undated items first seen after --until, "
                     "or malformed dates) are in the total but not in the series")

    recent = d.get("recent_papers") or []
    lines.append("")
    lines.append(f"Recent papers ({len(recent)} of {total})")
    if not recent:
        lines.append("  none in the window")
    for r in recent:
        secs = "/".join(r.get("sections") or []) or "-"
        lines.append(f"  {r.get('pub_date') or 'n.d.'}  [{secs}]  {r.get('title') or '(untitled)'}")
        lines.append(f"              {r.get('venue') or ''}  {r.get('url') or ''}".rstrip())
        if r.get("snippet"):
            lines.append(f"              …{r['snippet'][:220]}…")

    co = d.get("co_mentioned") or {}
    lines.append("")
    lines.append("Co-mentioned entities (same papers)")
    lines.append(_fmt_count("instruments:", co.get("instruments") or []))
    lines.append(_fmt_count("companies:  ", co.get("companies") or []))

    if d.get("kind") == "company" and d.get("portfolio") is not None:
        lines.append("")
        lines.append("This vendor's instruments in the window (papers / archive experiments)")
        if not d["portfolio"]:
            lines.append("  none")
        for r in d["portfolio"][:15]:
            lines.append(f"  {r['name']:30} {r['papers']:>6} {r['experiments']:>8}   {r.get('category') or ''}")

    a = d.get("archive")
    if a is not None:
        lines.append("")
        lines.append(f"Public sequencing data (SRA/ENA experiments, matched by {a.get('matched_by')}) · {a.get('experiments_total', 0)} experiments")
        if a.get("note"):
            lines.append(f"  note: {a['note']}")
        if a.get("experiments_out_of_range"):
            lines.append(f"  note: {a['experiments_out_of_range']} experiment(s) carry a first_public outside the window months and are in the total only")
        if a.get("per_month"):
            lines.append(f"  {a['per_month'][0]['month']} .. {a['per_month'][-1]['month']}   {a['sparkline']}   (max {max(s['experiments'] for s in a['per_month'])}/month)")
        for m in a.get("models") or []:
            lines.append(f"  {m['instrument_model'] or 'unspecified':34} {m['experiments']:>7} experiments  {m['studies']:>5} studies  [{m.get('archives') or ''}]")

    if d.get("kind") == "company":
        news = d.get("news") or []
        lines.append("")
        lines.append(f"News items tagged with this company ({len(news)})")
        if not news:
            lines.append("  none in the window")
        for n in news:
            lines.append(f"  {(n.get('published') or '')[:10] or 'n.d.'}  {n.get('title') or ''}")
            lines.append(f"              {n.get('feed') or ''}  {n.get('url') or ''}".rstrip())

    if d.get("kind") == "person":
        hits = d.get("tracked_hits") or []
        lines.append("")
        lines.append(f"Tracked-author hits ({len(hits)})")
        if not hits:
            lines.append("  none in the window")
        for h in hits:
            lines.append(f"  {h.get('pub_date') or 'n.d.'}  [{h.get('subgroup')} · by {h.get('matched_by')}]  {h.get('title') or ''}")
            lines.append(f"              {h.get('journal') or ''}  {h.get('url') or ''}".rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------------- watchlist
def ensure_watchlist_schema(store: Store) -> bool:
    """Create the watchlist table if missing. Returns False when the database cannot be written."""
    try:
        store.conn.executescript(WATCHLIST_SCHEMA)
        store.conn.commit()
        return True
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "readonly" in msg or "read-only" in msg:
            return False
        raise  # e.g. "database is locked" while a run is writing: not a read-only database, say so


def watch_list(store: Store) -> List[Dict[str, Any]]:
    try:
        return _rows(store.conn.execute("SELECT term, kind, added_at FROM watchlist ORDER BY added_at, rowid"))
    except sqlite3.OperationalError:
        return []  # table absent on a read-only database


def watch_add(store: Store, term: str, kind: Optional[str] = None, instruments: Optional[Gazetteer] = None,
              companies: Optional[Gazetteer] = None, subgroups: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Add a term. Without --kind the term is resolved: gazetteer entry -> 'entity' (stored by id),
    person -> 'author', anything else -> free-text 'term'."""
    term = (term or "").strip()
    if not term:
        raise ValueError("empty watch term")
    if kind is not None and kind not in WATCH_KINDS:
        raise ValueError(f"kind must be one of {', '.join(WATCH_KINDS)}")
    if not ensure_watchlist_schema(store):
        raise sqlite3.OperationalError("the database is read-only; the watchlist cannot be changed")
    resolved: Optional[Resolution] = None
    if kind in (None, "entity", "author"):
        res = resolve(store, term, instruments, companies, subgroups)
        if res.kind in ENTITY_KINDS and kind in (None, "entity"):
            kind, term, resolved = "entity", res.entity_id, res
        elif res.kind == "person" and kind in (None, "author"):
            kind, term, resolved = "author", res.name, res
        elif kind is None:
            kind = "term"
    existing = _rows(store.conn.execute("SELECT term, kind, added_at FROM watchlist WHERE term = ?", (term,)))
    if existing:
        row = existing[0]
        row["created"] = False
        return row
    now = _now()
    store.conn.execute("INSERT INTO watchlist (term, kind, added_at) VALUES (?, ?, ?)", (term, kind, now))
    store.conn.commit()
    out: Dict[str, Any] = {"term": term, "kind": kind, "added_at": now, "created": True}
    if resolved is not None:
        out["resolved"] = {"kind": resolved.kind, "id": resolved.entity_id, "name": resolved.name, "matched_by": resolved.matched_by}
    return out


def watch_remove(store: Store, term: str) -> bool:
    if not ensure_watchlist_schema(store):
        raise sqlite3.OperationalError("the database is read-only; the watchlist cannot be changed")
    cur = store.conn.execute("DELETE FROM watchlist WHERE term = ?", ((term or "").strip(),))
    store.conn.commit()
    return cur.rowcount > 0


_ITEM_COLS = "p.id, p.pub_date, p.title, p.journal, p.source, p.kind, p.url"


def _substring_matches(store: Store, term: str, since: str, until: str, limit: int) -> Tuple[List[Dict[str, Any]], int]:
    """Exact substring search over the FTS content columns (used for CJK terms that FTS cannot tokenise).
    Returns the newest `limit` rows and the total count in the window."""
    where = (
        "FROM papers_fts f JOIN papers p ON p.id = f.id "
        f"WHERE (instr(f.title, ?) > 0 OR instr(f.abstract, ?) > 0 OR instr(f.methods, ?) > 0 OR instr(f.body, ?) > 0) AND {_WINDOW}"
    )
    params = (term, term, term, term, since, until, since)
    total = store.conn.execute(f"SELECT COUNT(*) {where}", params).fetchone()[0]
    cur = store.conn.execute(
        f"SELECT {_ITEM_COLS}, CASE WHEN instr(f.title, ?) > 0 THEN 'title' WHEN instr(f.abstract, ?) > 0 THEN 'abstract' "
        f"WHEN instr(f.methods, ?) > 0 THEN 'methods' ELSE 'body' END AS section {where} ORDER BY ({_EFFECTIVE_DATE}) DESC LIMIT ?",
        (term, term, term, *params, limit),
    )
    rows = _rows(cur)
    for r in rows:
        r["snippet"] = f"substring match in {r.pop('section')}"
    return rows, total


def _fts_matches(store: Store, term: str, since: str, until: str, limit: int) -> Tuple[List[Dict[str, Any]], int]:
    """Newest `limit` papers in the window matching an FTS5 query, plus the total count (not capped by `limit`).
    Text that is not valid FTS5 syntax falls back to a phrase query, exactly as Store.search does."""
    if CJK_RE.search(term):
        return _substring_matches(store, term, since, until, limit)
    sql_from = f"FROM papers_fts JOIN papers p ON p.id = papers_fts.id WHERE papers_fts MATCH ? AND {_WINDOW}"
    select = f"SELECT {_ITEM_COLS}, snippet(papers_fts, -1, '[', ']', ' … ', 18) AS snippet {sql_from} ORDER BY ({_EFFECTIVE_DATE}) DESC LIMIT ?"

    def run(fts_query: str) -> Tuple[List[Dict[str, Any]], int]:
        params = (fts_query, since, until, since)
        total = store.conn.execute(f"SELECT COUNT(*) {sql_from}", params).fetchone()[0]
        return _rows(store.conn.execute(select, (*params, limit))), total

    try:
        return run(term)
    except sqlite3.OperationalError:
        return run('"' + term.replace('"', " ") + '"')


def _entity_matches(store: Store, kind: str, entity_id: str, since: str, until: str, limit: int) -> Tuple[List[Dict[str, Any]], int]:
    where = f"FROM mentions m JOIN papers p ON p.id = m.paper_id WHERE m.kind = ? AND m.entity_id = ? AND {_WINDOW}"
    params = (kind, entity_id, since, until, since)
    total = store.conn.execute(f"SELECT COUNT(DISTINCT p.id) {where}", params).fetchone()[0]
    rows = _rows(store.conn.execute(
        f"SELECT {_ITEM_COLS}, GROUP_CONCAT(DISTINCT m.section) AS sections, MIN(m.snippet) AS snippet {where} "
        f"GROUP BY p.id ORDER BY ({_EFFECTIVE_DATE}) DESC LIMIT ?", (*params, limit)))
    return rows, total


def watch_run(store: Store, since: str, until: Optional[str] = None, limit: int = 20, instruments: Optional[Gazetteer] = None,
              companies: Optional[Gazetteer] = None, subgroups: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Evaluate every watchlist term over [since, until]. Per term: `matches` is the full count in the window,
    `items` the newest `limit` of them (`shown` = len(items))."""
    until = until or _today()
    report: Dict[str, Any] = {"since": since, "until": until, "terms": [], "total_matches": 0}
    for row in watch_list(store):
        term, kind = row["term"], row["kind"]
        entry: Dict[str, Any] = {"term": term, "kind": kind, "added_at": row["added_at"], "matches": 0, "shown": 0, "items": []}
        total = 0
        try:
            if kind == "entity":
                res = resolve(store, term, instruments, companies, subgroups)
                if res.kind in ENTITY_KINDS:
                    entry["resolved"] = {"kind": res.kind, "id": res.entity_id, "name": res.name}
                    entry["items"], total = _entity_matches(store, res.kind, res.entity_id, since, until, limit)
                else:
                    entry["note"] = "entity id not found in gazetteers or mentions; searched as free text"
                    entry["items"], total = _fts_matches(store, term, since, until, limit)
            elif kind == "author":
                res = resolve(store, term, instruments, companies, subgroups)
                if res.kind != "person":
                    res = Resolution(query=term, kind="person", name=term, matched_by="authors", identity={"hit_names": []})
                found = _person_papers(store, res, since, until)
                total = len(found)
                items: List[Dict[str, Any]] = []
                for chunk in _chunks(list(found)):
                    marks = ",".join("?" for _ in chunk)
                    items += _rows(store.conn.execute(
                        f"SELECT {_ITEM_COLS}, ({_EFFECTIVE_DATE}) AS effective_date FROM papers p WHERE p.id IN ({marks})", tuple(chunk)))
                items.sort(key=lambda r: (r.pop("effective_date", "") or "", r["id"]), reverse=True)
                for it in items:
                    it["snippet"] = f"author: {found.get(it['id'], '')}"
                entry["items"] = items[:limit]
            else:
                entry["items"], total = _fts_matches(store, term, since, until, limit)
        except sqlite3.OperationalError as exc:
            entry["error"] = str(exc)
        entry["matches"] = total
        entry["shown"] = len(entry["items"])
        report["total_matches"] += total
        report["terms"].append(entry)
    return report


def render_watch_report(rep: Dict[str, Any]) -> str:
    lines = [f"Watchlist {rep.get('since')} .. {rep.get('until')}: {len(rep.get('terms') or [])} terms, {rep.get('total_matches', 0)} matches"]
    if not rep.get("terms"):
        lines.append("  (empty watchlist: add terms with `litradar watch add \"methylation ctDNA\"`)")
    for t in rep.get("terms") or []:
        label = f"{t['term']} ({t['kind']}"
        if t.get("resolved"):
            label += f" -> {t['resolved']['id']}"
        label += ")"
        lines.append("")
        shown = t.get("shown", len(t.get("items") or []))
        more = f" (showing {shown})" if shown < t["matches"] else ""
        lines.append(f"## {label}: {t['matches']} matches{more}" + (f"  [{t['error']}]" if t.get("error") else ""))
        if t.get("note"):
            lines.append(f"  note: {t['note']}")
        for it in t.get("items") or []:
            venue = (it.get("journal") or it.get("source") or "") + (" (preprint)" if it.get("kind") == "preprint" else "")
            lines.append(f"- {it.get('pub_date') or 'n.d.'}  {it.get('title') or '(untitled)'}")
            lines.append(f"    {venue}  {it.get('url') or ''}".rstrip())
            snip = (it.get("snippet") or "").replace("\n", " ").strip()
            if snip:
                lines.append(f"    …{snip[:200]}…")
    return "\n".join(lines)
