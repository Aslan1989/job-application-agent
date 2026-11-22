"""Public, allowlisted GitHub project metadata sync.

This is a READ-ONLY ingestion step. It does not clone code, enumerate private
repos, send data to an LLM, modify GitHub or submit applications.
Repository metadata is third-party untrusted data, not agent instructions.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .candidate_profile import public_projects

API_BASE = "https://api.github.com/repos/"
DEFAULT_CACHE_PATH = Path("data/github_projects.json")


def fetch_public_repo(repo_full_name: str, *, session: Any = None) -> dict[str, Any]:
    """Read exactly one allowlisted repo; reject private/forked on response."""
    owner, sep, name = repo_full_name.partition("/")
    if not sep or owner != "Aslan1989" or not name or "/" in name:
        raise ValueError("Only explicitly curated Aslan1989 repos are supported")
    http = session or requests
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "job-application-agent/0.4"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = http.get(API_BASE + quote(owner) + "/" + quote(name),
                        headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("private") is not False:
        raise ValueError(f"Refusing non-public repository: {repo_full_name}")
    if data.get("fork") is True:
        raise ValueError(f"Fork needs manual authorship review: {repo_full_name}")
    return {
        "full_name": repo_full_name,
        "html_url": data.get("html_url") or f"https://github.com/{repo_full_name}",
        "description": str(data.get("description") or "")[:500],
        "language": data.get("language"),
        "topics": [str(t)[:80] for t in data.get("topics", [])[:20]],
        "default_branch": data.get("default_branch"),
        "pushed_at": data.get("pushed_at"),
        "archived": bool(data.get("archived", False)),
        "visibility": "public",
        "fork": False,
        "source": "GitHub REST API, repository metadata only",
    }


def collect_curated_public_evidence(profile: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
    """Never scans the whole account. The local profile is the allowlist."""
    projects = public_projects(profile)
    results = []
    errors = []
    for project in projects:
        repo = project["repository"]
        try:
            item = fetch_public_repo(repo, session=session)
            item["tracks"] = list(project.get("tracks", []))
            item["curated_evidence_points"] = list(project.get("evidence_points", []))
            item["caution"] = project.get("caution", "")
            results.append(item)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            errors.append({"repository":repo,"error":type(exc).__name__})
    return {"schema_version": "0.4", "synced_at": datetime.now(timezone.utc).isoformat(),
            "public_repositories": results, "errors": errors,
            "note":"Read-only public metadata. No private repos, source code, credentials or CV were uploaded."}


def save_cache(result: dict[str, Any], target: Path = DEFAULT_CACHE_PATH) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
