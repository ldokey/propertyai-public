import copy
import json
from datetime import datetime

import pytest

from telegram_approval import bot_service, cleaner_home, cleaner_my_schedule as my


NOW = datetime.fromisoformat("2026-08-28T15:00:00+09:00")
MAPPINGS = {"house-jj": "JJ", "house-xx": "XX"}
CLEANING_SOURCE_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _stable_property_mappings(monkeypatch):
    monkeypatch.setenv("PROPERTYAI_NOTION_CLEANING_SOURCE_ID", CLEANING_SOURCE_ID)
    monkeypatch.setattr(my, "load_property_mappings", lambda: MAPPINGS)
    # Legacy /my tests isolate schedule selection/rendering from the new signed
    # unavailable action authority. Phase-2 action/UX behavior is covered in
    # test_cleaner_unavailable.py with exact fake 19/history bindings.
    monkeypatch.setattr(
        my,
        "build_schedule_ui",
        lambda **kwargs: (kwargs["items"], None),
    )


def _select(value):
    return {"type": "select", "select": {"name": value} if value is not None else None}


def _date(value):
    return {"type": "date", "date": {"start": value} if value is not None else None}


def cleaning_page(
    page_id="cleaning-1",
    *,
    state="담당자 배정",
    acceptance="수락",
    assignment_state=None,
    day="2026-08-28",
    start="2026-08-28T11:00:00+09:00",
    end="2026-08-28T15:00:00+09:00",
    assignee_ids=("party-cleaner",),
    property_ids=("house-jj",),
    environment="PRODUCTION",
    registration="APPROVED",
):
    return {
        "id": page_id,
        "properties": {
            "데이터 환경": _select(environment),
            "등록 상태": _select(registration),
            "상태": _select(state),
            "배정 수락 상태": _select(acceptance),
            "배정 상태": _select(assignment_state),
            "점검일": _date(day),
            "시작 예정": _date(start),
            "완료 목표": _date(end),
            "연결 집": {"type": "relation", "relation": [{"id": value} for value in property_ids]},
            "담당 참여자/업체": {
                "type": "relation",
                "relation": [{"id": value} for value in assignee_ids],
            },
            # Deliberately sensitive and legacy-looking fields. None may render.
            "주소": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_STREET_ADDRESS"}]},
            "출입 코드": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_DOOR_CODE"}]},
            "Guest PII": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_GUEST_PII"}]},
            "예약번호": {"type": "rich_text", "rich_text": [{"plain_text": "SECRET_BOOKING_REF"}]},
            "담당자": {"type": "rich_text", "rich_text": [{"plain_text": "OTHER_CLEANER_NAME"}]},
            "배정 Tier": _select("SECRET_RANK"),
        },
    }


def cleaner(
    *,
    user_id=202,
    chat_id=202,
    party_page_id="party-cleaner",
    status="ACTIVE",
    role="CLEANER",
    properties=None,
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


def update(text="/my", *, user_id=202, chat_id=202, chat_type="private", is_bot=False, extra_message=None):
    message = {
        "from": {"id": user_id, "is_bot": is_bot},
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
    }
    if extra_message:
        message.update(extra_message)
    return {"message": message}


class FakeSource:
    def __init__(self, pages=(), *, fail=False):
        self.pages = list(pages)
        self.fail = fail
        self.calls = []

    def query_for_cleaner(self, party_page_id):
        self.calls.append(party_page_id)
        if self.fail:
            raise RuntimeError("synthetic source failure")
        return self.pages


class CaptureApi:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, **values):
        self.calls.append((token, method, values))
        return {"message_id": len(self.calls)}

    @property
    def sent(self):
        return [values for _token, method, values in self.calls if method == "sendMessage"]

    @property
    def last_text(self):
        return self.sent[-1]["text"] if self.sent else None


