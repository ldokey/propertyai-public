-- G1 REVIEW ARTIFACT ONLY. Proposed 111. Concrete guards/ACL/read contracts, not financial workflow.
SET ROLE propertyai_owner;
CREATE FUNCTION propertyai.rent_assert_test_party(p_party uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE p propertyai.party;
BEGIN
 IF p_party IS NULL THEN RETURN; END IF;
 -- SHARE, not KEY SHARE: environment/active are non-key attributes.
 SELECT * INTO p FROM propertyai.party WHERE party_id=p_party FOR SHARE;
 IF NOT FOUND OR p.active IS DISTINCT FROM true OR p.data_environment IS DISTINCT FROM 'TEST' THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='RENT_PARTY_REFERENCE_INVALID';
 END IF;
END $$;
CREATE FUNCTION propertyai.rent_guard_binding_party() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
BEGIN
 -- Independently approved TEST fixture provisioning only; no runtime grant is added.
 PERFORM propertyai.rent_assert_test_party(NEW.actor_party_id);
 RETURN NEW;
END $$;
CREATE TRIGGER tg_rent_binding_party BEFORE INSERT OR UPDATE ON propertyai.rent_runtime_binding
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_binding_party();
CREATE FUNCTION propertyai.rent_context(p_org uuid,p_write boolean DEFAULT false)
RETURNS propertyai.rent_runtime_binding
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE b propertyai.rent_runtime_binding; o propertyai.organization; m propertyai.organization_member;
BEGIN
 SELECT * INTO b FROM propertyai.rent_runtime_binding WHERE login_name=session_user AND enabled;
 IF NOT FOUND OR b.organization_id IS DISTINCT FROM p_org THEN
  RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='RENT_NOT_AUTHORIZED';
 END IF;
 SELECT * INTO STRICT o FROM propertyai.organization WHERE organization_id=p_org;
 IF o.data_environment<>'TEST' OR o.organization_status<>'ACTIVE' OR b.data_environment<>'TEST' THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='RENT_TEST_CONTEXT_REQUIRED';
 END IF;
 IF b.principal_type='PARTY' THEN
  IF p_write THEN
   PERFORM propertyai.rent_assert_test_party(b.actor_party_id);
   SELECT * INTO m FROM propertyai.organization_member WHERE organization_id=p_org AND party_id=b.actor_party_id FOR SHARE;
  ELSE
   SELECT * INTO m FROM propertyai.organization_member WHERE organization_id=p_org AND party_id=b.actor_party_id;
  END IF;
  IF NOT FOUND OR m.membership_status<>'ACTIVE' OR (p_write AND m.membership_role NOT IN ('OWNER','ADMIN','OPERATOR'))
    OR NOT EXISTS(SELECT FROM propertyai.party WHERE party_id=b.actor_party_id AND active AND data_environment='TEST') THEN
   RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='RENT_MEMBERSHIP_REVOKED';
  END IF;
 END IF;
 IF p_write AND b.capability NOT IN ('WRITE','SCHEDULER') THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='RENT_WRITE_FORBIDDEN';
 END IF;
 RETURN b;
END $$;
CREATE FUNCTION propertyai.rent_visible_org() RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE o uuid;
BEGIN
 SELECT organization_id INTO o FROM propertyai.rent_runtime_binding WHERE login_name=session_user AND enabled;
 IF o IS NULL THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='RENT_NOT_AUTHORIZED'; END IF;
 PERFORM propertyai.rent_context(o,false); RETURN o;
END $$;
CREATE FUNCTION propertyai.rent_lock_scope(p_org uuid) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE v bigint;
BEGIN
 PERFORM propertyai.rent_context(p_org,true);
 IF current_setting('transaction_isolation')<>'serializable' THEN
  RAISE EXCEPTION USING ERRCODE='25001',MESSAGE='RENT_SERIALIZABLE_REQUIRED';
 END IF;
 SELECT revision INTO v FROM propertyai.finance_ledger_scope WHERE organization_id=p_org FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'RENT_SCOPE_NOT_PROVISIONED'; END IF;
 RETURN v;
END $$;
CREATE FUNCTION propertyai.rent_guard_write() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE b propertyai.rent_runtime_binding; allowed text[]; jnew jsonb; jold jsonb;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'RENT_DELETE_FORBIDDEN'; END IF;
 b:=propertyai.rent_context(NEW.organization_id,true);
 PERFORM propertyai.rent_lock_scope(NEW.organization_id);
 IF EXISTS(SELECT FROM propertyai.finance_command WHERE transaction_id=pg_current_xact_id()) THEN
  RAISE EXCEPTION 'RENT_COMMAND_ALREADY_SEALED';
 END IF;
 IF b.capability='SCHEDULER' AND TG_TABLE_NAME NOT IN ('finance_receivable','finance_receivable_line') THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='RENT_SCHEDULER_TABLE_FORBIDDEN';
 END IF;
 IF TG_OP='INSERT' THEN
  NEW.created_at:=transaction_timestamp();
  jnew:=to_jsonb(NEW);
  IF jnew ? 'version' AND ((jnew->>'version')::bigint<>1
    OR (jnew->>'last_command_id') IS DISTINCT FROM (jnew->>'created_command_id')) THEN
   RAISE EXCEPTION 'RENT_INITIAL_VERSION_OR_COMMAND_INVALID';
  END IF;
  IF TG_TABLE_NAME='finance_receivable' AND b.capability='SCHEDULER' AND jnew->>'replacement_of' IS NOT NULL THEN
   RAISE EXCEPTION 'RENT_SCHEDULER_REPLACEMENT_FORBIDDEN';
  END IF;
  IF TG_TABLE_NAME='finance_movement_revision' THEN
   IF NOT EXISTS(SELECT FROM propertyai.finance_account a WHERE a.organization_id=NEW.organization_id
    AND a.account_id=(jnew->>'account_id')::uuid AND (a.active OR
      EXISTS(SELECT FROM propertyai.finance_movement_revision v WHERE v.organization_id=NEW.organization_id
       AND v.movement_id=(jnew->>'movement_id')::uuid AND v.revision_no=(jnew->>'previous_revision_no')::integer AND v.account_id=a.account_id))) THEN
    RAISE EXCEPTION 'RENT_ACCOUNT_INACTIVE_OR_UNKNOWN';
   END IF;
  END IF;
 ELSE
  IF NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN RAISE EXCEPTION 'RENT_ORG_IMMUTABLE'; END IF;
  jnew:=to_jsonb(NEW); jold:=to_jsonb(OLD);
  CASE TG_TABLE_NAME
  WHEN 'rent_resident' THEN allowed:=ARRAY['display_name','linked_party_id','private_contact_ref','review_status','version','last_command_id'];
  WHEN 'rent_contract' THEN allowed:=ARRAY['starts_on','ends_on_exclusive','lifecycle','readiness','current_term_id','change_reason','version','last_command_id'];
  WHEN 'rent_term_revision' THEN allowed:=ARRAY['superseded_by','supersession_reason','version','last_command_id'];
  WHEN 'rent_occupancy' THEN allowed:=ARRAY['actual_start','actual_end_exclusive','review_status','version','last_command_id'];
  WHEN 'finance_account' THEN allowed:=ARRAY['display_name','masked_identifier','protected_identifier_ref','active','version','last_command_id'];
  WHEN 'finance_receivable' THEN allowed:=ARRAY['voided','void_reason','version','last_command_id'];
  WHEN 'finance_movement' THEN allowed:=ARRAY['current_revision','version','last_command_id'];
  WHEN 'finance_funding_source' THEN allowed:=ARRAY['attribution_status','attributed_property_id','attributed_contract_id','version','last_command_id'];
  ELSE RAISE EXCEPTION 'RENT_APPEND_ONLY: %',TG_TABLE_NAME;
  END CASE;
  IF (jnew-allowed) IS DISTINCT FROM (jold-allowed) OR (jnew->>'version')::bigint<>(jold->>'version')::bigint+1 THEN
   RAISE EXCEPTION 'RENT_IMMUTABLE_OR_VERSION_VIOLATION: %',TG_TABLE_NAME;
  END IF;
  IF TG_TABLE_NAME='rent_term_revision' AND (jold->>'superseded_by' IS NOT NULL OR jnew->>'superseded_by' IS NULL) THEN
   RAISE EXCEPTION 'RENT_TERM_SUPERSESSION_ONCE';
  END IF;
  IF TG_TABLE_NAME='finance_receivable' AND (jold->>'voided')::boolean AND NOT (jnew->>'voided')::boolean THEN
   RAISE EXCEPTION 'RENT_VOID_IRREVERSIBLE';
  END IF;
 END IF;
 -- Guard supplied non-null party references before any business row is accepted.
 CASE TG_TABLE_NAME
 WHEN 'rent_resident' THEN PERFORM propertyai.rent_assert_test_party((jnew->>'linked_party_id')::uuid);
 WHEN 'rent_contract_party' THEN PERFORM propertyai.rent_assert_test_party((jnew->>'party_id')::uuid);
 WHEN 'finance_account' THEN PERFORM propertyai.rent_assert_test_party((jnew->>'owner_party_id')::uuid);
 WHEN 'finance_movement_revision' THEN PERFORM propertyai.rent_assert_test_party((jnew->>'counterparty_party_id')::uuid);
 ELSE NULL;
 END CASE;
 UPDATE propertyai.finance_ledger_scope SET revision=revision+1 WHERE organization_id=NEW.organization_id;
 RETURN NEW;
END $$;
CREATE FUNCTION propertyai.rent_record_contract_history() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
BEGIN
 INSERT INTO propertyai.rent_contract_history(organization_id,contract_id,version,command_id,snapshot)
 VALUES(NEW.organization_id,NEW.contract_id,NEW.version,NEW.last_command_id,
  jsonb_build_object('starts_on',NEW.starts_on,'ends_on_exclusive',NEW.ends_on_exclusive,
   'lifecycle',NEW.lifecycle,'readiness',NEW.readiness,'current_term_id',NEW.current_term_id,'change_reason',NEW.change_reason));
 RETURN NULL;
END $$;
CREATE FUNCTION propertyai.rent_bump_related_versions() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
BEGIN
 IF TG_TABLE_NAME IN ('finance_allocation','finance_receivable_adjustment') THEN
  UPDATE propertyai.finance_receivable SET version=version+1,last_command_id=NEW.created_command_id
   WHERE organization_id=NEW.organization_id AND receivable_id=NEW.receivable_id;
 END IF;
 IF TG_TABLE_NAME IN ('finance_allocation','finance_source_return') THEN
  UPDATE propertyai.finance_funding_source SET version=version+1,last_command_id=NEW.created_command_id
   WHERE organization_id=NEW.organization_id AND source_id=NEW.source_id;
 ELSIF TG_TABLE_NAME='finance_movement_revision' THEN
  UPDATE propertyai.finance_funding_source SET version=version+1,last_command_id=NEW.created_command_id
   WHERE organization_id=NEW.organization_id AND movement_id=NEW.movement_id;
 END IF;
 RETURN NULL;
END $$;
-- These views preaggregate independent 1:N relations, preventing line/adjustment/allocation fanout.
CREATE VIEW propertyai.v_rent_receivable_amounts WITH (security_barrier=true) AS
WITH a AS (SELECT organization_id,receivable_id,sum(delta::numeric) delta FROM propertyai.finance_receivable_adjustment GROUP BY 1,2),
b AS (SELECT organization_id,receivable_id,sum(CASE record_kind WHEN 'APPLY' THEN amount::numeric ELSE -amount::numeric END) allocated
 FROM propertyai.finance_allocation GROUP BY 1,2)
SELECT r.organization_id,r.receivable_id,r.contract_id,r.property_id,r.period_id,r.due_on,r.voided,r.version,
 CASE WHEN r.voided THEN 0::numeric ELSE r.original_amount::numeric+coalesce(a.delta,0) END effective_amount,
 coalesce(b.allocated,0) allocated,
 CASE WHEN r.voided THEN 0::numeric ELSE r.original_amount::numeric+coalesce(a.delta,0) END-coalesce(b.allocated,0) balance
FROM propertyai.finance_receivable r LEFT JOIN a USING(organization_id,receivable_id) LEFT JOIN b USING(organization_id,receivable_id);
CREATE VIEW propertyai.v_rent_source_amounts WITH (security_barrier=true) AS
WITH a AS(SELECT organization_id,source_id,sum(CASE record_kind WHEN 'APPLY' THEN amount::numeric ELSE -amount::numeric END) allocated
 FROM propertyai.finance_allocation GROUP BY 1,2),
b AS(SELECT organization_id,source_id,sum(CASE record_kind WHEN 'APPLY' THEN amount::numeric ELSE -amount::numeric END) returned
 FROM propertyai.finance_source_return GROUP BY 1,2)
SELECT s.organization_id,s.source_id,s.movement_id,s.version,s.attribution_status,
 CASE WHEN r.record_status='REVERSED' THEN 0::numeric ELSE r.amount::numeric END principal,
 coalesce(a.allocated,0) allocated,coalesce(b.returned,0) returned,
 CASE WHEN r.record_status='REVERSED' THEN 0::numeric ELSE r.amount::numeric END-coalesce(a.allocated,0)-coalesce(b.returned,0) available
FROM propertyai.finance_funding_source s JOIN propertyai.finance_movement m USING(organization_id,movement_id)
JOIN propertyai.finance_movement_revision r ON(r.organization_id,r.movement_id,r.revision_no)=(m.organization_id,m.movement_id,m.current_revision)
LEFT JOIN a ON(a.organization_id,a.source_id)=(s.organization_id,s.source_id) LEFT JOIN b ON(b.organization_id,b.source_id)=(s.organization_id,s.source_id);
CREATE FUNCTION propertyai.rent_check_ledger(p_org uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE v_party_id uuid;
BEGIN
 IF EXISTS(SELECT FROM propertyai.finance_movement n JOIN propertyai.finance_movement o
  ON(o.organization_id,o.movement_id)=(n.organization_id,n.replaces_movement_id)
  JOIN propertyai.finance_movement_revision v ON(v.organization_id,v.movement_id,v.revision_no)=(o.organization_id,o.movement_id,o.current_revision)
  WHERE n.organization_id=p_org AND (n.direction<>'IN' OR o.direction<>'IN' OR v.record_status<>'REVERSED')) THEN
  RAISE EXCEPTION 'RENT_REPLACEMENT_REQUIRES_REVERSED_IN'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_receivable a JOIN propertyai.finance_receivable p
  ON(p.organization_id,p.receivable_id)=(a.organization_id,a.replacement_of)
  WHERE a.organization_id=p_org AND (NOT p.voided OR a.contract_id<>p.contract_id)) THEN
  RAISE EXCEPTION 'RENT_REPLACEMENT_REQUIRES_VOIDED_PREDECESSOR'; END IF;
 IF EXISTS(SELECT FROM propertyai.rent_contract c LEFT JOIN propertyai.rent_term_revision t
  ON(t.organization_id,t.term_id)=(c.organization_id,c.current_term_id)
  WHERE c.organization_id=p_org AND c.readiness='READY' AND
  (t.term_id IS NULL OR NOT t.amount_confirmed OR NOT t.cycle_confirmed OR t.superseded_by IS NOT NULL)) THEN
  RAISE EXCEPTION 'RENT_READY_TERMS_INCOMPLETE'; END IF;
 -- Final-state recheck includes every new Finance global-party FK, not just API input.
 -- All non-null references are checked/locked in UUID order through transaction end.
 FOR v_party_id IN
  SELECT DISTINCT q.party_id FROM (
   SELECT linked_party_id AS party_id FROM propertyai.rent_resident WHERE organization_id=p_org
   UNION ALL SELECT party_id FROM propertyai.rent_contract_party WHERE organization_id=p_org
   UNION ALL SELECT owner_party_id FROM propertyai.finance_account WHERE organization_id=p_org
   UNION ALL SELECT counterparty_party_id FROM propertyai.finance_movement_revision WHERE organization_id=p_org
   UNION ALL SELECT actor_party_id FROM propertyai.rent_runtime_binding WHERE organization_id=p_org
  ) q WHERE q.party_id IS NOT NULL ORDER BY q.party_id
 LOOP
  PERFORM propertyai.rent_assert_test_party(v_party_id);
 END LOOP;
 IF EXISTS(SELECT FROM propertyai.finance_receivable r JOIN propertyai.rent_contract c USING(organization_id,contract_id)
  JOIN propertyai.rent_billing_period p USING(organization_id,contract_id,period_id)
  WHERE r.organization_id=p_org AND (r.property_id<>c.property_id OR r.due_on<>p.due_on)) THEN
  RAISE EXCEPTION 'RENT_RECEIVABLE_BINDING_MISMATCH'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_receivable r LEFT JOIN
   (SELECT organization_id,receivable_id,sum(original_amount::numeric) amount,count(*) n
    FROM propertyai.finance_receivable_line GROUP BY 1,2) l USING(organization_id,receivable_id)
  WHERE r.organization_id=p_org AND (l.n IS NULL OR l.amount<>r.original_amount::numeric)) THEN
  RAISE EXCEPTION 'RENT_LINE_TOTAL_MISMATCH'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_receivable_line l JOIN propertyai.finance_receivable r USING(organization_id,receivable_id)
  JOIN propertyai.rent_term_revision t USING(organization_id,term_id)
  JOIN propertyai.rent_billing_period p
    ON p.organization_id=r.organization_id AND p.contract_id=r.contract_id AND p.period_id=r.period_id
  WHERE l.organization_id=p_org AND (r.contract_id<>t.contract_id OR NOT t.amount_confirmed OR l.monthly_rent IS DISTINCT FROM t.monthly_rent
  OR l.service_from<p.cycle_start OR l.service_to_exclusive>p.cycle_end_exclusive
  OR l.service_from<t.valid_from OR (t.valid_to_exclusive IS NOT NULL AND l.service_to_exclusive>t.valid_to_exclusive))) THEN
  RAISE EXCEPTION 'RENT_LINE_TERM_MISMATCH'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_receivable_adjustment a JOIN propertyai.finance_receivable_adjustment b
  ON (b.organization_id,b.adjustment_id)=(a.organization_id,a.reverse_of)
  WHERE a.organization_id=p_org AND (b.reverse_of IS NOT NULL OR a.delta::numeric<>-b.delta::numeric)) THEN
  RAISE EXCEPTION 'RENT_ADJUSTMENT_REVERSAL_MISMATCH'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_allocation a JOIN propertyai.finance_allocation b
  ON(b.organization_id,b.allocation_id)=(a.organization_id,a.reverse_of)
  WHERE a.organization_id=p_org AND b.record_kind<>'APPLY')
 OR EXISTS(SELECT FROM propertyai.finance_source_return a JOIN propertyai.finance_source_return b
  ON(b.organization_id,b.return_id)=(a.organization_id,a.reverse_of)
  WHERE a.organization_id=p_org AND b.record_kind<>'APPLY') THEN
  RAISE EXCEPTION 'RENT_REVERSAL_TARGET_INVALID'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_movement m WHERE m.organization_id=p_org AND
  m.current_revision IS DISTINCT FROM (SELECT max(r.revision_no) FROM propertyai.finance_movement_revision r
    WHERE(r.organization_id,r.movement_id)=(m.organization_id,m.movement_id))) THEN
  RAISE EXCEPTION 'RENT_MOVEMENT_CURRENT_REVISION_MISMATCH'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_movement_revision r JOIN propertyai.finance_movement_revision p
  ON(p.organization_id,p.movement_id,p.revision_no)=(r.organization_id,r.movement_id,r.previous_revision_no)
  WHERE r.organization_id=p_org AND (p.record_status='REVERSED' OR
   (r.record_status='REVERSED' AND ROW(r.amount,r.account_id,r.occurred_on,r.payer_raw,r.counterparty_party_id)
    IS DISTINCT FROM ROW(p.amount,p.account_id,p.occurred_on,p.payer_raw,p.counterparty_party_id)))) THEN
  RAISE EXCEPTION 'RENT_MOVEMENT_REVERSAL_MISMATCH'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_funding_source s JOIN propertyai.finance_movement m USING(organization_id,movement_id)
  LEFT JOIN propertyai.rent_contract c ON(c.organization_id,c.contract_id)=(s.organization_id,s.attributed_contract_id)
  WHERE s.organization_id=p_org AND (m.direction<>'IN' OR (s.attributed_contract_id IS NOT NULL AND c.property_id<>s.attributed_property_id))) THEN
  RAISE EXCEPTION 'RENT_FUNDING_SOURCE_BINDING_MISMATCH'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_movement m WHERE m.organization_id=p_org AND m.direction='IN'
  AND NOT EXISTS(SELECT FROM propertyai.finance_funding_source s WHERE(s.organization_id,s.movement_id)=(m.organization_id,m.movement_id))) THEN
  RAISE EXCEPTION 'RENT_RECEIPT_REQUIRES_FUNDING_SOURCE'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_allocation a JOIN propertyai.finance_funding_source s USING(organization_id,source_id)
  JOIN propertyai.finance_receivable r USING(organization_id,receivable_id)
  WHERE a.organization_id=p_org AND a.record_kind='APPLY'
   AND NOT EXISTS(SELECT FROM propertyai.finance_allocation z WHERE(z.organization_id,z.reverse_of)=(a.organization_id,a.allocation_id))
   AND ((s.attribution_status='UNMATCHED' OR s.attributed_property_id IS DISTINCT FROM r.property_id
    OR (s.attributed_contract_id IS NOT NULL AND s.attributed_contract_id<>r.contract_id))
    AND (a.override_attribution_reason IS NULL OR btrim(a.override_attribution_reason)=''))) THEN
  RAISE EXCEPTION 'RENT_ATTRIBUTION_CONFIRMATION_REQUIRED'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_receivable r LEFT JOIN
  (SELECT organization_id,receivable_id,sum(delta::numeric) delta FROM propertyai.finance_receivable_adjustment GROUP BY 1,2) a USING(organization_id,receivable_id)
  WHERE r.organization_id=p_org AND r.original_amount::numeric+coalesce(a.delta,0)<0)
 OR EXISTS(SELECT FROM propertyai.v_rent_receivable_amounts WHERE organization_id=p_org AND (allocated<0 OR balance<0 OR effective_amount>9223372036854775807::numeric))
 OR EXISTS(SELECT FROM propertyai.v_rent_source_amounts WHERE organization_id=p_org AND (allocated<0 OR returned<0 OR available<0)) THEN
  RAISE EXCEPTION 'RENT_BALANCE_INVARIANT'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_source_return r JOIN propertyai.finance_movement m
  ON(m.organization_id,m.movement_id)=(r.organization_id,r.outgoing_movement_id)
  WHERE r.organization_id=p_org AND m.direction<>'OUT') THEN RAISE EXCEPTION 'RENT_RETURN_REQUIRES_OUT'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_movement m JOIN propertyai.finance_movement_revision v
  ON(v.organization_id,v.movement_id,v.revision_no)=(m.organization_id,m.movement_id,m.current_revision)
  LEFT JOIN(SELECT organization_id,outgoing_movement_id,sum(CASE record_kind WHEN 'APPLY' THEN amount::numeric ELSE -amount::numeric END) amount
   FROM propertyai.finance_source_return GROUP BY 1,2) r ON(r.organization_id,r.outgoing_movement_id)=(m.organization_id,m.movement_id)
  WHERE m.organization_id=p_org AND m.direction='OUT'
   AND coalesce(r.amount,0)<>CASE WHEN v.record_status='REVERSED' THEN 0::numeric ELSE v.amount::numeric END) THEN
  RAISE EXCEPTION 'RENT_RETURN_OUT_TOTAL_MISMATCH'; END IF;
