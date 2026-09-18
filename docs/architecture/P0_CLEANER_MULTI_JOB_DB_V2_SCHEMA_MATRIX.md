# PropertyAI Cleaner Multi-Job DB V2.2.1 — Schema Matrix

Status: `DRAFT_FOR_FOCUSED_REREVIEW`

Source logical draft:
- `P0_CLEANER_MULTI_JOB_DB_V2_LOGICAL_SCHEMA_DRAFT.md`
- Logical version: `V2.2.1`
- Business logic authority: Backend transaction service
- PostgreSQL role: durable ledger + invariant/concurrency fence
- Core tables: 24

This matrix is the final logical-to-DDL bridge. It does not define behavioral test execution.

---

## 1. Ownership terminology

Two different meanings of “owner” are intentionally separated.

### PostgreSQL schema owner

`propertyai_owner`

- one `NOLOGIN` role
- owns schema/tables/functions
- not a real-world host account
- not multiplied when more hosts join the product

### Real-world hosts / operators

Modeled by:

- `organization`
- `organization_member`

One Organization may have multiple human Members with coarse roles such as OWNER/ADMIN/OPERATOR/VIEWER.

One Property belongs to exactly one operational Organization in V2.2.1.

This is operational tenancy, not legal title-share accounting.

---

## 2. Table-level schema matrix

