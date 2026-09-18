# PropertyAI Cleaner Multi-Job DB V2.2.1 DDL Candidate

Status: `DDL_MAJOR_REWORK_SUCCESSOR / NOT_FOR_PRODUCTION_CUTOVER`

Logical authority commit:
`c811cfa636538f2ba08f2d99ed2b6c104e57c15a`

Failed predecessor DDL commit:
`7a74601371604f220030fea16fae87b9c0283bdf`

## Deployment split

V2.2.1 deliberately separates privileged bootstrap from Flyway migrations.

1. DBA/superuser runs `bootstrap/001__privileged_roles_schema.sql`.
2. Deployment connects as the non-superuser LOGIN `propertyai_flyway`.
3. The checked-in PostgreSQL JDBC `options` property establishes
   `role=propertyai_owner` while retaining `SESSION_USER=propertyai_flyway` on
   every physical Flyway connection, including the schema-history connection.
4. Flyway discovers the non-transactional
   `flyway/callbacks/afterConnect.sql` callback, which reasserts `SET ROLE
   propertyai_owner` and fails unless the session/current-user contract is exact.
5. Flyway creates/uses `propertyai.flyway_schema_history` and applies
   `migration/V20260904.101...108`.

Role chain:

```text
propertyai_flyway LOGIN
  -- INHERIT FALSE / SET TRUE / ADMIN FALSE --> propertyai_migrator NOLOGIN
  -- INHERIT FALSE / SET TRUE / ADMIN FALSE --> propertyai_owner NOLOGIN
```

Flyway's checked-in non-secret configuration is:

```text
db/v2_2_1/flyway/flyway.conf
```

It fixes:

```text
defaultSchema=propertyai
schemas=propertyai
table=flyway_schema_history
createSchemas=false
jdbcProperties.options=-c role=propertyai_owner
```

Flyway 13 uses dedicated callback/event connections. Therefore the callback is
the fail-closed lifecycle assertion, while the JDBC startup option guarantees
the same owner role on every physical connection. This combination was executed
with Flyway 13.5.0; deprecated `initSql` is not used.

Run from the repository root and supply connection material outside Git:

```sh
flyway \
  -configFiles=db/v2_2_1/flyway/flyway.conf \
  -url="$PROPERTYAI_FLYWAY_JDBC_URL" \
  -user=propertyai_flyway \
  migrate
```

The JDBC URL, credential and any password/secret are provisioned outside
Git/DDL. The migration files deliberately do not execute `RESET ROLE`: owner
authority remains active until Flyway records each migration result.

The privileged bootstrap fails closed on role-attribute, complete privileged
membership-graph and schema-owner drift. Exact direct sets are
`owner <- migrator <- flyway`, both with `INHERIT FALSE / SET TRUE / ADMIN
FALSE`; Flyway has no members. Recursive SET-enabled paths to owner/migrator
must contain no other principals. The Flyway preflight independently revalidates
those conditions and `btree_gist`.

This directory remains intentionally outside the current `db/migration/` Production Flyway path.

Scope:
- 24 core tables
- PostgreSQL roles / schema / `btree_gist`
- declarative FK / UNIQUE / CHECK / partial indexes / exclusion constraint
- narrow invariant triggers/functions only
- runtime / worker / migrator privilege boundary

Out of scope:
- Production cutover
- live/race acceptance campaign
- application implementation
- data backfill
