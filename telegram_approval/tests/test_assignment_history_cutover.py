import copy
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from propertyai_core.global_writer import ProductionWriterError
from telegram_approval import assignment_history as history
from telegram_approval import bot_service


class RecoverableHistoryStore:
    def __init__(self, *, fail_creates=0, stale=False):
        self.rows = []
        self.fail_creates = fail_creates
        self.stale = stale
        self.create_calls = 0

    def query_assignment_generation(self, *, cleaning_page_id, assignment_version):
        return [
            copy.deepcopy(row)
            for row in self.rows
            if history._relation_ids(row, "Cleaning") == [cleaning_page_id]
            and history._text(row, "Assignment Version") == assignment_version
        ]

    def query_by_idempotency(self, key):
        return [
            copy.deepcopy(row)
            for row in self.rows
            if history._text(row, "Idempotency Key") == key
        ]

    @staticmethod
    def _row(evidence, assignment_version, idempotency_key, page_id):
        return {
            "id": page_id,
            "properties": {
                "Cleaning": history._relation_property(evidence.cleaning_page_id),
                "후보 인력": history._relation_property(evidence.cleaner_party_page_id),
                "제안 상태": history._select_property("ACCEPTED"),
                "Hold 상태": history._select_property("HARD_BOOKED"),
                "Binding Offer": history._checkbox_property(True),
                "응답 시각": history._date_property(evidence.accepted_at),
                "제안 시각": history._date_property(evidence.offered_at),
                "Action ID": history._rich_text_property(evidence.action_id),
                "Idempotency Key": history._rich_text_property(idempotency_key),
                "Assignment Version": history._rich_text_property(assignment_version),
                "데이터 환경": history._select_property("PRODUCTION"),
                "Telegram Message ID": history._rich_text_property(
                    evidence.telegram_message_id
                ),
                "제안 라운드": history._number_property(evidence.proposal_round),
            },
        }

    def create_accepted_assignment(self, *, evidence, assignment_version, idempotency_key):
        self.create_calls += 1
        if self.stale:
            raise ProductionWriterError("stale history writer")
        if self.fail_creates > 0:
            self.fail_creates -= 1
            raise history.AssignmentHistoryError(
                history.REMOTE_NOTION_FAILURE, "synthetic history failure"
            )
        row = self._row(
            evidence, assignment_version, idempotency_key, f"history-{self.create_calls}"
        )
        self.rows.append(copy.deepcopy(row))
        return row

    def accept_existing_offer(self, *args, **kwargs):
        raise AssertionError("not expected")

    def release_accepted_assignment(self, *args, **kwargs):
        raise AssertionError("not expected")


class AssignmentHistoryCutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.request_dir = self.root / "requests"
        self.request_dir.mkdir()
        self.secret = self.root / "action-secret"
        self.secret.write_text("cutover-secret")
        self.action_id = "assignment-cutover-A"
        self.record_path = self.request_dir / f"{self.action_id}.json"
        self.record_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "action_id": self.action_id,
                    "action_type": "CLEANING_ASSIGNMENT",
                    "execute_on_reject": True,
                    "cleaning_page_id": "cleaning-A",
                    "cleaning_name": "Cleaning A",
                    "property_nickname": "JJ",
                    "address": "safe",
                    "start_at": "2026-08-31 11:00",
                    "end_at": "2026-08-31 15:00",
                    "cleaning_fee_krw": 50000,
                    "expected_last_edited_time": "T0",
                    "expected_acceptance_status": "수락대기",
                    "candidate_user_id": 101,
                    "candidate_chat_id": 202,
                    "candidate_party_page_id": "party-A",
                    "candidate_label": "Cleaner A",
                    "remaining_candidates": [],
                    "observers": [],
                    "proposal_round": 1,
                    "status": "PENDING",
                    "created_at": "2026-08-30T00:00:00+09:00",
                    "expires_at": "2099-08-31T00:00:00+09:00",
                    "consumed": False,
                    "test_mode": False,
                    "external_writes_on_approval": 1,
                    "external_writes_on_reject": 1,
                    "payment_effects": 0,
                    "telegram_message_id": 303,
                }
            )
        )
        self.execution_calls = 0
        self.api_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def update(self):
        digest = hmac.new(
            self.secret.read_text().strip().encode(),
            f"{self.action_id}:approve".encode(),
            hashlib.sha256,
        ).hexdigest()[:16]
        return {
            "callback_query": {
                "id": "callback-1",
                "data": f"a:{self.action_id}:approve:{digest}",
                "from": {"id": 101},
                "message": {"chat": {"id": 202}},
            }
        }

    def fake_api(self, _token, method, **values):
        self.api_calls.append((method, values))
        return {"ok": True}

    def execute_success(self, _record, _decision):
        self.execution_calls += 1
        return {
            "cleaning_assignment": "ACCEPTED",
            "next_expected_last_edited_time": "T1",
            "projection_version": 1,
        }

    def patches(self):
        from gmail_ingest import lifecycle_actions

        return (
            patch.object(
                lifecycle_actions,
                "execute_lifecycle_action",
                side_effect=self.execute_success,
            ),
            patch.object(bot_service, "assert_current_production_writer", return_value={}),
            patch.object(history, "CANONICAL_ASSIGNMENT_REQUEST_DIR", self.request_dir),
            patch.object(history, "RECEIPT_SECRET_PATH", self.secret),
            patch.object(history, "ASSIGNMENT_HISTORY_LOCK_DIR", self.root / "locks"),
        )

    def invoke(self, store):
        return bot_service.callback(
            self.update(),
            "token",
            request_dir=self.request_dir,
            action_secret_path=self.secret,
            request_api=self.fake_api,
            assignment_history_store=store,
        )

    def test_history_failure_preserves_factual_success_and_replay_never_reexecutes(self):
        store = RecoverableHistoryStore(fail_creates=1)
        p1, p2, p3, p4, p5 = self.patches()
        with p1, p2, p3, p4, p5:
            first = self.invoke(store)
            after_first = json.loads(self.record_path.read_text())
            second = self.invoke(store)
            after_second = json.loads(self.record_path.read_text())

        self.assertEqual(first, "approved")
        self.assertEqual(second, "approved")
        self.assertEqual(self.execution_calls, 1)
        self.assertEqual(after_first["status"], "EXECUTED")
        self.assertEqual(after_first["execution_result"]["cleaning_assignment"], "ACCEPTED")
        self.assertIn("assignment_execution_receipt", after_first)
        self.assertTrue(
            after_first["assignment_history_operation_identity"].startswith(
                "CLEANING_ASSIGNMENT_ACCEPTED:v1:"
            )
        )
        self.assertEqual(after_first["assignment_history_status"], "RETRY_REQUIRED")
        self.assertNotEqual(after_first["status"], "EXECUTION_RETRY_REQUIRED")
        self.assertEqual(after_second["status"], "EXECUTED")
        self.assertEqual(after_second["assignment_history_status"], "MATERIALIZED")
        self.assertEqual(len(store.rows), 1)

    def test_stale_history_writer_propagates_fence_but_keeps_receipt_pending(self):
        store = RecoverableHistoryStore(stale=True)
        p1, p2, p3, p4, p5 = self.patches()
        with p1, p2, p3, p4, p5:
            with self.assertRaises(ProductionWriterError):
                self.invoke(store)
        record = json.loads(self.record_path.read_text())
        self.assertEqual(self.execution_calls, 1)
        self.assertEqual(record["status"], "EXECUTED")
        self.assertEqual(record["assignment_history_status"], "PENDING")
        self.assertIn("assignment_execution_receipt", record)
        self.assertNotIn("assignment_history_error_type", record)

    def test_parent_post_external_stale_fence_is_not_rewritten_as_execution_failure(self):
        from gmail_ingest import lifecycle_actions
        from telegram_approval.assignment_execution_receipt import (
            load_durable_successful_assignment_execution,
        )

        store = RecoverableHistoryStore()
        durable_snapshots = []
        original_atomic = bot_service.atomic_private

        def durable_write(write_path, value):
            durable_snapshots.append(copy.deepcopy(value))
            return original_atomic(write_path, value)

        with patch.object(
            lifecycle_actions,
            "execute_lifecycle_action",
            side_effect=self.execute_success,
        ) as execute, patch.object(
            bot_service,
            "assert_current_production_writer",
            side_effect=[{}, ProductionWriterError("stale parent")],
        ), patch.object(
            bot_service, "atomic_private", side_effect=durable_write
        ), patch.object(
            history, "CANONICAL_ASSIGNMENT_REQUEST_DIR", self.request_dir
        ), patch.object(
            history, "RECEIPT_SECRET_PATH", self.secret
        ), patch.object(
            history, "ASSIGNMENT_HISTORY_LOCK_DIR", self.root / "locks"
        ):
            with self.assertRaises(ProductionWriterError):
                self.invoke(store)

        record = json.loads(self.record_path.read_text())
        self.assertEqual(self.execution_calls, 1)
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(record["consumed"])
        self.assertEqual(record["status"], "APPROVED_PRODUCTION")
        self.assertNotIn("execution_result", record)
        self.assertNotIn("assignment_execution_receipt", record)
        self.assertNotEqual(record.get("status"), "EXECUTION_RETRY_REQUIRED")
        self.assertEqual(len(durable_snapshots), 1)
        self.assertEqual(store.rows, [])

        # Factual identity and receipt are durable despite zero stale
        # authoritative writes after the effect.
        factual = load_durable_successful_assignment_execution(
            self.record_path, record, secret_path=self.secret
        )
        self.assertEqual(
            factual["execution_result"]["cleaning_assignment"], "ACCEPTED"
        )
        self.assertTrue(
            factual["pre_effect_operation_identity"].startswith(
                "CLEANING_ASSIGNMENT_EFFECT:v1:"
            )
        )

        # Simulate process termination: a fresh callback reads only the request
        # and immutable sidecars, projects success, and materializes history.
        with patch.object(
            lifecycle_actions, "execute_lifecycle_action"
        ) as blind_reexecution, patch.object(
            bot_service, "assert_current_production_writer", return_value={}
        ), patch.object(
            history, "CANONICAL_ASSIGNMENT_REQUEST_DIR", self.request_dir
        ), patch.object(
            history, "RECEIPT_SECRET_PATH", self.secret
        ), patch.object(
            history, "ASSIGNMENT_HISTORY_LOCK_DIR", self.root / "locks"
        ):
            successor = self.invoke(store)

        converged = json.loads(self.record_path.read_text())
        self.assertEqual(successor, "approved")
        blind_reexecution.assert_not_called()
        self.assertEqual(self.execution_calls, 1)
        self.assertEqual(converged["status"], "EXECUTED")
        self.assertEqual(
            converged["execution_result"]["cleaning_assignment"], "ACCEPTED"
        )
        self.assertEqual(converged["assignment_history_status"], "MATERIALIZED")
        self.assertEqual(len(store.rows), 1)

    def test_stale_before_effect_fails_closed_without_execution_or_false_retry(self):
        from gmail_ingest import lifecycle_actions

        with patch.object(
            lifecycle_actions, "execute_lifecycle_action"
        ) as execute, patch.object(
            bot_service,
            "assert_current_production_writer",
            side_effect=ProductionWriterError("stale before effect"),
        ):
            with self.assertRaises(ProductionWriterError):
                self.invoke(RecoverableHistoryStore())

        record = json.loads(self.record_path.read_text())
        execute.assert_not_called()
        self.assertEqual(record["status"], "APPROVED_PRODUCTION")
        self.assertNotIn("execution_result", record)
        self.assertNotEqual(record.get("status"), "EXECUTION_RETRY_REQUIRED")
        provenance = list(
            (self.request_dir / ".assignment-execution-provenance").glob("*.json")
        )
        self.assertEqual([path.name for path in provenance], [f"{self.action_id}.intent.json"])

        with patch.object(
            lifecycle_actions, "execute_lifecycle_action"
        ) as replay_execute:
            replay = self.invoke(RecoverableHistoryStore())
        self.assertEqual(replay, "ignored")
        replay_execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
