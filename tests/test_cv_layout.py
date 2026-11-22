"""CV layout and content quality (v0.13.1).

Every integration is mocked: no Google Sheets, no model, no network. What is
asserted here is what the user asked for and what must not regress:

- one legible A4 page by default, and never an almost-empty second page;
- a grammatical, position-specific headline instead of "Junior {focus}";
- no defensive phrasing anywhere in a document;
- consistent German, including project and employment descriptions;
- two variants, `modern` (optional Bewerbungsfoto) and `ats` (plain);
- the photo and the contact details stay out of every model prompt;
- presentation never changes a fact: both variants assert the same claims.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from job_agent.candidate_profile import load_candidate_profile
from job_agent.documents.contact import (ContactDetails, find_photo, inspect_photo,
                                         load_contact)
from job_agent.documents.content import (CATEGORY_LABELS, CATEGORY_ORDER,
                                         MAX_CATEGORIES,
                                         CvContent, CvEntry, CvSection, build_cv,
                                         clean_role, deterministic_letter,
                                         format_dates, format_period, skill_rows)
from job_agent.documents.evidence import select_evidence
from job_agent.documents.factcheck import check_claims, check_text
from job_agent.documents.layout import (ATS, MAX_COMFORTABLE_FILL, MIN_BODY_PT,
                                        MODERN, TIER_NAMES, LayoutReport,
                                        assess_pages, get_style, pdf_page_count)
from job_agent.documents.pipeline import generate_package
from job_agent.documents.render_docx import read_docx_text, render_cv
from job_agent.documents.render_pdf import render_cv_pdf

pytest.importorskip("docx")
pytest.importorskip("reportlab")

# A bank-IT trainee advert, the shape of the real Finanz Informatik vacancy:
# German, names Java/Javascript/CSS and Windows/Linux/AIX, no PHP.
TRAINEE_ADVERT = (
    "Mach mit uns das Banking einfacher und werde Teil unseres Teams in "
    "Frankfurt am Main als Trainee IT. Verschaffe dir als IT-Profi den "
    "Ueberblick in Bereichen wie Softwareentwicklung, Cyber Security und der "
    "Bereitstellung von IT-Systemen. Du erhaeltst Einblicke in spannende "
    "Programmiersprachen und Tools wie Java, Javascript und CSS sowie "
    "unterschiedliche Betriebssysteme (Windows, Linux, AIX), Datenbanken und "
    "Anwendungsserver. Ein abgeschlossenes Studium der Wirtschaftsinformatik "
    "ist von Vorteil; wir bieten eine strukturierte Einarbeitung.")

CPP_ADVERT = (
    "Wir suchen Unterstuetzung fuer systemnahe C- und C++-Entwicklung. Du "
    "arbeitest mit Prozessen, TCP-Sockets, Netzwerk-Programmierung und Bash "
    "unter Linux. Deutsch B2 genuegt und eigene Projekte sind als Nachweis "
    "ausreichend fuer diese Position.")

JAVA_ADVERT = (
    "We are looking for a backend engineer to join our team. You will work "
    "with Java, Spring Boot, JPA, Hibernate, SQL and Docker on our REST APIs, "
    "write unit tests with JUnit and use Git. We offer a hybrid model.")

PHP_ADVERT = (
    "Wir suchen Unterstuetzung fuer unsere Backend-Entwicklung. Du pflegst "
    "PHP-8-Anwendungen, entwickelst REST-Schnittstellen, arbeitest mit SQL und "
    "MariaDB und schreibst PHPUnit-Tests. Deutsch B2 ist ausreichend.")

AI_ADVERT = (
    "Wir entwickeln KI-Anwendungen und suchen Unterstuetzung fuer unsere "
    "LLM-Projekte. Du arbeitest mit RAG-Pipelines, Embeddings und Ollama, "
    "containerisiert mit Docker und nutzt Git. Deutsch B2 genuegt.")

GOOD_LETTER = {"paragraphs": [
    "ich bewerbe mich auf die Position als Trainee IT bei Finanz Informatik. "
    "Ihre Anzeige nennt Java, Linux und Datenbanken - genau die Technologien, "
    "mit denen ich in eigenen Projekten gearbeitet habe.",
    "Im Projekt Pizza In Progress habe ich eine REST-API mit Spring Boot und "
    "JPA aufgebaut und mit JUnit getestet. Das Projekt DokuChat zeigt meine "
    "Arbeit mit PHP, MariaDB und Docker. Beides sind eigene beziehungsweise "
    "Ausbildungsprojekte.",
    "Beruflich komme ich aus der Gastronomie und der Logistik. Dort habe ich "
    "Verantwortung fuer Ablaeufe, Teams und Termine uebernommen. Meine "
    "Programmierkenntnisse stammen aus der Ausbildung an der 42 Heilbronn und "
    "aus eigenen Projekten. Verfuegbar bin ich ab sofort.",
]}


def profile():
    return load_candidate_profile(Path("data/candidate_profile.json"))


def contact(**overrides) -> ContactDetails:
    base = dict(full_name="Test Candidate", email="test@example.invalid",
                phone="+49 000 0000000", city="Frankfurt am Main",
                street="Teststrasse 1", postal_code="60311",
                github_url="https://github.com/Aslan1989")
    return ContactDetails(**{**base, **overrides})


def evidence_for(advert=TRAINEE_ADVERT, track="graduate_entry", **kwargs):
    return select_evidence(profile(), job_id="FI-1", role="Trainee IT (m/w/d)",
                           company="Finanz Informatik", description=advert,
                           track=track, **kwargs)


def job(**overrides):
    base = {"job_id": "FI-1", "company": "Finanz Informatik",
            "role": "Trainee IT (m/w/d)", "location": "Frankfurt am Main",
            "track": "graduate_entry", "status": "Review", "score": 85,
            "description": TRAINEE_ADVERT}
    return {**base, **overrides}


class Model:
    """Mocked Gemini. Records every prompt so confidentiality can be asserted."""

    def __init__(self, reply=None):
        self.reply = reply if reply is not None else GOOD_LETTER
        self.messages: list = []

    def complete(self, messages):
        self.messages.append(messages)
        return dict(self.reply)


def placeholder_photo(path: Path, size=(600, 800), fmt="JPEG") -> Path:
    """A plain coloured rectangle. Never a face: nothing here depicts a person."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 205, 215)).save(path, fmt)
    return path


