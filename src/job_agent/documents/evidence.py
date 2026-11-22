"""Choose which verified evidence a specific vacancy should see.

Entirely deterministic: no model call, no network. It reads
`data/candidate_profile.json` and the advert, and returns the subset of skills,
projects, employment and education that is relevant *and* evidenced.

Three rules it exists to enforce:

1. A private repository never leaves the machine. `public_projects` is the
   allowlist and nothing else is considered employer-shareable.
2. A skill marked `not_evidenced` is never presented as a skill. SAP/ABAP is
   the current example: searched for, never claimed.
3. Employment is management and logistics work. It is offered as *transferable*
   experience and never as software developer employment. The bank entries the
   user rejected are excluded by name as a second line of defence.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..candidate_profile import blocking_review_items, public_projects

# Career-track keys used by the profile's `tracks` fields.
PROFILE_TRACKS = ("backend_php", "backend_java", "systems_cpp", "devops_linux",
                  "python_data", "ai_integration", "sap_abap")

# Discovery tracks (career_tracks.py) -> profile evidence tracks.
TRACK_TO_PROFILE: dict[str, tuple[str, ...]] = {
    "ai_engineering": ("ai_integration", "python_data", "backend_php"),
    "python_automation": ("python_data", "ai_integration", "devops_linux"),
    "java_backend": ("backend_java",),
    "php_web": ("backend_php",),
    "cpp_systems": ("systems_cpp",),
    "linux_devops": ("devops_linux", "systems_cpp"),
    "data_engineering": ("python_data", "backend_php", "backend_java"),
    "sap_abap": ("sap_abap",),
    "it_consulting": ("devops_linux", "backend_php"),
    "graduate_entry": ("backend_php", "backend_java", "devops_linux"),
    "software_generalist": ("backend_php", "backend_java", "systems_cpp"),
    "unclassified": (),
}

# Technology vocabulary, used only to decide which evidence an advert calls for.
# A match here never creates a claim: the claim still has to exist in the
# profile. Keys are what an advert says; values are profile skill fragments.
REQUIREMENT_SIGNALS: dict[str, tuple[str, ...]] = {
    "php": ("php",), "symfony": ("php",), "laravel": ("php",),
    "mariadb": ("mariadb", "sql"), "mysql": ("mariadb", "sql"),
    "sql": ("sql",), "datenbank": ("sql",), "database": ("sql",),
    "phpunit": ("phpunit", "php"), "junit": ("junit",), "unit test": ("phpunit", "junit"),
    "docker": ("docker",), "container": ("docker",), "nginx": ("nginx",),
    "rest": ("http", "rest"), "api": ("http", "rest"), "http": ("http",),
    "java": ("java",), "spring": ("spring",), "jpa": ("jpa", "java"),
    "hibernate": ("jpa", "java"), "kotlin": ("java",),
    "python": ("python",), "rag": ("rag",), "llm": ("rag", "ollama"),
    "ollama": ("ollama",), "embedding": ("embeddings", "rag"),
    "langchain": ("rag",), "ki": ("rag",), "ai": ("rag",),
    "c++": ("c / c++", "c++"), "cpp": ("c / c++",), "c-programmierung": ("c / c++",),
    "embedded": ("c / c++",), "socket": ("tcp", "http"), "netzwerk": ("tcp",),
    "network": ("tcp",), "tcp": ("tcp",),
    "linux": ("linux",), "unix": ("linux",), "bash": ("bash",),
    "shell": ("bash",), "git": ("git",), "ci/cd": ("git", "docker"),
    "abap": (), "sap": (),  # Deliberately empty: no evidenced SAP experience.
}

# How a requirement signal is spelled when it appears in a document. The
# signals themselves are lowercase match keys, and printing them raw read as
# sloppy ("Themen docker, linux, mariadb").
REQUIREMENT_DISPLAY: dict[str, str] = {
    "php": "PHP", "symfony": "Symfony", "laravel": "Laravel",
    "mariadb": "MariaDB", "mysql": "MySQL", "sql": "SQL",
    "datenbank": "Datenbanken", "database": "databases",
    "phpunit": "PHPUnit", "junit": "JUnit", "unit test": "Unit-Tests",
    "docker": "Docker", "container": "Container", "nginx": "NGINX",
    "rest": "REST-Schnittstellen", "api": "APIs", "http": "HTTP",
    "java": "Java", "spring": "Spring Boot", "jpa": "JPA",
    "hibernate": "Hibernate", "kotlin": "Kotlin",
    "python": "Python", "rag": "RAG", "llm": "LLM", "ollama": "Ollama",
    "embedding": "Embeddings", "langchain": "LangChain", "ki": "KI", "ai": "AI",
    "c++": "C++", "cpp": "C++", "c-programmierung": "C",
    "embedded": "Embedded", "socket": "Sockets", "netzwerk": "Netzwerke",
    "network": "networking", "tcp": "TCP",
    "linux": "Linux", "unix": "Unix", "bash": "Bash", "shell": "Shell",
    "git": "Git", "ci/cd": "CI/CD", "sap": "SAP", "abap": "ABAP",
}


# Country and connector words that appear in profile date/location strings.
# These are data, not prose: translating them is safe and is not a claim.
_LOCALISE_DE = (
    (re.compile(r"\bGermany\b"), "Deutschland"),
    (re.compile(r"\bRussia\b"), "Russland"),
    # City names, for the same reason as country names: a German CV that says
    # "Moscow, Russland" is half-translated. These are place names, not claims.
    (re.compile(r"\bMoscow\b"), "Moskau"),
    (re.compile(r"\bMakhachkala\b"), "Machatschkala"),
    (re.compile(r"\bsince\b", re.I), "seit"),
    (re.compile(r"\s+to\s+"), " – "),
)


def localise(value: str, language: str) -> str:
    """Translate country names and date connectors only."""
    text = str(value or "")
    if language != "de":
        return text
    for pattern, replacement in _LOCALISE_DE:
        text = pattern.sub(replacement, text)
    return text


def display_requirement(term: str) -> str:
    return REQUIREMENT_DISPLAY.get(str(term).casefold(), str(term))


# German-language markers. Counting these is more reliable on advert text than
# any single keyword, because German adverts routinely use English job titles.
_GERMAN_MARKERS = re.compile(
    r"\b(und|oder|mit|für|fuer|wir|dich|sie|deine|ihre|unsere|aufgaben|"
    r"kenntnisse|erfahrung|abgeschlossen\w*|voraussetzung|mitarbeiter\w*|"
    r"stelle|bewerbung|freuen|bieten|suchen|arbeiten|entwickl\w*|"
    r"berufserfahrung|teamarbeit|vergütung|verguetung|urlaub)\b", re.I)
_ENGLISH_MARKERS = re.compile(
    r"\b(and|or|with|you|your|we|our|the|responsibilities|requirements|"
    r"experience|skills|team|role|position|offer|looking|apply|benefits)\b", re.I)


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def detect_language(description: str, *, role: str = "", company: str = "",
                    default: str = "de") -> str:
    """Pick the document language from the advert's own language.

    German wins ties on purpose: the vacancies are in Frankfurt, Stuttgart and
    Heilbronn, and a German employer advertising in English will still accept a
    German application, while the reverse is not reliably true.
    """
    text = f"{role} {company} {description}"
    german = len(_GERMAN_MARKERS.findall(text))
    english = len(_ENGLISH_MARKERS.findall(text))
    if german == 0 and english == 0:
        return default
    return "en" if english > german * 1.5 else "de"


@dataclass(frozen=True)
class SkillEvidence:
    """One skill the candidate may present, with what backs it."""

    name: str
    sources: tuple[str, ...]
    tracks: tuple[str, ...]
    confidence: str
    caution: str = ""

    @property
    def github_backed(self) -> bool:
        return any(source.startswith("github:") for source in self.sources)


@dataclass(frozen=True)
class ProjectEvidence:
    """A public, employer-shareable project."""

    name: str
    repository: str
    url: str
    points: tuple[str, ...]
    tracks: tuple[str, ...]
    caution: str = ""

    points_de: tuple[str, ...] = ()

    def described(self, language: str) -> tuple[str, ...]:
        """Description lines in the document's language, where available.

        The profile's `evidence_points` are written in English. A German CV
        containing English bullet points reads badly, so a profile may add
        `evidence_points_de`. Without it the English text is used and the
        review report says so, rather than silently shipping a mixed-language
        document or inventing a translation.
        """
        if language == "de" and self.points_de:
            return self.points_de
        return self.points

    def technologies(self) -> tuple[str, ...]:
        """Technology names mentioned in the evidence points.

        The points themselves are written in English. Weaving an English
        sentence into German prose produced broken text, so the deterministic
        letter uses these language-neutral names instead.
        """
        from .factcheck import KNOWN_TECHNOLOGIES

        display = {
            "php": "PHP", "mariadb": "MariaDB", "mysql": "MySQL", "sql": "SQL",
            "phpunit": "PHPUnit", "docker": "Docker", "nginx": "NGINX",
            "java": "Java", "spring": "Spring Boot", "jpa": "JPA",
            "hibernate": "Hibernate", "junit": "JUnit", "maven": "Maven",
            "python": "Python", "ollama": "Ollama", "linux": "Linux",
            "c": "C", "c++": "C++", "rag": "RAG", "git": "Git",
            "bash": "Bash", "wordpress": "WordPress", "pdo": "PDO",
            "embeddings": "Embeddings", "langchain": "LangChain",
        }
        found: list[str] = []
        haystack = " ".join(self.points)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+.#_-]{1,}", haystack):
            # Try the whole token, then its hyphen-separated parts, so
            # "PHP-based" and "Docker-Compose" resolve to PHP and Docker.
            for candidate in [token, *token.split("-")]:
                key = _fold(candidate).strip(".-_")
                if key in KNOWN_TECHNOLOGIES:
                    label = display.get(key, candidate)
                    if label not in found:
                        found.append(label)
                    break
        return tuple(found)


@dataclass(frozen=True)
class EmploymentEvidence:
    """Real employment. Management and logistics, never software development."""

    title: str
    employer: str
    location: str
    start: str
    end: str
    summary: str
    summary_de: str = ""
    title_de: str = ""

    def described(self, language: str) -> str:
        if language == "de" and self.summary_de:
            return self.summary_de
        return self.summary

    def titled(self, language: str) -> str:
        """The job title in the document's language, when the profile has one.

        Empty when it does not, so the caller falls back to its own mapping
        rather than this module inventing a German job title.
        """
        if language == "de" and self.title_de:
            return self.title_de
        return ""


@dataclass(frozen=True)
class EducationEvidence:
    title: str
    institution: str
    dates: str
    note: str = ""
    title_de: str = ""

    def titled(self, language: str) -> str:
        """The qualification as a German CV should print it, when known.

        The English `title` stays the profile's record and is what the
        credential check reads, so a German rendering cannot widen what may be
        claimed.
        """
        if language == "de" and self.title_de:
            return self.title_de
        return self.title


@dataclass
class EvidenceSelection:
    """Everything one application may draw on, and nothing more."""

    job_id: str
    language: str
    track: str
    profile_tracks: tuple[str, ...] = ()
    skills: list[SkillEvidence] = field(default_factory=list)
    projects: list[ProjectEvidence] = field(default_factory=list)
    employment: list[EmploymentEvidence] = field(default_factory=list)
    education: list[EducationEvidence] = field(default_factory=list)
    languages: tuple[str, ...] = ()
    availability: str = ""
    matched_requirements: tuple[str, ...] = ()
    unmet_requirements: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    cautions: tuple[str, ...] = ()

    @property
    def skill_names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)

    @property
    def allowed_terms(self) -> frozenset[str]:
        """Every technology the generated text is permitted to mention.

        The fact checker uses this: a technology outside it is, by definition,
        not evidenced for this candidate.
        """
        terms: set[str] = set()
        for skill in self.skills:
            for piece in re.split(r"[/,()]| and | und ", skill.name):
                cleaned = _fold(piece).strip()
                if len(cleaned) > 1:
                    terms.add(cleaned)
        for project in self.projects:
            terms.add(_fold(project.name))
            for point in project.points:
                for word in re.findall(r"[A-Za-z][A-Za-z0-9+.#-]{1,}", point):
                    terms.add(_fold(word))
        return frozenset(terms)

    def to_prompt_payload(self) -> dict[str, Any]:
        """The ONLY shape that may reach a model.

        Carries no name, no contact detail, no private repository and no CV
        filename. Compare with `contact.py`, which this module never imports.
        """
        return {
            "language": self.language,
            "education": [{"title": item.title, "institution": item.institution,
                           "dates": item.dates} for item in self.education],
            "evidenced_skills": [
                {"skill": skill.name, "backed_by": "project" if skill.github_backed
                 else "cv/training"} for skill in self.skills],
            "public_projects": [
                {"name": project.name, "demonstrates": list(project.points),
                 "limitation": project.caution} for project in self.projects],
            "employment_history_non_software": [
                {"title": item.title, "field": item.summary,
                 "from": item.start, "to": item.end} for item in self.employment],
            "languages": list(self.languages),
            "availability": self.availability,
            "job_requirements_matched": list(self.matched_requirements),
            "job_requirements_unmet": list(self.unmet_requirements),
            "open_questions": list(self.open_questions),
            "hard_constraints": [
                "No commercial software developer employment exists. Never imply any.",
                "Do not state a number of years of professional programming experience.",
                "Do not claim certifications, degrees or technologies absent above.",
                "Projects are personal or training work, never delivered products.",
            ],
        }


def _requirement_terms(description: str) -> list[str]:
    """Technologies the advert actually asks for, in the advert's own words."""
    folded = _fold(description)
    found: list[str] = []
    for signal in REQUIREMENT_SIGNALS:
        if re.search(r"(?<![a-z0-9])" + re.escape(signal) + r"(?![a-z0-9])", folded):
            found.append(signal)
    return found


