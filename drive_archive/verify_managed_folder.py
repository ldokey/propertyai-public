#!/usr/bin/env python3
"""Verify that the app-owned folder was moved under the Notion registry folder."""

import json
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parents[1]
TOKEN = ROOT / "secrets" / "google" / "drive-token.json"
STATE = ROOT / "drive_archive" / "runtime" / "folder-state.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> int:
    state = json.loads(STATE.read_text())
    credentials = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    folder = service.files().get(
        fileId=state["managed_folder_id"],
        fields="id,name,parents,trashed,capabilities(canAddChildren)",
    ).execute()
    parents = folder.get("parents", [])
    ok = (
        state["expected_parent_id"] in parents
        and not folder.get("trashed")
        and folder.get("capabilities", {}).get("canAddChildren", False)
    )
    state["moved_to_expected_parent"] = ok
    state["observed_parent_ids"] = parents
    temporary = STATE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE)
    print(json.dumps({
        "ok": ok,
        "folder_name": folder.get("name"),
        "parent_matches_notion_registry": state["expected_parent_id"] in parents,
        "can_add_children": folder.get("capabilities", {}).get("canAddChildren", False),
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
