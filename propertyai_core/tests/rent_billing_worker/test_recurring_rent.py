from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

import pytest

from propertyai_core.application.recurring_rent import (
    BillingTarget, RecurringRentWorker, ScanCursor, WorkerConfig, cycle_command_key,
)
from propertyai_core.application.rent_errors import rent_error
from propertyai_core.rent_recurring_billing import run_recurring_billing_once

ORG = UUID('00000000-0000-0000-0000-000000000001')
TARGET = BillingTarget(ORG, UUID(int=2), UUID(int=3), 'READY', 'Asia/Seoul')
D7 = datetime(2026, 9, 3, 0, tzinfo=timezone.utc)


def receipt(outcome='CREATED'):
    return {'schema_version': 1, 'command_id': str(uuid4()), 'result': {'outcome': outcome}}


class FakePort:
    organization_id = ORG

    def __init__(self, targets=None):
        self.targets = [TARGET] if targets is None else targets
        self.receipts = {}
        self.issue_calls = []
        self.preview_calls = []
        self.reads = 0
        self.status = 'READY'

    def candidates(self, after, limit):
        self.reads += 1
        targets = self.targets
        if after:
            targets = [t for t in targets if (t.contract_id, t.period_id or UUID(int=0)) > (after.contract_id, after.period_id)]
        return targets[:limit]

    def lookup(self, key):
        self.reads += 1
        return self.receipts.get(key)

    def preview(self, target, instant):
        self.preview_calls.append((target, instant))
        return {'status': self.status, 'due_on': '2026-09-10'}

    def issue(self, target, key, instant):
        self.issue_calls.append((target, key, instant))
        self.receipts[key] = receipt()
        return self.receipts[key], False


def worker(port, instant=D7, **config):
    return RecurringRentWorker(port, lambda: instant, WorkerConfig(enabled=True, **config))


def test_default_off_is_no_io_and_manual_preview_does_not_enable_execution():
    port = FakePort()
    value = RecurringRentWorker(port, lambda: D7).run_once()
    assert value['result_class'] == 'DISABLED' and port.reads == 0
    assert value['created_count'] == 0
    preview = RecurringRentWorker(port, lambda: D7).run_once(dry_run=True)
    assert preview['result_class'] == 'DRY_RUN_SUCCESS'
    assert preview['would_create_count'] == 1 and not port.issue_calls and not port.receipts
    assert not preview['scheduler_enabled'] and not preview['automatic_scheduler_activated']
    value = run_recurring_billing_once(connect=lambda: pytest.fail('unexpected connection'),
                                      organization_id=ORG, clock=lambda: D7)
    assert value['result_class'] == 'DISABLED'


@pytest.mark.parametrize('config', [{'enabled': 'true'}, {'max_items': True}, {'max_items': 0},
                                   {'max_items': 501}, {'max_attempts': 4}])
def test_configuration_bounds(config):
    with pytest.raises(ValueError):
        WorkerConfig(**config)


def test_invalid_clock_cursor_and_production_binding_fail_closed():
    port = FakePort()
    assert worker(port, D7.replace(tzinfo=None)).run_once()['result_class'] == 'PROCESS_FAILED'
    assert worker(port).run_once(after=ScanCursor(uuid4(), uuid4(), uuid4()))['result_class'] == 'PROCESS_FAILED'
    assert worker(port).run_once(dry_run='yes')['result_class'] == 'PROCESS_FAILED'
    assert worker(port).run_once(run_id='sensitive-text')['result_class'] == 'PROCESS_FAILED'
    assert port.reads == 0
    with pytest.raises(ValueError):
        run_recurring_billing_once(connect=lambda: None, organization_id=ORG,
                                   clock=lambda: D7, runtime='PRODUCTION')


def test_d8_d7_missed_d6_and_duplicate_use_one_cycle_key():
    port = FakePort()
    assert worker(port, datetime(2026, 9, 2, tzinfo=timezone.utc)).run_once()['created_count'] == 0
    assert worker(port).run_once()['created_count'] == 1
    assert worker(port, datetime(2026, 9, 4, tzinfo=timezone.utc)).run_once()['created_count'] == 0
    assert len(port.issue_calls) == 1
    assert port.issue_calls[0][1] == cycle_command_key(TARGET)
    assert cycle_command_key(replace(TARGET, readiness=None)) == cycle_command_key(TARGET)
    assert cycle_command_key(replace(TARGET, organization_id=uuid4())) != cycle_command_key(TARGET)
    missed = FakePort()
    assert worker(missed, datetime(2026, 9, 4, tzinfo=timezone.utc)).run_once()['created_count'] == 1


@pytest.mark.parametrize('readiness', ['NEEDS_REVIEW', 'NOT_READY', 'UNKNOWN', 'UNCONFIRMED', None])
def test_only_positive_ready_can_reach_finance(readiness):
    port = FakePort([replace(TARGET, readiness=readiness), replace(TARGET, contract_id=UUID(int=4))])
    value = worker(port).run_once()
    assert value['created_count'] == 1 and value['skipped_count'] == 1
    assert value['dispositions'][0]['disposition'] == 'SKIPPED_NOT_READY'
    assert len(port.preview_calls) == 1


