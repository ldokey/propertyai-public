import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from drive_archive import issue_evidence
from telegram_approval import cleaning_completion_evidence, cleaning_issue


class Result:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeFiles:
    def __init__(self, photo_size, photo_md5):
        self.photo_size = photo_size
        self.photo_md5 = photo_md5
        self.list_calls = 0
        self.create_calls = 0
        self.items = {}

    def list(self, **_kwargs):
        self.list_calls += 1
        return Result({"files": []})

    def create(self, body, media_body=None, fields=None):
        self.create_calls += 1
        parent = body.get("parents", [None])[0]
        if body.get("mimeType") == issue_evidence.FOLDER_MIME:
            item_id = "cleaning-folder" if self.create_calls == 1 else "issue-folder"
            item = {
                "id": item_id, "name": body["name"], "mimeType": issue_evidence.FOLDER_MIME,
                "parents": [parent], "trashed": False,
                "webViewLink": f"https://drive.google.com/drive/folders/{item_id}",
                "appProperties": body["appProperties"],
            }
        else:
            item = {
                "id": "photo-file", "name": body["name"], "mimeType": "image/jpeg",
                "parents": [parent], "trashed": False, "size": str(self.photo_size),
                "md5Checksum": self.photo_md5,
                "webViewLink": "https://drive.google.com/file/d/photo-file/view",
                "appProperties": body["appProperties"],
            }
        self.items[item["id"]] = item
        return Result(item)

    def get(self, fileId, **_kwargs):
        return Result(self.items[fileId])


class FakeService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


class DriveIssueEvidenceTests(unittest.TestCase):
    def test_archive_creates_verified_cleaning_and_issue_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo_path = root / "photo.jpg"
            photo_path.write_bytes(b"jpeg-evidence")
            state_path = root / "folder-state.json"
            state_path.write_text(json.dumps({
                "managed_folder_id": "managed-root",
                "moved_to_expected_parent": True,
            }))
            session = {
                "session_id": "session", "cleaning_page_id": "cleaning-page",
                "property_nickname": "JJ", "test_mode": False,
                "photos": [{
                    "path": str(photo_path), "filename": "photo.jpg",
                    "size": photo_path.stat().st_size,
                    "sha256": hashlib.sha256(photo_path.read_bytes()).hexdigest(),
                }],
            }
            files = FakeFiles(photo_path.stat().st_size, hashlib.md5(photo_path.read_bytes()).hexdigest())
            service = FakeService(files)
            with patch.object(issue_evidence, "FOLDER_STATE", state_path), \
                 patch.object(cleaning_issue, "ISSUE_DIR", root / "sessions"):
                result = issue_evidence.archive_issue_photos(
                    session, cleaning_date="2026-09-06", service=service,
                )
            self.assertTrue(result["verified"])
            self.assertEqual(result["cleaning_folder_id"], "cleaning-folder")
            self.assertEqual(result["issue_folder_id"], "issue-folder")
            self.assertEqual(result["uploads"][0]["drive_file_id"], "photo-file")
            self.assertEqual(files.create_calls, 3)

    def test_completion_archive_uses_completion_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo_path = root / "photo.jpg"
            photo_path.write_bytes(b"completion-evidence")
            state_path = root / "folder-state.json"
            state_path.write_text(json.dumps({
                "managed_folder_id": "managed-root", "moved_to_expected_parent": True,
            }))
            session = {
                "session_id": "completion-session", "cleaning_page_id": "cleaning-page",
                "property_nickname": "JJ", "test_mode": False, "evidence_type": "completion",
                "photos": [{
                    "path": str(photo_path), "filename": "photo.jpg", "size": photo_path.stat().st_size,
                    "sha256": hashlib.sha256(photo_path.read_bytes()).hexdigest(),
                }],
            }
            files = FakeFiles(photo_path.stat().st_size, hashlib.md5(photo_path.read_bytes()).hexdigest())
            service = FakeService(files)
            with patch.object(issue_evidence, "FOLDER_STATE", state_path), \
                 patch.object(cleaning_completion_evidence, "COMPLETION_DIR", root / "sessions"):
                result = issue_evidence.archive_completion_photos(
                    session, cleaning_date="2026-09-06", service=service,
                )
            self.assertTrue(result["verified"])
            self.assertEqual(result["completion_folder_id"], "issue-folder")
            self.assertEqual(files.items["issue-folder"]["name"], "01_청소완료")

    def test_test_session_is_never_archived(self):
        with self.assertRaisesRegex(ValueError, "TEST"):
            issue_evidence.archive_issue_photos({"test_mode": True}, service=object())

    def test_verified_photo_quarantines_then_deletes_after_24_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue_root = root / "issues"
            quarantine = root / "quarantine"
            source_dir = issue_root / "session"
            source_dir.mkdir(parents=True)
            source = source_dir / "photo.jpg"
            source.write_bytes(b"photo")
            session = {
                "session_id": "session", "status": "SUBMITTED",
                "drive_evidence": {"verified": True, "notion_recorded": True},
                "photos": [{"path": str(source)}],
            }
            now = datetime(2026, 8, 2, tzinfo=timezone.utc)
            with patch.object(issue_evidence, "QUARANTINE", quarantine), \
                 patch.object(issue_evidence, "ISSUE_DIR", issue_root), \
                 patch.object(cleaning_issue, "ISSUE_DIR", issue_root):
                moved = issue_evidence.quarantine_session_photos(session, now=now)
                cleaning_issue.save_session(session)
                self.assertEqual(moved["moved"], 1)
                self.assertTrue(Path(session["photos"][0]["path"]).exists())
                early = issue_evidence.cleanup_expired_quarantine(now=now + timedelta(hours=23))
                self.assertEqual(early["deleted"], 0)
                expired = issue_evidence.cleanup_expired_quarantine(now=now + timedelta(hours=25))
            self.assertEqual(expired["deleted"], 1)
            self.assertFalse(Path(session["photos"][0]["path"]).exists())


if __name__ == "__main__":
    unittest.main()
