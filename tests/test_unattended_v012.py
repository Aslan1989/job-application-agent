"""Unattended-operation guarantees: idempotency, no duplicate writes, resilience.

These are the properties a scheduled run depends on. Everything is driven
through the real `GoogleSheetsRepository` against the stateful mock sheet, so
the row guards and range arithmetic under test are the production code's own.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from job_agent.ai_matching import SYSTEM, minimal_llm_profile
from job_agent.cache import DescriptionCache, profile_fingerprint
from job_agent.candidate_profile import load_candidate_profile
from job_agent.daily_pipeline import _assess_with_retries, run_daily
from job_agent.providers import ProviderError, classify_exception
from job_agent.sheets import EXTENDED_HEADERS, GoogleSheetsRepository
from job_agent.sources import NormalizedJob, location_matches
from stateful_sheet import StatefulService, StatefulValues

pytest.importorskip("langgraph")

ADVERT = ("Wir suchen Unterstuetzung fuer unsere Backend-Entwicklung in Frankfurt "
          "am Main. Java, Spring Boot, SQL, Docker und Linux. Kommerzielle "
          "Berufserfahrung ist keine Voraussetzung. Deutsch B2 genuegt. ") * 2

ASSESSMENT = {
    "score": 85, "summary": "Strong trainee match", "matched_skills": ["Linux"],
    "transferable_skills": [], "missing_skills": [], "mandatory_unmet": [],
    "unknown_requirements": [], "critical_gaps": [], "language": "German B2",
    "track": "it_consulting", "eligibility": "eligible",
}
JOB_ID = "ba:10001-1003967804-S"
JOB_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003967804-S"


def profile():
    return load_candidate_profile(Path("data/candidate_profile.json"))


def fingerprint():
    return profile_fingerprint(minimal_llm_profile(profile()), SYSTEM)


def found_row(job_id=JOB_ID, role="Trainee IT Consulting", url=JOB_URL) -> list:
    return [job_id, "2026-11-20", "Computacenter AG & Co. oHG", role,
            "Frankfurt am Main", "Unknown", "BA Jobsuche (unofficial API)", url,
            "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", ""]


def ba_job(ref="10001-1003967804-S", role="Trainee IT Consulting") -> NormalizedJob:
    return NormalizedJob(
        source_key="ba", source_label="BA Jobsuche (unofficial API)",
        external_id=ref, company="Computacenter AG & Co. oHG", role=role,
        location="Frankfurt am Main", found_on="2026-11-20",
        job_url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}")


class Source:
    key = "ba"
    label = "BA Jobsuche (unofficial API)"

    def __init__(self, jobs=()):
        self.jobs = list(jobs)

    def discover(self, *, term="", city="", size=100):
        return [job for job in self.jobs if location_matches(job.location, (city,))]

    def describe(self, job):
        return ADVERT


class Model:
    def __init__(self, assessment=None):
        self.assessment = assessment or ASSESSMENT
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return dict(self.assessment)


def repo_for(values):
    return GoogleSheetsRepository(StatefulService(values), "MOCK-SHEET")


def run(values, **kwargs):
    kwargs.setdefault("max_searches", 3)
    kwargs.setdefault("profile", profile())
    kwargs.setdefault("llm_sleep", lambda _: None)
    return run_daily(repository=repo_for(values), **kwargs)


# --- the reported RowProtected-after-recovery bug -------------------------


def test_recovery_does_not_trigger_a_second_write_on_the_same_row(tmp_path):
    """The reported symptom: flushed rows re-entered the queue and were rewritten.

    `scored_now` was recomputed from `pending_assessments` *after* `mark_saved`
    had already removed those ids, so the exclusion set was always empty.
    """
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment(JOB_ID, ADVERT, fingerprint(), ASSESSMENT)
    values = StatefulValues(rows=[found_row()])
    model = Model()

    report = run(values, sources=[Source()], provider=model, write=True,
                 cache=cache, max_matches=3)

    assert report.flushed == 1
    assert model.calls == 0, "recovery must cost no model call"
    assert len(values.writes) == 1, "exactly one write for one row"
    assert report.errors == [], f"no warnings expected, got {report.errors}"
    assert not any("RowProtected" in error for error in report.errors)
    row = values.row_by_id(JOB_ID)
    assert row["Match %"] == 85 and row["Status"] == "Review"


def test_a_genuinely_protected_row_is_counted_not_warned_about():
    """A human decision is a correct outcome, not a processing error."""
    row = found_row()
    row[12], row[13] = "Approved", "Approved"
    values = StatefulValues(rows=[row])
    before = [list(item) for item in values.rows]

    report = run(values, sources=[Source([ba_job()])], provider=Model(), write=True)

    assert values.rows == before, "a human decision is never overwritten"
    assert not any("RowProtected" in error for error in report.errors)


def test_three_consecutive_runs_are_idempotent(tmp_path):
    """What a scheduled daily run actually does: the same thing, repeatedly."""
    cache = DescriptionCache(tmp_path / "cache.json")
    values = StatefulValues(headers=list(EXTENDED_HEADERS))
    source = Source([ba_job()])
    model = Model()

    first = run(values, sources=[source], provider=model, write=True, cache=cache,
                max_matches=3)
    assert first.new == 1 and first.saved_scores == 1
    assert model.calls == 1
    snapshot = [list(row) for row in values.rows]
    write_count = len(values.writes)

    for index in range(2, 4):
        report = run(values, sources=[source], provider=model, write=True,
                     cache=cache, max_matches=3)
        assert values.rows == snapshot, f"run {index} changed the sheet"
        assert model.calls == 1, f"run {index} called the model again"
        assert report.flushed == 0 and report.saved_scores == 0
        assert report.errors == [], f"run {index} produced {report.errors}"
    assert len(values.writes) == write_count, "no further writes at all"


def test_recovery_then_normal_run_writes_each_row_exactly_once(tmp_path):
    """A mixed backlog: one row cached, one needing a fresh assessment."""
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment(JOB_ID, ADVERT, fingerprint(), ASSESSMENT)
    second = found_row("ba:20002-S", "Java Developer",
                       "https://www.arbeitsagentur.de/jobsuche/jobdetail/20002-S")
    values = StatefulValues(rows=[found_row(), second])
    model = Model()

    report = run(values, sources=[Source()], provider=model, write=True,
                 cache=cache, max_matches=3)

    assert report.flushed == 1
    assert model.calls == 1, "only the uncached row needs the model"
    assert report.saved_scores == 1
    assert len(values.writes) == 2, "one write per row, no duplicates"
    assert report.errors == []
    assert values.row_by_id(JOB_ID)["Match %"] == 85
    assert values.row_by_id("ba:20002-S")["Match %"] == 85


def test_recovery_skips_a_row_scored_by_an_earlier_run(tmp_path):
    """A stale cache marker must not cause a write or a warning."""
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment(JOB_ID, ADVERT, fingerprint(), ASSESSMENT)
    row = found_row()
    row[8], row[12] = 85, "Review"  # already scored by a previous run
    values = StatefulValues(rows=[row])

    report = run(values, sources=[Source()], provider=Model(), write=True,
                 cache=cache, max_matches=3)

    assert report.flushed == 0
    assert report.recovery_skipped == 0, "scored rows never enter the backlog"
    assert values.writes == []
    assert report.errors == []


# --- provider failure classification --------------------------------------


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError("upstream detail that must not leak",
                              response=response)


@pytest.mark.parametrize("status,retryable,permanent", [
    (429, True, False), (500, True, False), (502, True, False),
    (503, True, False), (408, True, False),
    (400, False, True), (401, False, True), (403, False, True), (404, False, True),
])
def test_provider_errors_are_classified_by_status(status, retryable, permanent):
    error = classify_exception(http_error(status), provider="Gemini")
    assert error.status == status
    assert error.retryable is retryable
    assert error.permanent is permanent


def test_transport_faults_are_retryable_and_bad_payloads_are_not():
    assert classify_exception(requests.ConnectTimeout(), provider="G").retryable
    assert classify_exception(requests.ConnectionError(), provider="G").retryable
    assert not classify_exception(json.JSONDecodeError("x", "{}", 0),
                                  provider="G").retryable
    assert not classify_exception(KeyError("candidates"), provider="G").retryable


def test_provider_error_never_carries_the_body_or_the_key():
    error = classify_exception(http_error(403), provider="Gemini")
    rendered = str(error)
    assert "upstream detail that must not leak" not in rendered
    assert "HTTP 403" in rendered and "Gemini" in rendered


def test_gemini_provider_raises_a_classified_error(monkeypatch):
    from job_agent.ai_matching import GeminiProvider

    def rate_limited(url, **kwargs):
        raise http_error(429)

    monkeypatch.setattr("job_agent.ai_matching.requests.post", rate_limited)
    with pytest.raises(ProviderError) as caught:
        GeminiProvider(api_key="unit-test-key").complete([{"role": "user", "content": "x"}])
    assert caught.value.status == 429 and caught.value.retryable
    assert "unit-test-key" not in str(caught.value)


# --- bounded retries ------------------------------------------------------


class Graph:
    """Minimal matcher stand-in: a scripted sequence of outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, payload):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return {"assessment": dict(outcome)}


