from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from uuid import UUID, uuid5

from .models import AmbiguousIdentityError, ImmutableIdentityConflict
from .normalize import normalize_page_uuid, semantic_hash


ROOT_NAMESPACE = UUID("af69a66b-3c14-5f73-9544-78da052ee5c3")
ORGANIZATION_SEED = "organization:PROPERTYAI_PRODUCTION"
_UUID_TEXT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256_TEXT = re.compile(r"^[0-9a-f]{64}$")
_TELEGRAM_USER_ID = re.compile(r"^[1-9][0-9]*$")


def _uuid_text(value: str) -> bool:
    if not _UUID_TEXT.fullmatch(value):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _validate_durable_seed(seed: str) -> None:
    if seed == ORGANIZATION_SEED:
        return
    parts = seed.split(":")
    if len(parts) == 3 and parts[1] == "notion" and parts[0] in {
        "property",
        "rental-unit",
        "party",
        "reservation",
        "cleaning",
        "roster",
    }:
        if _uuid_text(parts[2]):
            return
    if len(parts) == 3 and parts[1] == "notion19" and parts[0] in {
        "assignment",
        "unavailability",
        "reassignment",
    }:
        if _uuid_text(parts[2]):
            return
    if (
        len(parts) == 4
        and parts[:3] == ["external-identity", "telegram", "user"]
        and _TELEGRAM_USER_ID.fullmatch(parts[3])
    ):
        return
    if len(parts) == 4 and parts[0] == "snapshot":
        predecessor = parts[2]
        if _uuid_text(parts[1]) and (predecessor == "ROOT" or _uuid_text(predecessor)) and _SHA256_TEXT.fullmatch(parts[3]):
            return
    if len(parts) == 2 and parts[0] == "run" and _uuid_text(parts[1]):
        return
    if len(parts) == 2 and parts[0] == "extraction" and _SHA256_TEXT.fullmatch(parts[1]):
        return
    raise ValueError(f"durable identity does not satisfy the exact contract: {seed!r}")


def deterministic_id(durable_identity: str) -> UUID:
    seed = durable_identity.strip()
    if not seed or ":" not in seed:
        raise ValueError("durable identity must be a namespaced exact seed")
    _validate_durable_seed(seed)
    return uuid5(ROOT_NAMESPACE, seed)


def notion_identity(kind: str, page_id: str | UUID) -> UUID:
    if kind not in {"property", "rental-unit", "party", "reservation", "cleaning", "roster"}:
        raise ValueError(f"unsupported Notion identity kind: {kind}")
    return deterministic_id(f"{kind}:notion:{normalize_page_uuid(page_id)}")


def assignment_identity(page_id: str | UUID) -> UUID:
    return deterministic_id(f"assignment:notion19:{normalize_page_uuid(page_id)}")


def telegram_identity(user_id: str | int) -> UUID:
    exact = str(user_id).strip()
    if not _TELEGRAM_USER_ID.fullmatch(exact):
        raise ValueError("Telegram user id must be a positive canonical decimal provider identity")
    return deterministic_id(f"external-identity:telegram:user:{exact}")


def extraction_identity(evidence_hash: str) -> UUID:
    if not _SHA256_TEXT.fullmatch(evidence_hash):
        raise ValueError("extraction evidence hash must be lowercase sha256")
    return deterministic_id(f"extraction:{evidence_hash}")


def derived_identity(kind: str, *durable_parts: object) -> UUID:
    """Construct an internal deterministic id from already-validated durable evidence.

    This path is intentionally separate from ``deterministic_id``: external/source
    identity strings must satisfy an exact provider contract, while these derived
    identities are only created by bounded Stage B reconstruction code.
    """
    allowed = {
        "campaign",
        "candidate",
        "schedule-revision",
        "binding",
        "migration-receipt",
        "obligation",
    }
    if kind not in allowed:
        raise ValueError(f"unsupported derived identity kind: {kind}")
    if not durable_parts or any(str(part).strip() == "" for part in durable_parts):
        raise ValueError("derived identity parts must be exact and nonblank")
    seed = f"{kind}:" + ":".join(str(part) for part in durable_parts)
    return uuid5(ROOT_NAMESPACE, seed)


@dataclass
class IdentityRegistry:
    """Detect both source ambiguity and deterministic UUID collisions."""

    _source_to_target: dict[str, UUID] = field(default_factory=dict)
    _target_fingerprint: dict[UUID, str] = field(default_factory=dict)

    def register(self, source_key: str, target_id: UUID, immutable_facts: object) -> UUID:
        source_key = source_key.strip()
        if not source_key:
            raise ValueError("source key must be nonblank")
        current = self._source_to_target.get(source_key)
        if current is not None and current != target_id:
            raise AmbiguousIdentityError(f"source identity {source_key!r} maps to multiple targets")
        fingerprint = semantic_hash(immutable_facts)
        existing = self._target_fingerprint.get(target_id)
        if existing is not None and existing != fingerprint:
            raise ImmutableIdentityConflict(
                f"target {target_id} received conflicting immutable identities"
            )
        self._source_to_target[source_key] = target_id
        self._target_fingerprint[target_id] = fingerprint
        return target_id


def exact_unique_match(
    wanted: object, candidates: Iterable[object], *, key=lambda value: value
) -> object | None:
    matches = [candidate for candidate in candidates if key(candidate) == wanted]
    if len(matches) > 1:
        raise AmbiguousIdentityError("exact identity matched more than one candidate")
    return matches[0] if matches else None


__all__ = [
    "IdentityRegistry",
    "ORGANIZATION_SEED",
    "ROOT_NAMESPACE",
    "assignment_identity",
    "derived_identity",
    "deterministic_id",
    "exact_unique_match",
    "extraction_identity",
    "notion_identity",
    "telegram_identity",
]
