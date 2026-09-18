import json
from datetime import datetime

from telegram_approval import cleaner_performance as perf
from telegram_approval import cleaner_unavailable as unavailable


def policy(*, threshold=None, premium=5000):
    return perf.PerformancePolicy(
        version="P3-V1",
        score_deltas={
            perf.EARLY_UNAVAILABLE: -1,
            perf.SAME_DAY_UNAVAILABLE: -2,
            perf.URGENT_ACCEPTED: 1,
            perf.URGENT_COMPLETED: 2,
        },
        same_day_penalty_enabled=True,
        same_day_penalty_default_krw=10000,
        urgent_premium_krw=premium,
        replacement_urgent_lead_minutes=threshold,
    )


def action(transition_at):
    return {
        "transition_at": transition_at,
        "end_key": "assignment-end-key-A",
        "cleaner_party_page_id": "cleaner-A",
        "cleaning_page_id": "cleaning-A",
        "history_page_id": "history-A",
        "assignment_version": "assignment-A",
    }


def accepted():
    return {"start_at": "2026-09-10T11:00:00+09:00", "cleaning_fee_krw": 55000}


def test_same_day_unavailable_creates_urgent_event_and_penalty_candidate(tmp_path):
    record = action("2026-09-10T00:00:00+09:00")
    path = tmp_path / "same-day.json"
    events = perf.InMemoryPerformanceEventStore()
    economics = unavailable._apply_unavailable_performance(
        record,
        path=path,
        accepted=accepted(),
        performance_policy_store=perf.InMemoryPerformancePolicyStore([policy(threshold=None)]),
        performance_event_store=events,
    )
    assert economics.replacement_urgency == perf.URGENT
    assert economics.total_agreed_fee_krw == 60000
    assert record["performance_classification"] == perf.SAME_DAY_UNAVAILABLE
    assert record["replacement_urgency"] == perf.URGENT
    only = next(iter(events.events.values()))
    assert only["event_type"] == perf.SAME_DAY_UNAVAILABLE
    assert only["financial_review_state"] == perf.PENDING_REVIEW
    assert only["default_financial_amount"] == 10000


def test_early_unavailable_default_normal_but_configured_threshold_can_make_it_urgent(tmp_path):
    normal = action("2026-09-09T23:59:00+09:00")
    normal["end_key"] = "early-normal"
    normal_events = perf.InMemoryPerformanceEventStore()
    normal_economics = unavailable._apply_unavailable_performance(
        normal, path=tmp_path / "normal.json", accepted=accepted(),
        performance_policy_store=perf.InMemoryPerformancePolicyStore([policy(threshold=None)]),
        performance_event_store=normal_events,
    )
    assert normal["performance_classification"] == perf.EARLY_UNAVAILABLE
    assert normal["replacement_urgency"] == perf.NORMAL
    assert normal_economics.urgent_premium_krw == 0

    urgent = action("2026-09-09T23:59:00+09:00")
    urgent["end_key"] = "early-urgent"
    urgent_economics = unavailable._apply_unavailable_performance(
        urgent, path=tmp_path / "urgent.json", accepted=accepted(),
        performance_policy_store=perf.InMemoryPerformancePolicyStore([policy(threshold=700)]),
        performance_event_store=perf.InMemoryPerformanceEventStore(),
    )
    assert urgent["performance_classification"] == perf.EARLY_UNAVAILABLE
    assert urgent["replacement_urgency"] == perf.URGENT
    assert urgent_economics.urgent_premium_krw == 5000


def test_event_failure_marks_reconciliation_without_mutating_assignment_fact(tmp_path):
    record = action("2026-09-10T00:10:00+09:00")
    record.update({"status": unavailable.UNAVAILABLE_COMPLETE, "assignment_end_status": "ENDED"})
    path = tmp_path / "event-failure.json"
    events = perf.InMemoryPerformanceEventStore()
    events.fail_next_create = True
    unavailable._apply_unavailable_performance(
        record, path=path, accepted=accepted(),
        performance_policy_store=perf.InMemoryPerformancePolicyStore([policy()]),
        performance_event_store=events,
    )
    saved = json.loads(path.read_text())
    assert saved["status"] == unavailable.UNAVAILABLE_COMPLETE
    assert saved["assignment_end_status"] == "ENDED"
    assert saved["performance_reconciliation_state"] == perf.RECONCILIATION_REQUIRED
    failed_key = saved["performance_event_key"]
    unavailable._apply_unavailable_performance(
        record, path=path, accepted=accepted(),
        performance_policy_store=perf.InMemoryPerformancePolicyStore([policy()]),
        performance_event_store=events,
    )
    assert record["performance_event_key"] == failed_key
    assert record["performance_reconciliation_state"] == perf.APPLIED
    assert len(events.events) == 1


def test_policy_failure_keeps_same_day_objective_fact_and_blocks_falsely_priced_offer(tmp_path):
    class FailPolicy:
        def resolve_at(self, _at):
            raise perf.PerformancePolicyError("synthetic policy outage")

    record = action("2026-09-10T00:15:00+09:00")
    result = unavailable._apply_unavailable_performance(
        record, path=tmp_path / "policy.json", accepted=accepted(),
        performance_policy_store=FailPolicy(),
        performance_event_store=perf.InMemoryPerformanceEventStore(),
    )
    assert result is None
    assert record["performance_classification"] == perf.SAME_DAY_UNAVAILABLE
    assert record["replacement_urgency"] == perf.URGENT
    assert record["performance_reconciliation_state"] == perf.POLICY_RESOLUTION_REQUIRED
    assert "replacement_total_agreed_fee_krw" not in record
