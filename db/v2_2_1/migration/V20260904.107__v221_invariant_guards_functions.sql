SET ROLE propertyai_owner;

-- ---------- Immutable / transition guards ----------

CREATE FUNCTION propertyai.tg_guard_property_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
BEGIN
    IF NEW.property_id IS DISTINCT FROM OLD.property_id
       OR NEW.property_code IS DISTINCT FROM OLD.property_code
       OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'PROPERTY_IMMUTABLE_IDENTITY';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_property_guard_update
BEFORE UPDATE ON propertyai.property
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_property_update();

CREATE FUNCTION propertyai.tg_guard_schedule_block_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
BEGIN
    IF ROW(NEW.schedule_block_id, NEW.cleaner_party_id, NEW.starts_at, NEW.ends_at,
           NEW.reason_code, NEW.source_ref, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.schedule_block_id, OLD.cleaner_party_id, OLD.starts_at, OLD.ends_at,
           OLD.reason_code, OLD.source_ref, OLD.created_at) THEN
        RAISE EXCEPTION 'SCHEDULE_BLOCK_IMMUTABLE_CONTENT';
    END IF;
    IF OLD.cancelled_at IS NOT NULL AND NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at THEN
        RAISE EXCEPTION 'SCHEDULE_BLOCK_ALREADY_CANCELLED';
    END IF;
    IF OLD.cancelled_at IS NULL AND NEW.cancelled_at IS NULL THEN
        RAISE EXCEPTION 'SCHEDULE_BLOCK_UPDATE_REQUIRES_CANCEL';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_schedule_block_guard_update
BEFORE UPDATE ON propertyai.cleaner_schedule_block
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_schedule_block_update();

CREATE FUNCTION propertyai.tg_guard_reservation_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_source_changed boolean;
BEGIN
    IF ROW(NEW.reservation_id, NEW.reservation_code, NEW.property_id, NEW.rental_unit_id,
           NEW.source_channel, NEW.external_reservation_id, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.reservation_id, OLD.reservation_code, OLD.property_id, OLD.rental_unit_id,
           OLD.source_channel, OLD.external_reservation_id, OLD.created_at) THEN
        RAISE EXCEPTION 'RESERVATION_IMMUTABLE_IDENTITY';
    END IF;

    v_source_changed := ROW(NEW.reservation_status, NEW.check_in_at, NEW.check_out_at)
                        IS DISTINCT FROM
                        ROW(OLD.reservation_status, OLD.check_in_at, OLD.check_out_at);

    IF v_source_changed AND NEW.source_version <> OLD.source_version + 1 THEN
        RAISE EXCEPTION 'RESERVATION_SOURCE_VERSION_MUST_INCREMENT';
    ELSIF NOT v_source_changed AND NEW.source_version <> OLD.source_version THEN
        RAISE EXCEPTION 'RESERVATION_SOURCE_VERSION_WITHOUT_CHANGE';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_reservation_guard_update
BEFORE UPDATE ON propertyai.reservation
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_reservation_update();

CREATE FUNCTION propertyai.tg_guard_cleaning_current_revision()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_old_no integer;
    v_new_no integer;
BEGIN
    IF NEW.current_schedule_revision_id IS NOT DISTINCT FROM OLD.current_schedule_revision_id THEN
        RETURN NEW;
    END IF;

    IF OLD.current_schedule_revision_id IS NULL THEN
        IF NEW.current_schedule_revision_id IS NULL THEN
            RETURN NEW;
        END IF;
        SELECT revision_no INTO STRICT v_new_no
          FROM propertyai.cleaning_schedule_revision
         WHERE cleaning_id = NEW.cleaning_id
           AND schedule_revision_id = NEW.current_schedule_revision_id;
        IF v_new_no <> 1 THEN
            RAISE EXCEPTION 'INITIAL_SCHEDULE_REVISION_MUST_BE_1';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.current_schedule_revision_id IS NULL THEN
        RAISE EXCEPTION 'SCHEDULE_REVISION_POINTER_CANNOT_CLEAR';
    END IF;

    SELECT revision_no INTO STRICT v_old_no
      FROM propertyai.cleaning_schedule_revision
     WHERE cleaning_id = OLD.cleaning_id
       AND schedule_revision_id = OLD.current_schedule_revision_id;
    SELECT revision_no INTO STRICT v_new_no
      FROM propertyai.cleaning_schedule_revision
     WHERE cleaning_id = NEW.cleaning_id
       AND schedule_revision_id = NEW.current_schedule_revision_id;

    IF v_new_no <> v_old_no + 1 THEN
        RAISE EXCEPTION 'SCHEDULE_REVISION_POINTER_NOT_MONOTONIC';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_cleaning_current_revision_guard
BEFORE UPDATE OF current_schedule_revision_id ON propertyai.cleaning_job
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_cleaning_current_revision();

CREATE FUNCTION propertyai.tg_guard_campaign_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_status_changed boolean := NEW.campaign_status IS DISTINCT FROM OLD.campaign_status;
    v_tier_changed boolean := NEW.open_tier_floor IS DISTINCT FROM OLD.open_tier_floor;
