# Cleaner Multi-Job DB Review — Validation Report

Status: **PASS_REVIEW_DB_CONTRACT / NOT_PRODUCTION_CUTOVER**

## Exact review environment

- Source base: `f5083587e73728fae4a847d18c2f55433277f087`
- Review branch: `p0-cleaner-multi-job-db-review`
- PostgreSQL: `18.6 (Homebrew)`
- Review database: `propertyai_review`
- Review-only socket/port: `/Users/kate/DKATE/propertyai-db-review/postgres18/socket:55432`
- No Production connection or deployment was used.

## Migration validation

All five migration files:

1. passed PostgreSQL parsing with `pglast`,
2. executed from an empty PostgreSQL 18.6 database with `ON_ERROR_STOP=1`.

During validation, two defects were found and fixed before this report:

1. A generated `tstzrange` conflict column was rejected because the expression did not satisfy generated-column immutability requirements. It was replaced with a DB `BEFORE INSERT/UPDATE` trigger that always derives and overwrites `conflict_window`.
2. PL/pgSQL guard locking used `SELECT 1 ... FOR UPDATE` without a result destination. It was corrected to `PERFORM 1 ... FOR UPDATE`.

A further logical hardening was added after review:

- acceptance determines which tier was actually open at `accepted_at` from immutable tier-opening audit rows, rather than trusting only the campaign's later `current_open_tier` value.
- Offer Campaign creation now fails before candidate distribution if per-Cleaning `required_work_minutes` is missing.

## Automated PostgreSQL behavioral result

`db/review/run_contract_tests.py`

```text
CONTRACT_TESTS_PASS=19
PASS WORK_DURATION_REQUIRED_BEFORE_OFFER_CAMPAIGN
PASS UNDECIDED_NOTIFICATION_AND_CAPACITY_VALUES_ARE_NOT_GUESSED
PASS TIER_EXPANSION_IS_CUMULATIVE_AND_SILENT
PASS CLOSED_TIER_CANNOT_ACCEPT
PASS CROSS_CLEANING_OVERLAP_BLOCKED_BOUNDARY_ALLOWED
PASS TRAVEL_BUFFER_PARTICIPATES_IN_CONFLICT
PASS REVOKED_ACCESS_BLOCKS_NEW_ACCEPTANCE
PASS POST_ACCEPT_REVOCATION_PRESERVES_HARD_BOOKING
PASS ACTIVE_TELEGRAM_IDENTITY_ONE_TO_ONE
PASS NULL_LIMIT_IS_UNRESTRICTED_CONFIGURED_DAILY_LIMIT_ENFORCED
PASS SCHEDULE_REVISION_SUPERSEDES_STALE_OPEN_OFFER
PASS SCHEDULE_REVISION_ACTIVATION_IS_IDEMPOTENT
PASS BOOKED_SCHEDULE_CHANGE_IS_FAIL_CLOSED_RECONCILIATION
PASS STALE_UI_OFFER_CANNOT_BYPASS_ACCEPTANCE_CUTOFF
PASS ORIGINAL_REASSIGNMENT_USES_HARD_BOOKING_AUTHORITY
PASS ORIGINAL_REASSIGNMENT_CANNOT_BYPASS_CROSS_CLEANING_CONFLICT
PASS REPLACEMENT_ACCEPTANCE_WINS_OVER_LATE_ORIGINAL_REASSIGN
PASS CONCURRENT_SAME_CLEANING_HAS_ONE_WINNER
PASS CONCURRENT_SAME_CLEANER_OVERLAP_HAS_ONE_WINNER
```

## What this PASS does and does not mean

PASS means the current review schema and DB commands execute and satisfy the tested scheduling invariants on an isolated PostgreSQL instance.

It does **not** mean:

- the schema is approved by external reviewers,
- all acceptance-matrix cases are automated,
- legacy Notion data is migrated,
- current Production uses this DB,
- notification/daily-limit/buffer values are decided,
- performance grouping/capping is decided,
- a Production cutover is authorized.

## Next validation gates after reviewer feedback

1. close material reviewer findings,
2. convert all mandatory acceptance-matrix cases for implemented slices to executable tests,
3. migration/backfill simulation from current Notion data,
4. shadow-read / dual-authority cutover rehearsal,
5. failure injection for worker leases/outbox/reconciliation,
6. controlled Telegram live validation only after DB and migration gates pass.
