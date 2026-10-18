"""Safe, line-based editing of the YAML configuration from the command line.

The config files are the user's own hand-written documents: comments, section headers and
ordering carry meaning. So this module never dumps a whole file back through the YAML emitter.
Every edit is a line insertion, a line replacement or a line removal, followed by a reload of
the file through the same code the pipeline uses (Gazetteer / yaml.safe_load) and the same
collision checks as `litradar check-config`. Before writing, the file is copied to
`.cache/config-backups/<file>.<timestamp>`; if the re-validation fails, the backup is restored.

File styles handled
-------------------
* companies.yaml / instruments.yaml: one flow-style mapping per line under `entries:`
  (`- {id: ..., name: ..., aliases: ["..."]}`); new entries are appended after the last one.
* subgroups.yaml: block style; tracked authors, keywords and OpenAlex topics are edited inside
  the subgroup's block, keeping every other line (including trailing comments) untouched.
* radar.yaml: `watchlist_terms` (block list) and a `priorities:` section written by
  `litradar prioritize`.

`priority_multiplier()` is the one hook other code calls to apply the priorities.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from .config import Settings, load_yaml
from .extract.gazetteer import Gazetteer

COMPANY_TYPES = ("tools", "diagnostics", "synthesis", "ai_bio", "pharma", "cro", "institute", "software")
INSTRUMENT_CATEGORIES = (
    "sequencer", "single_cell", "spatial", "proteomics", "library_prep", "synthesis", "pcr", "array", "cytometry",
    "cryo_em", "imaging", "automation", "cell_engineering", "bioprocess", "qc", "software", "reagent", "method",
)
PRIORITY_KINDS = ("subgroup", "company", "instrument_category", "journal", "source")
PRIORITY_MIN, PRIORITY_MAX = 0.05, 20.0

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_CACHE_MAX_AGE_DAYS = 7
SEC_USER_AGENT = "lit-radar/0.1 {email}"  # the SEC's "<name> <e-mail>" format; a URL in the UA gets a 403
TICKER_MIN_CONFIDENCE = 0.85  # below this the SEC match is shown but never written
TICKER_CANDIDATE_FLOOR = 0.7  # below this a fuzzy SEC match is not even listed as a candidate (0.62 'ProShares Trust II' for a biotech name is noise)
OPENALEX_AUTHORS_URL = "https://api.openalex.org/authors"
ORCID_SEARCH_URL = "https://pub.orcid.org/v3.0/expanded-search/"

_QUOTED_LIST_KEYS = {"aliases", "case_sensitive_aliases", "context_terms", "keywords"}
COMPANY_KEY_ORDER = ["id", "name", "ticker", "type", "parent", "watch_news", "domains", "aliases",
                      "case_sensitive_aliases", "context_required", "context_terms", "news_query", "notes"]
INSTRUMENT_KEY_ORDER = ["id", "name", "vendor", "ticker", "category", "parent", "launched", "status", "aliases",
                         "context_required", "context_terms", "case_sensitive_aliases", "notes"]


class TrackError(Exception):
    """A refused or rolled-back edit; the message is meant for the terminal."""


# ----------------------------------------------------------------------------- small helpers
def kebab(name: str) -> str:
    """'10x Genomics' -> '10x-genomics', 'Schrödinger' -> 'schrodinger'."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def snake(name: str) -> str:
    return kebab(name).replace("-", "_")