def test_invalid_contracts_are_bounded_and_do_not_poison_valid_contract():
    port = FakePort([replace(TARGET, period_id=None), replace(TARGET, timezone_name='Invalid/Zone'),
                     replace(TARGET, organization_id=uuid4()), TARGET])
    value = worker(port).run_once()
    assert (value['created_count'], value['skipped_count'], value['failed_count']) == (1, 2, 1)
    assert value['result_class'] == 'RUN_COMPLETED_WITH_CONTRACT_FAILURES'


@pytest.mark.parametrize('status,disposition', [('NEEDS_REVIEW', 'SKIPPED_NOT_READY'),
                                             ('UNKNOWN', 'SKIPPED_NOT_READY'),
                                             ('ALREADY_ISSUED', 'ALREADY_ISSUED'),
                                             ('NO_CHARGE', 'SKIPPED_NO_CHARGE')])
def test_preview_authority_dispositions(status, disposition):
    port = FakePort()
    port.status = status
    value = worker(port).run_once()
    assert value['dispositions'][0]['disposition'] == disposition
    assert not port.issue_calls


def test_zero_target_and_cursor_following_cover_every_target():
    assert worker(FakePort([])).run_once()['result_class'] == 'ZERO_TARGET_NOOP'
    port = FakePort([replace(TARGET, contract_id=UUID(int=n)) for n in range(10, 15)])
    scan = worker(port, max_items=2)
    result, cursor, total = None, None, 0
    for _ in range(3):
        result = scan.run_once(after=cursor)
        total += result['created_count']
        cursor = ScanCursor(**{k: UUID(v) for k, v in result['next_cursor'].items()}) if result['next_cursor'] else None
    assert total == 5 and result['scan_complete'] and cursor is None
    assert worker(port, max_items=5).run_once()['created_count'] == 0


def test_lost_response_is_recovered_from_durable_receipt_not_process_memory():
    port = FakePort()
    original = port.issue
    def lost(*args):
        original(*args)
        raise TimeoutError('DO_NOT_LOG_ACCOUNT_OR_SECRET')
    port.issue = lost
    value = worker(port).run_once()
    assert value['recovered_count'] == 1 and value['unknown_count'] == 0
    assert value['created_count'] == 0 and value['dispositions'][0]['disposition'] == 'ALREADY_ISSUED'
    assert worker(port).run_once()['created_count'] == 0 and len(port.issue_calls) == 1
    assert 'DO_NOT_LOG' not in json.dumps(value)


def test_unavailable_or_absent_receipt_after_dispatch_is_unknown_not_failed():
    for unavailable in (False, True):
        port = FakePort()
        def lost(*args):
            if unavailable:
                port.lookup = lambda key: (_ for _ in ()).throw(ConnectionError('secret'))
            raise rent_error('COMMIT_RESULT_UNKNOWN')
        port.issue = lost
        value = worker(port).run_once()
        assert value['result_class'] == 'PARTIAL_UNKNOWN_EFFECT'
        assert value['failed_count'] == 0 and value['unknown_count'] == 1
        assert 'secret' not in json.dumps(value)


def test_confirmed_conflict_retry_is_bounded_and_uses_same_key():
    port = FakePort()
    def conflict(target, key, instant):
        port.issue_calls.append(key)
        raise rent_error('VERSION_CONFLICT')
    port.issue = conflict
    value = worker(port).run_once()
    assert value['failed_count'] == 1 and value['unknown_count'] == 0
    assert port.issue_calls == [cycle_command_key(TARGET)] * 3
    assert value['dispositions'][0]['reason'] == 'BOUNDED_RETRY_EXHAUSTED'


def test_malformed_ack_is_unknown_and_pre_dispatch_failure_is_sanitized():
    port = FakePort()
    port.issue = lambda *args: ({'schema_version': 1, 'command_id': 'bad'}, False)
    assert worker(port).run_once()['unknown_count'] == 1
    port = FakePort()
    port.preview = lambda *args: (_ for _ in ()).throw(ValueError('protected-secret'))
    value = worker(port).run_once()
    assert value['failed_count'] == 1 and value['unknown_count'] == 0
    assert 'protected-secret' not in json.dumps(value)


def test_correlation_counts_and_authoritative_calendar_delegate(monkeypatch):
    import propertyai_core.domain.finance as finance
    original = finance.IssueRentEligibilityV1.evaluate
    calls = []
    def recorded(self):
        calls.append(self)
        return original(self)
    monkeypatch.setattr(finance.IssueRentEligibilityV1, 'evaluate', recorded)
    port = FakePort()
    correlation = uuid4()
    instant = datetime(2026, 9, 2, 15, tzinfo=timezone.utc)
    value = worker(port, instant).run_once(dry_run=True, run_id=correlation)
    assert value['run_id'] == value['request_id'] == str(correlation)
    assert value['started_at'] == value['ended_at'] == instant.isoformat()
    assert calls[0].effective_issue_date.isoformat() == '2026-09-03'
    assert value['targeted_count'] == value['would_create_count'] == value['evaluated_count'] == 1
    assert value['dispositions'][0]['targeting'] == 'TARGETED'
    assert value['evaluated_count'] == sum(value[k] for k in (
        'created_count', 'skipped_count', 'failed_count', 'unknown_count', 'would_create_count'))
    json.dumps(value)
