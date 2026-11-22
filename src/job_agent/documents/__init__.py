"""Application-document generation: tailored CV and cover letter.

Boundaries that define this package:

- `data/candidate_profile.json` is the only source of professional facts. No
  module here invents employment, years of experience, certifications, project
  outcomes or technologies.
- Contact details live in a separate local file and are inserted during
  rendering. They are never part of a model prompt.
- The model writes prose for the cover letter body only. Every factual claim it
  makes is checked against the evidence allowlist afterwards, and a failed
  check falls back to a deterministic template rather than shipping the text.
- Every output is a draft. Nothing in this package submits an application or
  changes an approval.
"""
from __future__ import annotations

__all__ = [
    "ApplicationPackage",
    "ContactDetails",
    "EvidenceSelection",
    "LetterContent",
    "ReviewReport",
    "detect_language",
    "generate_package",
    "load_contact",
    "select_evidence",
]


def __getattr__(name: str):  # pragma: no cover - lazy re-export
    if name in {"EvidenceSelection", "select_evidence", "detect_language"}:
        from . import evidence
        return getattr(evidence, name)
    if name in {"ContactDetails", "load_contact"}:
        from . import contact
        return getattr(contact, name)
    if name in {"LetterContent"}:
        from . import content
        return getattr(content, name)
    if name in {"ApplicationPackage", "ReviewReport", "generate_package"}:
        from . import pipeline
        return getattr(pipeline, name)
    raise AttributeError(name)
