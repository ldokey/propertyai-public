# PropertyAI Cleaner Multi-Job DB V2.2.1 — Flyway / Membership Rework Validation

Status: `PASS_CANDIDATE / FRESH_INDEPENDENT_REREVIEW_REQUIRED`

Exact source authority:

```text
BASE_HEAD=2efbde64c38aab4d543715d329b683f0796f2a55
EXPECTED_PARENT=7a74601371604f220030fea16fae87b9c0283bdf
INITIAL_WORKTREE=CLEAN
```

This bounded successor closes only F-DDL-05, F-DDL-06 and F-DDL-07. The
24-table topology and the accepted F-DDL-01 through F-DDL-04 contracts were not
redesigned. No Production mutation, cutover, deployment or active Flyway-path
switch was performed.

## 1. Actual Flyway execution path

The final checked-in path uses:

- non-secret `db/v2_2_1/flyway/flyway.conf`;
- PostgreSQL JDBC startup option `-c role=propertyai_owner` on every physical
  Flyway connection;
- non-transactional `afterConnect.sql`, which reasserts `SET ROLE
  propertyai_owner` and validates `SESSION_USER` plus `CURRENT_USER`;
- `defaultSchema=propertyai`, `schemas=propertyai`,
  `table=flyway_schema_history`, and `createSchemas=false`;
- no migration-local `RESET ROLE` in 101 through 108;
- no URL, username secret or password committed to Git;
- no deprecated `initSql`.

Flyway 13.5.0 uses dedicated callback/event connections. An actual
afterConnect-only control attempt reached the callback successfully but failed
when Flyway's separate schema-history connection tried to create the history
table. The final JDBC startup option closes that multi-connection gap, while the
afterConnect callback remains the fail-closed connection-lifecycle assertion.

The final acceptance run used the checked-in configuration without a
command-line role override:

```text
FLYWAY_VERSION=Flyway OSS Edition 13.5.0 by Redgate
POSTGRESQL_VERSION=18.6 (Homebrew)
ISOLATED_CLUSTER=/private/tmp/propertyai-v221-acceptance-pg18.BYbxmU
DATABASE=propertyai_v221_flyway_acceptance
FLYWAY_AUTHENTICATED_SESSION_USER=propertyai_flyway
FLYWAY_ACTIVE_CURRENT_USER=propertyai_owner
FLYWAY_DEFAULT_SCHEMA=propertyai
FLYWAY_SCHEMA_HISTORY_TABLE=propertyai.flyway_schema_history
FLYWAY_SCHEMA_HISTORY_OWNER=propertyai_owner
FLYWAY_HISTORY_INSTALLED_BY_SET=propertyai_owner
FLYWAY_MIGRATE_RESULT=SUCCESS
SUCCESSFUL_MIGRATION_COUNT=8
MIGRATION_20260904.101=SUCCESS
MIGRATION_20260904.102=SUCCESS
MIGRATION_20260904.103=SUCCESS
MIGRATION_20260904.104=SUCCESS
MIGRATION_20260904.105=SUCCESS
MIGRATION_20260904.106=SUCCESS
MIGRATION_20260904.107=SUCCESS
MIGRATION_20260904.108=SUCCESS
PUBLIC_SCHEMA_HISTORY_TABLE=ABSENT
SECOND_MIGRATE_UP_TO_DATE=PASS
```

Migration 101 itself rejects any session where the authenticated and active
roles are not exactly the values above, and verifies the history table owner
before later migrations run.

## 2. Fail-closed privileged membership graph

The bootstrap performs a drift check before its GRANT statements so it cannot
silently repair wrong authorized-edge options. It then performs an exact
post-GRANT assertion. Migration 101 and the review contract independently
repeat the postcondition.

The exact accepted direct sets are:

```text
OWNER_DIRECT_MEMBER_SET=propertyai_migrator[inherit=false,set=true,admin=false,grantor=superuser]
MIGRATOR_DIRECT_MEMBER_SET=propertyai_flyway[inherit=false,set=true,admin=false,grantor=superuser]
FLYWAY_DIRECT_MEMBER_SET=EMPTY
UNEXPECTED_OWNER_SET_ROLE_PATHS=0
UNEXPECTED_MIGRATOR_SET_ROLE_PATHS=0
EXPECTED_EDGE_OPTIONS=PASS
```

