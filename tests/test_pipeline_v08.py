"""End-to-end v0.8 pipeline behaviour, with every external service mocked.

Covers: multi-source discovery, cross-source dedup against the live sheet,
repeated daily runs, cache reuse and invalidation, Gemini matching, error
recovery, and the guarantee that human-edited rows are never overwritten.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataclasses import replace

from job_agent.cache import DescriptionCache
from job_agent.candidate_profile import load_candidate_profile
from job_agent.daily_pipeline import resumable_from_sheet, run_daily
from job_agent.sheets import EXTENDED_HEADERS, GoogleSheetsRepository
from job_agent.sources import NormalizedJob, location_matches
from stateful_sheet import StatefulService, StatefulValues

pytest.importorskip("langgraph")

GOOD_DESCRIPTION = (
    "Wir suchen Unterstuetzung fuer unsere Backend-Entwicklung in Frankfurt am Main. "
    "Du arbeitest mit Java, Spring Boot, SQL, Docker und Linux an REST-Schnittstellen. "
    "Eine abgeschlossene Ausbildung oder eigene Projekte sind ausreichend; kommerzielle "
    "Berufserfahrung als Entwickler ist keine Voraussetzung. Deutsch B2 genuegt. "
) * 2

WERKSTUDENT_DESCRIPTION = (
    "Als Werkstudent unterstuetzt du unser Team in Frankfurt am Main bei der "
    "Entwicklung mit Java und Docker. Eine gueltige Studienbescheinigung ist "
    "Voraussetzung fuer diese Taetigkeit in unserem Unternehmen. "
) * 2

ASSESSMENT = {
    "score": 86, "summary": "Spring Boot project evidence; no commercial employment",
    "matched_skills": ["Java 17", "Docker"], "transferable_skills": ["Process optimisation"],
    "missing_skills": ["Kubernetes"], "mandatory_unmet": [],
    "unknown_requirements": ["Exact German degree title"], "critical_gaps": [],
    "language": "German B2", "track": "backend_java", "eligibility": "eligible",
}


def profile():
    return load_candidate_profile(Path("data/candidate_profile.json"))


def make_job(source_key="ba", external_id="10001-12345678-S", role="Backend Developer",
             city="Frankfurt am Main", url=None) -> NormalizedJob:
    default_url = {
        "ba": f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{external_id}",
        "greenhouse": f"https://boards.greenhouse.io/acme/jobs/{external_id}",
        "lever": f"https://jobs.lever.co/acme/{external_id}",
        "ashby": f"https://jobs.ashbyhq.com/acme/{external_id}",
        "career_page": f"https://acme.example/jobs/{external_id}",
    }[source_key]
    return NormalizedJob(source_key=source_key, source_label=f"{source_key} source",
                         external_id=external_id, company="ACME GmbH", role=role,
                         location=city, job_url=url or default_url, found_on="2026-11-21")


class FakeSource:
    """Deterministic source double. Counts calls so budgets can be asserted."""

    def __init__(self, key, jobs, descriptions=None, *, fail_discover=False,
                 fail_describe=()):
        self.key = key
        self.label = f"{key} source"
        self.jobs = jobs
        self.descriptions = descriptions or {}
        self.fail_discover = fail_discover
        self.fail_describe = set(fail_describe)
        self.discover_calls = 0
        self.describe_calls = 0

    def discover(self, *, term, city, size):
        self.discover_calls += 1
        if self.fail_discover:
            raise RuntimeError("source unavailable")
        # Mirror the real adapters, which screen loosely and let the pipeline
        # apply the strict city rule.
        return [job for job in self.jobs
                if location_matches(job.location, (city,))][:size]

    def describe(self, job):
        self.describe_calls += 1
        if job.external_id in self.fail_describe:
            raise RuntimeError("detail unavailable")
        return self.descriptions.get(job.external_id, GOOD_DESCRIPTION)


class FakeModel:
    def __init__(self, assessment=None, fail_after=None):
        self.assessment = assessment or ASSESSMENT
        self.fail_after = fail_after
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("429 rate limited")
        return dict(self.assessment)


def repo_for(values: StatefulValues) -> GoogleSheetsRepository:
    return GoogleSheetsRepository(StatefulService(values), "test-sheet")


def run(values, *, sources, provider=None, write=False, cache=None, **kwargs):
    return run_daily(repository=repo_for(values), sources=sources, profile=profile(),
                     provider=provider, write=write, cache=cache,
                     max_searches=kwargs.pop("max_searches", 3), **kwargs)


# --- discovery and deduplication ------------------------------------------


def test_board_style_location_reaches_the_sheet():
    """Regression: a Greenhouse 'Frankfurt, Germany' must not be filtered out."""
    gh = FakeSource("greenhouse", [make_job("greenhouse", "4567", role="AI Engineer",
                                            city="Frankfurt, Germany")])
    values = StatefulValues()
    report = run(values, sources=[gh], write=True)
    assert report.new == 1
    assert values.job_ids == ["greenhouse:4567"]


def test_multi_source_discovery_appends_unscored_found_rows():
    ba = FakeSource("ba", [make_job()])
    gh = FakeSource("greenhouse", [make_job("greenhouse", "4567", role="AI Engineer")])
    values = StatefulValues()
    report = run(values, sources=[ba, gh], write=True)

    assert report.new == 2
    assert report.by_source == {"ba": 1, "greenhouse": 1}
    assert sorted(values.job_ids) == ["ba:10001-12345678-S", "greenhouse:4567"]
    for row in values.rows:
        record = dict(zip(EXTENDED_HEADERS, row))
        assert record["Status"] == "Found"
        assert record["Approval"] == "Not reviewed"
        assert record["Match %"] == "", "a discovery must never carry a score"
        assert record["Applied on"] == ""


def test_same_posting_on_two_sources_creates_one_row():
    shared_url = "https://boards.greenhouse.io/acme/jobs/4567"
    ba = FakeSource("ba", [make_job(url=shared_url)])
    gh = FakeSource("greenhouse", [make_job("greenhouse", "4567", url=shared_url)])
    values = StatefulValues()
    report = run(values, sources=[ba, gh], write=True)
    assert report.new == 1
    assert report.duplicates >= 1
    assert len(values.rows) == 1


def test_discovery_skips_postings_already_in_the_sheet():
    existing = ["ba:10001-12345678-S"] + [""] * 18
    values = StatefulValues(rows=[existing])
    report = run(values, sources=[FakeSource("ba", [make_job()])], write=True)
    assert report.new == 0
    assert len(values.rows) == 1
    assert not values.writes


def test_dry_run_never_touches_the_sheet():
    values = StatefulValues()
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=FakeModel(), write=False)
    assert report.new == 1 and report.llm_scored == 1
    assert values.writes == []
    assert values.rows == []


# --- write protection ------------------------------------------------------


@pytest.mark.parametrize("status,approval,score", [
    ("Review", "Not reviewed", ""),
    ("Approved", "Approved", 91),
    ("Found", "Rejected", ""),
    ("Skip", "Not reviewed", 42),
])
def test_human_edited_rows_are_never_overwritten(status, approval, score):
    row = ["ba:10001-12345678-S", "2026-11-20", "ACME GmbH", "Backend Developer",
           "Frankfurt am Main", "Unknown", "BA",
           "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-12345678-S",
           score, "", "", "Unknown", status, approval,
           "cv.pdf", "letter.pdf", "2026-11-20", "2026-12-01", "my own note"]
    values = StatefulValues(rows=[row])
    before = [list(r) for r in values.rows]
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=FakeModel(), write=True)
    assert report.new == 0
    assert report.pending == 0, "a human-touched row must not re-enter the queue"
    assert values.rows == before
    assert values.writes == []


def test_only_found_unreviewed_unscored_rows_get_a_score():
    row = ["ba:10001-12345678-S", "2026-11-20", "ACME GmbH", "Backend Developer",
           "Frankfurt am Main", "Unknown", "BA",
           "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-12345678-S",
           "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", ""]
    values = StatefulValues(rows=[row])
    model = FakeModel()
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=model, write=True)

    assert report.pending == 1 and report.saved_scores == 1
    saved = values.row_by_id("ba:10001-12345678-S")
    assert saved["Match %"] == 86
    assert saved["Status"] == "Review"
    assert saved["Approval"] == "Not reviewed", "approval stays the human's call"
    assert saved["Applied on"] == "" and saved["Notes"] == ""
    assert "Java 17" in saved["Match explanation"]
    assert "Transferable" in saved["Match explanation"]
    assert "To verify" in saved["Critical gaps"]


def test_eligibility_column_is_populated_when_the_sheet_has_one():
    values = StatefulValues(headers=list(EXTENDED_HEADERS))
    run(values, sources=[FakeSource("ba", [make_job()])],
        provider=FakeModel(), write=True)
    saved = values.row_by_id("ba:10001-12345678-S")
    assert saved["Eligibility"] == "eligible"


def test_legacy_19_column_sheet_still_works():
    values = StatefulValues()  # 19 columns, no Eligibility
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=FakeModel(), write=True)
    assert report.saved_scores == 1
    assert len(values.rows[0]) == 19
    assert values.row_by_id("ba:10001-12345678-S")["Status"] == "Review"


def test_schema_migration_is_additive_and_idempotent():
    values = StatefulValues()
    repo = repo_for(values)
    assert "DRY RUN" in repo.migrate_schema(write=False)
    assert values.writes == []
    assert "added column T" in repo.migrate_schema(write=True)
    assert values.headers[19] == "Eligibility"
    assert values.headers[:19] == list(EXTENDED_HEADERS[:19]), "A:S untouched"
    assert "no change needed" in repo_for(values).migrate_schema(write=True)


# --- repeated runs and caching --------------------------------------------


def test_repeated_daily_runs_neither_duplicate_nor_rescore(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json")
    values = StatefulValues()
    source = FakeSource("ba", [make_job()])
    model = FakeModel()

    first = run(values, sources=[source], provider=model, write=True, cache=cache)
    assert (first.new, first.llm_scored, first.saved_scores) == (1, 1, 1)
    assert model.calls == 1
    rows_after_first = [list(row) for row in values.rows]

    second = run(values, sources=[source], provider=model, write=True, cache=cache)
    assert (second.new, second.pending, second.llm_attempted) == (0, 0, 0)
    assert model.calls == 1, "a scored row must never be sent to the model again"
    assert values.rows == rows_after_first


def test_unchanged_advert_is_reread_but_not_rescored(tmp_path):
    """The advert is cheap to re-read; the model call is what must not repeat."""
    cache = DescriptionCache(tmp_path / "cache.json")
    source = FakeSource("ba", [make_job()])
    model = FakeModel()

    first = run(StatefulValues(), sources=[source], provider=model, cache=cache)
    assert first.llm_attempted == 1 and first.cache_hits == 0
    assert (tmp_path / "cache.json").exists(), "the pipeline persists the cache"

    reloaded = DescriptionCache.load(tmp_path / "cache.json")
    second = run(StatefulValues(), sources=[source], provider=model, cache=reloaded)
    assert second.cache_hits == 1
    assert second.llm_attempted == 0
    assert model.calls == 1
    assert second.llm_scored == 1, "a cached assessment still produces a decision"
    assert source.describe_calls == 2, "text re-read to detect an edited advert"


def test_cached_description_rescues_a_posting_the_source_cannot_redescribe(tmp_path):
    """A board source has no memory of yesterday's posting; the cache does."""
    cache = DescriptionCache(tmp_path / "cache.json")
    job = make_job()
    cache.remember_description(job.job_id, GOOD_DESCRIPTION)
    row = [job.job_id, "2026-11-20", "ACME GmbH", "Backend Developer",
           "Frankfurt am Main", "Unknown", "BA", job.job_url,
           "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", ""]
    values = StatefulValues(rows=[row])
    silent = FakeSource("ba", [], descriptions={job.external_id: ""})
    report = run(values, sources=[silent], provider=FakeModel(), write=True,
                 cache=cache)
    assert report.description_fallbacks == 1
    assert report.llm_scored == 1
    assert values.row_by_id(job.job_id)["Status"] == "Review"


