SET ROLE propertyai_owner;

CREATE TABLE propertyai.organization (
    organization_id uuid PRIMARY KEY,
    organization_code text NOT NULL UNIQUE,
    display_name text NOT NULL,
    organization_status text NOT NULL,
    data_environment text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_organization_code_nonblank CHECK (btrim(organization_code) <> ''),
    CONSTRAINT ck_organization_display_nonblank CHECK (btrim(display_name) <> ''),
    CONSTRAINT ck_organization_status CHECK (organization_status IN ('ACTIVE','SUSPENDED','CLOSED')),
    CONSTRAINT ck_organization_environment CHECK (data_environment IN ('PRODUCTION','TEST'))
);

CREATE TABLE propertyai.party (
    party_id uuid PRIMARY KEY,
    party_code text NOT NULL UNIQUE,
    display_name text NOT NULL,
    data_environment text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_party_code_nonblank CHECK (btrim(party_code) <> ''),
    CONSTRAINT ck_party_display_nonblank CHECK (btrim(display_name) <> ''),
    CONSTRAINT ck_party_environment CHECK (data_environment IN ('PRODUCTION','TEST'))
);

CREATE TABLE propertyai.organization_member (
    organization_member_id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
    party_id uuid NOT NULL REFERENCES propertyai.party(party_id),
    membership_role text NOT NULL,
    membership_status text NOT NULL,
    joined_at timestamptz NULL,
    removed_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_organization_member_current UNIQUE (organization_id, party_id),
    CONSTRAINT ck_organization_member_role CHECK (membership_role IN ('OWNER','ADMIN','OPERATOR','VIEWER')),
    CONSTRAINT ck_organization_member_status CHECK (membership_status IN ('ACTIVE','INVITED','REMOVED')),
    CONSTRAINT ck_organization_member_lifecycle CHECK (
        (membership_status = 'INVITED' AND joined_at IS NULL AND removed_at IS NULL)
        OR (membership_status = 'ACTIVE' AND joined_at IS NOT NULL AND removed_at IS NULL)
        OR (membership_status = 'REMOVED' AND removed_at IS NOT NULL)
    )
);

CREATE TABLE propertyai.property (
    property_id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
    property_code text NOT NULL UNIQUE,
    display_name text NOT NULL,
    timezone_name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_property_code_nonblank CHECK (btrim(property_code) <> ''),
    CONSTRAINT ck_property_display_nonblank CHECK (btrim(display_name) <> ''),
    CONSTRAINT ck_property_timezone_nonblank CHECK (btrim(timezone_name) <> '')
);

CREATE TABLE propertyai.rental_unit (
    rental_unit_id uuid PRIMARY KEY,
    rental_unit_code text NOT NULL UNIQUE,
    property_id uuid NOT NULL REFERENCES propertyai.property(property_id),
    display_name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_rental_unit_property_binding UNIQUE (property_id, rental_unit_id),
    CONSTRAINT ck_rental_unit_code_nonblank CHECK (btrim(rental_unit_code) <> ''),
    CONSTRAINT ck_rental_unit_display_nonblank CHECK (btrim(display_name) <> '')
);

CREATE TABLE propertyai.cleaner_profile (
    cleaner_party_id uuid PRIMARY KEY REFERENCES propertyai.party(party_id),
    operational_status text NOT NULL,
    max_daily_work_minutes integer NULL,
    max_daily_jobs integer NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_cleaner_profile_status CHECK (operational_status IN ('ACTIVE','PAUSED','INACTIVE')),
    CONSTRAINT ck_cleaner_profile_daily_minutes CHECK (max_daily_work_minutes IS NULL OR max_daily_work_minutes > 0),
    CONSTRAINT ck_cleaner_profile_daily_jobs CHECK (max_daily_jobs IS NULL OR max_daily_jobs > 0)
);

CREATE TABLE propertyai.external_identity (
    external_identity_id uuid PRIMARY KEY,
    party_id uuid NOT NULL REFERENCES propertyai.party(party_id),
    provider text NOT NULL,
    provider_user_id text NOT NULL,
    provider_chat_id text NULL,
    bound_at timestamptz NOT NULL,
    revoked_at timestamptz NULL,
    source_ref text NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_external_identity_party_target UNIQUE (external_identity_id, party_id),
    CONSTRAINT ck_external_identity_provider_nonblank CHECK (btrim(provider) <> ''),
    CONSTRAINT ck_external_identity_user_nonblank CHECK (btrim(provider_user_id) <> ''),
    CONSTRAINT ck_external_identity_chat_nonblank CHECK (provider_chat_id IS NULL OR btrim(provider_chat_id) <> ''),
    CONSTRAINT ck_external_identity_revoke_after_bind CHECK (revoked_at IS NULL OR revoked_at >= bound_at)
);

CREATE UNIQUE INDEX uq_external_identity_active_user
    ON propertyai.external_identity(provider, provider_user_id)
    WHERE revoked_at IS NULL;

CREATE UNIQUE INDEX uq_external_identity_active_chat
    ON propertyai.external_identity(provider, provider_chat_id)
    WHERE revoked_at IS NULL AND provider_chat_id IS NOT NULL;

CREATE UNIQUE INDEX uq_external_identity_active_telegram_party
    ON propertyai.external_identity(party_id)
    WHERE revoked_at IS NULL AND provider = 'TELEGRAM';

