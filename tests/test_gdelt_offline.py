"""GDELT DOC 2.0 adapter against canned responses (no network)."""
import json
import logging

import requests

from litradar.sources import gdelt


def _resp(status, text):
    r = requests.Response()
    r.status_code = status
    r._content = text.encode("utf-8")
    r.url = gdelt.DOC_API
    return r


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.blocked_hosts = set()

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,), retries=None):
        self.calls.append((url, params))
        return self.responses.pop(0) if self.responses else None


ARTICLES = {
    "articles": [
        {"url": "https://news.example.com/a", "url_mobile": "", "title": "Illumina  launches a new  sequencer", "seendate": "20261015T143000Z", "socialimage": "", "domain": "news.example.com", "language": "English", "sourcecountry": "United States"},
        {"url": "https://old.example.com/b", "title": "Older story about Illumina", "seendate": "20261001T090000Z", "domain": "old.example.com", "language": "English", "sourcecountry": "United Kingdom"},
        {"url": "", "title": "no url", "seendate": "20261015T143000Z"},
    ]
}


def test_build_query_quotes_name_parenthesises_or_groups_and_adds_language():
    ctx = '(sequencing OR genomics OR "life sciences")'
    assert gdelt.build_query("Illumina", ctx) == '"Illumina" (sequencing OR genomics OR "life sciences") sourcelang:english'
    assert gdelt.build_query("Illumina", "cancer OR diagnostic") == '"Illumina" (cancer OR diagnostic) sourcelang:english'
    assert gdelt.build_query("Illumina", "", language="") == '"Illumina"'
    assert gdelt.build_query("10x Genomics", "(x OR y)", extra="domain:example.com") == '"10x Genomics" (x OR y) domain:example.com sourcelang:english'


def test_batch_query_and_packing_under_the_query_length_limit():
    ctx = "(sequencing OR genomics)"
    assert gdelt.build_batch_query(["Illumina"], ctx) == gdelt.build_query("Illumina", ctx)  # a batch of one is the old query
    assert gdelt.build_batch_query(["Illumina", "10x Genomics"], ctx) == '("Illumina" OR "10x Genomics") (sequencing OR genomics) sourcelang:english'
    assert gdelt.build_batch_query([], ctx) == "(sequencing OR genomics) sourcelang:english"
    members = [("Illumina", "illumina"), ("10x Genomics", "txg"), ("Pacific Biosciences", "pacb"), ("Oxford Nanopore", "ont"), ("Bruker", "bruker")]
    # every query stays within max_chars; the greedy packer keeps the order and opens a new batch when the next name would not fit
    batches = gdelt.batch_companies(members, ctx, max_chars=100, batch_size=0)
    assert [[cid for _, cid in b] for b in batches] == [["illumina", "txg", "pacb"], ["ont", "bruker"]]
    assert all(len(gdelt.build_batch_query([n for n, _ in b], ctx)) <= 100 for b in batches)
    # the member cap applies on top of the length limit
    assert [[cid for _, cid in b] for b in gdelt.batch_companies(members, ctx, max_chars=250, batch_size=2)] == [["illumina", "txg"], ["pacb", "ont"], ["bruker"]]
    assert [[cid for _, cid in b] for b in gdelt.batch_companies(members, ctx, max_chars=250, batch_size=1)] == [[cid] for _, cid in members]
    # the real limits: 70+ names of typical length need about ten requests, never a query over 250 characters
    many = [(f"Company Number {i}", f"c{i}") for i in range(72)]
    real = gdelt.batch_companies(many, '(sequencing OR genomics OR proteomics OR "life sciences" OR instrument OR biotech)')
    assert 9 <= len(real) <= 12 and all(len(gdelt.build_batch_query([n for n, _ in b], '(sequencing OR genomics OR proteomics OR "life sciences" OR instrument OR biotech)')) <= gdelt.MAX_QUERY_CHARS for b in real)
    # an oversize single name still goes alone (the API decides); blank names and names the API rejects as too short are dropped
    assert gdelt.batch_companies([("x" * 300, "big"), ("", "blank"), ("Seer", "seer"), ("Illumina", "illumina")], ctx, max_chars=50) == [[("x" * 300, "big")], [("Illumina", "illumina")]]
    assert gdelt.too_short("Seer") and not gdelt.too_short("Roche") and gdelt.too_short("  Abc ")


