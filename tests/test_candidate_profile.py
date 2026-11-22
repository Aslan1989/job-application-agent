import json
from pathlib import Path
import pytest

from job_agent.candidate_profile import (load_candidate_profile, public_projects,
                                         relevant_projects, blocking_review_items)


def test_real_profile_has_no_contact_details():
    profile = load_candidate_profile(Path("data/candidate_profile.json"))
    assert not profile["candidate"]["contact_details_stored"]
    assert "phone" not in json.dumps(profile).lower()
    assert len(blocking_review_items(profile)) == 0
    assert profile["candidate"]["availability"]["display"] == "ab sofort"
    assert "Tinkoff" in json.dumps(profile["professional_background"]["excluded_as_incorrect_by_user"])


def test_public_employer_projects_exclude_private():
    profile = load_candidate_profile(Path("data/candidate_profile.json"))
    public = public_projects(profile)
    assert public
    assert all(p["visibility"] == "public" and p["employer_shareable"] for p in public)
    assert all(p["repository"] != "Aslan1989/webserv" for p in public)
    assert all(p["visibility"] == "public" for p in relevant_projects(profile, "systems_cpp"))


def test_invalid_profile_cannot_share_private_project(tmp_path):
    p = load_candidate_profile(Path("data/candidate_profile.json"))
    p["projects"][-1]["employer_shareable"] = True
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(p))
    with pytest.raises(ValueError, match="Private project"):
        load_candidate_profile(path)


def test_unsafe_auto_submit_is_rejected(tmp_path):
    p = load_candidate_profile(Path("data/candidate_profile.json"))
    p["agent_safety"]["may_submit_applications"] = True
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(p))
    with pytest.raises(ValueError, match="Unsafe"):
        load_candidate_profile(path)
