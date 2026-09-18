-- PropertyAI vNext / language-neutral PostgreSQL authority
-- Canonical DB commands for cumulative tier opening and offer acceptance.
-- These functions deliberately contain no Telegram/Notion/Calendar network effects.

CREATE OR REPLACE FUNCTION propertyai.open_next_offer_tier(
    p_campaign_id uuid,
    p_opened_at timestamptz,
    p_idempotency_key text,
    p_opening_reason_code text DEFAULT 'ELAPSED_TIME'
) RETURNS smallint
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
DECLARE
    v_campaign propertyai.cleaning_offer_campaign%ROWTYPE;
    v_existing propertyai.cleaning_offer_tier_opening%ROWTYPE;
    v_new_tier smallint;
BEGIN
    IF p_opened_at IS NULL THEN
        RAISE EXCEPTION 'OPEN_TIER_TIME_REQUIRED';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'OPEN_TIER_IDEMPOTENCY_KEY_REQUIRED';
    END IF;
    IF p_opening_reason_code NOT IN ('ELAPSED_TIME', 'URGENT_EXPANSION', 'MANUAL') THEN
        RAISE EXCEPTION 'OPEN_TIER_REASON_INVALID';
    END IF;

    SELECT * INTO v_existing
    FROM propertyai.cleaning_offer_tier_opening
    WHERE idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_existing.campaign_id <> p_campaign_id THEN
            RAISE EXCEPTION 'OPEN_TIER_IDEMPOTENCY_CONFLICT';
        END IF;
        RETURN v_existing.tier_no;
    END IF;

    SELECT * INTO v_campaign
    FROM propertyai.cleaning_offer_campaign
    WHERE campaign_id = p_campaign_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'OPEN_TIER_CAMPAIGN_NOT_FOUND';
    END IF;
    IF v_campaign.campaign_status <> 'OPEN' THEN
        RAISE EXCEPTION 'OPEN_TIER_CAMPAIGN_NOT_OPEN';
    END IF;
    IF p_opened_at > v_campaign.acceptance_cutoff_at THEN
        RAISE EXCEPTION 'OPEN_TIER_AFTER_ACCEPTANCE_CUTOFF';
    END IF;
    IF v_campaign.current_open_tier >= v_campaign.max_tier THEN
        RETURN v_campaign.current_open_tier;
    END IF;
    IF v_campaign.next_tier_open_at IS NOT NULL AND p_opened_at < v_campaign.next_tier_open_at
       AND p_opening_reason_code = 'ELAPSED_TIME' THEN
        RAISE EXCEPTION 'OPEN_TIER_NOT_DUE';
    END IF;

    v_new_tier := v_campaign.current_open_tier + 1;

    INSERT INTO propertyai.cleaning_offer_tier_opening (
        campaign_id, tier_no, opened_at, opening_reason_code, idempotency_key
    ) VALUES (
        p_campaign_id, v_new_tier, p_opened_at, p_opening_reason_code, p_idempotency_key
    );

    UPDATE propertyai.cleaning_offer_campaign
    SET current_open_tier = v_new_tier,
        next_tier_open_at = CASE
            WHEN v_new_tier < max_tier AND tier_expand_after_minutes IS NOT NULL
                THEN p_opened_at + (tier_expand_after_minutes * interval '1 minute')
            ELSE NULL
        END,
        lock_version = lock_version + 1,
        updated_at = p_opened_at
    WHERE campaign_id = p_campaign_id;

    -- IMPORTANT: no lower-tier candidate is expired or superseded here.
    -- Tier opening is cumulative and produces no notification/outbox effect by itself.
    INSERT INTO propertyai.domain_audit_event (
        aggregate_type, aggregate_id, event_type, actor_type,
        idempotency_key, after_state, occurred_at
    ) VALUES (
        'OFFER_CAMPAIGN', p_campaign_id, 'OFFER_TIER_OPENED', 'SYSTEM',
        p_idempotency_key,
        jsonb_build_object('current_open_tier', v_new_tier, 'opening_reason_code', p_opening_reason_code),
        p_opened_at
    );

    RETURN v_new_tier;
END;
$$;

CREATE OR REPLACE FUNCTION propertyai.decline_cleaning_offer(
    p_offer_candidate_id uuid,
    p_declined_at timestamptz,
    p_idempotency_key text
) RETURNS text
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
DECLARE
    v_candidate propertyai.cleaning_offer_candidate%ROWTYPE;
    v_campaign propertyai.cleaning_offer_campaign%ROWTYPE;
