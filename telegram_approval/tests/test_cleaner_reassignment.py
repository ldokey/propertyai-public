import copy
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from telegram_approval import assignment_history as history
from gmail_ingest import lifecycle_actions
from telegram_approval import bot_service
from telegram_approval import cleaner_reassignment as reassignment
from telegram_approval import cleaner_unavailable as unavailable


NOW = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
ORIGINAL_ENDED = NOW - timedelta(hours=2)
ORIGINAL_ACCEPTED = ORIGINAL_ENDED - timedelta(days=2)


def _row_text(value):
    return history._rich_text_property(value)


def original_row(*, request_status=reassignment.REASSIGNMENT_REQUESTED, economics=True):
    props = {
        "Cleaning": history._relation_property("cleaning-a"),
        "후보 인력": history._relation_property("party-original"),
        "제안 상태": history._select_property("ACCEPTED"),
        "Hold 상태": history._select_property("RELEASED"),
        "Binding Offer": history._checkbox_property(True),
        "응답 시각": history._date_property(ORIGINAL_ACCEPTED),
        "Action ID": _row_text("assign-original"),
        "Idempotency Key": _row_text("accept-original"),
        "Assignment Version": _row_text("assign-original"),
        "데이터 환경": history._select_property("PRODUCTION"),
        "Assignment Ended At": history._date_property(ORIGINAL_ENDED),
        "Assignment End Reason": history._select_property("CLEANER_UNAVAILABLE"),
        "Assignment End Key": _row_text("cleaner-unavailable:old"),
        "Reassignment Request Status": history._select_property(request_status),
        "Reassignment Requested At": history._date_property(ORIGINAL_ENDED + timedelta(minutes=30)),
        "Reassignment Request Key": _row_text("request-key-a"),
    }
    if economics:
        props.update({
            "Replacement Urgency": history._select_property("URGENT"),
            "Base Fee Snapshot": history._number_property(55000),
            "Urgent Premium Snapshot": history._number_property(15000),
            "Total Agreed Fee Snapshot": history._number_property(70000),
            "Urgent Premium Policy Version": _row_text("urgent-v3"),
        })
    return {"id": "history-original", "properties": props}


def accepted_replacement_row(version="replacement-accepted", accepted_at=None):
    accepted_at = accepted_at or ORIGINAL_ENDED + timedelta(minutes=10)
    return {
        "id": f"history-{version}",
        "properties": {
            "Cleaning": history._relation_property("cleaning-a"),
            "후보 인력": history._relation_property("party-replacement"),
            "제안 상태": history._select_property("ACCEPTED"),
            "Hold 상태": history._select_property("HARD_BOOKED"),
            "Binding Offer": history._checkbox_property(True),
            "응답 시각": history._date_property(accepted_at),
            "Action ID": _row_text(version),
            "Idempotency Key": _row_text(f"key-{version}"),
            "Assignment Version": _row_text(version),
            "데이터 환경": history._select_property("PRODUCTION"),
        },
    }


