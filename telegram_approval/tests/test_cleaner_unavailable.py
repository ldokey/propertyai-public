import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_approval import assignment_history as history
from telegram_approval import cleaner_unavailable as unavailable
from telegram_approval import cleaner_performance as performance


NOW = datetime.fromisoformat("2026-09-02T03:00:00+00:00")


@pytest.fixture(autouse=True)
def _stable_exact_history_binding(monkeypatch):
    # Orchestration tests isolate the already-tested canonical 19 resolver; a
    # dedicated stale-binding test below overrides this seam to fail closed.
    monkeypatch.setattr(unavailable, "_assert_exact_history_binding", lambda *_a, **_k: None)
    stable_policy = performance.PerformancePolicy(
        version="P3-TEST-V1",
        score_deltas={
            performance.EARLY_UNAVAILABLE: -1,
            performance.SAME_DAY_UNAVAILABLE: -2,
            performance.URGENT_ACCEPTED: 1,
            performance.URGENT_COMPLETED: 2,
        },
        same_day_penalty_enabled=True,
        same_day_penalty_default_krw=1000,
        urgent_premium_krw=5000,
        replacement_urgent_lead_minutes=None,
    )
    monkeypatch.setattr(
        unavailable, "PERFORMANCE_POLICY_STORE_FACTORY",
        lambda: performance.InMemoryPerformancePolicyStore([stable_policy]),
    )
    monkeypatch.setattr(
        unavailable, "PERFORMANCE_EVENT_STORE_FACTORY",
        performance.InMemoryPerformanceEventStore,
    )


CLEANER = {
    "identity_id": "cleaner-a",
    "role": "CLEANER",
    "status": "ACTIVE",
    "telegram_user_id": 101,
    "telegram_chat_id": 202,
    "party_page_id": "party-a",
    "properties": ["JJ"],
    "priority_by_property": {"JJ": 1},
}


def roster(cleaner=CLEANER):
    return {"schema_version": 1, "cleaners": [dict(cleaner)]}


def accepted_offer(action_id="assign-a"):
    return {
        "schema_version": 2,
        "action_id": action_id,
        "action_type": "CLEANING_ASSIGNMENT",
        "cleaning_page_id": "cleaning-a",
        "cleaning_name": "JJ checkout cleaning",
        "property_nickname": "JJ",
        "address": "private-runtime-address",
        "start_at": "2026-09-08 11:00",
        "end_at": "2026-09-08 15:00",
        "cleaning_fee_krw": 60000,
        "candidate_party_page_id": "party-a",
        "remaining_candidates": [{"party_page_id": "stale-never-use"}],
        "status": "EXECUTED",
        "consumed": True,
    }


def action_record(action_id="unavailable-a"):
    return {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": "CLEANER_UNAVAILABLE",
        "status": "CONFIRMATION_OPEN",
        "cleaning_page_id": "cleaning-a",
        "property_nickname": "JJ",
        "expected_cleaning_last_edited_time": "edit-before",
        "cleaner_party_page_id": "party-a",
        "telegram_user_id": 101,
        "telegram_chat_id": 202,
        "history_page_id": "history-a",
        "accepted_assignment_action_id": "assign-a",
        "assignment_version": "assign-a",
        "acceptance_idempotency_key": "accept-key-a",
        "proposal_round": 1,
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=24)).isoformat(),
        "business_mutations": 0,
    }


def write_action(tmp_path: Path, record=None):
    record = dict(record or action_record())
    path = tmp_path / f"{record['action_id']}.json"
    path.write_text(json.dumps(record))
    return path, record


def write_offer(tmp_path: Path):
    offer = accepted_offer()
    (tmp_path / "assign-a.json").write_text(json.dumps(offer))
    return offer


def result(status="UPDATED"):
    return SimpleNamespace(status=status, page_id="history-a", mutated=status == "UPDATED")


def test_unavailable_action_binds_exact_assignment_cleaning_identity_and_expiry(monkeypatch, tmp_path):
    evidence = SimpleNamespace(
        action_id="assign-a",
        assignment_version="assign-a",
        operation_identity="accept-key-a",
        proposal_round=7,
        cleaner_telegram_user_id=101,
        cleaner_telegram_chat_id=202,
    )
    monkeypatch.setattr(
        unavailable,
        "resolve_effective_accepted_assignment",
        lambda **_kw: (evidence, {"id": "history-a"}),
    )
    item = {
        "page_id": "cleaning-a",
        "property_label": "JJ",
        "expected_cleaning_last_edited_time": "edit-before",
    }
    record = unavailable.create_unavailable_action(
        item, CLEANER, request_dir=tmp_path, now=NOW,
    )
    assert record["cleaning_page_id"] == "cleaning-a"
    assert record["cleaner_party_page_id"] == "party-a"
    assert record["history_page_id"] == "history-a"
    assert record["accepted_assignment_action_id"] == "assign-a"
    assert record["assignment_version"] == "assign-a"
    assert record["acceptance_idempotency_key"] == "accept-key-a"
    assert record["expected_cleaning_last_edited_time"] == "edit-before"
    assert record["telegram_user_id"] == 101
    assert record["telegram_chat_id"] == 202
    assert record["action_id"]
    assert datetime.fromisoformat(record["created_at"]) == NOW
    assert datetime.fromisoformat(record["expires_at"]) - NOW == unavailable.ACTION_TTL
    assert record["business_mutations"] == 0


def test_action_creation_denies_assignment_receipt_bound_to_different_cleaner_identity(monkeypatch, tmp_path):
    evidence = SimpleNamespace(
        action_id="assign-a",
        assignment_version="assign-a",
        operation_identity="accept-key-a",
        proposal_round=1,
        cleaner_telegram_user_id=999,
        cleaner_telegram_chat_id=202,
    )
    monkeypatch.setattr(
        unavailable,
        "resolve_effective_accepted_assignment",
        lambda **_kw: (evidence, {"id": "history-a"}),
    )
    with pytest.raises(unavailable.CleanerUnavailableError, match="Telegram identity mismatch"):
        unavailable.create_unavailable_action(
            {
                "page_id": "cleaning-a",
                "property_label": "JJ",
                "expected_cleaning_last_edited_time": "edit-before",
            },
            CLEANER,
            request_dir=tmp_path,
            now=NOW,
        )


