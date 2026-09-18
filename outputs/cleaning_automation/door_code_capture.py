#!/usr/bin/env python3
"""Door-lock routing for cleaner Telegram messages.

Airbnb's guest phone-number suffix is treated as an operational value.  The
value is neither normalized nor rejected.  It is inserted exactly for every
property, regardless of the smart-door-lock flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


DEFAULT_SMART_DOORLOCK = False


def preserve_airbnb_phone_last4(raw: str) -> str:
    """Return Airbnb's phone-number suffix without any transformation."""
    return raw


def should_send_door_code(smart_doorlock: bool = DEFAULT_SMART_DOORLOCK) -> bool:
    """Every property receives the captured reservation door-code value."""
    return True


def build_cleaner_message(
    base_message: str,
    airbnb_phone_last4: str,
    smart_doorlock: bool = DEFAULT_SMART_DOORLOCK,
) -> str:
    """Build a cleaner message using the source value exactly when required."""
    return f"{base_message}\n도어락 번호: {preserve_airbnb_phone_last4(airbnb_phone_last4)}"


@dataclass(frozen=True)
class CaptureRequest:
    request_id: str
    reservation_ref: str
    access_point_ref: str
    prompt_message_id: int
    created_at: str
    expires_at: str
    smart_doorlock: bool = DEFAULT_SMART_DOORLOCK
    status: str = "PENDING_OPERATOR_INPUT"

    def as_record(self) -> dict:
        return {
            "schema_version": 2,
            "request_id": self.request_id,
            "reservation_ref": self.reservation_ref,
            "access_point_ref": self.access_point_ref,
            "prompt_message_id": self.prompt_message_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "smart_doorlock": self.smart_doorlock,
            "status": self.status,
        }


def new_request(
    request_id: str,
    reservation_ref: str,
    access_point_ref: str,
    prompt_message_id: int,
    smart_doorlock: bool = DEFAULT_SMART_DOORLOCK,
) -> CaptureRequest:
    now = datetime.now(timezone.utc)
    return CaptureRequest(
        request_id=request_id,
        reservation_ref=reservation_ref,
        access_point_ref=access_point_ref,
        prompt_message_id=prompt_message_id,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=24)).isoformat(),
        smart_doorlock=smart_doorlock,
        status="PENDING_OPERATOR_INPUT",
    )


def input_prompt(
    smart_doorlock: bool = DEFAULT_SMART_DOORLOCK,
    *,
    property_nickname: str | None = None,
    reservation_ref: str | None = None,
    check_in: str | None = None,
    check_out: str | None = None,
    guest_count: str | None = None,
    guest_total: str | None = None,
    host_payout: str | None = None,
) -> str:
    details = []
    for label, value in (
        ("숙소", property_nickname),
        ("예약번호", reservation_ref),
        ("체크인", check_in),
        ("체크아웃", check_out),
        ("인원", guest_count),
        ("게스트 결제", guest_total),
        ("호스트 수령", host_payout),
    ):
        if value is not None:
            details.append(f"{label}: {value}")
    header = "🔑 예약 출입 코드 입력 요청"
    summary = "\n".join(details)
    instruction = (
        "Airbnb에 표시된 고객 전화번호 뒤 4자리를 이 메시지에 답장해주세요.\n"
        "입력한 값은 변경하지 않고 예약 원장에 저장합니다."
    )
    return f"{header}\n\n{summary}\n\n{instruction}" if summary else f"{header}\n\n{instruction}"
