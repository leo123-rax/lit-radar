"""Pipeline.run / rematch against canned adapters (no network): re-ingest safety, the data policy, the
wiring of the archive / signal / citation adapters, tracked-author caps and the WAL checkpoint."""
import json
import logging

import pytest

from litradar import pipeline as pl
from litradar.config import Settings
from litradar.digest import render
from litradar.models import ArchiveRecord, Author, NewsItem, Paper
from litradar.pipeline import Pipeline

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
    assert pipe.store.search("Revio") == []  # nothing of the restricted text is searchable
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
    pipe.store.close()
