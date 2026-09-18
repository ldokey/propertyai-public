import copy
import json
import tempfile
import unittest
from pathlib import Path

from telegram_approval.assignment_execution_receipt import (
    AssignmentExecutionReceiptError,
    acceptance_operation_identity,
    canonical_assignment_version,
    create_successful_assignment_execution_receipt,
    load_durable_successful_assignment_execution,
    persist_assignment_execution_intent,
    persist_successful_assignment_execution_receipt,
    verify_assignment_execution_receipt,
)


class AssignmentExecutionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secret = Path(self.tmp.name) / "action-secret"
        self.secret.write_text("receipt-test-secret")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def approved_record():
        return {
            "schema_version": 2,
            "action_id": "action-A",
            "action_type": "CLEANING_ASSIGNMENT",
            "test_mode": False,
            "consumed": True,
            "status": "APPROVED_PRODUCTION",
            "cleaning_page_id": "cleaning-A",
            "candidate_party_page_id": "party-A",
            "candidate_user_id": 101,
            "candidate_chat_id": 202,
            "telegram_message_id": 303,
            "proposal_round": 1,
            "created_at": "2026-08-30T00:00:00+09:00",
            "expires_at": "2026-08-31T00:00:00+09:00",
            "consumed_at": "2026-08-30T00:05:00+09:00",
        }

    @staticmethod
    def execution_result():
        return {
            "cleaning_assignment": "ACCEPTED",
            "next_expected_last_edited_time": "2026-08-29T15:05:01.000Z",
            "projection_version": 7,
        }

    def executed_record(self):
        record = self.approved_record()
        executed_at = "2026-08-30T00:05:01+09:00"
        result = self.execution_result()
        record["assignment_execution_receipt"] = (
            create_successful_assignment_execution_receipt(
                record, result, executed_at=executed_at, secret_path=self.secret
            )
        )
        record["execution_result"] = result
        record["executed_at"] = executed_at
        record["status"] = "EXECUTED"
        return record

    def test_receipt_verifies_exact_semantic_execution_payload(self):
        record = self.executed_record()
        payload = verify_assignment_execution_receipt(record, secret_path=self.secret)
        self.assertEqual(payload["action_id"], "action-A")
        self.assertEqual(payload["cleaning_page_id"], "cleaning-A")
        self.assertEqual(payload["cleaner_party_page_id"], "party-A")
        self.assertEqual(payload["assignment_version"], "action-A")
        self.assertEqual(payload["execution_result"]["projection_version"], 7)

    def test_any_signed_semantic_drift_fails_closed(self):
        record = self.executed_record()
        for mutate in (
            lambda r: r.__setitem__("cleaning_page_id", "cleaning-B"),
            lambda r: r.__setitem__("candidate_party_page_id", "party-B"),
            lambda r: r["execution_result"].__setitem__("projection_version", 8),
            lambda r: r.__setitem__("executed_at", "2026-08-30T00:05:02+09:00"),
        ):
            candidate = copy.deepcopy(record)
            mutate(candidate)
            with self.assertRaises(AssignmentExecutionReceiptError):
                verify_assignment_execution_receipt(candidate, secret_path=self.secret)

    def test_operation_identity_is_retry_stable_and_payload_bound(self):
        record = self.executed_record()
        first = acceptance_operation_identity(record, secret_path=self.secret)
        second = acceptance_operation_identity(copy.deepcopy(record), secret_path=self.secret)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("CLEANING_ASSIGNMENT_ACCEPTED:v1:"))

        # A semantic mutation cannot silently reuse the original operation key;
        # the authenticated receipt rejects it before a new identity is derived.
        changed = copy.deepcopy(record)
        changed["execution_result"]["projection_version"] = 99
        with self.assertRaises(AssignmentExecutionReceiptError):
            acceptance_operation_identity(changed, secret_path=self.secret)

    def test_receipt_cannot_be_minted_before_factual_acceptance(self):
        record = self.approved_record()
        bad_results = [
            {"cleaning_assignment": "NEXT_CANDIDATE_REQUIRED"},
            {"cleaning_assignment": "FAILED"},
        ]
        for result in bad_results:
            with self.assertRaises(AssignmentExecutionReceiptError):
                create_successful_assignment_execution_receipt(
                    record,
                    result,
                    executed_at="2026-08-30T00:05:01+09:00",
                    secret_path=self.secret,
                )
        test_record = copy.deepcopy(record)
        test_record["test_mode"] = True
        with self.assertRaises(AssignmentExecutionReceiptError):
            create_successful_assignment_execution_receipt(
                test_record,
                self.execution_result(),
                executed_at="2026-08-30T00:05:01+09:00",
                secret_path=self.secret,
            )

    def test_assignment_version_is_action_generation_not_projection_round(self):
        record = self.approved_record()
        self.assertEqual(canonical_assignment_version(record), "action-A")
        record["proposal_round"] = 9
        self.assertEqual(canonical_assignment_version(record), "action-A")

    def test_pre_effect_identity_and_factual_receipt_survive_process_boundary(self):
        root = Path(self.tmp.name)
        request_path = root / "requests" / "action-A.json"
        request_path.parent.mkdir()
        record = self.approved_record()
        request_path.write_text(json.dumps(record))

        first = persist_assignment_execution_intent(
            request_path, record, "approve", secret_path=self.secret
        )
        second = persist_assignment_execution_intent(
            request_path, copy.deepcopy(record), "approve", secret_path=self.secret
        )
        self.assertEqual(first, second)
        self.assertTrue(
            first["operation_identity"].startswith("CLEANING_ASSIGNMENT_EFFECT:v1:")
        )

        receipt = persist_successful_assignment_execution_receipt(
            request_path,
            record,
            self.execution_result(),
            executed_at="2026-08-30T00:05:01+09:00",
            secret_path=self.secret,
        )
        del first, second, receipt

        # A fresh process needs only the canonical request and durable sidecars.
        fresh_record = json.loads(request_path.read_text())
        factual = load_durable_successful_assignment_execution(
            request_path, fresh_record, secret_path=self.secret
        )
        self.assertEqual(
            factual["execution_result"]["cleaning_assignment"], "ACCEPTED"
        )
        self.assertTrue(
            factual["pre_effect_operation_identity"].startswith(
                "CLEANING_ASSIGNMENT_EFFECT:v1:"
            )
        )

    def test_same_identity_different_semantics_never_overwrites_provenance(self):
        root = Path(self.tmp.name)
        request_path = root / "requests" / "action-A.json"
        request_path.parent.mkdir()
        record = self.approved_record()
        request_path.write_text(json.dumps(record))
        persist_assignment_execution_intent(
            request_path, record, "approve", secret_path=self.secret
        )
        persist_successful_assignment_execution_receipt(
            request_path,
            record,
            self.execution_result(),
            executed_at="2026-08-30T00:05:01+09:00",
            secret_path=self.secret,
        )

        changed = self.execution_result()
        changed["projection_version"] = 8
        with self.assertRaisesRegex(
            AssignmentExecutionReceiptError, "overwrite rejected"
        ):
            persist_successful_assignment_execution_receipt(
                request_path,
                record,
                changed,
                executed_at="2026-08-30T00:05:01+09:00",
                secret_path=self.secret,
            )
        factual = load_durable_successful_assignment_execution(
            request_path, record, secret_path=self.secret
        )
        self.assertEqual(factual["execution_result"]["projection_version"], 7)

    def test_durable_receipt_tamper_fails_closed(self):
        root = Path(self.tmp.name)
        request_path = root / "requests" / "action-A.json"
        request_path.parent.mkdir()
        record = self.approved_record()
        request_path.write_text(json.dumps(record))
        persist_assignment_execution_intent(
            request_path, record, "approve", secret_path=self.secret
        )
        persist_successful_assignment_execution_receipt(
            request_path,
            record,
            self.execution_result(),
            executed_at="2026-08-30T00:05:01+09:00",
            secret_path=self.secret,
        )
        receipt_path = (
            request_path.parent
            / ".assignment-execution-provenance"
            / "action-A.success.json"
        )
        tampered = json.loads(receipt_path.read_text())
        tampered["assignment_execution_receipt"]["execution_result"][
            "projection_version"
        ] = 999
        receipt_path.write_text(json.dumps(tampered))

        with self.assertRaises(AssignmentExecutionReceiptError):
            load_durable_successful_assignment_execution(
                request_path, record, secret_path=self.secret
            )


if __name__ == "__main__":
    unittest.main()
