\set ON_ERROR_STOP on

DO $verify_role$
DECLARE
    table_name text;
    column_name text;
    authority_role text;
    relation_name text;
    expected_insert text[] := ARRAY[
        'organization', 'property', 'rental_unit', 'party', 'cleaner_profile',
        'external_identity', 'cleaner_property_roster', 'reservation',
        'cleaning_job', 'cleaning_schedule_revision', 'cleaning_offer_campaign',
        'cleaning_offer_candidate', 'cleaning_assignment',
        'cleaner_unavailability_case', 'cleaner_reassignment_request',
        'cleaning_schedule_reconciliation', 'command_receipt',
        'integration_resource_binding'
    ];
    expected_select text[] := ARRAY[
        'organization', 'property', 'rental_unit', 'party', 'cleaner_profile',
        'external_identity', 'cleaner_property_roster', 'reservation',
        'cleaning_job', 'cleaning_schedule_revision', 'cleaning_offer_campaign',
        'cleaning_offer_candidate', 'cleaning_assignment',
        'cleaner_unavailability_case', 'cleaner_reassignment_request',
        'cleaning_schedule_reconciliation', 'command_receipt',
        'integration_resource_binding', 'flyway_schema_history',
        'authority_epoch', 'domain_event', 'business_scheduled_action',
        'integration_outbox'
    ];
    expected_updates jsonb := jsonb_build_object(
        'organization', to_jsonb(ARRAY['display_name','organization_status','updated_at']),
        'property', to_jsonb(ARRAY['display_name','timezone_name','active','updated_at']),
        'rental_unit', to_jsonb(ARRAY['display_name','active','updated_at']),
        'party', to_jsonb(ARRAY['display_name','active','updated_at']),
        'cleaner_profile', to_jsonb(ARRAY['operational_status','max_daily_work_minutes','max_daily_jobs','updated_at']),
        'external_identity', to_jsonb(ARRAY['revoked_at']),
        'cleaner_property_roster', to_jsonb(ARRAY['roster_status','offer_tier','priority_within_tier','eligible_from','eligible_until','updated_at']),
        'reservation', to_jsonb(ARRAY['reservation_status','check_in_at','check_out_at','source_version','updated_at']),
        'cleaning_job', to_jsonb(ARRAY['cleaning_status','current_schedule_revision_id','updated_at']),
        'cleaning_offer_campaign', to_jsonb(ARRAY['campaign_status','open_tier_floor','closed_at','closed_reason_code','updated_at']),
        'cleaning_offer_candidate', to_jsonb(ARRAY['candidate_status','proposal_version','proposed_start_at','proposed_end_at','proposed_buffer_before_minutes','proposed_buffer_after_minutes','buffer_basis','buffer_policy_ref','declined_at','accepted_at','updated_at']),
        'cleaning_assignment', to_jsonb(ARRAY['assignment_status','ended_at','end_reason_code','updated_at']),
        'cleaner_unavailability_case', to_jsonb(ARRAY['case_status','reason_code','reason_text','updated_at']),
        'cleaner_reassignment_request', to_jsonb(ARRAY['request_status','decided_at','decision_code','updated_at']),
        'cleaning_schedule_reconciliation', to_jsonb(ARRAY['target_schedule_revision_id','case_version','reconciliation_status','resolved_at','resolution_code','updated_at']),
        'integration_resource_binding', to_jsonb(ARRAY['external_uid','sync_status','last_applied_aggregate_version','external_version','last_synced_at','updated_at'])
    );
    should_update boolean;
