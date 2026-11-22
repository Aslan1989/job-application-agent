"""Persistent local cache of fetched job descriptions and model assessments.

Purpose: a daily run must not pay for the same Gemini call twice. An assessment
is reused only when the job description, the minimized candidate evidence *and*
the system prompt are all byte-identical to the cached run. Any change to the
profile or the prompt invalidates every entry, because the old score no longer
reflects what the model would now say.

The cache lives on the local disk only. It holds third-party advert text, so it
is treated as untrusted data on read-back and is never sent anywhere but the
configured model provider.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CACHE_PATH = Path("data/description_cache.json")
SCHEMA_VERSION = "0.8"
MAX_DESCRIPTION_CHARS = 30000


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def profile_fingerprint(safe_profile: Mapping[str, Any], system_prompt: str) -> str:
    """Identity of "what the model was asked, with what evidence".

    Includes the prompt so that tightening the instructions re-scores jobs
    instead of silently serving assessments made under the old rules.
    """
    payload = json.dumps({"profile": safe_profile, "prompt": system_prompt},
                         ensure_ascii=False, sort_keys=True)
    return _sha256(payload)


@dataclass(frozen=True)
class CacheHit:
    assessment: dict[str, Any]
    scored_at: str
    saved_to_sheet: bool = False


class DescriptionCache:
    """JSON-backed store keyed by stable job id. Single-process use only."""

    def __init__(self, path: Path = DEFAULT_CACHE_PATH, *, max_entries: int = 500,
                 entries: dict[str, dict[str, Any]] | None = None) -> None:
        if not 1 <= max_entries <= 20000:
            raise ValueError("max_entries must be between 1 and 20000")
        self.path = Path(path)
        self.max_entries = max_entries
        self.entries: dict[str, dict[str, Any]] = entries or {}
        self.hits = 0
        self.misses = 0
        self.dirty = False

    @classmethod
    def load(cls, path: Path = DEFAULT_CACHE_PATH, *, max_entries: int = 500) -> "DescriptionCache":
        """Never fail a run because the cache is missing or corrupt."""
        cache = cls(path, max_entries=max_entries)
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cache
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            return cache
        entries = raw.get("entries")
        if isinstance(entries, dict):
            cache.entries = {str(key): value for key, value in entries.items()
                             if isinstance(value, dict)}
        return cache

    # ---- descriptions -------------------------------------------------

    def description_for(self, job_id: str) -> str | None:
        entry = self.entries.get(str(job_id))
        text = entry.get("description") if isinstance(entry, dict) else None
        return text if isinstance(text, str) and text else None

    def remember_description(self, job_id: str, description: str) -> None:
        text = str(description)[:MAX_DESCRIPTION_CHARS]
        entry = self.entries.setdefault(str(job_id), {})
        if entry.get("description") == text:
            return
        entry["description"] = text
        entry["description_sha"] = _sha256(text)
        entry["seen_at"] = datetime.now(timezone.utc).isoformat()
        entry.pop("assessment", None)  # Description changed: old score is stale.
        entry.pop("profile_sha", None)
        entry.pop("sheet_outcome", None)
        entry.pop("saved_at", None)
        self.dirty = True

    # ---- assessments --------------------------------------------------

    def assessment_for(self, job_id: str, description: str,
                       fingerprint: str) -> CacheHit | None:
        entry = self.entries.get(str(job_id))
        if not isinstance(entry, dict) or not isinstance(entry.get("assessment"), dict):
            self.misses += 1
            return None
        if entry.get("description_sha") != _sha256(str(description)[:MAX_DESCRIPTION_CHARS]):
            self.misses += 1
            return None
        if entry.get("profile_sha") != fingerprint:
            self.misses += 1
            return None
        self.hits += 1
        return CacheHit(assessment=dict(entry["assessment"]),
                        scored_at=str(entry.get("scored_at") or ""),
                        saved_to_sheet=bool(entry.get("sheet_outcome")))

    def remember_assessment(self, job_id: str, description: str, fingerprint: str,
                            assessment: Mapping[str, Any]) -> None:
        text = str(description)[:MAX_DESCRIPTION_CHARS]
        entry = self.entries.setdefault(str(job_id), {})
        entry["description"] = text
        entry["description_sha"] = _sha256(text)
        entry["profile_sha"] = fingerprint
        entry["assessment"] = dict(assessment)
        entry["scored_at"] = datetime.now(timezone.utc).isoformat()
        # A fresh assessment has not reached the sheet yet.
        entry.pop("sheet_outcome", None)
        entry.pop("saved_at", None)
        entry.setdefault("seen_at", entry["scored_at"])
        self.dirty = True

    # ---- progress tracking --------------------------------------------
    #
    # A daily run can only read and score a few adverts, so a backlog of
    # unscored rows is drained over many runs. Recording attempts lets a job
    # that keeps failing move to the back of the queue instead of consuming the
    # same budget slot every day.

    def attempts(self, job_id: str) -> int:
        entry = self.entries.get(str(job_id))
        value = entry.get("fetch_attempts") if isinstance(entry, dict) else None
        return int(value) if isinstance(value, int) else 0

    def record_attempt(self, job_id: str, *, succeeded: bool) -> None:
        entry = self.entries.setdefault(str(job_id), {})
        if succeeded:
            if entry.get("fetch_attempts"):
                entry["fetch_attempts"] = 0
                self.dirty = True
            return
        entry["fetch_attempts"] = self.attempts(job_id) + 1
        entry["last_failure"] = datetime.now(timezone.utc).isoformat()
        self.dirty = True

    def last_attempt(self, job_id: str) -> str:
        entry = self.entries.get(str(job_id))
        if not isinstance(entry, dict):
            return ""
        return str(entry.get("last_failure") or entry.get("scored_at")
                   or entry.get("seen_at") or "")

    def mark_saved(self, job_id: str, outcome: str = "saved") -> None:
        """Remember that this assessment reached the spreadsheet.

        Without this, an assessment computed during a dry run looks identical
        to one already written, so a later `--write` run cannot tell which
        scores still need persisting.
        """
        entry = self.entries.setdefault(str(job_id), {})
        if entry.get("sheet_outcome") == outcome:
            return
        entry["sheet_outcome"] = outcome
        entry["saved_at"] = datetime.now(timezone.utc).isoformat()
        self.dirty = True

    def saved_to_sheet(self, job_id: str) -> bool:
        entry = self.entries.get(str(job_id))
        return bool(isinstance(entry, dict) and entry.get("sheet_outcome"))

    def pending_assessments(self, fingerprint: str) -> list[tuple[str, dict[str, Any]]]:
        """Assessments that are still valid but were never written to the sheet.

        This is what recovers a score computed during a dry run: re-persisting
        it costs no model call, so it must never wait for budget.
        """
        ready: list[tuple[str, dict[str, Any]]] = []
        for job_id, entry in self.entries.items():
            if not isinstance(entry, dict) or entry.get("sheet_outcome"):
                continue
            assessment = entry.get("assessment")
            if not isinstance(assessment, dict):
                continue
            if entry.get("profile_sha") != fingerprint:
                continue  # Stale: the evidence or prompt changed since.
            ready.append((job_id, dict(assessment)))
        ready.sort(key=lambda item: -int(item[1].get("score") or 0))
        return ready

    # ---- persistence --------------------------------------------------

    def prune(self) -> int:
        """Drop the oldest entries beyond `max_entries`; returns removals."""
        if len(self.entries) <= self.max_entries:
            return 0
        ordered = sorted(self.entries.items(),
                         key=lambda item: str(item[1].get("scored_at")
                                              or item[1].get("seen_at") or ""))
        removable = len(self.entries) - self.max_entries
        for key, _ in ordered[:removable]:
            del self.entries[key]
        self.dirty = True
        return removable

    def save(self) -> bool:
        """Atomic, owner-only write. Returns False when nothing changed."""
        if not self.dirty:
            return False
        self.prune()
        payload = json.dumps({"schema_version": SCHEMA_VERSION,
                              "updated_at": datetime.now(timezone.utc).isoformat(),
                              "entries": self.entries},
                             ensure_ascii=False, indent=1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        handle = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload + "\n")
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()
        self.dirty = False
        return True
