import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from telegram_approval import bot_service, cleaner_application, cleaner_jobs


NOW = datetime(2026, 8, 27, 7, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _stable_property_mappings(monkeypatch):
    mappings = {"house-jj": "JJ", "house-xx": "XX"}
    monkeypatch.setattr(cleaner_application, "load_property_mappings", lambda: mappings)
    monkeypatch.setattr(cleaner_jobs, "load_property_mappings", lambda: mappings)


def _rich(value):
    return {"type": "title", "title": [{"plain_text": value}]}


def _select(value):
    return {"type": "select", "select": {"name": value} if value is not None else None}


def _date(value):
    return {"type": "date", "date": {"start": value} if value is not None else None}


def _number(value):
    return {"type": "number", "number": value}


def cleaning_page(
    page_id="cleaning-1",
    *,
    nickname="JJ",
    strategy="OPEN_APPLICATION",
    environment="PRODUCTION",
    registration="APPROVED",
    state="예정",
    acceptance="미제안",
    assignment_state="OPEN",
    property_page_ids=("house-jj",),
    assignee_ids=(),
):
    return {
        "id": page_id,
        "properties": {
            "점검명": _rich(f"{nickname} · 퇴실청소"),
            "점검 유형": _select("퇴실청소"),
            "점검일": _date("2026-08-30"),
            "시작 예정": _date("2026-08-30T11:00:00+09:00"),
            "완료 목표": _date("2026-08-30T15:00:00+09:00"),
            "청소비 Snapshot": _number(60000),
            "배정 전략 Snapshot": _select(strategy),
            "데이터 환경": _select(environment),
            "등록 상태": _select(registration),
            "상태": _select(state),
            "배정 수락 상태": _select(acceptance),
            "배정 상태": _select(assignment_state),
            "연결 집": {"type": "relation", "relation": [{"id": value} for value in property_page_ids]},
            "담당 참여자/업체": {
                "type": "relation", "relation": [{"id": value} for value in assignee_ids]
            },
            "주소": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_STREET_ADDRESS"}]},
            "출입 코드": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_DOOR_CODE"}]},
            "Guest PII": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_GUEST_PII"}]},
            "예약번호": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_BOOKING_REF"}]},
            "담당자": {"type": "rich_text", "rich_text": [{"plain_text": "OTHER_CLEANER"}]},
            "배정 Tier": _select("SECRET_RANK"),
            "예상 총 지급액": _number(999999),
        },
    }


def cleaner(
    *,
    user_id=202,
    chat_id=202,
    status="ACTIVE",
    role="CLEANER",
    properties=None,
    party_page_id="party-cleaner",
    identity_id="worker",
):
    return {
        "identity_id": identity_id,
        "role": role,
        "status": status,
        "telegram_user_id": user_id,
        "telegram_chat_id": chat_id,
        "party_page_id": party_page_id,
        "properties": ["JJ"] if properties is None else properties,
        "priority_by_property": {"JJ": 1},
    }


def roster(*entries):
    return {"schema_version": 1, "cleaners": list(entries)}


def callback_update(
    page_id="cleaning-1",
    *,
    user_id=202,
    chat_id=202,
    callback_id="cb-1",
    message_id=10,
    chat_type="private",
):
    return {
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id, "is_bot": False},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": chat_type},
            },
            "data": cleaner_application.application_callback_data(page_id),
        }
    }


def application_row(
    *,
    key=None,
    cleaning_page_id="cleaning-1",
    cleaner_party_page_id="party-cleaner",
    status="APPLIED",
    environment="PRODUCTION",
):
    key = key or cleaner_application.application_idempotency_key(
        cleaning_page_id, cleaner_party_page_id
    )
    return {
        "id": "application-row",
        "properties": {
            "Idempotency Key": {
                "type": "rich_text",
                "rich_text": [{"plain_text": key}],
            },
            "Cleaning": {"type": "relation", "relation": [{"id": cleaning_page_id}]},
            "Cleaner": {"type": "relation", "relation": [{"id": cleaner_party_page_id}]},
            "Application Status": _select(status),
            "Data Environment": _select(environment),
        },
    }


class FakeSource:
    def __init__(self, pages=None, *, fail=False):
        self.pages = pages or {}
        self.fail = fail
        self.calls = []

    def get_cleaning(self, page_id):
        self.calls.append(page_id)
        if self.fail:
            raise RuntimeError("synthetic cleaning read failure")
        return self.pages[page_id]


