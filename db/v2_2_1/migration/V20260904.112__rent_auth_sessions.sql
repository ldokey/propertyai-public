-- W1/I1 additive opaque-session persistence only. No bearer plaintext is stored.
SET ROLE propertyai_owner;

CREATE TABLE propertyai.rent_auth_session (
    session_id text PRIMARY KEY,
    token_digest text NOT NULL UNIQUE,
    organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
    actor_party_id uuid NOT NULL REFERENCES propertyai.party(party_id),
    subject text NOT NULL,
    capabilities text[] NOT NULL,
    csrf_token text NOT NULL,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CONSTRAINT ck_rent_auth_session_id CHECK (session_id ~ '^[A-Za-z0-9_-]{32}$'),
    CONSTRAINT ck_rent_auth_token_digest CHECK (token_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_rent_auth_subject CHECK (length(subject) BETWEEN 1 AND 512 AND subject=btrim(subject)),
    CONSTRAINT ck_rent_auth_capabilities CHECK (cardinality(capabilities) <= 32 AND array_position(capabilities,NULL) IS NULL),
    CONSTRAINT ck_rent_auth_csrf CHECK (csrf_token ~ '^[A-Za-z0-9_-]{43}$'),
    CONSTRAINT ck_rent_auth_lifetime CHECK (expires_at > issued_at),
    CONSTRAINT ck_rent_auth_revocation CHECK (revoked_at IS NULL OR revoked_at >= issued_at)
);

REVOKE ALL ON propertyai.rent_auth_session FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,
 propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
GRANT USAGE ON SCHEMA propertyai TO propertyai_app_runtime;

CREATE FUNCTION propertyai.rent_auth_session_create(
    p_session_id text,
    p_token_digest text,
    p_organization_id uuid,
    p_actor_party_id uuid,
    p_subject text,
    p_capabilities text[],
    p_csrf_token text,
    p_issued_at timestamptz,
    p_expires_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE inserted_count integer;
BEGIN
    INSERT INTO propertyai.rent_auth_session(
        session_id,token_digest,organization_id,actor_party_id,subject,capabilities,
        csrf_token,issued_at,expires_at
    ) VALUES (
        p_session_id,p_token_digest,p_organization_id,p_actor_party_id,p_subject,p_capabilities,
        p_csrf_token,p_issued_at,p_expires_at
    ) ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS inserted_count = ROW_COUNT;
    RETURN inserted_count = 1;
END $$;

CREATE FUNCTION propertyai.rent_auth_session_get(p_token_digest text)
RETURNS TABLE(
    session_id text,
    token_digest text,
    organization_id uuid,
    actor_party_id uuid,
    subject text,
    capabilities text[],
    csrf_token text,
    issued_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
    SELECT s.session_id,s.token_digest,s.organization_id,s.actor_party_id,s.subject,s.capabilities,
           s.csrf_token,s.issued_at,s.expires_at,s.revoked_at
      FROM propertyai.rent_auth_session s
     WHERE s.token_digest=p_token_digest
$$;

CREATE FUNCTION propertyai.rent_auth_session_revoke(p_session_id text,p_revoked_at timestamptz)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE updated_count integer;
BEGIN
    UPDATE propertyai.rent_auth_session
       SET revoked_at=p_revoked_at
     WHERE session_id=p_session_id AND revoked_at IS NULL;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END $$;

REVOKE ALL ON FUNCTION propertyai.rent_auth_session_create(text,text,uuid,uuid,text,text[],text,timestamptz,timestamptz)
 FROM PUBLIC,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_auth_session_get(text)
 FROM PUBLIC,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_auth_session_revoke(text,timestamptz)
 FROM PUBLIC,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;

GRANT EXECUTE ON FUNCTION propertyai.rent_auth_session_create(text,text,uuid,uuid,text,text[],text,timestamptz,timestamptz)
 TO propertyai_app_runtime;
GRANT EXECUTE ON FUNCTION propertyai.rent_auth_session_get(text) TO propertyai_app_runtime;
GRANT EXECUTE ON FUNCTION propertyai.rent_auth_session_revoke(text,timestamptz) TO propertyai_app_runtime;
