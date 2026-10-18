"""CLI contracts found wanting by the live shakedown: validated dates and limits, distinct entity counts,
phrase search by default, loud check-config failures, parseable stats, quiet SIGPIPE."""
import io
import json
import shutil
import sys

import pytest

from litradar import cli
from litradar import query as Q
from litradar.config import DEFAULT_CONFIG_DIR
from litradar.extract.gazetteer import Gazetteer
from litradar.models import Author, Mention, Paper
from litradar.store import Store


def _paper(i, title, abstract, pub_date="2026-10-12", **kw):
    base = dict(id=f"doi:10.1000/s{i}", source="biorxiv", kind="preprint", title=title, doi=f"10.1000/s{i}", abstract=abstract,
                journal="biorxiv", pub_date=pub_date, url=f"https://example.org/{i}", authors=[Author(name="A. Author")], sections={})
    base.update(kw)
    return Paper(**base)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    db = tmp_path / "s.sqlite"
    st = Store(db)
    st.upsert_paper(_paper(1, "Liquid biopsy of plasma cfDNA", "A liquid biopsy study of plasma."))
    st.upsert_paper(_paper(2, "Biopsy of liquid tumours", "A tissue biopsy from liquid tumour samples.", pub_date="2026-10-13"))
    st.upsert_paper(_paper(3, "Xenium atlas", "Xenium in situ profiling of a tumour atlas.", pub_date="2026-10-14", sections={"methods": "Run on a Xenium Analyzer."}))
    st.upsert_paper(_paper(4, "Xenium validation", "We validate Xenium panels.", pub_date="2026-10-15"))
    st.upsert_paper(_paper(5, "Older Xenium paper", "Xenium again.", pub_date="2025-06-01"))
    st.replace_mentions("doi:10.1000/s3", [
        Mention("doi:10.1000/s3", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "...Xenium in situ..."),
        Mention("doi:10.1000/s3", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "methods", 2, "...Xenium Analyzer..."),
    ])
    st.replace_mentions("doi:10.1000/s4", [Mention("doi:10.1000/s4", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "...panels...")])
    st.replace_mentions("doi:10.1000/s5", [Mention("doi:10.1000/s5", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "title", 1, "...")])
    st.save_score("doi:10.1000/s3", 4.0, None, 5.5, "preprint; 1 platform(s)")
    st.commit()
    st.close()
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)
    return db


# ---- --since / --until / --limit are validated by every command -------------------------------------
@pytest.mark.parametrize("argv", [
    ["search", "Xenium", "--since", "yesterday"],
    ["search", "Xenium", "--since", "lastweek"],
    ["entity", "txg-xenium", "--since", "lastweek"],
    ["rematch", "--since", "7x"],
    ["tag", "--since", "soon"],
    ["coverage", "--since", "soon"],
    ["candidates", "--since", "nope"],
    ["digest", "--since", "x"],
    ["run", "--since", "x"],
    ["watch", "run", "--since", "x"],
    ["show", "illumina", "--until", "x"],
    ["fetch", "ena", "--since", "x"],
])
def test_bad_since_is_a_usage_error_everywhere(seeded, capsys, argv):
    if argv[0] == "fetch":
        assert cli.main(argv) == 2  # the fetch command reports usage errors itself
        err = capsys.readouterr().err
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
        err = capsys.readouterr().err
    assert "must be YYYY-MM-DD or Nd (for example 90d)" in err and "Traceback" not in err


