-- PropertyAI vNext / language-neutral PostgreSQL authority
-- Scope: Cleaner unavailable release and restricted original-Cleaner reassignment.
-- Performance scoring is intentionally deferred to a later migration/policy review.

ALTER TABLE propertyai.cleaning_assignment
    ADD CONSTRAINT uq_assignment_binding_tuple
    UNIQUE (assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id);

CREATE TABLE propertyai.cleaner_unavailability_case (
    unavailability_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaning_id uuid NOT NULL REFERENCES propertyai.cleaning_job(cleaning_id),
    schedule_revision_id uuid NOT NULL,
    original_assignment_id uuid NOT NULL,
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    unavailability_status text NOT NULL DEFAULT 'CONFIRMED',
    performance_classification text NOT NULL,
    replacement_urgency text NOT NULL,
    reason_code text,
    reason_text text,
    occurred_at timestamptz NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (unavailability_status IN ('CONFIRMED', 'CANCELLED')),
    CHECK (performance_classification IN ('EARLY_UNAVAILABLE', 'SAME_DAY_UNAVAILABLE')),
    CHECK (replacement_urgency IN ('NORMAL', 'URGENT')),
    FOREIGN KEY (cleaning_id, schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    FOREIGN KEY (original_assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id)
        REFERENCES propertyai.cleaning_assignment(assignment_id, cleaning_id, schedule_revision_id, cleaner_party_id),
    UNIQUE (original_assignment_id)
);

CREATE INDEX idx_unavailability_cleaning
    ON propertyai.cleaner_unavailability_case(cleaning_id, occurred_at, unavailability_id);

CREATE TABLE propertyai.cleaner_reassignment_request (
    reassignment_request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    unavailability_id uuid NOT NULL UNIQUE REFERENCES propertyai.cleaner_unavailability_case(unavailability_id),
    cleaning_id uuid NOT NULL REFERENCES propertyai.cleaning_job(cleaning_id),
    cleaner_party_id uuid NOT NULL REFERENCES propertyai.cleaner_profile(cleaner_party_id),
    original_assignment_id uuid NOT NULL REFERENCES propertyai.cleaning_assignment(assignment_id),
    target_schedule_revision_id uuid NOT NULL,
    request_status text NOT NULL DEFAULT 'REQUESTED',
    requested_at timestamptz NOT NULL,
    decided_at timestamptz,
    decision_key text,
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (request_status IN ('REQUESTED', 'REASSIGNED_ORIGINAL', 'CONTINUE_REPLACEMENT', 'CANCELLED')),
    CHECK ((request_status = 'REQUESTED') = (decided_at IS NULL)),
    FOREIGN KEY (cleaning_id, target_schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id)
);

CREATE INDEX idx_reassignment_request_open
    ON propertyai.cleaner_reassignment_request(cleaning_id, requested_at, reassignment_request_id)
    WHERE request_status = 'REQUESTED';

CREATE OR REPLACE FUNCTION propertyai.record_cleaner_unavailable(
    p_assignment_id uuid,
    p_occurred_at timestamptz,
    p_performance_classification text,
    p_replacement_urgency text,
    p_idempotency_key text,
    p_reason_code text DEFAULT NULL,
    p_reason_text text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
DECLARE
    v_assignment propertyai.cleaning_assignment%ROWTYPE;
    v_existing propertyai.cleaner_unavailability_case%ROWTYPE;
    v_unavailability_id uuid;
BEGIN
    IF p_occurred_at IS NULL THEN
        RAISE EXCEPTION 'UNAVAILABLE_TIME_REQUIRED';
    END IF;
    IF p_performance_classification NOT IN ('EARLY_UNAVAILABLE', 'SAME_DAY_UNAVAILABLE') THEN
        RAISE EXCEPTION 'UNAVAILABLE_CLASSIFICATION_INVALID';
    END IF;
    IF p_replacement_urgency NOT IN ('NORMAL', 'URGENT') THEN
        RAISE EXCEPTION 'UNAVAILABLE_REPLACEMENT_URGENCY_INVALID';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'UNAVAILABLE_IDEMPOTENCY_REQUIRED';
    END IF;

    SELECT * INTO v_existing
    FROM propertyai.cleaner_unavailability_case
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.original_assignment_id <> p_assignment_id THEN
            RAISE EXCEPTION 'UNAVAILABLE_IDEMPOTENCY_CONFLICT';
        END IF;
        RETURN v_existing.unavailability_id;
    END IF;

    SELECT * INTO v_assignment
    FROM propertyai.cleaning_assignment
    WHERE assignment_id = p_assignment_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'UNAVAILABLE_ASSIGNMENT_NOT_FOUND';
    END IF;

    INSERT INTO propertyai.cleaner_schedule_guard(cleaner_party_id)
    VALUES (v_assignment.cleaner_party_id)
    ON CONFLICT (cleaner_party_id) DO NOTHING;
    PERFORM 1 FROM propertyai.cleaner_schedule_guard
    WHERE cleaner_party_id = v_assignment.cleaner_party_id
    FOR UPDATE;

    INSERT INTO propertyai.cleaning_schedule_guard(cleaning_id)
    VALUES (v_assignment.cleaning_id)
    ON CONFLICT (cleaning_id) DO NOTHING;
    PERFORM 1 FROM propertyai.cleaning_schedule_guard
    WHERE cleaning_id = v_assignment.cleaning_id
    FOR UPDATE;

    SELECT * INTO v_assignment
    FROM propertyai.cleaning_assignment
    WHERE assignment_id = p_assignment_id
    FOR UPDATE;

    IF v_assignment.assignment_status <> 'HARD_BOOKED' THEN
        RAISE EXCEPTION 'UNAVAILABLE_ASSIGNMENT_NOT_HARD_BOOKED';
    END IF;
    IF p_occurred_at < v_assignment.accepted_at THEN
        RAISE EXCEPTION 'UNAVAILABLE_BEFORE_ACCEPTANCE';
    END IF;

    UPDATE propertyai.cleaning_assignment
    SET assignment_status = 'RELEASED',
        ended_at = p_occurred_at,
        end_reason_code = 'CLEANER_UNAVAILABLE',
        end_key = 'CLEANER_UNAVAILABLE:' || p_idempotency_key,
        updated_at = p_occurred_at
    WHERE assignment_id = p_assignment_id;

    UPDATE propertyai.cleaning_job
    SET cleaning_status = 'OFFERING',
        lock_version = lock_version + 1,
        updated_at = p_occurred_at
    WHERE cleaning_id = v_assignment.cleaning_id;

    v_unavailability_id := gen_random_uuid();
    INSERT INTO propertyai.cleaner_unavailability_case (
        unavailability_id, cleaning_id, schedule_revision_id,
        original_assignment_id, cleaner_party_id,
        performance_classification, replacement_urgency,
        reason_code, reason_text, occurred_at, idempotency_key
    ) VALUES (
        v_unavailability_id, v_assignment.cleaning_id, v_assignment.schedule_revision_id,
        v_assignment.assignment_id, v_assignment.cleaner_party_id,
        p_performance_classification, p_replacement_urgency,
        p_reason_code, p_reason_text, p_occurred_at, p_idempotency_key
    );

    INSERT INTO propertyai.cleaning_assignment_event (
        assignment_id, event_type, event_key, payload, occurred_at
    ) VALUES (
        v_assignment.assignment_id, 'RELEASED', 'ASSIGNMENT_RELEASED:' || p_idempotency_key,
        jsonb_build_object(
            'reason_code', 'CLEANER_UNAVAILABLE',
            'performance_classification', p_performance_classification,
            'replacement_urgency', p_replacement_urgency
        ), p_occurred_at
    );

    INSERT INTO propertyai.domain_audit_event (
        aggregate_type, aggregate_id, event_type, actor_type,
        idempotency_key, after_state, occurred_at
    ) VALUES (
        'CLEANING', v_assignment.cleaning_id, 'CLEANER_UNAVAILABLE_CONFIRMED', 'USER',
        'AUDIT:' || p_idempotency_key,
        jsonb_build_object(
            'unavailability_id', v_unavailability_id,
            'original_assignment_id', v_assignment.assignment_id,
            'cleaner_party_id', v_assignment.cleaner_party_id,
            'performance_scoring_deferred', true
        ), p_occurred_at
    );

    RETURN v_unavailability_id;
END;
$$;

CREATE OR REPLACE FUNCTION propertyai.request_original_cleaner_reassignment(
    p_unavailability_id uuid,
    p_requested_at timestamptz,
    p_idempotency_key text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
DECLARE
    v_case propertyai.cleaner_unavailability_case%ROWTYPE;
    v_cleaning propertyai.cleaning_job%ROWTYPE;
    v_existing propertyai.cleaner_reassignment_request%ROWTYPE;
    v_request_id uuid;
BEGIN
    IF p_requested_at IS NULL THEN
        RAISE EXCEPTION 'REASSIGN_REQUEST_TIME_REQUIRED';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'REASSIGN_REQUEST_IDEMPOTENCY_REQUIRED';
    END IF;

    SELECT * INTO v_existing
    FROM propertyai.cleaner_reassignment_request
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.unavailability_id <> p_unavailability_id THEN
            RAISE EXCEPTION 'REASSIGN_REQUEST_IDEMPOTENCY_CONFLICT';
        END IF;
        RETURN v_existing.reassignment_request_id;
    END IF;

    SELECT * INTO v_case
    FROM propertyai.cleaner_unavailability_case
    WHERE unavailability_id = p_unavailability_id;
    IF NOT FOUND OR v_case.unavailability_status <> 'CONFIRMED' THEN
        RAISE EXCEPTION 'REASSIGN_UNAVAILABILITY_NOT_ACTIONABLE';
    END IF;

    INSERT INTO propertyai.cleaning_schedule_guard(cleaning_id)
    VALUES (v_case.cleaning_id)
    ON CONFLICT (cleaning_id) DO NOTHING;
    PERFORM 1 FROM propertyai.cleaning_schedule_guard
    WHERE cleaning_id = v_case.cleaning_id
    FOR UPDATE;

    SELECT * INTO v_cleaning
    FROM propertyai.cleaning_job
    WHERE cleaning_id = v_case.cleaning_id
    FOR UPDATE;
    IF v_cleaning.cleaning_status <> 'OFFERING' THEN
        RAISE EXCEPTION 'REASSIGN_CLEANING_NOT_REASSIGNABLE';
    END IF;
    IF v_cleaning.current_schedule_revision_id IS NULL THEN
        RAISE EXCEPTION 'REASSIGN_CURRENT_SCHEDULE_REQUIRED';
    END IF;
    IF EXISTS (
        SELECT 1 FROM propertyai.cleaning_assignment
        WHERE cleaning_id = v_case.cleaning_id AND assignment_status = 'HARD_BOOKED'
    ) THEN
        RAISE EXCEPTION 'REASSIGN_REPLACEMENT_ALREADY_ACCEPTED';
    END IF;

    v_request_id := gen_random_uuid();
    INSERT INTO propertyai.cleaner_reassignment_request (
        reassignment_request_id, unavailability_id, cleaning_id,
        cleaner_party_id, original_assignment_id, target_schedule_revision_id,
        request_status, requested_at, idempotency_key
    ) VALUES (
        v_request_id, v_case.unavailability_id, v_case.cleaning_id,
        v_case.cleaner_party_id, v_case.original_assignment_id, v_cleaning.current_schedule_revision_id,
        'REQUESTED', p_requested_at, p_idempotency_key
    );

    INSERT INTO propertyai.domain_audit_event (
        aggregate_type, aggregate_id, event_type, actor_type,
        idempotency_key, after_state, occurred_at
    ) VALUES (
        'REASSIGNMENT_REQUEST', v_request_id, 'ORIGINAL_CLEANER_REASSIGNMENT_REQUESTED', 'USER',
        'AUDIT:' || p_idempotency_key,
        jsonb_build_object(
            'cleaning_id', v_case.cleaning_id,
            'cleaner_party_id', v_case.cleaner_party_id,
            'target_schedule_revision_id', v_cleaning.current_schedule_revision_id
        ), p_requested_at
    );

    RETURN v_request_id;
END;
$$;

CREATE OR REPLACE FUNCTION propertyai.reassign_original_cleaner(
    p_reassignment_request_id uuid,
    p_scheduled_start_at timestamptz,
    p_scheduled_end_at timestamptz,
    p_decided_at timestamptz,
    p_idempotency_key text,
    p_travel_buffer_before_minutes integer DEFAULT NULL,
    p_travel_buffer_after_minutes integer DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
DECLARE
    v_request propertyai.cleaner_reassignment_request%ROWTYPE;
    v_original propertyai.cleaning_assignment%ROWTYPE;
    v_cleaning propertyai.cleaning_job%ROWTYPE;
    v_revision propertyai.cleaning_schedule_revision%ROWTYPE;
    v_profile propertyai.cleaner_profile%ROWTYPE;
    v_party propertyai.party%ROWTYPE;
    v_property propertyai.property%ROWTYPE;
    v_access propertyai.cleaner_property_access%ROWTYPE;
    v_policy propertyai.cleaner_capacity_policy%ROWTYPE;
    v_existing propertyai.cleaning_assignment%ROWTYPE;
    v_assignment_id uuid;
    v_assignment_version text;
    v_identity_count integer;
    v_existing_jobs integer;
    v_existing_minutes bigint;
    v_buffer_before integer;
    v_buffer_after integer;
    v_conflict_window tstzrange;
    v_service_date date;
    v_campaign_id uuid;
BEGIN
    IF p_decided_at IS NULL OR p_scheduled_start_at IS NULL OR p_scheduled_end_at IS NULL THEN
        RAISE EXCEPTION 'REASSIGN_DECISION_TIME_REQUIRED';
    END IF;
    IF p_scheduled_end_at <= p_scheduled_start_at THEN
        RAISE EXCEPTION 'REASSIGN_SLOT_INVALID';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'REASSIGN_DECISION_IDEMPOTENCY_REQUIRED';
    END IF;

    SELECT * INTO v_request
    FROM propertyai.cleaner_reassignment_request
    WHERE reassignment_request_id = p_reassignment_request_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'REASSIGN_REQUEST_NOT_FOUND';
    END IF;

    SELECT * INTO v_existing
    FROM propertyai.cleaning_assignment
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.assignment_source = 'ORIGINAL_REASSIGNED'
           AND v_existing.cleaning_id = v_request.cleaning_id
           AND v_existing.cleaner_party_id = v_request.cleaner_party_id THEN
            RETURN v_existing.assignment_id;
        END IF;
        RAISE EXCEPTION 'REASSIGN_DECISION_IDEMPOTENCY_CONFLICT';
    END IF;

    INSERT INTO propertyai.cleaner_schedule_guard(cleaner_party_id)
    VALUES (v_request.cleaner_party_id)
    ON CONFLICT (cleaner_party_id) DO NOTHING;
    PERFORM 1 FROM propertyai.cleaner_schedule_guard
    WHERE cleaner_party_id = v_request.cleaner_party_id
    FOR UPDATE;

    INSERT INTO propertyai.cleaning_schedule_guard(cleaning_id)
    VALUES (v_request.cleaning_id)
    ON CONFLICT (cleaning_id) DO NOTHING;
    PERFORM 1 FROM propertyai.cleaning_schedule_guard
    WHERE cleaning_id = v_request.cleaning_id
    FOR UPDATE;

    SELECT * INTO v_request
    FROM propertyai.cleaner_reassignment_request
    WHERE reassignment_request_id = p_reassignment_request_id
    FOR UPDATE;
    IF v_request.request_status <> 'REQUESTED' THEN
        RAISE EXCEPTION 'REASSIGN_REQUEST_ALREADY_RESOLVED';
    END IF;

    SELECT * INTO v_original
    FROM propertyai.cleaning_assignment
    WHERE assignment_id = v_request.original_assignment_id;
    SELECT * INTO v_cleaning
    FROM propertyai.cleaning_job
    WHERE cleaning_id = v_request.cleaning_id
    FOR UPDATE;
    SELECT * INTO v_revision
    FROM propertyai.cleaning_schedule_revision
    WHERE cleaning_id = v_request.cleaning_id
      AND schedule_revision_id = v_request.target_schedule_revision_id;
    SELECT * INTO v_profile
    FROM propertyai.cleaner_profile
    WHERE cleaner_party_id = v_request.cleaner_party_id;
    SELECT * INTO v_party
    FROM propertyai.party
    WHERE party_id = v_request.cleaner_party_id;
    SELECT * INTO v_property
    FROM propertyai.property
    WHERE property_id = v_cleaning.property_id;

    IF v_original.assignment_id IS NULL OR v_revision.schedule_revision_id IS NULL
       OR v_profile.cleaner_party_id IS NULL OR v_party.party_id IS NULL OR v_property.property_id IS NULL THEN
        RAISE EXCEPTION 'REASSIGN_AUTHORITY_READ_FAILED';
    END IF;
    IF v_original.assignment_status <> 'RELEASED' OR v_original.end_reason_code <> 'CLEANER_UNAVAILABLE' THEN
        RAISE EXCEPTION 'REASSIGN_ORIGINAL_ASSIGNMENT_NOT_RELEASED_UNAVAILABLE';
    END IF;
    IF v_cleaning.cleaning_status <> 'OFFERING' THEN
        RAISE EXCEPTION 'REASSIGN_CLEANING_NOT_REASSIGNABLE';
    END IF;
    IF v_cleaning.current_schedule_revision_id IS DISTINCT FROM v_request.target_schedule_revision_id THEN
        RAISE EXCEPTION 'REASSIGN_STALE_SCHEDULE_REVISION';
    END IF;
    IF EXISTS (
        SELECT 1 FROM propertyai.cleaning_assignment
        WHERE cleaning_id = v_request.cleaning_id AND assignment_status = 'HARD_BOOKED'
    ) THEN
        RAISE EXCEPTION 'REASSIGN_REPLACEMENT_ALREADY_ACCEPTED';
    END IF;
    IF v_revision.required_work_minutes IS NULL THEN
        RAISE EXCEPTION 'REASSIGN_WORK_MINUTES_REQUIRED';
    END IF;
    IF p_scheduled_start_at < v_revision.service_window_start_at
       OR p_scheduled_end_at > v_revision.service_deadline_at
       OR p_scheduled_end_at - p_scheduled_start_at <> (v_revision.required_work_minutes * interval '1 minute') THEN
        RAISE EXCEPTION 'REASSIGN_SLOT_INVALID_FOR_CURRENT_SCHEDULE';
    END IF;

    IF v_profile.operational_status <> 'ACTIVE' OR v_party.active IS NOT TRUE OR v_property.active IS NOT TRUE THEN
        RAISE EXCEPTION 'REASSIGN_CURRENT_AUTHORITY_INACTIVE';
    END IF;
    SELECT count(*)::integer INTO v_identity_count
    FROM propertyai.external_identity e
    WHERE e.party_id = v_request.cleaner_party_id
      AND e.provider = 'TELEGRAM'
      AND e.revoked_at IS NULL
      AND e.provider_chat_id IS NOT NULL;
    IF v_identity_count <> 1 THEN
        RAISE EXCEPTION 'REASSIGN_TELEGRAM_IDENTITY_NOT_EXACTLY_ONE';
    END IF;

    SELECT * INTO v_access
    FROM propertyai.cleaner_property_access a
    WHERE a.cleaner_party_id = v_request.cleaner_party_id
      AND a.property_id = v_cleaning.property_id
      AND a.status = 'APPROVED'
      AND a.effective_from <= p_decided_at
      AND (a.effective_until IS NULL OR a.effective_until > p_decided_at)
    ORDER BY a.effective_from DESC, a.created_at DESC
    LIMIT 1;
    IF v_access.access_id IS NULL THEN
        RAISE EXCEPTION 'REASSIGN_PROPERTY_ACCESS_NOT_APPROVED';
    END IF;

    SELECT * INTO v_policy
    FROM propertyai.cleaner_capacity_policy p
    WHERE p.cleaner_party_id = v_request.cleaner_party_id
      AND p.effective_from <= p_decided_at
      AND (p.effective_until IS NULL OR p.effective_until > p_decided_at)
    ORDER BY p.effective_from DESC, p.policy_version DESC
    LIMIT 1;

    v_buffer_before := COALESCE(p_travel_buffer_before_minutes, v_policy.default_travel_buffer_minutes);
    v_buffer_after := COALESCE(p_travel_buffer_after_minutes, v_policy.default_travel_buffer_minutes);
    v_conflict_window := tstzrange(
        p_scheduled_start_at - (COALESCE(v_buffer_before, 0) * interval '1 minute'),
        p_scheduled_end_at + (COALESCE(v_buffer_after, 0) * interval '1 minute'), '[)'
    );

    IF EXISTS (
        SELECT 1 FROM propertyai.cleaner_availability_window w
        WHERE w.cleaner_party_id = v_request.cleaner_party_id
          AND w.availability_state = 'UNAVAILABLE'
          AND tstzrange(w.starts_at, w.ends_at, '[)') && v_conflict_window
    ) THEN
        RAISE EXCEPTION 'REASSIGN_CLEANER_UNAVAILABLE_WINDOW';
    END IF;

    v_service_date := (p_scheduled_start_at AT TIME ZONE 'Asia/Seoul')::date;
    SELECT count(*)::integer, COALESCE(sum(a.work_minutes_snapshot), 0)::bigint
      INTO v_existing_jobs, v_existing_minutes
    FROM propertyai.cleaning_assignment a
    WHERE a.cleaner_party_id = v_request.cleaner_party_id
      AND a.assignment_status = 'HARD_BOOKED'
      AND (a.scheduled_start_at AT TIME ZONE 'Asia/Seoul')::date = v_service_date;

    IF v_policy.max_daily_jobs IS NOT NULL AND v_existing_jobs + 1 > v_policy.max_daily_jobs THEN
        RAISE EXCEPTION 'REASSIGN_DAILY_JOB_LIMIT_EXCEEDED';
    END IF;
    IF v_policy.max_daily_work_minutes IS NOT NULL
       AND v_existing_minutes + v_revision.required_work_minutes > v_policy.max_daily_work_minutes THEN
        RAISE EXCEPTION 'REASSIGN_DAILY_WORK_MINUTES_EXCEEDED';
    END IF;
    IF EXISTS (
        SELECT 1 FROM propertyai.cleaning_assignment a
        WHERE a.cleaner_party_id = v_request.cleaner_party_id
          AND a.assignment_status = 'HARD_BOOKED'
          AND a.conflict_window && v_conflict_window
    ) THEN
        RAISE EXCEPTION 'REASSIGN_CLEANER_SCHEDULE_CONFLICT';
    END IF;

    v_assignment_id := gen_random_uuid();
    v_assignment_version := 'REASSIGN-' || replace(v_assignment_id::text, '-', '');
    BEGIN
        INSERT INTO propertyai.cleaning_assignment (
            assignment_id, assignment_version, idempotency_key,
            cleaning_id, schedule_revision_id, cleaner_party_id,
            assignment_source, assignment_status, accepted_at,
            scheduled_start_at, scheduled_end_at, work_minutes_snapshot,
            travel_buffer_before_minutes, travel_buffer_after_minutes,
            service_window_start_snapshot, service_deadline_snapshot,
            base_fee_krw, replacement_urgency, urgent_premium_krw,
            total_agreed_fee_krw, urgent_premium_policy_version
        ) VALUES (
            v_assignment_id, v_assignment_version, p_idempotency_key,
            v_request.cleaning_id, v_request.target_schedule_revision_id, v_request.cleaner_party_id,
            'ORIGINAL_REASSIGNED', 'HARD_BOOKED', p_decided_at,
            p_scheduled_start_at, p_scheduled_end_at, v_revision.required_work_minutes,
            v_buffer_before, v_buffer_after,
            v_revision.service_window_start_at, v_revision.service_deadline_at,
            v_original.base_fee_krw, v_original.replacement_urgency, v_original.urgent_premium_krw,
            v_original.total_agreed_fee_krw, v_original.urgent_premium_policy_version
        );
    EXCEPTION
        WHEN exclusion_violation THEN
            RAISE EXCEPTION 'REASSIGN_CLEANER_SCHEDULE_CONFLICT';
        WHEN unique_violation THEN
            RAISE EXCEPTION 'REASSIGN_ASSIGNMENT_ALREADY_WON';
    END;

    FOR v_campaign_id IN
        SELECT campaign_id FROM propertyai.cleaning_offer_campaign
        WHERE cleaning_id = v_request.cleaning_id AND campaign_status = 'OPEN'
        FOR UPDATE
    LOOP
        UPDATE propertyai.cleaning_offer_campaign
        SET campaign_status='SUPERSEDED',closed_at=p_decided_at,
            closed_reason_code='ORIGINAL_CLEANER_REASSIGNED',next_tier_open_at=NULL,
            lock_version=lock_version+1,updated_at=p_decided_at
        WHERE campaign_id=v_campaign_id;
        UPDATE propertyai.cleaning_offer_candidate
        SET candidate_status='SUPERSEDED',updated_at=p_decided_at
        WHERE campaign_id=v_campaign_id
          AND candidate_status NOT IN ('DECLINED','ACCEPTED','SUPERSEDED');
        UPDATE propertyai.business_scheduled_action
        SET action_status='CANCELLED',cancelled_at=p_decided_at,updated_at=p_decided_at
        WHERE aggregate_type='OFFER_CAMPAIGN' AND aggregate_id=v_campaign_id
          AND action_status IN ('PENDING','FAILED_RETRYABLE');
    END LOOP;

    UPDATE propertyai.cleaning_job
    SET cleaning_status='ASSIGNED',lock_version=lock_version+1,updated_at=p_decided_at
    WHERE cleaning_id=v_request.cleaning_id;

    UPDATE propertyai.cleaner_reassignment_request
    SET request_status='REASSIGNED_ORIGINAL',decided_at=p_decided_at,
        decision_key=p_idempotency_key,updated_at=p_decided_at
    WHERE reassignment_request_id=p_reassignment_request_id;

    INSERT INTO propertyai.cleaning_assignment_event(
        assignment_id,event_type,event_key,payload,occurred_at
    ) VALUES (
        v_assignment_id,'REASSIGNED','ORIGINAL_REASSIGNED:' || p_idempotency_key,
        jsonb_build_object('original_assignment_id',v_original.assignment_id,'reassignment_request_id',p_reassignment_request_id),
        p_decided_at
    );

    INSERT INTO propertyai.domain_audit_event(
        aggregate_type,aggregate_id,event_type,actor_type,idempotency_key,after_state,occurred_at
    ) VALUES (
        'REASSIGNMENT_REQUEST',p_reassignment_request_id,'ORIGINAL_CLEANER_REASSIGNED','OPERATOR',
        'AUDIT:' || p_idempotency_key,
        jsonb_build_object('new_assignment_id',v_assignment_id,'cleaner_party_id',v_request.cleaner_party_id),
        p_decided_at
    );

    RETURN v_assignment_id;
END;
$$;
