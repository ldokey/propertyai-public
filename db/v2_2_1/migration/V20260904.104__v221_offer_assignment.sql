SET ROLE propertyai_owner;

CREATE TABLE propertyai.cleaning_offer_campaign (
    campaign_id uuid PRIMARY KEY,
    cleaning_id uuid NOT NULL,
    schedule_revision_id uuid NOT NULL,
    campaign_no integer NOT NULL,
    campaign_status text NOT NULL,
    open_tier_floor smallint NOT NULL DEFAULT 1,
    max_tier smallint NOT NULL,
    tier_expand_after_minutes integer NULL,
    acceptance_cutoff_at timestamptz NOT NULL,
    base_fee_krw bigint NOT NULL,
    replacement_urgency text NOT NULL,
    urgent_premium_krw bigint NOT NULL DEFAULT 0,
    total_agreed_fee_krw bigint NOT NULL,
    urgent_premium_policy_version text NULL,
    opened_at timestamptz NOT NULL,
    closed_at timestamptz NULL,
    closed_reason_code text NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_campaign_schedule_revision
        FOREIGN KEY (cleaning_id, schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    CONSTRAINT uq_campaign_number UNIQUE (cleaning_id, campaign_no),
    CONSTRAINT uq_campaign_binding_target UNIQUE (campaign_id, cleaning_id, schedule_revision_id),
    CONSTRAINT ck_campaign_number CHECK (campaign_no > 0),
    CONSTRAINT ck_campaign_status CHECK (campaign_status IN ('OPEN','CLOSED','CANCELLED')),
    CONSTRAINT ck_campaign_tier_floor CHECK (open_tier_floor > 0 AND open_tier_floor <= max_tier),
    CONSTRAINT ck_campaign_expand_minutes CHECK (tier_expand_after_minutes IS NULL OR tier_expand_after_minutes > 0),
    CONSTRAINT ck_campaign_cutoff CHECK (opened_at < acceptance_cutoff_at),
    CONSTRAINT ck_campaign_fee_nonnegative CHECK (base_fee_krw >= 0 AND urgent_premium_krw >= 0),
    CONSTRAINT ck_campaign_fee_math CHECK (total_agreed_fee_krw = base_fee_krw + urgent_premium_krw),
    CONSTRAINT ck_campaign_urgency CHECK (replacement_urgency IN ('NORMAL','URGENT')),
    CONSTRAINT ck_campaign_premium_policy CHECK (urgent_premium_krw = 0 OR urgent_premium_policy_version IS NOT NULL),
    CONSTRAINT ck_campaign_close_fields CHECK (
        (campaign_status = 'OPEN' AND closed_at IS NULL AND closed_reason_code IS NULL)
        OR (campaign_status IN ('CLOSED','CANCELLED') AND closed_at IS NOT NULL AND closed_reason_code IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_campaign_open_cleaning
    ON propertyai.cleaning_offer_campaign(cleaning_id)
    WHERE campaign_status = 'OPEN';

CREATE TABLE propertyai.cleaning_offer_candidate (
    offer_candidate_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES propertyai.cleaning_offer_campaign(campaign_id),
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    tier_no smallint NOT NULL,
    candidate_status text NOT NULL,
    proposal_version integer NOT NULL DEFAULT 1,
    proposed_start_at timestamptz NOT NULL,
    proposed_end_at timestamptz NOT NULL,
    proposed_buffer_before_minutes integer NOT NULL DEFAULT 0,
    proposed_buffer_after_minutes integer NOT NULL DEFAULT 0,
    buffer_basis text NOT NULL,
    buffer_policy_ref text NULL,
    evaluated_at timestamptz NOT NULL,
    declined_at timestamptz NULL,
    accepted_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_candidate_cleaner_campaign UNIQUE (campaign_id, cleaner_party_id),
    CONSTRAINT uq_candidate_binding UNIQUE (campaign_id, offer_candidate_id, cleaner_party_id),
    CONSTRAINT uq_candidate_proposal_binding UNIQUE (campaign_id, offer_candidate_id, cleaner_party_id, proposal_version),
    CONSTRAINT ck_candidate_tier CHECK (tier_no > 0),
    CONSTRAINT ck_candidate_status CHECK (candidate_status IN ('ELIGIBLE','DECLINED','ACCEPTED')),
    CONSTRAINT ck_candidate_version CHECK (proposal_version > 0),
    CONSTRAINT ck_candidate_slot CHECK (proposed_end_at > proposed_start_at),
    CONSTRAINT ck_candidate_buffers CHECK (proposed_buffer_before_minutes >= 0 AND proposed_buffer_after_minutes >= 0),
    CONSTRAINT ck_candidate_buffer_basis CHECK (buffer_basis IN ('NOT_APPLIED','MANUAL','ROUTE_ESTIMATE','POLICY')),
    CONSTRAINT ck_candidate_buffer_provenance CHECK (
        (buffer_basis = 'NOT_APPLIED' AND proposed_buffer_before_minutes = 0 AND proposed_buffer_after_minutes = 0 AND buffer_policy_ref IS NULL)
        OR (buffer_basis = 'POLICY' AND buffer_policy_ref IS NOT NULL)
        OR (buffer_basis IN ('MANUAL','ROUTE_ESTIMATE'))
    ),
    CONSTRAINT ck_candidate_terminal_fields CHECK (
        (candidate_status = 'ELIGIBLE' AND declined_at IS NULL AND accepted_at IS NULL)
        OR (candidate_status = 'DECLINED' AND declined_at IS NOT NULL AND accepted_at IS NULL)
        OR (candidate_status = 'ACCEPTED' AND accepted_at IS NOT NULL AND declined_at IS NULL)
    )
);

CREATE UNIQUE INDEX uq_candidate_accepted_campaign
    ON propertyai.cleaning_offer_candidate(campaign_id)
    WHERE candidate_status = 'ACCEPTED';

CREATE TABLE propertyai.cleaning_assignment (
    assignment_id uuid PRIMARY KEY,
    cleaning_id uuid NOT NULL,
    assignment_no integer NOT NULL,
    schedule_revision_id uuid NOT NULL,
    campaign_id uuid NULL,
    offer_candidate_id uuid NULL,
    accepted_proposal_version integer NULL,
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    assignment_source text NOT NULL,
    assignment_status text NOT NULL,
    booked_at timestamptz NOT NULL,
    scheduled_start_at timestamptz NOT NULL,
    scheduled_end_at timestamptz NOT NULL,
    work_minutes_snapshot integer NOT NULL,
    travel_buffer_before_minutes integer NOT NULL DEFAULT 0,
    travel_buffer_after_minutes integer NOT NULL DEFAULT 0,
    buffer_basis text NOT NULL,
    buffer_policy_ref text NULL,
    busy_window tstzrange NOT NULL,
    base_fee_krw bigint NOT NULL,
    replacement_urgency text NOT NULL,
    urgent_premium_krw bigint NOT NULL,
    total_agreed_fee_krw bigint NOT NULL,
    urgent_premium_policy_version text NULL,
    ended_at timestamptz NULL,
    end_reason_code text NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_assignment_number UNIQUE (cleaning_id, assignment_no),
    CONSTRAINT uq_assignment_reconciliation_target UNIQUE (assignment_id, cleaning_id, schedule_revision_id),
    CONSTRAINT uq_assignment_unavailability_target UNIQUE (assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id),
    CONSTRAINT fk_assignment_schedule_revision
        FOREIGN KEY (cleaning_id, schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    CONSTRAINT fk_assignment_campaign_binding
        FOREIGN KEY (campaign_id, cleaning_id, schedule_revision_id)
        REFERENCES propertyai.cleaning_offer_campaign(campaign_id, cleaning_id, schedule_revision_id),
    CONSTRAINT fk_assignment_candidate_proposal
        FOREIGN KEY (campaign_id, offer_candidate_id, cleaner_party_id, accepted_proposal_version)
        REFERENCES propertyai.cleaning_offer_candidate(campaign_id, offer_candidate_id, cleaner_party_id, proposal_version),
    CONSTRAINT ck_assignment_number CHECK (assignment_no > 0),
    CONSTRAINT ck_assignment_source CHECK (assignment_source IN ('OFFER_ACCEPTED','ORIGINAL_REASSIGNED','SCHEDULE_REBOOKED','MANUAL_OVERRIDE')),
    CONSTRAINT ck_assignment_status CHECK (assignment_status IN ('HARD_BOOKED','RELEASED','COMPLETED','CANCELLED')),
    CONSTRAINT ck_assignment_offer_source_fields CHECK (
        (assignment_source = 'OFFER_ACCEPTED' AND campaign_id IS NOT NULL AND offer_candidate_id IS NOT NULL AND accepted_proposal_version IS NOT NULL)
        OR (assignment_source <> 'OFFER_ACCEPTED' AND campaign_id IS NULL AND offer_candidate_id IS NULL AND accepted_proposal_version IS NULL)
    ),
    CONSTRAINT ck_assignment_proposal_version CHECK (accepted_proposal_version IS NULL OR accepted_proposal_version > 0),
    CONSTRAINT ck_assignment_slot CHECK (scheduled_end_at > scheduled_start_at),
    CONSTRAINT ck_assignment_work_minutes CHECK (work_minutes_snapshot > 0),
    CONSTRAINT ck_assignment_exact_duration CHECK (scheduled_end_at - scheduled_start_at = work_minutes_snapshot * interval '1 minute'),
    CONSTRAINT ck_assignment_buffers CHECK (travel_buffer_before_minutes >= 0 AND travel_buffer_after_minutes >= 0),
    CONSTRAINT ck_assignment_buffer_basis CHECK (buffer_basis IN ('NOT_APPLIED','MANUAL','ROUTE_ESTIMATE','POLICY')),
    CONSTRAINT ck_assignment_buffer_provenance CHECK (
        (buffer_basis = 'NOT_APPLIED' AND travel_buffer_before_minutes = 0 AND travel_buffer_after_minutes = 0 AND buffer_policy_ref IS NULL)
        OR (buffer_basis = 'POLICY' AND buffer_policy_ref IS NOT NULL)
        OR (buffer_basis IN ('MANUAL','ROUTE_ESTIMATE'))
    ),
    CONSTRAINT ck_assignment_fee_nonnegative CHECK (base_fee_krw >= 0 AND urgent_premium_krw >= 0),
    CONSTRAINT ck_assignment_fee_math CHECK (total_agreed_fee_krw = base_fee_krw + urgent_premium_krw),
    CONSTRAINT ck_assignment_urgency CHECK (replacement_urgency IN ('NORMAL','URGENT')),
    CONSTRAINT ck_assignment_premium_policy CHECK (urgent_premium_krw = 0 OR urgent_premium_policy_version IS NOT NULL),
    CONSTRAINT ck_assignment_terminal_fields CHECK (
        (assignment_status = 'HARD_BOOKED' AND ended_at IS NULL AND end_reason_code IS NULL)
        OR (assignment_status IN ('RELEASED','COMPLETED','CANCELLED') AND ended_at IS NOT NULL AND end_reason_code IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_assignment_hard_booked_cleaning
    ON propertyai.cleaning_assignment(cleaning_id)
    WHERE assignment_status = 'HARD_BOOKED';

ALTER TABLE propertyai.cleaning_assignment
    ADD CONSTRAINT ex_assignment_cleaner_busy_window
    EXCLUDE USING gist (
        cleaner_party_id WITH =,
        busy_window WITH &&
    )
    WHERE (assignment_status = 'HARD_BOOKED');

CREATE INDEX ix_campaign_open_cutoff
    ON propertyai.cleaning_offer_campaign(acceptance_cutoff_at, campaign_id)
    WHERE campaign_status = 'OPEN';
CREATE INDEX ix_candidate_campaign_status_tier
    ON propertyai.cleaning_offer_candidate(campaign_id, candidate_status, tier_no);
CREATE INDEX ix_candidate_cleaner_eligible
    ON propertyai.cleaning_offer_candidate(cleaner_party_id, tier_no, campaign_id)
    WHERE candidate_status = 'ELIGIBLE';
CREATE INDEX ix_assignment_cleaner_start
    ON propertyai.cleaning_assignment(cleaner_party_id, scheduled_start_at);
