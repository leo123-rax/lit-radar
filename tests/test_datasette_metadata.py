"""datasette/metadata.yaml and datasette/README.md: every canned query runs against a seeded store with blank
parameters (no bound) and with values, the README's FTS examples parse, and the table list is complete."""
import re
import sqlite3
from pathlib import Path

import pytest
import yaml

from litradar import query as Q
from litradar.config import REPO_ROOT
from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.store import Store

METADATA = REPO_ROOT / "datasette" / "metadata.yaml"
README = REPO_ROOT / "datasette" / "README.md"
PARAM_RE = re.compile(r":([a-z_]+)")


def _paper(i, title, pub_date, affiliations, authors=None, **kw):
    base = dict(id=f"doi:10.1000/d{i}", source="europepmc", kind="article", title=title, doi=f"10.1000/d{i}", abstract=f"Abstract {i} about Xenium.",
                journal="Nature Methods", pub_date=pub_date, url=f"https://example.org/{i}", authors=authors or [Author(name="A. Author")],
                affiliations=affiliations, sections={"methods": "Sequenced on a NovaSeq X."})
    base.update(kw)
    return Paper(**base)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    db = tmp_path_factory.mktemp("ds") / "radar.sqlite"
    st = Store(db)
    st.upsert_paper(_paper(1, "Spatial atlas", "2026-10-12", ["Example Genomics Inc, Somewhere", "Alaska, LLC"]))
    st.upsert_paper(_paper(2, "Proteomics", "2026-10-14", ["b Frederick National Laboratory for Cancer Research/Leidos Biomedical, Inc.", "Calico\nLife Sciences LLC"]))
    st.upsert_paper(_paper(3, "Undated", "", ["Acme Therapeutics, Inc."]))  # pub_date blank: counted through first_seen
    st.upsert_paper(_paper(4, "Old", "2025-01-05", ["Dept of Something, Example University"], kind="preprint", journal="biorxiv",
                           published_doi="10.1000/d1"))
    for pid, section in (("doi:10.1000/d1", "affiliations"), ("doi:10.1000/d2", "methods"), ("doi:10.1000/d3", "coi"), ("doi:10.1000/d4", "abstract")):
        st.replace_mentions(pid, [
            Mention(pid, "company", "illumina", "Illumina", "", "ILMN", "", section, 1, "..."),
            Mention(pid, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "methods", 1, "...Xenium..."),
        ])
        st.save_score(pid, 3.0, None, 4.0, "journal tier A")
    st.upsert_archive_records([ArchiveRecord("sra", f"SRX{i}", "SRP1", "Illumina NovaSeq X", "ILLUMINA", "WGS", "2026-10-12", "c", "t", "Homo sapiens", "ilmn-novaseq-x", "Illumina", "ILMN") for i in range(3)])
    st.upsert_news([NewsItem("n1", "wire", "Illumina launches", "https://example.org/n1", "2026-10-15", "", ["illumina"])])
    st.add_tracked_author_hit("doi:10.1000/d1", "A. Author", "spatial", "name")
    st.record_run("2026-10-17T00:00:00Z", "2026-10-10", "2026-10-17", {"papers": 4})
    st.commit()
    st.close()
    return db


def _queries():
    meta = yaml.safe_load(METADATA.read_text(encoding="utf-8"))
    return meta["databases"]["radar"]["queries"]


def _run(db, sql, **params):
    names = set(PARAM_RE.findall(sql))
    bound = {n: "" for n in names}
    bound.update(params)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = conn.execute(sql, bound)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def test_metadata_loads_and_lists_every_table_the_commands_create():
    meta = yaml.safe_load(METADATA.read_text(encoding="utf-8"))
    tables = meta["databases"]["radar"]["tables"]
    assert {"papers", "papers_fts", "mentions", "scores", "archive_records", "news_items", "tracked_author_hits", "runs",
            "timeseries", "concepts", "concept_counts", "concept_papers", "candidate_companies"} <= set(tables)
    assert tables["concept_papers"].get("hidden") is True
    for name in ("concepts", "concept_counts", "candidate_companies", "timeseries"):
        assert tables[name].get("columns"), name
    head = METADATA.read_text(encoding="utf-8").split("title:")[0]
    assert "external-content" not in head and "YYYY-MM-DD" in head


