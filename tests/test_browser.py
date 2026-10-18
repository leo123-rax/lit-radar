"""BrowserFetcher / BrowserHttp with Playwright mocked entirely (no browser, no network, no sleeping).

The fake driver below mirrors the four Playwright calls the module makes: `chromium.connect_over_cdp`,
`chromium.launch_persistent_context`, `context.new_page()` and `page.goto()` returning a response with
`.status` and `.body()`. Tests check the allow-list, the login-path refusal, CDP-vs-launch selection,
per-host pacing, the page budget, the log line per fetch, and BrowserHttp's delegation and caching.
"""
import logging
from pathlib import Path

import pytest
import requests

from litradar import browser as br
from litradar.browser import (
    BrowserBudgetExhausted,
    BrowserFetcher,
    BrowserHttp,
    BrowserRefused,
    BrowserUnavailable,
    make_http,
)
from litradar.config import Settings

# ------------------------------------------------------------------ fake Playwright


class FakeResponse:
    def __init__(self, status=200, body=b"<article/>"):
        self.status = status
        self._body = body

    def body(self):
        return self._body


class FakePage:
    def __init__(self, context):
        self.context = context
        self.closed = False
        self.visited = []

    def goto(self, url, wait_until="domcontentloaded", timeout=None):
        self.visited.append((url, wait_until, timeout))
        self.context.driver.visited.append(url)
        outcome = self.context.driver.outcomes.pop(0) if self.context.driver.outcomes else FakeResponse()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, driver, label):
        self.driver = driver
        self.label = label
        self.pages = []
        self.closed = False

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, driver, contexts):
        self.driver = driver
        self.contexts = contexts
        self.closed = False

    def new_context(self):
        ctx = FakeContext(self.driver, "cdp-new")
        self.contexts.append(ctx)
        return ctx

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, driver):
        self.driver = driver
        self.cdp_calls = []
        self.launch_calls = []

    def connect_over_cdp(self, url, timeout=None):
        self.cdp_calls.append((url, timeout))
        if self.driver.cdp_error:
            raise self.driver.cdp_error
        contexts = [FakeContext(self.driver, "cdp-existing")] if self.driver.cdp_has_context else []
        self.driver.browser = FakeBrowser(self.driver, contexts)
        return self.driver.browser

    def launch_persistent_context(self, user_data_dir, headless=True):
        self.launch_calls.append((user_data_dir, headless))
        self.driver.launched = FakeContext(self.driver, "launched")
        return self.driver.launched


class FakeDriver:
    """What sync_playwright().start() returns."""

    def __init__(self, outcomes=None, cdp_has_context=True, cdp_error=None):
        self.outcomes = list(outcomes or [])
        self.visited = []
        self.cdp_has_context = cdp_has_context
        self.cdp_error = cdp_error
        self.browser = None
        self.launched = None
        self.stopped = False
        self.chromium = FakeChromium(self)

    def stop(self):
        self.stopped = True


def _fetcher(monkeypatch, driver=None, cdp_up=True, **kw):
    driver = driver or FakeDriver()
    sleeps = []
    monkeypatch.setattr(br.time, "sleep", lambda s: sleeps.append(s))
    clock = {"t": 1000.0}
    monkeypatch.setattr(br.time, "monotonic", lambda: clock["t"])
    f = BrowserFetcher(playwright_factory=lambda: driver, cdp_probe=lambda url: cdp_up, **kw)
    return f, driver, sleeps, clock


# ------------------------------------------------------------------ policy: allow-list and forbidden paths


def test_refuses_hosts_outside_the_allow_list_without_connecting(monkeypatch):
    f, driver, _, _ = _fetcher(monkeypatch)
    with pytest.raises(BrowserRefused) as exc:
        f.get("https://www.nature.com/articles/s41586-026-0001-1")
    assert "not on the browser allow-list" in str(exc.value) and "www.nature.com" in str(exc.value)
    assert driver.visited == [] and f.mode == "" and f.stats["refused"] == 1
    with pytest.raises(BrowserRefused):
        f.get("ftp://www.biorxiv.org/x")
    assert f.allows("https://www.biorxiv.org/content/x") and not f.allows("https://biorxiv.org/content/x")


