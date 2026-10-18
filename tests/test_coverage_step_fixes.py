"""Four defects found on a production copy the week full-text parsing began: concept z-scores that ignored the coverage
step the trends module already detects, instrument-facility data deposits credited to tracked authors and counted as
papers, archive census rows carried by one study, and "first appearances" that were really a channel's first parse."""
from __future__ import annotations

import json

import pytest

from litradar import concepts as C
from litradar import discovery as D
from litradar import trends, worktype
from litradar.commands import maintenance
from litradar.config import Settings
from litradar.digest.render import (
    _archive_table,
    _concentration_note,
    _first_appearance_lines,
    _strategy_item,
    render,
)
from litradar.models import ArchiveRecord, Author, Mention, Paper
from litradar.pipeline import Pipeline
from litradar.store import Store

UNTIL = "2026-10-17"  # a Saturday: the current ISO week is 2026-10-12..18
WINDOW = ("2026-10-11", "2026-10-18")
EMSL_DOI = "10.25582/data.2019-08.1825814/17306905"
EMSL_VENUE = "DOE Pacific Northwest National Laboratory (PNNL) Repository"
EMSL_TITLE = "Data for EMSL Project 50414 from August 2019"


def _paper(pid: str, pub_date: str, source: str = "openalex", title: str = "A paper", **kw) -> Paper:
    base = dict(id=pid, source=source, kind="article", title=title, doi=pid[4:] if pid.startswith("doi:") else None,
                abstract="An abstract.", journal="Nature Methods", pub_date=pub_date, url=f"https://example.org/{pid}")
    base.update(kw)
    return Paper(**base)


# ---- defect 1: the coverage step reaches the concepts z-scores ---------------------------------------------------------
def test_period_coverage_is_generic_over_the_period_length():
    assert trends.period_coverage(None, "2026-10-03", "2026-10-10") == {"covered_from": None, "days_covered": 8, "partial": False}
    assert trends.period_coverage("2024-01-01", "2026-10-03", "2026-10-10")["partial"] is False
    assert trends.period_coverage("2026-10-10", "2026-10-03", "2026-10-10") == {"covered_from": "2026-10-10", "days_covered": 1, "partial": True}
    assert trends.period_coverage("2026-10-11", "2026-10-03", "2026-10-10") == {"covered_from": "2026-10-11", "days_covered": 0, "partial": True}
    # the seven-day form compute_trends uses is unchanged
    assert trends.period_coverage("2026-10-08", "2026-10-04", "2026-10-10") == {"covered_from": "2026-10-08", "days_covered": 3, "partial": True}


def _seed_step(tmp_path, baseline_weeks: int = 10):
    """Concept counts for `baseline_weeks` + 4 weeks, over papers whose baseline weeks are one source (a backfill) and
    whose current week is three (the live pull): the trends coverage step."""
    st = Store(tmp_path / "step.sqlite")
    C.ensure_schema(st)
    weeks = C.weeks_ending(UNTIL, baseline_weeks + 4)
    rows = [("organoid intelligence", wk, 3, "organoid intelligence") for wk in weeks]
    st.conn.executemany("INSERT INTO concept_counts (term, week, papers, display, computed_at) VALUES (?,?,?,?,?)", [r + ("now",) for r in rows])
    n = 0
    for wk in weeks[:-1]:
        for _ in range(5):
            st.upsert_paper(_paper(f"doi:10.1000/b{n}", wk, source="openalex"))
            n += 1
    cur = weeks[-1]
    for src, k in (("openalex", 2), ("biorxiv", 10), ("crossref", 10)):
        for _ in range(k):
            st.upsert_paper(_paper(f"doi:10.1000/c{n}", cur, source=src))
            n += 1
    st.commit()
    return st, weeks


