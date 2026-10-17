"""Opt-in real-browser fetcher for hosts that block scripted clients but serve open-licensed content.

Some hosts answer HTTP 429 or 403 to anything that is not a browser while serving content whose
licence allows reading and text mining (bioRxiv / medRxiv full-text JATS XML is the motivating case:
the records' `jatsxml` URLs on www.biorxiv.org are rate-limited for scripts, see docs/SOURCES.md).
This module drives a real Chromium through Playwright for exactly those hosts, under rules that make
it unsuitable for anything else:

* a hard allow-list of hosts (default: www.biorxiv.org and www.medrxiv.org); any other host raises
* any URL whose path looks like a login, subscriber or account page raises, whatever the host
* at least `min_interval` seconds between fetches to the same host (default 5 s)
* at most `max_pages` fetches per process (default 50); the budget is enforced, not advisory
* one human-readable log line per fetch

Two ways to get a browser, both only when the optional extra is installed (`pip install 'lit-radar[browser]'`):

1. connect to a Chrome the user already runs with `--remote-debugging-port=9222` (Chrome DevTools
   protocol, `connect_over_cdp`); pages open as tabs in that session and are closed afterwards
2. otherwise launch a persistent Chromium context of our own (profile under the cache directory)

`BrowserHttp` is an `Http` whose `get()` goes through the browser for allow-listed hosts and through
the normal client for everything else, so the pipeline can swap it in with one line (`make_http`).
Read docs/browser.md and docs/LEGAL.md before enabling it: this uses the user's own browser session and
must never be pointed at paywalled publisher pages.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence
from urllib.parse import urlencode, urlsplit

import requests

from .config import Settings
from .http import Http

log = logging.getLogger(__name__)

DEFAULT_ALLOWED_HOSTS = ("www.biorxiv.org", "www.medrxiv.org")
DEFAULT_CDP_URL = "http://localhost:9222"
DEFAULT_MIN_INTERVAL = 5.0
DEFAULT_MAX_PAGES = 50
DEFAULT_TIMEOUT = 30.0
INSTALL_HINT = "pip install 'lit-radar[browser]' && python -m playwright install chromium"

# Path segments or query keys that mark login, subscription, account or purchase flows. Matched case-insensitively
# against the URL path and query; a match is refused for every host, allow-listed or not.
FORBIDDEN_URL_RE = re.compile(
    r"(^|/)(log-?in|sign-?in|sign-?up|auth|authenticate|oauth|sso|shibboleth|openathens|"
    r"subscribe|subscriber|subscribers|subscription|subscriptions|account|accounts|my-?account|"
    r"checkout|cart|purchase|buy|paywall|institution|institutional|access-?token|token)(/|$|\?|\.)"
    r"|[?&](login|password|token|session|sessionid|access_token)=",
    re.I,
)


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed (or could not start). The message says how to install it."""


class BrowserRefused(ValueError):
    """The URL is outside the allow-list or looks like a login / subscriber page. Never retried."""


class BrowserBudgetExhausted(RuntimeError):
    """More than `max_pages` fetches were attempted in this process."""


