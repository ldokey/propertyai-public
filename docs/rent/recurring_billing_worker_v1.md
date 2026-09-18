# W3-A recurring rent worker — contract v1

Base: `9d85d8c84e1756e174cec1836a2719cdf7a6ab4c`, tree `1657a4c87171521f3ebfda5aba469b3d5e1a7cc5` (accepted I2). This is an isolated implementation bundle, not scheduler installation or Production adoption.

## Ownership

W3-A owns exactly these six new paths:

- `propertyai_core/application/recurring_rent.py`
- `propertyai_core/adapters/postgres/recurring_rent.py`
- `propertyai_core/rent_recurring_billing.py`
- `propertyai_core/tests/rent_billing_worker/test_recurring_rent.py`
- `propertyai_core/tests/rent_billing_worker/test_recurring_postgres.py`
- `docs/rent/recurring_billing_worker_v1.md`

All existing I2 paths are read-only, including Finance calculations/commands/handlers/repository, schema, dependencies, web/auth and test harness. W3-B packaging, backup, restore, health and tests are read-only. Shared composition, configuration, migration numbering and integration tests belong to I3. No new schema or shared source change is requested.

The entrypoint was placed outside `propertyai_core.runtime`: its existing package initializer imports the unrelated Production Writer boundary. That shared initializer was not changed.

## Entrypoint

```python
from propertyai_core.rent_recurring_billing import run_recurring_billing_once
from propertyai_core.application.recurring_rent import WorkerConfig, ScanCursor

result = run_recurring_billing_once(
    connect=fixture_owned_scheduler_connection_factory,
    organization_id=expected_organization_uuid,
    clock=trusted_aware_datetime_provider,
    config=WorkerConfig(),  # enabled=False, max_items=100, max_attempts=3
    dry_run=False,
    run_id=None,            # optional UUID correlation; generated if omitted
    after=None,             # optional ScanCursor
    runtime="ISOLATED_TEST",
)
```

The default returns `DISABLED` without opening a connection. An explicit dry-run is allowed while disabled; it does not enable actual execution. Tests explicitly use `WorkerConfig(enabled=True)`. No environment or DSN discovery, scheduler loop, background thread, cron, launchd, external provider or Production wiring exists. `runtime` accepts only `ISOLATED_TEST`. Invalid constructor configuration raises `ValueError`; run-time input/discovery failures produce `PROCESS_FAILED`.

The checked connection factory verifies the expected organization, original login, membership in `propertyai_rent_scheduler` only, nonprivileged role attributes, no additional role memberships and no forbidden write ACL. The accepted `rent_visible_org()` enforces the TEST/ACTIVE binding; schema constraints bind SCHEDULER to SYSTEM with no actor. No fallback to the operator, owner, migrator or Production login exists.

## Authority and recovery

Discovery reads existing confirmed billing period identities through organization-filtered views. It does not generate new billing periods or infer a monthly schedule from incomplete contracts. In particular, an existing EXPLICIT_PERIODS contract has no implied future periods: creating or confirming them remains a contract command/integration responsibility.

A candidate is one contract/cycle pair; a contract without any period yields one missing-period disposition. Only positive `READY` is evaluated for issuance. Invalid/missing readiness, timezone, conditions and periods cannot create receivables. A bad candidate does not stop unrelated candidates.

`IssueRentEligibilityV1.evaluate()` is the sole D-7 eligibility authority. `RentService.preview_charge()` is the calculation/readiness authority and `RentService.issue_from_scheduler()` is the only effectful command. No worker date subtraction, amount calculation, proration, rounding, allocation, refund, replacement or Finance table write exists. The run captures one aware instant; each property's timezone converts that instant to the existing Application local-date provider. The Application rechecks eligibility inside the command.

Stable command key:

```text
UUID5(NAMESPACE_URL,
      "propertyai:rent-recurring:v1:" + organization_uuid + ":" + contract_uuid + ":" + period_uuid)
```

All UUID strings use canonical lowercase hyphenated form. The key excludes process identity, run ID, tick date, retry count, ledger revision and current calculation hash. Receipt lookup precedes every attempt. This is necessary because rebuilding a completed command from a fresh preview changes its ledger revision and therefore its normalized payload hash.

The accepted SERIALIZABLE command, per-organization `rent_lock_scope` ledger-row lock, command receipt and unique active/root period indexes are reused without modification. No additional global lock, lease, migration, checkpoint table or in-memory success authority is introduced. Existing organization-level locking is broader than one contract but mandatory under the accepted Finance transaction; W3-A does not widen it.

After an exception from command dispatch, durable receipt lookup comes first. A receipt confirms completion even after lost acknowledgement. An absent/unavailable receipt following an ambiguous dispatch yields `UNKNOWN_EFFECT`, not a confirmed failure. A later independent run reconciles the same key. Confirmed rollback conflicts retry with a fresh preview at most three times; arbitrary ambiguous effects are not automatically redispatched within that run. Existing or manually voided historical issuance is never automatically replaced.

## Bounded scan contract for W3-B / I3

`max_items` is an integer in 1..500. Discovery fetches at most `max_items + 1` rows using a keyset ordered by `(contract_id, coalesce(period_id, zero_uuid))`. `ScanCursor` carries the organization and that pair. `max_attempts` is an integer in 1..3.

When `scan_complete=False`, pass the returned `next_cursor` values as UUIDs to `ScanCursor` and explicitly invoke the next page. The last page returns `scan_complete=True` and `next_cursor=None`. Ignoring a continuation can postpone candidates beyond the first page; a page success is never a claim that the entire organization was scanned. Each new periodic sweep starts with `after=None`, so newly inserted candidates before an old cursor are not permanently skipped. A restart may safely discard the cursor and scan again. Cursor persistence is an optimization, never a Finance effect authority.