def test_changed_description_invalidates_the_cached_score(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json")
    job = make_job()
    model = FakeModel()
    run(StatefulValues(), sources=[FakeSource("ba", [job])], provider=model, cache=cache)
    assert model.calls == 1

    changed = FakeSource("ba", [job], descriptions={
        job.external_id: GOOD_DESCRIPTION + " Zusaetzlich suchen wir Kubernetes-Kenntnisse."})
    report = run(StatefulValues(), sources=[changed], provider=model, cache=cache)
    assert report.cache_hits == 0
    assert model.calls == 2, "a rewritten advert must be re-assessed"


def test_prompt_or_profile_change_invalidates_the_whole_cache(tmp_path):
    from job_agent.cache import profile_fingerprint

    base = profile_fingerprint({"skills": ["php"]}, "SYSTEM A")
    assert base != profile_fingerprint({"skills": ["php", "java"]}, "SYSTEM A")
    assert base != profile_fingerprint({"skills": ["php"]}, "SYSTEM B")
    assert base == profile_fingerprint({"skills": ["php"]}, "SYSTEM A")

    cache = DescriptionCache(tmp_path / "cache.json")
    cache.remember_assessment("ba:1", GOOD_DESCRIPTION, base, ASSESSMENT)
    assert cache.assessment_for("ba:1", GOOD_DESCRIPTION, base) is not None
    assert cache.assessment_for("ba:1", GOOD_DESCRIPTION, "other-fingerprint") is None


def test_corrupt_cache_file_does_not_break_a_run(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{not json", encoding="utf-8")
    cache = DescriptionCache.load(path)
    assert cache.entries == {}
    report = run(StatefulValues(), sources=[FakeSource("ba", [make_job()])],
                 provider=FakeModel(), cache=cache)
    assert report.llm_scored == 1


# --- resume ----------------------------------------------------------------


def test_resume_picks_up_unscored_rows_absent_from_todays_search():
    """Search terms rotate daily, so yesterday's find must survive."""
    row = ["ba:99999999-X", "2026-11-20", "ACME GmbH", "Python Developer",
           "Stuttgart", "Unknown", "BA",
           "https://www.arbeitsagentur.de/jobsuche/jobdetail/99999999-X",
           "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", ""]
    values = StatefulValues(rows=[row])
    # Today's search returns nothing at all.
    report = run(values, sources=[FakeSource("ba", [])], provider=FakeModel(), write=True)
    assert report.pending == 1
    assert report.saved_scores == 1
    assert values.row_by_id("ba:99999999-X")["Status"] == "Review"


def test_resume_rejects_a_row_whose_url_does_not_belong_to_its_source():
    rows = [{"Job ID": "ba:99999999-X", "Company": "ACME", "Role": "Dev",
             "Location": "Stuttgart", "Status": "Found", "Approval": "Not reviewed",
             "Match %": "", "Job URL": url}
            for url in ("https://evil.example/jobs/99999999-X",
                        "https://www.arbeitsagentur.de/jobsuche/jobdetail/99999999-X")]
    resumed = resumable_from_sheet(rows)
    assert [job.job_url for job in resumed] == [
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/99999999-X"]


def test_resume_ignores_sources_not_enabled_in_this_run():
    row = {"Job ID": "lever:acme-9f2", "Company": "ACME", "Role": "Dev",
           "Location": "Stuttgart", "Status": "Found", "Approval": "Not reviewed",
           "Match %": "", "Job URL": "https://jobs.lever.co/acme/9f2"}
    assert resumable_from_sheet([row], enabled=("ba",)) == []
    assert len(resumable_from_sheet([row], enabled=("lever",))) == 1


# --- eligibility gate ------------------------------------------------------


def test_werkstudent_advert_is_blocked_before_any_model_call():
    job = make_job()
    source = FakeSource("ba", [job], descriptions={job.external_id: WERKSTUDENT_DESCRIPTION})
    model = FakeModel()
    values = StatefulValues()
    report = run(values, sources=[source], provider=model, write=True)

    assert report.prescreen_blocked == 1
    assert model.calls == 0, "no tokens spent on a ruled-out role"
    assert report.llm_scored == 0
    # Closed as Ineligible so it stops re-entering the queue every run, but
    # with NO score: inventing a 0 would misreport it as assessed and rejected.
    saved = values.row_by_id("ba:10001-12345678-S")
    assert saved["Status"] == "Ineligible"
    assert saved["Match %"] == "", "a terminal status must not fabricate a score"
    assert saved["Approval"] == "Not reviewed"
    assert "pre-screen" in saved["Critical gaps"]
    assert report.closed_ineligible == 1


def test_verification_questions_reach_the_report_and_the_prompt():
    job = make_job()
    advert = GOOD_DESCRIPTION + " Eine Sicherheitsueberpruefung ist erforderlich."
    captured = {}

    class Recorder(FakeModel):
        def complete(self, messages):
            captured["payload"] = json.loads(messages[1]["content"].split("\n", 1)[1])
            return super().complete(messages)

    report = run(StatefulValues(),
                 sources=[FakeSource("ba", [job], descriptions={job.external_id: advert})],
                 provider=Recorder())
    assert report.prescreen_flagged == 1
    assert any("security clearance" in q for q in report.verification_questions)
    assert captured["payload"]["verification_questions"], "model must see the question"
    # The minimized evidence must still exclude identity and private repos.
    evidence = json.dumps(captured["payload"]["candidate"])
    assert "Aslan" not in evidence and "greenroute" not in evidence
    assert "webserv" not in evidence


# --- error recovery --------------------------------------------------------


def test_one_failing_source_does_not_stop_the_other():
    broken = FakeSource("greenhouse", [], fail_discover=True)
    working = FakeSource("ba", [make_job()])
    values = StatefulValues()
    report = run(values, sources=[broken, working], write=True)

    assert report.new == 1
    assert report.source_failures["greenhouse"] == report.queries_planned
    assert report.errors and "[discovery]" in report.errors[0]
    assert values.job_ids == ["ba:10001-12345678-S"]


def test_every_source_failing_aborts_without_any_write():
    values = StatefulValues()
    with pytest.raises(RuntimeError, match="Every search across 2 sources failed"):
        run(values, sources=[FakeSource("ba", [], fail_discover=True),
                             FakeSource("greenhouse", [], fail_discover=True)],
            write=True)
    assert values.writes == []


def test_single_source_failure_keeps_the_v07_message():
    with pytest.raises(RuntimeError, match="Every BA search failed"):
        run(StatefulValues(), sources=[FakeSource("ba", [], fail_discover=True)], write=True)


def test_unreadable_advert_leaves_the_row_unscored_and_retryable():
    job = make_job()
    source = FakeSource("ba", [job], fail_describe={job.external_id})
    values = StatefulValues()
    report = run(values, sources=[source], provider=FakeModel(), write=True)

    assert report.detail_missing == 1 and report.llm_scored == 0
    saved = values.row_by_id("ba:10001-12345678-S")
    assert saved["Status"] == "Found" and saved["Match %"] == ""
    assert any("[description]" in error for error in report.errors)


def test_one_model_failure_does_not_stop_the_queue():
    """v0.12 contract: a single failed assessment must not end the run."""
    jobs = [make_job(external_id=f"10001-{index:04d}-S", role=f"Python Developer {index}")
            for index in range(4)]
    values = StatefulValues()

    class FlakyOnce(FakeModel):
        def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient upstream hiccup")
            return dict(self.assessment)

    model = FlakyOnce()
    report = run(values, sources=[FakeSource("ba", jobs)], provider=model,
                 write=True, max_matches=4, llm_attempts=1,
                 llm_sleep=lambda _: None)
    assert report.llm_failed == 1
    assert report.llm_scored == 3, "the remaining jobs must still be assessed"
    assert report.saved_scores == 3
    assert not report.stop_reason
    # The failed job keeps its unscored row and is retried on a later run.
    assert len([row for row in values.rows if row[8] != ""]) == 3


def test_repeated_consecutive_failures_stop_the_stage():
    jobs = [make_job(external_id=f"10001-{index:04d}-S", role=f"Python Developer {index}")
            for index in range(6)]
    values = StatefulValues()

    class AlwaysFails(FakeModel):
        def complete(self, messages):
            self.calls += 1
            raise RuntimeError("upstream down")

    model = AlwaysFails()
    report = run(values, sources=[FakeSource("ba", jobs)], provider=model,
                 write=True, max_matches=6, llm_attempts=1,
                 max_consecutive_failures=3, llm_sleep=lambda _: None)
    assert report.llm_failed == 3, "stopped after the consecutive-failure limit"
    assert "consecutive provider failures" in report.stop_reason
    assert report.saved_scores == 0
    assert values.writes == [] or all(
        kind == "append" for kind, _ in values.writes)


def test_invalid_model_output_is_rejected_without_writing():
    values = StatefulValues()
    bad = FakeModel(assessment={**ASSESSMENT, "score": "ninety"})
    report = run(values, sources=[FakeSource("ba", [make_job()])], provider=bad, write=True)
    assert report.llm_scored == 0 and report.saved_scores == 0
    assert values.row_by_id("ba:10001-12345678-S")["Match %"] == ""


def test_write_refused_when_application_mode_was_relaxed():
    values = StatefulValues(profile=[
        ["Locations", "Frankfurt am Main"], ["Preferred roles", "Junior Software Developer"],
        ["Min match score", 80], ["Application mode", "Automatic"]])
    with pytest.raises(ValueError, match="Approval required"):
        run(values, sources=[FakeSource("ba", [make_job()])], write=True)
    assert values.writes == []


def test_budgets_are_enforced():
    jobs = [make_job(external_id=f"10001-{index:04d}-S", role=f"Python Developer {index}")
            for index in range(12)]
    model = FakeModel()
    report = run(StatefulValues(), sources=[FakeSource("ba", jobs)], provider=model,
                 max_new=5, max_matches=2, max_detail=3)
    assert report.new == 5
    assert model.calls <= 2
    assert report.llm_attempted <= 2


# --- Gemini ---------------------------------------------------------------


def test_gemini_matching_end_to_end_with_mocked_http(monkeypatch):
    """The real GeminiProvider drives a full run; only the socket is faked."""
    from job_agent.ai_matching import GeminiProvider

    posts = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(ASSESSMENT)}]}}]}

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return Response()

    monkeypatch.setattr("job_agent.ai_matching.requests.post", fake_post)
    values = StatefulValues()
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=GeminiProvider(api_key="unit-test-key",
                                         model="gemini-2.5-flash"),
                 write=True)

    assert report.llm_scored == 1 and report.review == 1
    assert values.row_by_id("ba:10001-12345678-S")["Match %"] == 86
    url, kwargs = posts[0]
    assert "unit-test-key" not in url, "the key must never appear in a URL"
    assert kwargs["headers"]["x-goog-api-key"] == "unit-test-key"
    assert kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"
    # The advert travels as JSON data, never as an instruction to the model.
    assert "CANDIDATE_EVIDENCE_AND_JOB_DATA" in kwargs["json"]["contents"][0]["parts"][0]["text"]


