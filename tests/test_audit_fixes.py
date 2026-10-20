"""Regression tests for the fresh-clone onboarding audit (offline).

Covers: `status` counts failures only (configuration skips and rate limiting are reported, not counted; host labels
map to their owning source); GDELT company queries are batched and attributed per batch member; identical error
lines are collapsed with a count in `status` and `coverage`; a host label never becomes a bogus `--sources` value;
`fetched.<source>` is written as 0 for a pull that returned nothing so coverage can tell "pulled 0" from "not
pulled"; `update --dry-run` creates no database; `digest` before the first run exits 1 with a hint.
"""
import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest
import requests
import yaml

from litradar import coverage as C
from litradar import incremental
from litradar import pipeline as pl
from litradar.cli import main
from litradar.config import DEFAULT_CONFIG_DIR, Settings
from litradar.pipeline import Pipeline
from litradar.sources import gdelt
from litradar.store import Store

NOW = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc)
SINCE, UNTIL = "2026-10-11", "2026-10-18"

SEC_SKIP = "sec: SEC 8-K feeds skipped (LITRADAR_CONTACT_EMAIL not set; EDGAR requires '<tool> <contact e-mail>' in the User-Agent and answers 403 otherwise)"
GDELT_429 = "gdelt: host rate-limited (HTTP 429) during the run; 9 of 10 quer(y/ies) skipped (68 companies not searched)"
NIH_DOWN = "nih_reporter: the API answered with its 'system unavailable' page"
RSS_HOST = "rss: all 1 request(s) to www.example-publisher.com failed; no successful response this run"


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_run(store: Store, since: str, until: str, finished: datetime, counts=None, errors=None):
    report = {"since": since, "until": until, "counts": counts or {}, "errors": errors or [], "http": {"requests": 3}}
    store.conn.execute(
        "INSERT INTO runs (started, finished, since, until, counts_json) VALUES (?,?,?,?,?)",
        (_ts(finished - timedelta(hours=1)), _ts(finished), since, until, json.dumps(report)),
    )
    store.conn.commit()


# ------------------------------------------------------------------ finding 1: the status exit rule ---------------
def test_classify_error_sorts_configuration_rate_limits_and_failures():
    assert incremental.classify_error(SEC_SKIP) == "config"
    assert incremental.classify_error("openalex: HTTP 429: Insufficient budget") == "config"  # a key fixes it, waiting does not
    assert incremental.classify_error(GDELT_429) == "degraded"
    assert incremental.classify_error("arxiv: host export.arxiv.org rate-limited (HTTP 429 2 times in a row); skipped for the rest of the run") == "degraded"
    assert incremental.classify_error(NIH_DOWN) == "failed"
    assert incremental.classify_error("sra: fetched 1429 experiments then timed out") == "failed"  # a bare number is not a status code
    assert incremental.sources_by_class([SEC_SKIP, GDELT_429, NIH_DOWN, NIH_DOWN, RSS_HOST]) == {
        "failed": ["nih_reporter", "rss"], "degraded": ["gdelt"], "config": ["sec"]}


