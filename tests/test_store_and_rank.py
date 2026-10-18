import json
import logging
import sqlite3

import pytest
import yaml

from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.rank import heuristics
from litradar.store import SCHEMA, Store, archive_vendor_label, mentions_text


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
    # the preprint's date is its first posting, which the bioRxiv DOI encodes (2026.01.01), not the stored revision date
    assert art_row["preprints"] == [{"id": pre.id, "pub_date": "2026-01-01", "url": pre.url, "matched_by": "published_doi"}]
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


# ---- the `mentions` full-text column and matched aliases ------------------------------------------
def test_mentions_column_makes_attributed_entities_searchable_and_aliases_are_stored(tmp_path):
    from litradar.extract.gazetteer import Gazetteer

    st = Store(tmp_path / "t.sqlite")
    pid = "doi:10.1000/test70"
    # the paper never says "Chromium": the gazetteer matched the alias "Next GEM"
    text = "Single-cell libraries were made with the Next GEM 5' kit."
    st.upsert_paper(_paper(70, title="Immune profiling of neonates", abstract=text, sections={}))
    gz = Gazetteer.from_yaml({"entries": [{"id": "txg-chromium", "name": "Chromium", "vendor": "10x Genomics", "ticker": "TXG",
                                           "category": "single_cell", "aliases": ["Next GEM", "Chromium X"]}]}, "instrument")
    found = gz.find(text, section="abstract", paper_id=pid)
    assert len(found) == 1 and found[0].count == 1 and found[0].alias == "Next GEM"
    two = gz.find(text + " Libraries were run on a Chromium X.", section="abstract", paper_id=pid)
    assert two[0].count == 2 and set(two[0].alias.split("; ")) == {"Next GEM", "Chromium X"}
    st.replace_mentions(pid, found)
    st.commit()
    assert st.mentions_for(pid)[0]["alias"] == "Next GEM"
    assert mentions_text([("Chromium", "Next GEM; Chromium X", "txg-chromium"), ("Chromium", "", "txg-chromium")]) == "Chromium ; Next GEM ; Chromium X ; txg-chromium"
    assert [r["id"] for r in st.search("Chromium")] == [pid]  # canonical name, absent from the text
    assert [r["id"] for r in st.search('"Next GEM"', sections=["mentions"])] == [pid]  # matched alias
    assert [r["id"] for r in st.search('"txg-chromium"')] == [pid]  # gazetteer id
    assert st.search("Chromium", sections=["title", "abstract"]) == []  # the text columns are untouched
    st.replace_mentions(pid, [])  # removing the mention removes it from search
    assert st.search("Chromium") == [] and st.fts_mentions_updated == 2