def _scalar(value: Any, quote: bool = False) -> str:
    """Render one YAML scalar for a flow context; plain when it round-trips, else double-quoted."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    s = str(value)
    if not quote and s and "\n" not in s:
        try:
            if yaml.safe_load("{k: " + s + "}") == {"k": s} and yaml.safe_load("- " + s) == [s]:
                return s
        except yaml.YAMLError:
            pass
    return json.dumps(s, ensure_ascii=False)


def _flow_list(values: Iterable[Any], quote: bool) -> str:
    return "[" + ", ".join(_scalar(v, quote=quote) for v in values) + "]"


def format_flow_entry(entry: Dict[str, Any], key_order: Sequence[str]) -> str:
    """Render `- {id: x, name: Y, ...}` in the style of companies.yaml / instruments.yaml."""
    keys = [k for k in key_order if k in entry] + [k for k in entry if k not in key_order]
    parts = []
    for k in keys:
        v = entry[k]
        if isinstance(v, (list, tuple)):
            parts.append(f"{k}: {_flow_list(v, quote=k in _QUOTED_LIST_KEYS)}")
        else:
            parts.append(f"{k}: {_scalar(v)}")
    line = "- {" + ", ".join(parts) + "}"
    if yaml.safe_load(line) != [entry]:
        raise TrackError(f"internal: rendered entry does not round-trip: {line}")
    return line


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def backup_file(path: Path, cache_dir: Path) -> Path:
    bdir = Path(cache_dir) / "config-backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / f"{path.name}.{_timestamp()}"
    shutil.copy2(path, dst)
    return dst


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp-litradar")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def apply_edit(
    path: Path,
    transform: Callable[[List[str]], List[str]],
    validate: Callable[[Path], None],
    cache_dir: Path,
) -> Optional[Path]:
    """Back up, rewrite `path` through a line-list transform, validate on disk, roll back on failure.

    Returns the backup path, or None when the transform changed nothing."""
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    new_lines = transform(original.splitlines())
    new_text = "\n".join(new_lines) + "\n"
    if new_text == original:
        return None
    bak = backup_file(path, cache_dir)
    _atomic_write(path, new_text)
    try:
        validate(path)
    except Exception as exc:
        shutil.copy2(bak, path)
        raise TrackError(f"{path.name}: edit rolled back ({exc})") from exc
    return bak


# ----------------------------------------------------------------------------- validation
def gazetteer_problems(data: Any, kind: str) -> List[str]:
    """Duplicate ids, missing id/name and alias collisions: the same checks as `litradar check-config`."""
    entries = (data or {}).get("entries", []) if isinstance(data, dict) else (data or [])
    problems: List[str] = []
    ids = [e.get("id") for e in entries if isinstance(e, dict)]
    for dup in sorted({i for i in ids if ids.count(i) > 1}, key=str):
        problems.append(f"duplicate id {dup!r}")
    for e in entries:
        if not isinstance(e, dict) or not e.get("id") or not e.get("name"):
            problems.append(f"entry missing id/name: {e!r}"[:120])
    gz = Gazetteer.from_yaml(data if isinstance(data, dict) else {"entries": entries}, kind)
    seen: Dict[str, str] = {}
    for e in gz.entries:
        for a in e.aliases:
            key = a.lower()
            if key in seen and seen[key] != e.id:
                problems.append(f"alias collision: {a!r} in {seen[key]} and {e.id}")
            seen.setdefault(key, e.id)
    return problems


def gazetteer_conflicts(entries: Sequence[Dict[str, Any]], new: Dict[str, Any]) -> List[str]:
    """Why `new` cannot be added next to `entries` (same id, or a name/alias another entry already claims)."""
    out: List[str] = []
    if any(e.get("id") == new["id"] for e in entries):
        out.append(f"id {new['id']!r} already exists (use `litradar untrack` first, or pick another name)")
    claimed: Dict[str, str] = {}
    for e in entries:
        for a in [e.get("name")] + list(e.get("aliases") or []):
            if a:
                claimed.setdefault(str(a).lower(), str(e.get("id")))
    for a in [new.get("name")] + list(new.get("aliases") or []):
        if a and str(a).lower() in claimed:
            out.append(f"{a!r} is already a name/alias of {claimed[str(a).lower()]}")
    return out


def _validator_for_gazetteer(kind: str, baseline: List[str]) -> Callable[[Path], None]:
    def validate(path: Path) -> None:
        data = load_yaml(path)
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise TrackError("file no longer has an `entries:` list")
        new_problems = [p for p in gazetteer_problems(data, kind) if p not in baseline]
        if new_problems:
            raise TrackError("; ".join(new_problems))

    return validate


def _validate_subgroups(path: Path) -> None:
    data = load_yaml(path)
    groups = data.get("subgroups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise TrackError("subgroups.yaml has no `subgroups:` list")
    ids = []
    for g in groups:
        if not isinstance(g, dict) or not g.get("id"):
            raise TrackError(f"subgroup without id: {g!r}"[:120])
        ids.append(g["id"])
        for a in g.get("tracked_authors") or []:
            if not isinstance(a, dict) or not a.get("name"):
                raise TrackError(f"tracked author without name in {g['id']}: {a!r}"[:120])
        for key in ("keywords", "openalex_topics"):
            if g.get(key) is not None and not isinstance(g[key], list):
                raise TrackError(f"{g['id']}.{key} is not a list")
    if len(set(ids)) != len(ids):
        raise TrackError("duplicate subgroup ids")


_TOP_KEY_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*:")


def duplicate_top_level_keys(text: str) -> List[str]:
    """Top-level mapping keys that appear more than once in a block-style YAML document.

    PyYAML silently keeps the last duplicate, so `priorities: {}` followed by a second `priorities:` block
    loads without complaint while the file carries two headers; this catches it at edit time."""
    seen: Dict[str, int] = {}
    for ln in text.splitlines():
        if not ln or ln[0] in " \t#-":
            continue
        m = _TOP_KEY_RE.match(ln)
        if m:
            seen[m.group(1)] = seen.get(m.group(1), 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def _validate_radar(path: Path) -> None:
    dups = duplicate_top_level_keys(path.read_text(encoding="utf-8"))
    if dups:
        raise TrackError(f"duplicate top-level key(s): {', '.join(dups)}")
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise TrackError("radar.yaml is not a mapping")
    terms = data.get("watchlist_terms")
    if terms is not None and (not isinstance(terms, list) or not all(isinstance(t, str) for t in terms)):
        raise TrackError("watchlist_terms is not a list of strings")
    pri = data.get("priorities")
    if pri is not None:
        if not isinstance(pri, dict):
            raise TrackError("priorities is not a mapping")
        for kind, table in pri.items():
            if table is None:
                continue
            if not isinstance(table, dict):
                raise TrackError(f"priorities.{kind} is not a mapping")
            for k, w in table.items():
                if not isinstance(w, (int, float)) or isinstance(w, bool):
                    raise TrackError(f"priorities.{kind}.{k} is not a number")


# ----------------------------------------------------------------------------- flow-style gazetteers
def _flow_entry_lines(lines: Sequence[str]) -> List[Tuple[int, Dict[str, Any]]]:
    out = []
    for i, line in enumerate(lines):
        if line.startswith("- {"):
            try:
                parsed = yaml.safe_load(line)
            except yaml.YAMLError:
                continue
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                out.append((i, parsed[0]))
    return out


def _append_flow_entry(lines: List[str], rendered: str) -> List[str]:
    idx = [i for i, _ in _flow_entry_lines(lines)]
    if idx:
        pos = idx[-1] + 1
    else:
        heads = [i for i, ln in enumerate(lines) if ln.startswith("entries:")]
        if not heads:
            raise TrackError("no `entries:` key found")
        pos = heads[0] + 1
    return lines[:pos] + [rendered] + lines[pos:]


def _remove_flow_entry(lines: List[str], target: str) -> Tuple[List[str], Dict[str, Any]]:
    t = target.lower()
    for i, entry in _flow_entry_lines(lines):
        if str(entry.get("id", "")).lower() == t or str(entry.get("name", "")).lower() == t:
            return lines[:i] + lines[i + 1 :], entry
    raise TrackError(f"no entry with id or name {target!r}")


def _company_entries(settings: Settings) -> List[Dict[str, Any]]:
    return list((settings.yaml("companies") or {}).get("entries") or [])


def find_company(settings: Settings, text: str) -> Optional[Dict[str, Any]]:
    """Company entry by id, name, alias or ticker (case-insensitive)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    entries = _company_entries(settings)
    for e in entries:
        if str(e.get("id", "")).lower() == t or str(e.get("name", "")).lower() == t:
            return e
    for e in entries:
        if any(str(a).lower() == t for a in (e.get("aliases") or [])):
            return e
    for e in entries:
        if str(e.get("ticker", "")).lower() == t and t not in ("private", "n/a"):
            return e
    return None