def test_status_counts_failures_only_and_keeps_the_limit_flag(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _seed_run(st, SINCE, UNTIL, NOW - timedelta(hours=1), counts={"papers.new": 1}, errors=[SEC_SKIP, GDELT_429, NIH_DOWN, NIH_DOWN, RSS_HOST])
    rep = incremental.health(st.conn, st.path, now=NOW)
    # four distinct sources errored, two of them failed: within the default limit of two -> exit 0
    lu = rep["last_update"]
    assert lu["error_sources"] == ["sec", "gdelt", "nih_reporter", "rss"]
    assert lu["failing_sources"] == ["nih_reporter", "rss"] and lu["degraded_sources"] == ["gdelt"] and lu["config_skipped_sources"] == ["sec"]
    assert rep["ok"] is True and incremental.exit_code(rep) == 0
    text = incremental.format_status(rep)
    assert "[configuration, not counted]" in text and "[degraded: rate-limited, not counted]" in text
    assert "(x2)" in text and text.count("system unavailable") == 1  # identical NIH lines collapsed
    assert "failing sources counted against the limit: nih_reporter, rss; degraded (rate-limited): gdelt; configuration skips: sec" in text
    # a third real failure tips it, and the problem line says what was left out of the count
    st2 = Store(tmp_path / "t2.sqlite")
    _seed_run(st2, SINCE, UNTIL, NOW - timedelta(hours=1), errors=[SEC_SKIP, GDELT_429, NIH_DOWN, RSS_HOST, "europepmc: HTTP 500"])
    rep2 = incremental.health(st2.conn, st2.path, now=NOW)
    assert incremental.exit_code(rep2) == 1
    assert any("errors on 3 sources (limit 2): nih_reporter, rss, europepmc (not counted, rate-limited: gdelt; not counted, configuration: sec)" in p for p in rep2["problems"])
    # --max-failing-sources still raises the tolerance for real failures
    assert incremental.exit_code(incremental.health(st2.conn, st2.path, now=NOW, max_failing_sources=3)) == 0
    # the whole keyless-fork case: SEC skip + GDELT 429 only -> green
    st3 = Store(tmp_path / "t3.sqlite")
    _seed_run(st3, SINCE, UNTIL, NOW - timedelta(hours=1), errors=[SEC_SKIP, GDELT_429])
    assert incremental.exit_code(incremental.health(st3.conn, st3.path, now=NOW)) == 0


def test_update_summary_names_degraded_and_configuration_sources():
    plan = incremental.compute_window(None, today=NOW.date())
    report = {"since": plan["since"], "until": plan["until"], "counts": {"papers.unique": 3}, "errors": [SEC_SKIP, GDELT_429, "rss Example: 404"], "http": {}}
    summary = incremental.summarize_update(report, plan, _ts(NOW), _ts(NOW), incremental.Path("/nonexistent/radar.sqlite"), None)
    assert summary["sources_by_class"] == {"failed": ["rss"], "degraded": ["gdelt"], "config": ["sec"]}
    text = incremental.summary_paragraph(summary)
    assert "3 error(s) on 3 source(s): sec, gdelt, rss (degraded by rate limiting: gdelt; configuration skips: sec; not counted by `status`)." in text


# ------------------------------------------------------------------ finding 2: GDELT batching in the pipeline -----
COMPANIES = {
    "entries": [
        {"id": "illumina", "name": "Illumina", "ticker": "ILMN", "type": "tools", "watch_news": True},
        {"id": "10x-genomics", "name": "10x Genomics", "ticker": "TXG", "type": "tools", "watch_news": True},
        {"id": "grail", "name": "GRAIL", "ticker": "GRAL", "type": "diagnostics", "watch_news": True, "case_sensitive_aliases": ["GRAIL"]},
        {"id": "quiet-co", "name": "Quiet Co", "ticker": "private", "type": "tools", "watch_news": False},
    ]
}
INSTRUMENTS = {"entries": [{"id": "ilmn-novaseq-x", "name": "NovaSeq X", "vendor": "Illumina", "ticker": "ILMN", "category": "sequencer.short_read"}]}
FEEDS = {"feeds": [{"name": "Publisher feed", "kind": "news", "url": "https://www.example-publisher.com/rss"}]}
RADAR = {
    "sources": {"sec": {"enabled": False}},
    "news": {"gdelt": True, "google_news": False, "query_context": {"tools": "(sequencing OR genomics)", "diagnostics": "(cancer OR diagnostic)"}},
}
TOOLS_BATCH = '("Illumina" OR "10x Genomics") (sequencing OR genomics) sourcelang:english'
GRAIL_QUERY = '"GRAIL" (cancer OR diagnostic) sourcelang:english'


def _gdelt_response(*titles):
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps({"articles": [{"url": f"https://news.example/{abs(hash(t))}", "title": t, "seendate": "20261015T120000Z"} for t in titles]}).encode()
    r.url = gdelt.DOC_API
    return r


class FakeHttp:
    """get() answers GDELT by exact query; get_text() answers RSS by URL prefix; host_report() is scripted."""

    def __init__(self, by_query=None, rss=None, blocked=(), report=None):
        self.by_query = by_query or {}
        self.rss = rss or {}
        self.blocked_hosts = set(blocked)
        self.queries = []
        self.urls = []
        self.stats = {}
        self._report = report or {}

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,), retries=None):
        assert url == gdelt.DOC_API
        self.queries.append(params["query"])
        return self.by_query.get(params["query"])

    def get_text(self, url, params=None, cache=False, retries=None):
        self.urls.append(url)
        for prefix, body in self.rss.items():
            if url.startswith(prefix):
                return body
        return None

    def host_report(self):
        return dict(self._report)


