from __future__ import annotations
from datetime import date
from uuid import UUID,uuid4
import pytest

from propertyai_core.application.commands.rent import RentCommand
from propertyai_core.application.rent_errors import RentError
from propertyai_core.domain.finance import IssueRentEligibilityV1,FinanceError,due_on,calculate_rent,ConfirmedTerm
from propertyai_core.tests.rent_finance.p1_cases import cases
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_resident,create_contract,issue

_D7=cases()["d7"]
_BINDINGS=[(c["id"],channel) for c in _D7 for channel in c["channels"]]+[
    ("D7_NEG_01","OPERATOR"),("D7_NEG_02","OPERATOR"),
    ("D7_NEG_03","OPERATOR"),("D7_NEG_04","SCHEDULER_TEST"),
    ("D7_NEG_05","SHARED_HANDLER_REPLAY")]

def test_cal01_cal10_against_product():
    # Calendar clamp, D-7, day-count proration, and one real read-only preview.
    for year,month,day_number,expected in [(2024,2,31,29),(2025,2,31,28),(2026,4,31,30),
                                           (2026,9,10,10),(2026,9,1,1)]:
        assert due_on(date(year,month,1),day_number).day==expected
    with operator_fixture(today=date(2026,9,28)) as (_,service,ids):
        resident,revision,_=create_resident(service)
        contract=create_contract(service,ids,[resident],revision,
            start="2026-09-01",end="2026-10-01",due_month="2026-10",due_day=31)
        preview=service.preview_charge({"contract_id":contract["contract_id"],"period_id":contract["period_id"]})
        assert preview["due_on"]=="2026-10-31"
        assert preview["generation_on"]=="2026-10-24"
        assert preview["amount"]=="1000000"
        assert len(preview["segments"])==1
        assert preview["segments"][0]["month_days"]==30
        assert preview["segments"][0]["charge_days"]==30

@pytest.mark.parametrize("binding",_BINDINGS,ids=lambda x:x[0]+"_"+x[1])
def test_v4_d7_entry_path_case(binding):
    case_id,channel=binding
    oracle=next((x for x in _D7 if x["id"]==case_id),None)
    if oracle:
        effective=date.fromisoformat(oracle["effective_issue_date"])
        frozen_due=date.fromisoformat(oracle["due_on"])
        assert due_on(date(2026,9,1),10)==frozen_due
        if not oracle["expected_eligible"]:
            with pytest.raises(FinanceError) as e:IssueRentEligibilityV1(effective,frozen_due).evaluate()
            assert e.value.code==oracle["error"]
        else:
            assert IssueRentEligibilityV1(effective,frozen_due).evaluate()["generation_on"]==oracle["generation_on"]
    else:
        effective=date(2026,9,3) if case_id=="D7_NEG_05" else date(2026,9,2)
    with operator_fixture(today=effective,with_scheduler=channel=="SCHEDULER_TEST") as (_,service,ids):
        resident,revision,_=create_resident(service)
        contract=create_contract(service,ids,[resident],revision,
            start="2026-09-01",end="2026-10-01",due_month="2026-09",due_day=10)
        preview=service.preview_charge({"contract_id":contract["contract_id"],"period_id":contract["period_id"]})
        assert preview["due_on"]=="2026-09-10"
        body={"contract_id":contract["contract_id"],"period_id":contract["period_id"],
              "expected_contract_version":preview["contract_version"],"expected_term_versions":preview["term_versions"],
              "expected_ledger_revision":preview["ledger_revision"],"calculation_sha256":preview["calculation_sha256"],
              "issuance_mode":"OPERATOR_CONFIRMED","replaces_receivable_id":None}
        key=uuid4()
        if case_id=="D7_NEG_01":
            body["effective_issue_date"]="2026-09-03"
            with pytest.raises(RentError) as e:service.handle(RentCommand("issueRent",body,key))
            assert e.value.code=="VALIDATION_ERROR"
        elif case_id=="D7_NEG_02":
            body["force"]=True
            with pytest.raises(RentError) as e:service.handle(RentCommand("issueRent",body,key))
            assert e.value.code=="VALIDATION_ERROR"
        elif case_id=="D7_NEG_03":
            body["replaces_receivable_id"]=str(uuid4())
            with pytest.raises(RentError) as e:service.handle(RentCommand("issueRent",body,key))
            assert e.value.code=="BILLING_NOT_YET_DUE"
        elif channel=="SCHEDULER_TEST":
            scheduler=ids["scheduler_service"]
            if case_id in {"CAL01","D7_NEG_04"}:
                with pytest.raises(RentError) as e:scheduler.issue_from_scheduler(UUID(contract["contract_id"]),UUID(contract["period_id"]),key)
                assert e.value.code=="BILLING_NOT_YET_DUE"
            else:
                committed,_=scheduler.issue_from_scheduler(UUID(contract["contract_id"]),UUID(contract["period_id"]),key)
                assert committed["result"]["issue_eligibility"]["effective_issue_date"]==effective.isoformat()
        else:
            command=RentCommand("issueRent",body,key)
            if case_id=="CAL01":
                with pytest.raises(RentError) as e:service.handle(command)
                assert e.value.code=="BILLING_NOT_YET_DUE"
            else:
                committed,replayed=service.handle(command)
                assert not replayed
                assert committed["result"]["issue_eligibility"]["generation_on"]=="2026-09-03"
                if case_id=="D7_NEG_05":
                    replay,was_replayed=service.handle(command)
                    assert was_replayed and replay==committed
                    assert replay["result"]["issue_eligibility"]==committed["result"]["issue_eligibility"]
        if case_id.startswith("D7_NEG_0") and case_id!="D7_NEG_05" or case_id=="CAL01":
            assert service.overview("2026-09")["rows"]==[]
