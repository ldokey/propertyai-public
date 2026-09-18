# W3-B operational readiness contract

Base: `9d85d8c84e1756e174cec1836a2719cdf7a6ab4c`, tree
`1657a4c87171521f3ebfda5aba469b3d5e1a7cc5`. Plan v2.1; W3-B only.

## Ownership

W3_B_WRITE: `propertyai_core/rent_runtime/`, `propertyai_core/tests/rent_runtime/`,
`docs/rent_runtime/`. All are additive. W3_A_READ_ONLY: its recurring billing worker
source, which is deliberately neither inspected nor imported here. SHARED_READ_ONLY:
all accepted Finance/domain/repository/Web/template code, `pyproject.toml`, `uv.lock`,
bootstrap and migrations. I3_INTEGRATION_OWNER_ONLY: shared composition changes,
accepted W3-A checkpoint consumption, exact worker entrypoint binding and activation.

## Package and identities

`artifact.build` reads Git blobs from an exact commit, never dirty bytes. Candidate
rehearsals explicitly use `CANDIDATE_TREE_ONLY`, a null source commit, the frozen
candidate tree and its base. They are not represented as final commit artifacts.
After one checkpoint, rebuild with its full commit hash and revalidate the artifact.

The canonical JSON format is UTF-8, sorted keys, comma/colon separators, no newline.
Payload identity hashes the sorted list of path, byte length and SHA-256 for every
included file. The manifest is excluded from this hash to avoid circular identity.
The deterministic uncompressed USTAR archive includes this manifest and is separately
SHA-256 bound by the build receipt. Tar metadata has zero time/UID/GID and fixed
permissions. Timestamp is non-authoritative sidecar metadata. Config bytes have a
separate external hash and must never be added to the archive. Materialization requires
both the expected archive hash and manifest hash; runtime requires the latter.

Included: tracked non-test PropertyAI Python, Web templates/static, exact SQL/bootstrap/
Flyway config, `pyproject.toml` and `uv.lock`. Tests, fixtures, secrets, provider config,
credentials, runtime state and evidence are not package payload. Dependencies use
`uv sync --frozen --no-dev`; the rehearsal additionally requires `--offline` and uses
an isolated virtual environment. Python must equal the manifest's exact 3.12 runtime.
The lock, source tree, artifact and external configuration identities are distinct.

## Processes and environment boundary

Only `ISOLATED_TEST` is executable now. LocalTarget requires the accepted owned-fixture
marker hash, UID/inode/0700 root, `/tmp/pa-stage-a-*` canonical root, matching postmaster
Unix socket/port and empty listen_addresses. No arbitrary host, DSN, real secret,
provider selection, public bind or Production approval is inferred from these flags.
External application requires a later approved adapter/target binding, not changing an
example value. No data or session is issued by normal runtime startup.

WEB starts with `python -m propertyai_core.rent_runtime web --config <external-file>
--config-sha256 <hash> --manifest-sha256 <hash> --state <owned-root/w3b-web-unique.json>`.
The external file is private and has exactly the keys validated by `load_config`.
Business date composition uses the existing injected provider with the property's
ZoneInfo timezone; no machine-local date assumption or Finance semantic change.
It contains an explicitly synthetic server-verified principal, distinct least-privilege
rent and durable-session logins, and the owned local target reference. There is no
HTTP login/IdP provisioner. Provisioning a test session is a separate test-only action.

Startup timeout: 15 seconds. Stop: SIGTERM only to the caller-owned process, then wait
up to 10 seconds and prove process-group absence; forced cleanup is not a successful
graceful stop. HTTP socket timeout 5s; DB connect timeout 3s and statement timeout 5s.
Exit 0 means clean stop; 78 is fail-closed config/package/worker refusal. Liveness is
`/health/live`. Readiness is `/health/ready`, not a substitute for liveness: it verifies
exact migration history/checksums and both DB role prerequisites. DB outage, migration
mismatch, invalid config or incomplete restore cannot become READY or an empty UI.
Scheduler is OFF with no enabling path in this bundle.

MIGRATOR is the approved external Flyway 13.5.0 distribution, not a newly downloaded
binary or a Python migration substitute. `migrator_plan` verifies package SQL and the
frozen executable hash and returns its explicit command. The executor must attest and
own the loopback-to-exact-disposable-Unix-socket bridge. The changed tests reuse the
accepted bridge/lifecycle, isolated HOME/TMP and a 60-second owned-process timeout.
The plan itself does not open a network connection or execute. `migrate`/`validate`
exit 0 is completion; nonzero requires effect classification and validation before
retry. Login is propertyai_flyway, effective role propertyai_owner, callbacks included.
There is no scheduler or externally activated migrator service.

I3 binds WORKER exactly to
`propertyai_core.rent_recurring_billing.run_recurring_billing_once` version 1 from the
accepted W3-A checkpoint `7fe5e7479bfd862a60ae4ef4f81490ede407481d`. The runtime
surface remains RECURRING_BILLING_CALLER with effect CREATE_ELIGIBLE_RECEIVABLE_ONLY and
DB role propertyai_rent_scheduler. The executable is an explicit single-run process only;
there is no daemon or automatic trigger. Its private external config binds one owned
ISOLATED_TEST target, organization, scheduler login and bounded W3-A page/retry limits.
Optional cursor continuation is supplied through a private hash-bound cursor file.
Scheduler default remains OFF and automatic_scheduler_activated is always false.