def schedule(pages, *, current_cleaner=None):
    return my.my_schedule(
        cleaner=current_cleaner or cleaner(),
        source=FakeSource(pages),
        now=NOW,
        property_mappings=MAPPINGS,
    )


def test_01_active_cleaner_slash_my_lists_only_confirmed_schedule():
    api = CaptureApi()
    source = FakeSource([cleaning_page()])
    result = my.handle_my_schedule(
        update(), "token", request_api=api, source=source,
        roster_loader=lambda: roster(cleaner()), now=NOW,
    )
    assert result == "my_schedule_listed"
    assert api.last_text == "📅 내 청소\n\n오늘\n• JJ · 11:00–15:00"
    assert source.calls == ["party-cleaner"]


def test_02_korean_my_button_reuses_exact_slash_handler(monkeypatch, tmp_path):
    source = FakeSource([cleaning_page()])
    monkeypatch.setattr(my, "NotionMyScheduleReader", lambda: source)
    monkeypatch.setattr(my, "load_roster", lambda: roster(cleaner()))
    api = CaptureApi()
    slash_result = bot_service.handle_cleaner(update("/my"), "token", request_api=api, request_dir=tmp_path, now=NOW)
    slash_message = dict(api.sent[-1])
    api.calls.clear()
    button_result = bot_service.handle_cleaner(
        update(cleaner_home.MY_BUTTON), "token", request_api=api, request_dir=tmp_path, now=NOW
    )
    assert slash_result == button_result == "my_schedule_listed"
    assert api.sent[-1] == slash_message


@pytest.mark.parametrize("alias", ["내 청소", "내 일정"])
def test_03_04_korean_aliases_route_to_my(alias, monkeypatch, tmp_path):
    source = FakeSource([])
    monkeypatch.setattr(my, "NotionMyScheduleReader", lambda: source)
    monkeypatch.setattr(my, "load_roster", lambda: roster(cleaner()))
    api = CaptureApi()
    result = bot_service.handle_cleaner(update(alias), "token", request_api=api, request_dir=tmp_path, now=NOW)
    assert result == "my_schedule_listed"
    assert api.last_text == my.EMPTY_MESSAGE


def test_06_effective_own_assignment_requires_exact_party_relation_and_acceptance():
    pages = [
        cleaning_page("mine", assignee_ids=("party-cleaner",)),
        cleaning_page("other", assignee_ids=("party-other",)),
        cleaning_page("ambiguous", assignee_ids=("party-cleaner", "party-other")),
    ]
    items = schedule(pages)
    assert [item["page_id"] for item in items] == ["mine"]


def test_07_other_cleaner_assignment_is_hidden_even_if_property_is_authorized():
    items = schedule([cleaning_page(assignee_ids=("party-other",))])
    assert items == []


def test_08_property_access_alone_never_creates_schedule_item():
    current = cleaner(properties=["JJ"])
    items = schedule([cleaning_page(assignee_ids=(), acceptance="수락")], current_cleaner=current)
    assert items == []


def test_09_application_alone_never_creates_schedule_item():
    items = schedule([cleaning_page(acceptance="미제안", assignee_ids=("party-cleaner",))])
    assert items == []


def test_10_unaccepted_offer_never_creates_schedule_item():
    items = schedule([cleaning_page(acceptance="수락대기", assignee_ids=("party-cleaner",))])
    assert items == []


def test_11_superseded_assignment_state_is_excluded():
    items = schedule([cleaning_page(assignment_state="SUPERSEDED")])
    assert items == []


def test_11a_unplanned_assignment_state_is_excluded_until_acceptance_projects_hard_booked():
    assert schedule([cleaning_page(assignment_state="UNPLANNED")]) == []
    assert [item["page_id"] for item in schedule([cleaning_page(assignment_state="HARD_BOOKED")])] == ["cleaning-1"]


def test_12_replacement_required_is_excluded_even_when_legacy_assignee_relation_remains():
    items = schedule([cleaning_page(acceptance="대체필요", assignee_ids=("party-cleaner",))])
    assert items == []


