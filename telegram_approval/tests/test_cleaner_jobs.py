import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from telegram_approval import bot_service, cleaner_jobs


NOW = datetime(2026, 8, 26, 11, 0, tzinfo=timezone.utc)
CLEANING_SOURCE_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _stable_property_mappings(monkeypatch):
    monkeypatch.setenv("PROPERTYAI_NOTION_CLEANING_SOURCE_ID", CLEANING_SOURCE_ID)
    monkeypatch.setattr(
        cleaner_jobs,
        "load_property_mappings",
        lambda: {"house-jj": "JJ", "house-xx": "XX"},
    )


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
    strategy="SEQUENTIAL_PRIORITY",
    environment="PRODUCTION",
    registration="APPROVED",
    state="담당자 배정",
    acceptance="수락대기",
    assignment_state="PROPOSING",
    job_type="퇴실청소",
    day="2026-08-30",
    start="2026-08-30T11:00:00+09:00",
    end="2026-08-30T15:00:00+09:00",
    fee=60000,
    property_page_ids=("house-jj",),
    assignee_ids=(),
):
    return {
        "id": page_id,
        "properties": {
            "점검명": _rich(f"{nickname} · {job_type} · {day}"),
            "점검 유형": _select(job_type),
            "점검일": _date(day),
            "시작 예정": _date(start),
            "완료 목표": _date(end),
            "청소비 Snapshot": _number(fee),
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
            # Deliberately sensitive fields: /jobs must never render these.
            "주소": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_STREET_ADDRESS"}]},
            "출입 코드": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_DOOR_CODE"}]},
            "Guest PII": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_GUEST_PII"}]},
            "예약번호": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_BOOKING_REF"}]},
            "담당자": {"type": "rich_text", "rich_text": [{"plain_text": "OTHER_CLEANER_NAME"}]},
            "배정 Tier": _select("OTHER_CLEANER_RANK"),
            "예상 총 지급액": _number(987654),
        },
    }


def cleaner(
    *, user_id=202, chat_id=202, status="ACTIVE", role="CLEANER",
    properties=None, identity_id="worker"
):
    return {
        "identity_id": identity_id,
        "role": role,
        "status": status,
        "telegram_user_id": user_id,
        "telegram_chat_id": chat_id,
        "properties": ["JJ"] if properties is None else properties,
        "priority_by_property": {"JJ": 1},
    }


def roster(*entries):
    return {"schema_version": 1, "cleaners": list(entries)}


def write_offer(
    request_dir: Path,
    *,
    action_id="offer-1",
    page_id="cleaning-1",
    user_id=202,
    chat_id=202,
    property_nickname="JJ",
    status="PENDING",
    consumed=False,
    expires_at=None,
    test_mode=False,
):
    request_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "action_id": action_id,
        "action_type": "CLEANING_ASSIGNMENT",
        "cleaning_page_id": page_id,
        "property_nickname": property_nickname,
        "candidate_user_id": user_id,
        "candidate_chat_id": chat_id,
        "candidate_label": "OTHER_CLEANER_NAME",
        "remaining_candidates": [{"label": "OTHER_CLEANER_NAME", "payment": 987654}],
        "address": "SECRET_STREET_ADDRESS",
        "status": status,
        "consumed": consumed,
        "expires_at": expires_at or (NOW + timedelta(hours=2)).isoformat(),
        "test_mode": test_mode,
    }
    path = request_dir / f"{action_id}.json"
    path.write_text(json.dumps(record))
    return path


class FakeSource:
    def __init__(self, pages=None, open_pages=None, *, fail_get=False, fail_query=False):
        self.pages = pages or {}
        self.open_pages = list(open_pages or [])
        self.fail_get = fail_get
        self.fail_query = fail_query
        self.calls = []

    def get_cleaning(self, page_id):
        self.calls.append(("GET", page_id))
        if self.fail_get:
            raise RuntimeError("synthetic get failure")
        return self.pages[page_id]

    def query_open_applications(self):
        self.calls.append(("POST_QUERY", None))
        if self.fail_query:
            raise RuntimeError("synthetic query failure")
        return self.open_pages


class CaptureApi:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, **values):
        self.calls.append((token, method, values))
        return {"message_id": len(self.calls)}

    @property
    def last_text(self):
        return self.calls[-1][2]["text"] if self.calls else None


