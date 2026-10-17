import json
import logging

import pytest

from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.rank import heuristics
from litradar.store import Store, archive_vendor_label


def _paper(i: int, **kw) -> Paper:
    base = dict(
        id=f"doi:10.1000/test{i}", source="biorxiv", kind="preprint", title=f"Paper {i} about Xenium spatial profiling",
        doi=f"10.1000/test{i}", abstract="We present a novel spatial transcriptomics method using Xenium.", journal="biorxiv",
        pub_date="2026-10-12", url=f"https://example.org/{i}", authors=[Author(name="Jane Doe", affiliation="10x Genomics")],
        affiliations=["10x Genomics, Pleasanton"], sections={"methods": "Sequenced on NovaSeq X Plus.", "body": "full body text here"},
        fulltext_available=True,
    )
    base.update(kw)
    return Paper(**base)


def test_upsert_search_and_counts(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    assert st.upsert_paper(_paper(1)) is True
    assert st.upsert_paper(_paper(1, abstract="")) is False  # update, abstract preserved
    assert st.get_paper("doi:10.1000/test1")["abstract"].startswith("We present")
    st.upsert_paper(_paper(2, title="Unrelated proteomics work", abstract="Olink Explore plasma proteomics", sections={}))
    st.replace_mentions("doi:10.1000/test1", [
        Mention("doi:10.1000/test1", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "..."),
        Mention("doi:10.1000/test1", "instrument", "ilmn-novaseq-x", "NovaSeq X", "Illumina", "ILMN", "sequencer.short_read", "methods", 1, "..."),
        Mention("doi:10.1000/test1", "company", "10x-genomics", "10x Genomics", "", "TXG", "", "affiliations", 1, "..."),
    ])
    st.commit()
    rows = st.search("Xenium", since="2026-10-01")
    assert [r["id"] for r in rows] == ["doi:10.1000/test1"]
    assert st.search("NovaSeq", sections=["methods"])[0]["id"] == "doi:10.1000/test1"
    assert st.search("Olink Explore")[0]["id"] == "doi:10.1000/test2"
    assert st.search("weird-query:with(chars")  == []  # falls back to a phrase query without raising
    inst = st.instrument_counts("2026-10-10", "2026-10-17")
    assert {r["entity_id"] for r in inst} == {"txg-xenium", "ilmn-novaseq-x"}
    comp = st.company_counts("2026-10-10", "2026-10-17")
    assert comp[0]["entity_id"] == "10x-genomics" and comp[0]["as_author"] == 1
    assert st.entity_papers("txg-xenium")[0]["id"] == "doi:10.1000/test1"


def test_archive_records_and_counts(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    recs = [ArchiveRecord("sra", f"SRX{i}", "SRP1", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", "2026-10-12", "c", "t", "Homo sapiens", "ilmn-novaseq-x", "Illumina", "ILMN") for i in range(3)]
    recs.append(ArchiveRecord("sra", "SRX9", "SRP2", "PacBio Revio", "PACBIO_SMRT", "WGS", "2026-10-13", "c", "t", "Mus musculus", "pacb-revio", "Pacific Biosciences", "PACB"))
    assert st.upsert_archive_records(recs) == 4
    assert st.upsert_archive_records(recs) == 0  # idempotent
    rows = st.archive_instrument_counts("2026-10-10", "2026-10-17")
    assert rows[0]["instrument_model"] == "Illumina NovaSeq X Plus" and rows[0]["experiments"] == 3
    assert rows[0]["archive"] == "sra" and rows[0]["vendor_mapped"] is True


def test_heuristic_scoring_is_monotone():
    paper = {"kind": "article", "title": "A novel method", "abstract": "We present a single-cell atlas.", "cited_by_count": 0, "fulltext_available": True}
    m_inst = [{"kind": "instrument", "entity_id": "x", "section": "methods"}]
    low, _ = heuristics.score_paper(paper, [], "C", ["single-cell"], False)
    mid, _ = heuristics.score_paper(paper, m_inst, "A", ["single-cell"], False)
    high, reasons = heuristics.score_paper(paper, m_inst, "A", ["single-cell"], True)
    assert low < mid < high <= 10
    assert "tracked author" in reasons
    assert heuristics.blend(4.0, 8.0, 0.5) == 6.0
    assert heuristics.blend(4.0, None) == 4.0


def test_json_roundtrip_of_sections(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_paper(_paper(3))
    row = st.get_paper("doi:10.1000/test3")
    assert json.loads(row["sections_json"])["methods"].startswith("Sequenced")
    assert "body" not in json.loads(row["sections_json"])  # body lives only in the FTS table
    assert st.body_for("doi:10.1000/test3") == "full body text here"


def test_http_circuit_breaker(monkeypatch):
    from litradar.http import Http

    class R:
        def __init__(self, code):
            self.status_code, self.headers, self.content, self.url = code, {"Retry-After": "120"}, b"", "u"

    http = Http(max_retries=1)
    http.max_backoff = 0.0
    monkeypatch.setattr(http.session, "get", lambda *a, **k: R(429))
    monkeypatch.setattr("litradar.http.time.sleep", lambda s: None)
    assert http.get("https://blocked.example/a") is not None  # second 429 returns the response and trips the breaker
    assert "blocked.example" in http.blocked_hosts
    assert http.get("https://blocked.example/b") is None and http.stats["blocked_skips"] == 1
    monkeypatch.setattr(http.session, "get", lambda *a, **k: R(200))
    assert http.get("https://other.example/c").status_code == 200


def test_rematch_roundtrip(tmp_path, monkeypatch):
    from litradar.config import Settings
    from litradar.pipeline import Pipeline

    settings = Settings.from_env()
    settings.db_path = tmp_path / "r.sqlite"
    pipe = Pipeline(settings, http=object())  # no network needed for rematch
    p = _paper(7, sections={"methods": "Sequenced on a Revio system (PacBio).", "body": "body"})
    pipe.store.upsert_paper(p)
    pipe.store.commit()
    rep = pipe.rematch()
    assert rep["counts"]["rematched"] == 1
    ids = {m["entity_id"] for m in pipe.store.mentions_for(p.id)}
    assert {"pacb-revio", "txg-xenium", "10x-genomics"} <= ids
    assert pipe.store.get_score(p.id)["final"] > 0


# ---- re-ingest safety -------------------------------------------------------------------------
def test_upsert_never_flips_kind_keeps_earliest_date_and_unions_extra(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    pid = "doi:10.1000/test10"
    st.upsert_paper(_paper(10, kind="article", pub_date="2026-10-05", journal="Nature Methods", extra={"pmcid": "PMC1", "sources": ["europepmc"]}))
    # a later, preprint-flavoured record of the same DOI with a newer date and different extra keys
    st.upsert_paper(_paper(10, kind="preprint", pub_date="2026-10-12", journal="", extra={"is_open_access": "Y", "sources": ["biorxiv"], "pmcid": ""}))
    row = st.get_paper(pid)
    assert row["kind"] == "article"
    assert row["pub_date"] == "2026-10-05"
    assert row["journal"] == "Nature Methods"
    extra = json.loads(row["extra_json"])
    assert extra["pmcid"] == "PMC1" and extra["is_open_access"] == "Y"
    assert extra["sources"] == ["biorxiv", "europepmc"]
    st.upsert_paper(_paper(10, pub_date="2026-09-30"))  # an earlier date arriving later wins
    assert st.get_paper(pid)["pub_date"] == "2026-09-30"
    st.upsert_paper(_paper(10, pub_date=""))  # an empty date never overwrites
    assert st.get_paper(pid)["pub_date"] == "2026-09-30"


def test_metadata_refetch_keeps_body_sections_and_affiliations(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    pid = "doi:10.1000/test11"
    st.upsert_paper(_paper(11))
    st.upsert_paper(_paper(11, sections={}, fulltext_available=False, affiliations=["Somewhere Else"]))
    row = st.get_paper(pid)
    assert json.loads(row["sections_json"])["methods"].startswith("Sequenced")
    assert st.body_for(pid) == "full body text here"
    assert row["fulltext_available"] == 1
    assert set(json.loads(row["affiliations_json"])) == {"Somewhere Else", "10x Genomics, Pleasanton"}
    assert st.search("full body text")[0]["id"] == pid


# ---- FTS keyed by rowid -------------------------------------------------------------------------
def test_fts_rows_keyed_by_papers_rowid_and_legacy_store_is_rebuilt_once(tmp_path, caplog):
    path = tmp_path / "t.sqlite"
    st = Store(path)
    for i in range(5):
        st.upsert_paper(_paper(20 + i))
    st.commit()
    pairs = st.conn.execute("SELECT f.rowid, p.rowid FROM papers_fts f JOIN papers p ON p.id = f.id").fetchall()
    assert len(pairs) == 5 and all(a == b for a, b in pairs)
    assert st.fts_rebuilt == 0
    # simulate a store written by an older version: FTS rows with arbitrary rowids
    st.conn.execute("DELETE FROM papers_fts")
    for i, (pid, title) in enumerate(st.conn.execute("SELECT id, title FROM papers ORDER BY rowid").fetchall()):
        st.conn.execute(
            "INSERT INTO papers_fts (rowid, id, title, abstract, methods, funding, coi, affiliations, body) VALUES (?,?,?,?,?,?,?,?,?)",
            (1000 + i, pid, title, "", "", "", "", "", f"legacy body {pid}"),
        )
    st.conn.commit()
    st.close()
    with caplog.at_level(logging.WARNING, logger="litradar.store"):
        st2 = Store(path)
    assert st2.fts_rebuilt == 10  # 5 misplaced rows + 5 papers without a row at their rowid
    assert any("rebuilding" in r.message for r in caplog.records)
    pairs = st2.conn.execute("SELECT f.rowid, p.rowid FROM papers_fts f JOIN papers p ON p.id = f.id").fetchall()
    assert len(pairs) == 5 and all(a == b for a, b in pairs)
    assert st2.body_for("doi:10.1000/test22") == "legacy body doi:10.1000/test22"  # bodies survive the rebuild
    assert len(st2.search("legacy")) == 5
    st2.close()
    assert Store(path).fts_rebuilt == 0  # only once


def test_iter_papers_streams_in_batches_while_writing(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    for i in range(7):
        st.upsert_paper(_paper(60 + i))
    st.commit()
    seen = []
    for row in st.iter_papers(batch=3):
        seen.append(row["id"])
        st.save_score(row["id"], 1.0, None, 1.0, "r")  # writes between batches are fine
        assert row["_rowid"] > 0
    assert len(seen) == 7 and len(set(seen)) == 7
    assert len(list(st.iter_papers(since="2026-10-01", batch=2))) == 7
    assert list(st.iter_papers(since="2027-01-01")) == []


# ---- data policy --------------------------------------------------------------------------------
def test_restricted_policy_strips_fulltext_and_purge_cleans_legacy_rows(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    p = _paper(30, license="cc_no", extra={"fulltext_policy": "facts_only", "grants": ["NIH"]},
               sections={"methods": "Revio", "funding": "long funding text parsed from the full text", "coi": "x", "body": "body"})
    st.upsert_paper(p)
    secs = json.loads(st.get_paper(p.id)["sections_json"])
    assert "methods" not in secs and "coi" not in secs and secs["funding"] == "NIH"  # funding only from grant metadata
    assert st.body_for(p.id) == ""
    # a cc_no preprint cached with full text before the policy existed
    q = _paper(31, license="cc_no", sections={"methods": "Sequenced on a Revio.", "body": "legacy body"})
    st.upsert_paper(q)
    assert st.body_for(q.id) == "legacy body"
    # a CC-BY paper and an open-access Europe PMC article are untouched
    r = _paper(32, license="cc_by", sections={"methods": "NovaSeq", "body": "ok body"})
    s = _paper(33, license="", source="europepmc", extra={"is_open_access": "Y"}, sections={"methods": "NovaSeq", "body": "oa body"})
    st.upsert_paper(r)
    st.upsert_paper(s)
    assert st.purge_restricted_fulltext() == 1
    assert st.body_for(q.id) == "" and "methods" not in json.loads(st.get_paper(q.id)["sections_json"])
    assert json.loads(st.get_paper(q.id)["extra_json"])["fulltext_policy"] == "facts_only"
    assert st.body_for(r.id) == "ok body" and st.body_for(s.id) == "oa body"
    assert st.purge_restricted_fulltext() == 0  # idempotent
    assert st.search("legacy") == []


def test_replace_mentions_keeps_sections_whose_text_is_not_stored(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    pid = "doi:10.1000/test40"
    st.upsert_paper(_paper(40))
    xen = Mention(pid, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "...")
    st.replace_mentions(pid, [Mention(pid, "instrument", "pacb-revio", "Revio", "Pacific Biosciences", "PACB", "sequencer.long_read", "methods", 2, "..."), xen])
    # a metadata-only re-extraction finds only the abstract mention; the methods facts must survive
    assert st.replace_mentions(pid, [xen], keep_sections=("methods",)) == 2
    assert ("pacb-revio", "methods") in {(m["entity_id"], m["section"]) for m in st.mentions_for(pid)}
    # without keep_sections the old behaviour (full replacement) applies
    assert st.replace_mentions(pid, [xen]) == 1
    assert {m["entity_id"] for m in st.mentions_for(pid)} == {"txg-xenium"}
    # when the new extraction DOES cover methods, stale methods mentions are replaced
    st.replace_mentions(pid, [Mention(pid, "instrument", "pacb-revio", "Revio", "", "", "", "methods", 1, "")])
    st.replace_mentions(pid, [Mention(pid, "instrument", "ilmn-novaseq-x", "NovaSeq X", "", "", "", "methods", 1, "")], keep_sections=("methods",))
    secs = {(m["entity_id"], m["section"]) for m in st.mentions_for(pid)}
    assert ("pacb-revio", "methods") not in secs and ("ilmn-novaseq-x", "methods") in secs


# ---- preprint -> publication --------------------------------------------------------------------
def test_preprint_collapses_into_its_journal_version(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    pre = _paper(50, id="doi:10.1101/2026.01.01.1", doi="10.1101/2026.01.01.1", kind="preprint", pub_date="2026-10-11",
                 published_doi="10.1038/s41592-026-0050-1", title="Spatial profiling of tumours with a new in situ method")
    art = _paper(51, id="doi:10.1038/s41592-026-0050-1", doi="10.1038/s41592-026-0050-1", kind="article", pub_date="2026-10-15",
                 journal="Nature Methods", title="A completely different title chosen by the journal")
    pre2 = _paper(52, id="doi:10.1101/2026.01.01.2", doi="10.1101/2026.01.01.2", kind="preprint", pub_date="2026-10-10",
                  published_doi=None, title="Title matched preprint of a spatial method paper")
    art2 = _paper(53, id="doi:10.1000/art2", doi="10.1000/art2", kind="article", pub_date="2026-10-16", journal="Cell",
                  title="Title-matched preprint of a spatial method paper.")
    lone = _paper(54, id="doi:10.1101/lone", doi="10.1101/lone", kind="preprint", pub_date="2026-10-12",
                  title="A lone preprint nobody has published in a journal yet")
    for p in (pre, art, pre2, art2, lone):
        st.upsert_paper(p)
    for pid, score in ((pre.id, 7.0), (art.id, 5.0), (pre2.id, 4.0), (art2.id, 4.5), (lone.id, 3.0)):
        st.save_score(pid, score, None, score, "r")
    st.commit()
    top = st.top_papers("2026-10-10", "2026-10-17", limit=10)
    ids = [t["id"] for t in top]
    assert pre.id not in ids and pre2.id not in ids and lone.id in ids and art.id in ids and art2.id in ids
    art_row = next(t for t in top if t["id"] == art.id)
    assert art_row["preprints"] == [{"id": pre.id, "pub_date": "2026-10-11", "url": pre.url, "matched_by": "published_doi"}]
    assert art_row["final"] == 7.0  # the better of the two scores
    assert next(t for t in top if t["id"] == art2.id)["preprints"][0]["matched_by"] == "title"
    conv = st.preprint_conversions("2026-10-10", "2026-10-17")
    assert {(c["preprint_id"], c["article_id"], c["matched_by"]) for c in conv} == {(pre.id, art.id, "published_doi"), (pre2.id, art2.id, "title")}
    # the journal version outside the window: the preprint stays
    assert st.preprint_conversions("2026-10-10", "2026-10-12") == []
    assert pre.id in [t["id"] for t in st.top_papers("2026-10-10", "2026-10-12")]


# ---- archive census labels ----------------------------------------------------------------------
def test_archive_counts_label_unmapped_platforms_and_filter_by_archive(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    recs = [ArchiveRecord("sra", f"SRX{i}", "SRP1", "unspecified", "ILLUMINA", "WGS", "2026-10-12") for i in range(3)]
    recs.append(ArchiveRecord("sra", "SRX9", "SRP2", "Illumina FooSeq 9000", "ILLUMINA", "WGS", "2026-10-12"))
    recs.append(ArchiveRecord("ena", "ERX1", "ERP1", "", "", "WGS", "2026-10-12"))
    recs.append(ArchiveRecord("pride", "PXD1", "PXD1", "Orbitrap Astral", "MS", "Bottom-up", "2026-10-13", entity_id="thermo-orbitrap-astral", vendor="Thermo Fisher Scientific", ticker="TMO"))
    recs.append(ArchiveRecord("cellxgene", "ds1#EFO:1", "col1", "10x 3' v3", "single-cell", "EFO:1", "2026-10-13", entity_id="txg-chromium", vendor="10x Genomics", ticker="TXG"))
    recs.append(ArchiveRecord("cellxgene", "ds2#EFO:2", "col1", "Drop-seq", "single-cell", "EFO:2", "2026-10-13"))
    st.upsert_archive_records(recs)
    rows = st.archive_instrument_counts("2026-10-10", "2026-10-17")
    assert all(r["vendor"] for r in rows)  # never blank
    by_model = {r["instrument_model"]: r for r in rows}
    assert by_model["unspecified"]["vendor"] == "Illumina (model unspecified)" and by_model["unspecified"]["vendor_mapped"] is False
    assert by_model["Illumina FooSeq 9000"]["vendor"] == "Illumina (unmapped model)"
    assert by_model[""]["vendor"] == "unknown platform"
    assert by_model["Drop-seq"]["vendor"] == "Single-cell assay (unmapped model)"
    seq = st.archive_instrument_counts("2026-10-10", "2026-10-17", archives=("sra", "ena", "geo"))
    assert {r["archive"] for r in seq} == {"sra", "ena"}
    pride = st.archive_instrument_counts("2026-10-10", "2026-10-17", archives=("pride",))
    assert len(pride) == 1 and pride[0]["vendor"] == "Thermo Fisher Scientific" and pride[0]["experiments"] == 1 and pride[0]["archive"] == "pride"
    assert archive_vendor_label("", "PACBIO_SMRT", "unspecified") == "Pacific Biosciences (model unspecified)"
    assert archive_vendor_label("", "", "Mystery 3000") == "vendor unmapped"
    assert archive_vendor_label("Illumina", "ILLUMINA", "x") == "Illumina"
    assert [s["library_strategy"] for s in st.archive_strategy_counts("2026-10-10", "2026-10-17", archives=("pride",))] == ["Bottom-up"]


# ---- news feeds, checkpoint, close ----------------------------------------------------------------
def test_news_feed_filters_checkpoint_and_idempotent_close(tmp_path):
    path = tmp_path / "t.sqlite"
    st = Store(path)
    st.upsert_news([
        NewsItem("n1", "news:x", "Headline", "https://e/1", "2026-10-12", "", []),
        NewsItem("s1", "fda-510k", "Device — Applicant", "https://e/2", "2026-10-12", "", ["illumina"]),
    ])
    st.commit()
    assert [r["id"] for r in st.news_in_window("2026-10-10", feeds=("fda-510k",))] == ["s1"]
    assert [r["id"] for r in st.news_in_window("2026-10-10", exclude_feeds=("fda-510k",))] == ["n1"]
    assert st.news_in_window("2026-10-10", feeds=("fda-510k",))[0]["companies"] == ["illumina"]
    st.checkpoint()
    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    st.close()
    st.close()  # second close is a no-op
    with Store(path) as st2:
        assert st2.counts()["news_items"] == 2


def test_search_rejects_unknown_section(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    with pytest.raises(ValueError):
        st.search("x", sections=["nope"])