# --- 1. one legible page -------------------------------------------------


@pytest.mark.parametrize("style_name", ["modern", "ats"])
def test_a_junior_cv_fits_one_a4_page(tmp_path, style_name):
    cv = build_cv(evidence_for(), role="Trainee IT (m/w/d)")
    rendered = render_cv_pdf(cv, contact(), tmp_path / f"cv-{style_name}.pdf",
                             style=get_style(style_name))
    assert rendered.layout.pages == 1, rendered.layout.describe()
    assert not rendered.layout.warnings, rendered.layout.describe()
    assert pdf_page_count(rendered.path) == 1


def test_the_page_is_used_rather_than_half_empty(tmp_path):
    cv = build_cv(evidence_for(), role="Trainee IT (m/w/d)")
    rendered = render_cv_pdf(cv, contact(), tmp_path / "cv.pdf", style=MODERN)
    assert rendered.layout.last_page_fill > 0.5, (
        "a one-page CV that only fills a third of the page wastes it: "
        + rendered.layout.describe())


def test_compaction_is_tried_before_a_second_page_is_accepted(tmp_path):
    """Too much content compacts once; it never shrinks below the floor."""
    evidence = evidence_for()
    crowded = build_cv(evidence, role="Trainee IT (m/w/d)", max_projects=6)
    crowded = CvContent(
        language=crowded.language, headline=crowded.headline,
        subline=crowded.subline, summary=crowded.summary * 3,
        availability_line=crowded.availability_line,
        sections=crowded.sections * 2, variant=crowded.variant)
    rendered = render_cv_pdf(crowded, contact(), tmp_path / "crowded.pdf",
                             style=MODERN)
    assert rendered.layout.attempts[0].startswith("regular")
    assert MODERN.compacted().body_pt >= MIN_BODY_PT
    assert rendered.layout.pages >= 1


def test_the_roomiest_tier_that_fits_comfortably_is_chosen(tmp_path):
    """Not the tightest that fits, and not one packed to the bottom margin."""
    cv = build_cv(evidence_for(), role="Trainee IT (m/w/d)")
    rendered = render_cv_pdf(cv, contact(), tmp_path / "cv.pdf", style=ATS)
    assert rendered.layout.pages == 1
    assert rendered.layout.last_page_fill <= MAX_COMFORTABLE_FILL, (
        "a page with no slack spills when Word repaginates the DOCX: "
        + rendered.layout.describe())
    # The tier used is the first on the ladder that fits one page *with slack*.
    # A tier that fits but fills the page to the edge is recorded and skipped,
    # because the DOCX repaginates in Word and would spill.
    def parse(attempt):
        tier, _, rest = attempt.partition(":")
        pages, _, fill = rest.partition("@")
        return tier, int(pages.rstrip("p")), float(fill.rstrip("%")) / 100

    walked = [parse(item) for item in rendered.layout.attempts if "@" in item]
    comfortable = [tier for tier, pages, fill in walked
                   if pages == 1 and fill <= MAX_COMFORTABLE_FILL]
    assert comfortable, rendered.layout.describe()
    assert rendered.layout.tier == comfortable[0], rendered.layout.describe()
    assert [tier for tier, _, _ in walked] == list(TIER_NAMES[:len(walked)]), \
        "the ladder must be walked roomiest-first"


def test_the_tier_ladder_runs_roomiest_to_tightest():
    tiers = MODERN.tiers()
    assert [tier.tier for tier in tiers] == list(TIER_NAMES)
    sizes = [tier.body_pt for tier in tiers]
    assert sizes == sorted(sizes, reverse=True), "each tier is tighter than the last"
    assert min(sizes) >= MIN_BODY_PT, "legibility floor is never crossed"
    assert max(sizes) <= 11.0, "body text stays in the 10-11pt range"
    spacing = [tier.section_before for tier in tiers]
    assert spacing == sorted(spacing, reverse=True)
    assert MODERN.compacted(0) is MODERN


def test_a_cv_that_cannot_fit_keeps_the_readable_layout(tmp_path):
    """Requirement 15: a balanced long CV beats unreadably small text."""
    section = CvSection(heading="Projekte", entries=tuple(
        CvEntry(title=f"Projekt {index}", subtitle="x" * 60,
                bullets=("Beschreibung " * 12,))
        for index in range(40)))
    huge = CvContent(language="de", headline="Trainee IT", subline="Schwerpunkt",
                     summary="Zusammenfassung. " * 40, sections=(section,),
                     variant="t:de")
    rendered = render_cv_pdf(huge, contact(), tmp_path / "huge.pdf", style=MODERN)
    assert rendered.layout.pages > 2
    assert rendered.layout.tier == "regular", \
        "a hopeless case must not be shrunk; it is reported instead"
    assert any("should not exceed" in item for item in rendered.layout.warnings)


def test_page_assessment_flags_an_almost_empty_last_page():
    assert assess_pages(1, 0.95) == ()
    assert any("accidental overflow" in item for item in assess_pages(2, 0.08))
    assert assess_pages(2, 0.70) == (), "a genuinely used second page is fine"
    assert any("should not exceed" in item for item in assess_pages(3, 0.9))
    assert any("could not be verified" in item for item in assess_pages(0, 0.0))


def test_page_count_is_read_back_off_the_file(tmp_path):
    cv = build_cv(evidence_for(), role="Trainee IT")
    rendered = render_cv_pdf(cv, contact(), tmp_path / "cv.pdf", style=ATS)
    assert pdf_page_count(rendered.path) == rendered.layout.pages
    assert pdf_page_count(tmp_path / "absent.pdf") == 0
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 nonsense")
    assert pdf_page_count(broken) == 0


