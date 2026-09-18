import json
from datetime import datetime
from pathlib import Path

from telegram_approval import bot_service
from telegram_approval import cleaner_performance as perf


def policy(version="PERF-V1"):
    return perf.PerformancePolicy(
        version=version,
        score_deltas={
            perf.EARLY_UNAVAILABLE: -1,
            perf.SAME_DAY_UNAVAILABLE: -2,
            perf.URGENT_ACCEPTED: 3,
            perf.URGENT_COMPLETED: 7,
        },
        same_day_penalty_enabled=True,
        same_day_penalty_default_krw=10000,
        urgent_premium_krw=5000,
        replacement_urgent_lead_minutes=360,
    )


def urgent_record():
    return {
        "schema_version": 2,
        "action_id": "urgent-action-A",
        "action_type": "CLEANING_ASSIGNMENT",
        "cleaning_page_id": "cleaning-A",
        "candidate_party_page_id": "cleaner-A",
        "replacement_urgency": "URGENT",
        "cleaning_fee_krw": 55000,
        "urgent_premium_krw": 5000,
        "total_agreed_fee_krw": 60000,
        "urgent_premium_policy_version": "OFFER-V1",
        "assignment_history_operation_identity": "CLEANING_ASSIGNMENT_ACCEPTED:v1:semantic-A",
        "assignment_history_page_id": "history-A",
        "assignment_version": "assignment-version-A",
        "executed_at": "2026-09-10T01:00:00+00:00",
        "test_mode": False,
    }


def test_urgent_accepted_event_is_exactly_once(tmp_path: Path):
    record = urgent_record()
    path = tmp_path / "accepted.json"
    path.write_text(json.dumps(record))
    events = perf.InMemoryPerformanceEventStore()
    policies = perf.InMemoryPerformancePolicyStore([policy()])

    first = bot_service._converge_urgent_accepted_event(
        record, path, policy_store=policies, event_store=events
    )
    second = bot_service._converge_urgent_accepted_event(
        record, path, policy_store=policies, event_store=events
    )

    assert first == perf.APPLIED
    assert second == perf.APPLIED
    assert len(events.events) == 1
    saved = json.loads(path.read_text())
    assert saved["performance_event_type"] == perf.URGENT_ACCEPTED
    assert saved["performance_event_status"] == perf.APPLIED
    only = next(iter(events.events.values()))
    assert only["assignment_page_id"] == "history-A"
    assert only["policy_version"] == "PERF-V1"
    assert only["default_score_delta"] == 3


def test_urgent_completed_event_failure_preserves_fact_and_retry_converges(tmp_path: Path):
    record = urgent_record()
    record.update({
        "action_id": "completion-submission-A",
        "action_type": "CLEANING_COMPLETION_SUBMISSION",
        "execution_result": {"cleaning_completion": "PHOTOS_SUBMITTED_REVIEW_REQUIRED"},
        "status": "EXECUTED",
        "consumed": True,
    })
    path = tmp_path / "completed.json"
    path.write_text(json.dumps(record))
    events = perf.InMemoryPerformanceEventStore()
    events.fail_next_create = True
    policies = perf.InMemoryPerformancePolicyStore([policy()])

    first = bot_service._converge_urgent_completed_event(
        record, path, policy_store=policies, event_store=events
    )
    assert first == perf.RECONCILIATION_REQUIRED
    after_failure = json.loads(path.read_text())
    assert after_failure["status"] == "EXECUTED"
    assert after_failure["execution_result"]["cleaning_completion"] == "PHOTOS_SUBMITTED_REVIEW_REQUIRED"
    failed_key = after_failure["performance_event_key"]

    second = bot_service._converge_urgent_completed_event(
        record, path, policy_store=policies, event_store=events
    )
    third = bot_service._converge_urgent_completed_event(
        record, path, policy_store=policies, event_store=events
    )
    assert second == perf.APPLIED
    assert third == perf.APPLIED
    assert len(events.events) == 1
    saved = json.loads(path.read_text())
    assert saved["performance_event_key"] == failed_key
    assert saved["performance_event_type"] == perf.URGENT_COMPLETED
    assert saved["performance_event_status"] == perf.APPLIED
    only = next(iter(events.events.values()))
    assert only["default_score_delta"] == 7


def test_urgent_event_policy_resolution_failure_is_reconciliation_only(tmp_path: Path):
    class FailingPolicyStore:
        def resolve_at(self, _occurred_at):
            raise perf.PerformancePolicyError("synthetic unavailable")

    record = urgent_record()
    path = tmp_path / "policy-fail.json"
    path.write_text(json.dumps(record))
    result = bot_service._converge_urgent_accepted_event(
        record,
        path,
        policy_store=FailingPolicyStore(),
        event_store=perf.InMemoryPerformanceEventStore(),
    )
    assert result == perf.POLICY_RESOLUTION_REQUIRED
    saved = json.loads(path.read_text())
    assert saved["performance_event_status"] == perf.POLICY_RESOLUTION_REQUIRED
    assert saved["assignment_history_page_id"] == "history-A"
