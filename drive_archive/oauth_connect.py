#!/usr/bin/env python3
"""Create an isolated, least-privilege Google Drive OAuth token."""

import hashlib
import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "secrets" / "google"
CREDENTIALS = SECRETS / "credentials.json"
TOKEN = SECRETS / "drive-token.json"
METADATA = ROOT / "drive_archive" / "runtime" / "token-metadata.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def atomic_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def connect() -> dict:
    if not CREDENTIALS.exists():
        raise FileNotFoundError(f"OAuth credentials missing: {CREDENTIALS}")

    credentials = None
    if TOKEN.exists():
        token_payload = json.loads(TOKEN.read_text())
        if set(SCOPES).issubset(set(token_payload.get("scopes", []))):
            credentials = Credentials.from_authorized_user_file(TOKEN, SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            credentials = flow.run_local_server(
                host="127.0.0.1", port=0, open_browser=True,
                authorization_prompt_message="브라우저에서 PropertyAI Local의 Google Drive 접근을 승인하세요: {url}",
                success_message="Google Drive 연결이 완료되었습니다. 이 창을 닫아도 됩니다.",
            )
        atomic_private_json(TOKEN, json.loads(credentials.to_json()))

    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    about = service.about().get(fields="user(permissionId),storageQuota").execute()
    permission_id = about.get("user", {}).get("permissionId", "")
    quota = about.get("storageQuota", {})
    metadata = {
        "schema_version": 1,
        "scope": SCOPES,
        "account_ref_hash": hashlib.sha256(permission_id.encode()).hexdigest() if permission_id else None,
        "refresh_token_present": bool(credentials.refresh_token),
        "token_file_mode": oct(TOKEN.stat().st_mode & 0o777),
        "storage_limit_present": bool(quota.get("limit")),
        "identity_stored": False,
    }
    atomic_private_json(METADATA, metadata)
    return metadata


if __name__ == "__main__":
    print(json.dumps(connect(), ensure_ascii=False, indent=2))
