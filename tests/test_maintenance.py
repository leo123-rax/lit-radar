"""`litradar maintenance`: placeholder purge, FTS rebuild, VACUUM and per-table stats on a temporary store."""
import json
import os

from litradar import cli
from litradar.commands import maintenance
from litradar.models import ArchiveRecord, Mention, Paper
from litradar.store import SRA_PLACEHOLDER_PREFIX, Store


def _seed(path):
    st = Store(path)
    for i in range(3):
        st.upsert_paper(Paper(id=f"doi:10.1000/m{i}", source="biorxiv", title=f"Paper {i} about Xenium", abstract="Spatial maps.",
                              pub_date="2026-10-1" + str(i), sections={"methods": "Run on a Xenium Analyzer (10x Genomics)."}))
        st.replace_mentions(f"doi:10.1000/m{i}", [Mention(f"doi:10.1000/m{i}", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "methods", 1, "Xenium Analyzer")])
    recs = [ArchiveRecord("sra", f"SRX{i}", "SRP1", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", "2026-10-12", "c", "t", "Homo sapiens", "ilmn-novaseq-x-plus", "Illumina", "ILMN") for i in range(4)]
    recs += [ArchiveRecord("sra", f"{SRA_PLACEHOLDER_PREFIX}{47431130 + i}", "", "Illumina MiSeq", "", "", "2026-10-16", "", "", "", "ilmn-miseq", "Illumina", "ILMN") for i in range(5)]
    recs += [ArchiveRecord("ena", "ERX1", "ERP1", "Illumina MiSeq", "ILLUMINA", "AMPLICON", "2026-10-01", "c", "t", "soil", "ilmn-miseq", "Illumina", "ILMN")]
    st.upsert_archive_records(recs)
    st.commit()
    return st


def test_purge_counts_and_deletes_only_placeholders(tmp_path):
    st = _seed(tmp_path / "m.sqlite")
    assert st.count_sra_placeholders() == 5 and st.counts()["archive_records"] == 10
    rep = maintenance.run_maintenance(st, purge_sra_placeholders=True)
    assert rep["actions"] == ["purge_sra_placeholders"]
    assert rep["purge_sra_placeholders"] == {"found": 5, "deleted": 5, "remaining": 0, "archive_records": 5}
    assert {r[0] for r in st.conn.execute("SELECT accession FROM archive_records")} == {"SRX0", "SRX1", "SRX2", "SRX3", "ERX1"}
    assert st.purge_sra_placeholders() == 0  # idempotent
    text = maintenance.format_report(rep)
    assert "5 found, 5 deleted, 0 remaining (5 archive records left)" in text


def test_fts_rebuild_keeps_search_working_and_counts_rows(tmp_path):
    st = _seed(tmp_path / "m.sqlite")
    assert [r["id"] for r in st.search("Xenium")] and len(st.search("Xenium")) == 3
    rep = maintenance.run_maintenance(st, fts_rebuild=True)
    assert rep["fts_rebuild"] == {"rows_written": 3, "papers": 3}
    assert len(st.search("Xenium")) == 3 and st.search("Xenium", sections=["mentions"])  # bodies and the mentions column survive
    assert st.conn.execute("SELECT COUNT(*) FROM papers_fts f JOIN papers p ON p.rowid = f.rowid WHERE p.id = f.id").fetchone()[0] == 3


def test_vacuum_reports_sizes_and_shrinks_after_deletes(tmp_path):
    path = tmp_path / "m.sqlite"
    st = _seed(path)
    # make something to reclaim: a large body, then drop it
    big = Paper(id="doi:10.1000/big", source="biorxiv", title="Big", pub_date="2026-10-13", sections={"methods": "m", "body": "x" * 2_000_000})
    st.upsert_paper(big)
    st.commit()
    st.conn.execute("DELETE FROM papers_fts WHERE id = 'doi:10.1000/big'")
    st.conn.execute("DELETE FROM papers WHERE id = 'doi:10.1000/big'")
    st.commit()
    rep = maintenance.run_maintenance(st, vacuum=True, stats_by_table=True)
    v = rep["vacuum"]
    assert v["bytes_before"] > v["bytes_after"] > 0 and v["freelist_before"] > 0
    assert rep["stats"]["freelist_count"] == 0 and rep["stats"]["file_bytes"] == v["bytes_after"]
    assert "VACUUM:" in maintenance.format_report(rep) and "bytes returned" in maintenance.format_report(rep)


def test_table_stats_lists_tables_indexes_rows_and_bytes(tmp_path):
    st = _seed(tmp_path / "m.sqlite")
    stats = st.table_stats()
    by_name = {o["name"]: o for o in stats["objects"]}
    assert by_name["papers"]["rows"] == 3 and by_name["papers"]["type"] == "table"
    assert by_name["archive_records"]["rows"] == 10 and by_name["mentions"]["rows"] == 3
    assert by_name["idx_archive_model"]["type"] == "index" and by_name["idx_archive_model"]["rows"] is None
    assert "papers_fts_data" in by_name  # FTS shadow tables are listed
    assert stats["page_size"] > 0 and stats["page_count"] > 0 and stats["file_bytes"] >= stats["page_size"] * stats["page_count"] - stats["page_size"]
    assert stats["sra_placeholders"] == 5
    if stats["bytes_available"]:
        assert by_name["papers"]["bytes"] >= stats["page_size"]
        assert sum(o["bytes"] or 0 for o in stats["objects"]) <= stats["page_size"] * stats["page_count"]
    text = maintenance.format_report({"actions": ["stats_by_table"], "stats": stats})
    assert "ATTENTION: 5 SRA placeholder rows" in text and "papers " in text and "archive_records" in text


def test_cli_runs_actions_in_order_and_defaults_to_stats(tmp_path, monkeypatch, capsys):
    path = tmp_path / "m.sqlite"
    _seed(path).close()
    monkeypatch.setenv("LITRADAR_DB", str(path))
    assert cli.main(["maintenance", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["actions"] == ["stats_by_table"] and rep["stats"]["sra_placeholders"] == 5
    assert cli.main(["maintenance", "--purge-sra-placeholders", "--fts-rebuild", "--vacuum", "--stats-by-table", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["actions"] == ["purge_sra_placeholders", "fts_rebuild", "vacuum", "stats_by_table"]
    assert rep["purge_sra_placeholders"]["deleted"] == 5 and rep["fts_rebuild"]["rows_written"] == 3
    assert rep["stats"]["sra_placeholders"] == 0 and rep["stats"]["freelist_count"] == 0
    assert cli.main(["maintenance"]) == 0
    out = capsys.readouterr().out
    assert "file:" in out and "ATTENTION" not in out  # placeholders are gone
    assert os.path.getsize(path) == rep["vacuum"]["bytes_after"] or os.path.getsize(path) > 0
    assert "maintenance" in __import__("subprocess").run([__import__("sys").executable, "-m", "litradar", "--help"], capture_output=True, text=True, check=False).stdout


def test_refresh_entity_names_rewrites_stale_rows_from_the_gazetteer(tmp_path, monkeypatch, capsys):
    from litradar.extract.gazetteer import Entry

    path = tmp_path / "m.sqlite"
    st = _seed(path)
    pid = "doi:10.1000/m0"
    st.replace_mentions(pid, [
        Mention(pid, "instrument", "ilmn-sequencing", "Illumina sequencing", "Illumina", "ILMN", "sequencer.short_read", "methods", 1, "Illumina platform"),
        Mention(pid, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "methods", 1, "Xenium Analyzer"),
        Mention(pid, "company", "hologic", "Hologic", "", "HOLX", "author-email", "affiliations", 1, "author e-mail domain"),
    ])
    st.commit()
    entries = [
        Entry(id="ilmn-sequencing", name="Illumina (model unspecified)", kind="instrument", aliases=[], vendor="Illumina", ticker="ILMN", category="sequencer.short_read"),
        Entry(id="txg-xenium", name="Xenium", kind="instrument", aliases=[], vendor="10x Genomics", ticker="TXG", category="spatial"),  # unchanged
        Entry(id="hologic", name="Hologic", kind="company", aliases=[], ticker="private"),
        Entry(id="never-stored", name="Nothing", kind="instrument", aliases=[]),
    ]
    assert st.refresh_entity_metadata(entries) == 2
    rows = {(m["kind"], m["entity_id"]): m for m in st.mentions_for(pid)}
    assert rows[("instrument", "ilmn-sequencing")]["entity_name"] == "Illumina (model unspecified)"
    assert rows[("company", "hologic")]["ticker"] == "private" and rows[("company", "hologic")]["category"] == "author-email"  # provenance kept
    assert st.search("unspecified", sections=["mentions"])[0]["id"] == pid  # the FTS mentions column follows the name
    assert st.refresh_entity_metadata(entries) == 0  # idempotent
    st.close()
    # the CLI reads the configured gazetteers; the real ilmn-sequencing entry carries the new display name
    monkeypatch.setenv("LITRADAR_DB", str(path))
    st = Store(path)
    st.conn.execute("UPDATE mentions SET entity_name='old name' WHERE entity_id='ilmn-sequencing'")
    st.commit()
    st.close()
    assert cli.main(["maintenance", "--refresh-entity-names", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["actions"] == ["refresh_entity_names"] and rep["refresh_entity_names"]["rows_updated"] >= 1 and rep["refresh_entity_names"]["entries"] > 400
    st = Store(path)
    assert {m["entity_name"] for m in st.mentions_for(pid) if m["entity_id"] == "ilmn-sequencing"} == {"Illumina (model unspecified)"}
    st.close()
    assert cli.main(["maintenance", "--refresh-entity-names"]) == 0
    assert "mention row(s) updated" in capsys.readouterr().out