# --- 2. headline and wording ---------------------------------------------


def test_the_headline_is_the_position_not_junior_plus_a_noun():
    cv = build_cv(evidence_for(), role="Trainee IT (m/w/d)")
    assert cv.headline == "Trainee IT"
    assert not cv.headline.startswith("Junior "), \
        "'Junior Softwareentwicklung' was ungrammatical; the role replaced it"
    assert cv.subline == ("Wirtschaftsinformatiker · Schwerpunkt "
                          "Softwareentwicklung und IT-Betrieb")


@pytest.mark.parametrize("raw,expected", [
    ("Trainee IT (m/w/d)", "Trainee IT"),
    ("Junior Java Developer (w/m/d)", "Junior Java Developer"),
    ("Softwareentwickler m/w/d", "Softwareentwickler"),
    ("Backend Engineer (all genders)", "Backend Engineer"),
    ("DevOps Engineer (m/f/x)", "DevOps Engineer"),
    ("IT-Consultant (gn)", "IT-Consultant"),
])
def test_gender_markers_are_stripped_from_the_headline(raw, expected):
    assert clean_role(raw) == expected


def test_a_vacancy_without_a_title_still_gets_a_grammatical_headline():
    cv = build_cv(evidence_for(), role="")
    assert cv.headline == "Schwerpunkt Softwareentwicklung und IT-Betrieb"


def test_no_defensive_phrasing_in_the_cv_or_the_letter():
    evidence = evidence_for()
    cv = build_cv(evidence, role="Trainee IT (m/w/d)")
    letter = deterministic_letter(evidence, company="Finanz Informatik",
                                  role="Trainee IT (m/w/d)")
    text = " ".join([cv.summary, cv.headline, cv.subline, *cv.all_claims(),
                     *(section.heading for section in cv.sections),
                     *letter.paragraphs])
    for phrase in ("Keine kommerzielle", "keine kommerzielle",
                   "nicht angestellt", "habe ich nicht als",
                   "Leider", "fehlt mir", "no commercial"):
        assert phrase not in text, f"defensive phrasing present: {phrase}"


def test_defensive_phrasing_from_a_model_is_warned_about_not_blocked():
    result = check_text(
        "Bei Finanz Informatik als Trainee IT bringe ich Java mit. Keine "
        "kommerzielle Tätigkeit als Entwickler liegt vor.",
        evidence_for(), company="Finanz Informatik", role="Trainee IT")
    assert result.ok, "a style problem must not discard an otherwise true letter"
    assert "defensive_phrasing" in {item.code for item in result.warnings}


def test_the_letter_subject_uses_the_printable_role():
    letter = deterministic_letter(evidence_for(), company="Finanz Informatik",
                                  role="Trainee IT (m/w/d)")
    assert letter.subject == "Bewerbung als Trainee IT"
    assert "Trainee IT (m/w/d)" not in letter.text, \
        "the body must spell the role the way the subject line does"
    assert "als Trainee IT bei Finanz Informatik" in letter.text


def test_the_letter_lists_requirements_grammatically():
    """"Themen Java, Linux deckt ... ab" was wrong in both list and verb."""
    letter = deterministic_letter(evidence_for(), company="Finanz Informatik",
                                  role="Trainee IT (m/w/d)")
    body = letter.text
    assert "Diese Projektarbeit deckt" in body, body
    assert "deckt diese Projektarbeit ab" not in body, "wrong verb agreement"
    found = re.search(r"Diese Projektarbeit deckt[^.]*\.", body)
    assert found, body
    sentence = found.group(0)
    assert sentence.endswith(" ab."), f"the verb must close the clause: {sentence}"
    if "," in sentence:
        assert " und " in sentence, \
            f"a list must join its last item with 'und': {sentence}"


# --- 3. consistent German ------------------------------------------------


def test_a_german_cv_uses_the_german_evidence_text():
    cv = build_cv(evidence_for(), role="Trainee IT")
    claims = " ".join(cv.all_claims())
    # The German renderings from the profile, not the English originals.
    assert "REST-API mit Spring Boot" in claims
    assert "Spring Boot REST API with JPA/Hibernate" not in claims
    assert "Digitale Paketerfassung" in claims
    assert "Digital parcel scanning" not in claims
    for english in (" with ", " and ", "project,"):
        assert english not in claims, f"English fragment in a German CV: {english}"


def test_german_evidence_text_still_passes_the_fact_check():
    """A fully German CV must not read as unverified: `*_de` is profile data."""
    evidence = evidence_for()
    cv = build_cv(evidence, role="Trainee IT")
    result = check_claims(cv.all_claims(), evidence)
    assert result.ok, [str(item) for item in result.findings]


def test_locations_and_titles_are_german_in_a_german_cv():
    cv = build_cv(evidence_for(), role="Trainee IT")
    experience = next(section for section in cv.sections
                      if section.heading == "Berufserfahrung")
    rendered = " ".join(f"{entry.title} {entry.subtitle}"
                        for entry in experience.entries)
    assert "Moskau, Russland" in rendered and "Moscow" not in rendered
    assert "Machatschkala" in rendered and "Makhachkala" not in rendered
    assert "Zusteller / Logistik" in rendered
    assert "Parcel delivery" not in rendered


def test_an_english_cv_keeps_the_english_text():
    english = select_evidence(
        profile(), job_id="EN-1", role="Backend Engineer", company="ACME",
        description="We are looking for a backend engineer to work with Java, "
                    "Spring Boot, SQL and Docker on our REST APIs and write "
                    "unit tests with JUnit. You will join an experienced team.",
        track="java_backend")
    cv = build_cv(english, role="Backend Engineer")
    assert cv.language == "en"
    assert cv.sections[0].heading == "Technical skills"
    assert "Spring Boot REST API with JPA/Hibernate" in " ".join(cv.all_claims())