def _pipeline(tmp_path, http, radar=RADAR, feeds=FEEDS):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    for name, data in (("companies", COMPANIES), ("instruments", INSTRUMENTS), ("feeds", feeds), ("radar", radar)):
        (cfg / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    settings = Settings(config_dir=cfg, db_path=tmp_path / "t.sqlite", contact_email="", radar=radar)
    return Pipeline(settings, http=http)


def test_gdelt_batches_companies_by_context_and_attributes_articles_per_member(tmp_path):
    http = FakeHttp(by_query={
        TOOLS_BATCH: _gdelt_response("Illumina posts results", "10x Genomics launches a new Xenium panel", "A story about neither of them"),
        GRAIL_QUERY: _gdelt_response("GRAIL Galleri study", "The holy grail of cooking"),
    })
    pipe = _pipeline(tmp_path, http)
    n = pipe.fetch_news("2026-10-10")
    # two requests for three companies: the two "tools" companies share one OR'd query, GRAIL has its own context
    assert http.queries == [TOOLS_BATCH, GRAIL_QUERY]
    assert pipe.counts["news.gdelt_queries"] == 2 and pipe.counts["news.gdelt"] == 3 and n == 3
    rows = pipe.store.news_in_window("2026-10-10", limit=100)
    by_title = {r["title"]: r for r in rows}
    assert by_title["Illumina posts results"]["feed"] == "gdelt:Illumina" and by_title["Illumina posts results"]["companies"] == ["illumina"]
    assert by_title["10x Genomics launches a new Xenium panel"]["feed"] == "gdelt:10x Genomics" and by_title["10x Genomics launches a new Xenium panel"]["companies"] == ["10x-genomics"]
    assert by_title["GRAIL Galleri study"]["feed"] == "gdelt:GRAIL"
    # an article from a batch that names none of the batch members is off-topic, exactly as before
    assert "A story about neither of them" not in by_title and "The holy grail of cooking" not in by_title
    assert pipe.counts["news.offtopic"] == 2 and not pipe.errors and "news.gdelt_skipped" not in pipe.counts


def test_gdelt_batch_size_one_restores_per_company_queries(tmp_path):
    radar = {**RADAR, "news": {**RADAR["news"], "gdelt_batch_size": 1}}
    http = FakeHttp(by_query={})
    pipe = _pipeline(tmp_path, http, radar=radar)
    pipe.fetch_news("2026-10-10")
    assert http.queries == ['"Illumina" (sequencing OR genomics) sourcelang:english', '"10x Genomics" (sequencing OR genomics) sourcelang:english', GRAIL_QUERY]
    assert pipe.counts["news.gdelt_queries"] == 3 and pipe.counts["news.gdelt_failed_queries"] == 3  # None = no usable answer
    assert pipe.counts["news.gdelt"] == 0 and "news.gdelt" in pipe.counts  # asked, got nothing: written as a zero


def test_gdelt_searches_a_too_short_name_by_its_alias_and_leaves_hopeless_ones_out(tmp_path, caplog):
    """GDELT answers 'The specified phrase is too short.' for a quoted name under five characters and voids the whole
    query: a short name is searched by its first long-enough alias; one without such an alias is left out and counted."""
    companies = {"entries": [
        {"id": "seer", "name": "Seer", "type": "tools", "watch_news": True, "aliases": ["Seer Inc", "Seer Bio"], "case_sensitive_aliases": ["Seer"]},
        {"id": "abc", "name": "Abc", "type": "tools", "watch_news": True},
        {"id": "illumina", "name": "Illumina", "type": "tools", "watch_news": True},
    ]}
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name, data in (("companies", companies), ("instruments", INSTRUMENTS), ("feeds", {"feeds": []}), ("radar", RADAR)):
        (cfg / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    query = '("Seer Inc" OR "Illumina") (sequencing OR genomics) sourcelang:english'
    http = FakeHttp(by_query={query: _gdelt_response("Seer Inc reports proteomics growth", "Illumina posts results")})
    pipe = Pipeline(Settings(config_dir=cfg, db_path=tmp_path / "t.sqlite", contact_email="", radar=RADAR), http=http)
    with caplog.at_level("INFO", logger="litradar.pipeline"):
        pipe.fetch_news("2026-10-10")
    assert http.queries == [query]  # "Seer" became its alias, "Abc" was never sent
    assert pipe.counts["news.gdelt_short_names"] == 1 and "Abc" in caplog.text and "shorter than 5 characters" in caplog.text
    rows = {r["title"]: r for r in pipe.store.news_in_window("2026-10-10", limit=10)}
    assert rows["Seer Inc reports proteomics growth"]["feed"] == "gdelt:Seer"  # the label is the company name, not the alias
    assert rows["Seer Inc reports proteomics growth"]["companies"] == ["seer"]


def test_gdelt_blocked_host_is_reported_once_as_degraded_with_query_and_company_counts(tmp_path, caplog):
    http = FakeHttp(blocked={gdelt.HOST})
    pipe = _pipeline(tmp_path, http)
    with caplog.at_level("WARNING", logger="litradar.pipeline"):
        pipe.fetch_news("2026-10-10")
    assert http.queries == []
    assert pipe.counts["news.gdelt_queries"] == 2 and pipe.counts["news.gdelt_skipped"] == 2 and pipe.counts["news.gdelt_companies_skipped"] == 3
    gdelt_errors = [e for e in pipe.errors if e.startswith("gdelt:")]
    assert gdelt_errors == ["gdelt: host rate-limited (HTTP 429) during the run; 2 of 2 quer(y/ies) skipped (3 companies not searched)"]
    assert caplog.text.count("news degraded:") == 1
    assert incremental.classify_error(gdelt_errors[0]) == "degraded"


# ------------------------------------------------------------------ findings 3 + 4: dedupe and host labels ----------
@pytest.fixture
def default_settings(tmp_path, monkeypatch):
    shutil.copytree(DEFAULT_CONFIG_DIR, tmp_path / "config")
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "radar.sqlite"))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("LITRADAR_CONTACT_EMAIL", raising=False)
    s = Settings.from_env(config_dir=tmp_path / "config")
    s.db_path = tmp_path / "radar.sqlite"
    s.contact_email = ""
    s.openalex_api_key = ""
    return s


