from __future__ import annotations
from uuid import UUID,uuid4
import pytest

from propertyai_core.application.rent_errors import RentError
from propertyai_core.adapters.postgres.rent_repository import RentPostgresTransaction
from propertyai_core.tests.rent_finance.p1_cases import cases
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import (
    create_resident,create_contract,occ,occupancy_command)

_CASES=[x for x in cases()["occupancy"] if x["id"].startswith("ROOM")]

@pytest.mark.parametrize("case",_CASES,ids=lambda x:x["id"])
def test_v4_room_move_case(case,monkeypatch):
    assert case["operation_id"]=="moveOccupancy"
    assert case["command_type"]=="MOVE_OCCUPANCY"
    with operator_fixture() as (fixture,service,ids):
        source_resident,revision,_=create_resident(service)
        source=create_contract(service,ids,[source_resident],revision,
            occupancies=[occ(source_resident,start="2026-09-01")])
        target_unit=ids["unit_id"] if case["id"]=="ROOM07" else uuid4()
        if target_unit!=ids["unit_id"]:
            fixture.cluster.psql(sql_text=f"""
                INSERT INTO propertyai.rental_unit(rental_unit_id,rental_unit_code,property_id,display_name)
                VALUES('{target_unit}','ROOM-{uuid4().hex[:10]}','{ids["property_id"]}','Synthetic Target Unit');
            """)
        other_resident=None
        if case["id"] in {"ROOM02","ROOM08"}:
            other_resident,revision,_=create_resident(service,source["revision"],"Synthetic Co-resident")
        target_residents=[source_resident]
        target_occupancies=[]
        if case["id"]=="ROOM08":
            target_residents.append(other_resident)
            target_occupancies=[occ(other_resident,start="2026-09-02")]
        if case["id"]=="ROOM09":
            target_occupancies=[occ(source_resident,start="2026-09-02")]
        base_revision=revision if other_resident else source["revision"]
        target=create_contract(service,ids,target_residents,base_revision,
            unit_id=target_unit,previous=None if case["id"]=="ROOM03" else source["contract_id"],
            occupancies=target_occupancies)
        if case["id"]=="ROOM02":
            unrelated=create_contract(service,ids,[other_resident],target["revision"],
                unit_id=target_unit,occupancies=[occ(other_resident,start="2026-09-02")])
            final_revision=unrelated["revision"]
        else:final_revision=target["revision"]
        source_id=source["occupancy_ids"][0]
        before=service.get_contract(UUID(source["contract_id"]))["occupancies"][0]
        assert before["actual_end_exclusive"] is None
        expected_version=before["version"]
        if case["id"]=="ROOM04":expected_version=str(int(expected_version)+1)
        move_on="2026-09-01" if case["id"]=="ROOM10" else "2026-09-10"
        if case["id"]=="ROOM11":
            ended=occupancy_command(service,"confirmOccupancyEnd",source_id,
                before["version"],source["version"],final_revision,
                actual_end_exclusive="2026-09-07")
            final_revision=ended["ledger_revision"]
            before=service.get_contract(UUID(source["contract_id"]))["occupancies"][0]
            expected_version=before['version']
            source['version']=service.get_contract(UUID(source['contract_id']))['version']
        if case['id']=='ROOM05':
            original=RentPostgresTransaction.insert
            def fault(self,table,values):
                if table=="rent_occupancy":raise RuntimeError("SYNTHETIC_FAULT_AFTER_SOURCE_END")
                return original(self,table,values)
            monkeypatch.setattr(RentPostgresTransaction,"insert",fault)
        key=uuid4()
        args=(service,"moveOccupancy",source_id,expected_version,source["version"],final_revision)
        fields={"target_contract_id":target["contract_id"],
                "expected_target_contract_version":target["version"],
                "effective_move_on":move_on}
        expected_error={
            "ROOM02":"PERIOD_CONFLICT","ROOM03":"VALIDATION_ERROR",
            "ROOM04":"VERSION_CONFLICT","ROOM05":"INTERNAL_ERROR",
            "ROOM07":"VALIDATION_ERROR","ROOM09":"PERIOD_CONFLICT",
            "ROOM10":"VALIDATION_ERROR","ROOM11":"PERIOD_CONFLICT"}
        if case["id"] in expected_error:
            with pytest.raises(RentError) as e:occupancy_command(*args,key=key,**fields)
            assert e.value.code==expected_error[case["id"]]
            after=service.get_contract(UUID(source["contract_id"]))["occupancies"][0]
            assert after==before
            assert service.lookup_command("MOVE_OCCUPANCY",key)["status"]=="NOT_FOUND"
            if case['id']=='ROOM05':
                assert service.get_contract(UUID(target['contract_id']))['occupancies']==[]
        else:
            result=occupancy_command(*args,key=key,**fields)
            changes=result["result"]["occupancy_changes"]
            assert len(changes)==2
            assert changes[0]["before"]["occupancy_id"]==source_id
            assert changes[0]["after"]["actual_end_exclusive"]==move_on
            assert changes[1]["after"]["actual_start"]==move_on
            assert changes[1]["after"]["resident_id"]==source_resident
            assert changes[1]["after"]["contract_id"]==target["contract_id"]
            target_detail=service.get_contract(UUID(target["contract_id"]))
            assert any(x["occupancy_id"]==changes[1]["after"]["occupancy_id"] for x in target_detail["occupancies"])
            if case["id"]=="ROOM08":
                assert len(target_detail["occupancies"])==2
            if case["id"]=="ROOM06":
                replay=occupancy_command(*args,key=key,**fields)
                assert replay==result
                assert service.lookup_command("MOVE_OCCUPANCY",key)["command"]==result
                assert len(service.get_contract(UUID(target["contract_id"]))["occupancies"])==1
            assert service.overview("2026-09")["rows"]==[]
