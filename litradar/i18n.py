"""Language + country layer: which language a paper is written in, which countries its authors sit in,
and (optionally) English translations of non-English titles and abstracts.

The corpus is global (Chinese, Japanese, Korean, Russian and Latin-script research alike), so every
stored paper gets two cheap, offline tags written into ``papers.extra_json``:

* ``lang``      - ISO 639-1 code: en, zh, ja, ko, ru, es, pt, fr, de, or ``other``. Pure stdlib:
                  Unicode script ranges (CJK / kana / Hangul / Cyrillic / Latin) decide the family,
                  weighted stopword counts separate the Latin-script languages (Italian and Dutch
                  stopwords are recognised so they land in ``other`` instead of es/fr/de).
* ``countries`` - ISO 3166-1 alpha-2 codes, most frequent first. Taken from OpenAlex institution
                  country codes when the record carries them, else from affiliation strings via the
                  bundled lookup in ``litradar/i18n_data.py`` (country names in several languages,
                  first-level regions incl. every Chinese province, ~900 research cities, ~1,000
                  institution names incl. the big Chinese ones, case-sensitive acronyms, CJK/Cyrillic
                  spellings, trailing alpha-3 codes, and US state codes as a last resort).

Dashboards and trend queries read those two keys; ``histograms()`` is the ready-made aggregate.

External APIs (verified live 2026-10-17)
----------------------------------------
OpenAlex works - https://docs.openalex.org/api-entities/works/work-object/authorship-object
  GET https://api.openalex.org/works/W2741809807?select=id,authorships  -> HTTP 200. Each authorship has
  ``institutions[]`` with ``{"id", "display_name", "ror", "country_code": "CA", "type": "nonprofit", "lineage"}``
  and a convenience ``"countries": ["CA"]`` list, plus ``raw_affiliation_strings``. The source adapter
  (``litradar/sources/openalex.py``) currently keeps only name/type/ror per institution; see
  ``docs/i18n.md`` for the one-line wiring that preserves ``country_code``. ``country_of`` already
  reads it from ``authors_json[].institutions[].country_code`` and from ``extra_json.institution_countries``.

Anthropic Message Batches - https://platform.claude.com/docs/en/build-with-claude/batch-processing
  POST https://api.anthropic.com/v1/messages/batches  body ``{"requests": [{"custom_id", "params"}]}``
  (custom_id must match ``^[a-zA-Z0-9_-]{1,64}$``; <= 100,000 requests or 256 MB per batch).
  GET  https://api.anthropic.com/v1/messages/batches/{id}  -> ``processing_status``: in_progress | canceling | ended,
  ``request_counts`` {processing, succeeded, errored, canceled, expired}, ``results_url``.
  Results are a .jsonl stream of ``{"custom_id", "result": {"type": succeeded|errored|canceled|expired, "message"}}``.
  Structured output - https://platform.claude.com/docs/en/build-with-claude/structured-outputs
  ``params.output_config = {"format": {"type": "json_schema", "schema": {...}}, "effort": "low"}`` (objects
  need ``additionalProperties: false``; the JSON comes back in the first ``text`` block; check ``stop_reason``
  for ``refusal`` / ``max_tokens`` before parsing). ``effort`` lives inside ``output_config`` and controls how
  much the model thinks; thinking tokens count against ``max_tokens`` on current models, hence
  ``max_tokens=16000`` and ``effort="low"`` for a translation job. SDK calls used, same as
  ``litradar/rank/llm.py``:
  ``client.messages.batches.create(requests=[Request(custom_id, params=MessageCreateParamsNonStreaming(...))])``,
  ``client.messages.batches.retrieve(id)``, ``client.messages.batches.results(id)``. Import paths and the
  ``output_config`` field were verified against anthropic 0.125.0; no credentials were available in the
  build environment, so the live batch round-trip was NOT exercised here - the control flow is covered by
  offline tests with a fake client (success, refusal, truncation, API error, exception, deadline).
  First real run: ``litradar tag --since 30d --translate`` with ANTHROPIC_API_KEY set, then inspect
  ``SELECT * FROM translations LIMIT 5``.

The ``anthropic`` package is optional (``pip install 'lit-radar[llm]'``); it is imported inside
``translate_titles`` only, and only when an API key is present.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import i18n_data as data
from .models import Paper
from .store import Store

log = logging.getLogger(__name__)

LANGS: Tuple[str, ...] = ("en", "zh", "ja", "ko", "ru", "es", "pt", "fr", "de", "other")

TRANSLATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
  paper_id TEXT PRIMARY KEY,
  lang TEXT,
  title_en TEXT,
  abstract_en TEXT,
  model TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_translations_lang ON translations(lang);
"""