BEGIN
    IF session_user <> 'propertyai_stage_b_migration'
       OR current_user <> 'propertyai_stage_b_migration' THEN
        RAISE EXCEPTION 'STAGE_B_NOT_DIRECT_LOGIN';
    END IF;

    IF NOT has_schema_privilege(session_user, 'propertyai', 'USAGE') THEN
        RAISE EXCEPTION 'STAGE_B_MISSING_SCHEMA_USAGE';
    END IF;
    IF has_schema_privilege(session_user, 'propertyai', 'CREATE') THEN
        RAISE EXCEPTION 'STAGE_B_SCHEMA_CREATE_GRANTED';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_auth_members membership
          JOIN pg_catalog.pg_roles member ON member.oid = membership.member
         WHERE member.rolname = session_user
    ) THEN
        RAISE EXCEPTION 'STAGE_B_EXTRA_ROLE_MEMBERSHIP';
    END IF;

    FOREACH authority_role IN ARRAY ARRAY[
        'propertyai_owner',
        'propertyai_migrator',
        'propertyai_app_runtime',
        'propertyai_async_worker'
    ] LOOP
        IF pg_has_role(session_user, authority_role, 'SET') THEN
            RAISE EXCEPTION 'STAGE_B_CAN_SET_AUTHORITY_ROLE: %', authority_role;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_proc p
          JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'propertyai'
           AND has_function_privilege(session_user, p.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'STAGE_B_FUNCTION_EXECUTE_GRANTED';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'propertyai'
           AND c.relkind = 'S'
           AND (has_sequence_privilege(session_user, c.oid, 'USAGE')
                OR has_sequence_privilege(session_user, c.oid, 'SELECT')
                OR has_sequence_privilege(session_user, c.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'STAGE_B_SEQUENCE_PRIVILEGE_GRANTED';
    END IF;

    FOR table_name IN
        SELECT c.relname
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'propertyai' AND c.relkind IN ('r','p')
         ORDER BY c.relname
    LOOP
        relation_name := format('propertyai.%I', table_name);
        IF has_table_privilege(session_user, relation_name, 'INSERT') <> (table_name = ANY(expected_insert)) THEN
            RAISE EXCEPTION 'STAGE_B_INSERT_PRIVILEGE_DRIFT: %', table_name;
        END IF;
        IF table_name = ANY(expected_select)
           AND NOT has_table_privilege(session_user, relation_name, 'SELECT') THEN
            RAISE EXCEPTION 'STAGE_B_REQUIRED_SELECT_MISSING: %', table_name;
        END IF;
        IF has_table_privilege(session_user, relation_name, 'DELETE')
           OR has_table_privilege(session_user, relation_name, 'TRUNCATE')
           OR has_table_privilege(session_user, relation_name, 'TRIGGER') THEN
            RAISE EXCEPTION 'STAGE_B_FORBIDDEN_TABLE_PRIVILEGE: %', table_name;
        END IF;

        FOR column_name IN
            SELECT a.attname
              FROM pg_catalog.pg_attribute a
              JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'propertyai'
               AND c.relname = table_name
               AND a.attnum > 0
               AND NOT a.attisdropped
        LOOP
            should_update := COALESCE((expected_updates -> table_name) ? column_name, false);
            IF has_column_privilege(session_user, relation_name, column_name, 'UPDATE') <> should_update THEN
                RAISE EXCEPTION 'STAGE_B_UPDATE_PRIVILEGE_DRIFT: %.%', table_name, column_name;
            END IF;
        END LOOP;
    END LOOP;

    -- Defense-in-depth attack probes for protected and deliberately empty
    -- Stage B population tables. Privilege denial must happen before any row
    -- or constraint behavior can matter.
    BEGIN
        UPDATE propertyai.authority_epoch SET current_epoch = current_epoch;
        RAISE EXCEPTION 'STAGE_B_AUTHORITY_EPOCH_UPDATE_SUCCEEDED';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        INSERT INTO propertyai.organization_member DEFAULT VALUES;
        RAISE EXCEPTION 'STAGE_B_ORGANIZATION_MEMBER_INSERT_SUCCEEDED';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        INSERT INTO propertyai.cleaner_schedule_block DEFAULT VALUES;
        RAISE EXCEPTION 'STAGE_B_SCHEDULE_BLOCK_INSERT_SUCCEEDED';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        INSERT INTO propertyai.domain_event(
            aggregate_type, aggregate_id, event_type, command_id, payload, occurred_at
        ) VALUES ('FORBIDDEN', gen_random_uuid(), 'FORBIDDEN', gen_random_uuid(), '{}', clock_timestamp());
        RAISE EXCEPTION 'STAGE_B_DOMAIN_EVENT_INSERT_SUCCEEDED';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        INSERT INTO propertyai.business_scheduled_action(
            scheduled_action_id, action_type, aggregate_type, aggregate_id,
            due_at, available_at, idempotency_key, payload, max_attempts
        ) VALUES (
            gen_random_uuid(), 'FORBIDDEN', 'FORBIDDEN', gen_random_uuid(),
            clock_timestamp(), clock_timestamp(), 'forbidden', '{}', 1
        );
        RAISE EXCEPTION 'STAGE_B_SCHEDULED_ACTION_INSERT_SUCCEEDED';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        INSERT INTO propertyai.integration_outbox(
            outbox_id, event_type, aggregate_type, aggregate_id, destination_type,
            available_at, idempotency_key, payload, max_attempts
        ) VALUES (
            gen_random_uuid(), 'FORBIDDEN', 'FORBIDDEN', gen_random_uuid(), 'FORBIDDEN',
            clock_timestamp(), 'forbidden', '{}', 1
        );
        RAISE EXCEPTION 'STAGE_B_OUTBOX_INSERT_SUCCEEDED';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
END
$verify_role$;

SELECT 'STAGE_B_MIGRATION_ROLE_VERIFIED' AS result;
