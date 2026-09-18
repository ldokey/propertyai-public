#!/usr/bin/env python3
import hashlib
import re
from datetime import datetime
from decimal import Decimal


MONTHS = {name: number for number, name in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
)}


def _capture(pattern, text, flags=0):
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else None


def _date_after(label, body, received_at):
    match = re.search(rf"{label}\s+\w{{3}},\s+([A-Z][a-z]{{2}})\s+(\d{{1,2}})", body)
    if not match:
        return None
    month, day = MONTHS[match.group(1)], int(match.group(2))
    received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    year = received.year
    candidate = datetime(year, month, day, tzinfo=received.tzinfo)
    if candidate.date() < received.date() and (received.date() - candidate.date()).days > 180:
        year += 1
    return f"{year:04d}-{month:02d}-{day:02d}"


CURRENCY_SYMBOLS = {
    "USD": "$",
    "KRW": "₩",
}


def _money_after(label, body, currency):
    expected_symbol = CURRENCY_SYMBOLS.get(currency)
    if not expected_symbol:
        return None
    match = re.search(
        rf"{re.escape(label)}\s+([$₩€£¥])\s*([0-9][0-9,.]*)(?=\s|$)",
        body,
        re.I,
    )
    if not match or match.group(1) != expected_symbol:
        return None
    value = match.group(2)
    valid_number = re.fullmatch(
        r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?",
        value,
    )
    return str(Decimal(value.replace(",", ""))) if valid_number else None


def _booking_code(body):
    return (
        _capture(r"Confirmation code\s+([A-Z0-9]{8,14})", body)
        or _capture(r"/hosting/reservations/details/([A-Z0-9]{8,14})", body)
        or _capture(r"/reservations/details/([A-Z0-9]{8,14})", body)
    )


