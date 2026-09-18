# PropertyAI Cleaner Multi-Job DB V2.2.1 — Logical Schema Draft

Status: `DRAFT_FOR_FOCUSED_REREVIEW`

Source base:
- Production source base: `f5083587e73728fae4a847d18c2f55433277f087`
- Rejected-as-final V1 review commit: `93c5f428c33ba331b418aff43bd702177691ffc5`
- V1 independent review result: `REWORK_RECOMMENDED`
- Project: `CHAT.PROJ.HQ`
- Context: `CTX.HQ`

This document defines the V2.2.1 logical database contract only.
It is not Production DDL, not a migration, and not a behavioral/live-test plan.

---

## 1. Architectural boundary

### 1.1 Backend is the business-logic authority

Business logic is implemented in the backend application layer, whether the runtime is Java/Spring, Python, or another language.

Backend responsibilities include:

- Cleaner candidate discovery and ranking
- Tier/priority policy evaluation
- Offer creation orchestration
- Accept/decline workflow
- checkout-change orchestration
- replacement selection
- original-Cleaner reassignment workflow
- notification policy and message generation
- Telegram callback interpretation
- operator/manual-override authorization
- state-transition orchestration
- transaction boundary ownership

The DB is not a workflow engine.

### 1.2 PostgreSQL is the durable ledger and invariant fence

PostgreSQL responsibilities include:

- durable state
- FK / composite FK
- UNIQUE / partial UNIQUE
- CHECK / NOT NULL
- append-only boundaries where required
- optimistic version/CAS fields
- row locks
- DB-clock authority for cutoff/freshness decisions
- exclusion constraints for cross-Cleaning overlap
- atomic commit of domain mutation + durable async intent
- stale-writer fencing
- queue lease fencing

### 1.3 Narrow DB primitives are allowed only for hard invariants

A small DB function/helper is allowed only where a correctness invariant cannot be expressed safely by declarative constraints alone.

Examples:

- immutable busy-window derivation helper, if required by generated-column implementation
- narrow queue claim/lease primitive, if application SQL would otherwise duplicate fragile fencing logic

Not allowed:

- giant stored procedures implementing the whole Cleaner workflow
- candidate-ranking logic in SQL procedures
- Telegram UX/message logic in DB
- policy orchestration hidden inside triggers
- event-sourcing/CQRS as the primary state model

### 1.4 Working principle

```text
Backend transaction service = workflow authority
PostgreSQL                 = durable ledger + concurrency/invariant fence
```

---

## 2. V2.2.1 target size and simplification

V1 review draft: 29 tables.

V2.2.1 target: 24 core tables.

The increase from 22 to 24 is deliberate multi-host readiness: one operational Organization can contain multiple host/operator Members, while each Property has exactly one operational Organization authority.

Removed from V1 core:

- `cleaner_schedule_guard`
- `cleaning_schedule_guard`
- `cleaner_capacity_policy`
- `cleaning_offer_tier_opening`
- `cleaning_assignment_event`
- `scheduler_job_definition`
- `scheduler_job_run`
- `integration_outbox_attempt`
- versioned `cleaning_notification_policy`
- candidate states `INELIGIBLE`, `SUPERSEDED`
- candidate-wide `SUPERSEDED` mass update
- caller-provided authoritative mutation timestamps
- arbitrary historical revision activation
- Assignment service-window snapshots duplicated from immutable revision

Added or strengthened:

- Candidate `proposal_version`
- Candidate buffer snapshots + provenance
- Reconciliation `case_version` CAS
- central `command_receipt`
- transport stream/event binding
- `authority_epoch` with shared-lock fencing
- `schedule_source_type`
- `source_reservation_version`
- expired-RUNNING queue reclaim
- lease fencing token
- resource-binding uniqueness with nullable key semantics
- explicit backend/DB responsibility boundary

---

## 3. Organization / Property / identity / eligibility

### 3.1 `organization`

Purpose: operational tenant/host-group boundary.

A Property belongs to one operational Organization. Multiple host/operator people participate through memberships. This avoids tying a Property directly to one human owner while keeping the operational authority model simple.

Columns:

- `organization_id uuid PK`
- `organization_code text UNIQUE NOT NULL`
- `display_name text NOT NULL`
- `organization_status text NOT NULL` (`ACTIVE`, `SUSPENDED`, `CLOSED`)
- `data_environment text NOT NULL` (`PRODUCTION`, `TEST`)
- timestamps

Semantics:

- one Organization may manage many Properties
- one Property has exactly one operational Organization in this slice
- legal/economic co-ownership percentages are out of scope; this is the system operating authority boundary
- if a future Property genuinely needs multiple independent organizations with equal operational authority, add an explicit many-to-many model then; do not complicate the current core now

### 3.2 `organization_member`

Purpose: multiple hosts/operators participating in one Organization.

Columns:

- `organization_member_id uuid PK`
- `organization_id uuid NOT NULL FK organization`
- `party_id uuid NOT NULL FK party`
- `membership_role text NOT NULL` (`OWNER`, `ADMIN`, `OPERATOR`, `VIEWER`)
- `membership_status text NOT NULL` (`ACTIVE`, `INVITED`, `REMOVED`)
- `joined_at timestamptz NULL`
- `removed_at timestamptz NULL`
- timestamps

Constraints:

- one current membership per `(organization_id, party_id)`
- ACTIVE membership requires non-null `joined_at`
- REMOVED membership requires non-null `removed_at`

Authorization:

- exact application permissions remain backend authorization logic
- `membership_role` is coarse organizational authority, not a DB grant/role
- Cleaner status and Organization membership are independent; one Party may theoretically be both if product policy ever allows it

### 3.3 `property`

Purpose: stable Property identity and timezone authority.

Columns:

- `property_id uuid PK`
- `organization_id uuid NOT NULL FK organization`
- `property_code text UNIQUE NOT NULL`
- `display_name text NOT NULL`
- `timezone_name text NOT NULL`
- `active boolean NOT NULL DEFAULT true`
- `created_at timestamptz NOT NULL`
- `updated_at timestamptz NOT NULL`

Notes:

- every Property belongs to exactly one operational Organization
- Notion/Calendar IDs are not stored directly here.
- External projection IDs live in `integration_resource_binding`.
- `timezone_name` is later used for human-local rendering and, only when enabled, daily-capacity attribution.

### 3.4 `rental_unit`

Columns:

- `rental_unit_id uuid PK`
- `rental_unit_code text UNIQUE NOT NULL`
- `property_id uuid NOT NULL FK property`
- `display_name text NOT NULL`
- `active boolean NOT NULL DEFAULT true`
- timestamps

Constraints:

- `UNIQUE(property_id, rental_unit_id)`

### 3.5 `party`

Columns:

- `party_id uuid PK`
- `party_code text UNIQUE NOT NULL`
- `display_name text NOT NULL`
- `data_environment text NOT NULL` (`PRODUCTION`, `TEST`)
- `active boolean NOT NULL DEFAULT true`
- timestamps

### 3.6 `cleaner_profile`

Purpose: current Cleaner operational state plus optional future capacity limits.

Columns:

- `cleaner_party_id uuid PK/FK party`
- `operational_status text NOT NULL` (`ACTIVE`, `PAUSED`, `INACTIVE`)
- `max_daily_work_minutes integer NULL`
- `max_daily_jobs integer NULL`
- timestamps

Semantics:

- `NULL` means policy not configured, not zero.
- No versioned capacity-policy table in V2.2.1.
- Default travel buffer is intentionally not stored here until a real routing/buffer policy exists.
- History is written to `domain_event` when operationally useful.

Lifecycle invariant:

- a Cleaner transitioned to `ACTIVE` must have exactly one active Telegram identity
- this minimum-cardinality rule is enforced by the backend lifecycle transaction plus DB uniqueness constraints that enforce the maximum-cardinality side

### 3.7 `external_identity`

Purpose: Party-to-provider identity binding.

Columns:

