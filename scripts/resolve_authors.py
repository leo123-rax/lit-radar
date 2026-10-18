#!/usr/bin/env python3
"""Resolve and verify the OpenAlex author ids in config/subgroups.yaml against the live API.

    python3 scripts/resolve_authors.py --dry-run                 # print proposals, change nothing
    python3 scripts/resolve_authors.py --write                   # apply the proposals to config/subgroups.yaml
    python3 scripts/resolve_authors.py --dry-run --names "Fei Chen" "Xiao Wang"
    python3 scripts/resolve_authors.py --dry-run --report out.json --markdown tables.md --sanity 5

What it does, per tracked author (``tracked_authors`` in every subgroup):

* **existing id** -> fetched in batches of 50 (``/authors?filter=openalex:A|B|...``). The profile is *verified*
  when its display name agrees with the configured name, an institution in ``last_known_institutions`` or
  ``affiliations`` matches the configured institution, and the works count is plausible for one person
  (``--merged-works`` and above is flagged as a merged profile, below ``--stub-works`` as a stub). An id that
  OpenAlex has merged away is followed to its new id and *corrected*.
* **every name** -> ``/authors?filter=display_name.search:<name>`` (25 rows); when that is not conclusive, or
  the name has more profiles than one page holds, a second query adds ``topics.id:<subgroup topics>``.
  Candidates are scored on institution match (strong: ``last_known_institutions``; weak: any affiliation),
  topic overlap with the subgroup's ``openalex_topics`` and works count.
* **decision** -> ``resolved`` when exactly one candidate matches the institution with a plausible works count
  (or the best such candidate has at least ``--split-ratio`` times the works of the next: the smaller ones are
  split profiles and are named in the comment); ``ambiguous`` when two institution-matching candidates are
  comparable (the top two are written into a YAML comment and the id stays empty); ``unresolved`` when no
  candidate matches the institution; ``corrected`` when an existing id fails verification and one candidate
  clearly matches; ``suspicious`` when it fails and nothing better was found (the id is kept, the comment says why).
* ``orcid:`` is added whenever the chosen profile carries one (the pipeline does not use it yet; it is the
  stable handle for re-resolving a split or merged profile later).

Edits are line-based: each author line is rewritten in place (``name``, ``openalex_id``, ``orcid``,
``institution`` order, the same as ``litradar track author`` writes), block structure and every other comment
survive, and the file is re-parsed afterwards: the set of (subgroup, name) pairs must be unchanged and every id
must look like ``A\\d{6,}`` or the write is refused. The first line of the file is replaced with a header that
carries the resolution date and counts.

Budget: one call per 50 existing ids, one or two calls per distinct name, plus ``--sanity`` works queries;
~250 calls for a 160-name file, against a free-key budget of roughly 10,000 list calls a day. Responses are
cached on disk (``--cache-dir``, default ``.cache/resolve-authors/``) so ``--dry-run`` followed by ``--write``
costs the budget once. The API key and contact address are read from the environment (``OPENALEX_API_KEY``,
``LITRADAR_CONTACT_EMAIL``), loaded from ``--env`` (default: the repository's ``.env``); neither is ever printed.
Python 3.9+, the repository's own packages (``litradar.http``, ``litradar.config``) and PyYAML.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from litradar.config import load_env  # noqa: E402
from litradar.http import Http  # noqa: E402

AUTHORS_URL = "https://api.openalex.org/authors"
WORKS_URL = "https://api.openalex.org/works"
ID_RE = re.compile(r"^A\d{6,}$")
ANY_ID_RE = re.compile(r"\bA\d{6,}\b")
AUTHOR_LINE_RE = re.compile(r"^(?P<indent>\s*)- \{(?P<body>.*?)\}(?P<tail>\s*(?:#.*)?)$")
SUBGROUP_LINE_RE = re.compile(r"^  - id:\s*(?P<id>[\w-]+)")
TRACKED_KEY_RE = re.compile(r"^    tracked_authors:")
# a trailing comment that describes the id's pre-resolution state (replaced on write); anything else is kept
RESOLUTION_NOTE_RE = re.compile(
    r"unresolved|not in notes|probable|split profile|merged|stub|works|verify|verified|resolved|corrected|display name|profile|candidates",
    re.I,
)
# a dated note this tool wrote on an earlier pass: kept as it is when the id simply verifies again (idempotent re-runs)
DATED_NOTE_RE = re.compile(r"^#\s*(resolved|verified|corrected|suspicious|unresolved) \d{4}-\d{2}-\d{2}")
HEADER_RE = re.compile(r"^# OpenAlex (author )?ids")
SPIKE_MIN_WORKS = 150  # a single publication year with at least this many works ...
SPIKE_SHARE = 0.4  # ... and at least this share of the profile's works is a bulk misattribution, not a person's output

# configured institution string -> phrases that name the same place in OpenAlex (matched token-wise)
INSTITUTION_ALIASES: Dict[str, List[str]] = {
    "mit": ["Massachusetts Institute of Technology"],
    "ucsf": ["University of California, San Francisco"],
    "uc berkeley": ["University of California, Berkeley", "Lawrence Berkeley National Laboratory"],
    "ucla": ["University of California, Los Angeles"],
    "uc san diego": ["University of California, San Diego"],
    "uc santa cruz": ["University of California, Santa Cruz"],
    "caltech": ["California Institute of Technology"],
    "nhgri": ["National Human Genome Research Institute", "National Institutes of Health"],
    "embl-ebi": ["European Bioinformatics Institute", "European Molecular Biology Laboratory"],
    "embl heidelberg": ["European Molecular Biology Laboratory"],
    "czi": ["Chan Zuckerberg Initiative", "Chan Zuckerberg Biohub"],
    "chan zuckerberg biohub": ["Chan Zuckerberg Biohub", "Chan Zuckerberg Initiative"],
    "chan zuckerberg imaging institute": ["Chan Zuckerberg Imaging Institute", "Chan Zuckerberg Initiative", "New York Structural Biology Center"],
    "wehi": ["Walter and Eliza Hall Institute of Medical Research"],
    "kth": ["KTH Royal Institute of Technology"],
    "scilifelab": ["Science for Life Laboratory", "SciLifeLab"],
    "hhmi": ["Howard Hughes Medical Institute"],
    "harvard": ["Harvard University", "Harvard Medical School"],
    "harvard medical school": ["Harvard Medical School", "Harvard University"],
    "harvard university": ["Harvard University"],
    "memorial sloan kettering": ["Memorial Sloan Kettering Cancer Center", "Sloan Kettering Institute"],
    "google deepmind": ["DeepMind", "Google"],
    "weizmann institute": ["Weizmann Institute of Science"],
    "nyu langone": ["NYU Langone Health", "New York University"],
    "nyu": ["New York University"],
    "microsoft research": ["Microsoft Research", "Microsoft"],
    "wyss institute": ["Wyss Institute for Biologically Inspired Engineering"],
    "whitehead institute": ["Whitehead Institute for Biomedical Research"],
    "epfl": ["École Polytechnique Fédérale de Lausanne"],
    "md anderson cancer center": ["The University of Texas MD Anderson Cancer Center"],
    "cnrs": ["Centre National de la Recherche Scientifique"],
    "slac": ["SLAC National Accelerator Laboratory"],
    "university of wisconsin": ["University of Wisconsin–Madison"],
    "jackson laboratory for genomic medicine": ["The Jackson Laboratory for Genomic Medicine", "Jackson Laboratory"],
    "umass chan medical school": ["UMass Chan Medical School", "University of Massachusetts Medical School"],
    "grail": ["GRAIL"],
    "adela": ["Adela"],
    "exact sciences": ["Exact Sciences"],
    "dana-farber cancer institute": ["Dana-Farber Cancer Institute"],
    "max planck institute for multidisciplinary sciences": ["Max Planck Institute for Multidisciplinary Sciences", "Max Planck Institute for Biophysical Chemistry"],
    "mrc laboratory of molecular biology": ["MRC Laboratory of Molecular Biology"],
    "princess margaret cancer centre": ["Princess Margaret Cancer Centre", "University Health Network"],
    "chinese university of hong kong": ["Chinese University of Hong Kong"],
    "gladstone institutes": ["Gladstone Institutes"],
    "innovative genomics institute": ["Innovative Genomics Institute"],
    "allen institute for brain science": ["Allen Institute for Brain Science", "Allen Institute"],
    "evolutionaryscale": ["EvolutionaryScale"],
    "fred hutchinson cancer center": ["Fred Hutchinson Cancer Center", "Fred Hutch Cancer Center", "Fred Hutchinson Cancer Research Center"],
    "boston children's hospital": ["Boston Children's Hospital", "Boston Childrens Hospital"],
    "massachusetts general hospital": ["Massachusetts General Hospital", "Mass General Brigham"],
    "francis crick institute": ["The Francis Crick Institute", "Francis Crick Institute"],
    "new york genome center": ["New York Genome Center"],
    "arc institute": ["Arc Institute"],
}
GENERIC_TOKENS = {
    "university", "institute", "institutes", "college", "school", "medical", "medicine", "center", "centre", "hospital",
    "laboratory", "laboratories", "research", "national", "science", "sciences", "technology", "health", "cancer",
    "foundation", "group", "department", "faculty", "general", "royal", "children", "childrens", "state", "united",
    "states", "kingdom", "germany", "china", "inc", "ltd", "llc", "campus", "system",
}
STOPWORDS = {"of", "the", "for", "and", "de", "in", "at", "on", "fur", "für", "der", "des", "di", "du", "la", "le"}
NICKNAMES: Dict[str, List[str]] = {
    "tom": ["thomas"], "bill": ["william"], "bob": ["robert"], "deb": ["deborah", "debra"], "ed": ["edward", "edwin"],
    "alex": ["alexander", "alexandra"], "steve": ["steven", "stephen"], "mike": ["michael"], "jeff": ["jeffrey", "jeffery"],
    "dan": ["daniel"], "ben": ["benjamin"], "chris": ["christopher", "christine", "christian"], "matt": ["matthew"],
    "jim": ["james"], "nick": ["nicholas", "nickolas"], "sam": ["samuel", "samantha"], "jon": ["jonathan"],
    "rick": ["richard"], "andy": ["andrew"], "ted": ["edward", "theodore"], "tim": ["timothy"], "greg": ["gregory"],
    "pat": ["patrick", "patricia"], "joe": ["joseph"], "dave": ["david"], "rob": ["robert"], "will": ["william"],
    "liz": ["elizabeth"], "beth": ["elizabeth"], "kate": ["katherine", "katharine", "kathryn"], "max": ["maximilian"],
}


# ----------------------------------------------------------------------------- text helpers
_LIGATURES = str.maketrans({"ß": "ss", "æ": "ae", "Æ": "ae", "ø": "o", "Ø": "o", "œ": "oe", "Œ": "oe", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "þ": "th"})
_DASHES_RE = re.compile(r"[‐‑‒–—−]")


def ascii_fold(s: str) -> str:
    """Lower-case ASCII: 'Strauß' -> 'strauss', 'Alix‐Panabières' -> 'alix-panabieres' (NFKD alone drops ß and the dash)."""
    s = _DASHES_RE.sub("-", (s or "").translate(_LIGATURES))
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def fold(s: str) -> str:
    s = ascii_fold(s).replace("'", "").replace("&", " and ")
    return re.sub(r"[^a-z0-9 ]+", " ", s.replace("-", " "))


def tokens(s: str) -> List[str]:
    return [t for t in fold(s).split() if t and t not in STOPWORDS]


def strip_country(s: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", s or "")


def name_parts(name: str) -> Tuple[str, List[str]]:
    """(surname, given tokens that are not initials) for a display name; 'Last, First' is accepted."""
    raw = ascii_fold(name)
    if "," in raw:
        last, _, given = raw.partition(",")
        raw = f"{given} {last}"
    toks = [t for t in re.sub(r"[^a-z ]", " ", raw.replace("'", "").replace("-", " ")).split() if t]
    if not toks:
        return "", []
    surname = toks[-1]
    given = [t for t in toks[:-1] if len(t) > 1]
    return surname, given


def name_variants(name: str) -> List[str]:
    """The configured name, then the same name without initials ('Fabian J. Theis' -> 'Fabian Theis')."""
    out = [name]
    bare = " ".join(t for t in name.replace(".", ". ").split() if len(t.rstrip(".")) > 1)
    if bare and bare != name:
        out.append(bare)
    return out


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (small strings only)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def given_equivalent(a: str, b: str) -> bool:
    """Equal given names, or a nickname and its full form ('Ed'/'Edward'). No prefix rule: 'Xiao' is not 'Xiaodong'."""
    return a == b or b in NICKNAMES.get(a, ()) or a in NICKNAMES.get(b, ())


def name_agrees(configured: str, candidate: Dict[str, Any]) -> bool:
    """The configured name and the profile are the same person's name: the profile's *display name* carries the
    same surname, and one of the configured given names appears among the given names of the display name or of
    one of its recorded alternatives ('Dennis' in 'Yuk Ming Dennis Lo'; 'Jürgen' behind 'J. Cox'). A profile whose
    display surname differs ('Yunfei Chen' for 'Fei Chen', 'Webster' for 'Church') never agrees, whatever its
    alternatives say: merged profiles accumulate alternatives that belong to other people."""
    surname, given = name_parts(configured)
    if not surname:
        return False
    display = candidate.get("display_name") or ""
    s_display, g_display = name_parts(display)
    if s_display != surname:
        return False
    if not given:
        return True
    if any(given_equivalent(x, g) for x in given for g in g_display):
        return True
    if g_display:
        # the display name spells out a different given name ('Xiaodong' for 'Xiao'): a different person, whatever
        # the alternatives accumulated from merges say
        return False
    # the display name carries only initials ('J. Cox'): the alternatives may spell the given name out
    for n in candidate.get("display_name_alternatives") or []:
        s2, g2 = name_parts(n)
        if s2 == surname and any(given_equivalent(x, g) for x in given for g in g2):
            return True
    return False


def institution_phrases(configured: str) -> List[str]:
    """Alternatives for a configured institution string ('MIT / Broad Institute' -> MIT aliases + Broad Institute)."""
    out: List[str] = []
    for part in re.split(r"\s*/\s*", configured or ""):
        part = part.strip()
        if not part:
            continue
        key = part.lower()
        out.append(part)
        out.extend(INSTITUTION_ALIASES.get(key, []))
    return list(dict.fromkeys(out))


def institution_matches(configured: str, candidate_name: str) -> int:
    """0 = no match; 2 = every token of a configured phrase is in the candidate's name (or vice versa);
    1 = the distinctive (non-generic) tokens of a configured phrase are all in the candidate's name."""
    cand = set(tokens(strip_country(candidate_name)))
    if not cand:
        return 0
    best = 0
    for phrase in institution_phrases(configured):
        ptoks = set(tokens(phrase))
        if not ptoks:
            continue
        if ptoks <= cand or cand <= ptoks:
            return 2
        distinctive = {t for t in ptoks if t not in GENERIC_TOKENS and len(t) > 2}
        if distinctive and distinctive <= cand:
            best = max(best, 1)
    return best