def test_execute_unavailable_fresh_recomputes_candidates_and_excludes_original(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    calls = {"end": 0, "resolve": 0, "send": 0}
    sent = {}

    def fake_end(**kwargs):
        calls["end"] += 1
        assert kwargs["end_reason"] == "CLEANER_UNAVAILABLE"
        assert kwargs["end_key"] == "cleaner-unavailable:unavailable-a"
        assert kwargs["ended_at"] == NOW
        return result()

    def resolver(_nickname):
        calls["resolve"] += 1
        return ([
            {"party_page_id": "party-a", "telegram_user_id": 101, "telegram_chat_id": 202},
            {"party_page_id": "party-b", "telegram_user_id": 303, "telegram_chat_id": 404},
        ], [{"telegram_chat_id": 999}])

    def sender(**kwargs):
        calls["send"] += 1
        sent.update(kwargs)
        return {"sent": True, "action_id": "replacement-offer"}

    monkeypatch.setattr(unavailable, "end_accepted_assignment", fake_end)
    out = unavailable.execute_unavailable(
        record, path=path, history_store=object(), request_dir=tmp_path, now=NOW,
        validator=lambda _record: {"fresh": True},
        projector=lambda _record: {"next_expected_last_edited_time": "edit-after"},
        candidate_resolver=resolver, assignment_sender=sender,
    )

    assert out["status"] == unavailable.UNAVAILABLE_COMPLETE
    assert calls == {"end": 1, "resolve": 1, "send": 1}
    assert sent["candidates"] == [
        {"party_page_id": "party-b", "telegram_user_id": 303, "telegram_chat_id": 404}
    ]
    assert sent["expected_acceptance_status"] == "대체필요"
    assert sent["expected_last_edited_time"] == "edit-after"
    assert sent["proposal_round"] == 2
    assert "stale-never-use" not in json.dumps(sent)
    persisted = json.loads(path.read_text())
    assert persisted["history_ended"] is True
    assert persisted["projection_status"] == unavailable.REPLACEMENT_REQUIRED
    assert persisted["original_cleaner_excluded"] is True


def test_no_candidate_is_successful_unavailable_and_operator_required(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    notifications = []
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **_kwargs: result())
    out = unavailable.execute_unavailable(
        record, path=path, history_store=object(), request_dir=tmp_path, now=NOW,
        validator=lambda _record: None,
        projector=lambda _record: {"next_expected_last_edited_time": "edit-after"},
        candidate_resolver=lambda _nickname: ([], []),
        assignment_sender=lambda **_kwargs: pytest.fail("must not send without candidate"),
        operator_notifier=lambda value: notifications.append(value["status"]),
    )
    assert out == {
        "status": unavailable.OPERATOR_REQUIRED,
        "assignment_end": "UPDATED",
        "projection": unavailable.REPLACEMENT_REQUIRED,
        "replacement_offer": "NO_CANDIDATE",
    }
    assert notifications == [unavailable.OPERATOR_REQUIRED]
    assert json.loads(path.read_text())["history_ended"] is True


def test_projection_failure_never_rolls_back_history_and_retry_does_not_end_twice(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    end_calls = []
    projection_calls = []
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **kw: end_calls.append(kw) or result())

    def projector(_record):
        projection_calls.append(1)
        if len(projection_calls) == 1:
            raise RuntimeError("synthetic projection response loss")
        return {"next_expected_last_edited_time": "edit-after"}

    first = unavailable.execute_unavailable(
        record, path=path, history_store=object(), request_dir=tmp_path, now=NOW,
        validator=lambda _record: None, projector=projector,
        candidate_resolver=lambda _nickname: ([], []),
        assignment_sender=lambda **_kwargs: pytest.fail("no candidate"),
    )
    assert first["status"] == unavailable.PROJECTION_RECONCILIATION_REQUIRED
    assert len(end_calls) == 1
    assert json.loads(path.read_text())["history_ended"] is True

    second = unavailable.execute_unavailable(
        record, path=path, history_store=object(), request_dir=tmp_path, now=NOW + timedelta(minutes=1),
        validator=lambda _record: pytest.fail("ended Assignment must not be revalidated as active"),
        projector=projector,
        candidate_resolver=lambda _nickname: ([], []),
        assignment_sender=lambda **_kwargs: pytest.fail("no candidate"),
    )
    assert second["status"] == unavailable.OPERATOR_REQUIRED
    assert len(end_calls) == 1
    assert len(projection_calls) == 2
    assert record["transition_at"] == NOW.isoformat()


def test_replacement_send_failure_is_recoverable_and_routing_retry_is_idempotent(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    end_calls = []
    send_calls = []
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **kw: end_calls.append(kw) or result())
    candidates = [{"party_page_id": "party-b", "telegram_user_id": 2, "telegram_chat_id": 2}]

    def sender(**kwargs):
        send_calls.append(kwargs)
        if len(send_calls) == 1:
            raise RuntimeError("synthetic route failure")
        return {"sent": False, "reason": "PENDING_REQUEST_EXISTS", "action_id": "replacement-existing"}

    kwargs = dict(
        record=record, path=path, history_store=object(), request_dir=tmp_path,
        validator=lambda _record: None,
        projector=lambda _record: {"next_expected_last_edited_time": "edit-after"},
        candidate_resolver=lambda _nickname: (candidates, []), assignment_sender=sender,
    )
    first = unavailable.execute_unavailable(now=NOW, **kwargs)
    assert first["status"] == unavailable.REPLACEMENT_ROUTING_REQUIRED
    second = unavailable.execute_unavailable(now=NOW + timedelta(minutes=1), **kwargs)
    assert second["status"] == unavailable.UNAVAILABLE_COMPLETE
    assert second["replacement_offer"] == "PENDING_REQUEST_EXISTS"
    assert len(end_calls) == 1
    assert len(send_calls) == 2


def test_double_final_execution_has_exactly_one_end_and_one_replacement_effect(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    ends, sends = [], []
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **kw: ends.append(kw) or result())
    kwargs = dict(
        record=record, path=path, history_store=object(), request_dir=tmp_path,
        validator=lambda _record: None,
        projector=lambda _record: {"next_expected_last_edited_time": "edit-after"},
        candidate_resolver=lambda _nickname: ([{"party_page_id": "party-b"}], []),
        assignment_sender=lambda **kw: sends.append(kw) or {"sent": True, "action_id": "replacement"},
    )
    one = unavailable.execute_unavailable(now=NOW, **kwargs)
    two = unavailable.execute_unavailable(now=NOW + timedelta(seconds=1), **kwargs)
    assert one == two
    assert len(ends) == 1
    assert len(sends) == 1


def test_assignment_changed_after_ui_render_fails_before_terminal_mutation(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    ends = []
    monkeypatch.setattr(
        unavailable,
        "_assert_exact_history_binding",
        lambda *_a, **_k: (_ for _ in ()).throw(unavailable.CleanerUnavailableError("assignment changed")),
    )
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **kw: ends.append(kw) or result())
    with pytest.raises(unavailable.CleanerUnavailableError):
        unavailable.execute_unavailable(
            record, path=path, history_store=object(), request_dir=tmp_path, now=NOW,
            validator=lambda _record: None,
            projector=lambda _record: pytest.fail("projection must not run"),
            candidate_resolver=lambda _nickname: pytest.fail("routing must not run"),
            assignment_sender=lambda **_kwargs: pytest.fail("send must not run"),
        )
    assert ends == []
    persisted = json.loads(path.read_text())
    assert persisted.get("history_ended") is not True
    assert "transition_at" not in persisted
    assert "end_key" not in persisted


def test_notification_failure_after_durable_no_candidate_does_not_rollback(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **_kw: result())
    out = unavailable.execute_unavailable(
        record, path=path, history_store=object(), request_dir=tmp_path, now=NOW,
        validator=lambda _record: None,
        projector=lambda _record: {"next_expected_last_edited_time": "edit-after"},
        candidate_resolver=lambda _nickname: ([], []),
        assignment_sender=lambda **_kwargs: None,
        operator_notifier=lambda _record: (_ for _ in ()).throw(RuntimeError("notify down")),
    )
    assert out["status"] == unavailable.OPERATOR_REQUIRED
    persisted = json.loads(path.read_text())
    assert persisted["history_ended"] is True
    assert persisted["notification_status"] == unavailable.NOTIFICATION_RETRY_REQUIRED


class FactStore:
    def __init__(self, *, fail_reason=False):
        self.reason_calls = []
        self.request_calls = []
        self.fail_reason = fail_reason

    def capture_unavailable_reason(self, page_id, **kwargs):
        self.reason_calls.append((page_id, kwargs))
        if self.fail_reason:
            raise RuntimeError("schema/write unavailable")
        return {"id": page_id}

    def record_reassignment_request(self, page_id, **kwargs):
        self.request_calls.append((page_id, kwargs))
        return {"id": page_id}


def accepted_history_row(version, accepted_at, *, cleaning_page_id="cleaning-a"):
    return {
        "id": f"history-{version}",
        "properties": {
            "데이터 환경": history._select_property("PRODUCTION"),
            "제안 상태": history._select_property("ACCEPTED"),
            "Cleaning": history._relation_property(cleaning_page_id),
            "Assignment Version": history._rich_text_property(version),
            "응답 시각": history._date_property(accepted_at),
        },
    }


def ended_unavailable_history_row(
    *, history_page_id="history-a", ended_at=None, assignment_version="assign-a",
    cleaner_party_page_id="party-a", action_id="assign-a", idempotency_key="accept-key-a",
    end_key="cleaner-unavailable:unavailable-a", cleaning_page_id="cleaning-a",
):
    ended_at = ended_at or (NOW - timedelta(minutes=30))
    return {
        "id": history_page_id,
        "properties": {
            "데이터 환경": history._select_property("PRODUCTION"),
            "제안 상태": history._select_property("ACCEPTED"),
            "Hold 상태": history._select_property("RELEASED"),
            "Cleaning": history._relation_property(cleaning_page_id),
            "후보 인력": history._relation_property(cleaner_party_page_id),
            "Assignment Version": history._rich_text_property(assignment_version),
            "Action ID": history._rich_text_property(action_id),
            "Idempotency Key": history._rich_text_property(idempotency_key),
            "Binding Offer": history._checkbox_property(True),
            "Assignment End Reason": history._select_property("CLEANER_UNAVAILABLE"),
            "Assignment Ended At": history._date_property(ended_at),
            "Assignment End Key": history._rich_text_property(end_key),
        },
    }


def predecessor_reassignment_record(action_id="reassign-predecessor"):
    value = reassignment_record(action_id)
    value.pop("original_ended_at")
    return value


class ReassignmentStore(FactStore):
    def __init__(self, accepted_rows=(), generation_rows=(), *, generation_failures=0):
        super().__init__()
        self.accepted_rows = list(accepted_rows)
        self.generation_rows = list(generation_rows)
        self.accepted_query_calls = 0
        self.generation_query_calls = 0
        self.generation_failures = generation_failures

    def query_assignment_generation(self, *, cleaning_page_id, assignment_version):
        self.generation_query_calls += 1
        if self.generation_failures:
            self.generation_failures -= 1
            raise history.AssignmentHistoryError(
                history.REMOTE_NOTION_FAILURE, "synthetic canonical read failure"
            )
        return list(self.generation_rows)

    def query_accepted_for_cleaning(self, *, cleaning_page_id):
        self.accepted_query_calls += 1
        return list(self.accepted_rows)


def ended_record():
    value = action_record()
    value.update({
        "history_ended": True,
        "end_key": "cleaner-unavailable:unavailable-a",
        "transition_at": NOW.isoformat(),
        "status": unavailable.UNAVAILABLE_COMPLETE,
    })
    return value


def test_optional_reason_is_post_transition_skip_safe_and_failure_never_rolls_back(tmp_path):
    path, record = write_action(tmp_path, ended_record())
    store = FactStore()
    captured = unavailable.capture_reason(
        record, path=path, operation="reason_health", history_store=store, now=NOW + timedelta(minutes=2)
    )
    assert captured == {"status": "CAPTURED", "reason": "건강/응급"}
    assert store.reason_calls[0][1]["end_key"] == "cleaner-unavailable:unavailable-a"

    path2, record2 = write_action(tmp_path, {**ended_record(), "action_id": "unavailable-b"})
    skipped = unavailable.capture_reason(record2, path=path2, operation="reason_skip", history_store=FactStore(), now=NOW)
    assert skipped == {"status": "SKIPPED"}

    path3, record3 = write_action(tmp_path, {**ended_record(), "action_id": "unavailable-c"})
    failing = FactStore(fail_reason=True)
    failed = unavailable.capture_reason(record3, path=path3, operation="reason_personal", history_store=failing, now=NOW)
    assert failed["status"] == unavailable.REASON_RETRY_REQUIRED
    assert json.loads(path3.read_text())["history_ended"] is True


def test_cleaner_result_notification_failure_preserves_ended_fact_and_records_retry(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **_kw: result())

    def failing_api(_token, _method, **_values):
        raise RuntimeError("telegram unavailable")

    with pytest.raises(RuntimeError, match="telegram unavailable"):
        unavailable.handle_unavailable_callback(
            signed_update(record["action_id"], "confirm", secret), "token",
            request_api=failing_api, request_dir=tmp_path, secret_path=secret,
            roster_loader=lambda: roster(), history_store=object(), now=NOW,
            validator=lambda _record: None,
            projector=lambda _record: {"next_expected_last_edited_time": "edit-after"},
            candidate_resolver=lambda _nickname: ([], []),
            assignment_sender=lambda **_kwargs: pytest.fail("no candidate"),
            operator_notifier=lambda _record: None,
        )
    persisted = json.loads(path.read_text())
    assert persisted["history_ended"] is True
    assert persisted["status"] == unavailable.OPERATOR_REQUIRED
    assert persisted["notification_status"] == unavailable.NOTIFICATION_RETRY_REQUIRED


def test_reason_before_assignment_end_is_rejected(tmp_path):
    path, record = write_action(tmp_path)
    with pytest.raises(unavailable.CleanerUnavailableError):
        unavailable.capture_reason(record, path=path, operation="reason_personal", history_store=FactStore(), now=NOW)


def projected_cleaning(*, acceptance="대체필요", assignees=(), state="담당자 배정", edited="edit-after"):
    return {
        "id": "cleaning-a",
        "last_edited_time": edited,
        "properties": {
            "데이터 환경": {"select": {"name": "PRODUCTION"}},
            "등록 상태": {"select": {"name": "APPROVED"}},
            "상태": {"select": {"name": state}},
            "배정 수락 상태": {"select": {"name": acceptance}},
            "담당 참여자/업체": {"relation": [{"id": value} for value in assignees]},
        },
    }


def reassignment_record(action_id="reassign-a"):
    return {
        "action_id": action_id,
        "action_type": "CLEANER_REASSIGNMENT_REQUEST",
        "status": "PENDING",
        "cleaning_page_id": "cleaning-a",
        "cleaner_party_page_id": "party-a",
        "telegram_user_id": 101,
        "telegram_chat_id": 202,
        "history_page_id": "history-a",
        "accepted_assignment_action_id": "assign-a",
        "assignment_version": "assign-a",
        "acceptance_idempotency_key": "accept-key-a",
        "end_key": "cleaner-unavailable:unavailable-a",
        "original_ended_at": (NOW - timedelta(minutes=30)).isoformat(),
        "expected_cleaning_last_edited_time": "edit-after",
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=24)).isoformat(),
        "business_mutations": 0,
    }


