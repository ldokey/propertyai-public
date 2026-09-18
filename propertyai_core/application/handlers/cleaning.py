from __future__ import annotations

from propertyai_core.adapters.sqlite.store import SQLiteStore
from propertyai_core.application.commands.cleaning import RecordCleaningDayConfirmationCommand
from propertyai_core.application.results.models import CommandResult
from propertyai_core.config.flags import FeatureFlags


class RecordCleaningDayConfirmationHandler:
    def __init__(self, store: SQLiteStore, flags: FeatureFlags):
        self.store = store
        self.flags = flags

    def handle(self, command: RecordCleaningDayConfirmationCommand) -> CommandResult:
        envelope = command.envelope
        if not self.flags.permits_system_test(envelope.data_environment, envelope.source_channel):
            return CommandResult(
                command_id=envelope.command_id,
                status="REJECTED",
                code="APPLICATION_GATE_CLOSED",
            )
        try:
            command.validate_synthetic_payload()
        except ValueError:
            return CommandResult(
                command_id=envelope.command_id,
                status="REJECTED",
                code="SYNTHETIC_PAYLOAD_REQUIRED",
            )

        command_id, reused, conflict = self.store.register_command(envelope)
        if conflict:
            return CommandResult(
                command_id=command_id,
                status="REJECTED",
                code="REJECTED_STATE_CONFLICT",
            )
        snapshot = self.store.command_snapshot(command_id)
        return CommandResult(
            command_id=command_id,
            status=snapshot["status"],
            code=snapshot["result_code"],
            reused=reused,
            result=snapshot["result"],
        )
