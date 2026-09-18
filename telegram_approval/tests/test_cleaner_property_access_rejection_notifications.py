import io
import json
import os
from pathlib import Path

import pytest

from telegram_approval import cleaner_property_access as access
from telegram_approval.cleaner_config import CLEANER_TOKEN_PATH_CONFIG
from telegram_approval.cleaner_property_access_notifications import (
    CleanerPropertyAccessDeliveryState,
    CleanerPropertyAccessNotificationBundle,
    CleanerPropertyAccessRejectionDeliveryState,
    CleanerPropertyAccessRejectionNotificationPump,
    CleanerPropertyAccessRejectionNotificationScanner,
    build_cleaner_property_access_notification_bundle,
    build_cleaner_property_access_rejection_notification_pump,
    enumerate_cleaner_property_access_rejection_notifications,
    render_cleaner_property_access_rejection,
)
from telegram_approval.ops_config import OPS_ALLOWLIST_CONFIG, OPS_TOKEN_PATH_CONFIG
from telegram_approval.telegram_transport import TelegramBotContext
from telegram_approval.tests import test_cleaner_property_access_notifications as approval_test


NOW = approval_test.NOW
MAPPINGS = approval_test.MAPPINGS


class RejectionLedger(approval_test.FakeLedger):
    def query_rejected(self):
        self.events.append(("query_rejected",))
        return list(self.discovered)


def rejected_row(**values):
    return approval_test.row(
        status="REJECTED", sync="NOT_REQUIRED", approved_at=None, **values
    )


def rejection_state_path(tmp_path):
    return tmp_path / "cleaner" / "property-access-rejection-notifications.json"


def rejection_pump(
    tmp_path,
    *,
    ledger=None,
    transport=None,
    roster_loader=approval_test.roster,
    mappings=MAPPINGS,
    max_candidates=25,
    max_deliveries=5,
):
    ledger = ledger or RejectionLedger([rejected_row()])
    return CleanerPropertyAccessRejectionNotificationPump(
        bot=TelegramBotContext("synthetic-cleaner-token"),
        transport=transport or approval_test.FakeTransport(),
        ledger=ledger,
        source=approval_test.FakeSource(),
        delivery_state=CleanerPropertyAccessRejectionDeliveryState(
            rejection_state_path(tmp_path)
        ),
        roster_loader=roster_loader,
        mappings=mappings,
        now=lambda: NOW,
        max_candidates_per_cycle=max_candidates,
        max_deliveries_per_cycle=max_deliveries,
    )


def test_rejected_record_sends_privacy_safe_cleaner_message_without_business_mutation(tmp_path):
    ledger = RejectionLedger([rejected_row()])
    transport = approval_test.FakeTransport()
    result = rejection_pump(tmp_path, ledger=ledger, transport=transport).run_once()

    assert result["sent"] == result["attempted"] == 1
    assert ledger.mutation_attempts == []
    token, method, values = transport.calls[0]
    assert token == "synthetic-cleaner-token" and method == "sendMessage"
    assert values["chat_id"] == 202
    assert values["text"] == "이번 숙소 신청은 승인되지 않았습니다.\n\n숙소: JJ"
    assert not any(secret in values["text"] for secret in [
        "SECRET_STREET_ADDRESS", "SECRET_DOOR_CODE", "SECRET_GUEST_PII",
        "SECRET_RESERVATION_ID", "access-a", "CPA-1", "Cleaner A", "999",
        "거절 사유", "reason",
    ])


def test_rejection_renderer_has_no_fabricated_reason():
    text = render_cleaner_property_access_rejection("JJ")
    assert text == "이번 숙소 신청은 승인되지 않았습니다.\n\n숙소: JJ"
    assert "사유" not in text and "reason" not in text.lower()


def test_rejection_completed_delivery_is_deduped(tmp_path):
    transport = approval_test.FakeTransport()
    subject = rejection_pump(tmp_path, transport=transport)
    assert subject.run_once()["sent"] == 1
    assert subject.run_once()["deduped"] == 1
    assert len(transport.calls) == 1


