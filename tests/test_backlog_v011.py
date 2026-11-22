"""Backlog processing, cached-assessment recovery, terminal statuses, dashboard.

Every test drives the real `GoogleSheetsRepository` against the stateful mock
sheet, so the range arithmetic and the row-protection guards under test are the
production code's own. Nothing here touches a live spreadsheet.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_agent.ai_matching import SYSTEM, minimal_llm_profile
from job_agent.cache import DescriptionCache, profile_fingerprint
from job_agent.candidate_profile import load_candidate_profile
from job_agent.daily_pipeline import run_daily
from job_agent.sheets import (ASSESSED_STATUSES, EXTENDED_HEADERS,
                              TERMINAL_STATUSES, GoogleSheetsRepository)
from job_agent.sources import NormalizedJob, location_matches
from stateful_sheet import StatefulService, StatefulValues

pytest.importorskip("langgraph")

ADVERT = ("Wir suchen Unterstuetzung fuer unsere Backend-Entwicklung in Frankfurt "
          "am Main. Java, Spring Boot, SQL, Docker und Linux. Kommerzielle "
          "Berufserfahrung ist keine Voraussetzung. Deutsch B2 genuegt. ") * 2
WERKSTUDENT = ("Als Werkstudent unterstuetzt du unser Team in Frankfurt am Main. "
               "Eine gueltige Studienbescheinigung ist Voraussetzung. ") * 3

ASSESSMENT = {
    "score": 85, "summary": "Strong trainee match on Linux and scripting evidence",
    "matched_skills": ["Linux", "Docker"], "transferable_skills": ["Process work"],
    "missing_skills": [], "mandatory_unmet": [], "unknown_requirements": [],
    "critical_gaps": [], "language": "German B2", "track": "it_consulting",
    "eligibility": "eligible",
}


def profile():
    return load_candidate_profile(Path("data/candidate_profile.json"))


def fingerprint():
    return profile_fingerprint(minimal_llm_profile(profile()), SYSTEM)


def found_row(job_id: str, role: str, url: str) -> list:
    return [job_id, "2026-11-20", "Computacenter AG & Co. oHG", role,
            "Frankfurt am Main", "Unknown", "BA Jobsuche (unofficial API)", url,
            "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", ""]


def ba_job(ref: str, role: str = "Trainee IT Consulting w/m/d") -> NormalizedJob:
    return NormalizedJob(
        source_key="ba", source_label="BA Jobsuche (unofficial API)",
        external_id=ref, company="Computacenter AG & Co. oHG", role=role,
        location="Frankfurt am Main", found_on="2026-11-20",
        job_url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}")


class Source:
    key = "ba"
    label = "BA Jobsuche (unofficial API)"

    def __init__(self, jobs=(), descriptions=None, fail=()):
        self.jobs = list(jobs)
        self.descriptions = descriptions or {}
        self.fail = set(fail)
        self.describe_calls = 0

    def discover(self, *, term="", city="", size=100):
        return [job for job in self.jobs if location_matches(job.location, (city,))]

    def describe(self, job):
        self.describe_calls += 1
        if job.external_id in self.fail:
            raise RuntimeError("detail unavailable")
        return self.descriptions.get(job.external_id, ADVERT)


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
    kwargs.setdefault("provider", None)
    return run_daily(repository=repo_for(values), **kwargs)


# --- 5. why the 85% assessments were never persisted ----------------------


def test_dry_run_scores_and_caches_but_writes_nothing(tmp_path):
    """The root cause: `--ai` without `--write` computes a score and drops it."""
    cache = DescriptionCache(tmp_path / "cache.json")
    values = StatefulValues(rows=[found_row(
        "ba:10001-1003967804-S", "Trainee IT Consulting w/m/d - Datacenter",
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003967804-S")])
    model = Model()
    report = run(values, sources=[Source([ba_job("10001-1003967804-S")])],
                 provider=model, write=False, cache=cache)

    assert report.llm_scored == 1 and model.calls == 1
    assert values.writes == [], "a dry run must not write"
    assert values.row_by_id("ba:10001-1003967804-S")["Match %"] == ""
    # The score survives locally, which is what makes recovery possible.
    assert cache.assessment_for("ba:10001-1003967804-S", ADVERT,
                                fingerprint()).assessment["score"] == 85
    assert cache.saved_to_sheet("ba:10001-1003967804-S") is False


def test_write_run_recovers_the_cached_assessment_without_a_model_call(tmp_path):
    """The fix: a later `--write` run persists it for free."""
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment("ba:10001-1003967804-S", ADVERT, fingerprint(),
                              ASSESSMENT)
    values = StatefulValues(rows=[found_row(
        "ba:10001-1003967804-S", "Trainee IT Consulting w/m/d - Datacenter",
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003967804-S")])
    model = Model()
    report = run(values, sources=[Source([])], provider=model, write=True,
                 cache=cache, max_matches=1)

    assert report.flushed == 1
    assert model.calls == 0, "recovery must cost no Gemini call"
    row = values.row_by_id("ba:10001-1003967804-S")
    assert row["Match %"] == 85
    assert row["Status"] == "Review"
    assert row["Approval"] == "Not reviewed"
    assert cache.saved_to_sheet("ba:10001-1003967804-S") is True
    assert any("RESTORED 85%" in line for line in report.preview)


def test_recovery_is_idempotent_and_cannot_double_write(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment("ba:10001-1003967804-S", ADVERT, fingerprint(),
                              ASSESSMENT)
    values = StatefulValues(rows=[found_row(
        "ba:10001-1003967804-S", "Trainee IT",
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003967804-S")])
    first = run(values, sources=[Source([])], provider=Model(), write=True, cache=cache)
    assert first.flushed == 1
    before = [list(row) for row in values.rows]
    second = run(values, sources=[Source([])], provider=Model(), write=True, cache=cache)
    assert second.flushed == 0
    assert values.rows == before


def test_recovery_skips_an_assessment_made_under_a_different_profile(tmp_path):
    """A stale score must be re-earned, not restored."""
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment("ba:10001-1003967804-S", ADVERT,
                              "fingerprint-from-an-older-profile", ASSESSMENT)
    values = StatefulValues(rows=[found_row(
        "ba:10001-1003967804-S", "Trainee IT",
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003967804-S")])
    report = run(values, sources=[Source([])], provider=Model(), write=True,
                 cache=cache, max_matches=1)
    assert report.flushed == 0


def test_recovery_never_touches_a_human_edited_row(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment("ba:10001-1003967804-S", ADVERT, fingerprint(),
                              ASSESSMENT)
    row = found_row("ba:10001-1003967804-S", "Trainee IT",
                    "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003967804-S")
    row[12], row[13] = "Approved", "Approved"
    values = StatefulValues(rows=[row])
    before = [list(item) for item in values.rows]
    report = run(values, sources=[Source([])], provider=Model(), write=True, cache=cache)
    assert report.flushed == 0
    assert values.rows == before


# --- 1 & 2. backlog first, drained progressively --------------------------


def backlog_sheet(count: int) -> StatefulValues:
    return StatefulValues(rows=[
        found_row(f"ba:9000{index}-S", f"Java Spring Developer {index}",
                  f"https://www.arbeitsagentur.de/jobsuche/jobdetail/9000{index}-S")
        for index in range(count)])


def test_saved_found_rows_are_scored_before_new_discoveries():
    values = backlog_sheet(4)
    seen = []

    class Recorder(Model):
        def complete(self, messages):
            payload = json.loads(messages[1]["content"].split("\n", 1)[1])
            seen.append(payload["job"]["title"])
            return super().complete(messages)

    report = run(values, sources=[Source([ba_job("77777-S", "Brand New Role")])],
                 provider=Recorder(), write=True, max_matches=2)
    assert len(seen) == 2
    assert all("Java Spring Developer" in title for title in seen)
    assert report.backlog_remaining == 4


def test_backlog_drains_across_consecutive_runs_within_budget():
    """38 unscored rows and a budget of 2 means 19 runs, not a stuck queue."""
    values = backlog_sheet(6)
    source = Source([])
    total_calls = 0
    for _ in range(3):
        model = Model()
        run(values, sources=[source], provider=model, write=True, max_matches=2)
        total_calls += model.calls
    assert total_calls == 6, "every run must make progress"
    scored = [row for row in values.rows if row[8] != ""]
    assert len(scored) == 6
    # Nothing is scored twice.
    assert all(row[12] == "Review" for row in scored)

    model = Model()
    final = run(values, sources=[source], provider=model, write=True, max_matches=2)
    assert model.calls == 0, "an empty backlog costs nothing"
    assert final.backlog_remaining == 0


def test_model_budget_is_respected_exactly():
    values = backlog_sheet(10)
    model = Model()
    report = run(values, sources=[Source([])], provider=model, write=True,
                 max_matches=3)
    assert model.calls == 3 and report.llm_attempted == 3
    assert len([row for row in values.rows if row[8] != ""]) == 3


# --- 3. terminal statuses -------------------------------------------------


def test_hard_ineligible_row_is_closed_not_left_as_found():
    values = StatefulValues(rows=[found_row(
        "ba:55555-S", "Werkstudent Softwareentwicklung",
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/55555-S")])
    model = Model()
    report = run(values, sources=[Source([ba_job("55555-S", "Backend Developer")],
                                         descriptions={"55555-S": WERKSTUDENT})],
                 provider=model, write=True)
    row = values.row_by_id("ba:55555-S")
    assert row["Status"] == "Ineligible"
    assert row["Match %"] == "", "no score may be invented"
    assert row["Approval"] == "Not reviewed"
    assert "pre-screen" in row["Critical gaps"]
    assert model.calls == 0
    assert report.closed_ineligible == 1


def test_repeatedly_unreadable_row_is_closed_after_the_attempt_limit(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json")
    url = "https://www.arbeitsagentur.de/jobsuche/jobdetail/66666-S"
    values = StatefulValues(rows=[found_row("ba:66666-S", "Java Developer", url)])
    source = Source([], fail={"66666-S"})
    for attempt in range(1, 4):
        report = run(values, sources=[source], provider=Model(), write=True,
                     cache=cache, max_detail_attempts=3)
        status = values.row_by_id("ba:66666-S")["Status"]
        if attempt < 3:
            assert status == "Found", f"attempt {attempt} must still retry"
            assert report.closed_unreadable == 0
    assert values.row_by_id("ba:66666-S")["Status"] == "Unreadable"
    assert values.row_by_id("ba:66666-S")["Match %"] == ""
    assert "expired" in values.row_by_id("ba:66666-S")["Critical gaps"]


def test_a_successful_fetch_resets_the_failure_count(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json")
    cache.record_attempt("ba:66666-S", succeeded=False)
    cache.record_attempt("ba:66666-S", succeeded=False)
    assert cache.attempts("ba:66666-S") == 2
    cache.record_attempt("ba:66666-S", succeeded=True)
    assert cache.attempts("ba:66666-S") == 0


def test_terminal_statuses_protect_a_human_edited_row():
    row = found_row("ba:55555-S", "Werkstudent",
                    "https://www.arbeitsagentur.de/jobsuche/jobdetail/55555-S")
    row[12], row[13] = "Approved", "Approved"
    values = StatefulValues(rows=[row])
    outcome = repo_for(values).record_terminal_status("ba:55555-S", "Ineligible", "x")
    assert "protected" in outcome
    assert values.row_by_id("ba:55555-S")["Status"] == "Approved"


def test_terminal_status_rejects_an_unknown_value():
    values = StatefulValues()
    with pytest.raises(ValueError, match="terminal status must be one of"):
        repo_for(values).record_terminal_status("ba:1", "Deleted", "x")
    assert set(TERMINAL_STATUSES).isdisjoint(ASSESSED_STATUSES)


# --- 6. Potential without overwriting humans ------------------------------


def test_potential_is_written_for_an_eligible_mid_band_score():
    values = StatefulValues(headers=list(EXTENDED_HEADERS),
                            rows=[found_row(
                                "ba:12345-S", "Java Developer",
                                "https://www.arbeitsagentur.de/jobsuche/jobdetail/12345-S")])
    run(values, sources=[Source([])], provider=Model({**ASSESSMENT, "score": 72}),
        write=True)
    row = values.row_by_id("ba:12345-S")
    assert row["Status"] == "Potential" and row["Match %"] == 72
    assert row["Eligibility"] == "eligible"
    assert row["Approval"] == "Not reviewed"


def test_reband_moves_legacy_skip_rows_into_potential():
    """Rows scored before the band existed were written as Skip."""
    rows = []
    for index, (score, status, approval, cv) in enumerate([
            (75, "Skip", "Not reviewed", ""),      # re-band
            (68, "Skip", "Not reviewed", ""),      # re-band
            (65, "Skip", "Not reviewed", ""),      # re-band
            (64, "Skip", "Not reviewed", ""),      # below the band
            (88, "Review", "Not reviewed", ""),    # already Review
            (75, "Skip", "Rejected", ""),          # human decision
            (75, "Skip", "Not reviewed", "cv.pdf"),  # human attached a CV
    ]):
        row = found_row(f"ba:7{index}-S", f"Role {index}",
                        f"https://www.arbeitsagentur.de/jobsuche/jobdetail/7{index}-S")
        row[8], row[12], row[13], row[14] = score, status, approval, cv
        rows.append(row)
    values = StatefulValues(rows=rows)
    repo = repo_for(values)

    preview = repo.reband_potential(write=False)
    assert [item["score"] for item in preview["eligible_rows"]] == [75, 68, 65]
    assert values.writes == [], "a dry run must not write"
    assert len(preview["protected"]) == 2

    applied = repo.reband_potential(write=True)
    assert applied["written"] == 3
    statuses = [dict(zip(EXTENDED_HEADERS, row))["Status"] for row in values.rows]
    assert statuses == ["Potential", "Potential", "Potential", "Skip", "Review",
                        "Skip", "Skip"]
    # Scores and every human-owned column are untouched.
    assert [row[8] for row in values.rows] == [75, 68, 65, 64, 88, 75, 75]
    assert values.row_by_id("ba:76-S")["CV file/link"] == "cv.pdf"


def test_reband_only_writes_the_status_column():
    row = found_row("ba:70-S", "Role",
                    "https://www.arbeitsagentur.de/jobsuche/jobdetail/70-S")
    row[8], row[12] = 70, "Skip"
    values = StatefulValues(rows=[row])
    repo_for(values).reband_potential(write=True)
    method, data = values.writes[0]
    assert method == "update"
    assert data["range"].endswith("!M2")
    assert data["valueInputOption"] == "RAW"


# --- 7. dashboard ---------------------------------------------------------


def test_dashboard_counts_every_status_band_and_error():
    rows = []
    plan = [(None, "Found"), (None, "Found"), (88, "Review"), (72, "Potential"),
            (45, "Skip"), (None, "Ineligible"), (None, "Unreadable")]
    for index, (score, status) in enumerate(plan):
        row = found_row(f"ba:8{index}-S", f"Role {index}",
                        f"https://www.arbeitsagentur.de/jobsuche/jobdetail/8{index}-S")
        row[8] = "" if score is None else score
        row[12] = status
        rows.append(row)
    summary = repo_for(StatefulValues(rows=rows)).status_summary()

    assert summary["total"] == 7
    assert summary["status"] == {"Found": 2, "Review": 1, "Potential": 1,
                                 "Skip": 1, "Ineligible": 1, "Unreadable": 1}
    assert summary["errors"] == 2, "Ineligible + Unreadable are processing errors"
    assert summary["unscored_found_backlog"] == 2
    assert summary["scored"] == 3
    assert summary["score_bands"] == {"review": 1, "potential": 1, "below": 1}
    assert summary["by_source"] == {"ba": 7}


def test_dashboard_is_read_only():
    values = StatefulValues(rows=[found_row(
        "ba:1-S", "Role", "https://www.arbeitsagentur.de/jobsuche/jobdetail/1-S")])
    repo_for(values).status_summary()
    assert values.writes == []


def test_dashboard_tolerates_a_legacy_19_column_sheet():
    values = StatefulValues(rows=[found_row(
        "ba:1-S", "Role", "https://www.arbeitsagentur.de/jobsuche/jobdetail/1-S")])
    summary = repo_for(values).status_summary()
    assert summary["eligibility"] == {}, "no Eligibility column yet"
    assert summary["status"] == {"Found": 1}


# --- 4. no repeat Gemini calls -------------------------------------------


def test_an_unchanged_job_is_never_sent_to_gemini_twice(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json")
    values = backlog_sheet(1)
    source = Source([])
    model = Model()
    run(values, sources=[source], provider=model, write=True, cache=cache)
    assert model.calls == 1
    # Row is now scored, so it leaves the backlog entirely.
    again = run(values, sources=[source], provider=model, write=True, cache=cache)
    assert model.calls == 1 and again.llm_attempted == 0