def test_paper_week_comparability_drops_single_source_backfill_weeks(tmp_path):
    st, weeks = _seed_step(tmp_path)
    info = trends.paper_week_comparability(st, "2026-10-18", weeks=13)
    assert info["week_start"] == "2026-10-12" and info["current_week_sources"] == {"biorxiv": 10, "crossref": 10, "openalex": 2}
    assert info["weeks_with_papers"] == list(range(1, 14)) and info["comparable_weeks"] == []
    dropped = {d["week"]: d for d in info["dropped_source_mix"]}
    assert set(dropped) == set(range(1, 14)) and dropped[4]["missing"] == ["biorxiv", "crossref"] and dropped[4]["covered_share"] == round(2 / 22, 3)
    assert info["week_bounds"][4] == ["2026-09-14", "2026-09-20"]  # bucket 4 is the ISO week four weeks back
    st.conn.execute("UPDATE papers SET source='openalex'")
    st.commit()
    same = trends.paper_week_comparability(st, "2026-10-18", weeks=13)
    assert same["comparable_weeks"] == list(range(1, 14)) and same["dropped_source_mix"] == []
    with pytest.raises(ValueError):
        trends.paper_week_comparability(st, "2026-10-18", weeks=0)
    st.close()


def test_concepts_switch_to_a_frequency_list_on_the_coverage_step(tmp_path):
    st, weeks = _seed_step(tmp_path)
    base, cur = weeks[:-4], weeks[-4:]
    rep = C.burst_report(st, UNTIL, baseline_weeks=10, current_weeks=4, min_papers=5, with_details=False)
    assert rep["baseline_active_weeks"] == 10 and rep["baseline_comparable_weeks"] == 0
    assert rep["mode"] == "frequency_list" and rep["mode_reason"] == "coverage_step" and rep["weak_baseline"] is True
    comp = rep["comparability"]
    assert comp["rule"] == "litradar.trends.paper_week_comparability" and comp["current_week"] == cur[-1]
    assert [d["week"] for d in comp["dropped_source_mix"]] == base and comp["current_week_sources"]["biorxiv"] == 10
    term = rep["risers"][0]
    assert term["papers"] == 12 and term["baseline_mean"] == 0.0 and term["baseline_mean_all_weeks"] == 3.0 and term["z"] == 12.0
    md = C.render_report_markdown(rep, top=5, with_examples=False)
    assert "Coverage step — this is a **frequency list, not burst detection**" in md
    assert "10 baseline week(s) hold papers but only 0 of them are comparable with the current week (8 are needed)" in md
    assert "lack biorxiv, crossref" in md and "the rule the 'What changed' section applies" in md
    assert "baseline weeks with data: 10 (0 comparable with the current week)" in md
    assert "| Baseline /wk (not comparable) | Score (count-driven) |" in md and "| 3.0 | 12.0 |" in md
    # the same counts over a uniform source mix: every baseline week comparable, burst detection resumes
    st.conn.execute("UPDATE papers SET source='openalex'")
    st.commit()
    rep2 = C.burst_report(st, UNTIL, baseline_weeks=10, current_weeks=4, min_papers=5, with_details=False)
    assert rep2["mode"] == "burst" and rep2["mode_reason"] is None and rep2["baseline_comparable_weeks"] == 10
    assert rep2["comparability"]["dropped_source_mix"] == []
    t2 = rep2["risers"][0]
    assert t2["baseline_mean"] == 3.0 and t2["z"] == 0.0  # 12 papers over 4 weeks is exactly the baseline rate
    md2 = C.render_report_markdown(rep2, top=5, with_examples=False)
    assert "frequency list" not in md2 and "Coverage step" not in md2 and "| Baseline /wk | Score |" in md2
    assert "baseline weeks with data: 10." in md2
    st.close()


def test_few_weeks_reason_is_kept_apart_from_the_coverage_step(tmp_path):
    st = Store(tmp_path / "short.sqlite")
    C.ensure_schema(st)
    weeks = C.weeks_ending(UNTIL, 5)
    st.conn.executemany("INSERT INTO concept_counts (term, week, papers, display, computed_at) VALUES (?,?,?,?,?)",
                        [("spatial proteomics", wk, 2, "spatial proteomics", "now") for wk in weeks])
    for i, wk in enumerate(weeks):
        st.upsert_paper(_paper(f"doi:10.1000/s{i}", wk))
    st.commit()
    rep = C.burst_report(st, UNTIL, baseline_weeks=26, current_weeks=4, min_papers=5, with_details=False)
    assert rep["mode"] == "frequency_list" and rep["mode_reason"] == "few_weeks" and rep["baseline_comparable_weeks"] == 1
    md = C.render_report_markdown(rep, top=5, with_examples=False)
    assert "Weak baseline — this is a **frequency list, not burst detection**: only 1 baseline week(s) hold papers" in md
    assert "Coverage step" not in md
    st.close()