class FakeHistoryStore:
    def __init__(self, *, original=None, extra=None):
        self.rows = [original or original_row()] + list(extra or [])
        self.create_calls = 0
        self.resolve_calls = 0
        self.fail_resolve_once = False

    def query_assignment_generation(self, *, cleaning_page_id, assignment_version):
        return [row for row in self.rows if (
            history._relation_ids(row, "Cleaning") == [cleaning_page_id]
            and history._text(row, "Assignment Version") == assignment_version
        )]

    def query_by_idempotency(self, key):
        return [row for row in self.rows if history._text(row, "Idempotency Key") == key]

    def query_accepted_for_cleaning(self, *, cleaning_page_id):
        return [row for row in self.rows if history._relation_ids(row, "Cleaning") == [cleaning_page_id]]

    def query_effective_accepted(self, *, cleaning_page_id, cleaner_party_page_id):
        return [row for row in self.rows if (
            history._relation_ids(row, "Cleaning") == [cleaning_page_id]
            and history._relation_ids(row, "후보 인력") == [cleaner_party_page_id]
            and history._select(row, "Hold 상태") == "HARD_BOOKED"
        )]

    def create_reassigned_assignment(self, *, evidence, assignment_version, idempotency_key):
        self.create_calls += 1
        props = {
            "Cleaning": history._relation_property(evidence.cleaning_page_id),
            "후보 인력": history._relation_property(evidence.cleaner_party_page_id),
            "제안 상태": history._select_property("ACCEPTED"),
            "Hold 상태": history._select_property("HARD_BOOKED"),
            "Binding Offer": history._checkbox_property(False),
            "응답 시각": history._date_property(evidence.accepted_at),
            "Action ID": _row_text(evidence.action_id),
            "Idempotency Key": _row_text(idempotency_key),
            "Assignment Version": _row_text(assignment_version),
            "데이터 환경": history._select_property("PRODUCTION"),
        }
        if evidence.base_fee_snapshot is not None:
            props.update({
                "Replacement Urgency": history._select_property(evidence.replacement_urgency),
                "Base Fee Snapshot": history._number_property(evidence.base_fee_snapshot),
                "Urgent Premium Snapshot": history._number_property(evidence.urgent_premium_snapshot),
                "Total Agreed Fee Snapshot": history._number_property(evidence.total_agreed_fee_snapshot),
                "Urgent Premium Policy Version": _row_text(evidence.urgent_premium_policy_version),
            })
        row = {"id": f"history-reassigned-{self.create_calls}", "properties": props}
        self.rows.append(row)
        return row

    def release_accepted_assignment(self, page_id, **kwargs):
        row = next(row for row in self.rows if row["id"] == page_id)
        row["properties"]["Hold 상태"] = history._select_property("RELEASED")
        row["properties"]["Assignment Ended At"] = history._date_property(kwargs["ended_at"])
        row["properties"]["Assignment End Reason"] = history._select_property(kwargs["end_reason"])
        row["properties"]["Assignment End Key"] = _row_text(kwargs["end_key"])
        return row

    def resolve_reassignment_request(self, _page_id, **kwargs):
        self.resolve_calls += 1
        if self.fail_resolve_once:
            self.fail_resolve_once = False
            raise RuntimeError("synthetic request status failure")
        row = self.rows[0]
        old = history._select(row, "Reassignment Request Status")
        if old not in {reassignment.REASSIGNMENT_REQUESTED, kwargs["resolution"]}:
            raise RuntimeError("conflicting request resolution")
        row["properties"]["Reassignment Request Status"] = history._select_property(kwargs["resolution"])
        return row


class Case:
    def __init__(self, tmp_path, monkeypatch, *, store=None, economics=True):
        self.tmp = tmp_path
        self.request_dir = tmp_path / "requests"
        self.request_dir.mkdir(parents=True)
        self.secret = tmp_path / "secret"
        self.secret.write_text("phase4-secret")
        self.store = store or FakeHistoryStore(original=original_row(economics=economics))
        self.cleaning = {
            "id": "cleaning-a", "last_edited_time": "c0",
            "properties": {
                "데이터 환경": history._select_property("PRODUCTION"),
                "등록 상태": history._select_property("APPROVED"),
                "상태": history._select_property("담당자 배정"),
                "배정 수락 상태": history._select_property("대체필요"),
                "담당 참여자/업체": {"relation": []},
                "관련 Reservation": history._relation_property("reservation-a"),
            },
        }
        self.reservation = {
            "id": "reservation-a",
            "properties": {
                "데이터 환경": history._select_property("PRODUCTION"),
                "등록 상태": history._select_property("APPROVED"),
                "상태": history._select_property("확정"),
            },
        }
        self.pages = {"cleaning-a": self.cleaning, "reservation-a": self.reservation}
        self.source = {
            "schema_version": 1,
            "action_id": "request-a",
            "action_type": reassignment.SOURCE_ACTION_TYPE,
            "status": reassignment.REASSIGNMENT_REQUESTED,
            "request_key": "request-key-a",
            "cleaning_page_id": "cleaning-a",
            "cleaner_party_page_id": "party-original",
            "history_page_id": "history-original",
            "accepted_assignment_action_id": "assign-original",
            "assignment_version": "assign-original",
            "acceptance_idempotency_key": "accept-original",
            "end_key": "cleaner-unavailable:old",
            "original_ended_at": ORIGINAL_ENDED.isoformat(),
            "telegram_user_id": 101,
            "telegram_chat_id": 202,
        }
        (self.request_dir / "request-a.json").write_text(json.dumps(self.source))
        self.decision = reassignment.create_reassignment_decision_action(
            self.source, request_dir=self.request_dir, now=NOW
        )
        monkeypatch.setattr(history, "CANONICAL_ASSIGNMENT_REQUEST_DIR", self.request_dir)
        monkeypatch.setattr(history, "ASSIGNMENT_HISTORY_LOCK_DIR", tmp_path / "locks")

    def page_reader(self, page_id):
        return self.pages[page_id]

    def project(self, record, _page_reader):
        assigned = history._relation_ids(self.cleaning, "담당 참여자/업체")
        if history._select(self.cleaning, "배정 수락 상태") == "수락":
            assert assigned == [record["cleaner_party_page_id"]]
            return {"projection": "ALREADY_REASSIGNED", "next_expected_last_edited_time": "c1"}
        assert history._select(self.cleaning, "배정 수락 상태") == "대체필요"
        self.cleaning["properties"]["배정 수락 상태"] = history._select_property("수락")
        self.cleaning["properties"]["담당 참여자/업체"] = history._relation_property(record["cleaner_party_page_id"])
        self.cleaning["last_edited_time"] = "c1"
        return {"projection": "REASSIGNED", "next_expected_last_edited_time": "c1"}

    def offer(self, action_id="replacement-pending", *, status="PENDING", consumed=False, expires=None):
        expires = expires or NOW + timedelta(hours=1)
        value = {
            "schema_version": 2, "action_id": action_id,
            "action_type": "CLEANING_ASSIGNMENT", "cleaning_page_id": "cleaning-a",
            "candidate_party_page_id": "party-replacement", "candidate_user_id": 303,
            "candidate_chat_id": 404, "status": status, "consumed": consumed,
            "expires_at": expires.isoformat(),
        }
        (self.request_dir / f"{action_id}.json").write_text(json.dumps(value))
        return value

    def execute(self, decision=reassignment.REASSIGNED_ORIGINAL, **kwargs):
        kwargs.setdefault("projection_writer", self.project)
        return reassignment.execute_reassignment_decision(
            self.decision["action_id"], decision, request_dir=self.request_dir,
            history_store=self.store, page_reader=self.page_reader,
            now=NOW, **kwargs,
        )


