"""Stage A search configuration: breadth, fairness and daily rotation."""
from __future__ import annotations

from datetime import date

import pytest

from job_agent.career_tracks import (CAREER_TRACKS, CareerTrack, resolve_tracks,
                                     search_plan, track_quota)

CITIES = ["Frankfurt am Main", "Stuttgart", "Heilbronn"]


def test_every_requested_career_category_is_searchable():
    keys = {track.key for track in CAREER_TRACKS}
    assert keys == {
        "ai_engineering", "python_automation", "java_backend", "php_web",
        "cpp_systems", "linux_devops", "data_engineering", "sap_abap",
        "it_consulting", "graduate_entry", "software_generalist",
    }


def test_broad_search_terms_replace_the_seven_fixed_titles():
    terms = [term.casefold() for track in CAREER_TRACKS for term in track.terms]
    # Far broader than v0.7's seven hard-coded titles.
    assert len(terms) > 50
    assert len(set(terms)) == len(terms), "duplicate search terms waste budget"
    # German and English variants both present.
    assert "python entwickler" in terms and "python developer" in terms
    assert "java entwickler" in terms and "java backend developer" in terms


def test_ai_engineer_discovery_terms_exist():
    ai_terms = [term.casefold() for term in
                next(t for t in CAREER_TRACKS if t.key == "ai_engineering").terms]
    for expected in ("ai engineer", "llm engineer", "rag developer", "ki entwickler"):
        assert expected in ai_terms
    python_terms = [t.casefold() for t in
                    next(t for t in CAREER_TRACKS if t.key == "python_automation").terms]
    assert "automation engineer" in python_terms


def test_generic_entry_level_titles_are_searched_without_the_word_junior():
    generic = next(t for t in CAREER_TRACKS if t.key == "software_generalist")
    folded = [term.casefold() for term in generic.terms]
    assert "software engineer" in folded
    assert "backend developer" in folded
    assert "application developer" in folded
    # No track may require "junior" in the query: that would hide entry roles
    # advertised under a plain title.
    assert not any("junior" in term.casefold()
                   for track in CAREER_TRACKS for term in track.terms)


def test_plan_is_bounded_and_covers_every_city():
    plan = search_plan(CITIES, 33, today=date(2026, 11, 21))
    assert len(plan) == 33
    assert {query.city for query in plan} == set(CITIES)
    assert {query.track for query in plan} == {t.key for t in CAREER_TRACKS}
    with pytest.raises(ValueError):
        search_plan(CITIES, 0)
    with pytest.raises(ValueError):
        search_plan([], 10)


def test_quota_is_fair_so_internships_cannot_dominate():
    """One round per track before any track gets a second query."""
    plan = search_plan(CITIES, len(CAREER_TRACKS), today=date(2026, 11, 21))
    assert len(plan) == len(CAREER_TRACKS)
    assert len({query.track for query in plan}) == len(CAREER_TRACKS)
    # With a budget below the track count, no track may take two slots.
    small = search_plan(CITIES, 4, today=date(2026, 11, 21))
    assert len({query.track for query in small}) == 4
    assert track_quota(12, 11) == 2


def test_rotation_reaches_other_terms_on_other_days():
    first = search_plan(CITIES, 11, today=date(2026, 11, 21))
    later = search_plan(CITIES, 11, today=date(2026, 11, 25))
    assert {(q.track, q.term) for q in first} != {(q.track, q.term) for q in later}
    # Same day is deterministic, so a re-run plans identical queries.
    assert search_plan(CITIES, 11, today=date(2026, 11, 21)) == first


def test_track_selection_and_validation():
    subset = resolve_tracks(["ai_engineering", "php_web"])
    assert [track.key for track in subset] == ["ai_engineering", "php_web"]
    assert resolve_tracks(None) == CAREER_TRACKS
    assert resolve_tracks([]) == CAREER_TRACKS
    with pytest.raises(ValueError, match="Unknown career tracks"):
        resolve_tracks(["not_a_track"])


def test_malformed_track_is_rejected_at_definition_time():
    with pytest.raises(ValueError, match="Invalid career track key"):
        CareerTrack(key="Bad Key", label="x", terms=("a",))
    with pytest.raises(ValueError, match="at least one search term"):
        CareerTrack(key="empty_track", label="x", terms=())
    with pytest.raises(ValueError, match="duplicate terms"):
        CareerTrack(key="dupe_track", label="x", terms=("a", "a"))


# --- v0.10: priority weighting by profile evidence ------------------------