BEGIN
    IF ROW(NEW.campaign_id, NEW.cleaning_id, NEW.schedule_revision_id, NEW.campaign_no,
           NEW.max_tier, NEW.tier_expand_after_minutes, NEW.acceptance_cutoff_at,
           NEW.base_fee_krw, NEW.replacement_urgency, NEW.urgent_premium_krw,
           NEW.total_agreed_fee_krw, NEW.urgent_premium_policy_version,
           NEW.opened_at, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.campaign_id, OLD.cleaning_id, OLD.schedule_revision_id, OLD.campaign_no,
           OLD.max_tier, OLD.tier_expand_after_minutes, OLD.acceptance_cutoff_at,
           OLD.base_fee_krw, OLD.replacement_urgency, OLD.urgent_premium_krw,
           OLD.total_agreed_fee_krw, OLD.urgent_premium_policy_version,
           OLD.opened_at, OLD.created_at) THEN
        RAISE EXCEPTION 'CAMPAIGN_IMMUTABLE_CONTENT';
    END IF;

    IF NEW.open_tier_floor < OLD.open_tier_floor THEN
        RAISE EXCEPTION 'CAMPAIGN_TIER_FLOOR_CANNOT_DECREASE';
    END IF;
    IF v_status_changed AND v_tier_changed THEN
        RAISE EXCEPTION 'CAMPAIGN_TIER_AND_TERMINAL_CHANGE_MUST_BE_SEPARATE';
    END IF;

    IF OLD.campaign_status <> 'OPEN' THEN
        IF ROW(NEW.campaign_status, NEW.open_tier_floor, NEW.closed_at, NEW.closed_reason_code)
           IS DISTINCT FROM
           ROW(OLD.campaign_status, OLD.open_tier_floor, OLD.closed_at, OLD.closed_reason_code) THEN
            RAISE EXCEPTION 'CAMPAIGN_TERMINAL_STATE_FROZEN';
        END IF;
    ELSIF v_status_changed AND NEW.campaign_status NOT IN ('CLOSED','CANCELLED') THEN
        RAISE EXCEPTION 'CAMPAIGN_INVALID_STATUS_TRANSITION';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_campaign_guard_update
BEFORE UPDATE ON propertyai.cleaning_offer_campaign
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_campaign_update();

CREATE FUNCTION propertyai.tg_guard_candidate_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_proposal_changed boolean;
    v_status_changed boolean := NEW.candidate_status IS DISTINCT FROM OLD.candidate_status;
BEGIN
    IF ROW(NEW.offer_candidate_id, NEW.campaign_id, NEW.cleaner_party_id, NEW.tier_no,
           NEW.evaluated_at, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.offer_candidate_id, OLD.campaign_id, OLD.cleaner_party_id, OLD.tier_no,
           OLD.evaluated_at, OLD.created_at) THEN
        RAISE EXCEPTION 'CANDIDATE_IMMUTABLE_BINDING';
    END IF;

    v_proposal_changed := ROW(NEW.proposed_start_at, NEW.proposed_end_at,
                              NEW.proposed_buffer_before_minutes, NEW.proposed_buffer_after_minutes,
                              NEW.buffer_basis, NEW.buffer_policy_ref)
                          IS DISTINCT FROM
                          ROW(OLD.proposed_start_at, OLD.proposed_end_at,
                              OLD.proposed_buffer_before_minutes, OLD.proposed_buffer_after_minutes,
                              OLD.buffer_basis, OLD.buffer_policy_ref);

    IF OLD.candidate_status <> 'ELIGIBLE' THEN
        IF ROW(NEW.candidate_status, NEW.proposal_version, NEW.proposed_start_at, NEW.proposed_end_at,
               NEW.proposed_buffer_before_minutes, NEW.proposed_buffer_after_minutes,
               NEW.buffer_basis, NEW.buffer_policy_ref, NEW.declined_at, NEW.accepted_at)
           IS DISTINCT FROM
           ROW(OLD.candidate_status, OLD.proposal_version, OLD.proposed_start_at, OLD.proposed_end_at,
               OLD.proposed_buffer_before_minutes, OLD.proposed_buffer_after_minutes,
               OLD.buffer_basis, OLD.buffer_policy_ref, OLD.declined_at, OLD.accepted_at) THEN
            RAISE EXCEPTION 'CANDIDATE_TERMINAL_STATE_FROZEN';
        END IF;
        RETURN NEW;
    END IF;

    IF v_status_changed THEN
        IF v_proposal_changed OR NEW.proposal_version <> OLD.proposal_version THEN
            RAISE EXCEPTION 'CANDIDATE_PROPOSAL_AND_TERMINAL_CHANGE_MUST_BE_SEPARATE';
        END IF;
        IF NEW.candidate_status NOT IN ('DECLINED','ACCEPTED') THEN
            RAISE EXCEPTION 'CANDIDATE_INVALID_STATUS_TRANSITION';
        END IF;
    ELSIF v_proposal_changed THEN
        IF NEW.candidate_status <> 'ELIGIBLE' OR NEW.proposal_version <> OLD.proposal_version + 1 THEN
            RAISE EXCEPTION 'CANDIDATE_PROPOSAL_VERSION_MUST_INCREMENT';
        END IF;
        IF NEW.declined_at IS DISTINCT FROM OLD.declined_at OR NEW.accepted_at IS DISTINCT FROM OLD.accepted_at THEN
            RAISE EXCEPTION 'CANDIDATE_PROPOSAL_CHANGE_CANNOT_SET_TERMINAL_TIME';
        END IF;
    ELSIF NEW.proposal_version <> OLD.proposal_version THEN
        RAISE EXCEPTION 'CANDIDATE_VERSION_WITHOUT_PROPOSAL_CHANGE';
    ELSIF NEW.declined_at IS DISTINCT FROM OLD.declined_at OR NEW.accepted_at IS DISTINCT FROM OLD.accepted_at THEN
        RAISE EXCEPTION 'CANDIDATE_TERMINAL_TIME_WITHOUT_STATUS_CHANGE';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_candidate_guard_update
BEFORE UPDATE ON propertyai.cleaning_offer_candidate
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_candidate_update();

CREATE FUNCTION propertyai.tg_derive_assignment_busy_window()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
BEGIN
    NEW.busy_window := tstzrange(
        NEW.scheduled_start_at - make_interval(mins => NEW.travel_buffer_before_minutes),
        NEW.scheduled_end_at + make_interval(mins => NEW.travel_buffer_after_minutes),
        '[)'
    );
    RETURN NEW;
END
$$;

CREATE TRIGGER a10_assignment_derive_busy_window
BEFORE INSERT OR UPDATE ON propertyai.cleaning_assignment
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_derive_assignment_busy_window();