- `external_identity_id uuid PK`
- `party_id uuid NOT NULL FK party`
- `provider text NOT NULL` (initially `TELEGRAM`)
- `provider_user_id text NOT NULL`
- `provider_chat_id text NULL`
- `bound_at timestamptz NOT NULL`
- `revoked_at timestamptz NULL`
- `source_ref text NULL`
- `created_at timestamptz NOT NULL`

Critical indexes:

- active `(provider, provider_user_id)` globally unique
- active `(provider, provider_chat_id)` globally unique when chat ID exists
- one active Telegram identity per Party
- `UNIQUE(external_identity_id, party_id)` for actor-binding composite FK

Identity rotation/revoke must use the Cleaner aggregate lock if it can affect a live acceptance/reassignment decision.

### 3.8 `cleaner_property_roster`

Purpose: current Cleaner eligibility/ranking for a Property.

This table is not physical-security/door-code access authority.

Columns:

- `roster_id uuid PK`
- `cleaner_party_id uuid NOT NULL FK cleaner_profile`
- `property_id uuid NOT NULL FK property`
- `roster_status text NOT NULL` (`ACTIVE`, `PAUSED`, `REMOVED`)
- `offer_tier smallint NOT NULL`
- `priority_within_tier integer NULL`
- `eligible_from timestamptz NULL`
- `eligible_until timestamptz NULL`
- timestamps

Constraints:

- one current roster row per `(cleaner_party_id, property_id)`
- `offer_tier > 0`
- validity interval sane

Semantics:

- elapsed Offer time never mutates this row
- opening Tier 2 never removes Tier 1
- Candidate `tier_no` is a snapshot of the opportunity at candidate creation and is not rewritten when roster tier later changes
- current roster `ACTIVE`/effective interval is fresh-checked at accept/reassign time
- if current roster is `PAUSED` or `REMOVED`, new acceptance fails even if an old Offer remains visible
- already `HARD_BOOKED` work is not released merely because roster status changes

### 3.9 `cleaner_schedule_block`

Purpose: explicit Cleaner blackout/unavailability interval independent of a Cleaning-specific cannot-perform case.

Columns:

- `schedule_block_id uuid PK`
- `cleaner_party_id uuid NOT NULL FK cleaner_profile`
- `starts_at timestamptz NOT NULL`
- `ends_at timestamptz NOT NULL`
- `reason_code text NULL`
- `source_ref text NULL`
- `cancelled_at timestamptz NULL`
- `created_at timestamptz NOT NULL`

Constraints:

- `ends_at > starts_at`
- active row means `cancelled_at IS NULL`

Mutation rule:

- interval/reason are immutable after creation
- change = cancel old row + insert new row
- cancellation uses Cleaner aggregate lock because it can race with acceptance/reassignment
- active overlap lookup uses a partial index/predicate on `cancelled_at IS NULL`

Frozen overlap basis:

- Cleaner schedule block compares against raw Assignment work slot
- Cleaner-to-Cleaner job conflict compares against buffer-expanded `busy_window`

---

## 4. Reservation / Cleaning schedule authority

### 4.1 `reservation`

Columns:

- `reservation_id uuid PK`
- `reservation_code text UNIQUE NOT NULL`
- `property_id uuid NOT NULL`
- `rental_unit_id uuid NULL`
- `source_channel text NULL`
- `external_reservation_id text NULL`
- `reservation_status text NOT NULL` (`CONFIRMED`, `CANCELLED` initially)
- `check_in_at timestamptz NULL`
- `check_out_at timestamptz NOT NULL`
- `source_version bigint NOT NULL`
- timestamps

Constraints:

- `source_version > 0`
- `check_out_at > check_in_at` when check-in exists
- `CHECK ((source_channel IS NULL) = (external_reservation_id IS NULL))`
- `UNIQUE(reservation_id, property_id)` — explicit composite-FK target key
- `UNIQUE(reservation_id, rental_unit_id)` — explicit composite-FK target key
- `(property_id, rental_unit_id) -> rental_unit(property_id, rental_unit_id)` when unit exists
- unique `(source_channel, external_reservation_id)` when source identity exists

Business mutation:

- checkout/cancel orchestration belongs in backend transaction service
- direct ad-hoc runtime update is not part of the supported contract

### 4.2 `cleaning_job`

Columns:

- `cleaning_id uuid PK`
- `cleaning_code text UNIQUE NOT NULL`
- `reservation_id uuid NULL`
- `property_id uuid NOT NULL`
- `rental_unit_id uuid NULL`
- `schedule_source_type text NOT NULL`
- `cleaning_status text NOT NULL`
- `current_schedule_revision_id uuid NULL`
- timestamps

Initial `schedule_source_type`:

- `RESERVATION_CHECKOUT`
- `MANUAL`
- `MID_STAY`

Initial status set:

- `PLANNED`
- `OFFERING`
- `ASSIGNED`
- `IN_PROGRESS`
- `COMPLETED`
- `CANCELLED`

Critical constraints:

- `(reservation_id, property_id) -> reservation(reservation_id, property_id)`
- `(reservation_id, rental_unit_id) -> reservation(reservation_id, rental_unit_id)` when unit exists
- `(property_id, rental_unit_id) -> rental_unit(property_id, rental_unit_id)` when unit exists
- composite FK `(cleaning_id, current_schedule_revision_id) -> cleaning_schedule_revision(cleaning_id, schedule_revision_id)`; deferrable for insert+pointer update transaction
- `RESERVATION_CHECKOUT` requires non-null `reservation_id`

Current-revision pointer guard:

- historical revision identity can never be reactivated
- NULL -> revision 1 is allowed for initialization
- non-null pointer update must move to exactly the next higher revision for the same Cleaning
- direct UPDATE that skips backward/sideways is rejected by a narrow OLD→NEW transition guard

Checkout-derived cardinality:

- at most one non-cancelled `RESERVATION_CHECKOUT` Cleaning per Reservation
- exact partial-index predicate is a DDL item

State invariants:

- a `CANCELLED` or `COMPLETED` Cleaning must not have an administratively OPEN campaign
- if a `HARD_BOOKED` Assignment exists, Cleaning status must be `ASSIGNED` or `IN_PROGRESS`
- these cross-table state invariants are primarily enforced by backend lifecycle transaction; DDL should add constraints only where they can be made declarative without fragile trigger workflow

### 4.3 `cleaning_schedule_revision`

Purpose: immutable schedule history.

Columns:

- `schedule_revision_id uuid PK`
- `cleaning_id uuid NOT NULL FK cleaning_job`
- `revision_no integer NOT NULL`
- `service_window_start_at timestamptz NOT NULL`
- `service_deadline_at timestamptz NOT NULL`
- `required_work_minutes integer NULL`
- `source_checkout_at timestamptz NULL`
- `source_reservation_version bigint NULL`
- `change_reason_code text NOT NULL`
- `source_command_id uuid NULL FK command_receipt(command_id)`
- `created_at timestamptz NOT NULL`

Constraints:

- `revision_no > 0`
- `UNIQUE(cleaning_id, revision_no)`
- `UNIQUE(cleaning_id, schedule_revision_id)`
- deadline > window start
- work minutes > 0 when non-null
- work minutes <= window length
- checkout-derived revision requires non-null `source_reservation_version`

Mutation policy:

- append-only
- historical row UPDATE/DELETE is unsupported for runtime
- past revision is never reactivated
- if business values return to a previous value, create a new higher `revision_no`

CAS / append primitive contract:

- app runtime has no generic direct INSERT privilege on `cleaning_schedule_revision` and no direct UPDATE privilege on `cleaning_job.current_schedule_revision_id`
- backend transaction calls narrow `append_cleaning_schedule_revision(...)` primitive with already-decided business values + expected current revision
- primitive locks/verifies the Cleaning row as required, inserts exactly `current revision_no + 1`, and advances the current pointer in the same transaction
- initial NULL pointer may create revision 1 only
- stale expected revision -> `STALE_SCHEDULE_REVISION`
- for `RESERVATION_CHECKOUT`, non-null `source_checkout_at` + `source_reservation_version` are mandatory and must exactly match the current Reservation source state
- this primitive does not close Campaigns, create Reconciliation, or perform checkout workflow; those remain Backend orchestration

