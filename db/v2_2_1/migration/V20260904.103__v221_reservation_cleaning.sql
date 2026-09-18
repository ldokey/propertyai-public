SET ROLE propertyai_owner;

CREATE TABLE propertyai.reservation (
    reservation_id uuid PRIMARY KEY,
    reservation_code text NOT NULL UNIQUE,
    property_id uuid NOT NULL REFERENCES propertyai.property(property_id),
    rental_unit_id uuid NULL,
    source_channel text NULL,
    external_reservation_id text NULL,
    reservation_status text NOT NULL,
    check_in_at timestamptz NULL,
    check_out_at timestamptz NOT NULL,
    source_version bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_reservation_property_target UNIQUE (reservation_id, property_id),
    CONSTRAINT uq_reservation_unit_target UNIQUE (reservation_id, rental_unit_id),
    CONSTRAINT uq_reservation_external_source UNIQUE (source_channel, external_reservation_id),
    CONSTRAINT fk_reservation_property_unit
        FOREIGN KEY (property_id, rental_unit_id)
        REFERENCES propertyai.rental_unit(property_id, rental_unit_id),
    CONSTRAINT ck_reservation_code_nonblank CHECK (btrim(reservation_code) <> ''),
    CONSTRAINT ck_reservation_status CHECK (reservation_status IN ('CONFIRMED','CANCELLED')),
    CONSTRAINT ck_reservation_source_pair CHECK ((source_channel IS NULL) = (external_reservation_id IS NULL)),
    CONSTRAINT ck_reservation_source_channel_nonblank CHECK (source_channel IS NULL OR btrim(source_channel) <> ''),
    CONSTRAINT ck_reservation_external_id_nonblank CHECK (external_reservation_id IS NULL OR btrim(external_reservation_id) <> ''),
    CONSTRAINT ck_reservation_version CHECK (source_version > 0),
    CONSTRAINT ck_reservation_times CHECK (check_in_at IS NULL OR check_out_at > check_in_at)
);

CREATE TABLE propertyai.cleaning_job (
    cleaning_id uuid PRIMARY KEY,
    cleaning_code text NOT NULL UNIQUE,
    reservation_id uuid NULL,
    property_id uuid NOT NULL REFERENCES propertyai.property(property_id),
    rental_unit_id uuid NULL,
    schedule_source_type text NOT NULL,
    cleaning_status text NOT NULL,
    current_schedule_revision_id uuid NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_cleaning_reservation_property
        FOREIGN KEY (reservation_id, property_id)
        REFERENCES propertyai.reservation(reservation_id, property_id),
    CONSTRAINT fk_cleaning_reservation_unit
        FOREIGN KEY (reservation_id, rental_unit_id)
        REFERENCES propertyai.reservation(reservation_id, rental_unit_id),
    CONSTRAINT fk_cleaning_property_unit
        FOREIGN KEY (property_id, rental_unit_id)
        REFERENCES propertyai.rental_unit(property_id, rental_unit_id),
    CONSTRAINT ck_cleaning_code_nonblank CHECK (btrim(cleaning_code) <> ''),
    CONSTRAINT ck_cleaning_source_type CHECK (schedule_source_type IN ('RESERVATION_CHECKOUT','MANUAL','MID_STAY')),
    CONSTRAINT ck_cleaning_status CHECK (cleaning_status IN ('PLANNED','OFFERING','ASSIGNED','IN_PROGRESS','COMPLETED','CANCELLED')),
    CONSTRAINT ck_cleaning_checkout_requires_reservation CHECK (schedule_source_type <> 'RESERVATION_CHECKOUT' OR reservation_id IS NOT NULL)
);

CREATE UNIQUE INDEX uq_cleaning_checkout_active_reservation
    ON propertyai.cleaning_job(reservation_id)
    WHERE schedule_source_type = 'RESERVATION_CHECKOUT'
      AND cleaning_status <> 'CANCELLED'
      AND reservation_id IS NOT NULL;

CREATE TABLE propertyai.cleaning_schedule_revision (
    schedule_revision_id uuid PRIMARY KEY,
    cleaning_id uuid NOT NULL REFERENCES propertyai.cleaning_job(cleaning_id),
    revision_no integer NOT NULL,
    service_window_start_at timestamptz NOT NULL,
    service_deadline_at timestamptz NOT NULL,
    required_work_minutes integer NULL,
    source_checkout_at timestamptz NULL,
    source_reservation_version bigint NULL,
    change_reason_code text NOT NULL,
    source_command_id uuid NULL REFERENCES propertyai.command_receipt(command_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_schedule_revision_number UNIQUE (cleaning_id, revision_no),
    CONSTRAINT uq_schedule_revision_binding UNIQUE (cleaning_id, schedule_revision_id),
    CONSTRAINT ck_schedule_revision_no CHECK (revision_no > 0),
    CONSTRAINT ck_schedule_revision_window CHECK (service_deadline_at > service_window_start_at),
    CONSTRAINT ck_schedule_revision_work_minutes CHECK (required_work_minutes IS NULL OR required_work_minutes > 0),
    CONSTRAINT ck_schedule_revision_work_fits_window CHECK (
        required_work_minutes IS NULL
        OR service_window_start_at + required_work_minutes * interval '1 minute' <= service_deadline_at
    ),
    CONSTRAINT ck_schedule_revision_reservation_source_pair CHECK (
        (source_checkout_at IS NULL) = (source_reservation_version IS NULL)
    ),
    CONSTRAINT ck_schedule_revision_source_version CHECK (source_reservation_version IS NULL OR source_reservation_version > 0),
    CONSTRAINT ck_schedule_revision_reason_nonblank CHECK (btrim(change_reason_code) <> '')
);

ALTER TABLE propertyai.cleaning_job
    ADD CONSTRAINT fk_cleaning_current_revision
    FOREIGN KEY (cleaning_id, current_schedule_revision_id)
    REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id)
    DEFERRABLE INITIALLY IMMEDIATE;

CREATE INDEX ix_reservation_property_checkout
    ON propertyai.reservation(property_id, check_out_at)
    WHERE reservation_status = 'CONFIRMED';
CREATE INDEX ix_cleaning_reservation_source_status
    ON propertyai.cleaning_job(reservation_id, schedule_source_type, cleaning_status)
    WHERE reservation_id IS NOT NULL;
CREATE INDEX ix_cleaning_property_status
    ON propertyai.cleaning_job(property_id, cleaning_status);
