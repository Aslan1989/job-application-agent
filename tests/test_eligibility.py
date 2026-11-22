"""Deterministic eligibility pre-screen: unknown must never read as negative."""
from __future__ import annotations

import pytest

from job_agent.eligibility import (PrescreenStats, prescreen_description,
                                   verification_notes)

FILLER = ("Wir sind ein Team in Frankfurt am Main und arbeiten mit Docker, "
          "Linux und REST-Schnittstellen an unseren Produkten. ") * 3


def advert(body: str) -> str:
    return body + " " + FILLER


def test_plain_entry_level_advert_has_no_hard_condition():
    result = prescreen_description(advert(
        "Wir suchen Unterstuetzung fuer unsere Java-Entwicklung. "
        "Erste Erfahrungen mit Spring Boot sind von Vorteil."))
    assert result.eligible
    assert result.questions == ()
    assert result.summary == "no explicit hard condition found"


def test_werkstudent_requirement_blocks_deterministically():
    result = prescreen_description(advert(
        "Als Werkstudent unterstuetzt du unser Entwicklungsteam."))
    assert not result.eligible
    assert result.codes == ("enrolment_required",)
    assert "enrolment" in result.blockers[0]


def test_explicit_enrolment_proof_blocks():
    for body in ("Du bist an einer Hochschule immatrikuliert.",
                 "Eine gueltige Studienbescheinigung ist erforderlich.",
                 "You must be currently enrolled at a university."):
        assert not prescreen_description(advert(body)).eligible, body


def test_internship_without_enrolment_requirement_stays_eligible():
    """Requirement 1: internships are suitable unless enrolment is demanded."""
    result = prescreen_description(
        advert("Praktikum in der Softwareentwicklung. Ein Studium ist nicht "
               "notwendig, wir suchen motivierte Einsteiger und Quereinsteiger."),
        title="Praktikum Softwareentwicklung")
    assert result.eligible
    assert result.blockers == ()


def test_completed_degree_is_not_an_enrolment_requirement():
    """"Abgeschlossenes Studium" is the opposite of "currently enrolled"."""
    result = prescreen_description(advert(
        "Ein abgeschlossenes Studium der Informatik ist erforderlich."))
    assert result.eligible
    assert "degree_mandatory" in result.codes


def test_hard_conditions_become_questions_not_verdicts():
    result = prescreen_description(advert(
        "Sehr gute Deutschkenntnisse auf Niveau C1 sind notwendig. "
        "Mindestens 5 Jahre Berufserfahrung in Java. "
        "Eine Sicherheitsueberpruefung ist erforderlich. "
        "Ein Fuehrerschein wird vorausgesetzt."))
    # Still "eligible": these are verification questions for a human, not
    # proven failures, and none of them is the one ruled-out condition.
    assert result.eligible
    assert set(result.codes) >= {"german_above_b2", "senior_experience",
                                 "security_clearance", "driving_licence"}
    assert "verification question" in result.summary


def test_absent_statements_produce_no_findings():
    """No clearance, permit or language claim is ever invented."""
    result = prescreen_description(advert(
        "Du entwickelst mit PHP 8 und MariaDB in einem kleinen Team."))
    assert result.codes == ()
    assert result.questions == ()


def test_short_text_is_not_treated_as_a_rejection():
    result = prescreen_description("Zu kurz.")
    assert result.eligible
    assert result.codes == ("insufficient_text",)


def test_stats_and_note_aggregation():
    blocked = prescreen_description(advert("Werkstudent gesucht."))
    flagged = prescreen_description(advert("Deutsch C1 erforderlich."))
    clean = prescreen_description(advert("PHP und MariaDB im Team."))
    stats = PrescreenStats()
    for result in (blocked, flagged, clean):
        stats.record(result)
    assert (stats.examined, stats.blocked, stats.flagged) == (3, 1, 1)
    assert stats.codes["enrolment_required"] == 1
    assert len(verification_notes([flagged, flagged])) == 1


