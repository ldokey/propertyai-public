\set ON_ERROR_STOP on
BEGIN;

-- ---------- Catalog / privilege contract ----------
DO $$
DECLARE
    v_history_owner text;
    v_unexpected_paths integer;
BEGIN
    IF to_regclass('propertyai.flyway_schema_history') IS NULL THEN
        RAISE EXCEPTION 'PROPERTYAI_FLYWAY_HISTORY_MISSING';
    END IF;
    IF to_regclass('public.flyway_schema_history') IS NOT NULL THEN
        RAISE EXCEPTION 'PUBLIC_FLYWAY_HISTORY_FORBIDDEN';
    END IF;
    SELECT pg_get_userbyid(c.relowner) INTO STRICT v_history_owner
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'propertyai'
       AND c.relname = 'flyway_schema_history'
       AND c.relkind = 'r';
    IF v_history_owner <> 'propertyai_owner' THEN
        RAISE EXCEPTION 'FLYWAY_HISTORY_OWNER_INVALID actual=%', v_history_owner;
    END IF;

    IF (
        SELECT count(*)
          FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles target_role ON target_role.oid = m.roleid
          JOIN pg_catalog.pg_roles member_role ON member_role.oid = m.member
          JOIN pg_catalog.pg_roles grantor_role ON grantor_role.oid = m.grantor
         WHERE target_role.rolname = 'propertyai_owner'
           AND member_role.rolname = 'propertyai_migrator'
           AND NOT m.inherit_option
           AND m.set_option
           AND NOT m.admin_option
           AND grantor_role.rolsuper
    ) <> 1 OR (
        SELECT count(*)
          FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname = 'propertyai_owner'
    ) <> 1 THEN
        RAISE EXCEPTION 'OWNER_DIRECT_MEMBER_SET_INVALID';
    END IF;

    IF (
        SELECT count(*)
          FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles target_role ON target_role.oid = m.roleid
          JOIN pg_catalog.pg_roles member_role ON member_role.oid = m.member
          JOIN pg_catalog.pg_roles grantor_role ON grantor_role.oid = m.grantor
         WHERE target_role.rolname = 'propertyai_migrator'
           AND member_role.rolname = 'propertyai_flyway'
           AND NOT m.inherit_option
           AND m.set_option
           AND NOT m.admin_option
           AND grantor_role.rolsuper
    ) <> 1 OR (
        SELECT count(*)
          FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname = 'propertyai_migrator'
    ) <> 1 THEN
        RAISE EXCEPTION 'MIGRATOR_DIRECT_MEMBER_SET_INVALID';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname = 'propertyai_flyway'
    ) THEN
        RAISE EXCEPTION 'FLYWAY_MUST_HAVE_NO_MEMBERS';
    END IF;

    WITH RECURSIVE set_paths(root_oid, member_oid, path) AS (
        SELECT m.roleid, m.member, ARRAY[m.roleid, m.member]
          FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname IN ('propertyai_owner', 'propertyai_migrator')
           AND m.set_option
        UNION ALL
        SELECT p.root_oid, m.member, p.path || m.member
          FROM set_paths p
          JOIN pg_catalog.pg_auth_members m ON m.roleid = p.member_oid
         WHERE m.set_option
           AND NOT m.member = ANY (p.path)
    )
    SELECT count(*) INTO v_unexpected_paths
      FROM set_paths p
      JOIN pg_catalog.pg_roles root_role ON root_role.oid = p.root_oid
      JOIN pg_catalog.pg_roles member_role ON member_role.oid = p.member_oid
     WHERE NOT (
         (root_role.rolname = 'propertyai_owner'
          AND member_role.rolname IN ('propertyai_migrator', 'propertyai_flyway'))
         OR
         (root_role.rolname = 'propertyai_migrator'
          AND member_role.rolname = 'propertyai_flyway')
     );
    IF v_unexpected_paths <> 0 THEN
        RAISE EXCEPTION 'UNEXPECTED_PRIVILEGED_SET_ROLE_PATHS count=%', v_unexpected_paths;
    END IF;
