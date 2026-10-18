"""Regression tests for the ranking-area shakedown fixes, on temporary stores (no network).

* records that are not research papers (letters, editorials, dataset / software deposits) are recognised
  (`litradar.worktype`), penalised by the heuristic and lose their venue prior, so a tier-A letter or a Zenodo
  deposit cannot outrank a platform paper on venue alone;
* `Store.search` puts research papers before such records, folds repeated titles, and sorts by relevance,
  rank score or date; the CLI and the JSON API expose the sort;
* `litradar show` and `companies show` say when a trend compares periods of unequal section coverage and
  give the title/abstract-only numbers instead of an unqualified percentage.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from litradar import api, cli, entities, worktype
from litradar import companies as C
from litradar.commands.companies import _print_detail
from litradar.extract.gazetteer import Gazetteer
from litradar.models import Author, Mention, Paper
from litradar.rank import heuristics as H
from litradar.rank.explain import why_it_matters
from litradar.store import SEARCH_SORTS, Store

TODAY = date(2026, 10, 17)
INSTRUMENTS = Gazetteer.from_yaml({"entries": [
    {"id": "txg-xenium", "name": "Xenium", "vendor": "10x Genomics", "ticker": "TXG", "category": "spatial", "launched": 2022, "aliases": []},
]}, "instrument")
COMPANIES = Gazetteer.from_yaml({"entries": [
    {"id": "illumina", "name": "Illumina", "ticker": "ILMN", "domains": ["illumina.com"], "aliases": []},
]}, "company")


def _paper(i, title, abstract="About spatial biology.", **kw):
    base = dict(id=f"doi:10.1000/r{i}", source="openalex", kind="article", title=title, doi=f"10.1000/r{i}", abstract=abstract,
                journal="Journal of Examples", pub_date="2026-10-12", url=f"https://example.org/r{i}", authors=[Author(name="A. Author")], sections={})
    base.update(kw)
    return Paper(**base)


def _xen(pid, section, count=1):
    return Mention(pid, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", section, count, "...Xenium...")


# ---- worktype: what is and is not a research paper ---------------------------------------------------------------
@pytest.mark.parametrize("kw, label", [
    (dict(abstract="To the Editor: The results of the trial reported last month show ..."), "letter"),
    (dict(doi="10.1056/nejmc2610854", journal="New England Journal of Medicine"), "letter"),
    (dict(doi="10.1056/nejme2612910"), "editorial"),
    (dict(title="Reply to: Statistical considerations in the evaluation of a biomarker"), "letter"),
    (dict(title="Editorial: Advances in immunogenicity risk assessment"), "editorial"),
    (dict(doi="10.5281/zenodo.23406824", journal="Zenodo (CERN European Organization for Nuclear Research)"), "deposit"),
    (dict(doi="10.6084/m9.figshare.30899039", journal="Figshare"), "deposit"),
    (dict(doi="10.25345/c54x54x54", journal="UC San Diego", work_type="dataset"), "dataset"),
    (dict(doi="10.17504/protocols.io.rm7vzj", work_type="preprint"), "protocol"),       # a protocol repository, whatever OpenAlex calls it
    (dict(journal="Mendeley Data"), "dataset"),
    (dict(work_type="software"), "software"),
    (dict(work_type="supplementary-materials"), "supplement"),
    (dict(work_type="dissertation"), "dissertation"),
    (dict(work_type="peer-review"), "peer_review"),
    (dict(work_type="erratum"), "notice"),
    (dict(title="MassIVE MSV000103560 - Scanning DIA on a mass spectrometer"), "dataset"),
    (dict(title="github.com/example-org/pipeline/atac"), "software"),
    (dict(title="Supplementary Figure 2 from Identification of Cancer Cell Stemness"), "supplement"),
])
def test_classify_flags_non_research_records(kw, label):
    args = dict(title="A record", abstract="", doi="", journal="", work_type="")
    args.update(kw)
    rt = worktype.classify(**args)
    assert rt is not None and rt.label == label, (kw, rt)
    assert rt.phrase == worktype.PHRASES[label]


@pytest.mark.parametrize("kw", [
    dict(title="Resolving sensitivity and specificity in a spatial platform", journal="Nature Methods", doi="10.1038/s41592-026-03089-8"),
    dict(doi="10.5281/zenodo.100", journal="Zenodo", work_type="preprint"),   # a preprint deposited on a general repository stays a paper
    dict(title="Correcting batch effects in single-cell data"),
    dict(title="A response to therapy predicts survival", abstract="We studied responses to therapy."),  # "response to" mid-sentence, not a reply
    dict(work_type="review"), dict(work_type="conference-abstract"), dict(work_type="data-paper"),
])
def test_classify_keeps_research_papers(kw):
    args = dict(title="A paper", abstract="", doi="", journal="", work_type="")
    args.update(kw)
    assert worktype.classify(**args) is None, kw


def test_sql_predicate_agrees_with_classify(tmp_path):
    st = Store(tmp_path / "w.sqlite")
    rows = [
        _paper(1, "Obinutuzumab in active lupus", "To the Editor: The results of the trial ...", doi="10.1056/nejmc2610854", journal="New England Journal of Medicine"),
        _paper(2, "A dataset of liver cells", journal="Zenodo (CERN European Organization for Nuclear Research)", doi="10.5281/zenodo.1", kind="preprint"),
        _paper(3, "A real preprint on Zenodo", journal="Zenodo (CERN European Organization for Nuclear Research)", doi="10.5281/zenodo.2", kind="preprint", extra={"work_type": "preprint"}),
        _paper(4, "Slide preparation v1", journal="", doi="10.17504/protocols.io.abc", kind="preprint", extra={"work_type": "preprint"}),
        _paper(5, "MassIVE MSV000103560 - Scanning DIA", journal="UC San Diego", doi="10.25345/c5x", kind="preprint", extra={"work_type": "dataset"}),
        _paper(6, "Supplementary Table 1 from A trial", journal="", doi="10.1158/x.1", extra={"work_type": "supplementary-materials"}),
        _paper(7, "Reply to: A comment on our paper", journal="PNAS", doi="10.1073/pnas.1"),
        _paper(8, "A platform paper", journal="Nature Methods", doi="10.1038/s41592-1"),
        _paper(9, "github.com/example/pipeline", journal="Zenodo (CERN European Organization for Nuclear Research)", doi="", kind="preprint"),
    ]
    for p in rows:
        st.upsert_paper(p)
    st.commit()
    got = {r[0]: bool(r[1]) for r in st.conn.execute(f"SELECT p.id, {worktype.sql_predicate('p')} FROM papers p")}
    for p in rows:
        want = worktype.classify(p.title, p.abstract, p.doi, p.journal, (p.extra or {}).get("work_type", "")) is not None
        assert got[p.id] == want, (p.id, p.title)
    assert got["doi:10.1000/r3"] is False and got["doi:10.1000/r8"] is False
    assert sum(got.values()) == 7
    st.close()


# ---- heuristics: venue beats substance no more ---------------------------------------------------------------------
def test_letter_in_a_tier_a_journal_scores_below_a_platform_paper():
    letter = {"kind": "article", "title": "Obinutuzumab in Active Systemic Lupus Erythematosus", "doi": "10.1056/nejmc2610854",
              "journal": "New England Journal of Medicine", "abstract": "To the Editor: The results of the trial show ...", "cited_by_count": 0}
    ments = [{"kind": "company", "entity_id": "roche", "entity_name": "Roche", "section": "affiliations", "category": "", "count": 1}]
    res = H.score_paper_v2(letter, ments, "A", [], None, company_types={"roche": "tools"})
    venue = res.component("venue")
    assert res.facts["record_type"] == "letter" and venue.points == H.DEFAULT_WEIGHTS["tier_unknown"] and "prior withheld: letter" in venue.label
    assert res.component("notice").points == H.DEFAULT_WEIGHTS["non_research_penalty"]
    assert "letter / correspondence, not a research paper (−4.0)" in res.reasons
    assert why_it_matters(res, letter).startswith("New England Journal of Medicine letter / correspondence, not a research paper")
    paper = {"kind": "article", "title": "A spatial platform paper", "abstract": "We present a new assay for Xenium.", "journal": "Nature Methods", "cited_by_count": 0}
    real = H.score_paper_v2(paper, [_xen("x", "abstract").__dict__], "A", [], None)
    assert real.raw > res.raw and res.scaled < 1.0
    # the penalty is a weight: 0 switches it off and the letter keeps its tier
    plain = H.score_paper_v2(letter, ments, "A", [], None, company_types={"roche": "tools"}, weights={"non_research_penalty": 0.0})
    assert plain.component("notice") is None and plain.component("venue").points == H.DEFAULT_WEIGHTS["tier_A"]


def test_dataset_deposits_lose_the_preprint_prior_and_are_penalised():
    deposit = {"kind": "preprint", "title": "MassIVE MSV000103560 - Scanning DIA on a mass spectrometer", "abstract": "", "doi": "10.25345/c54x54x54",
               "journal": "UC San Diego", "cited_by_count": 0, "extra": {"work_type": "dataset"}}
    res = H.score_paper_v2(deposit, [], "", ["proteomics"], True)
    assert res.facts["record_type"] == "dataset" and res.component("venue").points == H.DEFAULT_WEIGHTS["tier_unknown"]
    assert "dataset deposit, not a research paper (−4.0)" in res.reasons
    assert res.raw == pytest.approx(0.5 + 3.0 - 4.0)
    assert why_it_matters(res, deposit).startswith("UC San Diego dataset deposit, not a research paper")
    zen = dict(deposit, title="A cross-species PBMC dataset", doi="10.5281/zenodo.23406824", journal="Zenodo (CERN European Organization for Nuclear Research)", extra={})
    assert H.score_paper_v2(zen, [], "", [], None).facts["record_type"] == "deposit"
    # a work_type erratum is a notice (the title regex is not the only signal), never double-penalised
    err = H.score_paper_v2({"kind": "article", "title": "Some paper", "abstract": "", "extra": {"work_type": "erratum"}}, [], "A", [], None)
    assert err.facts.get("notice") is True and len([c for c in err.components if c.key == "notice"]) == 1
    # a genuine preprint on a general repository is untouched
    pre = H.score_paper_v2({"kind": "preprint", "title": "A preprint", "abstract": "", "doi": "10.5281/zenodo.5", "journal": "Zenodo", "extra": {"work_type": "preprint"}}, [], "", [], None)
    assert "record_type" not in pre.facts and pre.component("venue").points == H.DEFAULT_WEIGHTS["tier_preprint"]


# ---- search: papers first, folded duplicates, sort options --------------------------------------------------------
def _search_store(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.upsert_paper(_paper(1, "Resolving sensitivity and specificity in Xenium data", "A long abstract about Xenium in situ profiling and its " + "quality " * 80,
                           journal="Nature Methods", pub_date="2026-04-30", sections={"body": "Xenium " + "body text " * 300}, fulltext_available=True))
    st.upsert_paper(_paper(2, "A Xenium atlas of the gut", "Xenium spatial transcriptomics of the gut.", journal="Cells", pub_date="2026-10-01"))
    for i in range(3, 6):  # the same dataset title deposited three times
        st.upsert_paper(_paper(i, "Xenium data from the ovary of a 19-year-old female", "", kind="preprint", journal="Figshare", doi=f"10.6084/m9.figshare.{i}", pub_date="2026-10-1" + str(i)))
    st.upsert_paper(_paper(6, "Xenium Slide Preparation v1", "", kind="preprint", journal="", doi="10.17504/protocols.io.6", pub_date="2026-10-16", extra={"work_type": "preprint"}))
    st.upsert_paper(_paper(7, "Reply to: Xenium specificity", "To the Editor: Xenium ...", journal="Nature Methods", doi="10.1038/s41592-7", pub_date="2026-10-15"))
    st.replace_mentions("doi:10.1000/r1", [_xen("doi:10.1000/r1", "title"), _xen("doi:10.1000/r1", "abstract")])
    st.replace_mentions("doi:10.1000/r2", [_xen("doi:10.1000/r2", "title")])
    st.save_score("doi:10.1000/r1", 7.0, None, 7.2, "journal tier A")
    st.save_score("doi:10.1000/r2", 2.0, None, 2.5, "journal unranked")
    st.save_score("doi:10.1000/r3", 1.0, None, 1.0, "preprint")
    st.commit()
    return st


def test_search_puts_papers_first_folds_titles_and_sorts(tmp_path):
    st = _search_store(tmp_path)
    rows = st.search("Xenium", limit=50)
    ids = [r["id"] for r in rows]
    assert ids[:2] == ["doi:10.1000/r2", "doi:10.1000/r1"] or ids[:2] == ["doi:10.1000/r1", "doi:10.1000/r2"]  # the two papers lead, bm25 decides between them
    assert [r["record_type"] for r in rows[:2]] == [None, None]
    tail = {r["id"]: r for r in rows[2:]}
    assert set(tail) == {"doi:10.1000/r3", "doi:10.1000/r6", "doi:10.1000/r7"}  # r4 and r5 folded into r3
    assert tail["doi:10.1000/r3"]["record_type"] == "deposit" and tail["doi:10.1000/r3"]["duplicates"] == 2
    assert tail["doi:10.1000/r6"]["record_type"] == "protocol" and tail["doi:10.1000/r7"]["record_type"] == "letter"
    assert all(r["duplicates"] == 0 for r in rows if r["id"] != "doi:10.1000/r3")
    assert len(st.search("Xenium", limit=50, collapse=False)) == 7
    assert len(st.search("Xenium", limit=2)) == 2
    by_score = st.search("Xenium", limit=50, sort="score")
    assert [r["id"] for r in by_score[:2]] == ["doi:10.1000/r1", "doi:10.1000/r2"] and by_score[0]["score"] == 7.2
    by_date = st.search("Xenium", limit=50, sort="date")
    assert [r["id"] for r in by_date[:2]] == ["doi:10.1000/r2", "doi:10.1000/r1"]  # newest paper first; deposits still after the papers
    assert by_date[2]["record_type"] is not None
    with pytest.raises(ValueError):
        st.search("Xenium", sort="pagerank")
    assert st.search("nosuchcol:Xenium", limit=5) == [] and st.last_search_fallback  # the phrase fallback still works
    st.close()


@pytest.fixture
def search_env(tmp_path, monkeypatch):
    st = _search_store(tmp_path)
    st.close()
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "s.sqlite"))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)
    return tmp_path


def test_cli_search_sort_and_duplicates(search_env, capsys):
    assert cli.main(["search", "Xenium"]) == 0
    out = capsys.readouterr().out
    assert out.index("A Xenium atlas of the gut") < out.index("Xenium data from the ovary")
    assert "[repository deposit]  (+2 more with this title)" in out and "[protocol deposit]" in out and "[letter / correspondence]" in out
    assert cli.main(["search", "Xenium", "--sort", "score", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == "doi:10.1000/r1" and rows[0]["score"]["final"] == 7.2 and rows[0]["record_type"] is None
    assert cli.main(["search", "Xenium", "--keep-duplicates", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 7
    with pytest.raises(SystemExit):
        cli.main(["search", "Xenium", "--sort", "pagerank"])
    assert "invalid choice" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["search", "--help"])
    assert exc.value.code == 0 and "--sort" in capsys.readouterr().out
    assert tuple(SEARCH_SORTS) == ("relevance", "score", "date")


def test_api_search_sort_and_collapse(tmp_path):
    st = _search_store(tmp_path)
    st.close()
    digests = tmp_path / "digests"
    digests.mkdir()
    app = api.RadarAPI(tmp_path / "s.sqlite", digest_dir=digests, today=lambda: TODAY)
    server, _thread = api.start_in_thread(app)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    try:
        status, data = get("/search?q=Xenium")
        assert status == 200 and data["sort"] == "relevance" and data["collapse"] is True and data["count"] == 5
        assert data["results"][0]["record_type"] is None and data["results"][-1]["record_type"] is not None
        status, data = get("/search?q=Xenium&sort=score")
        assert status == 200 and data["results"][0]["id"] == "doi:10.1000/r1"
        status, data = get("/search?q=Xenium&sort=date&collapse=0")
        assert status == 200 and data["count"] == 7 and data["collapse"] is False and data["results"][0]["id"] == "doi:10.1000/r2"
        status, data = get("/search?q=Xenium&sort=pagerank")
        assert status == 400 and "sort must be one of" in data["error"]
        status, data = get("/api")
        assert "sort" in next(e["params"] for e in data["endpoints"] if e["path"] == "/search")
    finally:
        server.shutdown()
        server.server_close()


# ---- trends: like-for-like or say why not --------------------------------------------------------------------------
def _trend_store(tmp_path, recent_sections=("methods",), prior_sections=("abstract",), n=6, name="t.sqlite"):
    """`n` Xenium papers in the recent 30 days (2026-09-18..10-17) and `n` in the prior 30 (08-19..09-17)."""
    st = Store(tmp_path / name)
    k = 0
    for day, sections in ((f"2026-10-{d:02d}", recent_sections) for d in range(1, n + 1)):
        k += 1
        st.upsert_paper(_paper(k, f"Recent paper {k}", pub_date=day))
        st.replace_mentions(f"doi:10.1000/r{k}", [_xen(f"doi:10.1000/r{k}", s) for s in sections])
    for day, sections in ((f"2026-09-{d:02d}", prior_sections) for d in range(1, n + 1)):
        k += 1
        st.upsert_paper(_paper(k, f"Prior paper {k}", pub_date=day))
        st.replace_mentions(f"doi:10.1000/r{k}", [_xen(f"doi:10.1000/r{k}", s) for s in sections])
    st.commit()
    return st


def test_show_trend_is_voided_when_section_coverage_differs(tmp_path):
    st = _trend_store(tmp_path)
    res = entities.resolve(st, "txg-xenium", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, res, "2026-07-01", "2026-10-17", limit=3, instruments=INSTRUMENTS)
    t = d["trend"]
    assert t["recent_30d"] == 6 and t["prior_30d"] == 6 and t["change_pct"] is None and t["basis"] == "all sections"
    assert t["coverage"] == {"recent": {"papers": 6, "title_abstract": 0, "corpus": 6}, "prior": {"papers": 6, "title_abstract": 6, "corpus": 6}}
    assert t["like_for_like"] == {"basis": "title/abstract", "recent_30d": 0, "prior_30d": 6, "change_pct": -100.0}
    assert "not like-for-like" in t["note"] and "6 of 6 recent papers were matched only outside the title/abstract" in t["note"]
    text = entities.render_dossier(d)
    assert "trend: last 30 days 6 vs prior 30 days 6 (change n/a, see note)" in text
    assert "like-for-like (title/abstract mentions only): last 30 days 0 vs prior 30 days 6 (-100.0%)" in text
    assert not st.conn.in_transaction
    json.dumps(d)
    st.close()


def test_show_trend_keeps_the_percentage_when_coverage_matches(tmp_path):
    st = _trend_store(tmp_path, recent_sections=("abstract", "methods"), prior_sections=("abstract",))
    res = entities.resolve(st, "txg-xenium", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, res, "2026-07-01", "2026-10-17", limit=3, instruments=INSTRUMENTS)
    t = d["trend"]
    # every paper names the platform in the abstract, so the Methods hits do not change the section mix
    assert t["change_pct"] == 0.0 and "note" not in t and t["like_for_like"]["recent_30d"] == 6
    assert "trend: last 30 days 6 vs prior 30 days 6 (+0.0%)" in entities.render_dossier(d)
    # too few papers to judge the mix: the raw percentage stands
    small = _trend_store(tmp_path, n=3, name="small.sqlite")
    d2 = entities.dossier(small, entities.resolve(small, "txg-xenium", INSTRUMENTS, COMPANIES), "2026-07-01", "2026-10-17", instruments=INSTRUMENTS)
    assert d2["trend"]["change_pct"] == 0.0 and "note" not in d2["trend"]
    st.close()
    small.close()


def test_show_trend_is_voided_when_the_corpus_changes_size(tmp_path):
    st = _trend_store(tmp_path, recent_sections=("abstract",), prior_sections=("abstract",))
    # 300 unrelated papers in the recent period against 100 in the prior one: a backfill boundary, not a trend
    k = 100
    for d in range(1, 31):
        for _ in range(10):
            k += 1
            st.upsert_paper(_paper(k, f"Filler {k}", pub_date=f"2026-10-{min(d, 17):02d}" if d <= 17 else f"2026-09-{d:02d}"))
    for d in range(1, 11):
        for _ in range(10):
            k += 1
            st.upsert_paper(_paper(k, f"Older filler {k}", pub_date=f"2026-08-{19 + d:02d}"))
    st.commit()
    res = entities.resolve(st, "txg-xenium", INSTRUMENTS, COMPANIES)
    t = entities.dossier(st, res, "2026-07-01", "2026-10-17", instruments=INSTRUMENTS)["trend"]
    assert t["change_pct"] is None and "the store holds" in t["note"] and t["coverage"]["recent"]["corpus"] >= 300
    assert t["like_for_like"]["change_pct"] == 0.0
    st.close()


def test_company_monthly_series_carries_the_subject_count_and_a_coverage_note(tmp_path, capsys):
    st = Store(tmp_path / "c.sqlite")
    for i in range(1, 7):   # October: supplier mentions only (full text harvested)
        st.upsert_paper(_paper(i, f"October paper {i}", pub_date=f"2026-10-{i:02d}"))
        st.replace_mentions(f"doi:10.1000/r{i}", [Mention(f"doi:10.1000/r{i}", "company", "illumina", "Illumina", "", "ILMN", "", "methods", 2, "...")])
    for i in range(7, 13):  # September: abstract mentions only (abstract-only backfill)
        st.upsert_paper(_paper(i, f"September paper {i}", pub_date=f"2026-09-{i:02d}"))
        st.replace_mentions(f"doi:10.1000/r{i}", [Mention(f"doi:10.1000/r{i}", "company", "illumina", "Illumina", "", "ILMN", "", "abstract", 1, "...")])
    st.commit()
    entry = C.normalize_entry({"id": "illumina", "name": "Illumina", "ticker": "ILMN", "type": "tools"})
    d = C.company_detail(st, entry, today=TODAY, months=3)
    assert d["monthly"] == [
        {"month": "2026-08", "papers": 0, "mentions": 0, "subject": 0},
        {"month": "2026-09", "papers": 6, "mentions": 6, "subject": 6},
        {"month": "2026-10", "papers": 6, "mentions": 12, "subject": 0},
    ]
    cov = d["coverage"]
    assert cov["basis"] == "title/abstract" and cov["deep_share"] == {"2026-09": 0.0, "2026-10": 1.0}
    assert "in 2026-10 6 of 6 papers were matched only outside the title/abstract" in cov["note"] and "0 of 6 in 2026-09" in cov["note"]
    _print_detail(d)
    out = capsys.readouterr().out
    assert "by month: 2026-09 6  2026-10 6" in out and "by month (title/abstract only): 2026-09 6  2026-10 0" in out and "note: section coverage differs" in out
    # a flat section mix: no note, and the series still carries `subject`
    assert C.coverage_note([{"month": "2026-09", "papers": 6, "subject": 6}, {"month": "2026-10", "papers": 8, "subject": 7}]) is None
    assert C.coverage_note([{"month": "2026-09", "papers": 2, "subject": 0}, {"month": "2026-10", "papers": 8, "subject": 8}]) is None  # too few papers in one month
    json.dumps(d)
    st.close()


# ---- docs: every description of the heuristic states the shipped weights ------------------------------------------
def test_docs_describe_the_shipped_ranking_weights():
    root = Path(__file__).resolve().parent.parent
    w = H.DEFAULT_WEIGHTS
    tiers = f"A = {w['tier_A']:.1f}, B = {w['tier_B']:.1f}, C = {w['tier_C']:.1f}, preprint = {w['tier_preprint']:.1f}"
    readme = (root / "README.md").read_text(encoding="utf-8")
    design = (root / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    gazetteer = (root / "docs" / "gazetteer.md").read_text(encoding="utf-8")
    ranking = (root / "docs" / "ranking.md").read_text(encoding="utf-8")
    assert tiers in readme and tiers in design
    assert f"tier A +{w['tier_A']:.1f}, B +{w['tier_B']:.1f}" in ranking
    for text, name in ((readme, "README"), (design, "DESIGN"), (gazetteer, "gazetteer")):
        # the v1 table (A = 4, +1.0 per platform with cap 3, +0.5 per company with cap 3) survives only as history, labelled as such
        for line in text.splitlines():
            if "A = 4" in line or "per distinct platform" in line or "+0.5 per company" in line or "weight 1.0 per distinct entity" in line:
                assert "v1" in line or "v1" in text[max(0, text.index(line) - 200): text.index(line)], (name, line)
    assert "topical" in readme and "topical gate" in design and "not a research paper" in design and "non_research_penalty" in ranking
    assert f"+{w['tracked_author']:.1f} for a tracked author matched by OpenAlex id (+{w['tracked_author_name']:.1f} by name only)" in readme
    assert "docs/ranking.md" in readme