@pytest.mark.parametrize("url", [
    "https://www.biorxiv.org/login",
    "https://www.biorxiv.org/user/login?destination=node",
    "https://www.medrxiv.org/sign-in",
    "https://www.biorxiv.org/account/settings",
    "https://www.biorxiv.org/subscribe",
    "https://www.biorxiv.org/content/x?token=abc",
    "https://www.biorxiv.org/content/x?session=1",
    "https://www.biorxiv.org/institutional/access",
    "https://www.biorxiv.org/checkout",
])
def test_refuses_login_subscriber_and_account_paths_even_on_allowed_hosts(monkeypatch, url):
    f, driver, _, _ = _fetcher(monkeypatch)
    with pytest.raises(BrowserRefused) as exc:
        f.get(url)
    assert "login / subscriber / account" in str(exc.value)
    assert driver.visited == []


def test_ordinary_content_urls_are_not_mistaken_for_login_pages(monkeypatch):
    f, _, _, _ = _fetcher(monkeypatch)
    for url in (
        "https://www.biorxiv.org/content/biorxiv/early/2026/10/10/2026.10.01.1.source.xml",
        "https://www.biorxiv.org/content/10.1101/2026.10.01.1v1.full",
        "https://www.medrxiv.org/content/10.1101/2026.10.01.1v2.source.xml?versioned=true",
        "https://www.biorxiv.org/collection/genomics",  # 'tokenomics'-style substrings must not trip the regex
        "https://www.biorxiv.org/content/accounting-for-batch-effects",
    ):
        assert f.check_url(url) in ("www.biorxiv.org", "www.medrxiv.org"), url


def test_custom_allow_list_is_lower_cased_and_required():
    f = BrowserFetcher(allowed_hosts=["Www.Example-Publisher.org "], playwright_factory=lambda: FakeDriver())
    assert f.allowed_hosts == frozenset({"www.example-publisher.org"})
    with pytest.raises(ValueError):
        BrowserFetcher(allowed_hosts=[], playwright_factory=lambda: FakeDriver())


# ------------------------------------------------------------------ availability


def test_missing_playwright_gives_an_install_hint(monkeypatch):
    monkeypatch.setattr(br, "playwright_installed", lambda: False)
    with pytest.raises(BrowserUnavailable) as exc:
        BrowserFetcher()
    assert "pip install 'lit-radar[browser]'" in str(exc.value)
    # an injected driver (tests, or a caller with its own Playwright) bypasses the import check
    BrowserFetcher(playwright_factory=lambda: FakeDriver())


# ------------------------------------------------------------------ connection: CDP first, launch otherwise


def test_connects_to_the_users_chrome_over_cdp_and_reuses_its_context(monkeypatch, caplog):
    f, driver, _, _ = _fetcher(monkeypatch, cdp_up=True, cdp_url="http://localhost:9333", timeout=12)
    with caplog.at_level(logging.INFO, logger="litradar.browser"):
        body = f.get("https://www.biorxiv.org/content/early/2026/10/10/2026.10.01.1.source.xml")
    assert body == b"<article/>" and f.mode == "cdp"
    assert driver.chromium.cdp_calls == [("http://localhost:9333", 12000)] and driver.chromium.launch_calls == []
    ctx = driver.browser.contexts[0]
    assert ctx.label == "cdp-existing" and ctx.pages[0].closed  # tab opened in the user's session, then closed
    assert ctx.pages[0].visited[0][1:] == ("domcontentloaded", 12000)
    line = [r.getMessage() for r in caplog.records if "browser GET" in r.getMessage()][0]
    assert "-> 200, 10 bytes" in line and "(page 1/50, cdp)" in line
    f.close()
    assert driver.browser.closed and not ctx.closed and driver.stopped  # detach, never close the user's context


def test_cdp_without_a_default_context_creates_one_and_closes_it(monkeypatch):
    f, driver, _, _ = _fetcher(monkeypatch, driver=FakeDriver(cdp_has_context=False))
    f.get("https://www.biorxiv.org/content/x.source.xml")
    ctx = driver.browser.contexts[0]
    assert ctx.label == "cdp-new"
    f.close()
    assert ctx.closed


def test_launches_a_persistent_chromium_when_no_cdp_endpoint_answers(monkeypatch, tmp_path):
    f, driver, _, _ = _fetcher(monkeypatch, cdp_up=False, user_data_dir=tmp_path / "profile", headless=False)
    assert f.get("https://www.medrxiv.org/content/y.source.xml") == b"<article/>"
    assert f.mode == "launched" and driver.chromium.cdp_calls == []
    assert driver.chromium.launch_calls == [(str(tmp_path / "profile"), False)] and (tmp_path / "profile").is_dir()
    f.close()
    assert driver.launched.closed and driver.stopped