def test_13_cancelled_is_classified_separately_only_with_accepted_assignment_evidence():
    item = schedule([
        cleaning_page(
            state="취소", acceptance="수락", assignment_state="CANCELLED",
            day="2026-08-25", start="2026-08-25T11:00:00+09:00", end="2026-08-25T15:00:00+09:00",
        )
    ])[0]
    assert item["ui_state"] == "CANCELLED"
    assert "8월 25일 · JJ · 11:00–15:00 · 취소" in my.render_my_schedule([item])


def test_cancelled_with_only_leftover_relation_and_no_acceptance_proof_is_excluded():
    items = schedule([
        cleaning_page(
            state="취소", acceptance="미제안", assignment_state="CANCELLED",
            day="2026-08-25", start="2026-08-25T11:00:00+09:00", end="2026-08-25T15:00:00+09:00",
        )
    ])
    assert items == []


def test_14_completed_past_is_classified_separately():
    item = schedule([
        cleaning_page(
            state="관리자 확인 완료", day="2026-08-27",
            start="2026-08-27T11:00:00+09:00", end="2026-08-27T15:00:00+09:00",
        )
    ])[0]
    assert item["ui_state"] == "COMPLETED"
    assert "완료" in my.render_my_schedule([item])


def test_15_today_is_classified_from_kst_operational_date():
    item = schedule([cleaning_page(day="2026-08-28")])[0]
    assert item["ui_state"] == "TODAY"


def test_stale_active_assignment_on_past_date_is_not_presented_as_completed():
    assert schedule([
        cleaning_page(
            state="담당자 배정", day="2026-08-20",
            start="2026-08-20T11:00:00+09:00", end="2026-08-20T15:00:00+09:00",
        )
    ]) == []


def test_16_future_sort_is_operational_date_then_start_then_page_id():
    items = schedule([
        cleaning_page("late-day", day="2026-09-02", start="2026-09-02T11:00:00+09:00", end="2026-09-02T15:00:00+09:00"),
        cleaning_page("same-b", day="2026-08-29", start="2026-08-29T11:00:00+09:00", end="2026-08-29T15:00:00+09:00"),
        cleaning_page("same-a", day="2026-08-29", start="2026-08-29T11:00:00+09:00", end="2026-08-29T15:00:00+09:00"),
        cleaning_page("later-time", day="2026-08-29", start="2026-08-29T13:00:00+09:00", end="2026-08-29T15:00:00+09:00"),
    ])
    assert [item["page_id"] for item in items] == ["same-a", "same-b", "later-time", "late-day"]


def test_17_recent_history_sort_is_most_recent_first_with_stable_tie_breaker():
    items = schedule([
        cleaning_page("old", state="완료 보고", day="2026-08-24", start="2026-08-24T11:00:00+09:00", end="2026-08-24T15:00:00+09:00"),
        cleaning_page("recent-b", state="완료 보고", day="2026-08-27", start="2026-08-27T11:00:00+09:00", end="2026-08-27T15:00:00+09:00"),
        cleaning_page("recent-a", state="완료 보고", day="2026-08-27", start="2026-08-27T11:00:00+09:00", end="2026-08-27T15:00:00+09:00"),
    ])
    assert [item["page_id"] for item in items] == ["recent-a", "recent-b", "old"]


def _sensitive_rendered_text():
    return my.render_my_schedule(schedule([cleaning_page()]))


def test_18_guest_pii_never_renders():
    assert "SECRET_GUEST_PII" not in _sensitive_rendered_text()
    assert "SECRET_BOOKING_REF" not in _sensitive_rendered_text()


def test_19_door_code_and_exact_address_never_render():
    text = _sensitive_rendered_text()
    assert "SECRET_DOOR_CODE" not in text
    assert "SECRET_STREET_ADDRESS" not in text