def test_concept_counts_skip_facility_deposits_that_carry_no_work_type():
    assert C._is_paper({"title": EMSL_TITLE, "doi": EMSL_DOI, "journal": EMSL_VENUE, "extra_json": "{}"}) is False
    assert C._is_paper({"title": EMSL_TITLE, "extra_json": None}) is False  # the title alone
    assert C._is_paper({"title": "A spatial atlas", "doi": "10.1038/s41592-1", "journal": "Nature Methods", "extra_json": "{}"}) is True
    assert C._is_paper({"title": "Reply to: a comment", "doi": "10.1073/pnas.1", "extra_json": "{}"}) is True  # writing, not a deposit


# ---- defect 2: facility data deposits are deposits ------------------------------------------------------------------
@pytest.mark.parametrize("kw, evidence", [
    (dict(doi=EMSL_DOI, journal=EMSL_VENUE, title=EMSL_TITLE), "doi prefix 10.25582"),
    (dict(journal=EMSL_VENUE), "venue (pnnl) repository"),
    (dict(journal="Environmental Molecular Sciences Laboratory"), "venue environmental molecular sciences laboratory"),
    (dict(title=EMSL_TITLE), "title"),
    (dict(doi=EMSL_DOI, work_type="preprint"), "doi prefix 10.25582"),  # a facility repository never holds a preprint
])
def test_classify_recognises_facility_deposits(kw, evidence):
    args = dict(title="A record", abstract="", doi="", journal="", work_type="")
    args.update(kw)
    rt = worktype.classify(**args)
    assert rt is not None and rt.label == "facility_deposit" and rt.evidence == evidence
    assert rt.phrase == "instrument-facility data deposit" and worktype.is_deposit(rt)


def test_deposit_labels_and_the_restricted_sql_predicate_agree_with_classify(tmp_path):
    assert worktype.is_deposit(None) is False
    assert worktype.is_deposit(worktype.classify(doi="10.1056/nejmc2610854")) is False  # a letter is writing
    assert worktype.is_deposit(worktype.classify(doi="10.5281/zenodo.1", journal="Zenodo")) is True
    assert worktype.is_deposit(worktype.classify(doi="10.5281/zenodo.1", journal="Zenodo", work_type="preprint")) is False
    assert worktype.sql_predicate("p", labels=()) == "(0)"
    st = Store(tmp_path / "w.sqlite")
    rows = [
        _paper("doi:" + EMSL_DOI, "2026-10-16", kind="preprint", title=EMSL_TITLE, journal=EMSL_VENUE),
        _paper("doi:10.1056/nejmc2610854", "2026-10-12", journal="New England Journal of Medicine", abstract="To the Editor: we read ..."),
        _paper("doi:10.5281/zenodo.1", "2026-10-12", journal="Zenodo (CERN European Organization for Nuclear Research)", kind="preprint"),
        _paper("doi:10.5281/zenodo.2", "2026-10-12", journal="Zenodo (CERN European Organization for Nuclear Research)", kind="preprint", extra={"work_type": "preprint"}),
        _paper("doi:10.25345/c5x", "2026-10-12", title="MassIVE MSV000103560 - Scanning DIA", journal="UC San Diego"),
        _paper("doi:10.1000/gh", "2026-10-12", title="github.com/example/pipeline"),
        _paper("doi:10.1038/s41592-1", "2026-10-12", title="A platform paper"),
    ]
    for p in rows:
        st.upsert_paper(p)
    st.commit()
    deposit_sql = {r[0]: bool(r[1]) for r in st.conn.execute(f"SELECT p.id, {worktype.sql_predicate('p', labels=worktype.DEPOSIT_LABELS)} FROM papers p")}
    any_sql = {r[0]: bool(r[1]) for r in st.conn.execute(f"SELECT p.id, {worktype.sql_predicate('p')} FROM papers p")}
    for p in rows:
        rt = worktype.classify(p.title, p.abstract, p.doi, p.journal, (p.extra or {}).get("work_type", ""))
        assert deposit_sql[p.id] == worktype.is_deposit(rt), p.id
        assert any_sql[p.id] == (rt is not None), p.id
    assert deposit_sql["doi:" + EMSL_DOI] and deposit_sql["doi:10.25345/c5x"] and deposit_sql["doi:10.1000/gh"]
    assert not deposit_sql["doi:10.1056/nejmc2610854"] and any_sql["doi:10.1056/nejmc2610854"]  # flagged, but not as a deposit
    assert not deposit_sql["doi:10.5281/zenodo.2"] and not deposit_sql["doi:10.1038/s41592-1"]
    assert sum(deposit_sql.values()) == 4
    st.close()


