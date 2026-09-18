-- PropertyAI vNext / language-neutral PostgreSQL authority
-- Scope: cumulative tier opening, offer candidates, hard-booked assignments, cross-Cleaning conflict exclusion.

CREATE TABLE propertyai.cleaning_offer_campaign (
    campaign_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaning_id uuid NOT NULL REFERENCES propertyai.cleaning_job(cleaning_id),
    schedule_revision_id uuid NOT NULL,
    campaign_version integer NOT NULL,
    campaign_status text NOT NULL DEFAULT 'OPEN',
    current_open_tier smallint NOT NULL DEFAULT 1,
    max_tier smallint NOT NULL,
    tier_expand_after_minutes integer,
    next_tier_open_at timestamptz,
    acceptance_cutoff_at timestamptz NOT NULL,
    base_fee_krw bigint NOT NULL,
    replacement_urgency text NOT NULL DEFAULT 'NORMAL',
    urgent_premium_krw bigint NOT NULL DEFAULT 0,
    total_agreed_fee_krw bigint NOT NULL,
    urgent_premium_policy_version text,
    opened_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    closed_reason_code text,
    idempotency_key text NOT NULL UNIQUE,
    lock_version bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (campaign_version > 0),
    CHECK (current_open_tier > 0),
    CHECK (max_tier >= current_open_tier),
    CHECK (tier_expand_after_minutes IS NULL OR tier_expand_after_minutes > 0),
    CHECK (campaign_status IN ('OPEN', 'CLOSED', 'SUPERSEDED', 'CANCELLED')),
    CHECK (acceptance_cutoff_at > opened_at),
    CHECK (base_fee_krw >= 0),
    CHECK (urgent_premium_krw >= 0),
    CHECK (total_agreed_fee_krw = base_fee_krw + urgent_premium_krw),
    CHECK (replacement_urgency IN ('NORMAL', 'URGENT')),
    CHECK (
        (replacement_urgency = 'NORMAL' AND urgent_premium_krw = 0)
        OR replacement_urgency = 'URGENT'
    ),
    CHECK (
        (campaign_status = 'OPEN' AND closed_at IS NULL)
        OR (campaign_status <> 'OPEN' AND closed_at IS NOT NULL)
    ),
    FOREIGN KEY (cleaning_id, schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    UNIQUE (cleaning_id, campaign_version),
    UNIQUE (campaign_id, cleaning_id, schedule_revision_id)
);

CREATE UNIQUE INDEX uq_open_offer_campaign_per_cleaning
    ON propertyai.cleaning_offer_campaign(cleaning_id)
    WHERE campaign_status = 'OPEN';

-- A campaign is not allowed to open before the per-Cleaning work duration exists.
-- This makes duration a request/offer-time input instead of a Property default.
CREATE OR REPLACE FUNCTION propertyai.validate_cleaning_offer_campaign_schedule()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
DECLARE
    v_revision propertyai.cleaning_schedule_revision%ROWTYPE;
BEGIN
    SELECT * INTO v_revision
    FROM propertyai.cleaning_schedule_revision
    WHERE cleaning_id = NEW.cleaning_id
      AND schedule_revision_id = NEW.schedule_revision_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'OFFER_CAMPAIGN_SCHEDULE_REVISION_NOT_FOUND';
    END IF;
    IF v_revision.required_work_minutes IS NULL THEN
        RAISE EXCEPTION 'OFFER_CAMPAIGN_WORK_MINUTES_REQUIRED';
    END IF;
    IF NEW.opened_at >= v_revision.service_deadline_at THEN
        RAISE EXCEPTION 'OFFER_CAMPAIGN_OPEN_AFTER_SERVICE_DEADLINE';
    END IF;
    IF NEW.acceptance_cutoff_at > v_revision.service_deadline_at THEN
        RAISE EXCEPTION 'OFFER_CAMPAIGN_CUTOFF_AFTER_SERVICE_DEADLINE';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_offer_campaign_schedule_validation
BEFORE INSERT OR UPDATE OF cleaning_id, schedule_revision_id, opened_at, acceptance_cutoff_at
ON propertyai.cleaning_offer_campaign
FOR EACH ROW
EXECUTE FUNCTION propertyai.validate_cleaning_offer_campaign_schedule();

CREATE INDEX idx_offer_campaign_tier_due
    ON propertyai.cleaning_offer_campaign(next_tier_open_at, campaign_id)
    WHERE campaign_status = 'OPEN' AND next_tier_open_at IS NOT NULL;

CREATE INDEX idx_offer_campaign_cutoff
    ON propertyai.cleaning_offer_campaign(acceptance_cutoff_at, campaign_id)
    WHERE campaign_status = 'OPEN';

-- Opening Tier N is cumulative. Opening Tier 2 never removes Tier 1 eligibility.
-- This row is audit state only; it does not imply that any notification was sent.
CREATE TABLE propertyai.cleaning_offer_tier_opening (
    tier_opening_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL REFERENCES propertyai.cleaning_offer_campaign(campaign_id),
    tier_no smallint NOT NULL,
    opened_at timestamptz NOT NULL,
    opening_reason_code text NOT NULL,
    scheduled_action_id uuid,
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (tier_no > 0),
    CHECK (opening_reason_code IN ('INITIAL', 'ELAPSED_TIME', 'URGENT_EXPANSION', 'MANUAL')),
    UNIQUE (campaign_id, tier_no)
);

-- Candidate rows are discoverability / evaluation snapshots, not acceptance authority.
-- Acceptance must re-read current Cleaner, identity, property access, campaign tier,
-- current schedule revision and capacity inside the same transaction.
CREATE TABLE propertyai.cleaning_offer_candidate (
    offer_candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL REFERENCES propertyai.cleaning_offer_campaign(campaign_id),
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    tier_no smallint NOT NULL,
    candidate_status text NOT NULL DEFAULT 'ELIGIBLE',
    proposed_start_at timestamptz,
    proposed_end_at timestamptz,
    evaluated_at timestamptz,
    evaluation_reason_code text,
    notified_at timestamptz,
    declined_at timestamptz,
    accepted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (tier_no > 0),
    CHECK (candidate_status IN ('ELIGIBLE', 'DECLINED', 'ACCEPTED', 'INELIGIBLE', 'SUPERSEDED')),
    CHECK (
        (proposed_start_at IS NULL AND proposed_end_at IS NULL)
        OR (proposed_start_at IS NOT NULL AND proposed_end_at IS NOT NULL AND proposed_end_at > proposed_start_at)
    ),
    CHECK ((candidate_status = 'DECLINED') = (declined_at IS NOT NULL)),
    CHECK ((candidate_status = 'ACCEPTED') = (accepted_at IS NOT NULL)),
    UNIQUE (campaign_id, cleaner_party_id),
    UNIQUE (campaign_id, offer_candidate_id)
);

CREATE INDEX idx_offer_candidate_discovery
    ON propertyai.cleaning_offer_candidate(campaign_id, tier_no, candidate_status, cleaner_party_id);

CREATE INDEX idx_offer_candidate_cleaner
    ON propertyai.cleaning_offer_candidate(cleaner_party_id, candidate_status, campaign_id);

CREATE TABLE propertyai.cleaning_assignment (
    assignment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_version text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    cleaning_id uuid NOT NULL REFERENCES propertyai.cleaning_job(cleaning_id),
    schedule_revision_id uuid NOT NULL,
    campaign_id uuid REFERENCES propertyai.cleaning_offer_campaign(campaign_id),
    offer_candidate_id uuid,
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    assignment_source text NOT NULL,
    assignment_status text NOT NULL,
    accepted_at timestamptz NOT NULL,
    scheduled_start_at timestamptz NOT NULL,
    scheduled_end_at timestamptz NOT NULL,
    work_minutes_snapshot integer NOT NULL,
    travel_buffer_before_minutes integer,
    travel_buffer_after_minutes integer,
    service_window_start_snapshot timestamptz NOT NULL,
    service_deadline_snapshot timestamptz NOT NULL,
    conflict_window tstzrange NOT NULL,
    base_fee_krw bigint NOT NULL,
    replacement_urgency text NOT NULL DEFAULT 'NORMAL',
    urgent_premium_krw bigint NOT NULL DEFAULT 0,
    total_agreed_fee_krw bigint NOT NULL,
    urgent_premium_policy_version text,
    ended_at timestamptz,
    end_reason_code text,
    end_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(btrim(assignment_version)) > 0),
    CHECK (assignment_source IN ('OFFER_ACCEPTED', 'ORIGINAL_REASSIGNED', 'MANUAL_OVERRIDE')),
    CHECK (assignment_status IN ('HARD_BOOKED', 'RELEASED', 'COMPLETED', 'CANCELLED')),
    CHECK (scheduled_end_at > scheduled_start_at),
    CHECK (work_minutes_snapshot > 0),
    CHECK (travel_buffer_before_minutes IS NULL OR travel_buffer_before_minutes >= 0),
    CHECK (travel_buffer_after_minutes IS NULL OR travel_buffer_after_minutes >= 0),
    CHECK (service_deadline_snapshot > service_window_start_snapshot),
    CHECK (scheduled_start_at >= service_window_start_snapshot),
    CHECK (scheduled_end_at <= service_deadline_snapshot),
    CHECK (
        work_minutes_snapshot = floor(extract(epoch FROM (scheduled_end_at - scheduled_start_at)) / 60.0)
    ),
    CHECK (base_fee_krw >= 0),
    CHECK (urgent_premium_krw >= 0),
    CHECK (total_agreed_fee_krw = base_fee_krw + urgent_premium_krw),
    CHECK (replacement_urgency IN ('NORMAL', 'URGENT')),
    CHECK (
        (assignment_status = 'HARD_BOOKED' AND ended_at IS NULL AND end_reason_code IS NULL)
        OR (assignment_status <> 'HARD_BOOKED' AND ended_at IS NOT NULL AND end_reason_code IS NOT NULL)
    ),
    CHECK (ended_at IS NULL OR ended_at >= accepted_at),
    CHECK (
        (assignment_source = 'OFFER_ACCEPTED' AND campaign_id IS NOT NULL AND offer_candidate_id IS NOT NULL)
        OR (assignment_source <> 'OFFER_ACCEPTED')
    ),
    FOREIGN KEY (cleaning_id, schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    FOREIGN KEY (campaign_id, offer_candidate_id)
        REFERENCES propertyai.cleaning_offer_candidate(campaign_id, offer_candidate_id),
    UNIQUE (cleaning_id, assignment_version)
);

-- conflict_window is DB-derived on every insert/schedule-buffer update.
-- Any caller-supplied value is overwritten before constraints are checked.
CREATE OR REPLACE FUNCTION propertyai.derive_cleaning_assignment_conflict_window()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
BEGIN
    NEW.conflict_window := tstzrange(
        NEW.scheduled_start_at - (COALESCE(NEW.travel_buffer_before_minutes, 0) * interval '1 minute'),
        NEW.scheduled_end_at + (COALESCE(NEW.travel_buffer_after_minutes, 0) * interval '1 minute'),
        '[)'
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_cleaning_assignment_conflict_window
BEFORE INSERT OR UPDATE OF scheduled_start_at, scheduled_end_at,
    travel_buffer_before_minutes, travel_buffer_after_minutes
ON propertyai.cleaning_assignment
FOR EACH ROW
EXECUTE FUNCTION propertyai.derive_cleaning_assignment_conflict_window();

-- One effective assignment per Cleaning.
CREATE UNIQUE INDEX uq_hard_booked_assignment_per_cleaning
    ON propertyai.cleaning_assignment(cleaning_id)
    WHERE assignment_status = 'HARD_BOOKED';

-- DB-level final fence: a Cleaner cannot own two overlapping effective slots.
-- [start,end) means A ending exactly when B starts is not an overlap.
ALTER TABLE propertyai.cleaning_assignment
    ADD CONSTRAINT ex_cleaner_hard_booked_conflict
    EXCLUDE USING gist (
        cleaner_party_id WITH =,
        conflict_window WITH &&
    )
    WHERE (assignment_status = 'HARD_BOOKED');

CREATE INDEX idx_assignment_cleaner_day
    ON propertyai.cleaning_assignment(cleaner_party_id, scheduled_start_at, scheduled_end_at)
    WHERE assignment_status = 'HARD_BOOKED';

CREATE INDEX idx_assignment_cleaning_history
    ON propertyai.cleaning_assignment(cleaning_id, accepted_at, assignment_id);

CREATE TABLE propertyai.cleaning_assignment_event (
    assignment_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id uuid NOT NULL REFERENCES propertyai.cleaning_assignment(assignment_id),
    event_type text NOT NULL,
    event_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (event_type IN ('ACCEPTED', 'RELEASED', 'COMPLETED', 'CANCELLED', 'REASSIGNED'))
);

CREATE INDEX idx_assignment_event_assignment
    ON propertyai.cleaning_assignment_event(assignment_id, occurred_at);

-- Lightweight rows used only to acquire deterministic row locks for cross-Cleaning acceptance.
-- Every Cleaner and Cleaning that participates in scheduling must have one row.
CREATE TABLE propertyai.cleaner_schedule_guard (
    cleaner_party_id uuid PRIMARY KEY REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    lock_version bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (lock_version >= 0)
);

CREATE TABLE propertyai.cleaning_schedule_guard (
    cleaning_id uuid PRIMARY KEY REFERENCES propertyai.cleaning_job(cleaning_id),
    lock_version bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (lock_version >= 0)
);

-- The DB returns the active capacity knobs. NULL means the limit/buffer is not configured.
CREATE VIEW propertyai.v_cleaner_current_capacity_policy AS
SELECT DISTINCT ON (p.cleaner_party_id)
    p.cleaner_party_id,
    p.capacity_policy_id,
    p.policy_version,
    p.default_travel_buffer_minutes,
    p.max_daily_work_minutes,
    p.max_daily_jobs,
    p.effective_from,
    p.effective_until
FROM propertyai.cleaner_capacity_policy p
WHERE p.effective_from <= now()
  AND (p.effective_until IS NULL OR p.effective_until > now())
ORDER BY p.cleaner_party_id, p.effective_from DESC, p.policy_version DESC;

CREATE VIEW propertyai.v_cleaner_effective_schedule AS
SELECT
    a.assignment_id,
    a.cleaner_party_id,
    a.cleaning_id,
    a.schedule_revision_id,
    a.scheduled_start_at,
    a.scheduled_end_at,
    a.work_minutes_snapshot,
    a.travel_buffer_before_minutes,
    a.travel_buffer_after_minutes,
    a.conflict_window,
    a.accepted_at,
    j.property_id,
    j.rental_unit_id
FROM propertyai.cleaning_assignment a
JOIN propertyai.cleaning_job j ON j.cleaning_id = a.cleaning_id
WHERE a.assignment_status = 'HARD_BOOKED';

CREATE VIEW propertyai.v_cleaner_daily_load AS
SELECT
    a.cleaner_party_id,
    (a.scheduled_start_at AT TIME ZONE 'Asia/Seoul')::date AS service_date_kst,
    count(*)::integer AS hard_booked_jobs,
    sum(a.work_minutes_snapshot)::bigint AS hard_booked_work_minutes
FROM propertyai.cleaning_assignment a
WHERE a.assignment_status = 'HARD_BOOKED'
GROUP BY a.cleaner_party_id, (a.scheduled_start_at AT TIME ZONE 'Asia/Seoul')::date;