def test_cdp_connect_failure_falls_back_to_launch(monkeypatch, tmp_path):
    f, driver, _, _ = _fetcher(monkeypatch, driver=FakeDriver(cdp_error=RuntimeError("ECONNREFUSED")), cdp_up=True, user_data_dir=tmp_path / "p")
    f.get("https://www.biorxiv.org/content/z.source.xml")
    assert f.mode == "launched" and len(driver.chromium.cdp_calls) == 1 and len(driver.chromium.launch_calls) == 1


# ------------------------------------------------------------------ pacing, budget, failures


def test_minimum_interval_per_host(monkeypatch):
    f, driver, sleeps, clock = _fetcher(monkeypatch, min_interval=5.0)
    f.get("https://www.biorxiv.org/content/1.source.xml")
    assert sleeps == []
    clock["t"] += 1.0
    f.get("https://www.biorxiv.org/content/2.source.xml")
    assert sleeps == [pytest.approx(4.0)]  # 5 s gap, 1 s already elapsed
    f.get("https://www.medrxiv.org/content/3.source.xml")
    assert len(sleeps) == 1  # a different host is not paced against the first
    assert f.stats["waited_s"] == pytest.approx(4.0)


def test_page_budget_is_enforced(monkeypatch):
    f, driver, _, _ = _fetcher(monkeypatch, max_pages=2)
    f.get("https://www.biorxiv.org/content/1.source.xml")
    f.get("https://www.biorxiv.org/content/2.source.xml")
    with pytest.raises(BrowserBudgetExhausted) as exc:
        f.get("https://www.biorxiv.org/content/3.source.xml")
    assert "budget of 2" in str(exc.value)
    assert driver.visited == ["https://www.biorxiv.org/content/1.source.xml", "https://www.biorxiv.org/content/2.source.xml"]
    assert f.pages_fetched == 2 and f.stats["budget_refusals"] == 1


def test_non_200_and_navigation_errors_return_none_but_count_against_the_budget(monkeypatch, caplog):
    f, driver, _, _ = _fetcher(monkeypatch, driver=FakeDriver(outcomes=[FakeResponse(429, b"slow down"), RuntimeError("net::ERR_TIMED_OUT"), None]))
    with caplog.at_level(logging.INFO, logger="litradar.browser"):
        assert f.get("https://www.biorxiv.org/content/1.source.xml") is None
        assert f.get("https://www.biorxiv.org/content/2.source.xml") is None
        assert f.get("https://www.biorxiv.org/content/3.source.xml") is None
    # the clock is frozen, so each same-host fetch after the first pays the full 5 s gap: failures are paced too
    assert f.stats == {"fetched": 3, "ok": 0, "failed": 3, "refused": 0, "budget_refusals": 0, "waited_s": 10.0}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("-> 429, no body" in m for m in msgs) and any("ERR_TIMED_OUT" in m for m in msgs) and any("no response" in m for m in msgs)
    assert all(p.closed for p in driver.browser.contexts[0].pages)


# ------------------------------------------------------------------ BrowserHttp: delegation, caching, stats


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.gets = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params))
        r = requests.Response()
        r.status_code = 200
        r._content = b'{"via": "requests"}'
        r.url = url
        return r


def _browser_http(monkeypatch, tmp_path, **kw):
    f, driver, sleeps, clock = _fetcher(monkeypatch, **kw)
    h = BrowserHttp(browser=f, cache_dir=tmp_path / "cache")
    h.min_interval.clear()
    h.session = FakeSession()
    return h, f, driver


def test_browser_http_routes_allow_listed_hosts_to_the_browser_and_the_rest_to_requests(monkeypatch, tmp_path):
    h, f, driver = _browser_http(monkeypatch, tmp_path)
    r = h.get("https://www.biorxiv.org/content/early/2026/10/10/x.source.xml", params={"v": 1}, cache=True)
    assert r is not None and r.status_code == 200 and r.content == b"<article/>"
    assert driver.visited == ["https://www.biorxiv.org/content/early/2026/10/10/x.source.xml?v=1"]
    assert h.session.gets == []
    j = h.get_json("https://api.biorxiv.org/details/biorxiv/2026-10-10/2026-10-17/0")
    assert j == {"via": "requests"} and h.session.gets[0][0].startswith("https://api.biorxiv.org/")
    assert h.stats["browser"] == 1 and h.stats["requests"] == 2 and h.stats["failures"] == 0
    # second call for the same document is served from the disk cache, not the browser
    r2 = h.get("https://www.biorxiv.org/content/early/2026/10/10/x.source.xml", params={"v": 1}, cache=True)
    assert r2.content == b"<article/>" and len(driver.visited) == 1 and h.stats["cache_hits"] == 1
    h.close()
    assert driver.stopped


