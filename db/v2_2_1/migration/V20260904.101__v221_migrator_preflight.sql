-- Flyway V2.2.1 preflight.
-- JDBC startup options retain SESSION_USER=propertyai_flyway while activating
-- CURRENT_USER=propertyai_owner on every physical connection. afterConnect
-- independently reasserts and verifies the same identity contract.

DO $preflight_session$
BEGIN
    IF session_user <> 'propertyai_flyway'
       OR current_user <> 'propertyai_owner' THEN
        RAISE EXCEPTION
            'V221_FLYWAY_IDENTITY_INVALID session_user=% current_user=%',
            session_user, current_user;
    END IF;
END
$preflight_session$;

DO $preflight$
DECLARE
    v_schema_owner text;
    v_history_owner text;
    v_extension_count integer;
    v_unexpected_paths integer;
BEGIN
    IF (
        SELECT count(*) FROM pg_roles
         WHERE rolname IN (
             'propertyai_owner', 'propertyai_migrator', 'propertyai_flyway',
             'propertyai_app_runtime', 'propertyai_async_worker', 'propertyai_readonly'
         )
    ) <> 6 THEN
        RAISE EXCEPTION 'V221_REQUIRED_ROLE_MISSING';
    END IF;

    -- Exact role attributes must still match the privileged bootstrap contract.
    IF EXISTS (
        SELECT 1 FROM pg_roles
         WHERE rolname IN ('propertyai_owner','propertyai_migrator','propertyai_app_runtime','propertyai_async_worker','propertyai_readonly')
           AND (rolcanlogin OR rolinherit OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'V221_GROUP_ROLE_ATTRIBUTE_DRIFT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles
         WHERE rolname='propertyai_flyway'
           AND rolcanlogin AND NOT rolinherit AND NOT rolsuper AND NOT rolcreatedb
           AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION 'V221_FLYWAY_ROLE_ATTRIBUTE_DRIFT';
    END IF;

    SELECT pg_get_userbyid(nspowner) INTO v_schema_owner
      FROM pg_namespace WHERE nspname='propertyai';
    IF v_schema_owner IS DISTINCT FROM 'propertyai_owner' THEN
        RAISE EXCEPTION 'V221_SCHEMA_OWNER_INVALID actual=%', v_schema_owner;
    END IF;

    SELECT pg_get_userbyid(c.relowner) INTO v_history_owner
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'propertyai'
       AND c.relname = 'flyway_schema_history'
       AND c.relkind = 'r';
    IF v_history_owner IS DISTINCT FROM 'propertyai_owner' THEN
        RAISE EXCEPTION 'V221_FLYWAY_HISTORY_OWNER_INVALID actual=%', v_history_owner;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relname = 'flyway_schema_history'
    ) THEN
        RAISE EXCEPTION 'V221_PUBLIC_FLYWAY_HISTORY_FORBIDDEN';
    END IF;

    SELECT count(*) INTO v_extension_count FROM pg_extension WHERE extname='btree_gist';
    IF v_extension_count <> 1 THEN
        RAISE EXCEPTION 'V221_BTREE_GIST_MISSING';
    END IF;

    IF (
        SELECT count(*)
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
          JOIN pg_roles member_role ON member_role.oid = m.member
          JOIN pg_roles grantor_role ON grantor_role.oid = m.grantor
         WHERE target_role.rolname = 'propertyai_owner'
           AND member_role.rolname = 'propertyai_migrator'
           AND NOT m.inherit_option
           AND m.set_option
           AND NOT m.admin_option
           AND grantor_role.rolsuper
    ) <> 1 OR (
        SELECT count(*)
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname = 'propertyai_owner'
    ) <> 1 THEN
        RAISE EXCEPTION 'V221_OWNER_DIRECT_MEMBER_SET_INVALID';
    END IF;

    IF (
        SELECT count(*)
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
          JOIN pg_roles member_role ON member_role.oid = m.member
          JOIN pg_roles grantor_role ON grantor_role.oid = m.grantor
         WHERE target_role.rolname = 'propertyai_migrator'
           AND member_role.rolname = 'propertyai_flyway'
           AND NOT m.inherit_option
           AND m.set_option
           AND NOT m.admin_option
           AND grantor_role.rolsuper
    ) <> 1 OR (
        SELECT count(*)
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname = 'propertyai_migrator'
    ) <> 1 THEN
        RAISE EXCEPTION 'V221_MIGRATOR_DIRECT_MEMBER_SET_INVALID';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname = 'propertyai_flyway'
    ) THEN
        RAISE EXCEPTION 'V221_FLYWAY_MUST_HAVE_NO_MEMBERS';
    END IF;

    WITH RECURSIVE set_paths(root_oid, member_oid, path) AS (
        SELECT m.roleid, m.member, ARRAY[m.roleid, m.member]
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname IN ('propertyai_owner', 'propertyai_migrator')
           AND m.set_option
        UNION ALL
        SELECT p.root_oid, m.member, p.path || m.member
          FROM set_paths p
          JOIN pg_auth_members m ON m.roleid = p.member_oid
         WHERE m.set_option
           AND NOT m.member = ANY (p.path)
    )
    SELECT count(*) INTO v_unexpected_paths
      FROM set_paths p
      JOIN pg_roles root_role ON root_role.oid = p.root_oid
      JOIN pg_roles member_role ON member_role.oid = p.member_oid
     WHERE NOT (
         (root_role.rolname = 'propertyai_owner'
          AND member_role.rolname IN ('propertyai_migrator', 'propertyai_flyway'))
         OR
         (root_role.rolname = 'propertyai_migrator'
          AND member_role.rolname = 'propertyai_flyway')
     );
    IF v_unexpected_paths <> 0 THEN
        RAISE EXCEPTION 'V221_UNEXPECTED_PRIVILEGED_SET_ROLE_PATHS: %', v_unexpected_paths;
    END IF;
END
$preflight$;

-- Harden default function privilege before any SECURITY DEFINER function is created.
ALTER DEFAULT PRIVILEGES FOR ROLE propertyai_owner IN SCHEMA propertyai
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
