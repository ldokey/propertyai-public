import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gmail_ingest import lifecycle_actions
from telegram_approval import bot_service, cleaning_assignment


class CleaningAssignmentExpiryTests(unittest.TestCase):
    def test_expired_proposal_routes_once_to_next_candidate(self):
        now = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path = root / "operator.json"
            operator_path.write_text(json.dumps({
                "telegram_user_id": 1,
                "telegram_chat_id": 1,
            }))
            record = {
                "action_id": "old-action",
                "action_type": "CLEANING_ASSIGNMENT",
                "cleaning_page_id": "cleaning-page",
                "property_nickname": "JJ",
                "start_at": "2026-09-06 11:00",
                "candidate_chat_id": 10,
                "remaining_candidates": [{"telegram_user_id": 20, "telegram_chat_id": 20}],
                "status": "PENDING",
                "consumed": False,
                "expires_at": (now - timedelta(seconds=1)).isoformat(),
                "external_writes_on_reject": 1,
            }
            path = request_dir / "old-action.json"
            path.write_text(json.dumps(record))

            execution = {
                "cleaning_assignment": "NEXT_CANDIDATE_REQUIRED",
                "next_expected_last_edited_time": "2026-08-03T05:00:01.000Z",
            }
            with patch.object(bot_service, "REQUEST_DIR", request_dir), \
                 patch.object(bot_service, "OPERATOR_PATH", operator_path), \
                 patch.object(bot_service, "api", return_value={"message_id": 1}), \
                 patch.object(lifecycle_actions, "execute_lifecycle_action", return_value=execution) as execute, \
                 patch.object(cleaning_assignment, "send_next_assignment", return_value={"sent": True, "action_id": "next"}) as send_next:
                first = bot_service.expire_cleaning_assignments("token", now)
                second = bot_service.expire_cleaning_assignments("token", now + timedelta(minutes=1))

            saved = json.loads(path.read_text())
            self.assertEqual(first["expired"], 1)
            self.assertEqual(first["next_sent"], 1)
            self.assertEqual(second["expired"], 0)
            self.assertEqual(saved["status"], "EXPIRED_ROUTED")
            self.assertEqual(saved["assignment_response_reason"], "EXPIRED_24H")
            execute.assert_called_once()
            send_next.assert_called_once()


if __name__ == "__main__":
    unittest.main()
