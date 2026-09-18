"""Bounded disposable PostgreSQL backup and independent logical restore.

This is not a Production backup service, credential provisioner or RPO guarantee.
A consistent exported snapshot binds the data oracle and pg_dump. Restore requires
pre-provisioned matching roles, a different clean cluster, and an externally bound
receipt hash. No --clean, overwrite, role grant, or implicit target discovery exists.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time

from psycopg import sql

from .common import (LocalTarget, OperationalError, canonical, checked_file, clean_env, digest,
                     observe, require, write_new)


def role_facts(conn) -> dict:
    roles = conn.execute("SELECT rolname,rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                         "FROM pg_roles WHERE rolname LIKE 'propertyai_%' OR rolname LIKE 'rent_test_%' "
                         "OR rolname LIKE 'rent_session_%' OR rolname LIKE 'rent_scheduler_test_%' ORDER BY rolname").fetchall()
    names = [r[0] for r in roles]
    edges = conn.execute("SELECT r.rolname,u.rolname,m.admin_option,m.inherit_option,m.set_option "
                         "FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member "
                         "WHERE r.rolname=ANY(%s) OR u.rolname=ANY(%s) ORDER BY 1,2", (names, names)).fetchall()
    return {"roles": [list(r) for r in roles], "memberships": [list(r) for r in edges]}


def extension_facts(conn) -> list:
    return [list(r) for r in conn.execute(
        "SELECT e.extname,e.extversion,n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace ORDER BY e.extname").fetchall()]


_AUTH_SUBJECT_GROUPED = "CHECK ((((length(subject) >= 1) AND (length(subject) <= 512)) AND (subject = btrim(subject))))"
_AUTH_SUBJECT_FLAT = "CHECK (((length(subject) >= 1) AND (length(subject) <= 512) AND (subject = btrim(subject))))"


def constraint_fact(table: str, name: str, validated: bool, definition: str) -> list:
    """One bounded deparser equivalence observed in the frozen V112 constraint.

    PostgreSQL dump/reparse flattens (A AND B) AND C. This exact pair is
    associative even under SQL three-valued logic. No general SQL rewriting,
    predicate removal, column/name changes, or NOT VALID state is hidden.
    """
    if (table, name, definition) == ("propertyai.rent_auth_session", "ck_rent_auth_subject", _AUTH_SUBJECT_GROUPED):
        definition = _AUTH_SUBJECT_FLAT
    return [table, name, validated, definition]


def logical_snapshot(conn) -> dict:
    """Hash complete rows, definitions, constraints, grants and sequence state; never emit PII."""
    # pg_dump may elide an explicit owner-only ACL. Compare effective defaults,
    # not physical NULL-vs-array representation; retain every actual grant.
    tables = conn.execute("SELECT relname,relrowsecurity,relforcerowsecurity,relowner::regrole::text,ARRAY(SELECT a::text FROM unnest(coalesce(relacl,acldefault('r',relowner))) a ORDER BY a::text) "
                          "FROM pg_class WHERE relnamespace='propertyai'::regnamespace AND relkind='r' ORDER BY relname").fetchall()
    facts = {}
    for table in tables:
        rows = conn.execute(sql.SQL("SELECT to_jsonb(t)::text FROM propertyai.{} t ORDER BY to_jsonb(t)::text").format(sql.Identifier(table[0]))).fetchall()
        facts[table[0]] = {"count": len(rows), "sha256": digest(canonical([r[0] for r in rows]))}
    constraints = [constraint_fact(*row) for row in conn.execute(
        "SELECT conrelid::regclass::text,conname,convalidated,pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE connamespace='propertyai'::regnamespace ORDER BY 1,2,4").fetchall()]
    columns = conn.execute("SELECT table_name,column_name,data_type,is_nullable,column_default "
                           "FROM information_schema.columns WHERE table_schema='propertyai' ORDER BY table_name,ordinal_position").fetchall()
    functions = conn.execute("SELECT oid::regprocedure::text,proowner::regrole::text,ARRAY(SELECT a::text FROM unnest(coalesce(proacl,acldefault('f',proowner))) a ORDER BY a::text),pg_get_functiondef(oid) "
                             "FROM pg_proc WHERE pronamespace='propertyai'::regnamespace ORDER BY oid::regprocedure::text").fetchall()
    views = conn.execute("SELECT relname,relowner::regrole::text,ARRAY(SELECT a::text FROM unnest(coalesce(relacl,acldefault('r',relowner))) a ORDER BY a::text),pg_get_viewdef(oid) "
                         "FROM pg_class WHERE relnamespace='propertyai'::regnamespace AND relkind='v' ORDER BY relname").fetchall()
    sequences = {}
    for (name,) in conn.execute("SELECT relname FROM pg_class WHERE relnamespace='propertyai'::regnamespace AND relkind='S' ORDER BY relname"):
        sequences[name] = list(conn.execute(sql.SQL("SELECT last_value,is_called FROM propertyai.{}").format(sql.Identifier(name))).fetchone())
    return {"tables": facts, "schema_sha256": digest(canonical([tables, constraints, columns, functions, views])),
            "sequences": sequences, "roles": role_facts(conn), "extensions": extension_facts(conn)}


def backup(target, login: str, pg_dump: Path, destination: Path) -> dict:
    """Reuse one backup algorithm across owned disposable and explicit persistent targets."""
    state = "FAILED_PRE_EFFECT"
    started = time.monotonic()
    try:
        target.guard()
        pg_dump = Path(pg_dump).resolve()
        tool_hash = digest(checked_file(pg_dump))
        destination = Path(destination)
        require(destination.is_absolute() and destination == destination.resolve(), "BACKUP_DESTINATION_INVALID")
        if isinstance(target, LocalTarget):
            require(not destination.is_relative_to(target.root), "BACKUP_DESTINATION_INVALID")
            backup_env = {**clean_env(), "PGPASSFILE": str(target.root / ".w3b-no-password")}
        else:
            require(
                getattr(target, "backup_destination", None) == destination
                and callable(getattr(target, "backup_env", None)),
                "BACKUP_DESTINATION_INVALID",
            )
            backup_env = target.backup_env()
        # An existing path is never a successful backup or an overwrite target.
        destination.mkdir(mode=0o700, exist_ok=False)
        state = "FAILED_UNKNOWN_EFFECT"
        with target.connect(login, autocommit=False) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout=120000")
            identity = target.identity(conn)
            snapshot_id = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            facts = logical_snapshot(conn)
            output = destination / "database.dump"
            with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
                command = [str(pg_dump), *target.pg_args(login), "--format=custom", "--schema=propertyai", "--snapshot=" + snapshot_id]
                result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, env=backup_env, timeout=60)
                stream.flush()
                os.fsync(stream.fileno())
            require(result.returncode == 0, "BACKUP_FAILED")
            data = checked_file(output)
            require(data.startswith(b"PGDMP") and len(data) > 5, "BACKUP_FAILED")
        receipt = {"format": "RENT_BACKUP_V1", "action_state": "COMPLETED", "source": identity,
                   "archive": "database.dump", "archive_sha256": digest(data), "archive_bytes": len(data),
                   "snapshot": facts, "tool_sha256": tool_hash, "restore_proven": False,
                   "created_at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": round(time.monotonic() - started, 6)}
        write_new(destination / "backup-receipt.json", canonical(receipt))
        require(checked_file(destination / "backup-receipt.json") == canonical(receipt), "BACKUP_INVALID")
        observe("BACKUP", "COMPLETED")
        return receipt
    except Exception:
        observe("BACKUP", state, error_class="BACKUP_FAILED")
        raise OperationalError("BACKUP_FAILED", state) from None


def restore(target: LocalTarget, login: str, pg_restore: Path, backup_directory: Path, receipt_sha256: str) -> dict:
    state = "FAILED_PRE_EFFECT"
    started = time.monotonic()
    pending = target.root / "w3b-restore-pending.json"
    try:
        target.guard()
        receipt = json.loads(checked_file(backup_directory / "backup-receipt.json", receipt_sha256))
        require(receipt["format"] == "RENT_BACKUP_V1" and receipt["action_state"] == "COMPLETED"
                and receipt["archive"] == "database.dump", "BACKUP_INVALID")
        archive = backup_directory / "database.dump"
        data = checked_file(archive, receipt["archive_sha256"])
        require(len(data) == receipt["archive_bytes"] and data.startswith(b"PGDMP"), "BACKUP_INVALID")
        pg_restore = Path(pg_restore).resolve()
        tool_hash = digest(checked_file(pg_restore))
        with target.connect(login) as conn:
            identity = target.identity(conn)
            source = receipt["source"]
            if source.get("source_class") == "PERSISTENT_STAGING":
                require(isinstance(source.get("target_id"), str) and bool(source["target_id"]),
                        "BACKUP_INVALID")
            else:
                require(identity["system_identifier"] != source["system_identifier"],
                        "RESTORE_SOURCE_EQUALS_TARGET")
            require(conn.execute("SELECT to_regnamespace('propertyai') IS NULL").fetchone()[0], "RESTORE_TARGET_NOT_CLEAN")
            require(role_facts(conn) == receipt["snapshot"]["roles"], "RESTORE_ROLE_PREREQUISITES_INVALID")
            require(extension_facts(conn) == receipt["snapshot"]["extensions"], "RESTORE_EXTENSION_PREREQUISITES_INVALID")
            require(identity["server_version"] // 10000 == receipt["source"]["server_version"] // 10000, "RESTORE_SERVER_VERSION_INVALID")
        pending_bytes = canonical({"backup_receipt_sha256": receipt_sha256, "target": identity})
        write_new(pending, pending_bytes)
        state = "FAILED_UNKNOWN_EFFECT"
        result = subprocess.run([str(pg_restore), *target.pg_args(login), "--single-transaction", "--exit-on-error", str(archive)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**clean_env(), "PGPASSFILE": str(target.root / ".w3b-no-password")}, timeout=60)
        require(result.returncode == 0, "RESTORE_COMMAND_FAILED")
        checked_file(archive, receipt["archive_sha256"])
        with target.connect(login, autocommit=False) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            restored = logical_snapshot(conn)
        require(restored == receipt["snapshot"], "RESTORE_LOGICAL_MISMATCH")
        result_receipt = {"format": "RENT_RESTORE_V1", "action_state": "RECOVERED", "target": identity,
                          "backup_receipt_sha256": receipt_sha256, "archive_sha256": receipt["archive_sha256"],
                          "snapshot_sha256": digest(canonical(restored)), "logical_integrity": "PASS",
                          "tool_sha256": tool_hash, "elapsed_seconds": round(time.monotonic() - started, 6)}
        write_new(target.root / "w3b-restore-completed.json", canonical(result_receipt))
        require(checked_file(pending) == pending_bytes, "RESTORE_PENDING_CHANGED")
        pending.unlink()
        observe("RESTORE", "RECOVERED")
        return result_receipt
    except Exception as error:
        # A pending marker is deliberately retained after a started failure. Web
        # readiness is closed until separately proven recovery or owned cleanup.
        observe("RESTORE", state, error_class="RESTORE_FAILED")
        allowed = {"RESTORE_ROLE_PREREQUISITES_INVALID", "RESTORE_EXTENSION_PREREQUISITES_INVALID",
                   "RESTORE_SERVER_VERSION_INVALID", "RESTORE_SOURCE_EQUALS_TARGET", "RESTORE_TARGET_NOT_CLEAN",
                   "RESTORE_COMMAND_FAILED", "RESTORE_LOGICAL_MISMATCH", "HASH_MISMATCH", "BACKUP_INVALID"}
        code = error.code if isinstance(error, OperationalError) and error.code in allowed else "RESTORE_FAILED"
        raise OperationalError(code, state) from None


def backup_freshness(directory: Path, receipt_sha256: str, *, max_age_seconds: int, now: datetime | None = None) -> str:
    """Caller-selected observation window, NOT a selected Production RPO."""
    require(type(max_age_seconds) is int and max_age_seconds > 0, "FRESHNESS_WINDOW_REQUIRED")
    now = now or datetime.now(timezone.utc)
    try:
        receipt = json.loads(checked_file(directory / "backup-receipt.json", receipt_sha256))
        require(receipt["format"] == "RENT_BACKUP_V1" and receipt["action_state"] == "COMPLETED"
                and receipt["archive"] == "database.dump", "BACKUP_INVALID")
        checked_file(directory / "database.dump", receipt["archive_sha256"])
        created = datetime.fromisoformat(receipt["created_at"])
        age = (now - created).total_seconds()
        require(age >= 0, "BACKUP_INVALID")
        value = "BACKUP_FRESH" if age <= max_age_seconds else "BACKUP_STALE"
    except FileNotFoundError:
        value = "BACKUP_INVALID" if Path(directory).exists() else "BACKUP_MISSING"
    except Exception:
        value = "BACKUP_INVALID"
    observe("BACKUP", value, error_class="BACKUP_INVALID" if value == "BACKUP_INVALID" else "NONE")
    return value
