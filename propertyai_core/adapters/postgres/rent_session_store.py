"""Durable PostgreSQL adapter for the W1 opaque Rent session contract."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import re

import psycopg
from psycopg.rows import dict_row

from propertyai_core.web.auth_context import AuthorizedPrincipal
from propertyai_core.web.auth_session import SessionRecord

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{32}\Z")


class PostgresSessionStore:
    """Authoritative store with no bearer plaintext and no process-local cache."""

    persistent = True

    def __init__(self, connect: Callable[[], psycopg.Connection]):
        if not callable(connect):
            raise ValueError("SESSION_CONNECT_FACTORY_REQUIRED")
        self._connect = connect

    def create(self, record: SessionRecord) -> bool:
        if not isinstance(record, SessionRecord):
            raise ValueError("INVALID_SESSION_RECORD")
        conn = self._connection()
        try:
            row = conn.execute(
                "SELECT propertyai.rent_auth_session_create(%s,%s,%s,%s,%s,%s,%s,%s,%s) AS created",
                (
                    record.session_id,
                    record.token_digest,
                    record.principal.organization_id,
                    record.principal.actor_party_id,
                    record.principal.subject,
                    sorted(record.principal.capabilities),
                    record.csrf_token,
                    record.issued_at,
                    record.expires_at,
                ),
            ).fetchone()
            created = row["created"] if row else None
            if type(created) is not bool:
                raise RuntimeError("SESSION_CREATE_RESULT_INVALID")
            conn.commit()
            return created
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def get(self, token_digest: str) -> SessionRecord | None:
        if not isinstance(token_digest, str) or not _DIGEST.fullmatch(token_digest):
            return None
        conn = self._connection()
        try:
            row = conn.execute(
                "SELECT * FROM propertyai.rent_auth_session_get(%s)", (token_digest,)
            ).fetchone()
            conn.commit()
            if row is None:
                return None
            principal = AuthorizedPrincipal(
                row["organization_id"],
                row["actor_party_id"],
                row["subject"],
                frozenset(row["capabilities"]),
            )
            return SessionRecord(
                session_id=row["session_id"],
                token_digest=row["token_digest"],
                principal=principal,
                csrf_token=row["csrf_token"],
                issued_at=row["issued_at"],
                expires_at=row["expires_at"],
                revoked_at=row["revoked_at"],
            )
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def revoke(self, session_id: str, revoked_at: datetime) -> bool:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            return False
        conn = self._connection()
        try:
            row = conn.execute(
                "SELECT propertyai.rent_auth_session_revoke(%s,%s) AS revoked",
                (session_id, revoked_at),
            ).fetchone()
            revoked = row["revoked"] if row else None
            if type(revoked) is not bool:
                raise RuntimeError("SESSION_REVOKE_RESULT_INVALID")
            conn.commit()
            return revoked
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def _connection(self) -> psycopg.Connection:
        conn = self._connect()
        if conn is None or not hasattr(conn, "execute"):
            raise RuntimeError("SESSION_CONNECTION_INVALID")
        conn.autocommit = False
        conn.row_factory = dict_row
        return conn

    @staticmethod
    def _rollback(conn: psycopg.Connection) -> None:
        try:
            conn.rollback()
        except Exception:
            pass

    @staticmethod
    def _close(conn: psycopg.Connection) -> None:
        try:
            conn.close()
        except Exception:
            pass
