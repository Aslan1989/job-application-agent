"""LangGraph AI matcher with an explicit, opt-in Sheets write step.

A completed match only writes an assessment. It cannot submit a job application.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .ai_matching import (CompletionProvider, JobDescription, MatchResult,
                          as_candidate, decide_status, matching_messages,
                          minimal_llm_profile)
from .sheets import GoogleSheetsRepository


class MatchState(TypedDict, total=False):
    job: dict[str, Any]
    verification_questions: list[str]
    threshold: int
    potential_min: int
    write: bool
    score: int
    status: str
    assessment: dict[str, Any]
    outcome: str


def build_match_graph(*, profile: dict[str, Any], provider: CompletionProvider,
                      repository: GoogleSheetsRepository | None = None):
    safe_profile = minimal_llm_profile(profile)

    def evaluate(state: MatchState) -> MatchState:
        job = JobDescription.from_dict(state["job"])
        questions = state.get("verification_questions") or ()
        messages = matching_messages(job, safe_profile, verification_questions=questions)
        result = MatchResult.from_dict(provider.complete(messages))
        return {"score": result.score, "assessment": result.as_assessment()}

    def decide(state: MatchState) -> MatchState:
        threshold = state.get("threshold", 80)
        if type(threshold) is not int or not 0 <= threshold <= 100:
            raise ValueError("threshold must be an integer from 0 to 100")
        assessment = state["assessment"]
        potential_min = state.get("potential_min", 65)
        return {"status": decide_status(assessment["score"],
                                        assessment["eligibility"],
                                        threshold=threshold,
                                        potential_min=potential_min)}

    def store(state: MatchState) -> MatchState:
        if repository is None:
            raise ValueError("--write requires Google Sheets connection")
        job = JobDescription.from_dict(state["job"])
        result = MatchResult.from_dict(state["assessment"])
        return {"outcome": repository.record_ai_match(as_candidate(job, result), state["status"])}

    def route(state: MatchState) -> str:
        return "store" if state.get("write", False) else "finish"

    graph = StateGraph(MatchState)
    graph.add_node("evaluate_llm", evaluate)
    graph.add_node("decide_threshold", decide)
    graph.add_node("save_to_sheets", store)
    graph.add_edge(START, "evaluate_llm")
    graph.add_edge("evaluate_llm", "decide_threshold")
    graph.add_conditional_edges("decide_threshold", route,
                                {"store": "save_to_sheets", "finish": END})
    graph.add_edge("save_to_sheets", END)
    return graph.compile()
