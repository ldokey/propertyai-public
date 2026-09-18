import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from telegram_approval import bot_service, cleaner_property_access as access, cleaner_registry
from telegram_approval.ops_bot_service import OpsTelegramRouter
from telegram_approval.ops_config import OPS_ALLOWLIST_CONFIG, OpsAllowlistProvider, OpsCredentialProvider


NOW = datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc)
MAPPINGS = {"property-a": "JJ House", "property-b": "Browny House"}


def _select(value):
    return {"type": "select", "select": {"name": value} if value is not None else None}


def _checkbox(value):
    return {"type": "checkbox", "checkbox": value}


def _relation(*values):
    return {"type": "relation", "relation": [{"id": value} for value in values]}


def _rich(value):
    return {"type": "rich_text", "rich_text": [{"plain_text": value}]}


def _number(value):
    return {"type": "number", "number": value}


def rental_unit(
    page_id="unit-a",
    *,
    property_ids=("property-a",),
    environment="PRODUCTION",
    registration="APPROVED",
    state="운영중",
    mode="Airbnb",
    cleaning_active=True,
):
    return {
        "id": page_id,
        "properties": {
            "데이터 환경": _select(environment),
            "등록 상태": _select(registration),
            "상태": _select(state),
            "운영 모드": _select(mode),
            "청소 운영 활성": _checkbox(cleaning_active),
            "연결 집": _relation(*property_ids),
            "숙소 닉네임": _rich("SAFE-ish LABEL MUST NOT AUTHORIZE"),
            "도로명주소": _rich("SECRET_STREET_ADDRESS"),
            "Door Code": _rich("SECRET_DOOR_CODE"),
            "Guest PII": _rich("SECRET_GUEST_PII"),
            "예약번호": _rich("SECRET_BOOKING_REF"),
        },
    }


def relation_page(page_id):
    return {"id": page_id, "properties": {}}


def cleaner(
    *,
    user_id=202,
    chat_id=202,
    party_page_id="party-cleaner",
    status="ACTIVE",
    role="CLEANER",
    properties=None,
    priority=None,
    label="Cleaner A",
    identity_id="cleaner-a",
):
    return {
        "identity_id": identity_id,
        "label": label,
        "role": role,
        "status": status,
        "telegram_user_id": user_id,
        "telegram_chat_id": chat_id,
        "party_page_id": party_page_id,
        "properties": [] if properties is None else list(properties),
        "priority_by_property": {} if priority is None else dict(priority),
    }


def roster(*items):
    return {"schema_version": 1, "cleaners": list(items)}


def access_row(
    *,
    page_id="access-a",
    party_page_id="party-cleaner",
    property_page_id="property-a",
    status="REQUESTED",
    sync="NOT_REQUIRED",
    key=None,
    priority=None,
    environment="PRODUCTION",
):
    key = key or access.property_access_idempotency_key(party_page_id, property_page_id)
    props = {
        "인력": _relation(party_page_id),
        "집": _relation(property_page_id),
        "Idempotency Key": _rich(key),
        "신청 상태": _select(status),
        "Runtime Sync Status": _select(sync),
        "데이터 환경": _select(environment),
    }
    if priority is not None:
        props["우선순위"] = _number(priority)
    return {"id": page_id, "properties": props}


class FakeSource:
    def __init__(self, units=(), pages=None):
        self.units = list(units)
        self.pages = dict(pages or {})
        for unit in self.units:
            self.pages.setdefault(unit["id"], unit)
        self.query_count = 0
        self.get_calls = []

    def query_candidate_units(self):
        self.query_count += 1
        return list(self.units)

    def get_page(self, page_id):
        self.get_calls.append(page_id)
        return self.pages[page_id]