def test_superseded_exact_semantics_and_rejected_expired_unchanged(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer("pending")
    case.offer("rejected", status="REJECTED", consumed=True)
    case.offer("expired", expires=NOW - timedelta(seconds=1))
    result = reassignment.supersede_pending_replacement_offers(
        request_dir=case.request_dir, cleaning_page_id="cleaning-a",
        original_cleaner_party_page_id="party-original",
        decision_action_id=case.decision["action_id"], now=NOW,
    )
    pending = json.loads((case.request_dir / "pending.json").read_text())
    rejected = json.loads((case.request_dir / "rejected.json").read_text())
    expired = json.loads((case.request_dir / "expired.json").read_text())
    assert result == {"superseded": ["pending"], "failures": []}
    assert pending["status"] == reassignment.SUPERSEDED
    assert pending["consumed"] is True
    assert pending["superseded_cause"] == reassignment.SUPERSEDED_CAUSE
    assert rejected["status"] == "REJECTED"
    assert expired["status"] == "PENDING" and expired["consumed"] is False


def test_reassign_creates_one_new_version_preserves_old_and_exact_phase3_economics(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer()
    core_before = {
        name: copy.deepcopy(case.store.rows[0]["properties"][name])
        for name in ("제안 상태", "Hold 상태", "Assignment Ended At", "Assignment End Reason", "Assignment End Key")
    }
    out = case.execute()
    assert out["status"] == reassignment.REASSIGNMENT_COMPLETE
    assert case.store.create_calls == 1
    assert len(case.store.rows) == 2
    new = case.store.rows[1]
    assert history._relation_ids(new, "후보 인력") == ["party-original"]
    assert history._checkbox(new, "Binding Offer") is False
    assert history._number(new, "Base Fee Snapshot") == 55000
    assert history._number(new, "Urgent Premium Snapshot") == 15000
    assert history._number(new, "Total Agreed Fee Snapshot") == 70000
    assert history._text(new, "Urgent Premium Policy Version") == "urgent-v3"
    assert {name: case.store.rows[0]["properties"][name] for name in core_before} == core_before
    assert history._select(case.store.rows[0], "Reassignment Request Status") == reassignment.REASSIGNED_ORIGINAL
    assert history._relation_ids(case.cleaning, "담당 참여자/업체") == ["party-original"]
    assert json.loads((case.request_dir / "replacement-pending.json").read_text())["status"] == reassignment.SUPERSEDED


def test_double_reassign_is_one_generation_and_no_policy_lookup(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer()
    first = case.execute()
    second = case.execute()
    assert first["reassignment_assignment_version"] == second["reassignment_assignment_version"]
    assert case.store.create_calls == 1
    assert len([r for r in case.store.rows if history._checkbox(r, "Binding Offer") is False]) == 1


def test_continue_replacement_is_terminal_idempotent_and_does_not_touch_offer_or_assignment(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer()
    before = (case.request_dir / "replacement-pending.json").read_text()
    first = case.execute(reassignment.CONTINUE_REPLACEMENT)
    second = case.execute(reassignment.CONTINUE_REPLACEMENT)
    assert first["status"] == second["status"] == reassignment.CONTINUE_REPLACEMENT_COMPLETE
    assert case.store.create_calls == 0
    assert (case.request_dir / "replacement-pending.json").read_text() == before
    assert history._select(case.store.rows[0], "Reassignment Request Status") == reassignment.CONTINUE_REPLACEMENT
    with pytest.raises(reassignment.CleanerReassignmentError, match="resolved differently"):
        case.execute(reassignment.REASSIGNED_ORIGINAL)


def test_replacement_accepted_wins_and_original_reassign_writes_zero(tmp_path, monkeypatch):
    store = FakeHistoryStore(extra=[accepted_replacement_row()])
    case = Case(tmp_path, monkeypatch, store=store)
    case.offer()
    with pytest.raises(reassignment.CleanerReassignmentError, match="newer accepted"):
        case.execute()
    decision = json.loads((case.request_dir / f"{case.decision['action_id']}.json").read_text())
    assert decision["decision_committed"] is False
    assert store.create_calls == 0
    assert json.loads((case.request_dir / "replacement-pending.json").read_text())["status"] == "PENDING"


def test_original_reassign_wins_then_late_offer_callback_is_fail_closed(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    offer = case.offer()
    case.execute()
    digest = hmac.new(
        case.secret.read_text().encode(), f"{offer['action_id']}:approve".encode(), hashlib.sha256
    ).hexdigest()[:16]
    update = {"callback_query": {
        "id": "cb", "data": f"a:{offer['action_id']}:approve:{digest}",
        "from": {"id": 303}, "message": {"chat": {"id": 404}},
    }}
    calls = []
    result = bot_service.callback(
        update, "token", request_dir=case.request_dir, action_secret_path=case.secret,
        allowed_action_types={"CLEANING_ASSIGNMENT"}, preauthorized_identity=(303, 404),
        request_api=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
        assignment_history_store=case.store,
    )
    assert result == "ignored"
    assert json.loads((case.request_dir / f"{offer['action_id']}.json").read_text())["status"] == reassignment.SUPERSEDED
    assert len(case.store.rows) == 2


def test_partial_failure_a_supersede_then_assignment_failure_is_retryable_and_fenced(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer()
    real = reassignment.record_reassigned_original_assignment
    monkeypatch.setattr(
        reassignment, "record_reassigned_original_assignment",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("synthetic assignment write failure")),
    )
    first = case.execute()
    assert first["status"] == reassignment.ASSIGNMENT_RECONCILIATION_REQUIRED
    assert json.loads((case.request_dir / "replacement-pending.json").read_text())["status"] == reassignment.SUPERSEDED
    assert reassignment.original_reassignment_decision_committed(
        request_dir=case.request_dir, cleaning_page_id="cleaning-a"
    )
    monkeypatch.setattr(reassignment, "record_reassigned_original_assignment", real)
    second = case.execute()
    assert second["status"] == reassignment.REASSIGNMENT_COMPLETE
    assert case.store.create_calls == 1


def test_partial_failure_b_assignment_authoritative_when_offer_cleanup_fails_then_retry_cleans(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer()
    failures = {"replacement-pending": 1}

    def flaky(path, value):
        action_id = value.get("action_id")
        if failures.get(action_id):
            failures[action_id] -= 1
            raise RuntimeError("synthetic Offer write failure")
        reassignment._atomic_private(path, value)

    first = case.execute(offer_atomic_writer=flaky)
    assert first["status"] == reassignment.OFFER_CLEANUP_RECONCILIATION_REQUIRED
    assert case.store.create_calls == 1
    assert json.loads((case.request_dir / "replacement-pending.json").read_text())["status"] == "PENDING"
    second = case.execute()
    assert second["status"] == reassignment.REASSIGNMENT_COMPLETE
    assert case.store.create_calls == 1
    assert json.loads((case.request_dir / "replacement-pending.json").read_text())["status"] == reassignment.SUPERSEDED


def test_partial_failure_c_projection_and_d_request_resolution_do_not_recreate_assignment(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer()
    first = case.execute(projection_writer=lambda *_a: (_ for _ in ()).throw(RuntimeError("projection")))
    assert first["status"] == reassignment.PROJECTION_RECONCILIATION_REQUIRED
    assert case.store.create_calls == 1
    versions = [history._text(row, "Assignment Version") for row in case.store.rows]
    assert versions == ["assign-original", first["reassignment_assignment_version"]]
    assert history._select(case.store.rows[1], "Hold 상태") == "HARD_BOOKED"
    assert case.store.resolve_calls == 0
    case.store.fail_resolve_once = True
    second = case.execute()
    assert second["status"] == reassignment.REQUEST_RESOLUTION_RECONCILIATION_REQUIRED
    assert case.store.create_calls == 1
    assert case.store.resolve_calls == 1
    assert [history._text(row, "Assignment Version") for row in case.store.rows] == versions
    third = case.execute()
    assert third["status"] == reassignment.REASSIGNMENT_COMPLETE
    assert case.store.create_calls == 1
    assert case.store.resolve_calls == 2
    assert [history._text(row, "Assignment Version") for row in case.store.rows] == versions
    assert history._select(case.store.rows[0], "Reassignment Request Status") == reassignment.REASSIGNED_ORIGINAL


def test_notification_failure_is_downstream_and_retry_does_not_recreate_assignment(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer()
    first = case.execute(post_notifier=lambda _r: (_ for _ in ()).throw(RuntimeError("telegram down")))
    assert first["status"] == reassignment.REASSIGNMENT_COMPLETE
    assert first["notification_status"] == reassignment.NOTIFICATION_RETRY_REQUIRED
    assert case.store.create_calls == 1
    assert history._select(case.store.rows[1], "Hold 상태") == "HARD_BOOKED"
    delivered = []
    second = case.execute(post_notifier=lambda record: delivered.append(record["reassignment_assignment_version"]))
    assert second["notification_status"] == "DELIVERED"
    assert delivered == [first["reassignment_assignment_version"]]
    assert case.store.create_calls == 1
    assert len(case.store.rows) == 2


def test_reservation_cancel_first_and_work_start_first_fail_closed(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.offer()
    offer_before = (case.request_dir / "replacement-pending.json").read_text()
    case.reservation["properties"]["상태"] = history._select_property("취소")
    with pytest.raises(reassignment.CleanerReassignmentError, match="Reservation"):
        case.execute()
    assert case.store.create_calls == 0
    assert (case.request_dir / "replacement-pending.json").read_text() == offer_before

    case2 = Case(tmp_path / "second", monkeypatch)
    case2.cleaning["properties"]["상태"] = history._select_property("진행중")
    case2.cleaning["properties"]["배정 수락 상태"] = history._select_property("수락")
    case2.cleaning["properties"]["담당 참여자/업체"] = history._relation_property("party-replacement")
    with pytest.raises(reassignment.CleanerReassignmentError, match="reassignable"):
        case2.execute()
    assert case2.store.create_calls == 0


def test_reassign_first_new_effective_assignment_is_original_cleaner_and_old_is_not_effective(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.execute()
    evidence, row = history.resolve_effective_accepted_assignment(
        store=case.store, cleaning_page_id="cleaning-a", cleaner_party_page_id="party-original"
    )
    assert evidence.binding_offer is False
    assert evidence.cleaner_party_page_id == "party-original"
    assert row["id"].startswith("history-reassigned-")
    assert history._select(case.store.rows[0], "Hold 상태") == "RELEASED"


def test_reassign_first_then_reservation_cancellation_releases_new_effective_assignment(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.execute()

    def fake_notion(method, path, body=None):
        page_id = path.rsplit("/", 1)[-1]
        page = case.pages[page_id]
        if method == "GET":
            return page
        assert method == "PATCH"
        page["properties"].update((body or {}).get("properties", {}))
        page["last_edited_time"] = "cancelled"
        return page

    monkeypatch.setattr(lifecycle_actions, "_notion", fake_notion)
    monkeypatch.setattr(lifecycle_actions, "_finance_cancellation_review", lambda *_a, **_k: {"outcome": "TEST"})
    monkeypatch.setattr(lifecycle_actions, "_send_cleaner_cancellation", lambda *_a, **_k: [])
    result = lifecycle_actions.execute_cancellation({
        "reservation_page_id": "reservation-a",
        "reservation_code": "R-A",
        "source_message_hash": "a" * 64,
        "cancellation_source": "PLATFORM_NOTICE",
        "cancellation_received_at": NOW.isoformat(),
        "cleaning_page_ids": ["cleaning-a"],
        "calendar_event_ids": [],
        "cleaning_calendar_id": None,
    }, history_store=case.store)

    assert result["reservation"] == "CANCELLED"
    assert history._select(case.cleaning, "상태") == "취소"
    effective = [row for row in case.store.rows if history._select(row, "Hold 상태") == "HARD_BOOKED"]
    assert effective == []
    reassigned = case.store.rows[1]
    assert history._select(reassigned, "Assignment End Reason") == "RESERVATION_CANCELLED"
    assert history._text(reassigned, "Assignment End Key") == "reservation-cancel:reservation-a:cleaning-a"
    assert history._select(case.store.rows[0], "Assignment End Reason") == "CLEANER_UNAVAILABLE"


def test_legacy_base_only_preserved_without_fabricating_premium(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch, economics=False)
    # Exact legacy base authority comes from the original signed Offer file.
    (case.request_dir / "assign-original.json").write_text(json.dumps({
        "action_id": "assign-original", "action_type": "CLEANING_ASSIGNMENT",
        "cleaning_fee_krw": 45000,
    }))
    out = case.execute()
    assert out["reassignment_economics"] == {"legacy_base_fee_krw": 45000}
    new = case.store.rows[1]
    assert history._select(new, "Replacement Urgency") is None
    assert history._number(new, "Urgent Premium Snapshot") is None
    assert history._text(new, "Urgent Premium Policy Version") is None


def test_ambiguous_corrupt_economics_blocks_without_guessing(tmp_path, monkeypatch):
    bad = original_row()
    bad["properties"]["Total Agreed Fee Snapshot"] = history._number_property(99999)
    case = Case(tmp_path, monkeypatch, store=FakeHistoryStore(original=bad))
    with pytest.raises(reassignment.CleanerReassignmentError, match=reassignment.REASSIGNMENT_ECONOMICS_BLOCKED):
        case.execute()
    persisted = json.loads((case.request_dir / f"{case.decision['action_id']}.json").read_text())
    assert persisted["status"] == reassignment.REASSIGNMENT_ECONOMICS_BLOCKED
    assert case.store.create_calls == 0


def _offer_callback(case, offer):
    digest = hmac.new(
        case.secret.read_text().encode(), f"{offer['action_id']}:approve".encode(), hashlib.sha256
    ).hexdigest()[:16]
    return {"callback_query": {
        "id": "cb", "data": f"a:{offer['action_id']}:approve:{digest}",
        "from": {"id": 303}, "message": {"chat": {"id": 404}},
    }}


def test_late_pending_offer_is_blocked_by_local_and_canonical_assignment_fences(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    offer = case.offer()
    failures = {offer["action_id"]: 1}

    def flaky(path, value):
        if failures.get(value.get("action_id")):
            failures[value["action_id"]] -= 1
            raise RuntimeError("synthetic Offer cleanup response loss")
        reassignment._atomic_private(path, value)

    first = case.execute(offer_atomic_writer=flaky)
    assert first["status"] == reassignment.OFFER_CLEANUP_RECONCILIATION_REQUIRED
    assert case.store.create_calls == 1
    assert json.loads((case.request_dir / f"{offer['action_id']}.json").read_text())["status"] == "PENDING"

    calls = []
    assert bot_service.callback(
        _offer_callback(case, offer), "token", request_dir=case.request_dir,
        action_secret_path=case.secret, allowed_action_types={"CLEANING_ASSIGNMENT"},
        preauthorized_identity=(303, 404),
        request_api=lambda *a, **k: calls.append((a, k)) or {"ok": True},
        assignment_history_store=case.store,
    ) == "ignored"
    assert case.store.create_calls == 1

    monkeypatch.setattr(reassignment, "original_reassignment_decision_committed", lambda **_kw: False)
    assert bot_service.callback(
        _offer_callback(case, offer), "token", request_dir=case.request_dir,
        action_secret_path=case.secret, allowed_action_types={"CLEANING_ASSIGNMENT"},
        preauthorized_identity=(303, 404),
        request_api=lambda *a, **k: calls.append((a, k)) or {"ok": True},
        assignment_history_store=case.store,
    ) == "ignored"
    persisted = json.loads((case.request_dir / f"{offer['action_id']}.json").read_text())
    assert persisted["status"] == "PENDING" and persisted["consumed"] is False
    assert case.store.create_calls == 1


def test_cleaning_lock_serializes_same_cleaning_but_not_different_cleanings(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "ASSIGNMENT_HISTORY_LOCK_DIR", tmp_path / "locks")
    held = threading.Event()
    release = threading.Event()
    same_entered = threading.Event()
    different_entered = threading.Event()

    def holder():
        with history.cleaning_assignment_lock(cleaning_page_id="cleaning-a"):
            held.set()
            assert release.wait(2)

    def same_waiter():
        assert held.wait(2)
        with history.cleaning_assignment_lock(cleaning_page_id="cleaning-a"):
            same_entered.set()

    def different_waiter():
        assert held.wait(2)
        with history.cleaning_assignment_lock(cleaning_page_id="cleaning-b"):
            different_entered.set()

    first = threading.Thread(target=holder)
    same = threading.Thread(target=same_waiter)
    different = threading.Thread(target=different_waiter)
    first.start()
    assert held.wait(2)
    same.start()
    different.start()
    assert different_entered.wait(1)
    assert not same_entered.wait(0.05)
    release.set()
    first.join(2); same.join(2); different.join(2)
    assert same_entered.is_set()
    assert not first.is_alive() and not same.is_alive() and not different.is_alive()


def test_reassignment_acquires_cleaning_lock_before_generation_lock(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    events = []

    @contextmanager
    def cleaning_lock(**_kw):
        events.append("cleaning-enter")
        try:
            yield
        finally:
            events.append("cleaning-exit")

    @contextmanager
    def generation_lock(**_kw):
        events.append("generation-enter")
        try:
            yield
        finally:
            events.append("generation-exit")

    monkeypatch.setattr(reassignment, "cleaning_assignment_lock", cleaning_lock)
    monkeypatch.setattr(history, "assignment_generation_lock", generation_lock)
    case.execute()
    assert events == ["cleaning-enter", "generation-enter", "generation-exit", "cleaning-exit"]


def test_cancel_callback_acquires_multiple_cleaning_locks_in_sorted_order(tmp_path, monkeypatch):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("phase4-secret")
    action_id = "cancel-a"
    callback_now = datetime.now(timezone.utc)
    record = {
        "schema_version": 2, "action_id": action_id,
        "action_type": "CANCEL_RESERVATION_WORKFLOW",
        "cleaning_page_ids": ["cleaning-z", "cleaning-a", "cleaning-m", "cleaning-a"],
        "status": "PENDING", "consumed": False, "test_mode": True,
        "expires_at": (callback_now + timedelta(hours=1)).isoformat(),
        "external_writes_on_approval": 0,
    }
    assert datetime.now(timezone.utc) < datetime.fromisoformat(record["expires_at"])
    (request_dir / f"{action_id}.json").write_text(json.dumps(record))
    digest = hmac.new(secret.read_text().encode(), f"{action_id}:approve".encode(), hashlib.sha256).hexdigest()[:16]
    update = {"callback_query": {
        "id": "cancel-cb", "data": f"a:{action_id}:approve:{digest}",
        "from": {"id": 1}, "message": {"chat": {"id": 1}},
    }}
    entered = []

    @contextmanager
    def recording_lock(*, cleaning_page_id, **_kw):
        entered.append(cleaning_page_id)
        yield

    monkeypatch.setattr(history, "cleaning_assignment_lock", recording_lock)
    result = bot_service.callback(
        update, "token", request_dir=request_dir, action_secret_path=secret,
        allowed_action_types={"CANCEL_RESERVATION_WORKFLOW"}, preauthorized_identity=(1, 1),
        request_api=lambda *_a, **_k: {"ok": True},
    )
    assert result == "approved"
    assert entered == ["cleaning-a", "cleaning-m", "cleaning-z"]


def test_cancel_callback_expired_record_remains_fail_closed(tmp_path, monkeypatch):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("phase4-secret")
    action_id = "cancel-expired"
    record = {
        "schema_version": 2, "action_id": action_id,
        "action_type": "CANCEL_RESERVATION_WORKFLOW",
        "cleaning_page_ids": ["cleaning-a"],
        "status": "PENDING", "consumed": False, "test_mode": True,
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "external_writes_on_approval": 0,
    }
    (request_dir / f"{action_id}.json").write_text(json.dumps(record))
    digest = hmac.new(secret.read_text().encode(), f"{action_id}:approve".encode(), hashlib.sha256).hexdigest()[:16]
    update = {"callback_query": {
        "id": "cancel-expired-cb", "data": f"a:{action_id}:approve:{digest}",
        "from": {"id": 1}, "message": {"chat": {"id": 1}},
    }}
    entered = []

    @contextmanager
    def recording_lock(*, cleaning_page_id, **_kw):
        entered.append(cleaning_page_id)
        yield

    monkeypatch.setattr(history, "cleaning_assignment_lock", recording_lock)
    result = bot_service.callback(
        update, "token", request_dir=request_dir, action_secret_path=secret,
        allowed_action_types={"CANCEL_RESERVATION_WORKFLOW"}, preauthorized_identity=(1, 1),
        request_api=lambda *_a, **_k: {"ok": True},
    )
    persisted = json.loads((request_dir / f"{action_id}.json").read_text())
    assert result == "ignored"
    assert entered == ["cleaning-a"]
    assert persisted["status"] == "PENDING"
    assert persisted["consumed"] is False


def test_reassign_vs_continue_concurrent_decision_has_one_terminal_winner(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    barrier = threading.Barrier(3)
    outcomes = []

    def run(decision):
        barrier.wait()
        try:
            outcomes.append((decision, case.execute(decision)["status"]))
        except reassignment.CleanerReassignmentError as exc:
            outcomes.append((decision, f"ERROR:{exc}"))

    threads = [
        threading.Thread(target=run, args=(reassignment.REASSIGNED_ORIGINAL,)),
        threading.Thread(target=run, args=(reassignment.CONTINUE_REPLACEMENT,)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()

    successes = [item for item in outcomes if not item[1].startswith("ERROR:")]
    failures = [item for item in outcomes if item[1].startswith("ERROR:")]
    assert len(successes) == len(failures) == 1
    winner = successes[0][0]
    assert history._select(case.store.rows[0], "Reassignment Request Status") == winner
    assert case.store.create_calls == (1 if winner == reassignment.REASSIGNED_ORIGINAL else 0)


def test_my_terminal_reassignment_states_are_human_and_non_actionable(tmp_path, monkeypatch):
    page = {
        "id": "cleaning-a", "last_edited_time": "c1",
        "properties": {
            "데이터 환경": history._select_property("PRODUCTION"),
            "등록 상태": history._select_property("APPROVED"),
            "상태": history._select_property("담당자 배정"),
            "배정 수락 상태": history._select_property("수락"),
            "담당 참여자/업체": history._relation_property("party-original"),
            "연결 집": history._relation_property("house-a"),
            "점검일": history._date_property(NOW + timedelta(days=1)),
        },
    }
    base_fact = {
        "cleaning_page_id": "cleaning-a", "cleaner_party_page_id": "party-original",
        "assignment_version": "assign-original", "ended_at": ORIGINAL_ENDED.isoformat(),
        "reassignment_requested_at": ORIGINAL_ENDED.isoformat(), "reassignment_request_key": "request-key-a",
    }
    for terminal, wording in (
        (reassignment.REASSIGNED_ORIGINAL, "원래 일정 재배정 완료"),
        (reassignment.CONTINUE_REPLACEMENT, "대체 담당자 진행 유지"),
    ):
        fact = {**base_fact, "reassignment_request_status": terminal}
        item = unavailable._safe_recent_item(page, fact, {"house-a": "JJ"})
        assert item["recent_status_text"] == wording
        assert item["can_reassign"] is False
        rendered = repr(item)
        assert "party-replacement" not in rendered
        assert "REASSIGNED_ORIGINAL" not in item["recent_status_text"]
        assert "CONTINUE_REPLACEMENT" not in item["recent_status_text"]


def test_reassign_wins_then_only_new_effective_binding_may_start_work(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.execute()
    record = {"cleaning_page_id": "cleaning-a", "candidate_party_page_id": "party-original"}
    evidence, row = lifecycle_actions._assert_effective_assignment_for_work_start(record, case.store)
    assert evidence.binding_offer is False
    assert row["id"].startswith("history-reassigned-")
    with pytest.raises(ValueError, match="no longer effective"):
        lifecycle_actions._assert_effective_assignment_for_work_start(
            {"cleaning_page_id": "cleaning-a", "candidate_party_page_id": "party-replacement"},
            case.store,
        )
    assert history._select(case.store.rows[0], "Hold 상태") == "RELEASED"
    assert history._select(case.store.rows[1], "Hold 상태") == "HARD_BOOKED"


def test_my_successful_reassignment_keeps_new_upcoming_and_old_terminal_recent(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("phase4-secret")
    active = {
        "page_id": "cleaning-a",
        "ui_state": "UPCOMING",
        "property_label": "JJ",
    }
    recent = {
        "page_id": "cleaning-a",
        "property_label": "JJ",
        "reassignment_request_status": reassignment.REASSIGNED_ORIGINAL,
        "recent_status_text": "원래 일정 재배정 완료",
        "can_reassign": False,
    }
    monkeypatch.setattr(unavailable, "recent_change_items", lambda **_kw: [recent])
    monkeypatch.setattr(
        unavailable,
        "create_unavailable_action",
        lambda item, cleaner, **_kw: {"action_id": "active-unavailable-action"},
    )

    items, keyboard = unavailable.build_schedule_ui(
        cleaner={"party_page_id": "party-original"},
        items=[active],
        history_store=object(),
        request_dir=tmp_path,
        secret_path=secret,
        now=NOW,
    )

    assert items[0]["page_id"] == "cleaning-a"
    assert items[0]["ui_state"] == "UPCOMING"
    assert items[0]["human_status"] == "배정 확정"
    assert items[1]["recent_status_text"] == "원래 일정 재배정 완료"
    assert items[1]["can_reassign"] is False
    assert keyboard is not None
    labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
    assert labels == ["진행 불가 · JJ"]
    assert all("다시 가능해졌어요" not in label for label in labels)
