"""A polite HTTP client: honest User-Agent, per-host pacing, retries with backoff, optional disk cache.

Every public API we touch publishes an etiquette (NCBI: 3 req/s without a key, Crossref and
OpenAlex: identify yourself with mailto, arXiv: one request every 3 seconds, SEC EDGAR: a
User-Agent of the form ``<tool> <contact e-mail>`` and at most 10 req/s). This module is where
that etiquette lives so the source modules stay simple.

User-Agent
    ``lit-radar/0.1 contact@example.com`` when a contact address is configured, otherwise
    ``lit-radar/0.1 (no contact configured)``. Never put a URL in it: EDGAR answers 403 to any
    User-Agent containing one, whatever else it says.

Rate limiting
    A host that answers HTTP 429 ``circuit_threshold`` times in a row (default 2; NCBI E-utilities 6,
    because their 429s are transient and a long census must not be truncated by two unlucky pages;
    GDELT 4, because its load shedding comes in bursts that clear within a minute) is skipped for
    the rest of the run. A single 429 backs off for ``Retry-After`` or the host's minimum (NCBI 2 s,
    GDELT 10 s, otherwise 1 s) and is retried.

Logging
    Every URL that reaches a log line goes through :func:`_redact`, which blanks ``api_key``-style
    query values and e-mail addresses. A 403 is logged once per host at WARNING (with a hint for
    EDGAR), then at INFO. A 400 (the API rejected the request itself) is logged once per host at
    WARNING with the first 200 characters of the body, then at INFO.

Accounting
    ``stats`` holds run-wide counters; ``failures_by_host``, ``successes_by_host`` and
    ``blocked_skips_by_host`` break them down per host, and :meth:`Http.host_report` summarises them
    (blocked hosts, hosts that never answered successfully) so a pipeline can put a silently dead
    source into its error list.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "lit-radar/0.1"

DEFAULT_MIN_INTERVAL = {
    "eutils.ncbi.nlm.nih.gov": 0.4,  # 3 req/s nominal without an API key, but 0.34 s already draws 429s; 0.12 s with a key
    "export.arxiv.org": 3.0,  # arXiv asks for a 3-second gap between requests
    "api.biorxiv.org": 0.5,
    "www.biorxiv.org": 1.0,
    "www.medrxiv.org": 1.0,
    "api.openalex.org": 0.12,  # polite pool allows ~10 req/s
    "api.crossref.org": 0.2,
    "www.ebi.ac.uk": 0.2,
    "api.semanticscholar.org": 1.0,  # 1 req/s without a key
    "clinicaltrials.gov": 1.25,  # API v2 asks for ~50 requests/minute per IP; 1.25 s is ~48/min
    "api.reporter.nih.gov": 1.0,  # NIH RePORTER asks for no more than one request per second
    "api.fda.gov": 0.25,  # openFDA: 240 req/min per IP or key; 1,000/day anonymous, 120,000/day with a free key
    "api.cellxgene.cziscience.com": 1.0,  # CELLxGENE Discover curation API: one request per second
    "www.sec.gov": 0.11,  # EDGAR fair-access policy: at most 10 requests per second
    # GDELT DOC 2.0 API: "please limit requests to one every 5 seconds". Pacing is measured from the START of the
    # previous request and a GDELT answer takes 2-12 s, so 5.5 s left little margin once the response time is
    # subtracted; 8 s keeps every request start at least 5 s after the previous response on the slow path too.
    # Company queries are batched (gdelt.batch_companies), so a 70-company watchlist is ~10 requests, not 70.
    "api.gdeltproject.org": 8.0,
    "news.google.com": 1.0,  # only used when news.google_news is switched on
}
NCBI_KEYED_INTERVAL = 0.12  # 10 req/s with an api_key, with a little slack

# consecutive 429s before a host is skipped for the rest of the run
DEFAULT_CIRCUIT_THRESHOLD = 2
# NCBI: transient 429s must not truncate a long census. GDELT: its 429s come in runs of load shedding that clear
# after tens of seconds, and the first request of a run is often the one that is shed, so two in a row is too
# eager a trip; four 429s with 10/20/30 s back-offs between them (about a minute) is the evidence that the host
# is really closed to this client for now.
CIRCUIT_THRESHOLD = {"eutils.ncbi.nlm.nih.gov": 6, "api.gdeltproject.org": 4}
# minimum back-off after a 429 when the host sends no Retry-After header
RATE_LIMIT_BACKOFF = {"eutils.ncbi.nlm.nih.gov": 2.0, "api.gdeltproject.org": 10.0}

_SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "api-key", "key", "mailto", "email", "token", "access_token"}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.I)
_UA_URL_RE = re.compile(r"\s*\(\+?\s*https?://[^)]*\)|https?://\S+", re.I)


def _redact(url: Optional[str]) -> str:
    """Return a copy of ``url`` safe for a log line: credential-like query values and e-mail addresses blanked."""
    if not url:
        return url or ""
    try:
        parts = urlsplit(url)
        if parts.query:
            pairs = [(k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
            url = urlunsplit(parts._replace(query=urlencode(pairs)))
    except ValueError:
        pass
    return _EMAIL_RE.sub("***@***", url)


def user_agent_string(base: str = DEFAULT_USER_AGENT, contact_email: str = "") -> str:
    """Compose the User-Agent: ``<tool> <e-mail>`` or ``<tool> (no contact configured)``; URLs are stripped."""
    base = _UA_URL_RE.sub("", base or "").strip() or DEFAULT_USER_AGENT
    contact = (contact_email or "").strip()
    if contact:
        return f"{base} {contact}"
    return f"{base} (no contact configured)"


class Http:
    def __init__(
        self,
        contact_email: str = "",
        cache_dir: Optional[Path] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 60.0,
        max_retries: int = 4,
        ncbi_api_key: str = "",
    ) -> None:
        self.session = requests.Session()
        self.user_agent = user_agent_string(user_agent, contact_email)
        self.session.headers.update({"User-Agent": self.user_agent, "Accept": "*/*"})
        self.contact_email = contact_email
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = dict(DEFAULT_MIN_INTERVAL)
        if ncbi_api_key:
            self.min_interval["eutils.ncbi.nlm.nih.gov"] = NCBI_KEYED_INTERVAL
        self.default_circuit_threshold = DEFAULT_CIRCUIT_THRESHOLD
        self.circuit_threshold: Dict[str, int] = dict(CIRCUIT_THRESHOLD)
        self.rate_limit_backoff: Dict[str, float] = dict(RATE_LIMIT_BACKOFF)
        self._last_call: Dict[str, float] = {}
        self._consecutive_429: Dict[str, int] = {}
        self._warned_403: set = set()
        self._warned_400: set = set()
        self.blocked_hosts: set = set()  # hosts that hit their 429 threshold: skipped for the rest of the run
        self.max_backoff = 30.0
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "failures": 0, "blocked_skips": 0}
        # per-host breakdown of the counters above (cache hits are not counted as successes)
        self.failures_by_host: Dict[str, int] = {}
        self.successes_by_host: Dict[str, int] = {}
        self.blocked_skips_by_host: Dict[str, int] = {}

    # -- accounting -----------------------------------------------------------------------
    def _fail(self, host: str) -> None:
        self.stats["failures"] += 1
        self.failures_by_host[host] = self.failures_by_host.get(host, 0) + 1

    def _ok(self, host: str) -> None:
        self.successes_by_host[host] = self.successes_by_host.get(host, 0) + 1

    def _skip_blocked(self, host: str) -> None:
        self.stats["blocked_skips"] += 1
        self.blocked_skips_by_host[host] = self.blocked_skips_by_host.get(host, 0) + 1

    def host_report(self) -> Dict[str, Any]:
        """What went wrong per host this run, for the pipeline's error list.

        ``blocked_hosts``: hosts skipped after reaching their 429 threshold; ``failed_only_hosts``: hosts that were
        asked at least once and never answered successfully (and are not blocked); plus the three per-host maps."""
        failed_only = sorted(
            h for h, n in self.failures_by_host.items()
            if n > 0 and not self.successes_by_host.get(h) and h not in self.blocked_hosts
        )
        return {
            "blocked_hosts": sorted(self.blocked_hosts),
            "failed_only_hosts": failed_only,
            "failures_by_host": dict(self.failures_by_host),
            "successes_by_host": dict(self.successes_by_host),
            "blocked_skips_by_host": dict(self.blocked_skips_by_host),
            "circuit_threshold": {h: self._threshold(h) for h in sorted(self.blocked_hosts)},
        }

    # -- pacing ---------------------------------------------------------------------------
    def _pace(self, url: str) -> None:
        host = urlsplit(url).netloc
        interval = self.min_interval.get(host, 0.0)
        if interval <= 0:
            return
        last = self._last_call.get(host, 0.0)
        wait = last + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call[host] = time.monotonic()

    # -- cache ----------------------------------------------------------------------------
    def _cache_path(self, url: str, params: Optional[Dict[str, Any]]) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
        return self.cache_dir / key[:2] / (key + ".bin")

    # -- shared policy ---------------------------------------------------------------------
    def _threshold(self, host: str) -> int:
        return int(self.circuit_threshold.get(host, self.default_circuit_threshold))

    def _note_429(self, host: str) -> bool:
        """Count a 429 for ``host``; True when the host has just hit its threshold and is now blocked."""
        n = self._consecutive_429.get(host, 0) + 1
        self._consecutive_429[host] = n
        if n >= self._threshold(host):
            log.warning("%s answered 429 %d time(s) in a row; skipping this host for the rest of the run", host, n)
            self.blocked_hosts.add(host)
            self._fail(host)
            return True
        return False

    def _log_terminal(self, method: str, resp: requests.Response, url: str) -> None:
        shown = _redact(resp.url or url)
        host = urlsplit(resp.url or url).netloc
        if resp.status_code == 403 and host not in self._warned_403:
            self._warned_403.add(host)
            hint = ""
            if host.endswith("sec.gov"):
                hint = " (EDGAR requires a User-Agent of the form '<tool> <contact e-mail>' with no URL in it; set LITRADAR_CONTACT_EMAIL)"
            log.warning("%s %s -> 403 forbidden%s; further 403s from %s are logged at INFO", method, shown, hint, host)
            return
        if resp.status_code == 400 and host not in self._warned_400:
            # the API rejected the request itself (bad filter, retired field): that is a bug on our side or a
            # changed API, so the body is worth one WARNING per host
            self._warned_400.add(host)
            body = re.sub(r"\s+", " ", (getattr(resp, "text", "") or ""))[:200]
            log.warning("%s %s -> 400 bad request; body: %s; further 400s from %s are logged at INFO", method, shown, body or "(empty)", host)
            return
        log.info("%s %s -> %s (not retrying)", method, shown, resp.status_code)

    def _backoff_after(self, resp: Optional[requests.Response], host: str, backoff: float) -> float:
        if resp is None:
            return backoff
        if resp.status_code == 429:
            backoff = max(backoff, self.rate_limit_backoff.get(host, 1.0))
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            backoff = max(backoff, min(float(retry_after), self.max_backoff))
        return backoff

    # -- requests -------------------------------------------------------------------------
    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        cache: bool = False,
        ok_statuses: tuple = (200,),
        retries: Optional[int] = None,
    ) -> Optional[requests.Response]:
        """GET with pacing and retries. Returns None after exhausting retries (never raises).

        ``retries`` overrides the client-wide ``max_retries`` for this call: a full-text fetch that is
        known to answer 5xx deterministically should pass ``retries=1`` instead of paying 31 s of sleep.
        Terminal statuses (400/401/403/404/410) return the response unretried; a 429 is retried after
        ``Retry-After`` or the host's minimum back-off until the host's circuit threshold is reached.
        No sleep follows the final attempt.
        """
        cpath = self._cache_path(url, params) if cache else None
        if cpath and cpath.exists():
            self.stats["cache_hits"] += 1
            resp = requests.Response()
            resp.status_code = 200
            resp._content = cpath.read_bytes()
            resp.url = url
            return resp
        host = urlsplit(url).netloc
        if host in self.blocked_hosts:
            self._skip_blocked(host)
            return None
        attempts = (self.max_retries if retries is None else max(0, int(retries))) + 1
        backoff = 1.0
        for attempt in range(attempts):
            self._pace(url)
            self.stats["requests"] += 1
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("GET %s failed (%s); attempt %d/%d", _redact(url), exc, attempt + 1, attempts)
                resp = None
            if resp is not None and resp.status_code in ok_statuses:
                self._consecutive_429[host] = 0
                if resp.status_code < 400:
                    self._ok(host)
                if cpath:
                    cpath.parent.mkdir(parents=True, exist_ok=True)
                    cpath.write_bytes(resp.content)
                return resp
            if resp is not None and resp.status_code in (400, 401, 403, 404, 410):
                self._log_terminal("GET", resp, url)
                self._fail(host)
                return resp
            if resp is not None and resp.status_code == 429 and self._note_429(host):
                return resp
            if attempt == attempts - 1:
                break
            backoff = self._backoff_after(resp, host, backoff)
            if resp is not None:
                log.info("GET %s -> %s; retrying in %.1fs", _redact(resp.url or url), resp.status_code, backoff)
            self.stats["retries"] += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)
        self._fail(host)
        log.warning("GET %s gave up after %d attempt(s)", _redact(url), attempts)
        return None

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, cache: bool = False, retries: Optional[int] = None) -> Optional[Any]:
        resp = self.get(url, params=params, headers={"Accept": "application/json"}, cache=cache, retries=retries)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("non-JSON response from %s", _redact(url))
            return None

    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None, cache: bool = False, retries: Optional[int] = None) -> Optional[str]:
        resp = self.get(url, params=params, cache=cache, retries=retries)
        if resp is None or resp.status_code != 200:
            return None
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    def post_json(
        self,
        url: str,
        json_body: Any,
        headers: Optional[Dict[str, str]] = None,
        ok_statuses: tuple = (200,),
        retries: Optional[int] = None,
    ) -> Optional[Any]:
        """POST a JSON body with pacing and retries; returns the parsed JSON response (never raises).

        Same policy as get(): transient trouble (network error, 429, 5xx) is retried up to max_retries
        with exponential backoff capped at max_backoff and honouring Retry-After; 400/401/403/404/410 are
        terminal (a 400 here is a real validation error from the API, not a reason to retry); a host that
        reaches its 429 threshold is skipped for the rest of the run. Returns None on any failure or
        when the body is not JSON, so callers treat None as "no data" exactly as with get_json().
        """
        host = urlsplit(url).netloc
        if host in self.blocked_hosts:
            self._skip_blocked(host)
            return None
        attempts = (self.max_retries if retries is None else max(0, int(retries))) + 1
        backoff = 1.0
        for attempt in range(attempts):
            self._pace(url)
            self.stats["requests"] += 1
            try:
                resp = self.session.post(url, json=json_body, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("POST %s failed (%s); attempt %d/%d", _redact(url), exc, attempt + 1, attempts)
                resp = None
            if resp is not None and resp.status_code in ok_statuses:
                self._consecutive_429[host] = 0
                self._ok(host)
                try:
                    return resp.json()
                except ValueError:
                    log.warning("non-JSON response from POST %s", _redact(url))
                    return None
            if resp is not None and resp.status_code in (400, 401, 403, 404, 410):
                self._log_terminal("POST", resp, url)
                log.info("POST %s body: %s", _redact(url), (resp.text or "")[:200])
                self._fail(host)
                return None
            if resp is not None and resp.status_code == 429 and self._note_429(host):
                return None
            backoff = self._backoff_after(resp, host, backoff)
            if resp is not None:
                log.info("POST %s -> %s; retrying in %.1fs", _redact(url), resp.status_code, backoff)
            self.stats["retries"] += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)
        self._fail(host)
        log.warning("POST %s gave up after %d attempt(s)", _redact(url), attempts)
        return None