def jobs_update(user_id=202, chat_id=202, *, chat_type="private"):
    return {
        "message": {
            "from": {"id": user_id, "is_bot": False},
            "chat": {"id": chat_id, "type": chat_type},
            "text": "/jobs",
        }
    }


def test_active_registered_cleaner_can_invoke_jobs_through_cleaner_runtime_handler(tmp_path):
    request_dir = tmp_path / "requests"
    write_offer(request_dir)
    source = FakeSource(pages={"cleaning-1": cleaning_page()})
    api = CaptureApi()

    with patch.object(cleaner_jobs, "load_roster", return_value=roster(cleaner())), patch.object(
        cleaner_jobs, "NotionCleaningReader", return_value=source
    ):
        result = bot_service.handle_cleaner(
            jobs_update(),
            "synthetic-cleaner-token",
            request_dir=request_dir,
            request_api=api,
            now=NOW,
        )

    assert result == "jobs_listed"
    assert "🧹 지원 가능한 청소" in api.last_text
    assert "JJ · 8/30 퇴실청소" in api.last_text
    assert "내게 온 제안" in api.last_text


@pytest.mark.parametrize(
    "roster_value,user_id,chat_id",
    [
        (roster(), 999, 999),
        (roster(cleaner()), 202, 999),
        (roster(cleaner(status="INACTIVE")), 202, 202),
    ],
    ids=["unknown-user", "wrong-chat", "inactive-cleaner"],
)
def test_unauthorized_identity_never_sees_job_existence(tmp_path, roster_value, user_id, chat_id):
    source = FakeSource(open_pages=[cleaning_page(strategy="OPEN_APPLICATION", acceptance="미제안")])
    api = CaptureApi()

    result = cleaner_jobs.handle_jobs(
        jobs_update(user_id, chat_id),
        "token",
        request_dir=tmp_path / "requests",
        request_api=api,
        source=source,
        roster_loader=lambda: roster_value,
        now=NOW,
    )

    assert result == "jobs_unauthorized"
    assert api.last_text == cleaner_jobs.UNAUTHORIZED_MESSAGE
    assert "JJ" not in api.last_text
    assert source.calls == []


def test_identity_ambiguity_fails_closed_before_source_read(tmp_path):
    duplicate = cleaner(identity_id="duplicate")
    source = FakeSource(open_pages=[cleaning_page(strategy="OPEN_APPLICATION", acceptance="미제안")])
    api = CaptureApi()
    result = cleaner_jobs.handle_jobs(
        jobs_update(), "token", request_dir=tmp_path, request_api=api, source=source,
        roster_loader=lambda: roster(cleaner(), duplicate), now=NOW,
    )
    assert result == "jobs_unauthorized"
    assert source.calls == []


def test_targeted_pending_offer_appears_only_to_matching_candidate(tmp_path):
    request_dir = tmp_path / "requests"
    write_offer(request_dir, user_id=202, chat_id=202)
    page = cleaning_page()
    source_a = FakeSource(pages={page["id"]: page})
    source_b = FakeSource(pages={page["id"]: page})

    mine = cleaner_jobs.available_jobs(cleaner=cleaner(), request_dir=request_dir, source=source_a, now=NOW)
    other = cleaner_jobs.available_jobs(
        cleaner=cleaner(user_id=303, chat_id=303), request_dir=request_dir, source=source_b, now=NOW
    )

    assert [job["visibility"] for job in mine] == ["MY_ACTIVE_OFFER"]
    assert other == []
    assert ("GET", "cleaning-1") not in source_b.calls


def test_explicit_open_application_requires_property_authorization_and_never_infers_unassigned(tmp_path):
    open_authorized = cleaning_page(
        "open-jj", strategy="OPEN_APPLICATION", acceptance="미제안", state="예정"
    )
    open_unauthorized = cleaning_page(
        "open-xx", nickname="XX", strategy="OPEN_APPLICATION", acceptance="미제안",
        state="예정", property_page_ids=("house-xx",)
    )
    ordinary_unassigned = cleaning_page(
        "ordinary", strategy="SEQUENTIAL_PRIORITY", acceptance="미제안", state="예정"
    )
    targeted_priority = cleaning_page(
        "priority", strategy="WAVE_BROADCAST_FIRST_ACCEPT", acceptance="수락대기", state="담당자 배정"
    )
    source = FakeSource(open_pages=[open_authorized, open_unauthorized, ordinary_unassigned, targeted_priority])

    jobs = cleaner_jobs.available_jobs(
        cleaner=cleaner(properties=["JJ"]), request_dir=tmp_path / "requests", source=source, now=NOW
    )

    assert [(job["page_id"], job["visibility"]) for job in jobs] == [
        ("open-jj", "EXPLICIT_OPEN_APPLICATION")
    ]


