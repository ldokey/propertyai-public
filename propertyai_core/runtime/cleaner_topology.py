"""Explicit Cleaner runtime topology contract for legacy and PostgreSQL authority modes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


TOPOLOGY_ENV = "PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY"


class CleanerRuntimeTopology(str, Enum):
    PRE_CUTOVER = "PRE_CUTOVER"
    POST_CUTOVER_PG = "POST_CUTOVER_PG"


@dataclass(frozen=True)
class CleanerTopologyContract:
    topology: CleanerRuntimeTopology
    services: dict[str, tuple[str, bool]]
    recovery_targets: dict[str, frozenset[str]]
    scheduler_logs: frozenset[str]
    error_logs: frozenset[str]
    retired_labels: frozenset[str]


_BASE_SERVICES = {
    "telegram_cleaner": ("com.propertyai.telegram-cleaner", True),  # W03
    "gmail_readonly": ("com.propertyai.gmail-readonly", False),  # W01
    "health_monitor": ("com.propertyai.health-monitor", False),  # W06
}
_BASE_RECOVERY = {
    "com.propertyai.telegram-cleaner": frozenset({"SERVICE_TELEGRAM_CLEANER"}),
    "com.propertyai.gmail-readonly": frozenset({"SERVICE_GMAIL_READONLY", "LOG_FRESH_GMAIL_POLLER"}),
}
_LEGACY_SERVICES = {
    "cleaning_operations": ("com.propertyai.cleaning-operations", False),  # W04
    "cleaning_completion": ("com.propertyai.cleaning-completion", False),  # W05
}
_LEGACY_RECOVERY = {
    "com.propertyai.cleaning-operations": frozenset({"SERVICE_CLEANING_OPERATIONS", "LOG_FRESH_CLEANING_OPERATIONS"}),
    "com.propertyai.cleaning-completion": frozenset({"SERVICE_CLEANING_COMPLETION", "LOG_FRESH_CLEANING_COMPLETION"}),
}
_PG_SERVICES = {
    "cleaner_pg_outbox": ("com.propertyai.cleaner-pg-outbox", True),  # W07
}
_PG_RECOVERY = {
    "com.propertyai.cleaner-pg-outbox": frozenset({"SERVICE_CLEANER_PG_OUTBOX", "LOG_FRESH_CLEANER_PG_OUTBOX"}),
}


def topology_contract(topology: CleanerRuntimeTopology) -> CleanerTopologyContract:
    if topology is CleanerRuntimeTopology.PRE_CUTOVER:
        return CleanerTopologyContract(
            topology=topology,
            services={**_BASE_SERVICES, **_LEGACY_SERVICES},
            recovery_targets={**_BASE_RECOVERY, **_LEGACY_RECOVERY},
            scheduler_logs=frozenset({"gmail_poller", "cleaning_operations", "cleaning_completion"}),
            error_logs=frozenset({"gmail_poller", "telegram_cleaner", "cleaning_operations", "cleaning_completion"}),
            retired_labels=frozenset({"com.propertyai.telegram-approval", "com.propertyai.cleaner-pg-outbox"}),
        )
    if topology is CleanerRuntimeTopology.POST_CUTOVER_PG:
        return CleanerTopologyContract(
            topology=topology,
            services={**_BASE_SERVICES, **_PG_SERVICES},
            recovery_targets={**_BASE_RECOVERY, **_PG_RECOVERY},
            scheduler_logs=frozenset({"gmail_poller", "cleaner_pg_outbox"}),
            error_logs=frozenset({"gmail_poller", "telegram_cleaner", "cleaner_pg_outbox"}),
            retired_labels=frozenset({
                "com.propertyai.telegram-approval",
                "com.propertyai.cleaning-operations",
                "com.propertyai.cleaning-completion",
            }),
        )
    raise ValueError(f"unsupported Cleaner runtime topology: {topology!r}")


def topology_contract_from_env(env: dict[str, str] | None = None) -> CleanerTopologyContract:
    values = os.environ if env is None else env
    raw = values.get(TOPOLOGY_ENV, CleanerRuntimeTopology.PRE_CUTOVER.value)
    try:
        topology = CleanerRuntimeTopology(raw)
    except ValueError as error:
        raise RuntimeError(f"INVALID_CLEANER_RUNTIME_TOPOLOGY:{raw}") from error
    return topology_contract(topology)


__all__ = [
    "CleanerRuntimeTopology",
    "CleanerTopologyContract",
    "TOPOLOGY_ENV",
    "topology_contract",
    "topology_contract_from_env",
]


def assert_legacy_cleaner_writer_allowed(writer_code: str, environment=None) -> None:
    """Deny retired Cleaner dispatchers; this is a negative guard, not authority.

    Missing pre-cutover configuration retains legacy compatibility, but either
    explicit post-cutover signal forbids W04/W05. A contradictory/stale tuple is
    never an instruction to fall back. Other services still need their existing
    route, source/config admission and real global writer lease.
    """
    if writer_code not in {"W04", "W05"}:
        return
    env = os.environ if environment is None else environment
    topology = env.get("PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY", "PRE_CUTOVER")
    authority = env.get("PROPERTYAI_CLEANER_AUTHORITY", "LEGACY")
    if topology not in {"PRE_CUTOVER", "POST_CUTOVER_PG"} or authority not in {"LEGACY", "POSTGRES"}:
        raise RuntimeError("LEGACY_CLEANER_WRITER_CONFIG_INVALID")
    if topology == "POST_CUTOVER_PG" or authority == "POSTGRES":
        raise RuntimeError(f"RETIRED_CLEANER_WRITER_FORBIDDEN:{writer_code}")
