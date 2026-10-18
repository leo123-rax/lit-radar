"""Pipeline.fetch_news against a tiny config directory and a fake HTTP layer (no network).

Covers: Google News is opt-in (gnews feeds skipped by default, used when news.google_news is true),
per-company news comes from GDELT with gazetteer gating and title exclusions, SEC feeds need a contact
e-mail, publisher/press feeds are kept, GDELT rate-limiting is reported in errors.
"""
import json
from pathlib import Path

import requests
import yaml

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


def _gdelt(*titles):
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps({"articles": [{"url": f"https://news.example/{abs(hash(t))}", "title": t, "seendate": "20261015T120000Z", "domain": "news.example"} for t in titles]}).encode()
    r.url = gdelt.DOC_API
    return r


class FakeHttp:
    """get_text answers RSS by URL prefix; get answers GDELT from a per-company table."""

    def __init__(self, rss, gdelt_by_company, blocked=()):
        self.rss = rss
        self.gdelt_by_company = gdelt_by_company
        self.blocked_hosts = set(blocked)
        self.urls = []
        self.gdelt_queries = []
        self.stats = {}

    def get_text(self, url, params=None, cache=False, retries=None):
        self.urls.append(url)
        for prefix, body in self.rss.items():
            if url.startswith(prefix):
                return body.replace("{N}", str(len(self.urls)))  # unique links per fetch, as real feeds have
        return None

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,), retries=None):
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
    settings = Settings(config_dir=cfg, db_path=tmp_path / "t.sqlite", contact_email=contact, radar=radar)
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
    assert not any(u.startswith("https://www.sec.gov") for u in http.urls)
    assert any("LITRADAR_CONTACT_EMAIL" in e for e in pipe.errors)


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
