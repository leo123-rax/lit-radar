"""NIH RePORTER adapter against canned payloads (no network).

The FakeHttp here answers `post_json` (the only method the adapter uses) with a queue of payloads and
records every request body so the tests can check criteria, paging offsets and stop conditions.
"""
import json

import pytest

from litradar.http import DEFAULT_MIN_INTERVAL
from litradar.sources import nih_reporter as nr


class FakePostHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []  # (url, body)

    def post_json(self, url, json_body, headers=None):
        self.calls.append((url, json.loads(json.dumps(json_body))))
        return self.payloads.pop(0) if self.payloads else None


def _row(appl_id, title, **kw):
    base = {
        "appl_id": appl_id,
        "project_num": f"1S10OD0{appl_id}-01",
        "project_title": title,
        "abstract_text": "Abstract text about the instrument.",
        "activity_code": "S10",
        "award_amount": 1371730,
        "award_notice_date": "2026-09-18T00:00:00",
        "project_start_date": "2026-09-01T00:00:00",
        "fiscal_year": 2026,
        "organization": {"org_name": "EXAMPLE UNIVERSITY", "org_city": "SPRINGFIELD", "org_state": "IL", "org_country": "UNITED STATES"},
        "agency_ic_admin": {"code": "OD", "abbreviation": "OD", "name": "Office of the Director"},
        "project_detail_url": f"https://reporter.nih.gov/project-details/{appl_id}",
    }
    base.update(kw)
    return base


def _page(rows, total, offset=0, limit=500):
    return {"meta": {"search_id": "x", "total": total, "offset": offset, "limit": limit, "sort_field": "award_notice_date",
                     "sort_order": "asc", "sorted_by_relevance": False, "properties": {}}, "results": rows}


# -- normal payload --------------------------------------------------------------------------
def test_s10_awards_normal_payload():
    long_abstract = "A" * 2000
    http = FakePostHttp([_page([
        _row(11339465, "NovaSeq X Plus Sequencing System", abstract_text=long_abstract),
        _row(11338275, "  Acquisition of an\nOrbitrap Astral  ", award_amount=750000.0, activity_code="S10"),
    ], total=2)])
    items = list(nr.s10_awards(http, "2026-07-19", "2026-10-17"))
    assert [i.id for i in items] == ["nih-reporter:11339465", "nih-reporter:11338275"]
    a, b = items
    assert a.feed == "nih-reporter" and a.title == "NovaSeq X Plus Sequencing System"
    assert a.url == "https://reporter.nih.gov/project-details/11339465"
    assert a.published == "2026-09-18"  # award notice date, not project start
    assert a.companies == []
    # abstract is capped at 1500 chars and followed by the metadata line
    assert a.summary.startswith("A" * 1500 + "\n") and "A" * 1501 not in a.summary
    tail = a.summary.rsplit("\n", 1)[1]
    assert tail == "S10 | 1S10OD011339465-01 | EXAMPLE UNIVERSITY (SPRINGFIELD, IL) | $1,371,730 | OD | FY2026"
    # whitespace in titles is collapsed; float amounts render as integers
    assert b.title == "Acquisition of an Orbitrap Astral" and "$750,000" in b.summary
    # one request, shaped as the API expects
    assert len(http.calls) == 1
    url, body = http.calls[0]
    assert url == nr.API_URL
    assert body["criteria"] == {"award_notice_date": {"from_date": "2026-07-19", "to_date": "2026-10-17"}, "activity_codes": ["S10"]}
    assert body["offset"] == 0 and body["limit"] == 500 and body["sort_field"] == "award_notice_date"
    assert "ApplId" in body["include_fields"] and "AbstractText" in body["include_fields"]


def test_published_falls_back_to_project_start_and_url_is_built():
    row = _row(1, "Thing", award_notice_date=None, project_detail_url=None)
    item = nr.to_news_item(row)
    assert item.published == "2026-09-01" and item.url == "https://reporter.nih.gov/project-details/1"
    row = _row(2, "Thing", award_notice_date="", project_start_date="", budget_start="2026-10-01T00:00:00")
    assert nr.to_news_item(row).published == "2026-10-01"
    row = _row(3, "Thing", award_notice_date=None, project_start_date=None)
    assert nr.to_news_item(row).published == ""


