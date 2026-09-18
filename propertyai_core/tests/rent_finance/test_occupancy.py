from __future__ import annotations
from dataclasses import replace
from datetime import date
from threading import Barrier,Thread
from uuid import UUID,uuid4
import pytest

from propertyai_core.application.rent_errors import RentError
from propertyai_core.domain.longstay import ActualOccupancy,LongstayError,validate_actual_occupancies
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_resident,create_contract,occ,occupancy_command

ORG=uuid4();UNIT=uuid4();C1=uuid4();C2=uuid4();R1=uuid4();R2=uuid4()
def row(cid,rid,start,end=None,status="VERIFIED"):
    return ActualOccupancy(uuid4(),ORG,cid,rid,UNIT,status,start,end)

def test_adjacent_intervals_allowed():
    validate_actual_occupancies([row(C1,R1,date(2026,9,1),date(2026,9,10)),
                                 row(C2,R1,date(2026,9,10),None)],{C1:{R1},C2:{R1}})

def test_same_resident_overlap_rejected():
    with pytest.raises(LongstayError,match="PERIOD_CONFLICT"):
        validate_actual_occupancies([row(C1,R1,date(2026,9,1),date(2026,9,20)),
                                     row(C1,R1,date(2026,9,10),None)],{C1:{R1}})

def test_shared_different_residents_same_contract():
    validate_actual_occupancies([row(C1,R1,date(2026,9,1)),row(C1,R2,date(2026,9,1))],
                                {C1:{R1,R2}})

def test_unrelated_contract_overlap_rejected():
    with pytest.raises(LongstayError,match="PERIOD_CONFLICT"):
        validate_actual_occupancies([row(C1,R1,date(2026,9,1)),row(C2,R2,date(2026,9,1))],
                                    {C1:{R1},C2:{R2}})

def test_previous_contract_does_not_authorize_overlap():
    # Linkage is deliberately absent from the domain sharing rule.
    with pytest.raises(LongstayError,match="PERIOD_CONFLICT"):
        validate_actual_occupancies([row(C1,R1,date(2026,9,1)),row(C2,R2,date(2026,9,1))],
                                    {C1:{R1},C2:{R2}})

def test_unbounded_end_rejects_later_unrelated_contract():
    with pytest.raises(LongstayError,match="PERIOD_CONFLICT"):
        validate_actual_occupancies([row(C1,R1,date(2026,1,1),None),row(C2,R2,date(2027,1,1),None)],
                                    {C1:{R1},C2:{R2}})

def test_unknown_start_cannot_be_verified():
    with pytest.raises(LongstayError,match="VALIDATION_ERROR"):
        validate_actual_occupancies([row(C1,R1,None,None)],{C1:{R1}})

def test_missing_explicit_resident_membership_rejected():
    with pytest.raises(LongstayError,match="VALIDATION_ERROR"):
        validate_actual_occupancies([row(C1,R1,date(2026,9,1))],{C1:set()})

def test_verification_promotion_rechecks_overlap():
    with operator_fixture() as (_,service,ids):
        r1,revision,_=create_resident(service)
        r2,revision,_=create_resident(service,revision,"Other")
        first=create_contract(service,ids,[r1],revision,occupancies=[occ(r1,start="2026-09-01")])
        second=create_contract(service,ids,[r2],first["revision"],
            occupancies=[occ(r2,status="NEEDS_REVIEW",start=None)])
        detail=service.get_contract(UUID(second["contract_id"]))
        before=detail["occupancies"][0]
        with pytest.raises(RentError) as e:
            occupancy_command(service,"confirmOccupancyStart",before["occupancy_id"],before["version"],
                              second["version"],second["revision"],actual_start="2026-09-10")
        assert e.value.code=="PERIOD_CONFLICT"
        assert service.get_contract(UUID(second["contract_id"]))["occupancies"][0]==before

def test_two_connection_unrelated_contract_race():
    with operator_fixture() as (_,service,ids):
        r1,revision,_=create_resident(service)
        r2,revision,_=create_resident(service,revision,"Other")
        first=create_contract(service,ids,[r1],revision,occupancies=[occ(r1,status="NEEDS_REVIEW",start=None)])
        second=create_contract(service,ids,[r2],first["revision"],occupancies=[occ(r2,status="NEEDS_REVIEW",start=None)])
        a=service.get_contract(UUID(first["contract_id"]))["occupancies"][0]
        b=service.get_contract(UUID(second["contract_id"]))["occupancies"][0]
        barrier=Barrier(2);out=[]
        def worker(contract,item,ledger):
            barrier.wait()
            try:out.append(("ok",occupancy_command(service,"confirmOccupancyStart",item["occupancy_id"],item["version"],
                contract["version"],ledger,actual_start="2026-09-01")))
            except RentError as e:out.append(("err",e.code))
        t1=Thread(target=worker,args=(first,a,second["revision"]));t2=Thread(target=worker,args=(second,b,second["revision"]))
        t1.start();t2.start();t1.join();t2.join()
        assert sorted(x[0] for x in out)==["err","ok"]
        assert out[0][1]=="VERSION_CONFLICT" or out[1][1]=="VERSION_CONFLICT" or any(x==("err","PERIOD_CONFLICT") for x in out)
