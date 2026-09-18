from __future__ import annotations
from uuid import UUID,uuid4
import pytest
from propertyai_core.application.rent_errors import RentError
from propertyai_core.tests.rent_finance.p1_cases import cases
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import (
    create_resident,create_contract,occ,occupancy_command)

_CASES=[x for x in cases()["occupancy"] if x["id"].startswith("OCC_V4_")]

@pytest.mark.parametrize("case",_CASES,ids=lambda x:x["id"])
def test_v4_occupancy_command_case(case):
    assert case["command_type"] in {"CONFIRM_OCCUPANCY_START","CONFIRM_OCCUPANCY_END","CORRECT_OCCUPANCY_DATES"}
    with operator_fixture() as (_,service,ids):
        resident,revision,_=create_resident(service)
        if case["id"]=="OCC_V4_04":
            first=create_contract(service,ids,[resident],revision,
                occupancies=[occ(resident,start="2026-09-01",end="2026-09-10")])
            second=create_contract(service,ids,[resident],first["revision"],
                occupancies=[occ(resident,start="2026-09-10",end="2026-09-20")])
            contract=second
            operation="correctOccupancyDates"
            fields={"actual_start":"2026-09-05","actual_end_exclusive":"2026-09-20"}
        elif case["id"]=="OCC_V4_01":
            contract=create_contract(service,ids,[resident],revision,
                occupancies=[occ(resident,status="NEEDS_REVIEW",start=None)])
            operation="confirmOccupancyStart"
            fields={"actual_start":"2026-09-03"}
        else:
            contract=create_contract(service,ids,[resident],revision,
                occupancies=[occ(resident,start="2026-09-03")])
            operation="confirmOccupancyEnd" if case["id"]=="OCC_V4_02" else "correctOccupancyDates"
            fields={"actual_end_exclusive":"2026-09-20"} if case["id"]=="OCC_V4_02" else {
                "actual_start":"2026-09-05","actual_end_exclusive":"2026-09-22"}
        occ_id=contract["occupancy_ids"][0]
        before=service.get_contract(UUID(contract["contract_id"]))["occupancies"][0]
        args=(service,operation,occ_id,before["version"],contract["version"],contract["revision"])
        if case["id"]=="OCC_V4_04":
            with pytest.raises(RentError) as e:occupancy_command(*args,**fields)
            assert e.value.code=="PERIOD_CONFLICT"
            after=service.get_contract(UUID(contract["contract_id"]))["occupancies"][0]
            assert after==before
            assert service.lookup_command(case["command_type"],uuid4())["status"]=="NOT_FOUND"
        else:
            committed=occupancy_command(*args,**fields)
            changes=committed["result"]["occupancy_changes"]
            assert len(changes)==1
            assert changes[0]["before"]["occupancy_id"]==occ_id
            assert changes[0]["evidence"]["operator_confirmed"] is True
            after=service.get_contract(UUID(contract["contract_id"]))["occupancies"][0]
            if case["id"]=="OCC_V4_01":
                assert before["review_status"]=="NEEDS_REVIEW"
                assert after["review_status"]=="VERIFIED"
                assert after["actual_start"]=="2026-09-03"
            elif case["id"]=="OCC_V4_02":
                assert after["actual_end_exclusive"]=="2026-09-20"
                assert after["review_status"]=="VERIFIED"
            else:
                assert changes[0]["before"]["actual_start"]=="2026-09-03"
                assert changes[0]["after"]["actual_start"]=="2026-09-05"
            assert service.overview("2026-09")["rows"]==[]