class FakeLedger:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.create_count = 0
        self.events = []
        self.last_synced_at = None
        self._guard = threading.Lock()

    @staticmethod
    def _relation_id(row, name):
        return row["properties"][name]["relation"][0]["id"]

    @staticmethod
    def _key(row):
        return row["properties"]["Idempotency Key"]["rich_text"][0]["plain_text"]

    def query_by_identity(self, party_page_id, property_page_id):
        with self._guard:
            return [
                row for row in self.rows
                if self._relation_id(row, "인력") == party_page_id
                and self._relation_id(row, "집") == property_page_id
            ]

    def query_by_idempotency(self, key):
        with self._guard:
            return [row for row in self.rows if self._key(row) == key]

    def create_request(self, **values):
        with self._guard:
            self.create_count += 1
            row = access_row(
                page_id=f"access-{self.create_count}",
                party_page_id=values["party_page_id"],
                property_page_id=values["property_page_id"],
                key=values["key"],
            )
            self.rows.append(row)
            self.events.append(("create", row["id"], "REQUESTED", "NOT_REQUIRED"))
            return row

    def get_access(self, page_id):
        with self._guard:
            self.events.append(("read", page_id))
            return next(row for row in self.rows if row["id"] == page_id)

    def approve(self, page_id, approved_at):
        with self._guard:
            row = next(row for row in self.rows if row["id"] == page_id)
            row["properties"]["신청 상태"] = _select("APPROVED")
            row["properties"]["Runtime Sync Status"] = _select("PENDING")
            row["properties"]["우선순위"] = _number(access.SAFE_NEUTRAL_PRIORITY)
            row["properties"]["승인일"] = {"date": {"start": approved_at.isoformat()}}
            self.events.append(("approve", page_id, "APPROVED", "PENDING"))
            return row

    def reject(self, page_id):
        with self._guard:
            row = next(row for row in self.rows if row["id"] == page_id)
            row["properties"]["신청 상태"] = _select("REJECTED")
            row["properties"]["Runtime Sync Status"] = _select("NOT_REQUIRED")
            self.events.append(("reject", page_id, "REJECTED", "NOT_REQUIRED"))
            return row

    def set_sync_status(self, page_id, status, *, synced_at=None):
        with self._guard:
            row = next(row for row in self.rows if row["id"] == page_id)
            row["properties"]["Runtime Sync Status"] = _select(status)
            if status == "SYNCED":
                self.last_synced_at = synced_at
                row["properties"]["Last Synced At"] = {"date": {"start": synced_at.isoformat()}}
            self.events.append(("sync", page_id, status))
            return row


class CaptureApi:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, **values):
        self.calls.append((token, method, values))
        return {"message_id": len(self.calls)}

    @property
    def sent_texts(self):
        return [values["text"] for _token, method, values in self.calls if method == "sendMessage"]


def properties_update(user_id=202, chat_id=202):
    return {
        "message": {
            "from": {"id": user_id, "is_bot": False},
            "chat": {"id": chat_id, "type": "private"},
            "text": "/properties",
        }
    }


def request_update(unit_id="unit-a", user_id=202, chat_id=202, callback_id="cb-1", message_id=10):
    return {
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id, "is_bot": False},
            "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}},
            "data": access.property_request_callback_data(unit_id),
        }
    }


def ops_update(access_id="access-a", decision="approve", chat_id=101):
    return {
        "callback_query": {
            "id": "ops-cb",
            "from": {"id": 101, "is_bot": False},
            "message": {"message_id": 20, "chat": {"id": chat_id, "type": "private"}},
            "data": access.ops_access_callback_data(access_id, decision),
        }
    }


def source_for_approval(unit=None):
    unit = unit or rental_unit()
    return FakeSource(
        [unit],
        pages={
            "property-a": relation_page("property-a"),
            "property-b": relation_page("property-b"),
            "party-cleaner": relation_page("party-cleaner"),
            unit["id"]: unit,
        },
    )


def write_roster(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))


def test_01_new_cleaner_can_register_with_zero_properties_and_get_v2_onboarding(tmp_path, monkeypatch):
    invite_dir = tmp_path / "invites"
    roster_path = tmp_path / "cleaners.json"
    metadata_path = tmp_path / "metadata.json"
    invite_dir.mkdir()
    metadata_path.write_text(json.dumps({"bot_username": "synthetic_bot"}))
    monkeypatch.setattr(cleaner_registry, "INVITE_DIR", invite_dir)
    monkeypatch.setattr(cleaner_registry, "ROSTER_PATH", roster_path)
    monkeypatch.setattr(cleaner_registry, "METADATA_PATH", metadata_path)
    created = cleaner_registry.create_invite(
        label="Cleaner A", party_page_id="party-cleaner", properties=[], priority=999
    )
    code = created["url"].split("?start=c_", 1)[1]
    api = CaptureApi()
    result = bot_service.pair(
        {"message": {"from": {"id": 202, "is_bot": False}, "chat": {"id": 202, "type": "private"}, "text": f"/start c_{code}"}},
        "token",
        cleaner_only=True,
        request_api=api,
    )
    saved = json.loads(roster_path.read_text())["cleaners"][0]
    assert result == "cleaner_paired"
    assert saved["properties"] == []
    assert saved["priority_by_property"] == {}
    assert api.sent_texts[-1] == "✅ PropertyAI 청소 담당자 등록 완료\n\n현재 승인된 숙소: 0곳\n\n숙소 신청:\n/properties"


