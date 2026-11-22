"""Read-only adapter for Arbeitnow's public, paginated job board API.

Source: https://www.arbeitnow.com/blog/job-board-api
Do not scrape pages or submit applications. This endpoint has no full-text search;
filtering is performed locally after fetching a bounded number of pages.
"""
from __future__ import annotations

from typing import Any, Mapping

import requests

from ..sources import NormalizedJob, board_city_gate, strip_html, today_iso

BASE_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowCollector:
    def __init__(self, session: Any = None, timeout: float = 20.0) -> None:
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout

    def fetch(self, pages: int = 2) -> list[dict[str, Any]]:
        """Return at most `pages` pages, with stable slug-level deduplication.

        This bounded client deliberately does not follow arbitrary pagination URLs.
        """
        if not 1 <= pages <= 5:
            raise ValueError("pages must be between 1 and 5")
        jobs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            response = self.session.get(
                BASE_URL,
                params={"page": page},
                headers={"User-Agent": "JobApplicationAgent/0.8 (personal job-search project)"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
                raise ValueError("Unexpected Arbeitnow response: expected a data array")
            data = payload["data"]
            if not data:
                break
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                slug = str(entry.get("slug") or entry.get("url") or "").strip()
                if slug and slug not in seen:
                    seen.add(slug)
                    jobs.append(entry)
            links = payload.get("links")
            if isinstance(links, Mapping) and not links.get("next"):
                break
        return jobs


class ArbeitnowSource:
    """Normalized adapter. Arbeitnow has no server-side search.

    A bounded page window is fetched once per process and screened locally, so
    a plan with twenty queries still costs at most `pages` HTTP requests.
    Descriptions ship inside the listing, so `describe` needs no extra call.
    """

    key = "arbeitnow"
    label = "Arbeitnow (public job board API)"
    # No server-side search: one bounded page window per run, screened by city.
    single_shot = True

    def __init__(self, collector: ArbeitnowCollector | None = None, *,
                 pages: int = 3, allow_remote_germany: bool = False,
                 cities: tuple[str, ...] = ()) -> None:
        if not 1 <= pages <= 5:
            raise ValueError("pages must be between 1 and 5")
        self.collector = collector if collector is not None else ArbeitnowCollector()
        self.pages = pages
        self.allow_remote_germany = allow_remote_germany
        self.cities = cities
        self._entries: list[dict[str, Any]] | None = None
        self._descriptions: dict[str, str] = {}

    def _load(self) -> list[dict[str, Any]]:
        if self._entries is None:
            self._entries = self.collector.fetch(pages=self.pages)
        return self._entries

    def discover(self, *, term: str = "", city: str = "",
                 size: int = 100) -> list[NormalizedJob]:
        """Every posting in the configured cities. `term` and `city` are unused.

        Arbeitnow has no keyword search, and matching a career track's exact
        title phrase against a 300-entry feed discarded almost every real hit.
        The city gate and Stage A screen are what select here.
        """
        if not 1 <= size <= 500:
            raise ValueError("size must be 1..500")
        jobs: list[NormalizedJob] = []
        for entry in self._load():
            slug = str(entry.get("slug") or "").strip()
            title = str(entry.get("title") or "").strip()
            location = str(entry.get("location") or "").strip()
            if not slug or not title:
                continue
            if not board_city_gate(location, self.cities,
                                   fallback_city=city,
                                   allow_remote_germany=self.allow_remote_germany):
                continue
            try:
                job = NormalizedJob(
                    source_key=self.key, source_label=self.label,
                    external_id=slug[:180], company=str(entry.get("company_name") or "").strip(),
                    role=title, location=location,
                    job_url=str(entry.get("url") or "").strip(),
                    work_mode="Remote" if entry.get("remote") is True else "Unknown",
                    remote=entry.get("remote") is True, found_on=today_iso(),
                )
            except ValueError:
                continue
            self._descriptions[job.external_id] = strip_html(entry.get("description"))
            jobs.append(job)
            if len(jobs) >= size:
                break
        return jobs

    def describe(self, job: NormalizedJob) -> str:
        if job.source_key != self.key:
            raise ValueError("ArbeitnowSource cannot describe a foreign posting")
        return self._descriptions.get(job.external_id, "")