@pytest.mark.parametrize("start,end,expected", [
    ("2017-10", "2021-11", "10/2017 – 11/2021"),
    ("2022-02", "", "02/2022 – heute"),
    ("2012-08", "2017-08", "08/2012 – 08/2017"),
])
def test_periods_use_the_german_date_convention(start, end, expected):
    assert format_period(start, end, "de") == expected


@pytest.mark.parametrize("raw,expected", [
    ("2007-07 to 2012-06", "07/2007 – 06/2012"),
    ("since 2025-01", "seit 01/2025"),
    ("2022-05 to 2023-03", "05/2022 – 03/2023"),
])
def test_free_text_dates_are_normalised(raw, expected):
    assert format_dates(raw, "de") == expected


def test_an_unparsable_date_is_passed_through_not_invented():
    assert format_dates("Wintersemester 2024", "de") == "Wintersemester 2024"


# --- 4. content quality --------------------------------------------------


def test_only_the_most_relevant_projects_are_shown():
    cv = build_cv(evidence_for(), role="Trainee IT")
    projects = next(section for section in cv.sections
                    if "Projekte" in section.heading)
    assert 2 <= len(projects.entries) <= 3
    names = [entry.title for entry in projects.entries]
    assert "Pizza In Progress" in names, "a Java advert must surface the Java work"
    assert len(names) == len(set(names))


def test_the_project_count_is_configurable_within_bounds():
    assert len(build_cv(evidence_for(max_projects=6), role="x",
                        max_projects=2).sections[1].entries) == 2
    with pytest.raises(ValueError, match="max_projects"):
        generate_package(job=job(), profile=profile(), provider=None,
                         contact=contact(), write_files=False, max_projects=99)


def test_skills_are_grouped_into_compact_categories():
    evidence = evidence_for()
    cv = build_cv(evidence, role="Trainee IT")
    skills = next(section for section in cv.sections
                  if section.heading == "Technische Kenntnisse")
    assert skills.rows and not skills.bullets, "skills are rows, not a long list"
    assert len(skills.rows) <= MAX_CATEGORIES, "one CV is a selection, not the lot"
    labels = [label for label, _ in skills.rows]
    assert len(labels) == len(set(labels))
    assert set(labels) <= set(CATEGORY_LABELS["de"].values())

    # Uncapped, every technology the profile evidences is placed and printed.
    # The cap is what makes one CV vacancy-specific; it never shrinks the
    # inventory, which lives in the profile.
    printed = " ".join(value for _, value in skill_rows(evidence, "de",
                                                        max_categories=0))
    for skill in evidence.skills:
        shown = skill.aligned("de")[:1] if skill.private_backed else skill.aligned("de")
        for _, fragment in shown:
            assert fragment in printed, f"{fragment} was lost in grouping"


def test_c_and_cpp_survive_a_java_or_php_vacancy():
    """The inventory is never narrowed to the vacancy's own language."""
    for advert, track in [(TRAINEE_ADVERT, "graduate_entry"),
                          (PHP_ADVERT, "php_web"),
                          (JAVA_ADVERT, "java_backend"),
                          (AI_ADVERT, "ai_engineering")]:
        evidence = evidence_for(advert, track=track)
        rows = skill_rows(evidence, "de", track=track)
        printed = " ".join(value for _, value in rows)
        # `languages` is protected, so these survive every vacancy however it
        # is ranked -- that is the whole point of the protection.
        for technology in ("C", "C++"):
            assert technology in printed, \
                f"{technology} was discarded for a {track} vacancy"
        # And the rest of the inventory is never lost, only deselected.
        uncapped = " ".join(value for _, value in
                            skill_rows(evidence, "de", track=track, max_categories=0))
        for technology in ("Linux", "Prozesse und Pipes", "HTTP", "TCP-Sockets",
                           "Konfigurationsparser"):
            assert technology in uncapped, f"{technology} left the inventory"


def test_grouping_invents_no_competency():
    """Every printed item is a technology the profile's own skills name."""
    evidence = evidence_for()
    allowed = {fragment.strip()
               for skill in evidence.skills
               for name in (skill.name, skill.name_de)
               for fragment in str(name).split("/") if fragment.strip()}
    for _, value in skill_rows(evidence, "de", max_categories=0):
        for item in value.split("  ·  "):
            assert item in allowed, f"'{item}' is not a profile technology"


def test_every_profile_fragment_has_a_category():
    """An unmapped technology must fall back, never vanish."""
    evidence = evidence_for()
    rows = skill_rows(evidence, "de", max_categories=0)
    placed = {item for _, value in rows for item in value.split("  ·  ")}
    for skill in evidence.skills:
        shown = skill.aligned("de")[:1] if skill.private_backed else skill.aligned("de")
        for _, fragment in shown:
            assert fragment in placed, f"{fragment} was not placed"
    assert CATEGORY_LABELS["de"]["other"] not in [label for label, _ in rows], \
        "every technology in this profile has an explicit category"


@pytest.mark.parametrize("advert,track,expected_top_two", [
    (CPP_ADVERT, "cpp_systems",
     ["Systemprogrammierung & Netzwerke", "Programmiersprachen"]),
    (JAVA_ADVERT, "java_backend",
     ["Backend & Frameworks", "Programmiersprachen"]),
    # A PHP vacancy leads with the language and then the databases it names,
    # not with "Backend & Frameworks": the advert asks for PHP 8, SQL, MariaDB
    # and PHPUnit, while this profile's backend bundle is Spring-centric, so
    # leading with it would put Java work first in a PHP application. The
    # advert's own technologies outrank the track emphasis by design.
    (PHP_ADVERT, "php_web",
     ["Programmiersprachen", "Datenbanken"]),
    (AI_ADVERT, "ai_engineering", ["KI & LLM", "DevOps & Werkzeuge"]),
    (TRAINEE_ADVERT, "graduate_entry",
     ["Programmiersprachen", "Systemprogrammierung & Netzwerke"]),
])
def test_category_order_follows_the_vacancy(advert, track, expected_top_two):
    rows = skill_rows(evidence_for(advert, track=track), "de", track=track)
    assert [label for label, _ in rows[:2]] == expected_top_two, \
        [label for label, _ in rows]