def test_a_transient_failure_is_retried_then_succeeds():
    slept = []
    graph = Graph([http_error(429), ASSESSMENT])
    assessment, failure = _assess_with_retries(graph, {"job": {}}, attempts=3,
                                               sleep=slept.append)
    assert assessment["score"] == 85
    assert graph.calls == 2
    assert len(slept) == 1 and 0 <= slept[0] <= 1.0


def test_retries_are_bounded_and_then_give_up():
    slept = []
    graph = Graph([http_error(503)])
    assessment, failure = _assess_with_retries(graph, {"job": {}}, attempts=3,
                                               sleep=slept.append)
    assert assessment is None
    assert graph.calls == 3, "exactly the configured number of attempts"
    assert len(slept) == 2
    assert slept[1] <= 2.0, "backoff grows but stays bounded"
    assert failure.status == 503


def test_a_permanent_rejection_is_not_retried():
    slept = []
    graph = Graph([http_error(403)])
    assessment, failure = _assess_with_retries(graph, {"job": {}}, attempts=4,
                                               sleep=slept.append)
    assert assessment is None and graph.calls == 1
    assert slept == [], "retrying a rejected request only wastes quota"
    assert failure.permanent


def test_a_malformed_response_is_not_retried():
    graph = Graph([{"not": "an assessment"}])
    graph.invoke = lambda payload: {"assessment": "not a dict"}  # type: ignore[assignment]
    assessment, failure = _assess_with_retries(graph, {"job": {}}, attempts=3,
                                               sleep=lambda _: None)
    assert assessment is None
    assert failure.kind == "invalid_response"


