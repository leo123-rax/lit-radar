"""OpenAlex backfill (API route + snapshot route) tested against canned payloads and a tiny gz fixture; no network."""
import gzip
import io
import json
import zlib
from pathlib import Path

import pytest

from litradar import backfill, cli
from litradar.sources import openalex
from litradar.sources.openalex import OpenAlexBudgetExhausted
from litradar.store import Store

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "openalex_snapshot"
SEARCH = backfill.SEARCH_PARAM

GROUPS = [
    {"id": "single_cell", "openalex_topics": ["T11289", "T10222"], "keywords": ["single-cell RNA", "scRNA-seq"]},
    {"id": "spatial", "openalex_topics": ["T11289"], "keywords": ["spatial transcriptomics", "Xenium"]},
]


class FakeResp:
    def __init__(self, status, payload, raw=None, headers=None):
        self.status_code = status
        self._payload = payload
        self._raw = raw
        self.url = "https://api.openalex.org/works"
        self.headers = headers or {}

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


def probes(count_all, count_one, count_topic):
    """The three per-page=1 answers run_api needs before its first unit: OR-ed keywords, first keyword alone, topics only."""
    return [FakeResp(200, page([], count=count_all)), FakeResp(200, page([], count=count_one)), FakeResp(200, page([], count=count_topic))]


BUDGET_429 = {
    "error": "Rate limit exceeded",
    "message": "Insufficient budget. This request has no API key, so it counts against the free daily budget shared by everyone on your network's IP address, and that budget is used up ($0 remaining; resets at midnight UTC).",
    "retryAfter": 13878, "costUsd": 0.001, "dailyRemainingUsd": 0,
}
PLAIN_429 = {"error": "Rate limit exceeded", "message": "Rate limit exceeded: 10 requests per second", "retryAfter": 1}


def progress_rows(st, cols="subgroup, month, country, fetched"):
    return st.conn.execute(f"SELECT {cols} FROM backfill_progress ORDER BY subgroup, month, country").fetchall()


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


def test_subgroup_filters_use_documented_names_and_search_parameter():
    flt, params = backfill.subgroup_filters(GROUPS[0], "2024-01", country="cn")
    assert flt == {"primary_topic.id": "T11289|T10222", "from_publication_date": "2024-01-01", "to_publication_date": "2024-01-31", "institutions.country_code": "cn"}
    assert params == {"search.title_and_abstract": '"single-cell RNA" OR "scRNA-seq"'}
    assert backfill.SEARCH_FILTER not in flt  # the deprecated filter form is not sent
    flt, params = backfill.subgroup_filters(GROUPS[0], "2024-01")
    assert "institutions.country_code" not in flt and SEARCH in params
    assert backfill.subgroup_filters(GROUPS[0], "2024-01", topics_only=True)[1] == {}
    assert backfill.subgroup_filters({"id": "k", "openalex_topics": ["T1"]}, "2024-01")[1] == {}  # no keywords -> topic only
    assert "grants" not in backfill.BACKFILL_SELECT.split(",") and "funders" in backfill.BACKFILL_SELECT.split(",")
    with pytest.raises(backfill.BackfillError):
        backfill.subgroup_filters({"id": "x", "openalex_topics": []}, "2024-01")


def test_keyword_expression_dedupes_quotes_and_sanitises():
    assert backfill.keyword_expression(["a b", 'c"d', "a b", ""]) == '"a b" OR "c d"'
    assert backfill.keyword_expression([]) == ""
    assert backfill.keyword_expression(["foo, bar|baz:qux"]) == '"foo bar baz qux"'  # filter syntax characters removed
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
    assert extra["match"] == "topic+keyword"
    assert json.loads(row["sections_json"])["funding"] == "National Natural Science Foundation of China"
    # a second subgroup upserting the same work keeps the first tag and the stronger match mode
    assert backfill.upsert_work(st, w, ["spatial"], "snapshot", match="topic") is False
    extra = json.loads(st.get_paper("doi:10.1000/t1")["extra_json"])
    assert extra["subgroups"] == ["single_cell", "spatial"] and extra["match"] == "topic+keyword"
    st.close()