Checkout-derived invariant:

- `source_reservation_version` records which Reservation version produced the schedule revision

---

## 5. Offer authority

### 5.1 `cleaning_offer_campaign`

Purpose: one candidate-discovery/offer campaign bound to an exact Cleaning schedule revision.

Columns:

- `campaign_id uuid PK`
- `cleaning_id uuid NOT NULL`
- `schedule_revision_id uuid NOT NULL`
- `campaign_no integer NOT NULL`
- `campaign_status text NOT NULL` (`OPEN`, `CLOSED`, `CANCELLED`)
- `open_tier_floor smallint NOT NULL DEFAULT 1`
- `max_tier smallint NOT NULL`
- `tier_expand_after_minutes integer NULL`
- `acceptance_cutoff_at timestamptz NOT NULL`
- `base_fee_krw bigint NOT NULL`
- `replacement_urgency text NOT NULL` (`NORMAL`, `URGENT`)
- `urgent_premium_krw bigint NOT NULL DEFAULT 0`
- `total_agreed_fee_krw bigint NOT NULL`
- `urgent_premium_policy_version text NULL`
- `opened_at timestamptz NOT NULL`
- `closed_at timestamptz NULL`
- `closed_reason_code text NULL`
- timestamps

Constraints:

- exact composite FK `(cleaning_id, schedule_revision_id)`
- `UNIQUE(cleaning_id, campaign_no)`
- `UNIQUE(campaign_id, cleaning_id, schedule_revision_id)` — explicit Assignment/provenance FK target
- at most one administratively OPEN campaign per Cleaning
- `1 <= open_tier_floor <= max_tier`
- `tier_expand_after_minutes IS NULL OR tier_expand_after_minutes > 0`
- `opened_at < acceptance_cutoff_at`
- fee arithmetic exact
- OPEN/CLOSED timestamp consistency

Transition guard:

- `open_tier_floor` may stay equal or increase, never decrease
- `OPEN -> CLOSED/CANCELLED` is terminal
- CLOSED/CANCELLED cannot reopen
- immutable Campaign binding/fee snapshot fields cannot be changed after exposure

Creation precondition:

- bound revision must have `required_work_minutes IS NOT NULL`

Cutoff consistency:

- campaign cutoff must not exceed the revision service deadline
- backend validates this in the creation transaction; DDL may add a narrow guard only if it can remain declarative/simple

### 5.2 Time-derived cumulative Tier widening

V2.2.1 does not persist time-driven tier-opening rows and does not require a correctness scheduler for Tier expansion.

At one captured DB decision time `t`:

```text
if tier_expand_after_minutes IS NULL:
    time_tier = 1
else:
    time_tier = 1 + floor((t - opened_at) / tier_expand_after_minutes)

effective_open_tier = min(max_tier, max(open_tier_floor, time_tier))
```

Rules:

- Tier 1 remains eligible after Tier 2 opens
- time does not transfer ownership
- no extra notification is required merely because a higher Tier becomes time-eligible
- delayed/missing scheduler cannot postpone logical eligibility
- `open_tier_floor` is used only for explicit campaign-level widening and may move only upward
- explicit Cleaner decline does not itself mutate campaign Tier

Query/read-side guidance:

- use one `statement_timestamp()` value per read statement/view evaluation

Authoritative mutation guidance:

- use one captured `decision_at` obtained from DB clock after relevant locks

### 5.3 `cleaning_offer_candidate`

Purpose: candidate snapshot bound to one Campaign and one proposed work slot.

Columns:

- `offer_candidate_id uuid PK`
- `campaign_id uuid NOT NULL`
- `cleaner_party_id uuid NOT NULL`
- `tier_no smallint NOT NULL`
- `candidate_status text NOT NULL` (`ELIGIBLE`, `DECLINED`, `ACCEPTED`)
- `proposal_version integer NOT NULL DEFAULT 1`
- `proposed_start_at timestamptz NOT NULL`
- `proposed_end_at timestamptz NOT NULL`
- `proposed_buffer_before_minutes integer NOT NULL DEFAULT 0`
- `proposed_buffer_after_minutes integer NOT NULL DEFAULT 0`
- `buffer_basis text NOT NULL`
- `buffer_policy_ref text NULL`
- `evaluated_at timestamptz NOT NULL`
- `declined_at timestamptz NULL`
- `accepted_at timestamptz NULL`
- timestamps

Initial `buffer_basis` values:

- `NOT_APPLIED`
- `MANUAL`
- `ROUTE_ESTIMATE`
- `POLICY`

Constraints:

- `proposal_version > 0`
- `tier_no > 0`
- `proposed_end_at > proposed_start_at`
- buffers `>= 0`
- `UNIQUE(campaign_id, cleaner_party_id)`
- `UNIQUE(campaign_id, offer_candidate_id, cleaner_party_id)`
- `UNIQUE(campaign_id, offer_candidate_id, cleaner_party_id, proposal_version)` — Assignment accepted-proposal FK target
- partial unique: at most one `ACCEPTED` Candidate per Campaign
- DECLINED/ACCEPTED timestamp consistency

Immutable candidate binding after exposure:

- `campaign_id`
- `cleaner_party_id`
- `tier_no`

must not be silently rewritten after Candidate exposure.

Proposal-change contract:

- proposal fields are `proposed_start_at`, `proposed_end_at`, both buffers, `buffer_basis`, `buffer_policy_ref`
- any proposal-field change requires `proposal_version = OLD.proposal_version + 1`
- incrementing proposal_version without changing proposal fields is rejected
- proposal fields cannot change without incrementing proposal_version
- proposal change is permitted only while Candidate status is `ELIGIBLE`
- `ELIGIBLE -> DECLINED/ACCEPTED` is terminal; terminal Candidate cannot be reactivated or reproposed
- callback/token binds `offer_candidate_id + proposal_version`
- accept and decline commands require `expected_proposal_version`
- stale version -> `STALE_PROPOSAL`
- start/end themselves are not trusted callback inputs
- Candidate proposal version is part of semantic idempotency payload

Implementation boundary:

- these are narrow OLD→NEW transition guards, not workflow stored procedures

Acceptance-time rule:

- `decision_at < proposed_start_at`
- stale button cannot book a slot that has already started

---

## 6. Assignment authority

### 6.1 `cleaning_assignment`

Columns:

- `assignment_id uuid PK`
- `cleaning_id uuid NOT NULL`
- `assignment_no integer NOT NULL`
- `schedule_revision_id uuid NOT NULL`
- `campaign_id uuid NULL`
- `offer_candidate_id uuid NULL`
- `accepted_proposal_version integer NULL`
- `cleaner_party_id uuid NOT NULL`
- `assignment_source text NOT NULL`
- `assignment_status text NOT NULL`
- `booked_at timestamptz NOT NULL`
- `scheduled_start_at timestamptz NOT NULL`
- `scheduled_end_at timestamptz NOT NULL`
- `work_minutes_snapshot integer NOT NULL`
- `travel_buffer_before_minutes integer NOT NULL DEFAULT 0`
- `travel_buffer_after_minutes integer NOT NULL DEFAULT 0`
- `buffer_basis text NOT NULL`
- `buffer_policy_ref text NULL`
- `busy_window tstzrange NOT NULL`
- `base_fee_krw bigint NOT NULL`
- `replacement_urgency text NOT NULL`
- `urgent_premium_krw bigint NOT NULL`
- `total_agreed_fee_krw bigint NOT NULL`
- `urgent_premium_policy_version text NULL`
- `ended_at timestamptz NULL`
- `end_reason_code text NULL`
- timestamps

Removed from V1:

- `service_window_start_snapshot`
- `service_deadline_snapshot`

Reason:

- Assignment already references immutable schedule revision
- duplicated service-window snapshot would require another synchronization invariant

Assignment sources:

- `OFFER_ACCEPTED`
- `ORIGINAL_REASSIGNED`
- `SCHEDULE_REBOOKED`
- `MANUAL_OVERRIDE`

Statuses:

- `HARD_BOOKED`
- `RELEASED`
- `COMPLETED`
- `CANCELLED`

Constraints:

- `UNIQUE(cleaning_id, assignment_no)`
- `UNIQUE(assignment_id, cleaning_id, schedule_revision_id)` — reconciliation FK target
- `UNIQUE(assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id)` — unavailability FK target
- partial unique: one `HARD_BOOKED` Assignment per Cleaning
- exact composite FK `(cleaning_id, schedule_revision_id)` to Cleaning schedule revision
- OFFER_ACCEPTED exact FK `(campaign_id, cleaning_id, schedule_revision_id) -> cleaning_offer_campaign(campaign_id, cleaning_id, schedule_revision_id)`
- OFFER_ACCEPTED exact FK `(campaign_id, offer_candidate_id, cleaner_party_id, accepted_proposal_version) -> cleaning_offer_candidate(campaign_id, offer_candidate_id, cleaner_party_id, proposal_version)`
- `accepted_proposal_version` is NOT NULL for `OFFER_ACCEPTED` and NULL for non-offer sources
- exact duration: `scheduled_end_at - scheduled_start_at = work_minutes_snapshot * interval '1 minute'`
- buffers `>= 0`
- fee arithmetic exact
- terminal/ended-field consistency
- source-specific nullable-field consistency

Offer-provenance insert validator:

A narrow DB insert-time validator rejects `OFFER_ACCEPTED` Assignment unless:

- Assignment slot + buffer + buffer provenance equal Candidate proposal at `accepted_proposal_version`
- Assignment fee/urgency/policy snapshots equal Campaign snapshots
- Assignment work minutes equal bound Revision `required_work_minutes`
- Assignment Cleaning/revision equal Campaign binding

This validator protects immutable ledger provenance only; it does not perform candidate ranking, acceptance workflow, or messaging.

Manual override:

- may bypass selected normal business gates only through explicit backend authorization
- must record operator actor and reason in `domain_event`
- must never bypass hard DB integrity such as malformed FK or overlapping `HARD_BOOKED` unless a specifically approved emergency contract exists

### 6.2 Busy-window protection

Invariant:

- same Cleaner cannot have overlapping `HARD_BOOKED` busy windows

Target representation:

```text
busy_window = [
  scheduled_start_at - travel_buffer_before,
  scheduled_end_at   + travel_buffer_after
)
```

Final DB fence:

```text
EXCLUDE USING gist (
  cleaner_party_id WITH =,
  busy_window WITH &&
)
WHERE assignment_status = 'HARD_BOOKED'
```

V2.2.1 first-DDL implementation decision:

- use an unconditional BEFORE INSERT/UPDATE trigger that always derives `NEW.busy_window` from slot + non-null buffers
- application-supplied busy_window is ignored/overwritten and never authoritative
- generated-column optimization is deferred unless a later migration demonstrates a clearly understandable genuinely immutable expression

Reason:

- avoids pretending non-immutable timestamptz arithmetic is immutable
- keeps the initial DDL understandable and tamper-proof
- trigger is a narrow invariant primitive, not business workflow

Application-computed writable busy range is rejected.

PostgreSQL dependency:

- GiST equality for UUID + range overlap requires `btree_gist` extension in the migration baseline

---

## 7. Unavailable / reassignment / reconciliation

### 7.1 `cleaner_unavailability_case`

Columns:

- `unavailability_id uuid PK`
- `cleaning_id uuid NOT NULL`
- `schedule_revision_id uuid NOT NULL`
- `original_assignment_id uuid NOT NULL`
- `cleaner_party_id uuid NOT NULL`
- `case_status text NOT NULL` (`CONFIRMED`, `CANCELLED`)
- `availability_classification text NOT NULL` (`EARLY_UNAVAILABLE`, `SAME_DAY_UNAVAILABLE`)
- `replacement_urgency text NOT NULL`
- `reason_code text NULL`
- `reason_text text NULL`
- `occurred_at timestamptz NOT NULL`
- timestamps

Constraints:

- exact FK `(original_assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id) -> cleaning_assignment(assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id)`
- `UNIQUE(unavailability_id, cleaning_id, cleaner_party_id, original_assignment_id)` — reassignment-request FK target
- one unavailability case per Assignment

Time authority:

- `occurred_at` is backend-captured DB decision time, not caller-supplied authority time

Performance score persistence remains a later policy slice.

### 7.2 `cleaner_reassignment_request`

Columns:

- `reassignment_request_id uuid PK`
- `unavailability_id uuid NOT NULL`
- `cleaning_id uuid NOT NULL`
- `cleaner_party_id uuid NOT NULL`
- `original_assignment_id uuid NOT NULL`
- `requested_schedule_revision_id uuid NOT NULL`
- `request_no integer NOT NULL`
- `request_status text NOT NULL`
- `requested_at timestamptz NOT NULL`
- `decided_at timestamptz NULL`
- `decision_code text NULL`
- timestamps

Statuses:

- `REQUESTED`
- `REASSIGNED_ORIGINAL`
- `CONTINUE_REPLACEMENT`
- `SUPERSEDED`
- `CANCELLED`

Constraints:

- `UNIQUE(unavailability_id, request_no)`
- partial unique: at most one `REQUESTED` row per Cleaning
- exact FK `(unavailability_id, cleaning_id, cleaner_party_id, original_assignment_id) -> cleaner_unavailability_case(unavailability_id, cleaning_id, cleaner_party_id, original_assignment_id)`
- exact FK `(cleaning_id, requested_schedule_revision_id) -> cleaning_schedule_revision(cleaning_id, schedule_revision_id)`

Semantics:

- resolved request does not permanently block a later request for same unavailability case
- schedule revision change may supersede open request
- replacement win may supersede open request

### 7.3 `cleaning_schedule_reconciliation`

Purpose: one current unresolved mismatch between the effective hard-booked Assignment and the Cleaning's latest desired schedule.

Columns:

- `schedule_reconciliation_id uuid PK`
- `cleaning_id uuid NOT NULL`
- `hard_booked_assignment_id uuid NOT NULL`
- `base_assignment_revision_id uuid NOT NULL`
- `target_schedule_revision_id uuid NOT NULL`
- `case_version integer NOT NULL DEFAULT 1`
- `reconciliation_status text NOT NULL` (`PENDING`, `RESOLVED`, `CANCELLED`)
- `reason_code text NOT NULL`
- `source_ref text NULL`
- `created_at timestamptz NOT NULL`
- `updated_at timestamptz NOT NULL`
- `resolved_at timestamptz NULL`
- `resolution_code text NULL`

Constraints:

- `case_version > 0`
- partial unique: one `PENDING` reconciliation per Cleaning
- exact FK `(hard_booked_assignment_id, cleaning_id, base_assignment_revision_id) -> cleaning_assignment(assignment_id, cleaning_id, schedule_revision_id)`
- exact FK `(cleaning_id, target_schedule_revision_id) -> cleaning_schedule_revision(cleaning_id, schedule_revision_id)`
- base != target
- terminal-field consistency

Transition guard:

- while PENDING, target change requires `case_version = OLD.case_version + 1`
- incrementing case_version without target change is rejected
- terminal RESOLVED/CANCELLED rows cannot return to PENDING
- resolution cannot alter base Assignment/base revision

Rapid checkout semantics:

```text
HARD_BOOKED Assignment = R1
Desired revision R2 -> R3 -> R4

one PENDING reconciliation:
base   = R1
target = R4
case_version increments as target changes
```

Resolution CAS contract:

Backend resolution requires all of:

- `schedule_reconciliation_id`
- `expected_case_version`
- `expected_hard_booked_assignment_id`
- `expected_target_schedule_revision_id`

Conditional update succeeds only when all still match and status is `PENDING`.

Mismatch -> `STALE_RECONCILIATION`.

Scheduled-action payload must carry reconciliation ID + case version.

When target is coalesced forward:

- old scheduled action may be cancelled, or
- worker executes and becomes a stale no-op through case-version check

Assignment release invariant:

- any command that releases/cancels the referenced hard-booked Assignment must resolve/cancel related PENDING reconciliation/request/action in the same transaction

---

## 8. Command / transport idempotency / stale-writer fencing

### 8.1 `command_receipt`

Purpose:

- idempotency
- actor binding
- transport replay binding
- result replay

Columns:

- `command_id uuid PK`
- `authority_scope_code text NOT NULL`
- `command_type text NOT NULL`
- `idempotency_key text NOT NULL`
- `request_payload jsonb NOT NULL`
- `source_channel_code text NOT NULL`
- `source_stream_key text NULL`
- `source_event_id text NULL`
- `principal_type text NOT NULL`
- `actor_party_id uuid NULL`
- `actor_external_identity_id uuid NULL`
- `authority_epoch bigint NOT NULL`
- `source_observed_at timestamptz NULL`
- `decided_at timestamptz NOT NULL`
- `result_type text NULL`
- `result_id uuid NULL`
- `result_payload jsonb NOT NULL DEFAULT '{}'`

Idempotency uniqueness:

```text
UNIQUE(authority_scope_code, command_type, idempotency_key)
```

Transport replay uniqueness:

```text
UNIQUE(source_channel_code, source_stream_key, source_event_id)
WHERE source_event_id IS NOT NULL
```

Source-binding CHECK:

```text
CHECK ((source_stream_key IS NULL) = (source_event_id IS NULL))
```

Actor composite FK:

```text
(actor_external_identity_id, actor_party_id)
→ external_identity(external_identity_id, party_id)
```

Principal nullability contract:

- `TELEGRAM`: Party + External Identity both required
- `PARTY`: Party required, External Identity NULL
- `SYSTEM` / `MIGRATION`: both NULL
- all-or-none/mode-specific CHECKs prevent nullable composite-FK bypass

`source_stream_key` examples:

- Cleaner Telegram bot stream
- OPS Telegram bot stream
- Airbnb/Gmail reservation consumer stream

Exact replay requires equality of:

- authority scope
- command type
- canonical semantic payload
- principal/actor Party + external identity, or identical system principal
- source stream/event binding when present

Canonical semantic payload rules:

- produced from typed backend semantic inputs, not raw transport JSON
- JSON absent vs JSON null are distinct
- timestamp semantics compare normalized instant
- floating-point business values are prohibited
- array ordering meaning must be explicit
- include expected revision/proposal/source versions
- exclude transport observation timestamp
- exclude expected authority epoch from semantic payload equality

Receipt lifecycle:

- failed business command rolls back without a success receipt
- success receipt, business mutation, domain event, and required outbox intent commit atomically

### 8.2 `authority_epoch`

Columns:

- `scope_code text PK`
- `current_epoch bigint NOT NULL`
- `updated_at timestamptz NOT NULL`

Initial scope:

- `CLEANER_SCHEDULING`

New mutation transaction contract:

1. backend calls narrow `lock_and_verify_authority_epoch(scope, expected_epoch)` DB function
2. function runs `SELECT ... FOR SHARE` and compares epoch
3. shared row lock remains held until caller transaction commit/rollback

Privilege reason:

- PostgreSQL row-locking SELECT requires UPDATE privilege, so app runtime is not granted direct generic UPDATE on `authority_epoch`
- the narrow function is `SECURITY DEFINER`, fixed-search-path, schema-qualified, and `PUBLIC EXECUTE` is revoked
- runtime receives EXECUTE only; cutover authority owns epoch UPDATE

Cutover contract:

- epoch increment uses UPDATE on same row
- UPDATE waits for in-flight shared locks to finish
- after increment, old runtime cannot start a new mutation with stale epoch

Replay exception:

- exact replay of already committed result may return stored result without requiring old epoch to become current again, provided actor/source/payload replay binding is exact

### 8.3 Standard aggregate lock order

To avoid deadlock, backend transaction services use one global order:

1. authority epoch shared lock
2. Cleaner rows sorted by Cleaner Party ID
3. Cleaning rows sorted by Cleaning ID
4. Campaign / Candidate / Assignment / Reconciliation child rows

Roster/identity/schedule-block mutation that can race with acceptance also locks Cleaner parent first.

### 8.4 `domain_event`

Purpose:

- canonical audit/change ledger
- projection trigger source

Not an event-sourced state authority.

Columns:

- `domain_event_id bigserial PK`
- `aggregate_type text NOT NULL`
- `aggregate_id uuid NOT NULL`
- `aggregate_version bigint NULL`
- `event_type text NOT NULL`
- `command_id uuid NOT NULL FK command_receipt`
- `actor_party_id uuid NULL`
- `payload jsonb NOT NULL`
- `occurred_at timestamptz NOT NULL`
- `created_at timestamptz NOT NULL`

Critical clarification:

- `domain_event_id` is sequence allocation order, not global transaction commit order
- it must not be used as a global high-water projection cursor
- same-aggregate ordering may use aggregate version and aggregate-root serialization
- external projectors consume/claim `integration_outbox`, not a global event-ID cursor
- external projections should prefer desired-state upsert over fragile incremental patch replay

---

## 9. Async durable work

### 9.1 `business_scheduled_action`

Purpose: durable future business work only.

No DB-stored cron definition/run-history tables in core schema.

Columns:

- `scheduled_action_id uuid PK`
- `action_type text NOT NULL`
- `aggregate_type text NOT NULL`
- `aggregate_id uuid NOT NULL`
- `due_at timestamptz NOT NULL`
- `available_at timestamptz NOT NULL`
- `action_status text NOT NULL`
- `idempotency_key text NOT NULL UNIQUE`
- `payload jsonb NOT NULL`
- `attempt_count integer NOT NULL DEFAULT 0`
- `max_attempts integer NOT NULL`
- `lease_owner text NULL`
- `lease_until timestamptz NULL`
- `lease_fence bigint NOT NULL DEFAULT 0`
- `last_error_code text NULL`
- `completed_at timestamptz NULL`
- `cancelled_at timestamptz NULL`
- timestamps

Statuses:

- `PENDING`
- `RUNNING`
- `FAILED_RETRYABLE`
- `SUCCEEDED`
- `DEAD_LETTER`
- `CANCELLED`

Claim eligibility:

```text
(
  status IN (PENDING, FAILED_RETRYABLE)
  AND available_at <= decision_at
)
OR
(
  status = RUNNING
  AND lease_until < decision_at
)
```

Claim behavior:

- DB decision time captured once
- `FOR UPDATE SKIP LOCKED`
- increment `lease_fence`
- assign new owner/until
- apply attempt-count rule
- max attempts -> `DEAD_LETTER`

Worker privilege boundary:

- async worker has no generic direct UPDATE on queue tables
- worker executes narrow claim / complete / fail / reconcile functions that always enforce status + owner + lease_fence predicates
- app runtime also has no generic queue-status UPDATE privilege
- app INSERT is column-restricted: new rows use DB-default `PENDING`, `attempt_count=0`, `lease_fence=0`, with lease/terminal fields NULL
- app cancellation uses a narrow function and is allowed only before worker ownership (`PENDING` / `FAILED_RETRYABLE`)
- `business_scheduled_action.payload` is immutable; changing work means cancel old action and insert a new versioned/idempotent action

Complete/fail CAS:

```text
WHERE scheduled_action_id = ?
  AND action_status = 'RUNNING'
  AND lease_fence = ?
  AND lease_owner = ?
```

Queue field consistency:

- RUNNING requires owner + lease_until
- terminal status cannot retain active lease
- SUCCEEDED requires completed_at
- CANCELLED requires cancelled_at

Tier expansion/cutoff are not correctness-dependent scheduled actions because acceptance derives validity from DB time.

### 9.2 `integration_outbox`

Columns:

- `outbox_id uuid PK`
- `domain_event_id bigint NULL FK domain_event`
- `event_type text NOT NULL`
- `aggregate_type text NOT NULL`
- `aggregate_id uuid NOT NULL`
- `destination_type text NOT NULL`
- `destination_ref text NULL`
- `available_at timestamptz NOT NULL`
- `outbox_status text NOT NULL`
- `idempotency_key text NOT NULL UNIQUE`
- `payload jsonb NOT NULL`
- `attempt_count integer NOT NULL DEFAULT 0`
- `max_attempts integer NOT NULL`
- `lease_owner text NULL`
- `lease_until timestamptz NULL`
- `lease_fence bigint NOT NULL DEFAULT 0`
- `external_effect_id text NULL`
- `last_error_code text NULL`
- `delivered_at timestamptz NULL`
- timestamps

Statuses:

- `PENDING`
- `RUNNING`
- `FAILED_RETRYABLE`
- `PENDING_RECONCILIATION`
- `SUCCEEDED`
- `DEAD_LETTER`
- `CANCELLED`

Claim/reclaim/fencing contract follows `business_scheduled_action`; async worker has no generic queue UPDATE privilege and uses the same narrow claim/complete/fail/reconcile function boundary. App creation is column-restricted to DB-default `PENDING`; app cancellation uses a narrow pre-delivery cancel function rather than direct status UPDATE.

External-call limitation:

- lease fencing prevents stale worker DB completion
- it cannot undo an external Telegram/Calendar call already made
- destination-specific idempotency or `PENDING_RECONCILIATION` remains necessary

No separate outbox-attempt table in V2.2.1 core.

### 9.3 `integration_resource_binding`

Purpose: external projection identity/state.

Columns:

- `binding_id uuid PK`
- `aggregate_type text NOT NULL`
- `aggregate_id uuid NOT NULL`
- `destination_type text NOT NULL`
- `resource_code text NULL`
- `external_resource_id text NOT NULL`
- `external_uid text NULL`
- `sync_status text NOT NULL`
- `last_applied_aggregate_version bigint NULL`
- `external_version text NULL`
- `last_synced_at timestamptz NULL`
- timestamps

Uniqueness:

- one binding per `(aggregate_type, aggregate_id, destination_type, resource_code)` using `UNIQUE NULLS NOT DISTINCT` semantics for nullable `resource_code`
- external resource identity unique within destination/resource scope

Projection semantics:

- no assumption that global domain-event ID is commit order
- use aggregate version/resource version for per-resource stale-write fencing where needed
- prefer latest desired-state upsert

---

## 10. DB runtime roles

V2.2.1 simplifies runtime DB roles unless deployment topology proves a stronger trust boundary is useful.

Target roles:

- `propertyai_owner` (`NOLOGIN`)
- `propertyai_app_runtime`
- `propertyai_async_worker`
- `propertyai_readonly`
- `propertyai_migrator`

Backend authorization distinguishes Cleaner user vs OPS/operator permissions.

DB-role separation is not used to pretend there is a trust boundary if one Spring/Python backend uses one connection pool for both.

Owner/migrator/worker/readonly separation remains useful.

Role-membership boundary:

- privileged bootstrap is separate from Flyway migrations
- `propertyai_owner` and `propertyai_migrator` remain `NOLOGIN`
- canonical deployment LOGIN is `propertyai_flyway` with `INHERIT FALSE / SET TRUE / ADMIN FALSE` membership in `propertyai_migrator`
- `propertyai_migrator` has the same SET-only, non-inherited, non-admin membership in `propertyai_owner`
- the complete privileged membership graph is exact: owner has only migrator, migrator has only Flyway, and no unexpected direct or recursive SET-enabled path is accepted
- bootstrap/preflight fail closed on role attribute, membership option/grantor, duplicate membership, recursive SET path, extension, or schema-owner drift
- app runtime and async worker are never members of owner/migrator roles
- Flyway authenticates as `propertyai_flyway`; PostgreSQL JDBC startup options establish `role=propertyai_owner` on every physical connection and nontransactional `afterConnect` reasserts/verifies that session/current-user contract
- Flyway uses `defaultSchema=propertyai`, owns `propertyai.flyway_schema_history` through the active owner role, and migrations never `RESET ROLE` before history recording
- readonly access to sensitive receipt/identity data is through masked views; granting the base-table SELECT in parallel would defeat masking

### 10.1 Authority-table write policy

Business workflow remains in backend.

For scheduling-authority tables, runtime write access is exposed only through one of two approved mechanisms:

1. direct table DML inside a reviewed backend transaction when declarative DB constraints fully protect the invariant, or
2. a narrow DB mutation primitive when a cross-row invariant cannot be safely expressed by declarative constraints alone

The primitive must not contain business orchestration/ranking/UX logic.

Examples likely safe for application DML under constraints:

- Candidate decline CAS
- outbox insertion in same transaction
- simple state updates with version predicate

Examples that may justify narrow primitive/trigger/helper:

- tamper-proof busy-window derivation
- queue claim with lease fencing, if standardized SQL cannot be safely reused

### 10.2 Security hardening if SECURITY DEFINER is used

- function owner `NOLOGIN`
- `REVOKE EXECUTE ... FROM PUBLIC`
- fixed safe `search_path`
- schema-qualified object references
- application-writable schemas do not have unsafe CREATE privileges
- dynamic SQL avoided
- no broad break-glass arbitrary UPDATE function

---

## 11. Backend transaction services

The following are backend service operations, not giant DB workflow procedures.

### 11.1 Offer lifecycle

- create Campaign/Candidates
- explicit campaign Tier-floor widen
- Cleaner decline
- Cleaner accept

### 11.2 Reservation/Cleaning lifecycle

- apply reservation checkout change
- apply reservation cancellation
- manual Cleaning schedule change
- create/cancel Cleaning

### 11.3 Cleaner exception lifecycle

- record unavailable
- request original reassignment
- decide original reassignment
- resolve schedule reconciliation

### 11.4 Cleaner/eligibility lifecycle

- roster change
- identity rotate/revoke
- Cleaner operational-status change
- schedule-block change
- capacity-setting change

### 11.5 Cleaning execution lifecycle

- start Cleaning
- complete Cleaning
- cancel/release Assignment

These operations all obey the common epoch/lock/idempotency conventions where they mutate scheduling authority.

---

## 12. Accept-Offer backend transaction contract

Trusted callback input does not contain authoritative acceptance time or Cleaner-selected start/end.

Semantic inputs:

- offer candidate ID
- expected proposal version
- idempotency key
- expected authority epoch
- source channel/stream/event identifiers
- actor identity context

Transaction flow:

1. begin transaction
2. inspect existing receipt candidate by namespaced idempotency key/source event
3. acquire authority-epoch `FOR SHARE`; exact committed replay may use its separate replay path
4. identify Candidate/Campaign and Cleaner
5. lock Cleaner parent row
6. lock Cleaning parent row
7. lock Campaign/Candidate rows as needed
8. re-check receipt under transaction serialization point
9. verify current epoch
10. `decision_at := clock_timestamp()` from DB
11. Campaign is administratively OPEN
12. `opened_at <= decision_at < acceptance_cutoff_at`
13. calculate time-derived `effective_open_tier` from the same `decision_at`
14. Candidate status exactly `ELIGIBLE`
15. `candidate.proposal_version == expected_proposal_version`
16. Candidate tier <= effective Tier
17. `decision_at < candidate.proposed_start_at`
18. actor source stream/event and active Telegram identity bind to same Cleaner Party
19. Cleaner operational status fresh-valid
20. current Property roster fresh-valid
21. Cleaning current revision == Campaign revision
22. Candidate slot/buffer remains valid for immutable bound revision
23. explicit Cleaner schedule-block check
24. optional daily capacity check only if configured
25. pre-check existing HARD_BOOKED busy windows for useful domain error
26. INSERT new HARD_BOOKED Assignment using Candidate slot/buffer snapshots while Candidate is still `ELIGIBLE`
27. GiST exclusion is final cross-Cleaning fence
28. Candidate -> ACCEPTED with version predicate
29. Campaign -> CLOSED, reason ACCEPTED
30. Cleaning state update
31. insert success command receipt
32. insert domain event
33. insert required integration outbox intent
34. commit atomically

Loser Candidate rows are not mass-updated.

DDL invariant implementation note:

- Assignment INSERT-time validation checks immutable Candidate proposal / Campaign fee / Revision work-minute provenance without requiring Candidate terminal state yet
- a DEFERRABLE INITIALLY DEFERRED constraint trigger requires the exact bound Candidate proposal version to be `ACCEPTED` at transaction end
- therefore the Backend order above remains valid without weakening final provenance

---

## 13. Reservation checkout change contract

### 13.1 Public authority is one Backend transaction service

Preferred service:

```text
ReservationService.applyCheckoutChange(...)
```

This is not one giant public DB stored procedure.

The backend transaction atomically updates:

- Reservation `check_out_at`
- Reservation `source_version`
- checkout-derived Cleaning schedule revision
- Cleaning current revision pointer
- stale OPEN Campaign closure
- open reassignment-request supersede as needed
- one-current schedule reconciliation coalesce as needed
- domain event
- outbox intent
- command receipt

### 13.2 Checkout-derived Cleaning mapping

Only Cleaning with:

```text
schedule_source_type = RESERVATION_CHECKOUT
```

is driven by reservation checkout changes.

Cardinality:

- at most one active checkout-derived Cleaning per Reservation

Revision binding:

- new revision stores `source_reservation_version`

### 13.3 Revision append CAS

Backend transaction:

1. authority epoch shared lock
2. lock Reservation
3. lock checkout-derived Cleaning
4. verify source version / expected current revision
5. capture DB decision time
6. update Reservation source version/check-out
7. append `revision_no = prior + 1`
8. update current revision pointer
9. never reactivate old revision identity

If business values return to old date/time, create a new revision number.

### 13.4 Campaign/reconciliation effects

On schedule change:

- close old OPEN Campaign with `SCHEDULE_CHANGED`
- do not mass-update Candidate rows
- supersede open reassignment request bound to old target revision

If no HARD_BOOKED Assignment:

- no reconciliation required

If HARD_BOOKED Assignment exists:

- create one PENDING reconciliation when none exists
- otherwise coalesce target revision into same row
- increment `case_version`
- base revision remains the hard-booked Assignment revision

---

## 14. Reservation cancellation contract

Reservation cancellation is handled by one public backend lifecycle transaction.

It must atomically determine effects on:

- Reservation status
- checkout-derived Cleaning
- OPEN Campaign
- HARD_BOOKED Assignment
- PENDING schedule reconciliation
- open original-reassignment request
- scheduled actions
- domain event
- outbox intents

Exact cancellation policy remains a lifecycle specification item, but the DB structure must support one atomic transaction without orphaned OPEN/HARD_BOOKED/PENDING states.

---

## 15. Schedule reconciliation resolution

Resolution requires CAS against the latest case.

Required expected values:

- reconciliation ID
- case version
- hard-booked Assignment ID
- target schedule revision ID

Supported resolution families:

### 15.1 `KEEP_CLEANER_RESCHEDULE`

Backend:

- validate same Cleaner against target revision/new slot
- re-check Cleaner current eligibility/schedule conflicts according to policy
- release old Assignment
- create new `SCHEDULE_REBOOKED` HARD_BOOKED Assignment
- resolve reconciliation via expected case version
- emit event/outbox

### 15.2 `RELEASE_AND_REOFFER`

Backend:

- release old Assignment
- resolve reconciliation
- Cleaning becomes eligible for a new Campaign on current revision

### 15.3 `CANCELLED`

Used when Reservation/Cleaning cancellation removes the mismatch.

Any release of the referenced Assignment terminalizes the PENDING reconciliation in the same transaction.

---

## 16. Queue semantics

Both `business_scheduled_action` and `integration_outbox` use the same claim model. Claim arguments are fail-closed (`NULL` rejected), batch size and lease duration are bounded, and exhausted-row dead-letter reaping is itself bounded with `FOR UPDATE SKIP LOCKED` so one locked exhausted row cannot block the claim path.

### 16.1 Claimable states

```text
PENDING / FAILED_RETRYABLE and available_at <= decision_at
OR
RUNNING and lease_until < decision_at
```

### 16.2 Claim fencing

- claim under row lock / `SKIP LOCKED`
- increment `lease_fence`
- set new owner/lease

### 16.3 Completion/failure CAS

Requires:

- row ID
- status RUNNING
- expected lease fence
- expected lease owner when available

Stale worker affected rows = 0.

### 16.4 External side effects

Lease fencing only protects DB state.

If external API result is ambiguous:

- use provider idempotency when available
- otherwise move to explicit reconciliation state rather than blind retry

---

## 17. Missing invariants now incorporated

1. ACTIVE Cleaner requires exactly one active Telegram identity.
2. identity/roster/schedule-block/capacity mutations lock Cleaner parent when racing with acceptance.
3. Candidate binding and tier are immutable after exposure.
4. Candidate proposal change increments `proposal_version`.
5. Candidate tier remains opportunity snapshot; roster tier changes are not retroactive.
6. current roster status/effective interval is fresh gate on new accept/reassign.
7. PAUSED/REMOVED blocks new accept but does not auto-release HARD_BOOKED.
8. manual override requires actor/reason and cannot silently bypass DB integrity.
9. reconciliation resolution requires expected case version + assignment + target revision.
10. Assignment release terminalizes related PENDING reconciliation/request/action atomically.
11. Campaign OPEN/CLOSED fields are internally consistent.
12. Queue RUNNING/terminal lease fields are internally consistent.
13. Resource binding nullable-key uniqueness uses NULLS NOT DISTINCT semantics.
14. CANCELLED/COMPLETED Cleaning must not retain OPEN Campaign.
15. HARD_BOOKED implies Cleaning ASSIGNED/IN_PROGRESS at transaction completion.
16. Campaign creation requires revision work duration.
17. daily capacity remains disabled until timezone/cross-midnight policy is frozen.
18. schedule-block raw-slot-vs-busy-window rule must be frozen before DDL.

---

## 18. Deferred policy items — columns prepared, policy not invented

The schema prepares but does not activate:

- `max_daily_work_minutes`
- `max_daily_jobs`

Not yet fixed:

- daily attribution timezone/cross-midnight rule
- travel-buffer default policy
- notification lead/lag policy
- Performance score multi-job aggregation/cap

Travel buffer numeric snapshots are still NOT NULL on Candidate/Assignment when a proposal is created:

- no policy applied => numeric buffer `0`, basis `NOT_APPLIED`
- policy absence is not represented as NULL arithmetic

---

## 19. V2.2.1 table inventory

Target core tables: 24.

### Organization / Property / identity / eligibility

1. `organization`
2. `organization_member`
3. `property`
4. `rental_unit`
5. `party`
6. `cleaner_profile`
7. `external_identity`
8. `cleaner_property_roster`
9. `cleaner_schedule_block`

### Reservation / Cleaning

10. `reservation`
11. `cleaning_job`
12. `cleaning_schedule_revision`

### Offer / Assignment

13. `cleaning_offer_campaign`
14. `cleaning_offer_candidate`
15. `cleaning_assignment`

### Exception workflow

16. `cleaner_unavailability_case`
17. `cleaner_reassignment_request`
18. `cleaning_schedule_reconciliation`

### Command / audit authority

19. `command_receipt`
20. `authority_epoch`
21. `domain_event`

### Async / external projection

22. `business_scheduled_action`
23. `integration_outbox`
24. `integration_resource_binding`

## 20. V2.2.1 review adjudication summary

### Accepted from focused review

- B-01 epoch shared-lock fencing
- B-02 proposal version + Candidate buffer snapshots
- B-03 reconciliation case-version CAS
- B-04 command receipt actor/source-stream binding
- B-05 Reservation-to-checkout-Cleaning cardinality + source version
- B-06 expired RUNNING reclaim + lease fencing
- B-07 domain-event global-order claim removal

### Simplifications accepted

- remove Assignment service-window snapshots
- no separate Offer proposal table
- no event-sourcing/CQRS
- simplify app-facing DB roles to one runtime role unless deploy topology later proves separate credentials useful
- keep workflow in backend, not in DB stored procedures

### R1-R8 decisions

