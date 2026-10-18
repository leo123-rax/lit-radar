"""Pipeline.fetch_news against a tiny config directory and a fake HTTP layer (no network).

Covers: Google News is opt-in (gnews feeds skipped by default, used when news.google_news is true),
per-company news comes from GDELT with gazetteer gating and title exclusions, SEC feeds need a contact
e-mail, publisher/press feeds are kept, GDELT rate-limiting is reported in errors.
"""
import json
from pathlib import Path

import requests
import yaml

from litradar import track
from litradar.config import Settings
from litradar.pipeline import Pipeline
from litradar.sources import gdelt

COMPANIES = {
    "entries": [
        {"id": "illumina", "name": "Illumina", "ticker": "ILMN", "type": "tools", "watch_news": True, "aliases": ["Illumina Inc"]},
        {"id": "grail", "name": "GRAIL", "ticker": "GRAL", "type": "diagnostics", "watch_news": True, "aliases": ["Galleri"], "case_sensitive_aliases": ["GRAIL"]},
        {"id": "quiet-co", "name": "Quiet Co", "ticker": "private", "type": "tools", "watch_news": False},
    ]
}
INSTRUMENTS = {"entries": [{"id": "ilmn-novaseq-x", "name": "NovaSeq X", "vendor": "Illumina", "ticker": "ILMN", "category": "sequencer.short_read", "aliases": ["NovaSeq X Plus"]}]}
FEEDS = {
    "feeds": [
        {"name": "Trade RSS", "kind": "news", "url": "https://trade.example/feed"},
        {"name": "Wire", "kind": "press", "url": "https://wire.example/rss"},
        {"name": "Outlet (via Google News)", "kind": "gnews", "url": "https://news.google.com/rss/search?q=site:outlet.example"},
    ]
}
RADAR = {
    "sources": {"sec": {"enabled": True}},
    "news": {
        "gdelt": True,
        "google_news": False,
        "query_context": {"tools": "(sequencing OR genomics)", "diagnostics": "(cancer OR diagnostic)"},
        "exclude_title_patterns": [r"\bForm (4|144)\b", "MarketBeat|Zacks"],
    },
}

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Illumina ships NovaSeq X Plus to a new lab</title><link>https://trade.example/1</link><pubDate>Mon, 13 Oct 2026 00:00:00 GMT</pubDate><description>desc</description></item>
<item><title>Unrelated headline</title><link>https://trade.example/2</link><pubDate>Mon, 13 Oct 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""
WIRE = """<?xml version="1.0"?><rss version="2.0"><channel><title>w</title>
<item><title>GRAIL announces Galleri data</title><link>https://wire.example/1</link><pubDate>Tue, 14 Oct 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""
SEC_ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K  - Current report</title><link rel="alternate" href="https://www.sec.gov/Archives/{N}"/><updated>2026-10-14T10:00:00-04:00</updated><summary>Item 2.02: Results</summary></entry></feed>"""
GNEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>
<item><title>Google-sourced Illumina story</title><link>https://news.google.com/rss/articles/{N}</link><pubDate>Tue, 14 Oct 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""
# the SEC's company_tickers.json shape (two rows); the CIKs are the real ones so the expected URLs read like production
SEC_TICKER_TABLE = {"0": {"cik_str": 1110803, "ticker": "ILMN", "title": "ILLUMINA, INC."}, "1": {"cik_str": 1699031, "ticker": "GRAL", "title": "GRAIL, Inc."}}
# EDGAR's HTTP 200 text/html answer when its legacy ticker table does not know the symbol (shape recorded live 2026-10-18)
SEC_HTML = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0 Transitional//EN">
<html lang="ENG"><head><title>Company Information: </title><script language="JavaScript" src="/include/sec.js" type="text/javascript"></script></head>
<body style="margin: 0"><p>Home | EDGAR Search Home | Latest Filings | Previous Page --> No matching Ticker Symbol.</body></html>"""


def sec_url(cik_or_ticker):
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_or_ticker}&type=8-K&dateb=&owner=include&count=20&output=atom"


def _gdelt(*titles):
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps({"articles": [{"url": f"https://news.example/{abs(hash(t))}", "title": t, "seendate": "20261015T120000Z", "domain": "news.example"} for t in titles]}).encode()
    r.url = gdelt.DOC_API
    return r


class FakeHttp:
    """get_text answers RSS by URL prefix; get answers GDELT from a per-company table and the SEC ticker table
    (``tickers=None`` makes that download fail, as an unreachable sec.gov would)."""

    def __init__(self, rss, gdelt_by_company, blocked=(), tickers=SEC_TICKER_TABLE):
        self.rss = rss
        self.gdelt_by_company = gdelt_by_company
        self.blocked_hosts = set(blocked)
        self.urls = []
        self.gdelt_queries = []
        self.table_requests = 0
        self.tickers = tickers
        self.stats = {}
        self.contact_email = "contact@example.org"  # track.sec_company_tickers refuses to go online without one

    def get_text(self, url, params=None, cache=False, retries=None):
        self.urls.append(url)
        for prefix, body in self.rss.items():
            if url.startswith(prefix):
                return body.replace("{N}", str(len(self.urls)))  # unique links per fetch, as real feeds have
        return None

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,), retries=None):
        if url == track.SEC_TICKERS_URL:
            self.table_requests += 1
            if self.tickers is None:
                return None
            r = requests.Response()
            r.status_code = 200
            r._content = json.dumps(self.tickers).encode()
            r.url = url
            return r
        assert url == gdelt.DOC_API
        self.gdelt_queries.append(params["query"])
        for name, resp in self.gdelt_by_company.items():
            if params["query"].startswith(f'"{name}"'):
                return resp
        return None


def _pipeline(tmp_path: Path, http, radar=RADAR, contact="contact@example.org", feeds=FEEDS):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    for name, data in (("companies", COMPANIES), ("instruments", INSTRUMENTS), ("feeds", feeds), ("radar", radar)):
        (cfg / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    # cache_dir inside tmp_path: the SEC ticker table is cached there, never in the repository's .cache
    settings = Settings(config_dir=cfg, db_path=tmp_path / "t.sqlite", cache_dir=tmp_path / "cache", contact_email=contact, radar=radar)
    return Pipeline(settings, http=http)


def test_default_news_layer_uses_gdelt_and_skips_google_news(tmp_path):
    http = FakeHttp(
        {"https://trade.example": RSS, "https://wire.example": WIRE, "https://www.sec.gov": SEC_ATOM, "https://news.google.com": GNEWS},
        {"Illumina": _gdelt("Illumina posts results", "Form 4 insider filing for Illumina", "Zacks: Illumina stock", "A story that never names the company"),
         "GRAIL": _gdelt("GRAIL Galleri study", "The holy grail of cooking")},
    )
    pipe = _pipeline(tmp_path, http)
    n = pipe.fetch_news("2026-10-10")
    rows = pipe.store.news_in_window("2026-10-10", limit=100)
    feeds = sorted(r["feed"] for r in rows)
    # publisher + wire + SEC + GDELT stored; nothing from news.google.com
    assert not any(u.startswith("https://news.google.com") for u in http.urls)
    assert pipe.counts["news.gnews_skipped"] == 1 and "news.gnews" not in pipe.counts
    assert feeds.count("gdelt:Illumina") == 1 and feeds.count("gdelt:GRAIL") == 1
    assert "SEC 8-K ILMN" in feeds and "SEC 8-K GRAL" in feeds and "Trade RSS" in feeds and "Wire" in feeds
    assert n == len(rows) == 7
    # exclusions and gazetteer gating on the GDELT items
    titles = {r["title"] for r in rows}
    assert "Illumina posts results" in titles and "GRAIL Galleri study" in titles
    assert "Form 4 insider filing for Illumina" not in titles and "Zacks: Illumina stock" not in titles
    assert "A story that never names the company" not in titles and "The holy grail of cooking" not in titles
    assert pipe.counts["news.excluded"] == 2 and pipe.counts["news.offtopic"] == 2 and pipe.counts["news.gdelt"] == 2
    # GDELT queries carry the quoted name + the type's context clause + language
    assert '"Illumina" (sequencing OR genomics) sourcelang:english' in http.gdelt_queries
    assert '"GRAIL" (cancer OR diagnostic) sourcelang:english' in http.gdelt_queries
    # companies are attached: the trade item names Illumina, the SEC item is tagged with its company
    by_title = {r["title"]: r for r in rows}
    assert by_title["Illumina ships NovaSeq X Plus to a new lab"]["companies"] == ["illumina"]
    sec_rows = [r for r in rows if r["feed"].startswith("SEC")]
    assert all(r["companies"] for r in sec_rows) and sec_rows[0]["title"].startswith(("Illumina:", "GRAIL:"))
    assert not pipe.errors


def test_google_news_opt_in_adds_company_and_feed_queries(tmp_path):
    radar = {**RADAR, "news": {**RADAR["news"], "google_news": True, "gdelt": False}}
    http = FakeHttp({"https://trade.example": RSS, "https://wire.example": WIRE, "https://www.sec.gov": SEC_ATOM, "https://news.google.com": GNEWS}, {})
    pipe = _pipeline(tmp_path, http, radar=radar)
    pipe.fetch_news("2026-10-10")
    gurls = [u for u in http.urls if u.startswith("https://news.google.com")]
    assert len(gurls) == 3  # the feeds.yaml entry + one per watched company
    assert any("when%3A7d" in u and "%22Illumina%22" in u for u in gurls)
    assert http.gdelt_queries == [] and "news.gdelt" not in pipe.counts
    rows = pipe.store.news_in_window("2026-10-10", limit=100)
    assert sum(1 for r in rows if r["feed"].startswith("news:")) == 1  # the Illumina story (gated on the name)
    assert pipe.counts["news.gnews"] == 2  # outlet feed item + company item


def test_sec_feeds_require_a_contact_email(tmp_path):
    http = FakeHttp({"https://trade.example": RSS, "https://wire.example": WIRE, "https://www.sec.gov": SEC_ATOM}, {"Illumina": _gdelt(), "GRAIL": _gdelt()})
    pipe = _pipeline(tmp_path, http, contact="")
    pipe.fetch_news("2026-10-10")
    assert not any(u.startswith("https://www.sec.gov") for u in http.urls) and http.table_requests == 0
    assert any("LITRADAR_CONTACT_EMAIL" in e for e in pipe.errors)


def _sec_urls(http):
    return [u for u in http.urls if u.startswith("https://www.sec.gov")]


def test_sec_feeds_are_requested_by_cik_and_tickers_the_table_lacks_are_reported(tmp_path):
    """EDGAR's legacy CIK=<ticker> lookup answers an HTML "No matching Ticker Symbol." page for recent listings and
    renamed registrants (8 of 46 watched tickers on 2026-10-18), so feeds go out by CIK from company_tickers.json; a
    ticker that table does not know is not requested at all and is named in the run's error list."""
    from litradar.incremental import classify_error, error_sources

    companies = {"entries": COMPANIES["entries"] + [{"id": "newco", "name": "Newco Genomics", "ticker": "NEWCO", "type": "tools", "watch_news": True}]}
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name, data in (("companies", companies), ("instruments", INSTRUMENTS), ("feeds", FEEDS), ("radar", RADAR)):
        (cfg / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    http = FakeHttp({"https://www.sec.gov": SEC_ATOM}, {})
    settings = Settings(config_dir=cfg, db_path=tmp_path / "t.sqlite", cache_dir=tmp_path / "cache", contact_email="contact@example.org", radar=RADAR)
    pipe = Pipeline(settings, http=http)
    pipe.fetch_news("2026-10-10")
    urls = _sec_urls(http)
    assert sorted(urls) == [sec_url("0001110803"), sec_url("0001699031")]  # ILMN, GRAL by 10-digit CIK
    assert not any("CIK=ILMN" in u or "CIK=GRAL" in u or "CIK=NEWCO" in u for u in urls)
    feeds = sorted(r["feed"] for r in pipe.store.news_in_window("2026-10-10", limit=100) if r["feed"].startswith("SEC"))
    assert feeds == ["SEC 8-K GRAL", "SEC 8-K ILMN"]  # the feed label still carries the ticker
    assert pipe.counts["news.sec_feeds"] == 2 and pipe.counts["news.sec_unresolved"] == 1 and "news.sec_not_atom" not in pipe.counts
    sec_errors = [e for e in pipe.errors if e.startswith("sec:")]
    assert sec_errors == ["sec: 1 of 3 watched ticker(s) not in the SEC ticker table (company_tickers.json), no 8-K feed collected for them: "
                          "NEWCO (a foreign, delisted or misspelled ticker in companies.yaml)"]
    assert error_sources(sec_errors) == ["sec"] and classify_error(sec_errors[0]) == "failed"
    # the table was downloaded once and cached on disk: a second pipeline in the same cache dir does not ask again
    assert http.table_requests == 1 and (tmp_path / "cache" / "sec_company_tickers.json").exists()
    pipe2 = Pipeline(settings, http=http, store=pipe.store)
    pipe2.fetch_news("2026-10-10")
    assert http.table_requests == 1 and len(_sec_urls(http)) == 4


def test_sec_html_answer_is_reported_in_the_run_errors_not_only_logged(tmp_path, caplog):
    """An HTTP 200 text/html body (EDGAR's "No matching ..." page, a challenge page) parses to zero items and the HTTP
    layer counts a success, so the run report must hear of it from the news layer: once per source for the SEC feeds,
    naming each ticker with EDGAR's own explanation; per feed for the other kinds."""
    import logging

    from litradar.coverage import error_source_id

    html_trade ="<!doctype html><html><head><title>Just a moment...</title></head><body>challenge</body></html>"
    http = FakeHttp({"https://trade.example": html_trade, "https://wire.example": WIRE, "https://www.sec.gov": SEC_HTML}, {})
    pipe = _pipeline(tmp_path, http, radar={**RADAR, "news": {**RADAR["news"], "gdelt": False}})
    with caplog.at_level(logging.WARNING, logger="litradar.pipeline"):
        n = pipe.fetch_news("2026-10-10")
    rows = pipe.store.news_in_window("2026-10-10", limit=100)
    assert n == len(rows) == 1 and rows[0]["feed"] == "Wire"  # the wire feed still parsed
    assert pipe.counts["news.sec_feeds"] == 2 and pipe.counts["news.sec_not_atom"] == 2 and pipe.counts["news.sec"] == 0
    assert pipe.errors == [
        "feed Trade RSS: not XML (syntax error: line 1, column 0; body starts '<!doctype html><html><head><title>Just a moment...</title></head><body>challenge')",
        "sec: 2 of 2 8-K feed(s) answered an HTML page instead of Atom, no filings collected for them: "
        "GRAL (No matching Ticker Symbol.), ILMN (No matching Ticker Symbol.)",
    ]
    assert [error_source_id(e) for e in pipe.errors] == ["rss", "sec"]  # `--sources rss` / `--sources sec` in the coverage hint
    assert sum("answered an HTML page" in r.getMessage() for r in caplog.records) == 1


def test_sec_ticker_table_unavailable_falls_back_to_ticker_urls_and_says_so(tmp_path):
    http = FakeHttp({"https://www.sec.gov": SEC_ATOM}, {}, tickers=None)
    pipe = _pipeline(tmp_path, http, radar={**RADAR, "news": {**RADAR["news"], "gdelt": False}}, feeds={"feeds": []})
    pipe.fetch_news("2026-10-10")
    assert http.table_requests == 1
    assert sorted(_sec_urls(http)) == [sec_url("GRAL"), sec_url("ILMN")]  # the pre-fix URLs, which EDGAR resolves for most tickers
    assert pipe.counts["news.sec_feeds"] == 2 and pipe.counts["news.sec"] == 2 and "news.sec_unresolved" not in pipe.counts
    assert pipe.errors == ["sec: SEC ticker table unavailable (could not download https://www.sec.gov/files/company_tickers.json (no response)); "
                           "8-K feeds requested by ticker, which EDGAR cannot resolve for recent listings or renamed registrants"]


def test_gdelt_rate_limit_is_reported_not_silent(tmp_path):
    http = FakeHttp({"https://trade.example": RSS, "https://wire.example": WIRE, "https://www.sec.gov": SEC_ATOM}, {}, blocked={"api.gdeltproject.org"})
    pipe = _pipeline(tmp_path, http)
    pipe.fetch_news("2026-10-10")
    assert http.gdelt_queries == [] and pipe.counts["news.gdelt_skipped"] == 2
    assert any(e.startswith("gdelt:") and "429" in e for e in pipe.errors)


# ---- `--sources` and the news layer: Pipeline.run gates it on its own ids, fetch_news(only=...) narrows the layers ----
ALL_RSS = {"https://trade.example": RSS, "https://wire.example": WIRE, "https://www.sec.gov": SEC_ATOM, "https://news.google.com": GNEWS}
GDELT_ANSWERS = {"Illumina": _gdelt("Illumina posts results"), "GRAIL": _gdelt("GRAIL Galleri study")}


def test_run_calls_the_news_layer_for_every_news_source_id(tmp_path, monkeypatch):
    from litradar import pipeline as pl

    pipe = _pipeline(tmp_path, FakeHttp({}, {}))
    seen = []
    monkeypatch.setattr(pipe, "fetch_news", lambda since, only=None: seen.append(tuple(only or ())) or 0)
    monkeypatch.setattr(pipe, "post_process", lambda *a, **k: None)
    for only in (["news"], ["sec"], ["gdelt"], ["press"], ["gnews"], ["rss"], ["biorxiv", "sec"]):
        pipe.run("2026-10-10", "2026-10-17", only=only, fulltext=False)
        assert seen and seen[-1] == tuple(only), only  # `--sources news` used to fetch nothing at all: only `rss` reached the layer
    n = len(seen)
    pipe.run("2026-10-10", "2026-10-17", only=["biorxiv"], fulltext=False)
    assert len(seen) == n  # a paper-only subset leaves the news layer alone
    pipe.run("2026-10-10", "2026-10-17", only=None, fulltext=False)
    assert len(seen) == n + 1 and seen[-1] == ()
    # the ids are the ones the error list, HOST_SOURCES and the status / coverage re-run hints use
    assert set(pl.NEWS_SOURCES) == {"news", "press", "sec", "gdelt", "gnews"} and set(pl.NEWS_SOURCES) <= set(pl.RUN_SOURCES)
    assert {"sec", "gdelt", "gnews"} <= set(pl.HOST_SOURCES.values())
    assert pl.unknown_sources(["news", "sec", "gdelt", "nope"]) == ["nope"] and pl.unknown_sources(None) == []
    assert pl.news_layers(None) is None and pl.news_layers(["rss"]) is None and pl.news_layers(["news", "sec"]) is None
    assert pl.news_layers(["sec", "biorxiv"]) == {"sec"} and pl.news_layers(["biorxiv"]) == set()
    pipe.store.close()


def test_sources_sec_runs_only_the_sec_feeds(tmp_path):
    http = FakeHttp(ALL_RSS, GDELT_ANSWERS)
    pipe = _pipeline(tmp_path, http)
    n = pipe.fetch_news("2026-10-10", only=["sec"])
    assert len(http.urls) == 2 and all(u.startswith("https://www.sec.gov/cgi-bin/browse-edgar?") for u in http.urls)
    assert http.gdelt_queries == []
    feeds = sorted(r["feed"] for r in pipe.store.news_in_window("2026-10-10", limit=100))
    assert feeds == ["SEC 8-K GRAL", "SEC 8-K ILMN"] and n == 2
    assert pipe.counts["news.sec"] == 2 and "news.gdelt" not in pipe.counts and "news.news" not in pipe.counts
    assert not pipe.errors


def test_sources_gdelt_runs_only_gdelt_and_overrides_the_config_switch(tmp_path):
    radar = {**RADAR, "news": {**RADAR["news"], "gdelt": False}}
    http = FakeHttp(ALL_RSS, GDELT_ANSWERS)
    pipe = _pipeline(tmp_path, http, radar=radar)
    pipe.fetch_news("2026-10-10", only=["gdelt"])
    assert http.urls == [] and len(http.gdelt_queries) == 2  # named explicitly: on, like `_enabled` for every other source
    assert pipe.counts["news.gdelt"] == 2 and "news.sec" not in pipe.counts
    feeds = sorted(r["feed"] for r in pipe.store.news_in_window("2026-10-10", limit=100))
    assert feeds == ["gdelt:GRAIL", "gdelt:Illumina"]
    # not named (the whole layer via `news`): the config switch still holds
    (tmp_path / "b").mkdir()
    http2 = FakeHttp(ALL_RSS, GDELT_ANSWERS)
    pipe2 = _pipeline(tmp_path / "b", http2, radar=radar)
    pipe2.fetch_news("2026-10-10", only=["news"])
    assert http2.gdelt_queries == [] and len(http2.urls) == 4  # trade + wire + 2 SEC


def test_sources_press_and_gnews_subsets(tmp_path):
    http = FakeHttp(ALL_RSS, GDELT_ANSWERS)
    pipe = _pipeline(tmp_path, http)
    pipe.fetch_news("2026-10-10", only=["press"])
    assert http.urls == ["https://wire.example/rss"] and http.gdelt_queries == []
    assert [r["feed"] for r in pipe.store.news_in_window("2026-10-10", limit=100)] == ["Wire"]
    # Google News is a legal opt-in: naming it in --sources alone fetches nothing (news.google_news stays false)
    (tmp_path / "g").mkdir()
    http2 = FakeHttp(ALL_RSS, GDELT_ANSWERS)
    pipe2 = _pipeline(tmp_path / "g", http2)
    assert pipe2.fetch_news("2026-10-10", only=["gnews"]) == 0
    assert http2.urls == [] and http2.gdelt_queries == [] and pipe2.counts["news.gnews_skipped"] == 1


def test_sources_news_runs_every_layer_like_a_full_run(tmp_path):
    http = FakeHttp(ALL_RSS, GDELT_ANSWERS)
    pipe = _pipeline(tmp_path, http)
    n = pipe.fetch_news("2026-10-10", only=["news"])
    rows = pipe.store.news_in_window("2026-10-10", limit=100)
    assert n == len(rows) == 7
    assert {r["feed"] for r in rows} == {"SEC 8-K GRAL", "SEC 8-K ILMN", "Trade RSS", "Wire", "gdelt:GRAIL", "gdelt:Illumina"}
    assert not any(u.startswith("https://news.google.com") for u in http.urls)


def test_cli_run_and_update_reject_unknown_source_ids(tmp_path, monkeypatch, capsys):
    from litradar import cli

    db = tmp_path / "none.sqlite"
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)
    assert cli.main(["run", "--days", "1", "--sources", "nope"]) == 2
    err = capsys.readouterr().err
    assert "litradar run: error: unknown source(s) nope; choose from:" in err and "news" in err and "sec" in err and "gdelt" in err
    assert cli.main(["run", "--days", "1", "--sources", "news,bogus,rss"]) == 2
    assert "unknown source(s) bogus" in capsys.readouterr().err
    assert cli.main(["run", "--days", "1", "--sources", ","]) == 2
    assert "--sources is empty" in capsys.readouterr().err
    assert not db.exists()  # rejected before the pipeline (and its store) is built
    assert cli.main(["update", "--dry-run", "--sources", "sec,bogus"]) == 2
    assert "litradar update: error: unknown source(s) bogus" in capsys.readouterr().err
    assert cli.main(["update", "--dry-run", "--sources", "sec,gdelt"]) == 0  # the news ids are valid for `update` too
    assert json.loads(capsys.readouterr().out)["sources"] == ["sec", "gdelt"]
