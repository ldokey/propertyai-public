from __future__ import annotations
from datetime import date
from uuid import UUID,uuid4
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import (
    create_resident,create_account,create_contract,issue,receipt)

def test_committed_record_survives_fixture_restart():
    with operator_fixture(today=date(2026,9,28),durability=True) as (fixture,service,ids):
        resident,revision,_=create_resident(service)
        account,revision,_=create_account(service,revision)
        contract=create_contract(service,ids,[resident],revision)
        receivable,issued,_,_=issue(service,contract)
        source,payment=receipt(service,account,issued["ledger_revision"],ids,contract["contract_id"],amount="250000")
        issue_id=issued["command_id"];payment_id=payment["command_id"]
        before=service.overview("2026-09")
        sources_before=service.funding_sources()
        assert before["selected_month_balance"]=="1000000"
        assert sources_before["rows"][0]["available"]=="250000"
        fixture.restart()
        assert service.get_contract(UUID(contract["contract_id"]))["contract_id"]==contract["contract_id"]
        assert service.overview("2026-09")["selected_month_balance"]==before["selected_month_balance"]
        assert service.funding_sources()["rows"][0]["source_id"]==source
        assert service.get_command(UUID(issue_id))["command"]==issued
        assert service.get_command(UUID(payment_id))["command"]==payment
        assert fixture.query("SELECT count(*) FROM propertyai.domain_event WHERE event_type LIKE 'RENT_%'")[0][0]>=2
        assert tuple(fixture.introspection()["settings"][0][2:])==("on","on","on")
    assert fixture.cleanup_report["classification"]=="PASS"
    assert fixture.cleanup_report["root_absent"]
