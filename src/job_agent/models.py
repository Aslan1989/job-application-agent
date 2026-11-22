"""Typed candidate payload from a future job collector / LLM matcher."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping


@dataclass(frozen=True)
class JobCandidate:
    job_id: str
    company: str
    role: str
    location: str
    job_url: str
    match_score: int
    work_mode: str = "Unknown"
    source: str = "Manual"
    match_explanation: str = ""
    critical_gaps: str = ""
    language: str = "Unknown"
    found_on: str = ""
    notes: str = ""
    # Stored in the optional "Eligibility" column when the sheet has one.
    eligibility: str = "unknown"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobCandidate":
        required = ("job_id", "company", "role", "location", "job_url", "match_score")
        missing = [field for field in required if field not in value or value[field] in ("", None)]
        if missing:
            raise ValueError(f"Missing candidate fields: {', '.join(missing)}")
        score = value["match_score"]
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise ValueError("match_score must be an integer between 0 and 100")
        job_id = str(value["job_id"]).strip()
        if not job_id:
            raise ValueError("job_id must be nonempty")
        job_url = str(value["job_url"]).strip()
        if not job_url.startswith(("https://", "http://")):
            raise ValueError("job_url must be an absolute http(s) URL")
        return cls(
            job_id=job_id,
            company=str(value["company"]).strip(),
            role=str(value["role"]).strip(),
            location=str(value["location"]).strip(),
            job_url=job_url,
            match_score=score,
            work_mode=str(value.get("work_mode", "Unknown")),
            source=str(value.get("source", "Manual")),
            match_explanation=str(value.get("match_explanation", "")),
            critical_gaps=str(value.get("critical_gaps", "")),
            language=str(value.get("language", "Unknown")),
            found_on=str(value.get("found_on") or date.today().isoformat()),
            notes=str(value.get("notes", "")),
            eligibility=str(value.get("eligibility") or "unknown"),
        )


@dataclass(frozen=True)
class DiscoveredJob:
    """Unscored job discovery result; a finding is NOT a recommendation or approval."""
    job_id: str
    company: str
    role: str
    location: str
    job_url: str
    source: str
    found_on: str
    work_mode: str = "Unknown"
    notes: str = ""
