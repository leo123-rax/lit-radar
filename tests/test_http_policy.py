"""Http client policy: User-Agent shape, URL redaction in logs, per-host 429 thresholds, retries override, 403 logging."""
import logging

import requests

from litradar import http as http_mod
from litradar.http import Http, _redact, user_agent_string


def _resp(status, url="https://api.example.org/x", headers=None, body=b"{}"):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.headers.update(headers or {})
    r.url = url
    return r


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.gets = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append({"url": url, "params": params, "headers": headers})
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _http(outcomes, monkeypatch, **kw):
    sleeps = []
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: sleeps.append(s))
    h = Http(**kw)
    h.min_interval.clear()
    h.session = FakeSession(outcomes)
    return h, sleeps


# ---- User-Agent -----------------------------------------------------------------------------
def test_user_agent_is_tool_plus_bare_email_without_any_url():
    assert user_agent_string("lit-radar/0.1", "contact@example.com") == "lit-radar/0.1 contact@example.com"
    assert user_agent_string("lit-radar/0.1", "") == "lit-radar/0.1 (no contact configured)"
    # a URL smuggled into the base is stripped: EDGAR answers 403 to any UA containing one
    ua = user_agent_string("lit-radar/0.1 (+https://github.com/example/lit-radar)", "contact@example.com")
    assert ua == "lit-radar/0.1 contact@example.com" and "http" not in ua
    h = Http(contact_email="contact@example.com")
    assert h.session.headers["User-Agent"] == "lit-radar/0.1 contact@example.com"
    assert "mailto:" not in h.session.headers["User-Agent"]
    assert Http().session.headers["User-Agent"] == "lit-radar/0.1 (no contact configured)"


def test_gdelt_is_paced_at_eight_seconds_and_trips_only_after_four_429s(monkeypatch):
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    # pacing: the second request start waits out the 8 s interval measured from the previous start
    h = Http()
    sleeps = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(http_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: sleeps.append(s))
    assert h.min_interval["api.gdeltproject.org"] == 8.0
    h._pace(url)
    clock["t"] += 1.5
    h._pace(url)
    assert sleeps == [6.5]
    # breaker: three 429s back off 10 / 20 / 30 s and the fourth attempt may still succeed; four in a row block the host
    h, sleeps = _http([_resp(429, url=url)] * 3 + [_resp(200, url=url)], monkeypatch, max_retries=5)
    r = h.get(url)
    assert r is not None and r.status_code == 200 and h.blocked_hosts == set() and sleeps == [10.0, 20.0, 30.0]
    h, sleeps = _http([_resp(429, url=url)] * 4, monkeypatch, max_retries=5)
    assert h.get(url).status_code == 429 and "api.gdeltproject.org" in h.blocked_hosts and len(h.session.gets) == 4
    assert h.get(url) is None and h.stats["blocked_skips"] == 1  # later calls are skipped without a request


def test_sec_and_gdelt_hosts_are_paced():
    h = Http()
    assert h.min_interval["www.sec.gov"] == 0.11
    assert h.min_interval["api.gdeltproject.org"] >= 5.0
    assert h.min_interval["eutils.ncbi.nlm.nih.gov"] == 0.4
    assert Http(ncbi_api_key="k").min_interval["eutils.ncbi.nlm.nih.gov"] == 0.12


# ---- redaction ------------------------------------------------------------------------------
def test_redact_blanks_keys_and_emails():
    u = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=sra&api_key=SECRETKEY123&term=x"
    out = _redact(u)
    assert "SECRETKEY123" not in out and "api_key=%2A%2A%2A" in out and "term=x" in out
    u2 = "https://api.crossref.org/works?mailto=you%40example.com&rows=1"
    out2 = _redact(u2)
    assert "you" not in out2 and "example.com" not in out2 and "rows=1" in out2
    assert _redact("https://api.example.org/people/someone@example.org/x") == "https://api.example.org/people/***@***/x"
    assert _redact("") == "" and _redact(None) == ""