def test_ambiguous_rejection_send_requires_reconciliation_and_no_blind_retry(tmp_path):
    transport = approval_test.FakeTransport([
        RuntimeError("synthetic failure"), {"message_id": 88}
    ])
    subject = rejection_pump(tmp_path, transport=transport)
    first = subject.run_once()
    assert first["sent"] == 0
    assert first["failures"][0]["error_type"] == "RuntimeError"
    key = "access-a:CPA-1:REJECTION"
    saved = json.loads(rejection_state_path(tmp_path).read_text())["deliveries"][key]
    assert (saved["attempts"], saved["delivery_status"], saved["complete"]) == (
        1, "UNCERTAIN", False
    )
    assert saved["last_error_type"] == "RuntimeError"

    second = subject.run_once()
    assert second["sent"] == 0
    assert second["failures"][0]["error_type"] == "DeliveryReconciliationRequired"
    saved = json.loads(rejection_state_path(tmp_path).read_text())["deliveries"][key]
    assert saved["attempts"] == 1
    assert saved["delivery_status"] == "UNCERTAIN"
    assert saved["telegram_message_id"] is None
    assert saved["complete"] is False
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "status,sync",
    [("APPROVED", "SYNCED"), ("REQUESTED", "NOT_REQUIRED"), ("REVOKED", "NOT_REQUIRED")],
)
def test_non_rejected_states_are_not_rejection_candidates(tmp_path, status, sync):
    candidate = approval_test.row(status=status, sync=sync)
    transport = approval_test.FakeTransport()
    result = rejection_pump(
        tmp_path,
        ledger=RejectionLedger([candidate], discovered=[candidate]),
        transport=transport,
    ).run_once()
    assert result["sent"] == 0 and result["non_actionable"] == 1
    assert transport.calls == []


def test_nonproduction_rejection_is_excluded_fail_closed(tmp_path):
    candidate = rejected_row(environment="TEST")
    transport = approval_test.FakeTransport()
    result = rejection_pump(
        tmp_path, ledger=RejectionLedger([candidate]), transport=transport
    ).run_once()
    assert result["sent"] == 0
    assert result["failures"][0]["error_type"] == "PropertyAccessConflict"
    assert transport.calls == []


def test_fresh_read_suppresses_stale_rejection(tmp_path):
    discovered = rejected_row()
    fresh = approval_test.row(status="REQUESTED", sync="NOT_REQUIRED", approved_at=None)
    ledger = RejectionLedger(
        [fresh], discovered=[discovered], fresh_overrides={"access-a": fresh}
    )
    transport = approval_test.FakeTransport()
    result = rejection_pump(tmp_path, ledger=ledger, transport=transport).run_once()
    assert result["non_actionable"] == 1
    assert transport.calls == []
    assert ("get_access", "access-a") in ledger.events


def test_fresh_access_id_mismatch_fails_closed(tmp_path):
    discovered = rejected_row(access_number=1)
    fresh = rejected_row(access_number=2)
    transport = approval_test.FakeTransport()
    result = rejection_pump(
        tmp_path,
        ledger=RejectionLedger(
            [fresh], discovered=[discovered], fresh_overrides={"access-a": fresh}
        ),
        transport=transport,
    ).run_once()
    assert result["sent"] == 0
    assert result["failures"][0]["error_type"] == "PropertyAccessConflict"
    assert transport.calls == []


def test_missing_cleaner_identity_fails_closed(tmp_path):
    transport = approval_test.FakeTransport()
    result = rejection_pump(
        tmp_path,
        transport=transport,
        roster_loader=lambda: {"schema_version": 1, "cleaners": []},
    ).run_once()
    assert result["sent"] == 0
    assert result["failures"][0]["error_type"] == "PropertyAccessError"
    assert transport.calls == []


def test_missing_property_relation_fails_closed(tmp_path):
    candidate = rejected_row()
    candidate["properties"]["집"] = approval_test._relation()
    transport = approval_test.FakeTransport()
    result = rejection_pump(
        tmp_path, ledger=RejectionLedger([candidate]), transport=transport
    ).run_once()
    assert result["sent"] == 0 and transport.calls == []
    assert result["failures"][0]["error_type"] == "PropertyAccessConflict"


