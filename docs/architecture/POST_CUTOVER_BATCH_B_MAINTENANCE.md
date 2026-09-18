# Post-cutover maintenance boundaries — Batch B

Change: PROPERTYAI-POST-CUTOVER-TEST-HARDENING-01 / BATCH_B.
Product predecessor: `050bb8f80b025a101dbbcb29fab6e381472b4857`.
LT-02 predecessor: `05e0ae9263fed3d57235bc8a569da7fc47b4d637`.
Read-only Controller source: `3591fb815281cf1e304ed732ef1c0cab325ed1a9`.

This is a source responsibility inventory, not certification of live services,
authorization of effects, source acceptance or Production tuning. A historical
anchor proves one accepted contract; it does not require every future runtime to
remain on that commit.

## Source path and history boundaries

| Classification | Boundary | Evidence / maintenance rule |
| --- | --- | --- |
| ACTIVE_RUNTIME_PATH | `propertyai_core/application/cleaner_pg.py`, `propertyai_core/adapters/postgres/`, current W07 worker and projection adapters/clients in `propertyai_core/runtime/`, `gmail_ingest/postgres_ingress.py` and the PG route in `gmail_ingest/booking_bridge.py` | Keep current PG authority, durable receipts, exact outbox/recipient/resource bindings and worker source binding. `test_production_worker.py`, `test_cleaner_projection_contract.py`, `test_cleaner_projection_clients.py` and Batch A current runtime tests cover these paths. |
| ACTIVE_RUNTIME_PATH | `propertyai_core/global_writer.py`, current authority/topology checks and W07 fencing | Lease exclusivity, stale-fence rejection, idempotency and recovery latency remain safety boundaries. Low useful-work rate is not permission to omit them. Preserve active non-Cleaner services. |
| HISTORICAL_CUTOVER_ONLY_PATH | First-PG-command selection and fresh-source first-command paths in `gmail_ingest/booking_bridge.py`; original cutover materialization/bootstrap evidence | Retain exact source-selection safeguards and evidence. Do not replay after cutover or make ordinary W07 startup reconstruct one-time cutover objects. Historical tests remain in source, but are not permanent mandatory R9 current-runtime nodes. |
| RECOVERY_ONLY_PATH | `propertyai_core/runtime/cleaner_projection_reconciliation.py`, `propertyai_core/runtime/outbox_reconciliation.py`, fenced worker reconciliation and durable projection receipts | Ambiguous external effects require evidence, not blind replay. Read-only diagnosis cannot authorize a resolve/retry/write. Keep stale-attempt/fence and unknown-effect tests. |
| DEPRECATED_BUT_RETAINED_PATH | Retired Cleaner legacy writer entry points and cached legacy coordinators covered by `test_post_cutover_batch_a.py` | Keep retirement/topology guards. Retained source does not mean an active writer. Fail before credentials, writer acquisition or external effects when retired. Do not remove unrelated active services. |
| REMOVAL_CANDIDATE_REQUIRING_FUTURE_EVIDENCE | One-time cutover-only invocation glue and obsolete source-anchor special cases after individual owner/caller inventory | No deletion in Batch B. Removal needs an accepted successor, caller inventory, an agreed observation period, recovery-impact review, retained historical evidence and independent negative tests. Absence of observed traffic alone is insufficient. |

A mixed module may contain active routing and historical first-command selection.
Classify functions and call paths; do not delete a whole mixed module based on one
historical function. Current behavior is established by source and required
nodes, not filenames or stale issue labels.

## LT-02 current and historical contracts

The historical `cleaner.pg` profile keeps its R1–R7 contract and original R8/R9
unresolved interpretation. The successor `cleaner.pg.post-cutover` profile adds
exact semantic-outbox, fixture lifecycle, current runtime, reconciliation,
provider and routing nodes. It binds the accepted Batch A predecessor, current
candidate contents, catalog/rules, exact interpreter/cwd/argv, Controller source
and thin-client 0.5.0 artifact hash. A material binding change requires a new plan.

