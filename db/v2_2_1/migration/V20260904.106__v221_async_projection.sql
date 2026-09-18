SET ROLE propertyai_owner;

CREATE TABLE propertyai.business_scheduled_action (
    scheduled_action_id uuid PRIMARY KEY,
    action_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    due_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    action_status text NOT NULL DEFAULT 'PENDING',
    idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL,
    lease_owner text NULL,
    lease_until timestamptz NULL,
    lease_fence bigint NOT NULL DEFAULT 0,
    last_error_code text NULL,
    completed_at timestamptz NULL,
    cancelled_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_scheduled_action_type_nonblank CHECK (btrim(action_type) <> ''),
    CONSTRAINT ck_scheduled_action_aggregate_type_nonblank CHECK (btrim(aggregate_type) <> ''),
    CONSTRAINT ck_scheduled_action_key_nonblank CHECK (btrim(idempotency_key) <> ''),
    CONSTRAINT ck_scheduled_action_status CHECK (action_status IN ('PENDING','RUNNING','FAILED_RETRYABLE','SUCCEEDED','DEAD_LETTER','CANCELLED')),
    CONSTRAINT ck_scheduled_action_attempts CHECK (attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts),
    CONSTRAINT ck_scheduled_action_lease_fence CHECK (lease_fence >= 0),
    CONSTRAINT ck_scheduled_action_lease_fields CHECK (
        (action_status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL AND completed_at IS NULL AND cancelled_at IS NULL)
        OR (action_status = 'SUCCEEDED' AND lease_owner IS NULL AND lease_until IS NULL AND completed_at IS NOT NULL AND cancelled_at IS NULL)
        OR (action_status = 'CANCELLED' AND lease_owner IS NULL AND lease_until IS NULL AND completed_at IS NULL AND cancelled_at IS NOT NULL)
        OR (action_status IN ('PENDING','FAILED_RETRYABLE','DEAD_LETTER') AND lease_owner IS NULL AND lease_until IS NULL AND completed_at IS NULL AND cancelled_at IS NULL)
    )
);

CREATE TABLE propertyai.integration_outbox (
    outbox_id uuid PRIMARY KEY,
    domain_event_id bigint NULL REFERENCES propertyai.domain_event(domain_event_id),
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    destination_type text NOT NULL,
    destination_ref text NULL,
    available_at timestamptz NOT NULL,
    outbox_status text NOT NULL DEFAULT 'PENDING',
    idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL,
    lease_owner text NULL,
    lease_until timestamptz NULL,
    lease_fence bigint NOT NULL DEFAULT 0,
    external_effect_id text NULL,
    last_error_code text NULL,
    delivered_at timestamptz NULL,
    cancelled_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_outbox_event_type_nonblank CHECK (btrim(event_type) <> ''),
    CONSTRAINT ck_outbox_aggregate_type_nonblank CHECK (btrim(aggregate_type) <> ''),
    CONSTRAINT ck_outbox_destination_type_nonblank CHECK (btrim(destination_type) <> ''),
    CONSTRAINT ck_outbox_key_nonblank CHECK (btrim(idempotency_key) <> ''),
    CONSTRAINT ck_outbox_status CHECK (outbox_status IN ('PENDING','RUNNING','FAILED_RETRYABLE','PENDING_RECONCILIATION','SUCCEEDED','DEAD_LETTER','CANCELLED')),
    CONSTRAINT ck_outbox_attempts CHECK (attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts),
    CONSTRAINT ck_outbox_lease_fence CHECK (lease_fence >= 0),
    CONSTRAINT ck_outbox_lease_fields CHECK (
        (outbox_status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL AND delivered_at IS NULL AND cancelled_at IS NULL)
        OR (outbox_status = 'SUCCEEDED' AND lease_owner IS NULL AND lease_until IS NULL AND delivered_at IS NOT NULL AND cancelled_at IS NULL)
        OR (outbox_status = 'CANCELLED' AND lease_owner IS NULL AND lease_until IS NULL AND delivered_at IS NULL AND cancelled_at IS NOT NULL)
        OR (outbox_status IN ('PENDING','FAILED_RETRYABLE','PENDING_RECONCILIATION','DEAD_LETTER') AND lease_owner IS NULL AND lease_until IS NULL AND delivered_at IS NULL AND cancelled_at IS NULL)
    )
);

CREATE TABLE propertyai.integration_resource_binding (
    binding_id uuid PRIMARY KEY,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    destination_type text NOT NULL,
    resource_code text NULL,
    external_resource_id text NOT NULL,
    external_uid text NULL,
    sync_status text NOT NULL,
    last_applied_aggregate_version bigint NULL,
    external_version text NULL,
    last_synced_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_resource_binding_aggregate UNIQUE NULLS NOT DISTINCT (aggregate_type, aggregate_id, destination_type, resource_code),
    CONSTRAINT uq_resource_binding_external UNIQUE NULLS NOT DISTINCT (destination_type, resource_code, external_resource_id),
    CONSTRAINT ck_resource_binding_aggregate_type_nonblank CHECK (btrim(aggregate_type) <> ''),
    CONSTRAINT ck_resource_binding_destination_type_nonblank CHECK (btrim(destination_type) <> ''),
    CONSTRAINT ck_resource_binding_external_id_nonblank CHECK (btrim(external_resource_id) <> ''),
    CONSTRAINT ck_resource_binding_version CHECK (last_applied_aggregate_version IS NULL OR last_applied_aggregate_version > 0)
);

CREATE INDEX ix_scheduled_action_claim
    ON propertyai.business_scheduled_action(available_at, scheduled_action_id)
    WHERE action_status IN ('PENDING','FAILED_RETRYABLE');
CREATE INDEX ix_scheduled_action_reclaim
    ON propertyai.business_scheduled_action(lease_until, scheduled_action_id)
    WHERE action_status = 'RUNNING';
CREATE INDEX ix_outbox_claim
    ON propertyai.integration_outbox(available_at, outbox_id)
    WHERE outbox_status IN ('PENDING','FAILED_RETRYABLE');
CREATE INDEX ix_outbox_reclaim
    ON propertyai.integration_outbox(lease_until, outbox_id)
    WHERE outbox_status = 'RUNNING';
CREATE INDEX ix_outbox_aggregate_status
    ON propertyai.integration_outbox(aggregate_type, aggregate_id, outbox_status);
