"""Regression tests for the data-layer fixes: papers-only ingest (OpenAlex work type and component titles), the
preprint kind, one work = one row (version DOIs, DOI-less twins), backfill tags the tagger can read, tracked-author
gates (non-paper records, ORCID, topical plausibility), RSS item dates and the Europe PMC index-date window."""
import json
from datetime import date, datetime, timezone

from litradar import backfill, concepts, i18n
from litradar import pipeline as pl
from litradar.config import Settings
from litradar.models import Author, Paper, canonical_doi, is_component_title, is_paper_record, normalize_doi
from litradar.pipeline import Pipeline
from litradar.sources import europepmc, openalex, rss
from litradar.store import Store, twin_title_key

WINDOW = ("2026-10-10", "2026-10-17")
CONFIG = concepts.Path(__file__).resolve().parent.parent / "config"


class FakeHttp:
    stats = {"requests": 0, "failures": 0}

    def __init__(self, text=""):
        self._text = text

    def get(self, *a, **k):
        return None

    def get_json(self, *a, **k):
        return None

    def get_text(self, *a, **k):
        return self._text

    def post_json(self, *a, **k):
        return None


def _settings(tmp_path, **radar):
    s = Settings.from_env()
    s.db_path = tmp_path / "p.sqlite"
    s.radar = {"sources": {}, "watchlist_terms": [], "digest": {"top_n": 10}}
    s.radar.update(radar)
    return s


def _work(oa_id, title, wtype="article", source=("Nature Methods", "journal"), doi=None, authorships=None, language="en"):
    return {
        "id": f"https://openalex.org/{oa_id}", "doi": f"https://doi.org/{doi}" if doi else None, "title": title,
        "publication_date": "2026-10-12", "type": wtype, "language": language,
        "primary_location": {"source": {"display_name": source[0], "type": source[1]}, "landing_page_url": "https://x"},
        "authorships": authorships or [], "abstract_inverted_index": None,
        "topics": [{"id": "https://openalex.org/T11289", "display_name": "Topic"}],
        "primary_topic": {"id": "https://openalex.org/T11289", "display_name": "Topic", "score": 0.9}, "cited_by_count": 0,
    }


def _authorship(name, cc, position="first", aid="A1"):
    return {
        "author": {"id": f"https://openalex.org/{aid}", "display_name": name, "orcid": None}, "author_position": position,
        "countries": [cc], "institutions": [{"display_name": f"Univ {cc}", "ror": "r", "country_code": cc.lower(), "type": "education"}],
        "raw_affiliation_strings": [f"Univ {cc}"],
    }


# ---- DATA-03: version DOIs key one row ------------------------------------------------------------------------
def test_canonical_doi_folds_version_suffixes_of_known_publishers_only():
    assert canonical_doi("https://doi.org/10.7554/eLife.108336.3") == "10.7554/elife.108336"
    assert canonical_doi("10.7554/elife.108336.1") == canonical_doi("10.7554/elife.108336") == "10.7554/elife.108336"
    assert canonical_doi("10.7554/elife.00003.001") == "10.7554/elife.00003.001"  # three digits = a figure DOI, not a version
    assert canonical_doi("10.21203/rs.3.rs-1234567/v2") == canonical_doi("10.21203/rs.3.rs-1234567/v1") == "10.21203/rs.3.rs-1234567"
    assert canonical_doi("10.1038/s41586-026-0001-1") == "10.1038/s41586-026-0001-1"
    assert canonical_doi(None) is None and canonical_doi("not a doi") is None
    # the row key folds, the stored DOI does not: every version still resolves on its own
    assert Paper.make_id("openalex", "W1", "10.7554/eLife.108336.2") == "doi:10.7554/elife.108336"
    assert normalize_doi("10.7554/eLife.108336.2") == "10.7554/elife.108336.2"
    assert Paper.make_id("europepmc", "MED:1", None) == "europepmc:MED:1"