The assertions inspect `roleid`, `member`, `grantor`,
`inherit_option`, `set_option` and `admin_option`. They reject duplicate
rows from different grantors, any unexpected direct member, any member of
`propertyai_flyway`, and recursively any unexpected SET-enabled path to
`propertyai_owner` or `propertyai_migrator`.

All probes ran in transactions whose failing connection was closed, so injected
cluster-wide role state was rolled back:

```text
PROBE_A_EXTRA_OWNER_DIRECT_MEMBER=PASS
PROBE_B_EXTRA_MIGRATOR_LOGIN_MEMBER=PASS
PROBE_C_WRONG_EXPECTED_EDGE_OPTIONS=PASS
PROBE_D_INDIRECT_SET_PATH=PASS
UNEXPECTED_ROLES_AFTER_PROBES=0
ROLE_DRIFT_NEGATIVE_PROBE=PASS
EXTRA_DIRECT_MEMBER_NEGATIVE_PROBE=PASS
EXTRA_INDIRECT_MEMBER_NEGATIVE_PROBE=PASS
BOOTSTRAP_IDEMPOTENT_RERUN=PASS
```

## 3. Full DDL regression

`db/v2_2_1/review/ddl_contract_checks.sql` passed in a rollback-only
transaction on the final Flyway-created schema. Separate transaction/concurrency
probes covered the contracts that intentionally cannot be demonstrated in the
single rollback suite.

```text
F-DDL-01_NORMAL_ACCEPTANCE_ORDER=PASS
F-DDL-01_DEFERRED_NEGATIVE_COMMIT=PASS
F-DDL-02_APP_QUEUE_STATE_FORGING_DENIED=PASS
F-DDL-03_INVALID_CLAIM_PARAMETERS_DENIED=PASS
F-DDL-03_LOCKED_EXHAUSTED_ACTION_NONBLOCKING=PASS
F-DDL-03_LOCKED_EXHAUSTED_OUTBOX_NONBLOCKING=PASS
F-DDL-04_CHECKOUT_SOURCE_MISSING_DENIED=PASS
F-DDL-04_STALE_RESERVATION_VERSION_DENIED=PASS
F-DDL-04_EXACT_CURRENT_SOURCE_ACCEPTED=PASS
BUSY_WINDOW_DERIVATION_AND_GIST_EXCLUSION=PASS
SECURITY_DEFINER_SAFE_SEARCH_PATH=PASS
SECURITY_DEFINER_PUBLIC_EXECUTE_REVOKED=PASS
COLUMN_IMMUTABILITY_PRIVILEGES=PASS
QUEUE_STALE_OWNER_OR_FENCE_RETURNS_FALSE=PASS
QUEUE_CORRECT_OWNER_AND_FENCE_SUCCEEDS=PASS
FULL_DDL_CONTRACT_REGRESSION=PASS
```

The expected deferred failure was
`ASSIGNMENT_CANDIDATE_NOT_ACCEPTED_AT_COMMIT`. The locked exhausted-row probes
held both queue rows in another transaction for three seconds and used a
one-second statement timeout; both claim functions returned zero without
blocking.

## 4. Topology regression and scope

```text
TABLES=24
VIEWS=2
FOREIGN_KEYS=35
UNIQUE_CONSTRAINTS=31
CHECK_CONSTRAINTS=124
EXCLUSION_CONSTRAINTS=1
FUNCTIONS=27
SECURITY_DEFINER_FUNCTIONS=12
TRIGGERS=15
BTREE_GIST=1
NEW_CORE_TABLE=NO
TABLE_TOPOLOGY_CHANGE=NO
UNEXPECTED_SCOPE=NONE
PRODUCTION_MUTATION=NO
PUSH=NO
MERGE=NO
DEPLOY=NO
```

## 5. Candidate verdict

```text
F-DDL-01_REGRESSION=PASS
F-DDL-02_REGRESSION=PASS
F-DDL-03_REGRESSION=PASS
F-DDL-04_REGRESSION=PASS
F-DDL-05=PASS
F-DDL-06=PASS
F-DDL-07=PASS
ACTUAL_FLYWAY_EXECUTION=PASS
MEMBERSHIP_GRAPH=PASS
NEGATIVE_ROLE_GRAPH_PROBES=PASS
POSTGRESQL_18_EMPTY_DB=PASS
SAFE_FOR_FRESH_INDEPENDENT_REREVIEW=YES
```