def _default_playwright_factory() -> Any:
    """Import Playwright lazily and return a started sync_playwright driver (`.stop()` when done)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise BrowserUnavailable(f"the browser fetcher needs Playwright: {INSTALL_HINT}") from exc
    return sync_playwright().start()


def playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


def cdp_reachable(cdp_url: str, timeout: float = 1.5) -> bool:
    """True when a Chrome DevTools endpoint answers at cdp_url (GET /json/version)."""
    try:
        resp = requests.get(cdp_url.rstrip("/") + "/json/version", timeout=timeout)
    except requests.RequestException:
        return False
    return resp.status_code == 200


class BrowserFetcher:
    """Fetch allow-listed URLs through a real browser with pacing, a page budget and loud logging.

    Construction is cheap and does not start a browser; the first `get()` connects (or launches).
    Use as a context manager, or call `close()`, to release the browser.
    """

    def __init__(
        self,
        allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
        cdp_url: str = DEFAULT_CDP_URL,
        user_data_dir: Optional[Path] = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_pages: int = DEFAULT_MAX_PAGES,
        headless: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        playwright_factory: Optional[Callable[[], Any]] = None,
        cdp_probe: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.allowed_hosts = frozenset(h.strip().lower() for h in allowed_hosts if h and h.strip())
        if not self.allowed_hosts:
            raise ValueError("BrowserFetcher needs at least one allow-listed host")
        self.cdp_url = cdp_url
        self.user_data_dir = Path(user_data_dir) if user_data_dir else Path.home() / ".cache" / "lit-radar" / "browser-profile"
        self.min_interval = float(min_interval)
        self.max_pages = int(max_pages)
        self.headless = headless
        self.timeout = float(timeout)
        self._factory = playwright_factory or _default_playwright_factory
        self._probe = cdp_probe or cdp_reachable
        if playwright_factory is None and not playwright_installed():
            raise BrowserUnavailable(f"the browser fetcher needs Playwright: {INSTALL_HINT}")
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._own_context = False
        self.mode = ""  # "cdp" | "launched" once connected
        self.pages_fetched = 0
        self._last_call: Dict[str, float] = {}
        self.stats = {"fetched": 0, "ok": 0, "failed": 0, "refused": 0, "budget_refusals": 0, "waited_s": 0.0}

    # -- policy ---------------------------------------------------------------------------
    def check_url(self, url: str) -> str:
        """Return the host when the URL may be fetched; raise BrowserRefused otherwise."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            self.stats["refused"] += 1
            raise BrowserRefused(f"refusing {url!r}: not an http(s) URL")
        if FORBIDDEN_URL_RE.search(parts.path or "") or FORBIDDEN_URL_RE.search("?" + (parts.query or "")):
            self.stats["refused"] += 1
            raise BrowserRefused(f"refusing {url!r}: login / subscriber / account paths are never fetched by the browser")
        if host not in self.allowed_hosts:
            self.stats["refused"] += 1
            raise BrowserRefused(f"refusing {url!r}: host {host!r} is not on the browser allow-list {sorted(self.allowed_hosts)}")
        return host

    def allows(self, url: str) -> bool:
        """True when the host is allow-listed (does not check the path; `check_url` does)."""
        return (urlsplit(url).hostname or "").lower() in self.allowed_hosts

    def _pace(self, host: str) -> None:
        last = self._last_call.get(host)
        if last is not None:
            wait = last + self.min_interval - time.monotonic()
            if wait > 0:
                self.stats["waited_s"] += wait
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    # -- connection -----------------------------------------------------------------------
    def connect(self) -> str:
        """Connect to the user's Chrome over CDP, or launch a persistent Chromium. Returns the mode."""
        if self._context is not None:
            return self.mode
        self._pw = self._factory()
        if self.cdp_url and self._probe(self.cdp_url):
            try:
                self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=int(self.timeout * 1000))
                contexts = list(getattr(self._browser, "contexts", []) or [])
                if contexts:
                    self._context = contexts[0]
                    self._own_context = False
                else:
                    self._context = self._browser.new_context()
                    self._own_context = True
                self.mode = "cdp"
                log.info("browser: connected to Chrome at %s (%s)", self.cdp_url, "existing session" if not self._own_context else "new context")
                return self.mode
            except Exception as exc:  # noqa: BLE001 - fall back to our own browser
                log.warning("browser: CDP endpoint %s answered but connect failed (%s); launching Chromium instead", self.cdp_url, exc)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(str(self.user_data_dir), headless=self.headless)
        self._own_context = True
        self.mode = "launched"
        log.info("browser: launched persistent Chromium (headless=%s, profile %s)", self.headless, self.user_data_dir)
        return self.mode

    def close(self) -> None:
        try:
            if self._context is not None and self._own_context:
                self._context.close()
            if self._browser is not None and self.mode == "cdp":
                self._browser.close()  # detaches from the user's Chrome; does not quit it
        finally:
            self._context = None
            self._browser = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                finally:
                    self._pw = None

    def __enter__(self) -> BrowserFetcher:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- fetching -------------------------------------------------------------------------
    def get(self, url: str, wait_until: str = "domcontentloaded") -> Optional[bytes]:
        """Fetch one allow-listed URL; bytes of the response body, or None when the page did not answer 200.

        Raises BrowserRefused for a host outside the allow-list or a login-looking URL, and
        BrowserBudgetExhausted once more than `max_pages` fetches have been attempted.
        """
        host = self.check_url(url)
        if self.pages_fetched >= self.max_pages:
            self.stats["budget_refusals"] += 1
            raise BrowserBudgetExhausted(f"browser page budget of {self.max_pages} per run is used up; not fetching {url}")
        self.connect()
        self._pace(host)
        self.pages_fetched += 1
        self.stats["fetched"] += 1
        started = time.monotonic()
        page = self._context.new_page()
        status: Any = None
        body: Optional[bytes] = None
        try:
            resp = page.goto(url, wait_until=wait_until, timeout=int(self.timeout * 1000))
            if resp is not None:
                status = resp.status
                if status == 200:
                    body = resp.body()
        except Exception as exc:  # noqa: BLE001 - a navigation failure is a None, like Http.get
            log.warning("browser GET %s failed: %s", url, exc)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        elapsed = time.monotonic() - started
        if body is not None:
            self.stats["ok"] += 1
            log.info("browser GET %s -> %s, %d bytes in %.1f s (page %d/%d, %s)", url, status, len(body), elapsed, self.pages_fetched, self.max_pages, self.mode)
            return body
        self.stats["failed"] += 1
        log.info("browser GET %s -> %s, no body in %.1f s (page %d/%d, %s)", url, status if status is not None else "no response", elapsed, self.pages_fetched, self.max_pages, self.mode)
        return None