def test_predecessor_reassignment_direct_callback_hydrates_19_then_blocks_stale_08_and_replay(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    record = predecessor_reassignment_record("reassign-predecessor-stale")
    path, record = write_action(tmp_path, record)
    original_ended_at = NOW - timedelta(minutes=30)
    store = ReassignmentStore(
        accepted_rows=[accepted_history_row("assign-b", original_ended_at + timedelta(minutes=10))],
        generation_rows=[ended_unavailable_history_row(ended_at=original_ended_at)],
    )
    api = CaptureApi()

    first = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW,
        page_reader=lambda _id: projected_cleaning(),
        projector=lambda *_a, **_k: pytest.fail("assignment mutation must remain zero"),
        assignment_sender=lambda *_a, **_k: pytest.fail("pending offer mutation must remain zero"),
    )

    assert first == "unavailable_stale_fail_closed"
    assert store.generation_query_calls == 1
    assert store.accepted_query_calls == 1
    assert len(store.request_calls) == 0
    persisted = json.loads(path.read_text())
    assert datetime.fromisoformat(persisted["original_ended_at"]) == original_ended_at
    assert persisted["status"] == unavailable.REASSIGNMENT_STALE
    assert persisted["business_mutations"] == 0
    assert "invalidated_at" in persisted

    query_counts = (store.generation_query_calls, store.accepted_query_calls)
    message_count = len(api.calls)
    replay = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW + timedelta(minutes=1),
        page_reader=lambda _id: projected_cleaning(),
    )
    assert replay == "unavailable_stale_fail_closed"
    assert (store.generation_query_calls, store.accepted_query_calls) == query_counts
    assert len(store.request_calls) == 0
    assert len(api.calls) == message_count


