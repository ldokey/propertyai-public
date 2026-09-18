from datetime import datetime, timedelta

import pytest

from telegram_approval import cleaner_performance as perf


def policy(
    version="P1",
    *,
    threshold=None,
    premium=5000,
    penalty_enabled=True,
    penalty=10000,
    scores=None,
):
    return perf.PerformancePolicy(
        version=version,
        score_deltas=scores
        or {
            perf.EARLY_UNAVAILABLE: -2,
            perf.SAME_DAY_UNAVAILABLE: -10,
            perf.URGENT_ACCEPTED: 1,
            perf.URGENT_COMPLETED: 5,
        },
        same_day_penalty_enabled=penalty_enabled,
        same_day_penalty_default_krw=penalty if penalty_enabled else None,
        urgent_premium_krw=premium,
        replacement_urgent_lead_minutes=threshold,
    )


def event(store=None, *, kind=perf.SAME_DAY_UNAVAILABLE, pol=None, business="end-A"):
    pol = pol or policy()
    return perf.build_event(
        business_key=business,
        event_type=kind,
        cleaner_party_page_id="cleaner-A",
        cleaning_page_id="cleaning-A",
        assignment_page_id="assignment-A",
        assignment_version="version-A",
        occurred_at=datetime.fromisoformat("2026-09-10T00:10:00+09:00"),
        policy=pol,
        performance_classification=(kind if kind in {perf.EARLY_UNAVAILABLE, perf.SAME_DAY_UNAVAILABLE} else None),
        replacement_urgency=perf.URGENT,
        data_environment="TEST",
    )


def test_p3_classification_is_kst_calendar_boundary_not_rolling_hours():
    assert perf.classify_unavailable(
        cleaning_date="2026-09-10",
        occurred_at=datetime.fromisoformat("2026-09-09T23:59:00+09:00"),
    ) == perf.EARLY_UNAVAILABLE
    assert perf.classify_unavailable(
        cleaning_date="2026-09-10",
        occurred_at=datetime.fromisoformat("2026-09-10T00:00:00+09:00"),
    ) == perf.SAME_DAY_UNAVAILABLE


def test_p3_same_day_is_always_urgent_and_early_threshold_is_configurable():
    occurred = datetime.fromisoformat("2026-09-09T22:00:00+09:00")
    start = "2026-09-10T11:00:00+09:00"
    assert perf.resolve_replacement_urgency(
        classification=perf.SAME_DAY_UNAVAILABLE,
        occurred_at=datetime.fromisoformat("2026-09-10T00:01:00+09:00"),
        cleaning_start_at=start,
        policy=policy(threshold=None),
    ) == perf.URGENT
    assert perf.resolve_replacement_urgency(
        classification=perf.EARLY_UNAVAILABLE,
        occurred_at=occurred,
        cleaning_start_at=start,
        policy=policy(threshold=None),
    ) == perf.NORMAL
    assert perf.resolve_replacement_urgency(
        classification=perf.EARLY_UNAVAILABLE,
        occurred_at=occurred,
        cleaning_start_at=start,
        policy=policy(threshold=800),
    ) == perf.URGENT
    assert perf.resolve_replacement_urgency(
        classification=perf.EARLY_UNAVAILABLE,
        occurred_at=occurred,
        cleaning_start_at=start,
        policy=policy(threshold=700),
    ) == perf.NORMAL


