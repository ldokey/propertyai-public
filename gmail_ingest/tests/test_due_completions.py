import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from telegram_approval import send_due_completions
from telegram_approval.cleaner_config import CLEANER_TOKEN_PATH_CONFIG, CleanerRuntimePaths


class DueCompletionTests(unittest.TestCase):
    def test_only_due_accepted_assigned_cleaning_is_selected(self):
        page = {
            "id": "cleaning-page",
            "last_edited_time": "2026-08-02T05:00:00.000Z",
            "properties": {
                "상태": {"select": {"name": "담당자 배정"}},
                "배정 수락 상태": {"select": {"name": "수락"}},
                "완료 목표": {"date": {"start": "2026-08-02T15:00:00+09:00"}},
                "점검일": {"date": {"start": "2026-08-02"}},
                "청소비 Snapshot": {"number": 60000},
                "연결 집": {"relation": [{"id": "house"}]},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mappings_path = root / "mappings.json"
            requests = root / "requests"
            requests.mkdir()
            mappings_path.write_text(json.dumps({"listings": {"1": {
                "nickname": "JJ",
                "property_page_id": "house",
                "address": "test",
            }}}))
            candidate = {"party_page_id": "party", "telegram_user_id": 1, "telegram_chat_id": 1}
            with patch.object(send_due_completions, "MAPPINGS_PATH", mappings_path), \
                 patch.object(send_due_completions, "REQUEST_DIR", requests), \
                 patch.object(send_due_completions, "_notion", return_value={"results": [page]}), \
                 patch.object(send_due_completions, "assignment_targets", return_value=([candidate], [])), \
                 patch.object(send_due_completions, "_accepted_compensation_for_completion", return_value={
                     "replacement_urgency": "NORMAL", "base_fee_krw": 60000,
                     "urgent_premium_krw": 0, "total_agreed_fee_krw": 60000,
                     "urgent_premium_policy_version": None, "legacy_normal_fallback": True,
                     "assignment_page_id": "history", "assignment_version": "v1",
                     "accepted_assignment_action_id": "a1",
                 }):
                due = send_due_completions.due_cleanings(
                    datetime(2026, 8, 2, 15, 1, tzinfo=ZoneInfo("Asia/Seoul"))
                )
                future = send_due_completions.due_cleanings(
                    datetime(2026, 8, 2, 14, 59, tzinfo=ZoneInfo("Asia/Seoul"))
                )
            self.assertEqual(len(due), 1)
            self.assertEqual(future, [])

    def test_urgent_completion_economics_come_from_canonical_19_snapshot(self):
        history_page = {"id": "history-urgent", "properties": {
            "Replacement Urgency": {"select": {"name": "URGENT"}},
            "Base Fee Snapshot": {"number": 55000},
            "Urgent Premium Snapshot": {"number": 5000},
            "Total Agreed Fee Snapshot": {"number": 60000},
            "Urgent Premium Policy Version": {
                "rich_text": [{"plain_text": "P1", "type": "text", "text": {"content": "P1"}}]
            },
        }}
        with patch.object(
            send_due_completions, "resolve_effective_accepted_assignment",
            return_value=(type("Evidence", (), {"assignment_version": "v1", "action_id": "a1"})(), history_page),
        ) as resolver:
            economics = send_due_completions._accepted_compensation_for_completion(
                cleaning_page_id="cleaning-page", cleaner_party_page_id="party",
                base_fee_krw=55000, history_store=object(),
            )
        self.assertEqual(economics["replacement_urgency"], "URGENT")
        self.assertEqual(economics["base_fee_krw"], 55000)
        self.assertEqual(economics["urgent_premium_krw"], 5000)
        self.assertEqual(economics["total_agreed_fee_krw"], 60000)
        self.assertEqual(economics["urgent_premium_policy_version"], "P1")
        self.assertEqual(economics["assignment_page_id"], "history-urgent")
        self.assertEqual(economics["assignment_version"], "v1")
        resolver.assert_called_once()

    def test_role_specific_request_path_and_missing_cleaner_config_fail_closed(self):
        self.assertEqual(send_due_completions.REQUEST_DIR, CleanerRuntimePaths().request_dir)
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(send_due_completions, "due_cleanings") as due:
            with self.assertRaisesRegex(RuntimeError, CLEANER_TOKEN_PATH_CONFIG):
                send_due_completions.run(send=True)
        due.assert_not_called()


if __name__ == "__main__":
    unittest.main()
