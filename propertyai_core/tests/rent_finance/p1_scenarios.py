"""Synthetic product-path builders for required P1 tests."""
from __future__ import annotations
from datetime import date
from uuid import UUID, uuid4

from propertyai_core.application.commands.rent import RentCommand

def run(service, operation, body, *, target=None, key=None):
    return service.handle(RentCommand(operation, body, key or uuid4(), target))[0]

def entity(result, kind):
    return next(e for e in result["result"]["entities"] if e["kind"] == kind)

def create_resident(service, revision="0", name="Synthetic Resident", review="VERIFIED", party=None):
    result=run(service,"createResident",{
        "display_name":name,"linked_party_id":str(party) if party else None,
        "private_contact_ref":None,"review_status":review,"expected_ledger_revision":revision})
    return entity(result,"RESIDENT")["id"],result["ledger_revision"],result

def create_account(service, revision, owner=None):
    result=run(service,"createAccount",{
        "display_name":"Synthetic Account","currency":"KRW",
        "owner_party_id":str(owner) if owner else None,"masked_identifier":"****0000",
        "protected_identifier_ref":None,"expected_ledger_revision":revision})
    return entity(result,"ACCOUNT")["id"],result["ledger_revision"],result

def create_contract(service, ids, resident_ids, revision, *,
                    start="2026-09-01", end="2026-10-01", due_month="2026-10",
                    due_day=5, unit_id=None, previous=None, occupancies=None,
                    term_start=None, term_end=None, period_start=None, period_end=None,
                    parties=None, name="Synthetic confirmed"):
    body={
        "rental_unit_id":str(unit_id or ids["unit_id"]),"starts_on":start,"ends_on_exclusive":end,
        "lifecycle":"ACTIVE","readiness":"READY","resident_ids":resident_ids,
        "contract_parties":parties or [],
        "term":{"valid_from":term_start or start,"valid_to_exclusive":term_end or end,
                "monthly_rent":"1000000","amount_confirmed":True,"due_day":due_day,
                "cycle_confirmed":True,
                "cycle_rule":{"schema_version":1,"mode":"EXPLICIT_PERIODS",
                              "first_due_month":due_month,"confirmation_ref":name},
                "policy_version":"RENT_APPROVED_1G_2G_3G_4G_V1"},
        "billing_periods":[{"cycle_start":period_start or start,
                            "cycle_end_exclusive":period_end or end,
                            "due_month":due_month,"confirmation_ref":name}],
        "previous_contract_id":previous,"reason":name,
        "expected_ledger_revision":revision,"occupancies":occupancies or []}
    result=run(service,"createContract",body)
    return {"contract_id":entity(result,"CONTRACT")["id"],
            "period_id":entity(result,"PERIOD")["id"],
            "term_id":entity(result,"TERM")["id"],
            "occupancy_ids":[e["id"] for e in result["result"]["entities"] if e["kind"]=="OCCUPANCY"],
            "version":entity(result,"CONTRACT")["version"],
            "revision":result["ledger_revision"],"result":result}

def occ(resident_id, status="VERIFIED", start="2026-09-01", end=None):
    return {"resident_id":resident_id,"actual_start":start,"actual_end_exclusive":end,
            "review_status":status}

def issue(service, contract, *, key=None, mode="OPERATOR_CONFIRMED", replaces=None):
    preview=service.preview_charge({"contract_id":contract["contract_id"],
                                    "period_id":contract["period_id"]})
    body={"contract_id":contract["contract_id"],"period_id":contract["period_id"],
          "expected_contract_version":preview["contract_version"],
          "expected_term_versions":preview["term_versions"],
          "expected_ledger_revision":preview["ledger_revision"],
          "calculation_sha256":preview["calculation_sha256"],
          "issuance_mode":mode,"replaces_receivable_id":replaces}
    result=run(service,"issueRent",body,key=key)
    return entity(result,"RECEIVABLE")["id"],result,body,preview

def receipt(service, account_id, revision, ids, contract_id=None, amount="1000000", allocations=None):
    attribution={"status":"CONTRACT_CONFIRMED","property_id":str(ids["property_id"]),
                 "contract_id":contract_id} if contract_id else {
                 "status":"PROPERTY_CONFIRMED","property_id":str(ids["property_id"]),
                 "contract_id":None}
    result=run(service,"recordReceipt",{
        "account_id":account_id,"occurred_on":"2026-09-28","amount":amount,
        "currency":"KRW","payer_raw":None,"counterparty_party_id":None,
        "attribution":attribution,"allocations":allocations or [],
        "expected_ledger_revision":revision})
    return result["result"]["source_balances"][0]["source_id"],result

def evidence(ref="SYNTHETIC"):
    return {"operator_confirmed":True,"confirmation_ref":ref,"reason":"Synthetic factual evidence"}

def occupancy_command(service, operation, occ_id, occ_version, contract_version, ledger_revision, *, key=None, **fields):
    body={"expected_version":str(occ_version),"expected_contract_version":str(contract_version),
          "expected_ledger_revision":str(ledger_revision),"evidence":evidence(),**fields}
    return run(service,operation,body,target=UUID(str(occ_id)),key=key)