END $$;
CREATE FUNCTION propertyai.rent_deferred_check() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE c uuid; j jsonb;
BEGIN
 j:=to_jsonb(NEW); c:=coalesce((j->>'last_command_id')::uuid,(j->>'created_command_id')::uuid);
 IF NOT EXISTS(SELECT FROM propertyai.finance_command x WHERE x.organization_id=NEW.organization_id
  AND x.command_id=c AND x.transaction_id=pg_current_xact_id()) THEN
  RAISE EXCEPTION 'RENT_COMMAND_NOT_SEALED_IN_SAME_TRANSACTION'; END IF;
 PERFORM propertyai.rent_check_ledger(NEW.organization_id);
 RETURN NULL;
END $$;
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.rent_resident
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.rent_resident
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.rent_contract
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.rent_contract
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.rent_contract_party
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.rent_contract_party
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.rent_contract_resident
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.rent_contract_resident
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.rent_term_revision
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.rent_term_revision
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.rent_billing_period
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.rent_billing_period
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.rent_occupancy
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.rent_occupancy
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_account
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_account
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_receivable
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_receivable
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_receivable_line
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_receivable_line
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_receivable_adjustment
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_receivable_adjustment
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_movement
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_movement
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_movement_revision
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_movement_revision
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_funding_source
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_funding_source
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_allocation
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_allocation
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_guard BEFORE INSERT OR UPDATE OR DELETE ON propertyai.finance_source_return
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_guard_write();
CREATE CONSTRAINT TRIGGER tg_rent_final AFTER INSERT OR UPDATE ON propertyai.finance_source_return
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION propertyai.rent_deferred_check();
CREATE TRIGGER tg_rent_contract_history AFTER INSERT OR UPDATE ON propertyai.rent_contract
 FOR EACH ROW EXECUTE FUNCTION propertyai.rent_record_contract_history();
