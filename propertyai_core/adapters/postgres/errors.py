from __future__ import annotations

import psycopg
from psycopg import errors


class PostgresRepositoryError(RuntimeError):
    code = "POSTGRES_REPOSITORY_ERROR"
    retryable = False


class PostgresRetryableError(PostgresRepositoryError):
    code = "POSTGRES_RETRYABLE"
    retryable = True


class PostgresConfigurationError(PostgresRepositoryError):
    code = "POSTGRES_CONFIGURATION_ERROR"


class PostgresConstraintError(PostgresRepositoryError):
    code = "POSTGRES_CONSTRAINT"


class PostgresConflictError(PostgresRepositoryError):
    code = "POSTGRES_CONFLICT"


class AuthorityEpochError(PostgresRepositoryError):
    code = "AUTHORITY_EPOCH_INVALID"


class ReservationRevisionError(PostgresRepositoryError):
    code = "RESERVATION_REVISION_INVALID"


class IdempotencyConflictError(PostgresConflictError):
    code = "IDEMPOTENCY_CONFLICT"


class ReservationIdentityConflictError(PostgresConflictError):
    code = "RESERVATION_IDENTITY_CONFLICT"


class PostgresRoleMismatchError(PostgresConfigurationError):
    code = "POSTGRES_ROLE_MISMATCH"


def map_postgres_error(error: BaseException) -> PostgresRepositoryError:
    message = str(error)

    # Domain trigger exceptions are deterministic even though PostgreSQL exposes
    # them through a generic database-error class.
    if "STALE_AUTHORITY_EPOCH" in message or "AUTHORITY_SCOPE_NOT_FOUND" in message:
        return AuthorityEpochError(message)
    if "RESERVATION_SOURCE_VERSION_" in message:
        return ReservationRevisionError(message)

    # Authentication, authorization, and privilege/configuration faults must
    # fail closed.  They are never promoted to retryable by their Python base
    # class (some psycopg versions expose them beneath OperationalError).
    if isinstance(
        error,
        (
            errors.InvalidPassword,
            errors.InvalidAuthorizationSpecification,
            errors.InsufficientPrivilege,
        ),
    ):
        return PostgresConfigurationError(message)

    # Positive allow-list of database conditions for which a retry may be
    # meaningful.  Stage A classifies them; it does not implement retry loops.
    if isinstance(
        error,
        (
            errors.SerializationFailure,
            errors.DeadlockDetected,
            errors.LockNotAvailable,
            errors.ConnectionFailure,
            errors.ConnectionDoesNotExist,
            errors.SqlclientUnableToEstablishSqlconnection,
        ),
    ):
        return PostgresRetryableError(message)

    if isinstance(error, errors.UniqueViolation):
        return PostgresConflictError(message)
    if isinstance(
        error,
        (
            errors.ForeignKeyViolation,
            errors.CheckViolation,
            errors.NotNullViolation,
            errors.ExclusionViolation,
        ),
    ):
        return PostgresConstraintError(message)

    # In particular, generic OperationalError is intentionally non-retryable:
    # unknown auth/config/deterministic failures fail closed by default.
    if isinstance(error, psycopg.Error):
        return PostgresRepositoryError(message)
    return PostgresRepositoryError(message)