def test_a_systems_vacancy_leads_with_systems_programming():
    """The clearest case: a C/C++ advert must not open with Java or PHP."""
    rows = skill_rows(evidence_for(CPP_ADVERT, track="cpp_systems"), "de",
                      track="cpp_systems")
    assert rows[0][0] == "Systemprogrammierung & Netzwerke"
    assert rows[1][0] == "Programmiersprachen"
    assert rows[1][1].split("  ·  ")[:2] == ["C", "C++"]


def test_two_vacancies_get_genuinely_different_skill_content():
    """Adaptation is selection, not just reshuffling the same rows.

    The profile inventory is untouched -- uncapped, both vacancies can still
    reach every technology -- but the CV each one gets differs in *content*.
    """
    cpp_evidence = evidence_for(CPP_ADVERT, track="cpp_systems")
    java_evidence = evidence_for(JAVA_ADVERT, track="java_backend")
    cpp = skill_rows(cpp_evidence, "de", track="cpp_systems")
    java = skill_rows(java_evidence, "de", track="java_backend")

    def technologies(rows):
        return {item for _, value in rows for item in value.split("  ·  ")}

    assert [label for label, _ in cpp] != [label for label, _ in java], \
        "the categories shown must respond to the vacancy"
    assert technologies(cpp) != technologies(java), \
        "and so must the technologies, or this is only a reordering"
    # Neither loses C/C++, and neither loses access to the full inventory.
    for rows in (cpp, java):
        assert {"C", "C++"} <= technologies(rows)
    assert technologies(skill_rows(cpp_evidence, "de", max_categories=0)) == \
        technologies(skill_rows(java_evidence, "de", max_categories=0)), \
        "the inventory itself is the same; only the selection differs"


def test_the_leading_technology_in_a_category_follows_the_vacancy():
    cpp = dict(skill_rows(evidence_for(CPP_ADVERT, track="cpp_systems"), "de",
                          track="cpp_systems"))
    java = dict(skill_rows(evidence_for(JAVA_ADVERT, track="java_backend"), "de",
                           track="java_backend"))
    assert cpp["Programmiersprachen"].startswith("C "), cpp["Programmiersprachen"]
    assert java["Programmiersprachen"].startswith("Java 17"), \
        java["Programmiersprachen"]


def test_a_bundles_technologies_stay_together_in_a_category():
    """A category reads as a list, not an interleaving of two skill bundles."""
    rows = dict(skill_rows(evidence_for(), "de", max_categories=0))
    devops = rows["DevOps & Werkzeuge"].split("  ·  ")
    docker = [devops.index(name) for name in
              ("Docker", "Docker Compose", "NGINX", "PHP-FPM")]
    assert docker == sorted(docker) and max(docker) - min(docker) == 3


def test_an_unevidenced_technology_is_never_printed():
    """Unevidenced is still unevidenced. SAP/ABAP has no evidence at all."""
    printed = " ".join(value for _, value in
                       skill_rows(evidence_for(), "de", max_categories=0))
    for forbidden in ("ABAP", "SAP"):
        assert forbidden not in printed, \
            f"{forbidden} has no evidence of any kind and must not be printed"


def test_a_privately_evidenced_skill_is_listed_but_never_cited():
    """Python is evidenced by a real project the candidate keeps private.

    Dropping it understated genuine ability. It is listed, the repository is
    never named, its unverifiable helper libraries are left off, and it does
    not enter a model prompt.
    """
    evidence = evidence_for()
    python = next(skill for skill in evidence.skills
                  if skill.name.startswith("Python"))
    assert python.private_backed is True
    assert python.sources == (), "a private repository is not a citable source"

    printed = " ".join(value for _, value in
                       skill_rows(evidence, "de", max_categories=0))
    assert "Python" in printed, "a privately evidenced skill is still a skill"
    for noise in ("OSMnx", "NetworkX", "Streamlit"):
        assert noise not in printed, \
            f"{noise} has no citable project; only the headline technology is listed"
    for secret in ("greenroute", "GreenRoute", "webserv"):
        assert secret not in printed, f"private repository '{secret}' disclosed"

    # And it is absent from the only shape that may reach a model.
    payload = json.dumps(evidence.to_prompt_payload(), ensure_ascii=False)
    assert "Python" not in payload, \
        "a skill with no citable provenance must not be offered to a model"
    assert "greenroute" not in payload.casefold()


def test_experience_is_not_labelled_non_technical():
    cv = build_cv(evidence_for(), role="Trainee IT")
    headings = [section.heading for section in cv.sections]
    assert "Berufserfahrung" in headings
    assert "Berufserfahrung (nicht-technisch)" not in headings


def test_availability_is_a_header_line_not_a_lonely_section():
    cv = build_cv(evidence_for(), role="Trainee IT")
    assert cv.availability_line == "Verfügbar ab sofort"
    assert "Verfügbarkeit" not in [section.heading for section in cv.sections]


def test_real_dates_and_employers_are_preserved():
    cv = build_cv(evidence_for(), role="Trainee IT")
    experience = next(section for section in cv.sections
                      if section.heading == "Berufserfahrung")
    periods = {entry.period for entry in experience.entries}
    assert "02/2022 – 08/2024" in periods
    assert "10/2017 – 11/2021" in periods
    employers = " ".join(entry.subtitle for entry in experience.entries)
    assert "GLS" in employers and "Restaurant Garden" in employers
    for bank in ("Tinkoff", "Sberbank"):
        assert bank.casefold() not in employers.casefold()


# --- 5. the two variants -------------------------------------------------