def test_02_zero_property_onboarding_preserves_existing_roster(tmp_path, monkeypatch):
    invite_dir = tmp_path / "invites"
    roster_path = tmp_path / "cleaners.json"
    metadata_path = tmp_path / "metadata.json"
    invite_dir.mkdir()
    existing = cleaner(user_id=303, chat_id=303, party_page_id="existing-party", properties=["JJ House"], priority={"JJ House": 1})
    write_roster(roster_path, roster(existing))
    metadata_path.write_text(json.dumps({"bot_username": "synthetic_bot"}))
    monkeypatch.setattr(cleaner_registry, "INVITE_DIR", invite_dir)
    monkeypatch.setattr(cleaner_registry, "ROSTER_PATH", roster_path)
    monkeypatch.setattr(cleaner_registry, "METADATA_PATH", metadata_path)
    created = cleaner_registry.create_invite(label="New", party_page_id="new-party", properties=[], priority=999)
    code = created["url"].split("?start=c_", 1)[1]
    assert cleaner_registry.consume_invite(code, telegram_user_id=202, telegram_chat_id=202)
    saved = json.loads(roster_path.read_text())["cleaners"]
    assert saved[0]["party_page_id"] == "existing-party"
    assert saved[0]["properties"] == ["JJ House"]
    assert len(saved) == 2


@pytest.mark.parametrize(
    "roster_value,user_id,chat_id",
    [
        (roster(), 202, 202),
        (roster(cleaner(status="PAUSED")), 202, 202),
        (roster(cleaner()), 999, 202),
        (roster(cleaner()), 202, 999),
        (roster(cleaner(), cleaner(identity_id="duplicate")), 202, 202),
    ],
    ids=["unknown", "inactive", "wrong-user", "wrong-chat", "ambiguous"],
)
def test_03_to_07_properties_identity_fails_closed(roster_value, user_id, chat_id):
    source = FakeSource([rental_unit()])
    api = CaptureApi()
    result = access.handle_properties(
        properties_update(user_id, chat_id),
        "token",
        request_api=api,
        source=source,
        ledger=FakeLedger(),
        roster_loader=lambda: roster_value,
        mappings=MAPPINGS,
    )
    assert result == "properties_unauthorized"
    assert source.query_count == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "TEST"},
        {"registration": "TEMPORARY"},
        {"registration": "REVIEW_REQUIRED"},
        {"registration": "REJECTED"},
        {"registration": "ARCHIVED"},
        {"state": "준비중"},
        {"state": "일시중지"},
        {"state": "종료"},
        {"mode": "쉐어하우스"},
        {"cleaning_active": False},
    ],
)
def test_08_to_11_only_production_approved_operating_airbnb_cleaning_active_units_are_considered(overrides):
    good = rental_unit("unit-good")
    bad = rental_unit("unit-bad", **overrides)
    candidates = access.discover_properties(
        cleaner=cleaner(), source=FakeSource([bad, good]), ledger=FakeLedger(), mappings=MAPPINGS
    )
    assert [item["rental_unit_page_id"] for item in candidates] == ["unit-good"]



@pytest.mark.parametrize("state", ["모집중", "예약중", "입주중", "공실", "운영중"])
def test_known_airbnb_operational_states_remain_eligible(state):
    result = access.discover_properties(
        cleaner=cleaner(), source=FakeSource([rental_unit(state=state)]),
        ledger=FakeLedger(), mappings=MAPPINGS,
    )
    assert len(result) == 1
    assert result[0]["property_page_id"] == "property-a"

def test_12_missing_property_relation_fails_closed():
    with pytest.raises(access.PropertyAccessError):
        access.discover_properties(
            cleaner=cleaner(), source=FakeSource([rental_unit(property_ids=())]), ledger=FakeLedger(), mappings=MAPPINGS
        )


def test_13_ambiguous_property_relation_fails_closed():
    with pytest.raises(access.PropertyAccessError):
        access.discover_properties(
            cleaner=cleaner(), source=FakeSource([rental_unit(property_ids=("property-a", "property-b"))]), ledger=FakeLedger(), mappings=MAPPINGS
        )


def test_14_multiple_rental_units_same_property_deduplicate_to_one_property():
    result = access.discover_properties(
        cleaner=cleaner(),
        source=FakeSource([rental_unit("unit-2"), rental_unit("unit-1")]),
        ledger=FakeLedger(),
        mappings=MAPPINGS,
    )
    assert len(result) == 1
    assert result[0]["property_page_id"] == "property-a"
    assert result[0]["rental_unit_page_id"] == "unit-1"


def test_15_distinct_properties_remain_distinct():
    result = access.discover_properties(
        cleaner=cleaner(),
        source=FakeSource([rental_unit("unit-a"), rental_unit("unit-b", property_ids=("property-b",))]),
        ledger=FakeLedger(),
        mappings=MAPPINGS,
    )
    assert {item["property_page_id"] for item in result} == {"property-a", "property-b"}


def test_16_one_cleaner_can_request_two_properties_independently():
    source = FakeSource(
        [rental_unit("unit-a"), rental_unit("unit-b", property_ids=("property-b",))]
    )
    ledger = FakeLedger()
    worker = cleaner()
    for unit in ("unit-a", "unit-b"):
        assert access.request_property_access(
            cleaner=worker, rental_unit_page_id=unit, source_message_ref=f"ref-{unit}", source=source,
            ledger=ledger, mappings=MAPPINGS, now=NOW
        ) == "created"
    assert ledger.create_count == 2
    assert {FakeLedger._relation_id(row, "집") for row in ledger.rows} == {"property-a", "property-b"}


