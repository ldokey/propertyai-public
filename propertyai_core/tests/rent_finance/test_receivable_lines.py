from __future__ import annotations
from calendar import monthrange
from datetime import date
from hashlib import sha256
from uuid import UUID,uuid4

import pytest

from propertyai_core.application.rent_errors import RentError
from propertyai_core.tests.rent_finance.p1_cases import cases
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_resident,create_contract

_CASES=cases()["line_period"]

@pytest.mark.parametrize("case",_CASES,ids=lambda x:x["id"])
def test_v4_receivable_line_period_case(case):
    inputs=case["inputs"]
    assert case["invariant"]=="RECEIVABLE_LINE_FROZEN_PERIOD_TERM_INTERSECTION"
    assert inputs["same_contract"] and inputs["confirmed_term"]
    assert inputs["monthly_rent_matches"] and inputs["header_total_equals_lines"]
    with operator_fixture() as (_,service,ids):
        resident,revision,_=create_resident(service)
        contract=create_contract(service,ids,[resident],revision,
            start="2026-07-01",end="2026-11-01",due_month="2026-09",
            term_start=inputs["term_start"],term_end=inputs["term_end"],
            period_start=inputs["period_start"],period_end=inputs["period_end"])
        receivable_id=uuid4()
        line_id=uuid4()
        service_from=date.fromisoformat(inputs["service_from"])
        service_to=date.fromisoformat(inputs["service_to_exclusive"])
        charge_days=(service_to-service_from).days
        assert 1<=charge_days<=31
        def plan(tx,command_id):
            tx.insert("finance_receivable",{
                "receivable_id":receivable_id,"organization_id":tx.organization_id,
                "created_command_id":command_id,"last_command_id":command_id,
                "contract_id":UUID(contract["contract_id"]),"property_id":ids["property_id"],
                "period_id":UUID(contract["period_id"]),"origin_kind":"RENT","currency":"KRW",
                "original_amount":1000000,"calculation_snapshot":__import__("psycopg").types.json.Jsonb({"case_id":case["id"]}),
                "calculation_sha256":sha256(case["id"].encode()).hexdigest(),
                "due_on":date(2026,9,5),"voided":case["id"]=="LINE11",
                "void_reason":"Synthetic voided history" if case["id"]=="LINE11" else None,
                "replacement_of":None})
            tx.insert("finance_receivable_line",{
                "line_id":line_id,"organization_id":tx.organization_id,
                "created_command_id":command_id,"receivable_id":receivable_id,
                "term_id":UUID(contract["term_id"]),"line_no":1,
                "service_from":service_from,"service_to_exclusive":service_to,
                "monthly_rent":1000000,"month_days":monthrange(service_from.year,service_from.month)[1],
                "charge_days":charge_days,"original_amount":1000000})
            return {"case_id":case["id"],"path":inputs["path"]}
        def execute():
            return service.repository.run_command(command_type="ISSUE_RENT",
                idempotency_key=uuid4(),normalized_hash=sha256(case["id"].encode()).hexdigest(),
                expected_ledger_revision=int(contract["revision"]),plan=plan)
        if case["expected_valid"]:
            result,_=execute()
            assert result["result"]["case_id"]==case["id"]
            read=service.repository.read_rows(
                "SELECT service_from,service_to_exclusive FROM propertyai.finance_receivable_line WHERE line_id=%s",(line_id,))
            assert read[0]["service_from"]==service_from
            assert read[0]["service_to_exclusive"]==service_to
        else:
            with pytest.raises(RentError) as caught:execute()
            assert caught.value.code=="VALIDATION_ERROR"
            assert service.repository.read_rows(
                "SELECT line_id FROM propertyai.finance_receivable_line WHERE line_id=%s",(line_id,))==[]
            assert service.repository.read_rows(
                "SELECT receivable_id FROM propertyai.finance_receivable WHERE receivable_id=%s",(receivable_id,))==[]
