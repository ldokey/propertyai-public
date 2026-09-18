# P0 Cleaner Multi-Job Scheduling & Assignment Authority — DB Review

**Review status:** DRAFT_REVIEW  
**Production status:** NOT_PRODUCTION_CUTOVER  
**Source Project Code:** CHAT.PROJ.HQ  
**Context:** CTX.HQ  
**Review key:** `CHAT.PROJ.HQ:SCHEMA:CLEANER_MULTI_JOB_SCHEDULING:V1`  
**Base source:** `f5083587e73728fae4a847d18c2f55433277f087`

## 1. Review objective

Before continuing controlled live Cleaner tests, freeze a database contract that can survive an application-language change and can be independently reviewed by operations, database/concurrency reviewers, and application/integration reviewers.

The primary defect class being addressed is not “one Cleaning has two winners”; the existing legacy flow already has strong same-Cleaning fencing. The missing authority is **one Cleaner participating in several Cleanings at the same time**, plus the cross-effects of tier expansion, stale offers, checkout changes, reassignment, batch jobs, and later policy changes.

## 2. Decisions already treated as requirements

### 2.1 Tier expansion is widening, not transfer

After a configured elapsed time (24 hours is the current business concept), opening Tier 2 means:

- Tier 2 becomes eligible.
- Tier 1 remains eligible.
- Tier 1 is not expired merely because Tier 2 opened.
- No message is sent solely to announce the tier opening.
- Whichever currently eligible Cleaner first completes an authoritative acceptance can win, subject to all fresh checks.

The database therefore models an `Offer Campaign` with `current_open_tier` and immutable `cleaning_offer_tier_opening` audit rows rather than a single ownership token that is handed from one Cleaner to another.

### 2.2 Service window is not the Cleaner’s exclusive work slot

The current legacy values such as 11:00–15:00 are best represented as:

- `service_window_start_at`
- `service_deadline_at`

They do **not** by themselves mean one Cleaner is occupied for all four hours.

Each Cleaning request/schedule revision additionally has:

- `required_work_minutes`

Each accepted Assignment has:

- `scheduled_start_at`
- `scheduled_end_at`
- `work_minutes_snapshot`
- optional travel buffer before / after

The actual assignment slot is what participates in cross-Cleaning conflict detection.

### 2.3 Cleaning duration is per request

There is intentionally no Property-level default Cleaning duration in this slice. The duration is entered when preparing the Cleaning request/schedule and must exist before the Offer Campaign opens. It then becomes an immutable snapshot on the accepted Assignment.

This supports future differences by actual job condition without incorrectly assuming every Cleaning of a Property has the same duration.

### 2.4 Capacity knobs exist before limits are activated

`cleaner_capacity_policy` contains nullable:

- `default_travel_buffer_minutes`
- `max_daily_work_minutes`
- `max_daily_jobs`

No values are seeded. `NULL` means no configured limit/buffer. The schema can therefore be used now without silently imposing a policy that has not been decided.

### 2.5 Relative notifications, not hard-coded time-of-day

`cleaning_notification_policy` contains nullable lead/lag fields for day-confirm, reconfirm, escalation, arrival, start, and completion reminder.

No lead values are currently seeded. The intended future shape is “X minutes before the actual assigned start” rather than a permanent 08:30/09:00/09:20/10:20 rule.

### 2.6 One active Telegram identity per Cleaner Party

The database physically prevents more than one active Telegram identity for one Party, and also prevents an active Telegram user/chat from belonging to multiple Parties.

This is not only UI validation; it is a DB invariant.

### 2.7 Checkout/date changes are first-class schedule revisions

Reservation checkout changes are expected normal events, not exceptional corruption.

The database therefore does not overwrite a Cleaning’s schedule in place. It creates `cleaning_schedule_revision` rows and changes `cleaning_job.current_schedule_revision_id`.

If the Cleaning has no accepted assignment:

1. new revision becomes current,
2. old open Offer Campaign becomes `SUPERSEDED`,
3. stale candidates/timers become non-actionable,
4. a new campaign may be created against the new revision.

