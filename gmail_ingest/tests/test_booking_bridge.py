import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gmail_ingest import booking_bridge, poll_once


class BookingBridgeTests(unittest.TestCase):
    def test_shared_google_token_preserves_calendar_scope(self):
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", poll_once.SCOPES)
        self.assertIn("https://www.googleapis.com/auth/calendar", poll_once.SCOPES)

    def test_create_cleaning_starts_without_assignee_and_preserves_core_relations(self):
        event = {
            "check_out": "2026-10-03",
            "source_message_hash": "a" * 64,
        }
        mapping = {
            "nickname": "TEST",
            "rental_unit_page_id": "rental",
            "property_page_id": "property",
            "access_point_ids": ["door-1", "door-2"],
            "operator_party_page_id": "legacy-test-operator",
            "door_operation_mode": "OWNER_REMOTE",
            "cleaning_fee_krw": 45000,
            "cleaning_checklist_version": "TEST.v1",
            "check_out_time": "11:00",
            "cleaning_end_time": "15:00",
        }
        with patch.object(booking_bridge, "query_exact", return_value=[]), \
             patch.object(booking_bridge, "_notion", return_value={"id": "cleaning-page"}) as notion:
            cleaning_page_id, _ = booking_bridge.create_cleaning(event, "reservation-page", mapping)

        self.assertEqual(cleaning_page_id, "cleaning-page")
        notion.assert_called_once()
        method, path, body = notion.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/pages"))
        properties = body["properties"]
        self.assertNotIn("담당 참여자/업체", properties)
        self.assertEqual(properties["관련 Reservation"], booking_bridge._relation("reservation-page"))
        self.assertEqual(properties["연결 운영상품"], booking_bridge._relation("rental"))
        self.assertEqual(properties["연결 집"], booking_bridge._relation("property"))
        self.assertEqual(properties["Access Point"], booking_bridge._relation("door-1", "door-2"))

    def test_finalize_cleaning_does_not_reintroduce_assignee(self):
        with patch.object(booking_bridge, "_notion") as notion:
            booking_bridge.finalize_cleaning(
                "cleaning-page",
                {"id": "event-1", "iCalUID": "ical-1"},
            )

        notion.assert_called_once()
        method, path, body = notion.call_args.args
        self.assertEqual((method, path), ("PATCH", "/v1/pages/cleaning-page"))
        self.assertNotIn("담당 참여자/업체", body["properties"])

    def test_new_event_completes_all_downstream_steps(self):
        event = {
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
        mapping = {
            "nickname": "TEST",
            "address": "서울특별시 테스트구 테스트로 1",
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
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            path.write_text(json.dumps({"status": "PENDING_NOTION_READ", "source_message_hash": "a" * 64}))
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE1", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[]), \
                 patch.object(booking_bridge, "create_reservation", return_value="reservation-page"), \
                 patch.object(booking_bridge, "create_cleaning", return_value=("cleaning-page", "cleaning-key")), \
                 patch.object(booking_bridge, "ensure_calendar_event", return_value={"id": "event-1"}), \
                 patch.object(booking_bridge, "finalize_cleaning"), \
                 patch.object(booking_bridge, "cleaning_assignment_request_exists", return_value=False), \
                 patch.object(booking_bridge, "_notion", return_value={"last_edited_time": "2026-08-02T05:00:00.000Z"}), \
                 patch.object(booking_bridge, "assignment_targets", return_value=([{"telegram_user_id": 1, "telegram_chat_id": 1}], [])), \
                 patch.object(booking_bridge, "send_assignment", return_value={"action_id": "assignment-1"}) as send_assignment, \
                 patch.object(booking_bridge, "telegram_request_exists", return_value=False), \
                 patch.object(booking_bridge, "send_prompt", return_value={"request_id": "request-1"}):
                outcome = booking_bridge.process_one(path, {"listing-1": mapping})

            saved = json.loads(path.read_text())
            self.assertEqual(outcome, "completed")
            self.assertEqual(saved["status"], "COMPLETE")
            self.assertEqual(saved["classification"], "NEW_COMPLETED")
            self.assertEqual(saved["notion_reservation_page_id"], "reservation-page")
            self.assertEqual(saved["notion_cleaning_page_id"], "cleaning-page")
            self.assertEqual(saved["calendar_event_id"], "event-1")
            self.assertEqual(saved["cleaning_assignment_action_id"], "assignment-1")
            self.assertEqual(send_assignment.call_args.kwargs["default_candidate_party_page_id"], "operator")
            self.assertEqual(saved["door_code_request_id"], "request-1")

    def test_core_replay_completes_core_without_assignment_or_telegram_side_effects(self):
        event = {
            "event_type": "BOOKING_CONFIRMED",
            "source_message_hash": "f" * 64,
            "listing_id": "listing-1",
            "check_in": "2026-10-01",
            "check_out": "2026-10-03",
            "adults": 2,
            "nights": 2,
            "currency": "USD",
            "guest_total": "300.00",
            "host_payout": "250.00",
        }
        mapping = {
            "nickname": "TEST",
            "address": "서울특별시 테스트구 테스트로 1",
            "door_access_point_id": "door",
            "smart_doorlock": True,
            "cleaning_fee_krw": 1,
            "operator_party_page_id": "operator",
            "check_in_time": "15:00",
            "check_out_time": "11:00",
            "cleaning_end_time": "15:00",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            path.write_text(json.dumps({"status": "RETRY_REQUIRED", "source_message_hash": "f" * 64}))
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE5", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[]), \
                 patch.object(booking_bridge, "create_reservation", return_value="reservation-page") as create_reservation, \
                 patch.object(booking_bridge, "create_cleaning", return_value=("cleaning-page", "cleaning-key")) as create_cleaning, \
                 patch.object(booking_bridge, "ensure_calendar_event", return_value={"id": "event-1"}) as ensure_calendar, \
                 patch.object(booking_bridge, "finalize_cleaning") as finalize_cleaning, \
                 patch.object(booking_bridge, "cleaning_assignment_request_exists") as assignment_exists, \
                 patch.object(booking_bridge, "_notion") as notion, \
                 patch.object(booking_bridge, "assignment_targets") as targets, \
                 patch.object(booking_bridge, "send_assignment") as send_assignment, \
                 patch.object(booking_bridge, "telegram_request_exists") as telegram_exists, \
                 patch.object(booking_bridge, "send_prompt") as send_prompt:
                outcome = booking_bridge.process_one(
                    path, {"listing-1": mapping}, mode=booking_bridge.PROCESS_MODE_CORE_REPLAY
                )

            saved = json.loads(path.read_text())
            self.assertEqual(outcome, "completed")
            self.assertEqual(saved["status"], "COMPLETE")
            self.assertEqual(saved["classification"], "NEW_COMPLETED")
            self.assertNotIn("cleaning_assignment_action_id", saved)
            self.assertNotIn("door_code_request_id", saved)
            create_reservation.assert_called_once()
            create_cleaning.assert_called_once()
            ensure_calendar.assert_called_once()
            finalize_cleaning.assert_called_once()
            assignment_exists.assert_not_called()
            notion.assert_not_called()
            targets.assert_not_called()
            send_assignment.assert_not_called()
            telegram_exists.assert_not_called()
            send_prompt.assert_not_called()

    def test_core_replay_preserves_existing_core_idempotency_without_dispatch(self):
        event = {
            "event_type": "BOOKING_CONFIRMED",
            "source_message_hash": "g" * 64,
            "listing_id": "listing-1",
            "check_in": "2026-10-01",
            "check_out": "2026-10-03",
            "adults": 2,
            "nights": 2,
            "currency": "USD",
            "guest_total": "300.00",
            "host_payout": "250.00",
        }
        mapping = {
            "nickname": "TEST",
            "address": "서울특별시 테스트구 테스트로 1",
            "door_access_point_id": "door",
            "smart_doorlock": True,
            "cleaning_fee_krw": 1,
            "operator_party_page_id": "operator",
            "check_in_time": "15:00",
            "check_out_time": "11:00",
            "cleaning_end_time": "15:00",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            path.write_text(json.dumps({"status": "RETRY_REQUIRED", "source_message_hash": "g" * 64}))
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE6", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[{"id": "reservation-page"}]), \
                 patch.object(booking_bridge, "create_reservation") as create_reservation, \
                 patch.object(booking_bridge, "create_cleaning", return_value=("cleaning-page", "cleaning-key")) as create_cleaning, \
                 patch.object(booking_bridge, "ensure_calendar_event", return_value={"id": "event-1"}) as ensure_calendar, \
                 patch.object(booking_bridge, "finalize_cleaning") as finalize_cleaning, \
                 patch.object(booking_bridge, "send_assignment") as send_assignment, \
                 patch.object(booking_bridge, "send_prompt") as send_prompt:
                outcome = booking_bridge.process_one(
                    path, {"listing-1": mapping}, mode=booking_bridge.PROCESS_MODE_CORE_REPLAY
                )

            saved = json.loads(path.read_text())
            self.assertEqual(outcome, "completed")
            self.assertEqual(saved["classification"], "DUPLICATE_COMPLETED")
            create_reservation.assert_not_called()
            create_cleaning.assert_called_once()
            ensure_calendar.assert_called_once()
            finalize_cleaning.assert_called_once()
            send_assignment.assert_not_called()
            send_prompt.assert_not_called()

    def test_core_replay_skips_non_confirmation_without_telegram(self):
        event = {
            "event_type": "BOOKING_CANCELLED",
            "source_message_hash": "h" * 64,
            "received_at": "2026-08-02T04:32:00+00:00",
        }
        original = {"status": "RETRY_REQUIRED", "source_message_hash": "h" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            path.write_text(json.dumps(original))
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE7", "received")), \
                 patch.object(booking_bridge, "process_cancellation_event") as cancellation, \
                 patch.object(booking_bridge, "send_cancellation_approval") as approval, \
                 patch.object(booking_bridge, "send_change_applied") as change_applied, \
                 patch.object(booking_bridge, "send_change_review") as change_review:
                outcome = booking_bridge.process_one(
                    path, {}, mode=booking_bridge.PROCESS_MODE_CORE_REPLAY
                )

            self.assertEqual(outcome, "skipped")
            self.assertEqual(json.loads(path.read_text()), original)
            cancellation.assert_not_called()
            approval.assert_not_called()
            change_applied.assert_not_called()
            change_review.assert_not_called()

    def test_update_event_is_locked_for_review_and_not_applied_as_guessed_values(self):
        event = {"event_type": "BOOKING_UPDATED", "source_message_hash": "b" * 64,
                 "received_at": "2026-08-02T04:31:00+00:00"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            path.write_text(json.dumps({"status": "PENDING_NOTION_READ", "source_message_hash": "b" * 64}))
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE2", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[{"id": "reservation-page"}]), \
                 patch.object(booking_bridge, "find_pending_change", return_value=[]), \
                 patch.object(booking_bridge, "append_change_review") as mark, \
                 patch.object(booking_bridge, "send_change_review", return_value={"telegram_message_id": 10}):
                outcome = booking_bridge.process_one(path, {})
            self.assertEqual(outcome, "review_required")
            self.assertEqual(json.loads(path.read_text())["classification"], "BOOKING_UPDATE_EXACT_VALUES_REQUIRED")
            mark.assert_called_once()

    def test_exact_confirmed_change_is_applied_without_guessing(self):
        event = {"event_type": "BOOKING_UPDATED", "source_message_hash": "e" * 64,
                 "received_at": "2026-08-02T04:31:00+00:00", "actor_ref_hash": "actor",
                 "listing_ref_hash": "listing"}
        pending = {"status": "PENDING_CONFIRMATION", "original_adults": 1, "requested_adults": 4}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            pending_path = Path(temporary) / "pending.json"
            path.write_text(json.dumps({"status": "PENDING_NOTION_READ", "source_message_hash": "e" * 64}))
            pending_path.write_text(json.dumps(pending))
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE4", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[{"id": "reservation-page"}]), \
                 patch.object(booking_bridge, "find_pending_change", return_value=[(pending_path, pending)]), \
                 patch.object(booking_bridge, "apply_exact_pending_change", return_value="성인 1명 → 4명") as apply, \
                 patch.object(booking_bridge, "send_change_applied", return_value={"telegram_message_id": 11}), \
                 patch.object(booking_bridge, "append_change_review") as review:
                outcome = booking_bridge.process_one(path, {})
            saved = json.loads(path.read_text())
            self.assertEqual(outcome, "completed")
            self.assertEqual(saved["classification"], "BOOKING_UPDATE_EXACT_APPLIED")
            apply.assert_called_once()
            review.assert_not_called()

    def _cancelled_pre_ingest_fixture(self, temporary):
        confirmation_hash = "1" * 64
        cancellation_hash = "2" * 64
        booking_ref_hash = "3" * 64
        confirmation_path = Path(temporary) / "confirmation.json"
        cancellation_path = Path(temporary) / "cancellation.json"
        confirmation_path.write_text(json.dumps({
            "schema_version": 2,
            "status": "RETRY_REQUIRED",
            "event_type": "BOOKING_CONFIRMED",
            "source_message_hash": confirmation_hash,
            "external_booking_ref_hash": booking_ref_hash,
            "external_writes": 0,
        }))
        cancellation_path.write_text(json.dumps({
            "schema_version": 2,
            "status": "REVIEW_REQUIRED",
            "event_type": "BOOKING_UPDATED",
            "source_message_hash": cancellation_hash,
            "external_booking_ref_hash": booking_ref_hash,
            "external_writes": 0,
            "reason": "RESERVATION_EXACT_MATCH_REQUIRED",
        }))
        events = {
            confirmation_hash: ({
                "event_type": "BOOKING_CONFIRMED",
                "source_message_hash": confirmation_hash,
            }, "HMTESTL001", "received-confirmation"),
            cancellation_hash: ({
                "event_type": "BOOKING_CANCELLED",
                "source_message_hash": cancellation_hash,
            }, "HMTESTL001", "received-cancellation"),
        }
        return confirmation_path, cancellation_path, confirmation_hash, cancellation_hash, events

    def _provider_ical_resolver(
        self, *, reservation_code="HMTESTL001", uid="synthetic-ical-uid", status="cancelled", count=1
    ):
        evidence = [{
            "reservation_code": reservation_code,
            "iCalUID": uid,
            "status": status,
            "start": {"date": "2026-12-19"},
            "end": {"date": "2026-12-27"},
            "event_id": "provider-event-id",
        } for _ in range(count)]
        return lambda _requested_code: evidence

    def _reconcile_kwargs(self, confirmation_hash, cancellation_hash):
        return {
            "reservation_code": "HMTESTL001",
            "expected_confirmation_source_hash": confirmation_hash,
            "expected_cancellation_source_hash": cancellation_hash,
            "provider_ical_uid": "synthetic-ical-uid",
            "provider_ical_status": "cancelled",
            "provider_ical_evidence_resolver": self._provider_ical_resolver(),
            "change_id": "HMM2-CANCEL-LIFECYCLE-01",
            "canonical_reservation_created": False,
            "reconciled_at": "2026-08-28T05:30:00+00:00",
        }

    def test_cancelled_pre_ingest_reconciliation_is_bounded_dry_run_idempotent_and_no_business_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            confirmation_path, cancellation_path, confirmation_hash, cancellation_hash, events = (
                self._cancelled_pre_ingest_fixture(temporary)
            )
            before_confirmation = confirmation_path.read_text()
            before_cancellation = cancellation_path.read_text()
            kwargs = self._reconcile_kwargs(confirmation_hash, cancellation_hash)

            with patch.object(booking_bridge, "fresh_source", side_effect=lambda value: events[value]), \
                 patch.object(booking_bridge, "create_reservation") as create_reservation, \
                 patch.object(booking_bridge, "create_cleaning") as create_cleaning, \
                 patch.object(booking_bridge, "ensure_calendar_event") as ensure_calendar, \
                 patch.object(booking_bridge, "finalize_cleaning") as finalize_cleaning, \
                 patch.object(booking_bridge, "assignment_targets") as assignment_targets, \
                 patch.object(booking_bridge, "send_assignment") as send_assignment, \
                 patch.object(booking_bridge, "send_prompt") as send_prompt, \
                 patch.object(booking_bridge, "send_cancellation_approval") as send_cancellation_approval, \
                 patch.object(booking_bridge, "send_change_applied") as send_change_applied, \
                 patch.object(booking_bridge, "send_change_review") as send_change_review, \
                 patch.object(booking_bridge, "_notion") as notion:
                plan = booking_bridge.reconcile_cancelled_pre_ingest(
                    confirmation_path, cancellation_path, apply=False, **kwargs
                )
                self.assertEqual(plan["outcome"], "planned")
                self.assertEqual(confirmation_path.read_text(), before_confirmation)
                self.assertEqual(cancellation_path.read_text(), before_cancellation)

                result = booking_bridge.reconcile_cancelled_pre_ingest(
                    confirmation_path, cancellation_path, apply=True, **kwargs
                )
                again = booking_bridge.reconcile_cancelled_pre_ingest(
                    confirmation_path, cancellation_path, apply=True, **kwargs
                )

            confirmation = json.loads(confirmation_path.read_text())
            cancellation = json.loads(cancellation_path.read_text())
            self.assertEqual(result["outcome"], "reconciled")
            self.assertEqual(again["outcome"], "already_reconciled")
            self.assertEqual(confirmation["status"], "REVIEW_REQUIRED")
            self.assertEqual(confirmation["reason"], booking_bridge.CANCELLED_PRE_INGEST_REASON)
            self.assertEqual(confirmation["reconciliation_reason"], "CANCELLED_PRE_INGEST")
            self.assertEqual(confirmation["reconciliation_source_message_hash"], cancellation_hash)
            self.assertEqual(confirmation["reconciliation_source_event_type"], "BOOKING_CANCELLED")
            self.assertEqual(confirmation["reconciliation_provider_ical_uid"], "synthetic-ical-uid")
            self.assertEqual(confirmation["reconciliation_provider_ical_status"], "cancelled")
            self.assertEqual(confirmation["reconciliation_change_id"], "HMM2-CANCEL-LIFECYCLE-01")
            self.assertFalse(confirmation["canonical_reservation_created"])
            self.assertEqual(confirmation["external_writes"], 0)
            self.assertEqual(cancellation["event_type"], "BOOKING_CANCELLED")
            self.assertEqual(cancellation["status"], "REVIEW_REQUIRED")
            self.assertEqual(cancellation["source_message_hash"], cancellation_hash)
            self.assertEqual(cancellation["reconciliation_previous_event_type"], "BOOKING_UPDATED")
            self.assertEqual(cancellation["external_writes"], 0)
            create_reservation.assert_not_called()
            create_cleaning.assert_not_called()
            ensure_calendar.assert_not_called()
            finalize_cleaning.assert_not_called()
            assignment_targets.assert_not_called()
            send_assignment.assert_not_called()
            send_prompt.assert_not_called()
            send_cancellation_approval.assert_not_called()
            send_change_applied.assert_not_called()
            send_change_review.assert_not_called()
            notion.assert_not_called()

    def test_cancelled_pre_ingest_wrong_nonempty_provider_uid_fails_before_first_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            confirmation_path, cancellation_path, confirmation_hash, cancellation_hash, events = (
                self._cancelled_pre_ingest_fixture(temporary)
            )
            before_confirmation = confirmation_path.read_bytes()
            before_cancellation = cancellation_path.read_bytes()
            kwargs = self._reconcile_kwargs(confirmation_hash, cancellation_hash)
            kwargs["provider_ical_uid"] = "different-nonempty-uid"

            with patch.object(booking_bridge, "fresh_source", side_effect=lambda value: events[value]), \
                 patch.object(booking_bridge, "atomic_private_json") as atomic_write:
                with self.assertRaisesRegex(ValueError, "provider iCal UID mismatch"):
                    booking_bridge.reconcile_cancelled_pre_ingest(
                        confirmation_path, cancellation_path, apply=True, **kwargs
                    )

            self.assertEqual(confirmation_path.read_bytes(), before_confirmation)
            self.assertEqual(cancellation_path.read_bytes(), before_cancellation)
            atomic_write.assert_not_called()

    def test_cancelled_pre_ingest_provider_evidence_binding_fail_closed_cases(self):
        cases = [
            (
                "different_reservation",
                self._provider_ical_resolver(reservation_code="OTHERBOOKING"),
                "provider iCal reservation code mismatch",
            ),
            (
                "zero_exact_events",
                self._provider_ical_resolver(count=0),
                "provider iCal exact match count must be one",
            ),
            (
                "multiple_exact_events",
                self._provider_ical_resolver(count=2),
                "provider iCal exact match count must be one",
            ),
            (
                "provider_status_confirmed",
                self._provider_ical_resolver(status="confirmed"),
                "provider iCal status mismatch",
            ),
            (
                "correct_uid_belongs_to_other_booking",
                self._provider_ical_resolver(reservation_code="OTHERBOOKING", uid="synthetic-ical-uid"),
                "provider iCal reservation code mismatch",
            ),
        ]
        for label, resolver, error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                confirmation_path, cancellation_path, confirmation_hash, cancellation_hash, events = (
                    self._cancelled_pre_ingest_fixture(temporary)
                )
                before_confirmation = confirmation_path.read_bytes()
                before_cancellation = cancellation_path.read_bytes()
                kwargs = self._reconcile_kwargs(confirmation_hash, cancellation_hash)
                kwargs["provider_ical_evidence_resolver"] = resolver
                with patch.object(booking_bridge, "fresh_source", side_effect=lambda value: events[value]), \
                     patch.object(booking_bridge, "atomic_private_json") as atomic_write:
                    with self.assertRaisesRegex(ValueError, error):
                        booking_bridge.reconcile_cancelled_pre_ingest(
                            confirmation_path, cancellation_path, apply=True, **kwargs
                        )
                self.assertEqual(confirmation_path.read_bytes(), before_confirmation)
                self.assertEqual(cancellation_path.read_bytes(), before_cancellation)
                atomic_write.assert_not_called()

    def test_cancelled_pre_ingest_missing_provider_uid_fails_closed_without_resolver_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            confirmation_path, cancellation_path, confirmation_hash, cancellation_hash, _events = (
                self._cancelled_pre_ingest_fixture(temporary)
            )
            before_confirmation = confirmation_path.read_bytes()
            before_cancellation = cancellation_path.read_bytes()
            kwargs = self._reconcile_kwargs(confirmation_hash, cancellation_hash)
            kwargs["provider_ical_uid"] = ""
            resolver = Mock()
            kwargs["provider_ical_evidence_resolver"] = resolver
            with self.assertRaisesRegex(ValueError, "provider iCal UID required"):
                booking_bridge.reconcile_cancelled_pre_ingest(
                    confirmation_path, cancellation_path, apply=True, **kwargs
                )
            self.assertEqual(confirmation_path.read_bytes(), before_confirmation)
            self.assertEqual(cancellation_path.read_bytes(), before_cancellation)
            resolver.assert_not_called()

    def test_cancelled_pre_ingest_reconciliation_fails_closed_on_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            confirmation_path, cancellation_path, confirmation_hash, cancellation_hash, events = (
                self._cancelled_pre_ingest_fixture(temporary)
            )
            before_confirmation = confirmation_path.read_text()
            before_cancellation = cancellation_path.read_text()
            kwargs = self._reconcile_kwargs(confirmation_hash, cancellation_hash)
            kwargs["expected_cancellation_source_hash"] = "9" * 64
            with patch.object(booking_bridge, "fresh_source") as fresh_source:
                with self.assertRaisesRegex(ValueError, "cancellation source hash mismatch"):
                    booking_bridge.reconcile_cancelled_pre_ingest(
                        confirmation_path, cancellation_path, apply=True, **kwargs
                    )
            self.assertEqual(confirmation_path.read_text(), before_confirmation)
            self.assertEqual(cancellation_path.read_text(), before_cancellation)
            fresh_source.assert_not_called()

    def test_cancelled_pre_ingest_reconciliation_conflicting_second_apply_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            confirmation_path, cancellation_path, confirmation_hash, cancellation_hash, events = (
                self._cancelled_pre_ingest_fixture(temporary)
            )
            kwargs = self._reconcile_kwargs(confirmation_hash, cancellation_hash)
            with patch.object(booking_bridge, "fresh_source", side_effect=lambda value: events[value]):
                booking_bridge.reconcile_cancelled_pre_ingest(
                    confirmation_path, cancellation_path, apply=True, **kwargs
                )
                conflicting = dict(kwargs)
                conflicting["provider_ical_uid"] = "different-ical-uid"
                with self.assertRaisesRegex(ValueError, "provider iCal UID mismatch"):
                    booking_bridge.reconcile_cancelled_pre_ingest(
                        confirmation_path, cancellation_path, apply=True, **conflicting
                    )

    def test_cancelled_pre_ingest_failure_order_leaves_confirmation_non_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            confirmation_path, cancellation_path, confirmation_hash, cancellation_hash, events = (
                self._cancelled_pre_ingest_fixture(temporary)
            )
            kwargs = self._reconcile_kwargs(confirmation_hash, cancellation_hash)
            real_atomic = booking_bridge.atomic_private_json
            calls = []

            def fail_second(path, value):
                calls.append(Path(path))
                if len(calls) == 2:
                    raise OSError("synthetic second write failure")
                return real_atomic(path, value)

            with patch.object(booking_bridge, "fresh_source", side_effect=lambda value: events[value]), \
                 patch.object(booking_bridge, "atomic_private_json", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "synthetic second write failure"):
                    booking_bridge.reconcile_cancelled_pre_ingest(
                        confirmation_path, cancellation_path, apply=True, **kwargs
                    )

            confirmation = json.loads(confirmation_path.read_text())
            cancellation = json.loads(cancellation_path.read_text())
            self.assertEqual(calls[0], confirmation_path)
            self.assertEqual(calls[1], cancellation_path)
            self.assertEqual(confirmation["status"], "REVIEW_REQUIRED")
            self.assertEqual(confirmation["reconciliation_reason"], "CANCELLED_PRE_INGEST")
            self.assertEqual(cancellation["event_type"], "BOOKING_UPDATED")
            self.assertEqual(cancellation["status"], "REVIEW_REQUIRED")

    def test_review_required_reconciled_confirmation_is_not_process_pending_eligible(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue_dir = Path(temporary) / "queue"
            queue_dir.mkdir()
            queue_path = queue_dir / "confirmation.json"
            queue_path.write_text(json.dumps({
                "status": "REVIEW_REQUIRED",
                "event_type": "BOOKING_CONFIRMED",
                "source_message_hash": "4" * 64,
                "external_writes": 0,
                "reconciliation_reason": "CANCELLED_PRE_INGEST",
            }))
            mappings_path = Path(temporary) / "mappings.json"
            mappings_path.write_text(json.dumps({"listings": {}}))
            with patch.object(booking_bridge, "QUEUE_DIR", queue_dir), \
                 patch.object(booking_bridge, "MAPPINGS_PATH", mappings_path), \
                 patch.object(booking_bridge, "fresh_source") as fresh_source:
                counts = booking_bridge.process_pending()
            self.assertEqual(counts["skipped"], 1)
            self.assertEqual(counts["completed"], 0)
            self.assertEqual(counts["retry_required"], 0)
            fresh_source.assert_not_called()

    def test_cancellation_waits_for_telegram_approval(self):
        event = {"event_type": "BOOKING_CANCELLED", "source_message_hash": "c" * 64,
                 "received_at": "2026-08-02T04:32:00+00:00"}
        reservation = {"id": "reservation-page", "properties": {}}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            path.write_text(json.dumps({"status": "PENDING_NOTION_READ", "source_message_hash": "c" * 64}))
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE3", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[reservation]), \
                 patch.object(booking_bridge, "query_related_cleaning", return_value=[]), \
                 patch.object(booking_bridge, "send_cancellation_approval", return_value={"action_id": "action-1"}) as send:
                outcome = booking_bridge.process_one(path, {})
            saved = json.loads(path.read_text())
            self.assertEqual(outcome, "awaiting_approval")
            self.assertEqual(saved["status"], "AWAITING_CANCELLATION_APPROVAL")
            self.assertEqual(saved["approval_action_id"], "action-1")
            self.assertEqual(send.call_args.kwargs["refund_scope"], "UNKNOWN")

    def test_external_ambiguity_uses_fresh_readback_and_is_not_blind_retried(self):
        event = {
            "event_type": "BOOKING_CONFIRMED",
            "source_message_hash": "9" * 64,
            "listing_id": "listing-1",
            "check_in": "2026-10-01",
            "check_out": "2026-10-03",
            "adults": 2,
            "nights": 2,
            "currency": "USD",
            "guest_total": "300.00",
            "host_payout": "250.00",
        }
        mapping = {
            "nickname": "TEST",
            "address": "서울특별시 테스트구 테스트로 1",
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
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            path.write_text(json.dumps({"status": "PENDING_NOTION_READ", "source_message_hash": "9" * 64}))
            with patch.object(booking_bridge, "fresh_source", return_value=(event, "TESTCODE9", "received")), \
                 patch.object(booking_bridge, "query_exact", return_value=[]), \
                 patch.object(
                     booking_bridge,
                     "create_reservation",
                     side_effect=booking_bridge.ExternalEffectUncertain("NOTION_MUTATION_UNCERTAIN"),
                 ) as create_reservation, \
                 patch.object(
                     booking_bridge,
                     "_fresh_ambiguity_readback",
                     return_value={"performed": True, "reservation_match_count": 1},
                 ) as readback:
                outcome = booking_bridge.process_one(path, {"listing-1": mapping})

            saved = json.loads(path.read_text())
            self.assertEqual(outcome, "reconciliation_required")
            self.assertEqual(saved["status"], "RECONCILIATION_REQUIRED")
            self.assertEqual(saved["reason"], "EXTERNAL_EFFECT_UNCERTAIN_NO_BLIND_RETRY")
            self.assertEqual(saved["ambiguity_readback"]["reservation_match_count"], 1)
            create_reservation.assert_called_once()
            readback.assert_called_once()

            # RECONCILIATION_REQUIRED is deliberately outside automatic eligibility.
            with patch.object(booking_bridge, "fresh_source") as fresh_again, \
                 patch.object(booking_bridge, "create_reservation") as create_again:
                replay = booking_bridge.process_one(path, {"listing-1": mapping})
            self.assertEqual(replay, "skipped")
            fresh_again.assert_not_called()
            create_again.assert_not_called()


if __name__ == "__main__":
    unittest.main()
