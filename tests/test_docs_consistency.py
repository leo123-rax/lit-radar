"""The documentation must describe the shipped code.

Every test here guards a claim that went stale once because a parallel branch changed the code while the
documentation on the other branch was merged untouched (found by the 2026-10-18 shakedown): the phrase rule
of `search`, the `mentions` search column, which adapters are wired, where the database persists, which
config keys the digest reads, and which sources the policy documents cover."""
import inspect
import json
import re
from pathlib import Path

import pytest
import yaml

from litradar import api, cli, pipeline
from litradar.commands import concepts as concepts_cmd
from litradar.digest import render
from litradar.models import Author, Mention, Paper
from litradar.store import FTS_COLUMNS, Store

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _flat(rel: str) -> str:
    """The file with every run of whitespace collapsed, so a phrase wrapped across lines still matches."""
    return " ".join(_read(rel).split())


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


def _help(capsys, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
    return capsys.readouterr().out


def _radar_sources() -> dict:
    return yaml.safe_load(_read("config/radar.yaml"))["sources"]


# ---- search: the `mentions` column and the phrase rule ------------------------------------------------
def test_search_help_docs_and_api_agree_on_the_fts_columns(capsys):
    assert "mentions" in FTS_COLUMNS
    out = _help(capsys, ["search", "--help"])
    for col in FTS_COLUMNS:
        assert col in out, col
    assert "mentions" in out and "alias" in out
    assert "not necessarily by its name" not in out  # the pre-`mentions` caveat denied what the store does
    section_1 = _section(_read("docs/search.md"), "## 1. What is indexed", "## 2.")
    for col in FTS_COLUMNS:
        assert f"| `{col}` |" in section_1, col
    assert api.FTS_COLUMNS == FTS_COLUMNS  # one list, not a second copy that forgets a column
    assert '"mentions"' in api.PAGE_HTML and "mentions" in api.PAGE_HTML.split("<p class=\"sub\"")[1].split("</p>")[0]


def test_plain_search_finds_a_paper_attributed_through_an_alias(tmp_path, monkeypatch, capsys):
    db = tmp_path / "m.sqlite"
    st = Store(db)
    st.upsert_paper(Paper(id="doi:10.1000/alias1", source="biorxiv", kind="preprint", title="Single-cell profiling of a tumour",
                          doi="10.1000/alias1", abstract="Libraries were made with the Next GEM 5' reagent kit.", journal="biorxiv",
                          pub_date="2026-10-12", url="https://example.org/alias1", authors=[Author(name="A. Author")], sections={}))
    st.replace_mentions("doi:10.1000/alias1", [
        Mention("doi:10.1000/alias1", "instrument", "txg-chromium", "Chromium", "10x Genomics", "TXG", "single-cell", "abstract", 1, "...Next GEM..."),
    ])
    st.commit()
    st.close()
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)

    def ids(argv):
        assert cli.main(argv) == 0
        return [r["id"] for r in json.loads(capsys.readouterr().out)]

    assert ids(["search", "Chromium", "--json"]) == ["doi:10.1000/alias1"]  # the text never says "Chromium"
    assert ids(["search", "Chromium", "--in", "mentions", "--json"]) == ["doi:10.1000/alias1"]
    assert ids(["search", "Chromium", "--in", "title,abstract,methods,body,funding,coi,affiliations", "--json"]) == []
    assert ids(["search", "txg-chromium", "--in", "mentions", "--json"]) == ["doi:10.1000/alias1"]  # by id too


def test_readme_search_row_states_the_phrase_rule_the_cli_implements(capsys):
    readme = _read("README.md")
    row = next(line for line in readme.splitlines() if line.startswith("| `search "))
    assert "phrase" in row.lower() and "--raw-fts" in row and "`mentions`" in row
    assert "AND of stemmed terms" not in readme
    assert "is a **phrase** search" in _read("docs/search.md")
    assert "PHRASE" in _help(capsys, ["search", "--help"])


# ---- SOURCES.md, DESIGN.md, ROADMAP.md, SCALE.md: wired means wired ----------------------------------
def _sources_status_cells() -> dict:
    cells = {}
    for line in _read("docs/SOURCES.md").splitlines():
        if not line.startswith("|"):
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        m = re.search(r"`sources/([a-z_]+)\.py`", parts[1])
        if m and m.group(1) not in cells:
            cells[m.group(1)] = parts[2].strip()
    return cells