def ensure_schema(store: Store) -> None:
    """Additive: create the translations table if it does not exist yet."""
    store.conn.executescript(TRANSLATIONS_SCHEMA)
    store.conn.commit()


# =============================================================================================
# (a) language detection
# =============================================================================================
_CJK = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF), (0x20000, 0x2A6DF))
_KANA = ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x31F0, 0x31FF), (0xFF66, 0xFF9F))
_HANGUL = ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F), (0xA960, 0xA97F), (0xD7B0, 0xD7FF))
_CYRILLIC = ((0x0400, 0x04FF), (0x0500, 0x052F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F))
_LATIN = ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0xD6), (0xD8, 0xF6), (0xF8, 0x24F), (0x1E00, 0x1EFF))
# Japanese-only character forms: post-war shinjitai simplifications whose Simplified and Traditional Chinese
# counterparts are different code points (発 vs 发/發, 経 vs 经/經, 対 vs 对/對, 図 vs 图/圖 ...) plus kokuji
# (characters coined in Japan: 働 込 枠 畑 峠 辻). One of them in a kana-free CJK string is enough to call it
# Japanese; a kanji-only title with none of them (e.g. 肺癌患者) stays ``zh``.
_SHINJITAI = frozenset(
    "発経対図変気単価択検験剤伝壊応従続読総円栄営労駅薬挙歩戦悪絵緑録鉄塩売実関観権顕繊沢訳釈駆齢脳蔵臓滝覧覚増恵渋帰両辺鉱砕粋歓勧剰譲醸壌嚢稲穂縄聴毎黒隣雑歯鶏児効働込枠畑峠匂辻"
)


def _in(cp: int, ranges: Sequence[Tuple[int, int]]) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def script_counts(text: str) -> Dict[str, int]:
    """Count letters per script family. One CJK ideograph carries roughly a word of meaning, so callers
    weight ``cjk`` x3 against ``latin`` when deciding which script dominates a mixed string."""
    c = Counter()
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        if _in(cp, _LATIN):
            c["latin"] += 1
        elif _in(cp, _CJK):
            c["cjk"] += 1
        elif _in(cp, _KANA):
            c["kana"] += 1
        elif _in(cp, _HANGUL):
            c["hangul"] += 1
        elif _in(cp, _CYRILLIC):
            c["cyrillic"] += 1
        else:
            c["other"] += 1
    return dict(c)


_STOPWORDS: Dict[str, frozenset] = {
    "en": frozenset("the of and in for with to a is are by on from as that this an at be we using via between during among into their its which or not after than through across under without against towards these those were was has have been can may study results".split()),
    "es": frozenset("el la los las de del y en un una para con por es son se su sus al como más entre sobre desde hasta pero también este esta estos estas lo que o sin mediante estudio resultados análisis evaluación pacientes".split()),
    "pt": frozenset("o a os as de do da dos das e em um uma para com por é são se seu sua ao à como mais entre sobre não no na nos nas pelo pela ou que também este esta estudo resultados análise avaliação pacientes".split()),
    "fr": frozenset("le la les de des du et en un une pour avec par est sont se son sa ses au aux comme plus entre sur dans chez pas ne ce cette ces qui que ou où à étude résultats analyse évaluation patients chez".split()),
    "de": frozenset("der die das und in im ein eine einer eines für mit von vom zu zur zum ist sind sich sein seine als auch auf aus bei nach über unter zwischen durch nicht oder wie bis um an am dem den des mittels studie ergebnisse analyse untersuchung patienten".split()),
    # recognised only so that Italian / Dutch text lands in "other" instead of es / fr / de.
    # Tokens that also occur in English scientific prose (over, van, die, per, non, is, of, ...) are
    # deliberately left out of these two sets - they cost more in false positives than they earn.
    "it": frozenset("della delle degli nella nelle sono una questo questa anche come più dell nell dall sull risultati analisi valutazione pazienti".split()),
    "nl": frozenset("het een voor zijn niet deze wordt worden werd tussen onderzoek resultaten patiënten".split()),
}
# Latin and loan phrases that are ordinary English scientific usage but whose tokens belong to other
# languages' stopword sets ("van der Waals", "de novo", "in vivo"). Removed before scoring.
_LOAN_RE = re.compile(
    r"\b(?:van der waals|de novo|in vivo|ex vivo|in vitro|in situ|in silico|in utero|in toto|a priori|a posteriori|"
    r"per se|et al|vice versa|ad libitum|status quo|de facto|in press|post hoc|in vivo)\b"
)
MIN_SCORE = 4.0      # absolute stopword evidence a non-English guess needs
MIN_MARGIN = 2.0     # how far ahead of the runner-up a non-English guess must be
MIN_MARGIN_EN = 3.0  # ... and ahead of English, which is the prior for scientific text
_WORD_LANGS: Dict[str, int] = Counter(w for s in _STOPWORDS.values() for w in s)
_LATIN_ORDER = ("en", "es", "pt", "fr", "de", "it", "nl")
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_ELISION_FR = re.compile(r"\b[ldjncsmt]['’](?=[a-zà-ÿ])", re.IGNORECASE)
_ELISION_IT = re.compile(r"\b(?:dell|nell|dall|sull|all|quest|un)['’](?=[a-zà-ÿ])", re.IGNORECASE)


