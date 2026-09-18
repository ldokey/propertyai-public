"""Actual, owned, Unix-socket PostgreSQL tests; no Production DSNs or services."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import UUID, uuid4

import psycopg

from propertyai_core.adapters.postgres.recurring_rent import PostgresRecurringBilling
from propertyai_core.application.recurring_rent import RecurringRentWorker, ScanCursor, WorkerConfig
from propertyai_core.application.rent_errors import rent_error
from propertyai_core.rent_recurring_billing import run_recurring_billing_once
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import (
    create_account, create_contract, create_resident, entity, receipt, run,
)

D7 = datetime(2026, 9, 3, tzinfo=timezone.utc)
D6 = datetime(2026, 9, 4, tzinfo=timezone.utc)


def adapter(fixture, ids, *, login=None, organization_id=None):
    return PostgresRecurringBilling(
        lambda: psycopg.connect(fixture.cluster.login_dsn(login or ids['scheduler_login'])),
        organization_id=organization_id or ids['organization_id'],
    )


def tick(fixture, ids, instant=D7, *, port=None, dry_run=False, max_items=100, after=None):
    return RecurringRentWorker(port or adapter(fixture, ids), lambda: instant,
                               WorkerConfig(enabled=True, max_items=max_items)).run_once(dry_run=dry_run, after=after)


def revision(fixture, ids):
    return str(fixture.query('SELECT revision FROM propertyai.finance_ledger_scope WHERE organization_id=%s',
                             (ids['organization_id'],))[0][0])


def seed(fixture, service, ids, **kwargs):
    resident, rev, _ = create_resident(service, revision(fixture, ids))
    return create_contract(service, ids, [resident], rev, due_month=kwargs.pop('due_month', '2026-09'),
                           due_day=kwargs.pop('due_day', 10), **kwargs)


def effects(fixture):
    # Aggregate only synthetic fixture facts. Worker must not change these.
    return fixture.query('''SELECT
      (SELECT count(*) FROM propertyai.finance_allocation),
      (SELECT count(*) FROM propertyai.finance_source_return),
      (SELECT count(*) FROM propertyai.finance_movement),
      (SELECT count(*) FROM propertyai.finance_movement_revision),
      (SELECT count(*) FROM propertyai.finance_funding_source),
      (SELECT count(*) FROM propertyai.finance_receivable_adjustment),
      (SELECT count(*) FROM propertyai.rent_contract_history),
      (SELECT count(*) FROM propertyai.rent_occupancy),
      (SELECT count(*) FROM propertyai.rent_contract),
      (SELECT count(*) FROM propertyai.rent_term_revision),
      (SELECT count(*) FROM propertyai.rent_billing_period)''')[0]


def count_receivables(fixture):
    return fixture.query('SELECT count(*) FROM propertyai.finance_receivable')[0][0]


def test_pg_empty_d8_d7_preview_and_no_credit_consumption():
    with operator_fixture(with_scheduler=True) as (fixture, service, ids):
        zero = tick(fixture, ids)
        assert zero['result_class'] == 'ZERO_TARGET_NOOP' and zero['evaluated_count'] == 0
        contract = seed(fixture, service, ids)
        account, rev, _ = create_account(service, revision(fixture, ids))
        source, _ = receipt(service, account, rev, ids, contract['contract_id'], amount='250000')
        before = effects(fixture)
        ledger = revision(fixture, ids)
        receipts = fixture.query('SELECT count(*) FROM propertyai.command_receipt')[0][0]
        d8 = tick(fixture, ids, datetime(2026, 9, 2, tzinfo=timezone.utc))
        assert d8['dispositions'][0]['disposition'] == 'SKIPPED_NOT_DUE'
        preview = tick(fixture, ids, dry_run=True)
        assert preview['result_class'] == 'DRY_RUN_SUCCESS' and preview['would_create_count'] == 1
        assert count_receivables(fixture) == 0 and revision(fixture, ids) == ledger
        assert fixture.query('SELECT count(*) FROM propertyai.command_receipt')[0][0] == receipts
        result = tick(fixture, ids)
        assert result['created_count'] == 1 and result['failed_count'] == result['unknown_count'] == 0
        assert tick(fixture, ids, D6)['created_count'] == 0
        assert count_receivables(fixture) == 1 and effects(fixture) == before
        available = next(r for r in service.funding_sources()['rows'] if r['source_id'] == source)
        assert available['available'] == '250000'
        assert fixture.query('SELECT original_amount,due_on FROM propertyai.finance_receivable')[0][0] == 1000000
        fixture.record('W3_A_D8_D7_PREVIEW_CREDIT', result=result, preview=preview, forbidden_effects=0)


def test_pg_missed_tick_ready_only_and_bounded_cursor_recovery():
    with operator_fixture(with_scheduler=True) as (fixture, service, ids):
        contracts = [seed(fixture, service, ids) for _ in range(4)]
        unconfirmed = run(service, 'createContract', {
            'rental_unit_id': str(ids['unit_id']), 'starts_on': None, 'ends_on_exclusive': None,
            'lifecycle': 'DRAFT', 'readiness': 'NEEDS_REVIEW', 'resident_ids': [], 'contract_parties': [],
            'term': None, 'billing_periods': [], 'previous_contract_id': None,
            'reason': 'Synthetic unconfirmed contract', 'expected_ledger_revision': revision(fixture, ids),
            'occupancies': [],
        })
        invalid_id = entity(unconfirmed, 'CONTRACT')['id']
        cursor, results = None, []
        for _ in range(3):
            result = tick(fixture, ids, D6, max_items=2, after=cursor)
            results.append(result)
            raw = result['next_cursor']
            cursor = ScanCursor(**{k: UUID(v) for k, v in raw.items()}) if raw else None
        assert sum(r['created_count'] for r in results) == 4
        assert all(r['failed_count'] == r['unknown_count'] == 0 for r in results)
        assert results[-1]['scan_complete'] and cursor is None
        dispositions = [d for r in results for d in r['dispositions']]
        assert next(d for d in dispositions if d['contract_id'] == invalid_id)['disposition'] == 'SKIPPED_NOT_READY'
        assert tick(fixture, ids, D6)['created_count'] == 0
        assert count_receivables(fixture) == len(contracts)
        assert fixture.query('SELECT count(*) FROM propertyai.finance_receivable WHERE contract_id=%s',
                             (UUID(invalid_id),))[0][0] == 0
        fixture.record('W3_A_BOUNDED_CATCHUP_READY_ONLY', pages=results, unconfirmed_auto_bill=0)


def test_pg_month_end_leap_timezone_and_holiday_frozen_due_date():
    cases = [
        # first tick is one second before property-local D-7, then exactly D-7.
        ('2024-02-01', '2024-03-01', '2024-02', 31, '2024-02-29', '2024-02-21T14:59:59+00:00', '2024-02-21T15:00:00+00:00'),
        ('2025-02-01', '2025-03-01', '2025-02', 31, '2025-02-28', '2025-02-20T14:59:59+00:00', '2025-02-20T15:00:00+00:00'),
        ('2026-04-01', '2026-05-01', '2026-04', 31, '2026-04-30', '2026-04-22T14:59:59+00:00', '2026-04-22T15:00:00+00:00'),
        ('2026-09-01', '2026-10-01', '2026-09', 10, '2026-09-10', '2026-09-02T14:59:59+00:00', '2026-09-02T15:00:00+00:00'),
        ('2026-01-01', '2026-02-01', '2026-01', 1, '2026-01-01', '2025-12-24T14:59:59+00:00', '2025-12-24T15:00:00+00:00'),
    ]
    with operator_fixture(with_scheduler=True) as (fixture, service, ids):
        for start, end, month, day, expected_due, before, eligible in cases:
            contract = seed(fixture, service, ids, start=start, end=end, due_month=month, due_day=day)
            preview = service.preview_charge({'contract_id': contract['contract_id'], 'period_id': contract['period_id']})
            assert preview['due_on'] == expected_due
            before_result = tick(fixture, ids, datetime.fromisoformat(before))
            disposition = next(d for d in before_result['dispositions'] if d['contract_id'] == contract['contract_id'])
            assert disposition['disposition'] == 'SKIPPED_NOT_DUE'
            result = tick(fixture, ids, datetime.fromisoformat(eligible))
            assert result['created_count'] == 1 and result['failed_count'] == result['unknown_count'] == 0
            due, amount = fixture.query('SELECT due_on,original_amount FROM propertyai.finance_receivable WHERE period_id=%s',
                                       (UUID(contract['period_id']),))[0]
            assert due.isoformat() == expected_due and str(amount) == preview['amount']
        assert count_receivables(fixture) == len(cases)
        fixture.record('W3_A_CALENDAR', cases=len(cases), month_end=True, leap_year=True,
                       timezone_boundary=True, contractual_holiday_due_unchanged=True)


def test_pg_concurrent_workers_one_effective_receivable_and_one_command():
    with operator_fixture(with_scheduler=True) as (fixture, service, ids):
        contract = seed(fixture, service, ids)
        barrier = Barrier(2)
        ports = [adapter(fixture, ids), adapter(fixture, ids)]
        for port in ports:
            original = port.issue
            first = [True]
            def synchronized(target, key, instant, original=original, first=first):
                if first[0]:
                    first[0] = False
                    barrier.wait(timeout=15)
                return original(target, key, instant)
            port.issue = synchronized
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(tick, fixture, ids, D7, port=p) for p in ports]
            results = [f.result(timeout=35) for f in futures]
        assert sum(r['created_count'] for r in results) == 1
        assert all(r['failed_count'] == r['unknown_count'] == 0 for r in results)
        assert count_receivables(fixture) == 1
        assert fixture.query("SELECT count(*) FROM propertyai.finance_command WHERE command_type='ISSUE_RENT'")[0][0] == 1
        fixture.record('W3_A_CONCURRENT', results=results, effective_receivables=1, issue_commands=1)


def test_pg_lost_ack_reconciliation_and_unknown_then_reentry():
    with operator_fixture(with_scheduler=True) as (fixture, service, ids):
        seed(fixture, service, ids)
        port = adapter(fixture, ids)
        original = port.issue
        def lost(*args):
            original(*args)
            raise TimeoutError('synthetic lost response')
        port.issue = lost
        result = tick(fixture, ids, port=port)
        assert result['recovered_count'] == 1 and result['unknown_count'] == 0
        assert tick(fixture, ids)['created_count'] == 0 and count_receivables(fixture) == 1
        second = seed(fixture, service, ids)
        port = adapter(fixture, ids)
        original = port.issue
        def unavailable(key):
            raise ConnectionError('synthetic receipt transport outage')
        def committed_then_unavailable(*args):
            original(*args)
            port.lookup = unavailable
            raise rent_error('COMMIT_RESULT_UNKNOWN')
        port.issue = committed_then_unavailable
        unknown = tick(fixture, ids, port=port)
        assert unknown['result_class'] == 'PARTIAL_UNKNOWN_EFFECT' and unknown['unknown_count'] == 1
        assert next(d for d in unknown['dispositions'] if d['contract_id'] == second['contract_id'])['disposition'] == 'UNKNOWN_EFFECT'
        retried = tick(fixture, ids)
        assert retried['created_count'] == retried['unknown_count'] == retried['failed_count'] == 0
        assert count_receivables(fixture) == 2
        fixture.record('W3_A_LOST_RESPONSE', recovered=result, unknown=unknown, reentry=retried)


_CHILD = '''
import json, os
from datetime import datetime
from uuid import UUID
import psycopg
from propertyai_core.adapters.postgres.recurring_rent import PostgresRecurringBilling
from propertyai_core.application.recurring_rent import RecurringRentWorker, WorkerConfig
adapter = PostgresRecurringBilling(lambda: psycopg.connect(os.environ['W3A_FIXTURE_DSN']),
                                  organization_id=UUID(os.environ['W3A_FIXTURE_ORG']))
if os.environ['W3A_LOSE_RESPONSE'] == 'YES':
    original = adapter.issue
    def terminate_after_commit(*args):
        original(*args)
        os._exit(23)
    adapter.issue = terminate_after_commit
worker = RecurringRentWorker(adapter, lambda: datetime.fromisoformat('2026-09-03T00:00:00+00:00'),
                             WorkerConfig(enabled=True))
print(json.dumps(worker.run_once()))
'''


def test_pg_real_process_restart_and_durable_database_restart():
    with operator_fixture(with_scheduler=True, durability=True) as (fixture, service, ids):
        seed(fixture, service, ids)
        env = {**os.environ, 'W3A_FIXTURE_DSN': fixture.cluster.login_dsn(ids['scheduler_login']),
               'W3A_FIXTURE_ORG': str(ids['organization_id']), 'W3A_LOSE_RESPONSE': 'YES'}
        root = Path(__file__).resolve().parents[3]
        first = subprocess.run([sys.executable, '-c', _CHILD], cwd=root, env=env,
                               capture_output=True, text=True, timeout=40)
        assert first.returncode == 23 and first.stdout == '', first.stderr
        assert count_receivables(fixture) == 1
        fixture.restart()
        env['W3A_LOSE_RESPONSE'] = 'NO'
        second = subprocess.run([sys.executable, '-c', _CHILD], cwd=root, env=env,
                                capture_output=True, text=True, timeout=40)
        assert second.returncode == 0, second.stderr
        result = json.loads(second.stdout)
        assert result['created_count'] == result['failed_count'] == result['unknown_count'] == 0
        assert result['dispositions'][0]['disposition'] == 'ALREADY_ISSUED'
        assert count_receivables(fixture) == 1
        assert fixture.query("SELECT count(*) FROM propertyai.finance_command WHERE command_type='ISSUE_RENT'")[0][0] == 1
        fixture.record('W3_A_PROCESS_AND_DATABASE_RESTART', first_process_exit=23, reentry=result,
                       effective_receivables=1, confirmed_durable=True)
    assert fixture.cleanup_report['classification'] == 'PASS' and fixture.cleanup_report['root_absent']


def test_pg_minimum_privilege_binding_and_default_off():
    with operator_fixture(with_scheduler=True) as (fixture, service, ids):
        seed(fixture, service, ids)
        before = effects(fixture)
        for denied in (adapter(fixture, ids, login=ids['login']), adapter(fixture, ids, organization_id=uuid4())):
            result = tick(fixture, ids, port=denied)
            assert result['result_class'] == 'PROCESS_FAILED' and result['created_count'] == 0
        disabled = run_recurring_billing_once(
            connect=lambda: (_ for _ in ()).throw(AssertionError('Disabled must not connect')),
            organization_id=ids['organization_id'], clock=lambda: D7,
        )
        assert disabled['result_class'] == 'DISABLED'
        with psycopg.connect(fixture.cluster.login_dsn(ids['scheduler_login']), autocommit=True) as conn:
            assert conn.execute("SELECT has_table_privilege(current_user,'propertyai.finance_allocation','INSERT'), has_table_privilege(current_user,'propertyai.finance_source_return','INSERT'), has_table_privilege(current_user,'propertyai.rent_contract','UPDATE')").fetchone() == (False, False, False)
        assert count_receivables(fixture) == 0 and effects(fixture) == before
        fixture.record('W3_A_MINIMUM_PRIVILEGE', broad_login_rejected=True, wrong_org_rejected=True,
                       default_off=True, automatic_activation=False, forbidden_effects=0)
