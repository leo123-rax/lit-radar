"""Offline tests for litradar.export (CSV path, optional-dependency fallbacks, DuckDB when present)
and a smoke test of scripts/bench_store.py at a tiny N."""
import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from litradar import cli, export
from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.store import Store

ROOT = Path(__file__).resolve().parent.parent
CJK_TITLE = "空间转录组学方法在肿瘤研究中的应用"


def _paper(i: int, **kw) -> Paper:
    base = dict(
        id=f"doi:10.1000/exp{i}", source="biorxiv", kind="preprint", title=f"Paper {i} on spatial profiling", doi=f"10.1000/exp{i}",
        abstract="We present a method.", journal="biorxiv", pub_date=f"2026-10-1{i}", url=f"https://example.org/{i}",
        authors=[Author(name="A Author", affiliation="Institute")], affiliations=["Institute, City"],
        sections={"methods": "Sequenced on an instrument.", "body": "body text, lives only in the FTS table"}, fulltext_available=True,
    )
    base.update(kw)
    return Paper(**base)


@pytest.fixture
def seeded_db(tmp_path) -> Path:
    path = tmp_path / "seed.sqlite"
    st = Store(path)
    for i in range(5):
        p = _paper(i)
        if i == 2:
            p.title = CJK_TITLE
            p.affiliations = ["某大学 生命科学学院, 北京"]
        st.upsert_paper(p)
        st.save_score(p.id, 3.0 + i, {"novelty": i} if i % 2 else None, 3.5 + i, "reasons, with a comma")
    st.replace_mentions("doi:10.1000/exp0", [
        Mention("doi:10.1000/exp0", "instrument", "inst-a", "Instrument A", "Vendor", "TK", "spatial", "methods", 2, 'snippet with "quotes"\nand a newline'),
        Mention("doi:10.1000/exp0", "company", "co-a", "Company A", "", "TK", "", "affiliations", 1, "s"),
    ])
    st.upsert_archive_records([
        ArchiveRecord("sra", f"SRX{i}", "SRP1", "Model X", "PLAT", "WGS", "2026-10-12", "c", "t", "Homo sapiens", "inst-a", "Vendor", "TK") for i in range(3)
    ])
    st.upsert_news([NewsItem(id="n1", feed="f", title="News", url="https://example.org/n", published="2026-10-12", summary="s", companies=["co-a"])])
    st.add_tracked_author_hit("doi:10.1000/exp0", "A Author", "spatial", "name")
    st.record_run("2026-10-17T00:00:00Z", "2026-10-10", "2026-10-17", {"papers": 5})
    st.close()
    return path