END $$;

DO $$
DECLARE v_bad integer;
BEGIN
    SELECT count(*) INTO v_bad
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
     WHERE n.nspname='propertyai' AND p.prosecdef
       AND (
         NOT ('search_path=pg_catalog, propertyai, pg_temp' = ANY(COALESCE(p.proconfig, ARRAY[]::text[])))
         OR EXISTS (
           SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f',p.proowner))) a
            WHERE a.grantee=0 AND a.privilege_type='EXECUTE'
         )
       );
    IF v_bad <> 0 THEN RAISE EXCEPTION 'SECURITY_DEFINER_HARDENING_INVALID count=%',v_bad; END IF;
END $$;

DO $$
BEGIN
    IF has_column_privilege('propertyai_app_runtime','propertyai.cleaning_assignment','scheduled_start_at','UPDATE') THEN RAISE EXCEPTION 'APP_CAN_UPDATE_ASSIGNMENT_SLOT'; END IF;
    IF has_table_privilege('propertyai_app_runtime','propertyai.cleaning_schedule_revision','INSERT') THEN RAISE EXCEPTION 'APP_CAN_INSERT_REVISION_DIRECTLY'; END IF;
    IF has_column_privilege('propertyai_app_runtime','propertyai.cleaning_job','current_schedule_revision_id','UPDATE') THEN RAISE EXCEPTION 'APP_CAN_UPDATE_REVISION_POINTER_DIRECTLY'; END IF;
    IF has_table_privilege('propertyai_app_runtime','propertyai.authority_epoch','UPDATE') THEN RAISE EXCEPTION 'APP_CAN_UPDATE_EPOCH'; END IF;
    IF has_table_privilege('propertyai_app_runtime','propertyai.business_scheduled_action','UPDATE') THEN RAISE EXCEPTION 'APP_HAS_GENERIC_SCHEDULED_ACTION_UPDATE'; END IF;
    IF has_table_privilege('propertyai_app_runtime','propertyai.integration_outbox','UPDATE') THEN RAISE EXCEPTION 'APP_HAS_GENERIC_OUTBOX_UPDATE'; END IF;
    IF has_column_privilege('propertyai_app_runtime','propertyai.business_scheduled_action','action_status','INSERT') THEN RAISE EXCEPTION 'APP_CAN_INSERT_ACTION_STATUS'; END IF;
    IF has_column_privilege('propertyai_app_runtime','propertyai.integration_outbox','outbox_status','INSERT') THEN RAISE EXCEPTION 'APP_CAN_INSERT_OUTBOX_STATUS'; END IF;
    IF has_column_privilege('propertyai_app_runtime','propertyai.business_scheduled_action','lease_fence','INSERT') THEN RAISE EXCEPTION 'APP_CAN_INSERT_ACTION_FENCE'; END IF;
    IF has_column_privilege('propertyai_app_runtime','propertyai.integration_outbox','lease_fence','INSERT') THEN RAISE EXCEPTION 'APP_CAN_INSERT_OUTBOX_FENCE'; END IF;
    IF has_table_privilege('propertyai_async_worker','propertyai.business_scheduled_action','UPDATE') THEN RAISE EXCEPTION 'WORKER_HAS_GENERIC_ACTION_UPDATE'; END IF;
    IF has_table_privilege('propertyai_async_worker','propertyai.integration_outbox','UPDATE') THEN RAISE EXCEPTION 'WORKER_HAS_GENERIC_OUTBOX_UPDATE'; END IF;
    IF has_table_privilege('propertyai_readonly','propertyai.external_identity','SELECT') THEN RAISE EXCEPTION 'READONLY_RAW_IDENTITY'; END IF;
    IF has_table_privilege('propertyai_readonly','propertyai.command_receipt','SELECT') THEN RAISE EXCEPTION 'READONLY_RAW_RECEIPT'; END IF;
END $$;

