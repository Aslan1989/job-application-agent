"""schema.org JobPosting extraction from an employer page.

Used by the career-page adapter and by search-result verification. The point is
to read what the employer actually published, in a machine-readable standard,
instead of guessing a company or a city out of a page title or a search
snippet. If a page carries no JobPosting, the posting is skipped — an invented
vacancy is worse than a missed one.

Everything parsed here is untrusted third-party data. It is never treated as an
instruction, and `<script>` bodies never reach the description text.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Iterator, Mapping

from .sources import strip_html

log = logging.getLogger("job_agent.jobposting")

MAX_HTML_CHARS = 2_000_000
MAX_BLOCKS = 40


class _LdJsonCollector(HTMLParser):
    """Collects the bodies of <script type="application/ld+json"> blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._capturing = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script" or len(self.blocks) >= MAX_BLOCKS:
            return
        kinds = {(value or "").strip().lower() for name, value in attrs if name == "type"}
        if any("ld+json" in kind for kind in kinds):
            self._capturing = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self._capturing = False
            text = "".join(self._buffer).strip()
            if text:
                self.blocks.append(text)
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._buffer.append(data)


def _walk(node: Any, depth: int = 0) -> Iterator[Mapping[str, Any]]:
    """Yield every mapping in a JSON-LD tree, including @graph members."""
    if depth > 6:
        return
    if isinstance(node, Mapping):
        yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _walk(node[key], depth + 1)
    elif isinstance(node, list):
        for item in node[:200]:
            yield from _walk(item, depth + 1)


def _is_job_posting(node: Mapping[str, Any]) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(value).strip().lower() == "jobposting" for value in kinds)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("name", "legalName", "title", "@value"):
            if isinstance(value.get(key), str):
                return str(value[key]).strip()
    if isinstance(value, list):
        for item in value:
            found = _text(item)
            if found:
                return found
    return ""


def _locality(value: Any, depth: int = 0) -> str:
    """Pull a city out of a Place / PostalAddress, however deeply nested."""
    if depth > 4:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_locality(item, depth + 1) for item in value[:10]]
        return "; ".join(part for part in parts if part)
    if isinstance(value, Mapping):
        for key in ("addressLocality", "addressRegion"):
            if isinstance(value.get(key), str) and value[key].strip():
                city = value[key].strip()
                country = _text(value.get("addressCountry"))
                return f"{city}, {country}" if country else city
        for key in ("address", "jobLocation", "location", "geo"):
            if key in value:
                found = _locality(value[key], depth + 1)
                if found:
                    return found
        name = _text(value.get("name"))
        if name:
            return name
    return ""


def _parse_date(value: Any) -> date | None:
    text = _text(value)[:40]
    if not text:
        return None
    cleaned = text.replace("Z", "+00:00")
    for attempt in (cleaned, cleaned[:10]):
        try:
            return datetime.fromisoformat(attempt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class JobPosting:
    """One employer-published vacancy, as the employer described it."""

    title: str
    company: str
    location: str
    description: str
    url: str = ""
    employment_type: str = ""
    date_posted: str = ""
    valid_through: str = ""
    remote: bool = False
    direct_apply: bool = False

    def expired(self, *, today: date | None = None) -> bool:
        """True only when the employer published a past validThrough date.

        A missing date is unknown, not expired: the project never converts an
        absent statement into a negative fact.
        """
        deadline = _parse_date(self.valid_through)
        if deadline is None:
            return False
        return deadline < (today or date.today())


def _from_node(node: Mapping[str, Any]) -> JobPosting | None:
    title = _text(node.get("title")) or _text(node.get("name"))
    company = _text(node.get("hiringOrganization"))
    location = _locality(node.get("jobLocation"))
    remote = False
    if not location:
        remote_kind = _text(node.get("jobLocationType")).lower()
        applicant = _locality(node.get("applicantLocationRequirements"))
        if "telecommute" in remote_kind:
            remote = True
            location = f"Remote, {applicant}" if applicant else "Remote"
        else:
            location = applicant
    description = strip_html(node.get("description"))
    if not title or not company or not location:
        return None
    employment = _text(node.get("employmentType"))
    if not employment and isinstance(node.get("employmentType"), list):
        employment = ", ".join(_text(item) for item in node["employmentType"][:4])
    return JobPosting(
        title=title[:300], company=company[:300], location=location[:300],
        description=description, url=_text(node.get("url"))[:600],
        employment_type=employment[:120],
        date_posted=_text(node.get("datePosted"))[:40],
        valid_through=_text(node.get("validThrough"))[:40],
        remote=remote or "telecommute" in _text(node.get("jobLocationType")).lower(),
        direct_apply=node.get("directApply") is True,
    )


def extract_job_postings(html: str) -> list[JobPosting]:
    """Every JobPosting in a page's JSON-LD. Empty list means "none published"."""
    collector = _LdJsonCollector()
    try:
        collector.feed(str(html or "")[:MAX_HTML_CHARS])
    except Exception as exc:  # noqa: BLE001 - malformed markup is common
        log.info("could not parse page markup (%s)", type(exc).__name__)
    found: list[JobPosting] = []
    for block in collector.blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            # Some sites emit JS comments or trailing commas inside the block.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", block))
            except json.JSONDecodeError:
                continue
        for node in _walk(data):
            if not _is_job_posting(node):
                continue
            posting = _from_node(node)
            if posting is not None and not any(
                    posting.title == other.title and posting.url == other.url
                    for other in found):
                found.append(posting)
    return found


class _LinkCollector(HTMLParser):
    """Collects href values, for walking a career index page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or len(self.hrefs) >= 2000:
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value.strip())
                break


def extract_links(html: str) -> list[str]:
    collector = _LinkCollector()
    try:
        collector.feed(str(html or "")[:MAX_HTML_CHARS])
    except Exception:  # noqa: BLE001
        return list(collector.hrefs)
    return collector.hrefs