def test_gemini_error_never_leaks_the_key_or_the_body(monkeypatch):
    from job_agent.ai_matching import GeminiProvider

    def exploding_post(url, **kwargs):
        raise ValueError("upstream said: secret-token-abc123 quota exceeded")

    monkeypatch.setattr("job_agent.ai_matching.requests.post", exploding_post)
    report = run(StatefulValues(), sources=[FakeSource("ba", [make_job()])],
                 provider=GeminiProvider(api_key="secret-token-abc123"), write=True)
    joined = " ".join(report.errors)
    assert "secret-token-abc123" not in joined
    assert "quota exceeded" not in joined
    assert "[matching]" in joined


def test_unwritable_cache_does_not_discard_a_completed_run(tmp_path):
    """Results are already saved; a cache write failure is only a warning."""
    unwritable = tmp_path / "nodir"
    unwritable.write_text("not a directory", encoding="utf-8")
    cache = DescriptionCache(unwritable / "cache.json")
    values = StatefulValues()
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=FakeModel(), write=True, cache=cache)
    assert report.saved_scores == 1
    assert values.row_by_id("ba:10001-12345678-S")["Status"] == "Review"
    assert any("[cache]" in error for error in report.errors)


# --- v0.9: employer-direct and search sources through the full pipeline ----