class FakeLedger:
    def __init__(self, rows=None, *, query_failure_at=None, create_mode="normal"):
        self.rows = list(rows or [])
        self.query_failure_at = query_failure_at
        self.create_mode = create_mode
        self.query_count = 0
        self.create_count = 0
        self.created = []
        self._guard = threading.Lock()

    def query_by_idempotency(self, key):
        with self._guard:
            self.query_count += 1
            count = self.query_count
            rows = list(self.rows)
        if self.query_failure_at == count:
            raise RuntimeError("synthetic application query failure")
        return rows

    def create_application(self, **values):
        with self._guard:
            self.create_count += 1
            self.created.append(dict(values))
        if self.create_mode == "raise_before":
            raise TimeoutError("synthetic create failure")
        row = application_row(
            key=values["key"],
            cleaning_page_id=values["cleaning_page_id"],
            cleaner_party_page_id=values["cleaner_party_page_id"],
        )
        if self.create_mode == "invalid_readback":
            row = application_row(
                key=values["key"],
                cleaning_page_id=values["cleaning_page_id"],
                cleaner_party_page_id="wrong-party",
            )
        with self._guard:
            self.rows.append(row)
        if self.create_mode == "append_then_raise":
            raise TimeoutError("synthetic ambiguous create result")
        return {"id": "application-row"}


class CaptureApi:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, **values):
        self.calls.append((token, method, values))
        return {"message_id": len(self.calls)}

    @property
    def sent_texts(self):
        return [values["text"] for _token, method, values in self.calls if method == "sendMessage"]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _run_callback(
    *,
    update=None,
    roster_value=None,
    page=None,
    ledger=None,
):
    page = page or cleaning_page()
    source = FakeSource({page["id"]: page})
    ledger = ledger or FakeLedger()
    api = CaptureApi()
    result = cleaner_application.handle_application_callback(
        update or callback_update(page["id"]),
        "token",
        request_api=api,
        source=source,
        ledger=ledger,
        roster_loader=lambda: roster_value or roster(cleaner()),
        now=NOW,
    )
    return result, source, ledger, api


def test_application_action_is_exposed_only_for_explicit_open_application():
    jobs = [
        {"page_id": "open-1", "visibility": "EXPLICIT_OPEN_APPLICATION"},
        {"page_id": "offer-1", "visibility": "MY_ACTIVE_OFFER"},
    ]
    keyboard = json.loads(cleaner_application.application_reply_markup(jobs))
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    assert buttons == [{"text": "신청 · 1번", "callback_data": "ca1:open-1"}]
    assert "offer-1" not in json.dumps(keyboard)
    assert cleaner_application.application_reply_markup([
        {"page_id": "offer-1", "visibility": "MY_ACTIVE_OFFER"}
    ]) is None


def test_callback_payload_contains_only_cleaning_reference_and_no_sensitive_or_cleaner_state():
    data = cleaner_application.application_callback_data("cleaning-1")
    assert data == "ca1:cleaning-1"
    forbidden = [
        "party-cleaner", "202", "SECRET_STREET_ADDRESS", "SECRET_DOOR_CODE",
        "SECRET_GUEST_PII", "SECRET_BOOKING_REF", "SECRET_RANK", "60000",
    ]
    assert all(value not in data for value in forbidden)


def test_jobs_with_open_application_gets_application_keyboard_but_offer_only_does_not(tmp_path):
    api = CaptureApi()
    open_page = cleaning_page("open-1")

    class JobsSource:
        def query_open_applications(self):
            return [open_page]

        def get_cleaning(self, _page_id):
            raise AssertionError("no targeted offer expected")

    result = cleaner_jobs.handle_jobs(
        {
            "message": {
                "from": {"id": 202, "is_bot": False},
                "chat": {"id": 202, "type": "private"},
                "text": "/jobs",
            }
        },
        "token",
        request_dir=tmp_path,
        request_api=api,
        source=JobsSource(),
        roster_loader=lambda: roster(cleaner()),
        now=NOW,
    )
    assert result == "jobs_listed"
    values = api.calls[-1][2]
    assert json.loads(values["reply_markup"])["inline_keyboard"][0][0]["callback_data"] == "ca1:open-1"
    assert "SECRET_" not in values["text"]
    assert "SECRET_" not in values["reply_markup"]


