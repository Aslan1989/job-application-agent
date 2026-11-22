"""Brave Search discovery, profile-driven queries, rate limits and robots.txt.

No network: the Brave API and every employer page are mocked. The API response
shape follows Brave's documented `web.results` envelope.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from job_agent.candidate_profile import load_candidate_profile
from job_agent.career_tracks import infer_track, resolve_tracks
from job_agent.collectors.brave import BraveSearchSource
from job_agent.query_builder import (build_queries, classify_domain,
                                     describe_sources, evidenced_skill_terms,
                                     result_priority)
from job_agent.ratelimit import DEFAULT_INTERVALS, RateLimiter, RobotsGate

CITIES = ["Frankfurt am Main", "Stuttgart", "Heilbronn"]


def profile():
    return load_candidate_profile(Path("data/candidate_profile.json"))


# --- query generation ------------------------------------------------------


def test_queries_come_from_evidenced_skills_not_only_titles():
    terms = evidenced_skill_terms(profile())
    for expected in ("PHP", "Java", "Spring Boot", "Docker", "Linux", "C++",
                     "SQL", "RAG", "Python"):
        assert expected in terms, f"{expected} is evidenced but was not searched for"
    # SAP ABAP is marked not_evidenced and must never become a search claim.
    assert not any("abap" in term.casefold() for term in terms)


def test_query_plan_mixes_skill_and_title_queries():
    queries = build_queries(profile(), CITIES, 12, today=date(2026, 11, 21))
    assert len(queries) == 12
    kinds = {query.kind for query in queries}
    assert kinds == {"title", "skill"}, "a run of pure job titles ignores the profile"
    assert {query.city for query in queries} <= set(CITIES)
    # Employer-focused wording, so the search leans to careers pages.
    assert any("Karriere" in query.text or "careers" in query.text
               for query in queries)
    assert len({query.text for query in queries}) == len(queries), "no duplicates"


def test_query_plan_rotates_daily_and_is_deterministic_per_day():
    first = build_queries(profile(), CITIES, 10, today=date(2026, 11, 21))
    again = build_queries(profile(), CITIES, 10, today=date(2026, 11, 21))
    later = build_queries(profile(), CITIES, 10, today=date(2026, 11, 26))
    assert [q.text for q in first] == [q.text for q in again]
    assert [q.text for q in first] != [q.text for q in later]


def test_query_plan_can_be_restricted_to_one_track():
    queries = build_queries(profile(), CITIES, 6,
                            tracks=resolve_tracks(["ai_engineering"]),
                            today=date(2026, 11, 21))
    assert {query.track for query in queries} == {"ai_engineering"}
    assert any("AI Engineer" in q.text or "LLM" in q.text or "RAG" in q.text
               or "KI" in q.text for q in queries)


def test_query_budget_is_validated():
    with pytest.raises(ValueError):
        build_queries(profile(), CITIES, 0)
    with pytest.raises(ValueError):
        build_queries(profile(), [], 5)


def test_profile_diagnostics_name_what_was_excluded():
    facts = describe_sources(profile())
    assert "SAP ABAP" in facts["excluded_not_evidenced"]
    assert facts["cities"] == CITIES


# --- employer vs aggregator ------------------------------------------------


@pytest.mark.parametrize("url,kind", [
    ("https://acme.de/karriere/backend-developer", "employer"),
    ("https://jobs.acme-gmbh.com/apply/1", "employer"),
    ("https://boards.greenhouse.io/raisin/jobs/1", "ats"),
    ("https://jobs.lever.co/acme/1", "ats"),
    ("https://jobs.ashbyhq.com/acme/1", "ats"),
    ("https://acme.jobs.personio.de/job/1", "ats"),
    ("https://de.indeed.com/viewjob?jk=1", "aggregator"),
    ("https://www.stepstone.de/stelle/1", "aggregator"),
    ("https://www.linkedin.com/jobs/view/1", "aggregator"),
    ("https://www.xing.com/jobs/1", "aggregator"),
    ("https://www.arbeitsagentur.de/jobsuche/jobdetail/1", "aggregator"),
])
def test_domain_classification(url, kind):
    assert classify_domain(url) == kind


def test_employer_and_ats_results_outrank_aggregators():
    assert result_priority("https://acme.de/karriere/1") > result_priority(
        "https://de.indeed.com/viewjob?jk=1")
    assert result_priority("https://boards.greenhouse.io/x/jobs/1") == result_priority(
        "https://acme.de/karriere/1")


def test_track_inference_labels_postings_from_sources_without_search():
    assert infer_track("Backend Entwickler Java /Spring Boot") == "java_backend"
    assert infer_track("AI Engineer / KI-Entwickler - LLM & RAG") == "ai_engineering"
    assert infer_track("SAP Berater BW/4HANA") == "sap_abap"
    assert infer_track("Office Manager") == ""


# --- rate limiting and robots.txt -----------------------------------------


def test_rate_limiter_enforces_the_minimum_interval():
    now = {"t": 0.0}
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    limiter = RateLimiter(min_interval=1.1, clock=lambda: now["t"], sleep=sleep)
    assert limiter.wait() == 0.0
    assert limiter.wait() == pytest.approx(1.1)
    now["t"] += 5.0
    assert limiter.wait() == 0.0, "no wait once enough time has passed"
    assert slept == [pytest.approx(1.1)]


def test_brave_default_interval_respects_one_request_per_second():
    assert DEFAULT_INTERVALS["brave"] >= 1.0
    assert RateLimiter.for_source("brave").min_interval >= 1.0


def test_robots_gate_allows_and_blocks():
    gate = RobotsGate("test-agent", opener=lambda url:
                      "User-agent: *\nDisallow: /private/\nAllow: /")
    assert gate.allowed("https://acme.example/jobs/1")
    assert not gate.allowed("https://acme.example/private/1")
    assert gate.blocked == 1
    # One robots.txt per origin, cached.
    calls = []
    counting = RobotsGate("test-agent", opener=lambda url: calls.append(url) or "")
    counting.allowed("https://acme.example/a")
    counting.allowed("https://acme.example/b")
    assert len(calls) == 1


def test_robots_gate_rejects_non_http_urls():
    gate = RobotsGate("test-agent", opener=lambda url: "")
    assert not gate.allowed("file:///etc/passwd")
    assert not gate.allowed("")


# --- Brave adapter ---------------------------------------------------------


JOB_LD = {
    "@context": "https://schema.org", "@type": "JobPosting",
    "title": "AI Engineer (m/w/d)",
    "hiringOrganization": {"name": "ACME GmbH"},
    "jobLocation": {"address": {"addressLocality": "Frankfurt am Main",
                                "addressCountry": "DE"}},
    "description": "<p>Python, RAG und LLM-Anwendungen. Docker und SQL. "
                   "Berufserfahrung ist keine Voraussetzung.</p>" * 3,
    "validThrough": "2027-12-31", "url": "https://acme.example/karriere/ai-engineer",
}


def ld_page(payload) -> str:
    return ('<html><head><script type="application/ld+json">'
            + json.dumps(payload) + "</script></head><body>x</body></html>")


def brave_envelope(urls):
    return {"query": {"original": "x"},
            "web": {"results": [{"url": url, "title": "t", "description": "d"}
                                for url in urls]}}


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
            raise ValueError("no json")
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
        return Response(error="404")


def make_source(routes, **kwargs):
    queries = build_queries(profile(), CITIES, 2, today=date(2026, 11, 21))
    return BraveSearchSource(
        "unit-test-key", session=Session(routes), attempts=1, queries=queries,
        cities=CITIES, limiter=RateLimiter(min_interval=0.0),
        robots=RobotsGate("test-agent", opener=lambda url: "User-agent: *\nAllow: /"),
        **kwargs)


def test_brave_verifies_a_lead_against_the_employers_own_page():
    source = make_source({
        "api.search.brave.com": Response(brave_envelope(
            ["https://acme.example/karriere/ai-engineer"])),
        "acme.example/karriere": Response(text=ld_page(JOB_LD)),
    })
    found = source.discover(city="Frankfurt am Main", size=10)
    assert len(found) == 1
    job = found[0]
    # Everything authoritative comes from the employer's structured data,
    # never from the search snippet.
    assert job.company == "ACME GmbH"
    assert job.role == "AI Engineer (m/w/d)"
    assert job.location.startswith("Frankfurt am Main")
    assert "RAG" in source.describe(job)
    assert job.track, "the posting keeps the track of the query that found it"


def test_brave_sends_the_key_in_a_header_and_never_in_the_url():
    source = make_source({
        "api.search.brave.com": Response(brave_envelope([])),
    })
    source.discover(city="Frankfurt am Main", size=5)
    url, kwargs = source.session.calls[0]
    assert "unit-test-key" not in url
    assert kwargs["headers"]["X-Subscription-Token"] == "unit-test-key"
    assert kwargs["params"]["country"] == "de"


def test_brave_drops_aggregators_and_prefers_employer_pages():
    source = make_source({
        "api.search.brave.com": Response(brave_envelope([
            "https://de.indeed.com/viewjob?jk=1",
            "https://www.stepstone.de/stelle/1",
            "https://acme.example/karriere/ai-engineer",
        ])),
        "acme.example/karriere": Response(text=ld_page(JOB_LD)),
    })
    found = source.discover(city="Frankfurt am Main", size=10)
    assert [job.company for job in found] == ["ACME GmbH"]
    assert source.aggregators_skipped == 2
    fetched = [url for url, _ in source.session.calls if "brave" not in url]
    assert not any("indeed" in url or "stepstone" in url for url in fetched)


def test_brave_drops_a_lead_with_no_structured_job_data():
    """A search hit is a lead. Without JobPosting data, inventing one is worse."""
    source = make_source({
        "api.search.brave.com": Response(brave_envelope(
            ["https://acme.example/karriere/overview"])),
        "acme.example/karriere": Response(text="<html><body>Join us!</body></html>"),
    })
    assert source.discover(city="Frankfurt am Main", size=5) == []
    assert len(source.no_jobposting) == 1
    assert source.verified == 0


def test_brave_drops_a_posting_whose_deadline_has_passed():
    source = make_source({
        "api.search.brave.com": Response(brave_envelope(
            ["https://acme.example/karriere/ai-engineer"])),
        "acme.example/karriere": Response(
            text=ld_page({**JOB_LD, "validThrough": "2020-01-01"})),
    })
    assert source.discover(city="Frankfurt am Main", size=5) == []
    assert source.expired == 1


def test_brave_drops_a_posting_outside_the_configured_cities():
    source = make_source({
        "api.search.brave.com": Response(brave_envelope(
            ["https://acme.example/karriere/ai-engineer"])),
        "acme.example/karriere": Response(text=ld_page({
            **JOB_LD, "jobLocation": {"address": {"addressLocality": "Hamburg"}}})),
    })
    assert source.discover(city="Frankfurt am Main", size=5) == []


def test_brave_respects_robots_before_fetching_an_employer_page():
    blocked = RobotsGate("test-agent",
                         opener=lambda url: "User-agent: *\nDisallow: /karriere/")
    queries = build_queries(profile(), CITIES, 1, today=date(2026, 11, 21))
    source = BraveSearchSource(
        "unit-test-key", attempts=1, queries=queries, cities=CITIES,
        limiter=RateLimiter(min_interval=0.0), robots=blocked,
        session=Session({
            "api.search.brave.com": Response(brave_envelope(
                ["https://acme.example/karriere/ai-engineer"])),
            "acme.example/karriere": Response(text=ld_page(JOB_LD))}))
    assert source.discover(city="Frankfurt am Main", size=5) == []
    assert len(source.robots_blocked) == 1
    assert not any("karriere" in url for url, _ in source.session.calls
                   if "brave" not in url)


def test_brave_budgets_cap_api_calls_and_page_fetches():
    many = [f"https://acme{index}.example/karriere/role" for index in range(20)]
    source = make_source(
        {"api.search.brave.com": Response(brave_envelope(many))},
        max_queries=1, max_verifications=3)
    source.discover(city="Frankfurt am Main", size=50)
    assert source.api_calls == 1, "max_queries caps the metered calls"
    assert source.verifications == 3, "max_verifications caps page fetches"


def test_brave_is_single_shot_so_the_pipeline_cannot_burn_the_quota():
    assert BraveSearchSource.single_shot is True
    source = make_source({"api.search.brave.com": Response(brave_envelope([]))})
    source.discover(city="Frankfurt am Main", size=5)
    source.discover(city="Stuttgart", size=5)
    assert source.api_calls <= 2, "results are cached across discover() calls"


def test_brave_without_a_key_is_inactive_rather_than_failing():
    source = BraveSearchSource("", queries=build_queries(profile(), CITIES, 1))
    assert source.configured is False
    assert source.discover(city="Frankfurt am Main", size=5) == []
    assert any("BRAVE_SEARCH_API_KEY is not set" in error for error in source.errors)


def test_brave_empty_results_are_not_a_schema_error():
    source = make_source({"api.search.brave.com": Response({"query": {"original": "x"}})})
    assert source.discover(city="Frankfurt am Main", size=5) == []
    assert not any("schema changed" in error for error in source.errors)


def test_brave_schema_change_is_reported():
    source = make_source({"api.search.brave.com": Response({"unexpected": True})})
    source.discover(city="Frankfurt am Main", size=5)
    assert any("search failed" in error for error in source.errors)


def test_brave_report_exposes_what_the_api_cost():
    source = make_source({
        "api.search.brave.com": Response(brave_envelope([
            "https://de.indeed.com/viewjob?jk=1",
            "https://acme.example/karriere/ai-engineer"])),
        "acme.example/karriere": Response(text=ld_page(JOB_LD)),
    })
    source.discover(city="Frankfurt am Main", size=10)
    stats = source.report()
    assert stats["api_calls"] >= 1
    assert stats["aggregators_skipped"] == 1
    assert stats["postings_confirmed"] == 1
