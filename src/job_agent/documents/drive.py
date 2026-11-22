"""Optional Google Drive upload, so a spreadsheet cell can hold a real link.

Opt-in and deliberately inert by default, for two reasons:

1. **It needs a new OAuth scope.** `drive.file` is not in the Sheets-only
   scope the project uses, so enabling it invalidates `token.json` and
   requires an interactive re-authorisation. That is the user's decision, not
   something to trigger during a document run.
2. **It publishes personal documents.** A CV carries a name, an address and a
   phone number. Uploading one is an outward-facing action, so it happens only
   when asked for explicitly.

`drive.file` is the narrowest scope that works: it grants access only to files
this application itself creates, never to the rest of the user's Drive. Files
are created private, with no link sharing; nothing here changes permissions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger("job_agent.documents.drive")

# Narrowest scope that allows creating files. NOT in the default SCOPES.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"

MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".txt": "text/plain",
}


class DriveNotAuthorised(RuntimeError):
    """Raised when Drive is requested but the token lacks the scope."""


@dataclass(frozen=True)
class UploadedFile:
    name: str
    file_id: str
    url: str


def required_setup_steps() -> list[str]:
    """What the user must do before an upload can work."""
    return [
        "1. Enable the Google Drive API in the same Google Cloud project.",
        f"2. Add the scope {DRIVE_SCOPE} to SCOPES in src/job_agent/auth.py.",
        "3. Delete token.json and run 'uv run job-agent check' to re-authorise "
        "interactively. The existing Sheets access keeps working.",
        "4. Re-run with --drive. Files are created private; share them yourself.",
    ]


def upload_files(service: Any, paths: Sequence[Path | str], *,
                 folder_id: str = "") -> list[UploadedFile]:
    """Upload files with the Drive v3 API. Creates private files only.

    `service` is an authorised Drive client. Nothing here grants link sharing
    or changes a permission: a freshly created file is visible to its owner
    alone, and the user decides who else may see it.
    """
    from googleapiclient.http import MediaFileUpload

    uploaded: list[UploadedFile] = []
    for item in paths:
        path = Path(item)
        if not path.is_file():
            log.info("skipping %s: not a file", path)
            continue
        mime = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
        metadata: dict[str, Any] = {"name": path.name}
        if folder_id:
            metadata["parents"] = [folder_id]
        media = MediaFileUpload(str(path), mimetype=mime, resumable=False)
        created = service.files().create(
            body=metadata, media_body=media,
            fields="id,name,webViewLink").execute()
        uploaded.append(UploadedFile(
            name=str(created.get("name") or path.name),
            file_id=str(created.get("id") or ""),
            url=str(created.get("webViewLink") or "")))
    return uploaded


def make_drive_service(settings: Any) -> Any:
    """Build a Drive client, refusing clearly when the scope is absent."""
    from ..auth import SCOPES, credentials_for
    from googleapiclient.discovery import build

    if DRIVE_SCOPE not in SCOPES:
        raise DriveNotAuthorised(
            "Google Drive upload is not authorised for this project.\n"
            + "\n".join(required_setup_steps()))
    return build("drive", "v3", credentials=credentials_for(settings),
                 cache_discovery=False)