class SingleShotSource(FakeSource):
    """A whole-board source: one pass per run, term ignored (ATS, career page)."""

    single_shot = True

    def discover(self, *, term="", city="", size=100):
        self.discover_calls += 1
        if self.fail_discover:
            raise RuntimeError("source unavailable")
        return [job for job in self.jobs
                if location_matches(job.location, (city,),
                                    allow_remote_germany=True)][:size]


def test_single_shot_source_is_queried_once_per_run():
    """A metered or whole-board source must not be hit once per planned query."""
    board = SingleShotSource("ashby", [make_job("lever", "acme-1", role="AI Engineer")])
    report = run(StatefulValues(), sources=[board], write=False, max_searches=9)
    assert board.discover_calls == 1
    assert report.requests == 1, "one request, not one per query"
    assert report.new == 1


def test_remote_germany_postings_need_the_flag():
    remote = make_job("greenhouse", "4567", role="AI Engineer", city="Germany Remote")
    without = run(StatefulValues(), sources=[SingleShotSource("greenhouse", [remote])],
                  write=False)
    assert without.new == 0, "remote is outside the three cities by default"
    with_flag = run_daily(
        repository=repo_for(StatefulValues()),
        sources=[SingleShotSource("greenhouse", [remote])], profile=profile(),
        provider=None, write=False, max_searches=3, allow_remote_germany=True)
    assert with_flag.new == 1


