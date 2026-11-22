"""Read-only job source adapters, one per provider.

Every adapter returns `sources.NormalizedJob`, so the pipeline never branches
on the provider. No adapter may log in, bypass a challenge or submit anything.

Source families:
- `ba`, `arbeitnow`: public job feeds covering the whole market.
- `greenhouse`, `lever`, `ashby`: employer-direct ATS boards, driven by the
  verified registry in `data/employers.json`.
- `career_page`: an employer's own site, read only via schema.org JobPosting.
- `brave`: web search for leads, each verified against the employer's page.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # Avoid importing requests-backed modules at package import.
    from ..config import Settings
    from ..employers import Registry
    from ..query_builder import SearchString
    from ..sources import JobSource

log = logging.getLogger("job_agent.collectors")

__all__ = ["build_sources", "SOURCE_KEYS", "ATS_PLATFORMS"]

# Ordered: broad feeds first, then employer-direct, then metered search.
SOURCE_KEYS = ("ba", "arbeitnow", "greenhouse", "lever", "ashby",
               "career_page", "brave")
ATS_PLATFORMS = ("greenhouse", "lever", "ashby")


def build_sources(settings: "Settings", *, cities: Sequence[str] = (),
                  registry: "Registry | None" = None,
                  queries: Sequence["SearchString"] = (),
                  session: Any = None) -> list["JobSource"]:
    """Instantiate the configured sources in a deterministic order.

    A source that cannot do anything useful is left out rather than created
    half-configured: an ATS platform with no board in the registry makes no
    requests, and Brave without an API key or without queries is inactive.
    """
    from ..employers import load_registry, merge_board_overrides
    from ..ratelimit import RateLimiter, RobotsGate
    from .arbeitnow import ArbeitnowCollector, ArbeitnowSource
    from .arbeitsagentur import ArbeitsagenturCollector, BASource
    from .ats import AshbySource, GreenhouseSource, LeverSource

    book = registry if registry is not None else load_registry(settings.employers_file)
    ats_classes = {"greenhouse": GreenhouseSource, "lever": LeverSource,
                   "ashby": AshbySource}
    env_overrides = {"greenhouse": settings.greenhouse_boards,
                     "lever": settings.lever_companies, "ashby": ()}

    built: list["JobSource"] = []
    for key in SOURCE_KEYS:
        if key not in settings.enabled_sources:
            continue

        if key == "ba":
            built.append(BASource(ArbeitsagenturCollector(attempts=3)))

        elif key == "arbeitnow":
            built.append(ArbeitnowSource(
                ArbeitnowCollector(), pages=settings.arbeitnow_pages,
                allow_remote_germany=settings.allow_remote_germany,
                cities=tuple(cities)))

        elif key in ats_classes:
            boards, names = merge_board_overrides(book, key, env_overrides[key])
            if not boards:
                log.info("%s enabled but no board configured; skipping", key)
                continue
            built.append(ats_classes[key](
                boards, session=session, companies=names,
                limiter=RateLimiter.for_source(key),
                allow_remote_germany=settings.allow_remote_germany,
                cities=tuple(cities)))

        elif key == "career_page":
            from .career_page import CareerPageSource
            pages = book.enabled_career_pages()
            if not pages:
                log.info("career_page enabled but no page configured; skipping")
                continue
            built.append(CareerPageSource(
                pages, session=session,
                allow_remote_germany=settings.allow_remote_germany,
                cities=tuple(cities)))

        elif key == "brave":
            from .brave import BraveSearchSource
            if not settings.brave_api_key:
                log.info("brave enabled but BRAVE_SEARCH_API_KEY is unset; skipping")
                continue
            if not queries:
                log.info("brave enabled but no search queries were built; skipping")
                continue
            built.append(BraveSearchSource(
                settings.brave_api_key, session=session, queries=queries,
                results_per_query=settings.brave_results_per_query,
                max_queries=settings.brave_max_queries,
                max_verifications=settings.brave_max_verifications,
                allow_aggregators=settings.brave_allow_aggregators,
                allow_remote_germany=settings.allow_remote_germany,
                cities=tuple(cities),
                robots=RobotsGate("JobApplicationAgent/0.9 (personal job-search project)")))

    if not built:
        raise ValueError(
            "No job source is usable. Check JOB_SOURCES in .env, the boards in "
            f"{settings.employers_file}, and BRAVE_SEARCH_API_KEY.")
    return built