def test_missing_safe_property_mapping_fails_closed(tmp_path):
    transport = approval_test.FakeTransport()
    result = rejection_pump(tmp_path, transport=transport, mappings={}).run_once()
    assert result["sent"] == 0 and transport.calls == []
    assert result["failures"][0]["error_type"] == "PropertyAccessError"


def test_rejection_state_is_atomic_private_event_key_and_approval_file_is_unchanged(tmp_path):
    approval_path = approval_test.state_path(tmp_path)
    approval = CleanerPropertyAccessDeliveryState(approval_path)
    approval.record_attempt(access_page_id="access-a", access_id="CPA-1", attempted_at=NOW)
    approval.record_success(
        access_page_id="access-a", access_id="CPA-1", telegram_message_id=7, sent_at=NOW
    )
    approval_before = approval_path.read_text()

    assert rejection_pump(tmp_path).run_once()["sent"] == 1
    path = rejection_state_path(tmp_path)
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    value = json.loads(path.read_text())
    assert set(value["deliveries"]) == {"access-a:CPA-1:REJECTION"}
    assert value["deliveries"]["access-a:CPA-1:REJECTION"]["complete"] is True
    assert approval_path.read_text() == approval_before
    assert set(json.loads(approval_before)["deliveries"]) == {"access-a:CPA-1"}


def test_rejection_enumeration_is_deterministic_inspectable_and_read_only(tmp_path):
    rows = [
        rejected_row(page_id="access-z", access_number=3, property_page_id="property-c"),
        rejected_row(page_id="access-b", access_number=2, property_page_id="property-b"),
        rejected_row(page_id="access-a", access_number=1, property_page_id="property-a"),
    ]
    ledger = RejectionLedger(rows, discovered=list(reversed(rows)))
    scanner = CleanerPropertyAccessRejectionNotificationScanner(
        ledger=ledger,
        source=approval_test.FakeSource(),
        delivery_state=CleanerPropertyAccessRejectionDeliveryState(
            rejection_state_path(tmp_path)
        ),
        roster_loader=approval_test.roster,
        mappings=MAPPINGS,
    )
    result = scanner.enumerate_actionable()
    assert [item["access_id"] for item in result["actionable"]] == [
        "CPA-1", "CPA-2", "CPA-3"
    ]
    assert [item["property_label"] for item in result["actionable"]] == [
        "JJ", "Browny House", "Property C"
    ]
    assert result["discovered"] == 3 and result["completed"] == []
    assert result["failures"] == [] and ledger.mutation_attempts == []
    assert not rejection_state_path(tmp_path).exists()


def test_public_rejection_enumerator_requires_no_telegram_sender_or_state_write(tmp_path):
    result = enumerate_cleaner_property_access_rejection_notifications(
        ledger=RejectionLedger([rejected_row()]),
        source=approval_test.FakeSource(),
        delivery_state_path=rejection_state_path(tmp_path),
        roster_loader=approval_test.roster,
        mappings=MAPPINGS,
    )
    assert [item["access_id"] for item in result["actionable"]] == ["CPA-1"]
    assert not rejection_state_path(tmp_path).exists()


def test_rejection_factory_uses_cleaner_credential_and_never_reads_ops_configuration(tmp_path):
    cleaner_token = tmp_path / "cleaner.token"
    cleaner_token.write_text("synthetic-cleaner-token\n")
    transport = approval_test.FakeTransport()
    subject = build_cleaner_property_access_rejection_notification_pump(
        environment={
            CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token),
            OPS_TOKEN_PATH_CONFIG: str(tmp_path / "must-not-read-ops.token"),
            OPS_ALLOWLIST_CONFIG: "invalid-and-must-not-be-read",
        },
        transport=transport,
        ledger=RejectionLedger([rejected_row()]),
        source=approval_test.FakeSource(),
        delivery_state_path=rejection_state_path(tmp_path),
        roster_loader=approval_test.roster,
        mappings=MAPPINGS,
        now=lambda: NOW,
    )
    assert subject.run_once()["sent"] == 1
    assert {token for token, _method, _values in transport.calls} == {
        "synthetic-cleaner-token"
    }
    assert not (tmp_path / "must-not-read-ops.token").exists()


