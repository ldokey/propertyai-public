-- Flyway 13 uses dedicated callback/event connections. The JDBC startup option
-- activates owner on every physical connection; this callback independently
-- reasserts and verifies the required session/current-user contract without
-- relying on deprecated initSql behavior.
DO $$
BEGIN
    IF session_user <> 'propertyai_flyway' THEN
        RAISE EXCEPTION
            'Flyway must authenticate as propertyai_flyway (session_user=%)',
            session_user;
    END IF;
END
$$;

SET ROLE propertyai_owner;

DO $$
BEGIN
    IF session_user <> 'propertyai_flyway'
       OR current_user <> 'propertyai_owner' THEN
        RAISE EXCEPTION
            'Flyway role activation failed (session_user=%, current_user=%)',
            session_user, current_user;
    END IF;
END
$$;
