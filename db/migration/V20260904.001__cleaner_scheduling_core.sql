-- PropertyAI vNext / language-neutral PostgreSQL authority
-- Scope: Cleaner identity, property eligibility, capacity knobs, Reservation/Cleaning schedule revisions.
-- This migration is intentionally additive and does not cut over the legacy Notion runtime.

CREATE SCHEMA IF NOT EXISTS propertyai;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE propertyai.property (
    property_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_code text NOT NULL UNIQUE,
    notion_page_id uuid UNIQUE,
    display_name text NOT NULL,
    timezone_name text NOT NULL DEFAULT 'Asia/Seoul',
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(btrim(property_code)) > 0),
    CHECK (length(btrim(display_name)) > 0),
    CHECK (length(btrim(timezone_name)) > 0)
);

CREATE TABLE propertyai.rental_unit (
    rental_unit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rental_unit_code text NOT NULL UNIQUE,
    property_id uuid NOT NULL REFERENCES propertyai.property(property_id),
    notion_page_id uuid UNIQUE,
    display_name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(btrim(rental_unit_code)) > 0),
    CHECK (length(btrim(display_name)) > 0),
    UNIQUE (property_id, rental_unit_id)
);

CREATE INDEX idx_rental_unit_property
    ON propertyai.rental_unit(property_id, active);

CREATE TABLE propertyai.party (
    party_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    party_code text NOT NULL UNIQUE,
    notion_page_id uuid UNIQUE,
    display_name text NOT NULL,
    data_environment text NOT NULL DEFAULT 'PRODUCTION',
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (data_environment IN ('PRODUCTION', 'TEST')),
    CHECK (length(btrim(party_code)) > 0),
    CHECK (length(btrim(display_name)) > 0)
);

CREATE TABLE propertyai.cleaner_profile (
    cleaner_party_id uuid PRIMARY KEY REFERENCES propertyai.party(party_id),
    operational_status text NOT NULL DEFAULT 'ACTIVE',
    lifecycle_state text,
    onboarding_state text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (operational_status IN ('ACTIVE', 'PAUSED', 'INACTIVE'))
);

-- One ACTIVE Telegram identity per Cleaner Party, and one ACTIVE owner per Telegram user/chat.
-- Revoking an identity does not cascade into an already HARD_BOOKED Assignment.
CREATE TABLE propertyai.external_identity (
    external_identity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    party_id uuid NOT NULL REFERENCES propertyai.party(party_id),
    provider text NOT NULL,
    provider_user_id text NOT NULL,
    provider_chat_id text,
    bound_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    source_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (provider IN ('TELEGRAM')),
    CHECK (length(btrim(provider_user_id)) > 0),
    CHECK (provider_chat_id IS NULL OR length(btrim(provider_chat_id)) > 0),
    CHECK (revoked_at IS NULL OR revoked_at >= bound_at)
);

CREATE UNIQUE INDEX uq_active_identity_provider_user
    ON propertyai.external_identity(provider, provider_user_id)
    WHERE revoked_at IS NULL;

CREATE UNIQUE INDEX uq_active_identity_provider_chat
    ON propertyai.external_identity(provider, provider_chat_id)
    WHERE revoked_at IS NULL AND provider_chat_id IS NOT NULL;

CREATE UNIQUE INDEX uq_active_telegram_identity_per_party
    ON propertyai.external_identity(party_id, provider)
    WHERE revoked_at IS NULL AND provider = 'TELEGRAM';

-- Property access is eligibility for NEW offer acceptance.
-- REVOKED access does not retroactively cancel an already HARD_BOOKED Assignment.
CREATE TABLE propertyai.cleaner_property_access (
    access_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    property_id uuid NOT NULL REFERENCES propertyai.property(property_id),
    status text NOT NULL,
    offer_tier smallint NOT NULL,
    priority_within_tier integer,
    effective_from timestamptz NOT NULL DEFAULT now(),
    effective_until timestamptz,
    source_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('REQUESTED', 'APPROVED', 'REJECTED', 'REVOKED')),
    CHECK (offer_tier > 0),
    CHECK (priority_within_tier IS NULL OR priority_within_tier >= 0),
    CHECK (effective_until IS NULL OR effective_until > effective_from)
);

CREATE UNIQUE INDEX uq_current_cleaner_property_access
    ON propertyai.cleaner_property_access(cleaner_party_id, property_id)
    WHERE status IN ('REQUESTED', 'APPROVED');

CREATE INDEX idx_cleaner_property_access_candidate
    ON propertyai.cleaner_property_access(property_id, status, offer_tier, priority_within_tier, cleaner_party_id);

-- Nullable means "no configured limit", not zero.
-- Values can be introduced later without changing the scheduling schema.
CREATE TABLE propertyai.cleaner_capacity_policy (
    capacity_policy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    policy_version integer NOT NULL,
    default_travel_buffer_minutes integer,
    max_daily_work_minutes integer,
    max_daily_jobs integer,
    effective_from timestamptz NOT NULL,
    effective_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (policy_version > 0),
    CHECK (default_travel_buffer_minutes IS NULL OR default_travel_buffer_minutes >= 0),
    CHECK (max_daily_work_minutes IS NULL OR max_daily_work_minutes > 0),
    CHECK (max_daily_jobs IS NULL OR max_daily_jobs > 0),
    CHECK (effective_until IS NULL OR effective_until > effective_from),
    UNIQUE (cleaner_party_id, policy_version)
);

