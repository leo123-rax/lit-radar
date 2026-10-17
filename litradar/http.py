"""A polite HTTP client: honest User-Agent, per-host pacing, retries with backoff, optional disk cache.

Every public API we touch publishes an etiquette (NCBI: 3 req/s without a key, Crossref and
OpenAlex: identify yourself with mailto, arXiv: one request every 3 seconds). This module is
where that etiquette lives so the source modules stay simple.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

DEFAULT_MIN_INTERVAL = {
    "eutils.ncbi.nlm.nih.gov": 0.34,  # 3 req/s without an API key; tightened to 0.1 when a key is present
    "export.arxiv.org": 3.0,  # arXiv asks for a 3-second gap between requests
    "api.biorxiv.org": 0.5,
    "www.biorxiv.org": 1.0,
    "www.medrxiv.org": 1.0,
    "api.openalex.org": 0.12,  # polite pool allows ~10 req/s
    "api.crossref.org": 0.2,
    "www.ebi.ac.uk": 0.2,
    "api.semanticscholar.org": 1.0,  # 1 req/s without a key
    "api.fda.gov": 0.25,  # openFDA: 240 req/min per IP or key; 1,000/day anonymous, 120,000/day with a free key
}


class Http:
    def __init__(
        self,
        contact_email: str = "",
        cache_dir: Optional[Path] = None,
        user_agent: str = "lit-radar/0.1 (+https://github.com/lit-radar)",
        timeout: float = 60.0,
        max_retries: int = 4,
        ncbi_api_key: str = "",
    ) -> None:
        self.session = requests.Session()
        ua = user_agent + (f"; mailto:{contact_email}" if contact_email else "")
        self.session.headers.update({"User-Agent": ua, "Accept": "*/*"})
        self.contact_email = contact_email
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = dict(DEFAULT_MIN_INTERVAL)
        if ncbi_api_key:
            self.min_interval["eutils.ncbi.nlm.nih.gov"] = 0.11
        self._last_call: Dict[str, float] = {}
        self._consecutive_429: Dict[str, int] = {}
        self.blocked_hosts: set = set()  # hosts that answered 429 twice in a row: skipped for the rest of the run
        self.max_backoff = 30.0
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "failures": 0, "blocked_skips": 0}

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

    # -- requests -------------------------------------------------------------------------
    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        cache: bool = False,
        ok_statuses: tuple = (200,),
    ) -> Optional[requests.Response]:
        """GET with pacing and retries. Returns None after exhausting retries (never raises)."""
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
            self.stats["blocked_skips"] += 1
            return None
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            self._pace(url)
            self.stats["requests"] += 1
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("GET %s failed (%s); attempt %d", url, exc, attempt + 1)
                resp = None
            if resp is not None and resp.status_code in ok_statuses:
                self._consecutive_429[host] = 0
                if cpath:
                    cpath.parent.mkdir(parents=True, exist_ok=True)
                    cpath.write_bytes(resp.content)
                return resp
            if resp is not None and resp.status_code in (400, 401, 403, 404, 410):
                log.info("GET %s -> %s (not retrying)", resp.url, resp.status_code)
                self.stats["failures"] += 1
                return resp
            if resp is not None and resp.status_code == 429:
                # a host that rate-limits us twice running is treated as closed for this run
                self._consecutive_429[host] = self._consecutive_429.get(host, 0) + 1
                if self._consecutive_429[host] >= 2:
                    log.warning("%s answered 429 twice in a row; skipping this host for the rest of the run", host)
                    self.blocked_hosts.add(host)
                    self.stats["failures"] += 1
                    return resp
            if resp is not None:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    backoff = max(backoff, min(float(retry_after), self.max_backoff))
                log.warning("GET %s -> %s; retrying in %.1fs", resp.url, resp.status_code, backoff)
            self.stats["retries"] += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)
        self.stats["failures"] += 1
        return None

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, cache: bool = False) -> Optional[Any]:
        resp = self.get(url, params=params, headers={"Accept": "application/json"}, cache=cache)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("non-JSON response from %s", url)
            return None

    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None, cache: bool = False) -> Optional[str]:
        resp = self.get(url, params=params, cache=cache)
        if resp is None or resp.status_code != 200:
            return None
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    def post_json(self, url: str, json_body: Any, headers: Optional[Dict[str, str]] = None) -> Optional[Any]:
        self._pace(url)
        self.stats["requests"] += 1
        try:
            resp = self.session.post(url, json=json_body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("POST %s failed (%s)", url, exc)
            self.stats["failures"] += 1
            return None
        if resp.status_code != 200:
            log.warning("POST %s -> %s", url, resp.status_code)
            self.stats["failures"] += 1
            return None
        try:
            return resp.json()
        except ValueError:
            return None