# ---- CLI-08 / D1 / DATA-01: what counts as a paper ---------------------------------------------------------------
def test_component_titles_and_work_types_are_not_papers():
    for t in (
        "Figure S5 from Parallel 4-1BB Signaling in Dual DLL3/CD56-Targeting CAR T Cells",
        "Data from Parallel 4-1BB Signaling in Dual DLL3/CD56-Targeting CAR T Cells",
        "Table 2 from Late-Stage Outcomes as Surrogates for Mortality in Cancer Screening Trials",
        "Supplementary Figures S1-S51 from CDState Resolves Malignant Cell Heterogeneity",
        'Supplementary material from "Comparative genomics reveals contrasting candidate adaptive signatures"',
        "Table S1. Genome datasets and assembly statistics for the 12 species used in this study. from Comparative genomics",
        "Supplementary Data from A Spatial Atlas", "Video 1 from A Spatial Atlas",
    ):
        assert is_component_title(t), t
    for t in (
        "Data from a multi-year targeted proteomics study of a longitudinal birth cohort",  # a data descriptor paper
        "Data integration from multiple atlases", "Lessons from the clinic", "Figures of merit from single-cell data",
        "Spatial transcriptomics of tumours", "", None,
    ):
        assert not is_component_title(t), t
    assert is_paper_record("article", "Spatial transcriptomics of tumours")
    assert is_paper_record("", "Any title") and is_paper_record(None, None)  # sources without a work type
    for wt in ("dataset", "supplementary-materials", "other", "paratext", "peer-review", "software", "dissertation", "erratum"):
        assert not is_paper_record(wt, "Any title"), wt
    assert not is_paper_record("review", "Figure S2 from A Review")  # portals type component records as review / article
    assert openalex.TYPE_FILTER == "article|preprint|review"


# ---- DATA-02 / DATA-01: kind and work_type from OpenAlex -------------------------------------------------------------
def test_openalex_to_paper_kind_follows_the_type_not_the_repository():
    deposit = openalex.to_paper(_work("W1", "Visium data from the ovary", "dataset", ("Zenodo (CERN European Organization for Nuclear Research)", "repository")))
    assert deposit.kind == "article" and deposit.extra["work_type"] == "dataset"
    assert not openalex.is_paper_work(_work("W1", "Visium data from the ovary", "dataset", ("Zenodo", "repository")))
    rsq = openalex.to_paper(_work("W2", "A reviewed preprint", "preprint", ("Research Square", "repository")))
    assert rsq.kind == "preprint" and rsq.extra["work_type"] == "preprint"
    biorxiv = openalex.to_paper(_work("W3", "An older posting", "article", ("bioRxiv (Cold Spring Harbor Laboratory)", "repository")))
    assert biorxiv.kind == "preprint"  # a known preprint server, whatever the type says
    inst = openalex.to_paper(_work("W4", "A thesis chapter", "article", ("DSpace@MIT (Massachusetts Institute of Technology)", "repository")))
    assert inst.kind == "article"  # an institutional repository is not a preprint server
    software = openalex.to_paper(_work("W5", "teamtomo", "software", ("Zenodo", "repository")))
    assert software.kind == "article" and software.extra["work_type"] == "software"
    with_cc = openalex.to_paper(_work("W6", "Paper", authorships=[_authorship("A", "US"), _authorship("B", "cn", "middle", "A2")]))
    assert [i["country_code"] for a in with_cc.authors for i in a.institutions] == ["US", "CN"]  # kept for i18n.country_of
    assert i18n.country_of(with_cc) == ["CN", "US"]  # exact codes, ties alphabetical


