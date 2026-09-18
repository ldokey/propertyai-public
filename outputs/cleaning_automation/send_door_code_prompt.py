#!/usr/bin/env python3
"""Create a Telegram reply prompt for a regular-door-lock reservation."""

import argparse
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from telegram_approval.ops_config import OpsRuntimePaths  # noqa: E402
from propertyai_core.global_writer import assert_current_production_writer  # noqa: E402
from telegram_approval.outbound import ops_outbound_router  # noqa: E402
from telegram_approval.send_approval import OPERATOR_PATH, api, atomic_private  # noqa: E402
from door_code_capture import input_prompt  # noqa: E402


TOKEN_PATH = None
REQUEST_DIR = OpsRuntimePaths().callback_session_dir / "door-code-requests"


def parse_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def send_prompt(
    *,
    reservation_ref: str,
    reservation_page_id: str | None,
    access_point_ref: str,
    smart_doorlock: bool,
    base_cleaner_message: str,
    property_nickname: str | None = None,
    check_in: str | None = None,
    check_out: str | None = None,
    guest_count: str | None = None,
    currency: str | None = None,
    guest_total: str | None = None,
    host_payout: str | None = None,
    test_mode: bool = False,
) -> dict:
    if not test_mode and not reservation_page_id:
        raise ValueError("reservation_page_id is required outside TEST mode")
    context = {
        "property_nickname": property_nickname,
        "check_in": check_in,
        "check_out": check_out,
        "guest_count": guest_count,
        "currency": currency,
        "guest_total": guest_total,
        "host_payout": host_payout,
    }
    if not test_mode:
        missing = [name for name, value in context.items() if value is None]
        if missing:
            raise ValueError("missing production reservation context: " + ", ".join(missing))

    operator = json.loads(OPERATOR_PATH.read_text())
    now = datetime.now(timezone.utc)
    for path in REQUEST_DIR.glob("*.json"):
        existing = json.loads(path.read_text())
        if (
            existing.get("reservation_page_id") == reservation_page_id
            and existing.get("access_point_ref") == access_point_ref
            and existing.get("status") in {"PENDING_DELIVERY", "DELIVERY_UNCERTAIN", "PENDING_OPERATOR_INPUT"}
            and now <= datetime.fromisoformat(existing["expires_at"])
        ):
            return {
                "prompt_sent": False,
                "reason": "PENDING_REQUEST_EXISTS",
                "request_id": existing["request_id"],
                "prompt_message_id": existing.get("prompt_message_id"),
                "test_mode": test_mode,
            }
    request_id = secrets.token_urlsafe(8)
    record = {
        "schema_version": 2,
        "request_id": request_id,
        "reservation_ref": reservation_ref,
        "reservation_page_id": reservation_page_id,
        "access_point_ref": access_point_ref,
        "smart_doorlock": smart_doorlock,
        "prompt_message_id": None,
        "delivery_chat_id": operator["telegram_chat_id"],
        "base_cleaner_message": base_cleaner_message,
        "reservation_summary": context,
        "status": "PENDING_DELIVERY",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "test_mode": test_mode,
    }
    path = REQUEST_DIR / f"{request_id}.json"
    atomic_private(path, record)
    if not test_mode:
        assert_current_production_writer()
    try:
        sent = ops_outbound_router(
            api,
            token_path=TOKEN_PATH,
            synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
        ).send_message(
            operator["telegram_chat_id"],
            input_prompt(
                smart_doorlock,
                property_nickname=property_nickname,
                reservation_ref=reservation_ref,
                check_in=check_in,
                check_out=check_out,
                guest_count=guest_count,
                guest_total=(f"{currency} {guest_total}" if currency and guest_total else None),
                host_payout=(f"{currency} {host_payout}" if currency and host_payout else None),
            ),
            reply_markup=json.dumps({"force_reply": True, "selective": True}),
        )
    except Exception:
        if not test_mode:
            assert_current_production_writer()
        raise
    if not test_mode:
        assert_current_production_writer()
    record["prompt_message_id"] = sent["message_id"]
    record["status"] = "PENDING_OPERATOR_INPUT"
    atomic_private(path, record)
    return {"prompt_sent": True, "request_id": request_id, "prompt_message_id": sent["message_id"], "test_mode": test_mode}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reservation-ref", required=True)
    parser.add_argument("--reservation-page-id")
    parser.add_argument("--access-point-ref", required=True)
    parser.add_argument("--smart-doorlock", type=parse_bool, default=False)
    parser.add_argument("--base-cleaner-message", required=True)
    parser.add_argument("--property-nickname")
    parser.add_argument("--check-in")
    parser.add_argument("--check-out")
    parser.add_argument("--guest-count")
    parser.add_argument("--currency")
    parser.add_argument("--guest-total")
    parser.add_argument("--host-payout")
    parser.add_argument("--test-mode", action="store_true")
    args = parser.parse_args()
    if not args.test_mode and not args.reservation_page_id:
        parser.error("--reservation-page-id is required outside TEST mode")
    if not args.test_mode:
        required_context = {
            "--property-nickname": args.property_nickname,
            "--check-in": args.check_in,
            "--check-out": args.check_out,
            "--guest-count": args.guest_count,
            "--currency": args.currency,
            "--guest-total": args.guest_total,
            "--host-payout": args.host_payout,
        }
        missing = [name for name, value in required_context.items() if value is None]
        if missing:
            parser.error("missing production reservation context: " + ", ".join(missing))

    result = send_prompt(
        reservation_ref=args.reservation_ref,
        reservation_page_id=args.reservation_page_id,
        access_point_ref=args.access_point_ref,
        smart_doorlock=args.smart_doorlock,
        base_cleaner_message=args.base_cleaner_message,
        property_nickname=args.property_nickname,
        check_in=args.check_in,
        check_out=args.check_out,
        guest_count=args.guest_count,
        currency=args.currency,
        guest_total=args.guest_total,
        host_payout=args.host_payout,
        test_mode=args.test_mode,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
