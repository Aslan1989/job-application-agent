"""Public ATS board adapters: Greenhouse, Lever and Ashby job boards.

Both vendors publish a documented, unauthenticated, read-only JSON endpoint per
employer board. Neither has a server-side keyword search, so one bounded fetch
per configured board is screened locally.

Scope limits, deliberately:
- An employer is queried only when its board token is explicitly configured.
  There is no board discovery, no token guessing and no crawling.
- Only these JSON APIs are called. No career-page HTML is scraped, no login,
  cookie wall, CAPTCHA or anti-bot measure is touched, and nothing is submitted.
- One request per board per process, with backoff, well inside fair use.

Verification status (checked read-only on 2026-11-21):
- Greenhouse and Ashby response shapes were confirmed against live public
  boards, and `data/employers.json` lists only boards that answered.
- The Lever adapter is built to the documented Postings API shape but no live
  board was available to confirm it; treat it as unverified.
Confirm any board token and its terms before enabling it.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

import requests

from ..retry import retry_call
from ..sources import NormalizedJob, location_matches, strip_html, today_iso

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{board}"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"
USER_AGENT = "JobApplicationAgent/0.8 (personal job-search project)"
_BOARD_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}")


class _BoardSource:
    """Shared behaviour: fetch each configured board once, then filter locally.

    These APIs have no keyword search, and an employer board holds tens of
    postings rather than the whole market. Filtering them by a career track's
    exact title phrase discards nearly everything, so the adapter returns every
    posting in the configured cities and lets the pipeline's Stage A screen and
    per-track quota do the selecting. `single_shot` therefore makes the
    pipeline call this source once per run instead of once per planned query.
    """

    key = "board"
    label = "Public ATS board"
    single_shot = True

    def __init__(self, boards: Sequence[str] = (), *, session: Any = None,
                 timeout: float = 20.0, attempts: int = 3, sleep: Any = None,
                 allow_remote_germany: bool = False,
                 cities: Sequence[str] = (),
                 companies: Mapping[str, str] | None = None,
                 limiter: Any = None) -> None:
        cleaned: list[str] = []
        for board in boards:
            token = str(board).strip()
            if not token:
                continue
            if not _BOARD_TOKEN.fullmatch(token):
                raise ValueError(f"Invalid {self.key} board token: {token!r}")
            if token not in cleaned:
                cleaned.append(token)
        self.boards = tuple(cleaned)
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.allow_remote_germany = allow_remote_germany
        self.cities = tuple(cities)
        # Display names per board token, so a row shows "Celonis" and not "celonis".
        self.companies = dict(companies or {})
        self.limiter = limiter
        self.errors: list[str] = []
        self._postings: dict[str, list[NormalizedJob]] | None = None
        self._descriptions: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.boards)

    def display_name(self, board: str) -> str:
        return self.companies.get(board) or board

    def _fetch_json(self, url: str, *, params: Mapping[str, Any] | None = None,
                    label: str = "board") -> Any:
        def once() -> Any:
            if self.limiter is not None:
                self.limiter.wait()
            response = self.session.get(url, params=dict(params or {}),
                                        headers={"Accept": "application/json",
                                                 "User-Agent": USER_AGENT},
                                        timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        if self.attempts <= 1:
            return once()
        extra = {"sleep": self.sleep} if self.sleep is not None else {}
        return retry_call(once, attempts=self.attempts, label=label, **extra)

    def _load(self) -> dict[str, list[NormalizedJob]]:
        if self._postings is not None:
            return self._postings
        self._postings = {}
        for board in self.boards:
            try:
                self._postings[board] = self._parse(board, self._download(board))
            except Exception as exc:  # noqa: BLE001 - one board must not kill the run
                self.errors.append(f"{self.key}:{board} unavailable ({type(exc).__name__})")
                self._postings[board] = []
        return self._postings

    def _download(self, board: str) -> Any:
        raise NotImplementedError

    def _parse(self, board: str, payload: Any) -> list[NormalizedJob]:
        raise NotImplementedError

    def discover(self, *, term: str = "", city: str = "", size: int = 100) -> list[NormalizedJob]:
        """Every posting in the configured cities. `term` is intentionally unused."""
        if not 1 <= size <= 500:
            raise ValueError("size must be 1..500")
        wanted = tuple(part for part in (city, *self.cities) if part)
        found: list[NormalizedJob] = []
        for postings in self._load().values():
            for job in postings:
                if not location_matches(job.location, wanted,
                                        allow_remote_germany=self.allow_remote_germany):
                    continue
                found.append(job)
                if len(found) >= size:
                    return found
        return found

    def describe(self, job: NormalizedJob) -> str:
        if job.source_key != self.key:
            raise ValueError(f"{type(self).__name__} cannot describe a foreign posting")
        self._load()
        return self._descriptions.get(job.external_id, "")


class GreenhouseSource(_BoardSource):
    """Greenhouse Job Board API: one board token per employer."""

    key = "greenhouse"
    label = "Greenhouse public job board"

    def _download(self, board: str) -> Any:
        return self._fetch_json(GREENHOUSE_URL.format(board=board),
                                params={"content": "true"},
                                label=f"greenhouse board {board}")

    def _parse(self, board: str, payload: Any) -> list[NormalizedJob]:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Greenhouse schema changed: missing jobs array")
        jobs: list[NormalizedJob] = []
        for raw in payload["jobs"]:
            if not isinstance(raw, Mapping):
                continue
            posting_id = str(raw.get("id") or "").strip()
            title = str(raw.get("title") or "").strip()
            location = raw.get("location")
            city = str((location or {}).get("name") if isinstance(location, Mapping) else "").strip()
            url = str(raw.get("absolute_url") or "").strip()
            if not posting_id.isdigit() or not title or not city or not url:
                continue
            company = str(raw.get("company_name") or "").strip() or self.display_name(board)
            try:
                job = NormalizedJob(
                    source_key=self.key, source_label=self.label,
                    external_id=f"{board}-{posting_id}", company=company,
                    role=title, location=city, job_url=url, found_on=today_iso(),
                )
            except ValueError:
                continue
            self._descriptions[job.external_id] = strip_html(raw.get("content"))
            jobs.append(job)
        return jobs


class LeverSource(_BoardSource):
    """Lever Postings API: one company handle per employer board."""

    key = "lever"
    label = "Lever public postings"

    def _download(self, board: str) -> Any:
        return self._fetch_json(LEVER_URL.format(board=board),
                                params={"mode": "json"},
                                label=f"lever board {board}")

    def _parse(self, board: str, payload: Any) -> list[NormalizedJob]:
        if not isinstance(payload, list):
            raise ValueError("Lever schema changed: expected a postings array")
        jobs: list[NormalizedJob] = []
        for raw in payload:
            if not isinstance(raw, Mapping):
                continue
            posting_id = str(raw.get("id") or "").strip()
            title = str(raw.get("text") or "").strip()
            categories = raw.get("categories")
            city = str((categories or {}).get("location") if isinstance(categories, Mapping) else "").strip()
            url = str(raw.get("hostedUrl") or "").strip()
            if not posting_id or not title or not city or not url:
                continue
            try:
                job = NormalizedJob(
                    source_key=self.key, source_label=self.label,
                    external_id=f"{board}-{posting_id}"[:180],
                    company=self.display_name(board), role=title, location=city,
                    job_url=url, found_on=today_iso(),
                )
            except ValueError:
                continue
            # Lever splits the advert across several fields; keep them all so
            # mandatory requirements in a "Requirements" list are not lost.
            body = [str(raw.get("descriptionPlain") or raw.get("description") or "")]
            for block in raw.get("lists") or []:
                if isinstance(block, Mapping):
                    body.append(str(block.get("text") or ""))
                    body.append(strip_html(block.get("content")))
            body.append(str(raw.get("additionalPlain") or ""))
            self._descriptions[job.external_id] = strip_html("\n".join(body))
            jobs.append(job)
        return jobs


class AshbySource(_BoardSource):
    """Ashby public Job Posting API: one board name per employer.

    Response shape confirmed read-only against a live public board on
    2026-11-21: `{"apiVersion": ..., "jobs": [...]}` with `id`, `title`,
    `location`, `secondaryLocations`, `isListed`, `isRemote`, `jobUrl` and
    `descriptionPlain` per posting. Board names are case-sensitive.
    """

    key = "ashby"
    label = "Ashby public job board"

    def _download(self, board: str) -> Any:
        return self._fetch_json(ASHBY_URL.format(board=board),
                                label=f"ashby board {board}")

    def _parse(self, board: str, payload: Any) -> list[NormalizedJob]:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Ashby schema changed: missing jobs array")
        jobs: list[NormalizedJob] = []
        for raw in payload["jobs"]:
            if not isinstance(raw, Mapping):
                continue
            # isListed False means the employer unpublished it from the board.
            if raw.get("isListed") is False:
                continue
            posting_id = str(raw.get("id") or "").strip()
            title = str(raw.get("title") or "").strip()
            url = str(raw.get("jobUrl") or raw.get("applyUrl") or "").strip()
            places = [str(raw.get("location") or "").strip()]
            for extra in raw.get("secondaryLocations") or []:
                if isinstance(extra, Mapping):
                    places.append(str(extra.get("location") or "").strip())
                elif isinstance(extra, str):
                    places.append(extra.strip())
            location = "; ".join(place for place in places if place)
            if not posting_id or not title or not location or not url:
                continue
            remote = raw.get("isRemote") is True
            try:
                job = NormalizedJob(
                    source_key=self.key, source_label=self.label,
                    external_id=f"{board}-{posting_id}"[:180],
                    company=self.display_name(board), role=title,
                    location=location, job_url=url,
                    work_mode="Remote" if remote else "Unknown", remote=remote,
                    found_on=today_iso(),
                )
            except ValueError:
                continue
            body = [str(raw.get("descriptionPlain") or "")]
            if not body[0].strip():
                body = [strip_html(raw.get("descriptionHtml"))]
            employment = str(raw.get("employmentType") or "").strip()
            if employment:
                # Ashby reports Intern/FullTime/PartTime explicitly; keeping it
                # in the text lets the eligibility pre-screen see it.
                body.append(f"Employment type: {employment}")
            self._descriptions[job.external_id] = strip_html("\n".join(body))
            jobs.append(job)
        return jobs