@pytest.mark.parametrize(
    "page_overrides,offer_overrides",
    [
        ({"state": "취소"}, {}),
        ({"state": "관리자 확인 완료"}, {}),
        ({"assignment_state": "HARD_BOOKED", "acceptance": "수락"}, {}),
        ({"environment": "TEST"}, {}),
        ({"registration": "REVIEW_REQUIRED"}, {}),
        ({}, {"test_mode": True}),
        ({}, {"expires_at": (NOW - timedelta(seconds=1)).isoformat()}),
        ({}, {"consumed": True}),
        ({"state": "취소"}, {}),
    ],
    ids=[
        "cancelled", "completed", "hard-booked", "test-data", "not-approved",
        "test-mode-offer", "expired-offer", "consumed-offer", "stale-local-offer-closed-fresh",
    ],
)
def test_closed_unsafe_or_stale_targeted_offer_is_excluded(tmp_path, page_overrides, offer_overrides):
    request_dir = tmp_path / "requests"
    write_offer(request_dir, **offer_overrides)
    page = cleaning_page(**page_overrides)
    source = FakeSource(pages={page["id"]: page})

    assert cleaner_jobs.available_jobs(
        cleaner=cleaner(), request_dir=request_dir, source=source, now=NOW
    ) == []


def test_open_application_excludes_final_assigned_test_and_unapproved_items(tmp_path):
    pages = [
        cleaning_page("hard", strategy="OPEN_APPLICATION", acceptance="수락", assignment_state="HARD_BOOKED"),
        cleaning_page("test", strategy="OPEN_APPLICATION", acceptance="미제안", environment="TEST", state="예정"),
        cleaning_page("review", strategy="OPEN_APPLICATION", acceptance="미제안", registration="REVIEW_REQUIRED", state="예정"),
        cleaning_page("closed", strategy="OPEN_APPLICATION", acceptance="미제안", state="완료 보고"),
    ]
    source = FakeSource(open_pages=pages)
    assert cleaner_jobs.available_jobs(
        cleaner=cleaner(), request_dir=tmp_path / "requests", source=source, now=NOW
    ) == []


def test_authorized_title_with_wrong_canonical_property_relation_is_hidden(tmp_path):
    page = cleaning_page(
        "wrong-property", nickname="JJ", strategy="OPEN_APPLICATION", acceptance="미제안",
        state="예정", property_page_ids=("house-xx",)
    )
    jobs = cleaner_jobs.available_jobs(
        cleaner=cleaner(properties=["JJ"]),
        request_dir=tmp_path / "requests",
        source=FakeSource(open_pages=[page]),
        now=NOW,
    )
    assert jobs == []


@pytest.mark.parametrize(
    "property_page_ids",
    [(), ("house-jj", "house-xx")],
    ids=["missing", "ambiguous"],
)
def test_missing_or_ambiguous_canonical_property_relation_is_hidden(tmp_path, property_page_ids):
    page = cleaning_page(
        strategy="OPEN_APPLICATION", acceptance="미제안", state="예정",
        property_page_ids=property_page_ids,
    )
    assert cleaner_jobs.available_jobs(
        cleaner=cleaner(), request_dir=tmp_path / "requests",
        source=FakeSource(open_pages=[page]), now=NOW,
    ) == []


def test_open_application_with_existing_assignee_relation_is_hidden(tmp_path):
    page = cleaning_page(
        strategy="OPEN_APPLICATION", acceptance="미제안", state="예정",
        assignee_ids=("other-party",),
    )
    assert cleaner_jobs.available_jobs(
        cleaner=cleaner(), request_dir=tmp_path / "requests",
        source=FakeSource(open_pages=[page]), now=NOW,
    ) == []


def test_my_active_offer_with_existing_assignee_relation_is_hidden(tmp_path):
    request_dir = tmp_path / "requests"
    write_offer(request_dir)
    page = cleaning_page(assignee_ids=("other-party",))
    assert cleaner_jobs.available_jobs(
        cleaner=cleaner(), request_dir=request_dir,
        source=FakeSource(pages={page["id"]: page}), now=NOW,
    ) == []