CREATE TABLE propertyai.cleaner_property_roster (
    roster_id uuid PRIMARY KEY,
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    property_id uuid NOT NULL REFERENCES propertyai.property(property_id),
    roster_status text NOT NULL,
    offer_tier smallint NOT NULL,
    priority_within_tier integer NULL,
    eligible_from timestamptz NULL,
    eligible_until timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_cleaner_property_roster UNIQUE (cleaner_party_id, property_id),
    CONSTRAINT ck_cleaner_property_roster_status CHECK (roster_status IN ('ACTIVE','PAUSED','REMOVED')),
    CONSTRAINT ck_cleaner_property_roster_tier CHECK (offer_tier > 0),
    CONSTRAINT ck_cleaner_property_roster_priority CHECK (priority_within_tier IS NULL OR priority_within_tier >= 0),
    CONSTRAINT ck_cleaner_property_roster_interval CHECK (eligible_until IS NULL OR eligible_from IS NULL OR eligible_until > eligible_from)
);

CREATE TABLE propertyai.cleaner_schedule_block (
    schedule_block_id uuid PRIMARY KEY,
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    reason_code text NULL,
    source_ref text NULL,
    cancelled_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_cleaner_schedule_block_range CHECK (ends_at > starts_at),
    CONSTRAINT ck_cleaner_schedule_block_cancel_time CHECK (cancelled_at IS NULL OR cancelled_at >= created_at)
);

CREATE TABLE propertyai.command_receipt (
    command_id uuid PRIMARY KEY,
    authority_scope_code text NOT NULL,
    command_type text NOT NULL,
    idempotency_key text NOT NULL,
    request_payload jsonb NOT NULL,
    source_channel_code text NOT NULL,
    source_stream_key text NULL,
    source_event_id text NULL,
    principal_type text NOT NULL,
    actor_party_id uuid NULL REFERENCES propertyai.party(party_id),
    actor_external_identity_id uuid NULL,
    authority_epoch bigint NOT NULL,
    source_observed_at timestamptz NULL,
    decided_at timestamptz NOT NULL,
    result_type text NULL,
    result_id uuid NULL,
    result_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_command_receipt_idempotency UNIQUE (authority_scope_code, command_type, idempotency_key),
    CONSTRAINT fk_command_receipt_actor_identity
        FOREIGN KEY (actor_external_identity_id, actor_party_id)
        REFERENCES propertyai.external_identity(external_identity_id, party_id),
    CONSTRAINT ck_command_receipt_scope_nonblank CHECK (btrim(authority_scope_code) <> ''),
    CONSTRAINT ck_command_receipt_type_nonblank CHECK (btrim(command_type) <> ''),
    CONSTRAINT ck_command_receipt_key_nonblank CHECK (btrim(idempotency_key) <> ''),
    CONSTRAINT ck_command_receipt_channel_nonblank CHECK (btrim(source_channel_code) <> ''),
    CONSTRAINT ck_command_receipt_stream_event_pair CHECK ((source_stream_key IS NULL) = (source_event_id IS NULL)),
    CONSTRAINT ck_command_receipt_stream_nonblank CHECK (source_stream_key IS NULL OR btrim(source_stream_key) <> ''),
    CONSTRAINT ck_command_receipt_event_nonblank CHECK (source_event_id IS NULL OR btrim(source_event_id) <> ''),
    CONSTRAINT ck_command_receipt_epoch CHECK (authority_epoch >= 0),
    CONSTRAINT ck_command_receipt_principal CHECK (
        (principal_type = 'TELEGRAM' AND actor_party_id IS NOT NULL AND actor_external_identity_id IS NOT NULL)
        OR (principal_type = 'PARTY' AND actor_party_id IS NOT NULL AND actor_external_identity_id IS NULL)
        OR (principal_type IN ('SYSTEM','MIGRATION') AND actor_party_id IS NULL AND actor_external_identity_id IS NULL)
    )
);

CREATE UNIQUE INDEX uq_command_receipt_source_event
    ON propertyai.command_receipt(source_channel_code, source_stream_key, source_event_id)
    WHERE source_event_id IS NOT NULL;

CREATE TABLE propertyai.authority_epoch (
    scope_code text PRIMARY KEY,
    current_epoch bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_authority_epoch_scope_nonblank CHECK (btrim(scope_code) <> ''),
    CONSTRAINT ck_authority_epoch_nonnegative CHECK (current_epoch >= 0)
);

INSERT INTO propertyai.authority_epoch(scope_code, current_epoch)
VALUES ('CLEANER_SCHEDULING', 1)
ON CONFLICT (scope_code) DO NOTHING;

CREATE INDEX ix_organization_member_org_status_role
    ON propertyai.organization_member(organization_id, membership_status, membership_role);
CREATE INDEX ix_organization_member_party_status
    ON propertyai.organization_member(party_id, membership_status);
CREATE INDEX ix_property_org_active
    ON propertyai.property(organization_id, active);
CREATE INDEX ix_rental_unit_property_active
    ON propertyai.rental_unit(property_id, active);
CREATE INDEX ix_roster_property_active_tier
    ON propertyai.cleaner_property_roster(property_id, offer_tier, priority_within_tier)
    WHERE roster_status = 'ACTIVE';
CREATE INDEX ix_roster_cleaner_active
    ON propertyai.cleaner_property_roster(cleaner_party_id, property_id)
    WHERE roster_status = 'ACTIVE';
CREATE INDEX ix_schedule_block_active_cleaner_time
    ON propertyai.cleaner_schedule_block(cleaner_party_id, starts_at, ends_at)
    WHERE cancelled_at IS NULL;