def test_batch_news_reports_its_outcome_and_leaves_attribution_to_the_caller():
    http = FakeHttp([_resp(200, json.dumps(ARTICLES)), None])
    outcome = {}
    items = list(gdelt.batch_news(http, [("Illumina", "illumina"), ("10x Genomics", "txg")], context="(x OR y)", since="2026-10-10", outcome=outcome))
    assert http.calls[0][1]["query"] == '("Illumina" OR "10x Genomics") (x OR y) sourcelang:english'
    assert outcome == {"answered": True, "articles": 3} and len(items) == 1  # the since filter dropped the old story, the url-less one was skipped
    assert items[0].feed == "gdelt" and items[0].companies == []  # a multi-company batch: the pipeline's gazetteer attributes it
    outcome = {}
    assert list(gdelt.batch_news(http, [("Illumina", "illumina")], outcome=outcome)) == [] and outcome == {"answered": False, "articles": 0}
    assert list(gdelt.batch_news(http, [("", "")])) == [] and len(http.calls) == 2  # nothing to ask


def test_seen_date():
    assert gdelt._seen_date("20261015T143000Z") == "2026-10-15"
    assert gdelt._seen_date("20261015") == "2026-10-15"
    assert gdelt._seen_date("") == "" and gdelt._seen_date("garbage") == ""


def test_search_parses_articles_and_empty_and_error_bodies(caplog):
    http = FakeHttp([_resp(200, json.dumps(ARTICLES)), _resp(200, "{}"), _resp(200, "Queries containing OR'd terms must be surrounded by ()."), _resp(429, "slow down"), None])
    arts = gdelt.search(http, '"Illumina"', timespan="7d", max_records=9999)
    assert len(arts) == 3 and http.calls[0][1]["maxrecords"] == 250 and http.calls[0][1]["mode"] == "ArtList" and http.calls[0][1]["timespan"] == "7d"
    assert gdelt.search(http, "x") == []
    with caplog.at_level(logging.WARNING, logger="litradar.sources.gdelt"):
        assert gdelt.search(http, "a OR b") is None
    assert "rejected query" in caplog.text
    assert gdelt.search(http, "x") is None  # 429
    assert gdelt.search(http, "x") is None  # transport failure


def test_company_news_yields_items_with_gdelt_feed_and_since_filter():
    http = FakeHttp([_resp(200, json.dumps(ARTICLES))])
    items = list(gdelt.company_news(http, "Illumina", "illumina", context="(sequencing OR genomics)", since="2026-10-10"))
    assert len(items) == 1
    it = items[0]
    assert it.feed == "gdelt:Illumina" and it.companies == ["illumina"] and it.published == "2026-10-15"
    assert it.title == "Illumina launches a new sequencer" and it.url == "https://news.example.com/a" and it.summary == ""
    assert http.calls[0][1]["query"] == '"Illumina" (sequencing OR genomics) sourcelang:english'
    # no answer -> nothing yielded, no exception
    assert list(gdelt.company_news(FakeHttp([None]), "Illumina", "illumina")) == []


def test_rate_limit_stops_the_company_loop_and_the_one_error_line_reaches_coverage(tmp_path, monkeypatch):
    """A 429 blocks the host: later company queries are skipped without a request, the run records ONE error line
    naming how many were skipped, and the digest coverage block lists it under the errored sources."""
    from litradar.config import Settings
    from litradar.coverage import coverage_report
    from litradar.store import Store

    # the adapter side: once the shared client has blocked the host, search() must not be reached again
    http = FakeHttp([_resp(429, "slow down")])
    assert gdelt.search(http, '"Illumina"') is None and len(http.calls) == 1
    http.blocked_hosts.add(gdelt.DOC_API.split("/")[2])  # what Http._note_429 does on the second 429 in a row
    skipped = 0
    for company in ("Illumina", "PacBio", "10x Genomics"):
        if gdelt.DOC_API.split("/")[2] in http.blocked_hosts:  # the pipeline's guard before each company query
            skipped += 1
            continue
        list(gdelt.company_news(http, company, company.lower()))
    assert skipped == 3 and len(http.calls) == 1  # no further requests on a blocked host
    # the run-report side: the single error line the pipeline writes parses into source 'gdelt' for coverage
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    st = Store(tmp_path / "r.sqlite")
    msg = f"gdelt: host rate-limited (HTTP 429) during the run; {skipped} company quer(y/ies) skipped"
    st.record_run("2026-10-17T00:00:00Z", "2026-10-10", "2026-10-17", {"counts": {"news.gdelt_skipped": skipped}, "errors": [msg], "http": {}})
    report = coverage_report(st, "2026-10-10", "2026-10-17", Settings.from_env())
    _errs = report["errored_sources"]
    assert len(_errs) == 1 and _errs[0]['source'] == 'gdelt' and 'rate-limited (HTTP 429)' in _errs[0]['error'] and '3 company' in _errs[0]['error']
    st.close()
