import json
from pathlib import Path

import pytest

from job_agent.ai_matching import (JobDescription, MatchResult, minimal_llm_profile,
                                   matching_messages, GeminiProvider, OllamaProvider)
from job_agent.candidate_profile import load_candidate_profile, blocking_review_items
from job_agent.models import JobCandidate
from job_agent.sheets import GoogleSheetsRepository
from test_sheets import FakeService, FakeValues


def profile():
    return load_candidate_profile(Path("data/candidate_profile.json"))


def job():
    return JobDescription.from_dict({
        "job_id": "DEMO-MATCH-001", "company": "Synthetic Company — NOT REAL",
        "role": "Junior PHP Backend Developer", "location": "Frankfurt am Main",
        "job_url": "https://example.com/test-vacancy", "source": "Synthetic",
        "description": "Looking for a junior backend developer in Frankfurt. "
                       "PHP 8, SQL, REST APIs, Docker, Linux and unit tests required. "
                       "Candidate should have completed training; commercial software employment is not a prerequisite."
    })


def assessment(score=87, eligibility="eligible"):
    return {"score":score,"summary":"Strong PHP backend project evidence, no commercial development history",
            "matched_skills":["PHP 8", "Docker"], "missing_skills":["Symfony"],
            "critical_gaps":[], "language":"German B2", "track":"backend_php", "eligibility":eligibility}


def test_availability_resolved_without_changing_employment():
    p = profile()
    assert blocking_review_items(p) == []
    assert p["candidate"]["availability"]["display"] == "ab sofort"
    assert p["professional_background"]["employment_history_status"] == "user_confirmed_management_history"
    assert "Sberbank" in p["professional_background"]["excluded_as_incorrect_by_user"][1]


def test_remote_llm_input_excludes_private_repos_identity_and_sensitive_details():
    p = minimal_llm_profile(profile())
    txt = json.dumps(p)
    assert "webserv" not in txt.lower()
    assert "greenroute" not in txt.lower()
    assert "Aslan Isaev" not in txt
    assert "gmail" not in txt.lower()
    assert "Tinkoff" not in txt and "Sberbank" not in txt
    assert "ab sofort" in txt
    assert "SAP ABAP" in p["unsupported_skills"]
    messages = matching_messages(job(), p)
    assert messages[0]["role"] == "system"
    assert "Junior PHP" in messages[1]["content"]


def test_blocking_profile_prevents_prompt():
    p = profile()
    p["review_required"].append({"id":"other","severity":"blocking"})
    with pytest.raises(ValueError, match="Blocking"):
        minimal_llm_profile(p)


def test_model_scores_validated_and_never_assumed():
    assert MatchResult.from_dict(assessment()).score == 87
    for bad in (True, -2, 1000, "87"):
        with pytest.raises(ValueError, match="score"):
            MatchResult.from_dict(assessment(score=bad))
    with pytest.raises(ValueError, match="eligibility"):
        MatchResult.from_dict(assessment(eligibility="certainly"))


def test_description_must_be_long_and_https():
    raw = vars(job()).copy()
    raw["description"] = "Short"
    with pytest.raises(ValueError, match="too short"):
        JobDescription.from_dict(raw)
    raw = vars(job()).copy();raw["job_url"] = "http://example.com"
    with pytest.raises(ValueError, match="https"):
        JobDescription.from_dict(raw)


def test_record_ai_match_transitions_only_unscored_found():
    row = ["DEMO-MATCH-001", "2026-11-21", "Company", "PHP Developer", "Frankfurt", "Unknown",
           "Arbeitnow", "https://example.com", "", "", "", "Unknown", "Found", "Not reviewed", "", "", "", "", "user notes"]
    api = FakeValues(rows=[row])
    repo = GoogleSheetsRepository(FakeService(api), "some-sheet")
    candidate = JobCandidate.from_dict({"job_id":"DEMO-MATCH-001", "company":"Company", "role":"PHP Developer",
                                        "location":"Frankfurt", "job_url":"https://example.com","match_score":87})
    assert "assessed existing row 2" in repo.record_ai_match(candidate, "Review")
    assert api.writes[0][1]["range"] == "'Vacancies'!I2:M2"
    assert api.writes[0][1]["body"]["values"][0][-1] == "Review"
    assert api.writes[0][1]["valueInputOption"] == "RAW"


@pytest.mark.parametrize("status,approval,score",[("Approved","Approved",""),("Review","Not reviewed", ""),("Found","Not reviewed",91),("Found","Approved","")])
def test_record_ai_match_never_overwrites_human_state(status,approval,score):
    row = ["DEMO-MATCH-001", "2026-11-21", "Company", "PHP Developer", "Frankfurt", "Unknown",
           "Arbeitnow", "https://example.com", score, "", "", "Unknown", status, approval, "", "", "", "", "user notes"]
    api = FakeValues(rows=[row])
    repo = GoogleSheetsRepository(FakeService(api), "sheet")
    candidate = JobCandidate.from_dict({"job_id":"DEMO-MATCH-001", "company":"Company", "role":"PHP Developer",
                                        "location":"Frankfurt", "job_url":"https://example.com","match_score":87})
    assert "protected" in repo.record_ai_match(candidate, "Review")
    assert not api.writes


def test_json_response_ollama(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):return {"message":{"content":json.dumps(assessment())}}
    def fake_post(url,**kwargs):
        assert url.endswith("/api/chat")
        assert kwargs["json"]["stream"] is False
        return Response()
    monkeypatch.setattr("job_agent.ai_matching.requests.post",fake_post)
    assert OllamaProvider().complete(matching_messages(job(),minimal_llm_profile(profile())))["score"] == 87


def test_json_response_gemini_never_sends_key_in_url(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):return {"candidates":[{"content":{"parts":[{"text":json.dumps(assessment())}]}}]}
    def fake_post(url,**kwargs):
        assert "some-test-key" not in url
        assert kwargs["headers"]["x-goog-api-key"] == "some-test-key"
        assert kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"
        return Response()
    monkeypatch.setattr("job_agent.ai_matching.requests.post",fake_post)
    assert GeminiProvider(api_key="some-test-key").complete(matching_messages(job(),minimal_llm_profile(profile())))["score"] == 87


def test_graph_read_only_default():
    pytest.importorskip("langgraph")
    from job_agent.match_workflow import build_match_graph
    class Model:
        def complete(self,messages):return assessment()
    graph = build_match_graph(profile=profile(),provider=Model())
    state = graph.invoke({"job":vars(job()),"threshold":80,"write":False})
    assert state["status"] == "Review"
    assert state["score"] == 87
    assert "outcome" not in state


def test_graph_rejects_clear_ineligible_even_if_high_score():
    pytest.importorskip("langgraph")
    from job_agent.match_workflow import build_match_graph
    class Model:
        def complete(self,messages):return assessment(score=92,eligibility="ineligible")
    graph=build_match_graph(profile=profile(),provider=Model())
    state=graph.invoke({"job":vars(job()),"threshold":80,"write":False})
    assert state["status"] == "Skip"
