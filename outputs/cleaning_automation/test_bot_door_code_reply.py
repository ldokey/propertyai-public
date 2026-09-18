import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "telegram_approval"))
import bot_service  # noqa: E402


class BotDoorCodeReplyTests(unittest.TestCase):
    def test_authorized_reply_is_preserved_and_delivered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operator_path = root / "operator.json"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 10, "telegram_chat_id": 20}))
            request_path = request_dir / "req.json"
            request_path.write_text(json.dumps({
                "prompt_message_id": 30,
                "status": "PENDING_OPERATOR_INPUT",
                "smart_doorlock": False,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "base_cleaner_message": "청소 안내",
                "delivery_chat_id": 20,
                "test_mode": True,
            }))
            update = {"message": {
                "message_id": 40,
                "text": " 0123 ",
                "reply_to_message": {"message_id": 30},
                "from": {"id": 10},
                "chat": {"id": 20, "type": "private"},
            }}

            with patch.object(bot_service, "OPERATOR_PATH", operator_path), \
                 patch.object(bot_service, "DOOR_CODE_REQUEST_DIR", request_dir), \
                 patch.object(bot_service, "api", return_value={"message_id": 50}) as mocked_api:
                self.assertEqual(bot_service.door_code_reply(update, "token"), "door_code_delivered")

            self.assertEqual(mocked_api.call_args.kwargs["text"], "청소 안내\n도어락 번호:  0123 ")
            saved = json.loads(request_path.read_text())
            self.assertEqual(saved["airbnb_phone_last4"], " 0123 ")
            self.assertTrue(saved["input_preserved_exactly"])
            self.assertEqual(saved["status"], "DELIVERED_TEST")

    def test_plain_text_is_accepted_when_only_one_request_is_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operator_path = root / "operator.json"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 10, "telegram_chat_id": 20}))
            request_path = request_dir / "req.json"
            request_path.write_text(json.dumps({
                "prompt_message_id": 30,
                "status": "PENDING_OPERATOR_INPUT",
                "smart_doorlock": False,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "base_cleaner_message": "청소 안내",
                "delivery_chat_id": 20,
                "test_mode": True,
            }))
            update = {"message": {
                "message_id": 41,
                "text": "0123",
                "from": {"id": 10},
                "chat": {"id": 20, "type": "private"},
            }}

            with patch.object(bot_service, "OPERATOR_PATH", operator_path), \
                 patch.object(bot_service, "DOOR_CODE_REQUEST_DIR", request_dir), \
                 patch.object(bot_service, "api", return_value={"message_id": 51}):
                self.assertEqual(bot_service.door_code_reply(update, "token"), "door_code_delivered")

            saved = json.loads(request_path.read_text())
            self.assertEqual(saved["airbnb_phone_last4"], "0123")
            self.assertEqual(saved["status"], "DELIVERED_TEST")

    def test_smart_lock_captures_and_includes_exact_code_in_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operator_path = root / "operator.json"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 10, "telegram_chat_id": 20}))
            request_path = request_dir / "req.json"
            request_path.write_text(json.dumps({
                "prompt_message_id": 30,
                "status": "PENDING_OPERATOR_INPUT",
                "smart_doorlock": True,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "base_cleaner_message": "JJ 청소 안내",
                "delivery_chat_id": 20,
                "test_mode": True,
            }))
            update = {"message": {
                "message_id": 42,
                "text": "0123",
                "from": {"id": 10},
                "chat": {"id": 20, "type": "private"},
            }}

            with patch.object(bot_service, "OPERATOR_PATH", operator_path), \
                 patch.object(bot_service, "DOOR_CODE_REQUEST_DIR", request_dir), \
                 patch.object(bot_service, "api", return_value={"message_id": 52}) as mocked_api:
                self.assertEqual(bot_service.door_code_reply(update, "token"), "door_code_delivered")

            self.assertEqual(mocked_api.call_args.kwargs["text"], "JJ 청소 안내\n도어락 번호: 0123")
            saved = json.loads(request_path.read_text())
            self.assertEqual(saved["airbnb_phone_last4"], "0123")
            self.assertEqual(saved["notion_write_status"], "SKIPPED_TEST")


if __name__ == "__main__":
    unittest.main()