def test_predecessor_reassignment_direct_callback_without_newer_accept_writes_exactly_once(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    record = predecessor_reassignment_record("reassign-predecessor-valid")
    path, record = write_action(tmp_path, record)
    store = ReassignmentStore(
        generation_rows=[ended_unavailable_history_row()],
    )
    api = CaptureApi()

    first = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW,
        page_reader=lambda _id: projected_cleaning(),
    )
    second = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW + timedelta(minutes=1),
        page_reader=lambda _id: projected_cleaning(),
    )

    assert first == "unavailable_reassignment_requested"
    assert second == "unavailable_reassignment_requested"
    assert store.generation_query_calls == 1
    assert store.accepted_query_calls == 1
    assert len(store.request_calls) == 1
    persisted = json.loads(path.read_text())
    assert "original_ended_at" in persisted
    assert persisted["status"] == unavailable.REASSIGNMENT_REQUESTED
    assert persisted["business_mutations"] == 1


def test_predecessor_reassignment_newer_accepted_later_ended_remains_blocked(tmp_path):
    record = predecessor_reassignment_record("reassign-predecessor-ended-replacement")
    path, record = write_action(tmp_path, record)
    original_ended_at = NOW - timedelta(minutes=30)
    replacement = accepted_history_row("assign-b", original_ended_at + timedelta(minutes=5))
    replacement["properties"]["Hold 상태"] = history._select_property("RELEASED")
    replacement["properties"]["Assignment Ended At"] = history._date_property(NOW - timedelta(minutes=1))
    store = ReassignmentStore(
        accepted_rows=[replacement],
        generation_rows=[ended_unavailable_history_row(ended_at=original_ended_at)],
    )

    result = unavailable.execute_reassignment_request(
        record, path=path, history_store=store,
        page_reader=lambda _id: projected_cleaning(), now=NOW,
    )

    assert result["status"] == unavailable.REASSIGNMENT_STALE
    assert store.generation_query_calls == 1
    assert store.accepted_query_calls == 1
    assert len(store.request_calls) == 0