@pytest.mark.parametrize(
    "roster_value,user_id,chat_id",
    [
        (roster(), 999, 999),
        (roster(cleaner(status="INACTIVE")), 202, 202),
        (roster(cleaner()), 999, 202),
        (roster(cleaner()), 202, 999),
        (roster(cleaner(), cleaner(identity_id="duplicate")), 202, 202),
    ],
    ids=["unknown", "inactive", "wrong-user", "wrong-chat", "ambiguous"],
)
def test_unauthorized_or_ambiguous_cleaner_fails_before_cleaning_or_ledger_read(
    roster_value, user_id, chat_id
):
    source = FakeSource({"cleaning-1": cleaning_page()})
    ledger = FakeLedger()
    api = CaptureApi()
    result = cleaner_application.handle_application_callback(
        callback_update(user_id=user_id, chat_id=chat_id),
        "token",
        request_api=api,
        source=source,
        ledger=ledger,
        roster_loader=lambda: roster_value,
        now=NOW,
    )
    assert result == "application_unauthorized"
    assert source.calls == []
    assert ledger.query_count == 0
    assert ledger.create_count == 0
    assert all("cleaning-1" not in text for text in api.sent_texts)


@pytest.mark.parametrize(
    "page",
    [
        cleaning_page(property_page_ids=("house-xx",), nickname="XX"),
        cleaning_page(environment="TEST"),
        cleaning_page(registration="REVIEW_REQUIRED"),
        cleaning_page(state="취소"),
        cleaning_page(state="관리자 확인 완료"),
        cleaning_page(assignment_state="HARD_BOOKED"),
        cleaning_page(acceptance="수락"),
        cleaning_page(assignee_ids=("other-party",)),
        cleaning_page(strategy="SEQUENTIAL_PRIORITY"),
    ],
    ids=[
        "wrong-property", "test", "not-approved", "cancelled", "completed",
        "hard-booked", "finally-accepted", "conflicting-assignee", "not-open-application",
    ],
)
def test_fresh_cleaning_ineligibility_returns_unavailable_and_zero_application_create(page):
    result, source, ledger, api = _run_callback(page=page)
    assert result == "application_unavailable"
    assert source.calls == [page["id"]]
    assert ledger.query_count == 0
    assert ledger.create_count == 0
    assert api.sent_texts[-1] == cleaner_application.UNAVAILABLE_MESSAGE


def test_tampered_cleaning_reference_cannot_bypass_property_authorization():
    page = cleaning_page(
        "tampered-cleaning", nickname="XX", property_page_ids=("house-xx",)
    )
    result, _source, ledger, _api = _run_callback(
        update=callback_update("tampered-cleaning"), page=page
    )
    assert result == "application_unavailable"
    assert ledger.create_count == 0


def test_stale_open_application_closed_before_click_writes_nothing():
    page = cleaning_page(state="취소")
    result, source, ledger, api = _run_callback(page=page)
    assert source.calls == ["cleaning-1"]
    assert result == "application_unavailable"
    assert ledger.create_count == 0
    assert api.sent_texts[-1] == cleaner_application.UNAVAILABLE_MESSAGE


def test_valid_first_application_creates_exactly_one_applied_production_row_server_side():
    result, source, ledger, api = _run_callback()
    assert result == "application_created"
    assert source.calls == ["cleaning-1"]
    assert ledger.create_count == 1
    created = ledger.created[0]
    assert created["cleaning_page_id"] == "cleaning-1"
    assert created["cleaner_party_page_id"] == "party-cleaner"
    assert created["key"] == "CLEANING_JOB_APPLICATION:cleaning-1:party-cleaner:V1"
    assert created["source_message_ref"] == "telegram_callback:cb-1:message:10"
    assert created["applied_at"] == NOW
    assert api.sent_texts[-1] == cleaner_application.SUCCESS_MESSAGE
    assert "담당자로 확정된 것은 아닙니다." in api.sent_texts[-1]


def test_idempotency_key_is_stable_across_different_telegram_messages_and_refreshed_jobs():
    ledger = FakeLedger()
    first = _run_callback(
        update=callback_update(callback_id="cb-1", message_id=10), ledger=ledger
    )[0]
    second_result, _source, same_ledger, api = _run_callback(
        update=callback_update(callback_id="cb-2", message_id=999), ledger=ledger
    )
    assert first == "application_created"
    assert second_result == "application_existing"
    assert same_ledger.create_count == 1
    assert same_ledger.query_count == 3
    assert same_ledger.created[0]["key"] == cleaner_application.application_idempotency_key(
        "cleaning-1", "party-cleaner"
    )
    assert api.sent_texts[-1] == cleaner_application.DUPLICATE_MESSAGE
    assert "담당자로 확정된 것은 아닙니다." in api.sent_texts[-1]