CREATE TRIGGER tg_rent_versions AFTER INSERT ON propertyai.finance_allocation FOR EACH ROW EXECUTE FUNCTION propertyai.rent_bump_related_versions();
CREATE TRIGGER tg_rent_versions AFTER INSERT ON propertyai.finance_receivable_adjustment FOR EACH ROW EXECUTE FUNCTION propertyai.rent_bump_related_versions();
CREATE TRIGGER tg_rent_versions AFTER INSERT ON propertyai.finance_source_return FOR EACH ROW EXECUTE FUNCTION propertyai.rent_bump_related_versions();
CREATE TRIGGER tg_rent_versions AFTER INSERT ON propertyai.finance_movement_revision FOR EACH ROW EXECUTE FUNCTION propertyai.rent_bump_related_versions();

CREATE FUNCTION propertyai.rent_complete_command(p_org uuid,p_id uuid,p_type text,p_key uuid,p_hash text,p_result jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE b propertyai.rent_runtime_binding; v bigint; channel text; result jsonb;
BEGIN
 b:=propertyai.rent_context(p_org,true); v:=propertyai.rent_lock_scope(p_org);
 IF p_id IS NULL OR p_type IS NULL OR p_key IS NULL OR p_hash IS NULL OR p_hash!~'^[0-9a-f]{64}$'
   OR p_result IS NULL OR jsonb_typeof(p_result)<>'object' THEN RAISE EXCEPTION 'RENT_COMMAND_ARGUMENT_INVALID'; END IF;
 IF b.capability='SCHEDULER' AND p_type<>'ISSUE_RENT' THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='RENT_SCHEDULER_COMMAND_FORBIDDEN'; END IF;
 IF EXISTS(SELECT FROM propertyai.finance_command WHERE organization_id=p_org AND command_type=p_type AND idempotency_key=p_key) THEN
  RAISE EXCEPTION 'RENT_REPLAY_MUST_PRECEDE_WRITES'; END IF;
 channel:=CASE WHEN b.capability='SCHEDULER' THEN 'RENT_SCHEDULER_TEST' ELSE 'RENT_API' END;
 result:=jsonb_build_object('schema_version',1,'command_id',p_id,'result',p_result,
  'ledger_revision',v::text,'recorded_at',transaction_timestamp());
 -- The API MUST wait for COMMIT acknowledgement before returning this payload. recorded_at is NOT a commit timestamp.
 INSERT INTO propertyai.command_receipt(command_id,authority_scope_code,command_type,idempotency_key,
  request_payload,source_channel_code,principal_type,actor_party_id,authority_epoch,decided_at,result_type,result_id,result_payload)
 VALUES(p_id,'LONGSTAY_RENT:'||p_org::text,p_type,p_key::text,
  jsonb_build_object('schema_version',1,'request_sha256',p_hash),channel,b.principal_type,b.actor_party_id,0,
  transaction_timestamp(),'RENT_COMMAND',p_id,result);
 INSERT INTO propertyai.finance_command(command_id,organization_id,command_type,idempotency_key,request_sha256,transaction_id,ledger_revision_after)
 VALUES(p_id,p_org,p_type,p_key,p_hash,pg_current_xact_id(),v);
 INSERT INTO propertyai.domain_event(aggregate_type,aggregate_id,event_type,command_id,actor_party_id,payload,occurred_at)
 VALUES('RENT_COMMAND',p_id,'RENT_'||p_type,p_id,b.actor_party_id,
  jsonb_build_object('schema_version',1,'organization_id',p_org,'ledger_revision',v::text),transaction_timestamp());
 RETURN result;
END $$;
CREATE FUNCTION propertyai.rent_command_result(p_org uuid,p_type text,p_key uuid,p_hash text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE h text; r jsonb; b propertyai.rent_runtime_binding; actor uuid; principal text;
BEGIN
 b:=propertyai.rent_context(p_org,false);
 SELECT c.request_sha256,s.result_payload,s.actor_party_id,s.principal_type INTO h,r,actor,principal FROM propertyai.finance_command c JOIN propertyai.command_receipt s USING(command_id)
 WHERE c.organization_id=p_org AND c.command_type=p_type AND c.idempotency_key=p_key
  AND s.authority_scope_code='LONGSTAY_RENT:'||p_org::text;
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF principal IS DISTINCT FROM b.principal_type OR actor IS DISTINCT FROM b.actor_party_id THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='RENT_RESULT_ACTOR_MISMATCH'; END IF;
 IF p_hash IS NOT NULL AND h<>p_hash THEN RAISE EXCEPTION 'RENT_IDEMPOTENCY_CONFLICT'; END IF;
 RETURN r;
END $$;
-- Privileges: only the finance service role sees raw finance input; reader gets masked views only.
REVOKE ALL ON propertyai.rent_resident FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.rent_contract FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.rent_contract_party FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.rent_contract_resident FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.rent_term_revision FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.rent_billing_period FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.rent_occupancy FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_account FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_receivable FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_receivable_line FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_receivable_adjustment FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_movement FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_movement_revision FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_funding_source FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_allocation FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_source_return FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_command FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.finance_ledger_scope FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.rent_runtime_binding FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON propertyai.rent_contract_history FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
ALTER TABLE propertyai.rent_resident ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.rent_resident TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.rent_resident TO propertyai_rent_runtime;
GRANT UPDATE(display_name,linked_party_id,private_contact_ref,review_status,version,last_command_id) ON propertyai.rent_resident TO propertyai_rent_runtime;
ALTER TABLE propertyai.rent_contract ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.rent_contract TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.rent_contract TO propertyai_rent_runtime;
GRANT UPDATE(starts_on,ends_on_exclusive,lifecycle,readiness,current_term_id,change_reason,version,last_command_id) ON propertyai.rent_contract TO propertyai_rent_runtime;
ALTER TABLE propertyai.rent_contract_party ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.rent_contract_party TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.rent_contract_party TO propertyai_rent_runtime;
ALTER TABLE propertyai.rent_contract_resident ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.rent_contract_resident TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.rent_contract_resident TO propertyai_rent_runtime;
ALTER TABLE propertyai.rent_term_revision ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.rent_term_revision TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.rent_term_revision TO propertyai_rent_runtime;
GRANT UPDATE(superseded_by,supersession_reason,version,last_command_id) ON propertyai.rent_term_revision TO propertyai_rent_runtime;
ALTER TABLE propertyai.rent_billing_period ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.rent_billing_period TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.rent_billing_period TO propertyai_rent_runtime;
ALTER TABLE propertyai.rent_occupancy ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.rent_occupancy TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.rent_occupancy TO propertyai_rent_runtime;
GRANT UPDATE(actual_start,actual_end_exclusive,review_status,version,last_command_id) ON propertyai.rent_occupancy TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_account ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_account TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_account TO propertyai_rent_runtime;
GRANT UPDATE(display_name,masked_identifier,protected_identifier_ref,active,version,last_command_id) ON propertyai.finance_account TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_receivable ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_receivable TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_receivable TO propertyai_rent_runtime;
GRANT UPDATE(voided,void_reason,version,last_command_id) ON propertyai.finance_receivable TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_receivable_line ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_receivable_line TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_receivable_line TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_receivable_adjustment ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_receivable_adjustment TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_receivable_adjustment TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_movement ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_movement TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_movement TO propertyai_rent_runtime;
GRANT UPDATE(current_revision,version,last_command_id) ON propertyai.finance_movement TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_movement_revision ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_movement_revision TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_movement_revision TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_funding_source ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_funding_source TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_funding_source TO propertyai_rent_runtime;
GRANT UPDATE(attribution_status,attributed_property_id,attributed_contract_id,version,last_command_id) ON propertyai.finance_funding_source TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_allocation ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_allocation TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_allocation TO propertyai_rent_runtime;
ALTER TABLE propertyai.finance_source_return ENABLE ROW LEVEL SECURITY;
CREATE POLICY rent_org ON propertyai.finance_source_return TO propertyai_rent_runtime,propertyai_rent_scheduler
 USING(organization_id=propertyai.rent_visible_org()) WITH CHECK(organization_id=propertyai.rent_visible_org());
GRANT SELECT,INSERT ON propertyai.finance_source_return TO propertyai_rent_runtime;

GRANT USAGE ON SCHEMA propertyai TO propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
GRANT SELECT,INSERT ON propertyai.finance_receivable,propertyai.finance_receivable_line TO propertyai_rent_scheduler;
-- Never grant SELECT on unfiltered owner-executed helper views; explicitly filtered views are the public read surface.
CREATE VIEW propertyai.v_rent_receivables WITH(security_barrier=true) AS
 SELECT * FROM propertyai.v_rent_receivable_amounts WHERE organization_id=propertyai.rent_visible_org();
CREATE VIEW propertyai.v_rent_sources WITH(security_barrier=true) AS
 SELECT * FROM propertyai.v_rent_source_amounts WHERE organization_id=propertyai.rent_visible_org();
CREATE VIEW propertyai.v_rent_accounts WITH(security_barrier=true) AS
 SELECT organization_id,account_id,display_name,currency,masked_identifier,active,version
 FROM propertyai.finance_account WHERE organization_id=propertyai.rent_visible_org();
CREATE VIEW propertyai.v_rent_contracts WITH(security_barrier=true) AS
 SELECT organization_id,contract_id,property_id,rental_unit_id,starts_on,ends_on_exclusive,lifecycle,readiness,current_term_id,version
 FROM propertyai.rent_contract WHERE organization_id=propertyai.rent_visible_org();
CREATE VIEW propertyai.v_rent_reference_units WITH(security_barrier=true) AS
 SELECT p.organization_id,p.property_id,p.display_name AS property_name,p.timezone_name,u.rental_unit_id,u.display_name AS unit_name,
 p.active AS property_active,u.active AS unit_active
 FROM propertyai.property p JOIN propertyai.rental_unit u USING(property_id)
 WHERE p.organization_id=propertyai.rent_visible_org();
GRANT SELECT ON propertyai.v_rent_receivables,propertyai.v_rent_sources,propertyai.v_rent_accounts,
 propertyai.v_rent_contracts,propertyai.v_rent_reference_units
 TO propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_context(uuid,boolean) FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_visible_org() FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_lock_scope(uuid) FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_assert_test_party(uuid) FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_guard_binding_party() FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_guard_write() FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_record_contract_history() FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_bump_related_versions() FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_check_ledger(uuid) FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_deferred_check() FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_complete_command(uuid,uuid,text,uuid,text,jsonb) FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
REVOKE ALL ON FUNCTION propertyai.rent_command_result(uuid,text,uuid,text) FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly,propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
GRANT EXECUTE ON FUNCTION propertyai.rent_visible_org() TO propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
GRANT EXECUTE ON FUNCTION propertyai.rent_lock_scope(uuid),propertyai.rent_complete_command(uuid,uuid,text,uuid,text,jsonb)
 TO propertyai_rent_runtime,propertyai_rent_scheduler;
GRANT EXECUTE ON FUNCTION propertyai.rent_command_result(uuid,text,uuid,text)
 TO propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
-- No PUBLIC/legacy grants on new views; no shared receipt/event INSERT/UPDATE/SELECT or sequence grant to finance roles.
-- No owner/migrator membership, no authority_epoch mutation, no queue/outbox creation, no external effect.

CREATE VIEW propertyai.v_rent_terms WITH(security_barrier=true) AS
 SELECT * FROM propertyai.rent_term_revision WHERE organization_id=propertyai.rent_visible_org();
CREATE VIEW propertyai.v_rent_periods WITH(security_barrier=true) AS
 SELECT * FROM propertyai.rent_billing_period WHERE organization_id=propertyai.rent_visible_org();
CREATE VIEW propertyai.v_rent_contract_history WITH(security_barrier=true) AS
 SELECT * FROM propertyai.rent_contract_history WHERE organization_id=propertyai.rent_visible_org();
GRANT SELECT ON propertyai.v_rent_terms,propertyai.v_rent_periods,propertyai.v_rent_contract_history
 TO propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;

CREATE FUNCTION propertyai.rent_command_by_id(p_org uuid,p_command uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,propertyai,pg_temp AS $$
DECLARE c propertyai.finance_command;
BEGIN
 PERFORM propertyai.rent_context(p_org,false);
 SELECT * INTO c FROM propertyai.finance_command WHERE organization_id=p_org AND command_id=p_command;
 IF NOT FOUND THEN RETURN NULL; END IF;
 RETURN propertyai.rent_command_result(p_org,c.command_type,c.idempotency_key,NULL);
END $$;
REVOKE ALL ON FUNCTION propertyai.rent_command_by_id(uuid,uuid) FROM PUBLIC,propertyai_app_runtime,propertyai_async_worker,propertyai_readonly;
GRANT EXECUTE ON FUNCTION propertyai.rent_command_by_id(uuid,uuid) TO propertyai_rent_runtime,propertyai_rent_reader,propertyai_rent_scheduler;