def test_logged_urls_are_redacted_on_retry_and_failure(monkeypatch, caplog):
    secret_url = "https://eutils.ncbi.nlm.nih.gov/esummary.fcgi?db=sra&api_key=SECRETKEY123"
    h, _ = _http([_resp(429, url=secret_url), _resp(200, url=secret_url)], monkeypatch, max_retries=2)
    with caplog.at_level(logging.DEBUG, logger="litradar.http"):
        assert h.get("https://eutils.ncbi.nlm.nih.gov/esummary.fcgi", params={"db": "sra", "api_key": "SECRETKEY123"}).status_code == 200
    assert "SECRETKEY123" not in caplog.text and "retrying" in caplog.text


# ---- 429 thresholds and back-off ---------------------------------------------------------------
def test_default_host_blocks_after_two_429s_but_eutils_tolerates_six(monkeypatch):
    h, sleeps = _http([_resp(429)] * 2 + [_resp(200)], monkeypatch, max_retries=5)
    assert h.get("https://api.example.org/x").status_code == 429
    assert "api.example.org" in h.blocked_hosts and len(h.session.gets) == 2

    url = "https://eutils.ncbi.nlm.nih.gov/esearch.fcgi"
    h, sleeps = _http([_resp(429, url=url)] * 5 + [_resp(200, url=url)], monkeypatch, max_retries=6)
    r = h.get(url)
    assert r is not None and r.status_code == 200 and h.blocked_hosts == set()
    assert len(h.session.gets) == 6 and sleeps[0] == 2.0  # eutils 429 backs off at least 2 s, no trip
    h, sleeps = _http([_resp(429, url=url)] * 6, monkeypatch, max_retries=8)
    assert h.get(url).status_code == 429 and "eutils.ncbi.nlm.nih.gov" in h.blocked_hosts


def test_retry_after_wins_over_minimum_backoff(monkeypatch):
    url = "https://eutils.ncbi.nlm.nih.gov/esearch.fcgi"
    h, sleeps = _http([_resp(429, url=url, headers={"Retry-After": "7"}), _resp(200, url=url)], monkeypatch)
    assert h.get(url).status_code == 200 and sleeps == [7.0]


# ---- retries override -------------------------------------------------------------------------
def test_retries_parameter_limits_attempts_and_there_is_no_trailing_sleep(monkeypatch, caplog):
    h, sleeps = _http([_resp(500)] * 5, monkeypatch, max_retries=4)
    with caplog.at_level(logging.INFO, logger="litradar.http"):
        assert h.get("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML", retries=1) is None
    assert len(h.session.gets) == 2 and sleeps == [1.0] and h.stats["failures"] == 1
    assert "gave up after 2 attempt(s)" in caplog.text
    # default policy still applies when retries is not given
    h, sleeps = _http([_resp(500)] * 5, monkeypatch, max_retries=4)
    assert h.get("https://api.example.org/x") is None and len(h.session.gets) == 5 and sleeps == [1.0, 2.0, 4.0, 8.0]


def test_get_json_and_get_text_pass_retries_through(monkeypatch):
    h, sleeps = _http([_resp(503)] * 3, monkeypatch, max_retries=4)
    assert h.get_json("https://api.example.org/x", retries=0) is None and len(h.session.gets) == 1
    h, sleeps = _http([_resp(503)] * 3, monkeypatch, max_retries=4)
    assert h.get_text("https://api.example.org/x", retries=0) is None and len(h.session.gets) == 1


# ---- 403 logging ------------------------------------------------------------------------------
def test_403_is_warned_once_per_host_then_info(monkeypatch, caplog):
    sec = "https://www.sec.gov/cgi-bin/browse-edgar?CIK=ABC&type=8-K&output=atom"
    h, _ = _http([_resp(403, url=sec), _resp(403, url=sec), _resp(403, url="https://other.example/y")], monkeypatch)
    with caplog.at_level(logging.INFO, logger="litradar.http"):
        assert h.get(sec).status_code == 403
        assert h.get(sec).status_code == 403
        assert h.get("https://other.example/y").status_code == 403
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "403" in r.getMessage()]
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "403" in r.getMessage()]
    assert len(warnings) == 2 and len(infos) == 1  # one WARNING per host, repeats at INFO
    assert "LITRADAR_CONTACT_EMAIL" in warnings[0].getMessage()
    assert h.stats["failures"] == 3 and len(h.session.gets) == 3  # never retried