def test_sequential_duplicate_performs_zero_additional_create():
    key = cleaner_application.application_idempotency_key("cleaning-1", "party-cleaner")
    ledger = FakeLedger(rows=[application_row(key=key)])
    result, _source, ledger, api = _run_callback(ledger=ledger)
    assert result == "application_existing"
    assert ledger.create_count == 0
    assert api.sent_texts[-1] == cleaner_application.DUPLICATE_MESSAGE


def test_concurrent_same_key_application_calls_produce_one_logical_create():
    ledger = FakeLedger()
    source = FakeSource({"cleaning-1": cleaning_page()})
    worker = cleaner()

    def apply(ref):
        return cleaner_application.apply_for_cleaning(
            cleaner=worker,
            cleaning_page_id="cleaning-1",
            source_message_ref=ref,
            source=source,
            ledger=ledger,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(apply, ["telegram:1", "telegram:2"]))
    assert sorted(outcomes) == ["created", "existing"]
    assert ledger.create_count == 1
    assert len(ledger.rows) == 1


@pytest.mark.parametrize(
    "row",
    [
        application_row(cleaner_party_page_id="wrong-party"),
        application_row(cleaning_page_id="other-cleaning"),
        application_row(environment="TEST"),
        application_row(status="WITHDRAWN"),
    ],
    ids=["mismatched-cleaner", "mismatched-cleaning", "mismatched-environment", "mismatched-status"],
)
def test_same_idempotency_key_with_mismatched_row_fails_closed(row):
    expected_key = cleaner_application.application_idempotency_key("cleaning-1", "party-cleaner")
    row["properties"]["Idempotency Key"] = {
        "type": "rich_text", "rich_text": [{"plain_text": expected_key}]
    }
    ledger = FakeLedger(rows=[row])
    result, _source, ledger, api = _run_callback(ledger=ledger)
    assert result == "application_failed"
    assert ledger.create_count == 0
    assert api.sent_texts[-1] == cleaner_application.FAILURE_MESSAGE


def test_multiple_existing_rows_for_same_key_fail_closed():
    key = cleaner_application.application_idempotency_key("cleaning-1", "party-cleaner")
    ledger = FakeLedger(rows=[application_row(key=key), application_row(key=key)])
    result, _source, ledger, api = _run_callback(ledger=ledger)
    assert result == "application_failed"
    assert ledger.create_count == 0
    assert api.sent_texts[-1] == cleaner_application.FAILURE_MESSAGE


def test_application_query_failure_causes_failure_and_zero_create():
    ledger = FakeLedger(query_failure_at=1)
    result, _source, ledger, api = _run_callback(ledger=ledger)
    assert result == "application_failed"
    assert ledger.create_count == 0
    assert api.sent_texts[-1] == cleaner_application.FAILURE_MESSAGE


def test_create_failure_without_durable_row_causes_failure_after_reconciliation():
    ledger = FakeLedger(create_mode="raise_before")
    result, _source, ledger, api = _run_callback(ledger=ledger)
    assert result == "application_failed"
    assert ledger.create_count == 1
    assert ledger.query_count == 2
    assert api.sent_texts[-1] == cleaner_application.FAILURE_MESSAGE


def test_ambiguous_create_timeout_reconciles_by_key_and_emits_success_without_retry_create():
    ledger = FakeLedger(create_mode="append_then_raise")
    result, _source, ledger, api = _run_callback(ledger=ledger)
    assert result == "application_created"
    assert ledger.create_count == 1
    assert ledger.query_count == 2
    assert len(ledger.rows) == 1
    assert api.sent_texts[-1] == cleaner_application.SUCCESS_MESSAGE


def test_successful_create_with_invalid_readback_does_not_emit_false_success():
    ledger = FakeLedger(create_mode="invalid_readback")
    result, _source, ledger, api = _run_callback(ledger=ledger)
    assert result == "application_failed"
    assert ledger.create_count == 1
    assert api.sent_texts[-1] == cleaner_application.FAILURE_MESSAGE
    assert cleaner_application.SUCCESS_MESSAGE not in api.sent_texts