W3-B may consume this API/result contract but may not modify W3-A source. I3 may compose page traversal with its separately authorized scheduling controls. This bundle does not perform I3 or activate any scheduling facility.

## Structured result

Result schema version 1 is JSON-serializable. It contains:

- `run_id`, `request_id` (same UUID), `started_at`, `ended_at` (aware ISO timestamps), `dry_run`.
- `scheduler_enabled` (explicit invocation configuration), `automatic_scheduler_activated=False`.
- `result_class`, `process_reason`, `scan_complete`, `next_cursor`.
- `evaluated_count`, `targeted_count`, `created_count`, `skipped_count`, `failed_count`, `unknown_count`, `would_create_count`, `recovered_count`.
- `dispositions`: at most `max_items` entries with `contract_id`, `period_id`, `disposition`, bounded enum `reason`, `targeting` (`TARGETED` / `NOT_TARGETED`) and `recovered`.

`evaluated_count` counts candidate contract/cycle pairs, not distinct contracts. It equals created + skipped + failed + unknown + would-create. Already-issued counts as skipped. `created_count` counts only newly created effects directly acknowledged to this invocation; recovered durable completion does not claim that this particular worker created it. Such recovery increments `recovered_count` and is `ALREADY_ISSUED`, avoiding double-counted creation in concurrent results.

Dispositions: `SKIPPED_NOT_READY`, `SKIPPED_NOT_DUE`, `SKIPPED_NO_CHARGE`, `ALREADY_ISSUED`, `WOULD_CREATE`, `CREATED`, `ERROR`, `UNKNOWN_EFFECT`. Eligible candidates have `targeting=TARGETED`.

Run classes: `DISABLED`, `PROCESS_FAILED`, `RUN_COMPLETED_WITH_CONTRACT_FAILURES`, `ZERO_TARGET_NOOP`, `DRY_RUN_SUCCESS`, `PARTIAL_UNKNOWN_EFFECT`, `RUN_COMPLETED`. Unknown effect takes priority over per-contract failures. No eligible targets is a successful no-op. Scan coverage is independently represented by `scan_complete`, including in no-op results. If the clock/reporting fails after effects, confirmed counts are retained instead of claiming rollback; a missing timestamp remains null.

No raw exception messages, account identifiers, source credentials, DSNs, resident names or contact data appear in this result. It is returned to its caller; this bundle does not install a logging sink.

## Verification and reproduction

Use an isolated Python 3.12 environment from the unchanged lock (`uv sync --frozen --offline` where cached). Tests are explicitly scoped:

```text
.venv/bin/python -m pytest -q \
  --confcutdir=propertyai_core/tests/rent_billing_worker \
  propertyai_core/tests/rent_billing_worker
```

The root legacy conftest unconditionally imports/mocks a different Production Writer. It is excluded for this self-contained worker suite, without editing or substituting any Finance/DB guard. No `real_global_writer` marker is applied. PostgreSQL tests directly consume `operator_fixture` and the accepted owned fixture lifecycle/provenance checks.

Required process-scoped PostgreSQL test bindings are `PROPERTYAI_RENT_TEST_BINDING_FILE`, `PROPERTYAI_RENT_TEST_BINDING_SHA256`, `PROPERTYAI_FLYWAY_BIN`, and `PROPERTYAI_RENT_TEST_EVIDENCE_ROOT`. Use the accepted P1 test binding and Flyway artifact; never a Production DSN. Each test fixture is Unix-socket-only and cleans up through the accepted lifecycle. The real-process restart test uses only the generated fixture DSN and deliberately terminates after commit before response, then restarts the disposable database and starts a fresh worker process.

Requirement-to-test mapping:

| Requirement | New test |
|---|---|
| Default OFF, no connection, dry-run without enabling | `test_default_off_is_no_io_and_manual_preview_does_not_enable_execution` |
| Bounds, input, no Production configuration | `test_configuration_bounds`, `test_invalid_clock_cursor_and_production_binding_fail_closed` |
| D-8, D-7, no credit allocation, dry-run no command receipt | `test_pg_empty_d8_d7_preview_and_no_credit_consumption` |
| Missed D-7 / D-6, duplicate, READY-only, multi-page recovery | `test_pg_missed_tick_ready_only_and_bounded_cursor_recovery` |
| UNKNOWN / UNCONFIRMED / invalid candidates, independent valid contract | `test_only_positive_ready_can_reach_finance`, `test_invalid_contracts_are_bounded_and_do_not_poison_valid_contract` |
| Month end, leap/nonleap February, timezone, frozen holiday due | `test_pg_month_end_leap_timezone_and_holiday_frozen_due_date` |
| Concurrent actual PostgreSQL commands / one effective receivable | `test_pg_concurrent_workers_one_effective_receivable_and_one_command` |
| Lost acknowledgement, ambiguous effect, durable retry | `test_pg_lost_ack_reconciliation_and_unknown_then_reentry` |
| Actual worker-process restart and durable DB restart | `test_pg_real_process_restart_and_durable_database_restart` |
| Narrow role, wrong organization, forbidden effects | `test_pg_minimum_privilege_binding_and_default_off` |
| Correlation, counts, authoritative D-7 delegate | `test_correlation_counts_and_authoritative_calendar_delegate` |
| Bounded rollback conflicts, malformed receipt and error redaction | `test_confirmed_conflict_retry_is_bounded_and_uses_same_key`, `test_malformed_ack_is_unknown_and_pre_dispatch_failure_is_sanitized` |

Unchanged I2/W2 and Finance suites remain reused evidence, not a request for full-suite reruns. Raw execution, cleanup, ownership audit and final checkpoint identity are external bundle evidence. No push, merge, deployment or Production effect is performed.