CREATE UNIQUE INDEX uq_active_cleaner_capacity_policy
    ON propertyai.cleaner_capacity_policy(cleaner_party_id)
    WHERE effective_until IS NULL;

ALTER TABLE propertyai.cleaner_capacity_policy
    ADD CONSTRAINT ex_cleaner_capacity_policy_effective_overlap
    EXCLUDE USING gist (
        cleaner_party_id WITH =,
        tstzrange(effective_from, COALESCE(effective_until, 'infinity'::timestamptz), '[)') WITH &&
    );

-- Optional future availability / blackout input. Not required for P0 assignment creation.
CREATE TABLE propertyai.cleaner_availability_window (
    availability_window_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    availability_state text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    reason_code text,
    source_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (availability_state IN ('AVAILABLE', 'UNAVAILABLE')),
    CHECK (ends_at > starts_at)
);

CREATE INDEX idx_cleaner_availability_window
    ON propertyai.cleaner_availability_window(cleaner_party_id, starts_at, ends_at);

CREATE TABLE propertyai.reservation (
    reservation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reservation_code text NOT NULL UNIQUE,
    property_id uuid NOT NULL REFERENCES propertyai.property(property_id),
    rental_unit_id uuid,
    notion_page_id uuid UNIQUE,
    external_platform text,
    external_reservation_id text,
    reservation_status text NOT NULL,
    check_in_at timestamptz,
    check_out_at timestamptz NOT NULL,
    source_version bigint NOT NULL DEFAULT 1,
    last_source_event_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (reservation_status IN ('CONFIRMED', 'CANCELLED')),
    CHECK (source_version > 0),
    CHECK (check_in_at IS NULL OR check_out_at > check_in_at),
    CHECK (length(btrim(reservation_code)) > 0),
    FOREIGN KEY (property_id, rental_unit_id)
        REFERENCES propertyai.rental_unit(property_id, rental_unit_id),
    UNIQUE (property_id, reservation_id)
);

CREATE UNIQUE INDEX uq_reservation_external_identity
    ON propertyai.reservation(external_platform, external_reservation_id)
    WHERE external_platform IS NOT NULL AND external_reservation_id IS NOT NULL;

CREATE INDEX idx_reservation_checkout
    ON propertyai.reservation(reservation_status, check_out_at, property_id);

CREATE TABLE propertyai.cleaning_job (
    cleaning_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaning_code text NOT NULL UNIQUE,
    reservation_id uuid,
    property_id uuid NOT NULL REFERENCES propertyai.property(property_id),
    rental_unit_id uuid,
    notion_page_id uuid UNIQUE,
    cleaning_status text NOT NULL DEFAULT 'PLANNED',
    current_schedule_revision_id uuid,
    lock_version bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (cleaning_status IN ('PLANNED', 'OFFERING', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')),
    CHECK (lock_version >= 0),
    CHECK (length(btrim(cleaning_code)) > 0),
    FOREIGN KEY (property_id, reservation_id)
        REFERENCES propertyai.reservation(property_id, reservation_id),
    FOREIGN KEY (property_id, rental_unit_id)
        REFERENCES propertyai.rental_unit(property_id, rental_unit_id),
    UNIQUE (cleaning_id, property_id)
);

CREATE INDEX idx_cleaning_job_reservation
    ON propertyai.cleaning_job(reservation_id);

CREATE INDEX idx_cleaning_job_property_status
    ON propertyai.cleaning_job(property_id, cleaning_status);

-- The work duration belongs to a Cleaning request/revision, not to Property defaults.
-- It may be NULL while PLANNED, but an Offer Campaign cannot open until a positive snapshot exists.
CREATE TABLE propertyai.cleaning_schedule_revision (
    schedule_revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaning_id uuid NOT NULL REFERENCES propertyai.cleaning_job(cleaning_id),
    revision_no integer NOT NULL,
    service_window_start_at timestamptz NOT NULL,
    service_deadline_at timestamptz NOT NULL,
    required_work_minutes integer,
    source_checkout_at timestamptz,
    change_reason_code text NOT NULL,
    source_event_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (revision_no > 0),
    CHECK (service_deadline_at > service_window_start_at),
    CHECK (required_work_minutes IS NULL OR required_work_minutes > 0),
    CHECK (
        required_work_minutes IS NULL
        OR required_work_minutes <= floor(extract(epoch FROM (service_deadline_at - service_window_start_at)) / 60.0)
    ),
    UNIQUE (cleaning_id, revision_no),
    UNIQUE (cleaning_id, schedule_revision_id)
);

CREATE INDEX idx_cleaning_schedule_revision_window
    ON propertyai.cleaning_schedule_revision(cleaning_id, service_window_start_at, service_deadline_at);

ALTER TABLE propertyai.cleaning_job
    ADD CONSTRAINT fk_cleaning_current_schedule_revision
    FOREIGN KEY (cleaning_id, current_schedule_revision_id)
    REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id);

CREATE TABLE propertyai.cleaning_schedule_revision_event (
    schedule_revision_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaning_id uuid NOT NULL REFERENCES propertyai.cleaning_job(cleaning_id),
    schedule_revision_id uuid NOT NULL,
    event_type text NOT NULL,
    source_ref text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    CHECK (event_type IN ('CREATED', 'ACTIVATED', 'SUPERSEDED', 'RESERVATION_CHECKOUT_CHANGED', 'MANUAL_CHANGED')),
    FOREIGN KEY (cleaning_id, schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id)
);

CREATE INDEX idx_cleaning_schedule_revision_event
    ON propertyai.cleaning_schedule_revision_event(cleaning_id, occurred_at);
