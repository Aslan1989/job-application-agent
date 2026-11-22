"""A stateful in-memory stand-in for the Google Sheets values API.

`test_sheets.FakeValues` records writes but never applies them, which is right
for single-call assertions. Repeated-run and write-protection tests need the
sheet to actually change, so this double applies appends and ranged updates to
real row storage, exactly as the live API would.

Nothing here talks to Google. The real `GoogleSheetsRepository` is driven on
top of it, so the range arithmetic under test is the production code's own.
"""
from __future__ import annotations

import re
from typing import Any

from job_agent.sheets import EXTENDED_HEADERS, HEADERS

_RANGE = re.compile(
    r"^'?(?P<tab>[^'!]+)'?!(?P<c1>[A-Z]+)(?P<r1>\d+)(?::(?P<c2>[A-Z]+)(?P<r2>\d+))?$")


def column_index(letters: str) -> int:
    """'A' -> 0, 'T' -> 19."""
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


class Call:
    def __init__(self, result: dict[str, Any]):
        self.result = result

    def execute(self) -> dict[str, Any]:
        return self.result


class StatefulValues:
    """Applies writes to in-memory rows and logs every mutation."""

    def __init__(self, *, headers: list[str] | None = None,
                 rows: list[list[Any]] | None = None,
                 profile: list[list[Any]] | None = None) -> None:
        self.headers = list(headers if headers is not None else HEADERS)
        self.width = len(self.headers)
        self.rows: list[list[Any]] = [self._pad(row) for row in (rows or [])]
        self.profile = profile or [
            ["Locations", "Frankfurt am Main; Stuttgart; Heilbronn"],
            ["Preferred roles", "Junior Software Developer; Junior Python Developer"],
            ["Min match score", 80],
            ["Application mode", "Approval required"],
        ]
        self.writes: list[tuple[str, dict[str, Any]]] = []

    # --- helpers -------------------------------------------------------

    def _pad(self, row: list[Any]) -> list[Any]:
        return list(row) + [""] * max(0, self.width - len(row))

    def row_by_id(self, job_id: str) -> dict[str, Any] | None:
        for row in self.rows:
            if row and str(row[0]) == job_id:
                return dict(zip(EXTENDED_HEADERS, self._pad(row)))
        return None

    @property
    def job_ids(self) -> list[str]:
        return [str(row[0]) for row in self.rows if row and str(row[0]).strip()]

    # --- values API ----------------------------------------------------

    def get(self, **kwargs) -> Call:
        a1 = kwargs["range"]
        match = _RANGE.match(a1)
        assert match, f"unparsable range {a1!r}"
        if match.group("tab").endswith("Profile") or a1.endswith("!A2:B100"):
            return Call({"values": self.profile})
        first, last = column_index(match.group("c1")), column_index(match.group("c2") or match.group("c1"))
        start_row = int(match.group("r1"))
        if start_row == 1:  # header row request
            header = self.headers[first:last + 1]
            return Call({"values": [header]} if any(header) else {})
        window = [self._pad(row)[first:last + 1] for row in self.rows]
        # Google omits trailing empty cells and trailing empty rows.
        trimmed = []
        for row in window:
            while row and str(row[-1]).strip() == "":
                row = row[:-1]
            trimmed.append(row)
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        return Call({"values": trimmed})

    def append(self, **kwargs) -> Call:
        self.writes.append(("append", kwargs))
        assert kwargs["valueInputOption"] == "RAW", "formula injection guard"
        for row in kwargs["body"]["values"]:
            assert len(row) <= self.width, f"row wider than the sheet: {len(row)}"
            self.rows.append(self._pad(row))
        return Call({"updates": {"updatedRows": len(kwargs["body"]["values"])}})

    def update(self, **kwargs) -> Call:
        self.writes.append(("update", kwargs))
        assert kwargs["valueInputOption"] == "RAW", "formula injection guard"
        match = _RANGE.match(kwargs["range"])
        assert match, kwargs["range"]
        first = column_index(match.group("c1"))
        row_number = int(match.group("r1"))
        values = kwargs["body"]["values"][0]
        if row_number == 1:  # header write (schema migration)
            while len(self.headers) <= first + len(values) - 1:
                self.headers.append("")
            for offset, value in enumerate(values):
                self.headers[first + offset] = value
            self.width = len(self.headers)
            self.rows = [self._pad(row) for row in self.rows]
            return Call({"updatedRows": 1})
        target = self.rows[row_number - 2]
        for offset, value in enumerate(values):
            target[first + offset] = value
        return Call({"updatedRows": 1})


class StatefulService:
    def __init__(self, values: StatefulValues):
        self.fake_values = values

    def spreadsheets(self):
        return self

    def values(self):
        return self.fake_values
