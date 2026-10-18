"""Cheap, transparent scoring. Every point has a reason string so the digest can show its work.

Ranking v2. Score components (defaults live in `DEFAULT_WEIGHTS`; override any key from config/radar.yaml ->
ranking.weights). Every component is exposed in the reasons string as `label (+points)`, and
`score_paper_v2` returns them as structured `Component`s so the digest can build a "why it matters" line
without a language model.

  venue prior         tier A +5.0, tier B +3.0, tier C +1.0, preprint +1.0, unknown venue +0.5. Tier A/B is withheld (floor +0.5)
                      when no topical signal fires (`topical_gate`). A preprint whose
                      `published_doi` resolves to a stored journal article takes that journal's tier instead.
  first platform      +1.0 for the FIRST platform entity named (sequencers, single-cell, spatial, proteomics,
                      cryo-EM, arrays, synthesis, cell engineering); further platforms are listed, not scored
                      (instrument_cap = 1), so a Methods section that names six machines no longer outranks a
                      paper that introduces one.
  platform central    +1.0 when a platform is named in the title or abstract (the paper is about it, not merely
                      run on it)
  lab tools           +0.2 per distinct infrastructure entity (cap 3): QC, library prep, PCR, automation, imaging, ...;
                      entities in the `software` category (analysis software, structure-prediction models) are listed
                      but never scored, as supplier involvement or otherwise
  companies           +0.5 for the FIRST commercial company named (company_cap = 1); institutes, foundations,
                      plasmid repositories and AI tools / software credits are listed but never scored
  company author      +1.0 if an author has a commercial-company affiliation
  watchlist terms     +1.0 per matched watchlist term (cap 3)
  tracked author      +3.0 when matched by OpenAlex author id; +1.5 for a name-only match (`tracked_author` may be
                      True / "openalex_id" for an exact match or "name" for a name match)
  notice penalty      -4.0 for "Author Correction: ...", "Erratum", "Retraction" and the like (NOTICE_RE) and for
                      preprints whose record type is "withdrawn": they carry the venue's tier but are not papers
  novelty language    +0.5 per cue (cap 1.0)
  new platform cue    +1.5 when the title/abstract says the paper introduces an artefact: "we present / introduce /
                      develop ... a platform | method | assay | sequencer | instrument | ..." (NEW_PLATFORM_RE)
  citations           age-aware. Within `early_citation_days` (90) of publication: +1.0 once cited, +0.5 more when
                      the velocity is at least `velocity_per_week` citations per week. Older than that (backfilled
                      corpora, multi-week windows): nothing for being cited, +1.0 only when the paper runs at
                      `velocity_per_month` (3) citations per month or more since publication. Unknown age: +1.0 once
                      cited. +0.5 more when Semantic Scholar counts an influential citation
                      (extra_json.semantic_scholar.influential_citation_count); capped at citations_cap = 2.0
  preprint published  +1.0 for a preprint that carries a `published_doi`
  news co-mention     +1.0 when this window's news tagged a company the paper is about or authored from (title,
                      abstract, affiliations), or the vendor of a platform it names, or a headline names one of its
                      platforms (RankContext.news_*); a reagent supplier named only in the Methods does not count
  first concept       +1.0 when a concept the paper carries makes its first appearance in the corpus this window
                      (concept_counts / concept_papers), once the corpus holds enough weeks of counts
  full text parsed    +0 by default (data availability is not a quality signal)

The raw sum is scaled to 0-10 (`scale_max_raw`, default 16) for blending with the optional LLM score.

`score_paper(...)` keeps the historical `(scaled, reasons)` contract used by `Pipeline.rank`; the pipeline passes
no context, so there the store-derived signals (news, first concept, published venue) are simply absent.
`rank.rerank.rerank_window` scores the digest window with the full record and context and persists the result.
`score_paper(..., company_types=)` takes the {company id: type} mapping from config/companies.yaml; without it
every company entity is treated as commercial.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

DEFAULT_WEIGHTS = {
    # venue prior (tier from config/journals.yaml)
    "tier_A": 5.0,
    "tier_B": 3.0,
    "topical_gate": True,           # venue prior is withheld when no topical signal (platform, company, watchlist, tracked author, concept) fires
    "tier_C": 1.0,
    "tier_preprint": 1.0,
    "tier_unknown": 0.5,
    # what the paper names
    "instrument": 1.0,          # per scored PLATFORM entity; only the first is scored (instrument_cap)
    "instrument_cap": 1,
    "platform_central": 1.0,    # a platform named in the title or abstract
    "instrument_infra": 0.2,    # per distinct lab-infrastructure entity (qc, library prep, pcr, automation, imaging, ...)
    "instrument_infra_cap": 3,
    "company": 0.5,             # per scored commercial company; only the first is scored (company_cap)
    "company_cap": 1,
    "company_author": 1.0,
    # what the reader asked for
    "watchlist": 1.0,
    "watchlist_cap": 3,
    "tracked_author": 3.0,          # matched by OpenAlex author id (exact)
    "tracked_author_name": 1.5,     # matched by name only (a common name is weak evidence of identity)
    # editorial noise
    "notice_penalty": -4.0,         # "Author Correction: ...", "Erratum", "Retraction", ... (NOTICE_RE)
    # novelty language and the new-platform cue
    "novelty": 0.5,
    "novelty_cap": 1.0,
    "new_platform": 1.5,
    # attention signals (age-aware: a backfilled 2024 paper with hundreds of citations is not "early")
    "early_citations": 1.0,         # cited at all within early_citation_days of publication (or when the age is unknown)
    "early_citation_days": 90,
    "citation_velocity": 0.5,       # ... and fast: at least velocity_per_week citations per week since publication
    "velocity_per_week": 1.0,
    "citation_velocity_old": 1.0,   # older than early_citation_days: scored only on citations per month since publication
    "velocity_per_month": 3.0,
    "influential_citations": 0.5,
    "citations_cap": 2.0,
    "preprint_published": 1.0,
    "news_comention": 1.0,
    "first_concept": 1.0,
    "fulltext": 0.0,            # having full text is a data-availability artefact, not a quality signal
    "scale_max_raw": 16.0,
}

PLATFORM_CATEGORIES = ("sequencer", "single_cell", "spatial", "proteomics", "cryo_em", "array", "synthesis", "cell_engineering")
SOFTWARE_CATEGORIES = ("software",)  # analysis software and models: listed, never scored
# company `type` values (config/companies.yaml) that are not commercial signals: research institutes, foundations,
# non-profit repositories ... and the AI tools / software credited for language polishing or structure prediction
INSTITUTE_COMPANY_TYPES = ("institute", "foundation", "non_profit", "nonprofit", "non-profit")
AI_TOOL_COMPANY_TYPES = ("software", "ai_tool", "ai-tool", "ai tool", "llm")
SUBJECT_SECTIONS = ("title", "abstract")
TIERS = ("A", "B", "C")


def is_platform(category: str) -> bool:
    """Platforms are the instruments an investor tracks; the rest is lab infrastructure and consumables."""
    return category.startswith(PLATFORM_CATEGORIES)


def company_role(entity_id: str, company_types: Optional[Dict[str, str]]) -> str:
    """'company' (commercial, scored), 'institute' or 'ai_tool' (listed, not scored) for a company mention."""
    t = ((company_types or {}).get(entity_id) or "").lower()
    if t in INSTITUTE_COMPANY_TYPES:
        return "institute"
    if t in AI_TOOL_COMPANY_TYPES:
        return "ai_tool"
    return "company"


NOVELTY_CUES = [
    r"\bfirst\b", r"\bnovel\b", r"\bwe present\b", r"\bwe introduce\b", r"\bwe develop(ed)?\b", r"\bnew (method|platform|approach|technology)\b",
    r"\bscalable\b", r"\bhigh[- ]throughput\b", r"\bsingle[- ]molecule\b", r"\bgenome[- ]wide\b", r"\batlas\b", r"\bfoundation model\b",
]
NOVELTY_RE = re.compile("|".join(NOVELTY_CUES), re.I)

# ---- "new platform introduced" cue ---------------------------------------------------------------------------
# A paper that *introduces* an artefact, not one that merely used one. Three shapes:
#   "we present | introduce | develop(ed) | describe | report | propose | establish | engineer | build | create | design
#    ... a platform | method | assay | sequencer | instrument | ..."            (the object within 80 characters)
#   "NAME: a <...> method ..."  (title shape)        "NAME is a <...> platform"  (abstract shape)
# The span between verb and artefact may not contain a clause marker (that / which / how / whether) or a usage
# preposition (using / with / on / via / through / by), so "we present an analysis using the Visium platform" and
# "we report that the method fails" do not count.
_INTRO_SUBJECT = r"(?:here,?\s+)?(?:we|this\s+(?:study|work|paper|article|report))"
_INTRO_VERBS = (
    r"(?:present|introduce|describe|report|develop|propose|establish|engineer|build|built|"
    r"create|design|devise|implement|construct)(?:s|d|ed)?"
)
ARTEFACT_NOUNS = (
    r"(platform|method(?:ology)?|assay|sequencer|instrument|technolog(?:y|ies)|technique|pipeline|toolkit|toolbox|"
    r"tool|workflow|protocol|chemistry|device|framework|algorithm|software|resource|atlas|panel|kit|probe|reagent|"
    r"sensor|microscope|spectrometer|chip|array)"
)
NEW_PLATFORM_RE = re.compile(
    rf"\b{_INTRO_SUBJECT}\s+(?:(?:have|here|now|therefore|thus|first|also|then|recently)\s+){{0,2}}{_INTRO_VERBS}\b(?P<mid>[^.;:!?]{{0,80}}?)\b{ARTEFACT_NOUNS}s?\b",
    re.I,
)
NEW_PLATFORM_TITLE_RE = re.compile(
    rf"^\s*[A-Za-z][\w\-\.\+]*\s*(?::|-|–|—)\s+(?:a|an)\s+(?:[\w\-]+\s+){{0,5}}{ARTEFACT_NOUNS}\b", re.I
)
NEW_PLATFORM_IS_RE = re.compile(
    rf"\b[A-Z][\w\-\+]{{1,}}\s+is\s+(?:a|an)\s+(?:[\w\-]+\s+){{0,5}}{ARTEFACT_NOUNS}\b"
)
_CLAUSE_OR_USAGE_RE = re.compile(r"\b(that|which|how|whether|using|with|on|via|through|by|from|in)\b", re.I)
# correction / retraction notices carry the venue's tier but are not papers
NOTICE_RE = re.compile(r"^\s*(?:author |publisher |editor'?s? )?(?:correction|corrigendum|erratum|errata|retraction|addendum|expression of concern)\b", re.I)
TRACKED_EXACT = (True, "openalex_id", "id", "exact")


def new_platform_cue(title: str, abstract: str = "") -> Optional[str]:
    """The artefact noun ("platform", "assay", ...) when the text says the paper introduces one, else None.
    The title is read first ("NAME: a ... method", "NAME is a ... platform"), then the abstract."""
    title = title or ""
    for rx in (NEW_PLATFORM_TITLE_RE, NEW_PLATFORM_IS_RE):
        m = rx.search(title)
        if m:
            return m.group(m.lastindex).lower()  # the artefact group is the last one
    text = f"{title}. {abstract or ''}"
    for m in NEW_PLATFORM_RE.finditer(text):
        if _CLAUSE_OR_USAGE_RE.search(m.group("mid") or ""):
            continue
        return m.group(m.lastindex).lower()
    m = NEW_PLATFORM_IS_RE.search(abstract or "")
    if m:
        return m.group(m.lastindex).lower()
    return None


def citation_age_days(pub_date: str, as_of: str = "") -> Optional[int]:
    """Days between `pub_date` and `as_of` (default: today, UTC); None when either date is unusable."""
    try:
        d0 = date.fromisoformat(str(pub_date or "")[:10])
        d1 = date.fromisoformat(str(as_of)[:10]) if as_of else datetime.now(timezone.utc).date()
    except ValueError:
        return None
    return (d1 - d0).days


# ---- structured result -----------------------------------------------------------------------------------------
@dataclass
class Component:
    """One scored (or listed) signal. `points` is 0 for signals that are shown but never scored."""

    key: str
    label: str
    points: float = 0.0
    detail: Any = None

    def reason(self) -> str:
        if not self.points:
            return self.label
        return f"{self.label} ({'+' if self.points > 0 else '−'}{abs(self.points):.1f})"


@dataclass
class RankContext:
    """What the store knows about the window beyond the paper itself (built by `rank.context.build_context`)."""

    as_of: str = ""                                                   # YYYY-MM-DD for citation age; '' = today
    news_company_ids: Set[str] = field(default_factory=set)           # company ids tagged on news items in the window
    news_company_names: Set[str] = field(default_factory=set)         # their lower-case display names (platform vendors match by name)
    news_headlines: str = ""                                          # lower-case headlines, newline-joined
    news_items: int = 0
    first_concepts: Dict[str, List[str]] = field(default_factory=dict)  # paper id -> concept displays first seen now
    concept_history_weeks: int = 0                                    # weeks of concept counts before the window
    concept_history_ok: bool = False                                  # enough history for "first in corpus" to mean it
    published_venues: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # preprint id -> (journal, tier)


@dataclass
class ScoreResult:
    raw: float
    scaled: float
    components: List[Component]
    facts: Dict[str, Any] = field(default_factory=dict)

    @property
    def reasons(self) -> str:
        return "; ".join(c.reason() for c in self.components)

    def component(self, key: str) -> Optional[Component]:
        return next((c for c in self.components if c.key == key), None)

    def as_dict(self) -> Dict[str, Any]:
        return {"raw": round(self.raw, 3), "scaled": self.scaled, "components": [asdict(c) for c in self.components]}


def _watchlist_hits(text: str, terms: Iterable[str]) -> List[str]:
    hits = []
    low = text.lower()
    for t in terms:
        if t.lower() in low:
            hits.append(t)
    return hits


def _extra(paper: Dict[str, Any]) -> Dict[str, Any]:
    ex = paper.get("extra")
    if isinstance(ex, dict):
        return ex
    raw = paper.get("extra_json")
    if raw:
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


def _name(m: Dict[str, Any]) -> str:
    return str(m.get("entity_name") or m.get("entity_id") or "")


def _ordered(names: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(n for n in names if n))


def _news_hits(platforms: Sequence[Dict[str, Any]], subject_company_ids: Iterable[str], company_names: Dict[str, str], ctx: RankContext) -> List[str]:
    """Names that connect the paper to this window's news: a company the paper is about or authored from (title,
    abstract, affiliations) that the news tagged; the vendor of a platform the paper names; a platform named in a
    headline. A supplier named only in the Methods (a reagent brand) does not count: that is a pipette, not a story."""
    hits: List[str] = []
    for cid in subject_company_ids:
        if cid in ctx.news_company_ids:
            hits.append(company_names.get(cid) or cid)
    for p in platforms:
        vendor = str(p.get("vendor") or "")
        if vendor and vendor.lower() in ctx.news_company_names:
            hits.append(vendor)
    if ctx.news_headlines:
        for p in platforms:
            name = str(p.get("name") or "")
            if len(name) >= 4 and re.search(r"(?<![\w-])" + re.escape(name.lower()) + r"(?![\w-])", ctx.news_headlines):
                hits.append(name)
    return _ordered(hits)


# signals that tie a paper to the radar's subject matter. A tier-A/B venue prior alone must not carry an off-topic
# paper (weather forecasting in a tier-A journal) into the shortlist, and neither should a big-tech affiliation,
# a generic first-seen n-gram or a bare "we introduce a new framework" cue: those fire on any field.
TOPICAL_KEYS = frozenset({"platforms", "platform_central", "lab_tools", "watchlist", "tracked_author"})
NON_TOPICAL_COMPANY_TYPES = frozenset({"software"})


def _is_topical(comps: Sequence[Component], facts: Dict[str, Any], types: Dict[str, str]) -> bool:
    if any(c.key in TOPICAL_KEYS for c in comps):
        return True
    for cid in list(facts.get("companies") or []) + list(facts.get("author_companies") or []):
        if types.get(str(cid).lower(), "") not in NON_TOPICAL_COMPANY_TYPES:
            return True  # a tools, diagnostics, pharma, CRO or ai_bio company named or affiliated
    return False


def score_paper_v2(
    paper: Dict[str, Any],
    mentions: Sequence[Dict[str, Any]],
    journal_tier: str,
    watchlist_terms: Sequence[str],
    tracked_author: Any,
    weights: Optional[Dict[str, Any]] = None,
    company_types: Optional[Dict[str, str]] = None,
    context: Optional[RankContext] = None,
) -> ScoreResult:
    """Score one paper and return every component. `paper` is a `papers` row (or the minimal dict the pipeline
    builds: kind, title, abstract, cited_by_count, fulltext_available); `id`, `pub_date`, `published_doi` and
    `extra` / `extra_json` unlock the citation-velocity, conversion, news and first-concept signals."""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    types = {k: (v or "").lower() for k, v in (company_types or {}).items()}
    ctx = context
    comps: List[Component] = []
    facts: Dict[str, Any] = {}
    kind = paper.get("kind") or "article"
    pid = str(paper.get("id") or "")
    extra = _extra(paper)

    # ---- venue prior
    published = ctx.published_venues.get(pid) if ctx else None
    facts["kind"] = kind
    if kind == "preprint" and published and published[1] in TIERS:
        jname, jt = published
        comps.append(Component("venue", f"journal tier {jt} via published version {jname}", w[f"tier_{jt}"], {"journal": jname, "tier": jt}))
        facts.update(tier=jt, published_in=jname)
    elif kind == "preprint":
        comps.append(Component("venue", "preprint", w["tier_preprint"], {"tier": "", "preprint": True}))
        facts["tier"] = ""
    else:
        ranked = journal_tier in TIERS
        comps.append(Component("venue", f"journal tier {journal_tier}" if ranked else "journal unranked",
                               w[f"tier_{journal_tier}"] if ranked else w["tier_unknown"], {"tier": journal_tier if ranked else ""}))
        facts["tier"] = journal_tier if ranked else ""

    # ---- entities named
    plat_by_id: Dict[str, Dict[str, Any]] = {}
    infra_ids: List[str] = []
    software_ids: List[str] = []
    for m in mentions:
        if m.get("kind") != "instrument":
            continue
        eid = str(m.get("entity_id") or "")
        category = m.get("category") or ""
        if is_platform(category):
            slot = plat_by_id.setdefault(eid, {"name": _name(m), "vendor": m.get("vendor") or "", "central": False, "count": 0, "order": len(plat_by_id)})
            slot["count"] += int(m.get("count") or 1)
            if m.get("section") in SUBJECT_SECTIONS:
                slot["central"] = True
        elif category.startswith(SOFTWARE_CATEGORIES):
            software_ids.append(eid)
        else:
            infra_ids.append(eid)
    platforms = sorted(plat_by_id.values(), key=lambda s: (not s["central"], -s["count"], s["order"]))
    platform_names = _ordered(p["name"] for p in platforms)
    infra = _ordered(infra_ids)
    software = _ordered(software_ids)
    company_mentions = [m for m in mentions if m.get("kind") == "company"]
    company_names: Dict[str, str] = {}
    companies: List[str] = []
    institutes: List[str] = []
    ai_tools: List[str] = []
    author_companies: List[str] = []
    subject_companies: List[str] = []  # named in title / abstract / affiliations: the paper is about them or from them
    for m in company_mentions:
        eid = str(m.get("entity_id") or "")
        company_names.setdefault(eid, _name(m))
        role = company_role(eid, types)
        if role == "company":
            companies.append(eid)
            if m.get("section") == "affiliations":
                author_companies.append(eid)
            if m.get("section") in SUBJECT_SECTIONS + ("affiliations",):
                subject_companies.append(eid)
        elif role == "institute":
            institutes.append(eid)
        else:
            ai_tools.append(eid)
    companies, institutes, ai_tools, author_companies = _ordered(companies), _ordered(institutes), _ordered(ai_tools), _ordered(author_companies)
    facts.update(platforms=platform_names, companies=[company_names.get(c, c) for c in companies],
                 author_companies=[company_names.get(c, c) for c in author_companies])

    if platforms:
        n = min(len(platforms), int(w["instrument_cap"]))
        label = f"{len(platforms)} platform(s)" + (f", scored {n}" if len(platforms) > n else "") + ": " + ", ".join(platform_names[:3])
        comps.append(Component("platforms", label, n * w["instrument"], platform_names))
        central = [p["name"] for p in platforms if p["central"]]
        if central and w["platform_central"]:
            comps.append(Component("platform_central", "platform named in title/abstract: " + ", ".join(central[:2]), w["platform_central"], central))
            facts["central_platforms"] = central
    if infra:
        n = min(len(infra), int(w["instrument_infra_cap"]))
        comps.append(Component("lab_tools", f"{len(infra)} lab tool(s)", n * w["instrument_infra"], len(infra)))
    if software:
        comps.append(Component("software", f"{len(software)} software / model(s), not scored", 0.0, len(software)))
    if companies:
        n = min(len(companies), int(w["company_cap"]))
        label = f"{len(companies)} company mention(s)" + (f", scored {n}" if len(companies) > n else "")
        comps.append(Component("companies", label, n * w["company"], facts["companies"]))
    if institutes:
        comps.append(Component("institutes", f"{len(institutes)} institute mention(s), not scored", 0.0, [company_names.get(c, c) for c in institutes]))
    if ai_tools:
        comps.append(Component("ai_tools", f"{len(ai_tools)} AI tool(s), not scored", 0.0, [company_names.get(c, c) for c in ai_tools]))
    if author_companies:
        comps.append(Component("company_author", "company-affiliated author", w["company_author"], facts["author_companies"]))

    # ---- what the reader asked for
    text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}"
    wl = _watchlist_hits(text, watchlist_terms)
    if wl:
        n = min(len(wl), int(w["watchlist_cap"]))
        comps.append(Component("watchlist", "watchlist: " + ", ".join(wl[:4]), n * w["watchlist"], wl))
        facts["watchlist"] = wl
    if tracked_author:
        exact = tracked_author in TRACKED_EXACT
        comps.append(Component("tracked_author", "tracked author" + ("" if exact else " (name match)"),
                               w["tracked_author"] if exact else w["tracked_author_name"], "openalex_id" if exact else "name"))
        facts["tracked_exact"] = exact

    # ---- novelty language and the new-platform cue
    cues = len(NOVELTY_RE.findall(text))
    if cues:
        comps.append(Component("novelty", f"{cues} novelty cue(s)", min(cues * w["novelty"], w["novelty_cap"]), cues))
    noun = new_platform_cue(paper.get("title") or "", paper.get("abstract") or "") if w["new_platform"] else None
    if noun:
        comps.append(Component("new_platform", f"introduces a new {noun}", w["new_platform"], noun))
        facts["new_platform"] = noun

    # ---- attention: citations, conversion, news, first concept
    cited = int(paper.get("cited_by_count") or 0)
    if cited > 0:
        detail: Dict[str, Any] = {"cited": cited}
        age = citation_age_days(paper.get("pub_date") or "", ctx.as_of if ctx else "")
        if age is None or age < 0:
            pts = w["early_citations"]
            label = f"already cited {cited}x"
        elif age <= int(w["early_citation_days"]):
            pts = w["early_citations"]
            velocity = cited / max(1.0, age / 7.0)
            detail.update(age_days=age, per_week=round(velocity, 2))
            label = f"already cited {cited}x in {age} days"
            if velocity >= float(w["velocity_per_week"]):
                pts += w["citation_velocity"]
                detail["fast"] = True
        else:
            months = max(1.0, age / 30.0)
            per_month = cited / months
            detail.update(age_days=age, per_month=round(per_month, 2))
            label = f"cited {cited}x over {months:.0f} months ({per_month:.1f}/month)"
            pts = 0.0
            if per_month >= float(w["velocity_per_month"]):
                pts = w["citation_velocity_old"]
                detail["fast"] = True
        s2 = extra.get("semantic_scholar") if isinstance(extra.get("semantic_scholar"), dict) else {}
        influential = int((s2 or {}).get("influential_citation_count") or 0)
        if influential > 0:
            pts += w["influential_citations"]
            label += f", {influential} influential"
            detail["influential"] = influential
        if pts <= 0:
            label += ", not scored (slow for its age)"
        detail["scored"] = pts > 0
        comps.append(Component("citations", label, min(pts, w["citations_cap"]), detail))
        facts["citations"] = detail
    if kind == "preprint" and str(paper.get("published_doi") or "").strip():
        label = "preprint published as a journal article" + (f" ({published[0]})" if published else "")
        comps.append(Component("preprint_published", label, w["preprint_published"], str(paper.get("published_doi")).strip()))
    if ctx is not None and (ctx.news_company_ids or ctx.news_headlines):
        hits = _news_hits(platforms, _ordered(subject_companies), company_names, ctx)
        if hits:
            comps.append(Component("news_comention", "in the news this window: " + ", ".join(hits[:3]), w["news_comention"], hits))
            facts["news"] = hits
    if ctx is not None and ctx.concept_history_ok and pid and ctx.first_concepts.get(pid):
        terms = _ordered(ctx.first_concepts[pid])
        comps.append(Component("first_concept", "first corpus appearance: " + ", ".join(f"'{t}'" for t in terms[:3]), w["first_concept"], terms))
        facts["first_concepts"] = terms
    if paper.get("fulltext_available") and w["fulltext"]:
        comps.append(Component("fulltext", "full text parsed", w["fulltext"], True))
    if w["notice_penalty"]:
        if NOTICE_RE.search(paper.get("title") or ""):
            comps.append(Component("notice", "correction / retraction notice", float(w["notice_penalty"]), "title"))
            facts["notice"] = True
        elif str(extra.get("type") or "").lower() == "withdrawn":  # bioRxiv marks withdrawn preprints in the record type
            comps.append(Component("notice", "withdrawn preprint", float(w["notice_penalty"]), "withdrawn"))
            facts["notice"] = True

    # ---- topical gate
    topical = _is_topical(comps, facts, types)
    facts["topical"] = topical
    if w.get("topical_gate", True) and not topical:
        floor = float(w["tier_unknown"])
        for i, c in enumerate(comps):
            if c.key == "venue" and c.points > floor and isinstance(c.detail, dict) and c.detail.get("tier") in ("A", "B"):
                comps[i] = Component("venue", c.label + "; prior withheld: no platform, lab tool, watchlist term, tracked author or life-science company",
                                     floor, c.detail)

    raw = sum(c.points for c in comps)
    scaled = max(0.0, min(10.0, 10.0 * raw / float(w["scale_max_raw"])))
    return ScoreResult(raw=raw, scaled=round(scaled, 2), components=comps, facts=facts)


def score_paper(
    paper: Dict[str, Any],
    mentions: Sequence[Dict[str, Any]],
    journal_tier: str,
    watchlist_terms: Sequence[str],
    tracked_author: Any,
    weights: Optional[Dict[str, Any]] = None,
    company_types: Optional[Dict[str, str]] = None,
    context: Optional[RankContext] = None,
) -> Tuple[float, str]:
    """`(scaled score, reasons)`: the contract `Pipeline.rank` and `litradar rematch` rely on."""
    res = score_paper_v2(paper, mentions, journal_tier, watchlist_terms, tracked_author, weights, company_types, context)
    return res.scaled, res.reasons


def blend(heuristic: float, llm_score: Optional[float] = None, llm_weight: float = 0.6) -> float:
    if llm_score is None:
        return round(heuristic, 2)
    return round((1 - llm_weight) * heuristic + llm_weight * llm_score, 2)