def _read_csv(path: Path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------- CSV path (always available)
def test_csv_export_roundtrip(seeded_db, tmp_path):
    out = tmp_path / "csv"
    rep = export.export_tables(seeded_db, out, fmt="csv")
    assert rep.format_used == "csv" and rep.message == "" and not rep.skipped
    by_table = {t.table: t for t in rep.tables}
    assert set(by_table) == set(export.DEFAULT_TABLES)
    assert by_table["papers"].rows == 5 and by_table["mentions"].rows == 2 and by_table["scores"].rows == 5
    assert by_table["archive_records"].rows == 3 and by_table["news_items"].rows == 1
    papers = _read_csv(out / "papers.csv")
    assert len(papers) == 5
    titles = {r["id"]: r["title"] for r in papers}
    assert titles["doi:10.1000/exp2"] == CJK_TITLE  # UTF-8 survives
    assert json.loads(papers[0]["authors_json"])[0]["name"] == "A Author"  # JSON columns are opaque text
    mentions = _read_csv(out / "mentions.csv")
    assert any('"quotes"' in r["snippet"] and "\n" in r["snippet"] for r in mentions)  # quoting handles embedded quotes/newlines
    scores = _read_csv(out / "scores.csv")
    assert {r["llm_json"] for r in scores} >= {""}  # NULL -> empty field
    d = rep.to_dict()
    assert d["total_rows"] == 16 and d["total_bytes"] == sum(t.bytes for t in rep.tables) and d["seconds"] >= 0


def test_csv_export_optional_tables_and_fts_body(seeded_db, tmp_path):
    out = tmp_path / "csv"
    rep = export.export_tables(seeded_db, out, fmt="csv", tables=["tracked_author_hits", "runs", "papers_fts"])
    by_table = {t.table: t for t in rep.tables}
    assert by_table["tracked_author_hits"].rows == 1 and by_table["runs"].rows == 1 and by_table["papers_fts"].rows == 5
    fts = _read_csv(out / "papers_fts.csv")
    assert fts[0]["body"].startswith("body text")  # the body only exists in the FTS table and is exportable


def test_csv_gzip_and_small_chunks(seeded_db, tmp_path):
    import gzip

    rep = export.export_tables(seeded_db, tmp_path / "gz", fmt="csv", tables=["papers"], chunk_size=2, gzip_csv=True)
    assert rep.tables[0].format == "csv.gz" and rep.tables[0].rows == 5  # chunking (2,2,1) does not lose rows
    with gzip.open(tmp_path / "gz" / "papers.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 5


def test_export_empty_database(tmp_path):
    db = tmp_path / "empty.sqlite"
    Store(db).close()
    rep = export.export_tables(db, tmp_path / "out", fmt="csv")
    assert [t.rows for t in rep.tables] == [0] * len(export.DEFAULT_TABLES)
    for t in rep.tables:
        rows = Path(t.path).read_text(encoding="utf-8").splitlines()
        assert len(rows) == 1 and rows[0].split(",")[0] in ("id", "paper_id", "archive")  # header only


def test_export_malformed_inputs(tmp_path):
    # older / foreign schema: only one known table present -> others skipped, nothing raised
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT, cited_by_count INTEGER)")
    conn.execute("INSERT INTO papers VALUES ('a', 'T', 'not-a-number')")  # dynamic typing: text in an INTEGER column
    conn.execute("INSERT INTO papers VALUES ('b', X'00ff', 3)")  # blob in a TEXT column
    conn.commit()
    conn.close()
    rep = export.export_tables(db, tmp_path / "out", fmt="csv")
    assert [t.table for t in rep.tables] == ["papers"]
    assert set(rep.skipped) == {"mentions", "scores", "archive_records", "news_items"}
    rows = _read_csv(tmp_path / "out" / "papers.csv")
    assert rows[0]["cited_by_count"] == "not-a-number" and rows[1]["title"] == "00ff"
    with pytest.raises(ValueError):
        export.export_tables(db, tmp_path / "out", fmt="csv", tables=["papers", "nope"])
    with pytest.raises(FileNotFoundError):
        export.export_tables(tmp_path / "missing.sqlite", tmp_path / "out", fmt="csv")
    with pytest.raises(ValueError):
        export.export_tables(db, tmp_path / "out", fmt="xlsx")


def test_export_opens_read_only(seeded_db, tmp_path):
    conn = export.open_readonly(seeded_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM papers")
    conn.close()


def test_export_bare_copy_of_wal_database(seeded_db, tmp_path):
    # a restored snapshot or `cp` of the main file has no -wal/-shm side files; mode=ro alone cannot open it
    import shutil

    copy = tmp_path / "copy" / "snapshot.sqlite"
    copy.parent.mkdir()
    shutil.copy(seeded_db, copy)
    assert not (tmp_path / "copy" / "snapshot.sqlite-wal").exists()
    conn = export.open_readonly(copy)
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 5
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM papers")
    conn.close()
    rep = export.export_tables(copy, tmp_path / "copy-out", fmt="csv", tables=["papers"])
    assert rep.tables[0].rows == 5


# ---------------------------------------------------------------------------- optional dependencies
def test_parquet_request_falls_back_to_csv_without_pyarrow(seeded_db, tmp_path, monkeypatch):
    monkeypatch.setattr(export, "_import_pyarrow", lambda: None)
    rep = export.export_tables(seeded_db, tmp_path / "pq", fmt="parquet", tables=["papers"])
    assert rep.format_requested == "parquet" and rep.format_used == "csv"
    assert "pyarrow" in rep.message and rep.tables[0].path.endswith("papers.csv")
    rep = export.export_tables(seeded_db, tmp_path / "auto", fmt="auto", tables=["papers"])
    assert rep.format_used == "csv" and "pyarrow" in rep.message


def test_parquet_roundtrip_when_pyarrow_present(seeded_db, tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    rep = export.export_tables(seeded_db, tmp_path / "pq", fmt="parquet", chunk_size=2)
    assert rep.format_used == "parquet" and all(t.format == "parquet" for t in rep.tables)
    t = pq.read_table(tmp_path / "pq" / "papers.parquet")
    assert t.num_rows == 5 and t.schema.field("cited_by_count").type == pa.int64() and t.schema.field("title").type == pa.string()
    assert CJK_TITLE in t.column("title").to_pylist()
    s = pq.read_table(tmp_path / "pq" / "scores.parquet")
    assert s.schema.field("final").type == pa.float64() and None in s.column("llm_json").to_pylist()


def test_duckdb_missing_is_reported_not_raised_at_import(seeded_db, tmp_path, monkeypatch):
    monkeypatch.setattr(export, "_import_duckdb", lambda: None)
    with pytest.raises(export.OptionalDependencyMissing):
        export.export_duckdb(seeded_db, tmp_path / "x.duckdb")
    rc = cli.main(["export", "--duckdb", str(tmp_path / "x.duckdb"), "--db", str(seeded_db)])
    assert rc == 1  # requested output could not be produced -> non-zero, with a message on stderr


def test_duckdb_roundtrip_when_duckdb_present(seeded_db, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    rep = export.export_duckdb(seeded_db, tmp_path / "radar.duckdb", tables=list(export.DEFAULT_TABLES) + ["papers_fts"])
    assert {t.table: t.rows for t in rep.tables} == {"papers": 5, "mentions": 2, "scores": 5, "archive_records": 3, "news_items": 1, "papers_fts": 5}
    con = duckdb.connect(str(tmp_path / "radar.duckdb"), read_only=True)
    assert con.execute("SELECT COUNT(*) FROM papers WHERE title = ?", [CJK_TITLE]).fetchone()[0] == 1
    assert con.execute("SELECT body FROM papers_fts WHERE id='doi:10.1000/exp0'").fetchone()[0].startswith("body text")
    con.close()
    # re-export replaces the tables instead of failing
    rep2 = export.export_duckdb(seeded_db, tmp_path / "radar.duckdb", tables=["papers"])
    assert rep2.tables[0].rows == 5


# ---------------------------------------------------------------------------- CLI
def test_cli_export_csv(seeded_db, tmp_path, capsys):
    out = tmp_path / "cli-out"
    rc = cli.main(["export", "--csv", str(out), "--db", str(seeded_db), "--tables", "papers,mentions"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep[0]["format_used"] == "csv" and {t["table"] for t in rep[0]["tables"]} == {"papers", "mentions"}
    assert (out / "papers.csv").exists() and (out / "mentions.csv").exists()
    assert cli.main(["export", "--db", str(seeded_db)]) == 2  # nothing requested
    assert cli.main(["export", "--csv", str(out), "--db", str(tmp_path / "nope.sqlite")]) == 1


# ---------------------------------------------------------------------------- benchmark script smoke test
def test_bench_script_small_n(tmp_path):
    out = tmp_path / "bench.json"
    cmd = [sys.executable, str(ROOT / "scripts" / "bench_store.py"), "--n", "80", "--archive-n", "40", "--news-n", "5",
           "--repeat", "1", "--update-sample", "10", "--json", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["load"]["papers"] == 80 and rep["counts"]["papers"] == 80 and rep["counts"]["archive_records"] == 40
    assert rep["reupsert"]["papers"] == 10 and rep["counts"]["papers"] == 80  # updates do not add rows
    for phase in ("queries_before", "queries_after_indexes", "queries_after_analyze"):
        assert "instrument_counts_7d" in rep[phase] and rep[phase]["search_rare_word"]["rows"] == 0
    assert rep["fts_lookup"]["plan_by_id"] and "SCAN" in rep["fts_lookup"]["plan_by_id"][0]
    assert not Path(rep["db"]).exists()  # temp database removed
