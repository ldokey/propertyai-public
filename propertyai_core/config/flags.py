from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


_TRUE = {"1", "true", "yes", "on"}


def _enabled(environment: Mapping[str, str], name: str) -> bool:
    value = environment.get(name)
    if value is None:
        return False
    return value.strip().lower() in _TRUE


@dataclass(frozen=True)
class FeatureFlags:
    application_gate_enabled: bool = False
    system_test_enabled: bool = False
    durable_outbox_enabled: bool = False
    cleaning_day_command_enabled: bool = False
    production_writes_enabled: bool = False
    health_restart_guard_enabled: bool = False

    @classmethod
    def from_environment(cls, environment: Optional[Mapping[str, str]] = None) -> "FeatureFlags":
        source = environment or {}
        return cls(
            application_gate_enabled=_enabled(source, "PROPERTYAI_APPLICATION_GATE_ENABLED"),
            system_test_enabled=_enabled(source, "PROPERTYAI_SYSTEM_TEST_ENABLED"),
            durable_outbox_enabled=_enabled(source, "PROPERTYAI_DURABLE_OUTBOX_ENABLED"),
            cleaning_day_command_enabled=_enabled(source, "PROPERTYAI_CLEANING_DAY_COMMAND_ENABLED"),
            production_writes_enabled=_enabled(source, "PROPERTYAI_PRODUCTION_WRITES_ENABLED"),
            health_restart_guard_enabled=_enabled(source, "PROPERTYAI_HEALTH_RESTART_GUARD_ENABLED"),
        )

    def permits_system_test(self, data_environment: str, source_channel: str) -> bool:
        return all(
            (
                self.application_gate_enabled,
                self.system_test_enabled,
                self.durable_outbox_enabled,
                self.cleaning_day_command_enabled,
                not self.production_writes_enabled,
                data_environment == "TEST",
                source_channel == "SYSTEM_TEST",
            )
        )
