# W5-R1 Persistent Staging Runtime Contract

W5-R1 preserves the accepted W4 business source and adds one operational environment class: `PERSISTENT_STAGING`.
It does not grant Production deployment or activation authority.

## Supported environments

- `ISOLATED_TEST`: unchanged `LocalTarget` behavior, owned disposable PostgreSQL, synthetic principal allowed only here.
- `PERSISTENT_STAGING`: explicit stable `target_id`, explicit protected PostgreSQL/Flyway connection references, scheduler `OFF`, `real_business_data_expected=false`, and `external_activation=REQUIRES_LATER_EXPLICIT_APPROVAL`.

`PRODUCTION`, `EMPTY_PRODUCTION`, and `BUSINESS_LIVE` remain unsupported and fail closed.

## Target and secret boundary

Persistent staging never discovers a database, creates a PostgreSQL cluster, runs `initdb`, drops/resets/truncates a target, or searches neighboring credentials. Runtime config contains only non-secret protected-file references. DSN and Flyway secret bytes remain outside source and artifact. Every process config is role-specific.

Web uses separate `WEB` and `SESSION` database references. Worker uses an explicit `WORKER` reference. Migrator consumes a protected external Flyway config reference; Web startup never runs migration.

## Authentication boundary

Persistent staging reuses the accepted provider-neutral `ServerPrincipalDirectory` + durable PostgreSQL `SessionService` boundary. Runtime config may bind only a non-synthetic already-provider-verified subject. It does not choose an identity provider, create OAuth credentials, trust identity headers, or introduce a local-admin bypass.

## Worker boundary

The exact accepted `propertyai_core.rent_recurring_billing.run_recurring_billing_once@1` business worker remains byte-identical. W5 only adds process/config target translation. Invocation remains explicit single-run, scheduler activation remains absent, and the W3-A effect/idempotency/cursor contract is unchanged.

## Release layout

The successor artifact keeps four identities separate: immutable artifact identity, canonical non-secret config identity, target identity, and external secret-reference identity. Artifact manifest format `RENT_RUNTIME_V2` advertises exactly `ISOLATED_TEST` and `PERSISTENT_STAGING`, with Production support false.
