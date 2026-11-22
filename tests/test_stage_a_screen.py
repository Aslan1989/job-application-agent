"""Stage A title screen: keep ambiguity, drop only what is ruled out."""
from __future__ import annotations

import pytest

from job_agent.job_filter import is_entry_level_candidate, screen_title


@pytest.mark.parametrize("title", [
    "Software Engineer",
    "Software Engineer (m/w/d)",
    "Backend Developer",
    "Application Developer",
    "Anwendungsentwickler",
    "Softwareentwickler Java",
    "AI Engineer",
    "LLM Engineer (m/w/d)",
    "RAG Developer",
    "KI Entwickler",
    "Python Developer",
    "Automation Engineer",
    "Data Engineer",
    "SQL Entwickler",
    "DevOps Engineer",
    "Linux Systemadministrator",
    "C++ Entwickler",
    "Embedded Software Entwickler",
    "SAP ABAP Entwickler",
    "SAP Berater",
    "IT Consultant",
    "Technical Business Analyst",
    "Quereinsteiger IT",
    "Trainee Softwareentwicklung",
])
def test_generic_and_entry_level_titles_are_kept(title):
    """A missing "Junior" must never hide a relevant posting."""
    decision = screen_title(title)
    assert decision.keep, f"{title} dropped: {decision.reason}"


@pytest.mark.parametrize("title,reason", [
    ("Senior Java Developer", "senior"),
    ("Sr. Python Engineer", "senior"),
    ("Lead Backend Developer", "senior"),
    ("Principal Software Engineer", "senior"),
    ("Staff Engineer Platform", "senior"),
    ("Software Architect", "senior"),
    ("Head of Engineering", "senior"),
    ("Teamleiter IT", "senior"),
    ("IT-Leiter", "senior"),
    ("Erfahrener PHP Entwickler", "senior"),
    ("Java Entwickler mit 7 Jahren Erfahrung", "years"),
])
def test_clearly_senior_titles_are_excluded(title, reason):
    decision = screen_title(title)
    assert not decision.keep, f"{title} kept"
    assert reason in decision.reason or "senior" in decision.reason


@pytest.mark.parametrize("title", [
    "Werkstudent Softwareentwicklung",
    "Werkstudentin Data Science",
    "Working Student Backend",
    "Studentische Hilfskraft Informatik",
])
def test_enrolment_only_roles_are_excluded(title):
    """Traditional Werkstudent roles need enrolment proof the candidate lacks."""
    decision = screen_title(title)
    assert not decision.keep
    assert "enrolment" in decision.reason


@pytest.mark.parametrize("title", [
    "Praktikum Softwareentwicklung",
    "Praktikant IT (m/w/d)",
    "Internship Software Engineering",
    "IT Praktikum",
])
def test_internships_survive_stage_a_for_requirement_inspection(title):
    """An internship may be fine; only the advert text can decide."""
    decision = screen_title(title)
    assert decision.keep
    assert "verify enrolment" in decision.reason


@pytest.mark.parametrize("title", [
    "Pflegefachkraft",
    "LKW Fahrer",
    "Verkäufer Bäckerei",
    "",
])
def test_unrelated_titles_are_dropped(title):
    assert not is_entry_level_candidate(title)


def test_ambiguous_title_resolves_to_keep():
    """Neither senior nor obviously junior: fetch the advert and let AI decide."""
    for title in ("Softwareentwickler (m/w/d)", "Entwickler Backend",
                  "IT-Spezialist Anwendungen" .replace("Spezialist", "Mitarbeiter")):
        assert is_entry_level_candidate(title), title