def test_17_requesting_property_a_does_not_modify_property_b():
    existing_b = access_row(page_id="access-b", property_page_id="property-b", status="REQUESTED")
    ledger = FakeLedger([existing_b])
    source = FakeSource([rental_unit("unit-a")])
    before_b = json.dumps(existing_b, sort_keys=True)
    assert access.request_property_access(
        cleaner=cleaner(), rental_unit_page_id="unit-a", source_message_ref="ref", source=source,
        ledger=ledger, mappings=MAPPINGS, now=NOW
    ) == "created"
    assert json.dumps(existing_b, sort_keys=True) == before_b


def test_18_to_21_request_creates_exact_relations_requested_and_not_required():
    ledger = FakeLedger()
    result = access.request_property_access(
        cleaner=cleaner(), rental_unit_page_id="unit-a", source_message_ref="ref",
        source=FakeSource([rental_unit("unit-a")]), ledger=ledger,
        mappings=MAPPINGS, now=NOW
    )
    row = ledger.rows[0]
    assert result == "created"
    assert FakeLedger._relation_id(row, "인력") == "party-cleaner"
    assert FakeLedger._relation_id(row, "집") == "property-a"
    assert row["properties"]["신청 상태"]["select"]["name"] == "REQUESTED"
    assert row["properties"]["Runtime Sync Status"]["select"]["name"] == "NOT_REQUIRED"


def test_22_idempotency_key_is_deterministic_and_message_id_independent():
    expected = "CLEANER_PROPERTY_ACCESS:party-cleaner:property-a:V1"
    assert access.property_access_idempotency_key("party-cleaner", "property-a") == expected
    assert access.property_access_idempotency_key("party-cleaner", "property-a") == expected


def test_23_duplicate_callback_creates_zero_additional_rows():
    ledger = FakeLedger()
    source = FakeSource([rental_unit("unit-a")])
    worker = cleaner()
    first = access.request_property_access(
        cleaner=worker, rental_unit_page_id="unit-a", source_message_ref="ref-1", source=source,
        ledger=ledger, mappings=MAPPINGS, now=NOW
    )
    second = access.request_property_access(
        cleaner=worker, rental_unit_page_id="unit-a", source_message_ref="ref-2", source=source,
        ledger=ledger, mappings=MAPPINGS, now=NOW
    )
    assert (first, second, ledger.create_count) == ("created", "existing", 1)


def test_24_refreshed_properties_marks_requested_and_does_not_create():
    row = access_row()
    ledger = FakeLedger([row])
    result = access.discover_properties(
        cleaner=cleaner(), source=FakeSource([rental_unit()]), ledger=ledger, mappings=MAPPINGS
    )
    assert result[0]["status"] == "REQUESTED"
    assert ledger.create_count == 0


def test_25_concurrent_duplicate_creates_one_logical_row():
    ledger = FakeLedger()
    source = FakeSource([rental_unit()])
    worker = cleaner()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: access.request_property_access(
            cleaner=worker, rental_unit_page_id="unit-a", source_message_ref=f"ref-{i}", source=source,
            ledger=ledger, mappings=MAPPINGS, now=NOW
        ), range(12)))
    assert results.count("created") == 1
    assert results.count("existing") == 11
    assert ledger.create_count == 1


def test_26_duplicate_conflicting_rows_fail_closed():
    ledger = FakeLedger([access_row(page_id="access-1"), access_row(page_id="access-2")])
    result = access.request_property_access(
        cleaner=cleaner(), rental_unit_page_id="unit-a", source_message_ref="ref",
        source=FakeSource([rental_unit()]), ledger=ledger,
        mappings=MAPPINGS, now=NOW
    )
    assert result == "failed"
    assert ledger.create_count == 0


def test_26b_same_idempotency_key_with_mismatched_relation_fails_closed():
    key = access.property_access_idempotency_key("party-cleaner", "property-a")
    mismatch = access_row(page_id="access-x", property_page_id="property-b", key=key)
    ledger = FakeLedger([mismatch])
    result = access.request_property_access(
        cleaner=cleaner(), rental_unit_page_id="unit-a", source_message_ref="ref",
        source=FakeSource([rental_unit()]), ledger=ledger,
        mappings=MAPPINGS, now=NOW
    )
    assert result == "failed"