def test_sources_catalog_status_matches_radar_yaml():
    sources = _radar_sources()
    cells = _sources_status_cells()
    for key, cfg in sources.items():
        if key in ("sec", "rss"):  # both run through sources/rss.py; the SEC row is checked below
            continue
        assert key in cells, f"no `sources/{key}.py` row in docs/SOURCES.md"
        if cfg.get("enabled", True):
            assert cells[key].startswith("wired"), f"{key}: {cells[key]}"
        else:
            assert "built" in cells[key] and "disabled by default" in cells[key], f"{key}: {cells[key]}"
    text = _read("docs/SOURCES.md")
    assert "**built** (not wired)" not in text
    assert "still uses `from-pub-date`" not in text and "from-created-date" in text
    assert "blocked by User-Agent format" not in text and "LITRADAR_CONTACT_EMAIL" in text
    assert "under review → opt-in" not in text


def test_design_scale_and_roadmap_describe_the_shipped_pipeline():
    design = _read("docs/DESIGN.md")
    for stale in ("(built, not wired)", "opt-in pending", "Built, not yet wired", "lives only in the Actions cache",
                  "planned: dashboard", "being done by the code and legal owners", "on by default in the current code",
                  "request sanctioned access, do not script around"):
        assert stale not in design, stale
    assert "db-latest" in design and "fulltext_policy" in design
    scale = _read("docs/SCALE.md")
    assert "Keep the Actions cache as the hot copy" not in scale and "what `weekly.yml` does today) |" not in scale
    assert "db-latest" in scale
    weekly = _read(".github/workflows/weekly.yml")
    assert "gh release download" in weekly and "db-latest" in weekly  # the docs now say what the workflow does
    roadmap = _read("docs/ROADMAP.md")
    item_1 = roadmap.split("\n2. ")[0].split("\n1. ")[1]
    assert "done" in item_1.lower() and "Status: built." not in item_1
    assert "scheduling not wired" not in roadmap and "request for sanctioned access" not in roadmap
    assert "built and being wired in" not in _read("README.md")
    assert "GitHub Actions cache" not in _read("docs/trends.md")


def test_per_source_notes_describe_the_wiring_as_applied():
    for name in ("geo", "clinicaltrials", "openfda", "cellxgene", "semantic_scholar"):
        text = _read(f"docs/sources/{name}.md")
        assert "as applied in `litradar/pipeline.py`" in text, name
        for stale in ("not applied here", "not done by this adapter", "nothing below is in this branch",
                      "to add to `litradar/pipeline.py`", "to add to `config/radar.yaml`", "not yet wired"):
            assert stale not in text, (name, stale)
    src = inspect.getsource(pipeline)
    for call in ("geo_src.fetch(", "pride_src.fetch(", "cellxgene_src.fetch(", "ctgov_src.new_studies(", "openfda_src.fetch(",
                 "s2_src.enrich_citations("):
        assert call in src, call


# ---- the derived layers: documented config keys are the ones the renderer reads -----------------------
def test_trends_doc_names_the_digest_keys_the_renderer_reads():
    trends = _read("docs/trends.md")
    assert "trend_weeks" in trends and "trend_top_n" in trends
    assert not re.search(r"^trends:\s*$", trends, re.M), "a top-level `trends:` block that nothing reads"
    assert "is not modified by this change" not in trends
    digest_cfg = yaml.safe_load(_read("config/radar.yaml"))["digest"]
    assert "trend_weeks" in digest_cfg and "trend_top_n" in digest_cfg
    src = inspect.getsource(render._trends_section)
    assert "trend_weeks" in src and "trend_top_n" in src


