"""Ranking v2: every component, the caps, the context signals, the template explanation, diversity and the
render integration — offline, on temporary stores."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from litradar.digest.render import render
from litradar.models import Mention, NewsItem, Paper
from litradar.rank import heuristics as H
from litradar.rank.context import build_context, journal_tier_for, journal_tiers
from litradar.rank.diversity import diversify
from litradar.rank.explain import why_it_matters
from litradar.rank.rerank import _author_key, cluster_related, rerank_window
from litradar.rank.subgroups import assign_subgroups, primary_subgroup
from litradar.store import Store

CONFIG = Path(__file__).resolve().parent.parent / "config"
WINDOW = ("2026-10-10", "2026-10-17")
BASE = {"kind": "article", "title": "A paper", "abstract": "About things.", "cited_by_count": 0, "fulltext_available": True}


def _m(kind, eid, section, category="", name=None, count=1):
    return {"kind": kind, "entity_id": eid, "entity_name": name or eid, "section": section, "category": category, "count": count}


def _points(res: H.ScoreResult, key: str) -> float:
    c = res.component(key)
    return c.points if c else 0.0


# ---- caps: verbosity no longer wins ---------------------------------------------------------------------------
def test_only_the_first_platform_and_company_are_scored():
    one = [_m("instrument", "xenium", "methods", "spatial", "Xenium")]
    six = one + [_m("instrument", f"p{i}", "methods", "sequencer.short_read", f"Platform {i}") for i in range(5)]
    r1 = H.score_paper_v2(BASE, one, "B", [], False)
    r6 = H.score_paper_v2(BASE, six, "B", [], False)
    assert r1.scaled == r6.scaled and _points(r6, "platforms") == H.DEFAULT_WEIGHTS["instrument"]
    assert r6.component("platforms").label.startswith("6 platform(s), scored 1: Xenium, Platform 0, Platform 1")
    comps = [_m("company", f"c{i}", "methods") for i in range(23)]
    rc = H.score_paper_v2(BASE, comps, "B", [], False)
    assert _points(rc, "companies") == H.DEFAULT_WEIGHTS["company"] and "23 company mention(s), scored 1" in rc.reasons
    # lab tools keep their small capped reward
    tools = [_m("instrument", f"t{i}", "methods", "qc") for i in range(8)]
    rt = H.score_paper_v2(BASE, tools, "B", [], False)
    assert _points(rt, "lab_tools") == pytest.approx(3 * 0.2) and "8 lab tool(s) (+0.6)" in rt.reasons
    # the stacked verbose paper (6 platforms, 17 tools, 23 companies, company author) earns exactly what a paper naming
    # one platform, three tools and one company with a company author earns ...
    verbose = H.score_paper_v2(BASE, six + tools + comps + [_m("company", "c0", "affiliations")], "B", [], False)
    minimal = H.score_paper_v2(BASE, one + tools[:3] + comps[:1] + [_m("company", "c0", "affiliations")], "B", [], False)
    assert verbose.raw == minimal.raw == pytest.approx(3.0 + 1.0 + 0.6 + 0.5 + 1.0)
    # ... and no longer beats a tier-A paper on a watchlist topic with novelty language, nor a preprint that introduces a platform
    relevant_a = H.score_paper_v2(dict(BASE, abstract="We present a novel single-cell atlas."), [], "A", ["single-cell"], False)
    new_pre = H.score_paper_v2(dict(BASE, kind="preprint", abstract="Here we present a scalable assay for spatial proteomics of single cells."),
                               [_m("instrument", "xenium", "abstract", "spatial", "Xenium")], "", ["spatial proteomics", "single-cell"], False)
    assert verbose.raw < relevant_a.raw and verbose.raw < new_pre.raw


def test_tracked_author_name_match_is_weaker_and_notices_are_penalised():
    exact = H.score_paper_v2(BASE, [], "A", [], True)
    by_id = H.score_paper_v2(BASE, [], "A", [], "openalex_id")
    by_name = H.score_paper_v2(BASE, [], "A", [], "name")
    assert exact.raw == by_id.raw == 5.0 + 3.0 and "tracked author (+3.0)" in exact.reasons
    assert by_name.raw == 5.0 + 1.5 and "tracked author (name match) (+1.5)" in by_name.reasons and by_name.facts["tracked_exact"] is False
    assert why_it_matters(by_name, dict(BASE, journal="Nature")) == "Nature paper; by a tracked author (name match, not verified by id)"
    notice = H.score_paper_v2(dict(BASE, title="Author Correction: Parkinson's disease as a network disorder"), [], "A", [], "name")
    assert notice.raw == 5.0 + 1.5 - 4.0 and "correction / retraction notice (−4.0)" in notice.reasons
    assert "correction / retraction notice, not a study" in why_it_matters(notice, dict(BASE, journal="Nature"))
    for t in ("Publisher Correction: a paper", "Erratum for X", "Retraction: Y", "Corrigendum to Z", "Editor's Correction"):
        assert H.NOTICE_RE.search(t), t
    assert not H.NOTICE_RE.search("Correcting batch effects in single-cell data")
    floor = H.score_paper_v2(dict(BASE, title="Erratum", kind="preprint"), [], "", [], False)
    assert floor.scaled == 0.0  # clamped, never negative
    withdrawn = H.score_paper_v2(dict(BASE, kind="preprint", extra={"type": "withdrawn"}), [], "", [], False)
    assert "withdrawn preprint (−4.0)" in withdrawn.reasons and withdrawn.facts["notice"] is True


def test_platform_in_title_or_abstract_is_a_separate_component():
    methods_only = H.score_paper_v2(BASE, [_m("instrument", "xenium", "methods", "spatial", "Xenium")], "B", [], False)
    central = H.score_paper_v2(BASE, [_m("instrument", "xenium", "abstract", "spatial", "Xenium")], "B", [], False)
    assert central.raw == methods_only.raw + H.DEFAULT_WEIGHTS["platform_central"]
    assert "platform named in title/abstract: Xenium (+1.0)" in central.reasons and central.facts["central_platforms"] == ["Xenium"]


def test_every_component_is_exposed_with_points_in_reasons():
    paper = dict(BASE, id="doi:10.1/x", title="FIRE is a long-read Fiber-seq method", abstract="We present a novel single-cell atlas.",
                 cited_by_count=3, pub_date="2026-10-03", extra={"semantic_scholar": {"influential_citation_count": 1}})
    mentions = [_m("instrument", "xenium", "abstract", "spatial", "Xenium"), _m("instrument", "qubit", "methods", "qc", "Qubit"),
                _m("company", "10x-genomics", "affiliations", name="10x Genomics"), _m("company", "broad", "funding", name="Broad"),
                _m("company", "openai", "ack", name="OpenAI")]
    ctx = H.RankContext(as_of="2026-10-17", news_company_ids={"10x-genomics"}, news_company_names={"10x genomics"},
                        news_headlines="10x genomics launches xenium prime",
                        news_items=1, first_concepts={"doi:10.1/x": ["spatial proteomics"]}, concept_history_ok=True)
    res = H.score_paper_v2(paper, mentions, "A", ["single-cell"], True, company_types={"broad": "institute", "openai": "software"}, context=ctx)
    r = res.reasons
    for frag in ("journal tier A (+5.0)", "1 platform(s): Xenium (+1.0)", "platform named in title/abstract: Xenium (+1.0)", "1 lab tool(s) (+0.2)",
                 "1 company mention(s) (+0.5)", "1 institute mention(s), not scored", "1 AI tool(s), not scored", "company-affiliated author (+1.0)",
                 "watchlist: single-cell (+1.0)", "tracked author (+3.0)", "novelty cue(s) (+1.0)", "introduces a new method (+1.5)",
                 "already cited 3x in 14 days, 1 influential (+2.0)", "in the news this window: 10x Genomics, Xenium (+1.0)",
                 "first corpus appearance: 'spatial proteomics' (+1.0)"):
        assert frag in r, (frag, r)
    assert res.raw == pytest.approx(5 + 1 + 1 + 0.2 + 0.5 + 1 + 1 + 3 + 1 + 1.5 + 2 + 1 + 1)
    assert res.scaled == 10.0  # clamped
    d = res.as_dict()
    assert {c["key"] for c in d["components"]} >= {"venue", "platforms", "citations", "news_comention", "first_concept"}
    scaled, reasons = H.score_paper(paper, mentions, "A", ["single-cell"], True, company_types={"broad": "institute", "openai": "software"}, context=ctx)
    assert (scaled, reasons) == (res.scaled, res.reasons)  # the pipeline-facing wrapper


def test_news_comention_ignores_suppliers_named_only_in_methods():
    ctx = H.RankContext(news_company_ids={"danaher", "10x-genomics"}, news_company_names={"danaher", "10x genomics"}, news_headlines="danaher buys a company")
    supplier = [_m("company", "danaher", "methods", name="Danaher")]
    assert H.score_paper_v2(BASE, supplier, "B", [], False, context=ctx).component("news_comention") is None
    subject = [_m("company", "danaher", "abstract", name="Danaher")]
    assert "in the news this window: Danaher" in H.score_paper_v2(BASE, subject, "B", [], False, context=ctx).reasons
    vendor = [dict(_m("instrument", "txg-visium", "methods", "spatial", "Visium"), vendor="10x Genomics")]
    assert "in the news this window: 10x Genomics" in H.score_paper_v2(BASE, vendor, "B", [], False, context=ctx).reasons


def test_unranked_journal_prints_no_question_mark_and_software_is_never_scored():
    res = H.score_paper_v2(dict(BASE, journal="Frontiers in Immunology"), [_m("instrument", "alphafold", "methods", "software", "AlphaFold")], "", [], False)
    assert "journal unranked (+0.5)" in res.reasons and "?" not in res.reasons
    assert "1 software / model(s), not scored" in res.reasons and res.component("lab_tools") is None
    assert res.raw == 0.5 and res.component("companies") is None  # a model is not company involvement either
    assert "journal tier C (+1.0)" in H.score_paper_v2(BASE, [], "C", [], False).reasons


def test_cluster_related_folds_same_group_same_day_same_venue():
    def row(pid, title, authors, journal="Nature", day="2026-10-14", final=5.0):
        return {"id": pid, "title": title, "url": f"https://e/{pid}", "journal": journal, "pub_date": day, "final": final,
                "authors_json": json.dumps([{"name": a} for a in authors])}

    a = row("a", "De novo design of cysteine proteases", ["Ann Lee", "Bob Chen", "David Baker"])
    b = row("b", "Computational design of metalloproteases", ["Carl Diaz", "David Baker"], final=4.9)   # shared last author + 'design'
    c = row("c", "Unrelated Nature paper", ["Eve Fox", "Gil Hart"], final=4.8)
    d = row("d", "Design on another day", ["Ann Lee", "David Baker"], day="2026-10-15", final=4.7)
    e = row("e", "Design in another venue", ["Ann Lee", "David Baker"], journal="Science", final=4.6)
    f = row("f", "Design of a shared first-author paper", ["Ann Lee", "Zed Young"], final=4.5)          # shared first author + 'design'
    g = row("g", "Same surname and initial, different first name", ["Ida Jones", "Yun Wang"], final=4.4)
    h = row("h", "A different Wang lab on the same bioRxiv day", ["Kai Ortiz", "Yi Wang"], final=4.3)
    i = row("i", "Initials only, same day, same venue: design", ["Lee A", "Baker D"], final=4.2)         # initials never cluster
    j = row("j", "Same first author, nothing in common in the title", ["Ann Lee", "Quinn Ruiz"], final=4.1)  # no shared token ...
    k = row("k", "Both authors shared, unrelated words", ["Ann Lee", "David Baker"], final=4.0)         # ... unless both authors match
    out = cluster_related([a, b, c, d, e, f, g, h, i, j, k])
    assert [r["id"] for r in out] == ["a", "c", "d", "e", "g", "h", "i", "j"]
    assert [x["id"] for x in a["related"]] == ["b", "f", "k"] and "related" not in c and "related" not in g and "related" not in j
    assert _author_key("D. Baker") == _author_key("David Baker") == "baker d" and _author_key("Baker, David L.") == "baker d"
    assert _author_key("Baker") == "" and _author_key("") == ""
    bare = {"id": "x", "title": "no authors", "journal": "Nature", "pub_date": "2026-10-14"}
    assert cluster_related([bare, dict(bare, id="y")]) == [bare, dict(bare, id="y")]  # nothing to cluster on


def test_render_folds_near_duplicates_into_one_entry(tmp_path):
    st = Store(tmp_path / "nd.sqlite")
    from litradar.models import Author

    authors = [Author(name="Ann Lee"), Author(name="David Baker")]
    # the cited one leads the cluster; the other is folded under it
    st.upsert_paper(_paper("doi:10.1/enz1", journal="Nature", title="De novo design of cysteine proteases", pub_date="2026-10-14", authors=authors, cited_by_count=3))
    st.upsert_paper(_paper("doi:10.1/enz2", journal="Nature", title="Computational design of metalloproteases", pub_date="2026-10-14",
                           authors=[Author(name="Carl Diaz"), Author(name="David Baker")]))
    st.upsert_paper(_paper("doi:10.1/other", journal="Nature", title="An unrelated Nature paper", pub_date="2026-10-14"))
    for pid in ("doi:10.1/enz1", "doi:10.1/enz2", "doi:10.1/other"):
        st.save_score(pid, 1.0, None, 1.0, "r")
    st.commit()
    out = render(st, *WINDOW, {}, config_dir=CONFIG)
    md = out["markdown"]
    top = md[md.index("## Top"): md.index("## Platforms")]
    assert "## Top 2 innovations" in top and "1 near-duplicate (same group, same venue, same day) folded" in top
    assert "+1 related from the same group, same venue, same day: [Computational design of metalloproteases](https://e/doi:10.1/enz2)" in top
    assert out["summary"]["ranking"]["clustered"] == 1 and out["summary"]["top"][0]["related"][0]["id"] == "doi:10.1/enz2"
    off = render(st, *WINDOW, {"ranking": {"diversity": {"cluster_same_group": False}}}, config_dir=CONFIG)["markdown"]
    assert "## Top 3 innovations" in off and "related from the same group" not in off
    st.close()


# ---- the new-platform cue -------------------------------------------------------------------------------------
@pytest.mark.parametrize("title,abstract,noun", [
    ("A study", "Here we present a scalable assay for cell-free DNA.", "assay"),
    ("A study", "We introduce SpatialX, a platform for whole-transcriptome imaging.", "platform"),
    ("A study", "In this work we developed a nanopore-based sequencer with single-molecule resolution.", "sequencer"),
    ("HURDLER: a cloning method to overcome length limits in designed repeat proteins", "", "method"),
    ("A study", "FIRE is a long-read Fiber-seq method that resolves chromatin accessibility.", "method"),
    ("A study", "This study describes an open-source pipeline for variant calling.", "pipeline"),
])
def test_new_platform_cue_positives(title, abstract, noun):
    assert H.new_platform_cue(title, abstract) == noun


@pytest.mark.parametrize("title,abstract", [
    ("A study", "We present a comprehensive analysis using the Visium platform."),
    ("A study", "We report that the method fails in 30% of samples."),
    ("A study", "Samples were sequenced on an Illumina NovaSeq instrument."),
    ("A study", "We used the Xenium platform to profile tumours."),
    ("Spatial transcriptomics reveals region-specific host responses", "Regional patterns were observed in gill tissue."),
])
def test_new_platform_cue_negatives(title, abstract):
    assert H.new_platform_cue(title, abstract) is None


# ---- citations, conversion, venue through the published version ----------------------------------------------
def test_citation_components_are_age_aware():
    ctx = H.RankContext(as_of="2026-10-17")
    early = H.score_paper_v2(dict(BASE, cited_by_count=1, pub_date="2026-08-01"), [], "C", [], False, context=ctx)
    fast = H.score_paper_v2(dict(BASE, cited_by_count=3, pub_date="2026-10-10"), [], "C", [], False, context=ctx)
    assert _points(early, "citations") == 1.0 and "already cited 1x in 77 days (+1.0)" in early.reasons
    assert _points(fast, "citations") == 1.5 and fast.facts["citations"]["fast"] is True and "already cited 3x in 7 days (+1.5)" in fast.reasons
    # older than 90 days: being cited is not "early"; only the rate per month counts (a backfilled 2024 paper with
    # hundreds of citations must not outrank this week's work just for being old)
    slow_old = H.score_paper_v2(dict(BASE, cited_by_count=1, pub_date="2026-07-01"), [], "C", [], False, context=ctx)
    assert _points(slow_old, "citations") == 0.0 and "cited 1x over 4 months (0.3/month), not scored (slow for its age)" in slow_old.reasons
    fast_old = H.score_paper_v2(dict(BASE, cited_by_count=240, pub_date="2024-10-17"), [], "C", [], False, context=ctx)
    assert _points(fast_old, "citations") == 1.0 and "cited 240x over 24 months (9.9/month) (+1.0)" in fast_old.reasons
    assert "cited 240x (10/month since publication)" in why_it_matters(fast_old, dict(BASE, journal="X"))
    assert "cited" not in why_it_matters(slow_old, dict(BASE, journal="X"))
    infl = H.score_paper_v2(dict(BASE, cited_by_count=12, extra_json=json.dumps({"semantic_scholar": {"influential_citation_count": 4}})), [], "C", [], False)
    assert _points(infl, "citations") == 1.5 and "already cited 12x, 4 influential" in infl.reasons  # no pub_date: age unknown
    capped = H.score_paper_v2(dict(BASE, cited_by_count=30, pub_date="2026-10-10", extra={"semantic_scholar": {"influential_citation_count": 2}}), [], "C", [], False, context=ctx)
    assert _points(capped, "citations") == 2.0
    assert H.citation_age_days("2026-10-10", "2026-10-17") == 7 and H.citation_age_days("", "2026-10-17") is None
    assert H.score_paper_v2(BASE, [], "C", [], False).component("citations") is None


def test_preprint_conversion_and_venue_via_published_version():
    # the abstract carries a watchlist term so the topical gate lets the published venue's tier-A prior through
    pre = dict(BASE, id="doi:10.1101/p", kind="preprint", published_doi="10.1038/s41592-026-1", abstract="A long-read sequencing method.")
    plain = H.score_paper_v2(pre, [], "", ["long-read"], False)
    assert "preprint (+1.0)" in plain.reasons and "preprint published as a journal article (+1.0)" in plain.reasons
    ctx = H.RankContext(published_venues={"doi:10.1101/p": ("Nature Methods", "A")})
    pub = H.score_paper_v2(pre, [], "", ["long-read"], False, context=ctx)
    assert "journal tier A via published version Nature Methods (+5.0)" in pub.reasons
    assert "preprint published as a journal article (Nature Methods) (+1.0)" in pub.reasons
    assert pub.facts["published_in"] == "Nature Methods" and pub.raw == plain.raw - 1.0 + 5.0
    assert H.score_paper_v2(dict(BASE, kind="preprint"), [], "", [], False).component("preprint_published") is None


# ---- why it matters -------------------------------------------------------------------------------------------
def test_why_it_matters_template_is_built_from_components():
    paper = dict(BASE, id="doi:10.1/x", journal="Nature", title="We present a new platform", abstract="We present a new platform for spatial proteomics.",
                 cited_by_count=3, pub_date="2026-10-12")
    mentions = [_m("instrument", "xenium", "abstract", "spatial", "Xenium"), _m("company", "10x-genomics", "methods", name="10x Genomics")]
    ctx = H.RankContext(as_of="2026-10-17", first_concepts={"doi:10.1/x": ["spatial proteomics"]}, concept_history_ok=True)
    res = H.score_paper_v2(paper, mentions, "A", ["spatial proteomics"], False, context=ctx)
    why = why_it_matters(res, paper)
    assert why == ("Nature paper introducing a new platform; cited 3x in its first week; names Xenium and 10x Genomics; "
                   "on watchlist topics spatial proteomics; first corpus appearance of 'spatial proteomics'")
    bare = H.score_paper_v2(dict(BASE, kind="preprint", journal="biorxiv"), [], "", [], False)
    assert why_it_matters(bare, dict(BASE, kind="preprint", journal="biorxiv")) == "BioRxiv preprint"
    pub = H.score_paper_v2(dict(BASE, kind="preprint", journal="biorxiv", id="p", published_doi="10.1/x"), [], "", [], True,
                           context=H.RankContext(published_venues={"p": ("Cell", "A")}))
    assert why_it_matters(pub, dict(BASE, kind="preprint", journal="biorxiv", id="p")) == "BioRxiv preprint already published in Cell; by a tracked author"
    assert why_it_matters(H.ScoreResult(0, 0, []), {}) == ""
    long = H.score_paper_v2(dict(paper, title="x " * 50), mentions, "A", ["spatial proteomics"], True, context=ctx)
    assert len(why_it_matters(long, paper, max_chars=80)) <= 80


# ---- subgroups and diversity ----------------------------------------------------------------------------------
def test_subgroup_assignment_mirrors_the_pipeline_rule():
    defs = [{"id": "spatial", "keywords": ["Xenium", "spatial transcriptomics"], "openalex_topics": ["T1"]},
            {"id": "long_read", "keywords": ["nanopore"], "openalex_topics": ["T2"]}]
    assert assign_subgroups("A Xenium atlas", "", {}, defs) == ["spatial"]
    assert assign_subgroups("Nothing", "", {"primary_topic": "T2"}, defs) == ["long_read"]
    assert assign_subgroups("Nothing", "", {"subgroups": ["spatial"]}, defs) == ["spatial"]  # backfill tag
    assert assign_subgroups("Xenium and nanopore", "", {}, defs) == ["spatial", "long_read"]
    assert primary_subgroup([]) == "other" and primary_subgroup(["long_read", "spatial"]) == "long_read"


def test_diversify_soft_caps_reorder_but_never_shorten():
    cands = [{"id": i, "kind": "preprint" if i < 8 else "article", "subgroup": "spatial" if i % 2 == 0 else "other"} for i in range(12)]
    selected, deferred = diversify(cands, 6, max_per_kind=4, max_per_subgroup=2)
    ids = [c["id"] for c in selected]
    assert len(ids) == 6 and ids[:4] == [0, 1, 2, 3]  # 2 spatial (0, 2) and 4 preprints reached first
    assert all(c["kind"] == "article" or c["id"] < 4 for c in selected[4:]) and ids[4:] == [9, 11]  # articles in 'other' (uncapped)
    assert [d["rank"] for d in deferred][:3] == [5, 6, 7] and {d["cap"] for d in deferred} >= {"kind preprint", "subgroup spatial"}
    # soft: when the caps would leave slots empty the deferred ones come back in order
    tight, deferred2 = diversify(cands[:6], 6, max_per_kind=2, max_per_subgroup=1)
    assert [c["id"] for c in tight] == [0, 1, 2, 3, 4, 5] and sum(d["readmitted"] for d in deferred2) == 4
    assert diversify(cands, 5)[0] == cands[:5]  # caps of 0 disable the dimension
    assert diversify([], 5) == ([], [])


# ---- context from the store -----------------------------------------------------------------------------------
def _paper(pid, **kw):
    base = dict(id=pid, source="europepmc", kind="article", title=f"Paper {pid}", doi=pid[4:], abstract="An abstract.", journal="Nature",
                pub_date="2026-10-12", url=f"https://e/{pid}")
    base.update(kw)
    return Paper(**base)


def test_build_context_news_concepts_and_published_versions(tmp_path):
    st = Store(tmp_path / "c.sqlite")
    st.upsert_paper(_paper("doi:10.1/a"))
    st.upsert_paper(_paper("doi:10.1101/p", kind="preprint", journal="biorxiv", published_doi="10.1038/J1"))
    st.upsert_paper(_paper("doi:10.1038/j1", journal="Nature Methods", pub_date="2026-11-01"))  # journal version outside the window
    st.upsert_news([NewsItem("n1", "feed", "10x Genomics launches Xenium Prime", "https://e/n1", "2026-10-12", "", ["10x-genomics"]),
                    NewsItem("n2", "feed", "Old news", "https://e/n2", "2026-09-01", "", ["illumina"])])
    st.conn.executescript("""
        CREATE TABLE concept_counts (term TEXT, week TEXT, papers INTEGER, display TEXT, computed_at TEXT, PRIMARY KEY (term, week));
        CREATE TABLE concept_papers (term TEXT, paper_id TEXT, week TEXT, channel TEXT, PRIMARY KEY (term, paper_id, week));
    """)
    st.conn.execute("INSERT INTO concept_counts VALUES ('spatial proteomics', '2026-10-05', 2, 'spatial proteomics', 'now')")
    st.conn.execute("INSERT INTO concept_counts VALUES ('old term', '2026-08-03', 2, 'old term', 'now')")
    st.conn.execute("INSERT INTO concept_counts VALUES ('old term', '2026-10-05', 2, 'old term', 'now')")
    st.conn.execute("INSERT INTO concept_counts VALUES ('sangha river', '2026-10-05', 1, 'Sangha River', 'now')")  # one paper's place name
    st.conn.execute("INSERT INTO concept_papers VALUES ('spatial proteomics', 'doi:10.1/a', '2026-10-05', 'seed')")
    st.conn.execute("INSERT INTO concept_papers VALUES ('spatial proteomics', 'doi:10.1/b', '2026-10-05', 'seed')")
    st.conn.execute("INSERT INTO concept_papers VALUES ('old term', 'doi:10.1/a', '2026-10-05', 'seed')")
    st.conn.execute("INSERT INTO concept_papers VALUES ('sangha river', 'doi:10.1/a', '2026-10-05', 'term')")
    st.commit()
    by_name, by_issn = journal_tiers(CONFIG)
    assert by_name["nature methods"] == "A" and journal_tier_for("Nature Methods", {}, by_name, by_issn) == "A"
    assert journal_tier_for("", {"issn": ["1548-7105"]}, by_name, by_issn) == "A" and journal_tier_for("Unknown", {}, by_name, by_issn) == ""
    ctx = build_context(st, *WINDOW, by_name, by_issn)
    assert ctx.as_of == WINDOW[1] and ctx.news_items == 1 and ctx.news_company_ids == {"10x-genomics"} and "xenium prime" in ctx.news_headlines
    assert ctx.published_venues == {"doi:10.1101/p": ("Nature Methods", "A")}
    # only two weeks of concept history before the window: first-in-corpus is not trusted
    assert ctx.concept_history_weeks == 1 and ctx.concept_history_ok is False and ctx.first_concepts == {}
    for wk in ("2026-08-10", "2026-08-17", "2026-08-24"):
        st.conn.execute("INSERT INTO concept_counts VALUES ('old term', ?, 1, 'old term', 'now')", (wk,))
    st.commit()
    ctx2 = build_context(st, *WINDOW, by_name, by_issn)
    # a first-seen term counts once at least two papers in the window carry it; a singleton phrase (a river name, one
    # lab's acronym) is one paper's wording, not a concept entering the corpus
    assert ctx2.concept_history_ok is True and ctx2.first_concepts == {"doi:10.1/a": ["spatial proteomics"], "doi:10.1/b": ["spatial proteomics"]}
    ctx3 = build_context(st, *WINDOW, by_name, by_issn, first_concept_min_papers=1)
    assert ctx3.first_concepts["doi:10.1/a"] == ["Sangha River", "spatial proteomics"]
    st.close()


# ---- rerank_window end to end ---------------------------------------------------------------------------------
def _seed(st: Store) -> None:
    verbose = _paper("doi:10.1/verbose", journal="Science Advances", title="A verbose open-access paper", abstract="Many reagents were used.")
    nature = _paper("doi:10.1/nature", journal="Nature", title="A new assay", abstract="We present a scalable assay for spatial proteomics.", cited_by_count=3)
    pre = _paper("doi:10.1101/pre", kind="preprint", journal="biorxiv", source="biorxiv", title="Preprint on Xenium", abstract="Xenium profiling of tumours.")
    for p in (verbose, nature, pre):
        st.upsert_paper(p)
    st.replace_mentions(verbose.id, [Mention(verbose.id, "instrument", f"p{i}", f"Platform {i}", "V", "", "sequencer.short_read", "methods", 1, "") for i in range(6)]
                        + [Mention(verbose.id, "instrument", f"t{i}", f"Tool {i}", "V", "", "qc", "methods", 1, "") for i in range(17)]
                        + [Mention(verbose.id, "company", f"c{i}", f"Company {i}", "", "", "", "methods", 1, "") for i in range(23)]
                        + [Mention(verbose.id, "company", "c0", "Company 0", "", "", "", "affiliations", 1, "")])
    st.replace_mentions(pre.id, [Mention(pre.id, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, ""),
                                 Mention(pre.id, "company", "10x-genomics", "10x Genomics", "", "TXG", "", "methods", 1, "")])
    st.upsert_news([NewsItem("n1", "feed", "10x Genomics launches Xenium Prime", "https://e/n1", "2026-10-12", "", ["10x-genomics"])])
    # v1 scores: the verbose paper on top
    st.save_score(verbose.id, 6.0, None, 6.0, "journal tier B; 6 platform(s); 17 lab tool(s); 23 company mention(s)")
    st.save_score(nature.id, 2.9, None, 2.9, "journal tier A")
    st.save_score(pre.id, 2.1, None, 2.1, "preprint; 1 platform(s)")
    st.commit()


def test_rerank_window_rescored_persists_and_explains(tmp_path):
    st = Store(tmp_path / "r.sqlite")
    _seed(st)
    before = [p["id"] for p in st.top_papers(*WINDOW)]
    assert before[0] == "doi:10.1/verbose"
    cfg = {"watchlist_terms": ["spatial proteomics"], "digest": {"top_n": 25}}
    info = rerank_window(st, *WINDOW, cfg, config_dir=CONFIG)
    top = info["top"]
    # the verbose paper keeps its company-affiliated author and tier B, so it still beats a bare preprint; it no longer
    # beats the Nature paper that introduces an assay
    assert [p["id"] for p in top] == ["doi:10.1/nature", "doi:10.1/verbose", "doi:10.1101/pre"]
    nature = top[0]
    assert nature["why"].startswith("Nature paper introducing a new assay; cited 3x in its first week")
    assert "introduces a new assay (+1.5)" in nature["reasons"] and nature["subgroup"] in ("spatial", "other")
    pre = top[2]
    assert "in the news this window: 10x Genomics, Xenium (+1.0)" in pre["reasons"] and pre["subgroup"] == "spatial"
    assert pre["mentions"] and {c["key"] for c in pre["components"]} >= {"venue", "platforms", "platform_central", "news_comention"}
    assert info["persisted"] is True and info["candidates"] == 3 and info["clustered"] == 0 and info["context"]["news_items"] == 1
    stored = st.get_score("doi:10.1/verbose")
    assert stored["heuristic"] == top[1]["heuristic"] < 6.0 and "6 platform(s), scored 1" in stored["reasons"]
    assert [p["id"] for p in st.top_papers(*WINDOW)] == [p["id"] for p in top]  # the store now agrees with the digest
    # persist=False leaves the scores table alone
    st.save_score("doi:10.1/verbose", 9.0, None, 9.0, "manual")
    rerank_window(st, *WINDOW, cfg, config_dir=CONFIG, persist=False)
    assert st.get_score("doi:10.1/verbose")["final"] == 9.0
    st.close()


def test_rerank_window_blends_stored_llm_and_collapses_preprints(tmp_path):
    st = Store(tmp_path / "l.sqlite")
    pre = _paper("doi:10.1101/x", kind="preprint", journal="biorxiv", title="Preprint title that is long enough to match", published_doi="10.1038/y", pub_date="2026-10-11")
    art = _paper("doi:10.1038/y", journal="Nature", title="The journal version title", pub_date="2026-10-15")
    st.upsert_paper(pre)
    st.upsert_paper(art)
    st.save_score(art.id, 1.0, {"composite": 8.0}, 1.0, "r")
    st.commit()
    info = rerank_window(st, *WINDOW, {"ranking": {"llm": {"llm_weight": 0.5}}}, config_dir=CONFIG)
    assert [p["id"] for p in info["top"]] == [art.id]
    art_row = info["top"][0]
    assert art_row["preprints"][0]["id"] == pre.id and art_row["final"] == H.blend(art_row["heuristic"], 8.0, 0.5)
    assert json.loads(st.get_score(art.id)["llm_json"])["composite"] == 8.0  # the LLM json survives the re-score
    st.close()


def test_rerank_window_applies_diversity_caps_from_config(tmp_path):
    st = Store(tmp_path / "d.sqlite")
    for i in range(10):
        p = _paper(f"doi:10.1/pre{i}", kind="preprint", journal="biorxiv", title=f"Xenium preprint {i}", abstract="Xenium spatial transcriptomics.", pub_date=f"2026-10-1{i % 7}")
        st.upsert_paper(p)
        st.save_score(p.id, 5.0, None, 5.0, "r")
    for i in range(3):  # unranked venue: these score below the preprints, so the caps have to bite for them to appear
        p = _paper(f"doi:10.1/art{i}", journal="", title=f"Article {i}", abstract="Unrelated biology.")
        st.upsert_paper(p)
        st.save_score(p.id, 1.0, None, 1.0, "r")
    st.commit()
    cfg = {"digest": {"top_n": 6}, "ranking": {"diversity": {"max_per_kind": 4, "max_per_subgroup": 3}}}
    info = rerank_window(st, *WINDOW, cfg, config_dir=CONFIG)
    kinds = [p["kind"] for p in info["top"]]
    assert kinds == ["preprint"] * 3 + ["article"] * 3  # subgroup cap 3 bites before the kind cap 4 (all preprints are 'spatial')
    assert info["caps"] == {"max_per_kind": 4, "max_per_subgroup": 3}
    assert [d["cap"] for d in info["deferred"]] == ["subgroup spatial"] * 7 and not any(d["readmitted"] for d in info["deferred"])
    off = rerank_window(st, *WINDOW, {"digest": {"top_n": 6}, "ranking": {"diversity": {"enabled": False}}}, config_dir=CONFIG)
    assert [p["kind"] for p in off["top"]] == ["preprint"] * 6 and off["deferred"] == []
    st.close()


# ---- render integration ---------------------------------------------------------------------------------------
def test_render_uses_v2_shows_why_and_subgroup_and_can_fall_back(tmp_path):
    st = Store(tmp_path / "v.sqlite")
    _seed(st)
    out = render(st, *WINDOW, {"watchlist_terms": ["spatial proteomics"]}, config_dir=CONFIG)
    md = out["markdown"]
    top = md[md.index("## Top"): md.index("## Platforms")]
    assert "_Ranked by the v2 heuristic over 3 papers in the window" in top and "soft diversity caps: 15 per kind (preprint / article), 8 per subgroup; nothing deferred" in top
    assert "first-in-corpus concepts not scored (0 week(s) of concept history on record, 4 needed)" in top
    assert top.index("A new assay") < top.index("A verbose open-access paper") < top.index("Preprint on Xenium")
    assert "*Why it matters:* Nature paper introducing a new assay; cited 3x in its first week; on watchlist topics spatial proteomics <sub>(from the score components; no LLM)</sub>" in top
    assert "· score " in top and " · spatial_" in top  # subgroup tag after the score
    assert "Signals: journal tier A (+5.0); watchlist: spatial proteomics (+1.0); 2 novelty cue(s) (+1.0); introduces a new assay (+1.5); already cited 3x in 5 days (+1.5)" in top
    assert out["summary"]["ranking"]["method"] == "v2" and out["summary"]["top"][0]["why"].startswith("Nature paper")
    assert out["summary"]["top"][0]["components"]
    assert [t["subgroup"] for t in out["summary"]["top"]] == ["spatial", "other", "spatial"]  # "spatial proteomics" is a spatial keyword
    # an entry with LLM json keeps the LLM lines and gets no template
    st.save_score("doi:10.1/nature", 5.0, {"one_line": "LLM says", "why_it_matters": "LLM reason", "novelty": 4, "commercial_implication": 3,
                                            "instrument_role": "central", "clinical_readiness": "basic", "hype_risk": 1, "composite": 7.0}, 7.0, "r")
    st.commit()
    md2 = render(st, *WINDOW, {}, config_dir=CONFIG)["markdown"]
    entry = md2[md2.index("A new assay"): md2.index("A verbose open-access paper")]
    assert "*Why it matters:* LLM reason" in entry and "no LLM" not in entry
    # the legacy path: stored scores, no method line, no why line, no tag
    md3 = render(st, *WINDOW, {"ranking": {"v2": {"enabled": False}}}, config_dir=CONFIG)
    assert "Ranked by the v2 heuristic" not in md3["markdown"] and md3["summary"]["ranking"] == {"method": "stored"}
    assert "(from the score components; no LLM)" not in md3["markdown"]
    md4 = render(st, *WINDOW, {"digest": {"why_it_matters": False, "show_subgroup": False}}, config_dir=CONFIG)["markdown"]
    assert "(from the score components; no LLM)" not in md4 and " · spatial_" not in md4
    st.close()


def test_topical_gate_withholds_the_venue_prior_for_off_topic_tier_a_papers():
    from litradar.rank.heuristics import DEFAULT_WEIGHTS, score_paper_v2

    off_topic = {"kind": "article", "title": "Advancing subseasonal forecasting with machine learning",
                 "abstract": "Weather models improve with learned corrections over two to six weeks.", "cited_by_count": 0}
    res = score_paper_v2(off_topic, [], "A", ["spatial transcriptomics"], None)
    venue = [c for c in res.components if c.key == "venue"][0]
    assert res.facts["topical"] is False and venue.points == DEFAULT_WEIGHTS["tier_unknown"] and "prior withheld" in venue.label
    on_topic = dict(off_topic, abstract="A spatial transcriptomics atlas of the gut built on a commercial platform.")
    res2 = score_paper_v2(on_topic, [], "A", ["spatial transcriptomics"], None)
    venue2 = [c for c in res2.components if c.key == "venue"][0]
    assert res2.facts["topical"] is True and venue2.points == DEFAULT_WEIGHTS["tier_A"] and "withheld" not in venue2.label
    # a software-type (big tech) affiliation, a first-seen concept or a bare noun cue do not count as topical on their own
    weather = dict(off_topic, abstract="We introduce a new framework for weather forecasting.")
    ments = [_m("company", "microsoft-research", "affiliations", name="Microsoft Research")]
    res4 = score_paper_v2(weather, ments, "A", ["spatial transcriptomics"], None, company_types={"microsoft-research": "software"})
    assert res4.facts["topical"] is False and [c for c in res4.components if c.key == "venue"][0].points == DEFAULT_WEIGHTS["tier_unknown"]
    # an affiliation-derived unlisted company (not in the gazetteer, type unknown) is not topical either
    res4b = score_paper_v2(weather, [_m("company", "unlisted:bigco-united-states", "affiliations", category="openalex-company", name="BigCo (United States)")],
                           "A", [], None, company_types={"microsoft-research": "software"})
    assert res4b.facts["topical"] is False
    # the same affiliation from a tools company is topical
    res5 = score_paper_v2(weather, [_m("company", "acme-tools", "affiliations", name="Acme Tools")], "A", [], None, company_types={"acme-tools": "tools"})
    assert res5.facts["topical"] is True and [c for c in res5.components if c.key == "venue"][0].points == DEFAULT_WEIGHTS["tier_A"]
    # the gate can be switched off
    res3 = score_paper_v2(off_topic, [], "A", ["spatial transcriptomics"], None, weights={"topical_gate": False})
    assert [c for c in res3.components if c.key == "venue"][0].points == DEFAULT_WEIGHTS["tier_A"]
