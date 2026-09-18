# Post-cutover Batch A failure contract

Change: `PROPERTYAI-POST-CUTOVER-TEST-HARDENING-01`, Batch A only.
Frozen findings: TSA-01, TSA-02, TSA-04, PCR-01, PCR-02, PCR-03.
Predecessor TK-43 / DL-98 remains closed. This source candidate is not a deployment
or a new Production authorization, and Maker evidence is not independent acceptance.

## 1. Ownership and state machine

Cleaner Application still owns business meaning. Existing PostgreSQL uniqueness,
transactions and outbox functions own durable state and fence predicates. Product
owns delivery orchestration and evidence classification. Existing global writer
admission remains mandatory. No SQL migration, grant, role, epoch or authority-store
change is required by this candidate.

A new W07 delivery commits an uncertainty marker **before** calling a provider:

```
PENDING / permitted FAILED_RETRYABLE
  -> claim (RUNNING, increment attempt and lease_fence)
  -> exact claim/owner/fence CAS, COMMIT PENDING_RECONCILIATION
  -> provider adapter
  -> exact pending-row/fence CAS, COMMIT SUCCEEDED
```

The pre-provider marker is `DELIVERY_INTENT_UNRESOLVED`. A pending row cannot be
claimed. SIGKILL, an ambiguous provider acknowledgement, or process loss after
an external effect therefore cannot leave a reclaimable send behind. Errors after
crossing the provider boundary never schedule a blind retry.

A reclaimed historical RUNNING row might have been sent by the older worker.
Attempt greater than one is consequently quarantined with
`RECLAIMED_OUTCOME_REQUIRES_EVIDENCE`, unless the current row carries an exact,
single-use next-attempt/next-fence no-effect permit produced by the evidence path.
A crash after consuming that permit cannot carry it across another reclaim.

Validation failures before adapter entry use the existing fenced failure function.
They do not fabricate provider evidence. Any later attempt still needs the same
conservative retry admission. An exhausted no-effect attempt becomes DEAD_LETTER.

The worker does not write after losing global writer authority. Its durable intent
already owns the unresolved diagnostic. A known receipt can be returned in the
worker result, but is not persisted through an unfenced emergency update. After a
lost resolution-commit acknowledgement, an exact **read-only** full-row readback
can establish `COMPLETED_BY_DURABLE_READBACK`; it never authorizes another send.

### Existing destination adapter compatibility

`ProjectionDeliveryReceipt` and `DeliveryReceipt` implement the same port fields.
A non-empty exact receipt identifies an effect. The two existing accepted Calendar
no-op cases return `confirmed_no_effect=True` with no receipt; a missing receipt
without this explicit classification remains unresolved. No fake provider effect
identifier is manufactured to make a legitimate no-op succeed.

## 2. General reconciliation, not cutover replay

The reusable entrypoint is `GeneralOutboxReconciliation.resolve`. It accepts one
exact outbox UUID, expected durable attempt, expected lease fence, a bounded
nonsecret diagnostic owner, and an explicit retry authorization (default false).
It is called through the existing W07 source/config/global-writer boundary with
a read-only `ProviderEvidencePort`; it never calls a send method.

| Provider evidence | Allowed result |
| --- | --- |
| CONFIRMED_EFFECT | Exact matching receipt, current row and current fence: SUCCEEDED without send |
| CONFIRMED_NO_EFFECT | No automatic mutation. Explicit retry requires complete positive absence proof and exclusion of in-flight/delayed effects; next attempt/fence only, or DEAD_LETTER at limit |
| AMBIGUOUS | Leave pending; no send |
| PROVIDER_UNAVAILABLE | Leave pending; no send; unavailability is not absence |
| CONFLICTING_EVIDENCE | Leave pending; no send; investigate exact business/effect identity |

The evidence binds the complete observed row: business/outbox/event identity,
destination, semantic key/payload, durable status, attempt, fence and prior receipt.
Observation time must be timezone-aware, nonfuture and within the configured age
window. Wrong/stale rows, malformed proofs and conflicting receipts fail closed.
The service checks live writer authority and the complete row again before CAS.
The repository returns the exact durable readback in the same transaction.

The caller must retain the structured result, evidence reference and evidence
hash in its existing authorized audit/evidence channel. Applied resolutions also
persist the evidence digest and diagnostic owner in the existing `last_error_code`
field. Ambiguous diagnostic observations intentionally do not mutate the business
row. An existing completed row is not re-resolved by repeating this operation.

### Operational use and evidence trust

An authorized recovery caller first diagnoses the exact pending row and obtains
provider-specific readback through an already trusted adapter. It invokes the
public service API with that exact expectation, then records the returned state
and evidence digest. A changed expectation requires a fresh diagnosis, not a CAS
retry loop. This is an explicit recovery operation, not an automatic background
resender and not a normal-startup prerequisite.

Provider adapters must establish that a receipt actually belongs to the exact
business/destination/effect identity. A missing object, a timeout, an incomplete
search or an operator assertion is **not** complete no-effect proof. All delayed
or in-flight effects must be excluded before absence can authorize retry. This
candidate does not introduce a JSON evidence ingestion endpoint, accept arbitrary
operator-supplied assertions as provider proof, or add a new external trust store.