def add_company(
    settings: Settings,
    name: str,
    ticker: str = "",
    type_: str = "tools",
    aliases: Sequence[str] = (),
    domains: Sequence[str] = (),
    watch_news: bool = False,
    parent: str = "",
    entry_id: str = "",
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Append a company to companies.yaml. Returns (entry, backup path)."""
    name = (name or "").strip()
    if not name:
        raise TrackError("a company name is required")
    if type_ not in COMPANY_TYPES:
        raise TrackError(f"type must be one of {', '.join(COMPANY_TYPES)}")
    entry: Dict[str, Any] = {
        "id": entry_id or kebab(name),
        "name": name,
        "ticker": (ticker or "").strip() or "private",
        "type": type_,
    }
    if parent:
        entry["parent"] = parent
    entry["watch_news"] = bool(watch_news)
    entry["domains"] = [d.strip().lower() for d in domains if d.strip()]
    entry["aliases"] = list(dict.fromkeys(a.strip() for a in aliases if a.strip() and a.strip() != name))
    if not entry["id"]:
        raise TrackError(f"could not derive an id from {name!r}")
    path = Path(settings.config_dir) / "companies.yaml"
    data = load_yaml(path)
    conflicts = gazetteer_conflicts(data.get("entries") or [], entry)
    if conflicts:
        raise TrackError("refused: " + "; ".join(conflicts))
    rendered = format_flow_entry(entry, COMPANY_KEY_ORDER)
    baseline = gazetteer_problems(data, "company")
    bak = apply_edit(path, lambda ls: _append_flow_entry(ls, rendered), _validator_for_gazetteer("company", baseline), settings.cache_dir)
    return entry, bak


def _vendor_prefix(entries: Sequence[Dict[str, Any]], vendor_name: str) -> str:
    """The id prefix this vendor's existing instruments use ('pacb' for Pacific Biosciences), else its first word."""
    counts: Dict[str, int] = {}
    for e in entries:
        if str(e.get("vendor", "")).lower() == vendor_name.lower() and e.get("id"):
            p = str(e["id"]).split("-")[0]
            counts[p] = counts.get(p, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
    return kebab(vendor_name).split("-")[0] or "x"


def add_instrument(
    settings: Settings,
    name: str,
    vendor: str,
    category: str,
    aliases: Sequence[str] = (),
    context_terms: Sequence[str] = (),
    ticker: str = "",
    launched: Optional[int] = None,
    entry_id: str = "",
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Append an instrument to instruments.yaml; the ticker comes from the vendor's company entry unless given."""
    name = (name or "").strip()
    if not name:
        raise TrackError("an instrument name is required")
    if not vendor or not vendor.strip():
        raise TrackError("--vendor is required (the company that sells it)")
    base_cat = (category or "").split(".", 1)[0]
    if base_cat not in INSTRUMENT_CATEGORIES:
        raise TrackError(f"category must start with one of {', '.join(INSTRUMENT_CATEGORIES)} (e.g. sequencer.long_read)")
    company = find_company(settings, vendor)
    if company:
        vendor_name = str(company.get("name"))
        ticker = ticker or str(company.get("ticker") or "private")
    else:
        if not ticker:
            raise TrackError(
                f"vendor {vendor!r} is not in companies.yaml: add it first (`litradar track company \"{vendor}\"`) or pass --ticker"
            )
        vendor_name = vendor.strip()
    path = Path(settings.config_dir) / "instruments.yaml"
    data = load_yaml(path)
    existing = list(data.get("entries") or [])
    if not entry_id:
        prefix = _vendor_prefix(existing, vendor_name)
        slug = kebab(name)
        entry_id = slug if slug.startswith(prefix + "-") else f"{prefix}-{slug}"
    entry: Dict[str, Any] = {
        "id": entry_id,
        "name": name,
        "vendor": vendor_name,
        "ticker": ticker,
        "category": category,
    }
    if launched:
        entry["launched"] = int(launched)
    entry["aliases"] = list(dict.fromkeys(a.strip() for a in aliases if a.strip() and a.strip() != name))
    ctx = [c.strip() for c in context_terms if c.strip()]
    if ctx:
        entry["context_required"] = True
        entry["context_terms"] = ctx
    conflicts = gazetteer_conflicts(existing, entry)
    if conflicts:
        raise TrackError("refused: " + "; ".join(conflicts))
    rendered = format_flow_entry(entry, INSTRUMENT_KEY_ORDER)
    baseline = gazetteer_problems(data, "instrument")
    bak = apply_edit(path, lambda ls: _append_flow_entry(ls, rendered), _validator_for_gazetteer("instrument", baseline), settings.cache_dir)
    return entry, bak


def remove_gazetteer_entry(settings: Settings, kind: str, target: str) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Remove a company or instrument by id or name."""
    fname = "companies" if kind == "company" else "instruments"
    path = Path(settings.config_dir) / f"{fname}.yaml"
    removed: Dict[str, Any] = {}

    def transform(lines: List[str]) -> List[str]:
        new, entry = _remove_flow_entry(lines, target)
        removed.update(entry)
        return new

    baseline = gazetteer_problems(load_yaml(path), kind)
    bak = apply_edit(path, transform, _validator_for_gazetteer(kind, baseline), settings.cache_dir)
    return removed, bak


# ----------------------------------------------------------------------------- subgroups.yaml (block style)
_SUBGROUP_RE = re.compile(r"^  - id:\s*(\S+)\s*(#.*)?$")


def _subgroup_spans(lines: Sequence[str]) -> Dict[str, Tuple[int, int]]:
    """{subgroup id: (first line, line after last)}. A block ends at the next `  - id:` or the next top-level key."""
    starts = [(i, m.group(1).strip("'\"")) for i, ln in enumerate(lines) for m in [_SUBGROUP_RE.match(ln)] if m]
    spans: Dict[str, Tuple[int, int]] = {}
    for n, (start, sg_id) in enumerate(starts):
        end = len(lines)
        for j in range(start + 1, len(lines)):
            ln = lines[j]
            if _SUBGROUP_RE.match(ln) or (ln.strip() and not ln.startswith(" ") and not ln.lstrip().startswith("#")):
                end = j
                break
        if n + 1 < len(starts):
            end = min(end, starts[n + 1][0])
        spans[sg_id] = (start, end)
    return spans


def _block_insert_point(lines: Sequence[str], start: int, end: int) -> int:
    """Index after the last non-blank, non-comment line of a block."""
    k = end
    while k > start and (not lines[k - 1].strip() or lines[k - 1].lstrip().startswith("#")):
        k -= 1
    return k


def _find_flow_list_close(s: str, open_idx: int) -> int:
    depth, i, quote = 0, open_idx, ""
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise TrackError("unterminated flow list")


def _render_author(author: Dict[str, Any]) -> str:
    parts = [f"name: {_scalar(author.get('name', ''), quote=True)}", f"openalex_id: {_scalar(author.get('openalex_id', ''), quote=True)}"]
    if author.get("orcid"):
        parts.append(f"orcid: {_scalar(author['orcid'], quote=True)}")
    parts.append(f"institution: {_scalar(author.get('institution', ''), quote=True)}")
    line = "      - {" + ", ".join(parts) + "}"
    expected = {k: v for k, v in author.items() if k in ("name", "openalex_id", "orcid", "institution") and (k != "orcid" or v)}
    if yaml.safe_load(line) != [expected]:
        raise TrackError("internal: author line does not round-trip")
    return line


def _author_key(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    parts = re.sub(r"[^a-z ]", " ", s).split()
    return f"{parts[0]} {parts[-1]}" if len(parts) > 1 else (parts[0] if parts else "")


def _subgroups(settings: Settings) -> List[Dict[str, Any]]:
    return list((settings.yaml("subgroups") or {}).get("subgroups") or [])


def add_author(
    settings: Settings,
    subgroup: str,
    name: str,
    openalex_id: str = "",
    orcid: str = "",
    institution: str = "",
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Append a tracked author to a subgroup's tracked_authors (line insert; comments and order preserved)."""
    name = (name or "").strip()
    if not name:
        raise TrackError("an author name is required")
    groups = {g["id"]: g for g in _subgroups(settings) if g.get("id")}
    if subgroup not in groups:
        raise TrackError(f"subgroup {subgroup!r} not found (have: {', '.join(groups)}); create it with `litradar track topic {subgroup} --name ...`")
    for a in groups[subgroup].get("tracked_authors") or []:
        if _author_key(str(a.get("name", ""))) == _author_key(name) or (openalex_id and str(a.get("openalex_id") or "") == openalex_id):
            raise TrackError(f"refused: {a.get('name')!r} is already tracked in {subgroup}")
    author: Dict[str, Any] = {"name": name, "openalex_id": (openalex_id or "").strip()}
    if orcid:
        author["orcid"] = orcid.strip()
    author["institution"] = (institution or "").strip()
    rendered = _render_author(author)

    def transform(lines: List[str]) -> List[str]:
        spans = _subgroup_spans(lines)
        start, end = spans[subgroup]
        key_idx = next((i for i in range(start, end) if re.match(r"^    tracked_authors:", lines[i])), None)
        if key_idx is None:
            pos = _block_insert_point(lines, start, end)
            return lines[:pos] + ["    tracked_authors:", rendered] + lines[pos:]
        m = re.match(r"^    tracked_authors:\s*\[\s*\]\s*(#.*)?$", lines[key_idx])
        if m:  # empty flow list -> open a block list
            tail = ("  " + m.group(1)) if m.group(1) else ""
            return lines[:key_idx] + ["    tracked_authors:" + tail, rendered] + lines[key_idx + 1 :]
        last = key_idx
        for i in range(key_idx + 1, end):
            if lines[i].startswith("      - "):
                last = i
            elif lines[i].strip() and not lines[i].lstrip().startswith("#") and not lines[i].startswith("        "):
                break
        return lines[: last + 1] + [rendered] + lines[last + 1 :]

    bak = apply_edit(Path(settings.config_dir) / "subgroups.yaml", transform, _validate_subgroups, settings.cache_dir)
    return author, bak


def remove_author(settings: Settings, target: str, subgroup: str = "") -> Tuple[List[Tuple[str, str]], Optional[Path]]:
    """Remove a tracked author (by name or OpenAlex id) from one subgroup or from all of them."""
    key = _author_key(target)
    removed: List[Tuple[str, str]] = []

    def transform(lines: List[str]) -> List[str]:
        spans = _subgroup_spans(lines)
        if subgroup and subgroup not in spans:
            raise TrackError(f"subgroup {subgroup!r} not found")
        drop = set()
        for sg_id, (start, end) in spans.items():
            if subgroup and sg_id != subgroup:
                continue
            for i in range(start, end):
                if not lines[i].startswith("      - "):
                    continue
                try:
                    item = yaml.safe_load(lines[i])
                except yaml.YAMLError:
                    continue
                a = item[0] if isinstance(item, list) and item and isinstance(item[0], dict) else None
                if not a:
                    continue
                if (key and _author_key(str(a.get("name", ""))) == key) or (target and str(a.get("openalex_id") or "") == target):
                    drop.add(i)
                    removed.append((sg_id, str(a.get("name"))))
        if not drop:
            raise TrackError(f"no tracked author matching {target!r}" + (f" in {subgroup}" if subgroup else ""))
        return [ln for i, ln in enumerate(lines) if i not in drop]

    bak = apply_edit(Path(settings.config_dir) / "subgroups.yaml", transform, _validate_subgroups, settings.cache_dir)
    return removed, bak


def _edit_flow_list_line(line: str, key: str, add: Sequence[str], remove: Sequence[str], quote: bool) -> str:
    """Rewrite `    key: [a, b]  # comment` with items added/removed, keeping the comment."""
    m = re.match(rf"^(\s*{re.escape(key)}:\s*)(\[.*)$", line)
    if not m:
        raise TrackError(f"{key} is not a flow list on one line; edit it by hand")
    head, rest = m.group(1), m.group(2)
    close = _find_flow_list_close(rest, 0)
    items = list(yaml.safe_load(rest[: close + 1]) or [])
    tail = rest[close + 1 :]
    low = {str(x).lower() for x in items}
    for a in add:
        if str(a).lower() not in low:
            items.append(a)
            low.add(str(a).lower())
    if remove:
        rm = {str(r).lower() for r in remove}
        items = [x for x in items if str(x).lower() not in rm]
    return head + _flow_list(items, quote=quote) + tail


def edit_subgroup(
    settings: Settings,
    subgroup: str,
    add_keywords: Sequence[str] = (),
    add_topics: Sequence[str] = (),
    remove_keywords: Sequence[str] = (),
    remove_topics: Sequence[str] = (),
    name: str = "",
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Add/remove keywords and OpenAlex topics on a subgroup, creating the subgroup when `name` is given."""
    sg_id = snake(subgroup)
    if not sg_id:
        raise TrackError("a subgroup id is required")
    groups = {g["id"]: g for g in _subgroups(settings) if g.get("id")}
    add_keywords = [k.strip() for k in add_keywords if k.strip()]
    add_topics = [t.strip() for t in add_topics if t.strip()]
    for t in add_topics:
        if not re.fullmatch(r"T\d+", t):
            raise TrackError(f"OpenAlex topic ids look like T11289, got {t!r}")
    creating = sg_id not in groups
    if creating and not name:
        raise TrackError(f"subgroup {sg_id!r} does not exist; pass --name to create it (have: {', '.join(groups)})")

    def transform(lines: List[str]) -> List[str]:
        spans = _subgroup_spans(lines)
        if creating:
            block = [
                f"  - id: {sg_id}",
                f"    name: {_scalar(name)}",
                f"    openalex_topics: {_flow_list(add_topics, quote=False)}",
                f"    keywords: {_flow_list(add_keywords, quote=True)}",
                "    tracked_authors: []",
            ]
            if spans:
                pos = _block_insert_point(lines, 0, max(e for _, e in spans.values()))
            else:
                heads = [i for i, ln in enumerate(lines) if ln.startswith("subgroups:")]
                if not heads:
                    raise TrackError("subgroups.yaml has no `subgroups:` key")
                pos = heads[0] + 1
            return lines[:pos] + block + lines[pos:]
        start, end = spans[sg_id]
        out = list(lines)
        for key, add, remove, quote in (("keywords", add_keywords, remove_keywords, True), ("openalex_topics", add_topics, remove_topics, False)):
            if not add and not remove:
                continue
            idx = next((i for i in range(start, end) if re.match(rf"^    {key}:", out[i])), None)
            if idx is None:
                if not add:
                    continue
                name_idx = next((i for i in range(start, end) if re.match(r"^    name:", out[i])), start)
                out.insert(name_idx + 1, f"    {key}: {_flow_list(add, quote=quote)}")
                end += 1
            else:
                out[idx] = _edit_flow_list_line(out[idx], key, add, remove, quote)
        return out

    bak = apply_edit(Path(settings.config_dir) / "subgroups.yaml", transform, _validate_subgroups, settings.cache_dir)
    group = next((g for g in _subgroups(settings) if g.get("id") == sg_id), {})
    return group, bak


def remove_subgroup(settings: Settings, subgroup: str) -> Tuple[Dict[str, Any], Optional[Path]]:
    sg_id = snake(subgroup)
    group = next((g for g in _subgroups(settings) if g.get("id") == sg_id), None)
    if group is None:
        raise TrackError(f"subgroup {subgroup!r} not found")

    def transform(lines: List[str]) -> List[str]:
        start, end = _subgroup_spans(lines)[sg_id]
        return lines[:start] + lines[end:]

    bak = apply_edit(Path(settings.config_dir) / "subgroups.yaml", transform, _validate_subgroups, settings.cache_dir)
    return group, bak


# ----------------------------------------------------------------------------- radar.yaml: watchlist terms
def _flow_value_line(lines: Sequence[str], key: str) -> Optional[Tuple[int, str, str]]:
    """(index, flow text, trailing comment) when the top-level `key:` carries an inline flow value
    (`priorities: {}`, `watchlist_terms: [a, b]`), else None."""
    rx = re.compile(rf"^{re.escape(key)}:\s*([\[{{].*?[\]}}])\s*(#.*)?$")
    for i, ln in enumerate(lines):
        m = rx.match(ln)
        if m:
            return i, m.group(1), m.group(2) or ""
    return None


def _block_lines(value: Any, indent: int) -> List[str]:
    """Render a parsed mapping or list as block-style YAML lines at `indent` spaces."""
    pad = " " * indent
    out: List[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{_scalar(str(k))}:")
                out.extend(_block_lines(v, indent + 2))
            else:
                rendered = f"{float(v):g}" if isinstance(v, float) else _scalar(v) if not isinstance(v, (dict, list)) else ("{}" if isinstance(v, dict) else "[]")
                out.append(f"{pad}{_scalar(str(k))}: {rendered}")
    elif isinstance(value, list):
        for v in value:
            out.append(f"{pad}- {_scalar(v)}")
    return out


def expand_flow_value(lines: List[str], key: str) -> List[str]:
    """Rewrite a top-level `key: {...}` / `key: [...]` line into block form so the block editors can
    extend it in place instead of appending a second top-level key. Comments on the line are kept;
    lines are returned unchanged when the key is absent or already in block form."""
    found = _flow_value_line(lines, key)
    if found is None:
        return lines
    idx, flow, comment = found
    try:
        value = yaml.safe_load(flow)
    except yaml.YAMLError:
        return lines
    header = f"{key}:" + (f"  {comment}" if comment else "")
    body = _block_lines(value, 2) if isinstance(value, (dict, list)) else []
    return lines[:idx] + [header] + body + lines[idx + 1 :]


def _top_level_block(lines: Sequence[str], key: str) -> Tuple[int, int]:
    """(key line index, index after the block) for a top-level `key:` in a block-style file."""
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(key)}:\s*(#.*)?$", ln)), None)
    if start is None:
        raise KeyError(key)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and not ln.startswith(" ") and not ln.lstrip().startswith("#"):
            end = j
            break
    return start, end


def _radar(settings: Settings) -> Dict[str, Any]:
    return load_yaml(Path(settings.config_dir) / "radar.yaml") or {}


def add_term(settings: Settings, term: str) -> Tuple[str, Optional[Path]]:
    term = (term or "").strip()
    if not term:
        raise TrackError("a term is required")
    existing = [str(t) for t in _radar(settings).get("watchlist_terms") or []]
    if term.lower() in {t.lower() for t in existing}:
        raise TrackError(f"refused: {term!r} is already a watchlist term")

    def transform(lines: List[str]) -> List[str]:
        lines = expand_flow_value(list(lines), "watchlist_terms")  # `watchlist_terms: []` becomes a block first
        try:
            start, end = _top_level_block(lines, "watchlist_terms")
        except KeyError:
            return lines + ["", "watchlist_terms:", f"  - {_scalar(term)}"]
        last = start
        for i in range(start + 1, end):
            if lines[i].startswith("  - "):
                last = i
        return lines[: last + 1] + [f"  - {_scalar(term)}"] + lines[last + 1 :]

    bak = apply_edit(Path(settings.config_dir) / "radar.yaml", transform, _validate_radar, settings.cache_dir)
    return term, bak


def remove_term(settings: Settings, term: str) -> Tuple[str, Optional[Path]]:
    t = (term or "").strip().lower()

    def transform(lines: List[str]) -> List[str]:
        try:
            start, end = _top_level_block(lines, "watchlist_terms")
        except KeyError:
            raise TrackError("radar.yaml has no watchlist_terms") from None
        for i in range(start + 1, end):
            if lines[i].startswith("  - "):
                try:
                    val = yaml.safe_load(lines[i])
                except yaml.YAMLError:
                    continue
                if isinstance(val, list) and val and str(val[0]).lower() == t:
                    return lines[:i] + lines[i + 1 :]
        raise TrackError(f"watchlist term {term!r} not found")

    bak = apply_edit(Path(settings.config_dir) / "radar.yaml", transform, _validate_radar, settings.cache_dir)
    return term, bak


# ----------------------------------------------------------------------------- radar.yaml: priorities
def normalize_kind(kind: str) -> str:
    k = (kind or "").strip().lower().replace("-", "_")
    if k not in PRIORITY_KINDS:
        raise TrackError(f"kind must be one of {', '.join(PRIORITY_KINDS)} (instrument-category is accepted too)")
    return k


def load_priorities(radar_cfg: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """{kind: {lower-cased key: weight}} from a radar.yaml mapping; empty when nothing is configured."""
    out: Dict[str, Dict[str, float]] = {}
    pri = (radar_cfg or {}).get("priorities") or {}
    if not isinstance(pri, dict):
        return out
    for kind, table in pri.items():
        k = str(kind).lower().replace("-", "_")
        if not isinstance(table, dict):
            continue
        for key, w in table.items():
            try:
                out.setdefault(k, {})[str(key).lower()] = float(w)
            except (TypeError, ValueError):
                continue
    return out


_PRIORITIES_HEADER = [
    "",
    "# Coverage priorities written by `litradar prioritize`: multipliers applied to the heuristic score,",
    "# the full-text fetch order and the LLM shortlist order (see docs/track.md). 1.0 = neutral.",
    "priorities:",
]


def set_priority(settings: Settings, kind: str, key: str, weight: Optional[float]) -> Tuple[str, str, Optional[Path]]:
    """Write `priorities.<kind>.<key>: weight` in radar.yaml; weight None removes the line."""
    kind = normalize_kind(kind)
    key = (key or "").strip()
    if not key:
        raise TrackError("a target is required")
    if weight is not None and not (PRIORITY_MIN <= float(weight) <= PRIORITY_MAX):
        raise TrackError(f"weight must be between {PRIORITY_MIN} and {PRIORITY_MAX}")
    rendered_key = _scalar(key)
    value = f"{float(weight):g}" if weight is not None else ""

    def transform(lines: List[str]) -> List[str]:
        # the shipped radar.yaml carries `priorities: {}`: rewrite that line into block form and edit it in
        # place, never append a second top-level `priorities:` key (PyYAML would keep only the last one)
        out = expand_flow_value(list(lines), "priorities")
        try:
            start, end = _top_level_block(out, "priorities")
        except KeyError:
            if weight is None:
                raise TrackError("no priorities configured") from None
            return out + _PRIORITIES_HEADER + [f"  {kind}:", f"    {rendered_key}: {value}"]
        kind_idx = next((i for i in range(start + 1, end) if re.match(rf"^  {kind}:\s*(#.*)?$", out[i])), None)
        if kind_idx is None:
            if weight is None:
                raise TrackError(f"no {kind} priorities configured")
            pos = _block_insert_point(out, start + 1, end)
            return out[:pos] + [f"  {kind}:", f"    {rendered_key}: {value}"] + out[pos:]
        kend = end
        for j in range(kind_idx + 1, end):
            if out[j].strip() and re.match(r"^  \S", out[j]):
                kend = j
                break
        for i in range(kind_idx + 1, kend):
            if not out[i].startswith("    ") or out[i].lstrip().startswith("#"):
                continue
            try:
                item = yaml.safe_load(out[i])
            except yaml.YAMLError:
                continue
            if isinstance(item, dict) and len(item) == 1 and str(next(iter(item))).lower() == key.lower():
                if weight is None:
                    del out[i]
                    rest = [ln for ln in out[kind_idx + 1 : kend - 1] if ln.startswith("    ") and not ln.lstrip().startswith("#")]
                    if not rest:  # drop the now-empty kind header too
                        del out[kind_idx]
                    return out
                out[i] = f"    {rendered_key}: {value}"
                return out
        if weight is None:
            raise TrackError(f"no {kind} priority for {key!r}")
        pos = _block_insert_point(out, kind_idx + 1, kend)
        return out[:pos] + [f"    {rendered_key}: {value}"] + out[pos:]

    bak = apply_edit(Path(settings.config_dir) / "radar.yaml", transform, _validate_radar, settings.cache_dir)
    return kind, key, bak


def _get(obj: Any, key: str, default: Any = "") -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _best(table: Optional[Dict[str, float]], candidates: Iterable[Any], prefix: bool = False) -> float:
    """Largest configured weight among the candidates (1.0 when nothing matches)."""
    if not table:
        return 1.0
    hits = []
    for c in candidates:
        if c is None or c == "":
            continue
        c = str(c).lower()
        for key, w in table.items():
            if c == key or (prefix and (c.startswith(key + ".") or c.startswith(key + ":"))):
                hits.append(w)
    return max(hits) if hits else 1.0


_DEFAULT_PRIORITIES_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "value": {}}


def _default_priorities() -> Dict[str, Dict[str, float]]:
    """Priorities from the default radar.yaml, re-read when the file changes (used when none are passed)."""
    path = Settings.from_env().config_dir / "radar.yaml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    c = _DEFAULT_PRIORITIES_CACHE
    if c["path"] != path or c["mtime"] != mtime:
        c.update(path=path, mtime=mtime, value=load_priorities(load_yaml(path)))
    return c["value"]


def priority_multiplier(
    paper_row: Any,
    mentions: Sequence[Any],
    journal: str,
    subgroups: Sequence[str],
    priorities: Optional[Dict[str, Any]] = None,
) -> float:
    """Multiplier (default 1.0) for one paper from the `priorities:` section of radar.yaml.

    `paper_row` is a Paper, a `papers` row dict or any object with `source` (and optionally `journal`,
    `extra`/`extra_json` carrying a `sources` list); `mentions` are Mention objects or `mentions` rows;
    `subgroups` are the subgroup ids assigned to the paper. Within one kind the best matching weight
    wins (two instruments from the same prioritised category do not compound); kinds multiply.
    Pass `priorities=settings.radar.get("priorities")` from the pipeline; without it the default
    radar.yaml is read. Hook: `h = min(10.0, h * priority_multiplier(...))` after heuristics.score_paper().
    """
    pri = load_priorities({"priorities": priorities}) if priorities is not None else _default_priorities()
    if not pri:
        return 1.0
    mult = _best(pri.get("subgroup"), subgroups or [])
    company_keys: List[str] = []
    categories: List[str] = []
    for m in mentions or []:
        kind = _get(m, "kind")
        if kind == "company":
            company_keys += [_get(m, "entity_id"), _get(m, "ticker"), _get(m, "entity_name")]
        elif kind == "instrument":
            company_keys += [_get(m, "ticker"), _get(m, "vendor")]
            categories.append(_get(m, "category"))
    mult *= _best(pri.get("company"), company_keys)
    mult *= _best(pri.get("instrument_category"), categories, prefix=True)
    mult *= _best(pri.get("journal"), [journal or _get(paper_row, "journal")])
    sources = [_get(paper_row, "source")]
    extra = _get(paper_row, "extra", None)
    if extra is None:
        raw = _get(paper_row, "extra_json", None)
        if raw:
            try:
                extra = json.loads(raw)
            except ValueError:
                extra = None
    if isinstance(extra, dict):
        sources += list(extra.get("sources") or [])
    mult *= _best(pri.get("source"), sources, prefix=True)
    return round(mult, 4)


# ----------------------------------------------------------------------------- SEC ticker lookup
_CORP_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc", "lp", "nv",
    "sa", "ag", "se", "holdings", "holding", "the",
}


def _norm_company(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"/[a-z ]+/", " ", s)  # SEC titles carry state markers like "/DE/"
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(t for t in s.split() if t not in _CORP_SUFFIXES)


def sec_company_tickers(http: Any, cache_dir: Path, max_age_days: int = SEC_CACHE_MAX_AGE_DAYS) -> List[Dict[str, str]]:
    """The SEC's company_tickers.json as [{cik, ticker, title}], cached in `cache_dir` for `max_age_days`.

    The SEC answers 403 unless the User-Agent carries a contact (litradar.http.Http adds `mailto:<e-mail>`
    when LITRADAR_CONTACT_EMAIL is set), so this refuses to go online without one."""
    cache = Path(cache_dir) / "sec_company_tickers.json"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < max_age_days * 86400:
        try:
            rows = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(rows, list) and rows:
                return rows
        except ValueError:
            pass
    contact = getattr(http, "contact_email", "")
    if not contact:
        raise TrackError("the SEC requires a contact e-mail in the User-Agent: set LITRADAR_CONTACT_EMAIL or pass --ticker")
    # The SEC's documented format is "<name> <e-mail>"; its CDN answers 403 to User-Agents that carry a URL
    # (verified live 2026-10-17: the client's default "lit-radar/0.1 (+https://...); mailto:x" is refused).
    headers = {"User-Agent": SEC_USER_AGENT.format(email=contact), "Accept": "application/json"}
    resp = http.get(SEC_TICKERS_URL, headers=headers, ok_statuses=(200, 403))
    data = None
    if resp is not None and resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = None
    if not isinstance(data, dict) or not data:
        status = resp.status_code if resp is not None else "no response"
        raise TrackError(f"could not download {SEC_TICKERS_URL} ({status}); pass --ticker to skip the lookup")
    rows = []
    for v in data.values():
        if isinstance(v, dict) and v.get("ticker") and v.get("title"):
            rows.append({"cik": str(v.get("cik_str", "")), "ticker": str(v["ticker"]), "title": str(v["title"])})
    if not rows:
        raise TrackError("SEC company_tickers.json had an unexpected shape")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def match_ticker(
    name: str, rows: Sequence[Dict[str, str]], min_confidence: float = TICKER_MIN_CONFIDENCE
) -> Tuple[Optional[Dict[str, str]], float, List[Tuple[float, Dict[str, str]]]]:
    """Fuzzy match a company name to the SEC list: (accepted row or None, confidence, top candidates)."""
    q = _norm_company(name)
    scored: List[Tuple[float, Dict[str, str]]] = []
    if not q:
        return None, 0.0, scored
    qtoks = q.split()
    for r in rows:
        t = _norm_company(r.get("title", ""))
        if not t:
            continue
        if t == q:
            s = 1.0
        else:
            s = difflib.SequenceMatcher(None, q, t).ratio()
            ttoks = t.split()
            if len(qtoks) >= 2 and t.startswith(q + " "):
                s = max(s, 0.9)
            elif len(qtoks) == 1 and ttoks[0] == q and len(ttoks) <= 2:
                s = max(s, 0.86)
            if qtoks[0] not in ttoks:
                # the distinctive first word differs ("Azenta Life Sciences" vs "ENDRA Life Sciences"): show it, never write it
                s = min(s, min_confidence - 0.01)
        if s >= TICKER_CANDIDATE_FLOOR:
            scored.append((round(s, 3), r))
    scored.sort(key=lambda x: (-x[0], len(x[1]["title"]), len(x[1]["ticker"]), x[1]["ticker"]))
    top = scored[:5]
    if not top:
        return None, 0.0, top
    best_score, best = top[0]
    return (best if best_score >= min_confidence else None), best_score, top


# ----------------------------------------------------------------------------- author lookups
def openalex_author_candidates(http: Any, name: str, api_key: str = "", mailto: str = "", limit: int = 5) -> List[Dict[str, Any]]:
    """Top OpenAlex author profiles for a name: [{id, display_name, works_count, cited_by_count, institution, orcid}]."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params: Dict[str, Any] = {"search": name, "per-page": limit}
    if mailto:
        params["mailto"] = mailto
    resp = http.get(OPENALEX_AUTHORS_URL, params=params, headers=headers, ok_statuses=(200, 403, 429))
    if resp is None or resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    out = []
    for r in (data or {}).get("results") or []:
        inst = ""
        insts = r.get("last_known_institutions") or ([r["last_known_institution"]] if r.get("last_known_institution") else [])
        if insts and isinstance(insts[0], dict):
            inst = str(insts[0].get("display_name") or "")
        out.append({
            "id": str(r.get("id") or "").rsplit("/", 1)[-1],
            "display_name": str(r.get("display_name") or ""),
            "works_count": int(r.get("works_count") or 0),
            "cited_by_count": int(r.get("cited_by_count") or 0),
            "institution": inst,
            "orcid": str(r.get("orcid") or "").rsplit("/", 1)[-1],
        })
    return out


def orcid_candidates(http: Any, name: str, limit: int = 5) -> List[Dict[str, Any]]:
    """ORCID public expanded search: [{orcid, given_names, family_names, institutions, exact}]."""
    parts = (name or "").split()
    q = f'given-names:"{parts[0]}" AND family-name:"{parts[-1]}"' if len(parts) >= 2 else name
    resp = http.get(ORCID_SEARCH_URL, params={"q": q, "rows": limit}, headers={"Accept": "application/json"})
    if resp is None or resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    out = []
    for r in (data or {}).get("expanded-result") or []:
        given, family = str(r.get("given-names") or ""), str(r.get("family-names") or "")
        exact = bool(parts) and given.lower().split()[:1] == [parts[0].lower()] and family.lower() == parts[-1].lower()
        out.append({
            "orcid": str(r.get("orcid-id") or ""),
            "given_names": given,
            "family_names": family,
            "institutions": [str(x) for x in (r.get("institution-name") or [])],
            "exact": exact,
        })
    return out


# ----------------------------------------------------------------------------- what is tracked
def tracked_summary(settings: Settings) -> Dict[str, Any]:
    """Everything the index tracks, for `litradar track list`."""
    companies = _company_entries(settings)
    instruments = list((settings.yaml("instruments") or {}).get("entries") or [])
    groups = _subgroups(settings)
    radar = _radar(settings)
    return {
        "companies": [
            {"id": e.get("id"), "name": e.get("name"), "ticker": e.get("ticker"), "type": e.get("type"),
             "watch_news": bool(e.get("watch_news")), "aliases": len(e.get("aliases") or [])}
            for e in companies
        ],
        "instruments": [
            {"id": e.get("id"), "name": e.get("name"), "vendor": e.get("vendor"), "ticker": e.get("ticker"),
             "category": e.get("category"), "status": e.get("status", "current")}
            for e in instruments
        ],
        "authors": [
            {"subgroup": g.get("id"), "name": a.get("name"), "openalex_id": a.get("openalex_id") or "",
             "orcid": a.get("orcid") or "", "institution": a.get("institution") or ""}
            for g in groups for a in (g.get("tracked_authors") or []) if isinstance(a, dict)
        ],
        "topics": [
            {"id": g.get("id"), "name": g.get("name"), "openalex_topics": list(g.get("openalex_topics") or []),
             "keywords": list(g.get("keywords") or []), "tracked_authors": len(g.get("tracked_authors") or [])}
            for g in groups
        ],
        "terms": [str(t) for t in (radar.get("watchlist_terms") or [])],
        "priorities": load_priorities(radar),
    }
