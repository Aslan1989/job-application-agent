"""Employer-direct discovery: Ashby, the employer registry and career pages.

All HTTP is mocked. Response shapes come from read-only calls made against the
live public APIs on 2026-11-21 and are kept here as fixtures.
"""
from __future__ import annotations

import json

import pytest

from job_agent.collectors.ats import AshbySource, GreenhouseSource, LeverSource
from job_agent.collectors.career_page import CareerPageSource
from job_agent.employers import (CareerPage, Employer, Registry, load_registry,
                                 merge_board_overrides, summarize)
from job_agent.jobposting import extract_job_postings
from job_agent.ratelimit import RateLimiter, RobotsGate

# Shape confirmed against https://api.ashbyhq.com/posting-api/job-board/<board>
ASHBY_PAYLOAD = {
    "apiVersion": "1",
    "jobs": [
        {"id": "564a3a0b-4748-493c-84ba-d8625913bf85",
         "title": "AI Automation Engineer", "location": "Berlin/Remote",
         "secondaryLocations": [{"location": "Germany Remote"}],
         "isListed": True, "isRemote": True,
         "jobUrl": "https://jobs.ashbyhq.com/acme/564a3a0b",
         "applyUrl": "https://jobs.ashbyhq.com/acme/564a3a0b/application",
         "descriptionPlain": "Build Python automation and LLM workflows. "
                             "Docker and SQL experience welcome. " * 4,
         "descriptionHtml": "<p>ignored when plain text exists</p>",
         "employmentType": "FullTime", "department": "Engineering"},
        {"id": "unlisted-1", "title": "Secret Role", "location": "Frankfurt am Main",
         "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/unlisted-1",
         "descriptionPlain": "Should never surface."},
        {"id": "no-url", "title": "Broken", "location": "Berlin", "isListed": True,
         "jobUrl": "", "applyUrl": "", "descriptionPlain": "x"},
    ],
}


class Response:
    def __init__(self, payload=None, text="", error=None):
        self._payload = payload
        self.text = text
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise RuntimeError(self.error)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for marker, value in self.routes.items():
            if marker in url:
                if isinstance(value, Exception):
                    raise value
                return value
        return Response(error="404 not found")


# --- Ashby -----------------------------------------------------------------


def test_ashby_adapter_normalizes_a_live_shaped_payload():
    session = Session({"api.ashbyhq.com": Response(ASHBY_PAYLOAD)})
    source = AshbySource(["acme"], session=session, attempts=1,
                         companies={"acme": "ACME GmbH"},
                         allow_remote_germany=True)
    found = source.discover(city="Berlin")
    assert [job.job_id for job in found] == ["ashby:acme-564a3a0b-4748-493c-84ba-d8625913bf85"]
    job = found[0]
    assert job.company == "ACME GmbH", "display name from the registry, not the token"
    assert job.role == "AI Automation Engineer"
    # Secondary locations are preserved so a multi-city posting stays findable.
    assert "Germany Remote" in job.location
    assert job.remote is True and job.work_mode == "Remote"
    body = source.describe(job)
    assert "Python automation" in body
    assert "Employment type: FullTime" in body, "lets the pre-screen see Intern roles"


def test_ashby_skips_unlisted_and_malformed_postings():
    session = Session({"api.ashbyhq.com": Response(ASHBY_PAYLOAD)})
    source = AshbySource(["acme"], session=session, attempts=1, allow_remote_germany=True)
    roles = {job.role for job in source.discover(city="Frankfurt am Main")}
    assert "Secret Role" not in roles, "isListed=False means the employer unpublished it"
    assert "Broken" not in roles


def test_ashby_schema_change_is_reported_not_silently_empty():
    session = Session({"api.ashbyhq.com": Response({"apiVersion": "1"})})
    source = AshbySource(["acme"], session=session, attempts=1)
    assert source.discover(city="Berlin") == []
    assert any("ashby:acme unavailable" in error for error in source.errors)