Tests use a durable, quiescent, test-owned ledger which records every invocation
without deduplication. That ledger can prove complete absence after its child is
reaped. This does not assert that a real provider has the same capability. In
particular, the accepted Telegram adapter's stricter prior-attempt rejection is
retained; no unsupported Telegram absence proof or resend capability is invented.
Production recovery/adoption remains subject to its existing authorization.

### Why no new SQL or FOR UPDATE grant is necessary

The async worker role cannot directly UPDATE outbox rows. The repository first
checks the full row, then uses the existing atomic pending-status/fence predicate.
Through the accepted runtime capability surface, leaving pending either makes
that predicate fail or requires a new claim/fence before entering pending again.
Identity/attempt cannot change while remaining pending at the same fence. The
full-row expectation plus existing CAS therefore excludes same-fence pending ABA
without granting FOR UPDATE/direct business UPDATE privileges. Arbitrary admin
writes are outside this trust boundary, not silently accommodated.

## 3. Normal startup versus one-time finalization

`cleaner_pg_outbox_service.main` is NORMAL_STEADY_STATE_STARTUP. It validates current
POSTGRES topology/authority, strict DB login/role/ACLs, current source/config/process
identity, then enters the ordinary worker loop. Empty queues are valid. Provider
unavailability is a work-item outcome, not historical-cutover admission authority.

The explicit `cleaner_projection_reconciliation` one-time finalization command is
retained unchanged. Normal W07 main neither imports it nor invokes it. Historical
operation IDs, consumed flags, fixed business rows and obsolete source anchors are
not consulted by ordinary startup. A leftover historical environment value cannot
authorize replay. No Production W07 restart is performed by this tranche.

## 4. Expected semantic topology and negative ownership

| Writer | Post-cutover responsibility | Activation/authority constraints |
| --- | --- | --- |
| W01 | Gmail ingestion routed to the PG Application | Current route/source/config and global writer; PG failure cannot select legacy fallback |
| W03 | Cleaner Telegram ingress routed to the PG Application | Missing PG service fails before credentials/legacy dispatch |
| W04 | Retired Cleaner operations dispatcher | Post-cutover activation forbidden before credential reads and at coordinator reauthorization |
| W05 | Retired Cleaner completion dispatcher | Same negative guard as W04 |
| W06 | Health/recovery, not Cleaner business authority | Current topology recovery targets must not resurrect W04/W05 |
| W07 | PG outbox delivery and explicit evidence recovery | Strict worker-role/source/config/global-writer admission |

The canonical topology contract describes semantic service/recovery ownership,
not transient PIDs. Post-cutover expected labels include telegram-cleaner,
gmail-readonly, health-monitor and cleaner-pg-outbox. Scheduled services need not
be continuously running. Retired W04/W05 labels are not recovery targets.

The new legacy guard is negative only: either an explicit POSTGRES authority or
POST_CUTOVER_PG topology prohibits W04/W05. Contradictory/malformed tuples cannot
activate fallback. Cached coordinators recheck this restriction. Other services
are not rejected by this narrow guard and still require their original authority.
Missing pre-cutover settings preserve existing compatibility; they do not override
the independent live source/config admission. No legacy source is deleted.

## 5. One shared, owned failure harness

`batch_a_failure_harness.py` extends the accepted repository PostgreSQL fixture
with explicit fsync-on operation, owned per-case schema10 DCS, exact installed thin
client, actual OS process/source/config admission, deterministic checkpoints,
SIGKILL/restart, real PostgreSQL lock-wait observation, and a durable fake provider.
All DB connections identify a nonce-owned Unix-socket cluster. Test children get a
sanitized environment, and non-Unix Python network connections are forbidden.
The DCS schema is created with accepted Controller migration source on a NEW owned
file; no Production DCS is opened. No heartbeat or lease bypass is used: the real
thin-client expiry/reclaim contract is exercised with an explicit test clock.

Startup tests exercise actual build_worker, strict pool admission, main, runtime
identity, health observer and one-cycle loop. Only the DB transport and provider
clients are test-owned substitutions. PostgreSQL restarts are immediate process
stops followed by real restart with all durability settings still on. This proves
process-crash durability, not arbitrary hardware/power-loss guarantees.

Concurrency tests hold the winning transaction open until PostgreSQL reports the
loser blocked by that backend, then assert durable identity/count/fence results.
The claim case executes SKIP LOCKED while the winning transaction still owns its
row. Launching threads alone is not treated as overlap evidence.

Cleanup reaps each owned child, closes pools and ledger connections, checks DCS
integrity/foreign keys, stops the owned PG instance, verifies PID/socket removal,
and removes only its exact root. Failure/timeout/unknown cleanup is not PASS.
Raw logs and structured observations remain outside source worktrees.

Run the new nodes through the repository's normal pytest path with the accepted
thin-client environment and exact accepted Controller source/interpreter supplied
via `BATCH_A_CONTROLLER_ROOT` and `BATCH_A_CONTROLLER_PYTHON`. Set
`PROPERTYAI_FLYWAY_BIN` to the accepted Flyway executable, and
`BATCH_A_EVIDENCE_DIR` to an owned output directory. The session's external
`run_tests.py` records the exact sanitized invocation and JUnit/log identities.
No accepted LT02 profile yet contains these new nodes; no runner bypass or profile
claim is made. The small memory port supports FAST tests only and is not crash,
concurrency or durability evidence.