def test_27_cleaner_request_succeeds_without_ops_credentials(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Cleaner request must not construct OPS providers")

    monkeypatch.setattr(OpsCredentialProvider, "__init__", forbidden)
    monkeypatch.setattr(OpsAllowlistProvider, "__init__", forbidden)
    ledger = FakeLedger()
    assert access.request_property_access(
        cleaner=cleaner(), rental_unit_page_id="unit-a", source_message_ref="ref",
        source=FakeSource([rental_unit()]), ledger=ledger, mappings=MAPPINGS, now=NOW
    ) == "created"
    assert ledger.create_count == 1
    assert not hasattr(access, "_default_ops_notifier")


def test_28_cleaner_request_is_durable_and_duplicate_safe_without_ops_delivery():
    ledger = FakeLedger()
    source = FakeSource([rental_unit()])
    first = access.request_property_access(
        cleaner=cleaner(), rental_unit_page_id="unit-a", source_message_ref="ref",
        source=source, ledger=ledger, mappings=MAPPINGS, now=NOW
    )
    second = access.request_property_access(
        cleaner=cleaner(), rental_unit_page_id="unit-a", source_message_ref="ref2",
        source=source, ledger=ledger, mappings=MAPPINGS, now=NOW
    )
    assert first == "created"
    assert second == "existing"
    assert ledger.create_count == 1

def test_29_unauthorized_ops_chat_cannot_reach_approval_handler(tmp_path):
    token_path = tmp_path / "ops.token"
    token_path.write_text("synthetic")
    calls = []
    class Transport:
        def request(self, *_args, **_kwargs):
            return True
    router = OpsTelegramRouter(
        credential_provider=OpsCredentialProvider(token_path=token_path),
        allowlist_provider=OpsAllowlistProvider(environment={OPS_ALLOWLIST_CONFIG: "101"}),
        transport=Transport(),
        callback_handler=lambda update: calls.append(update) or "approved",
    )
    assert router.route(ops_update(chat_id=999)) == "ignored"
    assert calls == []


def _approval_setup(tmp_path, *, row=None, worker=None):
    row = row or access_row()
    ledger = FakeLedger([row])
    worker = worker or cleaner()
    roster_path = tmp_path / "cleaners.json"
    write_roster(roster_path, roster(worker, cleaner(
        user_id=303, chat_id=303, party_page_id="other-party", identity_id="other",
        properties=["JJ House"], priority={"JJ House": 1}, label="Preferred"
    )))
    source = source_for_approval()
    return ledger, roster_path, source


def test_30_stale_operator_callback_fresh_reads_current_access_state(tmp_path):
    row = access_row(status="REJECTED")
    ledger, roster_path, source = _approval_setup(tmp_path, row=row)
    result = access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    )
    assert result == "not_requestable"
    assert any(event[0] == "read" for event in ledger.events)
    assert "property-a" in source.get_calls and "party-cleaner" in source.get_calls


def test_31_approval_only_normally_transitions_requested(tmp_path):
    row = access_row(status="REVOKED")
    ledger, roster_path, source = _approval_setup(tmp_path, row=row)
    result = access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    )
    assert result == "not_requestable"
    assert not any(event[0] == "approve" for event in ledger.events)


def test_32_33_approval_sets_approved_and_pending_before_projection(tmp_path, monkeypatch):
    ledger, roster_path, source = _approval_setup(tmp_path)
    observed = []
    original = access._project_property_to_roster
    def wrapped(**kwargs):
        row = access._access_row(ledger.get_access("access-a"))
        observed.append((row["status"], row["sync_status"]))
        return original(**kwargs)
    monkeypatch.setattr(access, "_project_property_to_roster", wrapped)
    result = access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    )
    assert result == "approved_synced"
    assert observed == [("APPROVED", "PENDING")]


def test_34_neutral_priority_is_999_and_does_not_outrank_existing_preferred(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    assert access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    ) == "approved_synced"
    saved = json.loads(roster_path.read_text())["cleaners"]
    target = next(item for item in saved if item["party_page_id"] == "party-cleaner")
    preferred = next(item for item in saved if item["party_page_id"] == "other-party")
    assert target["priority_by_property"]["JJ House"] == 999
    assert preferred["priority_by_property"]["JJ House"] == 1


def test_35_projection_uses_canonical_property_mapping_not_callback_or_unit_label(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    assert access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    ) == "approved_synced"
    target = json.loads(roster_path.read_text())["cleaners"][0]
    assert target["properties"] == ["JJ House"]
    assert "SAFE-ish LABEL MUST NOT AUTHORIZE" not in target["properties"]


def test_36_projection_preserves_existing_properties(tmp_path):
    worker = cleaner(properties=["Browny House"], priority={"Browny House": 5})
    ledger, roster_path, source = _approval_setup(tmp_path, worker=worker)
    access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    )
    target = json.loads(roster_path.read_text())["cleaners"][0]
    assert target["properties"] == ["Browny House", "JJ House"]
    assert target["priority_by_property"]["Browny House"] == 5


def test_37_projection_deduplicates_property(tmp_path):
    path = tmp_path / "cleaners.json"
    worker = cleaner(properties=["JJ House", "JJ House"], priority={"JJ House": 999})
    write_roster(path, roster(worker))
    access._project_property_to_roster(roster_path=path, party_page_id="party-cleaner", property_label="JJ House")
    assert json.loads(path.read_text())["cleaners"][0]["properties"] == ["JJ House"]