@pytest.mark.parametrize(
    ("generation_rows", "expected_reason"),
    [
        ([], "ORIGINAL_ASSIGNMENT_NOT_FOUND"),
        (
            [
                ended_unavailable_history_row(),
                ended_unavailable_history_row(history_page_id="history-duplicate"),
            ],
            "ORIGINAL_ASSIGNMENT_AMBIGUOUS",
        ),
    ],
)
def test_predecessor_reassignment_missing_or_ambiguous_original_fails_closed_terminal(
    tmp_path, generation_rows, expected_reason
):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    record = predecessor_reassignment_record(f"reassign-{expected_reason.lower()}")
    path, record = write_action(tmp_path, record)
    store = ReassignmentStore(generation_rows=generation_rows)
    api = CaptureApi()

    out = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW,
        page_reader=lambda _id: projected_cleaning(),
    )

    assert out == "unavailable_stale_fail_closed"
    assert store.generation_query_calls == 1
    assert store.accepted_query_calls == 0
    assert len(store.request_calls) == 0
    persisted = json.loads(path.read_text())
    assert persisted["status"] == unavailable.REASSIGNMENT_STALE
    assert persisted["invalidation_reason"] == expected_reason
    assert "invalidated_at" in persisted
    assert persisted["business_mutations"] == 0


def test_predecessor_reassignment_transient_canonical_read_is_retryable_then_recovers(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    record = predecessor_reassignment_record("reassign-predecessor-retry")
    path, record = write_action(tmp_path, record)
    store = ReassignmentStore(
        generation_rows=[ended_unavailable_history_row()], generation_failures=1,
    )
    api = CaptureApi()

    first = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW,
        page_reader=lambda _id: projected_cleaning(),
    )
    after_failure = json.loads(path.read_text())
    assert first == "unavailable_reassignment_authority_retry_required"
    assert after_failure["status"] == unavailable.REASSIGNMENT_AUTHORITY_RETRY_REQUIRED
    assert after_failure["authority_retry_error_code"] == history.REMOTE_NOTION_FAILURE
    assert "invalidated_at" not in after_failure
    assert "original_ended_at" not in after_failure
    assert store.generation_query_calls == 1
    assert store.accepted_query_calls == 0
    assert len(store.request_calls) == 0

    second = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW + timedelta(minutes=1),
        page_reader=lambda _id: projected_cleaning(),
    )
    assert second == "unavailable_reassignment_requested"
    assert store.generation_query_calls == 2
    assert store.accepted_query_calls == 1
    assert len(store.request_calls) == 1
    recovered = json.loads(path.read_text())
    assert recovered["status"] == unavailable.REASSIGNMENT_REQUESTED
    assert "original_ended_at" in recovered
    assert "invalidated_at" not in recovered


def test_stale_reassignment_callback_fails_closed_when_newer_accepted_exists_in_19(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    path, record = write_action(tmp_path, reassignment_record())
    store = ReassignmentStore([
        accepted_history_row(
            "assign-b",
            datetime.fromisoformat(record["original_ended_at"]) + timedelta(minutes=10),
        )
    ])
    api = CaptureApi()

    out = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW,
        page_reader=lambda _id: projected_cleaning(),
        projector=lambda *_a, **_k: pytest.fail("assignment projection mutation must remain zero"),
        assignment_sender=lambda *_a, **_k: pytest.fail("pending offer/assignment mutation must remain zero"),
    )

    assert out == "unavailable_stale_fail_closed"
    assert store.accepted_query_calls >= 1
    assert len(store.request_calls) == 0
    persisted = json.loads(path.read_text())
    assert persisted["business_mutations"] == 0
    assert persisted["status"] == unavailable.REASSIGNMENT_STALE
    assert "대체 담당자가 이미 확정" in api.calls[-1][1]["text"]

    query_count = store.accepted_query_calls
    message_count = len(api.calls)
    replay = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW + timedelta(minutes=1),
        page_reader=lambda _id: projected_cleaning(),
    )
    assert replay == "unavailable_stale_fail_closed"
    assert store.accepted_query_calls == query_count
    assert len(store.request_calls) == 0
    assert len(api.calls) == message_count


def test_reassignment_request_is_idempotent_fact_only_and_never_self_restores(tmp_path):
    path, record = write_action(tmp_path, reassignment_record())
    store = ReassignmentStore()
    page = projected_cleaning()
    first = unavailable.execute_reassignment_request(
        record, path=path, history_store=store, page_reader=lambda _id: page, now=NOW
    )
    second = unavailable.execute_reassignment_request(
        record, path=path, history_store=store, page_reader=lambda _id: page, now=NOW + timedelta(minutes=1)
    )
    assert first == {
        "status": unavailable.REASSIGNMENT_REQUESTED,
        "assignment_changes": 0,
        "offer_cancellations": 0,
        "replayed": False,
    }
    assert second == {
        "status": unavailable.REASSIGNMENT_REQUESTED,
        "assignment_changes": 0,
        "offer_cancellations": 0,
        "replayed": True,
    }
    assert store.accepted_query_calls == 1
    assert len(store.request_calls) == 1
    assert json.loads(path.read_text())["business_mutations"] == 1


def test_reassignment_stays_blocked_when_newer_accepted_later_ended(tmp_path):
    path, record = write_action(tmp_path, reassignment_record("reassign-ended"))
    row = accepted_history_row(
        "assign-b", datetime.fromisoformat(record["original_ended_at"]) + timedelta(minutes=5)
    )
    row["properties"]["Hold 상태"] = history._select_property("RELEASED")
    row["properties"]["Assignment Ended At"] = history._date_property(NOW - timedelta(minutes=1))
    store = ReassignmentStore([row])

    result = unavailable.execute_reassignment_request(
        record, path=path, history_store=store,
        page_reader=lambda _id: projected_cleaning(), now=NOW,
    )

    assert result["status"] == unavailable.REASSIGNMENT_STALE
    assert len(store.request_calls) == 0
    assert json.loads(path.read_text())["business_mutations"] == 0


def test_reassignment_cancelled_cleaning_remains_fail_closed(tmp_path):
    path, record = write_action(tmp_path, reassignment_record("reassign-cancelled"))
    store = ReassignmentStore()
    with pytest.raises(unavailable.CleanerUnavailableError, match="no longer eligible"):
        unavailable.execute_reassignment_request(
            record, path=path, history_store=store,
            page_reader=lambda _id: projected_cleaning(state="취소"), now=NOW,
        )
    assert store.accepted_query_calls == 0
    assert len(store.request_calls) == 0
    assert json.loads(path.read_text())["business_mutations"] == 0


