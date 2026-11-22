"""Brave Search API adapter: discovery by web search, verified at the source.

How it works, and why in this order:

1. Query the Brave Web Search API with queries built from the candidate's
   verified skills *and* the career-track titles (`query_builder.py`).
2. Rank results: an employer's own site or its applicant-tracking host first,
   job aggregators last. A search hit is a *lead*, not a vacancy.
3. Fetch the employer's own page and read its schema.org `JobPosting` JSON-LD.
   The company, title, location and description come from that structured data.
   A page with no JobPosting, or one whose `validThrough` has passed, yields
   nothing — which is how "current availability" is verified rather than
   assumed from a search snippet.

Costs and limits are real and enforced here, not left to the caller: Brave's
free tier allows one request per second and 2,000 queries per month, so the
adapter rate-limits itself and caps queries per run.

Access rules: robots.txt is checked before fetching any employer page, and a
disallowed URL is skipped. No login, cookie wall or anti-bot measure is
touched, and nothing is ever submitted.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

import requests

from ..jobposting import extract_job_postings
from ..query_builder import SearchString, classify_domain, result_priority
from ..ratelimit import RateLimiter, RobotsGate
from ..retry import retry_call
from ..sources import (NormalizedJob, location_matches, normalize_url, today_iso)

log = logging.getLogger("job_agent.brave")

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
USER_AGENT = "JobApplicationAgent/0.9 (personal job-search project)"
MAX_PAGE_BYTES = 2_000_000
# Hosts that are never worth fetching: they are not the employer's posting.
SKIP_HOSTS = ("facebook.com", "twitter.com", "x.com", "instagram.com",
              "youtube.com", "reddit.com", "wikipedia.org", "glassdoor.")


class BraveSearchSource:
    """Discovery via Brave Search, verified against the employer's own page."""

    key = "brave"
    label = "Brave Search (employer postings, verified at source)"
    # Each API call is metered, so the pipeline invokes this source once per
    # run with the whole query plan rather than once per pipeline query.
    single_shot = True

    def __init__(self, api_key: str = "", *, session: Any = None,
                 timeout: float = 20.0, attempts: int = 3, sleep: Any = None,
                 country: str = "de", search_lang: str = "de",
                 results_per_query: int = 10, max_queries: int = 8,
                 max_verifications: int = 12, allow_aggregators: bool = False,
                 allow_remote_germany: bool = False, cities: Sequence[str] = (),
                 limiter: RateLimiter | None = None,
                 robots: RobotsGate | None = None,
                 queries: Sequence[SearchString] = ()) -> None:
        self.api_key = str(api_key or "").strip()
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.country = country
        self.search_lang = search_lang
        if not 1 <= results_per_query <= 20:
            raise ValueError("results_per_query must be 1..20")
        if not 1 <= max_queries <= 50:
            raise ValueError("max_queries must be 1..50")
        if not 1 <= max_verifications <= 60:
            raise ValueError("max_verifications must be 1..60")
        self.results_per_query = results_per_query
        self.max_queries = max_queries
        self.max_verifications = max_verifications
        self.allow_aggregators = allow_aggregators
        self.allow_remote_germany = allow_remote_germany
        self.cities = tuple(cities)
        self.limiter = limiter if limiter is not None else RateLimiter.for_source(self.key)
        self.robots = robots if robots is not None else RobotsGate(USER_AGENT)
        # Queries are injected so the pipeline, not this adapter, owns the
        # candidate profile. The adapter never reads the profile itself.
        self.queries = list(queries)

        self.errors: list[str] = []
        self.api_calls = 0
        self.results_seen = 0
        self.aggregators_skipped = 0
        self.verifications = 0
        self.verified = 0
        self.no_jobposting: list[str] = []
        self.expired = 0
        self.robots_blocked: list[str] = []
        self._postings: list[NormalizedJob] | None = None
        self._descriptions: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.queries)

    # --- Brave API -----------------------------------------------------

    def _search(self, query: str) -> list[Mapping[str, Any]]:
        self.limiter.wait()

        def once() -> Any:
            response = self.session.get(
                BRAVE_URL,
                params={"q": query, "count": self.results_per_query,
                        "country": self.country, "search_lang": self.search_lang,
                        "result_filter": "web", "safesearch": "off"},
                headers={"Accept": "application/json",
                         "Accept-Encoding": "gzip",
                         "X-Subscription-Token": self.api_key,
                         "User-Agent": USER_AGENT},
                timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        extra = {"sleep": self.sleep} if self.sleep is not None else {}
        payload = (once() if self.attempts <= 1
                   else retry_call(once, attempts=self.attempts,
                                   label="brave search", **extra))
        self.api_calls += 1
        if not isinstance(payload, Mapping):
            raise ValueError("Brave returned a non-object response")
        web = payload.get("web")
        results = web.get("results") if isinstance(web, Mapping) else None
        if results is None:
            # A query with no web results omits the block entirely. That is an
            # empty result set, not a schema change.
            if "query" in payload or "type" in payload:
                return []
            raise ValueError("Brave schema changed: missing web.results")
        if not isinstance(results, list):
            raise ValueError("Brave schema changed: web.results is not a list")
        return [item for item in results if isinstance(item, Mapping)]

    # --- verification at the employer's page ---------------------------

    def _fetch_page(self, url: str) -> str:
        if not self.robots.allowed(url):
            self.robots_blocked.append(url)
            raise PermissionError("robots.txt disallows this URL")
        self.limiter.wait()

        def once() -> str:
            response = self.session.get(
                url, headers={"User-Agent": USER_AGENT,
                              "Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout)
            response.raise_for_status()
            return str(getattr(response, "text", "") or "")[:MAX_PAGE_BYTES]

        if self.attempts <= 1:
            return once()
        extra = {"sleep": self.sleep} if self.sleep is not None else {}
        return retry_call(once, attempts=self.attempts, label="employer page", **extra)

    def _verify(self, url: str, query: SearchString) -> list[NormalizedJob]:
        """Read the employer's page and accept only what it actually published."""
        try:
            html = self._fetch_page(url)
        except PermissionError:
            return []
        except Exception as exc:  # noqa: BLE001 - one lead must not stop the run
            self.errors.append(f"brave: page unreadable ({type(exc).__name__})")
            return []
        postings = extract_job_postings(html)
        if not postings:
            # No structured data: the page may be a listing, a blog post or a
            # JavaScript portal. Guessing a vacancy from its text would invent
            # one, so the lead is dropped.
            self.no_jobposting.append(url)
            return []
        jobs: list[NormalizedJob] = []
        for posting in postings:
            if posting.expired():
                self.expired += 1
                continue
            if not location_matches(posting.location, self.cities,
                                    allow_remote_germany=self.allow_remote_germany):
                continue
            canonical = normalize_url(posting.url or url)
            if not canonical:
                continue
            external = canonical.split("//", 1)[-1][:180]
            try:
                job = NormalizedJob(
                    source_key=self.key, source_label=self.label,
                    external_id=external.replace("/", "_")[:180],
                    company=posting.company, role=posting.title,
                    location=posting.location, job_url=posting.url or url,
                    work_mode="Remote" if posting.remote else "Unknown",
                    remote=posting.remote, track=query.track, found_on=today_iso(),
                )
            except ValueError:
                continue
            self._descriptions[job.external_id] = posting.description
            jobs.append(job)
            self.verified += 1
        return jobs

    # --- loading -------------------------------------------------------

    def _load(self) -> list[NormalizedJob]:
        if self._postings is not None:
            return self._postings
        self._postings = []
        if not self.api_key:
            self.errors.append("brave: BRAVE_SEARCH_API_KEY is not set; source inactive")
            return self._postings
        if not self.queries:
            self.errors.append("brave: no search queries supplied; source inactive")
            return self._postings

        leads: list[tuple[int, str, SearchString]] = []
        seen_urls: set[str] = set()
        for query in self.queries[:self.max_queries]:
            try:
                results = self._search(query.text)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"brave: search failed ({type(exc).__name__})")
                continue
            self.results_seen += len(results)
            for item in results:
                url = str(item.get("url") or "").strip()
                canonical = normalize_url(url)
                if not canonical or canonical in seen_urls:
                    continue
                # Mark as seen before any rejection, so a URL that several
                # queries return is judged and counted exactly once.
                seen_urls.add(canonical)
                if any(host in canonical for host in SKIP_HOSTS):
                    continue
                kind = classify_domain(url)
                if kind == "aggregator" and not self.allow_aggregators:
                    self.aggregators_skipped += 1
                    continue
                leads.append((result_priority(url), url, query))

        # Employer-direct and ATS leads are verified first, so the fetch budget
        # is never spent on aggregator copies of the same advert.
        leads.sort(key=lambda entry: -entry[0])
        for _, url, query in leads:
            if self.verifications >= self.max_verifications:
                break
            self.verifications += 1
            self._postings.extend(self._verify(url, query))
        return self._postings

    # --- JobSource protocol --------------------------------------------

    def discover(self, *, term: str = "", city: str = "",
                 size: int = 100) -> list[NormalizedJob]:
        """Return verified postings. `term` and `city` are intentionally unused.

        Brave is searched once per run with the whole query plan, not once per
        pipeline query, because each API call is metered. The pipeline's own
        city gate still applies to everything returned.
        """
        if not 1 <= size <= 500:
            raise ValueError("size must be 1..500")
        return self._load()[:size]

    def describe(self, job: NormalizedJob) -> str:
        if job.source_key != self.key:
            raise ValueError("BraveSearchSource cannot describe a foreign posting")
        self._load()
        return self._descriptions.get(job.external_id, "")

    def report(self) -> dict[str, Any]:
        """Counters for the CLI, including what the API cost."""
        return {
            "api_calls": self.api_calls,
            "results_seen": self.results_seen,
            "aggregators_skipped": self.aggregators_skipped,
            "pages_verified": self.verifications,
            "postings_confirmed": self.verified,
            "no_jobposting_data": len(self.no_jobposting),
            "expired_postings": self.expired,
            "robots_blocked": len(self.robots_blocked),
        }