| # | Table | Aggregate / Purpose | Primary Key | Critical FK / Binding | Critical UNIQUE / EXCLUDE / CHECK | Main indexes | Runtime mutation model | App Runtime | Async Worker | DB owner |
|---:|---|---|---|---|---|---|---|---|---|---|
| 1 | `organization` | Operational tenant / host group | `organization_id` | — | `organization_code UNIQUE`; valid status/environment | status, code | current-state CRUD | RW | R | `propertyai_owner` |
| 2 | `organization_member` | Multi-host/operator membership | `organization_member_id` | `organization_id→organization`; `party_id→party` | one current membership per org+party; lifecycle timestamp CHECK | org/status; party/status | current-state lifecycle | RW | R | `propertyai_owner` |
| 3 | `property` | Property authority + timezone | `property_id` | `organization_id→organization` | `property_code UNIQUE`; valid timezone/name | organization; active | current-state | RW | R | `propertyai_owner` |
| 4 | `rental_unit` | Reservable/operational unit | `rental_unit_id` | `property_id→property` | `rental_unit_code UNIQUE`; `UNIQUE(property_id,rental_unit_id)` | property; active | current-state | RW | R | `propertyai_owner` |
| 5 | `party` | Person/business actor identity | `party_id` | — | `party_code UNIQUE`; environment CHECK | active/environment | current-state | RW | R | `propertyai_owner` |
| 6 | `cleaner_profile` | Cleaner current operational state / nullable future capacity limits | `cleaner_party_id` | PK/FK `party` | operational status; positive nullable limits | operational status | current-state; Cleaner parent lock | RW | R | `propertyai_owner` |
| 7 | `external_identity` | Telegram/provider identity binding | `external_identity_id` | `party_id→party`; composite identity-party binding | active provider-user unique; active provider-chat unique; max one active Telegram/Party | party/provider active lookup | bind/revoke lifecycle; identity binding immutable | RW | R | `propertyai_owner` |
| 8 | `cleaner_property_roster` | Current Property candidate eligibility / tier | `roster_id` | Cleaner→profile; Property→property | one current Cleaner+Property row; tier > 0; interval sane | Property+tier+status; Cleaner+status | current-state; Cleaner parent lock | RW | R | `propertyai_owner` |
| 9 | `cleaner_schedule_block` | Explicit raw work-slot blackout | `schedule_block_id` | Cleaner→profile | end > start; active=`cancelled_at IS NULL` | active Cleaner+time partial index | immutable interval/reason; cancel+insert | I/U(cancel)/R | R | `propertyai_owner` |
| 10 | `reservation` | Reservation current source state | `reservation_id` | explicit `(reservation_id,property_id)` + `(reservation_id,rental_unit_id)` target keys; Property/Unit consistency | source/external-ID all-or-none; source_version>0; external source unique | checkout; external source ID | source-version CAS current-state | RW | R | `propertyai_owner` |
| 11 | `cleaning_job` | Cleaning aggregate root/current status | `cleaning_id` | exact Reservation/Property, Reservation/Unit, Property/Unit FKs; current revision composite FK | cleaning code unique; one non-cancelled checkout-derived Cleaning/Reservation | reservation; property+status; source type | current-state; revision-pointer OLD→NEW guard | RW | R | `propertyai_owner` |
| 12 | `cleaning_schedule_revision` | Immutable schedule history incl. `source_reservation_version` | `schedule_revision_id` | `cleaning_id→cleaning_job`; `source_command_id→command_receipt` | `UNIQUE(cleaning_id,revision_no)`; `UNIQUE(cleaning_id,schedule_revision_id)`; immutable; valid window/duration | UNIQUE already serves latest/history prefix | append only through narrow revision primitive | X(append revision)/R | R | `propertyai_owner` |
| 13 | `cleaning_offer_campaign` | Offer campaign bound to exact revision | `campaign_id` | exact `(cleaning_id,schedule_revision_id)` | one OPEN/cleaning; `UNIQUE(campaign_id,cleaning_id,schedule_revision_id)`; monotonic tier/terminal guard | current OPEN; cutoff | guarded current-state lifecycle | RW | R | `propertyai_owner` |
| 14 | `cleaning_offer_candidate` | Candidate + versioned proposal snapshot | `offer_candidate_id` | Campaign; Cleaner; accepted-proposal composite target key | one Cleaner/campaign; max one ACCEPTED/campaign; `UNIQUE(campaign,candidate,cleaner,proposal_version)`; proposal/status guard | Campaign status/tier; Cleaner ELIGIBLE partial | immutable binding; coupled proposal-version guard; terminal freeze | RW | R | `propertyai_owner` |
| 15 | `cleaning_assignment` | Effective Cleaner booking ledger | `assignment_id` | exact revision + Campaign + Candidate `accepted_proposal_version` provenance | one HARD_BOOKED/Cleaning; GiST exclusion; parent target UNIQUE tuples; insert provenance validator | Cleaner+start; partial HARD_BOOKED/GiST | immutable booking/provenance; terminal columns only | I/U* | R | `propertyai_owner` |
| 16 | `cleaner_unavailability_case` | Cleaner unable-to-perform durable case | `unavailability_id` | exact 4-column original Assignment FK | one case/original Assignment; request-target composite UNIQUE; classification/status CHECK | Cleaning; Cleaner; Assignment | insert + narrow cancel/status lifecycle | RW | R | `propertyai_owner` |
| 17 | `cleaner_reassignment_request` | Original-Cleaner reassignment workflow state | `reassignment_request_id` | exact Unavailability binding + exact target revision | `UNIQUE(unavailability_id,request_no)`; one REQUESTED/Cleaning | partial current request | guarded status lifecycle | RW | R | `propertyai_owner` |
| 18 | `cleaning_schedule_reconciliation` | One current Assignment↔desired schedule mismatch | `schedule_reconciliation_id` | exact Assignment+Cleaning+base revision; exact target revision | one PENDING/Cleaning; case-version↔target guard; terminal freeze | current PENDING; hard-booked Assignment | coupled CAS guard | RW | R | `propertyai_owner` |
| 19 | `command_receipt` | Idempotency + actor/transport replay binding | `command_id` | composite external-identity→Party actor binding | scope+command+key unique; `source_stream_key`+event all-or-none + unique; principal CHECK | key lookup; source event lookup | **append-only success receipt** | I/R | R | `propertyai_owner` |
| 20 | `authority_epoch` | Runtime/cutover stale-writer fence | `scope_code` | — | current_epoch positive/nonnegative | PK only | runtime uses `lock_and_verify...` EXECUTE; no direct UPDATE | X(epoch-lock) | R | `propertyai_owner` |
| 21 | `domain_event` | Audit/projection-change ledger, not event store | `domain_event_id` | `command_id→command_receipt` | event fields; optional aggregate version | aggregate+version/id; command | **append-only** | I/R | R | `propertyai_owner` |
| 22 | `business_scheduled_action` | Durable future business work | `scheduled_action_id` | logical aggregate ref | idempotency unique; payload immutable; DB-default PENDING; lease/status terminal consistency | partial claim + partial expired-RUNNING reclaim | app column-restricted INSERT + cancel function; worker functions only | I/X(cancel)/R | X(queue funcs)/R | `propertyai_owner` |
| 23 | `integration_outbox` | Durable external-effect intent | `outbox_id` | optional domain event | idempotency key unique; payload immutable; DB-default PENDING; queue/lease/delivery consistency | partial claim + partial expired-RUNNING reclaim; aggregate | app column-restricted INSERT + cancel function; worker functions only | I/X(cancel)/R | X(queue funcs)/R | `propertyai_owner` |
| 24 | `integration_resource_binding` | External projection identity/current sync state | `binding_id` | logical aggregate ref | `UNIQUE NULLS NOT DISTINCT` aggregate/destination/resource_code; external ID unique in scope | aggregate+destination; external ID | projection upsert | R | RW | `propertyai_owner` |