def test_postings_are_labelled_with_an_inferred_track():
    """A board has no search endpoint, so the track comes from the title."""
    jobs = [make_job("greenhouse", "1", role="Java Spring Boot Entwickler"),
            make_job("greenhouse", "2", role="Linux Systemadministrator"),
            make_job("greenhouse", "3", role="AI Engineer LLM RAG")]
    report = run(StatefulValues(), sources=[SingleShotSource("greenhouse", jobs)],
                 write=False, max_new=10)
    assert set(report.by_track) == {"java_backend", "linux_devops", "ai_engineering"}


def test_dedup_spans_employer_boards_and_search_results():
    """The same advert found by Brave and on the employer's ATS is one row."""
    shared = "https://jobs.lever.co/acme/9f2a"
    ats = make_job("lever", "acme-9f2a", role="Backend Developer", url=shared)
    via_search = make_job("greenhouse", "x1", role="Backend Developer",
                          url=shared + "?utm_source=brave")
    values = StatefulValues()
    report = run(values, sources=[SingleShotSource("lever", [ats]),
                                  SingleShotSource("greenhouse", [via_search])],
                 write=True)
    assert report.new == 1
    assert len(values.rows) == 1


def test_career_page_and_brave_rows_are_not_resumed_from_the_sheet():
    """Their URLs can be any host, so a stored URL is never trusted for a refetch."""
    for source_key, url in (("brave", "https://acme.example/karriere/1"),
                            ("career_page", "https://acme.example/jobs/1")):
        row = {"Job ID": f"{source_key}:acme.example_karriere_1", "Company": "ACME",
               "Role": "Backend Developer", "Location": "Frankfurt am Main",
               "Status": "Found", "Approval": "Not reviewed", "Match %": "",
               "Job URL": url}
        assert resumable_from_sheet([row], enabled=(source_key,)) == []


