from __future__ import annotations

from typing import Any

import pytest

from job_agent.models import JobCandidate
from job_agent.sheets import (ELIGIBILITY_HEADER, EXTENDED_HEADERS, HEADERS,
                              GoogleSheetsRepository, a1_quote)


class Request:
    def __init__(self, value: dict[str, Any]):
        self.value = value
    def execute(self) -> dict[str, Any]:
        return self.value


class FakeValues:
    def __init__(self, *, headers=HEADERS, ids=None, rows=None, profile=None):
        self.headers = headers
        self.ids = ids or []
        self.rows = rows or []
        self.profile = profile or [["Min match score", 80]]
        self.eligibility_column = [[headers[19]]] if len(headers) > 19 else []
        self.writes = []
    def get(self, **kwargs):
        a1 = kwargs["range"]
        # Google Sheets omits trailing empty cells, so a narrow sheet answers a
        # wide request with only the populated columns.
        if a1.endswith("!A1:S1") or a1.endswith("!A1:T1"):
            return Request({"values": [self.headers]})
        if a1.endswith("!A2:A1000"):
            return Request({"values": self.ids})
        if a1.endswith("!A2:S1000") or a1.endswith("!A2:T1000"):
            return Request({"values": self.rows})
        if a1.endswith("!A2:B100"):
            return Request({"values": self.profile})
        if a1.endswith("!T1:T1000"):
            return Request({"values": self.eligibility_column})
        raise AssertionError(a1)
    def append(self, **kwargs):
        self.writes.append(("append", kwargs))
        return Request({"updates": {"updatedRows": 1}})
    def update(self, **kwargs):
        self.writes.append(("update", kwargs))
        return Request({"updatedRows": 1})


class FakeService:
    def __init__(self, values: FakeValues):
        self.fake_values = values
    def spreadsheets(self):
        return self
    def values(self):
        return self.fake_values


def candidate() -> JobCandidate:
    return JobCandidate.from_dict({
        "job_id": "stable-source-123",
        "company": "=HYPERLINK(\"https://example.com\",\"Bad\")",
        "role": "Junior Python Developer",
        "location": "Frankfurt am Main",
        "job_url": "https://example.org/jobs/123",
        "match_score": 88,
        "match_explanation": "Synthetic test score",
    })


def test_new_candidate_is_not_approved_or_submitted():
    api = FakeValues()
    repo = GoogleSheetsRepository(FakeService(api), "sheet-id")
    assert "inserted" in repo.upsert_candidate(candidate(), "Review")
    method, data = api.writes[0]
    row = data["body"]["values"][0]
    assert method == "append"
    assert data["valueInputOption"] == "RAW"
    assert len(row) == 19
    assert row[0] == "stable-source-123"
    assert row[2].startswith("=HYPERLINK")  # RAW avoids Sheets formula interpretation
    assert row[8] == 88
    assert row[12] == "Review"
    assert row[13] == "Not reviewed"
    assert row[16] == ""  # Applied on


def test_existing_candidate_preserves_manual_decisions():
    api = FakeValues(ids=[["other"], ["stable-source-123"]])
    repo = GoogleSheetsRepository(FakeService(api), "sheet-id")
    outcome = repo.upsert_candidate(candidate(), "Review")
    assert "updated row 3" in outcome
    method, data = api.writes[0]
    assert method == "update"
    assert data["range"] == "'Vacancies'!C3:L3"  # excludes M:N approvals
    assert data["valueInputOption"] == "RAW"
    assert len(data["body"]["values"][0]) == 10


def test_get_approved_requires_two_approvals():
    base = [""] * 19
    good = base.copy(); good[0], good[7], good[12], good[13] = "1", "https://a.example", "Approved", "Approved"
    one = base.copy(); one[0], one[7], one[12], one[13] = "2", "https://b.example", "Review", "Approved"
    two = base.copy(); two[0], two[7], two[12], two[13] = "3", "https://c.example", "Approved", "Not reviewed"
    api = FakeValues(rows=[good, one, two])
    repo = GoogleSheetsRepository(FakeService(api), "sheet-id")
    assert [row["Job ID"] for row in repo.get_approved()] == ["1"]
    assert not api.writes


def test_threshold_from_existing_profile():
    api = FakeValues(profile=[["Min match score", 80], ["Locations", "Frankfurt am Main; Stuttgart; Heilbronn"]])
    repo = GoogleSheetsRepository(FakeService(api), "sheet-id")
    assert repo.minimum_match() == 80
    assert "Heilbronn" in repo.get_profile()["Locations"]


def test_mismatched_headers_stop_write():
    api = FakeValues(headers=["some other header"])
    repo = GoogleSheetsRepository(FakeService(api), "sheet-id")
    with pytest.raises(ValueError, match="header schema"):
        repo.upsert_candidate(candidate(), "Review")
    assert not api.writes


def test_validation_and_a1_quoting():
    with pytest.raises(ValueError, match="integer"):
        JobCandidate.from_dict({"job_id": "a", "company": "c", "role": "r", "location": "l", "job_url": "https://x.com", "match_score": True})
    assert a1_quote("O'Reilly") == "'O''Reilly'"