def test_38_only_target_cleaner_changes(tmp_path):
    worker = cleaner(properties=["Browny House"], priority={"Browny House": 5})
    ledger, roster_path, source = _approval_setup(tmp_path, worker=worker)
    before_other = json.loads(roster_path.read_text())["cleaners"][1]
    access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    )
    after_other = json.loads(roster_path.read_text())["cleaners"][1]
    assert after_other == before_other


def test_39_40_projection_failure_sets_failed_and_does_not_notify_cleaner(tmp_path, monkeypatch):
    ledger, roster_path, source = _approval_setup(tmp_path)
    notifications = []
    def fail_projection(**_kwargs):
        raise OSError("synthetic projection failure")
    monkeypatch.setattr(access, "_project_property_to_roster", fail_projection)
    result = access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *args: notifications.append(args), mappings=MAPPINGS, now=NOW
    )
    row = access._access_row(ledger.get_access("access-a"))
    assert result == "reconciliation_required"
    assert (row["status"], row["sync_status"]) == ("APPROVED", "FAILED")
    assert notifications == []


def test_41_42_retry_repairs_failed_projection_and_marks_synced(tmp_path):
    row = access_row(status="APPROVED", sync="FAILED", priority=999)
    ledger, roster_path, source = _approval_setup(tmp_path, row=row)
    result = access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    )
    current = access._access_row(ledger.get_access("access-a"))
    assert result == "approved_synced"
    assert current["sync_status"] == "SYNCED"
    assert json.loads(roster_path.read_text())["cleaners"][0]["properties"] == ["JJ House"]


def test_43_last_synced_at_is_set_after_successful_projection(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    )
    assert ledger.last_synced_at == NOW


def test_44_45_rejection_sets_rejected_not_required_and_does_not_modify_roster(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    before = roster_path.read_text()
    notices = []
    result = access.reject_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()),
        cleaner_notifier=lambda chat, text: notices.append((chat, text)),
    )
    current = access._access_row(ledger.get_access("access-a"))
    assert result == "rejected"
    assert (current["status"], current["sync_status"]) == ("REJECTED", "NOT_REQUIRED")
    assert roster_path.read_text() == before
    assert notices == []


def test_rejection_business_result_is_independent_of_cleaner_notification(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    before = roster_path.read_text()
    calls = []

    def forbidden_notifier(*args):
        calls.append(args)
        raise RuntimeError("OPS must not send Cleaner rejection notification")

    result = access.reject_property_access(
        access_page_id="access-a",
        source=source,
        ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()),
        cleaner_notifier=forbidden_notifier,
    )

    current = access._access_row(ledger.get_access("access-a"))
    assert result == "rejected"
    assert (current["status"], current["sync_status"]) == ("REJECTED", "NOT_REQUIRED")
    assert roster_path.read_text() == before
    assert calls == []
    assert "CleanerCredentialProvider" not in Path(access.__file__).read_text()


def test_ops_rejection_callback_reports_async_cleaner_delivery_without_direct_send(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    api = CaptureApi()
    cleaner_notifications = []

    result = access.handle_ops_property_access_callback(
        ops_update(decision="reject"),
        "synthetic-ops-token",
        request_api=api,
        source=source,
        ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()),
        roster_path=roster_path,
        cleaner_notifier=lambda *args: cleaner_notifications.append(args),
    )

    current = access._access_row(ledger.get_access("access-a"))
    assert result == "property_access_ops_rejected"
    assert (current["status"], current["sync_status"]) == ("REJECTED", "NOT_REQUIRED")
    assert cleaner_notifications == []
    assert api.sent_texts[-1] == (
        "숙소 신청 거절 처리가 완료되었습니다. Cleaner 알림은 별도 전송됩니다."
    )


def test_46_property_a_approval_does_not_grant_property_b(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: None, mappings=MAPPINGS, now=NOW
    )
    target = json.loads(roster_path.read_text())["cleaners"][0]
    assert target["properties"] == ["JJ House"]
    assert "Browny House" not in target["properties"]


def test_47_to_49_property_ux_exposes_no_address_door_code_or_guest_pii():
    candidates = access.discover_properties(
        cleaner=cleaner(), source=FakeSource([rental_unit()]), ledger=FakeLedger(), mappings=MAPPINGS
    )
    text = access.render_properties(candidates)
    markup = access.properties_reply_markup(candidates)
    combined = text + (markup or "")
    assert "JJ House" in combined
    assert "SECRET_STREET_ADDRESS" not in combined
    assert "SECRET_DOOR_CODE" not in combined
    assert "SECRET_GUEST_PII" not in combined
    assert "SECRET_BOOKING_REF" not in combined