def test_employer_board_rows_resume_only_from_their_own_host():
    good = {"Job ID": "ashby:acme-9f2", "Company": "ACME", "Role": "Backend Developer",
            "Location": "Stuttgart", "Status": "Found", "Approval": "Not reviewed",
            "Match %": "", "Job URL": "https://jobs.ashbyhq.com/acme/9f2"}
    assert len(resumable_from_sheet([good], enabled=("ashby",))) == 1
    spoofed = {**good, "Job URL": "https://evil.example/jobs/9f2"}
    assert resumable_from_sheet([spoofed], enabled=("ashby",)) == []


def test_a_failing_employer_board_does_not_block_the_broad_feed():
    broken = SingleShotSource("ashby", [], fail_discover=True)
    working = FakeSource("ba", [make_job()])
    values = StatefulValues()
    report = run(values, sources=[broken, working], provider=FakeModel(), write=True)
    assert report.new == 1
    assert report.source_failures["ashby"] == 1
    assert values.job_ids == ["ba:10001-12345678-S"]


def test_source_notes_are_surfaced_without_failing_the_run():
    """An unconfigured board or a robots block is reported, not swallowed."""
    chatty = SingleShotSource("ashby", [make_job("lever", "acme-1", role="AI Engineer")])
    chatty.errors = ["ashby:acme published no postings"]
    report = run(StatefulValues(), sources=[chatty], write=False)
    assert report.new == 1
    assert any("SourceNote" in error and "published no postings" in error
               for error in report.errors)


# --- v0.10: free employer sources reach the pipeline ----------------------


def test_all_free_sources_are_enabled_by_default():
    """v0.9 defaulted to ba+arbeitnow, which bypassed the employer registry."""
    from job_agent.config import FREE_SOURCES, Settings

    settings = Settings(spreadsheet_id="x")
    assert set(FREE_SOURCES) == {"ba", "arbeitnow", "greenhouse", "lever",
                                 "ashby", "career_page"}
    assert settings.enabled_sources == FREE_SOURCES
    assert "brave" not in FREE_SOURCES, "a paid API must stay opt-in"
    # Remote-Germany on by default: without it the ATS boards return nothing.
    assert settings.allow_remote_germany is True


def test_registry_boards_are_not_disabled_by_empty_env_overrides(tmp_path, monkeypatch):
    """The second cause: config dropped greenhouse when GREENHOUSE_BOARDS was empty."""
    import json

    from job_agent.collectors import build_sources
    from job_agent.config import Settings
    from job_agent.employers import load_registry

    path = tmp_path / "employers.json"
    path.write_text(json.dumps({"schema_version": "0.9", "employers": [
        {"name": "Raisin", "platform": "greenhouse", "board": "raisin",
         "enabled": True, "verified_on": "2026-11-21", "observed": {"total": 31}},
        {"name": "Parloa", "platform": "ashby", "board": "parloa",
         "enabled": True, "verified_on": "2026-11-21", "observed": {"total": 46}},
    ]}), encoding="utf-8")
    for name in ("JOB_SOURCES", "GREENHOUSE_BOARDS", "LEVER_COMPANIES",
                 "BRAVE_SEARCH_API_KEY", "ALLOW_REMOTE_GERMANY"):
        monkeypatch.delenv(name, raising=False)

    settings = replace(Settings.from_env(), employers_file=path)
    assert "greenhouse" in settings.enabled_sources
    keys = [source.key for source in build_sources(
        settings, cities=("Frankfurt am Main",), registry=load_registry(path))]
    assert "greenhouse" in keys and "ashby" in keys
    # Platforms with no board configured still skip themselves.
    assert "lever" not in keys and "career_page" not in keys


