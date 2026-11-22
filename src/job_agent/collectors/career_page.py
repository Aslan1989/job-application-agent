"""Employer career-page adapter, restricted to published structured data.

Reads only schema.org `JobPosting` JSON-LD, which employers publish precisely so
that job search engines can index their vacancies. That keeps the adapter
honest: the company, title, location and description come from what the
employer declared, never from guessing at page text. A page with no JobPosting
yields nothing.

Access rules, enforced in code rather than documented and hoped for:
- `robots.txt` is consulted for every URL, including each followed link, and a
  disallowed URL is not fetched. An unreadable robots.txt means no fetch.
- A `Crawl-delay` in robots.txt raises this adapter's interval if it is longer
  than the local default.
- Link following is same-host only, bounded by `max_links`, one level deep from
  the configured index page. No login, cookie wall, CAPTCHA or anti-bot measure
  is touched, and nothing is submitted.

Not supported, by design: portals that render vacancies with JavaScript and
publish no JSON-LD (most SAP SuccessFactors and Workday instances). Those
cannot be read this way, and the adapter reports that instead of scraping.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Sequence
from urllib.parse import urljoin, urlsplit

import requests

from ..employers import CareerPage
from ..jobposting import JobPosting, extract_job_postings, extract_links
from ..ratelimit import RateLimiter, RobotsGate
from ..retry import retry_call
from ..sources import (NormalizedJob, board_city_gate, normalize_url,
                       today_iso)

log = logging.getLogger("job_agent.career_page")

USER_AGENT = "JobApplicationAgent/0.9 (personal job-search project)"
MAX_PAGE_BYTES = 2_000_000
# Path shapes that plausibly address a single vacancy. Used only to choose which
# same-host links to try; the JSON-LD on the page is what actually decides.
JOB_LINK = re.compile(
    r"/(job|jobs|stelle|stellen|stellenangebot\w*|vacanc\w+|career|careers|"
    r"karriere|position\w*|opening\w*|offer\w*|ausschreibung\w*)(/|[-_?#]|$)", re.I)


class CareerPageSource:
    """Normalized adapter over a configured list of employer career pages."""

    key = "career_page"
    label = "Employer career page (schema.org JobPosting)"
    allowed_hosts: tuple[str, ...] = ()
    # One pass over the configured pages per run; Stage A does the selecting.
    single_shot = True

    def __init__(self, pages: Sequence[CareerPage] = (), *, session: Any = None,
                 timeout: float = 20.0, attempts: int = 2, sleep: Any = None,
                 allow_remote_germany: bool = False, cities: Sequence[str] = (),
                 limiter: RateLimiter | None = None,
                 robots: RobotsGate | None = None,
                 max_pages_per_employer: int = 20) -> None:
        self.pages = tuple(pages)
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.allow_remote_germany = allow_remote_germany
        self.cities = tuple(cities)
        self.limiter = limiter if limiter is not None else RateLimiter.for_source(self.key)
        self.robots = robots if robots is not None else RobotsGate(USER_AGENT)
        self.max_pages_per_employer = max_pages_per_employer
        self.errors: list[str] = []
        self.robots_blocked: list[str] = []
        self.pages_fetched = 0
        self._postings: list[NormalizedJob] | None = None
        self._descriptions: dict[str, str] = {}
        self.allowed_hosts = tuple(sorted({page.host for page in self.pages if page.host}))

    @property
    def configured(self) -> bool:
        return bool(self.pages)

    # --- fetching ------------------------------------------------------

    def _fetch(self, url: str) -> str:
        """Fetch one page as text, after a robots check and a rate-limit wait."""
        if not self.robots.allowed(url):
            self.robots_blocked.append(url)
            raise PermissionError("robots.txt disallows this URL")
        delay = self.robots.crawl_delay(url)
        if delay is not None and delay > self.limiter.min_interval:
            # The site asked for a slower pace than our default; honour it.
            self.limiter.min_interval = min(float(delay), 30.0)
        self.limiter.wait()

        def once() -> str:
            response = self.session.get(
                url, headers={"User-Agent": USER_AGENT,
                              "Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout)
            response.raise_for_status()
            self.pages_fetched += 1
            return str(getattr(response, "text", "") or "")[:MAX_PAGE_BYTES]

        if self.attempts <= 1:
            return once()
        extra = {"sleep": self.sleep} if self.sleep is not None else {}
        return retry_call(once, attempts=self.attempts, label="career page", **extra)

    def _candidate_links(self, page: CareerPage, html: str) -> list[str]:
        host = page.host
        seen: set[str] = set()
        links: list[str] = []
        for href in extract_links(html):
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absolute = urljoin(page.url, href)
            parts = urlsplit(absolute)
            if parts.scheme != "https" or (parts.hostname or "").lower() != host:
                continue  # Same host only; never wander off the employer's site.
            if not JOB_LINK.search(parts.path):
                continue
            canonical = normalize_url(absolute)
            if not canonical or canonical in seen or canonical == normalize_url(page.url):
                continue
            seen.add(canonical)
            links.append(absolute)
            if len(links) >= page.max_links:
                break
        return links

    def _normalize(self, page: CareerPage, posting: JobPosting,
                   url: str) -> NormalizedJob | None:
        if posting.expired():
            log.info("%s: posting expired on %s", page.name, posting.valid_through)
            return None
        canonical = normalize_url(posting.url or url)
        if not canonical:
            return None
        # A stable id from the employer's own URL path, so the row survives
        # re-discovery without depending on this run's ordering.
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-",
                      urlsplit(canonical).path.strip("/") or page.host)[:150].strip("-")
        try:
            return NormalizedJob(
                source_key=self.key, source_label=f"{page.name} (career page)",
                external_id=f"{page.host}-{slug}"[:180],
                company=posting.company, role=posting.title,
                location=posting.location, job_url=posting.url or url,
                work_mode="Remote" if posting.remote else "Unknown",
                remote=posting.remote, found_on=today_iso(),
            )
        except ValueError:
            return None

    def _load(self) -> list[NormalizedJob]:
        if self._postings is not None:
            return self._postings
        self._postings = []
        for page in self.pages:
            budget = min(page.max_links, self.max_pages_per_employer)
            try:
                index_html = self._fetch(page.url)
            except Exception as exc:  # noqa: BLE001 - one employer must not kill the run
                self.errors.append(f"career_page:{page.name} unavailable "
                                   f"({type(exc).__name__})")
                continue
            found = 0
            for posting in extract_job_postings(index_html):
                job = self._normalize(page, posting, page.url)
                if job is not None:
                    self._descriptions[job.external_id] = posting.description
                    self._postings.append(job)
                    found += 1
            # Index pages usually list vacancies without per-job JSON-LD, so
            # follow a bounded set of same-host job links one level deep.
            for url in self._candidate_links(page, index_html)[:budget]:
                try:
                    html = self._fetch(url)
                except PermissionError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"career_page:{page.name} link failed "
                                       f"({type(exc).__name__})")
                    continue
                for posting in extract_job_postings(html):
                    job = self._normalize(page, posting, url)
                    if job is not None:
                        self._descriptions[job.external_id] = posting.description
                        self._postings.append(job)
                        found += 1
            if found == 0:
                self.errors.append(
                    f"career_page:{page.name} published no schema.org JobPosting "
                    "data (JavaScript-rendered portals cannot be read)")
        return self._postings

    # --- JobSource protocol --------------------------------------------

    def discover(self, *, term: str = "", city: str = "",
                 size: int = 100) -> list[NormalizedJob]:
        """Every posting in the configured cities.

        `term` and `city` are both intentionally unused: a career page has no
        search endpoint, and this source is read once per run. See
        `sources.board_city_gate`.
        """
        if not 1 <= size <= 500:
            raise ValueError("size must be 1..500")
        found: list[NormalizedJob] = []
        for job in self._load():
            if not board_city_gate(job.location, self.cities,
                                   fallback_city=city,
                                   allow_remote_germany=self.allow_remote_germany):
                continue
            found.append(job)
            if len(found) >= size:
                break
        return found

    def describe(self, job: NormalizedJob) -> str:
        if job.source_key != self.key:
            raise ValueError("CareerPageSource cannot describe a foreign posting")
        self._load()
        return self._descriptions.get(job.external_id, "")
