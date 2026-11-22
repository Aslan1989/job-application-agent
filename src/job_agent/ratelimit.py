"""Per-source rate limiting and robots.txt gating.

Two separate obligations:

- **Rate limits** are contractual. Brave's free tier allows one request per
  second, so `RateLimiter` enforces a minimum interval per source rather than
  trusting the code not to burst.
- **robots.txt** governs fetching ordinary web pages. It is checked for the
  career-page and search-verification adapters, which read employer websites.
  A documented public JSON API (Greenhouse, Lever, Ashby, BA, Arbeitnow) is an
  interface published for programmatic use and is not gated this way.

Nothing here bypasses a restriction: a disallowed URL is simply not fetched,
and a robots.txt that cannot be read is treated as "do not fetch".
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("job_agent.ratelimit")

# Conservative defaults, in seconds between requests to one source.
DEFAULT_INTERVALS: dict[str, float] = {
    "brave": 1.1,        # Brave free tier: 1 request/second.
    "career_page": 2.0,  # Ordinary employer websites: be a polite guest.
    "greenhouse": 0.5,
    "lever": 0.5,
    "ashby": 0.5,
    "arbeitnow": 0.5,
    "ba": 0.3,
}


@dataclass
class RateLimiter:
    """Minimum-interval limiter. `clock`/`sleep` are injected so tests are fast."""

    min_interval: float = 1.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last: float | None = field(default=None, repr=False)
    waits: int = 0
    slept: float = 0.0

    def __post_init__(self) -> None:
        if self.min_interval < 0:
            raise ValueError("min_interval cannot be negative")

    def wait(self) -> float:
        """Block until the next request is allowed. Returns seconds slept."""
        now = self.clock()
        if self._last is not None:
            remaining = self.min_interval - (now - self._last)
            if remaining > 0:
                self.sleep(remaining)
                self.waits += 1
                self.slept += remaining
                self._last = self.clock()
                return remaining
        self._last = now
        return 0.0

    @classmethod
    def for_source(cls, key: str, **kwargs) -> "RateLimiter":
        return cls(min_interval=DEFAULT_INTERVALS.get(key, 1.0), **kwargs)


class RobotsGate:
    """Caches one robots.txt per origin and answers allow/deny per URL.

    Fail-closed: an unreadable or malformed robots.txt means the URL is not
    fetched. A 404 robots.txt means the site published no restrictions, which
    by the standard permits fetching.
    """

    def __init__(self, user_agent: str, *, opener: Callable[[str], str] | None = None,
                 allow_when_missing: bool = True) -> None:
        self.user_agent = user_agent
        self.opener = opener
        self.allow_when_missing = allow_when_missing
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.checked = 0
        self.blocked = 0

    @staticmethod
    def origin(url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return ""
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def _parser(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        if origin in self._cache:
            return self._cache[origin]
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(origin + "/robots.txt")
        try:
            if self.opener is not None:
                parser.parse(self.opener(origin + "/robots.txt").splitlines())
            else:
                parser.read()
        except Exception as exc:  # noqa: BLE001 - any failure is "unknown"
            log.info("robots.txt unavailable for %s (%s)", origin, type(exc).__name__)
            parser = None if not self.allow_when_missing else parser
            if parser is not None:
                parser.parse([])  # No rules published: nothing is disallowed.
        self._cache[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        origin = self.origin(url)
        if not origin:
            return False
        self.checked += 1
        parser = self._parser(origin)
        if parser is None:
            self.blocked += 1
            return False
        try:
            verdict = parser.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001
            verdict = False
        if not verdict:
            self.blocked += 1
            log.info("robots.txt disallows %s", url)
        return bool(verdict)

    def crawl_delay(self, url: str) -> float | None:
        parser = self._parser(self.origin(url))
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:  # noqa: BLE001
            return None
        return float(delay) if delay is not None else None
