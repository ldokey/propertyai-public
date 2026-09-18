from __future__ import annotations

from typing import Tuple


Migration = Tuple[int, str, str]


MIGRATIONS: Tuple[Migration, ...] = (
    (
        1,
        "initial_durable_application_gate",
        """
        CREATE TABLE command_execution (
            command_id TEXT PRIMARY KEY,
            command_type TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            canonical_payload_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            data_environment TEXT NOT NULL CHECK (data_environment = 'TEST'),
            source_channel TEXT NOT NULL CHECK (source_channel = 'SYSTEM_TEST'),
            expected_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE command_result (
            command_id TEXT PRIMARY KEY REFERENCES command_execution(command_id) ON DELETE CASCADE,
            result_code TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE outbox_effect (
            effect_id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL REFERENCES command_execution(command_id) ON DELETE CASCADE,
            effect_type TEXT NOT NULL,
            effect_idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at TEXT,
            lease_owner TEXT,
            lease_until TEXT,
            heartbeat_at TEXT,
            last_error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE effect_dependency (
            effect_id TEXT NOT NULL REFERENCES outbox_effect(effect_id) ON DELETE CASCADE,
            depends_on_effect_id TEXT NOT NULL REFERENCES outbox_effect(effect_id) ON DELETE CASCADE,
            PRIMARY KEY (effect_id, depends_on_effect_id),
            CHECK (effect_id <> depends_on_effect_id)
        );

        CREATE TABLE effect_attempt (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            effect_id TEXT NOT NULL REFERENCES outbox_effect(effect_id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            outcome TEXT,
            error_code TEXT,
            UNIQUE (effect_id, attempt_number)
        );

        CREATE TABLE audit_record (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE worker_lease (
            lease_name TEXT PRIMARY KEY,
            lease_owner TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        );

        CREATE INDEX idx_outbox_claim
            ON outbox_effect(status, next_attempt_at, created_at);
        CREATE INDEX idx_effect_dependency_effect
            ON effect_dependency(effect_id);
        """,
    ),
)