# ----------------------------------------------------------------------------- the config file
def load_config(path: Path) -> Tuple[List[str], Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return text.splitlines(), yaml.safe_load(text) or {}


def author_lines(lines: Sequence[str]) -> List[Tuple[int, str, Dict[str, Any], str]]:
    """[(line index, subgroup id, author dict, trailing comment)] for every tracked-author line, in file order."""
    out = []
    sg = ""
    in_tracked = False
    for i, ln in enumerate(lines):
        m = SUBGROUP_LINE_RE.match(ln)
        if m:
            sg, in_tracked = m.group("id"), False
            continue
        if TRACKED_KEY_RE.match(ln):
            in_tracked = True
            continue
        if re.match(r"^    \w", ln):  # another key of the subgroup
            in_tracked = False
            continue
        if not in_tracked:
            continue
        m = AUTHOR_LINE_RE.match(ln)
        if not m or not ln.startswith("      - {"):
            continue
        try:
            d = yaml.safe_load("{" + m.group("body") + "}")
        except yaml.YAMLError:
            continue
        if isinstance(d, dict) and d.get("name"):
            out.append((i, sg, d, m.group("tail").strip()))
    return out


def rewrite_author_line(line: str, new_id: str, orcid: str, comment: Optional[str]) -> str:
    """Return the author line with its openalex_id (and orcid) replaced and its trailing comment set.

    `comment` None keeps the existing trailing comment; '' removes it; otherwise it replaces it (without the '#').
    The result must round-trip through YAML to the expected mapping, or ValueError is raised."""
    m = AUTHOR_LINE_RE.match(line)
    if not m:
        raise ValueError(f"not an author line: {line!r}")
    body = m.group("body")
    before = yaml.safe_load("{" + body + "}")
    body, n = re.subn(r'(openalex_id:\s*)"[^"]*"', lambda mm: f'{mm.group(1)}"{new_id}"', body, count=1)
    if n != 1:
        raise ValueError(f"no openalex_id field in {line!r}")
    if orcid:
        if re.search(r"\borcid:", body):
            body = re.sub(r'(orcid:\s*)"[^"]*"', lambda mm: f'{mm.group(1)}"{orcid}"', body, count=1)
        else:
            body = re.sub(r'(openalex_id:\s*"[^"]*")', lambda mm: f'{mm.group(1)}, orcid: "{orcid}"', body, count=1)
    tail = m.group("tail")
    if comment is not None:
        tail = f"  # {comment}" if comment else ""
    new_line = f"{m.group('indent')}- {{{body}}}{tail}"
    expected = dict(before)
    expected["openalex_id"] = new_id
    if orcid:
        expected["orcid"] = orcid
    got = yaml.safe_load(AUTHOR_LINE_RE.match(new_line).group("body").join("{}"))
    if got != expected:
        raise ValueError(f"author line does not round-trip: {new_line!r}")
    return new_line


# ----------------------------------------------------------------------------- OpenAlex client
class Client:
    def __init__(self, http: Http, api_key: str, mailto: str, cache: bool = True) -> None:
        self.http = http
        self.headers = {"Accept": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.mailto = mailto
        self.cache = cache
        self.calls = 0  # requests that reached the network (cache hits excluded)
        self.failures: List[str] = []

    def get(self, url: str, **params: Any) -> Optional[Dict[str, Any]]:
        if self.mailto:
            params["mailto"] = self.mailto
        before = self.http.stats["requests"]
        resp = self.http.get(url, params=params, headers=self.headers, cache=self.cache, ok_statuses=(200,))
        self.calls += self.http.stats["requests"] - before
        if resp is None or resp.status_code != 200:
            status = resp.status_code if resp is not None else "no response"
            shown = {k: v for k, v in params.items() if k != "mailto"}
            self.failures.append(f"{url.rsplit('/', 1)[-1]} {shown} -> {status}")
            return None
        try:
            data = resp.json()
        except ValueError:
            self.failures.append(f"{url} -> non-JSON body")
            return None
        return data if isinstance(data, dict) else None

    def authors_by_ids(self, ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        ids = list(dict.fromkeys(i for i in ids if i))
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            data = self.get(AUTHORS_URL, filter="openalex:" + "|".join(chunk), **{"per-page": 50})
            for r in (data or {}).get("results") or []:
                found[short_id(r.get("id"))] = r
        # ids the filter did not return: fetch singly so a merged/redirected id is followed
        for aid in ids:
            if aid not in found:
                data = self.get(f"{AUTHORS_URL}/{aid}")
                if data and data.get("id"):
                    data["_requested_id"] = aid
                    found[aid] = data
        return found

    def search(self, name: str, topics: Sequence[str] = (), per_page: int = 25) -> Tuple[List[Dict[str, Any]], int]:
        """``/authors?search=<name>`` (relevance-ranked over display names and their variants; a filter on
        ``display_name.search`` would miss 'Fabian Joachim Theis' for 'Fabian J. Theis'), optionally restricted to
        profiles whose topics include one of ``topics``."""
        params: Dict[str, Any] = {"search": name, "per-page": per_page}
        if topics:
            params["filter"] = "topics.id:" + "|".join(topics)
        data = self.get(AUTHORS_URL, **params)
        if not data:
            return [], -1
        return list(data.get("results") or []), int((data.get("meta") or {}).get("count") or 0)

    def works_since(self, author_id: str, since: str) -> Optional[int]:
        data = self.get(WORKS_URL, filter=f"authorships.author.id:{author_id},from_publication_date:{since}", **{"per-page": 1, "select": "id"})
        if not data:
            return None
        return int((data.get("meta") or {}).get("count") or 0)


def short_id(url_or_id: Any) -> str:
    return str(url_or_id or "").rsplit("/", 1)[-1]


# ----------------------------------------------------------------------------- scoring
def profile_summary(p: Dict[str, Any], configured_institutions: Sequence[str], topics: Sequence[str]) -> Dict[str, Any]:
    """Flatten an author profile to what the decision needs, with institution/topic evidence against the config."""
    last_known = [strip_country(i.get("display_name") or "") for i in (p.get("last_known_institutions") or []) if isinstance(i, dict)]
    affs: List[Tuple[str, int]] = []
    for a in p.get("affiliations") or []:
        inst = (a.get("institution") or {}) if isinstance(a, dict) else {}
        years = [int(y) for y in (a.get("years") or []) if isinstance(y, int)]
        affs.append((strip_country(inst.get("display_name") or ""), max(years) if years else 0))
    inst_strength, inst_hit = 0, ""
    for cfg in configured_institutions:
        for name in last_known:
            s = institution_matches(cfg, name)
            if s and (s + 1 > inst_strength or (s + 1 == inst_strength and not inst_hit)):
                inst_strength, inst_hit = s + 1, name  # 2 or 3 for last-known
        for name, _year in affs:
            s = institution_matches(cfg, name)
            if s and s > inst_strength:
                inst_strength, inst_hit = s, name  # 1 or 2 for an affiliation
    topic_ids = [short_id(t.get("id")) for t in (p.get("topics") or []) if isinstance(t, dict)]
    overlap = [t for t in topic_ids if t in set(topics)]
    works = int(p.get("works_count") or 0)
    by_year = {int(y.get("year")): int(y.get("works_count") or 0) for y in (p.get("counts_by_year") or []) if isinstance(y, dict) and y.get("year")}
    recent = sorted(((y, n) for y, n in by_year.items()), reverse=True)[:3]
    # signs of a merged or bulk-polluted profile: one year holding a large share of all works (a dataset or
    # patent dump, or another person's catalogue), or alternative names with a different surname
    spike = max(by_year.items(), key=lambda kv: kv[1]) if by_year else None
    if not (spike and spike[1] >= SPIKE_MIN_WORKS and spike[1] >= SPIKE_SHARE * max(works, 1)):
        spike = None
    surname, given_display = name_parts(p.get("display_name") or "")
    own_tokens = set(fold(p.get("display_name") or "").split())
    foreign = []
    for alt in p.get("display_name_alternatives") or []:
        s_alt, g_alt = name_parts(alt)
        if not (surname and s_alt and g_alt):
            continue
        # another person's name, not a typo ('Phillipy'), a truncation ('Ash A'), a maiden name already in the
        # display name or a reordering: the surname is far from the person's own and no given name is shared
        if len(s_alt) < 3 or s_alt in own_tokens or surname in set(fold(alt).split()) or edit_distance(s_alt, surname) <= 2:
            continue
        if any(given_equivalent(x, g) for x in given_display for g in g_alt):
            continue
        foreign.append(alt)
    return {
        "spike": spike,  # (year, works) or None
        "foreign_alternatives": foreign[:3],
        "id": short_id(p.get("id")),
        "display_name": p.get("display_name") or "",
        "works_count": works,
        "cited_by_count": int(p.get("cited_by_count") or 0),
        "orcid": short_id(p.get("orcid")),
        "last_known": last_known,
        "affiliations_recent": [n for n, y in sorted(affs, key=lambda x: -x[1])[:6]],
        "institution_strength": inst_strength,  # 0 none; 1 weak affiliation; 2 affiliation or weak last-known; 3 last-known
        "institution_hit": inst_hit,
        "topic_overlap": overlap,
        "recent_years": recent,
        "requested_id": p.get("_requested_id") or "",
    }


def score(summary: Dict[str, Any], name_ok: bool) -> float:
    if not name_ok:
        return -1.0
    return summary["institution_strength"] * 3.0 + (2.0 if summary["topic_overlap"] else 0.0) + math.log10(summary["works_count"] + 1)


# ----------------------------------------------------------------------------- decisions
class Decision:
    def __init__(self, name: str, subgroups: List[str], institutions: List[str], old_id: str, comment: str) -> None:
        self.name = name
        self.subgroups = subgroups
        self.institutions = institutions
        self.old_id = old_id
        self.old_comment = comment
        self.status = ""  # resolved | verified | corrected | ambiguous | unresolved | suspicious | error
        self.new_id = old_id
        self.orcid = ""
        self.evidence = ""
        self.candidates: List[Dict[str, Any]] = []
        self.chosen: Optional[Dict[str, Any]] = None
        self.search_count = -1
        self.flags: List[str] = []
        self.new_comment: Optional[str] = None  # None = keep the existing trailing comment

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "subgroups": self.subgroups, "institutions": self.institutions, "old_id": self.old_id,
            "new_id": self.new_id, "orcid": self.orcid, "status": self.status, "evidence": self.evidence,
            "flags": self.flags, "search_count": self.search_count, "old_comment": self.old_comment,
            "new_comment": self.new_comment, "chosen": self.chosen,
            "candidates": [{k: c.get(k) for k in ("id", "display_name", "works_count", "cited_by_count", "institution_hit", "institution_strength", "topic_overlap", "orcid", "name_ok", "spike", "foreign_alternatives")} for c in self.candidates[:6]],
        }


def describe(c: Dict[str, Any]) -> str:
    inst = c.get("institution_hit") or (c.get("last_known") or [""])[0] or (c.get("affiliations_recent") or ["no institution"])[0]
    return f"{c['id']} ({inst}, {c['works_count']} works)"


def pollution_flags(c: Dict[str, Any]) -> List[str]:
    """Human-readable warnings about a profile that is probably merged with someone else's catalogue."""
    out = []
    if c.get("spike"):
        year, n = c["spike"]
        out.append(f"{n} of {c['works_count']} works are dated {year} (bulk misattribution?)")
    if c.get("foreign_alternatives"):
        out.append("alternative names include " + ", ".join(repr(a) for a in c["foreign_alternatives"][:2]) + " (merged with another person?)")
    return out


def pollution_note(c: Dict[str, Any]) -> str:
    """The short form of pollution_flags for a YAML comment."""
    parts = []
    if c.get("spike"):
        year, n = c["spike"]
        parts.append(f"{n} works dated {year} look bulk-attributed")
    if c.get("foreign_alternatives"):
        parts.append("alternatives name another person (merged?)")
    return ("; " + "; ".join(parts)) if parts else ""


def is_clear(top: Optional[Dict[str, Any]], second: Optional[Dict[str, Any]], args: argparse.Namespace) -> bool:
    """`top` is the person's main profile and `second` a split or a namesake: far more citations, far more works,
    or the configured institution is `top`'s last known one while `second` only passed through it (and `top`
    still leads on citations)."""
    if not top:
        return False
    if second is None:
        return True
    if top["cited_by_count"] >= args.split_ratio * 2 * max(second["cited_by_count"], 1):
        return True
    if top["works_count"] >= args.split_ratio * max(second["works_count"], 1):
        return True
    return top["institution_strength"] == 3 and second["institution_strength"] < 3 and top["cited_by_count"] >= second["cited_by_count"]


def decide(d: Decision, existing: Optional[Dict[str, Any]], candidates: List[Dict[str, Any]], args: argparse.Namespace, today: str) -> None:
    """Fill d.status / d.new_id / d.orcid / d.new_comment from the existing profile and the search candidates."""
    matching = [c for c in candidates if c["name_ok"] and c["institution_strength"] > 0 and c["works_count"] >= args.stub_works]
    # the main profile of a PI is the one the citations went to; split profiles and namesakes trail far behind
    by_works = sorted(matching, key=lambda c: (-c["cited_by_count"], -c["works_count"]))
    top = by_works[0] if by_works else None
    second = by_works[1] if len(by_works) > 1 else None
    clear = is_clear(top, second, args)
    # a smaller same-institution profile that shares the subgroup's topics is a split of the same person; one
    # without topic overlap is a namesake (reported, never written into the file)
    splits = [c for c in by_works[1:] if c["topic_overlap"] or c.get("from_comment")] if (top and clear) else []
    namesakes = [c for c in by_works[1:] if not (c["topic_overlap"] or c.get("from_comment")) and c["id"] != d.old_id] if top else []
    if namesakes:
        d.flags.append("namesake(s) at the institution, no topic overlap: " + ", ".join(describe(c) for c in namesakes[:3]))

    def split_note() -> str:
        return ("; other same-topic profile" + ("s" if len(splits) > 1 else "") + " there: " + ", ".join(f"{c['id']} ({c['works_count']} works)" for c in splits[:3])) if splits else ""

    def evidence_for(c: Dict[str, Any]) -> str:
        parts = [f"{c['display_name']}", f"{c['works_count']} works", f"{c['cited_by_count']:,} citations"]
        if c["institution_hit"]:
            parts.append(f"institution: {c['institution_hit']}" + (" (last known)" if c["institution_strength"] >= 2 else " (affiliation)"))
        else:
            parts.append("institution: no match")
        parts.append(("topics: " + ", ".join(c["topic_overlap"][:3])) if c["topic_overlap"] else "topics: no overlap")
        if c["orcid"]:
            parts.append("ORCID " + c["orcid"])
        return "; ".join(parts)

    if d.old_id:
        if existing is None:
            d.status = "error"
            d.evidence = "the configured id could not be fetched (API failure); nothing changed"
            d.flags.append("fetch failed")
            return
        ex = existing
        ex_name_ok = ex["name_ok"]
        ex_inst_ok = ex["institution_strength"] > 0
        redirected = bool(ex.get("requested_id")) and ex["id"] != ex["requested_id"]
        if ex["works_count"] >= args.merged_works:
            d.flags.append(f"merged profile? {ex['works_count']} works")
        if ex["works_count"] < args.stub_works:
            d.flags.append(f"stub: {ex['works_count']} works")
        if not ex_name_ok:
            d.flags.append(f"display name {ex['display_name']!r} does not agree")
        if not ex_inst_ok:
            d.flags.append("no institution match")
        if redirected:
            d.flags.append(f"OpenAlex merged {ex['requested_id']} into {ex['id']}")
        others = [c for c in by_works if c["id"] != ex["id"]]
        # the configured institution may simply not exist in OpenAlex (a young company, an institute without a ROR):
        # with the subgroup's topics on the profile and no rival at the institution the id still stands
        inst_tolerated = (not ex_inst_ok) and bool(ex["topic_overlap"]) and not others and ex["cited_by_count"] >= 1000
        if inst_tolerated:
            d.flags = [f for f in d.flags if f != "no institution match"]
            on_profile = ", ".join((ex["last_known"] or ex["affiliations_recent"])[:2]) or "no institution"
            d.flags.append(f"institution not matched (configured {' / '.join(d.institutions)}; profile: {on_profile}); topics overlap, no rival profile")
        verified = ex_name_ok and (ex_inst_ok or inst_tolerated) and args.stub_works <= ex["works_count"] < args.merged_works
        # a same-institution profile that fits the subgroup's topics better and carries the citations is the
        # person's main profile; the configured one is then a namesake or the smaller half of a split
        alts = [c for c in others if c["topic_overlap"] and not c.get("from_comment")]
        best_alt = alts[0] if alts else None
        usurped = bool(best_alt) and (
            (not ex["topic_overlap"] and best_alt["cited_by_count"] >= ex["cited_by_count"])
            or (best_alt["cited_by_count"] >= 3 * max(ex["cited_by_count"], 1) and best_alt["works_count"] >= ex["works_count"])
        )
        if usurped and best_alt is not None:
            if not ex["topic_overlap"]:
                d.flags.append("no topic overlap with the subgroup")
            d.flags.append(f"{best_alt['id']} at the same institution fits better ({best_alt['works_count']} works, {best_alt['cited_by_count']:,} citations, topics {', '.join(best_alt['topic_overlap'][:2])})")
            if is_clear(best_alt, alts[1] if len(alts) > 1 else None, args):
                d.status = "corrected"
                d.new_id = best_alt["id"]
                d.orcid = best_alt["orcid"]
                d.chosen = best_alt
                why = "; ".join(d.flags)
                d.flags.extend(pollution_flags(best_alt))
                d.evidence = f"was {ex['id']} ({ex['display_name']}, {ex['works_count']} works, {ex['cited_by_count']:,} citations; {why}); now " + evidence_for(best_alt)
                d.new_comment = f"corrected {today}: was {ex['id']} ({ex['works_count']} works, {'no topic overlap' if not ex['topic_overlap'] else 'smaller split'}); now {best_alt['works_count']} works, {best_alt['institution_hit']}" + pollution_note(best_alt)
                return
            verified = False
        if verified and not redirected:
            d.status = "verified"
            d.new_id = ex["id"]
            d.orcid = ex["orcid"]
            d.chosen = ex
            d.flags.extend(pollution_flags(ex))
            d.evidence = evidence_for(ex) + ("; " + "; ".join(d.flags) if d.flags else "")
            ex_splits = [c for c in others if c["topic_overlap"] or c.get("from_comment")]
            if ex_splits:
                d.flags.append("split profile(s): " + ", ".join(describe(c) for c in ex_splits[:3]))
            if DATED_NOTE_RE.match(d.old_comment or ""):
                return  # an earlier pass already wrote the evidence; a plain re-verification does not rewrite it
            if RESOLUTION_NOTE_RE.search(d.old_comment or "") or ex_splits or pollution_flags(ex):
                if inst_tolerated:
                    where = f"profile lists {', '.join((ex['last_known'] or ex['affiliations_recent'])[:2]) or 'no institution'}; configured institution not in OpenAlex, topics agree"
                else:
                    where = ex["institution_hit"] or (", ".join(ex["last_known"][:1]) + " on the profile" if ex["last_known"] else "institution via affiliations")
                note = f"verified {today}: {ex['works_count']} works, {where}"
                if ex_splits:
                    note += "; other same-topic profile" + ("s" if len(ex_splits) > 1 else "") + " there: " + ", ".join(f"{c['id']} ({c['works_count']} works)" for c in ex_splits[:3])
                d.new_comment = note + pollution_note(ex)
            return
        # the existing id fails verification (or was redirected): is there a clear replacement?
        if top and clear and top["id"] != ex["id"]:
            d.status = "corrected"
            d.new_id = top["id"]
            d.orcid = top["orcid"]
            d.chosen = top
            why = "; ".join(d.flags) or "better profile found"
            d.evidence = f"was {ex['id']} ({ex['display_name']}, {ex['works_count']} works; {why}); now " + evidence_for(top)
            d.new_comment = f"corrected {today}: was {ex['id']} ({why}); now {top['works_count']} works, {top['institution_hit']}{split_note()}"
            return
        if redirected:
            d.status = "corrected"
            d.new_id = ex["id"]
            d.orcid = ex["orcid"]
            d.chosen = ex
            d.evidence = f"OpenAlex merged {ex['requested_id']} into {ex['id']}; " + evidence_for(ex)
            d.new_comment = f"corrected {today}: {ex['requested_id']} was merged by OpenAlex into {ex['id']}"
            return
        if verified and top and top["id"] == ex["id"]:
            d.status = "verified"
        else:
            d.status = "suspicious"
        d.new_id = ex["id"]
        d.orcid = ex["orcid"]
        d.chosen = ex
        d.evidence = evidence_for(ex) + ("; " + "; ".join(d.flags) if d.flags else "")
        alt = [c for c in by_works if c["id"] != ex["id"]][:2]
        if d.status == "suspicious":
            note = f"suspicious {today}: " + "; ".join(d.flags)
            if alt:
                note += "; alternatives " + " / ".join(describe(c) for c in alt)
            d.new_comment = note
        return

    # no id configured
    if top and clear:
        d.status = "resolved"
        d.new_id = top["id"]
        d.orcid = top["orcid"]
        d.chosen = top
        topic_note = ""
        if not top["topic_overlap"]:
            d.flags.append("no topic overlap with the subgroup's openalex_topics (check the profile's topics by hand)")
            topic_note = "; no overlap with the subgroup's topic ids, checked by hand"
        d.flags.extend(pollution_flags(top))
        d.evidence = evidence_for(top) + (f"; {len(splits)} same-topic profile(s) at the institution: " + ", ".join(c["id"] for c in splits[:3]) if splits else "")
        d.new_comment = f"resolved {today}: {top['works_count']} works, {top['institution_hit']}{split_note()}{topic_note}" + pollution_note(top)
        return
    if top and second:
        d.status = "ambiguous"
        d.evidence = "two comparable profiles at the institution: " + " vs ".join(describe(c) for c in (top, second))
        d.new_comment = f"unresolved {today}: ambiguous, {describe(top)} vs {describe(second)}"
        return
    d.status = "unresolved"
    plausible = [c for c in candidates if c["name_ok"] and c["works_count"] >= args.stub_works]
    plausible.sort(key=lambda c: -score(c, True))
    if plausible:
        d.evidence = "no candidate matches the institution; best by name: " + " / ".join(describe(c) for c in plausible[:2])
        d.new_comment = f"unresolved {today}: no institution match among {max(d.search_count, 0)} profiles; nearest {' / '.join(describe(c) for c in plausible[:2])}"
    else:
        d.evidence = f"no plausible profile among {max(d.search_count, 0)} search results"
        d.new_comment = f"unresolved {today}: no plausible profile among {max(d.search_count, 0)} search results"


# ----------------------------------------------------------------------------- main flow
def absorb(summaries: Dict[str, Dict[str, Any]], results: Iterable[Dict[str, Any]], d: Decision, topics: Sequence[str]) -> None:
    """Add search results to the candidate map (first sighting of an id wins)."""
    for r in results:
        s = profile_summary(r, d.institutions, topics)
        s["name_ok"] = name_agrees(d.name, r)
        summaries.setdefault(s["id"], s)


def conclusive(summaries: Dict[str, Dict[str, Any]], d: Decision, args: argparse.Namespace) -> bool:
    """True when the candidates so far already single out one profile (so no further query is needed)."""
    matching = sorted((s for s in summaries.values() if s["name_ok"] and s["institution_strength"] > 0 and s["works_count"] >= args.stub_works),
                      key=lambda c: (-c["cited_by_count"], -c["works_count"]))
    return bool(matching) and is_clear(matching[0], matching[1] if len(matching) > 1 else None, args) and (not d.old_id or d.old_id in summaries)


def gather(lines: Sequence[str], data: Dict[str, Any]) -> Tuple[List[Tuple[int, str, Dict[str, Any], str]], Dict[str, Decision], Dict[str, List[str]]]:
    """Author lines, one Decision per distinct name (union of subgroups/institutions) and topics per subgroup."""
    rows = author_lines(lines)
    topics_by_sg = {g.get("id"): [str(t) for t in (g.get("openalex_topics") or [])] for g in (data.get("subgroups") or []) if isinstance(g, dict)}
    decisions: Dict[str, Decision] = {}
    for _i, sg, a, comment in rows:
        name = str(a["name"])
        d = decisions.get(name)
        if d is None:
            d = Decision(name, [], [], str(a.get("openalex_id") or "").strip(), comment)
            decisions[name] = d
        if sg not in d.subgroups:
            d.subgroups.append(sg)
        inst = str(a.get("institution") or "")
        if inst and inst not in d.institutions:
            d.institutions.append(inst)
        cur = str(a.get("openalex_id") or "").strip()
        if cur and not d.old_id:
            d.old_id = cur
        if comment and not d.old_comment:
            d.old_comment = comment
        if cur and d.old_id and cur != d.old_id:
            d.flags.append(f"different ids across subgroups: {d.old_id} vs {cur}")
    return rows, decisions, topics_by_sg


def run(args: argparse.Namespace) -> int:
    today = dt.date.today().isoformat()
    config_path = Path(args.config)
    lines, data = load_config(config_path)
    rows, decisions, topics_by_sg = gather(lines, data)
    if args.names:
        wanted = {n.casefold() for n in args.names}
        decisions = {k: v for k, v in decisions.items() if k.casefold() in wanted}
    if not decisions:
        print("no tracked authors selected", file=sys.stderr)
        return 1

    load_env(Path(args.env) if args.env else None)
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    mailto = os.environ.get("LITRADAR_CONTACT_EMAIL", "").strip()
    if not api_key:
        print("warning: OPENALEX_API_KEY is not set; anonymous OpenAlex traffic is metered per IP and may answer 429", file=sys.stderr)
    http = Http(contact_email=mailto, cache_dir=Path(args.cache_dir) if args.cache_dir else None)
    client = Client(http, api_key, mailto, cache=bool(args.cache_dir))

    # 1. existing ids (and any ids mentioned in trailing comments, for the evidence) in batches
    comment_ids: Dict[str, List[str]] = {}
    for d in decisions.values():
        extra = [i for i in ANY_ID_RE.findall(d.old_comment or "") if i != d.old_id]
        if extra:
            comment_ids[d.name] = extra
    all_ids = [d.old_id for d in decisions.values() if d.old_id] + [i for v in comment_ids.values() for i in v]
    profiles = client.authors_by_ids(all_ids)

    # 2. search every name; add a topic-filtered search when the plain one is not conclusive
    for d in decisions.values():
        topics = list(dict.fromkeys(t for sg in d.subgroups for t in topics_by_sg.get(sg, [])))
        summaries: Dict[str, Dict[str, Any]] = {}
        results, count = client.search(d.name)
        d.search_count = count
        absorb(summaries, results, d, topics)
        # not conclusive, or more profiles than one page holds: narrow by the subgroup's topics, then drop initials
        if topics and (not conclusive(summaries, d, args) or count > 25):
            absorb(summaries, client.search(d.name, topics)[0], d, topics)
        for variant in name_variants(d.name)[1:]:
            if conclusive(summaries, d, args):
                break
            absorb(summaries, client.search(variant, topics)[0] if topics else client.search(variant)[0], d, topics)
        existing = None
        if d.old_id:
            p = profiles.get(d.old_id)
            if p is not None:
                existing = profile_summary(p, d.institutions, topics)
                existing["name_ok"] = name_agrees(d.name, p)
                summaries.setdefault(existing["id"], existing)
        # ids a human already wrote into the trailing comment ("split profile; also A...") are candidates too;
        # the name check is waived for them (a split often carries a mangled display name)
        for cid in comment_ids.get(d.name, []):
            p = profiles.get(cid)
            if p is None:
                d.flags.append(f"comment id {cid}: not found")
                continue
            s = summaries.get(short_id(p.get("id"))) or profile_summary(p, d.institutions, topics)
            s["name_ok"] = True
            s["from_comment"] = True
            summaries[s["id"]] = s
            d.flags.append(f"comment id {cid}: {s['display_name']}, {s['works_count']} works, {s['cited_by_count']:,} citations, {(s['institution_hit'] or 'no institution match')}")
        d.candidates = sorted(summaries.values(), key=lambda s: -score(s, s["name_ok"]))
        decide(d, existing, d.candidates, args, today)

    # 3. report
    order = ["corrected", "resolved", "suspicious", "ambiguous", "unresolved", "error", "verified"]
    counts = {k: 0 for k in order}
    for d in decisions.values():
        counts[d.status] = counts.get(d.status, 0) + 1
    print(f"# resolve_authors {today}: {len(rows)} author lines, {len(decisions)} distinct names, {client.calls} API calls"
          + (f", {http.stats['cache_hits']} cache hits" if http.stats["cache_hits"] else ""))
    print("# " + ", ".join(f"{k} {counts.get(k, 0)}" for k in order))
    for status in order:
        for d in sorted(decisions.values(), key=lambda x: x.name):
            if d.status != status:
                continue
            change = f"{d.old_id or '(empty)'} -> {d.new_id or '(empty)'}" if d.new_id != d.old_id else (d.new_id or "(empty)")
            orcid = f" orcid {d.orcid}" if d.orcid else ""
            print(f"[{status:<10}] {d.name} ({', '.join(d.subgroups)}): {change}{orcid}")
            print(f"             {d.evidence}")
            for f in d.flags:
                print(f"             ! {f}")
            if status in ("ambiguous", "unresolved", "suspicious", "corrected") or (status == "verified" and d.flags):
                for c in d.candidates[:4]:
                    print(f"             - {c['id']} {c['display_name']} | {c['works_count']} works | {c['cited_by_count']:,} cites | "
                          f"{(c['last_known'] or c['affiliations_recent'] or ['-'])[0]} | inst {c['institution_strength']} | topics {','.join(c['topic_overlap'][:2]) or '-'} | name {'ok' if c['name_ok'] else 'NO'}")
    if client.failures:
        print(f"# {len(client.failures)} request(s) failed:")
        for f in client.failures:
            print(f"#   {f}")

    # 4. live sanity: 2026 works for a few resolved/verified ids
    sanity: List[Dict[str, Any]] = []
    if args.sanity:
        pool = [d for d in decisions.values() if d.status in ("resolved", "corrected") and d.new_id]
        pool += [d for d in decisions.values() if d.status == "verified" and d.new_id]
        for d in pool[:args.sanity]:
            n = client.works_since(d.new_id, args.sanity_since)
            sanity.append({"name": d.name, "id": d.new_id, "status": d.status, "since": args.sanity_since, "works": n})
        print(f"# works since {args.sanity_since} for {len(sanity)} ids:")
        for s in sanity:
            print(f"#   {s['name']} {s['id']} ({s['status']}): {s['works'] if s['works'] is not None else 'query failed'}")
    print(f"# API calls this run: {client.calls}")

    if args.report:
        Path(args.report).write_text(json.dumps({
            "date": today, "config": str(config_path), "api_calls": client.calls, "counts": counts, "author_lines": len(rows),
            "decisions": [d.as_dict() for d in decisions.values()], "sanity": sanity, "failures": client.failures,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"# report written to {args.report}")
    if args.markdown:
        Path(args.markdown).write_text(render_markdown(decisions, rows, counts, sanity, today), encoding="utf-8")
        print(f"# markdown tables written to {args.markdown}")

    if not args.write:
        return 0
    if counts.get("error"):
        print("refusing to write: some configured ids could not be fetched; re-run when the API answers", file=sys.stderr)
        return 1
    new_lines = apply(lines, rows, decisions, counts, today)
    verify_rewrite(lines, new_lines)
    config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    changed = sum(1 for a, b in zip(lines, new_lines) if a != b) + abs(len(new_lines) - len(lines))
    print(f"# wrote {config_path} ({changed} line(s) changed)")
    return 0


def header_line(counts: Dict[str, int], n_lines: int, n_names: int, today: str, old: str) -> str:
    provenance = ""
    m = re.search(r"transcribed from research notes (?:on )?(\d{4}-\d{2}-\d{2})", old or "")
    if m:
        provenance = f"transcribed from research notes {m.group(1)}; "
    resolved, verified, corrected = counts.get("resolved", 0), counts.get("verified", 0), counts.get("corrected", 0)
    empty = counts.get("ambiguous", 0) + counts.get("unresolved", 0)
    return (f"# OpenAlex author ids: {provenance}resolved against the live API {today} with scripts/resolve_authors.py "
            f"({n_lines} author lines, {n_names} people: {resolved} resolved, {verified} verified, {corrected} corrected, "
            f"{counts.get('suspicious', 0)} suspicious kept, {empty} still empty; details in docs/researchers.md). "
            f"Run `litradar check-config` after editing.")


def apply(lines: Sequence[str], rows: Sequence[Tuple[int, str, Dict[str, Any], str]], decisions: Dict[str, Decision], counts: Dict[str, int], today: str) -> List[str]:
    out = list(lines)
    for i, _sg, a, _comment in rows:
        d = decisions.get(str(a["name"]))
        if d is None or d.status in ("error", ""):
            continue
        new_id = d.new_id if d.status in ("resolved", "corrected", "verified", "suspicious") else str(a.get("openalex_id") or "")
        if d.status in ("ambiguous", "unresolved"):
            new_id = ""
        orcid = d.orcid if new_id else ""
        comment = d.new_comment
        if comment is None and d.status == "verified":
            comment = None  # keep whatever is there
        out[i] = rewrite_author_line(out[i], new_id, orcid, comment)
    changed_ids = counts.get("resolved", 0) + counts.get("corrected", 0) + counts.get("suspicious", 0)
    hdr = header_line(counts, len(rows), len(decisions), today, out[0] if out else "")
    if out and HEADER_RE.match(out[0]):
        if changed_ids:  # a pass that changed nothing keeps the header that records the pass which did
            out[0] = hdr
    else:
        out.insert(0, hdr)
    return out


def verify_rewrite(old_lines: Sequence[str], new_lines: Sequence[str]) -> None:
    before = yaml.safe_load("\n".join(old_lines)) or {}
    after = yaml.safe_load("\n".join(new_lines)) or {}
    key = lambda doc: sorted((g.get("id"), a.get("name")) for g in doc.get("subgroups") or [] for a in g.get("tracked_authors") or [])  # noqa: E731
    if key(before) != key(after):
        raise SystemExit("refusing to write: the set of (subgroup, name) pairs changed")
    for g in after.get("subgroups") or []:
        seen = set()
        for a in g.get("tracked_authors") or []:
            aid = str(a.get("openalex_id") or "")
            if aid and not ID_RE.match(aid):
                raise SystemExit(f"refusing to write: bad id {aid!r} for {a.get('name')!r}")
            if aid and aid in seen:
                raise SystemExit(f"refusing to write: duplicate id {aid} in {g.get('id')}")
            if aid:
                seen.add(aid)
            orcid = str(a.get("orcid") or "")
            if orcid and not re.match(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", orcid):
                raise SystemExit(f"refusing to write: bad ORCID {orcid!r} for {a.get('name')!r}")
    for g_before, g_after in zip(before.get("subgroups") or [], after.get("subgroups") or []):
        for k in ("id", "name", "openalex_topics", "keywords"):
            if g_before.get(k) != g_after.get(k):
                raise SystemExit(f"refusing to write: subgroup field {k} changed in {g_before.get('id')}")


def render_markdown(decisions: Dict[str, Decision], rows: Sequence[Tuple[int, str, Dict[str, Any], str]], counts: Dict[str, int], sanity: List[Dict[str, Any]], today: str) -> str:
    def esc(s: Any) -> str:
        return str(s).replace("|", "\\|")

    out = [f"Generated {today} by `scripts/resolve_authors.py` ({len(rows)} author lines, {len(decisions)} people).", ""]
    out.append("| status | count |", )
    out.append("|---|---:|")
    for k in ("resolved", "verified", "corrected", "suspicious", "ambiguous", "unresolved", "error"):
        out.append(f"| {k} | {counts.get(k, 0)} |")
    out.append("")
    out.append("### Changed ids (resolved and corrected)")
    out.append("")
    out.append("| name | subgroup(s) | old id | new id | ORCID | evidence |")
    out.append("|---|---|---|---|---|---|")
    for d in sorted(decisions.values(), key=lambda x: (x.status != "corrected", x.name)):
        if d.status in ("resolved", "corrected"):
            out.append(f"| {esc(d.name)} | {', '.join(d.subgroups)} | {d.old_id or '(empty)'} | {d.new_id} | {d.orcid or '-'} | {esc(d.evidence)} |")
    out.append("")
    out.append("### Still empty")
    out.append("")
    out.append("| name | subgroup(s) | status | reason |")
    out.append("|---|---|---|---|")
    for d in sorted(decisions.values(), key=lambda x: x.name):
        if d.status in ("ambiguous", "unresolved"):
            out.append(f"| {esc(d.name)} | {', '.join(d.subgroups)} | {d.status} | {esc(d.evidence)} |")
    out.append("")
    out.append("### Kept but suspicious")
    out.append("")
    out.append("| name | subgroup(s) | id | why |")
    out.append("|---|---|---|---|")
    for d in sorted(decisions.values(), key=lambda x: x.name):
        if d.status == "suspicious":
            out.append(f"| {esc(d.name)} | {', '.join(d.subgroups)} | {d.new_id} | {esc(d.evidence)} |")
    out.append("")
    out.append("### Verified ids")
    out.append("")
    out.append("| name | subgroup(s) | id | works | citations | institution evidence | ORCID | notes |")
    out.append("|---|---|---|---|---:|---|---|---|")
    for d in sorted(decisions.values(), key=lambda x: x.name):
        if d.status == "verified" and d.chosen:
            c = d.chosen
            inst = c["institution_hit"] + (" (last known)" if c["institution_strength"] >= 2 else " (affiliation)")
            notes = "; ".join(f for f in d.flags)
            out.append(f"| {esc(d.name)} | {', '.join(d.subgroups)} | {d.new_id} | {c['works_count']} | {c['cited_by_count']:,} | {esc(inst)} | {d.orcid or '-'} | {esc(notes)} |")
    if sanity:
        out.append("")
        out.append(f"### Works since {sanity[0]['since']} (live check)")
        out.append("")
        out.append("| name | id | status | works |")
        out.append("|---|---|---|---:|")
        for s in sanity:
            out.append(f"| {esc(s['name'])} | {s['id']} | {s['status']} | {s['works'] if s['works'] is not None else 'query failed'} |")
    out.append("")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the proposals and change nothing")
    mode.add_argument("--write", action="store_true", help="apply the proposals to the config file")
    p.add_argument("--config", default=str(ROOT / "config" / "subgroups.yaml"), help="path to subgroups.yaml")
    p.add_argument("--env", default="", help=".env file to load OPENALEX_API_KEY / LITRADAR_CONTACT_EMAIL from (default: the repository's .env)")
    p.add_argument("--cache-dir", default=str(ROOT / ".cache" / "resolve-authors"), help="disk cache for API responses ('' to disable)")
    p.add_argument("--names", nargs="*", help="only these tracked names (default: all)")
    p.add_argument("--report", default="", help="write every decision with its candidates to this JSON file")
    p.add_argument("--markdown", default="", help="write the change / unresolved / verified tables to this Markdown file")
    p.add_argument("--sanity", type=int, default=0, help="query the works count since --sanity-since for this many resolved/verified ids")
    p.add_argument("--sanity-since", default=f"{dt.date.today().year}-01-01", help="start date for the sanity works count")
    p.add_argument("--stub-works", type=int, default=10, help="profiles with fewer works are stubs, never chosen (default 10)")
    p.add_argument("--merged-works", type=int, default=3000, help="profiles with this many works or more are flagged as merged (default 3000)")
    p.add_argument("--split-ratio", type=float, default=5.0, help="the best institution-matching profile must have this many times the works of the next to be chosen (default 5)")
    args = p.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
