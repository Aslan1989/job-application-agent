"""Read-only targeted BA Jobsuche adapter (unofficial/community-documented endpoint).

The endpoint is not a supported public API and may change without notice.
Never send applications, scrape HTML, or follow arbitrary redirect URLs.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

import requests

from ..retry import retry_call
# strip_html lives with the normalized job schema because every source needs it.
from ..sources import NormalizedJob, strip_html, today_iso

__all__ = ["ArbeitsagenturCollector", "BASearchHit", "BASource", "strip_html"]

BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "PersonalJobAgent/0.8"}


def _location(raw: Mapping[str, Any]) -> str:
    # v6 response uses stellenlokationen, older v4 response uses arbeitsort.
    options = raw.get("stellenlokationen") or raw.get("arbeitsorte") or raw.get("arbeitsort")
    if isinstance(options, dict):
        options = [options]
    if isinstance(options, list):
        for item in options:
            if isinstance(item, dict):
                nested = item.get("arbeitsort") or item.get("adresse") or item
                if isinstance(nested, dict):
                    city = nested.get("ort") or nested.get("stadt") or nested.get("name")
                    if city:
                        return str(city).strip()
    return str(raw.get("ort") or raw.get("location") or "").strip()


def _company(raw: Mapping[str, Any]) -> str:
    value = raw.get("firma") or raw.get("arbeitgeber") or raw.get("unternehmen") or ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("firma") or ""
    return str(value).strip()


@dataclass(frozen=True)
class BASearchHit:
    refnr: str
    title: str
    company: str
    location: str
    url: str

    @property
    def job_id(self) -> str:
        return "ba:" + self.refnr


class ArbeitsagenturCollector:
    def __init__(self, session: Any = None, timeout: int = 18, *,
                 attempts: int = 1, sleep: Any = None):
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        # attempts=1 keeps the historical single-shot behaviour; the daily
        # pipeline raises it so a 429 or gateway blip does not lose the query.
        self.attempts = attempts
        self.sleep = sleep

    def _request(self, url: str, *, params: Any = None, label: str = "BA request") -> Any:
        def once() -> Any:
            kwargs: dict[str, Any] = {"headers": HEADERS, "timeout": self.timeout}
            if params is not None:
                kwargs["params"] = params
            response = self.session.get(url, **kwargs)
            response.raise_for_status()
            return response.json()

        if self.attempts <= 1:
            return once()
        extra = {"sleep": self.sleep} if self.sleep is not None else {}
        return retry_call(once, attempts=self.attempts, label=label, **extra)

    def search(self, *, term: str, city: str, size: int = 10,
               radius_km: int = 25) -> list[BASearchHit]:
        if not term or not city or not 1 <= size <= 50 or not 0 <= radius_km <= 200:
            raise ValueError("Invalid BA search parameters")
        payload = self._request(
            BASE_URL + "/pc/v6/jobs",
            params={"was": term, "wo": city, "size": size, "page": 1,
                    "umkreis": radius_km, "veroeffentlichtseit": 30},
            label=f"BA search ({term})",
        )
        if not isinstance(payload, dict):
            raise ValueError("BA Jobsuche returned non-object response")
        # A published 2026 schema change replaced stellenangebote by ergebnisliste.
        raw_hits = payload.get("ergebnisliste")
        if raw_hits is None:
            raw_hits = payload.get("stellenangebote")
        if not isinstance(raw_hits, list):
            # A query with no matches omits the results list altogether and
            # returns only result metadata. That is an empty day, not a schema
            # change, and must not be reported as a source failure.
            if payload.get("maxErgebnisse") == 0 or (
                    "maxErgebnisse" in payload and "page" in payload):
                return []
            raise ValueError("BA Jobsuche schema changed: missing ergebnisliste/stellenangebote")
        hits: list[BASearchHit] = []
        for raw in raw_hits:
            if not isinstance(raw, dict):
                continue
            ref = str(raw.get("referenznummer") or raw.get("refnr") or "").strip()
            title = str(raw.get("stellenangebotsTitel") or raw.get("titel") or raw.get("beruf") or "").strip()
            company = _company(raw)
            location = _location(raw)
            # Only source-defined reference identifiers are accepted.
            if not re.fullmatch(r"[\w-]{6,80}", ref) or not title or not company or not location:
                continue
            hits.append(BASearchHit(ref, title, company, location,
                                    "https://www.arbeitsagentur.de/jobsuche/jobdetail/" + ref))
        return hits

    def detail(self, refnr: str) -> str:
        if not re.fullmatch(r"[\w-]{6,80}", refnr):
            raise ValueError("Invalid BA reference number")
        code = base64.b64encode(refnr.encode("utf-8")).decode("ascii")
        payload = self._request(BASE_URL + "/pc/v4/jobdetails/" + code,
                                label="BA detail")
        if not isinstance(payload, dict):
            raise ValueError("BA detail returned non-object")
        return strip_html(payload.get("stellenangebotsBeschreibung") or
                          payload.get("stellenbeschreibung") or "")


class BASource:
    """Normalized `JobSource` facade over `ArbeitsagenturCollector`.

    The BA endpoint searches server-side by term and city, so `discover` passes
    the query straight through. Descriptions are fetched on demand because the
    search response carries none.
    """

    key = "ba"
    label = "BA Jobsuche (unofficial API)"

    def __init__(self, collector: ArbeitsagenturCollector | None = None, *,
                 radius_km: int = 25) -> None:
        self.collector = collector if collector is not None else ArbeitsagenturCollector(attempts=3)
        if not 0 <= radius_km <= 200:
            raise ValueError("radius_km must be 0..200")
        self.radius_km = radius_km

    def discover(self, *, term: str, city: str, size: int) -> list[NormalizedJob]:
        hits = self.collector.search(term=term, city=city, size=size,
                                     radius_km=self.radius_km)
        jobs: list[NormalizedJob] = []
        for hit in hits:
            try:
                jobs.append(NormalizedJob(
                    source_key=self.key, source_label=self.label,
                    external_id=hit.refnr, company=hit.company, role=hit.title,
                    location=hit.location, job_url=hit.url,
                    found_on=today_iso(),
                ))
            except ValueError:
                continue  # Malformed upstream record; skip rather than fail the run.
        return jobs

    def describe(self, job: NormalizedJob) -> str:
        if job.source_key != self.key:
            raise ValueError("BASource cannot describe a foreign posting")
        return self.collector.detail(job.external_id)
