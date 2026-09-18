-- PropertyAI vNext / language-neutral PostgreSQL authority
-- Scope: batch definitions/runs, due business actions, relative reminder policy, integration outbox.

-- Defines recurring system work independently of the implementation language.
-- No schedules are seeded by this migration; operations can enable them after review.
CREATE TABLE propertyai.scheduler_job_definition (
    scheduler_job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_code text NOT NULL UNIQUE,
    handler_key text NOT NULL,
    schedule_kind text NOT NULL,
    cron_expression text,
    interval_seconds integer,
    timezone_name text NOT NULL DEFAULT 'Asia/Seoul',
    concurrency_policy text NOT NULL DEFAULT 'FORBID_OVERLAP',
    enabled boolean NOT NULL DEFAULT false,
    batch_size integer NOT NULL DEFAULT 100,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(btrim(job_code)) > 0),
    CHECK (length(btrim(handler_key)) > 0),
    CHECK (schedule_kind IN ('CRON', 'INTERVAL', 'EVENT_DRIVEN')),
    CHECK (
        (schedule_kind = 'CRON' AND cron_expression IS NOT NULL AND interval_seconds IS NULL)
        OR (schedule_kind = 'INTERVAL' AND cron_expression IS NULL AND interval_seconds IS NOT NULL AND interval_seconds > 0)
        OR (schedule_kind = 'EVENT_DRIVEN' AND cron_expression IS NULL AND interval_seconds IS NULL)
    ),
    CHECK (concurrency_policy IN ('FORBID_OVERLAP', 'ALLOW_OVERLAP', 'REPLACE')),
    CHECK (batch_size > 0)
);

CREATE TABLE propertyai.scheduler_job_run (
    scheduler_job_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scheduler_job_id uuid NOT NULL REFERENCES propertyai.scheduler_job_definition(scheduler_job_id),
    scheduled_for timestamptz NOT NULL,
    run_status text NOT NULL,
    worker_id text,
    lease_until timestamptz,
    started_at timestamptz,
    heartbeat_at timestamptz,
    finished_at timestamptz,
    cursor_after jsonb,
    scanned_count bigint NOT NULL DEFAULT 0,
    processed_count bigint NOT NULL DEFAULT 0,
    error_count bigint NOT NULL DEFAULT 0,
    last_error_code text,
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (run_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED_RETRYABLE', 'DEAD_LETTER', 'CANCELLED')),
    CHECK (scanned_count >= 0 AND processed_count >= 0 AND error_count >= 0),
    CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
    UNIQUE (scheduler_job_id, scheduled_for)
);

CREATE INDEX idx_scheduler_job_run_claim
    ON propertyai.scheduler_job_run(run_status, scheduled_for, scheduler_job_run_id);

-- A durable business timer. Examples: OPEN_NEXT_TIER, OFFER_CUTOFF,
-- BUILD_RELATIVE_REMINDER, RECONCILE_SCHEDULE_REVISION.
CREATE TABLE propertyai.business_scheduled_action (
    scheduled_action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    due_at timestamptz NOT NULL,
    action_status text NOT NULL DEFAULT 'PENDING',
    idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 8,
    next_attempt_at timestamptz,
    lease_owner text,
    lease_until timestamptz,
    heartbeat_at timestamptz,
    last_error_code text,
    completed_at timestamptz,
    cancelled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(btrim(action_type)) > 0),
    CHECK (length(btrim(aggregate_type)) > 0),
    CHECK (action_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED_RETRYABLE', 'DEAD_LETTER', 'CANCELLED')),
    CHECK (attempt_count >= 0),
    CHECK (max_attempts > 0),
    CHECK (completed_at IS NULL OR action_status = 'SUCCEEDED'),
    CHECK (cancelled_at IS NULL OR action_status = 'CANCELLED')
);

-- Keyset-pagination / worker-claim order: (due_at, scheduled_action_id).
CREATE INDEX idx_business_scheduled_action_due
    ON propertyai.business_scheduled_action(due_at, scheduled_action_id)
    WHERE action_status IN ('PENDING', 'FAILED_RETRYABLE');

CREATE INDEX idx_business_scheduled_action_lease
    ON propertyai.business_scheduled_action(lease_until, scheduled_action_id)
    WHERE action_status = 'RUNNING';

ALTER TABLE propertyai.cleaning_offer_tier_opening
    ADD CONSTRAINT fk_tier_opening_scheduled_action
    FOREIGN KEY (scheduled_action_id)
    REFERENCES propertyai.business_scheduled_action(scheduled_action_id);

-- Relative-to-assignment-start notification knobs. NULL means not configured.
-- This deliberately replaces hard-coded 08:30/09:00/09:20/10:20 semantics in the future model.
CREATE TABLE propertyai.cleaning_notification_policy (
    notification_policy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_code text NOT NULL,
    policy_version integer NOT NULL,
    day_confirm_lead_minutes integer,
    reconfirm_lead_minutes integer,
    escalation_lead_minutes integer,
    arrival_lead_minutes integer,
    start_lead_minutes integer,
    completion_reminder_lag_minutes integer,
    effective_from timestamptz NOT NULL,
    effective_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(btrim(policy_code)) > 0),
    CHECK (policy_version > 0),
    CHECK (day_confirm_lead_minutes IS NULL OR day_confirm_lead_minutes >= 0),
    CHECK (reconfirm_lead_minutes IS NULL OR reconfirm_lead_minutes >= 0),
    CHECK (escalation_lead_minutes IS NULL OR escalation_lead_minutes >= 0),
    CHECK (arrival_lead_minutes IS NULL OR arrival_lead_minutes >= 0),
    CHECK (start_lead_minutes IS NULL OR start_lead_minutes >= 0),
    CHECK (completion_reminder_lag_minutes IS NULL OR completion_reminder_lag_minutes >= 0),
    CHECK (effective_until IS NULL OR effective_until > effective_from),
    UNIQUE (policy_code, policy_version)
);

