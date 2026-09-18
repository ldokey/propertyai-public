from datetime import date
from uuid import UUID, uuid4

from propertyai_core.application.commands.rent import RentCommand
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture


def _run(service, operation, body, target=None, key=None):
    return service.handle(RentCommand(operation,body,key or uuid4(),target))[0]


def test_contract_charge_receipt_explicit_allocation_read():
    with operator_fixture(today=date(2026,9,28)) as (_,service,ids):
        resident = _run(service,"createResident",{
            "display_name":"Synthetic Resident","linked_party_id":None,"private_contact_ref":None,
            "review_status":"VERIFIED","expected_ledger_revision":"0",
        })
        resident_id = next(e["id"] for e in resident["result"]["entities"] if e["kind"]=="RESIDENT")
        account = _run(service,"createAccount",{
            "display_name":"Synthetic Account","currency":"KRW","owner_party_id":None,
            "masked_identifier":"****0000","protected_identifier_ref":None,
            "expected_ledger_revision":resident["ledger_revision"],
        })
        account_id = next(e["id"] for e in account["result"]["entities"] if e["kind"]=="ACCOUNT")
        contract = _run(service,"createContract",{
            "rental_unit_id":str(ids["unit_id"]),"starts_on":"2026-09-01","ends_on_exclusive":"2026-10-01",
            "lifecycle":"ACTIVE","readiness":"READY","resident_ids":[resident_id],"contract_parties":[],
            "term":{
                "valid_from":"2026-09-01","valid_to_exclusive":"2026-10-01",
                "monthly_rent":"1000000","amount_confirmed":True,"due_day":5,"cycle_confirmed":True,
                "cycle_rule":{"schema_version":1,"mode":"EXPLICIT_PERIODS","first_due_month":"2026-10","confirmation_ref":"SYNTHETIC-TERM"},
                "policy_version":"RENT_APPROVED_1G_2G_3G_4G_V1",
            },
            "billing_periods":[{
                "cycle_start":"2026-09-01","cycle_end_exclusive":"2026-10-01",
                "due_month":"2026-10","confirmation_ref":"SYNTHETIC-PERIOD",
            }],
            "previous_contract_id":None,"reason":"Synthetic confirmed contract",
            "expected_ledger_revision":account["ledger_revision"],"occupancies":[],
        })
        contract_id = next(e["id"] for e in contract["result"]["entities"] if e["kind"]=="CONTRACT")
        period_id = next(e["id"] for e in contract["result"]["entities"] if e["kind"]=="PERIOD")
        preview = service.preview_charge({"contract_id":contract_id,"period_id":period_id})
        assert preview["status"]=="READY"
        assert preview["amount"]=="1000000"
        assert preview["generation_on"]=="2026-09-28"
        issued = _run(service,"issueRent",{
            "contract_id":contract_id,"period_id":period_id,
            "expected_contract_version":preview["contract_version"],
            "expected_term_versions":preview["term_versions"],
            "expected_ledger_revision":preview["ledger_revision"],
            "calculation_sha256":preview["calculation_sha256"],
            "issuance_mode":"OPERATOR_CONFIRMED","replaces_receivable_id":None,
        })
        assert issued["result"]["issue_eligibility"]["effective_issue_date"]=="2026-09-28"
        receivable_id = next(e["id"] for e in issued["result"]["entities"] if e["kind"]=="RECEIVABLE")
        receipt = _run(service,"recordReceipt",{
            "account_id":account_id,"occurred_on":"2026-09-28","amount":"1000000",
            "currency":"KRW","payer_raw":None,"counterparty_party_id":None,
            "attribution":{"status":"CONTRACT_CONFIRMED","property_id":str(ids["property_id"]),"contract_id":contract_id},
            "allocations":[],"expected_ledger_revision":issued["ledger_revision"],
        })
        assert receipt["result"]["source_balances"][0]["available"]=="1000000"
        source_id = receipt["result"]["source_balances"][0]["source_id"]
        allocation = _run(service,"allocate",{
            "source_id":source_id,
            "expected_source_version":receipt["result"]["source_balances"][0]["version"],
            "expected_ledger_revision":receipt["ledger_revision"],
            "allocations":[{
                "receivable_id":receivable_id,"amount":"300000",
                "expected_version":issued["result"]["receivable_balances"][0]["version"],
                "attribution_confirmed":True,"override_attribution_reason":None,
            }],
            "attribution":{"status":"CONTRACT_CONFIRMED","property_id":str(ids["property_id"]),"contract_id":contract_id},
        })
        assert allocation["result"]["receivable_balances"][0]["balance"]=="700000"
        assert allocation["result"]["source_balances"][0]["available"]=="700000"
        overview = service.overview("2026-09",ids["property_id"])
        assert overview["selected_month_balance"]=="700000"
        assert overview["rows"][0]["payment_state"]=="PARTIAL"
        detail = service.get_contract(UUID(contract_id))
        assert detail["contract_id"] == contract_id
        assert detail["occupancies"] == []


def test_fixture_f01_f08_and_f10():
    from datetime import datetime, timedelta, timezone
    from propertyai_core.web.rent_api import LocalTestSessions, RentSession, RentAPI
    with operator_fixture() as (_,service,_):
        sessions = LocalTestSessions()
        sessions.register("synthetic-token",RentSession(service,"synthetic-csrf",
            datetime.now(timezone.utc)+timedelta(hours=1),frozenset({"READ","WRITE"})))
        api = RentAPI(sessions)
        sessions.register("no-capability-token",RentSession(service,"no-capability-csrf",
            datetime.now(timezone.utc)+timedelta(hours=1),frozenset()))
        denied=api.handle("GET","/api/v1/rent/commands/"+str(uuid4()),
                          {"Cookie":"rent_session=no-capability-token"})
        assert denied.status==403 and denied.body["code"]=="NOT_AUTHORIZED"
        body = {
            "display_name":"Synthetic Resident","linked_party_id":None,"private_contact_ref":None,
            "review_status":"NEEDS_REVIEW","expected_ledger_revision":"0",
        }
        encoded = __import__("json").dumps(body).encode()
        route = "/api/v1/longstay/residents"
        assert api.handle("POST",route,{"Content-Type":"application/json"},encoded).status == 401
        headers = {"Content-Type":"application/json","Cookie":"rent_session=synthetic-token",
                   "Idempotency-Key":str(uuid4())}
        assert api.handle("POST",route,headers,encoded).status == 403
        headers["X-CSRF-Token"] = "synthetic-csrf"
        first = api.handle("POST",route,headers,encoded)
        assert first.status == 200
        replay = api.handle("POST",route,headers,encoded)
        assert replay.status == 200
        assert replay.body == first.body
        assert replay.headers["X-Idempotent-Replayed"] == "true"
        lookup = api.handle("GET","/api/v1/rent/commands/by-key/"+headers["Idempotency-Key"]+"?command_type=CREATE_RESIDENT",
                            {"Cookie":"rent_session=synthetic-token"})
        assert lookup.status == 200
        assert lookup.body["status"] == "FOUND"
        assert lookup.body["command"] == first.body