-- ---------- Minimal domain fixture ----------
SET ROLE propertyai_owner;
INSERT INTO propertyai.organization VALUES
('00000000-0000-0000-0000-000000000001','ORG-T','Org Test','ACTIVE','TEST',clock_timestamp(),clock_timestamp());
INSERT INTO propertyai.party VALUES
('00000000-0000-0000-0000-000000000002','PTY-H','Host','TEST',true,clock_timestamp(),clock_timestamp()),
('00000000-0000-0000-0000-000000000003','PTY-C','Cleaner','TEST',true,clock_timestamp(),clock_timestamp());
INSERT INTO propertyai.property VALUES
('00000000-0000-0000-0000-000000000004','00000000-0000-0000-0000-000000000001','P-T','Property Test','Asia/Seoul',true,clock_timestamp(),clock_timestamp());
INSERT INTO propertyai.cleaner_profile VALUES
('00000000-0000-0000-0000-000000000003','ACTIVE',NULL,NULL,clock_timestamp(),clock_timestamp());
INSERT INTO propertyai.reservation(
 reservation_id,reservation_code,property_id,reservation_status,check_in_at,check_out_at,source_version
) VALUES (
 '00000000-0000-0000-0000-000000000005','R-T','00000000-0000-0000-0000-000000000004','CONFIRMED',
 '2026-09-10 15:00+09','2026-09-11 11:00+09',1
);
INSERT INTO propertyai.cleaning_job(
 cleaning_id,cleaning_code,reservation_id,property_id,schedule_source_type,cleaning_status
) VALUES (
 '00000000-0000-0000-0000-000000000006','C-T','00000000-0000-0000-0000-000000000005',
 '00000000-0000-0000-0000-000000000004','RESERVATION_CHECKOUT','PLANNED'
);
INSERT INTO propertyai.cleaning_schedule_revision(
 schedule_revision_id,cleaning_id,revision_no,service_window_start_at,service_deadline_at,
 required_work_minutes,source_checkout_at,source_reservation_version,change_reason_code
) VALUES (
 '00000000-0000-0000-0000-000000000008','00000000-0000-0000-0000-000000000006',1,
 '2026-09-11 11:00+09','2026-09-11 15:00+09',60,'2026-09-11 11:00+09',1,'INITIAL'
);
UPDATE propertyai.cleaning_job
   SET current_schedule_revision_id='00000000-0000-0000-0000-000000000008'
 WHERE cleaning_id='00000000-0000-0000-0000-000000000006';
INSERT INTO propertyai.cleaning_offer_campaign(
 campaign_id,cleaning_id,schedule_revision_id,campaign_no,campaign_status,open_tier_floor,max_tier,
 tier_expand_after_minutes,acceptance_cutoff_at,base_fee_krw,replacement_urgency,urgent_premium_krw,
 total_agreed_fee_krw,opened_at
) VALUES (
 '00000000-0000-0000-0000-00000000000b','00000000-0000-0000-0000-000000000006',
 '00000000-0000-0000-0000-000000000008',1,'OPEN',1,2,1440,
 '2026-09-11 10:30+09',50000,'NORMAL',0,50000,'2026-09-10 10:00+09'
);
INSERT INTO propertyai.cleaning_offer_candidate(
 offer_candidate_id,campaign_id,cleaner_party_id,tier_no,candidate_status,proposal_version,
 proposed_start_at,proposed_end_at,proposed_buffer_before_minutes,proposed_buffer_after_minutes,
 buffer_basis,evaluated_at
) VALUES (
 '00000000-0000-0000-0000-00000000000c','00000000-0000-0000-0000-00000000000b',
 '00000000-0000-0000-0000-000000000003',1,'ELIGIBLE',1,
 '2026-09-11 11:00+09','2026-09-11 12:00+09',0,0,'NOT_APPLIED','2026-09-10 10:00+09'
);
RESET ROLE;