@pytest.mark.parametrize(
    "duplicate",
    [
        cleaner(identity_id="inactive-duplicate", status="INACTIVE"),
        cleaner(identity_id="observer-duplicate", role="OPERATOR_OBSERVER"),
    ],
    ids=["inactive-duplicate", "different-role-duplicate"],
)
def test_roster_identity_collision_across_all_records_fails_closed(tmp_path, duplicate):
    source = FakeSource(open_pages=[cleaning_page(strategy="OPEN_APPLICATION", acceptance="미제안", state="예정")])
    api = CaptureApi()
    result = cleaner_jobs.handle_jobs(
        jobs_update(), "token", request_dir=tmp_path / "requests", request_api=api,
        source=source, roster_loader=lambda: roster(cleaner(), duplicate), now=NOW,
    )
    assert result == "jobs_unauthorized"
    assert api.last_text == cleaner_jobs.UNAUTHORIZED_MESSAGE
    assert source.calls == []


def test_malformed_local_offer_json_is_failure_not_empty(tmp_path):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    (request_dir / "broken.json").write_text("{not-json")
    api = CaptureApi()
    result = cleaner_jobs.handle_jobs(
        jobs_update(), "token", request_dir=request_dir, request_api=api,
        source=FakeSource(), roster_loader=lambda: roster(cleaner()), now=NOW,
    )
    assert result == "jobs_failed"
    assert api.last_text == cleaner_jobs.FAILURE_MESSAGE
    assert api.last_text != cleaner_jobs.EMPTY_MESSAGE


def test_jobs_response_whitelists_safe_fields_and_omits_all_sensitive_data(tmp_path):
    request_dir = tmp_path / "requests"
    record_path = write_offer(request_dir)
    page = cleaning_page()
    source = FakeSource(pages={page["id"]: page})
    jobs = cleaner_jobs.available_jobs(cleaner=cleaner(), request_dir=request_dir, source=source, now=NOW)
    text = cleaner_jobs.render_jobs(jobs)

    assert "SECRET_STREET_ADDRESS" not in text
    assert "SECRET_DOOR_CODE" not in text
    assert "SECRET_GUEST_PII" not in text
    assert "SECRET_BOOKING_REF" not in text
    assert "OTHER_CLEANER_NAME" not in text
    assert "OTHER_CLEANER_RANK" not in text
    assert "987654" not in text
    assert "주소" not in text
    assert "Door" not in text
    assert json.loads(record_path.read_text())["address"] == "SECRET_STREET_ADDRESS"


def test_empty_success_has_explicit_zero_result_message(tmp_path):
    api = CaptureApi()
    result = cleaner_jobs.handle_jobs(
        jobs_update(), "token", request_dir=tmp_path / "requests", request_api=api,
        source=FakeSource(), roster_loader=lambda: roster(cleaner()), now=NOW,
    )
    assert result == "jobs_listed"
    assert api.last_text == cleaner_jobs.EMPTY_MESSAGE


def test_valid_empty_notion_query_response_is_empty_result_ux(tmp_path):
    token_path = tmp_path / "notion-token"
    token_path.write_text("synthetic-token")

    def urlopen(_request, **_kwargs):
        return _Response(json.dumps({
            "results": [], "has_more": False, "next_cursor": None
        }).encode())

    api = CaptureApi()
    result = cleaner_jobs.handle_jobs(
        jobs_update(), "token", request_dir=tmp_path / "requests", request_api=api,
        source=cleaner_jobs.NotionCleaningReader(token_path=token_path, urlopen=urlopen),
        roster_loader=lambda: roster(cleaner()), now=NOW,
    )
    assert result == "jobs_listed"
    assert api.last_text == cleaner_jobs.EMPTY_MESSAGE


def test_malformed_notion_query_response_is_failure_ux(tmp_path):
    token_path = tmp_path / "notion-token"
    token_path.write_text("synthetic-token")

    def urlopen(_request, **_kwargs):
        return _Response(json.dumps({}).encode())

    api = CaptureApi()
    result = cleaner_jobs.handle_jobs(
        jobs_update(), "token", request_dir=tmp_path / "requests", request_api=api,
        source=cleaner_jobs.NotionCleaningReader(token_path=token_path, urlopen=urlopen),
        roster_loader=lambda: roster(cleaner()), now=NOW,
    )
    assert result == "jobs_failed"
    assert api.last_text == cleaner_jobs.FAILURE_MESSAGE
    assert api.last_text != cleaner_jobs.EMPTY_MESSAGE