def test_board_sources_are_single_shot_and_ignore_the_search_term():
    """These APIs have no keyword search; filtering by a track title lost everything."""
    for cls in (GreenhouseSource, LeverSource, AshbySource):
        assert cls.single_shot is True
    session = Session({"api.ashbyhq.com": Response(ASHBY_PAYLOAD)})
    source = AshbySource(["acme"], session=session, attempts=1, allow_remote_germany=True)
    with_term = source.discover(term="totally unrelated phrase", city="Berlin")
    assert len(with_term) == 1, "the term must not filter a whole-board source"
    source.discover(term="another", city="Berlin")
    assert len(session.calls) == 1, "one board fetch per run"


def test_board_rate_limiter_is_consulted():
    slept = []
    limiter = RateLimiter(min_interval=0.5, clock=lambda: 100.0, sleep=slept.append)
    session = Session({"api.ashbyhq.com": Response(ASHBY_PAYLOAD)})
    source = AshbySource(["acme", "other"], session=session, attempts=1, limiter=limiter)
    source.discover(city="Berlin")
    # Two boards, one frozen clock: the second fetch must wait.
    assert slept == [0.5]


# --- registry --------------------------------------------------------------


def test_registry_file_loads_and_only_lists_verified_boards():
    book = load_registry("data/employers.json")
    assert book.employers, "the shipped registry should not be empty"
    assert book.finding, "the registry records what the probe actually found"
    for employer in book.employers:
        assert employer.verified_on, f"{employer.name} has no verification date"
        assert employer.platform in ("greenhouse", "lever", "ashby")
        assert "total" in employer.observed, f"{employer.name} was never probed"
    # Enabled entries must be the ones with observed relevance.
    for employer in book.enabled_employers():
        assert employer.relevant > 0, f"{employer.name} is enabled but had no hits"
    assert "greenhouse" in book.platforms_in_use()
    assert summarize(book), "registry renders for the CLI"


def test_registry_rejects_bad_entries():
    with pytest.raises(ValueError, match="unsupported platform"):
        Employer(name="X", platform="workday", board="x")
    with pytest.raises(ValueError, match="invalid board token"):
        Employer(name="X", platform="greenhouse", board="../etc/passwd")
    with pytest.raises(ValueError, match="needs a name"):
        Employer(name="  ", platform="greenhouse", board="x")
    with pytest.raises(ValueError, match="absolute https"):
        CareerPage(name="X", url="http://insecure.example/jobs")