-- ---------- F-DDL-01: exact normal Accept order ----------
SET ROLE propertyai_app_runtime;
INSERT INTO propertyai.cleaning_assignment(
 assignment_id,cleaning_id,assignment_no,schedule_revision_id,campaign_id,offer_candidate_id,
 accepted_proposal_version,cleaner_party_id,assignment_source,assignment_status,booked_at,
 scheduled_start_at,scheduled_end_at,work_minutes_snapshot,
 travel_buffer_before_minutes,travel_buffer_after_minutes,buffer_basis,
 base_fee_krw,replacement_urgency,urgent_premium_krw,total_agreed_fee_krw
) VALUES (
 '00000000-0000-0000-0000-00000000000d','00000000-0000-0000-0000-000000000006',1,
 '00000000-0000-0000-0000-000000000008','00000000-0000-0000-0000-00000000000b',
 '00000000-0000-0000-0000-00000000000c',1,'00000000-0000-0000-0000-000000000003',
 'OFFER_ACCEPTED','HARD_BOOKED',clock_timestamp(),'2026-09-11 11:00+09','2026-09-11 12:00+09',60,
 0,0,'NOT_APPLIED',50000,'NORMAL',0,50000
);
UPDATE propertyai.cleaning_offer_candidate
   SET candidate_status='ACCEPTED', accepted_at=clock_timestamp(), updated_at=clock_timestamp()
 WHERE offer_candidate_id='00000000-0000-0000-0000-00000000000c' AND proposal_version=1 AND candidate_status='ELIGIBLE';
UPDATE propertyai.cleaning_offer_campaign
   SET campaign_status='CLOSED', closed_at=clock_timestamp(), closed_reason_code='ACCEPTED', updated_at=clock_timestamp()
 WHERE campaign_id='00000000-0000-0000-0000-00000000000b' AND campaign_status='OPEN';
UPDATE propertyai.cleaning_job SET cleaning_status='ASSIGNED', updated_at=clock_timestamp()
 WHERE cleaning_id='00000000-0000-0000-0000-000000000006';
SET CONSTRAINTS ALL IMMEDIATE;

-- Candidate terminal+proposal mutation in one statement remains rejected.
DO $$
BEGIN
    BEGIN
        UPDATE propertyai.cleaning_offer_candidate
           SET proposed_end_at=proposed_end_at+interval '1 minute', proposal_version=proposal_version+1,
               updated_at=clock_timestamp()
         WHERE offer_candidate_id='00000000-0000-0000-0000-00000000000c';
        RAISE EXCEPTION 'EXPECTED_CANDIDATE_TERMINAL_FREEZE_NOT_RAISED';
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM='EXPECTED_CANDIDATE_TERMINAL_FREEZE_NOT_RAISED' THEN RAISE; END IF;
        IF position('CANDIDATE_TERMINAL_STATE_FROZEN' in SQLERRM)=0 THEN RAISE; END IF;
    END;
END $$;

-- ---------- F-DDL-04: checkout revision source is mandatory and exact ----------
DO $$
BEGIN
    BEGIN
        PERFORM propertyai.append_cleaning_schedule_revision(
          '00000000-0000-0000-0000-000000000009','00000000-0000-0000-0000-000000000006',
          '00000000-0000-0000-0000-000000000008','2026-09-11 12:00+09','2026-09-11 16:00+09',60,
          NULL,NULL,'CHECKOUT_CHANGED',NULL);
        RAISE EXCEPTION 'EXPECTED_CHECKOUT_SOURCE_REQUIRED_NOT_RAISED';
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM='EXPECTED_CHECKOUT_SOURCE_REQUIRED_NOT_RAISED' THEN RAISE; END IF;
        IF position('CHECKOUT_REVISION_REQUIRES_RESERVATION_SOURCE' in SQLERRM)=0 THEN RAISE; END IF;
    END;
END $$;

UPDATE propertyai.reservation
   SET check_out_at='2026-09-11 12:00+09', source_version=2, updated_at=clock_timestamp()
 WHERE reservation_id='00000000-0000-0000-0000-000000000005';

