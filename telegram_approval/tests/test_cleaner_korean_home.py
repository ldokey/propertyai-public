import json

import pytest

from telegram_approval import bot_service, cleaner_home, cleaner_jobs


def cleaner(*, user_id=202, chat_id=202, status="ACTIVE", role="CLEANER"):
    return {
        "identity_id": "worker",
        "role": role,
        "status": status,
        "telegram_user_id": user_id,
        "telegram_chat_id": chat_id,
        "party_page_id": "party-cleaner",
        "properties": ["JJ"],
        "priority_by_property": {"JJ": 1},
    }


def roster(*entries):
    return {"schema_version": 1, "cleaners": list(entries)}


def message_update(text, *, user_id=202, chat_id=202, extra_message=None):
    message = {
        "from": {"id": user_id, "is_bot": False},
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
    }
    if extra_message:
        message.update(extra_message)
    return {"message": message}


class CaptureApi:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, **values):
        self.calls.append((token, method, values))
        return {"message_id": len(self.calls)}

    @property
    def sent(self):
        return [values for _token, method, values in self.calls if method == "sendMessage"]


@pytest.fixture(autouse=True)
def _active_roster(monkeypatch):
    monkeypatch.setattr(cleaner_home, "load_roster", lambda: roster(cleaner()))


def test_active_cleaner_start_renders_korean_home_with_only_implemented_capabilities():
    api = CaptureApi()
    result = cleaner_home.handle_home_navigation(message_update("/start"), "token", request_api=api)
    assert result == "cleaner_home"
    assert api.sent[-1]["text"] == cleaner_home.HOME_MESSAGE
    markup = json.loads(api.sent[-1]["reply_markup"])
    buttons = [item["text"] for row in markup["keyboard"] for item in row]
    assert buttons == [
        "🧹 신청 가능한 청소",
        "📅 내 청소",
        "🏠 내 숙소",
        "❓ 도움말",
    ]
    assert markup["resize_keyboard"] is True


@pytest.mark.parametrize(
    "source,expected",
    [
        ("🧹 신청 가능한 청소", "/jobs"),
        ("신청 가능한 청소", "/jobs"),
        ("청소", "/jobs"),
        ("청소 찾기", "/jobs"),
        ("📅 내 청소", "/my"),
        ("내 청소", "/my"),
        ("내 일정", "/my"),
        ("🏠 내 숙소", "/properties"),
        ("내 숙소", "/properties"),
        ("숙소", "/properties"),
        ("  청소 찾기  ", "/jobs"),
        ("\n 내 숙소 \t", "/properties"),
    ],
)
def test_deterministic_alias_and_whitespace_normalization(source, expected):
    normalized = cleaner_home.alias_command_update(message_update(source))
    assert normalized["message"]["text"] == expected


def test_jobs_button_and_slash_reuse_same_existing_handler(monkeypatch, tmp_path):
    calls = []

    def fake_jobs(update, token, **kwargs):
        if update.get("message", {}).get("text") != "/jobs":
            return None
        calls.append(update["message"]["text"])
        kwargs["request_api"](token, "sendMessage", chat_id=202, text="same jobs result")
        return "jobs_listed"

    monkeypatch.setattr(cleaner_jobs, "handle_jobs", fake_jobs)
    api = CaptureApi()
    slash_result = bot_service.handle_cleaner(message_update("/jobs"), "token", request_api=api, request_dir=tmp_path)
    slash_values = dict(api.sent[-1])
    api.calls.clear()
    button_result = bot_service.handle_cleaner(
        message_update(cleaner_home.JOBS_BUTTON), "token", request_api=api, request_dir=tmp_path
    )
    assert slash_result == button_result == "jobs_listed"
    assert api.sent[-1] == slash_values
    assert calls == ["/jobs", "/jobs"]


def test_properties_button_and_slash_reuse_same_existing_handler(monkeypatch, tmp_path):
    from telegram_approval import cleaner_property_access

    calls = []

    def fake_properties(update, token, **kwargs):
        if update.get("message", {}).get("text") != "/properties":
            return None
        calls.append(update["message"]["text"])
        kwargs["request_api"](token, "sendMessage", chat_id=202, text="same properties result")
        return "properties_listed"

    monkeypatch.setattr(cleaner_property_access, "handle_properties", fake_properties)
    api = CaptureApi()
    slash_result = bot_service.handle_cleaner(
        message_update("/properties"), "token", request_api=api, request_dir=tmp_path
    )
    slash_values = dict(api.sent[-1])
    api.calls.clear()
    button_result = bot_service.handle_cleaner(
        message_update(cleaner_home.PROPERTIES_BUTTON), "token", request_api=api, request_dir=tmp_path
    )
    assert slash_result == button_result == "properties_listed"
    assert api.sent[-1] == slash_values
    assert calls == ["/properties", "/properties"]