def test_browser_http_returns_none_when_the_budget_is_gone_and_warns_once(monkeypatch, tmp_path, caplog):
    h, f, driver = _browser_http(monkeypatch, tmp_path, max_pages=1)
    assert h.get("https://www.biorxiv.org/content/1.source.xml") is not None
    with caplog.at_level(logging.WARNING, logger="litradar.browser"):
        assert h.get("https://www.biorxiv.org/content/2.source.xml") is None
        assert h.get("https://www.biorxiv.org/content/3.source.xml") is None
    assert sum("budget" in r.getMessage() for r in caplog.records) == 1
    assert h.stats["failures"] == 2 and h.stats["browser"] == 1


def test_browser_http_propagates_refusals_for_login_paths(monkeypatch, tmp_path):
    h, f, driver = _browser_http(monkeypatch, tmp_path)
    with pytest.raises(BrowserRefused):
        h.get("https://www.biorxiv.org/user/login")
    assert driver.visited == []


def test_browser_http_failed_browser_fetch_is_none(monkeypatch, tmp_path):
    f, driver, _, _ = _fetcher(monkeypatch, driver=FakeDriver(outcomes=[FakeResponse(403, b"")]))
    h = BrowserHttp(browser=f, cache_dir=tmp_path / "c")
    assert h.get("https://www.biorxiv.org/content/1.source.xml", cache=True) is None
    assert h.stats["failures"] == 1 and not any((tmp_path / "c").rglob("*.bin"))


# ------------------------------------------------------------------ settings wiring


def test_make_http_returns_plain_http_unless_browser_is_enabled(monkeypatch, tmp_path):
    s = Settings(radar={}, cache_dir=tmp_path)
    plain = make_http(s, contact_email="a@b.c")
    assert type(plain).__name__ == "Http" and "a@b.c" in plain.session.headers["User-Agent"]
    s2 = Settings(radar={"browser": {"enabled": False}}, cache_dir=tmp_path)
    assert type(make_http(s2)).__name__ == "Http"


def test_make_http_builds_browser_http_from_radar_yaml(monkeypatch, tmp_path):
    monkeypatch.setattr(br, "playwright_installed", lambda: True)
    cfg = {"browser": {"enabled": True, "allowed_hosts": ["www.biorxiv.org", "www.example-oa.org"], "cdp_url": "http://127.0.0.1:9222",
                       "min_interval": 7, "max_pages": 10, "headless": False}}
    s = Settings(radar=cfg, cache_dir=tmp_path)
    h = make_http(s, contact_email="a@b.c", cache_dir=tmp_path, ncbi_api_key="k")
    assert isinstance(h, BrowserHttp) and "a@b.c" in h.session.headers["User-Agent"]
    assert h.min_interval["eutils.ncbi.nlm.nih.gov"] == 0.12  # Http kwargs pass through (keyed NCBI pacing)
    b = h.browser
    assert b.allowed_hosts == frozenset({"www.biorxiv.org", "www.example-oa.org"}) and b.cdp_url == "http://127.0.0.1:9222"
    assert b.min_interval == 7.0 and b.max_pages == 10 and b.headless is False and b.user_data_dir == Path(tmp_path) / "browser-profile"


def test_make_http_with_browser_enabled_but_no_playwright_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(br, "playwright_installed", lambda: False)
    with pytest.raises(BrowserUnavailable) as exc:
        make_http(Settings(radar={"browser": {"enabled": True}}, cache_dir=tmp_path))
    assert "pip install 'lit-radar[browser]'" in str(exc.value)


def test_status_and_browser_check_command(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(br, "playwright_installed", lambda: False)
    monkeypatch.setattr(br, "cdp_reachable", lambda url, timeout=1.5: False)
    info = br.status()
    assert info["playwright_installed"] is False and info["cdp_reachable"] is False and info["allowed_hosts"] == ["www.biorxiv.org", "www.medrxiv.org"]
    from litradar import cli

    rc = cli.main(["--config-dir", str(tmp_path), "browser-check", "--cdp-url", "http://localhost:9999"])
    out = capsys.readouterr().out
    # an optional extra that is simply not installed is information, not a failure ...
    assert rc == 0 and '"cdp_url": "http://localhost:9999"' in out and '"enabled_in_radar_yaml": false' in out
    # ... unless radar.yaml has switched the browser fetcher on and it is unusable
    (tmp_path / "radar.yaml").write_text("browser:\n  enabled: true\n", encoding="utf-8")
    rc = cli.main(["--config-dir", str(tmp_path), "browser-check", "--cdp-url", "http://localhost:9999"])
    assert rc == 1 and '"enabled_in_radar_yaml": true' in capsys.readouterr().out
