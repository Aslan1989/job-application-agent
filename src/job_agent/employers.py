"""Configurable employer registry for direct-from-source discovery.

`data/employers.json` is the single list of employers the agent may query. It
holds only boards that were confirmed by calling the public API, with the
observed posting counts recorded, so the file states facts rather than guesses.

Design rules:
- No board discovery and no token guessing. An employer is queried only when it
  appears here with `enabled: true`.
- Not every employer uses one of these platforms, and most large Frankfurt,
  Stuttgart and Heilbronn employers do not. `career_pages` and the search
  adapter exist for exactly that gap; an empty platform list is normal.
- A disabled entry is kept rather than deleted, because knowing a board is live
  but currently irrelevant is useful information.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

log = logging.getLogger("job_agent.employers")

DEFAULT_EMPLOYERS_PATH = Path("data/employers.json")
SUPPORTED_PLATFORMS = ("greenhouse", "lever", "ashby")
SCHEMA_VERSION = "0.9"
_BOARD_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}")


@dataclass(frozen=True)
class Employer:
    """One employer board. `observed` is evidence, not a promise."""

    name: str
    platform: str
    board: str
    enabled: bool = True
    verified_on: str = ""
    notes: str = ""
    observed: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("Employer needs a name")
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ValueError(
                f"{self.name}: unsupported platform {self.platform!r}; "
                f"supported: {', '.join(SUPPORTED_PLATFORMS)}")
        if not _BOARD_TOKEN.fullmatch(str(self.board)):
            raise ValueError(f"{self.name}: invalid board token {self.board!r}")

    @property
    def relevant(self) -> int:
        """Postings seen in the target cities, including remote-Germany."""
        return int(self.observed.get("with_remote_germany")
                   or self.observed.get("in_target_cities") or 0)


@dataclass(frozen=True)
class CareerPage:
    """An employer's own careers page, read via schema.org JobPosting only."""

    name: str
    url: str
    enabled: bool = False
    max_links: int = 20
    notes: str = ""

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"{self.name}: career page URL must be absolute https")
        if not 0 <= self.max_links <= 100:
            raise ValueError(f"{self.name}: max_links must be 0..100")

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True)
class Registry:
    employers: tuple[Employer, ...] = ()
    career_pages: tuple[CareerPage, ...] = ()
    note: str = ""
    finding: str = ""

    def enabled_employers(self) -> tuple[Employer, ...]:
        return tuple(item for item in self.employers if item.enabled)

    def enabled_career_pages(self) -> tuple[CareerPage, ...]:
        return tuple(page for page in self.career_pages if page.enabled)

    def boards_for(self, platform: str) -> tuple[str, ...]:
        return tuple(item.board for item in self.enabled_employers()
                     if item.platform == platform)

    def display_names_for(self, platform: str) -> dict[str, str]:
        return {item.board: item.name for item in self.enabled_employers()
                if item.platform == platform}

    def platforms_in_use(self) -> tuple[str, ...]:
        active = {item.platform for item in self.enabled_employers()}
        return tuple(name for name in SUPPORTED_PLATFORMS if name in active)