def detect_language(text: Optional[str]) -> str:
    """Cheap stdlib language guess -> en | zh | ja | ko | ru | es | pt | fr | de | other.

    Script ranges decide the family (CJK ideographs weighted x3 against Latin letters because one
    ideograph is roughly one word); kana => ja, Hangul-dominant => ko, a Japanese-only character form
    (``_SHINJITAI``) in kana-free CJK => ja, else zh; Cyrillic => ru. Latin-script text is split by
    weighted stopword counts (a word unique to one language counts 2, a shared one 1) plus small
    diacritic and elision bonuses. English is the prior for scientific text, so another language wins
    only with real evidence: at least ``MIN_SCORE``, ``MIN_MARGIN`` ahead of the next non-English
    candidate and ``MIN_MARGIN_EN`` ahead of English. A short English title therefore stays ``en``
    rather than being flipped by one loanword.

    Documented limits (see tests/test_i18n.py LANG_CASES for the probes): Latin-script languages with
    no stopword set here (Polish, Turkish, Indonesian, Vietnamese, ...) fall back to ``en``, not
    ``other``; Italian and Dutch are recognised only to land in ``other``; Serbian/Macedonian in
    Cyrillic report as ``ru``; Simplified and Traditional Chinese are both ``zh``; a kanji-only
    Japanese title without any shinjitai form is ``zh``; non-Latin/non-CJK/non-Cyrillic scripts
    (Greek, Arabic, Devanagari, Thai ...) => ``other``; empty / non-alphabetic input => ``other``.
    """
    if not text:
        return "other"
    text = text[:4000]
    sc = script_counts(text)
    latin, cjk, kana, hangul, cyr, other = (sc.get(k, 0) for k in ("latin", "cjk", "kana", "hangul", "cyrillic", "other"))
    if latin + cjk + kana + hangul + cyr + other == 0:
        return "other"
    east = 3 * (cjk + kana + hangul)
    if east > 0 and east >= latin and east >= 3 * cyr and east >= 3 * other:
        if hangul > 0 and hangul >= kana and 2 * hangul >= cjk:
            return "ko"
        if kana > 0 or any(ch in _SHINJITAI for ch in text):
            return "ja"
        return "zh"
    if cyr > 0 and cyr >= latin and cyr >= other:
        return "ru"
    if other > latin and other > cyr:
        return "other"
    # Latin script: weighted stopwords
    lowered = _LOAN_RE.sub(" ", text.lower().replace("’", "'"))
    tokens = _TOKEN_RE.findall(lowered)
    score: Dict[str, float] = dict.fromkeys(_LATIN_ORDER, 0.0)
    for tok in tokens:
        n = _WORD_LANGS.get(tok)
        if not n:
            continue
        w = 2.0 if n == 1 else 1.0
        for lang, words in _STOPWORDS.items():
            if tok in words:
                score[lang] += w
    score["fr"] += 1.5 * len(_ELISION_FR.findall(lowered))
    score["it"] += 2.0 * len(_ELISION_IT.findall(lowered))
    if "ñ" in lowered:
        score["es"] += 2
    if "ã" in lowered or "õ" in lowered:
        score["pt"] += 2
    if "ß" in lowered:
        score["de"] += 2
    if "ü" in lowered or "ö" in lowered or "ä" in lowered:
        score["de"] += 1
    if "œ" in lowered:
        score["fr"] += 2
    if "è" in lowered:
        score["fr"] += 0.5
        score["it"] += 0.5
    ranked = sorted(((v, -_LATIN_ORDER.index(k), k) for k, v in score.items()), reverse=True)
    best_score, _, best = ranked[0]
    if best == "en":
        return "en"
    runner_up = next((v for v, _, k in ranked[1:] if k != "en"), 0.0)
    if best_score < MIN_SCORE or best_score < runner_up + MIN_MARGIN or best_score < score["en"] + MIN_MARGIN_EN:
        return "en"  # not enough evidence to overrule the English prior
    return best if best in LANGS else "other"


# =============================================================================================
# (b) country assignment
# =============================================================================================
_SPECIAL = str.maketrans({"Ł": "L", "ł": "l", "Ø": "O", "ø": "o", "Đ": "D", "đ": "d", "ß": "ss", "Æ": "AE", "æ": "ae",
                          "Œ": "OE", "œ": "oe", "Þ": "Th", "þ": "th", "ð": "d", "ı": "i", "’": "'", "`": "'"})


