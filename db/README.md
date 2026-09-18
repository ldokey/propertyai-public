# PropertyAI PostgreSQL DB Review — Cleaner Multi-Job Scheduling

Status: **DRAFT_REVIEW / NOT_PRODUCTION_CUTOVER**

Source base: `f5083587e73728fae4a847d18c2f55433277f087`
Review branch: `p0-cleaner-multi-job-db-review`
Project: `CHAT.PROJ.HQ`
Idempotency / review key: `CHAT.PROJ.HQ:SCHEMA:CLEANER_MULTI_JOB_SCHEDULING:V1`

## Purpose

This directory defines a language-neutral PostgreSQL contract for Cleaner scheduling before the legacy Python/Notion Production path is migrated. The application implementation may later be Python, Java/Spring, or another stack; the scheduling invariants are intentionally expressed in PostgreSQL schema, constraints, durable commands, and audit rows.

The migrations are additive review artifacts. They do **not** deploy to the current Production runtime and do not change current Notion, Telegram, Calendar, or Finance business data.

## Confirmed domain rules represented here

1. Tier expansion is cumulative. Opening Tier 2 does not revoke Tier 1's opportunity.
2. Tier expansion itself does not generate a Telegram notification.
3. Acceptance is authoritative only after fresh checks inside the DB transaction.
4. One Cleaner Party may have exactly one active Telegram identity.
5. Work duration is a per-Cleaning request/schedule value, not a Property default.
6. An Offer Campaign cannot open until `required_work_minutes` exists.
7. `service_window_start_at` / `service_deadline_at` are distinct from the actual Cleaner assignment slot.
8. One Cleaner cannot hold overlapping `HARD_BOOKED` assignment slots. Travel buffers, when configured, participate in conflict detection.
9. `max_daily_jobs`, `max_daily_work_minutes`, and default travel buffer are nullable. `NULL` means “policy not configured”, not zero.
10. Notification lead times are nullable and not seeded. Future reminders should be relative to the actual assignment start/end instead of hard-coded clock times.
11. Checkout/date changes create a new Cleaning schedule revision. Stale open campaigns are superseded. If a Cleaning is already hard-booked, the assignment is preserved and explicit reconciliation is created rather than silently moving it.
12. Original-Cleaner reassignment uses the same Cleaner schedule authority as a normal acceptance; it cannot bypass cross-Cleaning conflicts.
13. Performance score grouping/capping for multiple unavailable events is deliberately deferred.
14. Batch/outbox tables use durable idempotency, leases, retries, audit, and keyset-friendly ordering so processing is not tied to a hidden 100-row limit.

## Important terminology split

### Offer opportunity

A Cleaner can remain an eligible candidate even after another tier opens. A timer does not transfer ownership away from the earlier tier.

### Administrative Property access

`cleaner_property_access` is a separate authority. In this review draft, administrative `REVOKED` access blocks a **new** acceptance/reassignment but does not cancel an already `HARD_BOOKED` assignment. This is intentionally separated from tier expansion so reviewers can decide whether administrative revocation should have different semantics without changing the offer model.

## Migrations

- `V20260904.001__cleaner_scheduling_core.sql`
  - Property / Rental Unit / Party / Cleaner identity
  - Cleaner Property access
  - optional capacity policy and availability windows
  - Reservation
  - Cleaning and immutable schedule revisions
- `V20260904.002__cleaner_offer_and_assignment_authority.sql`
  - Offer Campaign and cumulative tier opening
  - candidate snapshots
  - actual assignment slots
  - DB-derived conflict ranges
  - one effective assignment per Cleaning
  - GiST exclusion against overlapping Cleaner hard bookings
  - Cleaner/ Cleaning scheduling guard rows
- `V20260904.003__business_scheduler_and_outbox.sql`
  - scheduler definitions and runs
  - durable scheduled actions
  - nullable relative notification policy
  - integration outbox, retry attempts, reconciliation state
  - append-only domain audit
- `V20260904.004__cleaner_scheduling_commands.sql`
  - cumulative tier opening
  - decline
  - fresh authoritative offer acceptance
  - schedule-revision activation / reconciliation
- `V20260904.005__cleaner_unavailable_and_reassignment.sql`
  - unavailable release
  - original-Cleaner reassignment request
  - original-Cleaner rebooking through the same scheduling authority
  - no performance score write in this slice

## Review DB validation

The migrations were parsed and executed on isolated PostgreSQL 18.6. The review cluster is not connected to Production.

Behavioral validation lives in:

`db/review/run_contract_tests.py`

Current verified result at the time of this review document:

`CONTRACT_TESTS_PASS=19`

See `db/review/validation_report.md` for exact evidence.

## Applying to a disposable DB

The files are Flyway-compatible versioned SQL names, but they can also be applied with `psql` in order. Production migration tooling is not decided by this review artifact.

```bash
for f in db/migration/*.sql; do
  psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$f"
done
```

Do not apply these migrations to the current Production database/runtime before review acceptance and an explicit migration/cutover plan.

## Reviewer documents

- `docs/architecture/P0_CLEANER_MULTI_JOB_DB_REVIEW.md`
- `db/review/acceptance_matrix.md`
- `db/review/validation_report.md`