def test_request_callback_uses_server_side_candidate_without_ops_notification():
    ledger = FakeLedger()
    api = CaptureApi()
    result = access.handle_property_request_callback(
        request_update(), "token", request_api=api,
        source=FakeSource([rental_unit()]), ledger=ledger,
        roster_loader=lambda: roster(cleaner()), mappings=MAPPINGS, now=NOW,
    )
    assert result == "property_request_created"
    assert ledger.create_count == 1
    assert api.sent_texts[-1] == access.REQUEST_SUCCESS_MESSAGE


def test_already_approved_request_creates_zero_rows_and_returns_exact_message():
    ledger = FakeLedger([access_row(status="APPROVED", sync="SYNCED", priority=999)])
    api = CaptureApi()
    result = access.handle_property_request_callback(
        request_update(), "token", request_api=api, source=FakeSource([rental_unit()]), ledger=ledger,
        roster_loader=lambda: roster(cleaner()), mappings=MAPPINGS, now=NOW,
    )
    assert result == "property_request_approved"
    assert ledger.create_count == 0
    assert api.sent_texts[-1] == access.ALREADY_APPROVED_MESSAGE


def test_rejected_request_cannot_reapply():
    ledger = FakeLedger([access_row(status="REJECTED")])
    result = access.request_property_access(
        cleaner=cleaner(), rental_unit_page_id="unit-a", source_message_ref="ref",
        source=FakeSource([rental_unit()]), ledger=ledger,
        mappings=MAPPINGS, now=NOW,
    )
    assert result == "blocked"
    assert ledger.create_count == 0


def test_approval_business_result_is_independent_of_cleaner_notification(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    notifications = []
    result = access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *_args: notifications.append(True), mappings=MAPPINGS, now=NOW,
    )
    current = access._access_row(ledger.get_access("access-a"))
    assert result == "approved_synced"
    assert (current["status"], current["sync_status"]) == ("APPROVED", "SYNCED")
    assert notifications == []
    assert not hasattr(access, "_default_cleaner_notifier")


def test_ops_notification_renderer_contains_only_safe_labels_and_reference_buttons():
    from telegram_approval.ops_property_access_notifications import (
        render_ops_property_access_notification,
    )

    text, markup = render_ops_property_access_notification("access-a", "Cleaner A", "JJ House")
    assert "Cleaner A" in text and "JJ House" in text
    assert "telegram" not in text.lower()
    assert "SECRET" not in text
    assert "승인" in markup and "거절" in markup
    assert "cpaops1:access-a:approve" in markup
    assert "cpaops1:access-a:reject" in markup

def test_50_jobs_regression_route_is_still_owned_by_existing_handler(monkeypatch, tmp_path):
    from telegram_approval import cleaner_jobs
    called = []
    monkeypatch.setattr(access, "handle_properties", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(access, "handle_property_request_callback", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cleaner_jobs, "handle_jobs", lambda *_args, **_kwargs: called.append(True) or "jobs_listed")
    result = bot_service.handle_cleaner(
        {"message": {"from": {"id": 202}, "chat": {"id": 202, "type": "private"}, "text": "/jobs"}},
        "token", request_dir=tmp_path / "requests", request_api=CaptureApi(), now=NOW,
    )
    assert result == "jobs_listed" and called == [True]


def test_51_cleaning_application_callback_still_precedes_property_access(monkeypatch, tmp_path):
    from telegram_approval import cleaner_application
    monkeypatch.setattr(cleaner_application, "handle_application_callback", lambda *_args, **_kwargs: "application_created")
    monkeypatch.setattr(access, "handle_property_request_callback", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not route")))
    result = bot_service.handle_cleaner(
        {"callback_query": {"data": "ca1:cleaning-1"}}, "token",
        request_dir=tmp_path / "requests", request_api=CaptureApi(), now=NOW,
    )
    assert result == "application_created"


def test_52_existing_offer_callback_falls_through_property_access_router(monkeypatch, tmp_path):
    monkeypatch.setattr(access, "handle_property_request_callback", lambda *_args, **_kwargs: None)
    # Non-property callbacks must keep reaching the existing signed callback
    # implementation; a missing record is an existing fail-closed result.
    result = bot_service.handle_cleaner(
        {"callback_query": {"id": "cb", "from": {"id": 202}, "message": {"chat": {"id": 202}}, "data": "a:missing:approve:bad"}},
        "token", request_dir=tmp_path / "requests", request_api=CaptureApi(), now=NOW,
    )
    assert result == "ignored"


def test_notion_candidate_reader_queries_only_frozen_airbnb_operational_filters(tmp_path):
    import io
    token = tmp_path / "notion.token"
    token.write_text("synthetic")
    requests = []
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): self.close()
    def urlopen(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps({"results": [], "has_more": False, "next_cursor": None}).encode())
    reader = access.NotionPropertyAccessSource(token_path=token, urlopen=urlopen)
    assert reader.query_candidate_units() == []
    req = requests[0]
    body = json.loads(req.data)
    assert req.full_url.endswith(f"/v1/data_sources/{access.RENTAL_UNIT_SOURCE}/query")
    assert {item["property"] for item in body["filter"]["and"]} == {
        "데이터 환경", "등록 상태", "운영 모드", "청소 운영 활성"
    }
    assert {"property": "운영 모드", "select": {"equals": "Airbnb"}} in body["filter"]["and"]
    assert {"property": "청소 운영 활성", "checkbox": {"equals": True}} in body["filter"]["and"]


def test_notion_request_writer_targets_only_32_property_access_schema(tmp_path):
    import io
    token = tmp_path / "notion.token"
    token.write_text("synthetic")
    requests = []
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): self.close()
    def urlopen(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps({"id": "access-a", "properties": {}}).encode())
    ledger = access.NotionPropertyAccessLedger(token_path=token, urlopen=urlopen)
    key = access.property_access_idempotency_key("party-cleaner", "property-a")
    ledger.create_request(
        party_page_id="party-cleaner", property_page_id="property-a",
        source_message_ref="telegram_callback:cb:message:10", key=key, requested_at=NOW,
    )
    req = requests[0]
    body = json.loads(req.data)
    assert req.full_url.endswith("/v1/pages")
    assert body["parent"] == {"type": "data_source_id", "data_source_id": access.PROPERTY_ACCESS_SOURCE}
    assert set(body["properties"]) == {
        "권한명", "인력", "집", "신청 상태", "신청일", "Idempotency Key",
        "Source Message Ref", "데이터 환경", "Runtime Sync Status",
    }
    serialized = json.dumps(body, ensure_ascii=False)
    assert "65df5348-b0d5-446b-9c44-cb10b54e6e23" not in serialized  # 17 Qualification source
    assert "REQUESTED" in serialized and "NOT_REQUIRED" in serialized


