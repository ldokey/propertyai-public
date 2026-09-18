import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from telegram_approval import cleaning_assignment


class CleaningAssignmentMessageTests(unittest.TestCase):
    def test_test_mode_is_explicit_and_has_zero_external_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator_path = root / "operator.json"
            token_path = root / "token"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            token_path.write_text("test-token")
            captured = {}

            def fake_api(_token, _method, **values):
                captured.update(values)
                return {"message_id": 78}

            with patch.object(cleaning_assignment, "OPERATOR_PATH", operator_path), \
                 patch.object(cleaning_assignment, "TOKEN_PATH", token_path), \
                 patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "api", side_effect=fake_api), \
                 patch.object(cleaning_assignment, "secret", return_value="secret"), \
                 patch.object(cleaning_assignment, "signature", return_value="sig"):
                result = cleaning_assignment.send_assignment(
                    cleaning_page_id="test-cleaning",
                    cleaning_name="TEST",
                    property_nickname="JJ",
                    address="서울특별시 마포구 테스트로 1",
                    start_at="2026-09-06 11:00",
                    end_at="2026-09-06 15:00",
                    cleaning_fee_krw=60000,
                    expected_last_edited_time="TEST",
                    test_mode=True,
                )
            record = json.loads((request_dir / f"{result['action_id']}.json").read_text())
            self.assertTrue(record["test_mode"])
            self.assertEqual(record["external_writes_on_approval"], 0)
            self.assertFalse(record["execute_on_reject"])
            self.assertIn("[TEST]", captured["text"])
            self.assertIn("Notion·Calendar·정산은 변경되지 않습니다", captured["text"])

    def test_offer_omits_sensitive_fields_and_preserves_safe_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator_path = root / "operator.json"
            token_path = root / "token"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({
                "telegram_user_id": 101,
                "telegram_chat_id": 202,
            }))
            token_path.write_text("test-token")
            captured = {}

            def fake_api(_token, _method, **values):
                captured.update(values)
                return {"message_id": 77}

            exact_address = "서울특별시 마포구 테스트로 1, 지1층 전체"
            door_code = "9876"
            guest_name = "Alice Guest"
            guest_phone = "010-1234-5678"
            guest_email = "alice@example.com"
            access_marker = "entry-pass-2468"
            booking_reference = "BOOKING-REF-8842"

            with patch.object(cleaning_assignment, "OPERATOR_PATH", operator_path), \
                 patch.object(cleaning_assignment, "TOKEN_PATH", token_path), \
                 patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "api", side_effect=fake_api), \
                 patch.object(cleaning_assignment, "secret", return_value="secret"), \
                 patch.object(cleaning_assignment, "signature", return_value="sig"):
                result = cleaning_assignment.send_assignment(
                    cleaning_page_id=booking_reference,
                    cleaning_name=(
                        f"{guest_name} {guest_phone} {guest_email} "
                        f"door-code={door_code} {access_marker}"
                    ),
                    property_nickname="JJ",
                    address=f"{exact_address} · 공동현관 {door_code}",
                    start_at="2026-09-06 11:00",
                    end_at="2026-09-06 15:00",
                    cleaning_fee_krw=60000,
                    expected_last_edited_time="2026-08-02T05:00:00.000Z",
                    default_candidate_party_page_id="party-page",
                )

            record = json.loads((request_dir / f"{result['action_id']}.json").read_text())
            valid_hours = (
                datetime.fromisoformat(record["expires_at"])
                - datetime.fromisoformat(record["created_at"])
            ).total_seconds() / 3600
            self.assertEqual(valid_hours, 24)

            text = captured["text"]
            self.assertIn("숙소: JJ", text)
            self.assertIn("청소 시작: 2026-09-06 11:00", text)
            self.assertIn("완료 목표: 2026-09-06 15:00", text)
            self.assertIn("청소비: ₩60,000", text)
            self.assertIn("24시간 내 수락하지 않으면 다음 담당자", text)

            self.assertNotIn("주소:", text)
            self.assertNotIn(exact_address, text)
            self.assertNotIn(door_code, text)
            self.assertNotIn(guest_name, text)
            self.assertNotIn(guest_phone, text)
            self.assertNotIn(guest_email, text)
            self.assertNotIn(access_marker, text)
            self.assertNotIn(booking_reference, text)
            self.assertNotIn("reservation", text.lower())

            keyboard = json.loads(captured["reply_markup"])
            self.assertEqual(keyboard["inline_keyboard"][0], [
                {
                    "text": "✅ 수락",
                    "callback_data": f"a:{result['action_id']}:approve:sig",
                },
                {
                    "text": "❌ 거절",
                    "callback_data": f"a:{result['action_id']}:reject:sig",
                },
            ])
            self.assertEqual(record["candidate_party_page_id"], "party-page")
            self.assertEqual(record["address"], f"{exact_address} · 공동현관 {door_code}")


    def test_urgent_replacement_offer_discloses_base_premium_and_total_before_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator_path = root / "operator.json"
            token_path = root / "token"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            token_path.write_text("test-token")
            captured = {}

            def fake_api(_token, _method, **values):
                captured.update(values)
                return {"message_id": 82}

            with patch.object(cleaning_assignment, "OPERATOR_PATH", operator_path), \
                 patch.object(cleaning_assignment, "TOKEN_PATH", token_path), \
                 patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "api", side_effect=fake_api), \
                 patch.object(cleaning_assignment, "secret", return_value="secret"), \
                 patch.object(cleaning_assignment, "signature", return_value="sig"):
                result = cleaning_assignment.send_assignment(
                    cleaning_page_id="urgent-cleaning",
                    cleaning_name="urgent",
                    property_nickname="JJ",
                    address="private-runtime-address",
                    start_at="2026-09-08 11:00",
                    end_at="2026-09-08 15:00",
                    cleaning_fee_krw=55000,
                    expected_last_edited_time="projection-edit",
                    expected_acceptance_status="대체필요",
                    candidates=[{
                        "telegram_user_id": 303, "telegram_chat_id": 404,
                        "party_page_id": "party-urgent", "label": "urgent",
                    }],
                    test_mode=True,
                    replacement_urgency="URGENT",
                    urgent_premium_krw=5000,
                    total_agreed_fee_krw=60000,
                    urgent_premium_policy_version="PREMIUM-V1",
                )
            record = json.loads((request_dir / f"{result['action_id']}.json").read_text())
            self.assertEqual(record["cleaning_fee_krw"], 55000)
            self.assertEqual(record["urgent_premium_krw"], 5000)
            self.assertEqual(record["total_agreed_fee_krw"], 60000)
            self.assertEqual(record["urgent_premium_policy_version"], "PREMIUM-V1")
            self.assertIn("기본 청소비: ₩55,000", captured["text"])
            self.assertIn("긴급 대체 추가금: ₩5,000", captured["text"])
            self.assertIn("총 제안금액: ₩60,000", captured["text"])

    def test_replacement_offer_can_bind_to_replacement_required_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator_path = root / "operator.json"
            token_path = root / "token"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            token_path.write_text("test-token")
            with patch.object(cleaning_assignment, "OPERATOR_PATH", operator_path), \
                 patch.object(cleaning_assignment, "TOKEN_PATH", token_path), \
                 patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "api", return_value={"message_id": 81}), \
                 patch.object(cleaning_assignment, "secret", return_value="secret"), \
                 patch.object(cleaning_assignment, "signature", return_value="sig"):
                result = cleaning_assignment.send_assignment(
                    cleaning_page_id="replacement-cleaning",
                    cleaning_name="replacement",
                    property_nickname="JJ",
                    address="private-runtime-address",
                    start_at="2026-09-08 11:00",
                    end_at="2026-09-08 15:00",
                    cleaning_fee_krw=60000,
                    expected_last_edited_time="projection-edit",
                    expected_acceptance_status="대체필요",
                    candidates=[{
                        "telegram_user_id": 303,
                        "telegram_chat_id": 404,
                        "party_page_id": "party-replacement",
                        "label": "replacement",
                    }],
                    test_mode=True,
                )
            record = json.loads((request_dir / f"{result['action_id']}.json").read_text())
            self.assertEqual(record["expected_acceptance_status"], "대체필요")
            self.assertEqual(record["candidate_party_page_id"], "party-replacement")
            self.assertEqual(record["expected_last_edited_time"], "projection-edit")



if __name__ == "__main__":
    unittest.main()