BEGIN
    IF p_declined_at IS NULL THEN
        RAISE EXCEPTION 'DECLINE_TIME_REQUIRED';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'DECLINE_IDEMPOTENCY_KEY_REQUIRED';
    END IF;

    PERFORM 1
    FROM propertyai.domain_audit_event
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN 'ALREADY_DECLINED';
    END IF;

    SELECT * INTO v_candidate
    FROM propertyai.cleaning_offer_candidate
    WHERE offer_candidate_id = p_offer_candidate_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'DECLINE_CANDIDATE_NOT_FOUND';
    END IF;

    SELECT * INTO v_campaign
    FROM propertyai.cleaning_offer_campaign
    WHERE campaign_id = v_candidate.campaign_id
    FOR UPDATE;

    IF v_candidate.candidate_status = 'DECLINED' THEN
        RETURN 'ALREADY_DECLINED';
    END IF;
    IF v_candidate.candidate_status IN ('ACCEPTED', 'SUPERSEDED') THEN
        RAISE EXCEPTION 'DECLINE_CANDIDATE_TERMINAL';
    END IF;
    IF v_campaign.campaign_status <> 'OPEN' THEN
        RAISE EXCEPTION 'DECLINE_CAMPAIGN_NOT_OPEN';
    END IF;

    UPDATE propertyai.cleaning_offer_candidate
    SET candidate_status = 'DECLINED',
        declined_at = p_declined_at,
        accepted_at = NULL,
        updated_at = p_declined_at
    WHERE offer_candidate_id = p_offer_candidate_id;

    INSERT INTO propertyai.domain_audit_event (
        aggregate_type, aggregate_id, event_type, actor_type,
        idempotency_key, after_state, occurred_at
    ) VALUES (
        'OFFER_CANDIDATE', p_offer_candidate_id, 'OFFER_DECLINED', 'USER',
        p_idempotency_key,
        jsonb_build_object('campaign_id', v_candidate.campaign_id, 'cleaner_party_id', v_candidate.cleaner_party_id),
        p_declined_at
    );

    -- Decline does NOT transfer ownership. Whether it immediately opens another tier
    -- remains a campaign policy / scheduled action decision.
    RETURN 'DECLINED';
END;
$$;