def select_evidence(profile: Mapping[str, Any], *, job_id: str, role: str,
                    company: str, description: str, track: str = "",
                    assessment: Mapping[str, Any] | None = None,
                    language: str = "", max_projects: int = 3,
                    max_skills: int = 14) -> EvidenceSelection:
    """Build the evidence set for one vacancy.

    Raises when the profile still has a blocking review item: an unresolved
    discrepancy must be settled before anything is written in the candidate's
    name.
    """
    blocking = blocking_review_items(profile)
    if blocking:
        raise ValueError("Blocking CV discrepancies remain; run 'job-agent profile'. "
                         + ", ".join(str(item.get("id")) for item in blocking))

    chosen_language = language or detect_language(description, role=role, company=company)
    wanted = set(TRACK_TO_PROFILE.get(track, ()))
    requirements = _requirement_terms(description)
    needed_fragments = {fragment for term in requirements
                        for fragment in REQUIREMENT_SIGNALS[term]}

    shareable = public_projects(profile)
    allowed_repos = {str(item["repository"]).split("/")[-1] for item in shareable}
    excluded: list[str] = []
    cautions: list[str] = []

    # --- skills -------------------------------------------------------
    scored_skills: list[tuple[int, SkillEvidence]] = []
    for raw in profile.get("skills", []) or []:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name", "")).strip()
        if raw.get("confidence") == "not_evidenced":
            excluded.append(f"skill '{name}' (not evidenced)")
            continue
        sources = []
        for item in raw.get("evidence", []) or []:
            text = str(item)
            if text.startswith("github:") and text[7:] not in allowed_repos:
                excluded.append(f"evidence '{text}' (private repository)")
                continue
            sources.append(text)
        if not sources:
            excluded.append(f"skill '{name}' (only private evidence)")
            continue
        folded = _fold(name)
        relevance = 0
        if needed_fragments and any(fragment in folded for fragment in needed_fragments):
            relevance += 3
        if wanted & set(raw.get("tracks", []) or []):
            relevance += 2
        if any(source.startswith("github:") for source in sources):
            relevance += 1
        scored_skills.append((relevance, SkillEvidence(
            name=name, sources=tuple(sources),
            tracks=tuple(str(t) for t in raw.get("tracks", []) or []),
            confidence=str(raw.get("confidence", "")),
            caution=str(raw.get("note", "")))))
    # Stable sort on the score alone, so equally relevant skills keep the
    # profile's own order rather than an arbitrary one.
    scored_skills.sort(key=lambda item: -item[0])
    skills = [skill for _, skill in scored_skills[:max_skills]]

    # --- projects -----------------------------------------------------
    scored_projects: list[tuple[int, ProjectEvidence]] = []
    for raw in shareable:
        points = tuple(str(point) for point in raw.get("evidence_points", []) or [])
        tracks = tuple(str(t) for t in raw.get("tracks", []) or [])
        folded = _fold(" ".join(points) + " " + str(raw.get("name", "")))
        relevance = 0
        if wanted & set(tracks):
            relevance += 3
        relevance += sum(2 for fragment in needed_fragments if fragment in folded)
        project = ProjectEvidence(
            name=str(raw.get("name", "")).strip(),
            repository=str(raw.get("repository", "")),
            url="https://github.com/" + str(raw.get("repository", "")),
            points=points, tracks=tracks, caution=str(raw.get("caution", "")),
            points_de=tuple(str(item) for item in
                            raw.get("evidence_points_de", []) or []))
        if project.caution:
            cautions.append(f"{project.name}: {project.caution}")
        scored_projects.append((relevance, project))
    scored_projects.sort(key=lambda item: -item[0])
    projects = [project for score, project in scored_projects
                if score > 0][:max_projects]
    if not projects:
        # Nothing track-specific matched; show the strongest public work rather
        # than an empty project section.
        projects = [project for _, project in scored_projects[:max_projects]]

    for raw in profile.get("projects", []) or []:
        if isinstance(raw, Mapping) and raw.get("visibility") != "public":
            excluded.append(f"project '{raw.get('name')}' (private, confidential)")

    # --- employment ---------------------------------------------------
    background = profile.get("professional_background", {}) or {}
    forbidden = {_fold(item) for item in
                 background.get("excluded_as_incorrect_by_user", []) or []}
    employment: list[EmploymentEvidence] = []
    for raw in background.get("employment_history", []) or []:
        if not isinstance(raw, Mapping):
            continue
        employer = str(raw.get("employer", "")).strip()
        if any(_fold(employer) in entry or employer and _fold(employer) in entry
               for entry in forbidden):
            excluded.append(f"employment '{employer}' (rejected by the user)")
            continue
        employment.append(EmploymentEvidence(
            title=str(raw.get("title", "")).strip(), employer=employer,
            location=str(raw.get("location", "")).strip(),
            start=str(raw.get("from", "")), end=str(raw.get("to", "")),
            summary=str(raw.get("background", "")).strip(),
            summary_de=str(raw.get("background_de", "")).strip(),
            title_de=str(raw.get("title_de", "")).strip()))
    employment.sort(key=lambda item: item.start, reverse=True)
    for entry in background.get("excluded_as_incorrect_by_user", []) or []:
        note = f"never claim: {entry}"
        if note not in excluded:
            excluded.append(note)

    education = [EducationEvidence(
        title=str(item.get("title", "")).strip(),
        institution=str(item.get("institution", "")).strip(),
        dates=str(item.get("dates", "")), note=str(item.get("note", "")),
        title_de=str(item.get("title_de", "")).strip())
        for item in profile.get("education", []) or [] if isinstance(item, Mapping)]

    # --- what the advert asked for ------------------------------------
    verdict = dict(assessment or {})
    matched = tuple(str(item) for item in verdict.get("matched_skills", []) or [])
    unmet = tuple(str(item) for item in
                  (verdict.get("mandatory_unmet") or verdict.get("missing_skills") or []))
    questions = tuple(str(item) for item in verdict.get("unknown_requirements", []) or [])
    if not matched:
        matched = tuple(sorted({term for term in requirements
                                if REQUIREMENT_SIGNALS[term]}))

    availability = str(((profile.get("candidate") or {}).get("availability") or {})
                       .get("display", "")).strip()
    return EvidenceSelection(
        job_id=job_id, language=chosen_language, track=track or "unclassified",
        profile_tracks=tuple(sorted(wanted)), skills=skills, projects=projects,
        employment=employment, education=education,
        languages=("Deutsch B2", "English B2", "Russisch (Muttersprache)"),
        availability=availability, matched_requirements=matched,
        unmet_requirements=unmet, open_questions=questions,
        excluded=tuple(dict.fromkeys(excluded)), cautions=tuple(cautions))