def test_decorated_title_and_missing_metadata():
    row = _row(5, "Cryo-EM microscope", organization={"org_name": "SOME INSTITUTE"}, agency_ic_admin=None, award_amount=None, fiscal_year=None)
    item = nr.to_news_item(row, decorate_title=True)
    assert item.title == "Cryo-EM microscope — SOME INSTITUTE (S10)"
    assert item.summary.endswith("\nS10 | 1S10OD05-01 | SOME INSTITUTE")
    plain = nr.to_news_item(row)
    assert plain.title == "Cryo-EM microscope"


# -- empty payload ---------------------------------------------------------------------------
def test_empty_payload_yields_nothing_and_stops():
    http = FakePostHttp([_page([], total=0)])
    assert list(nr.s10_awards(http, "2026-07-19", "2026-10-17")) == []
    assert len(http.calls) == 1


# -- malformed payloads ----------------------------------------------------------------------
@pytest.mark.parametrize("payload", [
    None,  # HTTP error / network failure (post_json returns None)
    ["Error converting value \"x\" to type 'DateRange'. Path 'criteria.award_notice_date'."],  # the API's validation error shape
    {"message": "System doesn't support limit value greater than 500."},  # the other error shape: no results key
    {"meta": {"total": 3}, "results": "not-a-list"},
    "just a string",
    42,
])
def test_malformed_payloads_do_not_raise(payload):
    http = FakePostHttp([payload])
    assert list(nr.s10_awards(http, "2026-07-19", "2026-10-17")) == []
    assert len(http.calls) == 1


def test_malformed_rows_are_skipped():
    rows = [
        "not a dict",
        {"appl_id": None, "project_title": "no id"},
        {"appl_id": 7, "project_title": "   "},
        {"appl_id": 8, "project_title": "Good row", "organization": "not-a-dict", "agency_ic_admin": [], "award_amount": "n/a", "award_notice_date": "garbage"},
    ]
    http = FakePostHttp([{"meta": {"total": "four?"}, "results": rows}])
    items = list(nr.s10_awards(http, "2026-07-19", "2026-10-17"))
    assert [i.id for i in items] == ["nih-reporter:8"]
    assert items[0].published == "" and items[0].summary == ""  # no abstract, no usable metadata
    assert items[0].title == "Good row" and items[0].url.endswith("/8")


# -- pagination ------------------------------------------------------------------------------
def test_pagination_stops_at_total_without_probing_past_the_end():
    page1 = _page([_row(1, "A"), _row(2, "B")], total=3, offset=0, limit=2)
    page2 = _page([_row(3, "C")], total=3, offset=2, limit=2)
    http = FakePostHttp([page1, page2, _page([], total=3)])
    rows = list(nr.search(http, {"activity_codes": ["S10"]}, limit=2))
    assert [r["appl_id"] for r in rows] == [1, 2, 3]
    # exactly two requests: offset 0 then offset 2; a third request would be an HTTP 400 on the real API
    assert [b["offset"] for _, b in http.calls] == [0, 2]
    assert [b["limit"] for _, b in http.calls] == [2, 2]
    assert len(http.payloads) == 1  # the spare payload was never consumed


def test_pagination_stops_on_empty_page_even_if_total_claims_more():
    http = FakePostHttp([_page([_row(1, "A")], total=10), _page([], total=10)])
    rows = list(nr.search(http, {"activity_codes": ["S10"]}, limit=1))
    assert len(rows) == 1 and len(http.calls) == 2


def test_pagination_respects_max_records_and_dedupes_across_pages():
    page1 = _page([_row(1, "A"), _row(2, "B")], total=5)
    page2 = _page([_row(2, "B again"), _row(3, "C")], total=5)
    http = FakePostHttp([page1, page2, _page([_row(4, "D")], total=5)])
    rows = list(nr.search(http, {"activity_codes": ["S10"]}, limit=2, max_records=4))
    assert [r["appl_id"] for r in rows] == [1, 2, 3]  # duplicate appl_id 2 dropped
    assert [b["offset"] for _, b in http.calls] == [0, 2]  # fetched 4 rows = max_records, so no third page
    assert len(http.payloads) == 1


def test_pagination_stops_at_api_offset_cap(monkeypatch):
    monkeypatch.setattr(nr, "MAX_OFFSET", 3)
    pages = [_page([_row(i, "x"), _row(i + 1, "x")], total=100) for i in range(1, 100, 2)]
    http = FakePostHttp(pages)
    rows = list(nr.search(http, {"activity_codes": ["S10"]}, limit=2))
    assert len(rows) == 4 and [b["offset"] for _, b in http.calls] == [0, 2]


