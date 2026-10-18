"""Pipeline.run / rematch against canned adapters (no network): re-ingest safety, the data policy, the
wiring of the archive / signal / citation adapters, tracked-author caps and the WAL checkpoint."""
import json
import logging
from pathlib import Path

import pytest

from litradar import pipeline as pl
from litradar.config import Settings
from litradar.digest import render
from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.pipeline import Pipeline

JATS = (Path(__file__).parent / "fixtures" / "sample_jats.xml").read_bytes()

PID = "doi:10.1101/2026.10.01.777"
METHODS = "Libraries were sequenced on a Revio system (PacBio) and a NovaSeq X Plus."
WINDOW = ("2026-10-10", "2026-10-17")


class FakeHttp:
    stats = {"requests": 0}

    def get(self, *a, **k):
        return None

    def get_json(self, *a, **k):
        return None

    def get_text(self, *a, **k):
        return None

    def post_json(self, *a, **k):
        return None


def _settings(tmp_path, **radar):
    s = Settings.from_env()
    s.db_path = tmp_path / "p.sqlite"
    s.radar = {"sources": {}, "watchlist_terms": ["spatial transcriptomics", "ctDNA"], "digest": {"top_n": 10}}
    s.radar.update(radar)
    return s


def _preprint(license="cc_by", **kw):
    base = dict(
        id=PID, source="biorxiv", kind="preprint", title="Spatial transcriptomics of tumours with Xenium", doi="10.1101/2026.10.01.777",
        abstract="We profile tumours with Xenium.", journal="biorxiv", pub_date="2026-10-12",
        url="https://www.biorxiv.org/content/10.1101/2026.10.01.777v1", license=license,
        fulltext_url="https://www.biorxiv.org/content/biorxiv/early/2026/10/12/2026.10.01.777.source.xml",
        authors=[Author(name="Jane Doe")], affiliations=["Example University"],
    )
    base.update(kw)
    return Paper(**base)


def _fake_attach(http, p):
    p.sections["methods"] = METHODS
    p.sections["body"] = "Introduction. " + METHODS + " Results."
    p.fulltext_available = True
    return True


def _no_attach(http, p):
    raise AssertionError("full text must not be fetched again for an already parsed paper")


def _run(pipe, monkeypatch, papers, attach=_fake_attach, only=("biorxiv",)):
    monkeypatch.setattr(pl.biorxiv_src, "fetch", lambda http, since, until, server="biorxiv", categories=None: iter(list(papers)))
    monkeypatch.setattr(pl.biorxiv_src, "attach_fulltext", attach)
    return pipe.run(*WINDOW, only=list(only))


def _sections(store, pid=PID):
    return {(m["entity_id"], m["section"]) for m in store.mentions_for(pid)}


# ---- re-ingest safety -------------------------------------------------------------------------
def test_metadata_only_refetch_keeps_methods_mentions_kind_and_date(tmp_path, monkeypatch):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    _run(pipe, monkeypatch, [_preprint()])
    assert {("pacb-revio", "methods"), ("ilmn-novaseq-x", "methods"), ("txg-xenium", "abstract")} <= _sections(pipe.store)
    assert pipe.store.body_for(PID).startswith("Introduction")
    pipe.store.close()

    # week 2: the same record comes back as metadata only, now flagged as published; no full text is fetched
    pipe2 = Pipeline(_settings(tmp_path), http=FakeHttp())
    rep = _run(pipe2, monkeypatch, [_preprint(kind="article", journal="Nature Methods", pub_date="2026-10-14", fulltext_url="")], attach=_no_attach)
    assert ("pacb-revio", "methods") in _sections(pipe2.store)  # the wipe this test guards against
    row = pipe2.store.get_paper(PID)
    assert row["kind"] == "article" and row["journal"] == "Nature Methods"
    assert row["pub_date"] == "2026-10-12"  # earliest non-empty date is kept
    assert json.loads(row["sections_json"])["methods"] == METHODS
    assert rep["counts"]["papers.reingested"] == 1 and rep["counts"]["papers.new"] == 0
    pipe2.store.close()

    # week 3: a preprint-flavoured record of the same DOI must not flip the kind back
    pipe3 = Pipeline(_settings(tmp_path), http=FakeHttp())
    _run(pipe3, monkeypatch, [_preprint(kind="preprint", fulltext_url="")], attach=_no_attach)
    assert pipe3.store.get_paper(PID)["kind"] == "article"
    assert ("pacb-revio", "methods") in _sections(pipe3.store)
    pipe3.store.close()


# ---- data policy --------------------------------------------------------------------------------
def test_restricted_licence_keeps_derived_facts_but_no_text(tmp_path, monkeypatch):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    rep = _run(pipe, monkeypatch, [_preprint(license="cc_no")])
    row = pipe.store.get_paper(PID)
    secs = json.loads(row["sections_json"])
    assert "methods" not in secs and "body" not in secs and "coi" not in secs
    assert pipe.store.body_for(PID) == ""
    assert json.loads(row["extra_json"])["fulltext_policy"] == "facts_only"
    assert row["fulltext_available"] == 1  # the text was parsed, which is what the count reports
    assert {("pacb-revio", "methods"), ("ilmn-novaseq-x", "methods")} <= _sections(pipe.store)
    assert rep["counts"]["fulltext.policy_facts_only"] == 1 and rep["counts"]["fulltext.biorxiv"] == 1
    # nothing of the restricted TEXT is searchable: the Methods words are not indexed in any text column ...
    assert pipe.store.search("Libraries sequenced") == []
    assert pipe.store.search("Revio", sections=["methods", "body"]) == []
    # ... but the derived fact (the entity named in the Methods) is, through the `mentions` column, so search
    # agrees with `litradar entity pacb-revio` on which papers used the instrument
    assert [r["id"] for r in pipe.store.search("Revio")] == [PID]
    assert [r["id"] for r in pipe.store.search("Revio", sections=["mentions"])] == [PID]
    pipe.store.close()

    # a metadata-only re-ingest keeps the derived facts although no text is stored
    pipe2 = Pipeline(_settings(tmp_path), http=FakeHttp())
    _run(pipe2, monkeypatch, [_preprint(license="cc_no", fulltext_url="")], attach=_no_attach)
    assert ("pacb-revio", "methods") in _sections(pipe2.store)
    assert pipe2.store.body_for(PID) == ""
    pipe2.store.close()

    # and so does a rematch
    pipe3 = Pipeline(_settings(tmp_path), http=object())
    rep3 = pipe3.rematch()
    assert rep3["counts"]["rematched"] == 1 and rep3["counts"]["fulltext.purged"] == 0
    assert ("pacb-revio", "methods") in _sections(pipe3.store)
    pipe3.store.close()


def test_open_licence_stores_text_in_full(tmp_path, monkeypatch):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    rep = _run(pipe, monkeypatch, [_preprint(license="cc_by_nc_nd")])
    row = pipe.store.get_paper(PID)
    assert json.loads(row["extra_json"])["fulltext_policy"] == "full"
    assert json.loads(row["sections_json"])["methods"] == METHODS and pipe.store.body_for(PID)
    assert rep["counts"]["fulltext.policy_full"] == 1
    assert pipe.store.search("Revio", sections=["methods"])[0]["id"] == PID
    pipe.store.close()