@pytest.mark.parametrize("failure_kind", ["query", "fresh-get", "roster"])
def test_source_or_roster_failure_is_not_reported_as_empty(tmp_path, failure_kind):
    request_dir = tmp_path / "requests"
    source = FakeSource(fail_query=failure_kind == "query", fail_get=failure_kind == "fresh-get")
    if failure_kind == "fresh-get":
        write_offer(request_dir)
        source.pages["cleaning-1"] = cleaning_page()
    api = CaptureApi()

    def roster_loader():
        if failure_kind == "roster":
            raise OSError("synthetic roster failure")
        return roster(cleaner())

    result = cleaner_jobs.handle_jobs(
        jobs_update(), "token", request_dir=request_dir, request_api=api,
        source=source, roster_loader=roster_loader, now=NOW,
    )
    assert result == "jobs_failed"
    assert api.last_text == cleaner_jobs.FAILURE_MESSAGE
    assert api.last_text != cleaner_jobs.EMPTY_MESSAGE


def test_jobs_is_read_only_for_local_action_and_business_boundaries(tmp_path):
    request_dir = tmp_path / "requests"
    record_path = write_offer(request_dir)
    before = record_path.read_bytes()
    page = cleaning_page()
    source = FakeSource(pages={page["id"]: page})
    api = CaptureApi()

    result = cleaner_jobs.handle_jobs(
        jobs_update(), "token", request_dir=request_dir, request_api=api,
        source=source, roster_loader=lambda: roster(cleaner()), now=NOW,
    )

    assert result == "jobs_listed"
    assert record_path.read_bytes() == before
    assert sorted(path.name for path in request_dir.iterdir()) == [record_path.name]
    assert source.calls == [("GET", "cleaning-1"), ("POST_QUERY", None)]
    # The command has no Calendar/Finance collaborator and the only outbound call is Telegram sendMessage.
    assert [(method, set(values)) for _token, method, values in api.calls] == [
        ("sendMessage", {"chat_id", "text"})
    ]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_notion_reader_uses_only_get_and_post_query_and_rejects_write_surfaces(tmp_path):
    token_path = tmp_path / "notion-token"
    token_path.write_text("synthetic-token")
    requests = []

    def urlopen(request, **_kwargs):
        requests.append(request)
        if request.method == "GET":
            return _Response(json.dumps(cleaning_page()).encode())
        return _Response(json.dumps({"results": [], "has_more": False, "next_cursor": None}).encode())

    source = cleaner_jobs.NotionCleaningReader(token_path=token_path, urlopen=urlopen)
    source.get_cleaning("cleaning-1")
    source.query_open_applications()

    assert [(request.method, request.full_url.split("api.notion.com", 1)[1]) for request in requests] == [
        ("GET", "/v1/pages/cleaning-1"),
        ("POST", f"/v1/data_sources/{CLEANING_SOURCE_ID}/query"),
    ]
    query_payload = json.loads(requests[1].data)
    assert {item["property"] for item in query_payload["filter"]["and"]} == {
        "배정 전략 Snapshot", "데이터 환경", "등록 상태"
    }
    rejected = [
        ("GET", "/v1/users", None),
        ("GET", "/v1/data_sources/source", None),
        ("POST", "/v1/arbitrary/query", {}),
        ("POST", "/v1/pages", {}),
        ("PATCH", "/v1/pages/cleaning-1", {}),
        ("DELETE", "/v1/pages/cleaning-1", None),
        ("PUT", "/v1/pages/cleaning-1", {}),
    ]
    for method, path, body in rejected:
        with pytest.raises(RuntimeError):
            source._request(method, path, body)


def test_group_chat_jobs_fails_closed_without_source_or_job_details(tmp_path):
    source = FakeSource(open_pages=[cleaning_page(strategy="OPEN_APPLICATION", acceptance="미제안")])
    api = CaptureApi()
    result = cleaner_jobs.handle_jobs(
        jobs_update(chat_type="group"), "token", request_dir=tmp_path,
        request_api=api, source=source, roster_loader=lambda: roster(cleaner()), now=NOW,
    )
    assert result == "jobs_unauthorized"
    assert api.calls == []
    assert source.calls == []