def fold(s: str) -> str:
    """Accent-fold to ASCII, keep case, normalise hyphens/whitespace ("Tübingen" -> "Tubingen")."""
    s = unicodedata.normalize("NFKD", (s or "").translate(_SPECIAL)).encode("ascii", "ignore").decode()
    s = s.replace("-", " ").replace("–", " ").replace("—", " ")
    return re.sub(r"\s+", " ", s).strip()


def _build_phrase_regex(
    tables: Iterable[Dict[str, Optional[str]]],
) -> Tuple[Any, Dict[str, Optional[str]], List[Tuple[str, Optional[str], Optional[str]]]]:
    """Merge the lookup tables into one longest-match regex.

    Keys are accent-folded, so two spellings can collapse onto one key ("san josé" / "san jose"). The first
    definition wins (countries > regions > cities > institutions); a later definition with a *different*
    code is dead data and is reported in the third return value so a test can fail on it.
    """
    merged: Dict[str, Optional[str]] = {}
    collisions: List[Tuple[str, Optional[str], Optional[str]]] = []
    for table in tables:
        for phrase, code in table.items():
            key = fold(phrase).lower()
            if not key:
                continue
            if key in merged:
                if merged[key] != code:
                    collisions.append((key, merged[key], code))
                continue
            merged[key] = code
    ordered = sorted(merged, key=len, reverse=True)
    pattern = "(?<![a-z0-9])(?:" + "|".join(re.escape(p) for p in ordered) + ")(?![a-z0-9])"
    return re.compile(pattern), merged, collisions


_PHRASE_RE, _PHRASES, PHRASE_COLLISIONS = _build_phrase_regex((data.COUNTRY_NAMES, data.REGIONS, data.CITIES, data.INSTITUTIONS))
if PHRASE_COLLISIONS:  # pragma: no cover - data bug; tests/test_i18n.py asserts the list is empty
    log.warning("i18n gazetteer: %d phrase(s) fold onto an earlier entry with a different code (first wins): %s", len(PHRASE_COLLISIONS), PHRASE_COLLISIONS)
_ACRONYMS = {fold(k): v for k, v in data.ACRONYMS.items()}
_ACRONYM_RE = re.compile("(?<![A-Za-z0-9])(?:" + "|".join(re.escape(a) for a in sorted(_ACRONYMS, key=len, reverse=True)) + r")(?![A-Za-z0-9])")
_RAW_TERMS = {k.casefold(): v for k, v in data.RAW_TERMS.items()}
_RAW_ORDER = sorted(_RAW_TERMS, key=len, reverse=True)
_ALPHA3_RE = re.compile(r",\s*([A-Z]{3})\.?\s*$")
_STATE_ZIP_RE = re.compile(r"(?<![A-Za-z])([A-Z]{2})\s+\d{5}(?:-\d{4})?(?![0-9])")
_TRAILING_STATE_RE = re.compile(r",\s*([A-Z]{2})\.?\s*$")