def test_policy_levels_from_source_metadata():
    assert Pipeline.fulltext_policy(_preprint(license="cc_by")) == "full"
    assert Pipeline.fulltext_policy(_preprint(license="cc0")) == "full"
    assert Pipeline.fulltext_policy(_preprint(license="cc_no")) == "facts_only"
    assert Pipeline.fulltext_policy(_preprint(license="")) == "facts_only"  # unknown licence: facts only
    assert Pipeline.fulltext_policy(_preprint(license="cc_by", fulltext_url="")) == "none"  # nothing to fetch
    epmc = _preprint(source="europepmc", license="", fulltext_url="", extra={"pmcid": "PMC1", "is_open_access": "Y"})
    assert Pipeline.fulltext_policy(epmc) == "full"
    epmc.extra["is_open_access"] = "N"
    assert Pipeline.fulltext_policy(epmc) == "facts_only"  # author manuscript in PMC, not in the OA subset


def test_rematch_cleans_legacy_restricted_fulltext_but_keeps_mentions(tmp_path):
    pipe = Pipeline(_settings(tmp_path), http=object())
    p = _preprint(license="cc_no")
    p.sections = {"methods": METHODS, "body": "body " + METHODS}
    p.fulltext_available = True
    pipe.store.upsert_paper(p)  # no policy recorded: a row from before the policy existed
    pipe.store.commit()
    assert pipe.store.body_for(PID) != ""
    rep = pipe.rematch()
    assert rep["counts"]["fulltext.purged"] == 1
    assert pipe.store.body_for(PID) == "" and "methods" not in json.loads(pipe.store.get_paper(PID)["sections_json"])
    assert json.loads(pipe.store.get_paper(PID)["extra_json"])["fulltext_policy"] == "facts_only"
    assert {("pacb-revio", "methods"), ("ilmn-novaseq-x", "methods")} <= _sections(pipe.store)
    pipe.store.close()
    rep2 = Pipeline(_settings(tmp_path), http=object()).rematch()
    assert rep2["counts"]["fulltext.purged"] == 0 and rep2["counts"]["rematched"] == 1


def test_rank_accepts_any_iterable(tmp_path):
    pipe = Pipeline(_settings(tmp_path), http=object())
    ps = [_preprint(id=f"doi:10.1101/r{i}", doi=f"10.1101/r{i}") for i in range(3)]
    for p in ps:
        pipe.store.upsert_paper(p)
    assert pipe.rank(p for p in ps) == 3
    assert pipe.rank({p.id: p for p in ps}) == 3
    assert all(pipe.store.get_score(p.id)["final"] > 0 for p in ps)
    pipe.store.close()