# --- v0.10 regression: Trainee roles wrongly blocked ----------------------
#
# Reproduced against live BA adverts on 2026-11-21: of 4 blocked postings, 3
# were false positives. German adverts routinely list "Erfahrung durch
# Praktika oder Werkstudententätigkeit" as *desirable prior experience*, and a
# bare `werkstudent` match treated that as an enrolment requirement.

TRAINEE_WITH_WERKSTUDENT_EXPERIENCE = advert(
    "Idealerweise erste Erfahrung durch Praktika oder Werkstudenten-Taetigkeit "
    "im IT- oder Banking-Umfeld. Du bist ein Teamplayer mit Eigeninitiative.")

TRAINEE_WITH_EXAMPLE_WERKSTUDENT = advert(
    "Erste praktische Erfahrung in einem Industrieunternehmen, beispielsweise "
    "durch ein Praktikum oder eine Werkstudententaetigkeit, sowie ein sicherer "
    "Umgang mit Microsoft-Anwendungen ergaenzen Ihr Profil.")


@pytest.mark.parametrize("title,body", [
    ("Trainee IT (m/w/d)", TRAINEE_WITH_WERKSTUDENT_EXPERIENCE),
    ("Trainee IT (m/w/d)", TRAINEE_WITH_EXAMPLE_WERKSTUDENT),
    ("Trainee Softwareentwicklung", advert(
        "Wir bieten Praktika, Werkstudentenstellen und Traineeprogramme an.")),
    ("Praktikum Forensic - Data Analytics (w/m/d)", advert(
        "Eine Werkstudententaetigkeit ist nur in Anschluss an ein Praktikum moeglich.")),
    ("Quereinsteiger IT", advert(
        "Vorerfahrung als Werkstudent ist von Vorteil, aber nicht erforderlich.")),
])
def test_prior_werkstudent_experience_is_not_an_enrolment_requirement(title, body):
    result = prescreen_description(body, title=title)
    assert result.eligible, f"false positive: {result.blockers}"
    assert "enrolment_required" not in result.codes


@pytest.mark.parametrize("title,body", [
    # The title names the role, which is decisive on its own.
    ("Werkstudent Informatik", advert("Du unterstuetzt unser Team.")),
    ("Praktikum / Werkstudent IT-Beratung (w/m/d)", advert("Wir suchen dich.")),
    ("Studentische Hilfskraft IT", advert("Spannende Aufgaben warten.")),
    # Or the body frames enrolment as a condition for this position.
    ("Softwareentwickler", advert("Als Werkstudent unterstuetzt du unser Team.")),
    ("Praktikum", advert("Eine gueltige Studienbescheinigung ist erforderlich.")),
    ("Praktikum", advert("Du bist an einer Hochschule immatrikuliert.")),
    ("Internship", advert("You must be currently enrolled at a university.")),
    ("Praktikum", advert("Ein Nachweis der Immatrikulation ist vorzulegen.")),
])
def test_genuine_enrolment_requirements_still_block(title, body):
    result = prescreen_description(body, title=title)
    assert not result.eligible, "a real enrolment requirement must block"
    assert result.codes == ("enrolment_required",)


def test_blocker_quotes_the_clause_it_relied_on():
    """A block must be auditable, so the stated reason carries the evidence."""
    result = prescreen_description(
        advert("Eine gueltige Studienbescheinigung ist erforderlich."),
        title="Praktikum")
    assert "Studienbescheinigung" in result.blockers[0]


def test_enrolment_helper_is_exposed_for_diagnosis():
    from job_agent.eligibility import enrolment_requirement
    assert enrolment_requirement(TRAINEE_WITH_WERKSTUDENT_EXPERIENCE,
                                 title="Trainee IT") == ""
    assert enrolment_requirement(advert("Als Werkstudent arbeitest du mit uns."),
                                 title="Entwickler")
