from __future__ import annotations
from datetime import date
from threading import Barrier,Thread
from uuid import UUID,uuid4
import pytest

from propertyai_core.application.commands.rent import RentCommand
from propertyai_core.application.rent_errors import RentError
from propertyai_core.adapters.postgres.rent_repository import RentPostgresTransaction
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import (
    create_resident,create_account,create_contract,issue,receipt,run,entity)

def test_f10_same_key_lost_response_reconciliation():
    with operator_fixture() as (_,service,_):
        key=uuid4();body={"display_name":"Lost response resident","linked_party_id":None,
            "private_contact_ref":None,"review_status":"VERIFIED","expected_ledger_revision":"0"}
        command=RentCommand("createResident",body,key)
        committed,replayed=service.handle(command)
        assert not replayed
        # Simulate a transport loss by dropping the committed return value, then reconcile.
        lookup=service.lookup_command("CREATE_RESIDENT",key)
        assert lookup["status"]=="FOUND" and lookup["command"]==committed
        again,was_replayed=service.handle(command)
        assert was_replayed and again==committed
        rows=service.repository.read_rows("SELECT count(*) AS n FROM propertyai.v_rent_contracts")
        assert rows[0]["n"]==0
        assert len(service.repository.read_rows(
            "SELECT resident_id FROM propertyai.rent_resident WHERE resident_id=%s",
            (UUID(entity(committed,"RESIDENT")["id"]),)))==1
        different=dict(body,display_name="Changed")
        with pytest.raises(RentError) as e:service.handle(RentCommand("createResident",different,key))
        assert e.value.code=="IDEMPOTENCY_CONFLICT"

def _money_race(service,body,operation,key,out,barrier):
    barrier.wait()
    try:out.append(("ok",service.handle(RentCommand(operation,body,key))[0]))
    except RentError as e:out.append(("err",e.code))

def test_f12_two_connection_race():
    with operator_fixture(today=date(2026,9,28)) as (_,service,ids):
        resident,revision,_=create_resident(service)
        account,revision,_=create_account(service,revision)
        contract=create_contract(service,ids,[resident],revision)
        receivable,issued,_,_=issue(service,contract)
        source,payment=receipt(service,account,issued["ledger_revision"],ids,contract["contract_id"])
        allocation={"source_id":source,
          "expected_source_version":payment["result"]["source_balances"][0]["version"],
          "expected_ledger_revision":payment["ledger_revision"],
          "allocations":[{"receivable_id":receivable,"amount":"700000",
              "expected_version":issued["result"]["receivable_balances"][0]["version"],
              "attribution_confirmed":True,"override_attribution_reason":None}],
          "attribution":{"status":"CONTRACT_CONFIRMED","property_id":str(ids["property_id"]),
                         "contract_id":contract["contract_id"]}}
        barrier=Barrier(2);out=[]
        threads=[Thread(target=_money_race,args=(service,allocation,"allocate",uuid4(),out,barrier)) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        assert sorted(x[0] for x in out)==["err","ok"]
        assert next(x[1] for x in out if x[0]=="err") in {"VERSION_CONFLICT","RETRYABLE_TRANSACTION"}
        overview=service.overview("2026-09")
        assert overview["selected_month_allocated"]=="700000"
        assert overview["selected_month_balance"]=="300000"
        assert service.funding_sources()["rows"][0]["available"]=="300000"

def test_fault_rollback_all_components(monkeypatch):
    with operator_fixture() as (_,service,ids):
        resident,revision,_=create_resident(service)
        account,revision,_=create_account(service,revision)
        original=RentPostgresTransaction.insert
        def fault(self,table,values):
            original(self,table,values)
            if table=="finance_funding_source":raise RuntimeError("SYNTHETIC_FAULT")
        monkeypatch.setattr(RentPostgresTransaction,"insert",fault)
        body={"account_id":account,"occurred_on":"2026-09-28","amount":"1000","currency":"KRW",
              "payer_raw":None,"counterparty_party_id":None,
              "attribution":{"status":"PROPERTY_CONFIRMED","property_id":str(ids["property_id"]),"contract_id":None},
              "allocations":[],"expected_ledger_revision":revision}
        key=uuid4()
        with pytest.raises(RentError) as e:service.handle(RentCommand("recordReceipt",body,key))
        assert e.value.code=="INTERNAL_ERROR"
        assert service.funding_sources()["rows"]==[]
        assert service.lookup_command("RECORD_RECEIPT",key)["status"]=="NOT_FOUND"
        assert service.repository.read_rows("SELECT movement_id FROM propertyai.finance_movement")==[]
        assert service.repository.read_rows("SELECT movement_revision_id FROM propertyai.finance_movement_revision")==[]
