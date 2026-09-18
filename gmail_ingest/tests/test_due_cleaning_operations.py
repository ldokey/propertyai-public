import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from telegram_approval import send_cleaning_operations
from telegram_approval.cleaner_config import CLEANER_TOKEN_PATH_CONFIG
from telegram_approval.ops_config import OPS_ALLOWLIST_CONFIG, OPS_TOKEN_PATH_CONFIG


class DueCleaningOperationsTests(unittest.TestCase):
    def test_day_confirm_reconfirm_escalation_and_arrival_are_deduplicated(self):
        page = {
            "id": "cleaning-page",
            "last_edited_time": "2026-08-02T00:00:00.000Z",
            "properties": {
                "상태": {"select": {"name": "담당자 배정"}},
                "배정 수락 상태": {"select": {"name": "수락"}},
                "점검일": {"date": {"start": "2026-08-02"}},
                "시작 예정": {"date": {"start": "2026-08-02T11:00:00+09:00"}},
                "완료 목표": {"date": {"start": "2026-08-02T15:00:00+09:00"}},
                "연결 집": {"relation": [{"id": "house"}]},
                "관련 Reservation": {"relation": [{"id": "reservation"}]},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
            },
        }
        candidate = {"party_page_id": "party", "telegram_user_id": 1, "telegram_chat_id": 1}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mappings = root / "mappings.json"
            requests = root / "requests"
            requests.mkdir()
            mappings.write_text(json.dumps({"listings": {"1": {
                "nickname": "JJ", "property_page_id": "house", "address": "test"
            }}}))
            common = (
                patch.object(send_cleaning_operations, "MAPPINGS_PATH", mappings),
                patch.object(send_cleaning_operations, "REQUEST_DIR", requests),
                patch.object(send_cleaning_operations, "_notion", side_effect=lambda method, path, body=None: (
                    {"results": [page]} if method == "POST" else {
                        "properties": {
                            "데이터 환경": {"select": {"name": "PRODUCTION"}},
                            "등록 상태": {"select": {"name": "APPROVED"}},
                            "상태": {"select": {"name": "확정"}},
                            "출입정보 저장 상태": {"select": {"name": "STORED"}},
                            "출입 코드": {"type": "rich_text", "rich_text": [{"plain_text": "0419"}]},
                        }
                    }
                )),
                patch.object(send_cleaning_operations, "assignment_targets", return_value=([candidate], [])),
            )
            with common[0], common[1], common[2], common[3]:
                at_0830 = send_cleaning_operations.due_events(datetime(2026, 8, 2, 8, 30, tzinfo=ZoneInfo("Asia/Seoul")))
                self.assertEqual([item["event"] for item in at_0830], ["DAY_CONFIRM"])
                pending = {
                    "action_id": "day", "action_type": "CLEANING_DAY_CONFIRM",
                    "cleaning_page_id": "cleaning-page", "status": "PENDING",
                    "consumed": False, "test_mode": False, "created_at": "2026-08-02T00:00:00+00:00"
                }
                (requests / "day.json").write_text(json.dumps(pending))
                at_0900 = send_cleaning_operations.due_events(datetime(2026, 8, 2, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")))
                self.assertEqual([item["event"] for item in at_0900], ["DAY_RECONFIRM"])
                (requests / "reconfirm.json").write_text(json.dumps({
                    "action_id": "reconfirm", "action_type": "CLEANING_DAY_RECONFIRM",
                    "cleaning_page_id": "cleaning-page", "test_mode": False, "created_at": "2026-08-02T00:30:00+00:00"
                }))
                at_0920 = send_cleaning_operations.due_events(datetime(2026, 8, 2, 9, 20, tzinfo=ZoneInfo("Asia/Seoul")))
                self.assertEqual([item["event"] for item in at_0920], ["DAY_ESCALATION"])
                pending.update({"status": "EXECUTED", "consumed": True,
                                "execution_result": {"cleaning_operation": "DAY_VISIT_CONFIRMED"}})
                (requests / "day.json").write_text(json.dumps(pending))
                at_1020 = send_cleaning_operations.due_events(datetime(2026, 8, 2, 10, 20, tzinfo=ZoneInfo("Asia/Seoul")))
                self.assertEqual([item["event"] for item in at_1020], ["ARRIVAL"])
                self.assertEqual(at_1020[0]["door_code"], "0419")

    def test_delivery_uncertain_day_confirm_is_not_blind_redispatched(self):
        page = {
            "id": "cleaning-page",
            "last_edited_time": "2026-08-02T00:00:00.000Z",
            "properties": {
                "상태": {"select": {"name": "담당자 배정"}},
                "배정 수락 상태": {"select": {"name": "수락"}},
                "점검일": {"date": {"start": "2026-08-02"}},
                "시작 예정": {"date": {"start": "2026-08-02T11:00:00+09:00"}},
                "완료 목표": {"date": {"start": "2026-08-02T15:00:00+09:00"}},
                "연결 집": {"relation": [{"id": "house"}]},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
            },
        }
        candidate = {"party_page_id": "party", "telegram_user_id": 1, "telegram_chat_id": 1}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mappings = root / "mappings.json"
            requests = root / "requests"
            requests.mkdir()
            mappings.write_text(json.dumps({"listings": {"1": {
                "nickname": "JJ", "property_page_id": "house", "address": "test"
            }}}))
            (requests / "uncertain.json").write_text(json.dumps({
                "action_id": "uncertain",
                "action_type": "CLEANING_DAY_CONFIRM",
                "cleaning_page_id": "cleaning-page",
                "status": "DELIVERY_UNCERTAIN",
                "consumed": False,
                "test_mode": False,
                "created_at": "2026-08-01T23:30:00+00:00",
            }))
            with patch.object(send_cleaning_operations, "MAPPINGS_PATH", mappings), \
                 patch.object(send_cleaning_operations, "REQUEST_DIR", requests), \
                 patch.object(send_cleaning_operations, "_notion", return_value={"results": [page]}), \
                 patch.object(send_cleaning_operations, "assignment_targets", return_value=([candidate], [])):
                due = send_cleaning_operations.due_events(
                    datetime(2026, 8, 2, 8, 45, tzinfo=ZoneInfo("Asia/Seoul"))
                )
        self.assertEqual(due, [])

    def test_send_scheduler_requires_all_role_config_before_discovery(self):
        with patch.object(send_cleaning_operations, "due_events") as due:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, CLEANER_TOKEN_PATH_CONFIG):
                    send_cleaning_operations.run(send=True)
            due.assert_not_called()

            with patch.dict(os.environ, {CLEANER_TOKEN_PATH_CONFIG: "/synthetic/cleaner"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, OPS_TOKEN_PATH_CONFIG):
                    send_cleaning_operations.run(send=True)
            due.assert_not_called()

            with patch.dict(os.environ, {
                CLEANER_TOKEN_PATH_CONFIG: "/synthetic/cleaner",
                OPS_TOKEN_PATH_CONFIG: "/synthetic/ops",
            }, clear=True):
                with self.assertRaisesRegex(RuntimeError, OPS_ALLOWLIST_CONFIG):
                    send_cleaning_operations.run(send=True)
            due.assert_not_called()


if __name__ == "__main__":
    unittest.main()