# ---- DATA-01: both backfill routes keep non-papers out -------------------------------------------------------------
def test_backfill_type_filter_and_snapshot_gate(tmp_path):
    group = {"id": "g", "openalex_topics": ["T11289"], "keywords": ["spatial transcriptomics"]}
    flt, _ = backfill.subgroup_filters(group, "2026-10")
    assert flt["type"] == "article|preprint|review"
    matcher = backfill.SnapshotMatcher([group], topics_only=True)
    assert matcher.match(_work("W1", "Spatial transcriptomics atlas")) == ["g"]
    assert matcher.match(_work("W2", "Spatial transcriptomics atlas", "dataset", ("Zenodo", "repository"))) == []
    assert matcher.match(_work("W3", "Figure S1 from Spatial Transcriptomics Atlas", "review")) == []
    # the API route: a component record typed review slips through the type filter and is skipped on parse
    from tests.test_backfill import FakeResp, page

    class Http:
        stats = {"requests": 0, "failures": 0}

        def __init__(self):
            self.responses = [FakeResp(200, page([_work("W4", "Spatial transcriptomics atlas", doi="10.1000/ok"), _work("W5", "Figure S1 from Spatial Transcriptomics Atlas", "review", doi="10.1000/fig")]))]

        def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,)):
            return self.responses.pop(0) if self.responses else None

    st = Store(tmp_path / "b.sqlite")
    rep = backfill.run_api(st, Http(), [group], "2026-10", "2026-10", do_probe=False)
    assert rep["papers_fetched"] == 2 and rep["papers_new"] == 1 and rep["skipped_non_paper"] == 1 and rep["papers_updated"] == 0
    assert st.get_paper("doi:10.1000/ok") is not None and st.get_paper("doi:10.1000/fig") is None
    st.close()


# ---- CLI-07: backfill tags are the tagger's tags ----------------------------------------------------------------------
def test_backfill_tags_are_readable_by_i18n_and_keep_first_author_first(tmp_path):
    w = _work("W1", "A spatial atlas of the ovary", doi="10.1000/t1", authorships=[_authorship("A", "US", "first", "A1"), _authorship("B", "CN", "middle", "A2"), _authorship("C", "CN", "last", "A3")])
    st = Store(tmp_path / "t.sqlite")
    assert backfill.upsert_work(st, w, ["g"], "api") is True
    extra = json.loads(st.get_paper("doi:10.1000/t1")["extra_json"])
    assert extra["countries"] == ["US", "CN"] and extra["first_author_countries"] == ["US"]  # first author first, not alphabetical
    assert extra["lang"] == "en" and extra["language"] == "en" and extra["work_type"] == "article"
    h = i18n.histograms(st)
    assert h["tagged"] == 1 and h["untagged"] == 0 and h["primary_countries"] == {"US": 1} and h["languages"] == {"en": 1}
    # a re-tag recomputes from the exact codes (CN has two authorships) but keeps the first author in front
    i18n.tag_store(st)
    extra = json.loads(st.get_paper("doi:10.1000/t1")["extra_json"])
    assert extra["countries"] == ["US", "CN"] and extra["lang"] == "en"
    # no OpenAlex language: the house detector fills `lang`
    zh = _work("W2", "基于单细胞测序的肿瘤微环境异质性研究", doi="10.1000/t2", language=None, authorships=[_authorship("A", "CN")])
    backfill.upsert_work(st, zh, ["g"], "api")
    assert json.loads(st.get_paper("doi:10.1000/t2")["extra_json"])["lang"] == "zh"
    st.close()


def test_histograms_accept_openalex_language_and_tag_store_keeps_backfill_codes(tmp_path):
    st = Store(tmp_path / "t.sqlite")

    def paper(i, **extra):
        return Paper(id=f"doi:10.1000/h{i}", source="openalex", title=f"Paper {i} on spatial transcriptomics of tumours", pub_date="2026-10-12",
                     authors=[Author(name="A", affiliation="Somewhere in Paris")], affiliations=["Somewhere in Paris, France"], extra=extra)

    st.upsert_paper(paper(1, language="en", backfill_source="api", countries=["CN", "US"], first_author_countries=["CN"]))  # older backfill row
    st.upsert_paper(paper(2))  # untagged
    st.upsert_paper(paper(3, backfill_source="api", countries=[]))  # backfill row whose work carried no codes
    h = i18n.histograms(st)
    assert h["tagged"] == 1 and h["untagged"] == 2 and h["languages"] == {"en": 1} and h["primary_countries"] == {"CN": 1}
    rep = i18n.tag_store(st)
    assert rep["tagged"] == 3
    by_id = {pid: json.loads(extra_json) for pid, extra_json in st.conn.execute("SELECT id, extra_json FROM papers")}
    assert by_id["doi:10.1000/h1"]["countries"] == ["CN", "US"]  # OpenAlex codes kept, not replaced by the Paris guess
    assert by_id["doi:10.1000/h2"]["countries"] == ["FR"] and by_id["doi:10.1000/h2"]["lang"] == "en"
    assert by_id["doi:10.1000/h3"]["countries"] == ["FR"]  # nothing to keep: the affiliation strings decide
    assert i18n.histograms(st)["untagged"] == 0
    st.close()