def test_prioritised_tracks_keep_more_results_in_a_run():
    """A flat quota would cap AI at ceil(9/2)=5; its weight of 3 lifts that."""
    jobs = [make_job("greenhouse", f"ai-{index}", role=f"AI Engineer LLM RAG {index}")
            for index in range(10)]
    jobs += [make_job("greenhouse", f"sap-{index}", role=f"SAP Berater ABAP {index}")
             for index in range(2)]
    report = run(StatefulValues(), sources=[SingleShotSource("greenhouse", jobs)],
                 write=False, max_new=9)
    assert report.by_track.get("ai_engineering", 0) > 5
    assert report.by_track.get("ai_engineering", 0) > report.by_track.get("sap_abap", 0)


# --- v0.11: Potential band, fairness and evidence ranking -----------------


def assessment_at(score, eligibility="eligible"):
    return {**ASSESSMENT, "score": score, "eligibility": eligibility}


@pytest.mark.parametrize("score,eligibility,expected", [
    (92, "eligible", "Review"),
    (80, "eligible", "Review"),
    (79, "eligible", "Potential"),
    (75, "unknown", "Potential"),
    (65, "eligible", "Potential"),
    (64, "eligible", "Skip"),
    # A hard eligibility failure overrides any score.
    (95, "ineligible", "Skip"),
    (75, "ineligible", "Skip"),
])
def test_potential_band_and_eligibility_override(score, eligibility, expected):
    values = StatefulValues()
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=FakeModel(assessment_at(score, eligibility)), write=True)
    assert values.row_by_id("ba:10001-12345678-S")["Status"] == expected
    assert getattr(report, {"Review": "review", "Potential": "potential",
                            "Skip": "skip"}[expected]) == 1


def test_potential_threshold_is_configurable():
    for minimum, expected in ((65, "Potential"), (75, "Skip")):
        values = StatefulValues()
        run_daily(repository=repo_for(values),
                  sources=[FakeSource("ba", [make_job()])], profile=profile(),
                  provider=FakeModel(assessment_at(70)), write=True,
                  max_searches=3, potential_min=minimum)
        assert values.row_by_id("ba:10001-12345678-S")["Status"] == expected


def test_review_threshold_stays_at_the_sheet_value():
    """Adding Potential must not move the 80% Review bar."""
    values = StatefulValues()
    run(values, sources=[FakeSource("ba", [make_job()])],
        provider=FakeModel(assessment_at(80)), write=True)
    assert values.row_by_id("ba:10001-12345678-S")["Status"] == "Review"


def test_potential_rows_keep_every_human_column_untouched():
    values = StatefulValues(headers=list(EXTENDED_HEADERS))
    run(values, sources=[FakeSource("ba", [make_job()])],
        provider=FakeModel(assessment_at(72, "unknown")), write=True)
    row = values.row_by_id("ba:10001-12345678-S")
    assert row["Status"] == "Potential"
    assert row["Approval"] == "Not reviewed"
    assert row["Applied on"] == "" and row["CV file/link"] == ""
    assert row["Eligibility"] == "unknown"


def test_unknown_eligibility_is_labelled_as_a_question_not_a_rejection():
    values = StatefulValues()
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=FakeModel(assessment_at(85, "unknown")), write=True)
    assert report.by_eligibility == {"unknown": 1}
    gaps = values.row_by_id("ba:10001-12345678-S")["Critical gaps"]
    assert "not a disqualification" in gaps
    assert values.row_by_id("ba:10001-12345678-S")["Status"] == "Review"


def test_ineligible_is_labelled_as_a_conflict():
    values = StatefulValues()
    report = run(values, sources=[FakeSource("ba", [make_job()])],
                 provider=FakeModel(assessment_at(90, "ineligible")), write=True)
    assert report.by_eligibility == {"ineligible": 1}
    gaps = values.row_by_id("ba:10001-12345678-S")["Critical gaps"]
    assert "conflicts with evidence" in gaps


def test_per_source_cap_limits_the_largest_feed_in_a_real_run():
    from collections import Counter

    ba_jobs = [make_job(external_id=f"10001-{i:04d}-S", role=f"Python Developer {i}")
               for i in range(20)]
    board = [make_job("greenhouse", f"gh-{i}", role=f"Java Spring Developer {i}")
             for i in range(2)]
    values = StatefulValues()
    report = run(values, sources=[FakeSource("ba", ba_jobs),
                                  SingleShotSource("greenhouse", board)],
                 write=True, max_new=10, source_default_share=0.5)
    assert report.by_source["ba"] <= 5, "BA may not take the whole budget"
    assert report.by_source.get("greenhouse", 0) == 2
    assert report.source_capped.get("ba", 0) > 0
    assert dict(Counter(values.job_ids and [k.split(":")[0] for k in values.job_ids]))


def test_fill_unused_recovers_the_capped_slots():
    ba_jobs = [make_job(external_id=f"10001-{i:04d}-S", role=f"Python Developer {i}")
               for i in range(20)]
    board = [make_job("greenhouse", "gh-0", role="Java Spring Developer")]
    report = run_daily(repository=repo_for(StatefulValues()),
                       sources=[FakeSource("ba", ba_jobs),
                                SingleShotSource("greenhouse", board)],
                       profile=profile(), provider=None, write=False,
                       max_searches=3, max_new=10, source_default_share=0.5,
                       source_fill_leftover=True)
    assert report.new == 10, "unused capacity is handed back out"


