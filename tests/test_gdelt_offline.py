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