CREATE FUNCTION propertyai.tg_validate_assignment_offer_provenance()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_candidate propertyai.cleaning_offer_candidate%ROWTYPE;
    v_campaign propertyai.cleaning_offer_campaign%ROWTYPE;
    v_revision propertyai.cleaning_schedule_revision%ROWTYPE;
BEGIN
    IF NEW.assignment_source <> 'OFFER_ACCEPTED' THEN
        RETURN NEW;
    END IF;

    SELECT * INTO STRICT v_candidate
      FROM propertyai.cleaning_offer_candidate
     WHERE campaign_id = NEW.campaign_id
       AND offer_candidate_id = NEW.offer_candidate_id
       AND cleaner_party_id = NEW.cleaner_party_id
       AND proposal_version = NEW.accepted_proposal_version;

    IF v_candidate.candidate_status = 'DECLINED' THEN
        RAISE EXCEPTION 'ASSIGNMENT_CANDIDATE_DECLINED';
    END IF;

    SELECT * INTO STRICT v_campaign
      FROM propertyai.cleaning_offer_campaign
     WHERE campaign_id = NEW.campaign_id
       AND cleaning_id = NEW.cleaning_id
       AND schedule_revision_id = NEW.schedule_revision_id;

    SELECT * INTO STRICT v_revision
      FROM propertyai.cleaning_schedule_revision
     WHERE cleaning_id = NEW.cleaning_id
       AND schedule_revision_id = NEW.schedule_revision_id;

    IF ROW(NEW.scheduled_start_at, NEW.scheduled_end_at,
           NEW.travel_buffer_before_minutes, NEW.travel_buffer_after_minutes,
           NEW.buffer_basis, NEW.buffer_policy_ref)
       IS DISTINCT FROM
       ROW(v_candidate.proposed_start_at, v_candidate.proposed_end_at,
           v_candidate.proposed_buffer_before_minutes, v_candidate.proposed_buffer_after_minutes,
           v_candidate.buffer_basis, v_candidate.buffer_policy_ref) THEN
        RAISE EXCEPTION 'ASSIGNMENT_CANDIDATE_PROPOSAL_MISMATCH';
    END IF;

    IF ROW(NEW.base_fee_krw, NEW.replacement_urgency, NEW.urgent_premium_krw,
           NEW.total_agreed_fee_krw, NEW.urgent_premium_policy_version)
       IS DISTINCT FROM
       ROW(v_campaign.base_fee_krw, v_campaign.replacement_urgency, v_campaign.urgent_premium_krw,
           v_campaign.total_agreed_fee_krw, v_campaign.urgent_premium_policy_version) THEN
        RAISE EXCEPTION 'ASSIGNMENT_CAMPAIGN_FINANCIAL_MISMATCH';
    END IF;

    IF v_revision.required_work_minutes IS NULL
       OR NEW.work_minutes_snapshot <> v_revision.required_work_minutes THEN
        RAISE EXCEPTION 'ASSIGNMENT_REVISION_WORK_MINUTES_MISMATCH';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER a20_assignment_validate_offer_provenance
BEFORE INSERT ON propertyai.cleaning_assignment
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_validate_assignment_offer_provenance();

-- Normal backend acceptance order is Assignment INSERT -> Candidate ACCEPTED.
-- Final Candidate state is therefore checked at transaction end, not in the BEFORE INSERT snapshot validator.
CREATE FUNCTION propertyai.tg_validate_assignment_candidate_accepted_deferred()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_status text;
BEGIN
    IF NEW.assignment_source <> 'OFFER_ACCEPTED' THEN
        RETURN NULL;
    END IF;

    SELECT candidate_status
      INTO STRICT v_status
      FROM propertyai.cleaning_offer_candidate
     WHERE campaign_id = NEW.campaign_id
       AND offer_candidate_id = NEW.offer_candidate_id
       AND cleaner_party_id = NEW.cleaner_party_id
       AND proposal_version = NEW.accepted_proposal_version;

    IF v_status <> 'ACCEPTED' THEN
        RAISE EXCEPTION 'ASSIGNMENT_CANDIDATE_NOT_ACCEPTED_AT_COMMIT';
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER ctr_assignment_candidate_accepted
AFTER INSERT ON propertyai.cleaning_assignment
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_validate_assignment_candidate_accepted_deferred();

CREATE FUNCTION propertyai.tg_guard_assignment_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
BEGIN
    IF ROW(NEW.assignment_id, NEW.cleaning_id, NEW.assignment_no, NEW.schedule_revision_id,
           NEW.campaign_id, NEW.offer_candidate_id, NEW.accepted_proposal_version,
           NEW.cleaner_party_id, NEW.assignment_source, NEW.booked_at,
           NEW.scheduled_start_at, NEW.scheduled_end_at, NEW.work_minutes_snapshot,
           NEW.travel_buffer_before_minutes, NEW.travel_buffer_after_minutes,
           NEW.buffer_basis, NEW.buffer_policy_ref, NEW.base_fee_krw,
           NEW.replacement_urgency, NEW.urgent_premium_krw, NEW.total_agreed_fee_krw,
           NEW.urgent_premium_policy_version, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.assignment_id, OLD.cleaning_id, OLD.assignment_no, OLD.schedule_revision_id,
           OLD.campaign_id, OLD.offer_candidate_id, OLD.accepted_proposal_version,
           OLD.cleaner_party_id, OLD.assignment_source, OLD.booked_at,
           OLD.scheduled_start_at, OLD.scheduled_end_at, OLD.work_minutes_snapshot,
           OLD.travel_buffer_before_minutes, OLD.travel_buffer_after_minutes,
           OLD.buffer_basis, OLD.buffer_policy_ref, OLD.base_fee_krw,
           OLD.replacement_urgency, OLD.urgent_premium_krw, OLD.total_agreed_fee_krw,
           OLD.urgent_premium_policy_version, OLD.created_at) THEN
        RAISE EXCEPTION 'ASSIGNMENT_IMMUTABLE_PROVENANCE';
    END IF;

    IF OLD.assignment_status <> 'HARD_BOOKED' THEN
        IF ROW(NEW.assignment_status, NEW.ended_at, NEW.end_reason_code)
           IS DISTINCT FROM ROW(OLD.assignment_status, OLD.ended_at, OLD.end_reason_code) THEN
            RAISE EXCEPTION 'ASSIGNMENT_TERMINAL_STATE_FROZEN';
        END IF;
    ELSIF NEW.assignment_status NOT IN ('HARD_BOOKED','RELEASED','COMPLETED','CANCELLED') THEN
        RAISE EXCEPTION 'ASSIGNMENT_INVALID_STATUS_TRANSITION';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER a30_assignment_guard_update