def test_reassignment_button_eligibility_survives_sent_offer_but_disappears_after_accept(monkeypatch):
    fact = {
        "history_page_id": "history-a",
        "cleaning_page_id": "cleaning-a",
        "cleaner_party_page_id": "party-a",
        "assignment_version": "assign-a",
        "assignment_action_id": "assign-a",
        "acceptance_idempotency_key": "accept-key-a",
        "ended_at": NOW.isoformat(),
        "end_key": "cleaner-unavailable:unavailable-a",
        "reassignment_request_status": None,
        "reassignment_requested_at": None,
        "reassignment_request_key": None,
    }
    monkeypatch.setattr(unavailable, "recent_unavailable_assignments", lambda **_kw: [fact])
    monkeypatch.setattr(unavailable, "newer_accepted_assignment_exists", lambda **_kw: False)

    base = projected_cleaning()
    base["properties"].update({
        "연결 집": {"relation": [{"id": "house-jj"}]},
        "점검일": {"date": {"start": "2026-09-08"}},
        "시작 예정": {"date": {"start": "2026-09-08T11:00:00+09:00"}},
        "완료 목표": {"date": {"start": "2026-09-08T15:00:00+09:00"}},
    })
    pending = unavailable.recent_change_items(
        cleaner=CLEANER, history_store=object(), page_reader=lambda _id: base,
        now=NOW, property_mappings={"house-jj": "JJ"},
    )
    assert pending[0]["can_reassign"] is True
    assert pending[0]["recent_status_text"] == "대체 담당자 확인 중"

    accepted = json.loads(json.dumps(base))
    accepted["properties"]["배정 수락 상태"] = {"select": {"name": "수락"}}
    accepted["properties"]["담당 참여자/업체"] = {"relation": [{"id": "party-b"}]}
    done = unavailable.recent_change_items(
        cleaner=CLEANER, history_store=object(), page_reader=lambda _id: accepted,
        now=NOW, property_mappings={"house-jj": "JJ"},
    )
    assert done[0]["can_reassign"] is False
    assert done[0]["recent_status_text"] == "대체 담당자 배정 완료"
    assert "party-b" not in repr(done)

    # Once canonical 19 proves a newer replacement was ACCEPTED, the original
    # Cleaner must never regain eligibility even if that replacement later ends
    # and 08 returns to replacement-required.
    monkeypatch.setattr(unavailable, "newer_accepted_assignment_exists", lambda **_kw: True)
    historical_accept = unavailable.recent_change_items(
        cleaner=CLEANER, history_store=object(), page_reader=lambda _id: base,
        now=NOW, property_mappings={"house-jj": "JJ"},
    )
    assert historical_accept[0]["can_reassign"] is False
    assert historical_accept[0]["recent_status_text"] == "대체 담당자 배정 완료"


def signed_update(action_id, operation, secret_path, *, user_id=101, chat_id=202, chat_type="private"):
    sig = unavailable.signature(secret_path.read_text().strip().encode(), action_id, operation)
    return {
        "callback_query": {
            "from": {"id": user_id, "is_bot": False},
            "message": {"chat": {"id": chat_id, "type": chat_type}},
            "data": f"u:{action_id}:{operation}:{sig}",
        }
    }


class CaptureApi:
    def __init__(self):
        self.calls = []

    def __call__(self, _token, method, **values):
        self.calls.append((method, values))
        return {"message_id": len(self.calls)}


def test_first_click_opens_exact_two_step_confirmation_with_zero_business_mutation(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    path, record = write_action(tmp_path)
    record["status"] = "PENDING"
    path.write_text(json.dumps(record))
    api = CaptureApi()
    reads = []
    monkeypatch.setattr(unavailable, "_assert_exact_history_binding", lambda *_a, **_k: reads.append("history-read"))
    out = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "open", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=object(), now=NOW,
        validator=lambda _record: reads.append("cleaning-reservation-read"),
    )
    assert out == "unavailable_confirmation_open"
    assert reads == ["cleaning-reservation-read", "history-read"]
    persisted = json.loads(path.read_text())
    assert persisted["business_mutations"] == 0
    markup = json.loads(api.calls[-1][1]["reply_markup"])
    labels = [button["text"] for button in markup["inline_keyboard"][0]]
    assert labels == ["네, 진행 불가", "일정 유지"]
    assert "정말 이 청소를 진행하기 어려우신가요?" in api.calls[-1][1]["text"]


def test_inactive_cleaner_fails_callback_authorization(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    _path, record = write_action(tmp_path)
    inactive = {**CLEANER, "status": "INACTIVE"}
    api = CaptureApi()
    out = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "open", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(inactive), history_store=object(), now=NOW,
        validator=lambda _record: pytest.fail("inactive Cleaner must fail before business validation"),
    )
    assert out == "unavailable_stale_fail_closed"
    assert json.loads((tmp_path / f"{record['action_id']}.json").read_text())["business_mutations"] == 0


def test_schedule_keep_is_zero_business_mutation(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    path, record = write_action(tmp_path)
    api = CaptureApi()
    out = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "keep", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=object(), now=NOW,
        validator=lambda _record: pytest.fail("keep must not validate/mutate business data"),
    )
    assert out == "unavailable_keep_no_business_mutation"
    assert json.loads(path.read_text())["business_mutations"] == 0


