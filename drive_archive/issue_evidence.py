#!/usr/bin/env python3
"""Idempotent Google Drive archive for Cleaning issue evidence."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


ROOT = Path(__file__).resolve().parents[1]
TOKEN = ROOT / "secrets" / "google" / "drive-token.json"
FOLDER_STATE = ROOT / "drive_archive" / "runtime" / "folder-state.json"
QUARANTINE = ROOT / "drive_archive" / "runtime" / "quarantine"
ISSUE_DIR = ROOT / "telegram_approval" / "runtime" / "issue-uploads"
COMPLETION_DIR = ROOT / "telegram_approval" / "runtime" / "completion-uploads"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"


def _drive_service():
    credentials = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _digest(path: Path, algorithm: str) -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r]+", "_", str(value or "")).strip(" ._")
    return cleaned[:80] or "미지정"


def _q(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _get(service, file_id: str) -> dict:
    return service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,parents,trashed,size,md5Checksum,webViewLink,appProperties",
        supportsAllDrives=True,
    ).execute()


def _valid_folder(item: dict, parent_id: str) -> bool:
    return (
        item.get("mimeType") == FOLDER_MIME
        and not item.get("trashed")
        and parent_id in item.get("parents", [])
    )


def _find_or_create_folder(service, *, parent_id: str, name: str, properties: dict) -> tuple[dict, bool]:
    conditions = [f"'{_q(parent_id)}' in parents", "trashed = false"]
    for key, value in properties.items():
        conditions.append(f"appProperties has {{ key='{_q(key)}' and value='{_q(value)}' }}")
    matches = service.files().list(
        q=" and ".join(conditions),
        spaces="drive",
        pageSize=10,
        fields="files(id,name,mimeType,parents,trashed,webViewLink,appProperties)",
    ).execute().get("files", [])
    if len(matches) > 1:
        raise RuntimeError(f"Multiple Drive folders matched role {properties.get('propertyai_role')}")
    if matches:
        if not _valid_folder(matches[0], parent_id):
            raise RuntimeError("Existing Drive folder failed parent/type verification")
        return matches[0], False
    created = service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id], "appProperties": properties},
        fields="id,name,mimeType,parents,trashed,webViewLink,appProperties",
    ).execute()
    if not _valid_folder(created, parent_id):
        raise RuntimeError("Created Drive folder failed parent/type verification")
    return created, True


def _verified_remote(service, item: dict, photo: dict, parent_id: str) -> dict:
    remote = _get(service, item["id"])
    local_path = Path(photo["path"])
    valid = (
        not remote.get("trashed")
        and parent_id in remote.get("parents", [])
        and int(remote.get("size", -1)) == local_path.stat().st_size
        and remote.get("md5Checksum") == _digest(local_path, "md5")
        and photo["sha256"] == _digest(local_path, "sha256")
    )
    if not valid:
        raise RuntimeError("Drive photo readback did not match local evidence")
    return {
        "sha256": photo["sha256"],
        "drive_file_id": remote["id"],
        "drive_url": remote.get("webViewLink"),
        "filename": remote["name"],
        "size": int(remote["size"]),
        "verified": True,
    }


def _save_session_state(session: dict) -> None:
    if session.get("evidence_type") == "completion":
        from telegram_approval.cleaning_completion_evidence import save_session
    else:
        from telegram_approval.cleaning_issue import save_session
    save_session(session)


def _archive_photos(session: dict, *, cleaning_date: str | None, service,
                    folder_name: str, folder_role: str, file_role: str,
                    filename_label: str) -> dict:

    if session.get("test_mode"):
        raise ValueError("TEST sessions must not write to Google Drive")
    state = json.loads(FOLDER_STATE.read_text())
    if not state.get("moved_to_expected_parent"):
        raise RuntimeError("Managed Drive folder is not verified under the Notion registry folder")
    service = service or _drive_service()
    root_id = state["managed_folder_id"]
    nickname = _safe_name(session.get("property_nickname"))
    date_value = _safe_name(cleaning_date or "날짜미정")
    page_id = session["cleaning_page_id"]

    cleaning_folder, cleaning_created = _find_or_create_folder(
        service,
        parent_id=root_id,
        name=f"{date_value}_{nickname}_퇴실청소",
        properties={
            "propertyai_role": "cleaning_evidence",
            "propertyai_cleaning_page_id": page_id,
        },
    )
    issue_folder, issue_created = _find_or_create_folder(
        service,
        parent_id=cleaning_folder["id"],
        name=folder_name,
        properties={
            "propertyai_role": folder_role,
            "propertyai_cleaning_page_id": page_id,
        },
    )
    evidence = session.setdefault("drive_evidence", {})
    evidence.update({
        "cleaning_folder_id": cleaning_folder["id"],
        "cleaning_folder_url": cleaning_folder.get("webViewLink"),
        "evidence_folder_id": issue_folder["id"],
        "evidence_folder_url": issue_folder.get("webViewLink"),
        "folder_readback_verified": True,
    })
    uploads = evidence.setdefault("uploads", [])
    by_hash = {item["sha256"]: item for item in uploads if item.get("verified")}
    _save_session_state(session)

    for index, photo in enumerate(session.get("photos", []), start=1):
        if photo["sha256"] in by_hash:
            verified = _verified_remote(service, by_hash[photo["sha256"]], photo, issue_folder["id"])
        else:
            query = (
                f"'{_q(issue_folder['id'])}' in parents and trashed = false and "
                f"appProperties has {{ key='propertyai_sha256' and value='{_q(photo['sha256'])}' }}"
            )
            matches = service.files().list(
                q=query, spaces="drive", pageSize=10,
                fields="files(id,name,mimeType,parents,trashed,size,md5Checksum,webViewLink,appProperties)",
            ).execute().get("files", [])
            if len(matches) > 1:
                raise RuntimeError("Multiple Drive files matched one photo checksum")
            if matches:
                verified = _verified_remote(service, matches[0], photo, issue_folder["id"])
            else:
                path = Path(photo["path"])
                suffix = path.suffix.lower() or ".jpg"
                remote_name = f"{date_value}_{nickname}_{filename_label}_{index:02d}_{photo['sha256'][:8]}{suffix}"
                mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                created = service.files().create(
                    body={
                        "name": remote_name,
                        "parents": [issue_folder["id"]],
                        "appProperties": {
                            "propertyai_role": file_role,
                            "propertyai_cleaning_page_id": page_id,
                            "propertyai_session_id": session["session_id"],
                            "propertyai_sha256": photo["sha256"],
                        },
                    },
                    media_body=MediaFileUpload(str(path), mimetype=mime_type, resumable=True),
                    fields="id,name,parents,size,md5Checksum,webViewLink",
                ).execute()
                verified = _verified_remote(service, created, photo, issue_folder["id"])
            uploads.append(verified)
            by_hash[photo["sha256"]] = verified
            _save_session_state(session)

    evidence["verified"] = len(uploads) == len(session.get("photos", [])) and all(
        item.get("verified") for item in uploads
    )
    evidence["folder_creates"] = int(cleaning_created) + int(issue_created)
    evidence["verified_at"] = datetime.now(timezone.utc).isoformat()
    _save_session_state(session)
    if not evidence["verified"]:
        raise RuntimeError("Not every issue photo was verified in Drive")
    return evidence


def archive_issue_photos(session: dict, *, cleaning_date: str | None = None, service=None) -> dict:
    """Archive every issue photo, save progress after each write, and return verified links."""
    service = service or _drive_service()
    evidence = _archive_photos(
        session, cleaning_date=cleaning_date, service=service,
        folder_name="02_문제·하자", folder_role="issue_photos",
        file_role="issue_photo", filename_label="현장_문제",
    )
    evidence["issue_folder_id"] = evidence["evidence_folder_id"]
    evidence["issue_folder_url"] = evidence["evidence_folder_url"]
    _save_session_state(session)
    return evidence


def archive_completion_photos(session: dict, *, cleaning_date: str | None = None, service=None) -> dict:
    """Archive verified completion photos below the Cleaning evidence folder."""
    service = service or _drive_service()
    evidence = _archive_photos(
        session, cleaning_date=cleaning_date, service=service,
        folder_name="01_청소완료", folder_role="completion_photos",
        file_role="completion_photo", filename_label="청소_완료",
    )
    evidence["completion_folder_id"] = evidence["evidence_folder_id"]
    evidence["completion_folder_url"] = evidence["evidence_folder_url"]
    _save_session_state(session)
    return evidence


def quarantine_session_photos(session: dict, *, now: datetime | None = None) -> dict:
    """Move verified local originals into a 24-hour local quarantine."""
    now = now or datetime.now(timezone.utc)
    evidence = session.get("drive_evidence", {})
    if not evidence.get("verified") or not evidence.get("notion_recorded"):
        return {"moved": 0, "reason": "drive_or_notion_not_verified"}
    target_dir = QUARANTINE / session["session_id"]
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target_dir, 0o700)
    moved = 0
    cleanup_after = now + timedelta(hours=24)
    for photo in session.get("photos", []):
        source = Path(photo["path"])
        if not source.exists() or QUARANTINE in source.parents:
            continue
        target = target_dir / source.name
        shutil.move(str(source), str(target))
        os.chmod(target, 0o600)
        photo["path"] = str(target)
        photo["quarantined_at"] = now.isoformat()
        photo["cleanup_after"] = cleanup_after.isoformat()
        moved += 1
    return {"moved": moved, "cleanup_after": cleanup_after.isoformat()}


def cleanup_expired_quarantine(*, now: datetime | None = None) -> dict:
    """Delete only expired, verified quarantine files and retain session audit metadata."""
    now = now or datetime.now(timezone.utc)
    result = {"deleted": 0, "sessions_updated": 0, "errors": 0}
    session_paths = list(ISSUE_DIR.glob("*/session.json")) + list(COMPLETION_DIR.glob("*/session.json"))
    for session_path in session_paths:
        try:
            session = json.loads(session_path.read_text())
            evidence = session.get("drive_evidence", {})
            if not evidence.get("verified") or not evidence.get("notion_recorded"):
                continue
            changed = False
            for photo in session.get("photos", []):
                cleanup_after = photo.get("cleanup_after")
                if not cleanup_after or now < datetime.fromisoformat(cleanup_after):
                    continue
                path = Path(photo["path"])
                if path.exists() and QUARANTINE in path.parents:
                    path.unlink()
                    result["deleted"] += 1
                photo["local_deleted_at"] = now.isoformat()
                changed = True
            if changed:
                _save_session_state(session)
                result["sessions_updated"] += 1
        except Exception:
            result["errors"] += 1
    return result
