import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_approval import cleaner_registry


class CleanerRegistryTests(unittest.TestCase):
    def test_one_time_invite_registers_cleaner_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roster_path = root / "cleaners.json"
            invite_dir = root / "invites"
            metadata_path = root / "metadata.json"
            metadata_path.write_text(json.dumps({"bot_username": "TestBot"}))
            with patch.object(cleaner_registry, "ROSTER_PATH", roster_path), \
                 patch.object(cleaner_registry, "INVITE_DIR", invite_dir), \
                 patch.object(cleaner_registry, "METADATA_PATH", metadata_path), \
                 patch.object(cleaner_registry.secrets, "token_urlsafe", side_effect=["code", "invite", "identity"]):
                invitation = cleaner_registry.create_invite(
                    label="담당자 A",
                    party_page_id="party-a",
                    properties=["JJ"],
                    priority=1,
                )
                first = cleaner_registry.consume_invite("code", telegram_user_id=2, telegram_chat_id=2)
                second = cleaner_registry.consume_invite("code", telegram_user_id=3, telegram_chat_id=3)
            self.assertIn("start=c_code", invitation["url"])
            self.assertEqual(first["party_page_id"], "party-a")
            self.assertIsNone(second)

    def test_real_cleaner_is_actionable_and_operator_is_observer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roster_path = root / "cleaners.json"
            roster_path.write_text(json.dumps({
                "schema_version": 1,
                "cleaners": [
                    {
                        "identity_id": "operator",
                        "label": "운영자 확인용",
                        "role": "OPERATOR_OBSERVER",
                        "telegram_user_id": 1,
                        "telegram_chat_id": 1,
                        "party_page_id": "operator-party",
                        "properties": ["JJ"],
                        "priority_by_property": {"JJ": 999},
                        "observer_copy": True,
                        "fallback_candidate": True,
                        "status": "ACTIVE",
                    },
                    {
                        "identity_id": "worker",
                        "label": "담당자 A",
                        "role": "CLEANER",
                        "telegram_user_id": 2,
                        "telegram_chat_id": 2,
                        "party_page_id": "worker-party",
                        "properties": ["JJ"],
                        "priority_by_property": {"JJ": 1},
                        "observer_copy": False,
                        "fallback_candidate": False,
                        "status": "ACTIVE",
                    },
                ],
            }))
            with patch.object(cleaner_registry, "ROSTER_PATH", roster_path):
                candidates, observers = cleaner_registry.assignment_targets("JJ")
            self.assertEqual(candidates[0]["telegram_chat_id"], 2)
            self.assertEqual(observers[0]["telegram_chat_id"], 1)

    def test_operator_becomes_fallback_when_no_worker_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster_path = Path(tmp) / "cleaners.json"
            roster_path.write_text(json.dumps({
                "schema_version": 1,
                "cleaners": [{
                    "identity_id": "operator",
                    "label": "운영자 확인용",
                    "role": "OPERATOR_OBSERVER",
                    "telegram_user_id": 1,
                    "telegram_chat_id": 1,
                    "party_page_id": "operator-party",
                    "properties": ["JJ"],
                    "priority_by_property": {"JJ": 999},
                    "observer_copy": True,
                    "fallback_candidate": True,
                    "status": "ACTIVE",
                }],
            }))
            with patch.object(cleaner_registry, "ROSTER_PATH", roster_path):
                candidates, observers = cleaner_registry.assignment_targets("JJ")
            self.assertEqual(candidates[0]["telegram_chat_id"], 1)
            self.assertEqual(observers, [])


if __name__ == "__main__":
    unittest.main()