def test_profile_supported_tracks_carry_the_highest_priority():
    """AI/LLM/RAG, Python, backend and Linux/DevOps are what the profile evidences."""
    priority = {track.key: track.priority for track in CAREER_TRACKS}
    for key in ("ai_engineering", "python_automation", "java_backend", "php_web",
                "cpp_systems", "linux_devops", "software_generalist"):
        assert priority[key] == 3, f"{key} should be prioritised"
    assert priority["data_engineering"] == 2, "SQL evidenced, ETL/BI tooling not"
    # Not evidenced or transferable only: searched, but not prioritised.
    for key in ("sap_abap", "it_consulting", "graduate_entry"):
        assert priority[key] == 1


def test_priority_is_validated():
    with pytest.raises(ValueError, match="priority must be 1, 2 or 3"):
        CareerTrack(key="bad_priority", label="x", terms=("a",), priority=4)


def test_budget_share_follows_priority():
    from collections import Counter

    plan = search_plan(CITIES, 33, today=date(2026, 11, 21))
    share = Counter(query.track for query in plan)
    assert share["ai_engineering"] >= share["data_engineering"]
    assert share["data_engineering"] >= share["sap_abap"]
    assert share["python_automation"] >= 3
    assert share["linux_devops"] >= 3


def test_every_track_is_still_reached_at_a_small_budget():
    """Prioritising must not starve the low-priority routes completely."""
    plan = search_plan(CITIES, len(CAREER_TRACKS), today=date(2026, 11, 21))
    assert {query.track for query in plan} == {t.key for t in CAREER_TRACKS}


def test_quota_weights_match_priority():
    from job_agent.career_tracks import track_weights

    weights = track_weights()
    assert weights["ai_engineering"] == 3
    assert weights["sap_abap"] == 1


# --- v0.11: classification must rest on evidence --------------------------


def test_cosuno_titles_are_classified_or_admitted_unclassified():
    """Regression: these four were reported as python_automation(2)+cpp_systems(2).

    The two unmatched titles inherited whichever track the day's first planned
    query carried, which happened to be cpp_systems.
    """
    from job_agent.career_tracks import UNCLASSIFIED, classify_title

    assert classify_title("AI Automation Engineer").track == "ai_engineering"
    assert classify_title("Founders Associate – AI & Automation (m/w/d)").track == "ai_engineering"
    assert classify_title("Implementation Consultant (m/w/d)").track == "it_consulting"
    # Genuinely unidentifiable: a sales/growth title with no technical signal.
    unknown = classify_title("GTM & Growth Engineer")
    assert unknown.track == UNCLASSIFIED
    assert unknown.confident is False
    # None of them may land in cpp_systems.
    for title in ("AI Automation Engineer", "Implementation Consultant (m/w/d)",
                  "Founders Associate – AI & Automation (m/w/d)",
                  "GTM & Growth Engineer"):
        assert classify_title(title).track != "cpp_systems", title


def test_every_classification_carries_its_supporting_words():
    from job_agent.career_tracks import classify_title

    match = classify_title("Backend Entwickler Java / Spring Boot")
    assert match.track == "java_backend"
    assert set(match.evidence) >= {"java", "spring"}
    assert match.confident
    # And an unconfident match carries none, so it can never be presented as one.
    assert classify_title("Office Manager").evidence == ()


def test_ai_and_ki_are_signals_not_stopwords():
    """Suppressing them made every AI title classify as something else."""
    from job_agent.career_tracks import _STOPWORDS, classify_title

    assert "ai" not in _STOPWORDS and "ki" not in _STOPWORDS
    assert classify_title("KI Entwickler").track == "ai_engineering"
    assert classify_title("LLM Application Engineer").track == "ai_engineering"
    assert classify_title("RAG Platform Engineer").track == "ai_engineering"


@pytest.mark.parametrize("title", [
    "Office Manager", "Fachkraft für Arbeitssicherheit", "Account Executive",
    "Sales Development Representative", "Credit Risk Manager", "",
])
def test_unidentifiable_titles_are_never_guessed(title):
    from job_agent.career_tracks import UNCLASSIFIED, classify_title, infer_track

    assert classify_title(title).track == UNCLASSIFIED
    assert infer_track(title) == ""


def test_every_track_has_classification_signals():
    """A track with no signals would be unreachable by title classification."""
    from job_agent.career_tracks import _TRACK_TOKENS

    assert set(_TRACK_TOKENS) == {track.key for track in CAREER_TRACKS}
    for key, tokens in _TRACK_TOKENS.items():
        assert tokens, f"{key} has no distinctive tokens"
