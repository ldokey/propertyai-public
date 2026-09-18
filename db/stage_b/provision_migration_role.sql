-- Stage B PRE_CUTOVER direct-login role. Run as a PostgreSQL role administrator
-- only after the frozen V2.2.1 schema is provisioned.
DO $stage_b_role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'propertyai_stage_b_migration') THEN
        CREATE ROLE propertyai_stage_b_migration
            LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS;
    END IF;
END
$stage_b_role$;

ALTER ROLE propertyai_stage_b_migration
    NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- The login is intentionally not a member of owner, migrator, app-runtime, or
-- async-worker roles. It cannot SET ROLE into an authority-bearing identity.
REVOKE propertyai_owner FROM propertyai_stage_b_migration;
REVOKE propertyai_migrator FROM propertyai_stage_b_migration;
REVOKE propertyai_app_runtime FROM propertyai_stage_b_migration;
REVOKE propertyai_async_worker FROM propertyai_stage_b_migration;

GRANT CONNECT ON DATABASE :DBNAME TO propertyai_stage_b_migration;
GRANT USAGE ON SCHEMA propertyai TO propertyai_stage_b_migration;

GRANT SELECT, INSERT ON
    propertyai.organization,
    propertyai.property,
    propertyai.rental_unit,
    propertyai.party,
    propertyai.cleaner_profile,
    propertyai.external_identity,
    propertyai.cleaner_property_roster,
    propertyai.reservation,
    propertyai.cleaning_job,
    propertyai.cleaning_schedule_revision,
    propertyai.cleaning_offer_campaign,
    propertyai.cleaning_offer_candidate,
    propertyai.cleaning_assignment,
    propertyai.cleaner_unavailability_case,
    propertyai.cleaner_reassignment_request,
    propertyai.cleaning_schedule_reconciliation,
    propertyai.command_receipt,
    propertyai.integration_resource_binding
TO propertyai_stage_b_migration;

GRANT UPDATE (display_name, organization_status, updated_at)
    ON propertyai.organization TO propertyai_stage_b_migration;
GRANT UPDATE (display_name, timezone_name, active, updated_at)
    ON propertyai.property TO propertyai_stage_b_migration;
GRANT UPDATE (display_name, active, updated_at)
    ON propertyai.rental_unit TO propertyai_stage_b_migration;
GRANT UPDATE (display_name, active, updated_at)
    ON propertyai.party TO propertyai_stage_b_migration;
GRANT UPDATE (operational_status, max_daily_work_minutes, max_daily_jobs, updated_at)
    ON propertyai.cleaner_profile TO propertyai_stage_b_migration;
GRANT UPDATE (revoked_at)
    ON propertyai.external_identity TO propertyai_stage_b_migration;
GRANT UPDATE (roster_status, offer_tier, priority_within_tier, eligible_from, eligible_until, updated_at)
    ON propertyai.cleaner_property_roster TO propertyai_stage_b_migration;
GRANT UPDATE (reservation_status, check_in_at, check_out_at, source_version, updated_at)
    ON propertyai.reservation TO propertyai_stage_b_migration;
GRANT UPDATE (cleaning_status, current_schedule_revision_id, updated_at)
    ON propertyai.cleaning_job TO propertyai_stage_b_migration;
GRANT UPDATE (campaign_status, open_tier_floor, closed_at, closed_reason_code, updated_at)
    ON propertyai.cleaning_offer_campaign TO propertyai_stage_b_migration;
GRANT UPDATE (
    candidate_status, proposal_version, proposed_start_at, proposed_end_at,
    proposed_buffer_before_minutes, proposed_buffer_after_minutes, buffer_basis,
    buffer_policy_ref, declined_at, accepted_at, updated_at
) ON propertyai.cleaning_offer_candidate TO propertyai_stage_b_migration;
GRANT UPDATE (assignment_status, ended_at, end_reason_code, updated_at)
    ON propertyai.cleaning_assignment TO propertyai_stage_b_migration;
GRANT UPDATE (case_status, reason_code, reason_text, updated_at)
    ON propertyai.cleaner_unavailability_case TO propertyai_stage_b_migration;
GRANT UPDATE (request_status, decided_at, decision_code, updated_at)
    ON propertyai.cleaner_reassignment_request TO propertyai_stage_b_migration;
GRANT UPDATE (
    target_schedule_revision_id, case_version, reconciliation_status,
    resolved_at, resolution_code, updated_at
) ON propertyai.cleaning_schedule_reconciliation TO propertyai_stage_b_migration;
GRANT UPDATE (
    external_uid, sync_status, last_applied_aggregate_version,
    external_version, last_synced_at, updated_at
) ON propertyai.integration_resource_binding TO propertyai_stage_b_migration;

-- Explicit protected-object boundary. These REVOKEs are repeated after grants
-- so later edits fail closed if a protected table is accidentally added above.
REVOKE ALL ON propertyai.organization_member FROM propertyai_stage_b_migration;
REVOKE ALL ON propertyai.cleaner_schedule_block FROM propertyai_stage_b_migration;
REVOKE ALL ON propertyai.authority_epoch FROM propertyai_stage_b_migration;
REVOKE ALL ON propertyai.domain_event FROM propertyai_stage_b_migration;
REVOKE ALL ON propertyai.business_scheduled_action FROM propertyai_stage_b_migration;
REVOKE ALL ON propertyai.integration_outbox FROM propertyai_stage_b_migration;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA propertyai FROM propertyai_stage_b_migration;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA propertyai FROM propertyai_stage_b_migration;

-- Read-only safety reconciliation is required for zero-effect and unchanged-
-- epoch proof. No mutation privilege accompanies these exact grants.
GRANT SELECT ON
    propertyai.flyway_schema_history,
    propertyai.authority_epoch,
    propertyai.domain_event,
    propertyai.business_scheduled_action,
    propertyai.integration_outbox
TO propertyai_stage_b_migration;

ALTER ROLE propertyai_stage_b_migration SET timezone = 'UTC';
ALTER ROLE propertyai_stage_b_migration SET search_path = pg_catalog, propertyai;
