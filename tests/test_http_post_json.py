"""Http.post_json retry policy against a fake requests session (no network, no sleeping)."""
import json

import requests

from litradar import http as http_mod
from litradar.http import Http


def _resp(status, body=None, headers=None, text=None):
    r = requests.Response()
    r.status_code = status
    r._content = (text if text is not None else json.dumps(body if body is not None else {})).encode()
    r.headers.update(headers or {})
    r.url = "https://api.example.org/search"
    return r


class FakeSession:
    """Answers post() from a queue; an Exception instance in the queue is raised instead of returned."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.posts = []
        self.headers = {}

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _http(outcomes, monkeypatch):
    sleeps = []
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: sleeps.append(s))
    h = Http(max_retries=3)
    h.min_interval.clear()  # no pacing in tests
    h.session = FakeSession(outcomes)
    return h, sleeps


URL = "https://api.example.org/search"


def test_success_returns_parsed_json_and_sends_the_body(monkeypatch):
    h, sleeps = _http([_resp(200, {"meta": {"total": 1}, "results": [{"appl_id": 1}]})], monkeypatch)
    out = h.post_json(URL, {"criteria": {"a": 1}}, headers={"Accept": "application/json"})
    assert out == {"meta": {"total": 1}, "results": [{"appl_id": 1}]}
    assert h.session.posts[0]["json"] == {"criteria": {"a": 1}} and h.session.posts[0]["headers"] == {"Accept": "application/json"}
    assert h.stats["requests"] == 1 and h.stats["retries"] == 0 and h.stats["failures"] == 0 and sleeps == []


def test_transient_5xx_and_network_errors_are_retried_with_backoff(monkeypatch):
    h, sleeps = _http([_resp(503), requests.ConnectionError("boom"), _resp(200, {"ok": True})], monkeypatch)
    assert h.post_json(URL, {}) == {"ok": True}
    assert h.stats["requests"] == 3 and h.stats["retries"] == 2 and h.stats["failures"] == 0
    assert sleeps == [1.0, 2.0]  # exponential backoff


def test_retry_after_header_is_honoured_and_capped(monkeypatch):
    h, sleeps = _http([_resp(503, headers={"Retry-After": "7"}), _resp(503, headers={"Retry-After": "9999"}), _resp(200, {"ok": 1})], monkeypatch)
    assert h.post_json(URL, {}) == {"ok": 1}
    assert sleeps == [7.0, 30.0]  # Retry-After wins over the 1 s backoff; never above max_backoff


def test_gives_up_after_max_retries(monkeypatch):
    h, sleeps = _http([_resp(500)] * 4, monkeypatch)
    assert h.post_json(URL, {}) is None
    assert h.stats["requests"] == 4 and h.stats["retries"] == 4 and h.stats["failures"] == 1
    assert len(h.session.outcomes) == 0


def test_validation_errors_are_terminal(monkeypatch):
    # RePORTER answers 400 with a real validation message; retrying would only repeat the mistake
    h, sleeps = _http([_resp(400, {"message": "System doesn't support limit value greater than 500."}), _resp(200, {})], monkeypatch)
    assert h.post_json(URL, {"limit": 501}) is None
    assert h.stats["requests"] == 1 and h.stats["retries"] == 0 and h.stats["failures"] == 1 and sleeps == []
    for status in (401, 403, 404, 410):
        h, _ = _http([_resp(status), _resp(200, {})], monkeypatch)
        assert h.post_json(URL, {}) is None and h.stats["requests"] == 1


def test_two_consecutive_429s_block_the_host_for_the_run(monkeypatch):
    h, sleeps = _http([_resp(429), _resp(429), _resp(200, {"never": "reached"})], monkeypatch)
    assert h.post_json(URL, {}) is None
    assert "api.example.org" in h.blocked_hosts and h.stats["failures"] == 1 and h.stats["retries"] == 1
    # later calls to the same host are skipped without a request
    assert h.post_json(URL, {}) is None
    assert h.stats["requests"] == 2 and h.stats["blocked_skips"] == 1
    assert len(h.session.outcomes) == 1


def test_a_single_429_followed_by_success_resets_the_counter(monkeypatch):
    h, sleeps = _http([_resp(429, headers={"Retry-After": "2"}), _resp(200, {"ok": 1}), _resp(429), _resp(200, {"ok": 2})], monkeypatch)
    assert h.post_json(URL, {}) == {"ok": 1}
    assert h.post_json(URL, {}) == {"ok": 2}
    assert h.blocked_hosts == set() and sleeps == [2.0, 1.0]


def test_non_json_200_returns_none_without_retry(monkeypatch):
    h, sleeps = _http([_resp(200, text="<html>maintenance</html>"), _resp(200, {})], monkeypatch)
    assert h.post_json(URL, {}) is None
    assert h.stats["requests"] == 1 and sleeps == []
