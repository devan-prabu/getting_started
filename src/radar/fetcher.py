"""The one HTTP client everything shares.

Brief §6.1 and §9 are hard constraints and this module is where they live:
robots.txt is obeyed with no override flag, at least 3 seconds pass between
requests to the same host, the User-Agent is honest and carries a contact
address, and retries only ever happen on 429/5xx/timeout.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

MIN_HOST_INTERVAL = 3.0  # seconds; brief §9.2. This tool has zero urgency.
TIMEOUT = 20.0
RETRY_ATTEMPTS = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RobotsDisallowed(Exception):
    """The site's robots.txt forbids this path. Skip it; do not retry."""


class RetryableStatus(Exception):
    """A 429/5xx worth another attempt."""

    def __init__(self, status: int, url: str):
        self.status = status
        super().__init__(f"{status} from {url}")


def user_agent() -> str:
    contact = os.environ.get("CONTACT_EMAIL", "").strip()
    who = f"; {contact}" if contact else "; set CONTACT_EMAIL in .env"
    return f"UAE-Project-Radar/0.1 (personal job-search tool{who})"


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status_code: int
    text: str
    content_type: str


class _HostThrottle:
    """Token bucket per netloc. Simplest thing that enforces the 3s floor."""

    def __init__(self, min_interval: float = MIN_HOST_INTERVAL):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, netloc: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(netloc)
            if last is not None:
                delay = self.min_interval - (now - last)
                if delay > 0:
                    time.sleep(delay)
                    now = time.monotonic()
            self._last[netloc] = now


class Fetcher:
    """Shared client. Build one per run and pass it around.

    Robots rules are cached for the lifetime of the instance, i.e. per run, so a
    single crawl never hammers robots.txt.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        respect_robots: bool = True,
        min_interval: float = MIN_HOST_INTERVAL,
    ):
        self._client = client or httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": user_agent()},
        )
        self._owns_client = client is None
        # There is deliberately no way to turn this off from the CLI (brief §9.1).
        # The constructor flag exists so tests can run without a robots fixture.
        self.respect_robots = respect_robots
        self._throttle = _HostThrottle(min_interval)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- robots -----------------------------------------------------------

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        robots_url = urlunparse((parts.scheme, parts.netloc, "/robots.txt", "", "", ""))
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            self._throttle.wait(parts.netloc)
            resp = self._client.get(robots_url)
            if resp.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
            else:
                # No robots.txt (or it errored) means nothing is disallowed.
                log.debug(
                    "robots.txt for %s returned %s; treating as allow-all", origin, resp.status_code
                )
        except httpx.HTTPError as exc:
            # Unreachable robots.txt is not consent to ignore it, but it is also
            # not a disallow. Log it and proceed; the rate limit still applies.
            log.warning("could not fetch %s (%s); proceeding without robots rules", robots_url, exc)

        self._robots[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(user_agent(), url)

    # --- fetching ---------------------------------------------------------

    def get(self, url: str) -> FetchResult:
        """Fetch a URL, obeying robots and the per-host rate limit.

        Raises RobotsDisallowed if the path is forbidden, or httpx.HTTPError on
        a network failure that outlived the retries.
        """
        if not self.allowed(url):
            log.info("robots.txt disallows %s — skipping", url)
            raise RobotsDisallowed(url)
        return self._get_with_retry(url)

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type(
            (RetryableStatus, httpx.TimeoutException, httpx.TransportError)
        ),
        reraise=True,
    )
    def _get_with_retry(self, url: str) -> FetchResult:
        self._throttle.wait(urlparse(url).netloc)
        resp = self._client.get(url)
        if resp.status_code in RETRYABLE_STATUS:
            raise RetryableStatus(resp.status_code, url)
        # 403/404 and friends are answers, not failures to retry (brief §6.1).
        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text,
            content_type=resp.headers.get("content-type", ""),
        )

    def resolve(self, url: str) -> str:
        """Return the final URL after redirects, without downloading the body.

        Used for Google News redirect resolution. Falls back to the input URL if
        the host will not answer — a redirect we cannot follow is not a reason
        to lose the item.
        """
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        try:
            self._throttle.wait(urlparse(url).netloc)
            resp = self._client.head(url)
            if resp.status_code >= 400:
                # Some hosts refuse HEAD. One GET, then give up.
                self._throttle.wait(urlparse(url).netloc)
                resp = self._client.get(url)
            return str(resp.url)
        except httpx.HTTPError as exc:
            log.warning("could not resolve %s (%s); keeping original URL", url, exc)
            return url
