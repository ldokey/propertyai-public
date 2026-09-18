import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from telegram_approval import bot_service, cleaning_completion_evidence, cleaning_issue


class NegativeButtonPathTests(unittest.TestCase):
    def test_every_negative_button_stops_or_routes_without_external_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requests = root / "requests"
            requests.mkdir()
            operator = root / "operator.json"
            action_secret = root / "action-secret"
            issue_dir = root / "issue-uploads"
            completion_dir = root / "completion-uploads"
            operator.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            action_secret.write_text("secret")
            sent = []

            def fake_api(_token, method, **values):
                sent.append((method, values))
                return {"message_id": len(sent)}

            cases = {
                "CLEANING_ASSIGNMENT": "TEST 청소 거절 기록 완료",
                "CLEANING_COMPLETION": "TEST 청소 미완료 기록",
                "CLEANING_COMPLETION_SUBMISSION": "취소했습니다",
                "CLEANING_EVIDENCE_REVIEW": "사진 보완 요청",
                "CLEANING_PAYMENT_CONFIRMATION": "이체 보류",
                "CLEANING_DAY_CONFIRM": "방문 어려움",
                "CLEANING_ARRIVAL": "출입 문제",
                "CLEANING_START": "현장 문제",
                "CLEANING_ISSUE_SUBMISSION": "취소했습니다",
            }

            patches = (
                patch.object(bot_service, "OPERATOR_PATH", operator),
                patch.object(bot_service, "REQUEST_DIR", requests),
                patch.object(bot_service, "ACTION_SECRET_PATH", action_secret),
                patch.object(bot_service, "api", side_effect=fake_api),
                patch.object(cleaning_issue, "REQUEST_DIR", requests),
                patch.object(cleaning_issue, "ISSUE_DIR", issue_dir),
                patch.object(cleaning_issue, "api", side_effect=fake_api),
                patch.object(cleaning_issue, "secret", return_value=b"secret"),
                patch.object(cleaning_completion_evidence, "COMPLETION_DIR", completion_dir),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
                for index, (action_type, expected_text) in enumerate(cases.items()):
                    action_id = f"negative-{index}"
                    record = {
                        "schema_version": 1,
                        "action_id": action_id,
                        "action_type": action_type,
                        "cleaning_page_id": "cleaning-test",
                        "property_nickname": "JJ",
                        "address": "테스트 주소",
                        "cleaning_date": "2026-09-06",
                        "cleaning_fee_krw": 50000,
                        "start_at": "2026-09-06 11:00",
                        "end_at": "2026-09-06 15:00",
                        "candidate_user_id": 101,
                        "candidate_chat_id": 202,
                        "status": "PENDING",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                        "consumed": False,
                        "test_mode": True,
                        "execute_on_reject": False,
                        "external_writes_on_approval": 0,
                        "external_writes_on_reject": 0,
                    }
                    (requests / f"{action_id}.json").write_text(json.dumps(record))
                    supplied = hmac.new(
                        b"secret", f"{action_id}:reject".encode(), hashlib.sha256
                    ).hexdigest()[:16]
                    before = len(sent)
                    result = bot_service.callback({"callback_query": {
                        "id": f"callback-{index}",
                        "data": f"a:{action_id}:reject:{supplied}",
                        "from": {"id": 101},
                        "message": {"chat": {"id": 202}},
                    }}, "token")
                    self.assertEqual(result, "rejected", action_type)
                    updated = json.loads((requests / f"{action_id}.json").read_text())
                    self.assertTrue(updated["consumed"], action_type)
                    self.assertEqual(updated["status"], "REJECTED", action_type)
                    self.assertEqual(updated["external_writes_executed"], 0, action_type)
                    new_messages = [
                        values["text"] for method, values in sent[before:]
                        if method == "sendMessage" and "text" in values
                    ]
                    self.assertTrue(any(expected_text in text for text in new_messages), action_type)

            all_records = [json.loads(path.read_text()) for path in requests.glob("negative-*.json")]
            self.assertEqual(len(all_records), len(cases))
            self.assertTrue(all(record["external_writes_executed"] == 0 for record in all_records))


if __name__ == "__main__":
    unittest.main()
