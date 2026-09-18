import hashlib
import hmac
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from gmail_ingest import booking_bridge, lifecycle_actions
import send_door_code_prompt as door_prompt
from telegram_approval import (
    bot_service,
    cleaning_assignment,
    cleaning_completion,
    cleaning_completion_evidence,
    cleaning_operations,
    lifecycle_requests,
)


class TimeoutAfterApplyRouter:
    def __init__(self):
        self.calls = 0

    def send_message(self, *_args, **_kwargs):
        self.calls += 1
        raise TimeoutError("timeout-after-apply")


class PostExternalAuthorityReassertionTests(unittest.TestCase):
    @staticmethod
    def booking_event():
        return {
            "event_type": "BOOKING_CONFIRMED",
            "source_message_hash": "a" * 64,
            "listing_id": "listing-1",
            "check_in": "2026-10-01",
            "check_out": "2026-10-03",
            "adults": 2,
            "nights": 2,
            "currency": "USD",
            "guest_total": "300.00",
            "host_payout": "250.00",
        }

    @staticmethod
    def booking_mapping():
        return {
            "nickname": "TEST",
            "address": "test-address",
            "listing_title": "Synthetic listing",
            "rental_unit_page_id": "rental",
            "property_page_id": "property",
            "door_access_point_id": "door",
            "access_point_ids": ["door"],
            "smart_doorlock": True,
            "door_operation_mode": "OWNER_REMOTE",
            "cleaning_fee_krw": 1,
            "cleaning_checklist_version": "TEST.v1",
            "operator_party_page_id": "operator",
            "calendar_source_page_id": "calendar-source",
            "reservation_calendar_code": "CAL.TEST",
            "cleaning_calendar_id": "calendar-id",
            "check_in_time": "15:00",
            "check_out_time": "11:00",
            "cleaning_end_time": "15:00",
        }

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w01_timeout_after_apply_blocks_reconciliation_write(self):
        event = self.booking_event()
        mapping = self.booking_mapping()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.json"
            initial = {"status": "PENDING_NOTION_READ", "source_message_hash": event["source_message_hash"]}
            path.write_text(json.dumps(initial))

            def timeout_after_apply(*_args, **_kwargs):
                booking_bridge.assert_current_production_writer()
                raise booking_bridge.ExternalEffectUncertain("NOTION_MUTATION_UNCERTAIN")

            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE1", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[]), \
                 patch.object(booking_bridge, "create_reservation", side_effect=timeout_after_apply), \
                 patch.object(booking_bridge, "_fresh_ambiguity_readback", return_value={"performed": True}), \
                 patch.object(
                     booking_bridge,
                     "assert_current_production_writer",
                     side_effect=[{"status": "MATCH"}, RuntimeError("STALE_FENCE")],
                 ), \
                 patch.object(booking_bridge, "atomic_private_json", wraps=booking_bridge.atomic_private_json) as durable:
                with self.assertRaisesRegex(RuntimeError, "STALE_FENCE"):
                    booking_bridge.process_one(path, {"listing-1": mapping})

            self.assertEqual(durable.call_count, 0)
            self.assertEqual(json.loads(path.read_text()), initial)

            reservation = {"id": "reservation-page", "properties": {}}
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE1", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[reservation]), \
                 patch.object(booking_bridge, "create_reservation") as blind_resend, \
                 patch.object(booking_bridge, "create_cleaning", return_value=("cleaning-page", "cleaning-key")), \
                 patch.object(booking_bridge, "ensure_calendar_event", return_value={"id": "event-1"}), \
                 patch.object(booking_bridge, "finalize_cleaning"), \
                 patch.object(booking_bridge, "_notion", return_value={"last_edited_time": "2026-08-02T05:00:00.000Z"}), \
                 patch.object(booking_bridge, "cleaning_assignment_request_exists", return_value=True), \
                 patch.object(booking_bridge, "telegram_request_exists", return_value=True):
                outcome = booking_bridge.process_one(path, {"listing-1": mapping})
            self.assertEqual(outcome, "completed")
            blind_resend.assert_not_called()

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w01_assignment_timeout_retains_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path = root / "operator.json"
            operator_path.write_text(json.dumps({"telegram_user_id": 1, "telegram_chat_id": 1}))
            router = TimeoutAfterApplyRouter()
            kwargs = dict(
                cleaning_page_id="cleaning-1",
                cleaning_name="cleaning",
                property_nickname="TEST",
                address="address",
                start_at="2026-10-03T11:00:00+09:00",
                end_at="2026-10-03T15:00:00+09:00",
                cleaning_fee_krw=1,
                expected_last_edited_time="2026-08-02T05:00:00.000Z",
                candidates=[{"telegram_user_id": 1, "telegram_chat_id": 1, "party_page_id": "party"}],
                observers=[],
                test_mode=False,
            )
            with patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "OPERATOR_PATH", operator_path), \
                 patch.object(cleaning_assignment, "secret", return_value=b"secret"), \
                 patch.object(cleaning_assignment, "signature", return_value="sig"), \
                 patch.object(cleaning_assignment, "cleaner_outbound_router", return_value=router), \
                 patch.object(
                     cleaning_assignment,
                     "assert_current_production_writer",
                     side_effect=[{"status": "MATCH"}, RuntimeError("STALE_FENCE")],
                 ), \
                 patch.object(cleaning_assignment, "atomic_private", wraps=cleaning_assignment.atomic_private) as durable:
                with self.assertRaisesRegex(RuntimeError, "STALE_FENCE"):
                    cleaning_assignment.send_assignment(**kwargs)
            self.assertEqual(router.calls, 1)
            self.assertEqual(durable.call_count, 1)
            record = json.loads(next(request_dir.glob("*.json")).read_text())
            self.assertEqual(record["status"], "PENDING")
            retry_router = Mock()
            with patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_assignment, "cleaner_outbound_router", return_value=retry_router):
                replay = cleaning_assignment.send_assignment(**kwargs)
            self.assertEqual(replay["reason"], "PENDING_REQUEST_EXISTS")
            retry_router.send_message.assert_not_called()

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w01_prompt_timeout_retains_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path = root / "operator.json"
            operator_path.write_text(json.dumps({"telegram_user_id": 1, "telegram_chat_id": 1}))
            router = TimeoutAfterApplyRouter()
            kwargs = dict(
                reservation_ref="ABC123",
                reservation_page_id="reservation-page",
                access_point_ref="door",
                smart_doorlock=False,
                base_cleaner_message="base",
                property_nickname="TEST",
                check_in="2026-10-01 15:00",
                check_out="2026-10-03 11:00",
                guest_count="2",
                currency="USD",
                guest_total="300.00",
                host_payout="250.00",
                test_mode=False,
            )
            with patch.object(door_prompt, "REQUEST_DIR", request_dir), \
                 patch.object(door_prompt, "OPERATOR_PATH", operator_path), \
                 patch.object(door_prompt, "input_prompt", return_value="prompt"), \
                 patch.object(door_prompt, "ops_outbound_router", return_value=router), \
                 patch.object(
                     door_prompt,
                     "assert_current_production_writer",
                     side_effect=[{"status": "MATCH"}, RuntimeError("STALE_FENCE")],
                 ), \
                 patch.object(door_prompt, "atomic_private", wraps=door_prompt.atomic_private) as durable:
                with self.assertRaisesRegex(RuntimeError, "STALE_FENCE"):
                    door_prompt.send_prompt(**kwargs)
            self.assertEqual(router.calls, 1)
            self.assertEqual(durable.call_count, 1)
            record = json.loads(next(request_dir.glob("*.json")).read_text())
            self.assertEqual(record["status"], "PENDING_DELIVERY")
            self.assertIsNone(record["prompt_message_id"])
            retry_router = Mock()
            with patch.object(door_prompt, "REQUEST_DIR", request_dir), \
                 patch.object(door_prompt, "OPERATOR_PATH", operator_path), \
                 patch.object(door_prompt, "ops_outbound_router", return_value=retry_router):
                replay = door_prompt.send_prompt(**kwargs)
            self.assertEqual(replay["reason"], "PENDING_REQUEST_EXISTS")
            retry_router.send_message.assert_not_called()

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w01_change_notice_timeout_retains_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            notice_dir = Path(tmp) / "notices"
            notice_dir.mkdir()
            send = Mock(side_effect=TimeoutError("timeout-after-apply"))
            with patch.object(lifecycle_requests, "NOTICE_DIR", notice_dir), \
                 patch.object(lifecycle_requests, "_send_operator", send), \
                 patch.object(
                     lifecycle_requests,
                     "assert_current_production_writer",
                     side_effect=[{"status": "MATCH"}, {"status": "MATCH"}, RuntimeError("STALE_FENCE")],
                 ), \
                 patch.object(lifecycle_requests, "atomic_private", wraps=lifecycle_requests.atomic_private) as durable:
                with self.assertRaisesRegex(RuntimeError, "STALE_FENCE"):
                    lifecycle_requests.send_change_applied(
                        source_message_hash="b" * 64,
                        reservation_code="ABC123",
                        reservation_page_id="reservation-page",
                        change_summary="adults 2 -> 3",
                    )
            self.assertEqual(send.call_count, 1)
            self.assertEqual(durable.call_count, 1)
            path = next(notice_dir.glob("*.json"))
            record = json.loads(path.read_text())
            self.assertEqual(record["status"], "PENDING_DELIVERY")
            self.assertIsNone(record["telegram_message_id"])

            retry_send = Mock()
            with patch.object(lifecycle_requests, "NOTICE_DIR", notice_dir), \
                 patch.object(lifecycle_requests, "_send_operator", retry_send):
                with self.assertRaisesRegex(RuntimeError, "LIFECYCLE_NOTICE_RECONCILIATION_REQUIRED"):
                    lifecycle_requests.send_change_applied(
                        source_message_hash="b" * 64,
                        reservation_code="ABC123",
                        reservation_page_id="reservation-page",
                        change_summary="adults 2 -> 3",
                    )
            retry_send.assert_not_called()

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w01_cancellation_timeout_retains_action_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path = root / "operator.json"
            operator_path.write_text(json.dumps({"telegram_user_id": 1, "telegram_chat_id": 1}))
            send = Mock(side_effect=TimeoutError("timeout-after-apply"))
            kwargs = dict(
                source_message_hash="c" * 64,
                reservation_code="ABC123",
                reservation_page_id="reservation-page",
                cleaning_page_ids=["cleaning-page"],
                cleaning_calendar_id="calendar-id",
                calendar_event_ids=["event-1"],
                summary={"nickname": "TEST", "check_in": "2026-10-01", "check_out": "2026-10-03", "guests": "2", "amount": "USD 300"},
                cancellation_received_at="2026-08-29T00:00:00+00:00",
            )
            with patch.object(lifecycle_requests, "REQUEST_DIR", request_dir), \
                 patch.object(lifecycle_requests, "OPERATOR_PATH", operator_path), \
                 patch.object(lifecycle_requests, "secret", return_value=b"secret"), \
                 patch.object(lifecycle_requests, "signature", return_value="sig"), \
                 patch.object(lifecycle_requests, "_send_operator", send), \
                 patch.object(
                     lifecycle_requests,
                     "assert_current_production_writer",
                     side_effect=[{"status": "MATCH"}, {"status": "MATCH"}, RuntimeError("STALE_FENCE")],
                 ), \
                 patch.object(lifecycle_requests, "atomic_private", wraps=lifecycle_requests.atomic_private) as durable:
                with self.assertRaisesRegex(RuntimeError, "STALE_FENCE"):
                    lifecycle_requests.send_cancellation_approval(**kwargs)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(durable.call_count, 1)
            path = next(request_dir.glob("*.json"))
            record = json.loads(path.read_text())
            self.assertEqual(record["status"], "PENDING")
            action_id = record["action_id"]

            retry_send = Mock()
            with patch.object(lifecycle_requests, "REQUEST_DIR", request_dir), \
                 patch.object(lifecycle_requests, "_send_operator", retry_send):
                replay = lifecycle_requests.send_cancellation_approval(**kwargs)
            self.assertEqual(replay["reason"], "ALREADY_RECORDED")
            self.assertEqual(replay["action_id"], action_id)
            retry_send.assert_not_called()

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w04_timeout_after_send_blocks_result_write_and_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_dir = Path(tmp) / "requests"
            request_dir.mkdir()
            router = TimeoutAfterApplyRouter()
            kwargs = dict(
                stage="DAY_CONFIRM",
                cleaning_page_id="cleaning-1",
                property_nickname="TEST",
                address="address",
                start_at="2026-10-03T11:00:00+09:00",
                end_at="2026-10-03T15:00:00+09:00",
                candidate={"telegram_user_id": 1, "telegram_chat_id": 1, "party_page_id": "party"},
                test_mode=False,
                expected_last_edited_time="2026-08-02T05:00:00.000Z",
                parent_action_id="parent-1",
            )
            with patch.object(cleaning_operations, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_operations, "_keyboard", return_value="{}"), \
                 patch.object(cleaning_operations, "cleaner_outbound_router", return_value=router), \
                 patch.object(
                     cleaning_operations,
                     "assert_current_production_writer",
                     side_effect=[{"status": "MATCH"}, RuntimeError("STALE_FENCE")],
                 ), \
                 patch.object(cleaning_operations, "atomic_private", wraps=cleaning_operations.atomic_private) as durable:
                with self.assertRaisesRegex(RuntimeError, "STALE_FENCE"):
                    cleaning_operations.send_operation_stage(**kwargs)
            self.assertEqual(router.calls, 1)
            self.assertEqual(durable.call_count, 1)
            record = json.loads(next(request_dir.glob("*.json")).read_text())
            self.assertEqual(record["status"], "PENDING")
            retry_router = Mock()
            with patch.object(cleaning_operations, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_operations, "cleaner_outbound_router", return_value=retry_router):
                replay = cleaning_operations.send_operation_stage(**kwargs)
            self.assertEqual(replay["reason"], "PENDING_REQUEST_EXISTS")
            retry_router.send_message.assert_not_called()

    def _exercise_completion_send_authority_loss(self, kind):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path = root / "operator.json"
            operator_path.write_text(json.dumps({"telegram_user_id": 1, "telegram_chat_id": 1}))
            router = TimeoutAfterApplyRouter()
            common_patches = [
                patch.object(cleaning_completion, "REQUEST_DIR", request_dir),
                patch.object(cleaning_completion, "OPERATOR_PATH", operator_path),
                patch.object(cleaning_completion, "_keyboard", return_value="{}"),
                patch.object(
                    cleaning_completion,
                    "assert_current_production_writer",
                    side_effect=[{"status": "MATCH"}, RuntimeError("STALE_FENCE")],
                ),
                patch.object(cleaning_completion, "atomic_private", wraps=cleaning_completion.atomic_private),
            ]
            if kind == "completion":
                common_patches.append(patch.object(cleaning_completion, "cleaner_outbound_router", return_value=router))
                call = lambda: cleaning_completion.send_completion_request(
                    cleaning_page_id="cleaning-1",
                    property_nickname="TEST",
                    address="address",
                    cleaning_date="2026-10-03",
                    cleaning_fee_krw=1,
                    candidate={"telegram_user_id": 1, "telegram_chat_id": 1, "party_page_id": "party"},
                    test_mode=False,
                    expected_last_edited_time="2026-08-02T05:00:00.000Z",
                )
                retry_call = call
                router_patch = lambda retry: patch.object(cleaning_completion, "cleaner_outbound_router", return_value=retry)
            else:
                common_patches.append(patch.object(cleaning_completion, "ops_outbound_router", return_value=router))
                parent = {
                    "action_id": "parent-1",
                    "cleaning_page_id": "cleaning-1",
                    "property_nickname": "TEST",
                    "cleaning_date": "2026-10-03",
                    "cleaning_fee_krw": 1,
                    "candidate_party_page_id": "party",
                    "candidate_label": "Cleaner",
                    "test_mode": False,
                    "execution_result": {
                        "completion_folder_url": "https://example.invalid/folder",
                        "photo_count": 3,
                        "next_expected_last_edited_time": "2026-08-02T05:00:00.000Z",
                    },
                }
                if kind == "operator":
                    call = lambda: cleaning_completion.send_operator_review(parent)
                else:
                    call = lambda: cleaning_completion.send_transfer_confirmation(parent)
                retry_call = call
                router_patch = lambda retry: patch.object(cleaning_completion, "ops_outbound_router", return_value=retry)

            with ExitStack() as stack:
                for item in common_patches[:-2]:
                    stack.enter_context(item)
                durable = stack.enter_context(common_patches[-2])
                stack.enter_context(common_patches[-1])
                with self.assertRaisesRegex(RuntimeError, "STALE_FENCE"):
                    call()

            self.assertEqual(router.calls, 1)
            self.assertEqual(durable.call_count, 1)
            record = json.loads(next(request_dir.glob("*.json")).read_text())
            self.assertEqual(record["status"], "PENDING")
            retry_router = Mock()
            with patch.object(cleaning_completion, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_completion, "OPERATOR_PATH", operator_path), \
                 router_patch(retry_router):
                replay = retry_call()
            self.assertEqual(replay["reason"], "PENDING_REQUEST_EXISTS")
            retry_router.send_message.assert_not_called()

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w05_completion_notification(self):
        self._exercise_completion_send_authority_loss("completion")

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w05_operator_notification(self):
        self._exercise_completion_send_authority_loss("operator")

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w05_payment_delivery(self):
        self._exercise_completion_send_authority_loss("payment")

    @staticmethod
    def _completion_parent_fixture(root: Path):
        request_dir = root / "requests"
        request_dir.mkdir()
        action_secret = root / "action-secret"
        action_secret.write_text("secret")
        action_id = "completion-parent-post-external"
        record = {
            "schema_version": 1,
            "action_id": action_id,
            "action_type": "CLEANING_COMPLETION_SUBMISSION",
            "cleaning_page_id": "cleaning-page",
            "candidate_user_id": 101,
            "candidate_chat_id": 202,
            "status": "PENDING",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "consumed": False,
            "test_mode": False,
            "execute_on_reject": False,
            "external_writes_on_approval": 1,
            "external_writes_on_reject": 0,
        }
        path = request_dir / f"{action_id}.json"
        path.write_text(json.dumps(record))
        supplied = hmac.new(
            b"secret", f"{action_id}:approve".encode(), hashlib.sha256
        ).hexdigest()[:16]
        update = {
            "callback_query": {
                "id": "callback-parent-post-external",
                "data": f"a:{action_id}:approve:{supplied}",
                "from": {"id": 101},
                "message": {"chat": {"id": 202}},
            }
        }
        return request_dir, action_secret, path, update

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w05_parent_result_stale_after_factual_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_dir, action_secret, path, update = self._completion_parent_fixture(Path(tmp))
            authority = {"current": True}
            factual_effects = []
            assert_attempts = []
            durable_snapshots = []
            original_atomic = bot_service.atomic_private

            def factual_success(_record, _decision):
                self.assertTrue(authority["current"])
                factual_effects.append("SUCCEEDED")
                authority["current"] = False
                return {"cleaning_completion": "PHOTOS_SUBMITTED_REVIEW_REQUIRED"}

            def fresh_assert():
                assert_attempts.append("ATTEMPTED")
                if not authority["current"]:
                    raise RuntimeError("STALE_FENCE")
                return {"status": "MATCH"}

            def durable_write(write_path, value):
                durable_snapshots.append(json.loads(json.dumps(value)))
                return original_atomic(write_path, value)

            with patch.object(cleaning_completion_evidence, "session_ready", return_value=True), \
                 patch.object(lifecycle_actions, "execute_lifecycle_action", side_effect=factual_success) as execute, \
                 patch.object(bot_service, "assert_current_production_writer", side_effect=fresh_assert), \
                 patch.object(bot_service, "atomic_private", side_effect=durable_write):
                with self.assertRaisesRegex(RuntimeError, "STALE_FENCE"):
                    bot_service.callback(
                        update,
                        "token",
                        request_dir=request_dir,
                        action_secret_path=action_secret,
                        request_api=lambda *_args, **_kwargs: {"ok": True},
                    )

                replay = bot_service.callback(
                    update,
                    "token",
                    request_dir=request_dir,
                    action_secret_path=action_secret,
                    request_api=lambda *_args, **_kwargs: {"ok": True},
                )

            saved = json.loads(path.read_text())
            post_external_durable = [
                snapshot for snapshot in durable_snapshots
                if snapshot.get("execution_result") is not None or snapshot.get("status") == "EXECUTED"
            ]
            self.assertEqual(factual_effects, ["SUCCEEDED"])
            self.assertEqual(assert_attempts, ["ATTEMPTED"])
            self.assertEqual(post_external_durable, [])
            self.assertEqual(saved["status"], "APPROVED_PRODUCTION")
            self.assertTrue(saved["consumed"])
            self.assertNotIn("execution_result", saved)
            self.assertNotIn("executed_at", saved)
            self.assertNotIn("execution_error_type", saved)
            self.assertEqual(replay, "ignored")
            self.assertEqual(execute.call_count, 1)

    def test_POST_EXTERNAL_AUTHORITY_REASSERTION_w05_parent_result_current_persists_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_dir, action_secret, path, update = self._completion_parent_fixture(Path(tmp))
            assert_attempts = []
            durable_snapshots = []
            original_atomic = bot_service.atomic_private

            def durable_write(write_path, value):
                durable_snapshots.append(json.loads(json.dumps(value)))
                return original_atomic(write_path, value)

            with patch.object(cleaning_completion_evidence, "session_ready", return_value=True), \
                 patch.object(cleaning_completion_evidence, "finish_session"), \
                 patch.object(
                     lifecycle_actions,
                     "execute_lifecycle_action",
                     return_value={"cleaning_completion": "PHOTOS_SUBMITTED_REVIEW_REQUIRED"},
                 ) as execute, \
                 patch.object(
                     bot_service,
                     "assert_current_production_writer",
                     side_effect=lambda: assert_attempts.append("ATTEMPTED") or {"status": "MATCH"},
                 ), \
                 patch.object(cleaning_completion, "send_operator_review", return_value={"action_id": "review-1"}), \
                 patch.object(bot_service, "atomic_private", side_effect=durable_write):
                result = bot_service.callback(
                    update,
                    "token",
                    request_dir=request_dir,
                    action_secret_path=action_secret,
                    request_api=lambda *_args, **_kwargs: {"ok": True},
                )

            saved = json.loads(path.read_text())
            parent_result_transitions = [
                snapshot for snapshot in durable_snapshots
                if snapshot.get("status") == "EXECUTED"
                and snapshot.get("execution_result") is not None
                and "next_action" not in snapshot
            ]
            self.assertEqual(result, "approved")
            self.assertEqual(execute.call_count, 1)
            self.assertGreaterEqual(len(assert_attempts), 1)
            self.assertEqual(len(parent_result_transitions), 1)
            self.assertEqual(saved["status"], "EXECUTED")
            self.assertEqual(
                saved["execution_result"]["cleaning_completion"],
                "PHOTOS_SUBMITTED_REVIEW_REQUIRED",
            )
            self.assertEqual(saved["external_writes_executed"], 1)


if __name__ == "__main__":
    unittest.main()