# ---- adapter wiring -----------------------------------------------------------------------------
def test_run_reaches_archives_signals_and_citation_enrichment(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    pipe = Pipeline(settings, http=FakeHttp())
    calls = []

    def geo_fetch(http, start, end, api_key="", max_records=20000, gpl_cache=None, extra_term=""):
        calls.append(("geo", start, end, extra_term))
        yield ArchiveRecord("geo", "GSE1/GPL1", "GSE1", "Illumina NovaSeq 6000", "high-throughput sequencing", "Expression profiling", "2026-10-12", "", "Series", "Homo sapiens")

    def pride_fetch(http, start, end, max_projects=0):
        calls.append(("pride", start, end))
        yield ArchiveRecord("pride", "PXD1", "PXD1", "Orbitrap Astral", "MS", "Bottom-up proteomics", "2026-10-13", "", "Proteome", "Homo sapiens")

    def cxg_fetch(http, start, end, include_revisions=False, max_records=0, max_collection_details=50):
        calls.append(("cellxgene", start, end))
        yield ArchiveRecord("cellxgene", "ds1#EFO:0009922", "col1", "10x 3' v3", "single-cell", "EFO:0009922", "2026-10-14", "", "Atlas | ds | 1000 cells", "Homo sapiens", "txg-chromium", "10x Genomics", "TXG")
        yield ArchiveRecord("cellxgene", "ds2#EFO:0", "col1", "Drop-seq", "single-cell", "EFO:0", "2026-10-14", "", "x", "Homo sapiens")

    def s10(http, start, end, terms=None, max_records=5000, decorate_titles=False, errors=None):
        calls.append(("nih-s10", start, end))
        yield NewsItem("nih-reporter:1", "nih-reporter", "Acquisition of a NovaSeq X Plus sequencer — Example University (S10, $1,000,000)", "https://reporter.nih.gov/project-details/1", "2026-10-11", "S10 | Example University")

    def kw(http, start, end, terms, **k):
        calls.append(("nih-kw", len(terms)))
        yield NewsItem("nih-reporter:2", "nih-reporter", "Spatial profiling core with Xenium", "https://reporter.nih.gov/project-details/2", "2026-10-12", "R01 | instrument from 10x Genomics")

    def studies(http, start, end=None, sponsor_terms=None, intervention_terms=None, terms=None, **k):
        calls.append(("ctgov", list(sponsor_terms or [])[:1], list(intervention_terms or [])[:1]))
        yield NewsItem("ctgov:NCT1", "clinicaltrials", "ctDNA-guided therapy study", "https://clinicaltrials.gov/study/NCT1", "2026-10-13", "Lead sponsor: Guardant Health. Interventions: Guardant360.")

    def fda(http, start, end, kinds=("510k", "pma"), api_key=None, **k):
        calls.append(("fda", start, end, tuple(kinds)))
        yield NewsItem("openfda:K1", "fda-510k", "Sequencing assay — Illumina, Inc.", "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K1", "2026-10-01", "510(k) K1; applicant Illumina")
        yield NewsItem("openfda:K2", "fda-510k", "Bandage — Acme Devices", "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K2", "2026-10-02", "510(k) K2; applicant Acme Devices")
        yield NewsItem("openfda:P1", "fda-pma", "Companion diagnostic — Foundation Medicine", "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpma/pma.cfm?id=P1", "2026-10-03", "PMA P1; applicant Foundation Medicine")

    def s2(http, store, since=None, api_key="", limit=None, **k):
        calls.append(("s2", since, limit))
        return {"candidates": 1, "requested": 1, "matched": 1, "updated": 1, "unmatched": 0, "batches": 1, "failed_batches": 0, "citations": {PID: 12}}

    monkeypatch.setattr(pl.geo_src, "fetch", geo_fetch)
    monkeypatch.setattr(pl.geo_src, "sweep_error", lambda: "")
    monkeypatch.setattr(pl.pride_src, "fetch", pride_fetch)
    monkeypatch.setattr(pl.cellxgene_src, "fetch", cxg_fetch)
    monkeypatch.setattr(pl.nih_src, "s10_awards", s10)
    monkeypatch.setattr(pl.nih_src, "keyword_awards", kw)
    monkeypatch.setattr(pl.ctgov_src, "new_studies", studies)
    monkeypatch.setattr(pl.openfda_src, "fetch", fda)
    monkeypatch.setattr(pl.s2_src, "enrich_citations", s2)

    rep = _run(pipe, monkeypatch, [_preprint()], only=("biorxiv", "geo", "pride", "cellxgene", "nih_reporter", "clinicaltrials", "openfda", "semantic_scholar"))
    c = rep["counts"]
    assert rep["errors"] == []
    assert c["archive.geo_new"] == 1 and c["archive.pride_new"] == 1 and c["archive.cellxgene_new"] == 2
    assert c["signals.nih-reporter"] == 2 and c["signals.clinicaltrials"] == 1
    assert c["signals.fda-510k"] == 1 and c["signals.fda-pma"] == 1 and c["signals.fda_unwatched"] == 1
    assert c["semantic_scholar.matched"] == 1 and c["semantic_scholar.updated"] == 1
    names = [x[0] for x in calls]
    assert names.index("geo") < names.index("nih-s10") < names.index("s2")  # archives, then signals, then citations
    assert calls[names.index("geo")][3] == '"high-throughput sequencing"[PTYP]'
    fda_call = calls[names.index("fda")]
    assert fda_call[1] < "2026-10-10" and fda_call[2] == "2026-10-17" and fda_call[3] == ("510k", "pma")  # monthly refresh look-back
    kw_call = calls[names.index("nih-kw")]
    assert kw_call[1] > 50  # platform names + company names
    ct_call = calls[names.index("ctgov")]
    assert ct_call[1] and ct_call[2] == ["spatial transcriptomics"]
    s2_call = calls[names.index("s2")]
    assert s2_call[1] == "2025-10-10" and s2_call[2] == 5000

    rows = {r["instrument_model"]: r for r in pipe.store.archive_instrument_counts(*WINDOW)}
    assert rows["Illumina NovaSeq 6000"]["vendor_mapped"] and "Illumina" in rows["Illumina NovaSeq 6000"]["vendor"]
    assert rows["Orbitrap Astral"]["vendor_mapped"] and "Thermo" in rows["Orbitrap Astral"]["vendor"]
    assert rows["10x 3' v3"]["vendor"] == "10x Genomics"  # adapter-supplied id survives _store_archive and remap
    assert rows["Drop-seq"]["vendor"] == "Single-cell assay (unmapped model)"
    pipe.remap_archive()
    rows = {r["instrument_model"]: r for r in pipe.store.archive_instrument_counts(*WINDOW, archives=("cellxgene",))}
    assert rows["10x 3' v3"]["vendor"] == "10x Genomics" and rows["Drop-seq"]["vendor_mapped"] is False
    assert "already cited 12x" in pipe.store.get_score(PID)["reasons"]  # citations raised before ranking

    out = render.render(pipe.store, *WINDOW, settings.radar, run_report=rep, company_types=pipe.company_types)
    md = out["markdown"]
    i_sig, i_news = md.index("## Grants, trials and clearances"), md.index("## Trade press")
    assert i_sig < i_news
    signals_md = md[i_sig:i_news]
    assert "**NIH RePORTER awards** (2)" in signals_md and "**FDA 510(k) clearances** (1)" in signals_md and "**FDA PMA decisions** (1)" in signals_md
    assert "ctDNA-guided therapy study" in signals_md and "Acme Devices" not in md
    assert "ctDNA-guided" not in md[i_news:]
    assert "### Proteomics instruments behind new datasets (PRIDE)" in md and "Orbitrap Astral" in md
    assert "### Single-cell and spatial assays behind new datasets (CELLxGENE)" in md and "10x 3' v3" in md
    assert "New archive records: CELLXGENE 2, GEO 1, PRIDE 1" in md
    assert "Signals stored:" in md and "Citation enrichment: 1 DOIs looked up" in md
    assert out["summary"]["archive_proteomics"][0]["instrument_model"] == "Orbitrap Astral"
    assert [s["feed"] for s in out["summary"]["signals"]].count("nih-reporter") == 2
    pipe.store.close()


def test_sources_flag_gates_each_stage(tmp_path, monkeypatch):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    hit = []
    monkeypatch.setattr(pl.geo_src, "fetch", lambda *a, **k: hit.append("geo") or iter(()))
    monkeypatch.setattr(pl.geo_src, "sweep_error", lambda: "")
    monkeypatch.setattr(pl.openfda_src, "fetch", lambda *a, **k: hit.append("fda") or iter(()))
    monkeypatch.setattr(pl.s2_src, "enrich_citations", lambda *a, **k: hit.append("s2") or {"citations": {}})
    _run(pipe, monkeypatch, [_preprint()], only=("biorxiv",))
    assert hit == []
    _run(pipe, monkeypatch, [_preprint()], only=("geo", "openfda"))
    assert hit == ["geo", "fda"]
    pipe.store.close()


def test_disabled_source_in_config_is_skipped_and_errors_are_isolated(tmp_path, monkeypatch):
    settings = _settings(tmp_path, sources={"pride": {"enabled": False}, "geo": {"enabled": True}, "cellxgene": {"enabled": True}, "semantic_scholar": {"enabled": False}})
    pipe = Pipeline(settings, http=FakeHttp())
    monkeypatch.setattr(pl.pride_src, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pride is disabled")))
    monkeypatch.setattr(pl.geo_src, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("geo exploded")))
    monkeypatch.setattr(pl.geo_src, "sweep_error", lambda: "")
    monkeypatch.setattr(pl.cellxgene_src, "fetch", lambda *a, **k: iter([ArchiveRecord("cellxgene", "d#e", "c", "Visium", "spatial", "e", "2026-10-12")]))
    monkeypatch.setattr(pl.s2_src, "enrich_citations", lambda *a, **k: (_ for _ in ()).throw(AssertionError("s2 is disabled")))
    for name in ("sra", "ena", "nih_reporter", "clinicaltrials", "openfda", "europepmc", "openalex", "crossref", "arxiv", "rss"):
        settings.radar["sources"][name] = {"enabled": False}
    monkeypatch.setattr(pl.biorxiv_src, "fetch", lambda *a, **k: iter([_preprint(fulltext_url="")]))
    rep = pipe.run(*WINDOW, only=None, fulltext=False)
    assert any(e.startswith("geo: geo exploded") for e in rep["errors"])
    assert rep["counts"]["archive.cellxgene_new"] == 1 and "archive.pride_new" not in rep["counts"]
    pipe.store.close()


# ---- tracked-author hits ------------------------------------------------------------------------
def test_name_only_tracked_hits_are_marked_and_capped_per_run(tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path)
    pipe = Pipeline(settings, http=FakeHttp())
    pipe.tracked = {pl._norm_name("Alex Smith"): [("Alex Smith", "genomics")]}
    pipe.tracked_ids = {"A1": [("Jane Doe", "genomics")]}
    papers = [_preprint(id=f"doi:10.1101/t{i}", doi=f"10.1101/t{i}", title=f"Paper {i} on an unrelated topic", fulltext_url="",
                        authors=[Author(name="Alex Smith"), Author(name="Jane Doe", openalex_id="A1")]) for i in range(8)]
    with caplog.at_level(logging.WARNING, logger="litradar.pipeline"):
        rep = _run(pipe, monkeypatch, papers, attach=_no_attach)
    hits = pipe.store.tracked_hits_in_window(*WINDOW)
    by = {}
    for h in hits:
        by.setdefault(h["matched_by"], 0)
        by[h["matched_by"]] += 1
    assert by == {"name": 5, "openalex_id": 8}  # id matches are never capped
    assert rep["counts"]["tracked_hits"] == 13 and rep["counts"]["tracked_hits.name_capped"] == 3
    assert any("'Alex Smith'" in r.message and "8 name-only matches" in r.message for r in caplog.records)
    md = render.render(pipe.store, *WINDOW, settings.radar, run_report=rep)["markdown"]
    assert md.count("(name match; add the OpenAlex id to make it exact)") == 5
    assert "name matches dropped by the per-name cap: 3" in md
    pipe.store.close()
    # rematch re-applies the cap from a clean counter
    pipe2 = Pipeline(settings, http=object())
    pipe2.tracked, pipe2.tracked_ids = pipe.tracked, pipe.tracked_ids
    rep2 = pipe2.rematch()
    assert rep2["counts"]["tracked_hits.name_capped"] == 3 and pipe2.store.counts()["tracked_author_hits"] == 13
    pipe2.store.close()


def test_name_cap_is_configurable(tmp_path, monkeypatch):
    settings = _settings(tmp_path, tracked_authors={"name_match_cap": 2})
    pipe = Pipeline(settings, http=FakeHttp())
    pipe.tracked = {pl._norm_name("Alex Smith"): [("Alex Smith", "genomics")]}
    pipe.tracked_ids = {}
    papers = [_preprint(id=f"doi:10.1101/u{i}", doi=f"10.1101/u{i}", fulltext_url="", authors=[Author(name="Alex Q. Smith")]) for i in range(4)]
    rep = _run(pipe, monkeypatch, papers, attach=_no_attach)
    assert rep["counts"]["tracked_hits"] == 2 and rep["counts"]["tracked_hits.name_capped"] == 2
    pipe.store.close()


# ---- run end ------------------------------------------------------------------------------------
def test_run_checkpoints_the_wal_even_when_a_stage_fails(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    pipe = Pipeline(settings, http=FakeHttp())
    monkeypatch.setattr(pl.biorxiv_src, "fetch", lambda *a, **k: iter([_preprint()]))
    monkeypatch.setattr(pl.biorxiv_src, "attach_fulltext", _fake_attach)
    monkeypatch.setattr(pipe, "rank", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        pipe.run(*WINDOW, only=["biorxiv"])
    wal = settings.db_path.with_name(settings.db_path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    assert pipe.store.has_paper(PID)  # the store stays open for the caller to render from
    pipe.store.close()
    pipe.store.close()


def test_successful_run_checkpoints_and_records_the_run(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    pipe = Pipeline(settings, http=FakeHttp())
    rep = _run(pipe, monkeypatch, [_preprint()])
    wal = settings.db_path.with_name(settings.db_path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    assert pipe.store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert rep["counts"]["papers.new"] == 1
    assert rep["started"] <= rep["finished"] and rep["since"] == WINDOW[0]
    pipe.store.close()


# ---- tracked-author name matching -----------------------------------------------------------------
def test_norm_name_requires_full_given_name_and_handles_last_first():
    assert pl._norm_name("Daniel G. Anderson") == "daniel anderson" == pl._norm_name("Anderson, Daniel G.") == pl._norm_name("Daniel Anderson")
    assert pl._norm_name("Feng Zhang") == "feng zhang" == pl._norm_name("Zhang, Feng") == pl._norm_name("Féng  Zhang")
    for initials_only in ("Zhang F", "Zhang, F.", "F. Zhang", "Anderson, D. J.", "D. J. Anderson", "J Doe", "Zhang F."):
        assert pl._norm_name(initials_only) == "", initials_only
    assert pl._norm_name("Dana Pe'er") == pl._norm_name("Dana Pe’er") == "dana peer"
    assert pl._norm_name("Jean-Pierre O'Brien") == "jean obrien"
    assert pl._norm_name("") == "" and pl._norm_name("Consortium") == "consortium"
    assert pl._display_key("  Fei   Chen ") == "fei chen"


def test_name_matches_reject_initials_and_other_openalex_ids(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    pipe = Pipeline(settings, http=FakeHttp())
    pipe.tracked = {"feng zhang": [("Feng Zhang", "gene_editing")], "daniel anderson": [("Daniel G. Anderson", "delivery")]}
    pipe.tracked_display = {"feng zhang": [("Feng Zhang", "gene_editing")], "daniel g. anderson": [("Daniel G. Anderson", "delivery")],
                            "j. craig venter": [("J. Craig Venter", "synthesis")]}
    pipe.tracked_ids = {"A1": [("Feng Zhang", "gene_editing")]}
    pipe.tracked_id_for = {("Feng Zhang", "gene_editing"): "A1"}

    def paper(i, *authors):
        return _preprint(id=f"doi:10.1101/n{i}", doi=f"10.1101/n{i}", title=f"Paper {i}", fulltext_url="", authors=list(authors))

    papers = [
        paper(1, Author(name="Zhang F"), Author(name="Anderson, D. J.")),  # surname + initial: never a match
        paper(2, Author(name="Feng Zhang")),  # full name, record without author ids: provisional
        paper(3, Author(name="Feng Zhang", openalex_id="A999")),  # same name, a different id: another person
        paper(4, Author(name="Feng Zhang", openalex_id="A1")),  # the configured id
        paper(5, Author(name="Daniel Anderson")),  # full given name + surname, no id configured
        paper(6, Author(name="Zhang, Feng")),  # Last, First form
        paper(7, Author(name="J. Craig Venter")),  # exact display name when the normalised key is an initial
    ]
    rep = _run(pipe, monkeypatch, papers, attach=_no_attach)
    hits = {(h["id"], h["author_name"], h["matched_by"]) for h in pipe.store.tracked_hits_in_window(*WINDOW)}
    assert hits == {
        ("doi:10.1101/n2", "Feng Zhang", "name_no_ids"),
        ("doi:10.1101/n4", "Feng Zhang", "openalex_id"),
        ("doi:10.1101/n5", "Daniel G. Anderson", "name"),
        ("doi:10.1101/n6", "Feng Zhang", "name_no_ids"),
        ("doi:10.1101/n7", "J. Craig Venter", "name"),
    }
    assert rep["counts"]["tracked_hits.name_rejected_other_id"] == 1
    md = render.render(pipe.store, *WINDOW, settings.radar, run_report=rep)["markdown"]
    assert md.count("the record carried no OpenAlex author ids, so the configured id could not be checked") == 2
    assert md.count("add the OpenAlex id to make it exact") == 2
    assert "Paper 1" not in md[md.index("## Tracked researchers"): md.index("## Grants")]
    pipe.store.close()


def test_tracked_lists_are_built_from_subgroup_config(tmp_path):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    pipe.subgroups = [{"id": "g", "tracked_authors": [{"name": "Fei Chen", "openalex_id": ""}, {"name": "Sarah A. Teichmann", "openalex_id": "A5"}, {"name": "J. Craig Venter", "openalex_id": ""}]}]
    pipe2 = Pipeline.__new__(Pipeline)
    pipe2.__dict__.update(pipe.__dict__)
    pipe2.tracked, pipe2.tracked_display, pipe2.tracked_ids, pipe2.tracked_id_for = {}, {}, {}, {}
    for g in pipe2.subgroups:
        for a in g["tracked_authors"]:
            key = pl._norm_name(a["name"])
            if key:
                pipe2.tracked.setdefault(key, []).append((a["name"], g["id"]))
            pipe2.tracked_display.setdefault(pl._display_key(a["name"]), []).append((a["name"], g["id"]))
            if a["openalex_id"]:
                pipe2.tracked_ids.setdefault(a["openalex_id"], []).append((a["name"], g["id"]))
                pipe2.tracked_id_for[(a["name"], g["id"])] = a["openalex_id"]
    assert set(pipe2.tracked) == {"fei chen", "sarah teichmann"}  # the initial-only key is not a name key ...
    assert "j. craig venter" in pipe2.tracked_display  # ... but the display name still matches exactly
    assert pipe2.tracked_id_for == {("Sarah A. Teichmann", "g"): "A5"}
    pipe.store.close()


# ---- source failures reach the run's error list --------------------------------------------------
def test_openalex_query_failures_are_reported(tmp_path, monkeypatch, caplog):
    # country_slices off: this test pins the error list of the original query groups (the CN slice has its own test below)
    settings = _settings(tmp_path, sources={"openalex": {"enabled": True, "topics": True, "company_authors": True, "tracked_authors": False, "journals": False, "country_slices": []}})
    pipe = Pipeline(settings, http=FakeHttp())
    pipe.subgroups = [{"id": "g1", "openalex_topics": ["T1"], "keywords": ["x"]}, {"id": "g2", "openalex_topics": ["T2"], "keywords": []}]
    err = "HTTP 400: Invalid query parameters error: grants is not a valid select field"

    def works(http, filters, mailto="", api_key="", max_records=10000, select=None, extra_params=None, state=None):
        if state is not None:
            state["error"] = err
        return iter(())

    monkeypatch.setattr(pl.openalex_src, "works", works)
    with caplog.at_level(logging.WARNING, logger="litradar.pipeline"):
        papers = pipe.fetch_papers(*WINDOW, only=["openalex"])
    assert papers == {}
    assert pipe.errors == [f"openalex: company_authors: 1 of 1 query failed ({err})", f"openalex: topics: 2 of 2 queries failed ({err})"]
    assert pipe.counts["openalex.topics_failed_queries"] == 2
    assert sum("openalex topics query failed" in r.message for r in caplog.records) == 2
    from litradar.incremental import error_sources
    assert error_sources(pipe.errors) == ["openalex"]

    # a transport failure that leaves no state message is detected from the client's failure counter
    class CountingHttp(FakeHttp):
        def __init__(self):
            self.stats = {"requests": 0, "failures": 0}

    pipe2 = Pipeline(settings, http=CountingHttp())
    pipe2.subgroups = pipe.subgroups

    def works2(http, filters, *a, **k):
        http.stats["failures"] += 1
        return iter(())

    monkeypatch.setattr(pl.openalex_src, "works", works2)
    pipe2.fetch_papers(*WINDOW, only=["openalex"])
    assert any("1 request(s) failed, no records" in e for e in pipe2.errors)
    # a query that succeeds reports nothing
    pipe3 = Pipeline(settings, http=FakeHttp())
    pipe3.subgroups = pipe.subgroups
    monkeypatch.setattr(pl.openalex_src, "works", lambda http, filters, *a, state=None, **k: (state.__setitem__("complete", True), iter(()))[1])
    pipe3.fetch_papers(*WINDOW, only=["openalex"])
    assert pipe3.errors == []


def test_blocked_and_dead_hosts_land_in_run_errors_and_the_digest(tmp_path, monkeypatch):
    class HostHttp(FakeHttp):
        def __init__(self):
            self.stats = {"requests": 9, "failures": 9, "blocked_skips": 8}

        def host_report(self):
            return {
                "blocked_hosts": ["api.gdeltproject.org", "export.arxiv.org"],
                "failed_only_hosts": ["api.semanticscholar.org", "www.example.org"],
                "failures_by_host": {"export.arxiv.org": 2, "api.semanticscholar.org": 4, "www.example.org": 1, "api.gdeltproject.org": 2},
                "successes_by_host": {}, "blocked_skips_by_host": {"export.arxiv.org": 7, "api.gdeltproject.org": 1},
                "circuit_threshold": {"export.arxiv.org": 2, "api.gdeltproject.org": 2},
            }

    pipe = Pipeline(_settings(tmp_path), http=HostHttp())
    pipe.errors.append("gdelt: host rate-limited (HTTP 429) during the run; 1 company quer(y/ies) skipped")  # the source's own report
    rep = _run(pipe, monkeypatch, [_preprint(fulltext_url="")], attach=_no_attach)
    errs = rep["errors"]
    assert sum(e.startswith("gdelt:") for e in errs) == 1  # not listed twice
    assert "arxiv: host export.arxiv.org rate-limited (HTTP 429 2 times in a row); skipped for the rest of the run, 7 request(s) not sent" in errs
    assert "semantic_scholar: all 4 request(s) to api.semanticscholar.org failed; no successful response this run" in errs
    assert "www.example.org: all 1 request(s) to www.example.org failed; no successful response this run" in errs  # unknown host: named as itself
    assert rep["http"]["blocked_hosts"] == ["api.gdeltproject.org", "export.arxiv.org"] and rep["http"]["failures_by_host"]["export.arxiv.org"] == 2
    from litradar.incremental import error_sources
    assert error_sources(errs) == ["gdelt", "arxiv", "semantic_scholar", "www.example.org"]
    md = render.render(pipe.store, *WINDOW, pipe.settings.radar, run_report=rep)["markdown"]
    assert "Hosts skipped after repeated HTTP 429 in that run: api.gdeltproject.org, export.arxiv.org" in md
    assert "Source errors in that run (4): gdelt:" in md and "arxiv: host export.arxiv.org rate-limited" in md
    pipe.store.close()


def test_semantic_scholar_total_failure_is_an_error(tmp_path, monkeypatch):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    summary = {"candidates": 2848, "requested": 0, "matched": 0, "updated": 0, "unmatched": 0, "batches": 2, "failed_batches": 2, "citations": {}}
    monkeypatch.setattr(pl.s2_src, "enrich_citations", lambda *a, **k: dict(summary))
    rep = _run(pipe, monkeypatch, [_preprint(fulltext_url="")], attach=_no_attach, only=("biorxiv", "semantic_scholar"))
    assert rep["errors"] == ["semantic_scholar: 2 batch(es) failed and no DOI was matched (0 of 2848 candidate DOIs requested)"]
    # a partial failure with matches is not an error (it is counted)
    monkeypatch.setattr(pl.s2_src, "enrich_citations", lambda *a, **k: dict(summary, failed_batches=1, matched=5, requested=500))
    pipe2 = Pipeline(_settings(tmp_path), http=FakeHttp())
    rep2 = _run(pipe2, monkeypatch, [_preprint(fulltext_url="")], attach=_no_attach, only=("biorxiv", "semantic_scholar"))
    assert rep2["errors"] == [] and rep2["counts"]["semantic_scholar.failed_batches"] == 1
    pipe2.store.close()


def test_sec_skip_message_is_prefixed_with_its_source_id(tmp_path, monkeypatch):
    settings = _settings(tmp_path, news={"gdelt": False})
    settings.contact_email = ""
    pipe = Pipeline(settings, http=FakeHttp())
    pipe.feeds = []
    monkeypatch.setattr(pl.rss_src, "news_items", lambda *a, **k: iter(()))
    pipe.fetch_news("2026-10-10")
    assert len(pipe.errors) == 1 and pipe.errors[0].startswith("sec: SEC 8-K feeds skipped (LITRADAR_CONTACT_EMAIL not set;")
    from litradar.incremental import error_sources
    assert error_sources(pipe.errors) == ["sec"]  # so the coverage hint reads `--sources sec`, not `--sources SEC 8-K feeds skipped`
    pipe.store.close()


# ---- abstracts recovered from the full-text XML -------------------------------------------------
def test_missing_abstract_is_filled_from_the_fulltext_xml(tmp_path, monkeypatch):
    class XmlHttp(FakeHttp):
        def __init__(self):
            self.calls = 0

        def get(self, url, **k):
            self.calls += 1
            r = type("R", (), {})()
            r.status_code, r.content = 200, JATS
            return r

    def attach(http, p):
        p.sections["methods"] = METHODS
        p.fulltext_available = True
        return True

    http = XmlHttp()
    pipe = Pipeline(_settings(tmp_path), http=http)
    rep = _run(pipe, monkeypatch, [_preprint(abstract="")], attach=attach)
    row = pipe.store.get_paper(PID)
    assert row["abstract"].startswith("We profile tumours with a multiplexed in situ assay") and "The assay resolves single cells." in row["abstract"]
    assert json.loads(row["extra_json"])["abstract_source"] == "fulltext_xml"
    assert rep["counts"]["fulltext.abstract_from_jats"] == 1 and http.calls == 1
    assert "abstracts recovered from full-text XML 1" in render.render(pipe.store, *WINDOW, pipe.settings.radar, run_report=rep)["markdown"]
    # a record that already has an abstract is left alone and the XML is not re-read
    other = _preprint(id="doi:10.1101/other", doi="10.1101/other", abstract="Own abstract.")
    _run(pipe, monkeypatch, [other], attach=attach)
    assert http.calls == 1 and pipe.store.get_paper(other.id)["abstract"] == "Own abstract."
    pipe.store.close()
    # the Europe PMC path reads the XML through the source's (cached) fetcher
    monkeypatch.setattr(pl.epmc_src, "attach_fulltext", attach)
    monkeypatch.setattr(pl.epmc_src, "fulltext_xml", lambda http, pmcid, retries=1: JATS)
    ep = _preprint(id="doi:10.1000/epmc1", doi="10.1000/epmc1", source="europepmc", abstract="", fulltext_url="", extra={"pmcid": "PMC1", "is_open_access": "Y"})
    pipe3 = Pipeline(_settings(tmp_path), http=FakeHttp())
    pipe3.attach_fulltext({ep.id: ep})
    assert ep.abstract.startswith("We profile tumours") and pipe3.counts["fulltext.europepmc"] == 1
    pipe3.store.close()


# ---- rematch keeps what it cannot re-derive -----------------------------------------------------
def test_rematch_keeps_mentions_whose_source_text_is_not_stored(tmp_path):
    pipe = Pipeline(_settings(tmp_path), http=object())
    p = _preprint(license="cc_by")  # 'full' policy, but this record never had its text stored
    pipe.store.upsert_paper(p)
    pipe.store.replace_mentions(PID, [
        Mention(PID, "company", "acme", "Acme Genomics", "", "", "", "methods", 2, "kits from Acme Genomics"),  # from text that is not stored
        Mention(PID, "company", "acme", "Acme Genomics", "", "", "", "coi", 1, "equity in Acme Genomics"),
        Mention(PID, "company", "acme", "Acme Genomics", "", "", "", "ack", 1, "thanks"),
        Mention(PID, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "..."),
        Mention(PID, "instrument", "stale", "Stale Thing", "", "", "", "abstract", 1, "..."),  # the abstract IS stored: re-derived
    ])
    pipe.store.commit()
    assert Pipeline.underivable_sections(pipe.paper_from_row(pipe.store.get_paper(PID))) == ("methods", "funding", "coi", "ack", "data_availability", "body")
    rep = pipe.rematch()
    secs = _sections(pipe.store)
    assert {("acme", "methods"), ("acme", "coi"), ("acme", "ack"), ("txg-xenium", "abstract")} <= secs
    assert ("stale", "abstract") not in secs
    assert rep["counts"]["fulltext.purged"] == 0
    assert [r["id"] for r in pipe.store.search("Acme")] == [PID]  # the kept facts stay searchable
    # with Methods text stored, methods mentions ARE re-derived (a stale one goes); coi/ack without text stay
    p2 = _preprint(id="doi:10.1101/with-text", doi="10.1101/with-text", license="cc_by")
    p2.sections = {"methods": METHODS}
    p2.fulltext_available = True
    pipe.store.upsert_paper(p2)
    pipe.store.replace_mentions(p2.id, [
        Mention(p2.id, "instrument", "stale", "Stale Thing", "", "", "", "methods", 1, ""),
        Mention(p2.id, "company", "acme", "Acme Genomics", "", "", "", "coi", 1, ""),
    ])
    pipe.store.commit()
    assert Pipeline.underivable_sections(pipe.paper_from_row(pipe.store.get_paper(p2.id))) == ("funding", "coi", "ack", "data_availability", "body")
    pipe.rematch()
    secs2 = _sections(pipe.store, p2.id)
    assert ("stale", "methods") not in secs2 and ("pacb-revio", "methods") in secs2 and ("acme", "coi") in secs2
    pipe.store.close()


# ---- journal names, FTS maintenance --------------------------------------------------------------
def test_journal_names_are_canonicalised_from_journals_yaml(tmp_path, monkeypatch):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    pipe.journal_name_by_lower = {"nature methods": "Nature Methods"}
    pipe.journal_name_by_issn = {"1548-7091": "Nature Methods"}
    assert pipe.canonical_journal("Nature methods") == "Nature Methods" and pipe.canonical_journal("NATURE METHODS ") == "Nature Methods"
    assert pipe.canonical_journal("", ["1548-7091"]) == "Nature Methods" and pipe.canonical_journal("Cell") == "Cell"
    assert pipe.canonical_journal("Some Other Journal", ["1548-7091"]) == "Some Other Journal"  # a real name is never overridden by an ISSN
    papers = [
        _preprint(id="doi:10.1038/a", doi="10.1038/a", kind="article", journal="Nature methods", fulltext_url=""),
        _preprint(id="doi:10.1038/b", doi="10.1038/b", kind="article", journal="", fulltext_url="", extra={"issn": ["1548-7091"]}),
    ]
    _run(pipe, monkeypatch, papers, attach=_no_attach)
    assert pipe.store.get_paper("doi:10.1038/a")["journal"] == "Nature Methods" == pipe.store.get_paper("doi:10.1038/b")["journal"]
    pipe.store.close()


def test_run_merges_fts_segments_only_after_many_rewrites(tmp_path, monkeypatch):
    settings = _settings(tmp_path, fts_optimize_after=3)
    pipe = Pipeline(settings, http=FakeHttp())
    papers = [_preprint(id=f"doi:10.1101/o{i}", doi=f"10.1101/o{i}", fulltext_url="") for i in range(4)]
    rep = _run(pipe, monkeypatch, papers, attach=_no_attach)
    assert rep["counts"]["fts.rows_rewritten"] >= 4 and rep["counts"]["fts.optimized"] == 1
    pipe.store.close()
    pipe2 = Pipeline(settings, http=FakeHttp())
    rep2 = _run(pipe2, monkeypatch, papers, attach=_no_attach)  # the identical re-ingest rewrites nothing
    assert rep2["counts"]["fts.rows_unchanged"] == 4 and rep2["counts"]["fts.rows_rewritten"] == 0 and "fts.optimized" not in rep2["counts"]
    assert rep2["counts"]["papers.reingested"] == 4
    pipe2.store.close()


# ---- global-corpus wiring: GSA, GSA-Human, ChinaXiv, SciELO, KCI, J-STAGE and the OpenAlex country slices ------------
GLOBAL_PAPER_SOURCES = ("chinaxiv", "scielo", "kci", "jstage")


def _global_paper(src, i, **kw):
    base = dict(
        id=f"{src}:{i}", source=src, kind="article", title=f"{src} paper {i} on single-cell sequencing", doi=None,
        abstract="Libraries were sequenced on a DNBSEQ-T7.", journal=src.upper(), pub_date="2026-10-12",
        url=f"https://example.org/{src}/{i}", authors=[Author(name="Author One")],
    )
    base.update(kw)
    return Paper(**base)


def _raise(message):
    return lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message))


def test_run_reaches_global_corpus_sources_with_configured_caps(tmp_path, monkeypatch):
    settings = _settings(tmp_path, sources={
        "gsa": {"max_records": 123, "page_size": 50, "max_page_size": 900, "center": "NGDC"},
        "gsa_human": {"max_studies": 7},
        "chinaxiv": {"max_records": 50, "life_science_only": False},
        "scielo": {"collections": ["scl"], "subject_areas": ["Health Sciences"], "keywords": ["genom"], "max_articles": 9},
        "kci": {"keywords": ["genom"], "categories": ["의학"], "min_pub_year": 2020, "max_pages": 2},
        "jstage": {"enabled": True, "terms": ["genome"], "field": "text", "issns": ["1234-5678"], "max_records_per_query": 77},
    })
    pipe = Pipeline(settings, http=FakeHttp())
    calls = {}

    def gsa_fetch(http, start, end, center="NGDC", page_size=200, max_records=20000, max_page_size=1500):
        calls["gsa"] = (start, end, center, page_size, max_records, max_page_size)
        yield ArchiveRecord("gsa", "CRX1", "CRA1", "BGISEQ (model n/a)", "BGISEQ", "Genome", "2026-10-12", "", "WGS of a rice line", "")
        yield ArchiveRecord("gsa", "CRX2", "CRA1", "ILLUMINA (model n/a)", "ILLUMINA", "RNASeq", "2026-10-12", "", "RNA-seq", "")

    def gsa_human_fetch(http, start, end, max_studies=400):
        calls["gsa_human"] = (start, end, max_studies)
        yield ArchiveRecord("gsa-human", "HRX1", "HRA1", "DNBSEQ-T7", "BGISEQ", "WGS", "2026-10-13", "Example Hospital", "WGS cohort", "Homo sapiens")

    def chinaxiv_fetch(http, start, end, source="chinaxiv", max_records=5000, life_science_only=False):
        calls["chinaxiv"] = (start, end, source, max_records, life_science_only)
        yield _global_paper("chinaxiv", 1, kind="preprint", journal="ChinaXiv")

    def scielo_fetch(http, since, until, collections=None, subject_areas=None, keywords=(), max_articles=2000, fmt="opac", stats=None):
        calls["scielo"] = (since, until, collections, list(subject_areas or []), list(keywords), max_articles)
        if stats is not None:
            stats.update({"identifiers": 40, "fetched": 9, "kept": 1, "capped": 31, "identifier_errors": 0})
        yield _global_paper("scielo", 1)

    def kci_fetch(http, since, until, keywords=(), categories=(), min_pub_year=None, metadata_prefix="oai_kci", max_pages=10, stats=None):
        calls["kci"] = (since, until, list(keywords), list(categories), min_pub_year, max_pages)
        if stats is not None:
            stats.update({"records": 200, "kept": 1})
        yield _global_paper("kci", 1)

    def jstage_fetch(http, start, end, terms=(), field="abst", journals=(), issns=(), max_records=5000, year_slack=1, stats=None):
        calls["jstage"] = (start, end, list(terms), field, list(journals), list(issns), max_records)
        if stats is not None:
            stats.update({"queries": 2, "entries": 30, "in_window": 1, "papers": 1, "errors": 0, "truncated": 0})
        yield _global_paper("jstage", 1)

    monkeypatch.setattr(pl.gsa_src, "fetch", gsa_fetch)
    monkeypatch.setattr(pl.gsa_human_src, "fetch", gsa_human_fetch)
    monkeypatch.setattr(pl.chinaxiv_src, "fetch", chinaxiv_fetch)
    monkeypatch.setattr(pl.scielo_src, "fetch", scielo_fetch)
    monkeypatch.setattr(pl.kci_src, "fetch", kci_fetch)
    monkeypatch.setattr(pl.jstage_src, "fetch", jstage_fetch)
    rep = _run(pipe, monkeypatch, [_preprint(fulltext_url="")], attach=_no_attach,
               only=("biorxiv", "gsa", "gsa_human") + GLOBAL_PAPER_SOURCES)
    c = rep["counts"]
    assert rep["errors"] == []
    # radar.yaml caps and filters reach the adapters unchanged
    assert calls["gsa"] == (*WINDOW, "NGDC", 50, 123, 900)
    assert calls["gsa_human"] == (*WINDOW, 7)
    assert calls["chinaxiv"] == (*WINDOW, "chinaxiv", 50, False)
    assert calls["scielo"] == (*WINDOW, ["scl"], ["Health Sciences"], ["genom"], 9)
    assert calls["kci"] == (*WINDOW, ["genom"], ["의학"], 2020, 2)
    assert calls["jstage"] == (*WINDOW, ["genome"], "text", [], ["1234-5678"], 77)
    assert c["archive.gsa_new"] == 2 and c["archive.gsa_human_new"] == 1
    assert all(c[f"fetched.{src}"] == 1 for src in GLOBAL_PAPER_SOURCES)
    assert c["papers.unique"] == 5 and c["papers.new"] == 5
    assert c["scielo.fetched"] == 9 and c["scielo.kept"] == 1 and c["scielo.capped"] == 31 and "scielo.identifier_errors" not in c
    assert c["kci.records"] == 200 and c["kci.kept"] == 1
    assert c["jstage.queries"] == 2 and c["jstage.papers"] == 1
    for src in GLOBAL_PAPER_SOURCES:
        assert pipe.store.get_paper(f"{src}:1")["source"] == src
    # the Chinese archives reach the census through the instrument gazetteer: the GSA family label and the GSA-Human model both
    # resolve to MGI Tech; an Illumina family label without a model is labelled from the platform string instead of being dropped
    rows = {r["instrument_model"]: r for r in pipe.store.archive_instrument_counts(*WINDOW, archives=("gsa", "gsa-human"))}
    assert rows["BGISEQ (model n/a)"]["vendor"] == "MGI Tech" and rows["BGISEQ (model n/a)"]["ticker"] == "688114.SS"
    assert rows["BGISEQ (model n/a)"]["archive"] == "gsa" and rows["BGISEQ (model n/a)"]["vendor_mapped"] is True
    assert rows["DNBSEQ-T7"]["vendor"] == "MGI Tech" and rows["DNBSEQ-T7"]["archive"] == "gsa-human"
    assert rows["ILLUMINA (model n/a)"]["vendor_mapped"] is False and rows["ILLUMINA (model n/a)"]["vendor"].startswith("Illumina")
    md = render.render(pipe.store, *WINDOW, settings.radar, run_report=rep, company_types=pipe.company_types)["markdown"]
    assert "New archive records: GSA 2, GSA" in md and "HUMAN 1" in md
    pipe.store.close()


def test_global_corpus_toggles_are_respected_and_jstage_is_opt_in(tmp_path, monkeypatch):
    everything_else = ("sra", "ena", "geo", "pride", "cellxgene", "nih_reporter", "clinicaltrials", "openfda", "europepmc", "openalex",
                       "crossref", "arxiv", "rss", "semantic_scholar")
    settings = _settings(tmp_path, sources={name: {"enabled": False} for name in everything_else + ("gsa", "gsa_human", "chinaxiv", "scielo", "kci")})
    wired = ((pl.gsa_src, "gsa"), (pl.gsa_human_src, "gsa_human"), (pl.chinaxiv_src, "chinaxiv"), (pl.scielo_src, "scielo"), (pl.kci_src, "kci"), (pl.jstage_src, "jstage"))
    for mod, name in wired:
        monkeypatch.setattr(mod, "fetch", _raise(f"{name} must not run"))
    monkeypatch.setattr(pl.biorxiv_src, "fetch", lambda *a, **k: iter([_preprint(fulltext_url="")]))
    from litradar.incremental import error_sources
    wired_ids = {"gsa", "gsa_human", "chinaxiv", "scielo", "kci", "jstage"}
    # `enabled: false` is honoured for each new source, and J-STAGE stays off when radar.yaml has no `jstage:` key at all
    pipe = Pipeline(settings, http=FakeHttp())
    rep = pipe.run(*WINDOW, only=None, fulltext=False)
    assert not (set(error_sources(rep["errors"])) & wired_ids) and rep["counts"]["papers.unique"] == 1  # (the SEC skip notice is unrelated)
    pipe.store.close()
    # the other five default to on when their key is absent; J-STAGE needs an explicit `enabled: true`
    hit = []
    for mod, name in wired:
        monkeypatch.setattr(mod, "fetch", (lambda n: lambda *a, **k: hit.append(n) or iter(()))(name))
    for name in ("gsa", "gsa_human", "chinaxiv", "scielo", "kci"):
        del settings.radar["sources"][name]
    settings.radar["sources"]["jstage"] = {"enabled": True}
    pipe2 = Pipeline(settings, http=FakeHttp())
    rep2 = pipe2.run(*WINDOW, only=None, fulltext=False)
    assert not (set(error_sources(rep2["errors"])) & wired_ids) and sorted(hit) == ["chinaxiv", "gsa", "gsa_human", "jstage", "kci", "scielo"]
    pipe2.store.close()
    # --sources reaches the archive stage for the Chinese archives on its own, and naming J-STAGE there is the one-off opt-in
    hit.clear()
    del settings.radar["sources"]["jstage"]
    pipe3 = Pipeline(settings, http=FakeHttp())
    pipe3.run(*WINDOW, only=["gsa"], fulltext=False)
    assert hit == ["gsa"]
    pipe3.store.close()
    hit.clear()
    pipe4 = Pipeline(settings, http=FakeHttp())
    pipe4.run(*WINDOW, only=["jstage", "gsa_human"], fulltext=False)
    assert sorted(hit) == ["gsa_human", "jstage"]
    pipe4.store.close()


def test_global_corpus_source_failures_are_isolated(tmp_path, monkeypatch):
    pipe = Pipeline(_settings(tmp_path), http=FakeHttp())
    monkeypatch.setattr(pl.gsa_src, "fetch", _raise("GSA: unexpected search page for 2026-10-12 page 1 (JavaScript challenge page)"))
    monkeypatch.setattr(pl.gsa_human_src, "fetch", lambda *a, **k: iter([ArchiveRecord("gsa-human", "HRX9", "HRA9", "Illumina NovaSeq 6000", "ILLUMINA", "WGS", "2026-10-12", "", "", "Homo sapiens")]))
    monkeypatch.setattr(pl.scielo_src, "fetch", _raise("identifier page failed"))
    monkeypatch.setattr(pl.kci_src, "fetch", lambda *a, **k: iter([_global_paper("kci", 2)]))
    monkeypatch.setattr(pl.chinaxiv_src, "fetch", lambda *a, **k: iter([_global_paper("chinaxiv", 2, kind="preprint")]))
    rep = _run(pipe, monkeypatch, [_preprint(fulltext_url="")], attach=_no_attach, only=("biorxiv", "gsa", "gsa_human", "chinaxiv", "scielo", "kci"))
    assert any(e.startswith("gsa: GSA: unexpected search page") for e in rep["errors"])
    assert any(e.startswith("scielo: identifier page failed") for e in rep["errors"])
    assert rep["counts"]["archive.gsa_human_new"] == 1 and "archive.gsa_new" not in rep["counts"]
    assert rep["counts"]["fetched.kci"] == 1 and rep["counts"]["fetched.chinaxiv"] == 1 and rep["counts"]["papers.unique"] == 3
    from litradar.incremental import error_sources
    assert error_sources(rep["errors"]) == ["scielo", "gsa"]  # papers stage first, then archives; ids usable as `--sources`
    pipe.store.close()


def test_openalex_country_slices_repeat_each_topic_query_per_country(tmp_path, monkeypatch):
    oa_cfg = {"enabled": True, "topics": True, "company_authors": False, "tracked_authors": False, "journals": False}
    settings = _settings(tmp_path, sources={"openalex": dict(oa_cfg)})
    pipe = Pipeline(settings, http=FakeHttp())
    pipe.subgroups = [{"id": "g1", "openalex_topics": ["T1", "T2"], "keywords": ["x", "y"]}, {"id": "g2", "openalex_topics": [], "keywords": ["z"]}]
    seen = []

    def works(http, filters, mailto="", api_key="", max_records=10000, select=None, extra_params=None, state=None):
        seen.append(dict(filters))
        if state is not None:
            state["complete"] = True
        if filters.get("authorships.institutions.country_code") == "CN":
            yield {
                "id": "https://openalex.org/W1", "title": "A single-cell atlas", "publication_date": "2026-10-12", "type": "article",
                "authorships": [{"author": {"display_name": "A B", "id": "https://openalex.org/A1"},
                                 "institutions": [{"display_name": "Example University", "type": "education", "country_code": "CN"}],
                                 "raw_affiliation_strings": ["Example University, Beijing"]}],
            }

    monkeypatch.setattr(pl.openalex_src, "works", works)
    papers = pipe.fetch_papers(*WINDOW, only=["openalex"])
    assert pipe.errors == []
    assert len(seen) == 2  # the global topic query and its CN slice; the subgroup without topics issues nothing
    glob, cn = seen
    assert "authorships.institutions.country_code" not in glob
    assert cn["authorships.institutions.country_code"] == "CN" and cn["primary_topic.id"] == glob["primary_topic.id"] == "T1|T2"
    assert cn["title_and_abstract.search"] == glob["title_and_abstract.search"] == "x|y"
    assert cn["from_publication_date"] == WINDOW[0] and cn["to_publication_date"] == WINDOW[1]
    assert pipe.counts["fetched.openalex.topics_cn"] == 1 and list(papers) == ["openalex:W1"]
    pipe.store.close()
    # [] switches the slices off; several codes are each queried once per subgroup, upper-cased
    for slices, expected in (([], 1), (["cn", "JP"], 3)):
        seen.clear()
        settings.radar["sources"]["openalex"] = dict(oa_cfg, country_slices=slices)
        pipe2 = Pipeline(settings, http=FakeHttp())
        pipe2.subgroups = pipe.subgroups
        pipe2.fetch_papers(*WINDOW, only=["openalex"])
        assert len(seen) == expected
        assert [f.get("authorships.institutions.country_code") for f in seen] == [None] + [s.upper() for s in slices]
        pipe2.store.close()


def test_shipped_radar_yaml_wires_the_global_corpus_sources():
    sources = Settings.from_env().radar["sources"]
    for name in ("gsa", "gsa_human", "chinaxiv", "scielo", "kci"):
        assert sources[name]["enabled"] is True, name
    assert sources["jstage"]["enabled"] is False  # terms: 24-hour caching clause and credit requirement; opt-in only
    assert sources["openalex"]["country_slices"] == ["CN"]
    assert sources["gsa"]["center"] == "NGDC" and sources["gsa"]["statistics_snapshot"] is False
    assert sources["chinaxiv"]["life_science_only"] is True and sources["kci"]["keywords"]