The worker caller timeout is 60 seconds. Exit 0 is final ZERO_TARGET_NOOP,
DRY_RUN_SUCCESS or COMPLETED; exit 3 requires explicit cursor continuation; exit 4 is a
completed scan with contract failures; exit 75 is UNKNOWN_EFFECT; exit 78 is CONFIG_ERROR;
and exit 70 is RUNTIME_ERROR. The process envelope retains the complete accepted W3-A
result so continuation, contract failures and unknown effects are not collapsed. The
manifest binds the exact W3-A callable and checkpoint plus the frozen W3-B checkpoint
identity and common I2 base. I3 changes no W3-A source bytes.

## Observability and recovery

JSON events contain only UTC timestamp, process role, generated correlation UUID,
closed-enum result class and sanitized error class. No free-form exception, URL,
headers, token, principal, account identifier, password, or provider metadata is logged.
Signals include DB/health failure, unbound worker failure, backup failure, missing/stale/
invalid backup evidence and restore failure. Worker run signals must be bound to the
accepted worker at I3; there is no claim of an actual worker-run rehearsal here.

Backup: an explicit owned local source, fresh exclusive directory and custom-format
pg_dump. A repeatable-read exported snapshot binds the logical data oracle and dump.
The receipt hashes the archive, tool, all table rows, schema/constraints/grants, role
prerequisites and sequence state. A successful backup does not assert restore success.
The retained archive includes synthetic sensitive session state, so even rehearsal
artifacts are private. Production encryption/access controls/offsite storage are pending.

Restore: different clean cluster, externally hashed receipt, intact archive and exact
pre-provisioned nonprivileged role graph—including the TEST scheduler login once I3 binds
the worker—and exact extension versions (including the source-required btree_gist).
Missing role or extension prerequisites fail before restore starts.
No implicit role/extension provisioning or --clean.
pg_restore is single-transaction, exit-on-error, timeout bounded. A pending marker
prevents Web readiness until restore and logical equality succeed. ACL comparison normalizes SQL NULL to the
catalog's effective default ACL: an explicit owner-only grant and the equivalent
default are equal, while added PUBLIC grants are detected (negative test included).
V112's exact ck_rent_auth_subject deparser forms `(A AND B) AND C` and `A AND B AND C`
are normalized by a finite exact table/name/expression mapping. Other predicates,
changed limits, names and validation states remain distinct; negative tests enforce
this. No general SQL parenthesis stripping or schema changes are performed.
Failure after start
remains FAILED_UNKNOWN_EFFECT until readback; no rollback is claimed without evidence.
The test intentionally truncates an archive, observes failure, proves absent schema and
retained readiness guard, and cleans only the owned target. Representative durable
facts include acknowledged command results, payment/allocation balance and correction
history, active/revoked/expired sessions and session revocation after restore.

Clean-empty means every non-admin business table has zero rows before and after normal
startup. Allowed bootstrap tables: migration history, the immutable migration-seeded
authority_epoch control row (not a business fixture), organization/party/admin membership,
ledger scope, runtime login binding and auth session. No property/unit/resident/account/
contract/receivable/payment fixtures are allowed. Test-only admin and finance setup live
under the excluded test directory and require an owned disposable cluster.

Actions distinguish NOT_STARTED, FAILED_PRE_EFFECT, FAILED_UNKNOWN_EFFECT, COMPLETED
and RECOVERED. A command invocation alone is not proof of artifact creation or recovery.
Source/local evidence is reusable only across exact unchanged tree/payload bindings.

## Remaining external decisions (not blocking this local bundle)

OPERATIONAL_READINESS = CONDITIONAL_EXTERNAL_DECISIONS. No final Production RPO/RTO is
selected. Saved-record loss remains unacceptable as a user preference; a periodic local
backup alone cannot establish zero-loss recovery. Production must separately decide
failure scope, WAL/PITR or synchronous durability/replication, encryption, offsite retention,
restore authorization, monitoring/notification delivery, IdP, hosting and target binding.
The approximate monthly budget remains KRW 50,000 excluding separately scoped AI costs;
no provider or billable resource is chosen. Timings in receipts measure only the named
local action, not end-to-end service recovery or a Production SLA. Freshness tests use
an explicit 60-second test window; it is not a Production backup cadence or RPO.

## Changed-based verification

Run only the new operational tests, with explicit evidence and approved staging/provenance
binding environment. `pytest --confcutdir=propertyai_core/tests/rent_runtime
propertyai_core/tests/rent_runtime` deliberately does not import the unrelated root
Global Writer test shim. Rent read/write test paths have no Global Writer imports;
this is an owned disposable DB test, not a lease/Production authorization bypass.
Reuse I2 functionality evidence; do not rerun the complete legacy suite. After all pass,
freeze tree/diff/evidence, create at most one local checkpoint, rebuild from that exact
commit, and verify clean artifact startup. No push, merge, I3, W4 or external deployment.
