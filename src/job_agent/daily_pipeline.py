"""Bounded, human-approved daily discovery and AI screening.

Stage A (broad discovery) casts a wide net across career tracks and sources and
applies only noise filters. Stage B (AI matching) reads the full advert and
scores it against evidence. Nothing is written to Google Sheets unless the
caller passes `write=True`, and there is intentionally NO application
submission implementation anywhere in this project.

Pipeline order: discovery -> dedup -> description -> eligibility pre-screen ->
AI matching -> Google Sheets.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

from .ai_matching import (SYSTEM, JobDescription, MatchResult, as_candidate,
                          decide_status, minimal_llm_profile)
from .cache import DescriptionCache, profile_fingerprint
from .candidate_profile import load_candidate_profile
from .career_tracks import (UNCLASSIFIED, CareerTrack, SearchQuery, classify_title,
                            infer_track, resolve_tracks, search_plan, track_quota,
                            track_weights)
from .collectors.arbeitsagentur import ArbeitsagenturCollector, BASearchHit, BASource
from .eligibility import Prescreen, PrescreenStats, prescreen_description
from .job_filter import _role_match, clean, screen_title
from .models import DiscoveredJob
from .sheets import GoogleSheetsRepository
from .sources import (JobSource, NormalizedJob, apply_source_quota,
                      apply_track_quota, deduplicate, interleave_by_source,
                      location_matches, normalize_url, split_locations, today_iso)

log = logging.getLogger("job_agent.daily")

MIN_DESCRIPTION_CHARS = 100

# Hosts a source is allowed to own when a spreadsheet row is resumed. A row
# whose URL points anywhere else is ignored rather than trusted.
SOURCE_HOSTS: dict[str, tuple[str, ...]] = {
    "ba": ("arbeitsagentur.de",),
    "arbeitnow": ("arbeitnow.com",),
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("jobs.lever.co",),
    "ashby": ("jobs.ashbyhq.com", "ashbyhq.com"),
}
# career_page and brave reach arbitrary employer hosts, so a resumed row cannot
# be host-validated against a fixed list. Their rows are re-discovered instead
# of resumed, which is safer than trusting a URL stored in a spreadsheet.
UNRESUMABLE_SOURCES = frozenset({"career_page", "brave"})

# Diverse searches; rotate terms by day so default low-volume runs cover all tracks.
SEARCH_TERMS = (
    "Softwareentwickler", "Java Entwickler", "PHP Entwickler", "Python Entwickler",
    "DevOps Linux", "SAP ABAP", "IT Praktikum",
)


def career_priority(title: str) -> int:
    """Prefer junior technical work over internship listings requiring student status.

    Discovery rank is NOT a fit score and does not decide eligibility.
    """
    name = clean(title)
    if re.search(r"\b(praktik\w*|intern(?:ship)?|werkstudent\w*)\b", name):
        return 0
    if re.search(r"\b(junior|berufseinsteiger|einsteiger|graduate|trainee)\b", name):
        return 4
    # Tiers follow the candidate's evidenced skills: AI/LLM/RAG, Python,
    # backend and Linux/DevOps rank with the other engineering roles rather
    # than below SAP, which the profile marks as not evidenced.
    if re.search(r"\b(ai|ki|llm|rag|machine learning|python|backend|"
                 r"softwareentwickl\w*|software engineer|developer|entwickler|"
                 r"entwicklerin|devops|linux|cloud|data|automation|"
                 r"automatisier\w*|abap|sap)\b", name):
        return 3
    return 2


def ranked_hits(hits: list[BASearchHit]) -> list[BASearchHit]:
    """Stable sort: keep source order within equally relevant career tiers."""
    return sorted(hits, key=lambda h: -career_priority(h.title))


def pending_from_sheet(rows: list[dict[str, Any]]) -> list[BASearchHit]:
    """Reconstruct safe BA IDs so unscored Found rows survive search rotation.

    Never trust arbitrary spreadsheet URLs; detail requests use a validated BA refnr.
    """
    result: list[BASearchHit] = []
    for row in rows:
        jid = str(row.get("Job ID") or "")
        ref = jid.removeprefix("ba:") if jid.startswith("ba:") else ""
        if not re.fullmatch(r"[\w-]{6,80}", ref):
            continue
        if (row.get("Status") != "Found" or row.get("Approval") != "Not reviewed"
                or row.get("Match %") not in ("", None)):
            continue
        if str(row.get("Job URL") or "") != "https://www.arbeitsagentur.de/jobsuche/jobdetail/" + ref:
            continue
        if not all(str(row.get(k) or "").strip() for k in ("Company", "Role", "Location")):
            continue
        result.append(BASearchHit(ref, str(row["Role"]), str(row["Company"]),
                                  str(row["Location"]), str(row["Job URL"])))
    return result


@dataclass
class DailyReport:
    source: str = "Bundesagentur für Arbeit (community API)"
    requests: int = 0
    fetched: int = 0
    screened: int = 0
    new: int = 0
    pending: int = 0
    detail_ready: int = 0
    detail_missing: int = 0
    llm_attempted: int = 0
    llm_scored: int = 0
    review: int = 0
    skip: int = 0
    appended: int = 0
    saved_scores: int = 0
    errors: list[str] = field(default_factory=list)
    preview: list[str] = field(default_factory=list)


def search_pairs(cities: list[str], limit: int, *, today: date | None = None) -> list[tuple[str, str]]:
    if not cities or not 1 <= limit <= 21:
        raise ValueError("cities required; max searches 1..21")
    # All cities are represented first. A predictable daily offset rotates terms.
    rotation = (today or date.today()).toordinal() % len(SEARCH_TERMS)
    terms = SEARCH_TERMS[rotation:] + SEARCH_TERMS[:rotation]
    return [(term, city) for term in terms for city in cities][:limit]


# Spellings that mean Frankfurt am Main. Anything else beginning "frankfurt"
# is rejected, which keeps out Frankfurt (Oder) and the Frankfurter-* suburbs.
FRANKFURT_FORMS = frozenset({
    "frankfurt", "frankfurt am main", "frankfurt (main)", "frankfurt/main",
    "frankfurt a. m.", "frankfurt a.m.", "frankfurt a. main", "frankfurt a.main",
    "frankfurt am main (hessen)", "frankfurt/m.", "frankfurt/m",
})
FRANKFURT_PREFIXES = ("frankfurt am main,", "frankfurt,", "frankfurt a. m.,",
                      "frankfurt a.m.,", "frankfurt/main,", "frankfurt (main),")


def _exact_city(location: str, city: str) -> bool:
    """Strict city equality over every part of a multi-location field.

    Rejects suburbs like 'Bischofsheim bei Frankfurt' and Frankfurt (Oder),
    which is a different city ~500 km away. Accepts a trailing country or
    region, because the ATS boards publish 'Frankfurt, Germany' and combine
    several cities into one string.
    """
    wanted = clean(city)
    for part in split_locations(location) or [str(location or "")]:
        got = clean(part)
        if not got:
            continue
        if wanted == "frankfurt am main":
            if got in FRANKFURT_FORMS or got.startswith(FRANKFURT_PREFIXES):
                return True
        elif got == wanted or got.startswith(wanted + ","):
            return True
    return False


def discover_ba(collector: ArbeitsagenturCollector, preferences: Mapping[str, str],
                *, max_searches: int = 9, per_search: int = 10,
                strict: bool = False) -> tuple[list[Any], DailyReport]:
    cities = [x.strip() for x in preferences.get("Locations", "").split(";") if x.strip()]
    roles = [x.strip() for x in preferences.get("Preferred roles", "").split(";") if x.strip()]
    if not cities or not roles:
        raise ValueError("Profile tab must contain Locations and Preferred roles")
    if not 1 <= per_search <= 50:
        raise ValueError("per_search must be 1..50")
    report = DailyReport()
    seen: set[str] = set()
    selected: list[Any] = []
    for term, city in search_pairs(cities, max_searches):
        report.requests += 1
        try:
            hits = collector.search(term=term, city=city, size=per_search)
        except Exception as exc:
            # Keep going on individual query errors but report source failure clearly.
            report.errors.append(f"Search failed ({term}, {city}): {type(exc).__name__}")
            if strict:
                raise
            continue
        report.fetched += len(hits)
        for hit in hits:
            if hit.job_id in seen or not _exact_city(hit.location, city) or not _role_match(hit.title, roles):
                continue
            seen.add(hit.job_id)
            selected.append(hit)
    report.screened = len(selected)
    if report.requests and len(report.errors) == report.requests:
        raise RuntimeError("Every BA search failed. Endpoint may be blocked/changed; no results saved. " + "; ".join(report.errors[:2]))
    return selected, report


def _discovery(hit: Any) -> DiscoveredJob:
    return DiscoveredJob(job_id=hit.job_id, company=hit.company, role=hit.title,
                         location=hit.location, job_url=hit.url,
                         source="BA Jobsuche (unofficial API)",
                         found_on=date.today().isoformat(),
                         notes="Unverified BA listing; manual approval mandatory")


# --- v0.8: multi-source, skill-based pipeline ------------------------------


@dataclass(frozen=True)
class StageError:
    """One structured failure. Never carries a response body or a credential."""

    stage: str
    kind: str
    source: str = ""
    reference: str = ""
    detail: str = ""

    def __str__(self) -> str:
        where = "/".join(part for part in (self.source, self.reference) if part)
        tail = f": {self.detail}" if self.detail else ""
        return f"[{self.stage}] {self.kind} ({where}){tail}" if where else f"[{self.stage}] {self.kind}{tail}"


@dataclass
class DiscoveryReport:
    """Everything one run observed. Counters only; no candidate identity."""

    queries_planned: int = 0
    requests: int = 0
    fetched: int = 0
    screened: int = 0
    duplicates: int = 0
    new: int = 0
    pending: int = 0
    detail_ready: int = 0
    detail_missing: int = 0
    cache_hits: int = 0
    description_fallbacks: int = 0
    prescreen_blocked: int = 0
    prescreen_flagged: int = 0
    llm_attempted: int = 0
    llm_scored: int = 0
    review: int = 0
    skip: int = 0
    appended: int = 0
    saved_scores: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    # Per-source funnel, so "why did only one source produce rows?" is always
    # answerable from the report instead of needing a debugging session.
    fetched_by_source: dict[str, int] = field(default_factory=dict)
    screened_by_source: dict[str, int] = field(default_factory=dict)
    dropped_city: dict[str, int] = field(default_factory=dict)
    dropped_title: dict[str, int] = field(default_factory=dict)
    # Why each inferred track label was assigned, and what could not be labelled.
    track_evidence: dict[str, list[str]] = field(default_factory=dict)
    unclassified: list[str] = field(default_factory=list)
    # Uncertainty stays visible: an unknown is never folded into a failure.
    by_eligibility: dict[str, int] = field(default_factory=dict)
    potential: int = 0
    source_capped: dict[str, int] = field(default_factory=dict)
    # Assessments recovered from the cache and written without a model call.
    flushed: int = 0
    # Cached assessments whose row was already scored or human-decided.
    recovery_skipped: int = 0
    # Rows deliberately left alone because a human or an earlier run owns them.
    protected_rows: int = 0
    # Jobs closed out without a score, so they stop being re-queued forever.
    closed_ineligible: int = 0
    closed_unreadable: int = 0
    backlog_remaining: int = 0
    # Model failures that did NOT stop the run, and retries spent on them.
    llm_failed: int = 0
    llm_retried: int = 0
    stop_reason: str = ""
    by_track: dict[str, int] = field(default_factory=dict)
    source_failures: dict[str, int] = field(default_factory=dict)
    issues: list[StageError] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    preview: list[str] = field(default_factory=list)
    verification_questions: list[str] = field(default_factory=list)
    source: str = "multi-source"

    def fail(self, error: StageError) -> None:
        self.issues.append(error)
        self.errors.append(str(error))
        log.warning("%s", error)


def resumable_from_sheet(rows: Sequence[Mapping[str, Any]],
                         enabled: Sequence[str] = ()) -> list[NormalizedJob]:
    """Rebuild unscored `Found` rows so work survives search rotation.

    A posting discovered yesterday is usually absent from today's rotated
    queries, so without this the queue would only ever contain fresh hits and a
    job whose description fetch failed once would never be retried.

    Only rows that are still untouched by a human (`Found` / `Not reviewed` /
    no score) are returned, and a row is accepted only when its URL host
    belongs to the source named in its own Job ID.
    """
    allowed = {key for key in enabled} or set(SOURCE_HOSTS)
    result: list[NormalizedJob] = []
    for row in rows:
        job_id = str(row.get("Job ID") or "").strip()
        source_key, _, external_id = job_id.partition(":")
        if not external_id or source_key not in allowed or source_key not in SOURCE_HOSTS:
            continue
        if (row.get("Status") != "Found" or row.get("Approval") != "Not reviewed"
                or row.get("Match %") not in ("", None)):
            continue
        url = str(row.get("Job URL") or "")
        host = normalize_url(url).split("/")[2] if normalize_url(url) else ""
        if not any(host == domain or host.endswith("." + domain)
                   for domain in SOURCE_HOSTS[source_key]):
            continue
        try:
            result.append(NormalizedJob(
                source_key=source_key, source_label=str(row.get("Source") or source_key),
                external_id=external_id, company=str(row.get("Company") or ""),
                role=str(row.get("Role") or ""), location=str(row.get("Location") or ""),
                job_url=url, work_mode=str(row.get("Work mode") or "Unknown"),
                found_on=str(row.get("Found on") or today_iso()),
            ))
        except ValueError:
            continue  # Row is incomplete or malformed; a human can still see it.
    return result


def pending_from_sheet(rows: list[dict[str, Any]]) -> list[BASearchHit]:
    """BA-only view of `resumable_from_sheet`, kept for the v0.7 call shape."""
    canonical = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
    hits: list[BASearchHit] = []
    for job in resumable_from_sheet(rows, enabled=("ba",)):
        if not re.fullmatch(r"[\w-]{6,80}", job.external_id):
            continue
        if job.job_url != canonical + job.external_id:
            continue
        hits.append(BASearchHit(job.external_id, job.role, job.company,
                                job.location, job.job_url))
    return hits


def evidence_score(title: str, skill_terms: Sequence[str]) -> int:
    """How many of the candidate's *evidenced* skills the title names.

    A ranking signal only. It decides which adverts are read and scored first
    when the budget is smaller than the queue; it never becomes a match score,
    never reaches the model, and asserts nothing about professional experience.
    Terms come from `query_builder.evidenced_skill_terms`, which excludes
    anything the profile marks `not_evidenced`.
    """
    text = clean(title)
    hits = 0
    for term in skill_terms:
        token = clean(term)
        if not token:
            continue
        if re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text):
            hits += 1
    return hits


def rank_jobs(jobs: Sequence[NormalizedJob], *,
              skill_terms: Sequence[str] = (),
              track_priority: Mapping[str, int] | None = None) -> list[NormalizedJob]:
    """Order candidates for the limited description and model budgets.

    Sort keys, highest first:
      1. evidenced skills named in the title (verified project/training skills)
      2. the track's priority, i.e. how well the profile supports that track
      3. the v0.7 career tier (junior/technical over internship listings)
    Python's sort is stable, so ties keep each source's own ordering.
    """
    weights = dict(track_priority or {})
    return sorted(jobs, key=lambda job: (
        -evidence_score(job.role, skill_terms),
        -weights.get(job.track, 0),
        -career_priority(job.role),
    ))


def _matches_any_city(location: str, cities: Sequence[str], *,
                      allow_remote_germany: bool = False) -> bool:
    """Accept a hit in any configured city, not only the queried one.

    The BA endpoint searches with a radius, so a Stuttgart query legitimately
    returns a Heilbronn posting that the candidate also wants to see. Suburbs
    and Frankfurt (Oder) are still rejected by `_exact_city`.

    With `allow_remote_germany`, a remote-Germany posting is also accepted.
    Without this, employer-board sources were filtered out entirely: their
    German postings are overwhelmingly advertised as remote rather than against
    one of the three cities.
    """
    if any(_exact_city(location, city) for city in cities):
        return True
    return bool(allow_remote_germany
                and location_matches(location, cities, allow_remote_germany=True))


def discover_jobs(sources: Sequence[JobSource], queries: Sequence[SearchQuery], *,
                  cities: Sequence[str], per_search: int,
                  report: DiscoveryReport,
                  allow_remote_germany: bool = False,
                  tracks_for_titles: Sequence[CareerTrack] | None = None) -> list[NormalizedJob]:
    """Stage A. Run the plan across every source, keeping noise out only.

    A source that fails every query is recorded and skipped; the run continues
    on whatever other sources answered.
    """
    found: list[NormalizedJob] = []
    attempted: dict[str, int] = {}
    # A metered source (Brave) runs its whole query plan in one call and must
    # not be re-invoked per pipeline query.
    single_shot_done: set[str] = set()
    for query in queries:
        for source in sources:
            key = getattr(source, "key", "unknown")
            if getattr(source, "single_shot", False):
                if key in single_shot_done:
                    continue
                single_shot_done.add(key)
            attempted[key] = attempted.get(key, 0) + 1
            report.requests += 1
            # A single-shot source has no keyword search and returns its whole
            # city-filtered set once, so it needs more headroom than one query.
            size = (min(400, max(per_search, 100))
                    if getattr(source, "single_shot", False) else per_search)
            try:
                hits = source.discover(term=query.term, city=query.city, size=size)
            except Exception as exc:  # noqa: BLE001 - isolate per source
                report.source_failures[key] = report.source_failures.get(key, 0) + 1
                report.fail(StageError("discovery", type(exc).__name__, key,
                                       f"{query.term} @ {query.city}"))
                continue
            report.fetched += len(hits)
            report.fetched_by_source[key] = report.fetched_by_source.get(key, 0) + len(hits)
            for hit in hits:
                if not _matches_any_city(hit.location, cities,
                                         allow_remote_germany=allow_remote_germany):
                    report.dropped_city[key] = report.dropped_city.get(key, 0) + 1
                    continue
                decision = screen_title(hit.role)
                if not decision.keep:
                    report.dropped_title[key] = report.dropped_title.get(key, 0) + 1
                    continue
                report.screened_by_source[key] = report.screened_by_source.get(key, 0) + 1
                # A source that carries its own track (Brave) keeps it. For a
                # single-shot source the planned query is unrelated to the
                # posting, so the track is read off the title -- and if the
                # title does not identify one, the posting is UNCLASSIFIED.
                # Inheriting `query.track` here is what mislabelled Cosuno's
                # "Implementation Consultant" and "GTM & Growth Engineer" as
                # cpp_systems: whichever track the day's first query happened
                # to carry became the label.
                track = hit.track
                if not track:
                    if getattr(source, "single_shot", False):
                        match = classify_title(hit.role, tracks=tracks_for_titles)
                        track = match.track if match.confident else UNCLASSIFIED
                        if match.confident:
                            report.track_evidence.setdefault(match.track, [])
                            if str(match) not in report.track_evidence[match.track]:
                                report.track_evidence[match.track].append(str(match))
                        else:
                            report.unclassified.append(f"{hit.source_key}: {hit.role[:70]}")
                    else:
                        track = query.track
                found.append(NormalizedJob(**{**hit.__dict__, "track": track}))
    # Surface non-fatal notes a source collected (robots blocks, missing
    # structured data, unconfigured boards) without failing the run.
    for source in sources:
        for note in list(getattr(source, "errors", []))[:8]:
            report.fail(StageError("discovery", "SourceNote",
                                   getattr(source, "key", "?"), detail=str(note)[:200]))
    report.screened = len(found)

    # A source that answered nothing at all is a configuration or outage
    # problem, not an empty market. Fail loudly only when nothing worked.
    if attempted and all(report.source_failures.get(key, 0) == count
                         for key, count in attempted.items()):
        if len(attempted) == 1:
            only = next(iter(attempted))
            raise RuntimeError(
                f"Every {only.upper()} search failed. Endpoint may be blocked/changed; "
                "no results saved. " + "; ".join(report.errors[:2]))
        raise RuntimeError(
            f"Every search across {len(attempted)} sources failed; no results saved. "
            + "; ".join(report.errors[:2]))
    return found


def failure_attempts(failure: Any) -> int:
    """Retries actually spent on a call, recorded on the failure object."""
    return int(getattr(failure, "retries_used", 0) or 0)


def _assess_with_retries(graph: Any, payload: Mapping[str, Any], *, attempts: int,
                         sleep: Any | None = None
                         ) -> tuple[dict[str, Any] | None, Any]:
    """Invoke the matcher, retrying only genuinely transient failures.

    Returns `(assessment, failure)`. `assessment` is None when every attempt
    failed; `failure` is always a classified `ProviderError` so the caller can
    distinguish "rate limited" from "the key is wrong" without ever seeing a
    request or response body.

    A retry is spent only on a transient fault: a rate limit, a timeout, a
    connection error or a 5xx. A malformed response or a rejected request is
    returned immediately, because repeating it changes nothing and costs quota.
    """
    from .providers import ProviderError, classify_exception

    if not 1 <= attempts <= 6:
        raise ValueError("llm_attempts must be between 1 and 6")
    waiter = sleep if sleep is not None else time.sleep
    last: ProviderError | None = None
    for attempt in range(1, attempts + 1):
        try:
            state = graph.invoke(dict(payload))
            assessment = state.get("assessment")
            if not isinstance(assessment, dict):
                raise ProviderError("matcher returned no assessment",
                                    kind="invalid_response")
            if last is not None:
                last.retries_used = attempt - 1
            return assessment, last
        except BaseException as exc:  # noqa: BLE001 - classified and re-raised below
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            failure = classify_exception(exc, provider="matcher")
            failure.retries_used = attempt - 1
            last = failure
            if not failure.retryable or attempt == attempts:
                return None, failure
            # Exponential backoff with full jitter, bounded.
            ceiling = min(20.0, 1.0 * (2 ** (attempt - 1)))
            waiter(random.uniform(0, ceiling))
            log.info("retrying assessment after %s (attempt %d/%d)",
                     failure.kind, attempt + 1, attempts)
    return None, last


def _discovered(job: NormalizedJob) -> DiscoveredJob:
    return DiscoveredJob(
        job_id=job.job_id, company=job.company, role=job.role,
        location=job.location, job_url=job.job_url, source=job.source_label,
        found_on=job.found_on or today_iso(), work_mode=job.work_mode,
        notes="Unverified listing; verify requirements; manual approval mandatory",
    )


def run_daily(*, repository: GoogleSheetsRepository,
              collector: ArbeitsagenturCollector | None = None,
              profile: dict[str, Any], provider: Any | None, write: bool = False,
              sources: Sequence[JobSource] | None = None,
              tracks: Sequence[str] | None = None,
              cache: DescriptionCache | None = None,
              max_searches: int = 33, per_search: int = 10, max_new: int = 12,
              max_matches: int = 6, max_detail: int = 20,
              allow_remote_germany: bool = False,
              source_shares: Mapping[str, float] | None = None,
              source_default_share: float = 0.5,
              source_fill_leftover: bool = False,
              potential_min: int = 65,
              max_detail_attempts: int = 3,
              llm_attempts: int = 3,
              max_consecutive_failures: int = 3,
              llm_sleep: Any | None = None,
              today: date | None = None) -> DiscoveryReport:
    """One bounded daily cycle. Single writer only; no parallel schedulers.

    Safety properties this function must keep:
      - Nothing reaches the spreadsheet unless `write=True`.
      - Only `Found` + `Not reviewed` + unscored rows are ever updated, so a
        human's status, approval, documents and notes are never overwritten.
      - No application is submitted, and no code path exists to submit one.
      - A score is never invented: a job whose advert could not be read, or
        which the pre-screen blocked, keeps its unscored `Found` row.

    `collector` (v0.7) still works and is wrapped as the BA source. Pass
    `sources` to run several boards.
    """
    if not 1 <= max_new <= 50 or not 1 <= max_matches <= 15 or not 1 <= max_detail <= 50:
        raise ValueError("Daily limits out of bounds")

    repository.validate_schema()
    prefs = repository.get_profile()
    threshold = repository.minimum_match()
    if write and prefs.get("Application mode", "").lower() != "approval required":
        raise ValueError("Profile Application mode must remain Approval required")

    cities = [part.strip() for part in prefs.get("Locations", "").split(";") if part.strip()]
    if not cities:
        raise ValueError("Profile tab must contain Locations")

    active: list[JobSource] = list(sources) if sources else [
        BASource(collector if collector is not None else ArbeitsagenturCollector(attempts=3))
    ]
    chosen_tracks: tuple[CareerTrack, ...] = resolve_tracks(tracks)
    queries = search_plan(cities, max_searches, tracks=chosen_tracks, today=today)

    report = DiscoveryReport(queries_planned=len(queries))
    report.source = ", ".join(getattr(source, "label", "?") for source in active)
    log.info("planned %d queries over %d tracks, %d cities, %d sources",
             len(queries), len(chosen_tracks), len(cities), len(active))

    graph = None
    fingerprint = ""
    if provider is not None:
        from .match_workflow import build_match_graph
        graph = build_match_graph(profile=profile, provider=provider, repository=None)
        if cache is not None:
            # Fingerprint the exact evidence + prompt the model will see, so a
            # profile or prompt change re-scores instead of serving stale rows.
            fingerprint = profile_fingerprint(minimal_llm_profile(profile), SYSTEM)

    # Evidenced skills drive the pre-LLM ordering. Built here so no collector
    # ever touches the candidate profile.
    from .query_builder import evidenced_skill_terms
    skill_terms = evidenced_skill_terms(profile)
    priorities = track_weights(chosen_tracks)

    discovered = discover_jobs(active, queries, cities=cities,
                               per_search=per_search, report=report,
                               allow_remote_germany=allow_remote_germany,
                               tracks_for_titles=chosen_tracks)

    rows = repository.list_vacancies(limit=999)
    known_ids = [str(row.get("Job ID") or "") for row in rows]
    known_urls = [str(row.get("Job URL") or "") for row in rows]
    # Global deduplication still runs over every source at once, before any
    # fairness rule, so a per-source cap can never resurrect a duplicate.
    ranked = rank_jobs(discovered, skill_terms=skill_terms, track_priority=priorities)
    unique = deduplicate(ranked, known_ids=known_ids, known_urls=known_urls)
    report.duplicates = unique.duplicates
    # Fair share among the tracks that actually returned something. Dividing by
    # every *enabled* track instead would shrink the quota to one posting on a
    # day when only a few tracks had hits, discarding good results.
    present = {job.track or "_untracked" for job in unique.unique} or {"_untracked"}
    quota = track_quota(max_new, len(present))
    # Fairness in two steps: interleave so every source gets a turn, then cap
    # each source's absolute contribution so the largest feed cannot take the
    # whole budget once the small sources run dry.
    fair = interleave_by_source(unique.unique)
    capped = apply_source_quota(fair, max_new, shares=source_shares,
                                default_share=source_default_share,
                                fill_leftover=source_fill_leftover)
    for job in fair[len(capped):]:
        report.source_capped[job.source_key] = report.source_capped.get(job.source_key, 0) + 1
    new_jobs = apply_track_quota(capped, quota, weights=priorities)[:max_new]
    report.new = len(new_jobs)
    for job in new_jobs:
        report.by_source[job.source_key] = report.by_source.get(job.source_key, 0) + 1
        report.by_track[job.track or "_untracked"] = report.by_track.get(job.track or "_untracked", 0) + 1
    report.preview = [f"{job.source_key} | {job.company} | {job.role} | "
                      f"{job.location} | {job.job_url}" for job in new_jobs[:8]]

    resumable_rows = resumable_from_sheet(
        rows, enabled=tuple(getattr(source, "key", "") for source in active
                            if getattr(source, "key", "") not in UNRESUMABLE_SOURCES))
    resumable = rank_jobs(resumable_rows, skill_terms=skill_terms,
                          track_priority=priorities)
    report.pending = len(resumable)
    # Set before the provider check so a discovery-only run still reports how
    # much scoring work is outstanding.
    report.backlog_remaining = len(resumable)

    if write and new_jobs:
        report.appended = repository.append_discoveries(
            [_discovered(job) for job in new_jobs], max_new=max_new)

    if provider is None or graph is None:
        return report

    # Stage B. Resumable rows first: they are retries that already cost a
    # discovery slot, and leaving them unscored forever would be worse than
    # delaying a fresh hit by one day.
    source_by_key = {getattr(source, "key", ""): source for source in active}
    stats = PrescreenStats()
    prescreens: list[Prescreen] = []
    fetches = 0

    # Step 1, before any model call: write out assessments that were already
    # computed but never reached the sheet. A dry run scores and caches without
    # writing, so without this a `--write` run has to re-earn each score within
    # its budget -- which is how two 85% matches sat unsaved while the sheet
    # still showed them as unscored "Found".
    recovered_ids: set[str] = set()
    if write and cache is not None and fingerprint:
        on_sheet = {job.job_id for job in resumable}
        for job_id, assessment in cache.pending_assessments(fingerprint):
            if job_id not in on_sheet:
                continue  # Not an unscored row we may write to.
            try:
                recovered = MatchResult.from_dict(assessment)
            except ValueError:
                continue
            job = next(item for item in resumable if item.job_id == job_id)
            status = decide_status(recovered.score, recovered.eligibility,
                                   threshold=threshold, potential_min=potential_min)
            raw = {"job_id": job.job_id, "company": job.company, "role": job.role,
                   "location": job.location, "job_url": job.job_url,
                   "description": cache.description_for(job_id) or "",
                   "source": job.source_label, "work_mode": job.work_mode,
                   "found_on": job.found_on or today_iso()}
            try:
                restored = JobDescription.from_dict(raw)
            except ValueError:
                continue
            outcome = repository.record_ai_match(as_candidate(restored, recovered), status)
            cache.mark_saved(job_id, outcome.split(" ")[0])
            # Whatever the outcome, this row has now been dealt with and must
            # leave the scoring queue. Re-entering it caused a second write
            # attempt on an already-scored row and a spurious RowProtected.
            recovered_ids.add(job_id)
            if outcome.startswith(("assessed", "inserted")):
                report.flushed += 1
                report.by_eligibility[recovered.eligibility] = (
                    report.by_eligibility.get(recovered.eligibility, 0) + 1)
                report.preview.append(
                    f"RESTORED {recovered.score}% [{status}/{recovered.eligibility}] "
                    f"{job.company} | {job.role}")
        # Recompute the exclusion from what this pass actually handled.
        # Re-reading `pending_assessments` here is wrong: `mark_saved` has
        # already removed those ids, so the set came back empty and every
        # flushed row stayed in the queue.
        report.recovery_skipped = len(recovered_ids) - report.flushed
        resumable = [job for job in resumable if job.job_id not in recovered_ids]

    # Step 2. Previously saved `Found` rows come FIRST: they are the backlog
    # the tracker is waiting on, and they already cost a discovery slot. Each
    # group is interleaved by source so one feed cannot take the whole model
    # budget, but the groups themselves stay in order.
    report.backlog_remaining = len(resumable)
    queue = interleave_by_source(list(resumable)) + interleave_by_source(list(new_jobs))
    consecutive_failures = 0
    for job in queue:
        if report.llm_attempted >= max_matches:
            break
        source = source_by_key.get(job.source_key)
        if source is None:
            continue

        # Always try for fresh advert text first. Fetching is cheap and bounded;
        # the model call is what costs money. Serving a stale cached description
        # would silently hide an edited advert from re-assessment.
        cached_text = cache.description_for(job.job_id) if cache is not None else None
        description = ""
        if fetches < max_detail:
            fetches += 1
            try:
                description = source.describe(job) or ""
            except Exception as exc:  # noqa: BLE001 - one advert must not stop the run
                report.fail(StageError("description", type(exc).__name__,
                                       job.source_key, job.external_id))
        if len(description) < MIN_DESCRIPTION_CHARS:
            # No usable fresh text: a board source cannot re-describe a posting
            # resumed from the sheet, and a fetch can simply fail. Fall back to
            # the last known good copy rather than losing the job.
            if cached_text and len(cached_text) >= MIN_DESCRIPTION_CHARS:
                description = cached_text
                report.description_fallbacks += 1
                log.info("using cached description for %s", job.job_id)
            else:
                report.detail_missing += 1
                log.info("no usable description for %s", job.job_id)
                if cache is not None:
                    cache.record_attempt(job.job_id, succeeded=False)
                    if (write and cache.attempts(job.job_id) >= max_detail_attempts
                            and job.job_id in {item.job_id for item in resumable}):
                        # Repeatedly unreadable: usually an expired advert.
                        # Close it so it stops consuming a slot every run.
                        outcome = repository.record_terminal_status(
                            job.job_id, "Unreadable",
                            f"Advert could not be retrieved in "
                            f"{cache.attempts(job.job_id)} runs; it may have expired.")
                        if outcome.startswith("closed"):
                            report.closed_unreadable += 1
                continue
        if cache is not None:
            cache.remember_description(job.job_id, description)
            cache.record_attempt(job.job_id, succeeded=True)
        report.detail_ready += 1

        screen = prescreen_description(description, title=job.role)
        stats.record(screen)
        prescreens.append(screen)
        if not screen.eligible:
            # Deterministically ruled out. Closed as `Ineligible` rather than
            # left as `Found`: there is no score to record, but the job must
            # stop re-entering the queue every day. Match % stays empty.
            report.prescreen_blocked += 1
            report.preview.append(f"BLOCKED {job.company} | {job.role} | {screen.summary}")
            if write:
                outcome = repository.record_terminal_status(
                    job.job_id, "Ineligible",
                    "Deterministic pre-screen: " + "; ".join(screen.blockers)[:1200])
                if outcome.startswith("closed"):
                    report.closed_ineligible += 1
                else:
                    report.fail(StageError("sheets", "NotClosed", job.source_key,
                                           job.external_id, outcome))
            continue
        if screen.questions:
            report.prescreen_flagged += 1

        raw_job = {
            "job_id": job.job_id, "company": job.company, "role": job.role,
            "location": job.location, "job_url": job.job_url,
            "description": description, "source": job.source_label,
            "work_mode": job.work_mode, "found_on": job.found_on or today_iso(),
        }
        try:
            description_job = JobDescription.from_dict(raw_job)
        except ValueError as exc:
            report.fail(StageError("validation", "InvalidJobDescription",
                                   job.source_key, job.external_id, str(exc)[:120]))
            continue

        cached = (cache.assessment_for(job.job_id, description, fingerprint)
                  if cache is not None and fingerprint else None)
        if cached is not None:
            report.cache_hits += 1
            assessment = cached.assessment
            log.info("reusing cached assessment for %s (%s)", job.job_id, cached.scored_at)
        else:
            report.llm_attempted += 1
            payload = {"job": raw_job, "threshold": threshold,
                       "potential_min": potential_min,
                       "verification_questions": list(screen.questions),
                       "write": False}
            assessment, failure = _assess_with_retries(
                graph, payload, attempts=llm_attempts, sleep=llm_sleep)
            if assessment is None:
                # One failed assessment must not end the run. The job keeps its
                # unscored `Found` row and is retried tomorrow.
                report.llm_failed += 1
                consecutive_failures += 1
                detail = ("no score saved; job stays Found and will be retried"
                          if not failure.permanent else
                          "no score saved; provider rejected the request")
                report.fail(StageError("matching", failure.kind or "ProviderError",
                                       job.source_key, job.external_id,
                                       f"{failure}; {detail}"))
                if failure.permanent:
                    # A 401/403/404 will fail identically for every remaining
                    # job, so continuing would only waste the run.
                    report.stop_reason = (f"provider rejected the request "
                                          f"(HTTP {failure.status}); "
                                          "check GEMINI_API_KEY and GEMINI_MODEL")
                    break
                if consecutive_failures >= max_consecutive_failures:
                    report.stop_reason = (
                        f"{consecutive_failures} consecutive provider failures; "
                        "stopped to protect the API budget")
                    break
                continue
            report.llm_retried += failure_attempts(failure)
            consecutive_failures = 0
        try:
            result = MatchResult.from_dict(assessment)
        except ValueError as exc:
            report.fail(StageError("matching", "InvalidModelOutput", job.source_key,
                                   job.external_id, str(exc)[:120]))
            continue
        if cache is not None and fingerprint and cached is None:
            cache.remember_assessment(job.job_id, description, fingerprint,
                                      result.as_assessment())

        report.llm_scored += 1
        status = decide_status(result.score, result.eligibility,
                              threshold=threshold, potential_min=potential_min)
        if status == "Review":
            report.review += 1
        elif status == "Potential":
            report.potential += 1
        else:
            report.skip += 1
        # An unknown eligibility is counted as unknown, never as a rejection.
        report.by_eligibility[result.eligibility] = (
            report.by_eligibility.get(result.eligibility, 0) + 1)
        report.preview.append(
            f"AI {result.score}% [{status}/{result.eligibility}] {job.company} | "
            f"{job.role} | {job.job_url}")

        if write:
            outcome = repository.record_ai_match(
                as_candidate(description_job, result), status)
            if cache is not None:
                cache.mark_saved(job.job_id, outcome.split(" ")[0])
            if outcome.startswith(("assessed", "inserted")):
                report.saved_scores += 1
            elif outcome.startswith("protected"):
                # Expected and correct: a human decided, or an earlier run
                # already scored this row. Counted, not warned about.
                report.protected_rows += 1
                log.info("left row untouched for %s: %s", job.job_id, outcome)
            else:
                report.fail(StageError("sheets", "NotWritten", job.source_key,
                                       job.external_id, outcome))

    report.verification_questions = []
    for screen in prescreens:
        for question in screen.questions:
            if question not in report.verification_questions:
                report.verification_questions.append(question)
    if cache is not None:
        try:
            cache.save()
        except OSError as exc:
            # The run's results are already in the sheet. A cache that cannot be
            # persisted costs one repeated model call tomorrow; it must not
            # discard a completed run.
            report.fail(StageError("cache", type(exc).__name__, detail="cache not persisted"))
    log.info("run complete: new=%d pending=%d scored=%d review=%d skip=%d cache_hits=%d",
             report.new, report.pending, report.llm_scored, report.review,
             report.skip, report.cache_hits)
    return report
