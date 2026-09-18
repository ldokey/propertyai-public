import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from telegram_approval import bot_service, cleaning_issue


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class CleaningIssueTests(unittest.TestCase):
    def test_issue_session_captures_photo_and_submits_without_test_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator_path = root / "operator.json"
            token_path = root / "token"
            secret_path = root / "secret"
            request_dir = root / "requests"
            issue_dir = root / "issues"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 1, "telegram_chat_id": 2}))
            token_path.write_text("token")
            secret_path.write_text("secret")
            parent = {
                "action_id": "parent", "action_type": "CLEANING_START",
                "cleaning_page_id": "cleaning", "property_nickname": "JJ",
                "candidate_user_id": 1, "candidate_chat_id": 2,
                "candidate_party_page_id": "party", "test_mode": True,
            }
            sent = []

            def fake_api(_token, method, **values):
                sent.append((method, values))
                if method == "getFile":
                    return {"file_path": "photos/test.jpg"}
                return {"message_id": len(sent)}

            with patch.object(cleaning_issue, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_issue, "ISSUE_DIR", issue_dir), \
                 patch.object(cleaning_issue, "TOKEN_PATH", token_path), \
                 patch.object(cleaning_issue, "api", side_effect=fake_api), \
                 patch.object(cleaning_issue, "secret", return_value=b"secret"), \
                 patch.object(cleaning_issue.urllib.request, "urlopen", return_value=Response(b"jpeg-data")), \
                 patch.object(bot_service, "OPERATOR_PATH", operator_path), \
                 patch.object(bot_service, "REQUEST_DIR", request_dir), \
                 patch.object(bot_service, "ACTION_SECRET_PATH", secret_path), \
                 patch.object(bot_service, "api", side_effect=fake_api):
                opened = cleaning_issue.start_issue_session(parent)
                result = cleaning_issue.capture_issue_message({"message": {
                    "message_id": 10,
                    "photo": [{"file_id": "file", "file_unique_id": "unique", "file_size": 9}],
                    "from": {"id": 1}, "chat": {"id": 2, "type": "private"},
                }}, "token")
                self.assertEqual(result, "issue_photo_saved")
                record = json.loads((request_dir / f"{opened['action_id']}.json").read_text())
                callback_data = f"a:{opened['action_id']}:approve:" + __import__("hmac").new(
                    b"secret", f"{opened['action_id']}:approve".encode(), __import__("hashlib").sha256
                ).hexdigest()[:16]
                outcome = bot_service.callback({"callback_query": {
                    "id": "cb", "data": callback_data, "from": {"id": 1},
                    "message": {"chat": {"id": 2}},
                }}, "token")
                saved = cleaning_issue.load_session(record["issue_session_id"])
            self.assertEqual(outcome, "approved")
            self.assertEqual(saved["status"], "SUBMITTED_TEST")
            self.assertEqual(len(saved["photos"]), 1)
            self.assertEqual(saved["photos"][0]["sha256"], __import__("hashlib").sha256(b"jpeg-data").hexdigest())
            action = json.loads((request_dir / f"{opened['action_id']}.json").read_text())
            self.assertEqual(action["external_writes_executed"], 0)


if __name__ == "__main__":
    unittest.main()