def test_global_dedup_still_precedes_the_source_cap():
    """A cap must never resurrect a posting another source already supplied."""
    shared = "https://jobs.ashbyhq.com/acme/9f2a"
    values = StatefulValues()
    report = run(values, sources=[
        FakeSource("ba", [make_job(url=shared, role="Backend Developer")]),
        SingleShotSource("ashby", [make_job("ashby", "acme-9f2a",
                                            role="Backend Developer", url=shared)]),
    ], write=True, max_new=10)
    assert report.new == 1
    assert len(values.rows) == 1


def test_saved_found_rows_are_scored_before_new_discoveries():
    """The backlog the tracker is waiting on comes first."""
    rows = []
    for index in range(6):
        ref = f"9999{index}-X"
        rows.append([f"ba:{ref}", "2026-11-20", "ACME", f"Python Developer {index}",
                     "Frankfurt am Main", "Unknown", "BA",
                     f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
                     "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", ""])
    values = StatefulValues(rows=rows)
    board = [make_job("ashby", "acme-1", role="AI Automation Engineer")]
    scored = []

    class Recorder(FakeModel):
        def complete(self, messages):
            import json
            payload = json.loads(messages[1]["content"].split("\n", 1)[1])
            scored.append(payload["job"]["title"])
            return super().complete(messages)

    run(values, sources=[FakeSource("ba", []), SingleShotSource("ashby", board)],
        provider=Recorder(), write=True, max_matches=2)
    assert len(scored) == 2
    assert all("Python Developer" in title for title in scored), \
        "the saved Found backlog must be drained before new discoveries"

    # With the backlog empty, a new board posting does reach the model.
    fresh = []

    class FreshRecorder(FakeModel):
        def complete(self, messages):
            import json
            payload = json.loads(messages[1]["content"].split("\n", 1)[1])
            fresh.append(payload["job"]["title"])
            return super().complete(messages)

    run(StatefulValues(), sources=[FakeSource("ba", []),
                                   SingleShotSource("ashby", board)],
        provider=FreshRecorder(), write=True, max_matches=2)
    assert any("AI Automation" in title for title in fresh)


def test_backlog_is_interleaved_by_source_within_the_group():
    """Found-first must not let one feed monopolise the budget inside the group."""
    rows = []
    for index in range(4):
        ref = f"8888{index}-X"
        rows.append([f"ba:{ref}", "2026-11-20", "ACME", f"Python Developer {index}",
                     "Frankfurt am Main", "Unknown", "BA",
                     f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
                     "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", ""])
    rows.append(["ashby:acme-77", "2026-11-20", "ACME", "AI Automation Engineer",
                 "Frankfurt am Main", "Unknown", "Ashby",
                 "https://jobs.ashbyhq.com/acme/acme-77",
                 "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", ""])
    seen = []

    class Recorder(FakeModel):
        def complete(self, messages):
            import json
            payload = json.loads(messages[1]["content"].split("\n", 1)[1])
            seen.append(payload["job"]["title"])
            return super().complete(messages)

    run(StatefulValues(rows=rows), sources=[FakeSource("ba", []),
                                            SingleShotSource("ashby", [])],
        provider=Recorder(), write=True, max_matches=2)
    assert any("AI Automation" in title for title in seen), \
        "the single board row must not queue behind four BA rows"


def test_track_labels_never_inherit_an_unrelated_query_track():
    """The cpp_systems mislabel: unmatched titles took the first query's track."""
    jobs = [make_job("ashby", "acme-1", role="AI Automation Engineer"),
            make_job("ashby", "acme-2", role="Implementation Consultant (m/w/d)"),
            make_job("ashby", "acme-3", role="GTM & Growth Engineer")]
    report = run(StatefulValues(), sources=[SingleShotSource("ashby", jobs)],
                 write=False, max_new=10)
    assert report.by_track.get("cpp_systems", 0) == 0
    assert report.by_track.get("ai_engineering", 0) == 1
    assert report.by_track.get("it_consulting", 0) == 1
    assert report.by_track.get("unclassified", 0) == 1
    assert any("GTM" in line for line in report.unclassified)
    assert "ai_engineering" in report.track_evidence


def test_evidence_ranking_reads_the_best_matching_adverts_first():
    """Verified skills order the queue; they never become a score."""
    from job_agent.daily_pipeline import evidence_score, rank_jobs

    terms = ["Java", "Spring Boot", "Docker", "PHP"]
    assert evidence_score("Java Spring Boot Developer", terms) == 2
    assert evidence_score("Marketing Manager", terms) == 0
    # A substring must not count: "Javascript" is not "Java".
    assert evidence_score("Javascript Developer", ["Java"]) == 0

    unrelated = make_job("ba", "10001-0001-S", role="Technischer Redakteur")
    evidenced = make_job("ba", "10001-0002-S", role="Java Spring Boot Entwickler")
    ordered = rank_jobs([unrelated, evidenced], skill_terms=terms)
    assert ordered[0].role.startswith("Java")
