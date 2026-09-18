from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from propertyai_core.adapters.sqlite.migrations import MIGRATIONS
from propertyai_core.domain.models import CommandEnvelope, EffectClaim, isoformat, utc_now
from propertyai_core.domain.states import (
    CommandState,
    EffectState,
    InvalidStateTransition,
    validate_transition,
)


class SQLiteStore:
    """Explicit TEST-only SQLite store; construction never creates a database."""

    def __init__(self, database_path: Path, *, data_environment: str, busy_timeout_ms: int = 5000):
        if data_environment != "TEST":
            raise ValueError("SQLiteStore is restricted to TEST")
        candidate = Path(database_path).expanduser().resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if not candidate.is_relative_to(temporary_root):
            raise ValueError("SQLiteStore database must be inside the system temporary directory")
        self.database_path = candidate
        self.busy_timeout_ms = int(busy_timeout_ms)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path), timeout=self.busy_timeout_ms / 1000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except InvalidStateTransition as error:
            connection.execute("ROLLBACK")
            connection.close()
            context = getattr(error, "audit_context", None)
            if context is not None:
                self._persist_invalid_transition(*context)
            raise
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migration (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migration").fetchall()
            }
            for version, name, sql in MIGRATIONS:
                if version in applied:
                    continue
                for statement in sql.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migration(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, isoformat(utc_now())),
                )

    def pragmas(self) -> Dict[str, Any]:
        with self.connect() as connection:
            return {
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
                "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            }

    def register_command(self, envelope: CommandEnvelope) -> Tuple[str, bool, bool]:
        """Return command id, reused flag, and payload-conflict flag."""
        now = isoformat(utc_now())
        payload_hash = envelope.canonical_hash()
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT command_id, canonical_payload_hash FROM command_execution WHERE idempotency_key = ?",
                (envelope.idempotency_key,),
            ).fetchone()
            if existing:
                conflict = existing["canonical_payload_hash"] != payload_hash
                return existing["command_id"], not conflict, conflict

            connection.execute(
                """
                INSERT INTO command_execution(
                    command_id, command_type, idempotency_key, canonical_payload_hash,
                    status, data_environment, source_channel, expected_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.command_id,
                    envelope.command_type,
                    envelope.idempotency_key,
                    payload_hash,
                    CommandState.RECEIVED.value,
                    envelope.data_environment,
                    envelope.source_channel,
                    envelope.expected_version,
                    now,
                    now,
                ),
            )
            effect_a = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{envelope.idempotency_key}:effect-a"))
            effect_b = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{envelope.idempotency_key}:effect-b"))
            connection.executemany(
                """
                INSERT INTO outbox_effect(
                    effect_id, command_id, effect_type, effect_idempotency_key,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        effect_a,
                        envelope.command_id,
                        "TEST_RECORD_CONFIRMATION",
                        f"{envelope.idempotency_key}:record",
                        EffectState.READY.value,
                        now,
                        now,
                    ),
                    (
                        effect_b,
                        envelope.command_id,
                        "TEST_BUILD_NOTIFICATION",
                        f"{envelope.idempotency_key}:notification",
                        EffectState.PENDING.value,
                        now,
                        now,
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO effect_dependency(effect_id, depends_on_effect_id) VALUES (?, ?)",
                (effect_b, effect_a),
            )
            self._audit(
                connection,
                "command",
                envelope.command_id,
                "COMMAND_REGISTERED",
                None,
                CommandState.RECEIVED.value,
                {"command_type": envelope.command_type, "data_environment": "TEST"},
                now,
            )
            return envelope.command_id, False, False

    def command_snapshot(self, command_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            command = connection.execute(
                "SELECT command_id, status FROM command_execution WHERE command_id = ?", (command_id,)
            ).fetchone()
            result = connection.execute(
                "SELECT result_code, result_json FROM command_result WHERE command_id = ?", (command_id,)
            ).fetchone()
            if command is None:
                raise KeyError(command_id)
            return {
                "command_id": command["command_id"],
                "status": command["status"],
                "result_code": result["result_code"] if result else "ACCEPTED",
                "result": json.loads(result["result_json"]) if result and result["result_json"] else None,
            }

    def effect_rows(self, command_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox_effect WHERE command_id = ? ORDER BY created_at, effect_type",
                (command_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _acquire_worker_lease(
        self, connection: sqlite3.Connection, lease_name: str, worker_id: str, now: str, lease_until: str
    ) -> bool:
        connection.execute(
            """
            INSERT INTO worker_lease(lease_name, lease_owner, lease_until, heartbeat_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(lease_name) DO UPDATE SET
                lease_owner = excluded.lease_owner,
                lease_until = excluded.lease_until,
                heartbeat_at = excluded.heartbeat_at
            WHERE worker_lease.lease_until <= excluded.heartbeat_at
               OR worker_lease.lease_owner = excluded.lease_owner
            """,
            (lease_name, worker_id, lease_until, now),
        )
        row = connection.execute(
            "SELECT lease_owner, lease_until FROM worker_lease WHERE lease_name = ?", (lease_name,)
        ).fetchone()
        return bool(row and row["lease_owner"] == worker_id and row["lease_until"] == lease_until)

    def claim_next_effect(
        self,
        worker_id: str,
        *,
        now: Optional[datetime] = None,
        lease_seconds: int = 30,
        lease_name: str = "propertyai-system-test-worker",
    ) -> Optional[EffectClaim]:
        current = now or utc_now()
        now_text = isoformat(current)
        lease_until = isoformat(current + timedelta(seconds=lease_seconds))
        with self.transaction(immediate=True) as connection:
            if not self._acquire_worker_lease(connection, lease_name, worker_id, now_text, lease_until):
                return None
            row = connection.execute(
                """
                SELECT effect.*
                FROM outbox_effect AS effect
                WHERE effect.status IN (?, ?)
                  AND (effect.next_attempt_at IS NULL OR effect.next_attempt_at <= ?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM effect_dependency AS dependency
                      JOIN outbox_effect AS prerequisite
                        ON prerequisite.effect_id = dependency.depends_on_effect_id
                      WHERE dependency.effect_id = effect.effect_id
                        AND prerequisite.status <> ?
                  )
                ORDER BY effect.created_at, effect.effect_type
                LIMIT 1
                """,
                (
                    EffectState.READY.value,
                    EffectState.RETRY_WAIT.value,
                    now_text,
                    EffectState.SUCCEEDED.value,
                ),
            ).fetchone()
            if row is None:
                return None
            self._transition_effect(connection, row["effect_id"], EffectState.RUNNING, now_text)
            attempt_count = int(row["attempt_count"]) + 1
            connection.execute(
                """
                UPDATE outbox_effect
                SET attempt_count = ?, lease_owner = ?, lease_until = ?, heartbeat_at = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (attempt_count, worker_id, lease_until, now_text, now_text, row["effect_id"]),
            )
            connection.execute(
                """
                INSERT INTO effect_attempt(effect_id, attempt_number, worker_id, started_at)
                VALUES (?, ?, ?, ?)
                """,
                (row["effect_id"], attempt_count, worker_id, now_text),
            )
            command = connection.execute(
                "SELECT status FROM command_execution WHERE command_id = ?", (row["command_id"],)
            ).fetchone()
            current_command = CommandState(command["status"])
            if current_command in {
                CommandState.RECEIVED,
                CommandState.FAILED_RETRYABLE,
                CommandState.PENDING_RECONCILIATION,
            }:
                self._transition_command(
                    connection, row["command_id"], CommandState.RUNNING, now_text
                )
            return EffectClaim(
                effect_id=row["effect_id"],
                command_id=row["command_id"],
                effect_type=row["effect_type"],
                attempt_count=attempt_count,
                worker_id=worker_id,
                lease_until=lease_until,
            )

    def heartbeat(
        self,
        claim: EffectClaim,
        *,
        now: Optional[datetime] = None,
        lease_seconds: int = 30,
        lease_name: str = "propertyai-system-test-worker",
    ) -> bool:
        current = now or utc_now()
        now_text = isoformat(current)
        lease_until = isoformat(current + timedelta(seconds=lease_seconds))
        with self.transaction(immediate=True) as connection:
            effect_updated = connection.execute(
                """
                UPDATE outbox_effect
                SET heartbeat_at = ?, lease_until = ?, updated_at = ?
                WHERE effect_id = ? AND status = ? AND lease_owner = ?
                """,
                (
                    now_text,
                    lease_until,
                    now_text,
                    claim.effect_id,
                    EffectState.RUNNING.value,
                    claim.worker_id,
                ),
            ).rowcount
            lease_updated = connection.execute(
                """
                UPDATE worker_lease SET heartbeat_at = ?, lease_until = ?
                WHERE lease_name = ? AND lease_owner = ?
                """,
                (now_text, lease_until, lease_name, claim.worker_id),
            ).rowcount
            return effect_updated == 1 and lease_updated == 1

    def complete_effect(self, claim: EffectClaim, result: Optional[Dict[str, Any]] = None) -> None:
        now = isoformat(utc_now())
        with self.transaction(immediate=True) as connection:
            self._require_claim(connection, claim)
            self._transition_effect(connection, claim.effect_id, EffectState.SUCCEEDED, now)
            connection.execute(
                """
                UPDATE outbox_effect SET lease_owner = NULL, lease_until = NULL,
                    heartbeat_at = NULL, last_error_code = NULL, updated_at = ?
                WHERE effect_id = ?
                """,
                (now, claim.effect_id),
            )
            connection.execute(
                """
                UPDATE effect_attempt SET finished_at = ?, outcome = 'SUCCEEDED'
                WHERE effect_id = ? AND attempt_number = ?
                """,
                (now, claim.effect_id, claim.attempt_count),
            )
            dependents = connection.execute(
                """
                SELECT pending.effect_id
                FROM outbox_effect AS pending
                WHERE pending.command_id = ? AND pending.status = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM effect_dependency AS dependency
                    JOIN outbox_effect AS prerequisite
                      ON prerequisite.effect_id = dependency.depends_on_effect_id
                    WHERE dependency.effect_id = pending.effect_id
                      AND prerequisite.status <> ?
                  )
                """,
                (claim.command_id, EffectState.PENDING.value, EffectState.SUCCEEDED.value),
            ).fetchall()
            for dependent in dependents:
                self._transition_effect(connection, dependent["effect_id"], EffectState.READY, now)
            remaining = connection.execute(
                "SELECT COUNT(*) FROM outbox_effect WHERE command_id = ? AND status <> ?",
                (claim.command_id, EffectState.SUCCEEDED.value),
            ).fetchone()[0]
            if remaining == 0:
                self._transition_command(connection, claim.command_id, CommandState.SUCCEEDED, now)
                safe_result = result or {"completed_effects": 2, "data_environment": "TEST"}
                connection.execute(
                    """
                    INSERT OR REPLACE INTO command_result(command_id, result_code, result_json, created_at)
                    VALUES (?, 'SUCCEEDED', ?, ?)
                    """,
                    (claim.command_id, json.dumps(safe_result, sort_keys=True), now),
                )

    def mark_effect_reconciliation(
        self, claim: EffectClaim, *, error_code: str
    ) -> None:
        """Persist an ambiguous external result without making it retry-eligible."""
        if not isinstance(error_code, str) or not error_code:
            raise ValueError("error_code is required")
        now = isoformat(utc_now())
        with self.transaction(immediate=True) as connection:
            self._require_claim(connection, claim)
            self._transition_effect(
                connection, claim.effect_id, EffectState.PENDING_RECONCILIATION, now
            )
            connection.execute(
                """
                UPDATE outbox_effect SET next_attempt_at = NULL, last_error_code = ?,
                    lease_owner = NULL, lease_until = NULL, heartbeat_at = NULL, updated_at = ?
                WHERE effect_id = ?
                """,
                (error_code, now, claim.effect_id),
            )
            connection.execute(
                """
                UPDATE effect_attempt SET finished_at = ?, outcome = 'PENDING_RECONCILIATION',
                    error_code = ?
                WHERE effect_id = ? AND attempt_number = ?
                """,
                (now, error_code, claim.effect_id, claim.attempt_count),
            )
            command = connection.execute(
                "SELECT status FROM command_execution WHERE command_id = ?",
                (claim.command_id,),
            ).fetchone()
            if CommandState(command["status"]) != CommandState.PENDING_RECONCILIATION:
                self._transition_command(
                    connection, claim.command_id, CommandState.PENDING_RECONCILIATION, now
                )

    def fail_effect(
        self,
        claim: EffectClaim,
        *,
        error_code: str,
        retryable: bool,
        max_attempts: int,
        retry_delay_seconds: int = 5,
        now: Optional[datetime] = None,
    ) -> None:
        current = now or utc_now()
        now_text = isoformat(current)
        with self.transaction(immediate=True) as connection:
            self._require_claim(connection, claim)
            should_retry = retryable and claim.attempt_count < max_attempts
            effect_target = EffectState.RETRY_WAIT if should_retry else EffectState.DEAD_LETTER
            command_target = (
                CommandState.FAILED_RETRYABLE if should_retry else CommandState.OPERATOR_REQUIRED
            )
            self._transition_effect(connection, claim.effect_id, effect_target, now_text)
            next_attempt = (
                isoformat(current + timedelta(seconds=retry_delay_seconds)) if should_retry else None
            )
            connection.execute(
                """
                UPDATE outbox_effect SET next_attempt_at = ?, last_error_code = ?,
                    lease_owner = NULL, lease_until = NULL, heartbeat_at = NULL, updated_at = ?
                WHERE effect_id = ?
                """,
                (next_attempt, error_code, now_text, claim.effect_id),
            )
            connection.execute(
                """
                UPDATE effect_attempt SET finished_at = ?, outcome = ?, error_code = ?
                WHERE effect_id = ? AND attempt_number = ?
                """,
                (now_text, effect_target.value, error_code, claim.effect_id, claim.attempt_count),
            )
            command = connection.execute(
                "SELECT status FROM command_execution WHERE command_id = ?", (claim.command_id,)
            ).fetchone()
            if CommandState(command["status"]) != command_target:
                self._transition_command(connection, claim.command_id, command_target, now_text)

    def recover_stale(
        self, *, now: Optional[datetime] = None, policy: str = "reconcile"
    ) -> int:
        if policy not in {"retry", "reconcile"}:
            raise ValueError("unknown recovery policy")
        current = now or utc_now()
        now_text = isoformat(current)
        effect_target = (
            EffectState.READY if policy == "retry" else EffectState.PENDING_RECONCILIATION
        )
        command_target = (
            CommandState.RUNNING if policy == "retry" else CommandState.PENDING_RECONCILIATION
        )
        with self.transaction(immediate=True) as connection:
            stale = connection.execute(
                "SELECT effect_id, command_id FROM outbox_effect WHERE status = ? AND lease_until <= ?",
                (EffectState.RUNNING.value, now_text),
            ).fetchall()
            for row in stale:
                self._transition_effect(connection, row["effect_id"], effect_target, now_text)
                connection.execute(
                    """
                    UPDATE outbox_effect SET lease_owner = NULL, lease_until = NULL,
                        heartbeat_at = NULL, updated_at = ? WHERE effect_id = ?
                    """,
                    (now_text, row["effect_id"]),
                )
                current_command = connection.execute(
                    "SELECT status FROM command_execution WHERE command_id = ?", (row["command_id"],)
                ).fetchone()
                if CommandState(current_command["status"]) != command_target:
                    self._transition_command(connection, row["command_id"], command_target, now_text)
            return len(stale)

    def audit_text(self) -> str:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT event_type, metadata_json FROM audit_record ORDER BY audit_id"
            ).fetchall()
            return "\n".join(f"{row['event_type']} {row['metadata_json']}" for row in rows)

    def _require_claim(self, connection: sqlite3.Connection, claim: EffectClaim) -> None:
        row = connection.execute(
            "SELECT status, lease_owner FROM outbox_effect WHERE effect_id = ?", (claim.effect_id,)
        ).fetchone()
        if row is None or row["status"] != EffectState.RUNNING.value or row["lease_owner"] != claim.worker_id:
            raise RuntimeError("effect claim is not active")

    def _transition_command(
        self, connection: sqlite3.Connection, command_id: str, target: CommandState, now: str
    ) -> None:
        row = connection.execute(
            "SELECT status FROM command_execution WHERE command_id = ?", (command_id,)
        ).fetchone()
        current = CommandState(row["status"])
        try:
            validate_transition(current, target)
        except InvalidStateTransition as error:
            error.audit_context = ("command", command_id, current.value, target.value)
            raise
        connection.execute(
            "UPDATE command_execution SET status = ?, updated_at = ? WHERE command_id = ?",
            (target.value, now, command_id),
        )
        self._audit(connection, "command", command_id, "STATE_TRANSITION", current.value, target.value, {}, now)

    def _transition_effect(
        self, connection: sqlite3.Connection, effect_id: str, target: EffectState, now: str
    ) -> None:
        row = connection.execute(
            "SELECT status FROM outbox_effect WHERE effect_id = ?", (effect_id,)
        ).fetchone()
        current = EffectState(row["status"])
        try:
            validate_transition(current, target)
        except InvalidStateTransition as error:
            error.audit_context = ("effect", effect_id, current.value, target.value)
            raise
        connection.execute(
            "UPDATE outbox_effect SET status = ?, updated_at = ? WHERE effect_id = ?",
            (target.value, now, effect_id),
        )
        self._audit(connection, "effect", effect_id, "STATE_TRANSITION", current.value, target.value, {}, now)

    def _persist_invalid_transition(
        self, aggregate_type: str, aggregate_id: str, from_state: str, to_state: str
    ) -> None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._audit(
                connection,
                aggregate_type,
                aggregate_id,
                "INVALID_TRANSITION",
                from_state,
                to_state,
                {},
                isoformat(utc_now()),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        from_state: Optional[str],
        to_state: Optional[str],
        metadata: Dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_record(
                aggregate_type, aggregate_id, event_type, from_state,
                to_state, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                aggregate_type,
                aggregate_id,
                event_type,
                from_state,
                to_state,
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                created_at,
            ),
        )
