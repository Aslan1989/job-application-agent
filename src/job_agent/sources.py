"""Normalized job schema and cross-source deduplication.

Every collector returns `NormalizedJob`, so the pipeline never special-cases a
provider. All text in a `NormalizedJob` is third-party untrusted data: it is
never interpreted as an instruction, and descriptions are HTML-stripped before
they reach a model.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from html.parser import HTMLParser
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

MAX_DESCRIPTION_CHARS = 30000

# Query parameters that identify a posting rather than a tracking campaign.
_KEEP_QUERY_KEYS = ("gh_jid", "jobid", "job_id", "id", "lever-origin", "postingid")
# Everything not in _KEEP_QUERY_KEYS is dropped, so tracking parameters such as
# utm_*, gh_src, fbclid and gclid need no separate list.


def normalize_url(raw: str) -> str:
    """Canonical form used for cross-source duplicate detection.

    Drops scheme case, `www.`, default ports, tracking parameters, fragments and
    a trailing slash. Returns "" for anything that is not an absolute http(s)
    URL, which callers must treat as "no URL key available".
    """
    try:
        parts = urlsplit(str(raw or "").strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    kept: list[str] = []
    for chunk in parts.query.split("&"):
        if not chunk:
            continue
        name = chunk.split("=", 1)[0].lower()
        if name in _KEEP_QUERY_KEYS:
            kept.append(chunk)
    return urlunsplit(("https", host, path, "&".join(sorted(kept)), ""))


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split())


def content_fingerprint(company: str, role: str, location: str) -> str:
    """Secondary duplicate key for the same advert republished on another board.

    Deliberately coarse. It can collide for an employer that runs several
    identical openings in one city, so it only ever suppresses a *duplicate
    discovery*; it never merges rows that already exist in the spreadsheet.
    """
    parts = [_fold(company), _fold(role), _fold(location)]
    if not all(parts):
        return ""
    return "|".join(parts)


@dataclass(frozen=True)
class NormalizedJob:
    """A posting from any source, before any scoring or eligibility decision."""

    source_key: str
    source_label: str
    external_id: str
    company: str
    role: str
    location: str
    job_url: str
    track: str = ""
    work_mode: str = "Unknown"
    found_on: str = ""
    description: str = ""
    remote: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", self.source_key):
            raise ValueError(f"Invalid source key: {self.source_key!r}")
        if not re.fullmatch(r"[\w.:/-]{1,180}", self.external_id or ""):
            raise ValueError(f"Invalid external id for {self.source_key}")
        for name in ("company", "role", "location"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"NormalizedJob.{name} must be nonempty")
        if not normalize_url(self.job_url):
            raise ValueError("NormalizedJob.job_url must be an absolute http(s) URL")

    @property
    def job_id(self) -> str:
        """Stable, source-namespaced spreadsheet key. Must never change shape."""
        return f"{self.source_key}:{self.external_id}"

    @property
    def url_key(self) -> str:
        return normalize_url(self.job_url)

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(self.company, self.role, self.location)

    def with_description(self, description: str) -> "NormalizedJob":
        return NormalizedJob(**{**self.__dict__, "description": description})


class JobSource(Protocol):
    """Read-only adapter. No source may submit, log in or solve a challenge."""

    key: str
    label: str

    def discover(self, *, term: str, city: str, size: int) -> list[NormalizedJob]: ...

    def describe(self, job: NormalizedJob) -> str: ...


@dataclass
class DedupResult:
    unique: list[NormalizedJob] = field(default_factory=list)
    duplicates: int = 0
    by_source: dict[str, int] = field(default_factory=dict)


def deduplicate(jobs: Sequence[NormalizedJob], *,
                known_ids: Sequence[str] = (),
                known_urls: Sequence[str] = ()) -> DedupResult:
    """Collapse duplicates within a run and against rows already in the sheet.

    Three keys, in order of trust: source-namespaced job id, normalized URL,
    then company/role/location fingerprint. The first source to yield a posting
    wins, so a stable primary source keeps its id across days.
    """
    result = DedupResult()
    seen_ids = {str(value).strip() for value in known_ids if str(value).strip()}
    seen_urls = {normalize_url(value) for value in known_urls}
    seen_urls.discard("")
    seen_prints: set[str] = set()
    for job in jobs:
        keys = (job.job_id in seen_ids,
                bool(job.url_key) and job.url_key in seen_urls,
                bool(job.fingerprint) and job.fingerprint in seen_prints)
        if any(keys):
            result.duplicates += 1
            continue
        seen_ids.add(job.job_id)
        if job.url_key:
            seen_urls.add(job.url_key)
        if job.fingerprint:
            seen_prints.add(job.fingerprint)
        result.unique.append(job)
        result.by_source[job.source_key] = result.by_source.get(job.source_key, 0) + 1
    return result


def apply_track_quota(jobs: Sequence[NormalizedJob], quota: int, *,
                      weights: Mapping[str, int] | None = None) -> list[NormalizedJob]:
    """Keep at most `quota * weight` postings per track, preserving input order.

    The weight comes from the track's priority, so a track the candidate's
    profile actually evidences keeps more of what it found. Without it, the
    extra candidates a prioritised track discovers would be found and then
    immediately discarded by a flat cap.
    """
    if quota < 1:
        raise ValueError("quota must be positive")
    share = dict(weights or {})
    counts: dict[str, int] = {}
    kept: list[NormalizedJob] = []
    for job in jobs:
        bucket = job.track or "_untracked"
        allowed = quota * max(1, share.get(bucket, 1))
        if counts.get(bucket, 0) >= allowed:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        kept.append(job)
    return kept


def interleave_by_source(jobs: Sequence[NormalizedJob]) -> list[NormalizedJob]:
    """Round-robin across sources, preserving each source's own ordering.

    Without this, the `max_new` cut went to whichever source the pipeline
    happened to query first: a large feed such as BA filled every slot and the
    employer boards contributed nothing, even when they had good candidates.
    Each source now gets a turn before any source gets a second row.
    """
    buckets: dict[str, list[NormalizedJob]] = {}
    for job in jobs:
        buckets.setdefault(job.source_key, []).append(job)
    order = list(buckets)
    merged: list[NormalizedJob] = []
    position = 0
    while len(merged) < len(jobs):
        progressed = False
        for key in order:
            bucket = buckets[key]
            if position < len(bucket):
                merged.append(bucket[position])
                progressed = True
        if not progressed:
            break
        position += 1
    return merged


def apply_source_quota(jobs: Sequence[NormalizedJob], limit: int, *,
                       shares: Mapping[str, float] | None = None,
                       default_share: float = 1.0,
                       fill_leftover: bool = False) -> list[NormalizedJob]:
    """Cap each source's contribution to a share of the candidate budget.

    Round-robin interleaving alone is not fairness: once the small sources are
    exhausted, the largest feed takes every remaining slot. Measured on a real
    run, 40 BA candidates against 1 Arbeitnow and 1 Ashby produced 28/1/1.

    `shares` maps a source key to its maximum fraction of `limit`;
    `default_share` applies to the rest. The cap is on each source's *absolute*
    contribution, which is the part that can actually be controlled: when the
    other sources have nothing to offer, no allocation rule can raise their
    count, only lower the big one's.

    With `fill_leftover` the unused capacity is handed back out afterwards, in
    the original order, trading strict caps for a fuller budget.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    if default_share < 0:
        raise ValueError("default_share cannot be negative")
    contributing = {job.source_key for job in jobs}
    if len(contributing) <= 1:
        # Nothing to be fair about. Capping a lone source would just discard
        # half the day's candidates for no benefit.
        return list(jobs[:limit])
    allowance: dict[str, int] = {}
    for job in jobs:
        if job.source_key in allowance:
            continue
        share = shares.get(job.source_key, default_share) if shares else default_share
        if share < 0:
            raise ValueError(f"share for {job.source_key} cannot be negative")
        # At least one slot for any source that is enabled at all: a cap must
        # not silence a source outright.
        allowance[job.source_key] = max(1, int(limit * min(1.0, share)))

    taken: dict[str, int] = {}
    kept: list[NormalizedJob] = []
    deferred: list[NormalizedJob] = []
    for job in jobs:
        if len(kept) >= limit:
            break
        if taken.get(job.source_key, 0) >= allowance[job.source_key]:
            deferred.append(job)
            continue
        taken[job.source_key] = taken.get(job.source_key, 0) + 1
        kept.append(job)
    if fill_leftover:
        for job in deferred:
            if len(kept) >= limit:
                break
            kept.append(job)
    return kept