R8_CURRENT is narrowly the supported Airbnb W01 normalizer, provider business key,
explicit PG versus LEGACY routing and no legacy fallback. It does not claim a
normalizer for other providers. R9_CURRENT is represented by accepted Batch A
current-successor tests: normal W07 startup, crash/restart, durability, concurrent
claims/commands/reconciliation, topology guards and provider uncertainty safety.
The first-command selection tests are retained but excluded from that current
runtime requirement. Changes to historical selection source still require its
own relevant tests; do not use this profile to claim every historical path passed.

Unsupported path/profile requests remain unresolved; there is no catch-all
fallback. Required MISSING, SKIP, XFAIL, NOT_COLLECTED, TIMEOUT, INFRA_ERROR,
INCOMPLETE or SEMANTIC_BINDING_DRIFT cannot become PLAN_PASS. A green shell exit
without complete required-node and cleanup evidence is insufficient.

## Read-only diagnostic boundary

DX `dev_control/post_cutover_diagnostics.py` consumes the accepted Controller's
read-only connection, schema/profile and writer validation primitives. It does
not construct ControlStore, migrate, acquire a lease or decide authority. There
is no default Production DCS path. DCS reads require both an explicit path and
`--authorize-dcs-read`; this switch expresses caller intent, not a replacement
for operational authorization. Development validation uses owned disposable DCS.

DCS fields share one WAL-visible read transaction. Git facts carry their separate
observation times; no global cross-source atomicity is claimed. Each field retains
source and timestamp. Expected source is caller-declared intent, not acceptance.
The narrow history value is an exact W08 acquisition/takeover count for the
supplied operation, not all typed receipts. W07 process/current-bound-source and
authority mode remain UNAVAILABLE when not observed; PostgreSQL role context is
NOT_AUTHORIZED because this projection opens no PostgreSQL connection. Do not
fill these gaps from memory. `validate_projection` checks freshness/provenance,
not readiness or source acceptance. Capture a new observation before relying on
stale evidence.

## Measurable maintenance criteria — no Production tuning

Each measurement must name source, window, timestamp, scope and completeness.
Missing observations are UNKNOWN, not zero. Establish comparable complete
24-hour windows and a 7-day baseline before proposing optimizations. Avoid
credential, payload and personal-data logging solely for metrics.

| Measure | Definition | Criterion for a future bounded change |
| --- | --- | --- |
| Useful-work rate | Completed useful projections / polls, and claims / polls; retain numerator, denominator and failures separately. | A low ratio triggers analysis only. Preserve retry deadlines, recovery latency, fencing and provider safety; prove improvement on disposable workloads first. |
| Idle poll volume | Empty-work polls per hour/day, separately from lease contention, startup errors and unreadable state. | Never count unavailable control state as ordinary idle. Compare equal complete windows; do not suppress safety reads merely to lower counts. |
| Reconciliation frequency | Newly ambiguous rows, evidence reads, attempted/completed resolutions by destination/day; unresolved age distribution. | Repeated attempts without new evidence or increasing unresolved age warrant diagnosis, not blind retries or reclassification as success. |
| Control-history burden | Diagnostic query count/duration and writer-event observations per useful effect. | Distinguish historical evidence from active reads. Volume alone does not justify pruning or reducing writer safety. |
| Historical-object access | Ordinary-startup access to one-time cutover objects, excluding explicit historical audits. | Expected count under the accepted Batch A contract is zero. A nonzero count needs caller/source evidence before change. |
| Operator diagnostic effort | Manual readback steps and operator elapsed time for the same DCS/Git/runtime/receipt question, including unavailable fields and repeated reads. | Compare the same question before/after the projection. Shorter output that loses provenance or hides UNKNOWN is not improvement. |

These are measurement specifications, not claims of Production measurements.
No polling daemon, metric store, authority store, live schedule or tuning is added.
Reuse existing logs and the projection only when the read itself is authorized.

## Owned fixture evidence

Only positively owned roots and processes may be stopped or removed. Record
known ownership, termination attempted (or positively observed absence), observed
result, and residual check. Preserve unknown ownership as INCOMPLETE. Cleanup
success does not erase init/start/ready/run/timeout/stop failure history. Negative
fault injection is successful only when the expected error class and ownership /
residual proof are both asserted. Do not treat expected fault-test PASS as proof
that the injected infrastructure failure itself succeeded.
