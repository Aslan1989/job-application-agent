"""Search queries derived from the candidate's verified evidence.

A career track supplies job titles; this module supplies the *skill* half, so
the search engine sees what the candidate can actually demonstrate rather than
only a list of role names. Every term here traces back to an entry in
`data/candidate_profile.json`: nothing is invented, and a skill marked
`not_evidenced` is never used to claim competence.

Two deliberate asymmetries:

- A skill with only private-repository evidence (today: Python/OSMnx/NetworkX)
  may still be *searched for* — searching is harmless and costs the candidate
  nothing. It is the prompt in `ai_matching.py` that must not claim it, and
  that module filters it out independently.
- Queries prefer employer-direct phrasing ("karriere", "stellenangebot") over
  aggregator phrasing, and the adapter ranks aggregator domains down, because
  the task is to find the employer's own posting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from .career_tracks import CAREER_TRACKS, CareerTrack, resolve_tracks

# Skill names in the profile are human labels ("PHP 8 / OOP / PDO"). These map
# them to the words a job advert would actually use.
SKILL_QUERY_TERMS: dict[str, tuple[str, ...]] = {
    "php": ("PHP",),
    "rag": ("RAG", "Retrieval Augmented Generation"),
    "embeddings": ("Embeddings",),
    "ollama": ("LLM",),
    "mariadb": ("MariaDB", "SQL"),
    "mysql": ("MySQL",),
    "sql": ("SQL",),
    "docker": ("Docker",),
    "nginx": ("NGINX",),
    "java": ("Java",),
    "spring boot": ("Spring Boot",),
    "jpa": ("JPA", "Hibernate"),
    "junit": ("JUnit",),
    "c++": ("C++",),
    "linux": ("Linux",),
    "http": ("HTTP", "REST API"),
    "sockets": ("TCP", "Netzwerk"),
    "python": ("Python",),
    "streamlit": ("Python",),
    "networkx": ("Python",),
    "bash": ("Bash", "Shell"),
    "git": ("Git",),
}

# Words that steer a web search towards an employer's own careers page.
EMPLOYER_HINTS_DE = ("Karriere", "Stellenangebot", "Jobs")
EMPLOYER_HINTS_EN = ("careers", "job opening")

# Aggregators are excluded from the query itself where the API supports it, and
# ranked down afterwards. Discovery should surface the employer's own posting.
AGGREGATOR_DOMAINS: frozenset[str] = frozenset({
    "indeed.com", "de.indeed.com", "stepstone.de", "linkedin.com", "xing.com",
    "glassdoor.com", "glassdoor.de", "monster.de", "jobware.de", "kimeta.de",
    "jooble.org", "adzuna.de", "talent.com", "neuvoo.de", "careerjet.de",
    "jobrapido.com", "stellenanzeigen.de", "meinestadt.de", "whatjobs.com",
    "joblift.de", "heyjobs.co", "get-in-it.de", "absolventa.de", "jobvector.de",
    "ictjob.de", "it-jobs.de", "jobbörse.de", "jobboerse.de", "arbeitsagentur.de",
    "trovit.de", "jobsuma.de", "yourfirm.de", "empfehlungsbund.de", "azubiyo.de",
    "stellenwerk.de", "jobmensa.de", "f6s.com", "builtin.com", "wellfound.com",
})

# Applicant tracking hosts count as employer-direct: the posting there IS the
# employer's own posting, published through their recruiting system.
ATS_DOMAINS: frozenset[str] = frozenset({
    "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co",
    "jobs.ashbyhq.com", "myworkdayjobs.com", "successfactors.eu",
    "successfactors.com", "jobs.personio.de", "join.com", "smartrecruiters.com",
    "jobs.smartrecruiters.com", "apply.workable.com", "recruitee.com",
    "softgarden.io", "jobs.softgarden.io", "bite-app.de", "campus.jobs",
    "greenhouse.io", "lever.co", "ashbyhq.com", "personio.de", "workable.com",
    "teamtailor.com", "jobvite.com", "icims.com", "taleo.net", "eightfold.ai",
})


def classify_domain(url: str) -> str:
    """"aggregator", "ats" or "employer" for one result URL."""
    host = re.sub(r"^www\.", "", (re.split(r"/+", str(url or ""), maxsplit=3) + [""])[1].lower())
    if not host:
        return "employer"
    for domain in AGGREGATOR_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return "aggregator"
    for domain in ATS_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return "ats"
    return "employer"


def result_priority(url: str) -> int:
    """Higher is better. Employer-direct results outrank aggregators."""
    return {"employer": 2, "ats": 2, "aggregator": 0}[classify_domain(url)]


@dataclass(frozen=True)
class SearchString:
    """One web-search query, with the reason it was generated."""

    text: str
    track: str
    city: str
    kind: str  # "title" or "skill"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("empty search query")


def evidenced_skill_terms(profile: Mapping[str, Any], *,
                          tracks: Sequence[str] = ()) -> list[str]:
    """Advert-style keywords for skills the profile actually evidences.

    Skipped: anything marked `not_evidenced` (today, SAP ABAP). Included:
    skills whose only evidence is a private repository, because searching for a
    Python role is not a claim of Python employment.
    """
    wanted = {str(track) for track in tracks}
    terms: list[str] = []
    for skill in profile.get("skills", []) or []:
        if not isinstance(skill, Mapping):
            continue
        if skill.get("confidence") == "not_evidenced":
            continue
        if wanted and not (set(skill.get("tracks", []) or []) & wanted):
            continue
        label = str(skill.get("name", "")).casefold()
        for needle, mapped in SKILL_QUERY_TERMS.items():
            if needle in label:
                for term in mapped:
                    if term not in terms:
                        terms.append(term)
    return terms


def profile_city_terms(profile: Mapping[str, Any]) -> list[str]:
    cities = (profile.get("search_preferences", {}) or {}).get("locations", []) or []
    return [str(city) for city in cities if str(city).strip()]


def build_queries(profile: Mapping[str, Any], cities: Sequence[str], limit: int, *,
                  tracks: Sequence[CareerTrack] | None = None,
                  today: date | None = None,
                  employer_focused: bool = True) -> list[SearchString]:
    """Interleave title queries and evidence-driven skill queries, fairly.

    Alternates title and skill queries per track so a run never consists only
    of job titles, which is what the task asks for: queries generated from the
    candidate's profile, not merely from a predefined title list. Rotation is
    date-derived, so consecutive days explore different combinations with no
    stored state.
    """
    chosen = tuple(tracks) if tracks is not None else CAREER_TRACKS
    clean_cities = [str(city).strip() for city in cities if str(city).strip()]
    if not clean_cities:
        raise ValueError("At least one city is required")
    if not chosen:
        raise ValueError("At least one career track is required")
    if not 1 <= limit <= 120:
        raise ValueError("query budget must be between 1 and 120")

    offset = (today or date.today()).toordinal()
    order = [chosen[(offset + index) % len(chosen)] for index in range(len(chosen))]
    queries: list[SearchString] = []
    seen: set[str] = set()

    def add(text: str, track: str, city: str, kind: str) -> bool:
        normalized = " ".join(text.split())
        if normalized.casefold() in seen:
            return False
        seen.add(normalized.casefold())
        queries.append(SearchString(text=normalized, track=track, city=city, kind=kind))
        return len(queries) >= limit

    for round_index in range(max(len(track.terms) for track in order)):
        for position, track in enumerate(order):
            city = clean_cities[(offset + round_index + position) % len(clean_cities)]
            hint_de = EMPLOYER_HINTS_DE[(offset + round_index) % len(EMPLOYER_HINTS_DE)]
            hint_en = EMPLOYER_HINTS_EN[(offset + position) % len(EMPLOYER_HINTS_EN)]

            if round_index < len(track.terms):
                term = track.terms[(offset + round_index) % len(track.terms)]
                suffix = hint_de if re.search(r"[äöüß]|entwickl|berater|praktik", term, re.I) else hint_en
                text = f'"{term}" {city} {suffix}' if employer_focused else f'"{term}" {city}'
                if add(text, track.key, city, "title"):
                    return queries

            skills = evidenced_skill_terms(profile, tracks=track.profile_tracks)
            if skills:
                # Pair two evidenced skills with the track's own title so the
                # query describes the candidate, not just a job name.
                first = skills[(offset + round_index) % len(skills)]
                second = skills[(offset + round_index + 1) % len(skills)]
                pair = first if first == second else f"{first} {second}"
                anchor = track.terms[(offset + round_index) % len(track.terms)]
                if add(f'{pair} "{anchor}" {city} {hint_de}', track.key, city, "skill"):
                    return queries
    return queries


def describe_sources(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Diagnostics for `job-agent search-queries`."""
    return {
        "tracks": [track.key for track in CAREER_TRACKS],
        "cities": profile_city_terms(profile),
        "skill_terms": evidenced_skill_terms(profile),
        "excluded_not_evidenced": [
            str(skill.get("name")) for skill in profile.get("skills", []) or []
            if isinstance(skill, Mapping) and skill.get("confidence") == "not_evidenced"
        ],
    }


def resolve_query_tracks(keys: Sequence[str] | None) -> tuple[CareerTrack, ...]:
    return resolve_tracks(keys)