def test_registry_validation_errors_are_loud(tmp_path):
    path = tmp_path / "employers.json"
    path.write_text(json.dumps({"schema_version": "0.9", "employers": [
        {"name": "A", "platform": "greenhouse", "board": "a"},
        {"name": "A again", "platform": "greenhouse", "board": "A"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate board"):
        load_registry(path)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_registry(path)

    path.write_text(json.dumps({"schema_version": "0.1"}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_registry(path)

    # A missing file is normal: ATS sources simply stay inactive.
    assert load_registry(tmp_path / "absent.json").employers == ()


def test_env_overrides_merge_without_losing_registry_boards():
    book = Registry(employers=(
        Employer(name="Raisin", platform="greenhouse", board="raisin"),
        Employer(name="Off", platform="greenhouse", board="off", enabled=False)))
    boards, names = merge_board_overrides(book, "greenhouse", ["adhoc", "raisin"])
    assert boards == ("raisin", "adhoc")
    assert names["Raisin" if False else "raisin"] == "Raisin"
    assert names["adhoc"] == "adhoc"


# --- career pages ----------------------------------------------------------

JOB_LD = {
    "@context": "https://schema.org", "@type": "JobPosting",
    "title": "Backend Developer (m/w/d)",
    "hiringOrganization": {"@type": "Organization", "name": "ACME GmbH"},
    "jobLocation": {"@type": "Place", "address": {
        "@type": "PostalAddress", "addressLocality": "Frankfurt am Main",
        "addressCountry": "DE"}},
    "description": "<p>Java, Spring Boot, SQL und Docker. "
                   "Kommerzielle Erfahrung ist keine Voraussetzung.</p>" * 3,
    "datePosted": "2026-11-01", "validThrough": "2027-06-30",
    "employmentType": "FULL_TIME", "url": "https://acme.example/jobs/42",
}
INDEX_HTML = """<html><body>
<a href="/jobs/42">Backend Developer</a>
<a href="/jobs/43">Another role</a>
<a href="https://elsewhere.example/jobs/99">Off-site</a>
<a href="/impressum">Legal</a>
</body></html>"""


def ld_page(payload) -> str:
    return ('<html><head><script type="application/ld+json">'
            + json.dumps(payload) + "</script></head><body>x</body></html>")


def page_source(routes, **kwargs):
    robots = RobotsGate("test-agent", opener=lambda url: "User-agent: *\nAllow: /")
    return CareerPageSource(
        [CareerPage(name="ACME", url="https://acme.example/careers",
                    enabled=True, max_links=5)],
        session=Session(routes), attempts=1, robots=robots,
        limiter=RateLimiter(min_interval=0.0), **kwargs)


def test_career_page_reads_only_published_jobposting_data():
    source = page_source({
        "acme.example/careers": Response(text=INDEX_HTML),
        "acme.example/jobs/42": Response(text=ld_page(JOB_LD)),
        "acme.example/jobs/43": Response(text="<html>no structured data</html>"),
    })
    found = source.discover(city="Frankfurt am Main")
    assert len(found) == 1
    job = found[0]
    assert job.company == "ACME GmbH", "company comes from the employer's own data"
    assert job.role == "Backend Developer (m/w/d)"
    assert job.location.startswith("Frankfurt am Main")
    assert "Spring Boot" in source.describe(job)
    # A page without JobPosting data yields nothing rather than a guess.
    assert source.pages_fetched == 3


def test_career_page_never_leaves_the_configured_host():
    session = Session({
        "acme.example/careers": Response(text=INDEX_HTML),
        "acme.example/jobs/42": Response(text=ld_page(JOB_LD)),
        "acme.example/jobs/43": Response(text="<html></html>"),
    })
    source = page_source({})
    source.session = session
    source.discover(city="Frankfurt am Main")
    assert not any("elsewhere.example" in url for url, _ in session.calls)
    assert not any("impressum" in url for url, _ in session.calls)


def test_career_page_obeys_robots_disallow():
    robots = RobotsGate("test-agent",
                        opener=lambda url: "User-agent: *\nDisallow: /jobs/")
    source = CareerPageSource(
        [CareerPage(name="ACME", url="https://acme.example/careers", enabled=True)],
        session=Session({"acme.example/careers": Response(text=INDEX_HTML),
                         "acme.example/jobs/42": Response(text=ld_page(JOB_LD))}),
        attempts=1, robots=robots, limiter=RateLimiter(min_interval=0.0))
    assert source.discover(city="Frankfurt am Main") == []
    assert any("/jobs/42" in url for url in source.robots_blocked)


def test_career_page_honours_a_longer_crawl_delay():
    robots = RobotsGate("test-agent",
                        opener=lambda url: "User-agent: *\nAllow: /\nCrawl-delay: 7")
    limiter = RateLimiter(min_interval=1.0, clock=lambda: 0.0, sleep=lambda _: None)
    source = CareerPageSource(
        [CareerPage(name="ACME", url="https://acme.example/careers", enabled=True)],
        session=Session({"acme.example/careers": Response(text="<html></html>")}),
        attempts=1, robots=robots, limiter=limiter)
    source.discover(city="Frankfurt am Main")
    assert limiter.min_interval == 7.0


def test_career_page_reports_a_javascript_portal_instead_of_scraping():
    source = page_source({"acme.example/careers": Response(
        text="<html><body><div id='app'></div></body></html>")})
    assert source.discover(city="Frankfurt am Main") == []
    assert any("no schema.org JobPosting" in error for error in source.errors)


def test_expired_posting_is_dropped_but_a_missing_date_is_not():
    expired = {**JOB_LD, "validThrough": "2020-01-01"}
    source = page_source({"acme.example/careers": Response(text=ld_page(expired))})
    assert source.discover(city="Frankfurt am Main") == []

    undated = {k: v for k, v in JOB_LD.items() if k != "validThrough"}
    assert extract_job_postings(ld_page(undated))[0].expired() is False