def test_p3_urgent_offer_quote_and_legacy_normal_economics():
    urgent = perf.quote_replacement_offer(
        base_fee_krw=55000, replacement_urgency=perf.URGENT, policy=policy(premium=5000)
    )
    assert (urgent.base_fee_krw, urgent.urgent_premium_krw, urgent.total_agreed_fee_krw) == (
        55000,
        5000,
        60000,
    )
    assert urgent.urgent_premium_policy_version == "P1"
    normal = perf.canonical_offer_economics({"cleaning_fee_krw": 55000})
    assert normal.replacement_urgency == perf.NORMAL
    assert normal.total_agreed_fee_krw == 55000
    with pytest.raises(ValueError, match="requires frozen"):
        perf.canonical_offer_economics(
            {"cleaning_fee_krw": 55000, "replacement_urgency": perf.URGENT}
        )
    with pytest.raises(perf.PerformancePolicyError, match="requires a resolved"):
        perf.quote_replacement_offer(
            base_fee_krw=55000, replacement_urgency=perf.URGENT, policy=None
        )


def test_p3_one_business_fact_one_event_and_response_loss_converges():
    store = perf.InMemoryPerformanceEventStore()
    item = event()
    first = perf.append_performance_event(store=store, event=item)
    second = perf.append_performance_event(store=store, event=item)
    assert first["event_key"] == second["event_key"]
    assert len(store.events) == 1
    assert store.create_calls == 1

    response_loss = perf.InMemoryPerformanceEventStore()
    response_loss.create_then_fail = True
    recovered = perf.append_performance_event(store=response_loss, event=item)
    assert recovered["event_key"] == item["event_key"]
    assert len(response_loss.events) == 1


def test_p3_performance_event_identity_accepts_notion_minute_precision_readback():
    source = event()
    source["occurred_at"] = "2026-09-10T00:10:55.395012+09:00"
    persisted = dict(source)
    persisted["occurred_at"] = "2026-09-09T15:10:00.000+00:00"
    store = perf.InMemoryPerformanceEventStore()
    store.events[source["event_key"]] = persisted

    result = perf.append_performance_event(store=store, event=source)

    assert result["event_key"] == source["event_key"]
    assert store.create_calls == 0


def test_p3_performance_event_identity_rejects_adjacent_minute():
    source = event()
    source["occurred_at"] = "2026-09-10T00:10:55.395012+09:00"
    persisted = dict(source)
    persisted["occurred_at"] = "2026-09-09T15:11:00.000+00:00"
    store = perf.InMemoryPerformanceEventStore()
    store.events[source["event_key"]] = persisted

    with pytest.raises(perf.PerformanceEventError, match="conflicting business fact"):
        perf.append_performance_event(store=store, event=source)


def test_p3_performance_event_identity_rejects_malformed_timestamp():
    item = event()
    item["occurred_at"] = "not-a-timestamp"
    with pytest.raises(perf.PerformanceEventError, match="Occurred At identity malformed"):
        perf._immutable_event_identity(item)


def test_p3_failed_event_write_does_not_change_business_identity_and_retry_is_deterministic():
    store = perf.InMemoryPerformanceEventStore()
    item = event()
    store.fail_next_create = True
    with pytest.raises(perf.PerformanceEventError):
        perf.append_performance_event(store=store, event=item)
    assert store.events == {}
    assert item["event_key"] == perf.event_key(
        business_key="end-A", event_type=perf.SAME_DAY_UNAVAILABLE
    )
    result = perf.append_performance_event(store=store, event=item)
    assert result["event_key"] == item["event_key"]
    assert len(store.events) == 1


def test_p3_financial_review_state_authority_is_exactly_five_and_excludes_posted():
    expected = {
        perf.NOT_APPLICABLE,
        perf.PENDING_REVIEW,
        perf.APPROVED,
        perf.OVERRIDDEN,
        perf.WAIVED,
    }
    assert perf.FINANCIAL_REVIEW_STATES == expected
    assert "POSTED" not in perf.FINANCIAL_REVIEW_STATES
    assert not hasattr(perf, "POSTED")
    assert set(
        perf.PERFORMANCE_EVENT_SCHEMA["properties"]["Financial Review State"]["select"]
    ) == expected


