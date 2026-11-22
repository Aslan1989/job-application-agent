"""Configuration. Credentials never belong in Google Sheets or git."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

DEFAULT_SHEET_ID = "1pN3Y1090EVvfTal_-1N1bU-h-3TCWQ2POt-vYdcOmCs"

# Sources that need no paid API and no account. Brave is deliberately absent:
# it requires an API key, so it is opt-in via JOB_SOURCES even when a key exists.
FREE_SOURCES: tuple[str, ...] = ("ba", "arbeitnow", "greenhouse", "lever",
                                 "ashby", "career_page")


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(raw or "").replace(";", ",").split(",")
                 if part.strip())


@dataclass(frozen=True)
class Settings:
    spreadsheet_id: str
    vacancies_tab: str = "Vacancies"
    profile_tab: str = "Profile"
    auth_mode: str = "oauth"
    oauth_client_file: Path = Path("credentials.json")
    token_file: Path = Path("token.json")
    service_account_file: Path = Path("service-account.json")
    send_applications_enabled: bool = False
    # v0.8 discovery configuration. Sources beyond BA stay off until a board
    # token is configured, so no employer is contacted by accident.
    # All free sources on by default. A source with nothing configured skips
    # itself, so this default costs nothing when a board list is empty.
    enabled_sources: tuple[str, ...] = FREE_SOURCES
    greenhouse_boards: tuple[str, ...] = ()
    lever_companies: tuple[str, ...] = ()
    career_tracks: tuple[str, ...] = ()
    allow_remote_germany: bool = True
    arbeitnow_pages: int = 3
    cache_file: Path = Path("data/description_cache.json")
    # v0.9 employer-direct and search-engine discovery.
    employers_file: Path = Path("data/employers.json")
    brave_api_key: str = ""
    brave_max_queries: int = 8
    brave_results_per_query: int = 10
    brave_max_verifications: int = 12
    brave_allow_aggregators: bool = False
    # v0.11 per-source fairness. A share is the maximum fraction of the daily
    # candidate budget (--max-new) one source may contribute.
    source_shares: Mapping[str, float] = field(default_factory=dict)
    source_default_share: float = 0.5
    source_fill_leftover: bool = False
    # Scores at or above this but below the Sheets Review threshold are saved
    # as "Potential" instead of being discarded as "Skip".
    potential_min_score: int = 65

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        raw_send = os.getenv("SEND_APPLICATIONS_ENABLED", "false").strip().lower()
        if raw_send not in {"true", "false"}:
            raise ValueError("SEND_APPLICATIONS_ENABLED must be 'true' or 'false'")
        auth_mode = os.getenv("GOOGLE_AUTH_MODE", "oauth").strip().lower()
        if auth_mode not in {"oauth", "service_account"}:
            raise ValueError("GOOGLE_AUTH_MODE must be oauth or service_account")
        spreadsheet_id = os.getenv("GOOGLE_SHEET_ID", DEFAULT_SHEET_ID).strip()
        if not spreadsheet_id:
            raise ValueError("GOOGLE_SHEET_ID is empty")
        # Default true: measured read-only on 2026-11-21, the verified ATS
        # boards published 0 postings in the three cities but 34 including
        # remote-Germany. With this off, the employer boards return nothing.
        raw_remote = os.getenv("ALLOW_REMOTE_GERMANY", "true").strip().lower()
        if raw_remote not in {"true", "false"}:
            raise ValueError("ALLOW_REMOTE_GERMANY must be 'true' or 'false'")
        greenhouse = _split(os.getenv("GREENHOUSE_BOARDS", ""))
        lever = _split(os.getenv("LEVER_COMPANIES", ""))
        # Default: every free source. v0.9 defaulted to ("ba", "arbeitnow"),
        # which silently bypassed the employer boards in the registry even
        # though they were enabled there -- the registry was never consulted
        # because its *source family* was off.
        sources = _split(os.getenv("JOB_SOURCES", "")) or FREE_SOURCES
        brave_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
        # A metered source with no key would only produce confusing empty
        # reports, so it is dropped rather than left half-enabled.
        sources = tuple(key for key in sources if key != "brave" or brave_key)
        # Board availability is NOT decided here. This used to drop greenhouse
        # and lever whenever their env vars were empty, which disabled them
        # even when data/employers.json listed verified boards -- the second
        # reason the daily run reached only BA and Arbeitnow. `build_sources`
        # merges the registry with these env overrides and skips a platform
        # that ends up with no board, which is the correct place to decide.
        unknown = [key for key in sources
                   if key not in {"ba", "arbeitnow", "greenhouse", "lever",
                                  "ashby", "career_page", "brave"}]
        if unknown:
            raise ValueError("Unknown JOB_SOURCES entries: " + ", ".join(unknown))
        if not sources:
            raise ValueError("JOB_SOURCES left no usable source")
        try:
            pages = int(os.getenv("ARBEITNOW_PAGES", "3"))
        except ValueError as exc:
            raise ValueError("ARBEITNOW_PAGES must be an integer 1..5") from exc
        if not 1 <= pages <= 5:
            raise ValueError("ARBEITNOW_PAGES must be between 1 and 5")
        def bounded(name: str, default: int, low: int, high: int) -> int:  # noqa: E306
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer {low}..{high}") from exc
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
            return value

        raw_aggregators = os.getenv("BRAVE_ALLOW_AGGREGATORS", "false").strip().lower()
        if raw_aggregators not in {"true", "false"}:
            raise ValueError("BRAVE_ALLOW_AGGREGATORS must be 'true' or 'false'")
        shares: dict[str, float] = {}
        for item in _split(os.getenv("SOURCE_SHARES", "")):
            key, _, value = item.partition(":")
            key = key.strip().lower()
            try:
                share = float(value)
            except ValueError as exc:
                raise ValueError("SOURCE_SHARES entries must look like "
                                 "'ba:0.5,ashby:0.3'") from exc
            if not 0.0 <= share <= 1.0:
                raise ValueError(f"SOURCE_SHARES[{key}] must be between 0 and 1")
            shares[key] = share
        try:
            default_share = float(os.getenv("SOURCE_DEFAULT_SHARE", "0.5"))
        except ValueError as exc:
            raise ValueError("SOURCE_DEFAULT_SHARE must be a number 0..1") from exc
        if not 0.0 <= default_share <= 1.0:
            raise ValueError("SOURCE_DEFAULT_SHARE must be between 0 and 1")
        raw_fill = os.getenv("SOURCE_FILL_LEFTOVER", "false").strip().lower()
        if raw_fill not in {"true", "false"}:
            raise ValueError("SOURCE_FILL_LEFTOVER must be 'true' or 'false'")
        potential = bounded("POTENTIAL_MIN_SCORE", 65, 0, 100)
        return cls(
            source_shares=shares,
            source_default_share=default_share,
            source_fill_leftover=(raw_fill == "true"),
            potential_min_score=potential,
            enabled_sources=sources,
            employers_file=Path(os.getenv("EMPLOYERS_FILE", "data/employers.json")),
            brave_api_key=brave_key,
            brave_max_queries=bounded("BRAVE_MAX_QUERIES", 8, 1, 50),
            brave_results_per_query=bounded("BRAVE_RESULTS_PER_QUERY", 10, 1, 20),
            brave_max_verifications=bounded("BRAVE_MAX_VERIFICATIONS", 12, 1, 60),
            brave_allow_aggregators=(raw_aggregators == "true"),
            greenhouse_boards=greenhouse,
            lever_companies=lever,
            career_tracks=_split(os.getenv("CAREER_TRACKS", "")),
            allow_remote_germany=(raw_remote == "true"),
            arbeitnow_pages=pages,
            cache_file=Path(os.getenv("DESCRIPTION_CACHE_FILE", "data/description_cache.json")),
            spreadsheet_id=spreadsheet_id,
            vacancies_tab=os.getenv("GOOGLE_SHEET_VACANCIES_TAB", "Vacancies"),
            profile_tab=os.getenv("GOOGLE_SHEET_PROFILE_TAB", "Profile"),
            auth_mode=auth_mode,
            oauth_client_file=Path(os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "credentials.json")),
            token_file=Path(os.getenv("GOOGLE_TOKEN_FILE", "token.json")),
            service_account_file=Path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")),
            send_applications_enabled=(raw_send == "true"),
        )