def test_both_variants_are_produced_with_distinct_filenames(tmp_path):
    package = generate_package(job=job(), profile=profile(), provider=Model(),
                               contact=contact(), output_dir=tmp_path,
                               styles=["modern", "ats"])
    assert package.cv_docx is not None and "ATS" not in package.cv_docx.name
    docx_ats, pdf_ats = package.alternates["ats"]
    assert "ATS" in docx_ats.name and "ATS" in pdf_ats.name
    for path in package.files:
        assert path.is_file() and path.stat().st_size > 1000
    assert set(package.layouts) == {"modern", "ats"}
    assert package.report.cv_styles == ("modern", "ats")
    assert package.report.documents["cv_docx_ats"] == str(docx_ats)


def test_the_primary_style_is_the_one_a_tracker_reference_would_use(tmp_path):
    package = generate_package(job=job(), profile=profile(), provider=None,
                               contact=contact(), output_dir=tmp_path,
                               styles=["ats", "modern"])
    assert package.layouts["ats"].style == "ats"
    assert "ATS" not in package.cv_docx.name, \
        "the first requested style keeps the plain filename"
    assert "modern" in package.alternates


def test_the_ats_variant_has_no_photo_and_no_tables(tmp_path):
    photo = placeholder_photo(tmp_path / "photo.jpg")
    details = contact(photo_path=str(photo))
    cv = build_cv(evidence_for(), role="Trainee IT")
    from docx import Document

    ats_path = render_cv(cv, details, tmp_path / "ats.docx", style=ATS)
    document = Document(str(ats_path))
    assert document.tables == [], "an ATS CV must be a single column of text"
    assert not document.inline_shapes, "an ATS CV carries no image"
    assert ATS.allow_photo is False


def test_the_modern_variant_places_the_photo_without_altering_it(tmp_path):
    photo = placeholder_photo(tmp_path / "photo.jpg", size=(600, 800))
    before = photo.read_bytes()
    details = contact(photo_path=str(photo))
    cv = build_cv(evidence_for(), role="Trainee IT")
    from docx import Document

    modern_path = render_cv(cv, details, tmp_path / "modern.docx", style=MODERN)
    document = Document(str(modern_path))
    assert len(document.tables) == 1, "the header is one borderless layout table"
    assert len(document.inline_shapes) == 1, "the Bewerbungsfoto is embedded once"
    shape = document.inline_shapes[0]
    ratio = shape.width / shape.height
    assert abs(ratio - 600 / 800) < 0.02, "the photo keeps its aspect ratio"
    assert photo.read_bytes() == before, "the source image must never be rewritten"

    rendered = render_cv_pdf(cv, details, tmp_path / "modern.pdf", style=MODERN)
    assert rendered.layout.photo is True
    assert photo.read_bytes() == before


def test_both_variants_carry_the_same_text(tmp_path):
    details = contact()
    cv = build_cv(evidence_for(), role="Trainee IT (m/w/d)")
    modern = read_docx_text(render_cv(cv, details, tmp_path / "m.docx", style=MODERN))
    ats = read_docx_text(render_cv(cv, details, tmp_path / "a.docx", style=ATS))

    def words(text):
        return set(text.replace("\t", " ").split())

    assert words(modern) == words(ats), \
        "a style is presentation; it must not add or drop a single fact"


def test_an_unknown_style_is_refused(tmp_path):
    with pytest.raises(ValueError, match="Unknown CV style"):
        get_style("fancy")
    with pytest.raises(ValueError, match="Unknown CV style"):
        generate_package(job=job(), profile=profile(), provider=None,
                         contact=contact(), output_dir=tmp_path, styles=["fancy"])


# --- 6. the photo stays private -----------------------------------------


def test_the_photo_never_reaches_the_model_prompt(tmp_path):
    photo = placeholder_photo(tmp_path / "profile_photo.jpg")
    model = Model()
    package = generate_package(job=job(), profile=profile(), provider=model,
                               contact=contact(photo_path=str(photo)),
                               output_dir=tmp_path, styles=["modern"])
    assert package.layouts["modern"].photo is True
    prompt = " ".join(part["content"] for part in model.messages[0])
    for secret in (str(photo), "profile_photo", "photo", ".jpg",
                   "test@example.invalid", "+49 000 0000000", "Teststrasse"):
        assert secret not in prompt, f"'{secret}' leaked into the prompt"


def test_content_building_cannot_see_the_photo():
    """The structural guarantee: the prompt builder never imports contact.py."""
    import job_agent.documents.content as content

    source = Path(content.__file__).read_text(encoding="utf-8")
    assert "from .contact" not in source and "import contact" not in source
    assert not hasattr(content, "ContactDetails")


def test_a_missing_photo_is_reported_and_does_not_stop_generation(tmp_path):
    package = generate_package(job=job(), profile=profile(), provider=None,
                               contact=contact(photo_note="No Bewerbungsfoto found."),
                               output_dir=tmp_path, styles=["modern"])
    assert package.cv_pdf.is_file()
    assert package.layouts["modern"].photo is False
    assert any("Bewerbungsfoto" in item
               for item in package.report.verify_before_sending)


def test_an_unusable_photo_is_rejected_with_a_reason(tmp_path):
    tiny = placeholder_photo(tmp_path / "tiny.jpg", size=(40, 50))
    found, note = inspect_photo(tiny)
    assert found == "" and "too small" in note

    not_an_image = tmp_path / "broken.jpg"
    not_an_image.write_bytes(b"this is not a JPEG")
    found, note = inspect_photo(not_an_image)
    assert found == "" and "could not be read" in note

    landscape = placeholder_photo(tmp_path / "wide.jpg", size=(800, 500))
    found, note = inspect_photo(landscape)
    assert found == str(landscape) and "landscape" in note, \
        "a landscape photo is used uncropped and reported, never cut to fit"


def test_the_photo_is_discovered_locally_and_can_be_switched_off(tmp_path):
    photo = placeholder_photo(tmp_path / "profile_photo.jpg")
    assert find_photo(photo)[0] == str(photo)
    assert find_photo(photo, enabled=False) == ("", "")
    missing, note = find_photo(tmp_path / "nope.jpg")
    assert missing == "" and "No photo at" in note

    details = load_contact(tmp_path / "contact.json", fallback_name="X",
                           photo=False)
    assert details.photo_path == "" and details.has_photo is False


