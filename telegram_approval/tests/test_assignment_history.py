import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from telegram_approval import assignment_history as history
from telegram_approval.assignment_execution_receipt import (
    acceptance_operation_identity,
    create_successful_assignment_execution_receipt,
)


class FakeStore:
    def __init__(self):
        self.rows = []
        self.create_count = 0
        self.release_count = 0

    def query_assignment_generation(self, *, cleaning_page_id, assignment_version):
        result = []
        for row in self.rows:
            cleanings = history._relation_ids(row, "Cleaning")
            if (
                cleanings
                and history._same_page_id(cleanings[0], cleaning_page_id)
                and history._text(row, "Assignment Version") == assignment_version
                and history._select(row, "데이터 환경") == "PRODUCTION"
            ):
                result.append(copy.deepcopy(row))
        return result

    def query_by_idempotency(self, key):
        return [
            copy.deepcopy(row)
            for row in self.rows
            if history._text(row, "Idempotency Key") == key
            and history._select(row, "데이터 환경") == "PRODUCTION"
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
                "Assignment Ended At": {"date": None},
                "Assignment End Reason": {"select": None},
                "Assignment End Key": {"rich_text": []},
            },
        }

    def create_accepted_assignment(self, *, evidence, assignment_version, idempotency_key):
        self.create_count += 1
        row = self._row(
            evidence, assignment_version, idempotency_key, f"history-{self.create_count}"
        )
        self.rows.append(copy.deepcopy(row))
        return copy.deepcopy(row)

    def accept_existing_offer(self, page_id, *, evidence, assignment_version, idempotency_key):
        raise AssertionError("not needed by these focused tests")

    def release_accepted_assignment(
        self,
        page_id,
        *,
        cleaning_page_id,
        cleaner_party_page_id,
        assignment_version,
        action_id,
        idempotency_key,
        ended_at,
        end_reason,
        end_key,
    ):
        self.release_count += 1
        for row in self.rows:
            if row["id"] == page_id:
                row["properties"]["Hold 상태"] = history._select_property("RELEASED")
                row["properties"]["Assignment Ended At"] = history._date_property(ended_at)
                row["properties"]["Assignment End Reason"] = history._select_property(end_reason)
                row["properties"]["Assignment End Key"] = history._rich_text_property(end_key)
                return copy.deepcopy(row)
        raise AssertionError("row not found")


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode()


class AssignmentHistoryTimestampPrecisionTests(unittest.TestCase):
    def test_same_datetime_accepts_notion_minute_precision_for_receipt_seconds(self):
        self.assertTrue(
            history._same_datetime(
                "2026-09-03T15:16:00.000+00:00",
                datetime.fromisoformat("2026-09-03T15:16:55.395012+00:00"),
            )
        )

    def test_same_datetime_rejects_adjacent_minute(self):
        self.assertFalse(
            history._same_datetime(
                "2026-09-03T15:16:00.000+00:00",
                datetime.fromisoformat("2026-09-03T15:17:00+00:00"),
            )
        )

    def test_same_datetime_normalizes_timezone_before_minute_comparison(self):
        self.assertTrue(
            history._same_datetime(
                "2026-09-04T00:16:00+09:00",
                datetime.fromisoformat("2026-09-03T15:16:55+00:00"),
            )
        )


class AssignmentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requests = self.root / "requests"
        self.requests.mkdir()
        self.secret = self.root / "action-secret"
        self.secret.write_text("history-test-secret")
        self.lock_dir = self.root / "locks"

    def tearDown(self):
        self.tmp.cleanup()

    def write_executed_assignment(self, *, action_id="action-A", cleaning_id="cleaning-A"):
        record = {
            "schema_version": 2,
            "action_id": action_id,
            "action_type": "CLEANING_ASSIGNMENT",
            "test_mode": False,
            "consumed": True,
            "status": "APPROVED_PRODUCTION",
            "cleaning_page_id": cleaning_id,
            "candidate_party_page_id": "party-A",
            "candidate_user_id": 101,
            "candidate_chat_id": 202,
            "telegram_message_id": 303,
            "proposal_round": 1,
            "created_at": "2026-08-30T00:00:00+09:00",
            "expires_at": "2026-08-31T00:00:00+09:00",
            "consumed_at": "2026-08-30T00:05:00+09:00",
        }
        result = {
            "cleaning_assignment": "ACCEPTED",
            "next_expected_last_edited_time": "2026-08-29T15:05:01.000Z",
        }
        executed_at = "2026-08-30T00:05:01+09:00"
        record["assignment_execution_receipt"] = (
            create_successful_assignment_execution_receipt(
                record, result, executed_at=executed_at, secret_path=self.secret
            )
        )
        record["execution_result"] = result
        record["executed_at"] = executed_at
        record["status"] = "EXECUTED"
        (self.requests / f"{action_id}.json").write_text(json.dumps(record))
        return record

    def authority_patches(self):
        return (
            patch.object(history, "CANONICAL_ASSIGNMENT_REQUEST_DIR", self.requests),
            patch.object(history, "RECEIPT_SECRET_PATH", self.secret),
            patch.object(history, "ASSIGNMENT_HISTORY_LOCK_DIR", self.lock_dir),
        )

    def test_verified_execution_creates_exactly_one_accepted_history_generation(self):
        record = self.write_executed_assignment()
        store = FakeStore()
        p1, p2, p3 = self.authority_patches()
        with p1, p2, p3:
            key = acceptance_operation_identity(record, secret_path=self.secret)
            first = history.record_accepted_assignment(
                store=store, action_id="action-A", idempotency_key=key
            )
            second = history.record_accepted_assignment(
                store=store, action_id="action-A", idempotency_key=key
            )
        self.assertEqual(first.status, history.CREATED)
        self.assertEqual(second.status, history.ALREADY_ACCEPTED)
        self.assertEqual(store.create_count, 1)
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(history._select(store.rows[0], "제안 상태"), "ACCEPTED")
        self.assertEqual(history._select(store.rows[0], "Hold 상태"), "HARD_BOOKED")

    def test_idempotency_key_must_bind_authenticated_semantic_payload(self):
        self.write_executed_assignment()
        store = FakeStore()
        p1, p2, p3 = self.authority_patches()
        with p1, p2, p3:
            with self.assertRaises(history.AssignmentHistoryError) as caught:
                history.record_accepted_assignment(
                    store=store,
                    action_id="action-A",
                    idempotency_key="CLEANING_ASSIGNMENT_ACCEPTED:action-A",
                )
        self.assertEqual(caught.exception.code, history.VERSION_CONFLICT)
        self.assertEqual(store.create_count, 0)

    def test_end_preserves_accepted_fact_and_all_three_end_fields(self):
        record = self.write_executed_assignment()
        store = FakeStore()
        ended_at = datetime.fromisoformat("2026-08-30T01:00:00+09:00")
        p1, p2, p3 = self.authority_patches()
        with p1, p2, p3:
            key = acceptance_operation_identity(record, secret_path=self.secret)
            history.record_accepted_assignment(
                store=store, action_id="action-A", idempotency_key=key
            )
            result = history.end_accepted_assignment(
                store=store,
                cleaning_page_id="cleaning-A",
                cleaner_party_page_id="party-A",
                assignment_version="action-A",
                action_id="action-A",
                acceptance_idempotency_key=key,
                ended_at=ended_at,
                end_reason="OPERATOR_REPLACED",
                end_key="end:action-A:replacement-1",
            )
        self.assertEqual(result.status, history.UPDATED)
        row = store.rows[0]
        self.assertEqual(history._select(row, "제안 상태"), "ACCEPTED")
        self.assertEqual(history._select(row, "Hold 상태"), "RELEASED")
        self.assertEqual(history._select(row, "Assignment End Reason"), "OPERATOR_REPLACED")
        self.assertEqual(history._text(row, "Assignment End Key"), "end:action-A:replacement-1")
        self.assertEqual(
            history._date(row, "Assignment Ended At"), ended_at.isoformat()
        )

    def test_cleaner_unavailable_end_is_exactly_idempotent_and_conflicting_terminal_metadata_fails_closed(self):
        record = self.write_executed_assignment()
        store = FakeStore()
        ended_at = datetime.fromisoformat("2026-08-30T01:00:00+09:00")
        p1, p2, p3 = self.authority_patches()
        with p1, p2, p3:
            key = acceptance_operation_identity(record, secret_path=self.secret)
            history.record_accepted_assignment(store=store, action_id="action-A", idempotency_key=key)
            kwargs = dict(
                store=store,
                cleaning_page_id="cleaning-A",
                cleaner_party_page_id="party-A",
                assignment_version="action-A",
                action_id="action-A",
                acceptance_idempotency_key=key,
                ended_at=ended_at,
                end_reason="CLEANER_UNAVAILABLE",
                end_key="cleaner-unavailable:operation-A",
            )
            first = history.end_accepted_assignment(**kwargs)
            second = history.end_accepted_assignment(**kwargs)
            with self.assertRaises(history.AssignmentHistoryError) as caught:
                history.end_accepted_assignment(
                    **{**kwargs, "end_reason": "RESERVATION_CANCELLED", "end_key": "reservation-cancel:A"}
                )
        self.assertEqual(first.status, history.UPDATED)
        self.assertEqual(second.status, history.RETRY_SAME_KEY)
        self.assertEqual(caught.exception.code, history.CONFLICT)
        self.assertEqual(store.release_count, 1)
        row = store.rows[0]
        self.assertEqual(history._select(row, "제안 상태"), "ACCEPTED")
        self.assertEqual(history._select(row, "Hold 상태"), "RELEASED")
        self.assertEqual(history._select(row, "Assignment End Reason"), "CLEANER_UNAVAILABLE")
        self.assertEqual(history._text(row, "Assignment End Key"), "cleaner-unavailable:operation-A")
        self.assertEqual(history._date(row, "Assignment Ended At"), ended_at.isoformat())

    def test_newer_accepted_replacement_fact_is_durable_even_after_later_projection_changes(self):
        def accepted_row(version, accepted_at):
            return {
                "id": f"history-{version}",
                "properties": {
                    "데이터 환경": history._select_property("PRODUCTION"),
                    "제안 상태": history._select_property("ACCEPTED"),
                    "Cleaning": history._relation_property("cleaning-A"),
                    "Assignment Version": history._rich_text_property(version),
                    "응답 시각": history._date_property(datetime.fromisoformat(accepted_at)),
                },
            }

        class AcceptedStore:
            def __init__(self, rows):
                self.rows = rows

            def query_accepted_for_cleaning(self, *, cleaning_page_id):
                self.asserted_cleaning = cleaning_page_id
                return copy.deepcopy(self.rows)

        ended_at = "2026-08-30T01:00:00+09:00"
        original_only = AcceptedStore([
            accepted_row("action-A", "2026-08-30T00:05:00+09:00")
        ])
        self.assertFalse(history.newer_accepted_assignment_exists(
            store=original_only,
            cleaning_page_id="cleaning-A",
            original_assignment_version="action-A",
            original_ended_at=ended_at,
        ))
        with_replacement = AcceptedStore([
            accepted_row("action-B", "2026-08-30T01:10:00+09:00"),
            accepted_row("action-A", "2026-08-30T00:05:00+09:00"),
        ])
        self.assertTrue(history.newer_accepted_assignment_exists(
            store=with_replacement,
            cleaning_page_id="cleaning-A",
            original_assignment_version="action-A",
            original_ended_at=ended_at,
        ))

    def test_missing_legacy_history_is_not_inferred_or_backfilled(self):
        store = FakeStore()
        with patch.object(history, "ASSIGNMENT_HISTORY_LOCK_DIR", self.lock_dir):
            with self.assertRaises(history.AssignmentHistoryError) as caught:
                history.end_accepted_assignment(
                    store=store,
                    cleaning_page_id="cleaning-legacy",
                    cleaner_party_page_id="party-legacy",
                    assignment_version="legacy-version",
                    action_id="legacy-action",
                    acceptance_idempotency_key="legacy-key",
                    ended_at=datetime.fromisoformat("2026-08-30T01:00:00+09:00"),
                    end_reason="CLEANING_CANCELLED",
                    end_key="legacy-end",
                )
        self.assertEqual(caught.exception.code, history.NOT_FOUND)
        self.assertEqual(store.create_count, 0)
        self.assertEqual(store.release_count, 0)

    def test_notion_mutation_reasserts_global_writer_before_and_after_external_effect(self):
        calls = []
        token = self.root / "notion-token"
        token.write_text("fake")
        evidence = history.BindingAssignmentEvidence(
            action_id="action-A",
            cleaning_page_id="cleaning-A",
            cleaner_party_page_id="party-A",
            offered_at=datetime.fromisoformat("2026-08-30T00:00:00+09:00"),
            accepted_at=datetime.fromisoformat("2026-08-30T00:05:00+09:00"),
            executed_at=datetime.fromisoformat("2026-08-30T00:05:01+09:00"),
            telegram_message_id="303",
            proposal_round=1,
            cleaner_telegram_user_id=101,
            cleaner_telegram_chat_id=202,
            assignment_generation_identity="generation",
            assignment_version="action-A",
            operation_identity="semantic-key",
        )
        page = {
            "id": "history-1",
            "parent": {
                "data_source_id": history.ASSIGNMENT_HISTORY_SOURCE,
                "database_id": history.ASSIGNMENT_HISTORY_DATABASE,
            },
            "properties": {},
        }
        store = history.NotionAssignmentHistoryStore(
            token_path=token,
            urlopen=lambda *_args, **_kwargs: _Response(page),
            authority_assert=lambda: calls.append("assert"),
        )
        store.create_accepted_assignment(
            evidence=evidence,
            assignment_version="action-A",
            idempotency_key="semantic-key",
        )
        self.assertEqual(calls, ["assert", "assert"])


    def test_phase3_notion_assignment_create_writes_frozen_accepted_economics(self):
        calls = []
        token = self.root / "notion-token-phase3"
        token.write_text("fake")
        evidence = history.BindingAssignmentEvidence(
            action_id="urgent-action",
            cleaning_page_id="cleaning-urgent",
            cleaner_party_page_id="party-urgent",
            offered_at=datetime.fromisoformat("2026-09-10T00:10:00+09:00"),
            accepted_at=datetime.fromisoformat("2026-09-10T00:12:00+09:00"),
            executed_at=datetime.fromisoformat("2026-09-10T00:12:01+09:00"),
            telegram_message_id="909",
            proposal_round=2,
            cleaner_telegram_user_id=101,
            cleaner_telegram_chat_id=202,
            assignment_generation_identity="generation-urgent",
            assignment_version="urgent-action",
            operation_identity="semantic-urgent",
            replacement_urgency="URGENT",
            base_fee_snapshot=55000,
            urgent_premium_snapshot=5000,
            total_agreed_fee_snapshot=60000,
            urgent_premium_policy_version="PREMIUM-V1",
        )
        response_page = {
            "id": "history-urgent",
            "parent": {
                "data_source_id": history.ASSIGNMENT_HISTORY_SOURCE,
                "database_id": history.ASSIGNMENT_HISTORY_DATABASE,
            },
            "properties": {},
        }

        def urlopen(request, timeout=40):
            calls.append(json.loads(request.data.decode()))
            return _Response(response_page)

        store = history.NotionAssignmentHistoryStore(
            token_path=token, urlopen=urlopen, authority_assert=lambda: None
        )
        store.create_accepted_assignment(
            evidence=evidence, assignment_version="urgent-action",
            idempotency_key="semantic-urgent",
        )
        props = calls[0]["properties"]
        self.assertEqual(props["Replacement Urgency"]["select"]["name"], "URGENT")
        self.assertEqual(props["Base Fee Snapshot"]["number"], 55000)
        self.assertEqual(props["Urgent Premium Snapshot"]["number"], 5000)
        self.assertEqual(props["Total Agreed Fee Snapshot"]["number"], 60000)
        self.assertEqual(
            props["Urgent Premium Policy Version"]["rich_text"][0]["text"]["content"],
            "PREMIUM-V1",
        )

    def test_f06_duplicate_terminal_unavailable_threads_converge_to_one_release(self):
        record = self.write_executed_assignment()
        store = FakeStore()
        ended_at = datetime.fromisoformat("2026-08-30T01:00:00+09:00")
        p1, p2, p3 = self.authority_patches()
        with p1, p2, p3:
            key = acceptance_operation_identity(record, secret_path=self.secret)
            history.record_accepted_assignment(store=store, action_id="action-A", idempotency_key=key)
            kwargs = dict(
                store=store,
                cleaning_page_id="cleaning-A",
                cleaner_party_page_id="party-A",
                assignment_version="action-A",
                action_id="action-A",
                acceptance_idempotency_key=key,
                ended_at=ended_at,
                end_reason="CLEANER_UNAVAILABLE",
                end_key="cleaner-unavailable:race-A",
            )
            barrier = threading.Barrier(2)
            results, errors = [], []

            def run():
                barrier.wait()
                try:
                    results.append(history.end_accepted_assignment(**kwargs).status)
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual(results, [history.UPDATED, history.RETRY_SAME_KEY])
        self.assertEqual(store.release_count, 1)
        row = store.rows[0]
        self.assertEqual(history._select(row, "Assignment End Reason"), "CLEANER_UNAVAILABLE")
        self.assertEqual(history._text(row, "Assignment End Key"), "cleaner-unavailable:race-A")
        self.assertEqual(history._date(row, "Assignment Ended At"), ended_at.isoformat())

    def test_f06_competing_reservation_cancel_and_unavailable_each_winner_order_fails_loser_closed(self):
        cases = [
            ("CLEANER_UNAVAILABLE", "cleaner-unavailable:race", "RESERVATION_CANCELLED", "reservation-cancel:race"),
            ("RESERVATION_CANCELLED", "reservation-cancel:race", "CLEANER_UNAVAILABLE", "cleaner-unavailable:race"),
        ]
        for winner_reason, winner_key, loser_reason, loser_key in cases:
            with self.subTest(winner=winner_reason):
                record = self.write_executed_assignment(action_id=f"action-{winner_reason}", cleaning_id=f"cleaning-{winner_reason}")
                entered = threading.Event()
                release_winner = threading.Event()

                class GateStore(FakeStore):
                    def release_accepted_assignment(inner_self, page_id, **kwargs):
                        if kwargs["end_key"] == winner_key:
                            entered.set()
                            if not release_winner.wait(timeout=2):
                                raise AssertionError("winner gate timed out")
                        return super().release_accepted_assignment(page_id, **kwargs)

                store = GateStore()
                p1, p2, p3 = self.authority_patches()
                with p1, p2, p3:
                    key = acceptance_operation_identity(record, secret_path=self.secret)
                    history.record_accepted_assignment(
                        store=store,
                        action_id=record["action_id"],
                        idempotency_key=key,
                    )
                    common = dict(
                        store=store,
                        cleaning_page_id=record["cleaning_page_id"],
                        cleaner_party_page_id="party-A",
                        assignment_version=record["action_id"],
                        action_id=record["action_id"],
                        acceptance_idempotency_key=key,
                        ended_at=datetime.fromisoformat("2026-08-30T01:00:00+09:00"),
                    )
                    outcomes = {}
                    loser_started = threading.Event()

                    def winner():
                        try:
                            outcomes["winner"] = history.end_accepted_assignment(
                                **common, end_reason=winner_reason, end_key=winner_key
                            ).status
                        except Exception as exc:
                            outcomes["winner_error"] = exc

                    def loser():
                        loser_started.set()
                        try:
                            outcomes["loser"] = history.end_accepted_assignment(
                                **common, end_reason=loser_reason, end_key=loser_key
                            ).status
                        except Exception as exc:
                            outcomes["loser_error"] = exc

                    winner_thread = threading.Thread(target=winner)
                    winner_thread.start()
                    self.assertTrue(entered.wait(timeout=2))
                    loser_thread = threading.Thread(target=loser)
                    loser_thread.start()
                    self.assertTrue(loser_started.wait(timeout=2))
                    release_winner.set()
                    winner_thread.join(timeout=3)
                    loser_thread.join(timeout=3)

                self.assertEqual(outcomes.get("winner"), history.UPDATED)
                self.assertNotIn("winner_error", outcomes)
                self.assertIsInstance(outcomes.get("loser_error"), history.AssignmentHistoryError)
                self.assertEqual(outcomes["loser_error"].code, history.CONFLICT)
                self.assertEqual(store.release_count, 1)
                row = store.rows[0]
                self.assertEqual(history._select(row, "Assignment End Reason"), winner_reason)
                self.assertEqual(history._text(row, "Assignment End Key"), winner_key)


if __name__ == "__main__":
    unittest.main()