def test_coverage_collapses_identical_errors_into_one_row_and_one_suggestion(default_settings):
    st = Store(default_settings.db_path)
    st.record_run("2026-10-18T06:00:00Z", SINCE, UNTIL, {"counts": {"fetched.biorxiv": 3}, "errors": [NIH_DOWN] * 7 + [GDELT_429], "http": {}})
    r = C.coverage_report(st, SINCE, UNTIL, default_settings)
    rows = {e["source"]: e for e in r["errored_sources"]}
    assert len(r["errored_sources"]) == 2 and rows["nih_reporter"]["count"] == 7 and rows["nih_reporter"]["source_id"] == "nih_reporter"
    assert rows["gdelt"]["rate_limited"] is True and rows["gdelt"]["config"] is False and rows["gdelt"]["count"] == 1
    nih = [s for s in r["suggestions"] if s.startswith("nih_reporter")]
    assert len(nih) == 1 and "(7 identical errors)" in nih[0] and "`litradar run --sources nih_reporter -v`" in nih[0]
    assert any(s.startswith("gdelt was rate-limited") and "degraded, not an outage" in s for s in r["suggestions"])
    md = C.render_coverage_markdown(st, SINCE, UNTIL, default_settings, report=r)
    # one bullet in "Sources that errored" (with the count) and one suggestion, instead of seven of each
    assert md.count("- **nih_reporter**") == 1 and md.count("system unavailable") == 2 and "(x7)" in md
    assert "_(rate-limited: degraded, not an outage)_" in md