def test_keyboard_interrupt_is_never_swallowed():
    graph = Graph([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        _assess_with_retries(graph, {"job": {}}, attempts=3, sleep=lambda _: None)


def test_attempt_count_is_validated():
    with pytest.raises(ValueError, match="llm_attempts"):
        _assess_with_retries(Graph([ASSESSMENT]), {"job": {}}, attempts=0)


# --- safe continuation through the pipeline -------------------------------


def backlog(count: int) -> StatefulValues:
    return StatefulValues(rows=[
        found_row(f"ba:3000{index}-S", f"Java Spring Developer {index}",
                  f"https://www.arbeitsagentur.de/jobsuche/jobdetail/3000{index}-S")
        for index in range(count)])


def test_a_single_failure_does_not_stop_the_queue():
    values = backlog(4)

    class FailsOnce(Model):
        def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise http_error(429)
            return dict(self.assessment)

    model = FailsOnce()
    report = run(values, sources=[Source()], provider=model, write=True,
                 max_matches=4, llm_attempts=1)

    assert report.llm_failed == 1
    assert report.llm_scored == 3, "the rest of the queue is still processed"
    assert report.saved_scores == 3
    assert not report.stop_reason
    assert any("[matching]" in error and "retried" in error
               for error in report.errors), "the diagnostic must say what happens next"


def test_a_permanent_rejection_stops_the_stage_with_a_clear_reason():
    values = backlog(5)

    class Rejected(Model):
        def complete(self, messages):
            self.calls += 1
            raise http_error(403)

    model = Rejected()
    report = run(values, sources=[Source()], provider=model, write=True,
                 max_matches=5, llm_attempts=2)

    assert model.calls == 1, "a rejected request is not retried or repeated"
    assert report.llm_failed == 1
    assert "GEMINI_API_KEY" in report.stop_reason
    assert report.saved_scores == 0


def test_consecutive_failures_trip_the_circuit_breaker():
    values = backlog(8)

    class Down(Model):
        def complete(self, messages):
            self.calls += 1
            raise http_error(503)

    model = Down()
    report = run(values, sources=[Source()], provider=model, write=True,
                 max_matches=8, llm_attempts=1, max_consecutive_failures=3)

    assert report.llm_failed == 3
    assert "consecutive provider failures" in report.stop_reason
    assert model.calls == 3, "the budget is protected once it is clearly down"


def test_a_failure_leaves_the_row_untouched_for_the_next_run():
    values = backlog(1)

    class Down(Model):
        def complete(self, messages):
            self.calls += 1
            raise http_error(503)

    run(values, sources=[Source()], provider=Down(), write=True,
        max_matches=1, llm_attempts=1)
    row = values.row_by_id("ba:30000-S")
    assert row["Status"] == "Found" and row["Match %"] == ""
    assert row["Approval"] == "Not reviewed"

    # The next run picks it up and succeeds.
    model = Model()
    report = run(values, sources=[Source()], provider=model, write=True, max_matches=1)
    assert report.saved_scores == 1
    assert values.row_by_id("ba:30000-S")["Status"] == "Review"


def test_recovery_still_runs_when_the_provider_is_down(tmp_path):
    """Cached scores reach the sheet even if Gemini is unavailable today."""
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment(JOB_ID, ADVERT, fingerprint(), ASSESSMENT)
    values = StatefulValues(rows=[found_row(), found_row(
        "ba:40001-S", "Java Developer",
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/40001-S")])

    class Down(Model):
        def complete(self, messages):
            self.calls += 1
            raise http_error(503)

    report = run(values, sources=[Source()], provider=Down(), write=True,
                 cache=cache, max_matches=2, llm_attempts=1,
                 max_consecutive_failures=1)

    assert report.flushed == 1, "recovery happens before any model call"
    assert values.row_by_id(JOB_ID)["Match %"] == 85
    assert values.row_by_id("ba:40001-S")["Match %"] == ""


def test_human_decisions_survive_a_failing_run():
    rows = []
    for index, (status, approval, score) in enumerate([
            ("Approved", "Approved", 91), ("Skip", "Rejected", 42),
            ("Found", "Not reviewed", "")]):
        row = found_row(f"ba:5000{index}-S", f"Role {index}",
                        f"https://www.arbeitsagentur.de/jobsuche/jobdetail/5000{index}-S")
        row[8], row[12], row[13] = score, status, approval
        row[14], row[16] = "cv.pdf", "2026-11-19"
        rows.append(row)
    values = StatefulValues(rows=rows)
    protected = [list(row) for row in values.rows[:2]]

    class Down(Model):
        def complete(self, messages):
            self.calls += 1
            raise http_error(503)

    run(values, sources=[Source()], provider=Down(), write=True,
        max_matches=3, llm_attempts=1)
    assert [list(row) for row in values.rows[:2]] == protected
