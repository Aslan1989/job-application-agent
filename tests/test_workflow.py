from __future__ import annotations

import pytest

from job_agent.models import JobCandidate


def test_graph_uses_threshold_and_saves_only(monkeypatch):
    pytest.importorskip("langgraph")
    from job_agent.workflow import build_intake_graph

    class Repo:
        def __init__(self): self.saved = []
        def minimum_match(self): return 80
        def upsert_candidate(self, candidate: JobCandidate, status: str):
            self.saved.append((candidate.job_id, status))
            return "inserted"
    repo = Repo()
    graph = build_intake_graph(repo)
    template = {"company": "C", "role": "Junior", "location": "Stuttgart", "job_url": "https://example.com/job/1"}
    above = graph.invoke({"candidate": {**template, "job_id": "job-1", "match_score": 80}})
    below = graph.invoke({"candidate": {**template, "job_id": "job-2", "match_score": 79}})
    assert above["status"] == "Review"
    assert below["status"] == "Skip"
    assert repo.saved == [("job-1", "Review"), ("job-2", "Skip")]