def test_host_labels_map_to_the_owning_source_or_print_plainly(default_settings):
    hosts = C.feed_hosts(default_settings)
    assert "www.genomeweb.com" in hosts  # the default feed list carries the GenomeWeb Arc feed
    assert C.error_source_id("www.genomeweb.com", hosts) == "rss"
    assert C.error_source_id("cdn.unknown-host.net", hosts) == "cdn.unknown-host.net"
    assert C.error_source_id("www.genomeweb.com") == "www.genomeweb.com"  # never 'www', even without the feed table
    assert C.error_source_id("SEC 8-K feeds skipped") == "sec" and C.error_source_id("feed Example") == "rss"
    st = Store(default_settings.db_path)
    st.record_run("2026-10-18T06:00:00Z", SINCE, UNTIL, {"counts": {}, "errors": [
        "www.genomeweb.com: all 1 request(s) to www.genomeweb.com failed; no successful response this run",
        "cdn.unknown-host.net: all 2 request(s) to cdn.unknown-host.net failed; no successful response this run",
    ], "http": {}})
    r = C.coverage_report(st, SINCE, UNTIL, default_settings)
    ids = {e["source"]: e["source_id"] for e in r["errored_sources"]}
    assert ids == {"www.genomeweb.com": "rss", "cdn.unknown-host.net": "cdn.unknown-host.net"}
    sugg = "\n".join(r["suggestions"])
    assert "`litradar run --sources rss -v`" in sugg and "--sources www" not in sugg and "--sources cdn" not in sugg
    assert "host cdn.unknown-host.net answered nothing but errors" in sugg


def test_pipeline_labels_a_dead_feed_host_as_rss(tmp_path):
    report = {"blocked_hosts": [], "failed_only_hosts": ["www.example-publisher.com", "api.reporter.nih.gov", "cdn.unknown-host.net"],
              "failures_by_host": {"www.example-publisher.com": 1, "api.reporter.nih.gov": 7, "cdn.unknown-host.net": 2}}
    pipe = _pipeline(tmp_path, FakeHttp(report=report))
    pipe._fold_http_errors()
    assert pipe.errors == [
        "rss: all 1 request(s) to www.example-publisher.com failed; no successful response this run",
        "nih_reporter: all 7 request(s) to api.reporter.nih.gov failed; no successful response this run",
        "cdn.unknown-host.net: all 2 request(s) to cdn.unknown-host.net failed; no successful response this run",
    ]
    assert incremental.error_sources(pipe.errors) == ["rss", "nih_reporter", "cdn.unknown-host.net"]


# ------------------------------------------------------------------ finding 5: zero counters ----------------------
def test_fetch_papers_writes_a_zero_counter_for_a_pull_that_returned_nothing(tmp_path, monkeypatch):
    pipe = _pipeline(tmp_path, FakeHttp())
    monkeypatch.setattr(pl.arxiv_src, "fetch", lambda *a, **k: iter([]))
    papers = pipe.fetch_papers(SINCE, UNTIL, only=["arxiv"])
    assert papers == {} and pipe.counts["fetched.arxiv"] == 0 and "fetched.arxiv" in pipe.counts
    assert "fetched.crossref" not in pipe.counts  # not pulled: no key at all

    def boom(*a, **k):
        raise RuntimeError("down")

    pipe2 = _pipeline(tmp_path, FakeHttp())
    monkeypatch.setattr(pl.arxiv_src, "fetch", boom)
    pipe2.fetch_papers(SINCE, UNTIL, only=["arxiv"])
    assert "fetched.arxiv" not in pipe2.counts and pipe2.errors == ["arxiv: down"]  # failed before returning: not "pulled 0"


