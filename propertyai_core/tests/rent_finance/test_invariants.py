from __future__ import annotations
from datetime import date
from hashlib import sha256
from uuid import UUID,uuid4
import pytest
from propertyai_core.application.rent_errors import RentError
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_resident,create_account,create_contract,issue,receipt

def test_deferred_invariants_and_append_only():
    with operator_fixture(today=date(2026,9,28)) as (fixture,service,ids):
        resident,revision,_=create_resident(service)
        account,revision,_=create_account(service,revision)
        contract=create_contract(service,ids,[resident],revision)
        receivable,issued,_,_=issue(service,contract)
        source,payment=receipt(service,account,issued["ledger_revision"],ids,contract["contract_id"])
        command_count=fixture.query('SELECT count(*) FROM propertyai.finance_command')[0][0]
        receipts=service.repository.read_rows("SELECT propertyai.rent_command_by_id(propertyai.rent_visible_org(),%s) AS r",(UUID(issued["command_id"]),))
        assert receipts[0]["r"]==issued
        with pytest.raises(RentError) as e:
            service.repository.run_command(command_type="ALLOCATE",idempotency_key=uuid4(),
                normalized_hash=sha256(b"invalid-deferred").hexdigest(),
                expected_ledger_revision=int(payment["ledger_revision"]),
                plan=lambda tx,cid:(tx.insert("finance_allocation",{
                  "allocation_id":uuid4(),"organization_id":tx.organization_id,"created_command_id":cid,
                  "source_id":UUID(source),"receivable_id":UUID(receivable),"amount":1000001,
                  "record_kind":"APPLY","reverse_of":None,"attribution_confirmed":True,
                  "override_attribution_reason":None,"reason":None}) or {"invalid":True}))
        assert e.value.code=="VALIDATION_ERROR"
        assert fixture.query('SELECT allocation_id FROM propertyai.finance_allocation')==[]
        assert fixture.query('SELECT count(*) FROM propertyai.finance_command')[0][0]==command_count
        with pytest.raises(RentError) as e:
            service.repository.run_command(command_type="ALLOCATE",idempotency_key=uuid4(),
                normalized_hash=sha256(b"delete").hexdigest(),
                expected_ledger_revision=int(payment["ledger_revision"]),
                plan=lambda tx,cid:(tx.rows("DELETE FROM propertyai.finance_receivable WHERE receivable_id=%s",(UUID(receivable),)) or {"bad":True}))
        assert e.value.code=='NOT_AUTHORIZED'
        assert service.overview("2026-09")["selected_month_balance"]=="1000000"

def test_f11_schema_reversal_replacement_guards():
    with operator_fixture(today=date(2026,9,28)) as (fixture,service,ids):
        resident,revision,_=create_resident(service)
        account,revision,_=create_account(service,revision)
        contract=create_contract(service,ids,[resident],revision)
        receivable,issued,_,_=issue(service,contract)
        source,payment=receipt(service,account,issued["ledger_revision"],ids,contract["contract_id"])
        rows=service.repository.read_rows("""SELECT tgname,tgdeferrable,tginitdeferred
          FROM pg_trigger WHERE tgrelid IN (
            'propertyai.finance_receivable'::regclass,'propertyai.finance_allocation'::regclass,
            'propertyai.finance_movement_revision'::regclass) AND NOT tgisinternal ORDER BY tgname""")
        assert any(x["tgdeferrable"] and x["tginitdeferred"] for x in rows)
        grants=service.repository.read_rows("""SELECT has_table_privilege(session_user,'propertyai.finance_receivable','DELETE') AS d,
          has_table_privilege(session_user,'propertyai.finance_allocation','UPDATE') AS u""")[0]
        assert not grants["d"] and not grants["u"]
        command_receipt=service.get_command(UUID(payment['command_id']))['command']
        assert command_receipt==payment
        assert service.funding_sources()["rows"][0]["source_id"]==source
        assert service.overview("2026-09")["rows"][0]["receivable_id"]==receivable