def test_p3_same_day_penalty_is_review_candidate_and_never_posts_finance():
    store = perf.InMemoryPerformanceEventStore()
    item = perf.append_performance_event(store=store, event=event())
    assert item["financial_review_state"] == perf.PENDING_REVIEW
    assert item["default_financial_amount"] == 10000
    approved = perf.review_same_day_penalty(
        store=store,
        event_key_value=item["event_key"],
        decision=perf.APPROVED,
        operator="ops-A",
        action_key="review-A",
        action_version=1,
        applied_at=datetime.fromisoformat("2026-09-10T01:00:00+09:00"),
    )
    assert approved["financial_review_state"] == perf.APPROVED
    assert approved["applied_financial_amount"] == 10000
    assert approved["default_financial_amount"] == 10000
    assert store.update_calls == 1
    with pytest.raises(perf.PerformanceReviewError, match="out of Phase-3 scope"):
        perf.review_same_day_penalty(
            store=store,
            event_key_value=item["event_key"],
            decision="POSTED",
            operator="ops-A",
            action_key="post-A",
            action_version=2,
            applied_at=datetime.fromisoformat("2026-09-10T01:05:00+09:00"),
        )


def test_p3_penalty_override_and_waive_preserve_policy_default_and_audit():
    store = perf.InMemoryPerformanceEventStore()
    item = perf.append_performance_event(store=store, event=event(business="override-A"))
    overridden = perf.review_same_day_penalty(
        store=store,
        event_key_value=item["event_key"],
        decision=perf.OVERRIDDEN,
        operator="ops-A",
        action_key="review-A",
        action_version=1,
        applied_at=datetime.fromisoformat("2026-09-10T01:00:00+09:00"),
        applied_amount_krw=3000,
        reason="부분 감액",
    )
    assert overridden["default_financial_amount"] == 10000
    assert overridden["applied_financial_amount"] == 3000
    assert overridden["override"] is True
    assert overridden["override_reason"] == "부분 감액"

    waived_item = perf.append_performance_event(store=store, event=event(business="waive-A"))
    waived = perf.review_same_day_penalty(
        store=store,
        event_key_value=waived_item["event_key"],
        decision=perf.WAIVED,
        operator="ops-B",
        action_key="review-B",
        action_version=1,
        applied_at=datetime.fromisoformat("2026-09-10T01:10:00+09:00"),
        reason="운영상 면제",
    )
    assert waived["default_financial_amount"] == 10000
    assert waived["applied_financial_amount"] == 0
    assert waived["financial_review_state"] == perf.WAIVED


def test_p3_score_override_preserves_default_and_operator_version_idempotency():
    store = perf.InMemoryPerformanceEventStore()
    item = perf.append_performance_event(store=store, event=event(business="score-A"))
    assert item["default_score_delta"] == -10
    changed = perf.override_score(
        store=store,
        event_key_value=item["event_key"],
        applied_score_delta=-3,
        reason="상황 참작",
        operator="ops-A",
        action_key="score-1",
        action_version=1,
        applied_at=datetime.fromisoformat("2026-09-10T02:00:00+09:00"),
    )
    assert changed["default_score_delta"] == -10
    assert changed["applied_score_delta"] == -3
    retry = perf.override_score(
        store=store,
        event_key_value=item["event_key"],
        applied_score_delta=-3,
        reason="상황 참작",
        operator="ops-A",
        action_key="score-1",
        action_version=1,
        applied_at=datetime.fromisoformat("2026-09-10T02:01:00+09:00"),
    )
    assert retry["applied_score_delta"] == -3
    with pytest.raises(perf.PerformanceReviewError, match="newer version"):
        perf.override_score(
            store=store,
            event_key_value=item["event_key"],
            applied_score_delta=-2,
            reason="다른 결과",
            operator="ops-A",
            action_key="score-conflict",
            action_version=1,
            applied_at=datetime.fromisoformat("2026-09-10T02:02:00+09:00"),
        )
    with pytest.raises(perf.PerformanceReviewError, match="Override Reason"):
        perf.override_score(
            store=store,
            event_key_value=item["event_key"],
            applied_score_delta=0,
            reason="",
            operator="ops-A",
            action_key="score-2",
            action_version=2,
            applied_at=datetime.fromisoformat("2026-09-10T02:03:00+09:00"),
        )