BEFORE UPDATE ON propertyai.cleaning_assignment
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_assignment_update();

CREATE FUNCTION propertyai.tg_guard_unavailability_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
BEGIN
    IF ROW(NEW.unavailability_id, NEW.cleaning_id, NEW.schedule_revision_id,
           NEW.original_assignment_id, NEW.cleaner_party_id,
           NEW.availability_classification, NEW.replacement_urgency,
           NEW.occurred_at, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.unavailability_id, OLD.cleaning_id, OLD.schedule_revision_id,
           OLD.original_assignment_id, OLD.cleaner_party_id,
           OLD.availability_classification, OLD.replacement_urgency,
           OLD.occurred_at, OLD.created_at) THEN
        RAISE EXCEPTION 'UNAVAILABILITY_IMMUTABLE_BINDING';
    END IF;
    IF OLD.case_status = 'CANCELLED' AND NEW.case_status <> 'CANCELLED' THEN
        RAISE EXCEPTION 'UNAVAILABILITY_TERMINAL_STATE_FROZEN';
    END IF;
    IF OLD.case_status = 'CONFIRMED' AND NEW.case_status NOT IN ('CONFIRMED','CANCELLED') THEN
        RAISE EXCEPTION 'UNAVAILABILITY_INVALID_STATUS_TRANSITION';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_unavailability_guard_update
BEFORE UPDATE ON propertyai.cleaner_unavailability_case
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_unavailability_update();

CREATE FUNCTION propertyai.tg_guard_reassignment_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
BEGIN
    IF ROW(NEW.reassignment_request_id, NEW.unavailability_id, NEW.cleaning_id,
           NEW.cleaner_party_id, NEW.original_assignment_id,
           NEW.requested_schedule_revision_id, NEW.request_no, NEW.requested_at, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.reassignment_request_id, OLD.unavailability_id, OLD.cleaning_id,
           OLD.cleaner_party_id, OLD.original_assignment_id,
           OLD.requested_schedule_revision_id, OLD.request_no, OLD.requested_at, OLD.created_at) THEN
        RAISE EXCEPTION 'REASSIGNMENT_IMMUTABLE_BINDING';
    END IF;
    IF OLD.request_status <> 'REQUESTED' THEN
        IF ROW(NEW.request_status, NEW.decided_at, NEW.decision_code)
           IS DISTINCT FROM ROW(OLD.request_status, OLD.decided_at, OLD.decision_code) THEN
            RAISE EXCEPTION 'REASSIGNMENT_TERMINAL_STATE_FROZEN';
        END IF;
    ELSIF NEW.request_status NOT IN ('REQUESTED','REASSIGNED_ORIGINAL','CONTINUE_REPLACEMENT','SUPERSEDED','CANCELLED') THEN
        RAISE EXCEPTION 'REASSIGNMENT_INVALID_STATUS_TRANSITION';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_reassignment_guard_update
BEFORE UPDATE ON propertyai.cleaner_reassignment_request
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_reassignment_update();

CREATE FUNCTION propertyai.tg_guard_reconciliation_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_target_changed boolean := NEW.target_schedule_revision_id IS DISTINCT FROM OLD.target_schedule_revision_id;
    v_status_changed boolean := NEW.reconciliation_status IS DISTINCT FROM OLD.reconciliation_status;
BEGIN
    IF ROW(NEW.schedule_reconciliation_id, NEW.cleaning_id, NEW.hard_booked_assignment_id,
           NEW.base_assignment_revision_id, NEW.reason_code, NEW.source_ref, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.schedule_reconciliation_id, OLD.cleaning_id, OLD.hard_booked_assignment_id,
           OLD.base_assignment_revision_id, OLD.reason_code, OLD.source_ref, OLD.created_at) THEN
        RAISE EXCEPTION 'RECONCILIATION_IMMUTABLE_BINDING';
    END IF;

    IF OLD.reconciliation_status <> 'PENDING' THEN
        IF ROW(NEW.reconciliation_status, NEW.target_schedule_revision_id, NEW.case_version,
               NEW.resolved_at, NEW.resolution_code)
           IS DISTINCT FROM
           ROW(OLD.reconciliation_status, OLD.target_schedule_revision_id, OLD.case_version,
               OLD.resolved_at, OLD.resolution_code) THEN
            RAISE EXCEPTION 'RECONCILIATION_TERMINAL_STATE_FROZEN';
        END IF;
        RETURN NEW;
    END IF;

    IF v_status_changed THEN
        IF v_target_changed OR NEW.case_version <> OLD.case_version THEN
            RAISE EXCEPTION 'RECONCILIATION_TARGET_AND_TERMINAL_CHANGE_MUST_BE_SEPARATE';
        END IF;
        IF NEW.reconciliation_status NOT IN ('RESOLVED','CANCELLED') THEN
            RAISE EXCEPTION 'RECONCILIATION_INVALID_STATUS_TRANSITION';
        END IF;
    ELSIF v_target_changed THEN
        IF NEW.case_version <> OLD.case_version + 1 THEN
            RAISE EXCEPTION 'RECONCILIATION_CASE_VERSION_MUST_INCREMENT';
        END IF;
        IF NEW.resolved_at IS DISTINCT FROM OLD.resolved_at OR NEW.resolution_code IS DISTINCT FROM OLD.resolution_code THEN
            RAISE EXCEPTION 'RECONCILIATION_TARGET_CHANGE_CANNOT_RESOLVE';
        END IF;
    ELSIF NEW.case_version <> OLD.case_version THEN
        RAISE EXCEPTION 'RECONCILIATION_VERSION_WITHOUT_TARGET_CHANGE';
    ELSIF NEW.resolved_at IS DISTINCT FROM OLD.resolved_at OR NEW.resolution_code IS DISTINCT FROM OLD.resolution_code THEN
        RAISE EXCEPTION 'RECONCILIATION_RESOLUTION_WITHOUT_STATUS_CHANGE';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_reconciliation_guard_update