def test_reassignment_bad_signature_fails_before_canonical_reads(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    record = predecessor_reassignment_record("reassign-bad-signature")
    path, record = write_action(tmp_path, record)
    store = ReassignmentStore(generation_rows=[ended_unavailable_history_row()])
    api = CaptureApi()
    update = signed_update(record["action_id"], "reassign", secret)
    update["callback_query"]["data"] = f"u:{record['action_id']}:reassign:invalid-signature"

    out = unavailable.handle_unavailable_callback(
        update, "token", request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW,
        page_reader=lambda _id: projected_cleaning(),
    )

    assert out == "unavailable_stale_fail_closed"
    assert store.generation_query_calls == 0
    assert store.accepted_query_calls == 0
    assert len(store.request_calls) == 0
    assert json.loads(path.read_text())["status"] == "PENDING"


@pytest.mark.parametrize(
    "update_factory, current_time",
    [
        (lambda secret, record: signed_update(record["action_id"], "open", secret, user_id=999), NOW),
        (lambda secret, record: signed_update(record["action_id"], "open", secret, chat_id=999), NOW),
        (lambda secret, record: signed_update(record["action_id"], "open", secret), NOW + timedelta(days=2)),
    ],
)
def test_wrong_cleaner_wrong_chat_and_expired_action_fail_closed(monkeypatch, tmp_path, update_factory, current_time):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    _path, record = write_action(tmp_path)
    api = CaptureApi()
    monkeypatch.setattr(unavailable, "_assert_exact_history_binding", lambda *_a, **_k: None)
    out = unavailable.handle_unavailable_callback(
        update_factory(secret, record), "token", request_api=api,
        request_dir=tmp_path, secret_path=secret, roster_loader=lambda: roster(),
        history_store=object(), now=current_time, validator=lambda _record: None,
    )
    assert out == "unavailable_stale_fail_closed"
    assert "현재 상태가 변경되어" in api.calls[-1][1]["text"]


def test_build_schedule_ui_has_human_only_states_and_signed_buttons(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    item = {
        "page_id": "cleaning-a",
        "property_label": "JJ",
        "service_day": NOW.date(),
        "start_at": NOW,
        "end_at": NOW + timedelta(hours=4),
        "ui_state": "UPCOMING",
        "expected_cleaning_last_edited_time": "edit-before",
    }
    monkeypatch.setattr(
        unavailable, "create_unavailable_action",
        lambda *_a, **_k: {"action_id": "unavailable-a"},
    )
    monkeypatch.setattr(unavailable, "recent_change_items", lambda **_k: [])
    items, markup = unavailable.build_schedule_ui(
        cleaner=CLEANER, items=[item], history_store=object(), request_dir=tmp_path,
        secret_path=secret, now=NOW, property_mappings={"house-jj": "JJ"},
    )
    assert items[0]["human_status"] == "배정 확정"
    assert markup["inline_keyboard"][0][0]["text"] == "진행 불가 · JJ"
    rendered = repr({"items": items, "markup": markup})
    for internal in ("HARD_BOOKED", "REPLACEMENT_REQUIRED", "CLEANER_UNAVAILABLE"):
        assert internal not in rendered


def test_f01_keep_consumes_confirmation_and_old_confirm_replay_fails_closed(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    path, record = write_action(tmp_path)
    unrelated_path, unrelated = write_action(tmp_path, action_record("unavailable-other"))
    api = CaptureApi()
    end_calls = []
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **kw: end_calls.append(kw) or result())

    first = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "keep", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=object(), now=NOW,
    )
    second = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "keep", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=object(), now=NOW + timedelta(seconds=1),
    )
    replay = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "confirm", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=object(), now=NOW + timedelta(seconds=2),
        validator=lambda _record: pytest.fail("cancelled confirm must not reach business validation"),
    )

    persisted = json.loads(path.read_text())
    assert first == second == "unavailable_keep_no_business_mutation"
    assert persisted["status"] == unavailable.CONFIRMATION_CANCELLED
    assert persisted["business_mutations"] == 0
    assert persisted.get("history_ended") is not True
    assert replay == "unavailable_stale_fail_closed"
    assert end_calls == []
    assert json.loads(unrelated_path.read_text()) == unrelated


def test_f02_resolver_failure_converges_to_routing_recovery_and_retry_progresses(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    ends, resolver_calls, sends = [], [], []
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **kw: ends.append(kw) or result())

    def resolver(_nickname):
        resolver_calls.append(1)
        if len(resolver_calls) == 1:
            raise OSError("synthetic roster read failure")
        return ([{"party_page_id": "party-b", "telegram_user_id": 303, "telegram_chat_id": 404}], [])

    kwargs = dict(
        record=record, path=path, history_store=object(), request_dir=tmp_path,
        validator=lambda _record: None,
        projector=lambda _record: {"next_expected_last_edited_time": "edit-after"},
        candidate_resolver=resolver,
        assignment_sender=lambda **kw: sends.append(kw) or {"sent": True, "action_id": "replacement"},
    )
    first = unavailable.execute_unavailable(now=NOW, **kwargs)
    persisted = json.loads(path.read_text())
    assert first["status"] == unavailable.REPLACEMENT_ROUTING_REQUIRED
    assert persisted["replacement_error_stage"] == "CANDIDATE_RESOLUTION"
    assert persisted["replacement_error_type"] == "OSError"
    assert persisted["history_ended"] is True
    assert persisted["projection_status"] == unavailable.REPLACEMENT_REQUIRED
    assert sends == []

    second = unavailable.execute_unavailable(now=NOW + timedelta(minutes=1), **kwargs)
    assert second["status"] == unavailable.UNAVAILABLE_COMPLETE
    assert len(ends) == 1
    assert len(resolver_calls) == 2
    assert len(sends) == 1
    assert sends[0]["candidates"][0]["party_page_id"] == "party-b"


def _confirm_failure_case(monkeypatch, tmp_path, *, projection_failure=False):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    api = CaptureApi()
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **_kw: result())
    projector = (
        (lambda _record: (_ for _ in ()).throw(RuntimeError("projection down")))
        if projection_failure else
        (lambda _record: {"next_expected_last_edited_time": "edit-after"})
    )
    sender = (
        (lambda **_kw: pytest.fail("routing must not start after projection failure"))
        if projection_failure else
        (lambda **_kw: (_ for _ in ()).throw(RuntimeError("route down")))
    )
    out = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "confirm", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=object(), now=NOW,
        validator=lambda _record: None, projector=projector,
        candidate_resolver=lambda _nickname: ([{"party_page_id": "party-b"}], []),
        assignment_sender=sender,
    )
    return secret, path, record, api, out


@pytest.mark.parametrize("projection_failure", [True, False])
def test_f03_post_end_partial_failure_keeps_human_success_and_reason_actions(monkeypatch, tmp_path, projection_failure):
    secret, path, record, api, out = _confirm_failure_case(
        monkeypatch, tmp_path, projection_failure=projection_failure
    )
    persisted = json.loads(path.read_text())
    assert persisted["history_ended"] is True
    assert out in {
        "unavailable_projection_reconciliation_required",
        "unavailable_replacement_routing_required",
    }
    message = api.calls[-1][1]
    assert "진행 불가 처리가 완료되었습니다" in message["text"]
    assert "운영팀에서 대체 담당자를 확인하고 있습니다" in message["text"]
    assert "사유" in message["text"]
    assert "PROJECTION_RECONCILIATION_REQUIRED" not in message["text"]
    assert "REPLACEMENT_ROUTING_REQUIRED" not in message["text"]
    markup = json.loads(message["reply_markup"])
    labels = [row[0]["text"] for row in markup["inline_keyboard"]]
    assert labels == ["개인 일정", "건강/응급", "시간 문제", "기타", "건너뛰기"]

    store = FactStore()
    reason_out = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reason_personal", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW + timedelta(minutes=2),
        validator=lambda _record: pytest.fail("reason retry must not execute unavailable again"),
    )
    assert reason_out == "unavailable_reason_captured"
    assert len(store.reason_calls) == 1
    page_id, values = store.reason_calls[0]
    assert page_id == "history-a"
    assert values["cleaning_page_id"] == "cleaning-a"
    assert values["assignment_version"] == "assign-a"
    assert values["action_id"] == "assign-a"


