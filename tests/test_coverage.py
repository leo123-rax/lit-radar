"""Offline tests for `litradar coverage` on a seeded temp database and a temp copy of config/."""
import json
import shutil

import pytest
import yaml

from litradar import coverage as C
from litradar.cli import main
from litradar.config import DEFAULT_CONFIG_DIR, Settings
from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.store import Store

SINCE, UNTIL = "2026-10-10", "2026-10-17"



def _alias_count(settings, company_id):
    entries = yaml.safe_load((settings.config_dir / "companies.yaml").read_text())["entries"]
    e = next(x for x in entries if x["id"] == company_id)
    return len(e.get("aliases") or [])

@pytest.fixture
def settings(tmp_path, monkeypatch):
    shutil.copytree(DEFAULT_CONFIG_DIR, tmp_path / "config")
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "radar.sqlite"))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("LITRADAR_CONTACT_EMAIL", raising=False)
    s = Settings.from_env(config_dir=tmp_path / "config")
    s.cache_dir = tmp_path / ".cache"
    s.db_path = tmp_path / "radar.sqlite"
    s.openalex_api_key = ""
    s.contact_email = ""
    return s


def _paper(i, source, journal, pub_date, **kw):
    base = dict(id=f"doi:10.1000/cov{i}", source=source, kind="article", title=f"Paper {i}", doi=f"10.1000/cov{i}",
                abstract="", journal=journal, pub_date=pub_date, url=f"https://example.org/{i}",
                authors=[Author(name="Example Author")], sections={}, fulltext_available=False)
    base.update(kw)
    return Paper(**base)


@pytest.fixture
def store(settings):
    st = Store(settings.db_path)
    st.upsert_paper(_paper(1, "biorxiv", "biorxiv", "2026-10-12", kind="preprint", title="Xenium spatial transcriptomics of tumours", fulltext_available=True))
    st.upsert_paper(_paper(2, "europepmc", "Nature Methods", "2026-10-13", abstract="A single-cell method.", extra={"issn": ["1548-7091"]}))
    st.upsert_paper(_paper(3, "crossref", "Cell", "2026-09-20"))  # trailing window only
    st.upsert_paper(_paper(4, "rss:Nature", "Nature", "2026-10-15"))
    st.upsert_paper(_paper(5, "europepmc", "Nat Methods", "2026-10-14", extra={"issn": ["1548-7105"]}))  # abbreviated title, ISSN match
    st.replace_mentions("doi:10.1000/cov1", [
        Mention("doi:10.1000/cov1", "company", "10x-genomics", "10x Genomics", "", "TXG", "", "affiliations", 1, "..."),
        Mention("doi:10.1000/cov1", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "title", 1, "..."),
    ])
    st.replace_mentions("doi:10.1000/cov3", [Mention("doi:10.1000/cov3", "company", "illumina", "Illumina", "", "ILMN", "", "methods", 1, "...")])
    st.add_tracked_author_hit("doi:10.1000/cov2", "Aviv Regev", "single_cell", "openalex_id")
    st.upsert_archive_records([ArchiveRecord("sra", "SRX1", "SRP1", "Revio", "PACBIO_SMRT", "WGS", "2026-10-12", "c", "t", "Homo sapiens", "pacb-revio", "Pacific Biosciences", "PACB")])
    st.upsert_news([NewsItem("n1", "news:Illumina", "Illumina launches something", "https://example.org/n1", "2026-10-14", "", ["illumina"])])
    st.record_run("2026-10-17T06:00:00Z", SINCE, UNTIL, {
        "since": SINCE, "until": UNTIL,
        "counts": {"fetched.biorxiv": 100, "fetched.europepmc.journals": 40, "fetched.europepmc.methods": 10, "fetched.crossref": 5, "papers.unique": 120},
        "errors": ["europepmc: HTTP 500 boom", "feed Example Feed: timeout"], "http": {"failures": 2},
    })
    st.commit()
    return st


