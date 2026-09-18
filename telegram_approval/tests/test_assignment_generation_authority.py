import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_approval import cleaning_assignment
from telegram_approval.assignment_execution_receipt import canonical_assignment_version
from telegram_approval.assignment_history import (
    AssignmentHistoryError,
    CONFLICT,
    assignment_generation_lock,
)


class AssignmentGenerationAuthorityTests(unittest.TestCase):
    def test_assignment_version_is_retry_stable_action_identity(self):
        record = {
            "schema_version": 2,
            "action_type": "CLEANING_ASSIGNMENT",
            "action_id": "generation-A",
            "proposal_round": 1,
        }
        self.assertEqual(canonical_assignment_version(record), "generation-A")
        record["proposal_round"] = 22
        self.assertEqual(canonical_assignment_version(record), "generation-A")
        record["action_id"] = "generation-B"
        self.assertEqual(canonical_assignment_version(record), "generation-B")

    def test_action_id_collision_never_overwrites_existing_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            operator = root / "operator.json"
            operator.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            token = root / "token"
            token.write_text("test")
            existing = request_dir / "collision.json"
            original = {
                "schema_version": 2,
                "action_id": "collision",
                "action_type": "CLEANING_ASSIGNMENT",
                "cleaning_page_id": "other-cleaning",
                "status": "EXECUTED",
                "consumed": True,
            }
            existing.write_text(json.dumps(original))
            sent = []

            def fake_api(_token, method, **values):
                if method == "sendMessage":
                    sent.append(values)
                    return {"message_id": 77}
                return {}

            with patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "OPERATOR_PATH", operator), \
                 patch.object(cleaning_assignment, "TOKEN_PATH", token), \
                 patch.object(cleaning_assignment, "api", side_effect=fake_api), \
                 patch.object(cleaning_assignment, "secret", return_value="secret"), \
                 patch.object(cleaning_assignment, "signature", return_value="sig"), \
                 patch.object(cleaning_assignment, "assert_current_production_writer", return_value={"status": "MATCH"}), \
                 patch.object(
                     cleaning_assignment.secrets,
                     "token_urlsafe",
                     side_effect=["collision", "fresh-generation"],
                 ):
                result = cleaning_assignment.send_assignment(
                    cleaning_page_id="new-cleaning",
                    cleaning_name="NEW",
                    property_nickname="JJ",
                    address="safe",
                    start_at="2026-09-01 11:00",
                    end_at="2026-09-01 15:00",
                    cleaning_fee_krw=50000,
                    expected_last_edited_time="T0",
                    test_mode=False,
                )

            self.assertEqual(result["action_id"], "fresh-generation")
            self.assertEqual(json.loads(existing.read_text()), original)
            self.assertTrue((request_dir / "fresh-generation.json").exists())
            self.assertEqual(len(sent), 1)


    def test_same_pending_generation_reuses_action_without_second_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            operator = root / "operator.json"
            operator.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            token = root / "token"
            token.write_text("test")
            sent = []

            def fake_api(_token, method, **values):
                if method == "sendMessage":
                    sent.append(values)
                    return {"message_id": 77}
                return {}

            kwargs = dict(
                cleaning_page_id="cleaning-retry",
                cleaning_name="RETRY",
                property_nickname="JJ",
                address="safe",
                start_at="2026-09-01 11:00",
                end_at="2026-09-01 15:00",
                cleaning_fee_krw=50000,
                expected_last_edited_time="T0",
                candidates=[{
                    "telegram_user_id": 101,
                    "telegram_chat_id": 202,
                    "party_page_id": "party-A",
                }],
                test_mode=False,
            )
            with patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "OPERATOR_PATH", operator), \
                 patch.object(cleaning_assignment, "TOKEN_PATH", token), \
                 patch.object(cleaning_assignment, "api", side_effect=fake_api), \
                 patch.object(cleaning_assignment, "secret", return_value="secret"), \
                 patch.object(cleaning_assignment, "signature", return_value="sig"), \
                 patch.object(cleaning_assignment, "assert_current_production_writer", return_value={"status": "MATCH"}), \
                 patch.object(cleaning_assignment.secrets, "token_urlsafe", return_value="stable-action"):
                first = cleaning_assignment.send_assignment(**kwargs)
                second = cleaning_assignment.send_assignment(**kwargs)

            self.assertTrue(first["sent"])
            self.assertFalse(second["sent"])
            self.assertEqual(second["reason"], "PENDING_REQUEST_EXISTS")
            self.assertEqual(first["action_id"], second["action_id"])
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(list(request_dir.glob("*.json"))), 1)

    def test_repeated_collisions_fail_closed_before_any_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            operator = root / "operator.json"
            operator.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            token = root / "token"
            token.write_text("test")
            (request_dir / "collision.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "action_id": "collision",
                        "action_type": "CLEANING_ASSIGNMENT",
                        "cleaning_page_id": "other",
                        "status": "EXECUTED",
                        "consumed": True,
                    }
                )
            )
            calls = []

            def fake_api(*args, **kwargs):
                calls.append((args, kwargs))
                return {"message_id": 1}

            with patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "OPERATOR_PATH", operator), \
                 patch.object(cleaning_assignment, "TOKEN_PATH", token), \
                 patch.object(cleaning_assignment, "api", side_effect=fake_api), \
                 patch.object(
                     cleaning_assignment.secrets,
                     "token_urlsafe",
                     return_value="collision",
                 ):
                with self.assertRaises(RuntimeError):
                    cleaning_assignment.send_assignment(
                        cleaning_page_id="new-cleaning",
                        cleaning_name="NEW",
                        property_nickname="JJ",
                        address="safe",
                        start_at="2026-09-01 11:00",
                        end_at="2026-09-01 15:00",
                        cleaning_fee_krw=50000,
                        expected_last_edited_time="T0",
                        test_mode=True,
                    )
            self.assertEqual(calls, [])

    def test_same_generation_lock_fails_closed_for_live_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            with assignment_generation_lock(
                cleaning_page_id="cleaning-A",
                assignment_version="generation-A",
                lock_dir=lock_dir,
                timeout_seconds=0.2,
            ):
                with self.assertRaises(AssignmentHistoryError) as caught:
                    with assignment_generation_lock(
                        cleaning_page_id="cleaning-A",
                        assignment_version="generation-A",
                        lock_dir=lock_dir,
                        timeout_seconds=0.01,
                    ):
                        pass
            self.assertEqual(caught.exception.code, CONFLICT)


if __name__ == "__main__":
    unittest.main()
