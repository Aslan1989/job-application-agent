"""Phase-1 LangGraph: deterministic intake, NOT an AI matching or sender."""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .models import JobCandidate
from .sheets import GoogleSheetsRepository


class IntakeState(TypedDict, total=False):
    candidate: dict[str, Any]
    threshold: int
    status: str
    outcome: str


def build_intake_graph(repository: GoogleSheetsRepository):
    def load_preferences(state: IntakeState) -> IntakeState:
        return {"threshold": repository.minimum_match()}

    def screen(state: IntakeState) -> IntakeState:
        job = JobCandidate.from_dict(state["candidate"])
        status = "Review" if job.match_score >= state["threshold"] else "Skip"
        return {"status": status}

    def store(state: IntakeState) -> IntakeState:
        job = JobCandidate.from_dict(state["candidate"])
        outcome = repository.upsert_candidate(job, state["status"])
        return {"outcome": outcome}

    graph = StateGraph(IntakeState)
    graph.add_node("load_preferences", load_preferences)
    graph.add_node("screen_threshold", screen)
    graph.add_node("save_to_sheet", store)
    graph.add_edge(START, "load_preferences")
    graph.add_edge("load_preferences", "screen_threshold")
    graph.add_edge("screen_threshold", "save_to_sheet")
    graph.add_edge("save_to_sheet", END)
    return graph.compile()
