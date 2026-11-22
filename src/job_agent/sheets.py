"""First working Google Sheets module.

The underlying tracker is a human-managed CRM. Existing status, approval,
CV, application and notes fields are NEVER overwritten during an upsert.
"""
from __future__ import annotations

from typing import Any

from .models import JobCandidate, DiscoveredJob

# "Potential" (score 65..79) joins Review/Skip as a status an assessment may
# set. Existing rows and every human-set status are untouched by its addition.
ASSESSED_STATUSES = ("Review", "Potential", "Skip")

# Outcomes that are not a score. Without them a job the pipeline can never
# assess -- a Werkstudent-only role, an advert that cannot be read -- stays
# "Found" forever and is re-queued every single day.
#   Ineligible: a stated mandatory condition conflicts with the evidence.
#   Unreadable: the advert could not be retrieved after repeated attempts.
# Both are written with an EMPTY Match % on purpose: there is no score, and
# inventing a 0 would misrepresent the job as assessed and rejected.
TERMINAL_STATUSES = ("Ineligible", "Unreadable")
WRITABLE_STATUSES = ASSESSED_STATUSES + TERMINAL_STATUSES

# Columns A:S of the human-managed tracker. Fixed: order and meaning are a
# contract with the spreadsheet's owner and with every formula in it.
HEADERS = [
    "Job ID", "Found on", "Company", "Role", "Location", "Work mode",
    "Source", "Job URL", "Match %", "Match explanation", "Critical gaps",
    "Language", "Status", "Approval", "CV file/link", "Cover letter link",
    "Applied on", "Next follow-up", "Notes",
]

# v0.8 adds one optional column, T, so the model's eligibility verdict is
# filterable instead of being buried in the Critical gaps prose. It is OPTIONAL:
# a 19-column sheet stays fully supported and nothing is written to T until the
# owner runs `job-agent migrate-schema --write`.
ELIGIBILITY_HEADER = "Eligibility"
EXTENDED_HEADERS = HEADERS + [ELIGIBILITY_HEADER]
ELIGIBILITY_COLUMN = "T"


def a1_quote(tab_name: str) -> str:
    """Quote a sheet tab name safely in A1 notation."""
    return "'" + tab_name.replace("'", "''") + "'"


