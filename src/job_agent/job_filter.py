"""Conservative prefilter: title and requested city, NOT an AI match score.

Does not infer candidate fit from a job title. False negatives are possible;
future versions can add more sources and structured CV-based matching.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from .models import DiscoveredJob


SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|architect|head|director|leiter|teamleiter|"
    r"bereichsleiter|expert|experte|manager|berufserfahren)\b", re.I
)
IT_CONTEXT = re.compile(
    r"\b(it|software|python|developer|entwickl\w*|programm\w*|informatik|"
    r"computer|technik|backend|frontend|fullstack|full-stack|devops|"
    r"system\w*|linux|cloud|sap|abap|data|web)\b", re.I
)
IT_SOFTWARE = re.compile(
    r"\b(software|python|developer|entwickler(?:in)?|softwareentwickl\w*|"
    r"programmierer(?:in)?|backend|front.?end|full.?stack|webentwickl\w*|"
    r"application engineer|anwendungsentwickl\w*)\b", re.I
)
IT_OPERATIONS = re.compile(
    r"\b(linux|devops|sre|site reliability|system\w*|sysadmin|"
    r"cloud engineer|it[- ]?operations|platform engineer|infrastruktur|"
    r"administrator|network engineer)\b", re.I
)
SAP_TITLE = re.compile(r"\b(sap|abap)\b", re.I)
INTERNSHIP = re.compile(
    r"\b(intern(?:ship)?|praktik\w*|trainee|graduate|berufseinsteiger|"
    r"junior|werkstudent\w*)\b", re.I
)
STUDENT_ONLY = re.compile(r"\b(werkstudent\w*|working student|studentische hilfskraft)\b", re.I)


def clean(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def _city_match(location: str, city: str) -> bool:
    loc, target = clean(location), clean(city)
    if target == "frankfurt am main":
        return bool(re.search(r"\bfrankfurt\b", loc)) and not bool(
            re.search(r"\b(oder|od\.?|an der oder)\b", loc)
        )
    return bool(re.search(r"(?<!\w)" + re.escape(target) + r"(?!\w)", loc))


def _role_match(title: str, preferred_roles: list[str]) -> bool:
    title = clean(title)
    if SENIOR_TITLE.search(title) or STUDENT_ONLY.search(title):
        return False
    targets = clean(" ".join(preferred_roles))
    software = "software" in targets or "python" in targets
    operations = "linux" in targets or "devops" in targets
    sap = "sap" in targets or "abap" in targets
    internship = "intern" in targets or "praktik" in targets
    if sap and SAP_TITLE.search(title) and re.search(
        r"\b(junior|trainee|graduate|einsteiger|entwickl\w*|"
        r"developer|consultant|berater(?:in)?|abap)\b", title
    ):
        return True
    if operations and IT_OPERATIONS.search(title):
        return True
    if software and IT_SOFTWARE.search(title):
        return True
    if internship and INTERNSHIP.search(title) and IT_CONTEXT.search(title):
        return True
    return False


def _profile_values(profile: Mapping[str, str], key: str) -> list[str]:
    return [part.strip() for part in profile.get(key, "").split(";") if part.strip()]


def normalize_matching_jobs(raw_jobs: list[dict[str, Any]], profile: Mapping[str, str]) -> list[DiscoveredJob]:
    """Normalize only city/role candidates. Never manufacture a match percentage."""
    cities = _profile_values(profile, "Locations")
    roles = _profile_values(profile, "Preferred roles")
    if not cities or not roles:
        raise ValueError("Profile must specify Locations and Preferred roles")
    filtered: list[DiscoveredJob] = []
    ids: set[str] = set()
    for raw in raw_jobs:
        title = str(raw.get("title") or "").strip()
        location = str(raw.get("location") or "").strip()
        company = str(raw.get("company_name") or "").strip()
        url = str(raw.get("url") or "").strip()
        slug = str(raw.get("slug") or "").strip()
        parsed = urlparse(url)
        if not title or not company or not location or not slug:
            continue
        if parsed.scheme != "https" or not parsed.netloc:
            continue
        if not any(_city_match(location, city) for city in cities):
            continue
        if not _role_match(title, roles):
            continue
        job_id = "arbeitnow:" + slug
        if job_id in ids:
            continue
        ids.add(job_id)
        is_remote = raw.get("remote") is True
        filtered.append(DiscoveredJob(
            job_id=job_id,
            found_on=date.today().isoformat(),
            company=company,
            role=title,
            location=location,
            work_mode="Remote" if is_remote else "Unknown",
            source="Arbeitnow",
            job_url=url,
            notes="Collected from Arbeitnow (public API); not yet AI-scored. Verify details before applying.",
        ))
    return filtered


@dataclass(frozen=True)
class FilterDiagnostics:
    """Explain the prefilter without changing its selection decisions."""

    fetched: int
    structurally_valid: int
    invalid: int
    city_matches: int
    role_matches: int
    both_matches: int
    top_locations: tuple[tuple[str, int], ...]
    city_only_examples: tuple[tuple[str, str], ...]
    role_only_examples: tuple[tuple[str, str], ...]


def diagnose_matching_jobs(raw_jobs: list[dict[str, Any]],
                           profile: Mapping[str, str],
                           example_limit: int = 4) -> FilterDiagnostics:
    """Count geography and title matches independently for debugging zero results.

    Counts only jobs that have the fields needed for a valid discovery.
    Does not write to Sheets and does not create scores. Examples are bounded.
    """
    if example_limit < 0 or example_limit > 20:
        raise ValueError("example_limit must be 0..20")
    cities = _profile_values(profile, "Locations")
    roles = _profile_values(profile, "Preferred roles")
    if not cities or not roles:
        raise ValueError("Profile must specify Locations and Preferred roles")
    invalid = valid = city_count = role_count = both_count = 0
    locations: Counter[str] = Counter()
    city_only_examples: list[tuple[str, str]] = []
    role_only_examples: list[tuple[str, str]] = []

    for raw in raw_jobs:
        title = str(raw.get("title") or "").strip()
        location = str(raw.get("location") or "").strip()
        company = str(raw.get("company_name") or "").strip()
        url = str(raw.get("url") or "").strip()
        slug = str(raw.get("slug") or "").strip()
        parsed = urlparse(url)
        if not title or not company or not location or not slug or parsed.scheme != "https" or not parsed.netloc:
            invalid += 1
            continue
        valid += 1
        locations[location] += 1
        city_ok = any(_city_match(location, city) for city in cities)
        role_ok = _role_match(title, roles)
        city_count += int(city_ok)
        role_count += int(role_ok)
        both_count += int(city_ok and role_ok)
        if city_ok and not role_ok and len(city_only_examples) < example_limit:
            city_only_examples.append((title, location))
        if role_ok and not city_ok and len(role_only_examples) < example_limit:
            role_only_examples.append((title, location))

    return FilterDiagnostics(
        fetched=len(raw_jobs), structurally_valid=valid, invalid=invalid,
        city_matches=city_count, role_matches=role_count,
        both_matches=both_count, top_locations=tuple(locations.most_common(8)),
        city_only_examples=tuple(city_only_examples),
        role_only_examples=tuple(role_only_examples),
    )


# --- Stage A: broad, skill-based discovery screen (v0.8) -------------------
#
# Stage A is a *noise* filter, not a fit assessment. It rejects only what the
# candidate has explicitly ruled out (clearly senior-only titles, roles that
# require active university enrolment) and otherwise keeps the posting so the
# evidence-based Stage B matcher can read the real requirements. Entry-level
# work is frequently advertised as plain "Software Engineer", so the word
# "Junior" is never required.

SENIOR_ONLY = re.compile(
    r"\b(senior|sr\.?|lead|leiter(?:in)?|teamleiter(?:in)?|bereichsleiter(?:in)?|"
    r"principal|staff|chief|head of|director|vp|vice president|"
    r"architekt(?:in)?|architect|experte|expert|spezialist(?:in)?|"
    r"berufserfahren\w*|erfahrene[rn]?|abteilungsleiter(?:in)?)\b"
    # People-management titles. Scoped to these compounds on purpose: a bare
    # "manager" would also drop "Prozessmanager IT" and "Projektmanager",
    # which the consulting track deliberately searches for as a transferable
    # fit with the candidate's process and management background.
    r"|\b(?:engineering|development|software|product|delivery|program|account|"
    r"sales|marketing|growth|people|office)\s+manager(?:in)?\b", re.I
)
SENIOR_EXPERIENCE = re.compile(r"\b([5-9]|1\d)\s*\+?\s*(jahren?|years?|yrs)\b", re.I)
# Traditional Werkstudent roles require proof of current enrolment.
ENROLMENT_REQUIRED = re.compile(
    r"\b(werkstudent\w*|working student|studentische[rn]? (?:hilfskraft|mitarbeiter\w*)|"
    r"studentenjob|hiwi)\b", re.I
)
# An internship is in scope; a mandatory-semester internship is checked later
# against the advert text, not guessed from the title.
INTERNSHIP_TITLE = re.compile(
    r"\b(praktik\w*|intern|internship|trainee|graduate|volontariat)\b", re.I
)
# Weak relevance signal: broad on purpose so generic titles survive.
TECH_RELEVANT = re.compile(
    r"\b(it|ict|edv|software|softwareentwickl\w*|entwickl\w*|developer|engineer|"
    r"engineering|programmier\w*|programmer|informatik|informatiker(?:in)?|"
    r"computer|technik|technisch\w*|backend|back.?end|frontend|front.?end|"
    r"fullstack|full.?stack|web|webentwickl\w*|devops|sre|cloud|linux|unix|"
    r"system\w*|sysadmin|administrator|netzwerk|network|infrastruktur|"
    r"infrastructure|datenbank\w*|database|sql|data|daten|etl|bi|analytics|"
    r"python|java|php|javascript|typescript|golang|rust|kotlin|scala|ruby|"
    r"c\+\+|cpp|dotnet|\.net|c#|embedded|firmware|sap|abap|erp|hana|"
    r"ki|ai|ml|llm|rag|machine learning|deep learning|künstliche intelligenz|"
    r"consultant|berater(?:in)?|consulting|analyst|anwendungsentwickl\w*|"
    r"application|applikation\w*|digital\w*|automatisier\w*|automation|"
    r"qa|test\w*|security|cyber|devsecops|platform|api|integration|"
    r"prozess\w*|process|quereinsteiger\w*)\b", re.I
)


@dataclass(frozen=True)
class ScreenDecision:
    """Why a posting was kept or dropped, for `--diagnose` output."""

    keep: bool
    reason: str


def screen_title(title: str) -> ScreenDecision:
    """Decide whether a job title is worth fetching the full advert for.

    Ambiguity resolves to keep: a title that is merely unclear about seniority
    is passed through, because only the requirements section can settle it.
    """
    text = clean(title)
    if not text:
        return ScreenDecision(False, "empty title")
    if ENROLMENT_REQUIRED.search(text):
        return ScreenDecision(False, "requires active university enrolment")
    if SENIOR_ONLY.search(text):
        return ScreenDecision(False, "senior/lead title")
    if SENIOR_EXPERIENCE.search(text):
        return ScreenDecision(False, "title demands many years of experience")
    if not TECH_RELEVANT.search(text):
        return ScreenDecision(False, "no technical or IT signal in title")
    if INTERNSHIP_TITLE.search(text):
        # Kept: enrolment requirements are verified against the advert body.
        return ScreenDecision(True, "entry programme (verify enrolment requirement)")
    return ScreenDecision(True, "relevant technical title")


def is_entry_level_candidate(title: str) -> bool:
    return screen_title(title).keep