# ---------------------------------------------------------------- pager (sources/openalex.py) --------------------
def test_works_reports_completion_cap_and_errors():
    w = work("W1", "10.1000/p1", "x", "2024-01-01")
    state = {}
    out = list(openalex.works(FakeHttp([FakeResp(200, page([w], next_cursor="c2")), FakeResp(200, page([], next_cursor=None))]), {"a": "b"}, state=state))
    assert len(out) == 1 and state == {"complete": True}
    state = {}
    assert list(openalex.works(FakeHttp([FakeResp(200, page([], count=0))]), {"a": "b"}, state=state)) == [] and state.get("complete")
    state = {}
    list(openalex.works(FakeHttp([FakeResp(200, page([w] * 3, next_cursor="more", count=500))]), {"a": "b"}, max_records=3, state=state))
    assert state == {"capped": True}
    for responses, expected in (
        ([None], "transport failure"),
        ([FakeResp(200, None, raw="<html>maintenance</html>")], "non-JSON body (HTTP 200)"),
        ([FakeResp(200, {"error": "server", "message": "upstream timeout"})], "HTTP 200: upstream timeout"),
        ([FakeResp(500, {"error": "boom"})], "HTTP 500: boom"),
    ):
        state = {}
        assert list(openalex.works(FakeHttp(responses), {"a": "b"}, state=state)) == []
        assert state == {"error": expected}
    # extra query parameters travel with every page
    http = FakeHttp([FakeResp(200, page([w], next_cursor="c2")), FakeResp(200, page([]))])
    list(openalex.works(http, {"a": "b"}, extra_params={SEARCH: '"x" OR "y"'}))
    assert all(c["params"][SEARCH] == '"x" OR "y"' for c in http.calls) and http.calls[0]["params"]["filter"] == "a:b"


def test_works_plain_429_is_retried_once_but_budget_raises(monkeypatch):
    sleeps = []
    monkeypatch.setattr(openalex, "_sleep", lambda s: sleeps.append(s))
    w = work("W1", "10.1000/r1", "x", "2024-01-01")
    state = {}
    http = FakeHttp([FakeResp(429, PLAIN_429), FakeResp(200, page([w]))])
    assert len(list(openalex.works(http, {"a": "b"}, state=state))) == 1 and state == {"complete": True}
    assert sleeps == [1.0] and len(http.calls) == 2 and http.calls[1]["params"]["cursor"] == "*"
    state = {}
    http = FakeHttp([FakeResp(429, PLAIN_429), FakeResp(429, {"error": "Rate limit exceeded"}, headers={"Retry-After": "600"})])
    assert list(openalex.works(http, {"a": "b"}, state=state)) == [] and state["error"].startswith("HTTP 429")
    assert sleeps == [1.0, 1.0]  # the second 429 is not slept on; the unit is left for the next run
    assert openalex.retry_after_seconds(FakeResp(429, {}, headers={"Retry-After": "600"}), {}) == 60.0
    assert openalex.retry_after_seconds(FakeResp(429, {}), {"retryAfter": "nope"}) == 5.0
    with pytest.raises(OpenAlexBudgetExhausted):
        list(openalex.works(FakeHttp([FakeResp(429, BUDGET_429)]), {"a": "b"}))
    with pytest.raises(OpenAlexBudgetExhausted):
        list(openalex.works(FakeHttp([FakeResp(403, {"error": "forbidden"})]), {"a": "b"}))


