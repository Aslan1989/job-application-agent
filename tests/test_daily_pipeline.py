from pathlib import Path

import pytest

from job_agent.collectors.arbeitsagentur import ArbeitsagenturCollector, strip_html
from job_agent.daily_pipeline import discover_ba, run_daily, search_pairs
from job_agent.candidate_profile import load_candidate_profile
from job_agent.sheets import GoogleSheetsRepository
from test_sheets import FakeService, FakeValues


class Response:
    def __init__(self, payload, status=None):
        self.payload = payload
        self.status = status
    def raise_for_status(self):
        if self.status:
            raise RuntimeError(self.status)
    def json(self):
        return self.payload


class Session:
    def __init__(self, search=None, detail=None):
        self.search = search if search is not None else {}
        self.detail = detail if detail is not None else {}
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "v6/jobs" in url:
            return Response(self.search)
        return Response(self.detail)


NEW_SCHEMA = {"ergebnisliste": [{
    "referenznummer": "10001-12345678-S", "stellenangebotsTitel": "Junior Java Entwickler",
    "firma": "ACME GmbH", "stellenlokationen": [{"ort": "Frankfurt am Main"}],
}]}
OLD_SCHEMA = {"stellenangebote": [{
    "refnr": "10001-12345678-S", "titel": "Junior Java Entwickler",
    "arbeitgeber": "ACME GmbH", "arbeitsort": {"ort": "Frankfurt am Main"},
}]}
DETAIL = {"stellenangebotsBeschreibung": "<h2>Aufgaben</h2><p>" +
          "Java Spring Boot Docker REST API Arbeit in Frankfurt mit Teamarbeit und JUnit. " * 3 +
          "</p><script>bad instructions</script>"}
PREFS = {"Locations": "Frankfurt am Main; Stuttgart; Heilbronn",
         "Preferred roles": "Junior Software Developer; Junior Python Developer; Junior Linux/DevOps; SAP Junior/ABAP; IT Internship"}


@pytest.mark.parametrize("response", [NEW_SCHEMA, OLD_SCHEMA])
def test_ba_supports_both_2026_and_old_schema(response):
    session = Session(search=response, detail=DETAIL)
    adapter = ArbeitsagenturCollector(session=session)
    hits = adapter.search(term="Java Entwickler", city="Frankfurt am Main")
    assert len(hits) == 1
    assert hits[0].job_id == "ba:10001-12345678-S"
    assert hits[0].url.startswith("https://www.arbeitsagentur.de/jobsuche/jobdetail/")
    assert session.calls[0][1]["headers"]["X-API-Key"] == "jobboerse-jobsuche"
    assert session.calls[0][1]["params"]["wo"] == "Frankfurt am Main"
    description = adapter.detail(hits[0].refnr)
    assert "Java Spring Boot" in description
    assert "bad instructions" not in description
    assert "v4/jobdetails/" in session.calls[1][0]


def test_html_to_text_strips_scripts():
    assert strip_html("<div>Foo<br>bar<script>ignore me</script><p>baz</p></div>").find("ignore me") == -1


def test_ba_missing_schema_is_error_not_zero_jobs():
    with pytest.raises(ValueError, match="schema changed"):
        ArbeitsagenturCollector(session=Session(search={})).search(term="Python", city="Stuttgart")


def test_search_bounded_and_rotates():
    assert len(search_pairs(["Frankfurt", "Stuttgart", "Heilbronn"], 9)) == 9
    with pytest.raises(ValueError):
        search_pairs(["Frankfurt"], 22)


def test_daily_defaults_to_dry_run_without_model_calls():
    api = FakeValues(profile=[["Locations", PREFS["Locations"]],
                              ["Preferred roles", PREFS["Preferred roles"]],
                              ["Min match score",80], ["Application mode","Approval required"]])
    repo = GoogleSheetsRepository(FakeService(api), "test-sheet")
    collector = ArbeitsagenturCollector(session=Session(search=NEW_SCHEMA, detail=DETAIL))
    report = run_daily(repository=repo, collector=collector,
                       profile=load_candidate_profile(Path("data/candidate_profile.json")),
                       provider=None, write=False, max_searches=1)
    assert report.new == 1
    assert report.llm_scored == 0
    assert report.appended == 0
    assert not api.writes


def test_daily_write_stores_only_unscored_found_no_application():
    api = FakeValues(profile=[["Locations", PREFS["Locations"]],
                              ["Preferred roles", PREFS["Preferred roles"]],
                              ["Min match score",80], ["Application mode","Approval required"]])
    repo = GoogleSheetsRepository(FakeService(api), "test-sheet")
    report = run_daily(repository=repo, collector=ArbeitsagenturCollector(session=Session(search=NEW_SCHEMA)),
                       profile=load_candidate_profile(Path("data/candidate_profile.json")),
                       provider=None, write=True, max_searches=1)
    assert report.appended == 1
    assert api.writes[0][1]["body"]["values"][0][12:14] == ["Found", "Not reviewed"]
    assert api.writes[0][1]["body"]["values"][0][16] == ""