def test_20_other_cleaner_identity_and_rank_never_render():
    text = _sensitive_rendered_text()
    assert "OTHER_CLEANER_NAME" not in text
    assert "SECRET_RANK" not in text


def test_missing_authoritative_times_renders_date_and_property_only():
    items = schedule([
        cleaning_page(
            "future", day="2026-09-02", start=None, end=None,
        )
    ])
    text = my.render_my_schedule(items)
    assert "• 9월 2일 · JJ" in text
    assert "11:00" not in text and "15:00" not in text


def test_21_empty_state_is_distinct_from_source_failure():
    empty_api = CaptureApi()
    empty_result = my.handle_my_schedule(
        update(), "token", request_api=empty_api, source=FakeSource([]),
        roster_loader=lambda: roster(cleaner()), now=NOW,
    )
    failed_api = CaptureApi()
    failed_result = my.handle_my_schedule(
        update(), "token", request_api=failed_api, source=FakeSource(fail=True),
        roster_loader=lambda: roster(cleaner()), now=NOW,
    )
    assert empty_result == "my_schedule_listed"
    assert empty_api.last_text == my.EMPTY_MESSAGE
    assert failed_result == "my_schedule_failed"
    assert failed_api.last_text == my.FAILURE_MESSAGE
    assert empty_api.last_text != failed_api.last_text


def test_22_duplicate_underlying_row_does_not_duplicate_display():
    page = cleaning_page()
    items = schedule([page, copy.deepcopy(page)])
    assert [item["page_id"] for item in items] == ["cleaning-1"]
    assert my.render_my_schedule(items).count("• JJ") == 1


@pytest.mark.parametrize(
    "roster_value",
    [
        roster(cleaner(party_page_id=None)),
        roster(
            cleaner(identity_id="one"),
            cleaner(user_id=303, chat_id=303, identity_id="two"),
        ),
    ],
    ids=["missing-party", "duplicate-active-party-owner"],
)
def test_23_malformed_or_ambiguous_cleaner_party_identity_fails_closed(roster_value):
    source = FakeSource([cleaning_page()])
    api = CaptureApi()
    result = my.handle_my_schedule(
        update(), "token", request_api=api, source=source,
        roster_loader=lambda: roster_value, now=NOW,
    )
    assert result == "my_schedule_unauthorized"
    assert api.last_text == my.UNAUTHORIZED_MESSAGE
    assert source.calls == []


def test_24_inactive_or_wrong_identity_is_safe_and_never_queries_schedule():
    source = FakeSource([cleaning_page()])
    api = CaptureApi()
    result = my.handle_my_schedule(
        update(), "token", request_api=api, source=source,
        roster_loader=lambda: roster(cleaner(status="INACTIVE")), now=NOW,
    )
    assert result == "my_schedule_unauthorized"
    assert source.calls == []


def test_non_private_or_bot_sender_is_safe_and_does_not_query():
    source = FakeSource([cleaning_page()])
    api = CaptureApi()
    assert my.handle_my_schedule(
        update(chat_type="group"), "token", request_api=api, source=source,
        roster_loader=lambda: roster(cleaner()), now=NOW,
    ) == "my_schedule_unauthorized"
    assert my.handle_my_schedule(
        update(is_bot=True), "token", request_api=api, source=source,
        roster_loader=lambda: roster(cleaner()), now=NOW,
    ) == "my_schedule_unauthorized"
    assert source.calls == [] and api.calls == []


def test_unknown_or_ambiguous_domain_states_fail_closed():
    assert schedule([cleaning_page(state="재방문 필요")]) == []
    assert schedule([cleaning_page(state="담당자 배정", assignment_state="PROPOSING")]) == []
    assert schedule([cleaning_page(state="담당자 배정", assignment_state="OPEN")]) == []


