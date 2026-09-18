"""Fail-closed external bindings for private Notion resource identities."""

from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import RFC_4122, UUID

RESERVATION_SOURCE_ID_ENV = "PROPERTYAI_NOTION_RESERVATION_SOURCE_ID"
CLEANING_SOURCE_ID_ENV = "PROPERTYAI_NOTION_CLEANING_SOURCE_ID"


class NotionResourceBindingError(RuntimeError):
    """A required private Notion resource binding is absent or malformed."""


def _required_v4_uuid(
    environment_key: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environment is None else environment
    raw = str(env.get(environment_key, "")).strip()
    if not raw:
        raise NotionResourceBindingError(f"{environment_key}_REQUIRED")
    try:
        parsed = UUID(raw)
    except (ValueError, AttributeError):
        raise NotionResourceBindingError(f"{environment_key}_INVALID") from None
    if parsed.version != 4 or parsed.variant != RFC_4122:
        raise NotionResourceBindingError(f"{environment_key}_INVALID")
    return str(parsed)


def reservation_source_id(environment: Mapping[str, str] | None = None) -> str:
    return _required_v4_uuid(RESERVATION_SOURCE_ID_ENV, environment)


def cleaning_source_id(environment: Mapping[str, str] | None = None) -> str:
    return _required_v4_uuid(CLEANING_SOURCE_ID_ENV, environment)
