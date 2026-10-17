import json

from litradar.models import ArchiveRecord, Author, Mention, Paper
from litradar.rank import heuristics
from litradar.store import Store


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