# ---- DATA-01 / CLI-08: the live OpenAlex route ------------------------------------------------------------------------
def test_pipeline_openalex_queries_carry_the_type_filter_and_skip_non_papers(tmp_path, monkeypatch):
    settings = _settings(tmp_path, sources={"openalex": {"enabled": True, "topics": True, "company_authors": False, "tracked_authors": False, "journals": False, "country_slices": []}})
    pipe = Pipeline(settings, http=FakeHttp())
    pipe.subgroups = [{"id": "g", "openalex_topics": ["T11289"], "keywords": []}]
    seen = []

    def works(http, filters, *a, state=None, **k):
        seen.append(dict(filters))
        if state is not None:
            state["complete"] = True
        return iter([
            _work("W1", "Spatial transcriptomics atlas", doi="10.1000/a"),
            _work("W2", "Data for EMSL Project 50414 from September 2019", "dataset", ("Zenodo", "repository"), doi="10.1000/d"),
            _work("W3", "Figure S1 from Spatial Transcriptomics Atlas", "review", doi="10.1000/f"),
        ])

    monkeypatch.setattr(pl.openalex_src, "works", works)
    papers = pipe.fetch_papers(*WINDOW, only=["openalex"])
    assert seen and all(f["type"] == "article|preprint|review" for f in seen)
    assert set(papers) == {"doi:10.1000/a"} and pipe.counts["openalex.skipped_non_paper"] == 2


# ---- D2 / D9: tracked-author gates -----------------------------------------------------------------------------------
def _tracked_pipe(tmp_path):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    pipe.subgroups = [{"id": "spatial", "openalex_topics": ["T10001"], "keywords": ["spatial transcriptomics"],
                       "tracked_authors": [{"name": "Alex Smith", "openalex_id": "A1", "orcid": "0000-0001-2345-6789"}, {"name": "Pat Jones", "openalex_id": "", "orcid": ""}]}]
    pipe.tracked = {"alex smith": [("Alex Smith", "spatial")], "pat jones": [("Pat Jones", "spatial")]}
    pipe.tracked_display = {"alex smith": [("Alex Smith", "spatial")], "pat jones": [("Pat Jones", "spatial")]}
    pipe.tracked_ids = {"A1": [("Alex Smith", "spatial")]}
    pipe.tracked_id_for = {("Alex Smith", "spatial"): "A1"}
    pipe.tracked_orcid_for = {("Alex Smith", "spatial"): "0000-0001-2345-6789"}
    return pipe


def _p(i, title, *authors, **extra):
    return Paper(id=f"doi:10.1000/x{i}", source="openalex", title=title, doi=f"10.1000/x{i}", pub_date="2026-10-12", authors=list(authors), extra=extra)


def test_tracked_hits_skip_non_paper_records(tmp_path):
    pipe = _tracked_pipe(tmp_path)
    smith = Author(name="Alex Smith", openalex_id="A1")
    # an id match on a dataset deposit (DOE / EMSL style: 100 facility users listed as authors) is not a paper
    assert pipe.extract(_p(1, "Data for EMSL Project 50414 from September 2019", smith, *[Author(name=f"User {i}") for i in range(99)], work_type="dataset"))[1] == []
    assert pipe.extract(_p(2, "Figure S1 from Spatial Transcriptomics of Tumours", smith, work_type="supplementary-materials"))[1] == []
    assert pipe.extract(_p(3, "Figure S1 from Spatial Transcriptomics of Tumours", smith))[1] == []  # component title, no type recorded
    assert pipe.counts["tracked_hits.skipped_non_paper"] == 3
    # the same person on a real paper, off topic or not: an id is exact
    assert pipe.extract(_p(4, "Phylogenomics confirms Palaeoptera monophyly", smith, work_type="article"))[1] == [("Alex Smith", "spatial", "openalex_id")]
    pipe.store.close()