class _FakeHttp:
    stats = {"requests": 0, "failures": 0}

    def get(self, *a, **k):
        return None

    def get_json(self, *a, **k):
        return None

    def get_text(self, *a, **k):
        return ""

    def post_json(self, *a, **k):
        return None


def _tracked_pipe(tmp_path) -> Pipeline:
    s = Settings.from_env()
    s.db_path = tmp_path / "p.sqlite"
    s.radar = {"sources": {}, "watchlist_terms": [], "digest": {"top_n": 10}}
    pipe = Pipeline(s, http=_FakeHttp())
    pipe.subgroups = [{"id": "structural", "openalex_topics": [], "keywords": ["cryo-em"],
                       "tracked_authors": [{"name": "Alex Smith", "openalex_id": "A1", "orcid": ""}]}]
    pipe.tracked = {"alex smith": [("Alex Smith", "structural")]}
    pipe.tracked_display = {"alex smith": [("Alex Smith", "structural")]}
    pipe.tracked_ids = {"A1": [("Alex Smith", "structural")]}
    pipe.tracked_id_for = {("Alex Smith", "structural"): "A1"}
    pipe.tracked_orcid_for = {}
    return pipe


def test_pipeline_gate_skips_facility_deposits_that_arrive_without_a_work_type(tmp_path):
    pipe = _tracked_pipe(tmp_path)
    smith = Author(name="Alex Smith", openalex_id="A1")
    users = [Author(name=f"Facility User {i}") for i in range(60)]
    # the live OpenAlex pull records no work_type and types the record 'preprint': only the DOI / venue / title tell
    deposit = Paper(id="doi:" + EMSL_DOI, source="openalex", kind="preprint", title=EMSL_TITLE, doi=EMSL_DOI, journal=EMSL_VENUE,
                    pub_date="2026-10-16", authors=[smith, *users], extra={})
    assert pipe.extract(deposit)[1] == []
    assert pipe.counts["tracked_hits.skipped_deposit"] == 1 and pipe.counts["tracked_hits.skipped_non_paper"] == 0
    # the same person on a paper: a hit; on a preprint deposited to a general repository: still a hit (not a deposit)
    paper = Paper(id="doi:10.1038/s41592-026-1", source="crossref", title="Cryo-EM of a transporter", doi="10.1038/s41592-026-1",
                  journal="Nature Methods", pub_date="2026-10-15", authors=[smith], extra={})
    assert pipe.extract(paper)[1] == [("Alex Smith", "structural", "openalex_id")]
    zenodo = Paper(id="doi:10.5281/zenodo.77", source="openalex", kind="preprint", title="A preprint on Zenodo", doi="10.5281/zenodo.77",
                   journal="Zenodo", pub_date="2026-10-15", authors=[smith], extra={"work_type": "preprint"})
    assert pipe.extract(zenodo)[1] == [("Alex Smith", "structural", "openalex_id")]
    assert pipe.counts["tracked_hits.skipped_deposit"] == 1
    pipe.store.close()