def countries_in(affiliation: str) -> List[str]:
    """ISO alpha-2 codes found in one affiliation string (deduplicated, in order of appearance)."""
    if not affiliation or not affiliation.strip():
        return []
    found: List[str] = []
    folded = fold(affiliation)
    for m in _PHRASE_RE.finditer(folded.lower()):
        code = _PHRASES.get(m.group(0))
        if code and code not in found:
            found.append(code)
    for m in _ACRONYM_RE.finditer(folded):
        code = _ACRONYMS.get(m.group(0))
        if code and code not in found:
            found.append(code)
    raw = affiliation.casefold()
    if any(ord(ch) > 0x24F for ch in raw):  # only bother when non-Latin characters are present
        consumed = raw
        for term in _RAW_ORDER:
            if term in consumed:
                code = _RAW_TERMS[term]
                consumed = consumed.replace(term, " ")
                if code and code not in found:
                    found.append(code)
    m3 = _ALPHA3_RE.search(folded)
    if m3 and m3.group(1) in data.ALPHA3:
        code = data.ALPHA3[m3.group(1)]
        if code not in found:
            found.append(code)
    mz = _STATE_ZIP_RE.search(folded)
    if mz and mz.group(1) in data.US_STATE_CODES and "US" not in found:
        found.append("US")
    if not found:
        mt = _TRAILING_STATE_RE.search(folded)
        if mt and mt.group(1) in data.US_STATE_CODES:
            found.append("US")
    return found


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _fields(paper: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    """(extra, authors, affiliations) from a Paper, a store row dict, or a plain dict."""
    if isinstance(paper, Paper):
        return dict(paper.extra or {}), [a.__dict__ for a in paper.authors], list(paper.affiliations or [])
    if not isinstance(paper, dict):
        return {}, [], []
    extra = _loads(paper.get("extra_json", paper.get("extra")), {}) or {}
    authors = _loads(paper.get("authors_json", paper.get("authors")), []) or []
    affs = _loads(paper.get("affiliations_json", paper.get("affiliations")), []) or []
    return extra, [a for a in authors if isinstance(a, dict)], [a for a in affs if isinstance(a, str)]


def country_counts(paper: Any) -> Counter:
    """Country code -> number of author / affiliation records pointing at it."""
    extra, authors, affs = _fields(paper)
    counts: Counter = Counter()
    # 1. OpenAlex institution country codes (exact, when the record carries them)
    for a in authors:
        codes = {str(i.get("country_code")).upper() for i in (a.get("institutions") or []) if isinstance(i, dict) and i.get("country_code")}
        for c in codes:
            counts[c] += 1
    for c in extra.get("institution_countries") or []:
        if isinstance(c, str) and len(c) == 2:
            counts[c.upper()] += 1
    if counts:
        return counts
    # 2. affiliation strings (paper-level list + per-author strings), each distinct string once
    seen = set()
    for s in list(affs) + [a.get("affiliation") or "" for a in authors]:
        for piece in re.split(r"\s*;\s*", s or ""):
            if piece and piece not in seen:
                seen.add(piece)
                for c in countries_in(piece):
                    counts[c] += 1
    return counts


def country_of(paper: Any) -> List[str]:
    """ISO 3166-1 alpha-2 codes for a paper, most frequent first (ties alphabetical). Empty when unknown.

    Accepts a ``Paper``, a ``papers`` row from the store (JSON columns), or a plain dict with
    ``authors`` / ``affiliations`` / ``extra``.
    """
    counts = country_counts(paper)
    return [c for c, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# =============================================================================================
# (d) tagging the store
# =============================================================================================
def _window_clauses(since: Optional[str], until: Optional[str], updated_since: Optional[str] = None) -> Tuple[List[str], List[Any]]:
    """SQL predicates on ``papers p`` for a publication window plus, optionally, an ingest window.

    ``since`` / ``until`` follow the digest's rule (undated papers are matched on ``first_seen``).
    ``updated_since`` (an ISO timestamp, the format ``Store`` writes into ``updated_at``) selects every
    row an ingest touched since then, whatever its publication date - that is what a run should
    re-tag, because ``Store.upsert_paper`` rewrites ``extra_json`` and with it the tags.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if since:
        clauses.append("(p.pub_date >= ? OR (p.pub_date = '' AND substr(p.first_seen, 1, 10) >= ?))")
        params += [since, since]
    if until:
        clauses.append("(p.pub_date <= ? OR p.pub_date = '')")
        params.append(until)
    if updated_since:
        clauses.append("p.updated_at >= ?")
        params.append(updated_since)
    return clauses, params


def _window_sql(since: Optional[str], until: Optional[str], updated_since: Optional[str] = None) -> Tuple[str, List[Any]]:
    clauses, params = _window_clauses(since, until, updated_since)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _iter_chunks(store: Store, columns: str, clauses: List[str], params: List[Any], chunk_size: int) -> Iterable[List[Tuple[Any, ...]]]:
    """Yield ``papers`` rows in ``rowid`` order, ``chunk_size`` at a time, so a multi-year corpus is never
    held in memory at once and the caller can UPDATE + commit between chunks on the shared connection."""
    where = "".join(" AND " + c for c in clauses)
    last = -1
    while True:
        rows = store.conn.execute(
            f"SELECT p.rowid, {columns} FROM papers p WHERE p.rowid > ?{where} ORDER BY p.rowid LIMIT ?",
            [last, *params, chunk_size],
        ).fetchall()
        if not rows:
            return
        yield rows
        last = rows[-1][0]
        if len(rows) < chunk_size:
            return


def paper_language(title: str, abstract: str) -> str:
    """A non-Latin title decides on its own (Chinese journals often carry English abstracts);
    otherwise title + abstract together give the stopwords something to work with."""
    t = detect_language(title)
    if t in ("zh", "ja", "ko", "ru"):
        return t
    return detect_language(f"{title or ''}\n{abstract or ''}")


def tag_store(
    store: Store,
    since: Optional[str] = None,
    until: Optional[str] = None,
    updated_since: Optional[str] = None,
    chunk_size: int = 5000,
) -> Dict[str, Any]:
    """Write ``lang`` and ``countries`` into ``papers.extra_json`` for every paper in the window.

    Idempotent: tags are recomputed from title / abstract / authors / affiliations each time, never
    from the previous tags. Other keys in ``extra_json`` are preserved. Rows are processed in
    ``chunk_size`` rowid chunks with a commit after each, so memory stays flat on a large corpus.

    Tags are VOLATILE: ``Store.upsert_paper`` replaces the whole ``extra_json`` on re-ingest, so a
    paper fetched again by a later run loses them until it is tagged again. Pass
    ``updated_since=<run start timestamp>`` after each ingest to re-tag exactly the rows that run
    touched, regardless of publication date (``histograms()`` reports how many rows are untagged).
    Returns the histograms so a run can report them.
    """
    ensure_schema(store)
    clauses, params = _window_clauses(since, until, updated_since)
    langs: Counter = Counter()
    countries: Counter = Counter()
    n = tagged = with_country = 0
    for rows in _iter_chunks(store, "p.id, p.title, p.abstract, p.authors_json, p.affiliations_json, p.extra_json", clauses, params, chunk_size):
        for _rowid, pid, title, abstract, authors_json, affs_json, extra_json in rows:
            n += 1
            extra = _loads(extra_json, {}) or {}
            if not isinstance(extra, dict):
                extra = {}
            lang = paper_language(title or "", abstract or "")
            codes = country_of({"authors_json": authors_json, "affiliations_json": affs_json, "extra_json": extra})
            extra["lang"] = lang
            extra["countries"] = codes
            store.conn.execute("UPDATE papers SET extra_json=? WHERE id=?", (json.dumps(extra, ensure_ascii=False, default=str), pid))
            tagged += 1
            langs[lang] += 1
            if codes:
                with_country += 1
                for c in codes:
                    countries[c] += 1
            else:
                countries["??"] += 1
        store.conn.commit()
    store.conn.commit()
    return {
        "papers": n,
        "tagged": tagged,
        "with_country": with_country,
        "languages": dict(langs.most_common()),
        "countries": dict(countries.most_common()),
    }


def histograms(store: Store, since: Optional[str] = None, until: Optional[str] = None, chunk_size: int = 5000) -> Dict[str, Any]:
    """Language and country distributions of already-tagged papers in a window (for dashboards/trends).

    Read-only. ``countries`` counts a paper once per distinct country; ``primary_countries`` counts
    only the first. ``untagged`` is the number of papers in the window with no ``lang`` tag - after a
    re-ingest that is the count of rows whose tags were overwritten; when it is > 0 the other numbers
    under-count and ``tag_store`` should be run again.
    """
    clauses, params = _window_clauses(since, until)
    langs: Counter = Counter()
    countries: Counter = Counter()
    primary: Counter = Counter()
    n = tagged = 0
    for rows in _iter_chunks(store, "p.extra_json", clauses, params, chunk_size):
        for _rowid, extra_json in rows:
            n += 1
            extra = _loads(extra_json, {}) or {}
            if not isinstance(extra, dict) or "lang" not in extra:
                continue
            tagged += 1
            langs[extra.get("lang") or "other"] += 1
            codes = [c for c in (extra.get("countries") or []) if isinstance(c, str)]
            if codes:
                primary[codes[0]] += 1
                for c in codes:
                    countries[c] += 1
            else:
                countries["??"] += 1
                primary["??"] += 1
    return {
        "papers": n,
        "tagged": tagged,
        "untagged": n - tagged,
        "languages": dict(langs.most_common()),
        "countries": dict(countries.most_common()),
        "primary_countries": dict(primary.most_common()),
    }


# =============================================================================================
# (c) translation with the Anthropic SDK (optional)
# =============================================================================================
TRANSLATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "description": "ISO 639-1 code of the source text, e.g. zh, ja, ko, ru, es, pt, fr, de, en"},
        "title_en": {"type": "string", "description": "faithful English translation of the title"},
        "abstract_en": {"type": "string", "description": "faithful English translation of the abstract; empty string when none was given"},
    },
    "required": ["language", "title_en", "abstract_en"],
    "additionalProperties": False,
}

SYSTEM_TRANSLATE = """You translate scientific titles and abstracts into English for a literature-tracking database.
Rules:
- Translate faithfully and completely; do not summarise, shorten, embellish or add commentary.
- Keep gene, protein, drug, organism, instrument, product and organisation names exactly as written in the source (Latin-script names stay as they are; do not transliterate brand names).
- Preserve numbers, units, p-values and statistics exactly.
- Use standard English scientific terminology.
- If the text is already English, copy it unchanged and report language "en".
- Report the source language as an ISO 639-1 code.
Return only the JSON object."""


def _user_message(title: str, abstract: str, lang_hint: str, abstract_chars: int) -> str:
    return (
        f"LANGUAGE HINT (heuristic, may be wrong): {lang_hint or 'unknown'}\n\n"
        f"TITLE: {title}\n\n"
        f"ABSTRACT: {(abstract or '')[:abstract_chars] or '(none)'}"
    )


def _parse_translation(message: Any) -> Optional[Dict[str, Any]]:
    """None on refusal / truncation / non-JSON; otherwise the dict from the structured output."""
    stop = getattr(message, "stop_reason", None)
    if stop in ("refusal", "max_tokens"):
        return None
    text = next((getattr(b, "text", "") for b in (getattr(message, "content", None) or []) if getattr(b, "type", "") == "text"), "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not parsed.get("title_en"):
        return None
    return parsed


def _typed_request(custom_id: str, params: Dict[str, Any]) -> Any:
    """Use the SDK's TypedDict constructors when the SDK is installed (they are plain dicts at runtime)."""
    try:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request
    except ImportError:  # fake client in tests, or SDK absent
        return {"custom_id": custom_id, "params": params}
    return Request(custom_id=custom_id, params=MessageCreateParamsNonStreaming(**params))


def translation_candidates(store: Store, since: Optional[str] = None, until: Optional[str] = None, max_items: int = 1000) -> List[Dict[str, Any]]:
    """Non-English papers in the window that have no translation yet (tags computed on the fly if missing)."""
    ensure_schema(store)
    where, params = _window_sql(since, until)
    sql = (
        "SELECT p.id, p.title, p.abstract, p.extra_json FROM papers p LEFT JOIN translations t ON t.paper_id = p.id"
        + (where + " AND " if where else " WHERE ")
        + "t.paper_id IS NULL ORDER BY p.pub_date DESC, p.id"
    )
    out: List[Dict[str, Any]] = []
    for pid, title, abstract, extra_json in store.conn.execute(sql, params):
        if not (title or "").strip():
            continue
        extra = _loads(extra_json, {}) or {}
        lang = extra.get("lang") if isinstance(extra, dict) else None
        if not lang:
            lang = paper_language(title or "", abstract or "")
        if lang == "en":
            continue
        out.append({"id": pid, "title": title or "", "abstract": abstract or "", "lang": lang})
        if len(out) >= max_items:
            break
    return out


def save_translation(store: Store, paper_id: str, lang: str, title_en: str, abstract_en: str, model: str) -> None:
    """Insert or replace one row of ``translations`` (not committed; callers commit). Safe on a fresh store."""
    ensure_schema(store)
    store.conn.execute(
        "INSERT OR REPLACE INTO translations (paper_id, lang, title_en, abstract_en, model, created_at) VALUES (?,?,?,?,?,?)",
        (paper_id, lang, title_en, abstract_en, model, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
    )


def translate_titles(
    store: Store,
    since: Optional[str] = None,
    until: Optional[str] = None,
    model: str = "claude-opus-5-5",
    api_key: str = "",
    client: Any = None,
    use_batch: bool = True,
    poll_seconds: float = 30.0,
    max_wait_seconds: float = 3600.0,
    max_items: int = 1000,
    abstract_chars: int = 6000,
    max_tokens: int = 16000,
    effort: Optional[str] = "low",
) -> Dict[str, Any]:
    """Translate non-English titles + abstracts into English with Claude and store them in ``translations``.

    Runs ONLY when an API key is available (``api_key`` argument or ``ANTHROPIC_API_KEY``) or a
    ``client`` is injected; otherwise returns ``{"status": "skipped"}`` without importing the SDK.
    All candidates go into one Message Batches request (50% price, async, polled on a wall-clock
    deadline of ``max_wait_seconds``); ``use_batch=False`` translates synchronously for a handful of
    papers.

    ``max_tokens`` is generous (16,000) because on current models thinking tokens count against it
    and a long abstract plus reasoning can otherwise stop with ``max_tokens``; ``effort="low"`` keeps
    that reasoning short (translation does not need it). Pass ``effort=None`` for a model that does
    not accept ``output_config.effort``.

    Never raises for API, network or model failures. Check ``report["status"]``:
    ``ok`` | ``skipped`` (no key) | ``incomplete`` (batch not ``ended`` before the deadline; nothing
    stored) | ``error`` (SDK/API exception: ``report["error"]`` has ``ExceptionType: message``;
    whatever was saved before it is kept). Per-paper outcomes are counted in ``translated``,
    ``refused_or_invalid`` (refusal or non-JSON), ``truncated`` (``stop_reason == "max_tokens"``) and
    ``errored`` (``result.type`` errored/canceled/expired). The one thing that does raise is a
    configuration error: an API key is set but the optional ``anthropic`` package is not installed
    (``RuntimeError`` with the pip hint), so a misconfigured deployment is loud rather than silent.
    """
    ensure_schema(store)
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    report: Dict[str, Any] = {
        "status": "ok", "model": model, "candidates": 0, "submitted": 0,
        "translated": 0, "refused_or_invalid": 0, "truncated": 0, "errored": 0,
    }
    if client is None and not key:
        report.update(status="skipped", reason="ANTHROPIC_API_KEY not set")
        return report
    items = translation_candidates(store, since, until, max_items=max_items)
    report["candidates"] = len(items)
    if not items:
        return report
    if client is None:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("translation needs the Anthropic SDK: pip install 'lit-radar[llm]' (or pip install anthropic)") from exc
        try:
            client = anthropic.Anthropic(api_key=key)
        except Exception as exc:  # noqa: BLE001 - bad client configuration must not abort the caller's run
            log.warning("translation client could not be created: %s", exc)
            report.update(status="error", error=f"{type(exc).__name__}: {exc}")
            return report

    # Batches outlive the default 5-minute cache, so the batch path asks for the 1-hour TTL (the docs'
    # recommendation). Note: the rubric is far below the minimum cacheable prefix, so caching is a
    # no-op until the system prompt grows - it is kept for parity with rank/llm.py.
    cache = {"type": "ephemeral", "ttl": "1h"} if use_batch else {"type": "ephemeral"}
    system = [{"type": "text", "text": SYSTEM_TRANSLATE, "cache_control": cache}]
    output_config: Dict[str, Any] = {"format": {"type": "json_schema", "schema": TRANSLATION_SCHEMA}}
    if effort:
        output_config["effort"] = effort

    def params_for(it: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": _user_message(it["title"], it["abstract"], it["lang"], abstract_chars)}],
            "output_config": output_config,
        }

    def record(it: Dict[str, Any], message: Any) -> None:
        if getattr(message, "stop_reason", None) == "max_tokens":
            report["truncated"] += 1  # visible separately: a paper that truncates every run needs a bigger max_tokens
            return
        parsed = _parse_translation(message)
        if parsed is None:
            report["refused_or_invalid"] += 1
            return
        lang = str(parsed.get("language") or "").strip().lower()
        if not re.fullmatch(r"[a-z]{2}", lang):
            lang = it["lang"]
        save_translation(store, it["id"], lang, str(parsed.get("title_en") or ""), str(parsed.get("abstract_en") or ""), model)
        report["translated"] += 1

    if not use_batch:
        for it in items:
            try:
                msg = client.messages.create(**params_for(it))
            except Exception as exc:  # noqa: BLE001 - one bad request must not kill the run
                log.warning("translation failed for %s: %s", it["id"], exc)
                report["errored"] += 1
                continue
            record(it, msg)
            report["submitted"] += 1
        store.conn.commit()
        return report

    by_custom: Dict[str, Dict[str, Any]] = {}
    requests: List[Any] = []
    for i, it in enumerate(items):
        cid = f"tr-{i}"
        by_custom[cid] = it
        requests.append(_typed_request(cid, params_for(it)))
    try:
        batch = client.messages.batches.create(requests=requests)
        report["submitted"] = len(requests)
        report["batch_id"] = getattr(batch, "id", None)
        log.info("submitted translation batch %s with %d requests", report["batch_id"], len(requests))
        start = time.monotonic()
        deadline = start + max(float(max_wait_seconds), 0.0)
        status = getattr(batch, "processing_status", None)
        while status != "ended" and time.monotonic() < deadline:
            time.sleep(max(float(poll_seconds), 0.0))
            batch = client.messages.batches.retrieve(batch.id)
            status = getattr(batch, "processing_status", None)
        report["waited_seconds"] = round(time.monotonic() - start)
        if status != "ended":
            log.warning("translation batch %s still %s after %ss; nothing stored", report["batch_id"], status, report["waited_seconds"])
            report.update(status="incomplete", processing_status=status)
        else:
            for res in client.messages.batches.results(batch.id):
                it = by_custom.get(getattr(res, "custom_id", ""))
                if it is None:
                    continue
                result = getattr(res, "result", None)
                if getattr(result, "type", None) == "succeeded":
                    record(it, getattr(result, "message", None))
                else:
                    report["errored"] += 1
    except Exception as exc:  # noqa: BLE001 - a 401/429/529/network error must not abort the caller's run
        log.warning("translation batch failed: %s", exc)
        report.update(status="error", error=f"{type(exc).__name__}: {exc}")
    store.conn.commit()  # outside the try: rows saved before a failure are kept
    return report


def translations_for(store: Store, paper_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """{paper_id: {lang, title_en, abstract_en, model}} for the given ids (for digests / dashboards)."""
    ensure_schema(store)
    out: Dict[str, Dict[str, Any]] = {}
    ids = [i for i in paper_ids if i]
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for pid, lang, title_en, abstract_en, model in store.conn.execute(
            f"SELECT paper_id, lang, title_en, abstract_en, model FROM translations WHERE paper_id IN ({marks})", chunk
        ):
            out[pid] = {"lang": lang, "title_en": title_en, "abstract_en": abstract_en, "model": model}
    return out
