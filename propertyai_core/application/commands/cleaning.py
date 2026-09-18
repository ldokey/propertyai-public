from __future__ import annotations

from dataclasses import dataclass

from propertyai_core.domain.models import CommandEnvelope


@dataclass(frozen=True)
class RecordCleaningDayConfirmationCommand:
    envelope: CommandEnvelope

    def validate_synthetic_payload(self) -> None:
        if self.envelope.command_type != "RecordCleaningDayConfirmationCommand":
            raise ValueError("unsupported command type")
        cleaning_id = self.envelope.payload.get("cleaning_id")
        if not isinstance(cleaning_id, str) or not cleaning_id.startswith("synthetic-cleaning-"):
            raise ValueError("SYSTEM_TEST requires a synthetic cleaning id")