Legend:

- `R` = SELECT
- `I` = INSERT
- `U` = UPDATE
- `RW` = normal bounded application CRUD/update
- `U*` = only terminal/status fields; immutable booking identity/slot/fee fields are not edited in place
- `X(cancel)` = EXECUTE narrow cancellation primitive; no generic status UPDATE
- `LOCK` = locking SELECT where direct privilege permits it
- `X(...)` = EXECUTE only on named narrow DB primitive; no generic table UPDATE

---

## 3. Column immutability / mutability matrix

| Table | Immutable after creation | Mutable under backend transaction | Worker-mutable |
|---|---|---|---|
| `organization` | `organization_id`, `organization_code` | name/status | none |
| `organization_member` | membership ID, org, party | role/status/joined/removed | none |
| `property` | ID, code, **organization_id frozen in V2.2.1** | display/timezone/active | none |
| `rental_unit` | ID, code, property_id | display/active | none |
| `party` | ID, code | display/active | none |
| `cleaner_profile` | Cleaner Party ID | operational status, nullable capacity settings | none |
| `external_identity` | provider/user/chat binding, party | `revoked_at` only after bind | none |
| `cleaner_property_roster` | roster identity + Cleaner + Property | status/tier/priority/effective interval | none |
| `cleaner_schedule_block` | block ID + Cleaner + interval + reason | `cancelled_at` only; replacement is cancel+insert | none |
| `reservation` | reservation ID/code/source identity | source status/version/check-in/out | none |
| `cleaning_job` | ID/code, source type after first operational use | current status; current revision pointer only through append-revision primitive | none |
| `cleaning_schedule_revision` | **all business columns** | none | none |
| `cleaning_offer_campaign` | campaign ID, Cleaning, revision, campaign_no | status, open_tier_floor upward, close fields | none |
| `cleaning_offer_candidate` | Candidate ID, campaign, Cleaner, tier; terminal proposal frozen | proposal fields only while ELIGIBLE with exact +1 version; terminal status | none |
| `cleaning_assignment` | Cleaning, revision, Cleaner, campaign/candidate/accepted proposal version, slot, buffers, busy_window, fees, booked_at | assignment_status, ended_at, end_reason | none |
| `cleaner_unavailability_case` | original binding/classification/occurred_at | case_status/reason corrections only through lifecycle | none |
| `cleaner_reassignment_request` | request identity/binding/request_no | status/decision fields | none |
| `cleaning_schedule_reconciliation` | ID, Cleaning, base Assignment/revision | target revision + case_version; status/resolution | none |
| `command_receipt` | **all receipt semantic/result fields after successful insert** | none | none |
| `authority_epoch` | scope code | epoch only by cutover authority | none |
| `domain_event` | **all** | none | none |
| `business_scheduled_action` | action identity/type/aggregate/idempotency/**payload**; initial status/fence/attempt are DB-owned defaults | app cancellation only through narrow function; change requires cancel+new | worker functions mutate lease/status/attempt/error/completion |
| `integration_outbox` | outbox identity/event/aggregate/destination/idempotency/payload; initial status/fence/attempt are DB-owned defaults | app cancellation before external effect only through narrow function | lease/status/attempt/external effect/error/delivery |
| `integration_resource_binding` | binding identity + aggregate/destination scope | none normally | external ID/version/sync state/upsert |

---

## 4. Critical relational constraints matrix

| Invariant | DB mechanism | Backend responsibility |
|---|---|---|
| Property belongs to one operational host group | `property.organization_id NOT NULL FK organization` | authorization scopes query/mutation by Organization membership |
| Multiple hosts can operate same Organization | `organization_member` rows | membership-role authorization |
| one active Telegram identity max per Cleaner Party | partial UNIQUE | ACTIVE lifecycle checks minimum one identity |
| one current roster per Cleaner+Property | UNIQUE / partial UNIQUE | mutate under Cleaner parent lock |
| Reservation→Rental Unit belongs to same Property | composite FK | source adapter resolves IDs |
| Reservation source binding cannot be half-null | source/external-ID all-or-none CHECK | source adapter supplies both or neither |
| Cleaning Reservation/Property/Unit cannot cross-bind | three explicit composite FKs using Reservation target UNIQUE keys | resolve exact Reservation/Property/Unit IDs |
| checkout-derived Cleaning at most one non-cancelled/Reservation | partial UNIQUE | lifecycle decides when regeneration after cancellation is allowed |
| Cleaning current revision belongs to same Cleaning | composite FK | CAS expected-current under Cleaning lock |
| Revision history monotonic identity | `UNIQUE(cleaning_id,revision_no)` + narrow append-revision primitive; no generic app revision INSERT/pointer UPDATE | backend supplies expected current revision + prepared values |
| one OPEN Campaign/Cleaning | partial UNIQUE | close expired/stale Campaign in transaction when needed |
| Campaign bound to exact revision | composite FK | do not offer stale revision |
| Candidate binding immutable | column/privilege/update policy | proposal changes increment `proposal_version` only |
| Candidate proposal/version cannot decouple | OLD→NEW transition guard | proposal mutation only while ELIGIBLE |
| Candidate terminal state cannot reopen | OLD→NEW transition guard | create new Candidate/Campaign when needed |
| at most one ACCEPTED Candidate/Campaign | partial UNIQUE | conditional status update |
| one HARD_BOOKED Assignment/Cleaning | partial UNIQUE | normal booking lifecycle |
| same Cleaner cannot overlap hard bookings | GiST exclusion on generated/derived `[)` busy range | optional pre-check for friendly error |
| Assignment Offer provenance cannot cross aggregates | composite FKs campaign/candidate/cleaner/cleaning/revision | supply correct IDs from locked rows |
| Assignment snapshot must equal accepted Candidate/Campaign/Revision | exact accepted-proposal FK + narrow insert validator | copy snapshots from locked authoritative rows |
| Assignment normal insert order must still end with Candidate ACCEPTED | deferred constraint trigger on exact accepted proposal version | insert Assignment, then CAS Candidate→ACCEPTED in same transaction |
| one PENDING reconciliation/Cleaning | partial UNIQUE | coalesce target + increment case version |
| stale reconciliation cannot resolve | OLD→NEW case-version/target guard + conditional expected version/assignment/target | retain expected snapshot from read |
| command key replay consistency | namespaced UNIQUE + canonical payload comparison | construct typed semantic payload |
| transport event consumed once per stream | partial UNIQUE source stream/event | map Telegram bot/source consumer to stable stream key |
| stale runtime cannot cross cutover | narrow `lock_and_verify_authority_epoch` function holds shared row lock; runtime has no epoch UPDATE | every scheduling mutation invokes primitive |
| stale worker cannot complete reclaimed job | worker has no queue UPDATE; claim/complete/fail/reconcile functions enforce lease_fence+owner | worker carries claim fence |
| app cannot forge queue lifecycle | column-restricted insert defaults to PENDING; cancel function only | app never supplies status/attempt/lease/fence/terminal fields |
| queue claim cannot become unbounded/block on exhausted row | non-null bounded args + bounded exhausted SKIP LOCKED reaper | worker batch <= hard ceiling |
| domain mutation cannot lose external intent | same DB transaction inserts outbox | service transaction owns mutation+intent |
| external binding null resource scope still unique | `UNIQUE NULLS NOT DISTINCT` | worker desired-state upsert |

---

## 5. Index matrix

Indexes below are logical candidates; exact names/order are DDL-review items.

`cleaning_assignment` UUID equality + range-overlap exclusion requires `btree_gist` extension; the exclusion constraint itself supplies the GiST index, so no duplicate Assignment GiST index is added.

| Table | Required / likely index | Purpose |
|---|---|---|
| `organization_member` | `(organization_id, membership_status, membership_role)` | active member authorization/list |
| `organization_member` | `(party_id, membership_status)` | organizations accessible to user |
| `property` | `(organization_id, active)` | tenant-scoped Property lookup |
| `rental_unit` | `(property_id, active)` | unit lookup |
| `external_identity` | active provider-user / provider-chat partial uniques | inbound actor resolution |
| `cleaner_property_roster` | `(property_id, roster_status, offer_tier, priority_within_tier)` | candidate discovery |
| `cleaner_property_roster` | `(cleaner_party_id, roster_status)` | Cleaner eligibility lookup |
| `cleaner_schedule_block` | active partial range index/GiST on Cleaner where `cancelled_at IS NULL` | raw-slot availability overlap |
| `reservation` | source external identity unique | ingest resolution |
| `reservation` | `(property_id, check_out_at)` | operational upcoming lookup |
| `cleaning_job` | `(reservation_id, schedule_source_type, cleaning_status)` | checkout-driven Cleaning resolution |
| `cleaning_job` | `(property_id, cleaning_status)` | operations |
| `cleaning_schedule_revision` | **no extra latest index**; `UNIQUE(cleaning_id, revision_no)` supplies ordered prefix | latest/history without redundant index |
| `cleaning_offer_campaign` | partial `(cleaning_id)` where OPEN | current campaign |
| `cleaning_offer_campaign` | `(acceptance_cutoff_at)` where OPEN | stale campaign maintenance/queries |
| `cleaning_offer_candidate` | `(campaign_id, candidate_status, tier_no)` | Campaign display/accept gate |
| `cleaning_offer_candidate` | partial `(cleaner_party_id, tier_no, campaign_id)` where `candidate_status=ELIGIBLE` | `/jobs` equivalent hot query |
| `cleaning_assignment` | partial `(cleaning_id)` where HARD_BOOKED | effective Assignment |
| `cleaning_assignment` | GiST `(cleaner_party_id, busy_window)` partial HARD_BOOKED | overlap fence/query |
| `cleaning_assignment` | `(cleaner_party_id, scheduled_start_at)` | Cleaner schedule |
| `cleaner_unavailability_case` | `(cleaning_id, case_status)` | case lookup |
| `cleaner_reassignment_request` | partial `(cleaning_id)` where REQUESTED | current request |
| `cleaning_schedule_reconciliation` | partial `(cleaning_id)` where PENDING; partial `(hard_booked_assignment_id)` where PENDING | current mismatch / Assignment release lookup |
| `command_receipt` | namespaced idempotency unique | replay |
| `command_receipt` | partial source stream/event unique | transport duplicate fence |
| `domain_event` | `(aggregate_type, aggregate_id, aggregate_version)` | per-aggregate audit |
| `business_scheduled_action` | partial `(available_at, scheduled_action_id)` where status in PENDING/FAILED_RETRYABLE | keyset/claim without low-cardinality status prefix |
| `business_scheduled_action` | partial `(lease_until, scheduled_action_id)` where status=RUNNING | expired RUNNING reclaim |
| `integration_outbox` | partial `(available_at, outbox_id)` where status in PENDING/FAILED_RETRYABLE | keyset/claim |
| `integration_outbox` | partial `(lease_until, outbox_id)` where status=RUNNING | expired RUNNING reclaim |
| `integration_outbox` | `(aggregate_type, aggregate_id, outbox_status)` | reconciliation/diagnostics |
| `integration_resource_binding` | aggregate/destination/resource unique | desired-state projection binding |
| `integration_resource_binding` | destination/external-resource unique | reverse external lookup |

---

## 6. DB-role privilege matrix

Legend:

- `S` = SELECT
- `I` = INSERT
- `U` = UPDATE
- `D` = DELETE
- `L` = locking SELECT (`FOR SHARE`/`FOR UPDATE` as transaction requires)
- `Q` = queue claim/lease/status columns only
- `—` = no direct privilege

| Table group | `propertyai_app_runtime` | `propertyai_async_worker` | `propertyai_readonly` | `propertyai_migrator` |
|---|---|---|---|---|
| Organization/Member/Property/Unit/Party | S/I/U | S | S* | SET ROLE owner |
| Cleaner profile/identity/roster/block | S/I/U/L | S | S* | SET ROLE owner |
| Reservation/Cleaning root | S/I/U/L with revision-pointer column excluded | S | S | SET ROLE owner |
| Schedule revision | S + EXECUTE append-revision; **no generic I/U** | S | S | owner SET ROLE only |
| Campaign/Candidate | S/I/U/L subject to transition guards/column boundaries | S | S | SET ROLE owner |
| Assignment | S/I + UPDATE terminal columns only; no provenance/slot/fee UPDATE | S | S | SET ROLE owner |
| Unavailable/Reassignment/Reconciliation | S/I/U/L subject to exact binding/CAS guards | S | S | SET ROLE owner |
| Command receipt | S/I only; no U/D | S | masked S* | SET ROLE owner |
| Authority epoch | `EXECUTE lock_and_verify...`; optional plain S, **no U** | S | S | owner SET ROLE only |
| Domain event | S/I only; no U/D | S | S | SET ROLE owner |
| Scheduled action | S + column-restricted I + EXECUTE cancel; **no generic U** | S + EXECUTE claim/complete/fail/reconcile; **no generic U** | S | owner SET ROLE only |
| Integration outbox | S + column-restricted I + EXECUTE cancel; **no generic U** | S + EXECUTE claim/complete/fail/reconcile; **no generic U** | S | owner SET ROLE only |
| Resource binding | S | S/I/U | S | SET ROLE owner |

`S*` means production views may mask or omit sensitive transport/authentication fields where operational support does not need raw values. Readonly receives the masked view, not simultaneous base-table SELECT for the same sensitive data.

Migrator boundary:

- `propertyai_owner` and `propertyai_migrator` are NOLOGIN
- canonical deployment principal `propertyai_flyway` is LOGIN/NOINHERIT and may SET `propertyai_migrator` only (`INHERIT FALSE`, `SET TRUE`, `ADMIN FALSE`)
- `propertyai_migrator` may SET `propertyai_owner` with the same non-inherited/non-admin options
- the exact direct sets are owner <- migrator and migrator <- Flyway; bootstrap/preflight reject duplicates, unexpected direct members, wrong PG18 membership options/grantors, and unexpected recursive SET-enabled paths
- privileged bootstrap and Flyway migration are separate; preflight fails closed on role/membership/schema-owner drift
- app runtime / async worker are not owner/migrator members
- Flyway authenticates as `propertyai_flyway`; JDBC startup `role=propertyai_owner` covers every physical connection and nontransactional `afterConnect` reasserts/verifies `session_user` and `current_user`
- `defaultSchema=propertyai`; the only history table is `propertyai.flyway_schema_history`; migrations do not `RESET ROLE`
- `M` is documentation shorthand only; PostgreSQL has no generic DDL privilege independent of ownership/role membership

### Important privilege principle

The application role is not prevented from business workflow because the backend owns the workflow.

However, column-level privileges or reviewed repositories should prevent accidental edits to logically immutable fields such as:

- historical schedule revision content
- Assignment slot/provenance/fee snapshots after creation
- Command receipt after successful insert
- Domain event after insert

DDL review should choose the simplest enforceable mechanism among:

1. table privilege + dedicated update repository/query that touches only mutable columns
2. column-level UPDATE privileges where practical
3. narrow trigger/helper only when privilege/constraint cannot protect a hard invariant

Do not create stored-procedure workflow merely to hide all tables from the backend.

V2.2.1 narrow DB primitives/guards are limited to:

- authority epoch lock+verify
- queue claim/complete/fail/reconcile
- Candidate proposal/status OLD→NEW guard
- Schedule revision append/current-pointer primitive
- Campaign tier/status guard
- Reconciliation target/case-version/status guard
- Assignment OFFER_ACCEPTED insert provenance validator
- unconditional busy-window derivation trigger

---

## 7. Aggregate lock matrix

| Operation family | Parent lock order | Child lock/CAS |
|---|---|---|
| Accept Offer | epoch lock/verify function → Cleaner → Cleaning → Campaign/Candidate | proposal version + candidate status |
| Decline Offer | epoch SHARE if scheduling authority changes → Cleaner/Campaign as needed | proposal version/status CAS |
| Roster / Identity / Schedule Block mutation | epoch SHARE when relevant → Cleaner | current row/version |
| Checkout change | epoch lock/verify function → Reservation → Cleaning | source version + monotonic revision guard |
| Manual Cleaning schedule change | epoch SHARE → Cleaning | current revision CAS |
| Record unavailable | epoch SHARE → Cleaner → Cleaning → Assignment | Assignment HARD_BOOKED predicate |
| Reassign original Cleaner | epoch SHARE → Cleaner → Cleaning | current request/revision/proposal predicates |
| Resolve reconciliation | epoch SHARE → Cleaner if rebooking → Cleaning | case version + assignment + target revision |
| Queue claim | queue function uses `SKIP LOCKED` | lease fence + owner enforced inside primitive |
| Cutover epoch increment | UPDATE epoch row | waits for existing scheduling SHARE locks |

Global deterministic order when multiple parents are involved:

1. authority epoch
2. Cleaner Party IDs sorted
3. Reservation IDs if applicable
4. Cleaning IDs sorted
5. child rows

Final DDL/application review should keep one canonical documented order to avoid deadlock drift across Java/Python implementations.

---

## 8. Multi-host model decision

### Chosen model

```text
Organization 1 ── N OrganizationMember N ── 1 Party
Organization 1 ── N Property
```

Example:

```text
Organization: HOST-GROUP-A
  OWNER    Park
  OWNER    Partner A
  OPERATOR Manager B

  Property JJ
  Property Design House
```

This supports:

- one host today
- spouse/co-owner tomorrow
- staff/operator later
- multiple Properties under same operating group
- one person participating in more than one Organization if needed

### Intentionally not implemented now

```text
Property N ── N Organization
```

Reason:

- it creates ambiguity over which Organization owns operational decisions, billing, policies, staff roster, and audit authority
- the current requirement is multiple people/hosts participating, not multiple independent tenants jointly owning one operational ledger

If that requirement genuinely appears later, add a dedicated Property-Organization authority relation with explicit authority type rather than prematurely complicating V2.2.1.

Tenant isolation statement:

- V2.2.1 trusts Backend authorization for Organization membership boundaries; it does not claim RLS or per-tenant DB credentials
- `property.organization_id` is immutable in normal runtime
- downstream tables derive Organization through Property; `organization_id` is not replicated to every scheduling table without a concrete invariant/query need

---

## 9. Matrix-level decisions frozen

```text
L1_SCHEDULE_BLOCK_BASIS=RAW_WORK_SLOT
L2_CHECKOUT_CLEANING_CARDINALITY=ONE_NON_CANCELLED_PER_RESERVATION
L3_BUSY_WINDOW=UNCONDITIONAL_DERIVATION_TRIGGER_FIRST_DDL
L4_BUSINESS_LOGIC=BACKEND_TRANSACTION_SERVICE
L4_DB_LOGIC=DECLARATIVE_INVARIANTS_FIRST_MINIMAL_HELPERS_ONLY
MULTI_HOST_MODEL=ORGANIZATION_PLUS_MULTIPLE_MEMBERS
PROPERTY_OPERATIONAL_ORGANIZATION_CARDINALITY=MANY_PROPERTIES_TO_ONE_ORGANIZATION
DB_SCHEMA_OWNER_CARDINALITY=ONE_NOLOGIN_ROLE
CORE_TABLE_COUNT=24
```

---

## 10. V2.2.1 bounded closure of focused findings

```text
F-V22-01_EXACT_COMPOSITE_FK_MAP=CLOSED
F-V22-02_COUPLED_CAS_TRANSITION_GUARDS=CLOSED
F-V22-03_EPOCH_QUEUE_MIGRATOR_PRIVILEGES=CLOSED
F-V22-04_NULLABLE_SOURCE_ACTOR_BINDING=CLOSED
F-V22-05_ASSIGNMENT_ACCEPTED_PROPOSAL_PROVENANCE=CLOSED
CORE_TABLE_COUNT=24
MISSING_CORE_TABLE=NONE
```

### Exact composite FK target-key map

| Parent table | Non-partial UNIQUE candidate key | Principal child use |
|---|---|---|
| `reservation` | `(reservation_id, property_id)` | Cleaning Reservation→Property consistency |
| `reservation` | `(reservation_id, rental_unit_id)` | Cleaning Reservation→Unit consistency |
| `cleaning_schedule_revision` | `(cleaning_id, schedule_revision_id)` | Campaign/Assignment/Reconciliation target revision |
| `cleaning_offer_campaign` | `(campaign_id, cleaning_id, schedule_revision_id)` | Assignment Offer provenance |
| `cleaning_offer_candidate` | `(campaign_id, offer_candidate_id, cleaner_party_id, proposal_version)` | Assignment exact accepted proposal version |
| `cleaning_assignment` | `(assignment_id, cleaning_id, schedule_revision_id)` | Reconciliation base Assignment |
| `cleaning_assignment` | `(assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id)` | Unavailability exact binding |
| `cleaner_unavailability_case` | `(unavailability_id, cleaning_id, cleaner_party_id, original_assignment_id)` | Reassignment exact case binding |
| `external_identity` | `(external_identity_id, party_id)` | Command actor identity binding |

### Transition-guard map

| Table | Guarded OLD→NEW invariant |
|---|---|
| Candidate | proposal fields change iff version +1; only ELIGIBLE; terminal freeze |
| Cleaning/Revision | narrow append primitive inserts exact next revision and advances pointer atomically |
| Campaign | tier floor non-decreasing; OPEN terminal close only |
| Reconciliation | target change iff case version +1; terminal freeze |
| Assignment | immutable provenance/slot/fee; only terminal columns mutable |

### Focused re-review gate

The next review must inspect only F-V22-01~05 closure and return:

```text
SAFE_TO_WRITE_V2_DDL=YES/NO
```

No full-system architecture review is needed. DDL remains blocked until this focused gate passes.

---

## 11. DDL major-rework closure

```text
F-DDL-01_NORMAL_ACCEPT_DEFERRED_PROVENANCE=CLOSED
F-DDL-02_APP_QUEUE_STATE_FORGING=CLOSED
F-DDL-03_BOUNDED_NONBLOCKING_QUEUE_CLAIM=CLOSED
F-DDL-04_CHECKOUT_REVISION_SOURCE_BINDING=CLOSED
F-DDL-05_NON_SUPERUSER_FLYWAY_PATH=CLOSED
F-DDL-06_ACTUAL_FLYWAY_HISTORY_PATH=CLOSED
F-DDL-07_FAIL_CLOSED_MEMBERSHIP_GRAPH=CLOSED
CORE_TABLE_COUNT=24
```

No new core table is introduced by this DDL rework.