def load_registry(path: Path | str = DEFAULT_EMPLOYERS_PATH) -> Registry:
    """Read and validate the registry. A missing file yields an empty registry.

    A malformed entry raises: silently dropping an employer the user added
    would look like the board simply had no jobs.
    """
    location = Path(path)
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("no employer registry at %s; ATS sources stay inactive", location)
        return Registry()
    except json.JSONDecodeError as exc:
        raise ValueError(f"{location} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"{location} must contain a JSON object")
    if str(raw.get("schema_version")) != SCHEMA_VERSION:
        raise ValueError(f"{location}: expected schema_version {SCHEMA_VERSION}")

    employers: list[Employer] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw.get("employers", []) or []:
        if not isinstance(entry, Mapping):
            raise ValueError(f"{location}: each employer must be an object")
        observed = entry.get("observed") or {}
        employer = Employer(
            name=str(entry.get("name", "")).strip(),
            platform=str(entry.get("platform", "")).strip().lower(),
            board=str(entry.get("board", "")).strip(),
            enabled=entry.get("enabled", True) is True,
            verified_on=str(entry.get("verified_on", "")),
            notes=str(entry.get("notes", "")),
            observed={str(k): int(v) for k, v in observed.items()
                      if isinstance(v, int)} if isinstance(observed, Mapping) else {},
        )
        key = (employer.platform, employer.board.casefold())
        if key in seen:
            raise ValueError(f"{location}: duplicate board {employer.platform}:{employer.board}")
        seen.add(key)
        employers.append(employer)

    pages: list[CareerPage] = []
    for entry in raw.get("career_pages", []) or []:
        if not isinstance(entry, Mapping):
            raise ValueError(f"{location}: each career page must be an object")
        pages.append(CareerPage(
            name=str(entry.get("name", "")).strip() or "unnamed",
            url=str(entry.get("url", "")).strip(),
            enabled=entry.get("enabled", False) is True,
            max_links=int(entry.get("max_links", 20)),
            notes=str(entry.get("notes", "")),
        ))

    return Registry(employers=tuple(employers), career_pages=tuple(pages),
                    note=str(raw.get("note", "")), finding=str(raw.get("finding", "")))


def merge_board_overrides(registry: Registry, platform: str,
                          extra: Sequence[str] = ()) -> tuple[tuple[str, ...], dict[str, str]]:
    """Combine registry boards with ad-hoc tokens from the environment.

    `.env` overrides stay supported so a board can be tried without editing the
    registry, but anything kept long term belongs in `employers.json` where its
    verification date is recorded.
    """
    boards = list(registry.boards_for(platform))
    names = registry.display_names_for(platform)
    for token in extra:
        clean = str(token).strip()
        if clean and clean not in boards:
            boards.append(clean)
            names.setdefault(clean, clean)
    return tuple(boards), names


def summarize(registry: Registry) -> list[str]:
    """Human-readable registry report for the CLI."""
    lines: list[str] = []
    for employer in registry.employers:
        mark = "on " if employer.enabled else "off"
        seen = employer.observed
        stats = (f"total={seen.get('total', '?')} "
                 f"in_cities={seen.get('in_target_cities', '?')} "
                 f"+remoteDE={seen.get('with_remote_germany', '?')}"
                 if seen else "not probed")
        lines.append(f"  [{mark}] {employer.platform:10} {employer.board:14} "
                     f"{employer.name:16} {stats}")
        if employer.notes:
            lines.append(f"        {employer.notes}")
    for page in registry.career_pages:
        mark = "on " if page.enabled else "off"
        lines.append(f"  [{mark}] career_page {page.name}: {page.url}")
    return lines


def probe_counts(employers: Sequence[Employer], *, build_source: Any,
                 cities: Sequence[str], allow_remote_germany: bool = False) -> dict[str, Any]:
    """Re-check live boards and report what they actually publish now.

    Read-only: one request per board, no writes, no model calls. Used by
    `job-agent employers --probe` so `enabled` can be decided from evidence.
    """
    from .sources import location_matches

    results: dict[str, Any] = {}
    for employer in employers:
        source = build_source(employer)
        if source is None:
            results[f"{employer.platform}:{employer.board}"] = {"error": "unsupported"}
            continue
        try:
            postings = source._load().get(employer.board, [])  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            results[f"{employer.platform}:{employer.board}"] = {"error": type(exc).__name__}
            continue
        in_cities = sum(1 for job in postings if location_matches(job.location, cities))
        with_remote = sum(1 for job in postings
                          if location_matches(job.location, cities,
                                              allow_remote_germany=True))
        results[f"{employer.platform}:{employer.board}"] = {
            "name": employer.name, "total": len(postings),
            "in_target_cities": in_cities, "with_remote_germany": with_remote,
            "errors": list(getattr(source, "errors", [])),
        }
    return results