def test_a_complete_contact_record_without_a_photo_is_still_complete(tmp_path):
    path = tmp_path / "contact.json"
    path.write_text('{"full_name": "A B", "email": "a@b.example", '
                    '"phone": "+49 1", "city": "Frankfurt am Main"}',
                    encoding="utf-8")
    # An explicit absent path, so the result does not depend on whether this
    # machine happens to have a photo in data/.
    details = load_contact(path, photo_path=tmp_path / "no-such-photo.jpg")
    assert details.complete and details.verification_notes() == [], \
        "an optional photo must not read as missing contact data"
    assert "no-such-photo" in details.photo_note, \
        "the absent photo is still mentioned, separately"


# --- 7. the report ------------------------------------------------------


def test_the_report_states_the_page_count_and_the_style(tmp_path):
    package = generate_package(job=job(), profile=profile(), provider=Model(),
                               contact=contact(), output_dir=tmp_path,
                               styles=["modern", "ats"])
    text = package.report.render_text()
    assert "styles: modern, ats" in text
    assert "1 page(s)" in text
    assert "last page" in text
    assert package.report.approved is False, "layout work is never an approval"
    assert any("page(s)" in item for item in package.report.layouts)
    checks = " ".join(package.report.document_checks)
    assert "CV (modern) PDF readable" in checks and "CV (ats) PDF readable" in checks


def test_a_layout_warning_reaches_the_human_checklist():
    report = LayoutReport(style="modern", tier="regular", pages=3,
                          last_page_fill=0.05,
                          warnings=("3 pages: a junior CV should not exceed 2",))
    assert report.ok is False
    assert "3 page(s)" in report.describe()


def test_a_dry_run_still_writes_nothing(tmp_path):
    package = generate_package(job=job(), profile=profile(), provider=None,
                               contact=contact(), output_dir=tmp_path,
                               styles=["modern", "ats"], write_files=False)
    assert package.files == []
    assert package.alternates == {}
    assert list(tmp_path.iterdir()) == []


# --- 8. German terminology, education order, ZAB recognition -------------


@pytest.mark.parametrize("english,german", [
    ("Processes", "Prozesse und Pipes"),
    ("Configuration Parsing", "Konfigurationsparser"),
    ("Security", "Spring Security"),
    ("OOP", "Objektorientierung"),
    ("TCP Sockets", "TCP-Sockets"),
])
def test_a_german_cv_prints_german_skill_terminology(english, german):
    """The awkward English labels are gone, from the profile's own `name_de`."""
    evidence = evidence_for()
    printed = " ".join(value for _, value in
                       skill_rows(evidence, "de", max_categories=0))
    assert german in printed
    assert english not in printed.split("  ·  "), \
        f"the English label '{english}' is still being printed"


def test_an_english_cv_keeps_the_english_skill_terminology():
    english = select_evidence(
        profile(), job_id="EN-1", role="Backend Engineer", company="ACME",
        description="We need Java, Spring Boot, SQL, Docker and JUnit for our "
                    "REST APIs. You will join an experienced engineering team.",
        track="java_backend")
    printed = " ".join(value for _, value in
                       skill_rows(english, "en", max_categories=0))
    assert "Spring Security" in printed or "Security" in printed
    assert "Objektorientierung" not in printed
    assert "Konfigurationsparser" not in printed


def test_a_misaligned_german_name_falls_back_instead_of_mislabelling():
    """An unaligned `name_de` would silently rename the wrong technology."""
    from job_agent.documents.evidence import SkillEvidence

    good = SkillEvidence(name="A / B / C", sources=("cv_x",), tracks=(),
                         confidence="x", name_de="A / B-de / C-de")
    assert good.named("de") == "A / B-de / C-de"
    assert good.aligned("de") == (("A", "A"), ("B", "B-de"), ("C", "C-de"))

    bad = SkillEvidence(name="A / B / C", sources=("cv_x",), tracks=(),
                        confidence="x", name_de="A / B-de")
    assert bad.named("de") == "A / B / C", "a mismatched pair must not be used"
    assert bad.aligned("de") == (("A", "A"), ("B", "B"), ("C", "C"))

    none = SkillEvidence(name="A / B", sources=("cv_x",), tracks=(), confidence="x")
    assert none.named("de") == "A / B"


def test_education_is_reverse_chronological_with_the_current_study_first():
    evidence = evidence_for()
    assert [item.started for item in evidence.education] == \
        sorted((item.started for item in evidence.education), reverse=True)
    cv = build_cv(evidence, role="Trainee IT")
    education = next(section for section in cv.sections
                     if section.heading == "Ausbildung")
    titles = [entry.title for entry in education.entries]
    assert titles[0] == "Software Engineering", titles
    assert education.entries[0].subtitle == "42 Heilbronn"
    assert titles[-1].startswith("Wirtschaftsinformatik"), titles
    assert education.entries[0].period == "seit 01/2025"


def test_the_professional_qualifier_survives_the_reordering():
    """The subline must still name the degree, not whatever sorts first."""
    cv = build_cv(evidence_for(), role="Trainee IT")
    assert cv.subline.startswith("Wirtschaftsinformatiker · "), cv.subline


def test_zab_recognition_is_shown_and_the_internal_note_is_not():
    """Item 9: shown because the profile evidences it; no degree title claimed."""
    evidence = evidence_for()
    degree = next(item for item in evidence.education if item.is_academic_degree)
    assert "ZAB" in degree.shown_note("de")
    assert "Hochschulabschluss" in degree.shown_note("de")

    cv = build_cv(evidence, role="Trainee IT")
    claims = " ".join(cv.all_claims())
    assert "ZAB-Bewertung" in claims
    # The internal `note` is an instruction to the generator, not CV text.
    assert "do not claim" not in claims
    assert degree.note not in claims
    assert check_claims(cv.all_claims(), evidence).ok