CREATE OR REPLACE FUNCTION propertyai.accept_cleaning_offer(
    p_offer_candidate_id uuid,
    p_scheduled_start_at timestamptz,
    p_scheduled_end_at timestamptz,
    p_accepted_at timestamptz,
    p_idempotency_key text,
    p_travel_buffer_before_minutes integer DEFAULT NULL,
    p_travel_buffer_after_minutes integer DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
DECLARE
    v_candidate propertyai.cleaning_offer_candidate%ROWTYPE;
    v_campaign propertyai.cleaning_offer_campaign%ROWTYPE;
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
    v_open_tier_at_accept smallint;
    v_existing_jobs integer;
    v_existing_minutes bigint;
    v_buffer_before integer;
    v_buffer_after integer;
    v_conflict_window tstzrange;
    v_service_date date;
BEGIN
    IF p_accepted_at IS NULL OR p_scheduled_start_at IS NULL OR p_scheduled_end_at IS NULL THEN
        RAISE EXCEPTION 'ACCEPT_TIME_REQUIRED';
    END IF;
    IF p_scheduled_end_at <= p_scheduled_start_at THEN
        RAISE EXCEPTION 'ACCEPT_SLOT_INVALID';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'ACCEPT_IDEMPOTENCY_KEY_REQUIRED';
    END IF;
    IF p_travel_buffer_before_minutes IS NOT NULL AND p_travel_buffer_before_minutes < 0 THEN
        RAISE EXCEPTION 'ACCEPT_BUFFER_INVALID';
    END IF;
    IF p_travel_buffer_after_minutes IS NOT NULL AND p_travel_buffer_after_minutes < 0 THEN
        RAISE EXCEPTION 'ACCEPT_BUFFER_INVALID';
    END IF;

    -- Fast idempotent read; rechecked after the scheduling guards are locked.
    SELECT * INTO v_existing
    FROM propertyai.cleaning_assignment
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.offer_candidate_id = p_offer_candidate_id THEN
            RETURN v_existing.assignment_id;
        END IF;
        RAISE EXCEPTION 'ACCEPT_IDEMPOTENCY_CONFLICT';
    END IF;

    SELECT * INTO v_candidate
    FROM propertyai.cleaning_offer_candidate
    WHERE offer_candidate_id = p_offer_candidate_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ACCEPT_CANDIDATE_NOT_FOUND';
    END IF;

    -- Deterministic lock order for every acceptance path:
    -- 1) Cleaner schedule guard, 2) Cleaning schedule guard.
    INSERT INTO propertyai.cleaner_schedule_guard(cleaner_party_id)
    VALUES (v_candidate.cleaner_party_id)
    ON CONFLICT (cleaner_party_id) DO NOTHING;

    PERFORM 1
    FROM propertyai.cleaner_schedule_guard
    WHERE cleaner_party_id = v_candidate.cleaner_party_id
    FOR UPDATE;

    SELECT cleaning_id INTO v_cleaning.cleaning_id
    FROM propertyai.cleaning_offer_campaign
    WHERE campaign_id = v_candidate.campaign_id;
    IF v_cleaning.cleaning_id IS NULL THEN
        RAISE EXCEPTION 'ACCEPT_CAMPAIGN_NOT_FOUND';
    END IF;

    INSERT INTO propertyai.cleaning_schedule_guard(cleaning_id)
    VALUES (v_cleaning.cleaning_id)
    ON CONFLICT (cleaning_id) DO NOTHING;

    PERFORM 1
    FROM propertyai.cleaning_schedule_guard
    WHERE cleaning_id = v_cleaning.cleaning_id
    FOR UPDATE;

    SELECT * INTO v_existing
    FROM propertyai.cleaning_assignment
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.offer_candidate_id = p_offer_candidate_id THEN
            RETURN v_existing.assignment_id;
        END IF;
        RAISE EXCEPTION 'ACCEPT_IDEMPOTENCY_CONFLICT';
    END IF;

    -- Fresh authoritative reads after both scheduling guards are held.
    SELECT * INTO v_candidate
    FROM propertyai.cleaning_offer_candidate
    WHERE offer_candidate_id = p_offer_candidate_id
    FOR UPDATE;

    SELECT * INTO v_campaign
    FROM propertyai.cleaning_offer_campaign
    WHERE campaign_id = v_candidate.campaign_id
    FOR UPDATE;

    SELECT * INTO v_cleaning
    FROM propertyai.cleaning_job
    WHERE cleaning_id = v_campaign.cleaning_id
    FOR UPDATE;

    SELECT * INTO v_revision
    FROM propertyai.cleaning_schedule_revision
    WHERE cleaning_id = v_campaign.cleaning_id
      AND schedule_revision_id = v_campaign.schedule_revision_id;

    SELECT * INTO v_profile
    FROM propertyai.cleaner_profile
    WHERE cleaner_party_id = v_candidate.cleaner_party_id;

    SELECT * INTO v_party
    FROM propertyai.party
    WHERE party_id = v_candidate.cleaner_party_id;

    SELECT * INTO v_property
    FROM propertyai.property
    WHERE property_id = v_cleaning.property_id;

    IF v_campaign.campaign_id IS NULL OR v_revision.schedule_revision_id IS NULL
       OR v_profile.cleaner_party_id IS NULL OR v_party.party_id IS NULL OR v_property.property_id IS NULL THEN
        RAISE EXCEPTION 'ACCEPT_AUTHORITY_READ_FAILED';
    END IF;

    IF v_candidate.candidate_status IN ('DECLINED', 'SUPERSEDED') THEN
        RAISE EXCEPTION 'ACCEPT_CANDIDATE_TERMINAL';
    END IF;
    IF v_campaign.campaign_status <> 'OPEN' THEN
        RAISE EXCEPTION 'ACCEPT_CAMPAIGN_NOT_OPEN';
    END IF;
    IF p_accepted_at < v_campaign.opened_at THEN
        RAISE EXCEPTION 'ACCEPT_BEFORE_CAMPAIGN_OPEN';
    END IF;
    IF p_accepted_at > v_campaign.acceptance_cutoff_at THEN
        RAISE EXCEPTION 'ACCEPT_AFTER_CUTOFF';
    END IF;
    IF v_cleaning.cleaning_status NOT IN ('PLANNED', 'OFFERING') THEN
        RAISE EXCEPTION 'ACCEPT_CLEANING_NOT_ASSIGNABLE';
    END IF;
    IF v_cleaning.current_schedule_revision_id IS DISTINCT FROM v_campaign.schedule_revision_id THEN
        RAISE EXCEPTION 'ACCEPT_STALE_SCHEDULE_REVISION';
    END IF;
    IF v_revision.required_work_minutes IS NULL THEN
        RAISE EXCEPTION 'ACCEPT_WORK_MINUTES_REQUIRED';
    END IF;
    IF p_scheduled_start_at < v_revision.service_window_start_at
       OR p_scheduled_end_at > v_revision.service_deadline_at THEN
        RAISE EXCEPTION 'ACCEPT_SLOT_OUTSIDE_SERVICE_WINDOW';
    END IF;
    IF p_scheduled_end_at - p_scheduled_start_at
       <> (v_revision.required_work_minutes * interval '1 minute') THEN
        RAISE EXCEPTION 'ACCEPT_SLOT_DURATION_MISMATCH';
    END IF;

    IF v_profile.operational_status <> 'ACTIVE' OR v_party.active IS NOT TRUE THEN
        RAISE EXCEPTION 'ACCEPT_CLEANER_NOT_ACTIVE';
    END IF;
    IF v_property.active IS NOT TRUE THEN
        RAISE EXCEPTION 'ACCEPT_PROPERTY_NOT_ACTIVE';
    END IF;

    SELECT count(*)::integer INTO v_identity_count
    FROM propertyai.external_identity e
    WHERE e.party_id = v_candidate.cleaner_party_id
      AND e.provider = 'TELEGRAM'
      AND e.revoked_at IS NULL
      AND e.provider_chat_id IS NOT NULL;
    IF v_identity_count <> 1 THEN
        RAISE EXCEPTION 'ACCEPT_TELEGRAM_IDENTITY_NOT_EXACTLY_ONE';
    END IF;

    SELECT * INTO v_access
    FROM propertyai.cleaner_property_access a
    WHERE a.cleaner_party_id = v_candidate.cleaner_party_id
      AND a.property_id = v_cleaning.property_id
      AND a.status = 'APPROVED'
      AND a.effective_from <= p_accepted_at
      AND (a.effective_until IS NULL OR a.effective_until > p_accepted_at)
    ORDER BY a.effective_from DESC, a.created_at DESC
    LIMIT 1;

    IF v_access.access_id IS NULL THEN
        RAISE EXCEPTION 'ACCEPT_PROPERTY_ACCESS_NOT_APPROVED';
    END IF;

    SELECT max(o.tier_no)::smallint INTO v_open_tier_at_accept
    FROM propertyai.cleaning_offer_tier_opening o
    WHERE o.campaign_id = v_campaign.campaign_id
      AND o.opened_at <= p_accepted_at;
    IF v_open_tier_at_accept IS NULL THEN
        RAISE EXCEPTION 'ACCEPT_NO_TIER_OPEN_AT_ACCEPTED_TIME';
    END IF;
    IF v_access.offer_tier > v_open_tier_at_accept THEN
        RAISE EXCEPTION 'ACCEPT_TIER_NOT_OPEN';
    END IF;

    SELECT * INTO v_policy
    FROM propertyai.cleaner_capacity_policy p
    WHERE p.cleaner_party_id = v_candidate.cleaner_party_id
      AND p.effective_from <= p_accepted_at
      AND (p.effective_until IS NULL OR p.effective_until > p_accepted_at)
    ORDER BY p.effective_from DESC, p.policy_version DESC
    LIMIT 1;

    v_buffer_before := COALESCE(p_travel_buffer_before_minutes, v_policy.default_travel_buffer_minutes);
    v_buffer_after := COALESCE(p_travel_buffer_after_minutes, v_policy.default_travel_buffer_minutes);
    v_conflict_window := tstzrange(
        p_scheduled_start_at - (COALESCE(v_buffer_before, 0) * interval '1 minute'),
        p_scheduled_end_at + (COALESCE(v_buffer_after, 0) * interval '1 minute'),
        '[)'
    );

    IF EXISTS (
        SELECT 1
        FROM propertyai.cleaner_availability_window w
        WHERE w.cleaner_party_id = v_candidate.cleaner_party_id
          AND w.availability_state = 'UNAVAILABLE'
          AND tstzrange(w.starts_at, w.ends_at, '[)') && v_conflict_window
    ) THEN
        RAISE EXCEPTION 'ACCEPT_CLEANER_UNAVAILABLE_WINDOW';
    END IF;

    v_service_date := (p_scheduled_start_at AT TIME ZONE 'Asia/Seoul')::date;
    SELECT count(*)::integer, COALESCE(sum(a.work_minutes_snapshot), 0)::bigint
      INTO v_existing_jobs, v_existing_minutes
    FROM propertyai.cleaning_assignment a
    WHERE a.cleaner_party_id = v_candidate.cleaner_party_id
      AND a.assignment_status = 'HARD_BOOKED'
      AND (a.scheduled_start_at AT TIME ZONE 'Asia/Seoul')::date = v_service_date;

    IF v_policy.max_daily_jobs IS NOT NULL
       AND v_existing_jobs + 1 > v_policy.max_daily_jobs THEN
        RAISE EXCEPTION 'ACCEPT_DAILY_JOB_LIMIT_EXCEEDED';
    END IF;
    IF v_policy.max_daily_work_minutes IS NOT NULL
       AND v_existing_minutes + v_revision.required_work_minutes > v_policy.max_daily_work_minutes THEN
        RAISE EXCEPTION 'ACCEPT_DAILY_WORK_MINUTES_EXCEEDED';
    END IF;

    -- A pre-check gives a domain-specific error; the GiST exclusion constraint below
    -- remains the final race-safe DB fence.
    IF EXISTS (
        SELECT 1
        FROM propertyai.cleaning_assignment a
        WHERE a.cleaner_party_id = v_candidate.cleaner_party_id
          AND a.assignment_status = 'HARD_BOOKED'
          AND a.conflict_window && v_conflict_window
    ) THEN
        RAISE EXCEPTION 'ACCEPT_CLEANER_SCHEDULE_CONFLICT';
    END IF;

    v_assignment_id := gen_random_uuid();
    v_assignment_version := 'DB-' || replace(v_assignment_id::text, '-', '');

    BEGIN
        INSERT INTO propertyai.cleaning_assignment (
            assignment_id, assignment_version, idempotency_key,
            cleaning_id, schedule_revision_id, campaign_id, offer_candidate_id,
            cleaner_party_id, assignment_source, assignment_status, accepted_at,
            scheduled_start_at, scheduled_end_at, work_minutes_snapshot,
            travel_buffer_before_minutes, travel_buffer_after_minutes,
            service_window_start_snapshot, service_deadline_snapshot,
            base_fee_krw, replacement_urgency, urgent_premium_krw,
            total_agreed_fee_krw, urgent_premium_policy_version
        ) VALUES (
            v_assignment_id, v_assignment_version, p_idempotency_key,
            v_campaign.cleaning_id, v_campaign.schedule_revision_id, v_campaign.campaign_id, v_candidate.offer_candidate_id,
            v_candidate.cleaner_party_id, 'OFFER_ACCEPTED', 'HARD_BOOKED', p_accepted_at,
            p_scheduled_start_at, p_scheduled_end_at, v_revision.required_work_minutes,
            v_buffer_before, v_buffer_after,
            v_revision.service_window_start_at, v_revision.service_deadline_at,
            v_campaign.base_fee_krw, v_campaign.replacement_urgency, v_campaign.urgent_premium_krw,
            v_campaign.total_agreed_fee_krw, v_campaign.urgent_premium_policy_version
        );
    EXCEPTION
        WHEN exclusion_violation THEN
            RAISE EXCEPTION 'ACCEPT_CLEANER_SCHEDULE_CONFLICT';
        WHEN unique_violation THEN
            RAISE EXCEPTION 'ACCEPT_ASSIGNMENT_ALREADY_WON';
    END;

    UPDATE propertyai.cleaning_offer_candidate
    SET candidate_status = CASE WHEN offer_candidate_id = p_offer_candidate_id THEN 'ACCEPTED' ELSE 'SUPERSEDED' END,
        accepted_at = CASE WHEN offer_candidate_id = p_offer_candidate_id THEN p_accepted_at ELSE NULL END,
        updated_at = p_accepted_at
    WHERE campaign_id = v_campaign.campaign_id
      AND candidate_status NOT IN ('DECLINED', 'SUPERSEDED');

    UPDATE propertyai.cleaning_offer_campaign
    SET campaign_status = 'CLOSED',
        closed_at = p_accepted_at,
        closed_reason_code = 'ASSIGNMENT_ACCEPTED',
        next_tier_open_at = NULL,
        lock_version = lock_version + 1,
        updated_at = p_accepted_at
    WHERE campaign_id = v_campaign.campaign_id;

    UPDATE propertyai.cleaning_job
    SET cleaning_status = 'ASSIGNED',
        lock_version = lock_version + 1,
        updated_at = p_accepted_at
    WHERE cleaning_id = v_campaign.cleaning_id;

    UPDATE propertyai.business_scheduled_action
    SET action_status = 'CANCELLED',
        cancelled_at = p_accepted_at,
        updated_at = p_accepted_at
    WHERE aggregate_type = 'OFFER_CAMPAIGN'
      AND aggregate_id = v_campaign.campaign_id
      AND action_status IN ('PENDING', 'FAILED_RETRYABLE')
      AND action_type IN ('OPEN_NEXT_TIER', 'OFFER_CUTOFF');

    INSERT INTO propertyai.cleaning_assignment_event (
        assignment_id, event_type, event_key, payload, occurred_at
    ) VALUES (
        v_assignment_id,
        'ACCEPTED',
        'ASSIGNMENT_ACCEPTED:' || p_idempotency_key,
        jsonb_build_object(
            'campaign_id', v_campaign.campaign_id,
            'offer_candidate_id', p_offer_candidate_id,
            'cleaner_party_id', v_candidate.cleaner_party_id,
            'schedule_revision_id', v_campaign.schedule_revision_id
        ),
        p_accepted_at
    );

    INSERT INTO propertyai.domain_audit_event (
        aggregate_type, aggregate_id, event_type, actor_type,
        idempotency_key, after_state, occurred_at
    ) VALUES (
        'CLEANING_ASSIGNMENT', v_assignment_id, 'ASSIGNMENT_ACCEPTED', 'USER',
        'AUDIT:' || p_idempotency_key,
        jsonb_build_object(
            'cleaning_id', v_campaign.cleaning_id,
            'cleaner_party_id', v_candidate.cleaner_party_id,
            'scheduled_start_at', p_scheduled_start_at,
            'scheduled_end_at', p_scheduled_end_at,
            'current_open_tier', v_campaign.current_open_tier,
            'open_tier_at_accepted_time', v_open_tier_at_accept,
            'accepted_access_tier', v_access.offer_tier
        ),
        p_accepted_at
    );

    RETURN v_assignment_id;
