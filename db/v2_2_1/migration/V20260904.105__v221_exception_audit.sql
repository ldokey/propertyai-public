SET ROLE propertyai_owner;

CREATE TABLE propertyai.cleaner_unavailability_case (
    unavailability_id uuid PRIMARY KEY,
    cleaning_id uuid NOT NULL,
    schedule_revision_id uuid NOT NULL,
    original_assignment_id uuid NOT NULL,
    cleaner_party_id uuid NOT NULL,
    case_status text NOT NULL,
    availability_classification text NOT NULL,
    replacement_urgency text NOT NULL,
    reason_code text NULL,
    reason_text text NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_unavailability_assignment
        FOREIGN KEY (original_assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id)
        REFERENCES propertyai.cleaning_assignment(assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id),
    CONSTRAINT uq_unavailability_assignment UNIQUE (original_assignment_id),
    CONSTRAINT uq_unavailability_request_target UNIQUE (unavailability_id, cleaning_id, cleaner_party_id, original_assignment_id),
    CONSTRAINT ck_unavailability_status CHECK (case_status IN ('CONFIRMED','CANCELLED')),
    CONSTRAINT ck_unavailability_classification CHECK (availability_classification IN ('EARLY_UNAVAILABLE','SAME_DAY_UNAVAILABLE')),
    CONSTRAINT ck_unavailability_urgency CHECK (replacement_urgency IN ('NORMAL','URGENT'))
);

CREATE TABLE propertyai.cleaner_reassignment_request (
    reassignment_request_id uuid PRIMARY KEY,
    unavailability_id uuid NOT NULL,
    cleaning_id uuid NOT NULL,
    cleaner_party_id uuid NOT NULL,
    original_assignment_id uuid NOT NULL,
    requested_schedule_revision_id uuid NOT NULL,
    request_no integer NOT NULL,
    request_status text NOT NULL,
    requested_at timestamptz NOT NULL,
    decided_at timestamptz NULL,
    decision_code text NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_reassignment_unavailability
        FOREIGN KEY (unavailability_id, cleaning_id, cleaner_party_id, original_assignment_id)
        REFERENCES propertyai.cleaner_unavailability_case(unavailability_id, cleaning_id, cleaner_party_id, original_assignment_id),
    CONSTRAINT fk_reassignment_target_revision
        FOREIGN KEY (cleaning_id, requested_schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    CONSTRAINT uq_reassignment_request_no UNIQUE (unavailability_id, request_no),
    CONSTRAINT ck_reassignment_request_no CHECK (request_no > 0),
    CONSTRAINT ck_reassignment_status CHECK (request_status IN ('REQUESTED','REASSIGNED_ORIGINAL','CONTINUE_REPLACEMENT','SUPERSEDED','CANCELLED')),
    CONSTRAINT ck_reassignment_terminal_fields CHECK (
        (request_status = 'REQUESTED' AND decided_at IS NULL AND decision_code IS NULL)
        OR (request_status <> 'REQUESTED' AND decided_at IS NOT NULL AND decision_code IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_reassignment_requested_cleaning
    ON propertyai.cleaner_reassignment_request(cleaning_id)
    WHERE request_status = 'REQUESTED';

CREATE TABLE propertyai.cleaning_schedule_reconciliation (
    schedule_reconciliation_id uuid PRIMARY KEY,
    cleaning_id uuid NOT NULL,
    hard_booked_assignment_id uuid NOT NULL,
    base_assignment_revision_id uuid NOT NULL,
    target_schedule_revision_id uuid NOT NULL,
    case_version integer NOT NULL DEFAULT 1,
    reconciliation_status text NOT NULL,
    reason_code text NOT NULL,
    source_ref text NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz NULL,
    resolution_code text NULL,
    CONSTRAINT fk_reconciliation_assignment
        FOREIGN KEY (hard_booked_assignment_id, cleaning_id, base_assignment_revision_id)
        REFERENCES propertyai.cleaning_assignment(assignment_id, cleaning_id, schedule_revision_id),
    CONSTRAINT fk_reconciliation_target_revision
        FOREIGN KEY (cleaning_id, target_schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    CONSTRAINT ck_reconciliation_version CHECK (case_version > 0),
    CONSTRAINT ck_reconciliation_status CHECK (reconciliation_status IN ('PENDING','RESOLVED','CANCELLED')),
    CONSTRAINT ck_reconciliation_revisions CHECK (base_assignment_revision_id <> target_schedule_revision_id),
    CONSTRAINT ck_reconciliation_reason_nonblank CHECK (btrim(reason_code) <> ''),
    CONSTRAINT ck_reconciliation_terminal_fields CHECK (
        (reconciliation_status = 'PENDING' AND resolved_at IS NULL AND resolution_code IS NULL)
        OR (reconciliation_status IN ('RESOLVED','CANCELLED') AND resolved_at IS NOT NULL AND resolution_code IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_reconciliation_pending_cleaning
    ON propertyai.cleaning_schedule_reconciliation(cleaning_id)
    WHERE reconciliation_status = 'PENDING';

CREATE INDEX ix_reconciliation_pending_assignment
    ON propertyai.cleaning_schedule_reconciliation(hard_booked_assignment_id)
    WHERE reconciliation_status = 'PENDING';

CREATE INDEX ix_unavailability_cleaning_status
    ON propertyai.cleaner_unavailability_case(cleaning_id, case_status);
CREATE INDEX ix_unavailability_cleaner_status
    ON propertyai.cleaner_unavailability_case(cleaner_party_id, case_status);

CREATE TABLE propertyai.domain_event (
    domain_event_id bigserial PRIMARY KEY,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_version bigint NULL,
    event_type text NOT NULL,
    command_id uuid NOT NULL REFERENCES propertyai.command_receipt(command_id),
    actor_party_id uuid NULL REFERENCES propertyai.party(party_id),
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_domain_event_aggregate_type_nonblank CHECK (btrim(aggregate_type) <> ''),
    CONSTRAINT ck_domain_event_type_nonblank CHECK (btrim(event_type) <> ''),
    CONSTRAINT ck_domain_event_version CHECK (aggregate_version IS NULL OR aggregate_version > 0)
);

CREATE INDEX ix_domain_event_aggregate
    ON propertyai.domain_event(aggregate_type, aggregate_id, aggregate_version, domain_event_id);
CREATE INDEX ix_domain_event_command
    ON propertyai.domain_event(command_id);