def test_coverage_report_sections(store, settings):
    r = C.coverage_report(store, SINCE, UNTIL, settings)
    assert r["papers_in_window"] == 4
    by_src = {s["source"]: s for s in r["sources"]}
    assert set(by_src) == {"biorxiv", "europepmc", "rss"}
    assert by_src["biorxiv"]["papers"] == 1 and by_src["biorxiv"]["fulltext"] == 1 and by_src["biorxiv"]["share"] == 0.25
    assert by_src["biorxiv"]["fulltext_share"] == 1.0 and by_src["biorxiv"]["pulled_last_run"] == 100
    assert by_src["europepmc"]["papers"] == 2 and by_src["europepmc"]["pulled_last_run"] == 50  # journals + methods labels summed
    assert by_src["rss"]["feeds"] == 1 and by_src["rss"]["pulled_last_run"] is None

    zero = {j["name"]: j for j in r["journals"]["zero"]}
    assert r["journals"]["total"] == 37
    assert "Nature Methods" not in zero and "Nature" not in zero
    assert zero["Science"]["last_seen"] is None and zero["Science"]["trailing_13w_avg"] == 0.0
    assert zero["Cell"]["last_seen"] == "2026-09-20" and zero["Cell"]["trailing_13w_avg"] == round(1 / 13, 2)

    w = r["watched"]
    cz = {c["id"]: c for c in w["companies"]["zero"]}
    assert "10x-genomics" not in cz
    assert cz["illumina"]["news_items"] == 1 and cz["illumina"]["trailing_13w_avg"] == round(1 / 13, 2) and cz["illumina"]["aliases"] == _alias_count(settings, "illumina")
    assert w["companies"]["total"] == sum(1 for e in yaml.safe_load((settings.config_dir / "companies.yaml").read_text())["entries"] if e.get("watch_news"))
    pz = {p["id"] for p in w["platforms"]["zero"]}
    assert "txg-xenium" not in pz and "pacb-revio" not in pz and "ilmn-novaseq-x" in pz
    az = {(a["subgroup"], a["name"]) for a in w["authors"]["zero"]}
    assert ("single_cell", "Aviv Regev") not in az and ("spatial", "Xiaowei Zhuang") in az
    assert w["authors"]["total"] > w["authors"]["with_openalex_id"] > 0
    tz = {t["term"] for t in w["terms"]["zero"]}
    assert "spatial transcriptomics" not in tz and "single-cell" not in tz and "cryo-EM" in tz

    assert r["errored_sources"] == [{"source": "europepmc", "error": "HTTP 500 boom"}, {"source": "feed Example Feed", "error": "timeout"}]
    sugg = "\n".join(r["suggestions"])
    assert "europepmc errored in the last run" in sugg
    assert "No OpenAlex key configured" in sugg
    assert "LITRADAR_CONTACT_EMAIL is empty" in sugg
    assert "tracked authors have no OpenAlex id" in sugg
    assert "Source crossref is enabled but contributed 0 papers" in sugg and "Source arxiv is enabled" in sugg
    assert "check the ISSN" in sugg  # never-seen journals with an ISSN
    json.dumps(r)  # serialisable


def test_suggestions_react_to_configuration(store, settings):
    settings.openalex_api_key = "k"
    settings.contact_email = "noreply@example.org"
    r = C.coverage_report(store, SINCE, UNTIL, settings)
    sugg = "\n".join(r["suggestions"])
    assert "No OpenAlex key" not in sugg and "LITRADAR_CONTACT_EMAIL" not in sugg


def test_markdown_rendering(store, settings):
    md = C.render_coverage_markdown(store, SINCE, UNTIL, settings)
    assert md.startswith(f"## Coverage: {SINCE} to {UNTIL}")
    for heading in ("### Records per source", "### Journals with zero items in the window", "### Watched entities with zero mentions",
                    "### Sources that errored in the last run", "### Suggestions"):
        assert heading in md
    assert "| biorxiv | 1 | 25% | 1 | 100% | 100 |" in md
    assert "- **europepmc**: HTTP 500 boom" in md
    assert f"| Illumina | ILMN | 1 | 0.08 | {_alias_count(settings, 'illumina')} |" in md


def test_empty_database_and_cli_json(settings, store, capsys):
    empty = Store(settings.db_path.with_name("empty.sqlite"))
    r = C.coverage_report(empty, SINCE, UNTIL, settings)
    assert r["papers_in_window"] == 0 and r["sources"] == [] and r["last_run"] is None
    assert any("No pipeline run is recorded" in s for s in r["suggestions"])
    assert "_no papers in the window_" in C.render_coverage_markdown(empty, SINCE, UNTIL, settings)
    store.close()
    assert main(["--config-dir", str(settings.config_dir), "coverage", "--since", SINCE, "--until", UNTIL, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["papers_in_window"] == 4 and out["errored_sources"][0]["source"] == "europepmc"
    assert main(["--config-dir", str(settings.config_dir), "coverage", "--days", "7"]) == 0
    assert "### Suggestions" in capsys.readouterr().out