def today_iso() -> str:
    return date.today().isoformat()


class _Text(HTMLParser):
    """Collect visible text only; script/style content is dropped entirely."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.suppressed += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"} and not self.suppressed:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.suppressed:
            self.suppressed -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"} and not self.suppressed:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def strip_html(value: Any) -> str:
    """Advert HTML to plain text before anything reaches a model or the sheet.

    Some boards double-escape their content, so one extra unescape pass runs
    before parsing. Bounded on input and output; `<script>` bodies never
    survive, which keeps embedded text out of the prompt.
    """
    raw = str(value or "")[:120000]
    if "&lt;" in raw and "<" not in raw:
        raw = unescape(raw)  # Greenhouse ships HTML inside an escaped string.
    parser = _Text()
    parser.feed(raw)
    text = unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_DESCRIPTION_CHARS]


def split_locations(location: str) -> list[str]:
    """Split a multi-location field into its parts.

    Several ATS boards publish one posting against several cities in a single
    string, e.g. "Berlin, Berlin, Germany; Frankfurt a. M.; Hamburg". Without
    splitting, such a posting is invisible to a city filter.
    """
    parts = re.split(r"\s*(?:;|\||\bor\b|/(?![^/]*\bmain\b))\s*", str(location or ""))
    return [part.strip() for part in parts if part.strip()]


def _city_variants(city: str) -> tuple[str, ...]:
    folded = _fold(city)
    if folded == "frankfurt am main":
        # German adverts abbreviate heavily: "Frankfurt a. M.", "Frankfurt/Main",
        # "Frankfurt (Main)". _fold has already stripped the punctuation.
        return ("frankfurt am main", "frankfurt a m", "frankfurt a main",
                "frankfurt main", "frankfurt")
    return (folded,)


REMOTE_GERMANY = re.compile(
    r"\b(remote|home ?office|deutschlandweit|anywhere in germany|germany remote)\b")
_GERMANY = re.compile(r"\b(german|germany|deutschland|de)\b")


def _is_frankfurt_oder(folded: str) -> bool:
    """Frankfurt (Oder) is a different city roughly 500 km away."""
    return bool(re.search(r"\bfrankfurt\b[^a-z0-9]*\boder\b", folded))


def location_matches(location: str, cities: Sequence[str], *,
                     allow_remote_germany: bool = False) -> bool:
    """Loose location match for boards that publish free-text locations.

    Each part of a multi-location string is tested independently. Stricter city
    equality is applied separately by the pipeline for sources that search by
    city server-side.
    """
    for part in split_locations(location) or [str(location or "")]:
        folded = _fold(part)
        if not folded or _is_frankfurt_oder(folded):
            continue
        for city in cities:
            for variant in _city_variants(city):
                if re.search(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", folded):
                    return True
        if allow_remote_germany and REMOTE_GERMANY.search(folded):
            if folded == "remote" or _GERMANY.search(folded):
                return True
    return False


def term_matches(haystack: str, term: str) -> bool:
    """Local full-text screen for boards with no server-side search.

    Every alphanumeric token of the query must appear; short tokens such as
    "C" or "IT" are matched as whole words so "C" does not match "Scala".
    """
    text = _fold(haystack)
    tokens = [token for token in _fold(term).split() if token]
    if not tokens:
        return False
    return all(re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text) for token in tokens)
