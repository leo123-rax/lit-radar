"""The read-only JSON API: served on an ephemeral port against a temp seeded store."""
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from litradar import api
from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.store import Store

TODAY = date(2026, 10, 17)


def _paper(i: int, **kw) -> Paper:
    base = dict(
        id=f"doi:10.1000/test{i}", source="biorxiv", kind="preprint", title=f"Paper {i} about Xenium spatial profiling",
        doi=f"10.1000/test{i}", abstract="We present a novel spatial transcriptomics method using Xenium.", journal="biorxiv",
        pub_date="2026-10-12", url=f"https://example.org/{i}", authors=[Author(name="A. Author", affiliation="Example Genomics Inc")],
        affiliations=["Example Genomics Inc, Somewhere"], sections={"methods": "Sequenced on NovaSeq X Plus.", "body": "full body text"},
        fulltext_available=True,
    )
    base.update(kw)
    return Paper(**base)


def _seed(db_path: Path, digest_dir: Path) -> None:
    st = Store(db_path)
    st.upsert_paper(_paper(1))
    st.upsert_paper(_paper(2, title="Unrelated proteomics work", abstract="Olink Explore plasma proteomics", sections={}, pub_date="2026-10-14", kind="article", journal="Nature Methods", source="crossref"))
    st.upsert_paper(_paper(3, title="Old paper on Xenium", pub_date="2025-01-05", sections={"methods": "Imaged on a slide scanner."}))
    st.replace_mentions("doi:10.1000/test1", [
        Mention("doi:10.1000/test1", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "...Xenium..."),
        Mention("doi:10.1000/test1", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "title", 1, "...Xenium..."),
        Mention("doi:10.1000/test1", "instrument", "ilmn-novaseq-x", "NovaSeq X", "Illumina", "ILMN", "sequencer.short_read", "methods", 1, "...NovaSeq X Plus..."),
        Mention("doi:10.1000/test1", "company", "10x-genomics", "10x Genomics", "", "TXG", "", "affiliations", 1, "..."),
    ])
    st.replace_mentions("doi:10.1000/test3", [
        Mention("doi:10.1000/test3", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "title", 1, "..."),
    ])
    st.save_score("doi:10.1000/test1", 5.0, {"novelty": 4}, 6.5, "preprint; 2 platform(s)")
    st.save_score("doi:10.1000/test2", 3.0, None, 3.0, "journal tier A")
    st.upsert_archive_records([
        ArchiveRecord("sra", f"SRX{i}", "SRP1", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", "2026-10-12", "c", "t", "Homo sapiens", "ilmn-novaseq-x", "Illumina", "ILMN")
        for i in range(3)
    ] + [ArchiveRecord("ena", "ERX1", "ERP1", "PacBio Revio", "PACBIO_SMRT", "WGS", "2026-10-13", "c", "t", "Mus musculus", "pacb-revio", "Pacific Biosciences", "PACB")])
    st.upsert_news([
        NewsItem("n1", "wire", "Vendor A launches instrument", "https://example.org/n1", "2026-10-15", "summary", ["vendor-a"]),
        NewsItem("n2", "wire", "Unrelated headline", "https://example.org/n2", "2026-10-16", "summary", []),
    ])
    st.add_tracked_author_hit("doi:10.1000/test1", "A. Author", "spatial", "name")
    st.record_run("2026-10-17T00:00:00Z", "2026-10-10", "2026-10-17", {"papers": 3})
    st.commit()
    st.close()
    digest_dir.mkdir()
    (digest_dir / "latest.md").write_text("# Science literature radar: 2026-10-10 to 2026-10-17\n\nhello\n", encoding="utf-8")


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    root = tmp_path_factory.mktemp("api")
    db = root / "radar.sqlite"
    _seed(db, root / "digests")
    app = api.RadarAPI(db, digest_dir=root / "digests", today=lambda: TODAY)
    server, thread = api.start_in_thread(app)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    before = _fingerprint(db)
    yield {"base": base, "db": db, "app": app, "before": before}
    server.shutdown()
    server.server_close()


def _fingerprint(db: Path):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return tuple(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("papers", "mentions", "scores", "runs", "news_items"))
    finally:
        c.close()


def _get(base: str, path: str, method: str = "GET"):
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _json(base: str, path: str):
    status, headers, body = _get(base, path)
    assert headers["Content-Type"].startswith("application/json"), headers
    return status, json.loads(body.decode("utf-8"))


# ---- /search ---------------------------------------------------------------------------
def test_search_shape_and_chips(served):
    status, data = _json(served["base"], "/search?q=Xenium")
    assert status == 200
    assert set(data) == {"query", "since", "sections", "limit", "count", "results"}
    assert data["limit"] == 50 and data["count"] == 2
    hit = next(r for r in data["results"] if r["id"] == "doi:10.1000/test1")
    assert {"id", "doi", "title", "journal", "pub_date", "url", "kind", "source", "snippet", "rank", "instruments", "companies"} <= set(hit)
    xen = next(c for c in hit["instruments"] if c["entity_id"] == "txg-xenium")
    assert xen["name"] == "Xenium" and xen["ticker"] == "TXG" and sorted(xen["sections"]) == ["abstract", "title"]
    assert hit["companies"][0]["entity_id"] == "10x-genomics"
    assert "[" in hit["snippet"]  # FTS5 highlight markers


def test_search_since_and_sections(served):
    _, data = _json(served["base"], "/search?q=Xenium&since=2026-10-01")
    assert [r["id"] for r in data["results"]] == ["doi:10.1000/test1"]
    _, data = _json(served["base"], "/search?q=Xenium&since=30d")
    assert data["since"] == "2026-09-17" and data["count"] == 1
    _, data = _json(served["base"], "/search?q=NovaSeq&in=methods")
    assert data["sections"] == ["methods"] and [r["id"] for r in data["results"]] == ["doi:10.1000/test1"]
    _, data = _json(served["base"], "/search?q=methods:NovaSeq")  # user-written FTS5 column filter
    assert data["count"] == 1
    _, data = _json(served["base"], "/search?q=Olink&in=methods")
    assert data["count"] == 0
    _, data = _json(served["base"], "/search?q=Xenium&mentions=0")
    assert "instruments" not in data["results"][0]


def test_search_parameter_bounds(served):
    base = served["base"]
    status, data = _json(base, "/search")
    assert status == 400 and "q is required" in data["error"]
    status, data = _json(base, "/search?q=Xenium&limit=99999")
    assert status == 200 and data["limit"] == api.MAX_LIMIT
    status, data = _json(base, "/search?q=Xenium&limit=abc")
    assert status == 400 and data["status"] == 400
    status, data = _json(base, "/search?q=Xenium&limit=0")
    assert status == 400
    status, data = _json(base, "/search?q=Xenium&since=yesterday")
    assert status == 400 and "since" in data["error"]
    status, data = _json(base, "/search?q=Xenium&since=2026-13-45")
    assert status == 400
    status, data = _json(base, "/search?q=Xenium&in=methods,nope")
    assert status == 400 and "nope" in data["error"]
    status, data = _json(base, "/search?q=" + "x" * (api.MAX_QUERY_LEN + 1))
    assert status == 400
    status, data = _json(base, "/search?q=weird-query:with(chars")  # phrase fallback, not a 500
    assert status == 200 and data["count"] == 0


# ---- /papers/{id} ----------------------------------------------------------------------
def test_paper_detail(served):
    status, data = _json(served["base"], "/papers/doi%3A10.1000%2Ftest1")
    assert status == 200
    assert data["id"] == "doi:10.1000/test1" and data["fulltext_available"] is True
    assert data["authors"][0]["name"] == "A. Author" and data["sections"]["methods"].startswith("Sequenced")
    assert "authors_json" not in data and "body" not in data["sections"]
    assert {m["entity_id"] for m in data["mentions"]} == {"txg-xenium", "ilmn-novaseq-x", "10x-genomics"}
    assert data["score"]["final"] == 6.5 and data["score"]["llm"] == {"novelty": 4}
    assert data["tracked_authors"] == [{"author_name": "A. Author", "subgroup": "spatial", "matched_by": "name"}]
    status, data = _json(served["base"], "/papers/doi:10.1000/test1")  # unencoded slash works too
    assert status == 200
    status, data = _json(served["base"], "/papers?id=doi:10.1000/test2")
    assert status == 200 and data["score"]["llm"] is None
    status, data = _json(served["base"], "/papers/doi:10.1000/nope")
    assert status == 404 and data["error"] == "paper not found"


# ---- /entities -------------------------------------------------------------------------
def test_entity_papers(served):
    status, data = _json(served["base"], "/entities/instrument/txg-xenium/papers")
    assert status == 200
    assert data["entity"] == {"name": "Xenium", "vendor": "10x Genomics", "ticker": "TXG", "category": "spatial"}
    assert [r["id"] for r in data["results"]] == ["doi:10.1000/test1", "doi:10.1000/test3"]  # one row per paper, newest first
    assert sorted(data["results"][0]["sections"]) == ["abstract", "title"] and data["results"][0]["mentions"] == 2
    _, data = _json(served["base"], "/entities/instrument/txg-xenium/papers?since=90d&limit=1")
    assert data["count"] == 1 and data["limit"] == 1 and data["results"][0]["id"] == "doi:10.1000/test1"
    _, data = _json(served["base"], "/entities/company/txg-xenium/papers")  # wrong kind: empty, not an error
    assert data["entity"] is None and data["count"] == 0
    status, data = _json(served["base"], "/entities/vendor/x/papers")
    assert status == 404
    status, data = _json(served["base"], "/entities/instrument/bad%20id!/papers")
    assert status == 400
    status, data = _json(served["base"], "/entities/instrument/txg-xenium/papers?limit=5000")
    assert data["limit"] == api.MAX_LIMIT


# ---- count tables, top, news, stats ----------------------------------------------------
def test_instrument_and_company_counts(served):
    _, data = _json(served["base"], "/instruments?since=2026-10-10&until=2026-10-17")
    assert set(data) == {"since", "until", "limit", "count", "results"}
    assert {r["entity_id"] for r in data["results"]} == {"txg-xenium", "ilmn-novaseq-x"}
    row = next(r for r in data["results"] if r["entity_id"] == "txg-xenium")
    assert row["papers"] == 1 and row["mentions"] == 2 and sorted(row["sections"]) == ["abstract", "title"]
    _, data = _json(served["base"], "/instruments")  # defaults: last 7 days ending today
    assert data["since"] == "2026-10-10" and data["until"] == "2026-10-17"
    _, data = _json(served["base"], "/companies?since=2026-10-10&until=2026-10-17")
    assert data["results"][0]["entity_id"] == "10x-genomics" and data["results"][0]["as_author"] == 1
    status, data = _json(served["base"], "/companies?since=2026-10-17&until=2026-10-10")
    assert status == 400 and "since" in data["error"]
    status, data = _json(served["base"], "/instruments?until=soon")
    assert status == 400
    _, data = _json(served["base"], "/instruments?limit=10000")
    assert data["limit"] == api.MAX_COUNT_LIMIT


def test_archive_top_news_stats(served):
    _, data = _json(served["base"], "/archive/instruments?since=2026-10-10&until=2026-10-17")
    assert data["results"][0]["instrument_model"] == "Illumina NovaSeq X Plus" and data["results"][0]["experiments"] == 3
    assert data["results"][0]["archives"] == ["sra"]
    _, data = _json(served["base"], "/top?since=2026-10-10&until=2026-10-17&limit=1")
    assert data["count"] == 1 and data["results"][0]["id"] == "doi:10.1000/test1" and data["results"][0]["final"] == 6.5
    assert data["results"][0]["mentions"] and data["results"][0]["authors"][0]["name"] == "A. Author"
    status, data = _json(served["base"], "/top?limit=100000")
    assert status == 200 and data["limit"] == api.MAX_LIMIT
    _, data = _json(served["base"], "/news?since=2026-10-10")
    assert data["count"] == 2 and all("companies_json" not in r for r in data["results"])
    _, data = _json(served["base"], "/news?since=2026-10-10&company=vendor-a")
    assert data["count"] == 1 and data["results"][0]["id"] == "n1" and data["results"][0]["companies"] == ["vendor-a"]
    status, data = _json(served["base"], "/news?company=drop%20table")
    assert status == 400
    _, data = _json(served["base"], "/stats")
    assert data["counts"]["papers"] == 3 and data["counts"]["archive_records"] == 4
    assert data["pub_date_range"] == {"min": "2025-01-05", "max": "2026-10-14"}
    assert data["runs"][0]["counts"] == {"papers": 3} and data["database"]["file"] == "radar.sqlite"
    _, data = _json(served["base"], "/api")
    assert {e["path"] for e in data["endpoints"]} >= {"/search", "/papers/{id}", "/stats", "/digest/latest"}


# ---- digest, HTML page, errors ---------------------------------------------------------
def test_digest_and_html_page(served):
    status, headers, body = _get(served["base"], "/digest/latest")
    assert status == 200 and headers["Content-Type"].startswith("text/markdown")
    assert body.decode("utf-8").startswith("# Science literature radar")
    status, headers, body = _get(served["base"], "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    html = body.decode("utf-8")
    assert '<input id="q"' in html and "/search?" in html and "prefers-color-scheme: dark" in html
    assert "Content-Security-Policy" in headers and "'nonce-" in headers["Content-Security-Policy"]
    nonce = headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in html and "__NONCE__" not in html
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, data = _json(served["base"], "/nope")
    assert status == 404 and "see /api" in data["error"]


def test_digest_missing_is_404(tmp_path):
    db = tmp_path / "r.sqlite"
    Store(db).close()
    app = api.RadarAPI(db)  # no digest dir configured
    resp = app.dispatch("GET", "/digest/latest")
    assert resp.status == 404 and "latest.md" in resp.json()["error"]


# ---- read-only behaviour ---------------------------------------------------------------
def test_read_only(served):
    status, headers, body = _get(served["base"], "/search?q=Xenium", method="POST")
    assert status == 405 and headers["Allow"] == "GET, HEAD, OPTIONS"
    status, headers, body = _get(served["base"], "/papers/doi:10.1000/test1", method="DELETE")
    assert status == 405
    status, headers, body = _get(served["base"], "/search?q=Xenium", method="HEAD")
    assert status == 200 and body == b"" and int(headers["Content-Length"]) > 0
    ro = served["app"].open()
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|query_only"):
        ro.conn.execute("INSERT INTO runs (started) VALUES ('x')")
    with pytest.raises(sqlite3.OperationalError):
        ro.conn.execute("DELETE FROM papers")
    ro.close()
    assert _fingerprint(served["db"]) == served["before"]


def test_missing_database_fails_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        api.RadarAPI(tmp_path / "missing.sqlite")


def test_cors_and_options(tmp_path):
    db = tmp_path / "r.sqlite"
    Store(db).close()
    app = api.RadarAPI(db, cors_origin="https://friend.example")
    server, _thread = api.start_in_thread(app)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, headers, _ = _get(base, "/stats")
        assert headers["Access-Control-Allow-Origin"] == "https://friend.example" and headers["Vary"] == "Origin"
        status, headers, body = _get(base, "/stats", method="OPTIONS")
        assert status == 204 and body == b"" and headers["Access-Control-Allow-Methods"] == "GET, HEAD, OPTIONS"
    finally:
        server.shutdown()
        server.server_close()


def test_no_cors_header_by_default(served):
    _status, headers, _ = _get(served["base"], "/stats")
    assert "Access-Control-Allow-Origin" not in headers


def test_log_line_redacts_emails(caplog, served):
    assert api.redact_path("/search?q=mail%20me%20at%20someone@example.org&since=7d") == "/search?q=mail me at [email]&since=7d"
    assert api.redact_path("/search?q=a%40b.co") == "/search?q=[email]"
    assert api.redact_path("/search?q=Xenium%20NovaSeq&since=7d") == "/search?q=Xenium%20NovaSeq&since=7d"  # untouched when clean
    assert api.redact_path("/search?q=" + "x" * 500).endswith("…")
    with caplog.at_level("INFO", logger="litradar.api"):
        _get(served["base"], "/search?q=Xenium%20someone@example.org")
    line = next(r.getMessage() for r in caplog.records if r.name == "litradar.api" and "/search" in r.getMessage())
    assert "@" not in line and "[email]" in line and " 200 " in line


# ---- client stub -----------------------------------------------------------------------
def test_radar_client(served):
    client = api.RadarClient(served["base"])
    hits = client.search("Xenium", since="2026-10-01", sections=["title", "abstract"], limit=5)
    assert hits["count"] == 1 and hits["sections"] == ["title", "abstract"] and hits["limit"] == 5
    assert client.paper("doi:10.1000/test1")["score"]["final"] == 6.5
    assert client.entity_papers("instrument", "txg-xenium")["count"] == 2
    assert client.instruments(since="2026-10-10", until="2026-10-17")["count"] == 2
    assert client.companies(since="2026-10-10", until="2026-10-17")["results"][0]["entity_id"] == "10x-genomics"
    assert client.archive_instruments(since="2026-10-10", until="2026-10-17")["results"][0]["experiments"] == 3
    assert client.top(since="2026-10-10", until="2026-10-17", limit=1)["count"] == 1
    assert client.news(since="2026-10-10", company="vendor-a")["count"] == 1
    assert client.stats()["counts"]["papers"] == 3
    assert client.digest_latest().startswith("# Science literature radar")
    assert client.endpoints()["endpoints"]
    with pytest.raises(api.RadarAPIError) as exc:
        client.paper("doi:10.1000/missing")
    assert exc.value.status == 404 and "paper not found" in str(exc.value)
    with pytest.raises(api.RadarAPIError) as exc:
        client.search("Xenium", since="never")
    assert exc.value.status == 400


# ---- parameter helpers and the CLI plugin ------------------------------------------------
def test_param_helpers():
    assert api.parse_date("90d", "since", TODAY) == "2026-07-19"
    assert api.parse_date("", "since") is None and api.parse_date(None, "since") is None
    with pytest.raises(api.ApiError):
        api.parse_date("2026-02-30", "since")
    assert api.parse_limit(None, 50, 200) == 50 and api.parse_limit("1000", 50, 200) == 200
    assert api.parse_sections("methods, COI,methods") == ("methods", "coi")
    with pytest.raises(api.ApiError):
        api.parse_slug("x; drop", "company")


def test_serve_command_is_registered_and_datasette_hint(monkeypatch, capsys, tmp_path):
    import argparse

    from litradar.commands import serve as serve_cmd

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    serve_cmd.register(sub)
    args = parser.parse_args(["serve", "--datasette", "--db", str(tmp_path / "r.sqlite")])
    args.config_dir = None
    Store(tmp_path / "r.sqlite").close()
    monkeypatch.setattr(serve_cmd, "datasette_available", lambda: False)
    assert serve_cmd.cmd_serve(args) == 1
    assert "pip install" in capsys.readouterr().err
    args = parser.parse_args(["serve", "--db", str(tmp_path / "absent.sqlite")])
    args.config_dir = None
    assert serve_cmd.cmd_serve(args) == 2
    cmd = serve_cmd.datasette_command(Path("data/radar.sqlite"), Path("datasette/metadata.yaml"), "127.0.0.1", 8001)
    assert cmd[1:5] == ["-m", "datasette", "-i", "data/radar.sqlite"]  # -i takes the path (it is --immutable PATH)
    assert cmd[cmd.index("--setting") + 1 : cmd.index("--setting") + 3] == ["sql_time_limit_ms", "5000"]
    live = serve_cmd.datasette_command(Path("x"), Path("y"), "h", 1, immutable=False)
    assert "-i" not in live and live[3] == "x"
    assert os.path.exists(serve_cmd.DATASETTE_METADATA)