def test_fts_row_is_rewritten_only_when_indexed_text_changes(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    p = _paper(71)
    st.upsert_paper(p)
    assert (st.fts_rows_written, st.fts_rows_unchanged) == (1, 0)
    st.upsert_paper(_paper(71))  # identical re-ingest
    assert (st.fts_rows_written, st.fts_rows_unchanged) == (1, 1)
    # a metadata-only re-fetch: stored sections, body and abstract win, so nothing indexed changes
    st.upsert_paper(_paper(71, sections={}, fulltext_available=False, cited_by_count=5, abstract=""))
    assert (st.fts_rows_written, st.fts_rows_unchanged) == (1, 2)
    assert st.get_paper(p.id)["cited_by_count"] == 5  # the papers row itself was still updated
    assert st.body_for(p.id) == "full body text here" and st.search("Xenium")[0]["id"] == p.id
    st.upsert_paper(_paper(71, abstract="A longer, different abstract about Xenium."))
    assert st.fts_rows_written == 2
    assert "longer" in st.search("longer")[0]["snippet"]
    # a mentions change refreshes the mentions column once, and not again for the same mentions
    m = Mention(p.id, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "...")
    st.replace_mentions(p.id, [m])
    st.replace_mentions(p.id, [m])
    assert st.fts_mentions_updated == 1 and st.fts_changes == 3
    st.upsert_paper(_paper(71, abstract="A longer, different abstract about Xenium."))  # unchanged again; mentions text kept
    assert st.fts_rows_written == 2 and [r["id"] for r in st.search("txg", sections=["mentions"])] == [p.id]
    st.optimize()  # segment merge is safe at any time and keeps the index intact
    assert st.search("Xenium")[0]["id"] == p.id


def test_legacy_fts_without_mentions_column_is_migrated_once(tmp_path, caplog):
    path = tmp_path / "t.sqlite"
    st = Store(path)
    st.upsert_paper(_paper(72))
    st.upsert_paper(_paper(73, sections={"methods": "Sequenced on a Revio.", "body": "legacy body text"}))
    st.replace_mentions("doi:10.1000/test73", [Mention("doi:10.1000/test73", "instrument", "pacb-revio", "Revio", "", "", "sequencer.long_read", "methods", 1, "")])
    st.commit()
    # recreate the pre-`mentions` layout: an 8-column FTS table with the same rows, and a mentions table without alias
    st.conn.execute("CREATE TEMP TABLE keep AS SELECT rowid AS rid, id, title, abstract, methods, funding, coi, affiliations, body FROM papers_fts")
    st.conn.execute("DROP TABLE papers_fts")
    st.conn.execute("CREATE VIRTUAL TABLE papers_fts USING fts5(id UNINDEXED, title, abstract, methods, funding, coi, affiliations, body, tokenize='porter unicode61')")
    st.conn.execute("INSERT INTO papers_fts(rowid, id, title, abstract, methods, funding, coi, affiliations, body) SELECT rid, id, title, abstract, methods, funding, coi, affiliations, body FROM keep")
    st.conn.execute("ALTER TABLE mentions DROP COLUMN alias")
    st.conn.commit()
    st.close()
    with caplog.at_level(logging.WARNING, logger="litradar.store"):
        st2 = Store(path)
    assert st2.fts_migrated is True and st2.fts_rebuilt == 0
    assert any("lacks column(s) mentions" in r.message for r in caplog.records)
    assert [r[1] for r in st2.conn.execute("PRAGMA table_info(papers_fts)")][-1] == "mentions"
    assert "alias" in {r[1] for r in st2.conn.execute("PRAGMA table_info(mentions)")}
    assert st2.body_for("doi:10.1000/test73") == "legacy body text"  # bodies survive the rebuild
    assert [r["id"] for r in st2.search("Revio", sections=["mentions"])] == ["doi:10.1000/test73"]  # mentions text filled from the table
    pairs = st2.conn.execute("SELECT f.rowid, p.rowid FROM papers_fts f JOIN papers p ON p.id = f.id").fetchall()
    assert len(pairs) == 2 and all(a == b for a, b in pairs)
    st2.close()
    assert Store(path).fts_migrated is False  # only once


def test_search_fallback_is_visible_or_raises(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    pid = "doi:10.1000/test74"
    st.upsert_paper(_paper(74))
    st.commit()
    assert st.search("nosuchcol:Xenium") == []
    assert st.last_search_fallback and "nosuchcol" in st.last_search_fallback  # the FTS5 message is kept for the caller
    assert st.search("Xenium")[0]["id"] == pid and st.last_search_fallback is None  # reset by a clean search
    rows = st.search("Xenium spatial(")  # bad syntax, but the phrase fallback does find the paper
    assert [r["id"] for r in rows] == [pid] and st.last_search_fallback
    with pytest.raises(sqlite3.OperationalError):
        st.search("(Xenium OR", fallback=False)


def test_instrument_counts_split_platforms_from_lab_tools_in_sql(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    pids = []
    for i in range(3):
        p = _paper(80 + i, sections={})
        st.upsert_paper(p)
        pids.append(p.id)
    rows = []
    for pid in pids:  # three consumables in every paper ...
        rows += [
            Mention(pid, "instrument", "qiagen-kits", "QIAGEN kits", "QIAGEN", "", "library_prep", "methods", 1, ""),
            Mention(pid, "instrument", "nanodrop", "NanoDrop", "Thermo", "TMO", "qc", "methods", 1, ""),
            Mention(pid, "instrument", "mystery-tool", "Mystery tool", "", "", "", "methods", 1, ""),
            Mention(pid, "instrument", "fake-singlexcell", "Not a platform", "", "", "singleXcell", "methods", 1, ""),
        ]
    # ... and two platforms in one paper each
    rows.append(Mention(pids[0], "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "methods", 1, ""))
    rows.append(Mention(pids[1], "instrument", "ilmn-novaseq-x", "NovaSeq X", "Illumina", "ILMN", "sequencer.short_read", "methods", 1, ""))
    rows.append(Mention(pids[1], "instrument", "txg-chromium", "Chromium", "10x Genomics", "TXG", "single_cell.3prime", "methods", 1, ""))
    by_paper = {}
    for m in rows:
        by_paper.setdefault(m.paper_id, []).append(m)
    for pid, ms in by_paper.items():
        st.replace_mentions(pid, ms)
    st.commit()
    w = ("2026-10-10", "2026-10-17")
    shared = st.instrument_counts(*w, limit=4)
    assert not any(r["entity_id"] in ("txg-xenium", "ilmn-novaseq-x", "txg-chromium") for r in shared)  # the old shared budget drops every platform
    plat = st.instrument_counts(*w, limit=4, platform_only=True)
    assert {r["entity_id"] for r in plat} == {"txg-xenium", "ilmn-novaseq-x", "txg-chromium"}  # 'singleXcell' is not 'single_cell'
    assert [r["entity_id"] for r in st.instrument_counts(*w, limit=2, platform_only=False)] == ["qiagen-kits", "nanodrop"] or \
        len(st.instrument_counts(*w, limit=2, platform_only=False)) == 2
    assert st.distinct_entities_in_window(*w, "instrument", platform_only=False) == 4
    assert st.distinct_entities_in_window(*w, "instrument", platform_only=True) == 3
    assert st.distinct_entities_in_window(*w, "instrument") == 7
    assert [r["entity_id"] for r in st.instrument_counts(*w, categories=("spatial",))] == ["txg-xenium"]
    assert {r["entity_id"] for r in st.instrument_counts(*w, categories=("single_cell", "sequencer"))} == {"txg-chromium", "ilmn-novaseq-x"}


def test_entity_papers_distinct_mode_and_entity_names(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    a, b = _paper(90, pub_date="2026-10-12", sections={}), _paper(91, pub_date="2026-10-14", sections={})
    st.upsert_paper(a)
    st.upsert_paper(b)
    st.replace_mentions(a.id, [
        Mention(a.id, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "abstract snippet"),
        Mention(a.id, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "methods", 6, "methods snippet"),
        Mention(a.id, "company", "10x-genomics", "10x Genomics", "", "TXG", "", "affiliations", 1, ""),
    ])
    st.replace_mentions(b.id, [Mention(b.id, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "title", 1, "title snippet")])
    st.commit()
    assert len(st.entity_papers("txg-xenium")) == 3  # one row per (paper, section)
    rows = st.entity_papers("txg-xenium", distinct=True)
    assert [r["id"] for r in rows] == [b.id, a.id]  # one row per paper, newest first
    r = rows[1]
    assert r["sections"] == ["abstract", "methods"] and r["section"] == "abstract, methods" and r["mentions"] == 7
    assert r["snippet"] == "methods snippet"  # the section with most occurrences
    assert len(st.entity_papers("txg-xenium", distinct=True, limit=1)) == 1  # limit counts papers
    assert [r["id"] for r in st.entity_papers("txg-xenium", distinct=True, since="2026-10-13")] == [b.id]
    assert st.entity_names("instrument") == {"txg-xenium": "Xenium"}
    assert st.entity_names() == {"txg-xenium": "Xenium", "10x-genomics": "10x Genomics"}


def test_institutes_and_ai_tools_are_listed_but_not_scored():
    paper = {"kind": "article", "title": "A paper", "abstract": "About things.", "cited_by_count": 0, "fulltext_available": True}
    m = [
        {"kind": "company", "entity_id": "broad", "section": "affiliations"},
        {"kind": "company", "entity_id": "openai", "section": "ack"},
        {"kind": "company", "entity_id": "illumina", "section": "methods"},
    ]
    types = {"broad": "institute", "openai": "software"}
    with_types, reasons = heuristics.score_paper(paper, m, "A", [], False, company_types=types)
    without, reasons0 = heuristics.score_paper(paper, m, "A", [], False)
    assert with_types < without
    assert "1 company mention(s)" in reasons and "1 institute mention(s), not scored" in reasons and "1 AI tool(s), not scored" in reasons
    assert "company-affiliated author" not in reasons and "company-affiliated author" in reasons0 and "3 company mention(s)" in reasons0
    only_institute, _ = heuristics.score_paper(paper, m[:1], "A", [], False, company_types=types)
    nothing, _ = heuristics.score_paper(paper, [], "A", [], False)
    assert only_institute == nothing
    assert heuristics.company_role("broad", types) == "institute" and heuristics.company_role("openai", types) == "ai_tool"
    assert heuristics.company_role("x", None) == "company" and heuristics.company_role("broad", {"broad": "Institute"}) == "institute"


def test_http_host_accounting_and_400_body_warning(monkeypatch, caplog):
    from litradar.http import Http

    class R:
        def __init__(self, code, text=""):
            self.status_code, self.headers, self.content, self.url, self.text = code, {}, b"", "u", text

    http = Http(max_retries=0)
    monkeypatch.setattr("litradar.http.time.sleep", lambda s: None)
    monkeypatch.setattr(http.session, "get", lambda url, **k: R(400, '{"error": "Invalid query parameters", "message": "grants is not a valid select field"}') if "dead" in url else R(200))
    with caplog.at_level(logging.WARNING, logger="litradar.http"):
        http.get("https://dead.example/a")
        http.get("https://dead.example/b")
        http.get("https://ok.example/c")
    assert http.failures_by_host == {"dead.example": 2} and http.successes_by_host == {"ok.example": 1}
    rep = http.host_report()
    assert rep["failed_only_hosts"] == ["dead.example"] and rep["blocked_hosts"] == []
    warned = [r for r in caplog.records if "400 bad request" in r.message]
    assert len(warned) == 1 and "grants is not a valid select field" in warned[0].message  # once per host, with the body
    # a host that trips the 429 breaker shows up blocked, with its skipped requests counted
    monkeypatch.setattr(http.session, "get", lambda url, **k: R(429))
    http.get("https://busy.example/a")
    http.get("https://busy.example/b")
    http.get("https://busy.example/c")
    rep = http.host_report()
    assert rep["blocked_hosts"] == ["busy.example"] and rep["blocked_skips_by_host"] == {"busy.example": 1}
    assert rep["circuit_threshold"] == {"busy.example": 2} and "busy.example" not in rep["failed_only_hosts"]
    assert http.stats["failures"] == 4 and http.stats["blocked_skips"] == 1


# ---- mention roles and family roll-ups (hygiene pass of 2026-10-17) ----
from litradar.store import mention_role  # noqa: E402 - appended block


def test_mention_role_rules():
    assert mention_role("methods", "Libraries were sequenced on a NovaSeq X Plus.") == "used"
    assert mention_role("body", "run on the Chromium Controller (10x Genomics)") == "used"
    for cue in ("GSE196239 (10x Genomics Chromium v3, platform GPL24676)", "data were downloaded from GEO", "publicly available Xenium data",
                "GEO accession GSE12345", "re-analysed Visium sections", "Re-analysis of previously published data", "raw reads from the SRA (PRJNA893571)",
                "ArrayExpress E-MTAB-1234", "retrieved from the Sequence Read Archive"):
        assert mention_role("methods", cue) == "reanalysis", cue
    assert mention_role("abstract", "We re-analysed 12 Visium datasets") == "reanalysis"
    for sec in ("title", "abstract", "funding", "coi", "ack", "affiliations", "data_availability"):
        assert mention_role(sec, "Xenium spatial profiling") == "mentioned", sec
    # accession tokens are case-sensitive, phrases are not
    assert mention_role("methods", "the sra of the sample") == "used"
    assert mention_role("methods", "gse is a gene") == "used"
    assert mention_role("methods", "PUBLICLY AVAILABLE data") == "reanalysis"


def test_roles_are_stored_derived_or_given_and_counted(tmp_path):
    st = Store(tmp_path / "r.sqlite")
    a, b, c = _paper(90, sections={}), _paper(91, sections={}), _paper(92, sections={})
    for p in (a, b, c):
        st.upsert_paper(p)
    st.replace_mentions(a.id, [
        Mention(a.id, "instrument", "txg-chromium", "Chromium", "10x Genomics", "TXG", "single_cell", "methods", 2, "loaded on the Chromium Controller (10x Genomics)"),
        Mention(a.id, "instrument", "txg-chromium", "Chromium", "10x Genomics", "TXG", "single_cell", "abstract", 1, "Chromium single-cell profiling"),
    ])
    st.replace_mentions(b.id, [Mention(b.id, "instrument", "txg-chromium", "Chromium", "10x Genomics", "TXG", "single_cell", "methods", 1,
                                       "(2) GSE196239 (10x Genomics Chromium v3, platform GPL24676): human embryonic cortex")])
    st.replace_mentions(c.id, [Mention(c.id, "instrument", "txg-chromium", "Chromium", "10x Genomics", "TXG", "single_cell", "methods", 1, "", role="reanalysis")])
    st.commit()
    roles = {(m["section"], m["paper_id"]): m["role"] for m in st.mentions_for(a.id) + st.mentions_for(b.id) + st.mentions_for(c.id)}
    assert roles == {("methods", a.id): "used", ("abstract", a.id): "mentioned", ("methods", b.id): "reanalysis", ("methods", c.id): "reanalysis"}
    row = st.instrument_counts("2026-10-10", "2026-10-17")[0]
    assert (row["entity_id"], row["papers"], row["papers_used"], row["papers_reanalysis"], row["papers_mentioned"]) == ("txg-chromium", 3, 1, 2, 1)
    assert row["family"] == "" and row["members"] == ["txg-chromium"]
    # kept rows (text not stored) keep their role
    assert st.replace_mentions(b.id, [], keep_sections=("methods",)) == 1
    assert st.mentions_for(b.id)[0]["role"] == "reanalysis"
    # recomputing every row replaces the one caller-given role (paper c: blank snippet -> derived 'used'); then it is idempotent
    assert st.backfill_mention_roles(only_blank=False) == 1
    assert st.mentions_for(c.id)[0]["role"] == "used"
    assert st.backfill_mention_roles(only_blank=False) == 0


def test_legacy_store_without_role_column_is_backfilled_on_open(tmp_path, caplog):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA.replace("  role TEXT DEFAULT '',\n", ""))
    assert "role" not in {r[1] for r in conn.execute("PRAGMA table_info(mentions)")}
    conn.execute("INSERT INTO papers (id, title, pub_date, first_seen) VALUES ('doi:10.1000/legacy', 'Legacy', '2026-10-12', '2026-10-12T00:00:00Z')")
    conn.execute("INSERT INTO mentions (paper_id, kind, entity_id, entity_name, section, count, snippet) VALUES "
                 "('doi:10.1000/legacy', 'instrument', 'txg-visium', 'Visium', 'methods', 1, 'publicly available 10x Genomics Visium dataset GSE179572'), "
                 "('doi:10.1000/legacy', 'instrument', 'txg-xenium', 'Xenium', 'methods', 1, 'Xenium slides were processed'), "
                 "('doi:10.1000/legacy', 'instrument', 'txg-xenium', 'Xenium', 'title', 1, 'Xenium')")
    conn.commit()
    conn.close()
    with caplog.at_level("INFO", logger="litradar.store"):
        st = Store(path)
    assert st.roles_backfilled == 3
    assert "no 'role' column" in caplog.text
    assert {(m["entity_id"], m["section"], m["role"]) for m in st.mentions_for("doi:10.1000/legacy")} == {
        ("txg-visium", "methods", "reanalysis"), ("txg-xenium", "methods", "used"), ("txg-xenium", "title", "mentioned")}
    st.close()
    assert Store(path).roles_backfilled == 0  # second open: nothing to do


def test_instrument_counts_roll_sibling_models_up_by_family(tmp_path):
    st = Store(tmp_path / "f.sqlite")
    pids = []
    for i in range(4):
        p = _paper(60 + i, sections={})
        st.upsert_paper(p)
        pids.append(p.id)
    nx = lambda pid, n=1: Mention(pid, "instrument", "ilmn-novaseq-x", "NovaSeq X", "Illumina", "ILMN", "sequencer.short_read", "methods", n, "NovaSeq X run")  # noqa: E731
    nxp = lambda pid, n=1: Mention(pid, "instrument", "ilmn-novaseq-x-plus", "NovaSeq X Plus", "Illumina", "ILMN", "sequencer.short_read", "methods", n, "NovaSeq X Plus run")  # noqa: E731
    st.replace_mentions(pids[0], [nx(pids[0])])
    st.replace_mentions(pids[1], [nxp(pids[1], 2)])
    st.replace_mentions(pids[2], [nx(pids[2]), nxp(pids[2])])  # names both models: counts once for the family
    st.replace_mentions(pids[3], [Mention(pids[3], "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "methods", 1, "")])
    st.commit()
    w = ("2026-10-10", "2026-10-17")
    flat = {r["entity_id"]: r for r in st.instrument_counts(*w)}
    assert flat["ilmn-novaseq-x"]["papers"] == 2 and flat["ilmn-novaseq-x-plus"]["papers"] == 2 and flat["txg-xenium"]["papers"] == 1
    assert all(r["family"] == "" and r["members"] == [eid] for eid, r in flat.items())
    families = {"ilmn-novaseq-x": "novaseq-x", "ilmn-novaseq-x-plus": "novaseq-x", "pacb-sequel": "sequel"}
    rolled = st.instrument_counts(*w, families=families)
    assert [(r["entity_id"], r["entity_name"], r["papers"], r["mentions"]) for r in rolled] == [("novaseq-x", "NovaSeq X", 3, 5), ("txg-xenium", "Xenium", 1, 1)]
    fam = rolled[0]
    assert fam["family"] == "novaseq-x" and fam["members"] == ["ilmn-novaseq-x", "ilmn-novaseq-x-plus"] and fam["papers_used"] == 3
    assert fam["example_title"] and fam["vendor"] == "Illumina" and fam["ticker"] == "ILMN"
    assert rolled[1]["family"] == "" and rolled[1]["members"] == ["txg-xenium"]
    assert st.distinct_entities_in_window(*w, "instrument") == 3
    assert st.distinct_entities_in_window(*w, "instrument", families=families) == 2
    assert st.distinct_entities_in_window(*w, "instrument", families=families, platform_only=True) == 2
    assert [r["entity_id"] for r in st.instrument_counts(*w, families=families, categories=("sequencer",))] == ["novaseq-x"]
    # the real gazetteer's family map plugs straight in
    from litradar.config import DEFAULT_CONFIG_DIR
    from litradar.extract.gazetteer import Gazetteer
    gz = Gazetteer.from_yaml(yaml.safe_load(open(DEFAULT_CONFIG_DIR / "instruments.yaml")), "instrument")
    assert [r["entity_id"] for r in st.instrument_counts(*w, families=gz.families)] == ["novaseq-x", "txg-xenium"]


def test_preprint_links_need_a_real_preprint_record_posted_no_later_than_the_article(tmp_path):
    """Seen live: four 'conversions' in one digest, every preprint dated at or after its article; two were same-day
    Figshare publisher deposits with the article's title, one a plain-language summary. A title match now needs a record
    that demonstrably comes from a preprint server and a posting date no later than the article's; the publisher's own
    `published_doi` link is taken as it stands, and a preprint posted before the window is found from the article."""
    st = Store(tmp_path / "links.sqlite")
    art = _paper(1, id="doi:10.1080/17581966.2026.2743569", doi="10.1080/17581966.2026.2743569", kind="article", journal="Lung Cancer Management",
                 pub_date="2026-10-12", title="Potential to improve early detection of lung cancer through expansion of screening")
    deposit = _paper(2, id="doi:10.6084/m9.figshare.34408562", doi="10.6084/m9.figshare.34408562", kind="preprint", source="openalex", journal="Figshare",
                     pub_date="2026-10-13", title=art.title)
    art2 = _paper(3, id="doi:10.1002/prot.70189", doi="10.1002/prot.70189", kind="article", journal="Proteins", pub_date="2026-10-12",
                  title="AlphaFold3 and intrinsically disordered proteins: reliable monomer predictions")
    late = _paper(4, id="doi:10.1101/2026.10.14.100001", doi="10.1101/2026.10.14.100001", kind="preprint", journal="biorxiv", pub_date="2026-10-14", title=art2.title)
    art3 = _paper(5, id="doi:10.1038/s41592-026-0100-1", doi="10.1038/s41592-026-0100-1", kind="article", journal="Nature Methods", pub_date="2026-10-15",
                  title="A spatial method paper whose preprint was posted months earlier")
    early = _paper(6, id="doi:10.1101/2026.06.01.200002", doi="10.1101/2026.06.01.200002", kind="preprint", journal="biorxiv", pub_date="2026-06-01", title=art3.title)
    art4 = _paper(7, id="doi:10.1016/j.cell.2026.10.001", doi="10.1016/j.cell.2026.10.001", kind="article", journal="Cell", pub_date="2026-10-16",
                  title="A completely different title chosen by the journal for this paper")
    linked = _paper(8, id="doi:10.64898/2025.12.05.691730", doi="10.64898/2025.12.05.691730", kind="preprint", journal="biorxiv", pub_date="2026-10-13",
                    published_doi="10.1016/j.cell.2026.10.001", title="Preprint title that the journal rewrote")
    for p in (art, deposit, art2, late, art3, early, art4, linked):
        st.upsert_paper(p)
    st.commit()
    assert Store.is_preprint_record(dict(kind="preprint", source="openalex", journal="Figshare", doi="10.6084/m9.figshare.1")) is False
    assert Store.is_preprint_record(dict(kind="preprint", source="openalex", journal="bioRxiv (Cold Spring Harbor Laboratory)", doi="10.1101/2026.01.01.000001")) is True
    assert Store.is_preprint_record(dict(kind="preprint", source="openalex", journal="", doi="10.21203/rs.3.rs-1")) is True
    assert Store.is_preprint_record(dict(kind="preprint", source="europepmc", journal="Research Square", doi=None)) is True
    assert Store.is_preprint_record(dict(kind="preprint", source="openalex", journal="Zenodo", doi="10.5281/zenodo.1", published_doi="10.1/x")) is True
    assert Store.is_preprint_record(dict(kind="article", source="biorxiv", journal="biorxiv", doi="10.1101/2026.01.01.000001")) is False
    assert Store.preprint_date({"doi": "10.64898/2025.12.05.691730", "pub_date": "2026-10-13"}) == "2025-12-05"
    assert Store.preprint_date({"doi": "10.1101/2026.10.14.100001", "pub_date": "2026-10-01"}) == "2026-10-01"  # stored date already earlier
    window = ("2026-10-10", "2026-10-17")
    conv = {c["preprint_id"]: c for c in st.preprint_conversions(*window)}
    assert set(conv) == {early.id, linked.id}
    assert conv[early.id]["matched_by"] == "title" and conv[early.id]["article_id"] == art3.id and conv[early.id]["lag_days"] == 136
    assert conv[early.id]["preprint_server"] == "biorxiv" and conv[early.id]["preprint_date"] == "2026-06-01"
    assert conv[linked.id]["matched_by"] == "published_doi" and conv[linked.id]["preprint_date"] == "2025-12-05" and conv[linked.id]["lag_days"] == 315
    assert [c["article_id"] for c in st.preprint_conversions(*window)] == [art4.id, art3.id]  # newest article first
    # the deposit and the later-dated preprint stay separate rows in the window's list
    rows, _ = Store.collapse_preprints(st.papers_in_window(*window))
    ids = {r["id"] for r in rows}
    assert {deposit.id, late.id, art.id, art2.id} <= ids and "preprints" not in next(r for r in rows if r["id"] == art.id)
    assert next(r for r in rows if r["id"] == art4.id)["preprints"][0]["pub_date"] == "2025-12-05"
    st.close()