class GoogleSheetsRepository:
    def __init__(self, service: Any, spreadsheet_id: str,
                 vacancies_tab: str = "Vacancies", profile_tab: str = "Profile") -> None:
        if not spreadsheet_id.strip():
            raise ValueError("spreadsheet_id cannot be empty")
        self.service = service
        self.spreadsheet_id = spreadsheet_id
        self.vacancies_tab = vacancies_tab
        self.profile_tab = profile_tab
        # Resolved by validate_schema(); None means "not inspected yet".
        self.has_eligibility_column: bool | None = None

    def _values(self):
        return self.service.spreadsheets().values()

    def _get(self, a1_range: str) -> list[list[Any]]:
        response = self._values().get(
            spreadsheetId=self.spreadsheet_id,
            range=a1_range,
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        return response.get("values", [])

    def validate_schema(self) -> None:
        """Accept the legacy 19-column sheet or the 20-column v0.8 sheet.

        Anything else aborts before a single write, because a shifted column
        would mean writing a score over somebody's approval decision.
        """
        actual = self._get(f"{a1_quote(self.vacancies_tab)}!A1:T1")
        header = list(actual[0]) if actual else []
        # Google omits trailing empty cells, so a 19-column sheet returns 19.
        while header and str(header[-1]).strip() == "":
            header.pop()
        if header == HEADERS:
            self.has_eligibility_column = False
            return
        if header == EXTENDED_HEADERS:
            self.has_eligibility_column = True
            return
        raise ValueError(
            "Vacancies header schema has changed. Expected the existing "
            "19-column tracker: " + ", ".join(HEADERS)
            + f" (optionally followed by '{ELIGIBILITY_HEADER}')"
        )

    def _eligibility_enabled(self) -> bool:
        if self.has_eligibility_column is None:
            self.validate_schema()
        return bool(self.has_eligibility_column)

    def migrate_schema(self, *, write: bool = False) -> str:
        """Add the optional Eligibility header. Opt-in, additive, idempotent.

        Backward compatible by construction: it only ever writes T1, never
        touches A:S, and every other method keeps working against a sheet that
        was never migrated.
        """
        self.validate_schema()
        if self.has_eligibility_column:
            return f"no change needed: column {ELIGIBILITY_COLUMN} is already '{ELIGIBILITY_HEADER}'"
        target = f"{a1_quote(self.vacancies_tab)}!{ELIGIBILITY_COLUMN}1"
        if not write:
            return (f"DRY RUN: would set {target} to '{ELIGIBILITY_HEADER}'; "
                    "existing columns A:S are untouched")
        probe = self._get(f"{a1_quote(self.vacancies_tab)}!{ELIGIBILITY_COLUMN}1:{ELIGIBILITY_COLUMN}1000")
        if any(row and str(row[0]).strip() for row in probe):
            raise ValueError(
                f"Column {ELIGIBILITY_COLUMN} already holds data that is not the "
                f"'{ELIGIBILITY_HEADER}' header. Resolve it manually; refusing to overwrite."
            )
        self._values().update(
            spreadsheetId=self.spreadsheet_id, range=target,
            valueInputOption="RAW", body={"values": [[ELIGIBILITY_HEADER]]},
        ).execute()
        self.has_eligibility_column = True
        return f"added column {ELIGIBILITY_COLUMN} '{ELIGIBILITY_HEADER}' (A:S untouched)"

    def get_profile(self) -> dict[str, str]:
        """Google Sheets 'Profile' is the source of matching preferences."""
        values = self._get(f"{a1_quote(self.profile_tab)}!A2:B100")
        result: dict[str, str] = {}
        for row in values:
            if row and str(row[0]).strip():
                key = str(row[0]).strip()
                result[key] = str(row[1]).strip() if len(row) > 1 else ""
        return result

    def minimum_match(self) -> int:
        raw = self.get_profile().get("Min match score", "80")
        try:
            threshold = int(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("Profile!B6: 'Min match score' must be an integer") from exc
        if not 0 <= threshold <= 100:
            raise ValueError("Min match score must be between 0 and 100")
        return threshold

    def list_vacancies(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 999:
            raise ValueError("limit must be between 1 and 999")
        self.validate_schema()
        rows = self._get(f"{a1_quote(self.vacancies_tab)}!A2:T1000")
        width = len(EXTENDED_HEADERS)
        # The Eligibility key is always present, empty on an unmigrated sheet,
        # so callers never branch on the sheet's width.
        return [dict(zip(EXTENDED_HEADERS, (row + [""] * (width - len(row)))[:width]))
                for row in rows if row and str(row[0]).strip()][:limit]

    def get_approved(self) -> list[dict[str, Any]]:
        """Two explicit gates; this method NEVER submits any applications."""
        return [
            row for row in self.list_vacancies(limit=999)
            if row["Status"] == "Approved" and row["Approval"] == "Approved"
            and row["Job URL"]
        ]

    def status_summary(self, limit: int = 999) -> dict[str, Any]:
        """Read-only tracker overview for the dashboard. Writes nothing."""
        rows = self.list_vacancies(limit=limit)
        counts: dict[str, int] = {}
        approvals: dict[str, int] = {}
        eligibility: dict[str, int] = {}
        sources: dict[str, int] = {}
        unscored_found = 0
        scored = 0
        bands = {"review": 0, "potential": 0, "below": 0}
        for row in rows:
            status = str(row.get("Status") or "(blank)")
            counts[status] = counts.get(status, 0) + 1
            approvals[str(row.get("Approval") or "(blank)")] = approvals.get(
                str(row.get("Approval") or "(blank)"), 0) + 1
            verdict = str(row.get("Eligibility") or "").strip()
            if verdict:
                eligibility[verdict] = eligibility.get(verdict, 0) + 1
            source = str(row.get("Job ID") or "").split(":")[0] or "(none)"
            sources[source] = sources.get(source, 0) + 1
            raw_score = str(row.get("Match %") or "").strip()
            if raw_score in ("", "None"):
                if status == "Found" and row.get("Approval") == "Not reviewed":
                    unscored_found += 1
                continue
            scored += 1
            try:
                score = int(float(raw_score))
            except ValueError:
                continue
            if score >= 80:
                bands["review"] += 1
            elif score >= 65:
                bands["potential"] += 1
            else:
                bands["below"] += 1
        return {"total": len(rows), "status": counts, "approval": approvals,
                "eligibility": eligibility, "by_source": sources,
                "scored": scored, "unscored_found_backlog": unscored_found,
                "score_bands": bands,
                "errors": sum(counts.get(name, 0) for name in TERMINAL_STATUSES)}

    def reband_potential(self, *, potential_min: int = 65, write: bool = False,
                         limit: int = 999) -> dict[str, Any]:
        """Move already-scored rows in the Potential band from Skip to Potential.

        Rows scored before the Potential band existed were written as `Skip`.
        Only a machine-written `Skip` is eligible: the row must still be
        `Approval=Not reviewed`, carry a score inside the band, and have no
        CV, cover letter or applied-on value. Anything a human touched, and
        every other status, is left exactly as it is.
        """
        if not 0 <= potential_min <= 100:
            raise ValueError("potential_min must be between 0 and 100")
        threshold = self.minimum_match()
        rows = self._get(f"{a1_quote(self.vacancies_tab)}!A2:T1000")
        changed: list[dict[str, Any]] = []
        skipped: list[str] = []
        for idx, raw in enumerate(rows, start=2):
            if not raw or not str(raw[0]).strip():
                continue
            row = list(raw) + [""] * max(0, len(EXTENDED_HEADERS) - len(raw))
            score_text = str(row[8]).strip()
            if score_text in ("", "None"):
                continue
            try:
                score = int(float(score_text))
            except ValueError:
                continue
            if not potential_min <= score < threshold:
                continue
            if row[12] != "Skip":
                continue
            if row[13] != "Not reviewed" or any(str(row[i]).strip()
                                                for i in (14, 15, 16)):
                skipped.append(f"row {idx} ({row[0]}): human-owned fields present")
                continue
            changed.append({"row": idx, "job_id": str(row[0]), "score": score})
            if write:
                self._values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{a1_quote(self.vacancies_tab)}!M{idx}",
                    valueInputOption="RAW", body={"values": [["Potential"]]},
                ).execute()
        return {"threshold": threshold, "potential_min": potential_min,
                "eligible_rows": changed, "protected": skipped,
                "written": bool(write) and len(changed)}

    def upsert_candidate(self, job: JobCandidate, initial_status: str) -> str:
        """Append or update a candidate by Job ID; no application submission.

        For existing rows only C:L are updated. This intentionally preserves:
        first-seen date, manual status, manual approval, CV, letter, sent time,
        follow-up and notes. The write uses RAW to avoid formula injection.
        """
        if initial_status not in ASSESSED_STATUSES:
            raise ValueError("initial_status must be one of: "
                             + ", ".join(ASSESSED_STATUSES))
        self.validate_schema()
        ids = self._get(f"{a1_quote(self.vacancies_tab)}!A2:A1000")
        existing_row = next((idx + 2 for idx, row in enumerate(ids)
                             if row and str(row[0]).strip() == job.job_id), None)
        data_c_l = [
            job.company, job.role, job.location, job.work_mode, job.source,
            job.job_url, job.match_score, job.match_explanation,
            job.critical_gaps, job.language,
        ]
        if existing_row is not None:
            self._values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{a1_quote(self.vacancies_tab)}!C{existing_row}:L{existing_row}",
                valueInputOption="RAW",
                body={"values": [data_c_l]},
            ).execute()
            self._write_eligibility(existing_row, job.eligibility)
            return f"updated row {existing_row} (manual decisions preserved)"

        row = [
            job.job_id, job.found_on, *data_c_l,
            initial_status, "Not reviewed", "", "", "", "", job.notes,
        ]
        if self._eligibility_enabled():
            row.append(job.eligibility)
        self._values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{a1_quote(self.vacancies_tab)}!A1:T1000",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return f"inserted ({initial_status}; approval required)"


    def _write_eligibility(self, row_number: int, eligibility: str) -> None:
        """Write column T only. A no-op on a sheet without that column.

        Kept as a separate single-cell update because the alternative, one
        contiguous I:T range, would overwrite Approval, CV links, the applied
        date and Notes — exactly the human-owned columns this class protects.
        """
        if not self._eligibility_enabled() or row_number < 2:
            return
        value = str(eligibility or "").strip()[:40]
        if not value:
            return
        self._values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{a1_quote(self.vacancies_tab)}!{ELIGIBILITY_COLUMN}{row_number}"
                  f":{ELIGIBILITY_COLUMN}{row_number}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()

    def append_discoveries(self, jobs: list[DiscoveredJob], max_new: int = 20) -> int:
        """Append only NEW, UNSCORED real vacancies as Found / Not reviewed.

        Existing rows are NEVER changed, including manual status, approval and
        eventual AI scores. Deduplicates job IDs in a single bounded batch.
        This is a single-user operation; overlapping writers require an external lock.
        """
        if not 1 <= max_new <= 100:
            raise ValueError("max_new must be between 1 and 100")
        if not jobs:
            return 0
        self.validate_schema()
        existing = self._get(f"{a1_quote(self.vacancies_tab)}!A2:A1000")
        seen = {str(row[0]).strip() for row in existing if row and str(row[0]).strip()}
        new_rows: list[list[Any]] = []
        for job in jobs:
            if job.job_id in seen:
                continue
            seen.add(job.job_id)
            discovery_row = [
                job.job_id, job.found_on, job.company, job.role,
                job.location, job.work_mode, job.source, job.job_url,
                "", "", "", "Unknown", "Found", "Not reviewed",
                "", "", "", "", job.notes,
            ]
            if self._eligibility_enabled():
                discovery_row.append("")  # Not assessed yet; never guess.
            new_rows.append(discovery_row)
            if len(new_rows) >= max_new:
                break
        if not new_rows:
            return 0
        self._values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{a1_quote(self.vacancies_tab)}!A1:T1000",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()
        return len(new_rows)


    def record_documents(self, job_id: str, *, cv_reference: str = "",
                         letter_reference: str = "",
                         overwrite: bool = False) -> str:
        """Record generated document references in columns O and P only.

        Protections, all deliberate:
        - Only O (CV file/link) and P (Cover letter link) are ever written.
          Status, Approval, the applied-on date and Notes are untouched, so
          generating documents can never look like an approval.
        - An existing value is preserved unless `overwrite=True` is passed
          explicitly. A human may have put a better document there.
        - A local filesystem path is stored with a `local:` prefix, because it
          is not a link anybody else can open. Use the Drive upload for a real
          URL.
        """
        if not str(job_id).strip():
            raise ValueError("job_id is required")
        if not cv_reference and not letter_reference:
            raise ValueError("nothing to record")
        self.validate_schema()
        rows = self._get(f"{a1_quote(self.vacancies_tab)}!A2:T1000")
        for idx, raw in enumerate(rows, start=2):
            if not raw or str(raw[0]).strip() != str(job_id).strip():
                continue
            row = list(raw) + [""] * max(0, len(EXTENDED_HEADERS) - len(raw))
            existing_cv, existing_letter = str(row[14]), str(row[15])
            if not overwrite and (existing_cv.strip() or existing_letter.strip()):
                return (f"protected existing documents on row {idx} "
                        "(pass overwrite to replace them)")
            values = [cv_reference or existing_cv, letter_reference or existing_letter]
            self._values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{a1_quote(self.vacancies_tab)}!O{idx}:P{idx}",
                valueInputOption="RAW", body={"values": [values]},
            ).execute()
            return f"recorded documents on row {idx} (status and approval unchanged)"
        return f"no row found for {job_id}"

    def record_terminal_status(self, job_id: str, status: str, reason: str) -> str:
        """Close out a job the pipeline can never score, without inventing one.

        Same protection as `record_ai_match`: only a row that is still
        `Found` / `Not reviewed` / unscored is touched, so a human decision or
        an existing assessment is never overwritten. Match % stays empty.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError("terminal status must be one of: "
                             + ", ".join(TERMINAL_STATUSES))
        if not str(job_id).strip():
            raise ValueError("job_id is required")
        self.validate_schema()
        rows = self._get(f"{a1_quote(self.vacancies_tab)}!A2:T1000")
        for idx, row in enumerate(rows, start=2):
            if not row or str(row[0]).strip() != str(job_id).strip():
                continue
            row = list(row) + [""] * max(0, len(EXTENDED_HEADERS) - len(row))
            if row[12] != "Found" or row[13] != "Not reviewed" or row[8] not in ("", None):
                return f"protected existing row {idx} (human status or score preserved)"
            self._values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{a1_quote(self.vacancies_tab)}!I{idx}:M{idx}",
                valueInputOption="RAW",
                body={"values": [["", "", str(reason)[:3000],
                                  row[11] or "Unknown", status]]},
            ).execute()
            if status == "Ineligible":
                self._write_eligibility(idx, "ineligible")
            return f"closed row {idx} as {status} (no score; approval unchanged)"
        return f"no row found for {job_id}"

    def record_ai_match(self, job: JobCandidate, status: str) -> str:
        """Save first AI assessment, without overwriting any human decision.

        Already-discovered jobs move Found -> Review/Skip only when not previously
        assessed and not approved. A manually edited or previously scored row
        is protected. Uses RAW values, not a formula-parsing input mode.

        This is single-writer optimistic logic. Do not run two schedulers at once.
        """
        if status not in ASSESSED_STATUSES:
            raise ValueError("AI match status must be one of: "
                             + ", ".join(ASSESSED_STATUSES))
        self.validate_schema()
        rows = self._get(f"{a1_quote(self.vacancies_tab)}!A2:T1000")
        for idx, row in enumerate(rows, start=2):
            if not row or str(row[0]).strip() != job.job_id:
                continue
            row = list(row) + [""] * max(0, len(EXTENDED_HEADERS) - len(row))
            if row[12] != "Found" or row[13] != "Not reviewed" or row[8] not in ("", None):
                return f"protected existing row {idx} (human status or score preserved)"
            self._values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{a1_quote(self.vacancies_tab)}!I{idx}:M{idx}",
                valueInputOption="RAW",
                body={"values": [[job.match_score, job.match_explanation,
                                   job.critical_gaps, job.language, status]]},
            ).execute()
            self._write_eligibility(idx, job.eligibility)
            return f"assessed existing row {idx} (Found -> {status}; approval unchanged)"
        return self.upsert_candidate(job, status)
