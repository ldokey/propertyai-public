SET ROLE propertyai_owner;

-- Default hardening for future functions owned by propertyai_owner.
ALTER DEFAULT PRIVILEGES FOR ROLE propertyai_owner IN SCHEMA propertyai
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

REVOKE ALL ON ALL TABLES IN SCHEMA propertyai FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA propertyai FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA propertyai FROM PUBLIC;

-- Masked support views: readonly never receives the corresponding sensitive base-table SELECT.
CREATE VIEW propertyai.v_external_identity_masked AS
SELECT
    external_identity_id,
    party_id,
    provider,
    bound_at,
    revoked_at,
    created_at
FROM propertyai.external_identity;

CREATE VIEW propertyai.v_command_receipt_masked AS
SELECT
    command_id,
    authority_scope_code,
    command_type,
    source_channel_code,
    principal_type,
    actor_party_id,
    authority_epoch,
    source_observed_at,
    decided_at,
    result_type,
    result_id,
    created_at
FROM propertyai.command_receipt;

-- ----- Read access -----
GRANT SELECT ON ALL TABLES IN SCHEMA propertyai TO propertyai_app_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA propertyai TO propertyai_async_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA propertyai TO propertyai_readonly;

REVOKE SELECT ON propertyai.external_identity FROM propertyai_readonly;
REVOKE SELECT ON propertyai.command_receipt FROM propertyai_readonly;
GRANT SELECT ON propertyai.v_external_identity_masked, propertyai.v_command_receipt_masked TO propertyai_readonly;

-- ----- App-runtime creation privileges -----
GRANT INSERT ON
    propertyai.organization,
    propertyai.organization_member,
    propertyai.property,
    propertyai.rental_unit,
    propertyai.party,
    propertyai.cleaner_profile,
    propertyai.external_identity,
    propertyai.cleaner_property_roster,
    propertyai.cleaner_schedule_block,
    propertyai.reservation,
    propertyai.cleaning_job,
    propertyai.cleaning_offer_campaign,
    propertyai.cleaning_offer_candidate,
    propertyai.cleaning_assignment,
    propertyai.cleaner_unavailability_case,
    propertyai.cleaner_reassignment_request,
    propertyai.cleaning_schedule_reconciliation,
    propertyai.command_receipt,
    propertyai.domain_event
TO propertyai_app_runtime;

-- Deliberately no generic app INSERT on cleaning_schedule_revision.
-- Deliberately no app INSERT/UPDATE on integration_resource_binding.
-- Deliberately no app UPDATE on authority_epoch.

-- ----- App-runtime bounded UPDATE columns -----
GRANT UPDATE (display_name, organization_status, updated_at)
    ON propertyai.organization TO propertyai_app_runtime;
GRANT UPDATE (membership_role, membership_status, joined_at, removed_at, updated_at)
    ON propertyai.organization_member TO propertyai_app_runtime;
GRANT UPDATE (display_name, timezone_name, active, updated_at)
    ON propertyai.property TO propertyai_app_runtime;
GRANT UPDATE (display_name, active, updated_at)
    ON propertyai.rental_unit TO propertyai_app_runtime;
GRANT UPDATE (display_name, active, updated_at)
    ON propertyai.party TO propertyai_app_runtime;
GRANT UPDATE (operational_status, max_daily_work_minutes, max_daily_jobs, updated_at)
    ON propertyai.cleaner_profile TO propertyai_app_runtime;
GRANT UPDATE (revoked_at)
    ON propertyai.external_identity TO propertyai_app_runtime;
GRANT UPDATE (roster_status, offer_tier, priority_within_tier, eligible_from, eligible_until, updated_at)
    ON propertyai.cleaner_property_roster TO propertyai_app_runtime;
GRANT UPDATE (cancelled_at)
    ON propertyai.cleaner_schedule_block TO propertyai_app_runtime;
GRANT UPDATE (reservation_status, check_in_at, check_out_at, source_version, updated_at)
    ON propertyai.reservation TO propertyai_app_runtime;
GRANT UPDATE (cleaning_status, updated_at)
    ON propertyai.cleaning_job TO propertyai_app_runtime;
GRANT UPDATE (campaign_status, open_tier_floor, closed_at, closed_reason_code, updated_at)
    ON propertyai.cleaning_offer_campaign TO propertyai_app_runtime;
