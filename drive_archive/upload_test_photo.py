#!/usr/bin/env python3
"""Upload one known TEST photo and verify its remote integrity."""

import argparse
import hashlib
import json
import mimetypes
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


ROOT = Path(__file__).resolve().parents[1]
TOKEN = ROOT / "secrets" / "google" / "drive-token.json"
STATE = ROOT / "drive_archive" / "runtime" / "folder-state.json"
DEFAULT_PHOTO = ROOT / "telegram_approval" / "runtime" / "issue-uploads" / "VEhuJeS4LbA" / "01_AQADORFrGwp9eVd-.jpg"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def digest(path: Path, algorithm: str) -> str:
    checksum = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--photo", type=Path, default=DEFAULT_PHOTO)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    photo = args.photo.resolve()
    state = json.loads(STATE.read_text())
    plan = {
        "execute": args.execute,
        "source_exists": photo.exists(),
        "source_name": photo.name,
        "remote_name": "2026-08-02_JJ_house_현장_문제_TEST.jpg",
        "parent_verified": bool(state.get("moved_to_expected_parent")),
        "local_size": photo.stat().st_size if photo.exists() else None,
        "local_sha256": digest(photo, "sha256") if photo.exists() else None,
    }
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not photo.exists() or not state.get("moved_to_expected_parent"):
        raise RuntimeError("Source photo or verified managed folder is missing")

    credentials = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    mime_type = mimetypes.guess_type(photo.name)[0] or "application/octet-stream"
    remote = service.files().create(
        body={
            "name": plan["remote_name"],
            "parents": [state["managed_folder_id"]],
            "appProperties": {
                "propertyai_source": "telegram_issue_test",
                "propertyai_session": "VEhuJeS4LbA",
                "propertyai_sha256": plan["local_sha256"],
            },
        },
        media_body=MediaFileUpload(str(photo), mimetype=mime_type, resumable=True),
        fields="id,name,parents,size,md5Checksum,webViewLink,createdTime",
    ).execute()
    verified = (
        state["managed_folder_id"] in remote.get("parents", [])
        and int(remote.get("size", -1)) == photo.stat().st_size
        and remote.get("md5Checksum") == digest(photo, "md5")
    )
    result = {
        **plan,
        "drive_file_id": remote.get("id"),
        "drive_url": remote.get("webViewLink"),
        "remote_size": int(remote.get("size", -1)),
        "remote_md5_matches": remote.get("md5Checksum") == digest(photo, "md5"),
        "remote_parent_matches": state["managed_folder_id"] in remote.get("parents", []),
        "verified": verified,
        "local_cleanup_allowed": False,
        "cleanup_reason": "Notion 기록과 24시간 격리가 아직 필요합니다.",
    }
    output = ROOT / "drive_archive" / "runtime" / "last-upload.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if verified else 4


if __name__ == "__main__":
    raise SystemExit(main())