If the Cleaning is already `HARD_BOOKED`:

1. the old assignment is **not silently moved or deleted**,
2. the new revision becomes current,
3. `cleaning_schedule_reconciliation` is created,
4. a durable `RECONCILE_SCHEDULE_REVISION` scheduled action is created,
5. operations/application logic must explicitly resolve the Cleaner’s new slot or release/re-offer.

### 2.8 Unavailable / original Cleaner reassignment uses the same authority

`record_cleaner_unavailable()` releases the old hard booking and records durable unavailable facts without writing the deferred performance score policy.

`reassign_original_cleaner()` must acquire the same Cleaner + Cleaning scheduling guards and re-check:

- current schedule revision,
- actual work slot,
- active identity,
- current administrative Property access,
- availability windows,
- optional daily capacity,
- all other `HARD_BOOKED` Cleaner assignments.

Therefore “다시 가능해졌어요” cannot bypass a Cleaning that the same Cleaner accepted in the meantime.

## 3. Authority layers

### 3.1 Stable identities

`property`, `rental_unit`, `party`, `external_identity`

External IDs such as Notion page IDs are bridge identifiers. The DB uses UUID primary keys and stable business codes.

### 3.2 Eligibility

`cleaner_profile`, `cleaner_property_access`, `cleaner_availability_window`

These answer whether a Cleaner is currently eligible to accept new work. Offer-tier opportunity is deliberately separate.

### 3.3 Cleaning time authority

`cleaning_job` + immutable `cleaning_schedule_revision`

A campaign/assignment is always bound to an exact schedule revision.

### 3.4 Offer discovery

`cleaning_offer_campaign`, `cleaning_offer_tier_opening`, `cleaning_offer_candidate`

Candidate rows are snapshots used for display/evaluation. They are **not** sufficient authority to accept. Acceptance always re-reads current state.

### 3.5 Effective schedule authority

`cleaning_assignment` where `assignment_status='HARD_BOOKED'`

This is the authoritative Cleaner schedule. `v_cleaner_effective_schedule` and `v_cleaner_daily_load` derive from it.

### 3.6 Scheduled work / integration delivery

`scheduler_job_definition`, `scheduler_job_run`, `business_scheduled_action`, `integration_outbox`, `integration_outbox_attempt`

A future Python, Java, or other worker may execute these rows. Business timing and idempotency remain in the database contract.

## 4. Concurrency design

### 4.1 Lock order

Every normal acceptance and original-Cleaner reassignment follows:

1. lock `cleaner_schedule_guard` for the Cleaner,
2. lock `cleaning_schedule_guard` for the Cleaning,
3. fresh-read all mutable authority,
4. validate,
5. insert one `HARD_BOOKED` assignment,
6. close/supersede related offer state.

This serializes different Cleanings competing for the same Cleaner.

### 4.2 Final database fences

Even if application pre-checks race, PostgreSQL has two final constraints:

- partial unique index: at most one `HARD_BOOKED` assignment for a Cleaning,
- GiST exclusion: the same Cleaner cannot have overlapping `conflict_window` ranges while `HARD_BOOKED`.

`conflict_window` is derived by a DB trigger from the work slot plus optional travel buffers, so the caller cannot supply a fake non-overlapping range.

## 5. Acceptance-time fresh check

Normal offer acceptance re-checks, inside the locks:

1. idempotency key,
2. candidate/campaign identity,
3. campaign still OPEN,
4. accepted timestamp is inside campaign lifetime/cutoff,
5. tier was actually open at the accepted timestamp,
6. Cleaning is assignable,
7. campaign revision equals current Cleaning revision,
8. per-request work duration exists,
9. slot is inside service window and exactly matches work duration,
10. Cleaner Party/profile currently active,
11. Property currently active,
12. exactly one active Telegram identity,
13. current administrative Property access,
14. optional unavailable windows,
15. optional daily job/work-minute limits,
16. other hard-booked conflict windows,
17. final DB uniqueness/exclusion constraint.

This allows old UI messages to remain visible without making them perpetual authority.

