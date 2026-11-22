import pytest
from job_agent.github_evidence import fetch_public_repo, collect_curated_public_evidence


class FakeResponse:
    def __init__(self, obj): self.obj = obj
    def raise_for_status(self): return None
    def json(self): return self.obj


class FakeHTTP:
    def __init__(self, private=False, fork=False):
        self.calls = []
        self.private = private
        self.fork = fork
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse({"private": self.private, "fork": self.fork,
                             "language": "PHP", "html_url": "https://github.com/Aslan1989/dokuchat"})


def test_public_repo_metadata_is_read_only():
    api = FakeHTTP()
    result = fetch_public_repo("Aslan1989/dokuchat", session=api)
    assert result["visibility"] == "public"
    assert len(api.calls) == 1
    assert api.calls[0][1]["timeout"] == 15
    assert "/repos/Aslan1989/dokuchat" in api.calls[0][0]


def test_private_repo_is_not_ingested():
    with pytest.raises(ValueError, match="non-public"):
        fetch_public_repo("Aslan1989/webserv", session=FakeHTTP(private=True))


def test_fork_requires_review():
    with pytest.raises(ValueError, match="Fork"):
        fetch_public_repo("Aslan1989/osiris", session=FakeHTTP(fork=True))


def test_other_owners_not_allowed():
    with pytest.raises(ValueError, match="Aslan1989"):
        fetch_public_repo("someone_else/unrelated", session=FakeHTTP())


def test_public_allowlist_skips_private_projects():
    profile = {"projects":[
        {"repository":"Aslan1989/dokuchat","visibility":"public","employer_shareable":True,"tracks":["php"]},
        {"repository":"Aslan1989/webserv","visibility":"private","employer_shareable":False,"tracks":["cpp"]}]}
    http = FakeHTTP()
    cache = collect_curated_public_evidence(profile, session=http)
    assert [x["full_name"] for x in cache["public_repositories"]] == ["Aslan1989/dokuchat"]
    assert len(http.calls) == 1