def test_name_matches_need_a_matching_orcid_and_topical_plausibility(tmp_path):
    pipe = _tracked_pipe(tmp_path)
    on_topic = "Spatial transcriptomics of tumours with a multiplexed assay"
    # full name, no ids, on topic: provisional hit as before
    assert pipe.extract(_p(1, on_topic, Author(name="Alex Smith")))[1] == [("Alex Smith", "spatial", "name_no_ids")]
    assert pipe.extract(_p(2, on_topic, Author(name="Pat Jones")))[1] == [("Pat Jones", "spatial", "name")]
    # the record's ORCID is another person's: dropped
    assert pipe.extract(_p(3, on_topic, Author(name="Alex Smith", orcid="0000-0009-9999-9999")))[1] == []
    # the configured ORCID, in URL form: kept
    assert pipe.extract(_p(4, on_topic, Author(name="Alex Smith", orcid="https://orcid.org/0000-0001-2345-6789")))[1] == [("Alex Smith", "spatial", "name_no_ids")]
    # a namesake in another field: the paper matches none of the subgroup's keywords or topics
    assert pipe.extract(_p(5, "Phylogenomics confirms Palaeoptera monophyly and illuminates the origin of winged insects", Author(name="Alex Smith")))[1] == []
    assert pipe.extract(_p(6, "Hymenoptera wing evolution", Author(name="Pat Jones")))[1] == []
    # the OpenAlex topic alone makes it plausible when the keywords are absent
    assert pipe.extract(_p(7, "An atlas", Author(name="Pat Jones"), primary_topic="T10001"))[1] == [("Pat Jones", "spatial", "name")]
    assert pipe.counts["tracked_hits.name_rejected_other_orcid"] == 1 and pipe.counts["tracked_hits.name_rejected_off_topic"] == 2
    # a subgroup without keywords or topics cannot judge plausibility: name matches pass as before
    pipe.subgroups = [{"id": "spatial", "tracked_authors": []}]
    assert pipe.extract(_p(8, "Hymenoptera wing evolution", Author(name="Pat Jones")))[1] == [("Pat Jones", "spatial", "name")]
    pipe.store.close()


def test_merge_prefers_the_author_list_that_carries_identifiers_on_a_tie():
    crossref = Paper(id="doi:10.1000/m", source="crossref", title="T", authors=[Author(name="A B", affiliation="Univ X"), Author(name="C D", affiliation="Univ Y")])
    oa = Paper(id="doi:10.1000/m", source="openalex", title="T", authors=[
        Author(name="A B", affiliation="Univ X", openalex_id="A1", institutions=[{"name": "Univ X"}]), Author(name="C D", affiliation="Univ Y", openalex_id="A2")])
    merged = Pipeline.merge(crossref, oa)
    assert [a.openalex_id for a in merged.authors] == ["A1", "A2"]
    # the richer list (more authors with metadata) still wins over identifiers
    longer = Paper(id="doi:10.1000/m", source="europepmc", title="T", authors=[Author(name=n, affiliation="U") for n in ("A B", "C D", "E F")])
    assert len(Pipeline.merge(merged, longer).authors) == 3


# ---- DATA-03: one work, one row -----------------------------------------------------------------------------------------
def _run(pipe, monkeypatch, papers):
    monkeypatch.setattr(pl.biorxiv_src, "fetch", lambda http, since, until, server="biorxiv", categories=None: iter(list(papers)))
    monkeypatch.setattr(pl.biorxiv_src, "attach_fulltext", lambda http, p: False)
    return pipe.run(*WINDOW, only=["biorxiv"], fulltext=False)


TITLE = "Product-stabilized filamentation by human glutamine synthetase"


def test_twin_title_key_ignores_short_or_generic_titles():
    assert twin_title_key("Correction") == "" and twin_title_key("Author Correction: a note") == ""
    assert twin_title_key(TITLE + ".") == twin_title_key(TITLE.upper()) == "product stabilized filamentation by human glutamine synthetase"


