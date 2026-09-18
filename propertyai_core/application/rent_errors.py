"""Sanitized Rent application errors."""
from __future__ import annotations


class RentError(Exception):
    def __init__(self, code: str, status: int, *, retryable: bool = False, state_unknown: bool = False, latest_versions: list[dict] | None = None):
        self.code, self.status = code, status
        self.retryable, self.state_unknown = retryable, state_unknown
        self.latest_versions = latest_versions or []
        super().__init__(code)

    def wire(self, request_id: str) -> dict:
        return {
            "code": self.code, "message": self.code.replace("_", " ").title(),
            "request_id": request_id, "retryable": self.retryable, "state_unknown": self.state_unknown,
            "field_errors": [], "latest_versions": self.latest_versions,
        }


def rent_error(code: str, *, latest_versions: list[dict] | None = None) -> RentError:
    statuses = {
        "UNAUTHENTICATED": 401, "NOT_AUTHORIZED": 403, "NOT_FOUND": 404,
        "VALIDATION_ERROR": 422, "VERSION_CONFLICT": 409, "IDEMPOTENCY_CONFLICT": 409,
        "CONTRACT_NOT_READY": 409, "PERIOD_CONFLICT": 409,
        "BILLING_NOT_YET_DUE": 422, "CHARGE_CORRECTION_REQUIRED": 409,
        "ATTRIBUTION_REQUIRED": 422, "ALLOCATION_EXCEEDS_AVAILABLE": 422,
        "RECEIVABLE_OVERALLOCATED": 422, "RETRYABLE_TRANSACTION": 503,
        "COMMIT_RESULT_UNKNOWN": 503, "INTERNAL_ERROR": 500,
    }
    return RentError(code, statuses.get(code, 500),
                     retryable=code in {"RETRYABLE_TRANSACTION", "COMMIT_RESULT_UNKNOWN"},
                     state_unknown=code == "COMMIT_RESULT_UNKNOWN", latest_versions=latest_versions)
