-- PropertyAI V2.2.1 privileged bootstrap.
-- Run exactly once (or idempotently) by a DBA/superuser BEFORE Flyway.
-- Credentials/passwords for LOGIN roles are provisioned outside this file.

DO $bootstrap$
DECLARE
    r record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'propertyai_owner') THEN
        CREATE ROLE propertyai_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'propertyai_migrator') THEN
        CREATE ROLE propertyai_migrator NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'propertyai_flyway') THEN
        CREATE ROLE propertyai_flyway LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'propertyai_app_runtime') THEN
        CREATE ROLE propertyai_app_runtime NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'propertyai_async_worker') THEN
        CREATE ROLE propertyai_async_worker NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'propertyai_readonly') THEN
        CREATE ROLE propertyai_readonly NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;

    -- Fail closed instead of silently accepting pre-existing role drift.
    FOR r IN
        SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
          FROM pg_roles
         WHERE rolname IN ('propertyai_owner','propertyai_migrator','propertyai_flyway',
                           'propertyai_app_runtime','propertyai_async_worker','propertyai_readonly')
    LOOP
        IF r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls OR r.rolinherit THEN
            RAISE EXCEPTION 'PROPERTYAI_ROLE_ATTRIBUTE_DRIFT: %', r.rolname;
        END IF;
        IF r.rolname = 'propertyai_flyway' AND NOT r.rolcanlogin THEN
            RAISE EXCEPTION 'PROPERTYAI_FLYWAY_MUST_BE_LOGIN';
        ELSIF r.rolname <> 'propertyai_flyway' AND r.rolcanlogin THEN
            RAISE EXCEPTION 'PROPERTYAI_GROUP_ROLE_MUST_BE_NOLOGIN: %', r.rolname;
        END IF;
    END LOOP;
END
$bootstrap$;

-- Reject drift before GRANT can repair or obscure wrong existing edge options.
-- Missing expected edges are allowed here because the GRANTs below create them.
DO $membership_precheck$
DECLARE
    v_count integer;
    v_unexpected_paths integer;
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
          JOIN pg_roles member_role ON member_role.oid = m.member
          JOIN pg_roles grantor_role ON grantor_role.oid = m.grantor
         WHERE target_role.rolname = 'propertyai_owner'
           AND (
               member_role.rolname <> 'propertyai_migrator'
               OR m.inherit_option
               OR NOT m.set_option
               OR m.admin_option
               OR NOT grantor_role.rolsuper
           )
    ) THEN
        RAISE EXCEPTION 'PROPERTYAI_OWNER_DIRECT_MEMBERSHIP_DRIFT';
    END IF;
    SELECT count(*) INTO v_count
      FROM pg_auth_members m
      JOIN pg_roles target_role ON target_role.oid = m.roleid
      JOIN pg_roles member_role ON member_role.oid = m.member
     WHERE target_role.rolname = 'propertyai_owner'
       AND member_role.rolname = 'propertyai_migrator';
    IF v_count > 1 THEN
        RAISE EXCEPTION 'PROPERTYAI_OWNER_MEMBERSHIP_DUPLICATE: %', v_count;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
          JOIN pg_roles member_role ON member_role.oid = m.member
          JOIN pg_roles grantor_role ON grantor_role.oid = m.grantor
         WHERE target_role.rolname = 'propertyai_migrator'
           AND (
               member_role.rolname <> 'propertyai_flyway'
               OR m.inherit_option
               OR NOT m.set_option
               OR m.admin_option
               OR NOT grantor_role.rolsuper
           )
    ) THEN
        RAISE EXCEPTION 'PROPERTYAI_MIGRATOR_DIRECT_MEMBERSHIP_DRIFT';
    END IF;
    SELECT count(*) INTO v_count
      FROM pg_auth_members m
      JOIN pg_roles target_role ON target_role.oid = m.roleid
      JOIN pg_roles member_role ON member_role.oid = m.member
     WHERE target_role.rolname = 'propertyai_migrator'
       AND member_role.rolname = 'propertyai_flyway';
    IF v_count > 1 THEN
        RAISE EXCEPTION 'PROPERTYAI_MIGRATOR_MEMBERSHIP_DUPLICATE: %', v_count;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname = 'propertyai_flyway'
    ) THEN
        RAISE EXCEPTION 'PROPERTYAI_FLYWAY_MUST_HAVE_NO_MEMBERS';
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
        RAISE EXCEPTION 'PROPERTYAI_UNEXPECTED_PRIVILEGED_SET_ROLE_PATHS: %', v_unexpected_paths;
    END IF;
END
$membership_precheck$;

GRANT propertyai_owner TO propertyai_migrator
    WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
GRANT propertyai_migrator TO propertyai_flyway
    WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;

DO $membership_postcheck$
DECLARE
    v_unexpected_paths integer;
BEGIN
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
        RAISE EXCEPTION 'PROPERTYAI_OWNER_DIRECT_MEMBER_SET_INVALID';
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
        RAISE EXCEPTION 'PROPERTYAI_MIGRATOR_DIRECT_MEMBER_SET_INVALID';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_auth_members m
          JOIN pg_roles target_role ON target_role.oid = m.roleid
         WHERE target_role.rolname = 'propertyai_flyway'
    ) THEN
        RAISE EXCEPTION 'PROPERTYAI_FLYWAY_MUST_HAVE_NO_MEMBERS';
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
        RAISE EXCEPTION 'PROPERTYAI_UNEXPECTED_PRIVILEGED_SET_ROLE_PATHS: %', v_unexpected_paths;
    END IF;
END
$membership_postcheck$;

CREATE EXTENSION IF NOT EXISTS btree_gist;

DO $schema$
DECLARE
    v_owner text;
BEGIN
    SELECT pg_get_userbyid(nspowner)
      INTO v_owner
      FROM pg_namespace
     WHERE nspname = 'propertyai';

    IF NOT FOUND THEN
        EXECUTE 'CREATE SCHEMA propertyai AUTHORIZATION propertyai_owner';
    ELSIF v_owner <> 'propertyai_owner' THEN
        RAISE EXCEPTION 'PROPERTYAI_SCHEMA_OWNER_DRIFT: %', v_owner;
    END IF;
END
$schema$;

REVOKE ALL ON SCHEMA propertyai FROM PUBLIC;
GRANT USAGE ON SCHEMA propertyai TO propertyai_app_runtime, propertyai_async_worker, propertyai_readonly, propertyai_migrator, propertyai_flyway;