def test_fetch_folds_a_doi_less_record_onto_the_doi_row_in_the_same_batch(tmp_path, monkeypatch):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    epmc = Paper(id="europepmc:PMC:PMC13657631", source="europepmc", title=TITLE, pub_date="2026-10-16", abstract="Long abstract from Europe PMC.",
                 authors=[Author(name="A B", affiliation="Univ")], extra={"pmcid": "PMC13657631", "is_open_access": "Y"})
    crossref = Paper(id="doi:10.7554/elife.108336", source="crossref", title=TITLE, doi="10.7554/elife.108336.1", pub_date="2026-10-14", journal="eLife")
    other = Paper(id="doi:10.1000/other", source="crossref", title=TITLE, doi="10.1000/other", pub_date="2026-10-14")
    rsskey = Paper(id="rss:eLife:7ad268c9aa8b2aa7", source="rss:eLife", title=TITLE, pub_date="2026-10-15", url="https://elifesciences.org/x")
    papers = _run_fetch(pipe, monkeypatch, [epmc, crossref, other, rsskey])
    assert set(papers) == {"doi:10.7554/elife.108336", "doi:10.1000/other"}  # two records with different DOIs stay apart
    folded = papers["doi:10.7554/elife.108336"]
    assert folded.abstract == "Long abstract from Europe PMC." and folded.extra["pmcid"] == "PMC13657631" and folded.journal == "eLife"
    assert folded.doi == "10.7554/elife.108336.1" and pipe.counts["papers.twins_folded_in_batch"] == 2


def _run_fetch(pipe, monkeypatch, papers):
    monkeypatch.setattr(pl.biorxiv_src, "fetch", lambda http, since, until, server="biorxiv", categories=None: iter(list(papers)))
    return pipe.fetch_papers(*WINDOW, only=["biorxiv"])


