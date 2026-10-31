"""Polite HTTP: shared client, per-host throttling, retries, robots.txt checks."""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 15,
        min_delay: float = 0.3,
        respect_robots: bool = True,
        transport: httpx.BaseTransport | None = None,
    ):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.respect_robots = respect_robots
        self.backoff = 1.5   # seconds, multiplied by the attempt number on retries
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en"},
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=8),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
            transport=transport,
        )
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()
        self._robots: dict[str, RobotFileParser | None] = {}

    # -- throttling -----------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next.get(host, 0.0))
            self._next[host] = t + self.min_delay
        if t > now:
            time.sleep(t - now)

    # -- core request ---------------------------------------------------------------
    def request(self, method: str, url: str, *, retries: int = 2, **kw) -> httpx.Response | None:
        """Returns the final response (any status), or None if the network failed. Retries 429/5xx."""
        host = urlparse(url).netloc
        for attempt in range(retries + 1):
            self._throttle(host)
            try:
                resp = self.client.request(method, url, **kw)
            except httpx.InvalidURL:
                return None
            except httpx.TransportError as exc:
                log.debug("transport error %s: %s", url, exc)
                if attempt >= min(retries, 1):  # network failures: one retry at most
                    return None
                time.sleep(self.backoff * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                try:
                    wait = min(float(resp.headers.get("Retry-After", "")), 20.0)
                except ValueError:
                    wait = self.backoff * (attempt + 1)
                time.sleep(wait)
                continue
            return resp
        return None

    def get(self, url: str, **kw) -> httpx.Response | None:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw):
        """Parsed JSON on a 200 response, else None."""
        resp = self.get(url, **kw)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def get_html(self, url: str, max_chars: int = 2_000_000) -> tuple[str, str] | None:
        """(final_url, html) for a 200 HTML response that robots.txt allows, else None."""
        if not self.allowed(url):
            log.debug("robots.txt disallows %s", url)
            return None
        resp = self.get(url)
        if resp is None or resp.status_code != 200:
            return None
        ctype = resp.headers.get("content-type", "")
        if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            return None
        return str(resp.url), resp.text[:max_chars]

    # -- robots.txt -----------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            known = origin in self._robots
            rp = self._robots.get(origin)
        if not known:
            rp = None
            resp = self.get(origin + "/robots.txt", retries=0)
            if resp is not None and resp.status_code == 200 and "html" not in resp.headers.get("content-type", ""):
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
            with self._lock:
                self._robots[origin] = rp
        if rp is None:  # missing/unreadable robots.txt -> allowed
            return True
        return rp.can_fetch(self.user_agent, url) or rp.can_fetch("*", url)

    def close(self) -> None:
        self.client.close()