- R1 Time-derived Tier widening: `ACCEPT`
- R2 Candidate-bound slot: `ACCEPT_WITH_MODIFICATION` — proposal version + buffers
- R3 Busy-window derivation: `ACCEPT_WITH_MODIFICATION` — immutable generated helper preferred if genuinely immutable
- R4 Checkout transaction boundary: `MODIFY` — one public backend transaction service, not giant DB workflow function
- R5 Central command receipt: `ACCEPT_WITH_MODIFICATION` — scope/actor/source-stream binding
- R6 Role boundary: `MODIFY` — backend workflow authority; DB primitives only for hard invariant needs
- R7 Roster removal: `ACCEPT` — fresh current roster blocks new accept; old HARD_BOOKED remains
- R8 Decline vs Tier widen: `ACCEPT` — separate operations

---

## 21. Frozen logical decisions before matrix/DDL

### L1 — Schedule-block overlap basis: `RAW_WORK_SLOT`

Decision:

- explicit Cleaner schedule block is compared to raw `scheduled_start_at..scheduled_end_at`
- Cleaner-to-Cleaner Cleaning conflict is compared using buffer-expanded `busy_window`

Reason:

- a schedule block represents time the person cannot perform work
- travel buffer is a separate inter-job scheduling safety margin
- conflating them would over-block availability

Future extension:

- if a block must include travel/non-work margin, introduce an explicit block semantic/type later rather than changing the base meaning silently

### L2 — Checkout-derived Cleaning uniqueness: `ONE_NON_CANCELLED_PER_RESERVATION`

Decision:

```text
UNIQUE(reservation_id)
WHERE schedule_source_type = 'RESERVATION_CHECKOUT'
  AND cleaning_status <> 'CANCELLED'
  AND reservation_id IS NOT NULL
```

Additional rule:

- `RESERVATION_CHECKOUT` requires non-null `reservation_id`
- COMPLETED remains inside uniqueness; a second checkout Cleaning after completion is treated as duplicate unless prior Cleaning was formally CANCELLED and product policy permits regeneration

### L3 — Busy-window implementation: unconditional derivation trigger for first DDL

Decision:

1. V2.2.1 first DDL uses unconditional BEFORE INSERT/UPDATE derivation trigger
2. application-computed/writable `busy_window` is prohibited
3. generated-column alternative is deferred to a later migration only if a genuinely immutable expression is both proven and clearer than the trigger

The invariant and maintainability matter more than generated syntax.

### L4 — Runtime authority model: backend transactional DML + declarative DB fence

Decision:

- backend transaction service owns workflow and business rules
- application may directly DML current-state tables inside reviewed transactions where FK/UNIQUE/CHECK/EXCLUDE/version predicates protect integrity
- append-only/history tables receive narrower privileges
- DB helper/function is used only for hard invariant primitives that are materially safer than repeating SQL in the application

DB roles:

- `propertyai_owner` NOLOGIN
- `propertyai_app_runtime`
- `propertyai_async_worker`
- `propertyai_readonly`
- `propertyai_migrator`

Important terminology:

- `propertyai_owner` is PostgreSQL schema ownership and remains a single NOLOGIN role
- real-world hosts/owners are modeled by `organization` + `organization_member`, allowing multiple human hosts without multiplying DB schema-owner roles

## 22. V2.2.1 bounded schema closure — Focused review findings

The focused V2.2 matrix review returned `MINOR_REWORK_BEFORE_DDL`; 24-table topology remains unchanged.

### F-V22-01 — Exact composite FK target map: CLOSED IN V2.2.1

Explicit non-partial parent candidate keys are now part of the logical contract:

- `reservation(reservation_id, property_id)`
- `reservation(reservation_id, rental_unit_id)`
- `cleaning_schedule_revision(cleaning_id, schedule_revision_id)`
- `cleaning_offer_campaign(campaign_id, cleaning_id, schedule_revision_id)`
- `cleaning_offer_candidate(campaign_id, offer_candidate_id, cleaner_party_id, proposal_version)`
- `cleaning_assignment(assignment_id, cleaning_id, schedule_revision_id)`
- `cleaning_assignment(assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id)`
- `cleaner_unavailability_case(unavailability_id, cleaning_id, cleaner_party_id, original_assignment_id)`
- `external_identity(external_identity_id, party_id)`

### F-V22-02 — Coupled CAS / OLD→NEW guards: CLOSED IN V2.2.1

Narrow transition guards protect:

- Candidate proposal fields ↔ proposal_version
- Candidate terminal status freeze
- Reconciliation target ↔ case_version
- Reconciliation terminal freeze
- Schedule revision append + current-pointer monotonicity
- Campaign open_tier_floor monotonicity and terminal status

These guards reject invalid state transitions only; workflow remains in backend.

### F-V22-03 — Epoch / Queue / Migrator privilege contract: CLOSED IN V2.2.1

- epoch lock/verify: narrow SECURITY DEFINER function; no runtime epoch UPDATE
- queue worker: narrow claim/complete/fail/reconcile functions; no generic queue UPDATE
- owner: NOLOGIN
- migrator only: explicit SET ROLE owner
- runtime/worker: never owner members

### F-V22-04 — Nullable transport/source/actor binding: CLOSED IN V2.2.1

- source stream/event all-or-none CHECK
- reservation source/external ID all-or-none CHECK
- principal-type actor nullability CHECK
- Telegram actor composite identity→Party FK

### F-V22-05 — Assignment accepted proposal provenance: CLOSED IN V2.2.1

Added `accepted_proposal_version`; OFFER_ACCEPTED Assignment binds exact Candidate version and is protected by a narrow insert-time provenance validator comparing Candidate proposal + Campaign financial snapshot + Revision work duration.

### Additional focused-review simplifications accepted

- `cleaner_schedule_block.cancelled_at`; block rows are immutable and changes cancel+insert
- `business_scheduled_action.payload` immutable; change cancels old and inserts new
- global `domain_event_id` remains identity only, not commit order
- no new 25th table
- no Proposal history table
- no Tier opening table
- no event sourcing/CQRS
- multi-host Organization model remains unchanged

---

## 23. DDL major-rework closure contract

The first DDL candidate (`7a74601371604f220030fea16fae87b9c0283bdf`) is rejected as a freeze candidate but preserved as review evidence. Its seven DDL findings are closed by bounded successors without changing the 24-table topology:

- `F-DDL-01`: normal Accept order preserved via insert-time snapshot validation + deferred final Candidate-ACCEPTED constraint
- `F-DDL-02`: app queue/outbox state-forging privilege removed; column-restricted PENDING insert + narrow cancel only
- `F-DDL-03`: NULL/oversized claim arguments rejected; exhausted reaping bounded with `SKIP LOCKED`
- `F-DDL-04`: checkout-derived revision source is mandatory and exact against current Reservation state
- `F-DDL-05`: privileged bootstrap is separate and rerunnable, while migrations run with owner authority authenticated through the non-superuser Flyway LOGIN
- `F-DDL-06`: every Flyway physical connection activates owner at JDBC startup, `afterConnect` verifies/reasserts the identity contract, history is explicitly in `propertyai`, and migrations do not reset the role
- `F-DDL-07`: bootstrap/preflight validate the complete direct and recursive privileged membership graph fail closed, including exact PG18 edge options

---

## 24. Gate after DDL rework

```text
V2_LOGICAL_SCHEMA = V2_2_1_BOUNDED_SCHEMA_CLOSURE_FROZEN
BUSINESS_LOGIC_AUTHORITY = BACKEND
DB_ROLE = DURABLE_LEDGER_AND_INVARIANT_FENCE
CORE_TABLES = 24
BEHAVIORAL_MULTI_JOB_TEST = OUT_OF_SCOPE_THIS_SESSION
DDL_REWORK = F_DDL_01_TO_07_CLOSED_PENDING_FOCUSED_REREVIEW
PRODUCTION_CUTOVER = NO
```

The bounded DDL successor must receive a focused DDL re-review before final freeze. After DDL freeze, full acceptance/race/live verification is handed off to a separate test session.