def test_stale_duplicate_approval_is_business_idempotent(tmp_path):
    row = access_row(status="APPROVED", sync="SYNCED", priority=999)
    worker = cleaner(properties=["JJ House"], priority={"JJ House": 999})
    ledger, roster_path, source = _approval_setup(tmp_path, row=row, worker=worker)
    before = roster_path.read_text()
    notifications = []
    result = access.approve_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()), roster_path=roster_path,
        cleaner_notifier=lambda *args: notifications.append(args), mappings=MAPPINGS, now=NOW,
    )
    assert result == "already_approved"
    assert roster_path.read_text() == before
    assert not any(event[0] == "approve" for event in ledger.events)
    assert notifications == []


def test_stale_rejection_cannot_corrupt_approved_access(tmp_path):
    row = access_row(status="APPROVED", sync="SYNCED", priority=999)
    worker = cleaner(properties=["JJ House"], priority={"JJ House": 999})
    ledger, roster_path, source = _approval_setup(tmp_path, row=row, worker=worker)
    result = access.reject_property_access(
        access_page_id="access-a", source=source, ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()),
        cleaner_notifier=lambda *_args: None,
    )
    assert result == "not_requestable"
    assert access._access_row(ledger.get_access("access-a"))["status"] == "APPROVED"
    assert not any(event[0] == "reject" for event in ledger.events)


def test_notion_approval_writer_sets_pending_and_neutral_priority(tmp_path):
    import io
    token = tmp_path / "notion.token"
    token.write_text("synthetic")
    requests = []
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): self.close()
    def urlopen(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps({"id": "access-a", "properties": {}}).encode())
    ledger = access.NotionPropertyAccessLedger(token_path=token, urlopen=urlopen)
    ledger.approve("access-a", NOW)
    req = requests[0]
    body = json.loads(req.data)["properties"]
    assert req.method == "PATCH"
    assert body["신청 상태"]["select"]["name"] == "APPROVED"
    assert body["Runtime Sync Status"]["select"]["name"] == "PENDING"
    assert body["우선순위"]["number"] == 999


def test_ops_approval_callback_stops_after_synced_and_reports_async_cleaner_delivery(tmp_path):
    ledger, roster_path, source = _approval_setup(tmp_path)
    api = CaptureApi()
    cleaner_notifications = []

    result = access.handle_ops_property_access_callback(
        ops_update(decision="approve"),
        "synthetic-ops-token",
        request_api=api,
        source=source,
        ledger=ledger,
        roster_loader=lambda: json.loads(roster_path.read_text()),
        roster_path=roster_path,
        cleaner_notifier=lambda *args: cleaner_notifications.append(args),
        mappings=MAPPINGS,
        now=NOW,
    )

    current = access._access_row(ledger.get_access("access-a"))
    assert result == "property_access_ops_approved_synced"
    assert (current["status"], current["sync_status"]) == ("APPROVED", "SYNCED")
    assert cleaner_notifications == []
    assert api.sent_texts[-1] == (
        "숙소 승인과 Runtime 반영이 완료되었습니다. Cleaner 알림은 별도 전송됩니다."
    )
    assert "CleanerCredentialProvider" not in Path(access.__file__).read_text()