class BrowserHttp(Http):
    """An Http whose get() goes through a BrowserFetcher for allow-listed hosts and through requests otherwise.

    Everything else (get_json, get_text, post_json, pacing, retries, disk cache, stats) is inherited.
    Browser responses are cached on disk exactly like normal ones when `cache=True`, so a run that has
    fetched a document once never asks the browser for it again.
    """

    def __init__(self, *args: Any, browser: Optional[BrowserFetcher] = None, browser_options: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.browser = browser or BrowserFetcher(**(browser_options or {}))
        self.stats["browser"] = 0
        self._budget_warned = False

    @classmethod
    def from_settings(cls, settings: Settings, **http_kwargs: Any) -> BrowserHttp:
        """Build from the `browser:` block of radar.yaml (see docs/browser.md for every key)."""
        cfg = dict((settings.radar or {}).get("browser") or {})
        options: Dict[str, Any] = {
            "allowed_hosts": cfg.get("allowed_hosts") or DEFAULT_ALLOWED_HOSTS,
            "cdp_url": cfg.get("cdp_url", DEFAULT_CDP_URL),
            "min_interval": float(cfg.get("min_interval", DEFAULT_MIN_INTERVAL)),
            "max_pages": int(cfg.get("max_pages", DEFAULT_MAX_PAGES)),
            "headless": bool(cfg.get("headless", True)),
            "timeout": float(cfg.get("timeout", DEFAULT_TIMEOUT)),
            "user_data_dir": Path(cfg["user_data_dir"]) if cfg.get("user_data_dir") else Path(settings.cache_dir) / "browser-profile",
        }
        return cls(browser_options=options, **http_kwargs)

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        cache: bool = False,
        ok_statuses: tuple = (200,),
        retries: Optional[int] = None,
        **kwargs: Any,
    ) -> Optional[requests.Response]:
        if not self.browser.allows(url):
            return super().get(url, params=params, headers=headers, cache=cache, ok_statuses=ok_statuses, retries=retries, **kwargs)
        full = url + (("&" if "?" in url else "?") + urlencode(params, doseq=True) if params else "")
        cpath = self._cache_path(url, params) if cache else None
        if cpath and cpath.exists():
            self.stats["cache_hits"] += 1
            return _response(200, cpath.read_bytes(), full)
        try:
            body = self.browser.get(full)
        except BrowserBudgetExhausted as exc:
            if not self._budget_warned:
                log.warning("%s; remaining browser fetches in this run return None", exc)
                self._budget_warned = True
            self.stats["failures"] += 1
            return None
        self.stats["requests"] += 1
        self.stats["browser"] += 1
        if body is None:
            self.stats["failures"] += 1
            return None
        if cpath:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_bytes(body)
        return _response(200, body, full)

    def close(self) -> None:
        self.browser.close()