def _stable_text_hash(value):
    normalized = " ".join((value or "").casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else None


def _base_event(message, event_type, confirmation=None):
    return {
        "schema_version": 2,
        "source": "GMAIL_AIRBNB",
        "event_type": event_type,
        "source_message_hash": hashlib.sha256(message["id"].encode()).hexdigest(),
        "external_booking_ref_hash": hashlib.sha256(confirmation.encode()).hexdigest() if confirmation else None,
        "received_at": message["email_ts"],
        "pii_stored": False,
        "raw_body_stored": False,
    }


def _date_range(body, received_at):
    match = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+[–-]\s+(?:(?:([A-Z][a-z]{2})\s+)?)?(\d{1,2}),\s+(\d+)\s+guests?", body)
    if not match:
        return None, None, None
    start_month = MONTHS[match.group(1)]
    start_day = int(match.group(2))
    end_month = MONTHS[match.group(3)] if match.group(3) else start_month
    end_day = int(match.group(4))
    received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    year = received.year
    start = datetime(year, start_month, start_day, tzinfo=received.tzinfo)
    if start.date() < received.date() and (received.date() - start.date()).days > 180:
        year += 1
    end_year = year + (1 if end_month < start_month else 0)
    return f"{year:04d}-{start_month:02d}-{start_day:02d}", f"{end_year:04d}-{end_month:02d}-{end_day:02d}", int(match.group(5))


def parse_airbnb_confirmation(message):
    body = message.get("body", "")
    subject = message.get("subject", "")
    if "booking confirmed" not in body.casefold() and "reservation confirmed" not in subject.casefold():
        raise ValueError("not an Airbnb reservation confirmation")
    confirmation = _booking_code(body)
    listing_id = _capture(r"airbnb\.com/rooms/(\d+)", body)
    guests = _capture(r"Guests\s+(\d+)\s+adult", body, re.I)
    nights = _capture(r"(?m)^[^\r\n]*\s+x\s+(\d+)\s+nights?\s*$", body, re.I)
    currency = _capture(r"Total \(([A-Z]{3})\)", body)
    result = {
        **_base_event(message, "BOOKING_CONFIRMED", confirmation),
        "listing_id": listing_id,
        "check_in": _date_after("Check-in", body, message["email_ts"]),
        "check_out": _date_after("Checkout", body, message["email_ts"]),
        "adults": int(guests) if guests else None,
        "nights": int(nights) if nights else None,
        "currency": currency,
        "guest_total": _money_after(f"Total ({currency})", body, currency) if currency else None,
        "host_payout": _money_after("You earn", body, currency) if currency else None,
    }
    required = (
        "external_booking_ref_hash", "listing_id", "check_in", "check_out",
        "adults", "currency", "guest_total", "host_payout",
    )
    missing = [key for key in required if result[key] is None]
    result["status"] = "PARSED" if not missing else "REVIEW_REQUIRED"
    result["missing_fields"] = missing
    return result


def parse_airbnb_event(message):
    """Parse supported Airbnb lifecycle mail without persisting raw mail or guest identity."""
    body = message.get("body", "")
    subject = message.get("subject", "")
    template = message.get("template", "")
    folded = f"{subject}\n{body}".casefold()
    template_folded = template.casefold()

    if (
        "booking confirmed" in folded
        or "reservation confirmed" in subject.casefold()
        or "예약 확정" in folded
    ):
        return parse_airbnb_confirmation(message)

    confirmation = _booking_code(body)
    if "alteration_requested" in template_folded or "wants to change their reservation" in folded:
        actor = _capture(r"^(.+?)\s+wants to change their reservation", subject, re.I)
        listing_title = _capture(r"\n([^\n]+)\n\s*\nORIGINAL GUESTS", body, re.I)
        result = {
            **_base_event(message, "BOOKING_CHANGE_REQUESTED", confirmation),
            "requested_adults": None,
            "original_adults": None,
            "actor_ref_hash": _stable_text_hash(actor),
            "listing_ref_hash": _stable_text_hash(listing_title),
            "alteration_ref_hash": (
                _stable_text_hash(_capture(r"/reservation/alteration/(\d+)", body))
            ),
        }
        original = _capture(r"ORIGINAL GUESTS\s+(\d+)\s+guests?", body, re.I)
        requested = _capture(r"REQUESTED GUESTS\s+(\d+)\s+guests?", body, re.I)
        result["original_adults"] = int(original) if original else None
        result["requested_adults"] = int(requested) if requested else None
        result["status"] = "OBSERVED_NO_WRITE"
        result["missing_fields"] = [] if confirmation else ["external_booking_ref_hash"]
        return result

    cancelled = (
        any(marker in template_folded for marker in ("cancellation", "cancelled", "canceled"))
        or re.search(r"^\s*(?:cancelled|canceled):?\s+reservation\b", subject, re.I)
        or re.search(r"^\s*reservation\s+(?:cancelled|canceled)\b", subject, re.I)
        or re.search(r"reservation(?: has been| was)? (?:cancelled|canceled)", folded)
        or re.search(r"예약.{0,20}취소", folded)
    )
    if cancelled:
        check_in, check_out, guests = _date_range(body, message["email_ts"])
        result = {
            **_base_event(message, "BOOKING_CANCELLED", confirmation),
            "cancellation_evidence": "PLATFORM_NOTICE",
            "listing_id": _capture(r"Listing\s*#(\d+)", body, re.I) or _capture(r"airbnb\.com/rooms/(\d+)", body),
            "check_in": check_in,
            "check_out": check_out,
            "guests": guests,
            "refund_scope": "GUEST_FULL_REFUND" if "complete refund was given to the guest" in folded else "UNKNOWN",
        }
        result["status"] = "PARSED" if confirmation else "REVIEW_REQUIRED"
        result["missing_fields"] = [] if confirmation else ["external_booking_ref_hash"]
        return result

    if "alteration_accepted" in template_folded or "reservation updated" in folded or "has been updated" in folded:
        actor = _capture(r"YOUR RESERVATION WITH\s+(.+?)\s+HAS BEEN UPDATED", body, re.I)
        listing_title = _capture(r"\n([^\n]+)\n\s*\nWe.ve already updated", body, re.I)
        result = {
            **_base_event(message, "BOOKING_UPDATED", confirmation),
            "exact_changes_in_email": False,
            "actor_ref_hash": _stable_text_hash(actor),
            "listing_ref_hash": _stable_text_hash(listing_title),
        }
        result["status"] = "PARSED" if confirmation else "REVIEW_REQUIRED"
        result["missing_fields"] = [] if confirmation else ["external_booking_ref_hash"]
        return result

    raise ValueError("unsupported Airbnb lifecycle email")