def test_notion_rejection_discovery_is_read_only_and_strictly_filtered(tmp_path):
    token = tmp_path / "notion.token"
    token.write_text("synthetic")
    requests = []

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): self.close()

    def urlopen(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps({
            "results": [], "has_more": False, "next_cursor": None
        }).encode())

    ledger = access.NotionPropertyAccessLedger(token_path=token, urlopen=urlopen)
    assert ledger.query_rejected() == []
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert json.loads(request.data)["filter"] == {"and": [
        {"property": "신청 상태", "select": {"equals": "REJECTED"}},
        {"property": "Runtime Sync Status", "select": {"equals": "NOT_REQUIRED"}},
        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
    ]}


def test_rejection_cycle_is_bounded_and_default_total_send_attempt_limit_is_ten(tmp_path):
    property_ids = [
        "property-a", "property-b", "property-c", "property-d", "property-e", "property-f"
    ]
    rows = [
        rejected_row(
            page_id=f"access-{index}", access_number=index,
            property_page_id=property_ids[index - 1],
        )
        for index in range(1, 7)
    ]
    subject = rejection_pump(
        tmp_path, ledger=RejectionLedger(rows), transport=approval_test.FakeTransport(),
        max_candidates=4, max_deliveries=2,
    )
    result = subject.run_once()
    assert (result["discovered"], result["examined"], result["attempted"]) == (6, 4, 2)
    assert result["sent"] == 2 and result["deferred"] == 4

    approval_pump = approval_test.pump(tmp_path)
    rejection = rejection_pump(tmp_path)
    assert approval_pump._max_candidates == rejection._max_candidates == 25
    assert approval_pump._max_deliveries == rejection._max_deliveries == 5
    assert approval_pump._max_deliveries + rejection._max_deliveries == 10


def test_notification_bundle_isolates_one_outbound_pump_failure_from_the_other():
    calls = []

    class Stub:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail
        def run_once(self):
            calls.append(self.name)
            if self.fail:
                raise RuntimeError("synthetic")
            return {"sent": 1}

    bundle = CleanerPropertyAccessNotificationBundle(
        approval_pump=Stub("approval", fail=True),
        rejection_pump=Stub("rejection"),
    )
    result = bundle.run_once()
    assert calls == ["approval", "rejection"]
    assert result["approval"]["failures"][0]["error_type"] == "RuntimeError"
    assert result["rejection"] == {"sent": 1}


def test_bundle_factory_shares_one_cleaner_identity_and_preserves_approval_key_shape(tmp_path):
    cleaner_token = tmp_path / "cleaner.token"
    cleaner_token.write_text("synthetic-cleaner-token\n")
    bundle = build_cleaner_property_access_notification_bundle(
        environment={CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token)},
        transport=approval_test.FakeTransport(),
        ledger=RejectionLedger([rejected_row()]),
        source=approval_test.FakeSource(),
        approval_delivery_state_path=approval_test.state_path(tmp_path),
        rejection_delivery_state_path=rejection_state_path(tmp_path),
        roster_loader=approval_test.roster,
        mappings=MAPPINGS,
        now=lambda: NOW,
    )
    assert bundle._approval_pump._bot.token == "synthetic-cleaner-token"
    assert bundle._rejection_pump._bot.token == "synthetic-cleaner-token"
    approval = CleanerPropertyAccessDeliveryState(approval_test.state_path(tmp_path))
    approval.record_attempt(access_page_id="access-a", access_id="CPA-1", attempted_at=NOW)
    serialized = approval_test.state_path(tmp_path).read_text()
    assert "access-a:CPA-1" in serialized and ":REJECTION" not in serialized


def test_malformed_rejection_delivery_state_fails_before_send(tmp_path):
    path = rejection_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":1,"deliveries":{"bad":{"complete":true}}}')
    transport = approval_test.FakeTransport()
    result = rejection_pump(tmp_path, transport=transport).run_once()
    assert result["sent"] == 0 and result["failures"]
    assert transport.calls == []
