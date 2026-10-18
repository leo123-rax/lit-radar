"""Digest sections that must agree with the store and with each other: the sequencing census labels, the source-credit
footer, and the single preprint-to-journal conversions list."""
from litradar.digest.render import SEQUENCING_ARCHIVE_LABELS, SEQUENCING_ARCHIVES, SOURCE_CREDITS, render
from litradar.models import ArchiveRecord, Paper
from litradar.store import Store

WINDOW = ("2026-10-10", "2026-10-17")


def _paper(pid: str, **kw) -> Paper:
    base = dict(id=pid, source="europepmc", kind="article", title=f"Paper {pid}", doi=pid[4:] if pid.startswith("doi:") else None,
                abstract="An abstract about spatial profiling.", journal="Nature", pub_date="2026-10-12", url=f"https://e/{pid}")
    base.update(kw)
    return Paper(**base)


def test_gsa_human_rows_use_the_adapters_archive_id_in_the_census(tmp_path):
    """The GSA-Human adapter stores archive='gsa-human'; the renderer spelt it 'gsa_human', so those experiments could
    never reach the sequencing census table or its heading."""
    assert "gsa-human" in SEQUENCING_ARCHIVES and SEQUENCING_ARCHIVE_LABELS["gsa-human"] == "GSA-Human" and "gsa_human" not in SEQUENCING_ARCHIVES
    st = Store(tmp_path / "gsa.sqlite")
    st.upsert_archive_records([
        ArchiveRecord("gsa-human", "HRX1", "HRA1", "DNBSEQ-T7", "BGISEQ", "WGS", "2026-10-13", "Example Hospital", "WGS cohort", "Homo sapiens", "mgi-dnbseq-t7", "MGI Tech", "688114.SS"),
        ArchiveRecord("gsa-human", "HRX2", "HRA1", "DNBSEQ-T7", "BGISEQ", "WGS", "2026-10-13", "Example Hospital", "WGS cohort", "Homo sapiens", "mgi-dnbseq-t7", "MGI Tech", "688114.SS"),
        ArchiveRecord("sra", "SRX1", "SRP1", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", "2026-10-12", "", "", "", "ilmn-novaseq-x", "Illumina", "ILMN"),
    ])
    st.commit()
    out = render(st, *WINDOW, {})
    md = out["markdown"]
    assert "## Instruments behind newly public sequencing data (NCBI SRA, GSA-Human)\n" in md
    census = md[md.index("## Instruments behind"): md.index("### Proteomics")]
    assert "| DNBSEQ-T7 | MGI Tech | 688114.SS | 2 | 1 | 100% HRA1 | gsa-human |" in census  # one study = the whole row: named
    assert "3 new sequencing experiments in public archives" in md
    assert {r["archive"] for r in out["summary"]["archive"]} == {"gsa-human", "sra"}
    st.close()


def test_source_credits_come_from_the_rendered_papers_with_the_required_wording_and_links(tmp_path):
    """The footer used to key off the last run's `fetched.*` counters (so `litradar digest` after a `--sources` run
    dropped it while KCI papers still rendered) and printed a label without the wording or link the terms require."""
    assert SOURCE_CREDITS["jstage"] == ("Powered by J-STAGE", "https://www.jstage.jst.go.jp/")
    assert SOURCE_CREDITS["kci"][1] == "https://www.kci.go.kr/" and SOURCE_CREDITS["kci"][0].startswith("Source: KCI (Korea Citation Index)")
    st = Store(tmp_path / "credits.sqlite")
    st.upsert_paper(_paper("kci:ART001", source="kci", journal="Korean Journal of Genomics", title="A Korean genomics paper"))
    st.upsert_paper(_paper("jstage:ART002", source="jstage", journal="Genes & Genetic Systems", title="A Japanese genomics paper"))
    st.upsert_paper(_paper("doi:10.1038/plain", title="A plain paper"))
    # the last run did not pull KCI or J-STAGE at all (a `--sources biorxiv` run): the credit must still appear
    st.record_run("2026-10-17T00:00:00Z", *WINDOW, {"counts": {"fetched.biorxiv": 5}})
    st.commit()
    out = render(st, *WINDOW, {})
    md = out["markdown"]
    cov = md[md.index("## Coverage and method"):]
    line = next(ln for ln in cov.splitlines() if ln.startswith("Source credits: "))
    assert "[Powered by J-STAGE](https://www.jstage.jst.go.jp/) (1 record in this window)" in line
    assert "[Source: KCI (Korea Citation Index), National Research Foundation of Korea](https://www.kci.go.kr/) (1 record in this window)" in line
    assert line.index("Powered by J-STAGE") < line.index("Source: KCI")
    assert "Their terms ask for this credit wherever their records are shown (see docs/SOURCES.md)." in line
    assert cov.index("Source credits: ") < cov.index("**Last pipeline run**")  # not inside the run block
    assert out["summary"]["source_credits"] == [
        {"source": "jstage", "text": "Powered by J-STAGE", "url": "https://www.jstage.jst.go.jp/", "papers": 1},
        {"source": "kci", "text": "Source: KCI (Korea Citation Index), National Research Foundation of Korea", "url": "https://www.kci.go.kr/", "papers": 1},
    ]
    st.close()
    # no KCI / J-STAGE paper in the window: no credit, whatever the run's counters say
    st2 = Store(tmp_path / "nocredit.sqlite")
    st2.upsert_paper(_paper("doi:10.1038/plain2", title="A plain paper"))
    st2.record_run("2026-10-17T00:00:00Z", *WINDOW, {"counts": {"fetched.kci": 100, "fetched.jstage": 20}})
    st2.commit()
    out2 = render(st2, *WINDOW, {})
    assert "Source credits" not in out2["markdown"] and out2["summary"]["source_credits"] == []
    st2.close()


def test_digest_prints_one_conversions_answer(tmp_path):
    """One digest printed four 'conversions' under the top list (all dated impossibly, none a preprint) and 'none
    detected' under What changed. The top-list section is now the single answer, fed by `Store.preprint_conversions`,
    and the trends section points to it."""
    st = Store(tmp_path / "conv.sqlite")
    art = _paper("doi:10.1038/s41592-026-0100-1", journal="Nature Methods", pub_date="2026-10-15", title="A spatial method paper whose preprint was posted months earlier")
    early = _paper("doi:10.1101/2026.06.01.200002", kind="preprint", source="biorxiv", journal="biorxiv", pub_date="2026-06-01", title=art.title)
    art2 = _paper("doi:10.1016/j.cell.2026.10.001", journal="Cell", pub_date="2026-10-16", title="A completely different title chosen by the journal")
    linked = _paper("doi:10.64898/2025.12.05.691730", kind="preprint", source="biorxiv", journal="biorxiv", pub_date="2026-10-13",
                    published_doi="10.1016/j.cell.2026.10.001", title="Preprint title that the journal rewrote")
    art3 = _paper("doi:10.1080/17581966.2026.2743569", journal="Lung Cancer Management", pub_date="2026-10-12", title="Potential to improve early detection of lung cancer through expansion of screening")
    deposit = _paper("doi:10.6084/m9.figshare.34408562", kind="preprint", source="openalex", journal="Figshare", pub_date="2026-10-13", title=art3.title)
    # a second preprint record of the same paper (an OSF mirror OpenAlex lists with a blank venue): one line per article
    mirror = _paper("doi:10.31219/osf.io/abcde", kind="preprint", source="openalex", journal="", pub_date="2026-07-15", title=art.title)
    for p in (art, early, art2, linked, art3, deposit, mirror):
        st.upsert_paper(p)
    for pid in (art.id, art2.id, art3.id, deposit.id, linked.id):
        st.save_score(pid, 4.0, None, 4.0, "r")
    st.commit()
    out = render(st, *WINDOW, {})
    md = out["markdown"]
    top = md[md.index("### Preprints that appeared as journal articles this window"): md.index("## Platforms named")]
    assert top.count("\n- ") == 2
    assert "preprint 2026-06-01 on biorxiv, 136 days earlier (matched by title) (+1 further preprint record)" in top
    assert "preprint 2025-12-05 on biorxiv, 315 days earlier (linked by the publisher)" in top
    assert "Figshare" not in top and "Lung Cancer Management" not in top and "on openalex" not in top
    changed = md[md.index("## What changed"):]
    conv_section = changed[changed.index("### Preprints that became journal articles"):]
    assert "_3 preprint → journal conversions in this digest's window, listed under the top innovations above" in conv_section
    assert "no preprint → journal conversions detected this week" not in md and "| Preprint |" not in changed
    convs = out["summary"]["preprint_conversions"]
    assert [c["preprint_id"] for c in convs] == [linked.id, early.id, mirror.id]
    assert next(c for c in convs if c["preprint_id"] == mirror.id)["preprint_server"] == "OSF Preprints"  # from the DOI, the venue being blank
    # the journal version carries its preprint's first-posting date, not the revision date
    assert "(also as preprint 2025-12-05)" in md
    st.close()
    # nothing to pair: both places say so, in the same words
    st2 = Store(tmp_path / "none.sqlite")
    st2.upsert_paper(_paper("doi:10.1038/lone", title="A lone article"))
    st2.commit()
    md2 = render(st2, *WINDOW, {})["markdown"]
    assert "_no preprint → journal conversions detected in this window_" in md2
    assert "_no preprint → journal conversions detected in this digest's window (see the top innovations section)._" in md2
    st2.close()