def test_store_refuses_tracked_hits_on_deposit_records(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.upsert_paper(_paper("doi:" + EMSL_DOI, "2026-10-16", kind="preprint", title=EMSL_TITLE, journal=EMSL_VENUE))
    st.upsert_paper(_paper("doi:10.1038/s41592-1", "2026-10-15", title="A cryo-EM paper"))
    assert st.add_tracked_author_hit("doi:" + EMSL_DOI, "Alex Smith", "structural", "openalex_id") is False
    assert st.add_tracked_author_hit("doi:10.1038/s41592-1", "Alex Smith", "structural", "openalex_id") is True
    assert st.add_tracked_author_hit("doi:10.1000/not-stored-yet", "Alex Smith", "structural", "name") is True  # nothing to judge by
    assert {r[0] for r in st.conn.execute("SELECT paper_id FROM tracked_author_hits")} == {"doi:10.1038/s41592-1", "doi:10.1000/not-stored-yet"}
    st.close()


def test_maintenance_relabels_deposits_and_purges_their_tracked_hits(tmp_path):
    st = Store(tmp_path / "m.sqlite")
    st.upsert_paper(_paper("doi:" + EMSL_DOI, "2026-10-16", kind="preprint", title=EMSL_TITLE, journal=EMSL_VENUE))
    st.upsert_paper(_paper("doi:10.5281/zenodo.9", "2026-10-12", journal="Zenodo (CERN European Organization for Nuclear Research)", kind="preprint", title="A dataset"))
    st.upsert_paper(_paper("doi:10.1038/s41592-1", "2026-10-15", title="A cryo-EM paper"))
    # rows written before the gate existed
    st.conn.executemany("INSERT INTO tracked_author_hits (paper_id, author_name, subgroup, matched_by) VALUES (?,?,?,?)", [
        ("doi:" + EMSL_DOI, "Alex Smith", "structural", "openalex_id"), ("doi:" + EMSL_DOI, "Pat Jones", "ai_bio", "openalex_id"),
        ("doi:10.5281/zenodo.9", "Alex Smith", "structural", "openalex_id"), ("doi:10.1038/s41592-1", "Alex Smith", "structural", "openalex_id"),
    ])
    st.commit()
    assert st.count_deposit_tracked_hits() == 3 and st.table_stats()["deposit_tracked_hits"] == 3
    assert "ATTENTION: 3 tracked-author hit(s) sit on deposit records" in maintenance.format_report(maintenance.run_maintenance(st, stats_by_table=True))
    rep = maintenance.run_maintenance(st, relabel_deposits=True)
    assert rep["actions"] == ["relabel_deposits"]
    assert rep["relabel_deposits"] == {"papers_relabelled": {"facility_deposit": 1, "dataset": 0, "software": 0, "supplement": 0}, "papers_relabelled_total": 1,
                                       "tracked_hits_found": 3, "tracked_hits_deleted": 3, "tracked_hits_remaining": 0, "tracked_author_hits": 1}
    types = {r[0]: r[1] for r in st.conn.execute("SELECT id, json_extract(extra_json, '$.work_type') FROM papers")}
    assert types["doi:" + EMSL_DOI] == "dataset" and types["doi:10.5281/zenodo.9"] is None and types["doi:10.1038/s41592-1"] is None
    assert [r[0] for r in st.conn.execute("SELECT paper_id FROM tracked_author_hits")] == ["doi:10.1038/s41592-1"]
    text = maintenance.format_report(rep)
    assert "deposit records relabelled with a work_type: 1 (facility_deposit 1); tracked-author hits on deposit records: 3 found, 3 deleted, 0 remaining (1 tracked-author hits left)" in text
    again = maintenance.run_maintenance(st, relabel_deposits=True)["relabel_deposits"]
    assert again["papers_relabelled_total"] == 0 and again["tracked_hits_found"] == 0 and again["tracked_hits_deleted"] == 0
    assert "ATTENTION" not in maintenance.format_report(maintenance.run_maintenance(st, stats_by_table=True))
    assert "--relabel-deposits" in maintenance.format_report({"actions": []})
    st.close()


# ---- defect 3: archive census concentration --------------------------------------------------------------------------
def _archive_store(tmp_path) -> Store:
    st = Store(tmp_path / "a.sqlite")
    recs = [ArchiveRecord("sra", f"SRX{i}", "SRP1", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", "2026-10-12", "c", "Big cohort", "Homo sapiens",
                          "ilmn-novaseq-x", "Illumina", "ILMN") for i in range(9)]
    recs.append(ArchiveRecord("sra", "SRX9", "SRP2", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", "2026-10-13", "c", "Small study", "Mus musculus",
                              "ilmn-novaseq-x", "Illumina", "ILMN"))
    for i in range(4):
        recs.append(ArchiveRecord("sra", f"SRX2{i}", f"SRP3{i}", "PacBio Revio", "PACBIO_SMRT", "RNA-Seq", "2026-10-13", "c", f"Study {i}", "Homo sapiens",
                                  "pacb-revio", "Pacific Biosciences", "PACB"))
    # no study accession: counts in the row's total and never as a study or as 'largest'
    recs.append(ArchiveRecord("sra", "SRX99", "", "PacBio Revio", "PACBIO_SMRT", "RNA-Seq", "2026-10-13", "c", "", "", "pacb-revio", "Pacific Biosciences", "PACB"))
    recs.append(ArchiveRecord("pride", "PXD1", "PXD1", "Orbitrap Astral", "MS", "Bottom-up", "2026-10-13", entity_id="thermo-orbitrap-astral",
                              vendor="Thermo Fisher Scientific", ticker="TMO"))
    st.upsert_archive_records(recs)
    st.commit()
    return st


def test_archive_counts_carry_the_largest_study_share(tmp_path):
    st = _archive_store(tmp_path)
    strat = {s["library_strategy"]: s for s in st.archive_strategy_counts(*WINDOW, archives=("sra",))}
    wgs, rna = strat["WGS"], strat["RNA-Seq"]
    assert (wgs["experiments"], wgs["studies"], wgs["largest_study"], wgs["largest_study_experiments"], wgs["largest_study_share"], wgs["largest_study_title"]) == (10, 2, "SRP1", 9, 0.9, "Big cohort")
    assert (rna["experiments"], rna["studies"], rna["largest_study_experiments"], rna["largest_study_share"]) == (5, 4, 1, 0.2) and rna["largest_study"].startswith("SRP3")
    models = {r["instrument_model"]: r for r in st.archive_instrument_counts(*WINDOW, archives=("sra",))}
    assert models["Illumina NovaSeq X Plus"]["largest_study"] == "SRP1" and models["Illumina NovaSeq X Plus"]["largest_study_share"] == 0.9
    assert models["PacBio Revio"]["largest_study_share"] == 0.2 and models["PacBio Revio"]["vendor"] == "Pacific Biosciences"
    pride = st.archive_instrument_counts(*WINDOW, archives=("pride",))
    assert pride[0]["largest_study_share"] == 1.0 and pride[0]["largest_study"] == "PXD1"
    assert st.archive_strategy_counts("2030-01-01", "2030-01-02") == []
    # rendering: the share in the table, the accession named at 50 % or more, the strategy line and the footnote
    rows = st.archive_instrument_counts(*WINDOW, archives=("sra",))
    table = _archive_table(rows, 10, show_archive=True, concentration=True)
    assert "| Instrument model | Vendor | Ticker | Experiments | Studies | Largest study | Archive |" in table
    assert "| Illumina NovaSeq X Plus | Illumina | ILMN | 10 | 2 | 90% SRP1 | sra |" in table
    assert "| PacBio Revio | Pacific Biosciences | PACB | 5 | 4 | 20% | sra |" in table
    assert "Largest study" not in _archive_table(rows, 10)  # the proteomics / single-cell tables keep their shape
    assert _strategy_item(wgs) == "WGS (10; 90% one study, SRP1)" and _strategy_item(rna) == "RNA-Seq (5)"
    note = _concentration_note([wgs, rna], "library_strategy", "experiments")
    assert note.startswith("_Concentration: WGS is 90% one study (SRP1, 'Big cohort': 9 of 10 experiments).") and "RNA-Seq" not in note
    assert _concentration_note([rna], "library_strategy", "experiments") == ""
    st.close()


def test_digest_census_names_the_study_behind_a_concentrated_row(tmp_path):
    st = _archive_store(tmp_path)
    out = render(st, *WINDOW, {})
    md = out["markdown"]
    census = md[md.index("## Instruments behind"): md.index("### Proteomics")]
    assert "| Illumina NovaSeq X Plus | Illumina | ILMN | 10 | 2 | 90% SRP1 | sra |" in census
    assert "_Concentration: Illumina NovaSeq X Plus is 90% one study (SRP1, 'Big cohort': 9 of 10 experiments)." in census
    assert "Library strategies: WGS (10; 90% one study, SRP1), RNA-Seq (5)" in census
    assert "_Concentration: WGS is 90% one study (SRP1, 'Big cohort': 9 of 10 experiments)." in census
    assert "'Largest study' is the share of a row's experiments that its single biggest study / project supplies" in census
    proteomics = md[md.index("### Proteomics"): md.index("### Single-cell")]
    assert "| Instrument model | Vendor | Ticker | Datasets | Studies |\n" in proteomics and "Largest study" not in proteomics
    assert out["summary"]["archive_strategies"][0]["largest_study_share"] == 0.9
    assert out["summary"]["archive"][0]["largest_study"] == "SRP1"
    st.close()


# ---- defect 4: first appearance by channel ---------------------------------------------------------------------------
def _mention(pid: str, entity: str, section: str, kind: str = "company") -> Mention:
    name = entity.replace("-", " ").title()
    return Mention(pid, kind, entity, name, "", "", "", section, 1, "...")


def _channel_store(tmp_path) -> Store:
    """Title / abstract, funding and affiliation mentions reach back to July; Methods text exists from 2026-10-09 and
    COI text from 2026-10-12 (inside or after the comparison period 2026-10-04..10 of the window 2026-10-11..17)."""
    st = Store(tmp_path / "c.sqlite")

    def add(pid, day, *mentions):
        st.upsert_paper(_paper(pid, day))
        st.replace_mentions(pid, list(mentions))

    add("doi:10.1000/h1", "2026-07-01", _mention("doi:10.1000/h1", "old-co", "abstract"), _mention("doi:10.1000/h1", "old-funder", "funding"))
    add("doi:10.1000/h2", "2026-10-09", _mention("doi:10.1000/h2", "supplier-early", "methods"))
    add("doi:10.1000/w1", "2026-10-13", _mention("doi:10.1000/w1", "new-subject", "abstract"), _mention("doi:10.1000/w1", "old-co", "methods"))
    add("doi:10.1000/w2", "2026-10-14", _mention("doi:10.1000/w2", "supplier-only", "methods"), _mention("doi:10.1000/w2", "new-subject", "methods"))
    add("doi:10.1000/w3", "2026-10-12", _mention("doi:10.1000/w3", "coi-only", "coi"))
    add("doi:10.1000/w4", "2026-10-15", _mention("doi:10.1000/w4", "funder-new", "funding"))
    st.commit()
    return st


def test_new_entities_label_the_channel_and_withhold_short_history_channels(tmp_path):
    st = _channel_store(tmp_path)
    rep = st.new_entities_report("2026-10-11", "company", "2026-10-17")
    assert rep["has_history"] is True and rep["comparison_period"] == {"start": "2026-10-04", "end": "2026-10-10"}
    assert [(r["entity_id"], r["channel"], r["first_seen"], r["papers"]) for r in rep["rows"]] == [
        ("new-subject", "title/abstract", "2026-10-13", 2), ("funder-new", "funding/COI", "2026-10-15", 1)]
    assert rep["withheld"] == {"funding/COI": 1, "methods": 1}
    ch = rep["channels"]
    assert ch["title/abstract"] == {"sections": ["abstract"], "history_from": "2026-07-01", "covers": True, "short_sections": {}}
    assert ch["methods"]["covers"] is False and ch["methods"]["history_from"] == "2026-10-09" and ch["methods"]["short_sections"] == {"methods": "2026-10-09"}
    assert ch["funding/COI"]["covers"] is False and ch["funding/COI"]["history_from"] == "2026-07-01" and ch["funding/COI"]["short_sections"] == {"coi": "2026-10-12"}
    assert "old-co" not in {r["entity_id"] for r in rep["rows"]}  # an old subject re-named in Methods is not new
    # the plain list is the rows; `until` defaults to a seven-day window
    assert [r["entity_id"] for r in st.new_entities("2026-10-11", "company")] == ["new-subject", "funder-new"]
    assert st.new_entities("2026-10-11", "company")[0]["entity_name"] == "New Subject"
    lines = _first_appearance_lines(rep, "company", with_counts=False)
    assert lines[0].startswith("**First appearance in the corpus this window** (by publication date; the channel it first appeared in is given): "
                               "New Subject (title/abstract), Funder New (funding/COI)")
    assert "1 company named only in coi text (parsed since 2026-10-12); 1 company named only in methods text (parsed since 2026-10-09)" in lines[1]
    assert "shorter than the comparison period (2026-10-04 to 2026-10-10)" in lines[1]
    counted = _first_appearance_lines(rep, "company", with_counts=True)[0]
    assert "New Subject (2; title/abstract)" in counted
    assert _first_appearance_lines({"rows": [], "withheld": {"methods": 3}, "channels": {}, "has_history": False}, "instrument", True) == []
    assert st.new_entities_report("2026-10-11", "instrument", "2026-10-17")["rows"] == []
    st.close()


def test_digest_prints_first_appearances_with_channels_and_the_withheld_note(tmp_path):
    st = _channel_store(tmp_path)
    md = render(st, "2026-10-11", "2026-10-17", {})["markdown"]
    companies = md[md.index("## Companies in the literature"): md.index("## Tracked researchers")]
    assert "New Subject (title/abstract), Funder New (funding/COI)" in companies
    assert "_First appearances withheld: 1 company named only in coi text (parsed since 2026-10-12); 1 company named only in methods text (parsed since 2026-10-09)." in companies
    assert "First appearance" not in md[md.index("## Platforms named"): md.index("## Instruments behind")]  # no instrument has history
    st.close()


def test_discovery_labels_first_appearances_by_channel_and_marks_short_channels(tmp_path):
    cand = D.Candidate("acmebio", "Acme Bio Inc", evidence=[
        D.Evidence(name="Acme Bio Inc", evidence_type="coi", snippet="employee of Acme Bio Inc", ref="p1", date="2026-10-14"),
        D.Evidence(name="Acme Bio Inc", evidence_type="email_domain", snippet="a@acme.bio", ref="p2", date="2026-10-12", domain="acme.bio"),
        D.Evidence(name="Acme Bio Inc", evidence_type="news:press", snippet="Acme Bio raises", ref="n1", date="2026-10-13"),
    ])
    assert cand.first_seen_by_channel == {"coi statements": "2026-10-14", "author e-mails": "2026-10-12", "news": "2026-10-13"}
    assert cand.first_seen_channel == "author e-mails" and D.evidence_channel("news:fda") == "news" and D.evidence_channel("odd") == "odd"
    st = Store(tmp_path / "d.sqlite")
    st.upsert_paper(_paper("doi:10.1000/f1", "2026-01-05", sections={"funding": "Supported by Old Funder Ltd."}))
    st.upsert_paper(_paper("doi:10.1000/c1", "2026-10-14", sections={"coi": "A.B. is an employee of Acme Spatial Therapeutics Inc."}))
    st.commit()
    hist = D.channel_history(st.conn)
    assert hist["funding statements"] == "2026-01-05" and hist["coi statements"] == "2026-10-14"
    assert hist["acknowledgements"] is None and hist["author e-mails"] is None and hist["affiliations"] is None and hist["news"] is None
    assert D.short_history_channels(st.conn, "2026-10-11", "2026-10-18") == {"coi statements": "2026-10-14"}
    # refresh stores the channel of the earliest evidence with the candidate
    D.refresh(st, None, "2026-01-01", "2026-10-18", today="2026-10-18")
    rows = {r["name_norm"]: r for r in D.list_candidates(st, min_score=0)}
    acme = next(r for k, r in rows.items() if k.startswith("acmespatial"))
    assert acme["first_seen_channel"] == "coi statements" and acme["first_seen_by_channel"] == {"coi statements": "2026-10-14"}
    old = next(r for k, r in rows.items() if k.startswith("oldfunder"))
    assert old["first_seen_channel"] == "funding statements"
    short = D.short_history_channels(st.conn, "2026-10-11", "2026-10-18")
    assert D.first_seen_label(acme, short) == "≤ 2026-10-14 (coi statements; parsed since then)"
    assert D.first_seen_label(old, short) == "2026-01-05 (funding statements)"
    assert D.first_seen_label({"first_seen": "2026-10-01"}, short) == "2026-10-01" and D.first_seen_label({}, short) == ""
    md = D.render_candidates_markdown(st, "2026-10-11", "2026-10-18", top=5)
    assert "| Candidate | Score | Signals | Papers / items | First seen | Last seen | Evidence |" in md
    assert "| ≤ 2026-10-14 (coi statements; parsed since then) |" in md
    assert "'≤ date' marks channels whose history is shorter than the comparison window (coi statements since 2026-10-14)" in md
    # a payload written before the channel existed renders the bare date and no footnote
    st.conn.execute("UPDATE candidate_companies SET evidence_json=?", (json.dumps({"evidence": [], "types": {"coi": 1}}),))
    st.commit()
    md2 = D.render_candidates_markdown(st, "2026-10-11", "2026-10-18", top=5)
    assert "| 2026-10-14 |" in md2 and "≤" not in md2
    st.close()