def test_unauthorized_user_cannot_bypass_jobs_auth_with_korean_alias(monkeypatch, tmp_path):
    monkeypatch.setattr(cleaner_jobs, "load_roster", lambda: roster())
    api = CaptureApi()
    result = bot_service.handle_cleaner(
        message_update("청소"), "token", request_api=api, request_dir=tmp_path
    )
    assert result == "jobs_unauthorized"
    assert api.sent[-1]["text"] == cleaner_jobs.UNAUTHORIZED_MESSAGE


@pytest.mark.parametrize("text", ["메뉴", "홈", "시작"])
def test_home_aliases_render_same_korean_home(text):
    api = CaptureApi()
    result = cleaner_home.handle_home_navigation(message_update(text), "token", request_api=api)
    assert result == "cleaner_home"
    assert api.sent[-1]["text"] == cleaner_home.HOME_MESSAGE


@pytest.mark.parametrize("text", ["❓ 도움말", "도움말"])
def test_help_is_korean_first_and_keeps_home_keyboard(text):
    api = CaptureApi()
    result = cleaner_home.handle_home_navigation(message_update(text), "token", request_api=api)
    assert result == "cleaner_help"
    assert "신청 가능한 청소" in api.sent[-1]["text"]
    assert "내 청소" in api.sent[-1]["text"]
    assert "내 숙소" in api.sent[-1]["text"]
    assert "runtime" not in api.sent[-1]["text"]
    assert "handler" not in api.sent[-1]["text"]
    assert "ledger" not in api.sent[-1]["text"]
    assert "reply_markup" in api.sent[-1]


def test_unknown_safe_text_returns_navigation_guidance_for_active_cleaner():
    api = CaptureApi()
    result = cleaner_home.handle_home_navigation(message_update("이건 어떻게 하나요?"), "token", request_api=api)
    assert result == "cleaner_navigation_guidance"
    assert api.sent[-1]["text"] == cleaner_home.NAVIGATION_GUIDANCE_MESSAGE
    assert "reply_markup" in api.sent[-1]


def test_menu_is_authorized_without_mutating_roster():
    value = roster(cleaner())
    before = json.dumps(value, sort_keys=True)
    api = CaptureApi()
    result = cleaner_home.handle_home_navigation(
        message_update("/start"), "token", request_api=api, roster_loader=lambda: value
    )
    assert result == "cleaner_home"
    assert json.dumps(value, sort_keys=True) == before


def test_home_and_help_do_not_expose_sensitive_fields():
    combined = cleaner_home.HOME_MESSAGE + "\n" + cleaner_home.HELP_MESSAGE
    for forbidden in (
        "SECRET_STREET_ADDRESS",
        "SECRET_DOOR_CODE",
        "Guest PII",
        "Reservation ID",
        "other Cleaner",
        "rank",
        "internal page",
    ):
        assert forbidden not in combined


def test_unknown_slash_command_remains_unhandled():
    api = CaptureApi()
    assert cleaner_home.handle_home_navigation(message_update("/unknown"), "token", request_api=api) is None
    assert api.calls == []


def test_callback_flow_precedes_korean_navigation(monkeypatch, tmp_path):
    from telegram_approval import cleaner_application

    monkeypatch.setattr(cleaner_application, "handle_application_callback", lambda *args, **kwargs: "application_callback")
    api = CaptureApi()
    update = {
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 202, "is_bot": False},
            "message": {"chat": {"id": 202, "type": "private"}},
            "data": "ca1:cleaning-1",
        }
    }
    assert bot_service.handle_cleaner(update, "token", request_api=api, request_dir=tmp_path) == "application_callback"
    assert api.calls == []


def test_plain_start_reaches_home_through_cleaner_composition(tmp_path):
    api = CaptureApi()
    result = bot_service.handle_cleaner(
        message_update("/start"), "token", request_api=api, request_dir=tmp_path
    )
    assert result == "cleaner_home"
    assert api.sent[-1]["text"] == cleaner_home.HOME_MESSAGE
    assert "reply_markup" in api.sent[-1]


def test_reply_scoped_capture_precedes_korean_alias(monkeypatch, tmp_path):
    from telegram_approval import cleaning_issue

    monkeypatch.setattr(cleaning_issue, "capture_issue_message", lambda *args, **kwargs: "issue_text_saved")
    api = CaptureApi()
    result = bot_service.handle_cleaner(
        message_update("내 청소", extra_message={"reply_to_message": {"message_id": 99}}),
        "token",
        request_api=api,
        request_dir=tmp_path,
    )
    assert result == "issue_text_saved"
    assert api.calls == []
