"""Google Sheets API authentication: OAuth desktop or shared service account."""
from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _save_token(token_file: Path, credentials: Credentials) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    # Private credentials. Avoid exposing the file to other local users.
    tmp = token_file.with_name(token_file.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(credentials.to_json())
        os.replace(tmp, token_file)
        try:
            token_file.chmod(0o600)
        except OSError:
            pass  # Windows may not support Unix permission bits.
    finally:
        if tmp.exists():
            tmp.unlink()


def credentials_for(settings: Settings):
    if settings.auth_mode == "service_account":
        if not settings.service_account_file.is_file():
            raise FileNotFoundError(
                f"Missing {settings.service_account_file}. Create a service account and "
                "share the Sheet with its client_email as Editor."
            )
        return service_account.Credentials.from_service_account_file(
            str(settings.service_account_file), scopes=SCOPES
        )

    creds = None
    if settings.token_file.exists():
        creds = Credentials.from_authorized_user_file(str(settings.token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not settings.oauth_client_file.is_file():
            raise FileNotFoundError(
                f"Missing {settings.oauth_client_file}. Download OAuth Desktop app "
                "credentials from Google Cloud; see README."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(settings.oauth_client_file), SCOPES
        )
        creds = flow.run_local_server(port=0)
    _save_token(settings.token_file, creds)
    return creds


def make_sheets_service(settings: Settings):
    """Build a Google Sheets v4 client; does not run until called."""
    return build("sheets", "v4", credentials=credentials_for(settings), cache_discovery=False)
