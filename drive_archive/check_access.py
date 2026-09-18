#!/usr/bin/env python3
"""Verify access to the canonical cleaning archive without modifying Drive."""

import json
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


ROOT = Path(__file__).resolve().parents[1]
TOKEN = ROOT / "secrets" / "google" / "drive-token.json"
CONFIG = Path(__file__).with_name("config.json")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> int:
    config = json.loads(CONFIG.read_text())
    credentials = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    try:
        folder = service.files().get(
            fileId=config["folder_id"],
            fields="id,name,mimeType,trashed,capabilities(canAddChildren)",
            supportsAllDrives=True,
        ).execute()
    except HttpError as error:
        status = getattr(error.resp, "status", None)
        print(json.dumps({
            "ok": False,
            "status": status,
            "resource_code": config["resource_code"],
            "reason": "drive.file 권한에서 기존 폴더가 아직 앱에 공개되지 않았습니다.",
        }, ensure_ascii=False, indent=2))
        return 2

    result = {
        "ok": folder.get("mimeType") == "application/vnd.google-apps.folder" and not folder.get("trashed"),
        "resource_code": config["resource_code"],
        "folder_id_matches": folder.get("id") == config["folder_id"],
        "folder_name": folder.get("name"),
        "can_add_children": folder.get("capabilities", {}).get("canAddChildren", False),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] and result["can_add_children"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