def test_limit_is_clamped_to_api_maximum():
    http = FakePostHttp([_page([], total=0)])
    list(nr.search(http, {"activity_codes": ["S10"]}, limit=10000))
    assert http.calls[0][1]["limit"] == 500


# -- keyword / SBIR entry points -------------------------------------------------------------
def test_keyword_awards_builds_phrase_query_and_batches_terms():
    terms = ["NovaSeq X", "Revio", "Orbitrap Astral", 'Xenium "Analyzer"', "  ", "Revio", "revio"]  # dupes cost nothing
    http = FakePostHttp([_page([_row(1, "A")], total=1), _page([_row(1, "A"), _row(2, "B")], total=2)])
    items = list(nr.keyword_awards(http, "2026-01-01", "2026-10-17", terms, activity_codes=["S10", "P30"], terms_per_query=2))
    assert [i.id for i in items] == ["nih-reporter:1", "nih-reporter:2"]  # de-duplicated across batches
    assert len(http.calls) == 2
    c1 = http.calls[0][1]["criteria"]
    assert c1["activity_codes"] == ["S10", "P30"]
    assert c1["advanced_text_search"] == {"operator": "advanced", "search_field": "projecttitle,abstracttext", "search_text": '"NovaSeq X" OR "Revio"'}
    c2 = http.calls[1][1]["criteria"]
    assert c2["advanced_text_search"]["search_text"] == '"Orbitrap Astral" OR "Xenium Analyzer"'
    assert "org_names" not in c2


def test_keyword_awards_org_names_and_exact_match():
    http = FakePostHttp([_page([], total=0), _page([], total=0)])
    list(nr.keyword_awards(http, "2026-01-01", "2026-10-17", [], org_names=["Example Genomics Inc", " "], activity_codes=nr.SBIR_STTR_CODES))
    list(nr.keyword_awards(http, "2026-01-01", "2026-10-17", [], org_names=["Example Genomics Inc"], exact_org_names=True))
    c1, c2 = http.calls[0][1]["criteria"], http.calls[1][1]["criteria"]
    assert c1["org_names"] == ["Example Genomics Inc"] and c1["activity_codes"] == list(nr.SBIR_STTR_CODES)
    assert "advanced_text_search" not in c1
    assert c2["org_names_exact_match"] == ["Example Genomics Inc"] and "org_names" not in c2


def test_keyword_awards_refuses_an_unbounded_query():
    http = FakePostHttp([])
    with pytest.raises(ValueError):
        list(nr.keyword_awards(http, "2026-01-01", "2026-10-17", []))
    assert http.calls == []


def test_sbir_awards_defaults():
    http = FakePostHttp([_page([_row(9, "Phase I", activity_code="R43", award_amount=299_000)], total=1)])
    items = list(nr.sbir_awards(http, "2026-09-17", "2026-10-17", org_names=["Example"]))
    assert items[0].id == "nih-reporter:9" and "R43" in items[0].summary and "$299,000" in items[0].summary
    crit = http.calls[0][1]["criteria"]
    assert crit["activity_codes"] == ["R41", "R42", "R43", "R44", "U43", "U44", "SB1"] and crit["org_names"] == ["Example"]


def test_s10_awards_with_terms_adds_text_search():
    http = FakePostHttp([_page([], total=0)])
    list(nr.s10_awards(http, "2023-01-01", "2026-10-17", terms=["PacBio", "Revio"]))
    crit = http.calls[0][1]["criteria"]
    assert crit["activity_codes"] == ["S10"] and crit["advanced_text_search"]["search_text"] == '"PacBio" OR "Revio"'


def test_count_uses_limit_zero():
    http = FakePostHttp([_page([], total=578), None])
    assert nr.count(http, {"activity_codes": ["S10"]}) == 578
    assert http.calls[0][1]["limit"] == 0
    assert nr.count(http, {"activity_codes": ["S10"]}) == 0  # malformed -> 0


def test_text_search_helper_edge_cases():
    assert nr.text_search([]) is None
    assert nr.text_search(["", "  "]) is None
    assert nr.text_search(["a"], search_fields="projecttitle") == {"operator": "advanced", "search_field": "projecttitle", "search_text": '"a"'}
    assert nr.text_search(["Revio", "revio", "PacBio Revio"])["search_text"] == '"Revio" OR "PacBio Revio"'


def test_host_pacing_is_registered():
    assert DEFAULT_MIN_INTERVAL["api.reporter.nih.gov"] >= 1.0