def test_derived_layer_docs_no_longer_call_wired_features_pending():
    stale = {
        "docs/concepts.md": ("Wiring into the weekly digest is one line", "is not part of this module"),
        "docs/discovery.md": ("Until that is wired", "files this module does not own"),
        "docs/track.md": ("(not done by this module)",),
        "docs/i18n.md": ("Wiring (not applied here",),
        "docs/browser.md": ("and make `Pipeline.__init__` build its client through",),
    }
    for rel, phrases in stale.items():
        flat = _flat(rel)
        for phrase in phrases:
            assert phrase not in flat, (rel, phrase)
    for rel in ("docs/concepts.md", "docs/discovery.md", "docs/track.md", "docs/browser.md"):
        assert "as applied" in _flat(rel), rel
    assert "**2 is applied**" in _read("docs/i18n.md")
    # ...and the code really has each wiring the docs now describe
    pipe_src = inspect.getsource(pipeline)
    for call in ("priority_multiplier(", "discovery.refresh(", "i18n.tag_store(", "make_http("):
        assert call in pipe_src, call
    assert "render_concepts_markdown(" in inspect.getsource(render) and "render_candidates_markdown(" in inspect.getsource(render)
    assert "render_coverage_markdown(" in inspect.getsource(cli)
    assert "--since-baseline" in inspect.getsource(concepts_cmd)


# ---- LEGAL.md, NOTICE, ACCEPTABLE_USE.md: every configured source has its terms on record -------------
SOURCE_NAMES = {
    "biorxiv": ("bioRxiv",), "europepmc": ("Europe PMC",), "openalex": ("OpenAlex",), "crossref": ("Crossref",),
    "arxiv": ("arXiv",), "sra": ("SRA",), "ena": ("ENA",), "sec": ("SEC EDGAR",), "rss": ("RSS",),
    "chinaxiv": ("ChinaXiv",), "scielo": ("SciELO",), "kci": ("KCI",), "jstage": ("J-STAGE",),
    "geo": ("GEO",), "pride": ("PRIDE",), "cellxgene": ("CELLxGENE",), "gsa": ("GSA",), "gsa_human": ("GSA-Human",),
    "nih_reporter": ("NIH RePORTER",), "clinicaltrials": ("ClinicalTrials.gov",), "openfda": ("openFDA",),
    "semantic_scholar": ("Semantic Scholar",),
}
NEW_IN_2026_10 = ("chinaxiv", "scielo", "kci", "jstage", "gsa", "gsa_human")


def test_policy_documents_cover_every_configured_source():
    sources = _radar_sources()
    legal = _read("docs/LEGAL.md")
    legal_terms = _section(legal, "## 4. Per-source terms", "## 5. ")
    notice = _read("NOTICE")
    aup = _read("docs/ACCEPTABLE_USE.md")
    for key in sources:
        assert key in SOURCE_NAMES, f"add {key} to SOURCE_NAMES and to the policy documents"
        names = SOURCE_NAMES[key]
        assert any(n in legal_terms for n in names), f"{key}: no row in docs/LEGAL.md section 4"
        assert any(n in notice for n in names), f"{key}: missing from NOTICE"
    for key in NEW_IN_2026_10:
        assert any(n in aup for n in SOURCE_NAMES[key]), f"{key}: missing from docs/ACCEPTABLE_USE.md"
    # the two conflicts are stated in the policy document, not only in the source notes
    assert "Article 3(5)" in legal and "24 hours" in legal and "Article 9" in legal
    assert "no commercial use" in legal and "sources.kci.enabled: false" in legal
    assert "KOGL" in notice and "Article 3(5)" in notice
    assert "sources.kci.enabled: false" in aup and "24" in aup


def test_html_exception_for_ngdc_is_declared_where_the_rule_is_stated():
    legal = _read("docs/LEGAL.md")
    assert "ngdc.cncb.ac.cn" in legal and "gsa_human.py" in legal and "exception" in _section(legal, "## 3. What is never done", "## 4. ")
    assert "only uses documented machine routes" not in legal
    contributing = _read("CONTRIBUTING.md")
    assert "gsa.py" in contributing and "docs/LEGAL.md" in contributing
    assert "Adapters call documented APIs or RSS feeds only." not in contributing
    aup = _read("docs/ACCEPTABLE_USE.md")
    assert "GSA" in aup and "docs/LEGAL.md` section 3" in aup
    # the adapters the exception covers are the ones that read HTML, and nothing else does
    html_readers = {p.name for p in (ROOT / "litradar" / "sources").glob("*.py") if "server-rendered" in p.read_text(encoding="utf-8")}
    assert html_readers == {"gsa.py", "gsa_human.py"}, html_readers
    # the "new sources" checklist now sends contributors to the policy trio
    checklist = _section(contributing, "3. **New sources.**", "4. **OpenAlex")
    for doc in ("docs/LEGAL.md", "NOTICE", "docs/ACCEPTABLE_USE.md", "docs/SOURCES.md"):
        assert doc in checklist, doc
