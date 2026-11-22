"""Configurable, skill-based career tracks for broad job discovery (Stage A).

A career track is a *search strategy*, not a claim of candidate competence.
Searching for "AI Engineer" does not assert AI engineering experience; the
evidence-gated Stage B matcher in `ai_matching.py` decides fit.

Replaces the seven hard-coded job titles of v0.7. Each track carries German and
English query variants because the German market advertises the same role under
both. Quotas are allocated per track so that, for example, the large volume of
"Praktikum" listings cannot crowd out every other category.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class CareerTrack:
    """One searchable career direction.

    `terms` are sent verbatim to a job source's full-text search.
    `profile_tracks` link back to the `tracks` values inside
    `data/candidate_profile.json` so that Stage B can report which evidence
    bucket a posting was matched against. An empty `profile_tracks` means the
    track is searched on transferable grounds only, with no direct technical
    evidence in the profile — that is deliberate and must stay visible.
    """

    key: str
    label: str
    terms: tuple[str, ...]
    profile_tracks: tuple[str, ...] = ()
    note: str = ""
    # Share of the search budget and of the per-track result quota, 1..3.
    # Set from how well the candidate profile evidences the track, never from
    # how attractive the job titles sound:
    #   3 = directly demonstrated by projects or training
    #   2 = partially evidenced
    #   1 = transferable only, or explicitly not evidenced
    priority: int = 2

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", self.key):
            raise ValueError(f"Invalid career track key: {self.key!r}")
        if self.priority not in (1, 2, 3):
            raise ValueError(f"{self.key}: priority must be 1, 2 or 3")
        if not self.terms:
            raise ValueError(f"Career track {self.key} needs at least one search term")
        if len(self.terms) != len(set(self.terms)):
            raise ValueError(f"Career track {self.key} has duplicate terms")


# The ten categories requested, plus `software_generalist` for the very common
# generic entry-level titles ("Software Engineer", "Anwendungsentwickler") that
# belong to no single technology track. Order is the default round-robin order.
CAREER_TRACKS: tuple[CareerTrack, ...] = (
    CareerTrack(
        key="ai_engineering",
        label="AI / LLM / RAG Engineering",
        terms=(
            "AI Engineer", "LLM Engineer", "RAG Developer", "KI Entwickler",
            "AI Integration Engineer", "Machine Learning Engineer",
            "KI Softwareentwickler", "Generative AI Developer",
        ),
        profile_tracks=("ai_integration",),
        note="Evidence is a local RAG/Ollama project, not production ML operations.",
        priority=3,  # DokuChat: PHP RAG with local Ollama, embeddings, MariaDB.
    ),
    CareerTrack(
        key="python_automation",
        label="Python Development / Automation",
        terms=(
            "Python Entwickler", "Python Developer", "Automation Engineer",
            "Automatisierungsentwickler", "Python Backend Developer",
            "Scripting Entwickler",
        ),
        profile_tracks=("python_data", "ai_integration"),
        note="Python evidence in the profile is currently private-repository only.",
        priority=3,  # Python/OSMnx/NetworkX/Streamlit project evidence.
    ),
    CareerTrack(
        key="java_backend",
        label="Java / Spring Boot Backend",
        terms=(
            "Java Entwickler", "Spring Boot Entwickler", "Java Backend Developer",
            "Java Software Engineer", "Backend Entwickler Java",
        ),
        profile_tracks=("backend_java",),
        priority=3,  # Pizza In Progress: Spring Boot, JPA, Security; Tel-Ran training.
    ),
    CareerTrack(
        key="php_web",
        label="PHP Backend / Web Development",
        terms=(
            "PHP Entwickler", "PHP Developer", "Webentwickler",
            "Web Developer", "Backend Entwickler PHP", "Symfony Entwickler",
            "Laravel Entwickler",
        ),
        profile_tracks=("backend_php",),
        priority=3,  # DokuChat and php-login-system: PHP 8, PDO, MariaDB.
    ),
    CareerTrack(
        key="cpp_systems",
        label="C / C++ / Systems Programming",
        terms=(
            "C++ Entwickler", "C++ Developer", "C Entwickler",
            "Systemprogrammierung", "Embedded Software Entwickler",
            "Systems Software Engineer",
        ),
        profile_tracks=("systems_cpp",),
        priority=3,  # minishell, Cub3D, webserv: C/C++, processes, sockets.
    ),
    CareerTrack(
        key="linux_devops",
        label="Linux / DevOps / IT Infrastructure",
        terms=(
            "DevOps Engineer", "Linux Administrator", "Systemadministrator",
            "IT Infrastruktur", "Cloud Engineer", "Site Reliability Engineer",
            "Linux Systemengineer",
        ),
        profile_tracks=("devops_linux",),
        priority=3,  # Inception: Docker Compose, NGINX/TLS, MariaDB; Bash/Git.
    ),
    CareerTrack(
        key="data_engineering",
        label="Data Engineering / SQL / Integration",
        terms=(
            "Data Engineer", "SQL Entwickler", "Datenbankentwickler",
            "Data Integration Engineer", "ETL Entwickler",
            "Datenbank Administrator", "BI Entwickler",
        ),
        profile_tracks=("python_data", "backend_java"),
        priority=2,  # SQL/MariaDB evidenced; no ETL or BI tooling evidence.
    ),
    CareerTrack(
        key="sap_abap",
        label="SAP / ABAP (entry level)",
        terms=(
            "SAP ABAP Entwickler", "ABAP Entwickler", "SAP Consultant",
            "SAP Berater", "SAP Technologieberater", "SAP Entwickler",
        ),
        profile_tracks=("sap_abap",),
        note="No evidenced hands-on ABAP experience; only training/entry roles can fit.",
        priority=1,  # Explicitly not evidenced; only training/entry roles can fit.
    ),
    CareerTrack(
        key="it_consulting",
        label="IT Consulting / Technical Business Analysis",
        terms=(
            "IT Consultant", "IT Berater", "Technical Business Analyst",
            "Business Analyst IT", "IT Projektmitarbeiter",
            "Prozessmanager IT", "Technology Consultant",
        ),
        profile_tracks=(),
        note="Transferable: Wirtschaftsinformatik degree plus process/management history.",
        priority=1,  # Transferable only: Wirtschaftsinformatik plus process work.
    ),
    CareerTrack(
        key="graduate_entry",
        label="Graduate / Trainee / Quereinsteiger programmes",
        terms=(
            "Trainee IT", "Quereinsteiger IT", "Berufseinsteiger Softwareentwickler",
            "Graduate Software Engineer", "IT Praktikum", "Praktikum Softwareentwicklung",
            "Einsteiger Programmierer",
        ),
        profile_tracks=(),
        note="Internships are in scope only without a university-enrolment requirement.",
        priority=1,  # Entry programmes, no technical evidence of their own.
    ),
    CareerTrack(
        key="software_generalist",
        label="Generic software engineering titles",
        terms=(
            "Softwareentwickler", "Software Engineer", "Anwendungsentwickler",
            "Backend Developer", "Application Developer", "Fullstack Entwickler",
            "Softwareingenieur",
        ),
        profile_tracks=("backend_php", "backend_java", "systems_cpp"),
        note="Many entry-level roles carry no seniority word in the title at all.",
        priority=3,  # Covers the same evidence under generic titles.
    ),
)

TRACKS_BY_KEY: Mapping[str, CareerTrack] = {track.key: track for track in CAREER_TRACKS}


@dataclass(frozen=True)
class SearchQuery:
    """One planned source query. `track` is provenance, not a scoring input."""

    track: str
    term: str
    city: str


def resolve_tracks(keys: Iterable[str] | None = None) -> tuple[CareerTrack, ...]:
    """Select enabled tracks, preserving the canonical order for stable plans."""
    if keys is None:
        return CAREER_TRACKS
    wanted = {str(key).strip().lower() for key in keys if str(key).strip()}
    if not wanted:
        return CAREER_TRACKS
    unknown = sorted(wanted - set(TRACKS_BY_KEY))
    if unknown:
        raise ValueError("Unknown career tracks: " + ", ".join(unknown)
                         + ". Available: " + ", ".join(TRACKS_BY_KEY))
    return tuple(track for track in CAREER_TRACKS if track.key in wanted)


def search_plan(cities: Sequence[str], limit: int, *,
                tracks: Sequence[CareerTrack] | None = None,
                today: date | None = None) -> list[SearchQuery]:
    """Allocate a bounded daily search budget fairly across all career tracks.

    Fairness: queries are handed out round-robin, one per track per round, so a
    budget smaller than the number of tracks still rotates across days instead
    of permanently favouring whichever track happens to be listed first. Terms
    and cities advance together with a date-derived offset, so repeated daily
    runs explore new term/city combinations without any stored state.
    """
    chosen = tuple(tracks) if tracks is not None else CAREER_TRACKS
    clean_cities = [str(city).strip() for city in cities if str(city).strip()]
    if not clean_cities:
        raise ValueError("At least one city is required")
    if not chosen:
        raise ValueError("At least one career track is required")
    if not 1 <= limit <= 120:
        raise ValueError("search budget must be between 1 and 120 queries")

    offset = (today or date.today()).toordinal()
    # Rotate which track is served first so a small budget still reaches every
    # track over consecutive days, then weight the rotation by priority so the
    # tracks the candidate can actually evidence get the larger share.
    rotated = [chosen[(offset + index) % len(chosen)] for index in range(len(chosen))]
    order = weighted_order(rotated)

    plan: list[SearchQuery] = []
    seen: set[tuple[str, str]] = set()
    exhausted = False
    round_index = 0
    while len(plan) < limit and not exhausted:
        exhausted = True
        for position, track in enumerate(order):
            if len(plan) >= limit:
                break
            if round_index >= len(track.terms):
                continue  # This track has no unseen term left in this rotation.
            exhausted = False
            term = track.terms[(offset + round_index) % len(track.terms)]
            city = clean_cities[(offset + round_index + position) % len(clean_cities)]
            if (term, city) in seen:
                continue
            seen.add((term, city))
            plan.append(SearchQuery(track=track.key, term=term, city=city))
        round_index += 1
    return plan


def weighted_order(tracks: Sequence[CareerTrack]) -> list[CareerTrack]:
    """Interleave tracks so a priority-3 track appears three times per cycle.

    The first pass contains *every* track, and only the later passes are
    restricted to higher priorities. Ordering it the other way round starved
    the priority-1 tracks completely whenever the budget was smaller than the
    weighted cycle, which would have hidden the SAP and trainee routes the
    candidate still wants to see.
    """
    order: list[CareerTrack] = []
    for slot in range(1, 4):
        for track in tracks:
            if track.priority >= slot:
                order.append(track)
    return order or list(tracks)


def track_weights(tracks: Sequence[CareerTrack] | None = None) -> dict[str, int]:
    """Per-track multiplier for the result quota, keyed by track."""
    chosen = tuple(tracks) if tracks is not None else CAREER_TRACKS
    return {track.key: track.priority for track in chosen}


def track_quota(limit: int, track_count: int) -> int:
    """Maximum discoveries kept per track so one category cannot dominate."""
    if limit < 1 or track_count < 1:
        raise ValueError("limit and track_count must be positive")
    return max(1, -(-limit // track_count))  # ceil division


# --- classification of a posting that arrived without a query ------------
#
# Sources with no keyword search (employer boards, career pages) hand over a
# posting with no query attached, so its track has to be read off the title.
# This must either produce a defensible answer or admit it cannot: labelling a
# posting from an unrelated planned query is worse than leaving it unlabelled,
# because the per-track quota and every report then describe the wrong thing.

# Words too generic to identify a track on their own. "ai" and "ki" are
# deliberately NOT here: they are the strongest signal for AI/LLM work, and
# suppressing them made "AI Automation Engineer" classify as plain Python work.
_STOPWORDS = frozenset({
    "it", "in", "und", "der", "die", "das", "mit", "fuer", "für", "the", "and",
    "developer", "engineer", "engineering", "entwickler", "entwicklerin",
    "consultant", "berater", "beraterin", "software", "systems", "system",
    "manager", "associate", "specialist", "junior", "senior", "m", "w", "d",
    "x", "f", "all", "genders", "mwd", "wmd",
})

# Distinctive vocabulary per track, written out rather than derived from the
# search terms. Search terms are phrased for a search box ("Linux
# Systemengineer"); these are the words that actually identify the work.
_TRACK_SIGNALS: dict[str, tuple[str, ...]] = {
    "ai_engineering": ("ai", "ki", "llm", "rag", "genai", "ml", "nlp",
                       "machine", "learning", "generative", "inference",
                       "embeddings", "langchain", "chatbot", "agentic"),
    "python_automation": ("python", "automation", "automatisierung",
                          "automatisierungs", "scripting", "rpa", "django",
                          "flask", "fastapi", "pandas"),
    "java_backend": ("java", "spring", "boot", "jvm", "kotlin", "jakarta",
                     "hibernate", "maven"),
    "php_web": ("php", "symfony", "laravel", "wordpress", "typo3", "web",
                "webentwickler", "webentwicklung", "frontend", "fullstack"),
    "cpp_systems": ("c", "c++", "cpp", "embedded", "firmware", "rust",
                    "systemprogrammierung", "kernel", "treiber", "realtime",
                    "mikrocontroller"),
    "linux_devops": ("linux", "unix", "devops", "sre", "kubernetes", "docker",
                     "cloud", "aws", "azure", "ansible", "terraform",
                     "infrastruktur", "infrastructure", "systemadministrator",
                     "sysadmin", "netzwerk", "network", "platform"),
    "data_engineering": ("data", "daten", "sql", "etl", "bi", "datenbank",
                         "database", "warehouse", "analytics", "snowflake",
                         "dbt", "spark", "pipeline"),
    "sap_abap": ("sap", "abap", "hana", "s4", "erp", "fiori", "successfactors"),
    "it_consulting": ("implementation", "business", "analyst", "prozess",
                      "process", "requirements", "anforderungsmanagement",
                      "technologieberater", "solution", "presales",
                      "digitalisierung", "transformation"),
    "graduate_entry": ("trainee", "praktikum", "praktikant", "praktikantin",
                       "internship", "intern", "quereinsteiger",
                       "berufseinsteiger", "einsteiger", "graduate",
                       "traineeprogramm", "volontariat"),
    "software_generalist": ("anwendungsentwicklung", "anwendungsentwickler",
                            "backend", "api", "microservices", "programmierer",
                            "softwareentwicklung", "softwareentwickler",
                            "softwareingenieur", "application"),
}


def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9+#.]+", str(text or "").casefold())
            if token]


_TRACK_TOKENS: dict[str, frozenset[str]] = {
    key: frozenset(token for signal in signals for token in _tokens(signal)
                   if token not in _STOPWORDS)
    for key, signals in _TRACK_SIGNALS.items()
}
# A track defined in CAREER_TRACKS but given no signals would be unreachable.
assert set(_TRACK_TOKENS) == {track.key for track in CAREER_TRACKS}

UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class TrackMatch:
    """A classification and the words that justify it."""

    track: str
    evidence: tuple[str, ...] = ()

    @property
    def confident(self) -> bool:
        return bool(self.track and self.track != UNCLASSIFIED and self.evidence)

    def __str__(self) -> str:
        return (f"{self.track} ({', '.join(self.evidence)})" if self.evidence
                else self.track)


def classify_title(title: str, *,
                   tracks: Sequence[CareerTrack] | None = None) -> TrackMatch:
    """Classify a job title, or return `unclassified` with no evidence.

    Ties break towards the higher-priority track and then the canonical order,
    so the result is stable across runs. A single distinctive word is enough --
    "SAP" or "Kubernetes" identifies the work -- but a title made only of
    generic words stays unclassified rather than being guessed at.
    """
    words = set(_tokens(title))
    if not words:
        return TrackMatch(UNCLASSIFIED)
    candidates = tuple(tracks) if tracks is not None else CAREER_TRACKS
    best: TrackMatch | None = None
    best_rank: tuple[int, int, int] | None = None
    for position, track in enumerate(candidates):
        hits = tuple(sorted(words & _TRACK_TOKENS.get(track.key, frozenset())))
        if not hits:
            continue
        rank = (len(hits), track.priority, -position)
        if best_rank is None or rank > best_rank:
            best_rank, best = rank, TrackMatch(track.key, hits)
    return best or TrackMatch(UNCLASSIFIED)


def infer_track(title: str, *, tracks: Sequence[CareerTrack] | None = None) -> str:
    """Best-matching track key, or "" when the title does not identify one.

    Deliberately a *label*, not a judgement: it never decides whether a posting
    is kept, only which quota bucket it counts against.
    """
    match = classify_title(title, tracks=tracks)
    return match.track if match.confident else ""
