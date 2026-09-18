-- G1 REVIEW ARTIFACT ONLY. NOT INSTALLED. Privileged bootstrap, before 109.
-- Existing bootstrap 001 and owner/migrator/flyway graph are immutable.
DO $roles$
DECLARE n text; r record;
BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname=session_user AND rolsuper) THEN
   RAISE EXCEPTION 'RENT_BOOTSTRAP_REQUIRES_DBA';
 END IF;
 FOREACH n IN ARRAY ARRAY['propertyai_rent_runtime','propertyai_rent_reader','propertyai_rent_scheduler'] LOOP
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname=n) THEN
     EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',n);
   END IF;
   SELECT * INTO STRICT r FROM pg_roles WHERE rolname=n;
   IF r.rolcanlogin OR r.rolinherit OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls THEN
     RAISE EXCEPTION 'RENT_ROLE_ATTRIBUTE_DRIFT: %',n;
   END IF;
   -- No role grants are made here. Unexpected existing inheritance/SET ROLE edges fail closed.
   IF EXISTS(SELECT FROM pg_auth_members WHERE member=r.oid OR roleid=r.oid) THEN
     RAISE EXCEPTION 'RENT_ROLE_MEMBERSHIP_REQUIRES_SEPARATE_BINDING: %',n;
   END IF;
 END LOOP;
END $roles$;
-- No LOGIN, passwords, Production authority, fixture identities, or membership grants.
