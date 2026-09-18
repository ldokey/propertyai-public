from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping
from uuid import UUID


_UUID_HEX = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
_UUID_COMPACT = (
    r"[0-9a-fA-F]{12}[1-8][0-9a-fA-F]{3}"
    r"[89abAB][0-9a-fA-F]{3}[0-9a-fA-F]{12}"
)
_PAGE_UUID_RE = re.compile(rf"(?:{_UUID_HEX}|{_UUID_COMPACT})")
_NOTION_PAGE_URL_RE = re.compile(
    rf"https://(?:www\.)?notion\.(?:so|site)/[^?#]*?(?P<uuid>{_UUID_HEX}|{_UUID_COMPACT})(?:[?#].*)?"
)


def normalize_text(value: str, *, blank_to_none: bool = False) -> str | None:
    normalized = unicodedata.normalize("NFC", value).strip()
    if blank_to_none and not normalized:
        return None
    return normalized


def normalize_page_uuid(value: str | UUID) -> str:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise ValueError("Notion page identity must be exact UUID text or a Notion page URL")
    match = _PAGE_UUID_RE.fullmatch(value)
    if match is None:
        match = _NOTION_PAGE_URL_RE.fullmatch(value)
    if match is None:
        raise ValueError("Notion page identity does not contain an exact UUID")
    compact = (match.groupdict().get("uuid") or match.group(0)).replace("-", "")
    return str(UUID(compact))


def normalize_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def normalize_status(value: str) -> str:
    status = str(normalize_text(value)).replace("-", "_").replace(" ", "_").upper()
    if not status:
        raise ValueError("status must be nonblank")
    return status


def normalize_integer(value: Any, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer fact")
    result = int(value)
    if Decimal(str(value)) != Decimal(result):
        raise ValueError("integer fact contains a fractional value")
    if minimum is not None and result < minimum:
        raise ValueError(f"integer must be >= {minimum}")
    return result


def canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not canonical")
        return json.loads(json.dumps(value))
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return normalize_datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [canonical_value(item) for item in value]
        return sorted(items, key=lambda item: canonical_json(item))
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonical_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def semantic_projection(payload: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {field: canonical_value(payload.get(field)) for field in fields}


def reservation_semantics(payload: Mapping[str, Any]) -> dict[str, Any]:
    return semantic_projection(payload, ("reservation_status", "check_in_at", "check_out_at"))


__all__ = [
    "canonical_json",
    "canonical_value",
    "normalize_datetime",
    "normalize_integer",
    "normalize_page_uuid",
    "normalize_status",
    "normalize_text",
    "reservation_semantics",
    "semantic_hash",
    "semantic_projection",
]