BEFORE UPDATE ON propertyai.cleaning_schedule_reconciliation
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_reconciliation_update();

-- ---------- Queue transition guards ----------

CREATE FUNCTION propertyai.tg_guard_scheduled_action_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
BEGIN
    IF ROW(NEW.scheduled_action_id, NEW.action_type, NEW.aggregate_type, NEW.aggregate_id,
           NEW.due_at, NEW.idempotency_key, NEW.payload, NEW.max_attempts, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.scheduled_action_id, OLD.action_type, OLD.aggregate_type, OLD.aggregate_id,
           OLD.due_at, OLD.idempotency_key, OLD.payload, OLD.max_attempts, OLD.created_at) THEN
        RAISE EXCEPTION 'SCHEDULED_ACTION_IMMUTABLE_CONTENT';
    END IF;

    IF OLD.action_status IN ('SUCCEEDED','DEAD_LETTER','CANCELLED') THEN
        IF ROW(NEW.action_status, NEW.available_at, NEW.attempt_count, NEW.lease_owner,
               NEW.lease_until, NEW.lease_fence, NEW.last_error_code,
               NEW.completed_at, NEW.cancelled_at)
           IS DISTINCT FROM
           ROW(OLD.action_status, OLD.available_at, OLD.attempt_count, OLD.lease_owner,
               OLD.lease_until, OLD.lease_fence, OLD.last_error_code,
               OLD.completed_at, OLD.cancelled_at) THEN
            RAISE EXCEPTION 'SCHEDULED_ACTION_TERMINAL_STATE_FROZEN';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.action_status = 'RUNNING' AND OLD.action_status IN ('PENDING','FAILED_RETRYABLE','RUNNING') THEN
        IF NEW.lease_fence <> OLD.lease_fence + 1 OR NEW.attempt_count <> OLD.attempt_count + 1 THEN
            RAISE EXCEPTION 'SCHEDULED_ACTION_CLAIM_FENCE_OR_ATTEMPT_INVALID';
        END IF;
    ELSIF NEW.action_status <> OLD.action_status THEN
        IF NOT (
            (OLD.action_status = 'RUNNING' AND NEW.action_status IN ('FAILED_RETRYABLE','SUCCEEDED','DEAD_LETTER'))
            OR (OLD.action_status IN ('PENDING','FAILED_RETRYABLE') AND NEW.action_status IN ('CANCELLED','DEAD_LETTER'))
        ) THEN
            RAISE EXCEPTION 'SCHEDULED_ACTION_INVALID_STATUS_TRANSITION';
        END IF;
        IF NEW.lease_fence <> OLD.lease_fence OR NEW.attempt_count <> OLD.attempt_count THEN
            RAISE EXCEPTION 'SCHEDULED_ACTION_NONCLAIM_CANNOT_CHANGE_FENCE_ATTEMPT';
        END IF;
    ELSIF NEW.lease_fence <> OLD.lease_fence OR NEW.attempt_count <> OLD.attempt_count THEN
        RAISE EXCEPTION 'SCHEDULED_ACTION_FENCE_ATTEMPT_WITHOUT_CLAIM';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_scheduled_action_guard_update
BEFORE UPDATE ON propertyai.business_scheduled_action
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_scheduled_action_update();

CREATE FUNCTION propertyai.tg_guard_outbox_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
BEGIN
    IF ROW(NEW.outbox_id, NEW.domain_event_id, NEW.event_type, NEW.aggregate_type, NEW.aggregate_id,
           NEW.destination_type, NEW.destination_ref, NEW.idempotency_key, NEW.payload,
           NEW.max_attempts, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.outbox_id, OLD.domain_event_id, OLD.event_type, OLD.aggregate_type, OLD.aggregate_id,
           OLD.destination_type, OLD.destination_ref, OLD.idempotency_key, OLD.payload,
           OLD.max_attempts, OLD.created_at) THEN
        RAISE EXCEPTION 'OUTBOX_IMMUTABLE_CONTENT';
    END IF;

    IF OLD.outbox_status IN ('SUCCEEDED','DEAD_LETTER','CANCELLED') THEN
        IF ROW(NEW.outbox_status, NEW.available_at, NEW.attempt_count, NEW.lease_owner,
               NEW.lease_until, NEW.lease_fence, NEW.external_effect_id,
               NEW.last_error_code, NEW.delivered_at, NEW.cancelled_at)
           IS DISTINCT FROM
           ROW(OLD.outbox_status, OLD.available_at, OLD.attempt_count, OLD.lease_owner,
               OLD.lease_until, OLD.lease_fence, OLD.external_effect_id,
               OLD.last_error_code, OLD.delivered_at, OLD.cancelled_at) THEN
            RAISE EXCEPTION 'OUTBOX_TERMINAL_STATE_FROZEN';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.outbox_status = 'RUNNING' AND OLD.outbox_status IN ('PENDING','FAILED_RETRYABLE','RUNNING') THEN
        IF NEW.lease_fence <> OLD.lease_fence + 1 OR NEW.attempt_count <> OLD.attempt_count + 1 THEN
            RAISE EXCEPTION 'OUTBOX_CLAIM_FENCE_OR_ATTEMPT_INVALID';
        END IF;
    ELSIF NEW.outbox_status <> OLD.outbox_status THEN
        IF NOT (
            (OLD.outbox_status = 'RUNNING' AND NEW.outbox_status IN ('FAILED_RETRYABLE','PENDING_RECONCILIATION','SUCCEEDED','DEAD_LETTER'))
            OR (OLD.outbox_status IN ('PENDING','FAILED_RETRYABLE') AND NEW.outbox_status IN ('CANCELLED','DEAD_LETTER'))
            OR (OLD.outbox_status = 'PENDING_RECONCILIATION' AND NEW.outbox_status IN ('FAILED_RETRYABLE','SUCCEEDED','DEAD_LETTER'))
        ) THEN
            RAISE EXCEPTION 'OUTBOX_INVALID_STATUS_TRANSITION';
        END IF;
        IF NEW.lease_fence <> OLD.lease_fence OR NEW.attempt_count <> OLD.attempt_count THEN
            RAISE EXCEPTION 'OUTBOX_NONCLAIM_CANNOT_CHANGE_FENCE_ATTEMPT';
        END IF;
    ELSIF NEW.lease_fence <> OLD.lease_fence OR NEW.attempt_count <> OLD.attempt_count THEN
        RAISE EXCEPTION 'OUTBOX_FENCE_ATTEMPT_WITHOUT_CLAIM';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_outbox_guard_update