@pytest.mark.parametrize("name", sorted(_queries()))
def test_every_canned_query_runs_with_blank_parameters(seeded, name):
    q = _queries()[name]
    rows = _run(seeded, q["sql"])
    assert isinstance(rows, list)
    assert "YYYY-MM-DD" in q["description"] or not PARAM_RE.search(q["sql"]) or name in ("news_by_company",) or "since" not in q["sql"]


def test_companies_by_role_treats_blank_dates_as_no_bound(seeded):
    sql = _queries()["companies_by_role"]["sql"]
    unbounded = _run(seeded, sql)
    assert unbounded and unbounded[0]["entity_id"] == "illumina" and unbounded[0]["papers"] == 4
    assert unbounded[0]["as_author"] == 1 and unbounded[0]["as_funder_or_coi"] == 1 and unbounded[0]["as_subject"] == 1 and unbounded[0]["as_supplier"] == 1
    assert _run(seeded, sql, since="", until="") == unbounded
    window = _run(seeded, sql, since="2026-10-10", until="2026-10-17")
    assert window[0]["papers"] == 3  # the 2025 paper drops out; the undated one counts through first_seen
    assert _run(seeded, sql, since="2026-10-13")[0]["papers"] == 2
    assert _run(seeded, sql, until="2026-01-01")[0]["papers"] == 2  # the 2025 paper and the undated one
    assert "YYYY-MM-DD" in _queries()["companies_by_role"]["description"]


def test_search_query_parses_the_readme_examples(seeded):
    sql = _queries()["search"]["sql"]
    assert _run(seeded, sql, query="Xenium")  # four papers mention Xenium in the abstract
    assert _run(seeded, sql, query="Xenium", since="2026-10-13")
    assert _run(seeded, sql, query="") == []  # blank query, no error
    text = README.read_text(encoding="utf-8")
    examples = [ln.split("query=", 1)[1].split("&", 1)[0] for ln in text.splitlines() if ln.startswith("/radar/search?query=")]
    assert len(examples) >= 5 and not any("AND NOT" in ex for ex in examples)
    conn = sqlite3.connect(f"file:{seeded}?mode=ro", uri=True)
    try:
        for ex in examples:
            assert Q.fts_syntax_error(conn, ex) is None, ex
        assert Q.fts_syntax_error(conn, '"spatial transcriptomics" AND NOT Visium') is not None  # what the old example did
    finally:
        conn.close()


def test_unlisted_company_institutions_drops_glued_false_positives(seeded):
    rows = _run(seeded, _queries()["unlisted_company_institutions"]["sql"])
    names = [r["institution"] for r in rows]
    assert "Example Genomics Inc" in names and "Acme Therapeutics, Inc." in names
    assert "Calico Life Sciences LLC" in names  # the embedded newline is collapsed
    assert not any("Alaska" in n for n in names)  # a place name glued to a bare suffix
    assert not any("Laboratory" in n for n in names)  # the academic exclusion applies to the glued string
    assert not any("University" in n for n in names)
    assert all("\n" not in n for n in names)


def test_other_queries_return_seeded_rows(seeded):
    q = _queries()
    assert _run(seeded, q["papers_naming_instrument_in_methods"]["sql"], instrument="txg-xenium")
    assert _run(seeded, q["papers_naming_instrument_in_methods"]["sql"], instrument="Xenium", since="2026-10-01")
    assert _run(seeded, q["archive_experiments_per_model_per_week"]["sql"], model="novaseq")[0]["experiments"] == 3
    assert _run(seeded, q["tracked_author_hits"]["sql"], subgroup="spatial")[0]["author_name"] == "A. Author"
    assert _run(seeded, q["news_by_company"]["sql"], company="illumina")[0]["title"] == "Illumina launches"
    assert _run(seeded, q["preprint_to_publication_pairs"]["sql"])[0]["article_title"] == "Spatial atlas"
    assert _run(seeded, q["top_scored_papers"]["sql"], since="2026-10-01", until="2026-10-17")
    assert Path(METADATA).is_file() and Path(README).is_file()