## 6. “권한 회수” terminology that reviewers must distinguish

The phrase can mean two different things and should not be collapsed into one flag.

### A. Offer opportunity / tier priority

This is **not revoked by elapsed time or tier expansion**. Tier 1 remains eligible after Tier 2 opens.

### B. Administrative/physical Property work access

The current review implementation treats `cleaner_property_access.status='REVOKED'` as a true administrative block on **new** acceptance/reassignment, while preserving an assignment that was already hard-booked before revocation.

This is a deliberate review point, not a hidden assumption. If the intended business meaning is that even an explicit administrative Property revocation should still permit a new acceptance, reviewers should reject this rule and define a separate security/operational authority field before Production cutover.

## 7. Batch and query design

The legacy runtime has bounded queries that can stop at 100 rows. This DB slice introduces durable batch concepts instead of assuming an entire workload fits in one query.

Recommended worker pattern:

- due items ordered by `(due_at, id)`,
- keyset pagination,
- small batches,
- row/lease claim,
- heartbeat,
- retry with `next_attempt_at`,
- dead-letter terminal state,
- unique idempotency key,
- external writes through outbox,
- reconciliation state when external success is uncertain.

Suggested future scheduler job codes include:

- `CLEANER_OFFER_TIER_EXPANSION`
- `CLEANER_OFFER_CUTOFF`
- `CLEANER_RELATIVE_REMINDER_BUILD`
- `CLEANER_SCHEDULE_RECONCILIATION`
- `INTEGRATION_OUTBOX_DELIVERY`
- `INTEGRATION_RECONCILIATION`

No actual schedule is enabled by these migrations.

## 8. Not included / deliberately deferred

- final application language/framework,
- Production cutover,
- migration of existing Notion Production records,
- final notification lead minutes,
- final travel buffer,
- final max daily jobs/minutes,
- performance score daily cap / one-incident grouping,
- Finance posting rules,
- generic C3 functionality,
- route/geospatial travel-time optimization.

## 9. Reviewer questions

### Reviewer A — Domain / Operations

Please challenge:

- Is cumulative Tier expansion exactly right?
- Should a Cleaner who explicitly declines remain declined for that campaign while other tiers continue opening?
- What is the correct acceptance cutoff for normal vs urgent replacement?
- Who chooses the actual scheduled slot when multiple positions fit inside the service window?
- Should an explicit administrative Property-access revocation block a new acceptance, or only remove future discovery visibility?
- How should a hard-booked Cleaner be handled if access/profile is revoked later?
- What should happen when checkout changes after assignment?
- Which relative notification lead times should be activated later?

**Feedback / decision:**

> [Reviewer A writes here]

### Reviewer B — DBA / Concurrency / Reliability

Please challenge:

- lock order and deadlock risk,
- GiST exclusion semantics and buffer range,
- transaction isolation assumptions,
- idempotency conflict behavior,
- schedule revision reconciliation,
- scheduler leasing and retry state,
- outbox/reconciliation correctness,
- indexes and keyset pagination for larger volumes,
- whether additional immutable event rows or constraints are required.

**Feedback / decision:**

> [Reviewer B writes here]

### Reviewer C — Application / Integration / Migration

Please challenge:

- whether DB command functions are the right stable boundary across Python/Java/other runtimes,
- legacy Notion → PostgreSQL migration strategy,
- Notion projection ownership after cutover,
- Telegram callback binding to exact candidate/campaign/revision,
- Calendar reschedule behavior on checkout changes,
- outbox delivery/reconciliation adapter boundaries,
- observability and operator UX for blocked/reconciliation cases,
- backward compatibility and phased rollout.

**Feedback / decision:**

> [Reviewer C writes here]

## 10. Promotion condition

This DB slice should not become Production authority until:

1. reviewer feedback is collected and material findings are closed,
2. open policy questions are explicitly decided,
3. the acceptance matrix is completed,
4. migration/backfill/readback strategy is tested,
5. dual-write/shadow-read or another bounded cutover plan is approved,
6. rollback conditions are defined,
7. controlled live validation is restarted only after those gates pass.
