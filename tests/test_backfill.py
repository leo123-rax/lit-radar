"""OpenAlex backfill (API route + snapshot route) tested against canned payloads and a tiny gz fixture; no network."""
import gzip
import json
from pathlib import Path

import pytest

from litradar import backfill, cli
from litradar.sources.openalex import OpenAlexBudgetExhausted
from litradar.store import Store

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "openalex_snapshot"

GROUPS = [
    {"id": "single_cell", "openalex_topics": ["T11289", "T10222"], "keywords": ["single-cell RNA", "scRNA-seq"]},
    {"id": "spatial", "openalex_topics": ["T11289"], "keywords": ["spatial transcriptomics", "Xenium"]},
]


class FakeResp:
    def __init__(self, status, payload, raw=None):
        self.status_code = status
        self._payload = payload
        self._raw = raw
        self.url = "https://api.openalex.org/works"
        self.headers = {}

    def json(self):
        if self._raw is not None:
            return json.loads(self._raw)  # raises ValueError for malformed bodies
        return self._payload


class FakeHttp:
    """Mimics litradar.http.Http.get for the OpenAlex pager: one queued response per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.stats = {"requests": 0, "failures": 0}

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,)):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        self.stats["requests"] += 1
        if not self.responses:
            self.stats["failures"] += 1
            return None
        r = self.responses.pop(0)
        if r is None:
            self.stats["failures"] += 1
        return r


def work(oa_id, doi, title, date, topic="T11289", countries=(("CN", "Tsinghua University"), ("US", "Example Univ")), abstract=None, language="en"):
    authorships = []
    for i, (cc, inst) in enumerate(countries):
        authorships.append({
            "author": {"id": f"https://openalex.org/A{oa_id[1:]}{i}", "display_name": f"Author {i}", "orcid": None},
            "author_position": "first" if i == 0 else "middle",
            "countries": [cc],
            "institutions": [{"id": "https://openalex.org/I1", "display_name": inst, "ror": "https://ror.org/x", "country_code": cc, "type": "education"}],
            "raw_affiliation_strings": [inst],
            "is_corresponding": i == 0,
        })
    inv = None
    if abstract:
        inv = {}
        for pos, tok in enumerate(abstract.split()):
            inv.setdefault(tok, []).append(pos)
    return {
        "id": f"https://openalex.org/{oa_id}", "doi": f"https://doi.org/{doi}" if doi else None, "title": title,
        "publication_date": date, "type": "article", "language": language,
        "primary_location": {"source": {"display_name": "Nature Methods", "type": "journal"}, "landing_page_url": "https://x", "license": "cc-by"},
        "authorships": authorships, "abstract_inverted_index": inv,
        "topics": [{"id": f"https://openalex.org/{topic}", "display_name": "Topic"}],
        "primary_topic": {"id": f"https://openalex.org/{topic}", "display_name": "Topic", "score": 0.9},
        "cited_by_count": 3, "funders": [{"id": "https://openalex.org/F1", "display_name": "National Natural Science Foundation of China"}],
    }


def page(results, next_cursor=None, count=None):
    return {"meta": {"count": count if count is not None else len(results), "next_cursor": next_cursor, "per_page": 200}, "results": results}


BUDGET_429 = {
    "error": "Rate limit exceeded",
    "message": "Insufficient budget. This request has no API key, so it counts against the free daily budget shared by everyone on your network's IP address, and that budget is used up ($0 remaining; resets at midnight UTC).",
    "retryAfter": 13878, "costUsd": 0.001, "dailyRemainingUsd": 0,
}


# ---------------------------------------------------------------- helpers ----------------------------------------
def test_months_and_bounds():
    assert backfill.months("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert backfill.months("2026-10", "2026-10") == ["2026-10"]
    assert backfill.month_bounds("2024-02") == ("2024-02-01", "2024-02-29")
    assert backfill.month_bounds("2026-12") == ("2026-12-01", "2026-12-31")
    with pytest.raises(backfill.BackfillError):
        backfill.months("2026-03", "2026-01")
    with pytest.raises(backfill.BackfillError):
        backfill.parse_month("2026-13")
    with pytest.raises(backfill.BackfillError):
        backfill.parse_month("2026")


def test_subgroup_filters_match_documented_names():
    flt = backfill.subgroup_filters(GROUPS[0], "2024-01", country="cn")
    assert flt["primary_topic.id"] == "T11289|T10222"
    assert flt["from_publication_date"] == "2024-01-01" and flt["to_publication_date"] == "2024-01-31"
    assert flt["title_and_abstract.search"] == '"single-cell RNA" OR "scRNA-seq"'
    assert flt["institutions.country_code"] == "cn"
    assert "institutions.country_code" not in backfill.subgroup_filters(GROUPS[0], "2024-01")
    assert "grants" not in backfill.BACKFILL_SELECT.split(",") and "funders" in backfill.BACKFILL_SELECT.split(",")
    with pytest.raises(backfill.BackfillError):
        backfill.subgroup_filters({"id": "x", "openalex_topics": []}, "2024-01")


def test_keyword_expression_dedupes_and_quotes():
    assert backfill.keyword_expression(["a b", 'c"d', "a b", ""]) == '"a b" OR "c d"'
    assert backfill.keyword_expression([]) == ""
    assert backfill.keyword_expression([str(i) for i in range(100)]).count(" OR ") == backfill.MAX_KEYWORDS - 1


def test_country_codes_and_tagging(tmp_path):
    w = work("W1", "10.1000/t1", "A single-cell RNA atlas", "2024-01-05")
    w["authorships"].append({"author": {"display_name": "No inst"}, "institutions": [], "countries": [], "author_position": "last"})
    allc, first = backfill.country_codes(w)
    assert allc == ["CN", "US"] and first == ["CN"]
    st = Store(tmp_path / "t.sqlite")
    assert backfill.upsert_work(st, w, ["single_cell"], "api") is True
    row = st.get_paper("doi:10.1000/t1")
    extra = json.loads(row["extra_json"])
    assert extra["countries"] == ["CN", "US"] and extra["first_author_countries"] == ["CN"] and extra["language"] == "en"
    assert extra["subgroups"] == ["single_cell"] and extra["backfill_source"] == "api" and extra["primary_topic"] == "T11289"
    assert json.loads(row["sections_json"])["funding"] == "National Natural Science Foundation of China"
    # a second subgroup upserting the same work keeps the first tag (extra_json is otherwise replaced wholesale)
    assert backfill.upsert_work(st, w, ["spatial"], "snapshot") is False
    extra = json.loads(st.get_paper("doi:10.1000/t1")["extra_json"])
    assert extra["subgroups"] == ["single_cell", "spatial"]
    st.close()


# ---------------------------------------------------------------- API route --------------------------------------
def test_api_run_pages_upserts_and_records_progress(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    w1 = work("W1", "10.1000/a1", "A single-cell RNA atlas of liver", "2024-01-05")
    w2 = work("W2", "10.1000/a2", "scRNA-seq of kidney", "2024-01-20", countries=(("DE", "MPI"),))
    w3 = work("W3", None, "Xenium spatial transcriptomics map", "2024-01-11", countries=(("JP", "RIKEN"),))
    http = FakeHttp([
        FakeResp(200, page([w1], count=3)),                 # probe (per-page=1)
        FakeResp(200, page([w1, w2], next_cursor="c2", count=3)),
        FakeResp(200, page([w3], next_cursor="c3", count=3)),
        FakeResp(200, page([], next_cursor=None, count=3)),  # pagination stop: empty page
        FakeResp(200, page([], count=0)),                    # spatial, no works this month
    ])
    report = backfill.run_api(st, http, GROUPS, "2024-01", "2024-01", mailto="me@example.org", api_key="k")
    assert report["stopped"] is None and report["units"] == 2 and report["done"] == 2 and report["skipped"] == 0
    assert report["papers_fetched"] == 3 and report["papers_new"] == 3 and report["papers_updated"] == 0
    assert st.counts()["papers"] == 3
    assert st.get_paper("openalex:W3") is not None  # no DOI -> openalex:<id>
    p = http.calls[1]["params"]
    assert "primary_topic.id:T11289|T10222" in p["filter"] and "from_publication_date:2024-01-01,to_publication_date:2024-01-31" in p["filter"]
    assert 'title_and_abstract.search:"single-cell RNA" OR "scRNA-seq"' in p["filter"]
    assert p["select"] == backfill.BACKFILL_SELECT and p["mailto"] == "me@example.org" and p["per-page"] == 200
    assert http.calls[1]["headers"]["Authorization"] == "Bearer k"
    assert http.calls[2]["params"]["cursor"] == "c2"
    assert http.calls[0]["params"]["per-page"] == 1  # the probe
    rows = st.conn.execute("SELECT subgroup, month, country, fetched FROM backfill_progress ORDER BY subgroup").fetchall()
    assert rows == [("single_cell", "2024-01", "", 3), ("spatial", "2024-01", "", 0)]
    # resume: everything is done, no HTTP call is made
    http2 = FakeHttp([])
    report2 = backfill.run_api(st, http2, GROUPS, "2024-01", "2024-01")
    assert report2["skipped"] == 2 and report2["done"] == 0 and http2.calls == []
    summary = backfill.progress_summary(st)
    assert [(r["subgroup"], r["units"], r["fetched"]) for r in summary] == [("single_cell", 1, 3), ("spatial", 1, 0)]
    st.close()


def test_api_run_with_countries_adds_filter_and_separate_units(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    http = FakeHttp([FakeResp(200, page([], count=0))] * 5)
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-02", countries=["cn", "US"], do_probe=False)
    assert report["units"] == 4 and report["done"] == 4
    filters = [c["params"]["filter"] for c in http.calls]
    assert sum("institutions.country_code:cn" in f for f in filters) == 2 and sum("institutions.country_code:us" in f for f in filters) == 2
    rows = st.conn.execute("SELECT month, country FROM backfill_progress ORDER BY month, country").fetchall()
    assert rows == [("2024-01", "CN"), ("2024-01", "US"), ("2024-02", "CN"), ("2024-02", "US")]
    st.close()


def test_api_budget_exhausted_saves_progress_and_returns_cleanly(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    w1 = work("W1", "10.1000/b1", "single-cell RNA study", "2024-01-05")
    http = FakeHttp([
        FakeResp(200, page([w1], count=1)),   # probe
        FakeResp(200, page([w1], count=1)),   # single_cell 2024-01 complete
        FakeResp(429, BUDGET_429),            # spatial 2024-01: budget gone
    ])
    report = backfill.run_api(st, http, GROUPS, "2024-01", "2024-02")
    assert report["stopped"] == "budget" and "Insufficient budget" in report["message"] and "Re-run" in report["message"]
    assert report["done"] == 1 and report["papers_new"] == 1
    rows = st.conn.execute("SELECT subgroup, month FROM backfill_progress").fetchall()
    assert rows == [("single_cell", "2024-01")]  # the interrupted unit is NOT marked done
    assert st.counts()["papers"] == 1
    # resuming continues from the interrupted unit
    http2 = FakeHttp([FakeResp(200, page([], count=0))] * 3)
    report2 = backfill.run_api(st, http2, GROUPS, "2024-01", "2024-02", do_probe=False)
    assert report2["skipped"] == 1 and report2["done"] == 3 and report2["stopped"] is None
    st.close()


def test_api_budget_exhausted_mid_month_keeps_papers(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    w1 = work("W1", "10.1000/c1", "single-cell RNA study", "2024-01-05")
    http = FakeHttp([
        FakeResp(200, page([w1], next_cursor="c2", count=400)),  # first page ok
        FakeResp(403, {"error": "forbidden", "message": "Insufficient budget"}),
    ])
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01", do_probe=False)
    assert report["stopped"] == "budget" and report["papers_fetched"] == 1 and report["done"] == 0
    assert st.counts()["papers"] == 1 and st.conn.execute("SELECT COUNT(*) FROM backfill_progress").fetchone()[0] == 0
    st.close()


def test_api_probe_reports_http_400_with_message(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    http = FakeHttp([FakeResp(400, {"error": "Invalid query parameters error.", "message": "grants is not a valid select field"})])
    with pytest.raises(backfill.BackfillError) as exc:
        backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01")
    assert "HTTP 400" in str(exc.value) and "grants is not a valid select field" in str(exc.value)
    assert st.conn.execute("SELECT COUNT(*) FROM backfill_progress").fetchone()[0] == 0
    st.close()


def test_probe_raises_budget_error_on_429():
    http = FakeHttp([FakeResp(429, BUDGET_429)])
    with pytest.raises(OpenAlexBudgetExhausted):
        backfill.probe(http, {"primary_topic.id": "T11289"})
    assert backfill.probe(FakeHttp([None]), {"primary_topic.id": "T11289"}) is None
    assert backfill.probe(FakeHttp([FakeResp(200, None, raw="<html>not json")]), {"primary_topic.id": "T11289"}) is None


def test_api_malformed_and_failed_responses_do_not_mark_done(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    # malformed JSON body: the pager stops, nothing fetched, the unit is left for the next run
    http = FakeHttp([None])  # transport failure -> None, failures += 1
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01", do_probe=False)
    assert report["done"] == 0 and report["errors"] and st.conn.execute("SELECT COUNT(*) FROM backfill_progress").fetchone()[0] == 0
    # a 200 with a body that is not JSON: works() breaks; no failure counted by the client, so it is recorded as an empty month
    http = FakeHttp([FakeResp(200, None, raw="<html>maintenance</html>")])
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01", do_probe=False)
    assert report["papers_fetched"] == 0 and report["stopped"] is None
    # payload without 'results' key
    http = FakeHttp([FakeResp(200, {"meta": {"count": 0}})])
    report = backfill.run_api(st, http, GROUPS[:1], "2024-02", "2024-02", do_probe=False)
    assert report["done"] == 1 and report["papers_fetched"] == 0
    st.close()


def test_api_max_per_month_cap_is_reported(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    ws = [work(f"W{i}", f"10.1000/d{i}", "single-cell RNA paper", "2024-01-05") for i in range(3)]
    http = FakeHttp([FakeResp(200, page(ws, next_cursor="more", count=5000)), FakeResp(200, page([], count=5000))])
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01", max_per_month=3, do_probe=False)
    assert report["papers_fetched"] == 3 and report["capped_units"] == ["single_cell 2024-01 all"]
    assert len(http.calls) == 1  # the pager stops once max_records is reached
    st.close()


def test_select_subgroups_and_plan():
    assert [g["id"] for g in backfill.select_subgroups(GROUPS, None)] == ["single_cell", "spatial"]
    assert [g["id"] for g in backfill.select_subgroups(GROUPS, ["spatial"])] == ["spatial"]
    with pytest.raises(backfill.BackfillError):
        backfill.select_subgroups(GROUPS, ["nope"])
    assert backfill.plan(GROUPS[:1], "2024-01", "2024-02", ["cn"]) == [("single_cell", "2024-01", "CN"), ("single_cell", "2024-02", "CN")]


# ---------------------------------------------------------------- snapshot route ---------------------------------
def test_snapshot_fixture_import_filters_and_resumes(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    files = backfill.snapshot_files(FIXTURE_DIR)
    names = [p.name for p in files]
    assert "part_0000.gz" in names and "part_0000.jsonl" in names and "manifest.json" not in names and "deleted_ids.csv.gz" not in names
    report = backfill.run_snapshot(st, FIXTURE_DIR, GROUPS)
    assert report["files"] == 2 and report["files_done"] == 2 and report["errors"] == []
    assert report["lines"] == 6 and report["malformed"] == 1
    # kept: W1 (single_cell CN+US), W3 (scRNA-seq, 2019), W5 (spatial DE); dropped: W2 (topic ok, no keyword), W4 (other topic)
    assert report["matched"] == 3 and report["papers_new"] == 3
    ids = {r[0] for r in st.conn.execute("SELECT id FROM papers")}
    assert ids == {"doi:10.1000/snap1", "doi:10.1000/snap3", "doi:10.1000/snap5"}
    e1 = json.loads(st.get_paper("doi:10.1000/snap1")["extra_json"])
    assert e1["countries"] == ["CN", "US"] and e1["subgroups"] == ["single_cell"] and e1["backfill_source"] == "snapshot" and e1["language"] == "zh"
    e5 = json.loads(st.get_paper("doi:10.1000/snap5")["extra_json"])
    assert e5["subgroups"] == ["spatial"] and e5["countries"] == ["DE"]
    rows = st.conn.execute("SELECT subgroup, month, fetched FROM backfill_progress ORDER BY month").fetchall()
    assert [r[0] for r in rows] == [backfill.SNAPSHOT_PROGRESS_SUBGROUP] * 2 and sorted(r[2] for r in rows) == [1, 2]
    # re-run skips both files
    report2 = backfill.run_snapshot(st, FIXTURE_DIR, GROUPS)
    assert report2["files_skipped"] == 2 and report2["files_done"] == 0 and report2["matched"] == 0
    st.close()


def test_snapshot_country_and_window_filters(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    report = backfill.run_snapshot(st, FIXTURE_DIR, GROUPS, countries=["CN"])
    assert report["matched"] == 1 and st.get_paper("doi:10.1000/snap1") is not None
    st.close()
    st = Store(tmp_path / "t2.sqlite")
    report = backfill.run_snapshot(st, FIXTURE_DIR, GROUPS, from_month="2024-01", to_month="2025-12")
    assert {r[0] for r in st.conn.execute("SELECT id FROM papers")} == {"doi:10.1000/snap1", "doi:10.1000/snap5"}  # 2019 paper excluded
    assert report["matched"] == 2
    st.close()


def test_snapshot_matcher_keyword_boundaries():
    m = backfill.SnapshotMatcher(GROUPS)
    base = work("W9", "10.1000/k", "", "2024-01-01")
    base["title"] = "Single cell RNA profiling"  # space instead of hyphen still matches
    assert m.match(base) == ["single_cell"]
    base["title"] = "Xeniumlike probes"  # no word boundary -> no spatial match
    assert m.match(base) == []
    base["title"] = "No keywords here"
    base["abstract_inverted_index"] = {"spatial": [0], "transcriptomics": [1], "and": [2], "scRNA-seq": [3]}
    assert m.match(base) == ["single_cell", "spatial"]
    base["primary_topic"] = {"id": "https://openalex.org/T99999"}
    assert m.match(base) == []
    assert m.match({"title": "x"}) == []


def test_snapshot_handles_empty_truncated_and_missing(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    snap = tmp_path / "snap" / "data" / "jsonl" / "works" / "updated_date=2026-10-14"
    snap.mkdir(parents=True)
    with gzip.open(snap / "part_0000.gz", "wt", encoding="utf-8") as fh:
        fh.write("")  # empty file
    good = gzip.compress((json.dumps(work("W7", "10.1000/t7", "scRNA-seq atlas", "2025-02-02")) + "\n").encode("utf-8") * 20)
    (snap / "part_0001.gz").write_bytes(good[: len(good) // 2])  # truncated download
    (snap / "part_0002.gz").write_bytes(b"not gzip at all")
    report = backfill.run_snapshot(st, tmp_path / "snap", GROUPS)
    assert report["files"] == 3 and report["files_done"] == 1 and len(report["errors"]) == 2
    done = [r[0] for r in st.conn.execute("SELECT month FROM backfill_progress")]
    assert done == ["data/jsonl/works/updated_date=2026-10-14/part_0000.gz"]
    with pytest.raises(backfill.BackfillError):
        backfill.run_snapshot(st, tmp_path / "does-not-exist", GROUPS)
    with pytest.raises(backfill.BackfillError):
        backfill.run_snapshot(st, tmp_path / "snap", [{"id": "x", "openalex_topics": []}])
    st.close()


# ---------------------------------------------------------------- CLI --------------------------------------------
def _config_dir(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "subgroups.yaml").write_text(json.dumps({"subgroups": GROUPS}), encoding="utf-8")  # JSON is valid YAML
    return cfg


def test_cli_backfill_budget_message_exits_cleanly(tmp_path, monkeypatch, capsys):
    cfg = _config_dir(tmp_path)
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    fake = FakeHttp([FakeResp(429, BUDGET_429)])
    monkeypatch.setattr(cli, "Http", lambda **kw: fake)
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--from", "2024-01", "--to", "2024-01", "--subgroup", "single_cell"])
    out = capsys.readouterr().out
    assert rc == 0 and '"stopped": "budget"' in out and "Re-run the same command to resume" in out
    assert len(fake.calls) == 1


def test_cli_backfill_dry_run_status_and_errors(tmp_path, monkeypatch, capsys):
    cfg = _config_dir(tmp_path)
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "db.sqlite"))
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--from", "2024-01", "--to", "2024-02", "--country", "CN", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "4 unit(s)" in out and "single_cell" in out and "CN" in out
    assert cli.main(["--config-dir", str(cfg), "backfill", "--from", "2024-01", "--to", "2024-01", "--subgroup", "nope"]) == 2
    assert "unknown subgroup" in capsys.readouterr().err
    assert cli.main(["--config-dir", str(cfg), "backfill", "--subgroup", "spatial"]) == 2  # no window, no snapshot dir
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--snapshot-dir", str(FIXTURE_DIR), "--country", "DE"])
    out = capsys.readouterr().out
    assert rc == 0 and '"matched": 1' in out
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--status"])
    out = capsys.readouterr().out
    assert rc == 0 and backfill.SNAPSHOT_PROGRESS_SUBGROUP in out
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--status", "--json"])
    assert rc == 0 and json.loads(capsys.readouterr().out)[0]["units"] == 2