DO $$
BEGIN
    BEGIN
        PERFORM propertyai.append_cleaning_schedule_revision(
          '00000000-0000-0000-0000-000000000009','00000000-0000-0000-0000-000000000006',
          '00000000-0000-0000-0000-000000000008','2026-09-11 12:00+09','2026-09-11 16:00+09',60,
          '2026-09-11 12:00+09',1,'CHECKOUT_CHANGED',NULL);
        RAISE EXCEPTION 'EXPECTED_CHECKOUT_SOURCE_MISMATCH_NOT_RAISED';
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM='EXPECTED_CHECKOUT_SOURCE_MISMATCH_NOT_RAISED' THEN RAISE; END IF;
        IF position('CHECKOUT_REVISION_SOURCE_MISMATCH' in SQLERRM)=0 THEN RAISE; END IF;
    END;
END $$;

SELECT propertyai.append_cleaning_schedule_revision(
  '00000000-0000-0000-0000-000000000009','00000000-0000-0000-0000-000000000006',
  '00000000-0000-0000-0000-000000000008','2026-09-11 12:00+09','2026-09-11 16:00+09',60,
  '2026-09-11 12:00+09',2,'CHECKOUT_CHANGED',NULL
);

-- ---------- F-DDL-02/03: queue/outbox creation and mutation boundary ----------
INSERT INTO propertyai.business_scheduled_action(
 scheduled_action_id,action_type,aggregate_type,aggregate_id,due_at,available_at,idempotency_key,payload,max_attempts
) VALUES
('00000000-0000-0000-0000-00000000000f','TEST','CLEANING','00000000-0000-0000-0000-000000000006',clock_timestamp()-interval '1 minute',clock_timestamp()-interval '1 minute','ACT-1','{}',3),
('00000000-0000-0000-0000-000000000011','TEST','CLEANING','00000000-0000-0000-0000-000000000006',clock_timestamp()-interval '1 minute',clock_timestamp()-interval '1 minute','ACT-2','{}',3);
INSERT INTO propertyai.integration_outbox(
 outbox_id,event_type,aggregate_type,aggregate_id,destination_type,available_at,idempotency_key,payload,max_attempts
) VALUES
('00000000-0000-0000-0000-000000000010','TEST','CLEANING','00000000-0000-0000-0000-000000000006','TELEGRAM',clock_timestamp()-interval '1 minute','OUT-1','{}',3),
('00000000-0000-0000-0000-000000000012','TEST','CLEANING','00000000-0000-0000-0000-000000000006','TELEGRAM',clock_timestamp()-interval '1 minute','OUT-2','{}',3);

DO $$
BEGIN
    BEGIN
        INSERT INTO propertyai.business_scheduled_action(
          scheduled_action_id,action_type,aggregate_type,aggregate_id,due_at,available_at,action_status,idempotency_key,payload,max_attempts,
          attempt_count,lease_owner,lease_until,lease_fence
        ) VALUES (
          '00000000-0000-0000-0000-000000000013','FORGED','CLEANING','00000000-0000-0000-0000-000000000006',
          clock_timestamp(),clock_timestamp(),'RUNNING','ACT-FORGE','{}',3,1,'fake',clock_timestamp()+interval '1 minute',1
        );
        RAISE EXCEPTION 'EXPECTED_ACTION_FORGED_INSERT_DENIED_NOT_RAISED';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;

    BEGIN
        INSERT INTO propertyai.integration_outbox(
          outbox_id,event_type,aggregate_type,aggregate_id,destination_type,available_at,outbox_status,idempotency_key,payload,max_attempts,delivered_at
        ) VALUES (
          '00000000-0000-0000-0000-000000000014','FORGED','CLEANING','00000000-0000-0000-0000-000000000006','TELEGRAM',
          clock_timestamp(),'SUCCEEDED','OUT-FORGE','{}',3,clock_timestamp()
        );
        RAISE EXCEPTION 'EXPECTED_OUTBOX_FORGED_INSERT_DENIED_NOT_RAISED';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;

    BEGIN
        UPDATE propertyai.business_scheduled_action SET action_status='DEAD_LETTER'
         WHERE scheduled_action_id='00000000-0000-0000-0000-00000000000f';
        RAISE EXCEPTION 'EXPECTED_ACTION_DIRECT_STATUS_UPDATE_DENIED_NOT_RAISED';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END $$;