def test_no_particular_german_degree_title_is_claimed():
    cv = build_cv(evidence_for(), role="Trainee IT")
    text = " ".join([cv.summary, cv.subline, cv.headline, *cv.all_claims()])
    for forbidden in ("Bachelor", "Master", "Diplom", "B.Sc", "M.Sc", "Magister"):
        assert forbidden not in text, f"claimed a German degree title: {forbidden}"


# --- 9. the two named vacancies -----------------------------------------

DATACENTER_ADVERT = (
    "Starte als IT-Consultant im Bereich Datacenter durch. Moderne "
    "DevOps-Architekturen zur automatisierten Bereitstellung von "
    "Rechenzentrumsplattformen, Integration von Datacenter-Infrastrukturen in "
    "Multicloud-Umgebungen, Effizienzoptimierung von IT-Betriebsprozessen "
    "mittels Automatisierung, Aufbau von Big Data und KI-Infrastrukturloesungen. "
    "Dein Profil: relevante IT-(Hochschul-)Ausbildung, idealerweise erste "
    "Erfahrungen mit Scripting sowie mit Linux, Server- oder "
    "Storage-Technologien, sehr gute Deutsch- und Englischkenntnisse.")

BANK_IT_ADVERT = (
    "Werde Teil unseres Teams in Frankfurt am Main als Trainee IT. Verschaffe "
    "dir den Ueberblick in Bereichen wie Softwareentwicklung, Cyber Security "
    "und der Bereitstellung von IT-Systemen. Du erhaeltst Einblicke in "
    "Programmiersprachen und Tools wie Java, Javascript und CSS sowie "
    "unterschiedliche Betriebssysteme (Windows, Linux, AIX), Datenbanken und "
    "Anwendungsserver. Teamarbeit und analytisches Denken sind uns wichtig.")


def test_a_datacenter_consulting_vacancy_leads_with_infrastructure():
    """Item 4: Linux, networking, Docker and server evidence come first."""
    evidence = evidence_for(DATACENTER_ADVERT, track="it_consulting")
    rows = skill_rows(evidence, "de", track="it_consulting")
    labels = [label for label, _ in rows]
    assert labels[0] == "DevOps & Werkzeuge", labels
    assert "Systemprogrammierung & Netzwerke" in labels[:2], labels
    printed = " ".join(value for _, value in rows)
    for technology in ("Docker", "NGINX", "Linux", "Bash", "TCP-Sockets"):
        assert technology in printed, f"{technology} missing from a datacenter CV"

    projects = [item.name for item in evidence.projects]
    assert "Inception" in projects, "the Docker/NGINX infrastructure project"
    assert "Minishell" in projects, "the Unix/systems project"
    # No infrastructure experience is invented, and nothing private leaks.
    cv = build_cv(evidence, role="Trainee IT Consulting w/m/d - Datacenter")
    text = " ".join([cv.summary, *cv.all_claims()])
    for invented in ("Kubernetes", "Terraform", "Ansible", "AWS", "Azure",
                     "Rechenzentrum betrieben", "Jahre Erfahrung"):
        assert invented not in text, f"invented infrastructure claim: {invented}"
    for secret in ("webserv", "greenroute", "GreenRoute"):
        assert secret not in text
    assert check_claims(cv.all_claims(), evidence).ok


def test_a_bank_it_vacancy_leads_with_programming_and_data():
    """Item 5: programming, backend and database evidence come first."""
    evidence = evidence_for(BANK_IT_ADVERT, track="graduate_entry")
    rows = skill_rows(evidence, "de", track="graduate_entry")
    labels = [label for label, _ in rows]
    assert labels[0] == "Programmiersprachen", labels
    assert "Datenbanken" in labels, labels
    assert "Backend & Frameworks" in labels, labels
    assert rows[0][1].startswith("Java 17"), rows[0][1]

    projects = [item.name for item in evidence.projects]
    assert projects[0] == "Pizza In Progress", \
        f"the Spring Boot/Java project must lead a Java vacancy: {projects}"
    cv = build_cv(evidence, role="Trainee IT (m/w/d)")
    assert check_claims(cv.all_claims(), evidence).ok
    # Javascript and CSS are named by the advert and evidenced nowhere.
    printed = " ".join(value for _, value in rows)
    assert "JavaScript" not in printed and "CSS" not in printed


def test_the_two_vacancies_produce_different_cvs():
    datacenter = skill_rows(evidence_for(DATACENTER_ADVERT, track="it_consulting"),
                            "de", track="it_consulting")
    bank = skill_rows(evidence_for(BANK_IT_ADVERT, track="graduate_entry"),
                      "de", track="graduate_entry")
    assert [label for label, _ in datacenter] != [label for label, _ in bank]
    assert {item for _, value in datacenter for item in value.split("  ·  ")} != \
        {item for _, value in bank for item in value.split("  ·  ")}
    for rows in (datacenter, bank):
        printed = " ".join(value for _, value in rows)
        assert "C" in printed.split("  ·  ") or "C++" in printed


def test_a_gender_marker_outside_parentheses_is_stripped():
    assert clean_role("Trainee IT Consulting w/m/d - Datacenter") == \
        "Trainee IT Consulting - Datacenter"
    assert clean_role("Developer d/m/w") == "Developer"


def test_the_letter_uses_german_terminology_and_a_readable_skills_list():
    """Item 1 covers the letter too, not only the CV."""
    letter = deterministic_letter(evidence_for(), company="Finanz Informatik",
                                  role="Trainee IT (m/w/d)")
    body = letter.text
    assert "Processes" not in body, "an English label in a German letter"
    assert "Configuration Parsing" not in body
    # Headline technologies, not whole slash-separated bundles inside a list.
    found = re.search(r"Meine technischen Schwerpunkte sind ([^.]*)\.", body)
    assert found, body
    listed = found.group(1)
    assert " / " not in listed, f"bundles read badly in a list: {listed}"
    assert " und " in listed, f"the last item must join with 'und': {listed}"
    assert check_text(body, evidence_for(), company="Finanz Informatik",
                      role="Trainee IT").ok