CREATE UNIQUE INDEX uq_active_cleaning_notification_policy
    ON propertyai.cleaning_notification_policy(policy_code)
    WHERE effective_until IS NULL;

ALTER TABLE propertyai.cleaning_notification_policy
    ADD CONSTRAINT ex_cleaning_notification_policy_effective_overlap
    EXCLUDE USING gist (
        policy_code WITH =,
        tstzrange(effective_from, COALESCE(effective_until, 'infinity'::timestamptz), '[)') WITH &&
    );

-- Every external mutation/delivery is durable before execution.
-- Tier expansion itself creates no Telegram delivery unless an explicit outbox row is produced.
CREATE TABLE propertyai.integration_outbox (
    outbox_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    destination_type text NOT NULL,
    destination_ref text,
    due_at timestamptz NOT NULL DEFAULT now(),
    outbox_status text NOT NULL DEFAULT 'PENDING',
    idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 8,
    next_attempt_at timestamptz,
    lease_owner text,
    lease_until timestamptz,
    heartbeat_at timestamptz,
    external_effect_id text,
    last_error_code text,
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(btrim(event_type)) > 0),
    CHECK (length(btrim(aggregate_type)) > 0),
    CHECK (destination_type IN ('TELEGRAM', 'NOTION', 'GOOGLE_CALENDAR', 'GOOGLE_DRIVE', 'INTERNAL')),
    CHECK (outbox_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED_RETRYABLE', 'PENDING_RECONCILIATION', 'DEAD_LETTER', 'CANCELLED')),
    CHECK (attempt_count >= 0),
    CHECK (max_attempts > 0),
    CHECK (delivered_at IS NULL OR outbox_status = 'SUCCEEDED')
);

-- Keyset-pagination / worker-claim order: (due_at, outbox_id).
CREATE INDEX idx_integration_outbox_due
    ON propertyai.integration_outbox(due_at, outbox_id)
    WHERE outbox_status IN ('PENDING', 'FAILED_RETRYABLE');

CREATE INDEX idx_integration_outbox_reconciliation
    ON propertyai.integration_outbox(destination_type, created_at, outbox_id)
    WHERE outbox_status = 'PENDING_RECONCILIATION';

CREATE TABLE propertyai.integration_outbox_attempt (
    outbox_attempt_id bigserial PRIMARY KEY,
    outbox_id uuid NOT NULL REFERENCES propertyai.integration_outbox(outbox_id),
    attempt_number integer NOT NULL,
    worker_id text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    outcome text,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (attempt_number > 0),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    UNIQUE (outbox_id, attempt_number)
);

CREATE INDEX idx_integration_outbox_attempt
    ON propertyai.integration_outbox_attempt(outbox_id, attempt_number);

-- Append-only operational audit. This is business/domain audit, not DCS.
CREATE TABLE propertyai.domain_audit_event (
    audit_event_id bigserial PRIMARY KEY,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL,
    actor_type text NOT NULL,
    actor_ref text,
    idempotency_key text,
    source_ref text,
    before_state jsonb,
    after_state jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(btrim(aggregate_type)) > 0),
    CHECK (length(btrim(event_type)) > 0),
    CHECK (actor_type IN ('USER', 'SYSTEM', 'OPERATOR', 'MIGRATION', 'RECONCILIATION'))
);

CREATE UNIQUE INDEX uq_domain_audit_idempotency
    ON propertyai.domain_audit_event(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX idx_domain_audit_aggregate
    ON propertyai.domain_audit_event(aggregate_type, aggregate_id, occurred_at, audit_event_id);
