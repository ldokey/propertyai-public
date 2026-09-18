import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_approval import bot_service, cleaning_operations


class CleaningOperationsTests(unittest.TestCase):
    def test_test_day_confirm_chains_arrival_and_start_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator_path = root / "operator.json"
            token_path = root / "token"
            action_secret_path = root / "action-secret"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            token_path.write_text("token")
            action_secret_path.write_text("secret")
            sent = []

            def fake_api(_token, method, **values):
                sent.append((method, values))
                return {"message_id": len(sent)}

            candidate = {
                "telegram_user_id": 101,
                "telegram_chat_id": 202,
                "party_page_id": "party",
                "label": "운영자 겸 청소담당자(TEST)",
            }
            patches = (
                patch.object(cleaning_operations, "TOKEN_PATH", token_path),
                patch.object(cleaning_operations, "REQUEST_DIR", request_dir),
                patch.object(cleaning_operations, "api", side_effect=fake_api),
                patch.object(cleaning_operations, "secret", return_value=b"secret"),
                patch.object(bot_service, "OPERATOR_PATH", operator_path),
                patch.object(bot_service, "REQUEST_DIR", request_dir),
                patch.object(bot_service, "ACTION_SECRET_PATH", action_secret_path),
                patch.object(bot_service, "api", side_effect=fake_api),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                day = cleaning_operations.send_operation_stage(
                    stage="DAY_CONFIRM",
                    cleaning_page_id="test-cleaning",
                    property_nickname="JJ House",
                    address="서울특별시 마포구 테스트로 1",
                    start_at="2026-09-06 11:00",
                    end_at="2026-09-06 15:00",
                    candidate=candidate,
                    test_mode=True,
                )
                day_callback = json.loads(sent[0][1]["reply_markup"])["inline_keyboard"][0][0]["callback_data"]
                self.assertEqual(self._click(day_callback), "approved")
                day_record = json.loads((request_dir / f"{day['action_id']}.json").read_text())
                arrival_id = day_record["next_action"]["action_id"]
                arrival_message = next(
                    values for method, values in sent
                    if method == "sendMessage" and arrival_id in values.get("reply_markup", "")
                )
                arrival_callback = json.loads(arrival_message["reply_markup"])["inline_keyboard"][0][0]["callback_data"]
                self.assertEqual(self._click(arrival_callback), "approved")
                arrival_record = json.loads((request_dir / f"{arrival_id}.json").read_text())
                start_id = arrival_record["next_action"]["action_id"]
                start_message = next(
                    values for method, values in sent
                    if method == "sendMessage" and start_id in values.get("reply_markup", "")
                )
                start_callback = json.loads(start_message["reply_markup"])["inline_keyboard"][0][0]["callback_data"]
                self.assertEqual(self._click(start_callback), "approved")

            records = [json.loads(path.read_text()) for path in request_dir.glob("*.json")]
            self.assertEqual({record["action_type"] for record in records}, {
                "CLEANING_DAY_CONFIRM", "CLEANING_ARRIVAL", "CLEANING_START"
            })
            self.assertTrue(all(record["external_writes_executed"] == 0 for record in records))
            self.assertEqual(json.loads((request_dir / f"{start_id}.json").read_text())["status"], "APPROVED_TEST")

    def test_arrival_message_includes_door_code_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_path = root / "token"
            request_dir = root / "requests"
            request_dir.mkdir()
            token_path.write_text("token")
            sent = []

            def fake_api(_token, method, **values):
                sent.append(values)
                return {"message_id": 1}

            with patch.object(cleaning_operations, "TOKEN_PATH", token_path), \
                 patch.object(cleaning_operations, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_operations, "api", side_effect=fake_api), \
                 patch.object(cleaning_operations, "secret", return_value=b"secret"):
                cleaning_operations.send_operation_stage(
                    stage="ARRIVAL", cleaning_page_id="cleaning", property_nickname="JJ",
                    address="주소", start_at="11:00", end_at="15:00",
                    candidate={"telegram_user_id": 1, "telegram_chat_id": 1},
                    test_mode=True, door_code=" 04-19 ",
                )
            self.assertIn("도어락 번호:  04-19 ", sent[0]["text"])

    @staticmethod
    def _click(callback_data):
        return bot_service.callback({"callback_query": {
            "id": "callback",
            "data": callback_data,
            "from": {"id": 101},
            "message": {"chat": {"id": 202}},
        }}, "token")


if __name__ == "__main__":
    unittest.main()