def test_f03_reason_skip_after_partial_failure_writes_no_reason(monkeypatch, tmp_path):
    secret, _path, record, api, _out = _confirm_failure_case(
        monkeypatch, tmp_path, projection_failure=True
    )
    store = FactStore()
    skip = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reason_skip", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW + timedelta(minutes=1),
    )
    assert skip == "unavailable_reason_skipped"
    assert store.reason_calls == []


def test_f04_durable_recent_end_suppresses_stale_active_projection_before_action_creation(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    stale = {
        "page_id": "cleaning-a",
        "property_label": "JJ",
        "service_day": NOW.date(),
        "start_at": NOW,
        "end_at": NOW + timedelta(hours=4),
        "ui_state": "UPCOMING",
        "expected_cleaning_last_edited_time": "stale-edit",
    }
    recent = {
        "page_id": "cleaning-a",
        "property_label": "JJ",
        "service_day": NOW.date(),
        "start_at": None,
        "end_at": None,
        "ui_state": "UNAVAILABLE",
        "recent_status_text": "대체 담당자 확인 중",
        "can_reassign": False,
    }
    monkeypatch.setattr(unavailable, "recent_change_items", lambda **_kw: [dict(recent)])
    monkeypatch.setattr(
        unavailable, "create_unavailable_action",
        lambda *_a, **_k: pytest.fail("durably ended stale 08 item must not create a new action"),
    )
    items, markup = unavailable.build_schedule_ui(
        cleaner=CLEANER, items=[stale], history_store=object(), request_dir=tmp_path,
        secret_path=secret, now=NOW,
    )
    assert items == [recent]
    assert markup is None
    from telegram_approval.cleaner_my_schedule import render_my_schedule
    rendered = render_my_schedule(items)
    assert "최근 변경" in rendered
    assert "대체 담당자 확인 중" in rendered
    assert "예정" not in rendered
    assert "PROJECTION_RECONCILIATION_REQUIRED" not in rendered


def test_f04_replacement_accepted_recent_state_remains_human_and_non_actionable(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-secret")
    recent = {
        "page_id": "cleaning-a", "property_label": "JJ", "service_day": NOW.date(),
        "start_at": None, "end_at": None, "ui_state": "UNAVAILABLE",
        "recent_status_text": "대체 담당자 배정 완료", "can_reassign": False,
    }
    monkeypatch.setattr(unavailable, "recent_change_items", lambda **_kw: [dict(recent)])
    items, markup = unavailable.build_schedule_ui(
        cleaner=CLEANER, items=[], history_store=object(), request_dir=tmp_path,
        secret_path=secret, now=NOW,
    )
    assert items == [recent]
    assert markup is None


def test_f05_failed_final_validation_does_not_capture_transition_timestamp(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    monkeypatch.setattr(unavailable, "end_accepted_assignment", lambda **_kw: pytest.fail("end must not run"))
    with pytest.raises(ValueError, match="work already started"):
        unavailable.execute_unavailable(
            record, path=path, history_store=object(), request_dir=tmp_path, now=NOW,
            validator=lambda _record: (_ for _ in ()).throw(ValueError("work already started")),
            projector=lambda _record: pytest.fail("projection must not run"),
        )
    persisted = json.loads(path.read_text())
    assert "transition_at" not in record
    assert "end_key" not in record
    assert "transition_at" not in persisted
    assert "end_key" not in persisted


def test_f05_transition_clock_is_captured_once_after_all_final_validation(monkeypatch, tmp_path):
    path, record = write_action(tmp_path)
    write_offer(tmp_path)
    order = []
    monkeypatch.setattr(unavailable, "_assert_exact_history_binding", lambda *_a, **_k: order.append("history-validation"))

    def fake_end(**kwargs):
        order.append("end")
        assert record["transition_at"] == NOW.isoformat()
        assert kwargs["ended_at"] == NOW
        return result()

    monkeypatch.setattr(unavailable, "end_accepted_assignment", fake_end)
    first = unavailable.execute_unavailable(
        record, path=path, history_store=object(), request_dir=tmp_path, now=NOW,
        validator=lambda _record: order.append("cleaning-reservation-validation"),
        projector=lambda _record: {"next_expected_last_edited_time": "edit-after"},
        candidate_resolver=lambda _nickname: ([], []),
        assignment_sender=lambda **_kw: pytest.fail("no candidate"),
    )
    assert first["status"] == unavailable.OPERATOR_REQUIRED
    assert order == ["cleaning-reservation-validation", "history-validation", "end"]
    original_transition = record["transition_at"]
    second = unavailable.execute_unavailable(
        record, path=path, history_store=object(), request_dir=tmp_path,
        now=NOW + timedelta(hours=2),
        validator=lambda _record: pytest.fail("completed retry must not revalidate"),
    )
    assert second == first
    assert record["transition_at"] == original_transition
    assert record.get("reason_captured_at") is None


def test_reassignment_callback_queues_ops_notification_for_w02_without_ops_credentials(tmp_path):
    secret = tmp_path / "secret-w02-queue"
    secret.write_text("test-secret")
    record = predecessor_reassignment_record("reassign-w02-queue")
    path, record = write_action(tmp_path, record)
    store = ReassignmentStore(generation_rows=[ended_unavailable_history_row()])
    api = CaptureApi()

    result_value = unavailable.handle_unavailable_callback(
        signed_update(record["action_id"], "reassign", secret), "token",
        request_api=api, request_dir=tmp_path, secret_path=secret,
        roster_loader=lambda: roster(), history_store=store, now=NOW,
        page_reader=lambda _id: projected_cleaning(),
    )

    decisions = []
    for candidate in tmp_path.glob("*.json"):
        value = json.loads(candidate.read_text())
        if value.get("action_type") == "CLEANER_REASSIGNMENT_DECISION":
            decisions.append(value)
    assert result_value == "unavailable_reassignment_requested"
    assert len(decisions) == 1
    assert decisions[0]["status"] == "PENDING"
    assert decisions[0]["notification_status"] == "PENDING"
    assert decisions[0]["business_mutations"] == 0