def _response(status: int, body: bytes, url: str) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.url = url
    resp.encoding = "utf-8"
    return resp


def browser_enabled(settings: Settings) -> bool:
    return bool(((settings.radar or {}).get("browser") or {}).get("enabled"))


def make_http(settings: Settings, **http_kwargs: Any) -> Http:
    """The one-line swap for `Pipeline.__init__`: BrowserHttp when radar.yaml has `browser.enabled: true`, else Http.

        self.http = http or make_http(settings, contact_email=settings.contact_email, cache_dir=settings.cache_dir, ncbi_api_key=settings.ncbi_api_key)

    Raises BrowserUnavailable (with the install hint) when the browser is enabled but Playwright is missing.
    """
    if browser_enabled(settings):
        return BrowserHttp.from_settings(settings, **http_kwargs)
    return Http(**http_kwargs)


# ----------------------------------------------------------------------------- CLI helper


def status(cdp_url: str = DEFAULT_CDP_URL, allowed_hosts: Sequence[str] = DEFAULT_ALLOWED_HOSTS) -> Dict[str, Any]:
    """What `litradar browser-check` prints: is the extra installed, is a Chrome listening, what is allow-listed."""
    return {
        "playwright_installed": playwright_installed(),
        "install_hint": INSTALL_HINT,
        "cdp_url": cdp_url,
        "cdp_reachable": cdp_reachable(cdp_url) if cdp_url else False,
        "allowed_hosts": list(allowed_hosts),
        "min_interval_s": DEFAULT_MIN_INTERVAL,
        "max_pages_per_run": DEFAULT_MAX_PAGES,
        "docs": "docs/browser.md",
    }


def cmd_browser_check(args: Any) -> int:
    import json

    settings = Settings.from_env(config_dir=getattr(args, "config_dir", None))
    cfg = (settings.radar or {}).get("browser") or {}
    info = status(cdp_url=getattr(args, "cdp_url", None) or cfg.get("cdp_url") or DEFAULT_CDP_URL, allowed_hosts=cfg.get("allowed_hosts") or DEFAULT_ALLOWED_HOSTS)
    info["enabled_in_radar_yaml"] = bool(cfg.get("enabled"))
    print(json.dumps(info, indent=1))
    return 0 if info["playwright_installed"] else 1


__all__ = [
    "DEFAULT_ALLOWED_HOSTS", "DEFAULT_CDP_URL", "DEFAULT_MIN_INTERVAL", "DEFAULT_MAX_PAGES", "FORBIDDEN_URL_RE", "INSTALL_HINT",
    "BrowserUnavailable", "BrowserRefused", "BrowserBudgetExhausted", "BrowserFetcher", "BrowserHttp",
    "make_http", "browser_enabled", "playwright_installed", "cdp_reachable", "status", "cmd_browser_check",
]