def test_all_queries_failed_fails_loudly_without_sheet_mutations():
    api = FakeValues(profile=[["Locations", PREFS["Locations"]],
                              ["Preferred roles", PREFS["Preferred roles"]],
                              ["Min match score",80], ["Application mode","Approval required"]])
    repo = GoogleSheetsRepository(FakeService(api), "test-sheet")
    collector = ArbeitsagenturCollector(session=Session(search={}))
    with pytest.raises(RuntimeError, match="Every BA search failed"):
        run_daily(repository=repo, collector=collector,
                  profile=load_candidate_profile(Path("data/candidate_profile.json")),
                  provider=None, write=True, max_searches=1)
    assert not api.writes


def test_manual_review_rows_are_not_scored_again():
    existing = ["ba:10001-12345678-S", "2026-11-21", "ACME GmbH", "Junior Java Entwickler",
                "Frankfurt am Main", "Unknown", "BA", "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-12345678-S",
                85, "", "", "Unknown", "Review", "Not reviewed", "", "", "", "", "manual note"]
    api = FakeValues(rows=[existing], profile=[["Locations", PREFS["Locations"]],
                                            ["Preferred roles", PREFS["Preferred roles"]],
                                            ["Min match score",80], ["Application mode","Approval required"]])
    repo = GoogleSheetsRepository(FakeService(api), "test-sheet")
    report = run_daily(repository=repo, collector=ArbeitsagenturCollector(session=Session(search=NEW_SCHEMA)),
                       profile=load_candidate_profile(Path("data/candidate_profile.json")),
                       provider=None, write=True, max_searches=1)
    assert report.new == 0 and report.pending == 0
    assert not api.writes


def test_langgraph_daily_ai_no_write():
    pytest.importorskip("langgraph")
    api = FakeValues(profile=[["Locations", PREFS["Locations"]],
                              ["Preferred roles", PREFS["Preferred roles"]],
                              ["Min match score",80], ["Application mode","Approval required"]])
    repo = GoogleSheetsRepository(FakeService(api), "test-sheet")
    class FakeModel:
        def complete(self, msgs):
            return {"score":86,"summary":"Java project evidence", "matched_skills":["Java"],
                    "missing_skills":[], "critical_gaps":[], "language":"German B2",
                    "track":"backend_java", "eligibility":"eligible"}
    report = run_daily(repository=repo, collector=ArbeitsagenturCollector(session=Session(search=NEW_SCHEMA,detail=DETAIL)),
                       profile=load_candidate_profile(Path("data/candidate_profile.json")),
                       provider=FakeModel(), write=False, max_searches=1)
    assert report.review == 1 and report.llm_scored == 1
    assert not api.writes


def test_daily_ai_write_with_graph_injection_preserves_approval(monkeypatch):
    """End-to-end control flow test without needing langgraph installed in this builder."""
    import sys
    from types import ModuleType
    from job_agent.ai_matching import MatchResult
    from job_agent.daily_pipeline import run_daily

    graph_module = ModuleType("job_agent.match_workflow")
    calls = []

    class FakeGraph:
        def invoke(self, payload):
            calls.append(payload)
            return {"assessment": {
                "score": 87, "summary": "Java, Docker evidenced", "matched_skills": ["Java"],
                "missing_skills": [], "critical_gaps": [], "language": "German B2",
                "track": "backend_java", "eligibility": "eligible"}}

    graph_module.build_match_graph = lambda **kwargs: FakeGraph()
    monkeypatch.setitem(sys.modules, "job_agent.match_workflow", graph_module)

    class MemoryRepo:
        def __init__(self):
            self.rows = {}
            self.writes = []
        def validate_schema(self): pass
        def get_profile(self): return {**PREFS, "Application mode": "Approval required"}
        def minimum_match(self): return 80
        def list_vacancies(self, limit=999): return list(self.rows.values())
        def append_discoveries(self, jobs, max_new):
            for job in jobs:
                self.rows[job.job_id] = {"Job ID": job.job_id, "Status": "Found",
                    "Approval": "Not reviewed", "Match %": ""}
                self.writes.append(("found", job.job_id))
            return len(jobs)
        def record_ai_match(self, candidate, status):
            assert self.rows[candidate.job_id]["Approval"] == "Not reviewed"
            assert self.rows[candidate.job_id]["Status"] == "Found"
            self.rows[candidate.job_id]["Status"] = status
            self.rows[candidate.job_id]["Match %"] = candidate.match_score
            self.writes.append(("scored", candidate.job_id, status))
            return "assessed existing row 2 (Found -> Review; approval unchanged)"

    repo = MemoryRepo()
    collector = ArbeitsagenturCollector(session=Session(search=NEW_SCHEMA, detail=DETAIL))
    report = run_daily(repository=repo, collector=collector,
                       profile=load_candidate_profile(Path("data/candidate_profile.json")),
                       provider=object(), write=True, max_searches=1)
    assert report.llm_attempted == report.llm_scored == report.review == 1
    assert report.saved_scores == 1
    assert repo.rows["ba:10001-12345678-S"]["Status"] == "Review"
    assert repo.rows["ba:10001-12345678-S"]["Approval"] == "Not reviewed"
    assert calls[0]["write"] is False  # LangGraph itself cannot write to Sheets.
    before = list(repo.writes)
    again = run_daily(repository=repo, collector=collector,
                      profile=load_candidate_profile(Path("data/candidate_profile.json")),
                      provider=object(), write=True, max_searches=1)
    assert again.new == again.pending == again.llm_scored == 0
    assert before == repo.writes