# ---------------------------------------------------------------- API route --------------------------------------
def test_api_run_pages_upserts_and_records_progress(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    w1 = work("W1", "10.1000/a1", "A single-cell RNA atlas of liver", "2024-01-05")
    w2 = work("W2", "10.1000/a2", "scRNA-seq of kidney", "2024-01-20", countries=(("DE", "MPI"),))
    w3 = work("W3", None, "Xenium spatial transcriptomics map", "2024-01-11", countries=(("JP", "RIKEN"),))
    http = FakeHttp(probes(3, 2, 10) + [
        FakeResp(200, page([w1, w2], next_cursor="c2", count=3)),
        FakeResp(200, page([w3], next_cursor="c3", count=3)),
        FakeResp(200, page([], next_cursor=None, count=3)),  # pagination stop: empty page
        FakeResp(200, page([], count=0)),                    # spatial, no works this month
    ])
    report = backfill.run_api(st, http, GROUPS, "2024-01", "2024-01", mailto="me@example.org", api_key="k")
    assert report["stopped"] is None and report["units"] == 2 and report["done"] == 2 and report["skipped"] == 0 and report["errors"] == []
    assert report["papers_fetched"] == 3 and report["papers_new"] == 3 and report["papers_updated"] == 0
    assert st.counts()["papers"] == 3
    assert st.get_paper("openalex:W3") is not None  # no DOI -> openalex:<id>
    assert json.loads(st.get_paper("doi:10.1000/a1")["extra_json"])["match"] == "topic+keyword"
    # the three probes: OR-ed keywords, first keyword alone, topics only
    assert [c["params"]["per-page"] for c in http.calls[:3]] == [1, 1, 1]
    assert http.calls[0]["params"][SEARCH] == '"single-cell RNA" OR "scRNA-seq"' and http.calls[1]["params"][SEARCH] == '"single-cell RNA"'
    assert SEARCH not in http.calls[2]["params"]
    p = http.calls[3]["params"]
    assert p["filter"] == "primary_topic.id:T11289|T10222,from_publication_date:2024-01-01,to_publication_date:2024-01-31"
    assert p[SEARCH] == '"single-cell RNA" OR "scRNA-seq"' and ".search:" not in p["filter"]
    assert p["select"] == backfill.BACKFILL_SELECT and p["mailto"] == "me@example.org" and p["per-page"] == 200
    assert http.calls[3]["headers"]["Authorization"] == "Bearer k"
    assert http.calls[4]["params"]["cursor"] == "c2"
    assert progress_rows(st, "subgroup, month, country, fetched, cap") == [("single_cell", "2024-01", "", 3, 2000), ("spatial", "2024-01", "", 0, 2000)]
    # resume: everything is done, no HTTP call is made
    http2 = FakeHttp([])
    report2 = backfill.run_api(st, http2, GROUPS, "2024-01", "2024-01")
    assert report2["skipped"] == 2 and report2["done"] == 0 and http2.calls == []
    summary = backfill.progress_summary(st)
    assert [(r["subgroup"], r["units"], r["fetched"], r["capped_units"]) for r in summary] == [("single_cell", 1, 3, 0), ("spatial", 1, 0, 0)]
    st.close()


def test_api_run_with_countries_adds_filter_and_separate_units(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    http = FakeHttp([FakeResp(200, page([], count=0))] * 5)
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-02", countries=["cn", "US"], do_probe=False)
    assert report["units"] == 4 and report["done"] == 4 and len(http.calls) == 4
    filters = [c["params"]["filter"] for c in http.calls]
    assert sum("institutions.country_code:cn" in f for f in filters) == 2 and sum("institutions.country_code:us" in f for f in filters) == 2
    assert [r[:2] for r in progress_rows(st, "month, country")] == [("2024-01", "CN"), ("2024-01", "US"), ("2024-02", "CN"), ("2024-02", "US")]
    st.close()


def test_api_topics_only_omits_search_and_tags_match(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    w1 = work("W1", "10.1000/to1", "Untitled work in the topic", "2024-01-05", language="zh")
    http = FakeHttp([FakeResp(200, page([], count=7)), FakeResp(200, page([w1], count=1))])  # one probe (topic-only), one page
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01", topics_only=True)
    assert report["done"] == 1 and report["papers_new"] == 1
    assert all(SEARCH not in c["params"] for c in http.calls)
    assert json.loads(st.get_paper("doi:10.1000/to1")["extra_json"])["match"] == "topic"
    st.close()


def test_api_budget_exhausted_saves_progress_and_returns_cleanly(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    w1 = work("W1", "10.1000/b1", "single-cell RNA study", "2024-01-05")
    http = FakeHttp(probes(1, 1, 4) + [
        FakeResp(200, page([w1], count=1)),   # single_cell 2024-01 complete
        FakeResp(429, BUDGET_429),            # spatial 2024-01: budget gone
    ])
    report = backfill.run_api(st, http, GROUPS, "2024-01", "2024-02")
    assert report["stopped"] == "budget" and "Insufficient budget" in report["message"] and "Re-run" in report["message"]
    assert report["done"] == 1 and report["papers_new"] == 1
    assert progress_rows(st, "subgroup, month") == [("single_cell", "2024-01")]  # the interrupted unit is NOT marked done
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
    assert st.counts()["papers"] == 1 and progress_rows(st) == []
    st.close()


def test_api_probe_reports_http_400_with_message(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    http = FakeHttp([FakeResp(400, {"error": "Invalid query parameters error.", "message": "grants is not a valid select field"})])
    with pytest.raises(backfill.BackfillError) as exc:
        backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01")
    assert "HTTP 400" in str(exc.value) and "grants is not a valid select field" in str(exc.value)
    assert progress_rows(st) == []
    st.close()


def test_probe_budget_rate_limit_and_unusable_answers(monkeypatch):
    sleeps = []
    monkeypatch.setattr(openalex, "_sleep", lambda s: sleeps.append(s))
    with pytest.raises(OpenAlexBudgetExhausted):
        backfill.probe(FakeHttp([FakeResp(429, BUDGET_429)]), {"primary_topic.id": "T11289"})
    assert backfill.probe(FakeHttp([None]), {"primary_topic.id": "T11289"}) is None
    assert backfill.probe(FakeHttp([FakeResp(200, None, raw="<html>not json")]), {"primary_topic.id": "T11289"}) is None
    assert backfill.probe(FakeHttp([FakeResp(429, PLAIN_429), FakeResp(200, page([], count=12))]), {"primary_topic.id": "T11289"}) == 12
    assert backfill.probe(FakeHttp([FakeResp(429, PLAIN_429), FakeResp(429, PLAIN_429)]), {"primary_topic.id": "T11289"}) is None
    assert sleeps == [1.0, 1.0]


def test_api_keyword_syntax_guard(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    # OR treated as AND: the OR-ed expression returns fewer works than its first term alone -> stop before recording anything
    http = FakeHttp(probes(0, 5, 40))
    with pytest.raises(backfill.BackfillError) as exc:
        backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-02")
    assert "not honouring boolean OR" in str(exc.value) and progress_rows(st) == []
    # inconclusive first unit (all zero): the month is recorded as empty and the check is repeated on the next unit
    w1 = work("W1", "10.1000/g1", "single-cell RNA study", "2024-02-05")
    http = FakeHttp(probes(0, 0, 0) + [FakeResp(200, page([], count=0))] + probes(1, 1, 3) + [FakeResp(200, page([w1], count=1))] + [FakeResp(200, page([], count=0))])
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-03")
    assert report["done"] == 3 and report["papers_new"] == 1 and len(http.calls) == 9  # 3 + 1 + 3 + 1 + 1 (verified: no more probes)
    st.close()


def test_api_incomplete_units_are_not_marked_done(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    # (a) transport failure -> None
    report = backfill.run_api(st, FakeHttp([None]), GROUPS[:1], "2024-01", "2024-01", do_probe=False)
    assert report["done"] == 0 and report["errors"] == ["single_cell 2024-01 all: transport failure; 0 fetched, unit left for the next run"]
    # (b) HTTP 200 with an HTML body
    report = backfill.run_api(st, FakeHttp([FakeResp(200, None, raw="<html>maintenance</html>")]), GROUPS[:1], "2024-01", "2024-01", do_probe=False)
    assert report["done"] == 0 and report["stopped"] is None and "non-JSON body" in report["errors"][0]
    # (c) page 1 ok, page 2 transport failure: the 3 papers are stored but the month stays open
    ws = [work(f"W{i}", f"10.1000/i{i}", "single-cell RNA paper", "2024-01-05") for i in range(3)]
    report = backfill.run_api(st, FakeHttp([FakeResp(200, page(ws, next_cursor="c2", count=800)), None]), GROUPS[:1], "2024-01", "2024-01", do_probe=False)
    assert report["done"] == 0 and report["papers_fetched"] == 3 and st.counts()["papers"] == 3
    assert report["errors"] == ["single_cell 2024-01 all: transport failure; 3 fetched, unit left for the next run"]
    # (d) HTTP 200 carrying an error object
    report = backfill.run_api(st, FakeHttp([FakeResp(200, {"error": "server", "message": "upstream timeout"})]), GROUPS[:1], "2024-01", "2024-01", do_probe=False)
    assert report["done"] == 0 and "upstream timeout" in report["errors"][0]
    assert progress_rows(st) == []
    # a well-formed empty answer (no results key) is a complete, empty month
    report = backfill.run_api(st, FakeHttp([FakeResp(200, {"meta": {"count": 0}})]), GROUPS[:1], "2024-02", "2024-02", do_probe=False)
    assert report["done"] == 1 and report["papers_fetched"] == 0 and progress_rows(st, "month") == [("2024-02",)]
    st.close()


def test_api_max_per_month_cap_is_recorded_and_a_larger_cap_reopens(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    ws = [work(f"W{i}", f"10.1000/d{i}", "single-cell RNA paper", "2024-01-05") for i in range(5)]
    http = FakeHttp([FakeResp(200, page(ws[:3], next_cursor="more", count=5))])
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01", max_per_month=3, do_probe=False)
    assert report["papers_fetched"] == 3 and report["done"] == 1 and report["capped_units"] == ["single_cell 2024-01 all"]
    assert len(http.calls) == 1  # the pager stops once max_records is reached
    assert progress_rows(st, "fetched, cap") == [(3, 3)] and backfill.progress_summary(st)[0]["capped_units"] == 1
    # same cap: skipped; larger cap: the unit is pulled again and completes
    assert backfill.run_api(st, FakeHttp([]), GROUPS[:1], "2024-01", "2024-01", max_per_month=3, do_probe=False)["skipped"] == 1
    http = FakeHttp([FakeResp(200, page(ws, next_cursor="c2", count=5)), FakeResp(200, page([], count=5))])
    report = backfill.run_api(st, http, GROUPS[:1], "2024-01", "2024-01", max_per_month=5000, do_probe=False)
    assert report["skipped"] == 0 and report["done"] == 1 and report["capped_units"] == [] and len(http.calls) == 2
    assert report["papers_fetched"] == 5 and report["papers_new"] == 2 and report["papers_updated"] == 3 and st.counts()["papers"] == 5
    assert progress_rows(st, "fetched, cap") == [(5, 5000)]
    assert backfill.run_api(st, FakeHttp([]), GROUPS[:1], "2024-01", "2024-01", max_per_month=10000, do_probe=False)["skipped"] == 1  # complete, not capped
    with pytest.raises(backfill.BackfillError):
        backfill.run_api(st, FakeHttp([]), GROUPS[:1], "2024-01", "2024-01", max_per_month=0, do_probe=False)
    st.close()


def test_clear_progress_for_redo(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    http = FakeHttp([FakeResp(200, page([], count=0))] * 6)
    backfill.run_api(st, http, GROUPS, "2024-01", "2024-03", do_probe=False)
    assert len(progress_rows(st)) == 6
    assert backfill.clear_progress(st, ["single_cell"], "2024-02", "2024-03") == 2
    assert [r[:2] for r in progress_rows(st, "subgroup, month")] == [("single_cell", "2024-01"), ("spatial", "2024-01"), ("spatial", "2024-02"), ("spatial", "2024-03")]
    assert backfill.clear_progress(st, ["single_cell"], "2024-01", "2024-01", countries=["CN"]) == 0  # country units are tracked apart
    st.close()


def test_ensure_schema_adds_cap_to_an_older_table(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.conn.execute("CREATE TABLE backfill_progress (subgroup TEXT NOT NULL, month TEXT NOT NULL, country TEXT NOT NULL DEFAULT '', fetched INTEGER DEFAULT 0, done_at TEXT, PRIMARY KEY (subgroup, month, country))")
    st.conn.execute("INSERT INTO backfill_progress VALUES ('single_cell', '2024-01', '', 2000, '2026-10-17T00:00:00Z')")
    st.conn.commit()
    backfill.ensure_schema(st)
    assert backfill.progress_done(st, "single_cell", "2024-01", "", max_per_month=5000) is True  # unknown cap: not reopened
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
    assert report["lines"] == 7 and report["prefiltered"] == 1 and report["malformed"] == 1  # W1004 (other topic) never JSON-decoded
    # kept: W1 (single_cell CN+US), W3 (scRNA-seq, 2019), W6 (spatial DE), W7 (zh metadata, topic only)
    # dropped: W2 (topic ok, English, no keyword), W4 (other topic), W5 (malformed)
    assert report["matched"] == 4 and report["papers_new"] == 4
    ids = {r[0] for r in st.conn.execute("SELECT id FROM papers")}
    assert ids == {"doi:10.1000/snap1", "doi:10.1000/snap3", "doi:10.1000/snap5", "doi:10.1000/snap7"}
    e1 = json.loads(st.get_paper("doi:10.1000/snap1")["extra_json"])
    # W1 has Chinese-language metadata on the shared topic T11289: single_cell by keyword, spatial on the topic alone
    assert e1["countries"] == ["CN", "US"] and e1["subgroups"] == ["single_cell", "spatial"] and e1["backfill_source"] == "snapshot" and e1["language"] == "zh"
    assert e1["match"] == "topic+keyword"
    e5 = json.loads(st.get_paper("doi:10.1000/snap5")["extra_json"])
    assert e5["subgroups"] == ["spatial"] and e5["countries"] == ["DE"]
    e7 = json.loads(st.get_paper("doi:10.1000/snap7")["extra_json"])
    assert e7["match"] == "topic" and e7["language"] == "zh" and e7["countries"] == ["CN"] and e7["subgroups"] == ["single_cell", "spatial"]
    rows = progress_rows(st, "subgroup, month, fetched")
    assert [r[0] for r in rows] == [backfill.SNAPSHOT_PROGRESS_SUBGROUP] * 2 and sorted(r[2] for r in rows) == [2, 2]
    # re-run skips both files
    report2 = backfill.run_snapshot(st, FIXTURE_DIR, GROUPS)
    assert report2["files_skipped"] == 2 and report2["files_done"] == 0 and report2["matched"] == 0
    assert backfill.snapshot_plan(st, FIXTURE_DIR) == [
        ("data/jsonl/works/updated_date=2026-01-01/part_0000.gz", True), ("data/jsonl/works/updated_date=2026-01-02/part_0000.jsonl", True)]
    assert backfill.clear_snapshot_progress(st, FIXTURE_DIR) == 2 and backfill.run_snapshot(st, FIXTURE_DIR, GROUPS)["files_done"] == 2
    st.close()
    # --topics-only also keeps W2 (tracked topic, English, no keyword)
    st = Store(tmp_path / "t2.sqlite")
    report = backfill.run_snapshot(st, FIXTURE_DIR, GROUPS, topics_only=True)
    assert report["matched"] == 5 and json.loads(st.get_paper("doi:10.1000/snap2")["extra_json"])["match"] == "topic"
    st.close()


def test_snapshot_country_and_window_filters(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    report = backfill.run_snapshot(st, FIXTURE_DIR, GROUPS, countries=["CN"])
    assert report["matched"] == 2 and st.get_paper("doi:10.1000/snap1") is not None and st.get_paper("doi:10.1000/snap7") is not None
    st.close()
    st = Store(tmp_path / "t2.sqlite")
    report = backfill.run_snapshot(st, FIXTURE_DIR, GROUPS, from_month="2024-01", to_month="2025-12")
    assert {r[0] for r in st.conn.execute("SELECT id FROM papers")} == {"doi:10.1000/snap1", "doi:10.1000/snap5", "doi:10.1000/snap7"}  # 2019 paper excluded
    assert report["matched"] == 3
    st.close()


def test_snapshot_matcher_keyword_boundaries_and_language():
    m = backfill.SnapshotMatcher(GROUPS)
    base = work("W9", "10.1000/k", "", "2024-01-01")
    base["title"] = "Single cell RNA profiling"  # space instead of hyphen still matches
    assert m.match_detail(base) == (["single_cell"], "topic+keyword")
    base["title"] = "Xeniumlike probes"  # no word boundary -> no spatial match
    assert m.match(base) == []
    base["title"] = "No keywords here"
    base["abstract_inverted_index"] = {"spatial": [0], "transcriptomics": [1], "and": [2], "scRNA-seq": [3]}
    assert m.match(base) == ["single_cell", "spatial"]
    base["abstract_inverted_index"] = None
    assert m.match(base) == []
    base["language"] = "zh"  # non-English metadata: accepted on the topic alone
    assert m.match_detail(base) == (["single_cell", "spatial"], "topic")
    base["language"] = None  # unknown language is treated as English
    assert m.match(base) == []
    assert backfill.SnapshotMatcher(GROUPS, topics_only=True).match_detail(base) == (["single_cell", "spatial"], "topic")
    base["primary_topic"] = {"id": "https://openalex.org/T99999"}
    assert m.match(base) == []
    assert m.match({"title": "x"}) == []


def test_topic_hint_prefilter(tmp_path):
    hint = backfill.topic_hint(["T11289", "T10222"])
    assert hint.search(b'"primary_topic":{"id":"https://openalex.org/T11289","display_name":"x"}')
    assert hint.search(b'"id":"https:\\/\\/openalex.org\\/T10222"')  # escaped slashes tolerated
    assert hint.search(b'"id":"https://openalex.org/T112890"') is None  # longer id is a different topic
    assert hint.search(b'{"id": "https://openalex.org/W1", "title": "no topic"}') is None
    assert backfill.topic_hint([]) is None
    counters = {}
    data = b'{"a": 1}\n{"primary_topic": {"id": "https://openalex.org/T11289"}}\nnot json but openalex.org/T11289" here\n'
    (tmp_path / "part.jsonl").write_bytes(data)
    recs = list(backfill.iter_jsonl(tmp_path / "part.jsonl", counters, hint))
    assert len(recs) == 1 and counters == {"lines": 3, "prefiltered": 1, "malformed": 1}


def test_snapshot_handles_empty_truncated_corrupted_and_missing(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    snap = tmp_path / "snap" / "data" / "jsonl" / "works" / "updated_date=2026-10-14"
    snap.mkdir(parents=True)
    with gzip.open(snap / "part_0000.gz", "wt", encoding="utf-8") as fh:
        fh.write("")  # empty file
    good = gzip.compress((json.dumps(work("W7", "10.1000/t7", "scRNA-seq atlas", "2025-02-02")) + "\n").encode("utf-8") * 20)
    (snap / "part_0001.gz").write_bytes(good[: len(good) // 2])  # truncated download
    (snap / "part_0002.gz").write_bytes(b"not gzip at all")
    corrupted = None  # flip bytes inside the deflate stream until the stdlib raises zlib.error (not a CRC/EOF error)
    for off in range(20, len(good) - 8):
        b = bytearray(good)
        for i in range(off, off + 8):
            b[i] ^= 0xFF
        try:
            with gzip.open(io.BytesIO(bytes(b)), "rb") as fh:
                for _ in fh:
                    pass
        except zlib.error:
            corrupted = bytes(b)
            break
        except (OSError, EOFError):
            continue
    assert corrupted is not None
    (snap / "part_0003.gz").write_bytes(corrupted)
    report = backfill.run_snapshot(st, tmp_path / "snap", GROUPS)
    assert report["files"] == 4 and report["files_done"] == 1 and len(report["errors"]) == 3
    assert any(e.startswith("data/jsonl/works/updated_date=2026-10-14/part_0003.gz: error:") for e in report["errors"])  # zlib.error's class name
    assert [r[0] for r in progress_rows(st, "month")] == ["data/jsonl/works/updated_date=2026-10-14/part_0000.gz"]
    assert backfill.snapshot_plan(st, tmp_path / "snap")[1] == ("data/jsonl/works/updated_date=2026-10-14/part_0001.gz", False)
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


def test_cli_backfill_budget_message_exits_with_code_3(tmp_path, monkeypatch, capsys):
    cfg = _config_dir(tmp_path)
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    fake = FakeHttp([FakeResp(429, BUDGET_429)])
    monkeypatch.setattr(cli, "Http", lambda **kw: fake)
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--from", "2024-01", "--to", "2024-01", "--subgroup", "single_cell"])
    out = capsys.readouterr().out
    assert rc == 3 and '"stopped": "budget"' in out and "Re-run the same command to resume" in out
    assert len(fake.calls) == 1
    # a finished run exits 0
    fake = FakeHttp(probes(0, 0, 0) + [FakeResp(200, page([], count=0))])
    monkeypatch.setattr(cli, "Http", lambda **kw: fake)
    assert cli.main(["--config-dir", str(cfg), "backfill", "--from", "2024-01", "--to", "2024-01", "--subgroup", "single_cell"]) == 0
    assert '"done": 1' in capsys.readouterr().out


def test_cli_backfill_dry_run_status_redo_and_errors(tmp_path, monkeypatch, capsys):
    cfg = _config_dir(tmp_path)
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "db.sqlite"))
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--from", "2024-01", "--to", "2024-02", "--country", "CN", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "4 unit(s)" in out and "single_cell" in out and "CN" in out
    assert cli.main(["--config-dir", str(cfg), "backfill", "--from", "2024-01", "--to", "2024-01", "--subgroup", "nope"]) == 2
    assert "unknown subgroup" in capsys.readouterr().err
    assert cli.main(["--config-dir", str(cfg), "backfill", "--subgroup", "spatial"]) == 2  # no window, no snapshot dir
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config-dir", str(cfg), "backfill", "--from", "2024-01", "--to", "2024-01", "--max-per-month", "0"])
    assert exc.value.code == 2 and "at least 1" in capsys.readouterr().err
    # snapshot: dry-run lists files without importing, then import, then --redo re-imports
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--snapshot-dir", str(FIXTURE_DIR), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "2 snapshot file(s); 2 pending" in out and "pending data/jsonl/works/updated_date=2026-01-01/part_0000.gz" in out
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--snapshot-dir", str(FIXTURE_DIR), "--country", "DE"])
    out = capsys.readouterr().out
    assert rc == 0 and '"matched": 1' in out
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--snapshot-dir", str(FIXTURE_DIR), "--dry-run"])
    assert rc == 0 and "0 pending, 2 already done" in capsys.readouterr().out
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--snapshot-dir", str(FIXTURE_DIR), "--redo", "--topics-only"])
    out = capsys.readouterr().out
    assert rc == 0 and "--redo: cleared 2 snapshot progress row(s)" in out and '"matched": 5' in out
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--status"])
    out = capsys.readouterr().out
    assert rc == 0 and backfill.SNAPSHOT_PROGRESS_SUBGROUP in out
    rc = cli.main(["--config-dir", str(cfg), "backfill", "--status", "--json"])
    assert rc == 0 and json.loads(capsys.readouterr().out)[0]["units"] == 2