END;
$$;

-- Checkout/date changes can invalidate open offers and can make an existing hard booking conflict.
-- The old hard booking is never silently moved or deleted; it becomes an explicit reconciliation case.
CREATE TABLE propertyai.cleaning_schedule_reconciliation (
    schedule_reconciliation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cleaning_id uuid NOT NULL REFERENCES propertyai.cleaning_job(cleaning_id),
    from_schedule_revision_id uuid NOT NULL,
    to_schedule_revision_id uuid NOT NULL,
    hard_booked_assignment_id uuid REFERENCES propertyai.cleaning_assignment(assignment_id),
    reconciliation_status text NOT NULL DEFAULT 'PENDING',
    reason_code text NOT NULL,
    source_ref text,
    idempotency_key text NOT NULL UNIQUE,
    scheduled_action_id uuid REFERENCES propertyai.business_scheduled_action(scheduled_action_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolution_code text,
    CHECK (from_schedule_revision_id <> to_schedule_revision_id),
    CHECK (reconciliation_status IN ('PENDING', 'RESOLVED', 'CANCELLED')),
    CHECK ((reconciliation_status = 'RESOLVED') = (resolved_at IS NOT NULL)),
    FOREIGN KEY (cleaning_id, from_schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    FOREIGN KEY (cleaning_id, to_schedule_revision_id)
        REFERENCES propertyai.cleaning_schedule_revision(cleaning_id, schedule_revision_id),
    UNIQUE (cleaning_id, to_schedule_revision_id)
);

CREATE INDEX idx_schedule_reconciliation_pending
    ON propertyai.cleaning_schedule_reconciliation(created_at, schedule_reconciliation_id)
    WHERE reconciliation_status = 'PENDING';

CREATE OR REPLACE FUNCTION propertyai.activate_cleaning_schedule_revision(
    p_cleaning_id uuid,
    p_to_schedule_revision_id uuid,
    p_activated_at timestamptz,
    p_reason_code text,
    p_source_ref text,
    p_idempotency_key text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = propertyai, pg_temp
AS $$
DECLARE
    v_cleaning propertyai.cleaning_job%ROWTYPE;
    v_to propertyai.cleaning_schedule_revision%ROWTYPE;
    v_from_id uuid;
    v_assignment propertyai.cleaning_assignment%ROWTYPE;
    v_campaign_id uuid;
    v_reconciliation_id uuid;
    v_action_id uuid;
    v_existing propertyai.cleaning_schedule_reconciliation%ROWTYPE;
    v_receipt propertyai.domain_audit_event%ROWTYPE;
BEGIN
    IF p_activated_at IS NULL THEN
        RAISE EXCEPTION 'SCHEDULE_ACTIVATION_TIME_REQUIRED';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'SCHEDULE_ACTIVATION_IDEMPOTENCY_REQUIRED';
    END IF;

    SELECT * INTO v_existing
    FROM propertyai.cleaning_schedule_reconciliation
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.cleaning_id <> p_cleaning_id
           OR v_existing.to_schedule_revision_id <> p_to_schedule_revision_id THEN
            RAISE EXCEPTION 'SCHEDULE_ACTIVATION_IDEMPOTENCY_CONFLICT';
        END IF;
        RETURN v_existing.schedule_reconciliation_id;
    END IF;

    SELECT * INTO v_receipt
    FROM propertyai.domain_audit_event
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_receipt.aggregate_type <> 'CLEANING'
           OR v_receipt.aggregate_id <> p_cleaning_id
           OR v_receipt.after_state ->> 'to_schedule_revision_id' <> p_to_schedule_revision_id::text THEN
            RAISE EXCEPTION 'SCHEDULE_ACTIVATION_IDEMPOTENCY_CONFLICT';
        END IF;
        RETURN NULL;
    END IF;

    INSERT INTO propertyai.cleaning_schedule_guard(cleaning_id)
    VALUES (p_cleaning_id)
    ON CONFLICT (cleaning_id) DO NOTHING;

    PERFORM 1
    FROM propertyai.cleaning_schedule_guard
    WHERE cleaning_id = p_cleaning_id
    FOR UPDATE;

    SELECT * INTO v_cleaning
    FROM propertyai.cleaning_job
    WHERE cleaning_id = p_cleaning_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'SCHEDULE_CLEANING_NOT_FOUND';
    END IF;

    SELECT * INTO v_to
    FROM propertyai.cleaning_schedule_revision
    WHERE cleaning_id = p_cleaning_id
      AND schedule_revision_id = p_to_schedule_revision_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'SCHEDULE_REVISION_NOT_FOUND_FOR_CLEANING';
    END IF;

    v_from_id := v_cleaning.current_schedule_revision_id;
    IF v_from_id IS NULL THEN
        UPDATE propertyai.cleaning_job
        SET current_schedule_revision_id = p_to_schedule_revision_id,
            updated_at = p_activated_at,
            lock_version = lock_version + 1
        WHERE cleaning_id = p_cleaning_id;

        INSERT INTO propertyai.cleaning_schedule_revision_event (
            cleaning_id, schedule_revision_id, event_type, source_ref, payload, occurred_at
        ) VALUES (
            p_cleaning_id, p_to_schedule_revision_id, 'ACTIVATED', p_source_ref,
            jsonb_build_object('reason_code', p_reason_code), p_activated_at
        );
        INSERT INTO propertyai.domain_audit_event (
            aggregate_type, aggregate_id, event_type, actor_type,
            idempotency_key, source_ref, after_state, occurred_at
        ) VALUES (
            'CLEANING', p_cleaning_id, 'SCHEDULE_REVISION_ACTIVATED', 'SYSTEM',
            p_idempotency_key, p_source_ref,
            jsonb_build_object('from_schedule_revision_id', NULL, 'to_schedule_revision_id', p_to_schedule_revision_id),
            p_activated_at
        );
        RETURN NULL;
    END IF;

    IF v_from_id = p_to_schedule_revision_id THEN
        INSERT INTO propertyai.domain_audit_event (
            aggregate_type, aggregate_id, event_type, actor_type,
            idempotency_key, source_ref, after_state, occurred_at
        ) VALUES (
            'CLEANING', p_cleaning_id, 'SCHEDULE_REVISION_ALREADY_ACTIVE', 'SYSTEM',
            p_idempotency_key, p_source_ref,
            jsonb_build_object('from_schedule_revision_id', v_from_id, 'to_schedule_revision_id', p_to_schedule_revision_id),
            p_activated_at
        );
        RETURN NULL;
    END IF;

    UPDATE propertyai.cleaning_job
    SET current_schedule_revision_id = p_to_schedule_revision_id,
        updated_at = p_activated_at,
        lock_version = lock_version + 1
    WHERE cleaning_id = p_cleaning_id;

    INSERT INTO propertyai.cleaning_schedule_revision_event (
        cleaning_id, schedule_revision_id, event_type, source_ref, payload, occurred_at
    ) VALUES (
        p_cleaning_id, p_to_schedule_revision_id, 'ACTIVATED', p_source_ref,
        jsonb_build_object('reason_code', p_reason_code, 'from_schedule_revision_id', v_from_id), p_activated_at
    );

    FOR v_campaign_id IN
        SELECT campaign_id
        FROM propertyai.cleaning_offer_campaign
        WHERE cleaning_id = p_cleaning_id
          AND campaign_status = 'OPEN'
          AND schedule_revision_id <> p_to_schedule_revision_id
        FOR UPDATE
    LOOP
        UPDATE propertyai.cleaning_offer_campaign
        SET campaign_status = 'SUPERSEDED',
            closed_at = p_activated_at,
            closed_reason_code = 'SCHEDULE_REVISION_CHANGED',
            next_tier_open_at = NULL,
            lock_version = lock_version + 1,
            updated_at = p_activated_at
        WHERE campaign_id = v_campaign_id;

        UPDATE propertyai.cleaning_offer_candidate
        SET candidate_status = 'SUPERSEDED',
            updated_at = p_activated_at
        WHERE campaign_id = v_campaign_id
          AND candidate_status NOT IN ('DECLINED', 'ACCEPTED', 'SUPERSEDED');

        UPDATE propertyai.business_scheduled_action
        SET action_status = 'CANCELLED',
            cancelled_at = p_activated_at,
            updated_at = p_activated_at
        WHERE aggregate_type = 'OFFER_CAMPAIGN'
          AND aggregate_id = v_campaign_id
          AND action_status IN ('PENDING', 'FAILED_RETRYABLE');
    END LOOP;

    SELECT * INTO v_assignment
    FROM propertyai.cleaning_assignment
    WHERE cleaning_id = p_cleaning_id
      AND assignment_status = 'HARD_BOOKED'
    FOR UPDATE;

    IF v_assignment.assignment_id IS NULL THEN
        INSERT INTO propertyai.domain_audit_event (
            aggregate_type, aggregate_id, event_type, actor_type,
            idempotency_key, source_ref, after_state, occurred_at
        ) VALUES (
            'CLEANING', p_cleaning_id, 'SCHEDULE_REVISION_ACTIVATED', 'SYSTEM',
            p_idempotency_key, p_source_ref,
            jsonb_build_object('from_schedule_revision_id', v_from_id, 'to_schedule_revision_id', p_to_schedule_revision_id),
            p_activated_at
        );
        RETURN NULL;
    END IF;

    v_reconciliation_id := gen_random_uuid();
    v_action_id := gen_random_uuid();

    INSERT INTO propertyai.business_scheduled_action (
        scheduled_action_id, action_type, aggregate_type, aggregate_id,
        due_at, action_status, idempotency_key, payload
    ) VALUES (
        v_action_id, 'RECONCILE_SCHEDULE_REVISION', 'CLEANING', p_cleaning_id,
        p_activated_at, 'PENDING', 'ACTION:' || p_idempotency_key,
        jsonb_build_object(
            'cleaning_id', p_cleaning_id,
            'assignment_id', v_assignment.assignment_id,
            'from_schedule_revision_id', v_from_id,
            'to_schedule_revision_id', p_to_schedule_revision_id
        )
    );

    INSERT INTO propertyai.cleaning_schedule_reconciliation (
        schedule_reconciliation_id, cleaning_id,
        from_schedule_revision_id, to_schedule_revision_id,
        hard_booked_assignment_id, reconciliation_status,
        reason_code, source_ref, idempotency_key, scheduled_action_id
    ) VALUES (
        v_reconciliation_id, p_cleaning_id,
        v_from_id, p_to_schedule_revision_id,
        v_assignment.assignment_id, 'PENDING',
        p_reason_code, p_source_ref, p_idempotency_key, v_action_id
    );

    -- Existing HARD_BOOKED is intentionally preserved until an explicit reschedule/release decision.
    INSERT INTO propertyai.domain_audit_event (
        aggregate_type, aggregate_id, event_type, actor_type,
        idempotency_key, source_ref, after_state, occurred_at
    ) VALUES (
        'CLEANING', p_cleaning_id, 'SCHEDULE_RECONCILIATION_REQUIRED', 'SYSTEM',
        'AUDIT:' || p_idempotency_key, p_source_ref,
        jsonb_build_object(
            'assignment_id', v_assignment.assignment_id,
            'from_schedule_revision_id', v_from_id,
            'to_schedule_revision_id', p_to_schedule_revision_id
        ),
        p_activated_at
    );

    RETURN v_reconciliation_id;
END;
$$;