GRANT UPDATE (
    candidate_status, proposal_version,
    proposed_start_at, proposed_end_at,
    proposed_buffer_before_minutes, proposed_buffer_after_minutes,
    buffer_basis, buffer_policy_ref,
    declined_at, accepted_at, updated_at
) ON propertyai.cleaning_offer_candidate TO propertyai_app_runtime;
GRANT UPDATE (assignment_status, ended_at, end_reason_code, updated_at)
    ON propertyai.cleaning_assignment TO propertyai_app_runtime;
GRANT UPDATE (case_status, reason_code, reason_text, updated_at)
    ON propertyai.cleaner_unavailability_case TO propertyai_app_runtime;
GRANT UPDATE (request_status, decided_at, decision_code, updated_at)
    ON propertyai.cleaner_reassignment_request TO propertyai_app_runtime;
GRANT UPDATE (
    target_schedule_revision_id, case_version,
    reconciliation_status, resolved_at, resolution_code, updated_at
) ON propertyai.cleaning_schedule_reconciliation TO propertyai_app_runtime;


-- Queue/outbox creation is column-restricted so runtime cannot forge RUNNING/terminal/fence state.
GRANT INSERT (
    scheduled_action_id, action_type, aggregate_type, aggregate_id, due_at, available_at,
    idempotency_key, payload, max_attempts
) ON propertyai.business_scheduled_action TO propertyai_app_runtime;
GRANT INSERT (
    outbox_id, domain_event_id, event_type, aggregate_type, aggregate_id, destination_type,
    destination_ref, available_at, idempotency_key, payload, max_attempts
) ON propertyai.integration_outbox TO propertyai_app_runtime;

-- Domain-event bigserial sequence.
GRANT USAGE, SELECT ON SEQUENCE propertyai.domain_event_domain_event_id_seq TO propertyai_app_runtime;

-- ----- Async worker resource-binding projection privilege -----
GRANT INSERT ON propertyai.integration_resource_binding TO propertyai_async_worker;
GRANT UPDATE (
    external_resource_id, external_uid, sync_status,
    last_applied_aggregate_version, external_version,
    last_synced_at, updated_at
) ON propertyai.integration_resource_binding TO propertyai_async_worker;

-- ----- Narrow app primitives -----
GRANT EXECUTE ON FUNCTION propertyai.lock_and_verify_authority_epoch(text, bigint)
    TO propertyai_app_runtime;
GRANT EXECUTE ON FUNCTION propertyai.append_cleaning_schedule_revision(
    uuid, uuid, uuid, timestamptz, timestamptz, integer,
    timestamptz, bigint, text, uuid
) TO propertyai_app_runtime;

GRANT EXECUTE ON FUNCTION propertyai.cancel_business_scheduled_action(uuid)
    TO propertyai_app_runtime;
GRANT EXECUTE ON FUNCTION propertyai.cancel_integration_outbox(uuid)
    TO propertyai_app_runtime;

-- ----- Narrow worker queue/outbox primitives -----
GRANT EXECUTE ON FUNCTION propertyai.claim_business_scheduled_actions(text, integer, integer)
    TO propertyai_async_worker;
GRANT EXECUTE ON FUNCTION propertyai.complete_business_scheduled_action(uuid, text, bigint)
    TO propertyai_async_worker;
GRANT EXECUTE ON FUNCTION propertyai.fail_business_scheduled_action(uuid, text, bigint, text, integer)
    TO propertyai_async_worker;
GRANT EXECUTE ON FUNCTION propertyai.claim_integration_outbox(text, integer, integer)
    TO propertyai_async_worker;
GRANT EXECUTE ON FUNCTION propertyai.complete_integration_outbox(uuid, text, bigint, text)
    TO propertyai_async_worker;
GRANT EXECUTE ON FUNCTION propertyai.fail_integration_outbox(uuid, text, bigint, text, integer)
    TO propertyai_async_worker;
GRANT EXECUTE ON FUNCTION propertyai.mark_outbox_pending_reconciliation(uuid, text, bigint, text, text)
    TO propertyai_async_worker;
GRANT EXECUTE ON FUNCTION propertyai.resolve_outbox_reconciliation(uuid, bigint, text, text, text, integer)
    TO propertyai_async_worker;

-- Migrator receives no ordinary application DML here. Its DDL authority is explicit SET ROLE propertyai_owner
-- through the membership created in the privileged bootstrap migration.