def test_coverage_tells_pulled_zero_from_not_pulled(default_settings):
    st = Store(default_settings.db_path)
    st.record_run("2026-10-18T06:00:00Z", "2026-10-17", "2026-10-18", {"counts": {"fetched.biorxiv": 3, "fetched.arxiv": 0, "fetched.europepmc.journals": 0}, "errors": [], "http": {}})
    r = C.coverage_report(st, "2026-10-17", "2026-10-18", default_settings)
    sugg = {s.split(" is enabled")[0]: s for s in r["suggestions"] if s.startswith("Source ")}
    assert "was pulled in the last run and returned 0 records" in sugg["Source arxiv"] and "normal on a window this short" in sugg["Source arxiv"]
    assert "was pulled in the last run and returned 0 records" in sugg["Source europepmc"]
    assert "was not pulled in the last run" in sugg["Source crossref"] and "was not pulled in the last run" in sugg["Source openalex"]
    assert "Source biorxiv" not in sugg or "fetched 3 record(s)" in sugg["Source biorxiv"]


# ------------------------------------------------------------------ findings 6 + 7: side effects of dry-run / digest
@pytest.fixture
def live_env(tmp_path, monkeypatch):
    db = tmp_path / "data" / "radar.sqlite"
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "digests"))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setattr(incremental, "utc_now", lambda: NOW)
    return {"db": db, "out": tmp_path / "digests"}


def test_update_dry_run_creates_no_database(live_env, capsys, monkeypatch):
    def boom(self, *a, **k):
        raise AssertionError("Pipeline.run must not be called in a dry run")

    monkeypatch.setattr(Pipeline, "run", boom)
    assert main(["update", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["last_run"] is None and plan["plan"]["basis"] == "first_run"
    db = live_env["db"]
    assert not db.exists() and not db.with_name("radar.sqlite-wal").exists() and not db.with_name("radar.sqlite-shm").exists()
    assert not db.parent.exists()  # not even the data/ directory
    # with an existing database the plan continues from its last run, read-only
    st = Store(db)
    _seed_run(st, "2026-10-08", "2026-10-15", NOW - timedelta(days=1))
    st.close()
    assert main(["update", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["plan"]["basis"] == "last_run" and plan["last_run"]["until"] == "2026-10-15"


def test_open_readonly_never_creates_a_file(tmp_path):
    assert incremental.open_readonly(tmp_path / "missing.sqlite") is None and not (tmp_path / "missing.sqlite").exists()
    st = Store(tmp_path / "t.sqlite")
    _seed_run(st, SINCE, UNTIL, NOW)
    st.close()
    conn = incremental.open_readonly(tmp_path / "t.sqlite")
    assert conn is not None and incremental.last_run(conn)["until"] == UNTIL
    conn.close()


def test_digest_before_the_first_run_exits_1_with_a_hint_and_creates_nothing(live_env, capsys):
    assert main(["digest", "--days", "7"]) == 1
    err = capsys.readouterr().err
    assert "no runs yet; run `litradar run --days 7` first" in err and "--allow-empty" in err
    assert not live_env["db"].exists() and not live_env["out"].exists()
    # a database with no completed run is the same case
    st = Store(live_env["db"])
    st.close()
    assert main(["digest", "--days", "7"]) == 1
    assert "holds no completed run" in capsys.readouterr().err and not live_env["out"].exists()
    # --allow-empty keeps the old behaviour for a deliberately empty render
    assert main(["digest", "--days", "7", "--allow-empty"]) == 0
    assert "digest written to" in capsys.readouterr().out and (live_env["out"] / "latest.md").exists()
    # after a run it renders as before
    st = Store(live_env["db"])
    _seed_run(st, SINCE, UNTIL, NOW - timedelta(hours=1))
    st.close()
    assert main(["digest", "--since", SINCE, "--until", UNTIL]) == 0


def test_throttling_and_partial_host_failures_are_degraded_not_failed():
    from litradar.incremental import classify_error

    assert classify_error("semantic_scholar: rate-limited (shared pool throttling): 2 batch(es) failed and no DOI was matched") == "degraded"
    assert classify_error("rss: degraded: all 3 request(s) to www.example.org failed while the source's other hosts answered (219 record(s) fetched)") == "degraded"
    assert classify_error("rss: all 3 request(s) to www.example.org failed; no successful response this run") == "failed"
    assert classify_error("kci: KCI OAI-PMH: empty response (HTTP_ERROR)") == "failed"