BEFORE UPDATE ON propertyai.integration_outbox
FOR EACH ROW EXECUTE FUNCTION propertyai.tg_guard_outbox_update();

-- ---------- Narrow SECURITY DEFINER primitives ----------

CREATE FUNCTION propertyai.lock_and_verify_authority_epoch(
    p_scope_code text,
    p_expected_epoch bigint
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_epoch bigint;
BEGIN
    SELECT current_epoch
      INTO v_epoch
      FROM propertyai.authority_epoch
     WHERE scope_code = p_scope_code
     FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'AUTHORITY_SCOPE_NOT_FOUND: %', p_scope_code;
    END IF;
    IF v_epoch <> p_expected_epoch THEN
        RAISE EXCEPTION 'STALE_AUTHORITY_EPOCH expected=% actual=%', p_expected_epoch, v_epoch;
    END IF;
    RETURN v_epoch;
END
$$;

CREATE FUNCTION propertyai.append_cleaning_schedule_revision(
    p_schedule_revision_id uuid,
    p_cleaning_id uuid,
    p_expected_current_revision_id uuid,
    p_service_window_start_at timestamptz,
    p_service_deadline_at timestamptz,
    p_required_work_minutes integer,
    p_source_checkout_at timestamptz,
    p_source_reservation_version bigint,
    p_change_reason_code text,
    p_source_command_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_current uuid;
    v_current_no integer;
    v_next_no integer;
    v_schedule_source_type text;
    v_reservation_id uuid;
    v_reservation_checkout timestamptz;
    v_reservation_version bigint;
BEGIN
    -- Non-locking identity lookup first; runtime cannot mutate source type/reservation binding.
    SELECT schedule_source_type, reservation_id
      INTO STRICT v_schedule_source_type, v_reservation_id
      FROM propertyai.cleaning_job
     WHERE cleaning_id = p_cleaning_id;

    IF v_schedule_source_type = 'RESERVATION_CHECKOUT' THEN
        IF p_source_checkout_at IS NULL OR p_source_reservation_version IS NULL OR v_reservation_id IS NULL THEN
            RAISE EXCEPTION 'CHECKOUT_REVISION_REQUIRES_RESERVATION_SOURCE';
        END IF;

        -- Respect global lock order: Reservation before Cleaning.
        SELECT check_out_at, source_version
          INTO STRICT v_reservation_checkout, v_reservation_version
          FROM propertyai.reservation
         WHERE reservation_id = v_reservation_id
         FOR SHARE;

        IF p_source_checkout_at IS DISTINCT FROM v_reservation_checkout
           OR p_source_reservation_version IS DISTINCT FROM v_reservation_version THEN
            RAISE EXCEPTION 'CHECKOUT_REVISION_SOURCE_MISMATCH expected_checkout=% actual_checkout=% expected_version=% actual_version=%',
                p_source_checkout_at, v_reservation_checkout,
                p_source_reservation_version, v_reservation_version;
        END IF;
    END IF;

    SELECT current_schedule_revision_id
      INTO STRICT v_current
      FROM propertyai.cleaning_job
     WHERE cleaning_id = p_cleaning_id
     FOR UPDATE;

    IF v_current IS DISTINCT FROM p_expected_current_revision_id THEN
        RAISE EXCEPTION 'STALE_SCHEDULE_REVISION expected=% actual=%', p_expected_current_revision_id, v_current;
    END IF;

    IF v_current IS NULL THEN
        v_next_no := 1;
    ELSE
        SELECT revision_no INTO STRICT v_current_no
          FROM propertyai.cleaning_schedule_revision
         WHERE cleaning_id = p_cleaning_id
           AND schedule_revision_id = v_current;
        v_next_no := v_current_no + 1;
    END IF;

    INSERT INTO propertyai.cleaning_schedule_revision(
        schedule_revision_id, cleaning_id, revision_no,
        service_window_start_at, service_deadline_at, required_work_minutes,
        source_checkout_at, source_reservation_version,
        change_reason_code, source_command_id
    ) VALUES (
        p_schedule_revision_id, p_cleaning_id, v_next_no,
        p_service_window_start_at, p_service_deadline_at, p_required_work_minutes,
        p_source_checkout_at, p_source_reservation_version,
        p_change_reason_code, p_source_command_id
    );

    UPDATE propertyai.cleaning_job
       SET current_schedule_revision_id = p_schedule_revision_id,
           updated_at = clock_timestamp()
     WHERE cleaning_id = p_cleaning_id;

    RETURN p_schedule_revision_id;
END
$$;

CREATE FUNCTION propertyai.cancel_business_scheduled_action(
    p_scheduled_action_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE v_count integer;
BEGIN
    IF p_scheduled_action_id IS NULL THEN
        RAISE EXCEPTION 'INVALID_SCHEDULED_ACTION_ID';
    END IF;
    UPDATE propertyai.business_scheduled_action
       SET action_status = 'CANCELLED',
           cancelled_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE scheduled_action_id = p_scheduled_action_id
       AND action_status IN ('PENDING','FAILED_RETRYABLE');
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;

CREATE FUNCTION propertyai.cancel_integration_outbox(
    p_outbox_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE v_count integer;
BEGIN
    IF p_outbox_id IS NULL THEN
        RAISE EXCEPTION 'INVALID_OUTBOX_ID';
    END IF;
    UPDATE propertyai.integration_outbox
       SET outbox_status = 'CANCELLED',
           cancelled_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE outbox_id = p_outbox_id
       AND outbox_status IN ('PENDING','FAILED_RETRYABLE');
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;

CREATE FUNCTION propertyai.claim_business_scheduled_actions(
    p_worker text,
    p_limit integer,
    p_lease_seconds integer
)
RETURNS SETOF propertyai.business_scheduled_action
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
BEGIN
    IF p_worker IS NULL OR btrim(p_worker) = ''
       OR p_limit IS NULL OR p_limit <= 0 OR p_limit > 100
       OR p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'INVALID_QUEUE_CLAIM_ARGUMENT';
    END IF;

    -- Bounded, non-blocking reaping of exhausted rows. Never scan/update the whole exhausted set.
    WITH exhausted AS (
        SELECT scheduled_action_id
          FROM propertyai.business_scheduled_action
         WHERE attempt_count >= max_attempts
           AND (
                (action_status IN ('PENDING','FAILED_RETRYABLE') AND available_at <= v_now)
                OR (action_status = 'RUNNING' AND lease_until < v_now)
           )
         ORDER BY CASE WHEN action_status = 'RUNNING' THEN lease_until ELSE available_at END,
                  scheduled_action_id
         FOR UPDATE SKIP LOCKED
         LIMIT p_limit
    )
    UPDATE propertyai.business_scheduled_action a
       SET action_status = 'DEAD_LETTER',
           lease_owner = NULL,
           lease_until = NULL,
           last_error_code = COALESCE(a.last_error_code, 'MAX_ATTEMPTS_EXHAUSTED'),
           updated_at = v_now
      FROM exhausted
     WHERE a.scheduled_action_id = exhausted.scheduled_action_id;

    RETURN QUERY
    WITH picked AS (
        SELECT scheduled_action_id
          FROM propertyai.business_scheduled_action
         WHERE attempt_count < max_attempts
           AND (
                (action_status IN ('PENDING','FAILED_RETRYABLE') AND available_at <= v_now)
                OR (action_status = 'RUNNING' AND lease_until < v_now)
           )
         ORDER BY CASE WHEN action_status = 'RUNNING' THEN lease_until ELSE available_at END,
                  scheduled_action_id
         FOR UPDATE SKIP LOCKED
         LIMIT p_limit
    )
    UPDATE propertyai.business_scheduled_action a
       SET action_status = 'RUNNING',
           attempt_count = a.attempt_count + 1,
           lease_owner = p_worker,
           lease_until = v_now + make_interval(secs => p_lease_seconds),
           lease_fence = a.lease_fence + 1,
           updated_at = v_now
      FROM picked
     WHERE a.scheduled_action_id = picked.scheduled_action_id
     RETURNING a.*;
END
$$;

CREATE FUNCTION propertyai.complete_business_scheduled_action(
    p_scheduled_action_id uuid,
    p_worker text,
    p_lease_fence bigint
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE v_count integer;
BEGIN
    UPDATE propertyai.business_scheduled_action
       SET action_status = 'SUCCEEDED',
           lease_owner = NULL,
           lease_until = NULL,
           completed_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE scheduled_action_id = p_scheduled_action_id
       AND action_status = 'RUNNING'
       AND lease_owner = p_worker
       AND lease_fence = p_lease_fence;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;

CREATE FUNCTION propertyai.fail_business_scheduled_action(
    p_scheduled_action_id uuid,
    p_worker text,
    p_lease_fence bigint,
    p_error_code text,
    p_retry_delay_seconds integer
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE v_count integer;
BEGIN
    IF p_retry_delay_seconds IS NULL OR p_retry_delay_seconds < 0 OR p_retry_delay_seconds > 86400 THEN
        RAISE EXCEPTION 'INVALID_RETRY_DELAY';
    END IF;
    UPDATE propertyai.business_scheduled_action
       SET action_status = CASE WHEN attempt_count >= max_attempts THEN 'DEAD_LETTER' ELSE 'FAILED_RETRYABLE' END,
           available_at = CASE WHEN attempt_count >= max_attempts THEN available_at ELSE clock_timestamp() + make_interval(secs => p_retry_delay_seconds) END,
           lease_owner = NULL,
           lease_until = NULL,
           last_error_code = p_error_code,
           updated_at = clock_timestamp()
     WHERE scheduled_action_id = p_scheduled_action_id
       AND action_status = 'RUNNING'
       AND lease_owner = p_worker
       AND lease_fence = p_lease_fence;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;

CREATE FUNCTION propertyai.claim_integration_outbox(
    p_worker text,
    p_limit integer,
    p_lease_seconds integer
)
RETURNS SETOF propertyai.integration_outbox
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
BEGIN
    IF p_worker IS NULL OR btrim(p_worker) = ''
       OR p_limit IS NULL OR p_limit <= 0 OR p_limit > 100
       OR p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'INVALID_OUTBOX_CLAIM_ARGUMENT';
    END IF;

    WITH exhausted AS (
        SELECT outbox_id
          FROM propertyai.integration_outbox
         WHERE attempt_count >= max_attempts
           AND (
                (outbox_status IN ('PENDING','FAILED_RETRYABLE') AND available_at <= v_now)
                OR (outbox_status = 'RUNNING' AND lease_until < v_now)
           )
         ORDER BY CASE WHEN outbox_status = 'RUNNING' THEN lease_until ELSE available_at END,
                  outbox_id
         FOR UPDATE SKIP LOCKED
         LIMIT p_limit
    )
    UPDATE propertyai.integration_outbox o
       SET outbox_status = 'DEAD_LETTER',
           lease_owner = NULL,
           lease_until = NULL,
           last_error_code = COALESCE(o.last_error_code, 'MAX_ATTEMPTS_EXHAUSTED'),
           updated_at = v_now
      FROM exhausted
     WHERE o.outbox_id = exhausted.outbox_id;

    RETURN QUERY
    WITH picked AS (
        SELECT outbox_id
          FROM propertyai.integration_outbox
         WHERE attempt_count < max_attempts
           AND (
                (outbox_status IN ('PENDING','FAILED_RETRYABLE') AND available_at <= v_now)
                OR (outbox_status = 'RUNNING' AND lease_until < v_now)
           )
         ORDER BY CASE WHEN outbox_status = 'RUNNING' THEN lease_until ELSE available_at END,
                  outbox_id
         FOR UPDATE SKIP LOCKED
         LIMIT p_limit
    )
    UPDATE propertyai.integration_outbox o
       SET outbox_status = 'RUNNING',
           attempt_count = o.attempt_count + 1,
           lease_owner = p_worker,
           lease_until = v_now + make_interval(secs => p_lease_seconds),
           lease_fence = o.lease_fence + 1,
           updated_at = v_now
      FROM picked
     WHERE o.outbox_id = picked.outbox_id
     RETURNING o.*;
END
$$;

CREATE FUNCTION propertyai.complete_integration_outbox(
    p_outbox_id uuid,
    p_worker text,
    p_lease_fence bigint,
    p_external_effect_id text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE v_count integer;
BEGIN
    UPDATE propertyai.integration_outbox
       SET outbox_status = 'SUCCEEDED',
           lease_owner = NULL,
           lease_until = NULL,
           external_effect_id = p_external_effect_id,
           delivered_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE outbox_id = p_outbox_id
       AND outbox_status = 'RUNNING'
       AND lease_owner = p_worker
       AND lease_fence = p_lease_fence;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;

CREATE FUNCTION propertyai.fail_integration_outbox(
    p_outbox_id uuid,
    p_worker text,
    p_lease_fence bigint,
    p_error_code text,
    p_retry_delay_seconds integer
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE v_count integer;
BEGIN
    IF p_retry_delay_seconds IS NULL OR p_retry_delay_seconds < 0 OR p_retry_delay_seconds > 86400 THEN
        RAISE EXCEPTION 'INVALID_RETRY_DELAY';
    END IF;
    UPDATE propertyai.integration_outbox
       SET outbox_status = CASE WHEN attempt_count >= max_attempts THEN 'DEAD_LETTER' ELSE 'FAILED_RETRYABLE' END,
           available_at = CASE WHEN attempt_count >= max_attempts THEN available_at ELSE clock_timestamp() + make_interval(secs => p_retry_delay_seconds) END,
           lease_owner = NULL,
           lease_until = NULL,
           last_error_code = p_error_code,
           updated_at = clock_timestamp()
     WHERE outbox_id = p_outbox_id
       AND outbox_status = 'RUNNING'
       AND lease_owner = p_worker
       AND lease_fence = p_lease_fence;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;

CREATE FUNCTION propertyai.mark_outbox_pending_reconciliation(
    p_outbox_id uuid,
    p_worker text,
    p_lease_fence bigint,
    p_error_code text,
    p_external_effect_id text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE v_count integer;
BEGIN
    UPDATE propertyai.integration_outbox
       SET outbox_status = 'PENDING_RECONCILIATION',
           lease_owner = NULL,
           lease_until = NULL,
           external_effect_id = p_external_effect_id,
           last_error_code = p_error_code,
           updated_at = clock_timestamp()
     WHERE outbox_id = p_outbox_id
       AND outbox_status = 'RUNNING'
       AND lease_owner = p_worker
       AND lease_fence = p_lease_fence;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;

CREATE FUNCTION propertyai.resolve_outbox_reconciliation(
    p_outbox_id uuid,
    p_expected_lease_fence bigint,
    p_resolution text,
    p_external_effect_id text,
    p_error_code text,
    p_retry_delay_seconds integer
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, propertyai, pg_temp
AS $$
DECLARE v_count integer;
BEGIN
    IF p_resolution NOT IN ('SUCCEEDED','FAILED_RETRYABLE','DEAD_LETTER') THEN
        RAISE EXCEPTION 'INVALID_OUTBOX_RECONCILIATION_RESOLUTION';
    END IF;
    IF p_retry_delay_seconds IS NULL OR p_retry_delay_seconds < 0 OR p_retry_delay_seconds > 86400 THEN
        RAISE EXCEPTION 'INVALID_RETRY_DELAY';
    END IF;

    UPDATE propertyai.integration_outbox
       SET outbox_status = p_resolution,
           available_at = CASE WHEN p_resolution = 'FAILED_RETRYABLE' THEN clock_timestamp() + make_interval(secs => p_retry_delay_seconds) ELSE available_at END,
           external_effect_id = COALESCE(p_external_effect_id, external_effect_id),
           last_error_code = p_error_code,
           delivered_at = CASE WHEN p_resolution = 'SUCCEEDED' THEN clock_timestamp() ELSE NULL END,
           updated_at = clock_timestamp()
     WHERE outbox_id = p_outbox_id
       AND outbox_status = 'PENDING_RECONCILIATION'
       AND lease_fence = p_expected_lease_fence;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;
