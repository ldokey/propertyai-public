#!/usr/bin/env python3
"""Create the app-owned upload folder used with the drive.file scope."""

import json
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parents[1]
TOKEN = ROOT / "secrets" / "google" / "drive-token.json"
STATE = ROOT / "drive_archive" / "runtime" / "folder-state.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"


def atomic_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> None:
    if STATE.exists():
        print(STATE.read_text(), end="")
        return

    credentials = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    folder = service.files().create(
        body={
            "name": "00_PropertyAI_자동업로드",
            "mimeType": FOLDER_MIME,
            "appProperties": {"propertyai_role": "cleaning_photo_archive"},
        },
        fields="id,name,webViewLink,parents",
    ).execute()
    state = {
        "schema_version": 1,
        "managed_folder_id": folder["id"],
        "managed_folder_name": folder["name"],
        "web_view_link": folder.get("webViewLink"),
        "expected_parent_resource": "DRIVE.OPS.CLEANING",
        "expected_parent_id": "1UHPwJWLMAK5wg3eriHUVoizpXYlU7wKR",
        "moved_to_expected_parent": False,
    }
    atomic_private_json(STATE, state)
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
