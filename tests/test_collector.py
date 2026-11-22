from __future__ import annotations

import pytest

from job_agent.collectors.arbeitnow import ArbeitnowCollector
from job_agent.job_filter import normalize_matching_jobs, diagnose_matching_jobs
from job_agent.sheets import GoogleSheetsRepository, HEADERS


PROFILE = {
    "Locations": "Frankfurt am Main; Stuttgart; Heilbronn",
    "Preferred roles": "Junior Software Developer; Junior Python Developer; Junior Linux/DevOps; Junior SAP/ABAP; IT Internship",
}


def job(slug, title, location, **extra):
    return {
        "slug": slug,
        "title": title,
        "location": location,
        "company_name": "Example GmbH",
        "url": f"https://www.arbeitnow.com/view/{slug}",
        "remote": False,
        **extra,
    }


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.pages = []
    def get(self, url, **kwargs):
        self.pages.append(kwargs["params"]["page"])
        idx = kwargs["params"]["page"]
        if idx == 1:
            return Response({"data": [job("x", "Junior Python Developer", "Frankfurt am Main")], "links": {"next": "yes"}})
        return Response({"data": [job("x", "Junior Python Developer", "Frankfurt am Main"), job("y", "SAP ABAP Trainee", "Heilbronn")], "links": {"next": None}})


def test_fetch_is_bounded_and_deduplicates():
    sess = Session()
    collector = ArbeitnowCollector(session=sess)
    found = collector.fetch(pages=5)
    assert [j["slug"] for j in found] == ["x", "y"]
    assert sess.pages == [1, 2]
    with pytest.raises(ValueError, match="pages"):
        collector.fetch(pages=99)


def test_filters_use_city_and_role_but_never_fake_ai_score():
    feed = [
        job("j1", "Junior Python Developer", "Frankfurt am Main"),
        job("j2", "Senior Python Developer", "Stuttgart"),
        job("j3", "Junior Linux Engineer", "Heilbronn"),
        job("j4", "Junior Java Developer", "Berlin"),
        job("j5", "SAP ABAP Trainee", "Stuttgart"),
        job("j6", "Werkstudent Softwareentwicklung", "Frankfurt am Main"),
        job("j7", "Junior Python Developer", "Frankfurt (Oder)"),
        job("j8", "IT Praktikum", "Stuttgart"),
        job("j9", "Marketing Praktikum", "Stuttgart"),
    ]
    result = normalize_matching_jobs(feed, PROFILE)
    assert [j.job_id for j in result] == ["arbeitnow:j1", "arbeitnow:j3", "arbeitnow:j5", "arbeitnow:j8"]
    assert all(not hasattr(j, "match_score") for j in result)


def test_only_https_and_configured_profile():
    bad = job("x", "Junior Python Developer", "Stuttgart", url="javascript:alert(1)")
    assert normalize_matching_jobs([bad], PROFILE) == []
    with pytest.raises(ValueError, match="Locations"):
        normalize_matching_jobs([job("x", "Junior Python Developer", "Stuttgart")], {})


class FakeCall:
    def __init__(self, result): self.result = result
    def execute(self): return self.result


class FakeValues:
    def __init__(self):
        self.rows = [["arbeitnow:old"]]
        self.appends = []
    def get(self, **kwargs):
        rng = kwargs["range"]
        if rng.endswith("A1:S1") or rng.endswith("A1:T1"):
            return FakeCall({"values": [HEADERS]})
        if rng.endswith("A2:A1000"):
            return FakeCall({"values": self.rows})
        raise AssertionError(f"unexpected range: {rng}")
    def append(self, **kwargs):
        self.appends.append(kwargs)
        for row in kwargs["body"]["values"]:
            self.rows.append([row[0]])
        return FakeCall({"updates": {}})


class FakeService:
    def __init__(self, values): self._values = values
    def spreadsheets(self): return self
    def values(self): return self._values


def test_only_new_jobs_appended_unscored_and_unapproved():
    api = FakeValues()
    repo = GoogleSheetsRepository(FakeService(api), "sheet-id")
    results = normalize_matching_jobs([
        job("old", "Junior Python Developer", "Stuttgart"),
        job("new", "Junior Python Developer", "Stuttgart"),
        job("new", "Junior Python Developer", "Stuttgart"),
    ], PROFILE)
    assert repo.append_discoveries(results) == 1
    write = api.appends[0]
    assert write["valueInputOption"] == "RAW"
    values = write["body"]["values"][0]
    assert len(values) == 19
    assert values[0] == "arbeitnow:new"
    assert values[8] == ""  # Match % deliberately blank
    assert values[12:14] == ["Found", "Not reviewed"]
    assert values[16] == ""
    assert repo.append_discoveries(results) == 0
    assert len(api.appends) == 1


def test_diagnostics_explain_both_filters_without_writing():
    feed = [
        job("a", "Junior Python Developer", "Frankfurt am Main"),
        job("b", "Senior Python Developer", "Stuttgart"),
        job("c", "Junior Linux Engineer", "Berlin"),
        job("d", "Marketing Praktikum", "Heilbronn"),
        job("e", "Junior Python Developer", "Frankfurt (Oder)"),
        job("f", "SAP ABAP Trainee", "Stuttgart"),
        job("g", "Junior Python Developer", "Stuttgart", url="http://unsafe.test"),
    ]
    d = diagnose_matching_jobs(feed, PROFILE)
    assert d.fetched == 7
    assert d.structurally_valid == 6
    assert d.invalid == 1
    assert d.city_matches == 4
    assert d.role_matches == 4
    assert d.both_matches == 2
    assert d.city_only_examples == (("Senior Python Developer", "Stuttgart"), ("Marketing Praktikum", "Heilbronn"))
    assert d.role_only_examples == (("Junior Linux Engineer", "Berlin"), ("Junior Python Developer", "Frankfurt (Oder)"))
    assert d.top_locations[0][0] in {"Frankfurt am Main", "Stuttgart", "Berlin", "Heilbronn", "Frankfurt (Oder)"}


def test_diagnostics_zero_and_bounded_examples():
    jobs = [job("x", "Senior Accountant", "Frankfurt am Main"),
            job("y", "Junior Python Developer", "Berlin")]
    d = diagnose_matching_jobs(jobs, PROFILE, example_limit=0)
    assert d.city_matches == 1 and d.role_matches == 1 and d.both_matches == 0
    assert d.city_only_examples == () and d.role_only_examples == ()
    with pytest.raises(ValueError, match="example_limit"):
        diagnose_matching_jobs(jobs, PROFILE, example_limit=99)