def test_post_create_readback_query_failure_does_not_emit_false_success():
    ledger = FakeLedger(query_failure_at=2)
    result, _source, ledger, api = _run_callback(ledger=ledger)
    assert result == "application_failed"
    assert ledger.create_count == 1
    assert api.sent_texts[-1] == cleaner_application.FAILURE_MESSAGE


def test_application_row_payload_is_exact_whitelist_applied_production_telegram(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("synthetic-token")
    requests = []

    def urlopen(request, **_kwargs):
        requests.append(request)
        return _Response(json.dumps({"id": "application-row"}).encode())

    ledger = cleaner_application.NotionApplicationLedger(
        token_path=token_path, urlopen=urlopen
    )
    ledger.create_application(
        cleaning_page_id="cleaning-1",
        cleaner_party_page_id="party-cleaner",
        source_message_ref="telegram_callback:cb-1:message:10",
        key="CLEANING_JOB_APPLICATION:cleaning-1:party-cleaner:V1",
        applied_at=NOW,
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.full_url.endswith("/v1/pages")
    body = json.loads(request.data)
    assert body["parent"] == {
        "type": "data_source_id",
        "data_source_id": cleaner_application.APPLICATION_SOURCE,
    }
    assert set(body["properties"]) == cleaner_application.APPLICATION_PROPERTY_WHITELIST
    props = body["properties"]
    assert props["Cleaning"] == {"relation": [{"id": "cleaning-1"}]}
    assert props["Cleaner"] == {"relation": [{"id": "party-cleaner"}]}
    assert props["Application Status"] == {"select": {"name": "APPLIED"}}
    assert props["Data Environment"] == {"select": {"name": "PRODUCTION"}}
    assert props["Source Channel"] == {"select": {"name": "TELEGRAM"}}
    serialized = json.dumps(body, ensure_ascii=False)
    for forbidden in [
        "SECRET_STREET_ADDRESS", "SECRET_DOOR_CODE", "SECRET_GUEST_PII",
        "SECRET_BOOKING_REF", "SECRET_RANK", "Offer", "Assignment",
    ]:
        assert forbidden not in serialized


def test_application_ledger_queries_only_exact_data_source(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("synthetic-token")
    requests = []

    def urlopen(request, **_kwargs):
        requests.append(request)
        return _Response(json.dumps({
            "results": [], "has_more": False, "next_cursor": None
        }).encode())

    ledger = cleaner_application.NotionApplicationLedger(
        token_path=token_path, urlopen=urlopen
    )
    ledger.query_by_idempotency("CLEANING_JOB_APPLICATION:cleaning-1:party-cleaner:V1")
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].full_url.endswith(
        f"/v1/data_sources/{cleaner_application.APPLICATION_SOURCE}/query"
    )
    body = json.loads(requests[0].data)
    assert body == {
        "filter": {
            "property": "Idempotency Key",
            "rich_text": {"equals": "CLEANING_JOB_APPLICATION:cleaning-1:party-cleaner:V1"},
        },
        "page_size": 100,
    }


def test_notion_application_writer_rejects_other_parent_arbitrary_query_and_all_mutating_methods(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("synthetic-token")
    ledger = cleaner_application.NotionApplicationLedger(
        token_path=token_path,
        urlopen=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must not be reached")),
    )
    valid_properties = {
        "신청명": {"title": [{"text": {"content": "청소 작업 신청"}}]},
        "Cleaning": {"relation": [{"id": "cleaning-1"}]},
        "Cleaner": {"relation": [{"id": "party-cleaner"}]},
        "Application Status": {"select": {"name": "APPLIED"}},
        "Applied At": {"date": {"start": NOW.isoformat()}},
        "Source Channel": {"select": {"name": "TELEGRAM"}},
        "Source Message Ref": {"rich_text": [{"text": {"content": "telegram:1"}}]},
        "Idempotency Key": {"rich_text": [{"text": {"content": "key"}}]},
        "Data Environment": {"select": {"name": "PRODUCTION"}},
    }
    wrong_parent = {
        "parent": {"type": "data_source_id", "data_source_id": "other-source"},
        "properties": valid_properties,
    }
    with pytest.raises(cleaner_application.ApplicationLedgerError):
        ledger._request("POST", "/v1/pages", wrong_parent)
    with pytest.raises(cleaner_application.ApplicationLedgerError):
        ledger._request(
            "POST", "/v1/data_sources/other-source/query",
            {"filter": {"property": "Idempotency Key", "rich_text": {"equals": "key"}}, "page_size": 100},
        )
    for method in ["PATCH", "PUT", "DELETE"]:
        with pytest.raises(cleaner_application.ApplicationLedgerError):
            ledger._request(method, "/v1/pages/cleaning-1", {})


def test_application_writer_rejects_extra_or_non_applied_properties(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("synthetic-token")
    ledger = cleaner_application.NotionApplicationLedger(
        token_path=token_path,
        urlopen=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must not be reached")),
    )
    base = {
        "parent": {"type": "data_source_id", "data_source_id": cleaner_application.APPLICATION_SOURCE},
        "properties": {
            "신청명": {"title": [{"text": {"content": "청소 작업 신청"}}]},
            "Cleaning": {"relation": [{"id": "cleaning-1"}]},
            "Cleaner": {"relation": [{"id": "party-cleaner"}]},
            "Application Status": {"select": {"name": "APPLIED"}},
            "Applied At": {"date": {"start": NOW.isoformat()}},
            "Source Channel": {"select": {"name": "TELEGRAM"}},
            "Source Message Ref": {"rich_text": [{"text": {"content": "telegram:1"}}]},
            "Idempotency Key": {"rich_text": [{"text": {"content": "key"}}]},
            "Data Environment": {"select": {"name": "PRODUCTION"}},
        },
    }
    extra = json.loads(json.dumps(base))
    extra["properties"]["Door Code"] = {"rich_text": [{"text": {"content": "secret"}}]}
    with pytest.raises(cleaner_application.ApplicationLedgerError):
        ledger._request("POST", "/v1/pages", extra)
    selected = json.loads(json.dumps(base))
    selected["properties"]["Application Status"] = {"select": {"name": "SELECTED"}}
    with pytest.raises(cleaner_application.ApplicationLedgerError):
        ledger._request("POST", "/v1/pages", selected)


def test_application_callback_routing_does_not_enter_existing_offer_callback():
    update = callback_update()
    with patch.object(cleaner_application, "handle_application_callback", return_value="application_existing") as app, patch.object(
        bot_service, "callback", side_effect=AssertionError("legacy offer callback must not run")
    ):
        result = bot_service.handle_cleaner(update, "token", request_api=CaptureApi(), now=NOW)
    assert result == "application_existing"
    assert app.call_count == 1


def test_non_application_callback_falls_through_to_existing_offer_callback_unchanged():
    update = {"callback_query": {"data": "a:offer-1:approve:sig"}}
    with patch.object(cleaner_application, "handle_application_callback", return_value=None), patch.object(
        bot_service, "callback", return_value="approved"
    ) as existing:
        result = bot_service.handle_cleaner(update, "token", request_api=CaptureApi(), now=NOW)
    assert result == "approved"
    assert existing.call_count == 1


def test_application_flow_has_no_cleaning_party_offer_calendar_or_finance_mutation_collaborators():
    source = FakeSource({"cleaning-1": cleaning_page()})
    ledger = FakeLedger()
    outcome = cleaner_application.apply_for_cleaning(
        cleaner=cleaner(),
        cleaning_page_id="cleaning-1",
        source_message_ref="telegram:1",
        source=source,
        ledger=ledger,
        now=NOW,
    )
    assert outcome == "created"
    assert source.calls == ["cleaning-1"]
    assert ledger.create_count == 1
    # The application service accepts only a read-only Cleaning source and the application ledger.
    # It has no Party/Offer/Assignment/Calendar/Finance writer collaborator at all.
    assert set(ledger.created[0]) == {
        "cleaning_page_id", "cleaner_party_page_id", "source_message_ref", "key", "applied_at"
    }


def test_application_ux_never_reveals_protected_cleaning_or_other_cleaner_data():
    result, _source, _ledger, api = _run_callback()
    assert result == "application_created"
    all_text = "\n".join(api.sent_texts)
    for forbidden in [
        "SECRET_STREET_ADDRESS", "SECRET_DOOR_CODE", "SECRET_GUEST_PII",
        "SECRET_BOOKING_REF", "OTHER_CLEANER", "SECRET_RANK", "999999",
    ]:
        assert forbidden not in all_text
