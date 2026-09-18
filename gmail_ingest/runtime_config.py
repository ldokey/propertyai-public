"""Fail-closed authority boundary for the Gmail ingest runtime configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


GMAIL_INGEST_CONFIG_PATH_ENV = "PROPERTYAI_GMAIL_INGEST_CONFIG_PATH"


class GmailIngestRuntimeConfigError(RuntimeError):
    """The configured Gmail ingest runtime authority is missing or invalid."""


def resolve_gmail_ingest_config_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve exactly one externally supplied config path with no fallback search."""

    env = os.environ if environment is None else environment
    raw = env.get(GMAIL_INGEST_CONFIG_PATH_ENV)
    if not isinstance(raw, str) or not raw.strip():
        raise GmailIngestRuntimeConfigError(
            f"GMAIL_INGEST_CONFIG_PATH_MISSING:{GMAIL_INGEST_CONFIG_PATH_ENV}"
        )
    if raw != raw.strip():
        raise GmailIngestRuntimeConfigError("GMAIL_INGEST_CONFIG_PATH_INVALID_WHITESPACE")

    path = Path(raw)
    if not path.is_absolute():
        raise GmailIngestRuntimeConfigError("GMAIL_INGEST_CONFIG_PATH_NOT_ABSOLUTE")
    if path.is_symlink():
        raise GmailIngestRuntimeConfigError("GMAIL_INGEST_CONFIG_PATH_SYMLINK_FORBIDDEN")
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise GmailIngestRuntimeConfigError("GMAIL_INGEST_CONFIG_PATH_NOT_FOUND") from error
    if not resolved.is_file():
        raise GmailIngestRuntimeConfigError("GMAIL_INGEST_CONFIG_PATH_NOT_FILE")
    return resolved


def load_gmail_ingest_config(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    path = resolve_gmail_ingest_config_path(environment)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise GmailIngestRuntimeConfigError("GMAIL_INGEST_CONFIG_READ_FAILED") from error
    except json.JSONDecodeError as error:
        raise GmailIngestRuntimeConfigError("GMAIL_INGEST_CONFIG_JSON_INVALID") from error
    if not isinstance(config, dict):
        raise GmailIngestRuntimeConfigError("GMAIL_INGEST_CONFIG_OBJECT_REQUIRED")
    return config


__all__ = [
    "GMAIL_INGEST_CONFIG_PATH_ENV",
    "GmailIngestRuntimeConfigError",
    "load_gmail_ingest_config",
    "resolve_gmail_ingest_config_path",
]
