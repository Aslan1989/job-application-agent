"""Local, privacy-conscious candidate profile. No outbound API calls."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PROFILE_PATH = Path("data/candidate_profile.json")


def load_candidate_profile(path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("schema_version") != "0.5":
        raise ValueError("Unsupported candidate profile version")
    if not isinstance(profile.get("candidate"), dict):
        raise ValueError("Missing candidate identity block")
    if not isinstance(profile.get("skills"), list) or not isinstance(profile.get("projects"), list):
        raise ValueError("Missing skills/projects")
    safety = profile.get("agent_safety", {})
    if safety.get("requires_manual_approval_for_every_application") is not True or safety.get("may_submit_applications") is not False:
        raise ValueError("Unsafe application settings in profile")
    if safety.get("may_expose_private_repos_to_employer") is not False:
        raise ValueError("Private repositories cannot be employer-visible")
    for project in profile["projects"]:
        if project.get("visibility") != "public" and project.get("employer_shareable") is not False:
            raise ValueError("Private project marked as employer-shareable")
    return profile


def public_projects(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Only explicitly allowlisted, shareable public projects."""
    return [p for p in profile["projects"]
            if p.get("visibility") == "public" and p.get("employer_shareable") is True]


def relevant_projects(profile: dict[str, Any], track: str, *, employer_visible: bool = True) -> list[dict[str, Any]]:
    projects = public_projects(profile) if employer_visible else profile["projects"]
    return [project for project in projects if track in project.get("tracks", [])]


def blocking_review_items(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in profile.get("review_required", []) if item.get("severity") == "blocking"]
