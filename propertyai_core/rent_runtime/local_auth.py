"""Persistent-staging-only fixed local identity issuer.

The issuer has no credential, signing key, caller-selected subject, or Production
activation path. It is a narrow non-Production verification boundary that turns
one server-owned actor binding into one collision-free verified subject.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from .common import require
from .staging import PERSISTENT_STAGING

AUTH_BINDING_KIND = "PROVIDER_VERIFIED_SUBJECT_DIRECTORY"
LOCAL_TEST_FIXED_SUBJECT = "LOCAL_TEST_FIXED_SUBJECT"
_SUBJECT_PREFIX = "local-test:propertyai-rent:"


def fixed_local_subject(actor_party_id: UUID) -> str:
    require(isinstance(actor_party_id, UUID) and actor_party_id.int != 0, "CONFIG_INVALID")
    subject = _SUBJECT_PREFIX + str(actor_party_id).lower()
    require(
        0 < len(subject) <= 512
        and subject == subject.strip()
        and subject.isprintable()
        and not subject.startswith("synthetic:"),
        "CONFIG_INVALID",
    )
    return subject


@dataclass(frozen=True, slots=True, repr=False)
class PersistentStagingLocalIssuer:
    """Server-owned verifier for one fixed Persistent Staging principal."""

    environment: str
    auth_binding_kind: str
    issuer_kind: str
    bind_host: str
    actor_party_id: UUID
    allowlisted_subject: str = field(repr=False)

    def __post_init__(self) -> None:
        require(
            self.environment == PERSISTENT_STAGING
            and self.auth_binding_kind == AUTH_BINDING_KIND
            and self.issuer_kind == LOCAL_TEST_FIXED_SUBJECT
            and self.bind_host == "127.0.0.1",
            "CONFIG_INVALID",
        )
        expected = fixed_local_subject(self.actor_party_id)
        require(
            isinstance(self.allowlisted_subject, str)
            and self.allowlisted_subject == expected,
            "CONFIG_INVALID",
        )

    def verified_subject(self) -> str:
        """Return the only identity this issuer is authorized to verify."""
        return fixed_local_subject(self.actor_party_id)