def test_p3_policy_v1_snapshots_do_not_change_when_v2_becomes_current():
    v1 = policy("V1", premium=5000)
    v2 = policy("V2", premium=9000)
    original = event(pol=v1, business="policy-A")
    assert original["policy_version"] == "V1"
    assert original["default_score_delta"] == -10
    accepted = perf.quote_replacement_offer(
        base_fee_krw=55000, replacement_urgency=perf.URGENT, policy=v1
    )
    assert accepted.total_agreed_fee_krw == 60000
    assert accepted.urgent_premium_policy_version == "V1"
    assert v2.urgent_premium_krw == 9000
    assert original["policy_version"] == "V1"
    assert accepted.total_agreed_fee_krw == 60000


def test_p3_summary_is_derived_from_events_and_auto_enforcement_is_off():
    store = perf.InMemoryPerformanceEventStore()
    now = datetime.fromisoformat("2026-09-10T12:00:00+09:00")
    for idx, kind in enumerate(
        [perf.EARLY_UNAVAILABLE, perf.SAME_DAY_UNAVAILABLE, perf.URGENT_ACCEPTED, perf.URGENT_COMPLETED]
    ):
        item = perf.build_event(
            business_key=f"fact-{idx}",
            event_type=kind,
            cleaner_party_page_id="cleaner-A",
            cleaning_page_id=f"cleaning-{idx}",
            assignment_page_id=f"assignment-{idx}",
            assignment_version=f"version-{idx}",
            occurred_at=now - timedelta(days=idx),
            policy=policy(),
            performance_classification=(kind if "UNAVAILABLE" in kind else None),
            replacement_urgency=perf.URGENT,
            data_environment="TEST",
        )
        perf.append_performance_event(store=store, event=item)
    summary = perf.performance_summary(store=store, cleaner_party_page_id="cleaner-A", now=now)
    assert summary["recent_90_days"]["early_unavailable"] == 1
    assert summary["recent_90_days"]["same_day_unavailable"] == 1
    assert summary["recent_90_days"]["urgent_accepted"] == 1
    assert summary["recent_90_days"]["urgent_completed"] == 1
    assert summary["recent_90_days"]["completed"] == 1
    assert summary["recent_90_days"]["derived_score"] == -6
    rendered = perf.format_ops_performance_summary(cleaner_label="김OO", summary=summary)
    assert "최근 90일" in rendered and "긴급 대체 완료 1" in rendered
    assert perf.AUTO_TIER_MOVE is False
    assert perf.AUTO_ROLE_CHANGE is False
    assert perf.AUTO_SUSPEND is False
    assert perf.AUTO_BAN is False


def test_p3_schema_contract_is_exactly_one_new_performance_source_and_finite_policy_patch():
    assert perf.PERFORMANCE_EVENT_SCHEMA["name"] == "청소 인력 성과 이벤트 DB_cleaner_performance_event"
    assert "Event Key" in perf.PERFORMANCE_EVENT_SCHEMA["properties"]
    assert "Financial Review State" in perf.PERFORMANCE_EVENT_SCHEMA["properties"]
    assert set(perf.ASSIGNMENT_19_SCHEMA_PATCH) == {
        "Replacement Urgency",
        "Base Fee Snapshot",
        "Urgent Premium Snapshot",
        "Total Agreed Fee Snapshot",
        "Urgent Premium Policy Version",
    }
    assert "Urgent Premium KRW" in perf.POLICY_28_SCHEMA_PATCH
    assert "Replacement Urgent Lead Minutes" in perf.POLICY_28_SCHEMA_PATCH