def test_recent_history_is_bounded_to_30_days_and_10_items():
    pages = [
        cleaning_page(
            f"recent-{offset:02d}", state="완료 보고",
            day=f"2026-08-{27-offset:02d}",
            start=f"2026-08-{27-offset:02d}T11:00:00+09:00",
            end=f"2026-08-{27-offset:02d}T15:00:00+09:00",
        )
        for offset in range(11)
    ]
    pages.append(cleaning_page(
        "too-old", state="완료 보고", day="2026-07-01",
        start="2026-07-01T11:00:00+09:00", end="2026-07-01T15:00:00+09:00",
    ))
    items = schedule(pages)
    assert len(items) == my.RECENT_HISTORY_LIMIT
    assert "recent-10" not in {item["page_id"] for item in items}
    assert "too-old" not in {item["page_id"] for item in items}


def test_current_schedule_is_bounded_to_20_items():
    pages = [
        cleaning_page(
            f"future-{idx:02d}", day=f"2026-09-{idx+1:02d}",
            start=f"2026-09-{idx+1:02d}T11:00:00+09:00",
            end=f"2026-09-{idx+1:02d}T15:00:00+09:00",
        )
        for idx in range(21)
    ]
    items = schedule(pages)
    assert len(items) == my.CURRENT_DISPLAY_LIMIT
    assert items[0]["page_id"] == "future-00"
    assert items[-1]["page_id"] == "future-19"


def test_source_overflow_fails_instead_of_silently_truncating_schedule():
    pages = [cleaning_page(f"p-{idx}") for idx in range(my.MAX_SOURCE_ROWS + 1)]
    with pytest.raises(RuntimeError, match="unbounded"):
        schedule(pages)


def test_31_reply_scoped_workflow_precedes_my_alias(monkeypatch, tmp_path):
    from telegram_approval import cleaning_issue

    monkeypatch.setattr(cleaning_issue, "capture_issue_message", lambda *args, **kwargs: "issue_text_saved")
    monkeypatch.setattr(my, "NotionMyScheduleReader", lambda: (_ for _ in ()).throw(AssertionError("must not query")))
    api = CaptureApi()
    result = bot_service.handle_cleaner(
        update("내 청소", extra_message={"reply_to_message": {"message_id": 99}}),
        "token", request_api=api, request_dir=tmp_path, now=NOW,
    )
    assert result == "issue_text_saved"
    assert api.calls == []


def test_32_query_path_is_read_only_and_uses_server_side_own_party_filter(tmp_path):
    token = tmp_path / "token"
    token.write_text("synthetic-token")
    captured = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return json.dumps({"results": [], "has_more": False, "next_cursor": None}).encode()

    def urlopen(request, timeout):
        captured.append((request, timeout))
        return Response()

    reader = my.NotionMyScheduleReader(token_path=token, urlopen=urlopen)
    assert reader.query_for_cleaner("party-cleaner") == []
    request, timeout = captured[0]
    body = json.loads(request.data)
    assert request.get_method() == "POST"
    assert request.full_url.endswith(f"/v1/data_sources/{CLEANING_SOURCE_ID}/query")
    assert timeout == 40
    assert {"property": "담당 참여자/업체", "relation": {"contains": "party-cleaner"}} in body["filter"]["and"]
    assert body["page_size"] == my.MAX_SOURCE_ROWS
    assert "/pages" not in request.full_url
    assert "PATCH" not in request.get_method() and "DELETE" not in request.get_method()


def test_query_and_render_do_not_mutate_roster_or_source_business_data():
    roster_value = roster(cleaner())
    pages = [cleaning_page()]
    roster_before = copy.deepcopy(roster_value)
    pages_before = copy.deepcopy(pages)
    source = FakeSource(pages)
    api = CaptureApi()
    result = my.handle_my_schedule(
        update(), "token", request_api=api, source=source,
        roster_loader=lambda: roster_value, now=NOW,
    )
    assert result == "my_schedule_listed"
    assert roster_value == roster_before
    assert pages == pages_before
