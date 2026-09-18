from __future__ import annotations
from datetime import date
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_resident,create_account,create_contract,issue,receipt

def test_f09_multiple_sources_no_join_fanout():
    with operator_fixture(today=date(2026,9,28)) as (_,service,ids):
        resident,revision,_=create_resident(service)
        account,revision,_=create_account(service,revision)
        contract=create_contract(service,ids,[resident],revision)
        receivable,issued,_,_=issue(service,contract)
        _,first=receipt(service,account,issued["ledger_revision"],ids,contract["contract_id"],amount="100000")
        _,second=receipt(service,account,first["ledger_revision"],ids,contract["contract_id"],amount="200000")
        sources=service.funding_sources()
        assert len(sources["rows"])==2
        assert sorted(x["available"] for x in sources["rows"])==["100000","200000"]
        overview=service.overview("2026-09",ids["property_id"])
        assert len(overview["rows"])==1
        assert overview["selected_month_obligation"]=="1000000"
        assert overview["selected_month_allocated"]=="0"
        assert overview["selected_month_balance"]=="1000000"
        assert overview["all_period_balance"]=="1000000"
        assert service.overview("2026-09",ids["property_id"],page_size=1)["next_cursor"] is None