-- App cancellation is narrow, not generic status UPDATE.
DO $$ DECLARE v boolean; BEGIN
    SELECT propertyai.cancel_business_scheduled_action('00000000-0000-0000-0000-000000000011') INTO v;
    IF NOT v THEN RAISE EXCEPTION 'ACTION_CANCEL_FUNCTION_FAILED'; END IF;
    SELECT propertyai.cancel_integration_outbox('00000000-0000-0000-0000-000000000012') INTO v;
    IF NOT v THEN RAISE EXCEPTION 'OUTBOX_CANCEL_FUNCTION_FAILED'; END IF;
END $$;
RESET ROLE;

SET ROLE propertyai_async_worker;
DO $$
BEGIN
    BEGIN
        PERFORM * FROM propertyai.claim_business_scheduled_actions('worker-null',NULL,60);
        RAISE EXCEPTION 'EXPECTED_NULL_LIMIT_REJECTION_NOT_RAISED';
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM='EXPECTED_NULL_LIMIT_REJECTION_NOT_RAISED' THEN RAISE; END IF;
        IF position('INVALID_QUEUE_CLAIM_ARGUMENT' in SQLERRM)=0 THEN RAISE; END IF;
    END;
    BEGIN
        PERFORM * FROM propertyai.claim_integration_outbox('worker-big',101,60);
        RAISE EXCEPTION 'EXPECTED_MAX_LIMIT_REJECTION_NOT_RAISED';
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM='EXPECTED_MAX_LIMIT_REJECTION_NOT_RAISED' THEN RAISE; END IF;
        IF position('INVALID_OUTBOX_CLAIM_ARGUMENT' in SQLERRM)=0 THEN RAISE; END IF;
    END;
END $$;

SELECT count(*) AS claimed_actions FROM propertyai.claim_business_scheduled_actions('worker-a',1,60);
DO $$ DECLARE v boolean; v_fence bigint; BEGIN
    SELECT lease_fence INTO STRICT v_fence FROM propertyai.business_scheduled_action WHERE scheduled_action_id='00000000-0000-0000-0000-00000000000f';
    SELECT propertyai.complete_business_scheduled_action('00000000-0000-0000-0000-00000000000f','worker-a',999) INTO v;
    IF v THEN RAISE EXCEPTION 'QUEUE_COMPLETE_WRONG_FENCE_MUST_FALSE'; END IF;
    SELECT propertyai.complete_business_scheduled_action('00000000-0000-0000-0000-00000000000f','worker-a',v_fence) INTO v;
    IF NOT v THEN RAISE EXCEPTION 'QUEUE_COMPLETE_CORRECT_FENCE_MUST_TRUE'; END IF;
END $$;
SELECT count(*) AS claimed_outbox FROM propertyai.claim_integration_outbox('worker-b',1,60);
DO $$ DECLARE v boolean; v_fence bigint; BEGIN
    SELECT lease_fence INTO STRICT v_fence FROM propertyai.integration_outbox WHERE outbox_id='00000000-0000-0000-0000-000000000010';
    SELECT propertyai.fail_integration_outbox('00000000-0000-0000-0000-000000000010','wrong-worker',v_fence,'ERR',60) INTO v;
    IF v THEN RAISE EXCEPTION 'OUTBOX_FAIL_WRONG_OWNER_MUST_FALSE'; END IF;
    SELECT propertyai.fail_integration_outbox('00000000-0000-0000-0000-000000000010','worker-b',v_fence,'ERR',60) INTO v;
    IF NOT v THEN RAISE EXCEPTION 'OUTBOX_FAIL_CORRECT_OWNER_FENCE_MUST_TRUE'; END IF;
END $$;
RESET ROLE;

ROLLBACK;
