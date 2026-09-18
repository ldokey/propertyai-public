# PropertyAI TEST-only Application Gate

Phase 3A-4 supplies a deliberately narrow durable command/outbox slice. It is not
connected to legacy services or any external API.

Execution is fail-closed unless all TEST flags are enabled, the envelope uses
`data_environment=TEST` and `source_channel=SYSTEM_TEST`, and production writes
remain disabled. The only supported command is the synthetic
`RecordCleaningDayConfirmationCommand`.

The SQLite store is constructed explicitly with `data_environment="TEST"`; it does
not create a database at import time. Tests pass a pytest `tmp_path` database.

No Notion, Telegram, Gmail, Calendar, Drive, OpenClaw, Ollama, or `launchctl`
adapter exists in this package.