def test_impossible_calendar_dates_and_zero_limits_are_rejected(seeded, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["search", "Xenium", "--since", "2026-13-45"])
    assert exc.value.code == 2 and "is not a real calendar date: 2026-13-45" in capsys.readouterr().err
    for argv in (["search", "Xenium", "--limit", "0"], ["entity", "txg-xenium", "--limit", "-1"], ["show", "illumina", "--limit", "0"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2 and "positive integer" in capsys.readouterr().err
    # the shared parser behind all of it
    assert Q.parse_since("90d", today=__import__("datetime").date(2026, 10, 17)) == "2026-07-19"
    assert Q.parse_since("") is None and Q.parse_since(None) is None and Q.parse_since("2026-10-01") == "2026-10-01"
    for bad in ("yesterday", "7x", "2026/10/01", "20261001", "d", "2026-02-30"):
        with pytest.raises(ValueError):
            Q.parse_since(bad)


# ---- search: phrase by default, --raw-fts, visible fallback, validated --in --------------------------
def test_search_plain_multiword_query_is_a_phrase(seeded, capsys):
    assert cli.main(["search", "liquid biopsy", "--json"]) == 0
    captured = capsys.readouterr()
    assert [r["id"] for r in json.loads(captured.out)] == ["doi:10.1000/s1"]
    assert 'searching for the phrase "liquid biopsy"' in captured.err and "--raw-fts" in captured.err
    assert cli.main(["search", "liquid biopsy", "--raw-fts", "--json"]) == 0
    captured = capsys.readouterr()
    assert {r["id"] for r in json.loads(captured.out)} == {"doi:10.1000/s1", "doi:10.1000/s2"}  # AND of the two words
    assert "searching for the phrase" not in captured.err
    assert cli.main(["search", '"liquid biopsy"', "--json"]) == 0  # already quoted: untouched
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["doi:10.1000/s1"]
    assert cli.main(["search", "liquid OR tumours", "--json"]) == 0  # FTS5 operators pass through (and "tumours" stems to "tumour")
    assert {r["id"] for r in json.loads(capsys.readouterr().out)} == {"doi:10.1000/s1", "doi:10.1000/s2", "doi:10.1000/s3"}
    assert Q.auto_phrase("single-cell") == '"single-cell"' and Q.auto_phrase("Xenium") == "Xenium" and Q.auto_phrase("methods:Xenium") == "methods:Xenium"
    assert Q.auto_phrase("Xenium NOT Visium") == "Xenium NOT Visium" and Q.auto_phrase("NovaSeq*") == "NovaSeq*"


def test_search_says_when_it_falls_back_and_reports_scores(seeded, capsys):
    assert cli.main(["search", "nosuchcol:Xenium", "--json"]) == 0
    captured = capsys.readouterr()
    assert "not valid FTS5" in captured.err and "no such column: nosuchcol" in captured.err and 'phrase "nosuchcol:Xenium"' in captured.err
    assert json.loads(captured.out) == []
    assert cli.main(["search", "Xenium", "--since", "2026-10-01", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    scored = next(r for r in rows if r["id"] == "doi:10.1000/s3")
    assert scored["score"] == {"final": 5.5, "heuristic": 4.0, "reasons": "preprint; 1 platform(s)"}
    assert next(r for r in rows if r["id"] == "doi:10.1000/s4")["score"] is None


def test_search_unknown_section_is_a_message_not_a_traceback(seeded, capsys):
    assert cli.main(["search", "Xenium", "--in", "methods,foo"]) == 2
    err = capsys.readouterr().err
    assert "unknown section(s) foo; choose from title, abstract, methods" in err and "Traceback" not in err
    assert cli.main(["search", "Xenium", "--in", "methods", "--json"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["doi:10.1000/s3"]


# ---- entity: distinct papers, sections grouped, honest counts ----------------------------------------
def test_entity_counts_distinct_papers_not_mention_rows(seeded, capsys):
    assert cli.main(["entity", "txg-xenium"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("3 papers mention txg-xenium\n") and out.count("Xenium atlas") == 1
    assert "[abstract/methods] Xenium atlas" in out or "[methods/abstract] Xenium atlas" in out
    assert cli.main(["entity", "txg-xenium", "--since", "2026-10-01"]) == 0
    assert capsys.readouterr().out.startswith("2 papers mention txg-xenium\n")
    assert cli.main(["entity", "txg-xenium", "--limit", "1"]) == 0
    assert capsys.readouterr().out.startswith("3 papers mention txg-xenium (showing 1)\n")
    assert cli.main(["entity", "txg-xenium", "--limit", "1", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["papers"] == 3 and d["shown"] == 1 and d["kind"] == "instrument" and d["results"][0]["id"] == "doi:10.1000/s4"
    assert d["results"][0]["score"] is None


def test_entity_unknown_id_exits_1_known_but_silent_id_exits_0(seeded, capsys):
    assert cli.main(["entity", "not-a-real-id"]) == 1
    err = capsys.readouterr().err
    assert "'not-a-real-id' is not a gazetteer id and has no stored mentions" in err and "litradar entities --grep" in err
    from litradar.config import Settings

    gz = Gazetteer.from_yaml(Settings.from_env().yaml("instruments"), "instrument")
    silent = next(e.id for e in gz.entries if e.id != "txg-xenium")
    assert cli.main(["entity", silent]) == 0
    assert capsys.readouterr().out.strip() == f"0 papers mention {silent}"


# ---- stats ------------------------------------------------------------------------------------------
def test_stats_json_is_one_object_and_text_is_readable(seeded, capsys):
    assert cli.main(["stats", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)  # one document, nothing trailing
    assert d["counts"]["papers"] == 5 and d["runs"] == []
    assert cli.main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "papers" in out and "recent runs: none recorded" in out and not out.lstrip().startswith("{")


# ---- check-config -----------------------------------------------------------------------------------
def test_check_config_fails_loudly_on_a_missing_directory(seeded, tmp_path, capsys, monkeypatch):
    assert cli.main(["--config-dir", str(tmp_path / "no" / "such" / "dir"), "check-config"]) == 1
    captured = capsys.readouterr()
    assert "config directory not found" in captured.err and str(tmp_path / "no" / "such" / "dir") in captured.err
    monkeypatch.setenv("LITRADAR_CONFIG_DIR", str(tmp_path / "also-missing"))
    assert cli.main(["check-config"]) == 1
    assert "config directory not found" in capsys.readouterr().err
    monkeypatch.delenv("LITRADAR_CONFIG_DIR")
    shutil.copytree(DEFAULT_CONFIG_DIR, tmp_path / "config")
    (tmp_path / "config" / "feeds.yaml").unlink()
    assert cli.main(["--config-dir", str(tmp_path / "config"), "check-config"]) == 1
    assert "missing feeds.yaml" in capsys.readouterr().err
    assert cli.main(["check-config"]) == 0  # the repository's own config is fine
    assert "config dir:" in capsys.readouterr().out


def test_check_config_reports_malformed_yaml_in_one_line(seeded, tmp_path, capsys):
    shutil.copytree(DEFAULT_CONFIG_DIR, tmp_path / "config")
    with open(tmp_path / "config" / "companies.yaml", "a", encoding="utf-8") as fh:
        fh.write("\nthis: [is: broken\n")
    assert cli.main(["--config-dir", str(tmp_path / "config"), "check-config"]) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err and "Traceback" not in captured.out
    line = next(ln for ln in captured.err.splitlines() if "companies.yaml" in ln)
    assert line.startswith("check-config: companies.yaml: YAML parse error at line ") and "column" in line
    # a malformed radar.yaml is caught too (it is loaded by Settings itself)
    shutil.rmtree(tmp_path / "config")
    shutil.copytree(DEFAULT_CONFIG_DIR, tmp_path / "config")
    with open(tmp_path / "config" / "radar.yaml", "a", encoding="utf-8") as fh:
        fh.write("\nbroken: [\n")
    assert cli.main(["--config-dir", str(tmp_path / "config"), "check-config"]) == 1
    assert "radar.yaml: YAML parse error" in capsys.readouterr().err


def test_check_config_warns_about_duplicate_top_level_keys(seeded, tmp_path, capsys):
    shutil.copytree(DEFAULT_CONFIG_DIR, tmp_path / "config")
    with open(tmp_path / "config" / "radar.yaml", "a", encoding="utf-8") as fh:
        fh.write("\npriorities:\n  company:\n    illumina: 2\n")
    rc = cli.main(["--config-dir", str(tmp_path / "config"), "check-config"])
    out = capsys.readouterr().out
    assert rc == 0 and "WARNING radar.yaml: top-level key 'priorities' appears 2 times" in out


# ---- SIGPIPE ----------------------------------------------------------------------------------------
class _ClosedPipe(io.TextIOBase):
    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        pass


def test_broken_pipe_is_a_quiet_exit(seeded, monkeypatch):
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    assert cli.main(["entities"]) == 0
    assert cli.main(["stats", "--json"]) == 0


@pytest.mark.parametrize("argv", [["entities"], ["--version"], ["check-config"]])
def test_closed_pipe_leaves_no_trace_in_a_real_process(seeded, argv):
    """`litradar <listing> | head`: the reader closes early. Both the write-time EPIPE (large output) and the
    exit-time flush of a small buffered output must end quietly, with nothing on stderr."""
    import os
    import subprocess

    env = dict(os.environ, LITRADAR_DB=str(seeded), PYTHONUNBUFFERED="")
    proc = subprocess.Popen([sys.executable, "-m", "litradar", *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    proc.stdout.close()  # the reader goes away before (or while) the writer writes
    _, err = proc.communicate(timeout=120)
    text = err.decode("utf-8", "replace")
    assert "BrokenPipeError" not in text and "Exception ignored" not in text and "Traceback" not in text, text
    assert proc.returncode == 0


# ---- watch terms are phrases ------------------------------------------------------------------------
def test_watch_term_matches_as_a_phrase(seeded, capsys):
    assert cli.main(["watch", "add", "liquid biopsy", "--kind", "term"]) == 0
    capsys.readouterr()
    assert cli.main(["watch", "run", "--since", "2026-10-01", "--until", "2026-10-17", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    term = next(t for t in rep["terms"] if t["term"] == "liquid biopsy")
    assert term["matches"] == 1 and term["items"][0]["id"] == "doi:10.1000/s1"


# ---- help hygiene -----------------------------------------------------------------------------------
def test_help_texts_name_formats_and_one_kind_spelling(capsys):
    for argv in (["entity", "--help"], ["digest", "--help"], ["run", "--help"], ["search", "--help"], ["prioritize", "--help"], ["candidates", "--help"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 0
        out = capsys.readouterr().out
        if argv[0] in ("entity", "digest", "run"):
            assert "YYYY-MM-DD or Nd" in out and "--since SINCE\n" not in out  # every flag carries a description
        if argv[0] == "prioritize":
            assert "instrument-category" in out and "instrument_category" not in out
        if argv[0] == "search":
            assert "PHRASE" in out and "--raw-fts" in out and "docs/search.md" in out
        if argv[0] == "candidates":
            assert "litradar candidates promote <name>" in out  # line breaks preserved, not reflowed into one paragraph