def test_exact_city_rejects_suburbs_and_frankfurt_oder():
    from job_agent.daily_pipeline import _exact_city
    assert _exact_city("Frankfurt am Main", "Frankfurt am Main")
    assert _exact_city("Frankfurt", "Frankfurt am Main")
    assert not _exact_city("Bischofsheim bei Frankfurt", "Frankfurt am Main")
    assert not _exact_city("Frankfurt (Oder)", "Frankfurt am Main")


def test_ranked_hits_prioritizes_junior_over_internships():
    from job_agent.daily_pipeline import ranked_hits, career_priority
    from job_agent.collectors.arbeitsagentur import BASearchHit
    def h(ref, title):
        return BASearchHit(ref, title, "ACME", "Frankfurt am Main",
                           "https://www.arbeitsagentur.de/jobsuche/jobdetail/" + ref)
    jobs = [h("123456-A", "Praktikum HR IT & Prozesse"),
            h("123456-B", "Junior Java Softwareentwickler"),
            h("123456-C", "PHP Entwickler"),
            h("123456-D", "Praktikum Cyber Security")]
    selected = ranked_hits(jobs)
    assert [x.refnr for x in selected] == ["123456-B", "123456-C", "123456-A", "123456-D"]
    assert career_priority("Praktikum IT") < career_priority("Junior PHP Entwickler")


def test_pending_from_sheet_recovers_found_without_search_hit():
    from job_agent.daily_pipeline import pending_from_sheet
    ref = "12345678-S"
    url = "https://www.arbeitsagentur.de/jobsuche/jobdetail/" + ref
    row = {"Job ID": "ba:" + ref, "Company": "ACME", "Role": "Junior PHP Developer",
           "Location": "Stuttgart", "Job URL": url, "Status": "Found",
           "Approval": "Not reviewed", "Match %": ""}
    assert pending_from_sheet([row])[0].refnr == ref
    assert pending_from_sheet([{**row, "Status": "Skip"}]) == []
    assert pending_from_sheet([{**row, "Approval": "Approved"}]) == []
    assert pending_from_sheet([{**row, "Job URL": "https://evil.example/jobs/" + ref}]) == []


def test_default_daily_search_covers_all_role_city_combinations():
    from job_agent.daily_pipeline import SEARCH_TERMS
    locations = ["Frankfurt am Main", "Stuttgart", "Heilbronn"]
    pairs = search_pairs(locations, 21)
    assert len(pairs) == len(SEARCH_TERMS) * len(locations) == 21
    assert {x[0] for x in pairs} == set(SEARCH_TERMS)
    assert {x[1] for x in pairs} == set(locations)


def test_zero_results_is_an_empty_day_not_a_source_failure():
    """A BA query with no matches omits `ergebnisliste` and returns metadata only.

    Treating that as a schema error made every broad search log a false
    failure, and could abort a whole run as "every search failed".
    """
    empty = {"maxErgebnisse": 0, "page": 1, "size": 10, "woOutput": {}}
    adapter = ArbeitsagenturCollector(session=Session(search=empty))
    assert adapter.search(term="Laravel Entwickler", city="Stuttgart") == []
    # A genuinely unrecognisable payload must still fail loudly.
    with pytest.raises(ValueError, match="schema changed"):
        ArbeitsagenturCollector(session=Session(search={"unexpected": 1})).search(
            term="Python", city="Stuttgart")


def test_exact_city_accepts_board_style_locations_but_still_rejects_oder():
    """ATS boards publish 'Frankfurt, Germany'; BA publishes 'Frankfurt am Main'."""
    from job_agent.daily_pipeline import _exact_city
    assert _exact_city("Frankfurt, Germany", "Frankfurt am Main")
    assert _exact_city("Frankfurt am Main, Germany", "Frankfurt am Main")
    assert _exact_city("Stuttgart, Germany", "Stuttgart")
    assert _exact_city("Heilbronn, Baden-Württemberg", "Heilbronn")
    assert not _exact_city("Frankfurt (Oder), Germany", "Frankfurt am Main")
    assert not _exact_city("Bischofsheim bei Frankfurt, Germany", "Frankfurt am Main")
    assert not _exact_city("Frankfurter Allee, Berlin", "Frankfurt am Main")