def test_run_folds_a_stored_twin_in_either_direction(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    pipe = Pipeline(settings, http=FakeHttp())
    # 1. the store holds the DOI-less Europe PMC record (with a mention and a score); the DOI row arrives a run later
    epmc = Paper(id="europepmc:PMC:PMC13665939", source="europepmc", title=TITLE, pub_date="2026-10-16", abstract="We used the Xenium platform on 30 patients.",
                 authors=[Author(name="A B", affiliation="Univ")], extra={"pmcid": "PMC13665939"})
    _run(pipe, monkeypatch, [epmc])
    assert pipe.store.has_paper(epmc.id) and pipe.store.mentions_for(epmc.id)
    crossref = Paper(id="doi:10.1126/sciadv.ady1161", source="crossref", title=TITLE, doi="10.1126/sciadv.ady1161", pub_date="2026-10-14", journal="Science Advances")
    rep = _run(pipe, monkeypatch, [crossref])
    assert rep["counts"]["papers.twins_folded"] == 1
    assert not pipe.store.has_paper(epmc.id) and pipe.store.has_paper(crossref.id)
    row = pipe.store.get_paper(crossref.id)
    assert row["abstract"].startswith("We used the Xenium") and json.loads(row["extra_json"])["pmcid"] == "PMC13665939" and row["journal"] == "Science Advances"
    assert {m["entity_id"] for m in pipe.store.mentions_for(crossref.id)} == {"txg-xenium"}
    assert pipe.store.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
    assert pipe.store.conn.execute("SELECT COUNT(*) FROM papers_fts WHERE id=?", (epmc.id,)).fetchone()[0] == 0
    # 2. the reverse: the DOI row is stored, a DOI-less record for the same work comes in and takes the stored id
    again = Paper(id="europepmc:PMC:PMC13665939", source="europepmc", title=TITLE + ".", pub_date="2026-10-16", abstract="Longer abstract from Europe PMC about Xenium.",
                  authors=[Author(name="A B", affiliation="Univ"), Author(name="C D", affiliation="Univ")], extra={"pmcid": "PMC13665939"})
    rep = _run(pipe, monkeypatch, [again])
    assert rep["counts"]["papers.twins_folded"] == 2 and rep["counts"]["papers.reingested"] == 1  # counters accumulate on one Pipeline
    assert pipe.store.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1 and pipe.store.has_paper(crossref.id)
    assert pipe.store.get_paper(crossref.id)["abstract"].startswith("Longer abstract")
    # 3. a record with a different DOI and the same title is another work (preprint / article pairs fold at render time)
    preprint = Paper(id="doi:10.1101/2026.01.01.000001", source="biorxiv", kind="preprint", title=TITLE, doi="10.1101/2026.01.01.000001", pub_date="2026-10-11")
    _run(pipe, monkeypatch, [preprint])
    assert pipe.store.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
    pipe.store.close()


def test_store_find_twin_and_delete_paper(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_paper(Paper(id="doi:10.1000/a", source="crossref", title=TITLE, doi="10.1000/a", pub_date="2024-01-01"))
    st.upsert_paper(Paper(id="europepmc:MED:1", source="europepmc", title="Short title", pub_date="2024-01-01", extra={"pmcid": "PMC1"}))
    assert st.find_twin(Paper(id="europepmc:MED:9", source="europepmc", title=TITLE + "."))["id"] == "doi:10.1000/a"
    assert st.find_twin(Paper(id="europepmc:MED:9", source="europepmc", title=TITLE, pub_date="2026-10-01")) is None  # dates two years apart
    assert st.find_twin(Paper(id="doi:10.1000/b", source="crossref", title=TITLE, doi="10.1000/b")) is None  # both carry DOIs
    assert st.find_twin(Paper(id="openalex:W1", source="openalex", title="Other", extra={"pmcid": "PMC1"}))["id"] == "europepmc:MED:1"
    assert st.find_twin(Paper(id="europepmc:MED:1", source="europepmc", title="Short title", extra={"pmcid": "PMC1"})) is None  # itself
    assert st.delete_paper("europepmc:MED:1") is True and st.delete_paper("europepmc:MED:1") is False
    assert st.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1 and st.conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0] == 1
    st.close()


# ---- CLI-08 / D1: concept counts ------------------------------------------------------------------------------------
def test_concept_counts_ignore_component_records_and_non_paper_types(tmp_path):
    st = Store(tmp_path / "c.sqlite")
    excl = concepts.load_exclusions(CONFIG)
    article = "Parallel 4-1BB Signaling in Dual DLL3/CD56-Targeting CAR T Cells"
    st.upsert_paper(Paper(id="doi:10.1158/0008-5472.article", source="openalex", title=article, doi="10.1158/0008-5472.article", pub_date="2026-10-13",
                          abstract="Spatial transcriptomics and perturb-seq screens of CAR T cells.", extra={"work_type": "article"}))
    for n in range(1, 12):
        st.upsert_paper(Paper(id=f"doi:10.1158/0008-5472.3452040{n}", source="openalex", title=f"Figure S{n} from {article}", doi=f"10.1158/0008-5472.3452040{n}",
                              pub_date="2026-10-13", abstract="Spatial transcriptomics and perturb-seq screens of CAR T cells.", extra={"work_type": "supplementary-materials"}))
    st.upsert_paper(Paper(id="doi:10.1158/0008-5472.c.8775298", source="openalex", title=f"Data from {article}", doi="10.1158/0008-5472.c.8775298", pub_date="2026-10-13",
                          abstract="Spatial transcriptomics and perturb-seq screens of CAR T cells.", extra={"work_type": "dataset"}))
    st.upsert_paper(Paper(id="doi:10.1000/zenodo", source="openalex", title="JoVE Video Dataset", doi="10.1000/zenodo", pub_date="2026-10-13",
                          abstract="Spatial transcriptomics dataset.", extra={"work_type": "dataset"}))
    stats = concepts.compute_week_counts(st, ["2026-10-12"], excl)
    assert stats["papers"] == 1
    counted = {r[0] for r in st.conn.execute("SELECT DISTINCT paper_id FROM concept_papers")}
    assert counted == {"doi:10.1158/0008-5472.article"}
    assert max(r[0] for r in st.conn.execute("SELECT papers FROM concept_counts")) == 1
    st.close()


# ---- SRC-01: RSS item dates ------------------------------------------------------------------------------------------
RDF = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
  <channel rdf:about="https://example.org/loi/jco"><title>Table of Contents</title></channel>
  <item rdf:about="https://example.org/doi/abs/10.1200/JCO-26-00469?af=R">
    <title>Nested date</title><link>https://example.org/doi/abs/10.1200/JCO-26-00469?af=R</link>
    <dc:identifier>doi:10.1200/JCO-26-00469</dc:identifier>
    <dc:date>
       <dc:date>2026-09-08T07:00:00Z</dc:date>
    </dc:date>
    <dc:creator>A Person</dc:creator>
    <prism:coverDate>2026-10-20T07:00:00Z</prism:coverDate><prism:doi>10.1200/JCO-26-00469</prism:doi>
  </item>
  <item rdf:about="https://example.org/doi/abs/10.1200/JCO-26-00470">
    <title>Cover date only</title><link>https://example.org/doi/abs/10.1200/JCO-26-00470</link>
    <dc:date>   </dc:date>
    <prism:coverDate>2026-10-20T07:00:00Z</prism:coverDate><prism:doi>10.1200/JCO-26-00470</prism:doi>
  </item>
  <item rdf:about="https://example.org/doi/abs/10.1016/S1470-2045(26)00001-1">
    <title>In press, month precision</title><link>https://example.org/doi/abs/10.1016/S1470-2045(26)00001-1</link>
    <dc:date>2099-11</dc:date><prism:publicationDate>2099-11</prism:publicationDate>
  </item>
  <item rdf:about="https://example.org/doi/abs/10.1200/JCO-26-00471">
    <title>No date at all</title><link>https://example.org/doi/abs/10.1200/JCO-26-00471</link>
  </item>
</rdf:RDF>"""


def test_rss_dates_come_from_nested_and_prism_elements():
    items = {it["title"]: it for it in rss.parse_feed(RDF, feed_name="JCO")}
    assert items["Nested date"]["date"] == "2026-09-08" and items["Nested date"]["doi"] == "10.1200/jco-26-00469" and items["Nested date"]["authors"] == ["A Person"]
    assert items["Cover date only"]["date"] == "2026-10-20"
    today = datetime.now(timezone.utc).date().isoformat()  # the parser caps at the UTC date, not the local one
    assert items["In press, month precision"]["date"] == today  # a future issue month: the item is online now
    assert items["No date at all"]["date"] == ""
    assert rss._parse_date("2026-09", today=date(2026, 10, 18)) == "2026-09-01" and rss._parse_date("2026-13") == ""
    assert rss._parse_date("2026-10-20T07:00:00Z") == "2026-10-20"
    # the since filter now applies to dated items that used to come through undated every run
    papers = {p.title: p for p in rss.journal_papers(FakeHttp(RDF), "JCO", "https://example.org/feed", "Journal of Clinical Oncology", since="2026-10-01")}
    assert set(papers) == {"Cover date only", "In press, month precision", "No date at all"}
    assert papers["Cover date only"].pub_date == "2026-10-20" and papers["Cover date only"].id == "doi:10.1200/jco-26-00470"


# ---- SRC-03: Europe PMC window ----------------------------------------------------------------------------------------
def test_europepmc_window_collects_records_by_index_date_too():
    q = europepmc.journal_query(["0028-0836"], "2026-10-11", "2026-10-18")
    assert q == '(ISSN:"0028-0836") AND ((FIRST_PDATE:[2026-10-11 TO 2026-10-18]) OR (CREATION_DATE:[2026-10-11 TO 2026-10-18] AND FIRST_PDATE:[2026-09-11 TO 2026-10-18]))'
    assert europepmc.journal_query(["0028-0836"], "2026-10-11", "2026-10-18", index_window=False) == '(ISSN:"0028-0836") AND (FIRST_PDATE:[2026-10-11 TO 2026-10-18])'
    assert "CREATION_DATE" in europepmc.section_query("METHODS", ["NovaSeq"], "2026-10-11", "2026-10-18")
    assert europepmc.date_clause("now-7d", "now") == "((FIRST_PDATE:[now-7d TO now]) OR (CREATION_DATE:[now-7d TO now]))"  # no floor without an ISO start
    assert europepmc.INDEX_LAG_DAYS == 30
