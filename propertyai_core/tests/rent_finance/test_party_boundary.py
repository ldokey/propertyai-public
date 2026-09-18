from __future__ import annotations
from threading import Barrier,Thread
from uuid import uuid4
import psycopg,pytest

from propertyai_core.application.commands.rent import RentCommand
from propertyai_core.application.rent_errors import RentError
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_resident,create_account,create_contract,receipt

MATRIX=[
("rent_resident","linked_party_id","active_test","ACCEPT_REFERENCE"),
("rent_resident","linked_party_id","active_production","REJECT_REFERENCE"),
("rent_resident","linked_party_id","inactive_test","REJECT_REFERENCE"),
("rent_resident","linked_party_id","missing","REJECT_REFERENCE"),
("rent_resident","linked_party_id","null","ACCEPT_REFERENCE"),
("rent_contract_party","party_id","active_test","ACCEPT_REFERENCE"),
("rent_contract_party","party_id","active_production","REJECT_REFERENCE"),
("rent_contract_party","party_id","inactive_test","REJECT_REFERENCE"),
("rent_contract_party","party_id","missing","REJECT_REFERENCE"),
("rent_contract_party","party_id","null","REJECT_REFERENCE"),
("finance_account","owner_party_id","active_test","ACCEPT_REFERENCE"),
("finance_account","owner_party_id","active_production","REJECT_REFERENCE"),
("finance_account","owner_party_id","inactive_test","REJECT_REFERENCE"),
("finance_account","owner_party_id","missing","REJECT_REFERENCE"),
("finance_account","owner_party_id","null","ACCEPT_REFERENCE"),
("finance_movement_revision","counterparty_party_id","active_test","ACCEPT_REFERENCE"),
("finance_movement_revision","counterparty_party_id","active_production","REJECT_REFERENCE"),
("finance_movement_revision","counterparty_party_id","inactive_test","REJECT_REFERENCE"),
("finance_movement_revision","counterparty_party_id","missing","REJECT_REFERENCE"),
("finance_movement_revision","counterparty_party_id","null","ACCEPT_REFERENCE"),
("rent_runtime_binding","actor_party_id","active_test","ACCEPT_REFERENCE"),
("rent_runtime_binding","actor_party_id","active_production","REJECT_REFERENCE"),
("rent_runtime_binding","actor_party_id","inactive_test","REJECT_REFERENCE"),
("rent_runtime_binding","actor_party_id","missing","REJECT_REFERENCE"),
("rent_runtime_binding","actor_party_id","null","ACCEPT_ONLY_SYSTEM_SCHEDULER; PARTY_PRINCIPAL_REJECT"),
]

@pytest.fixture(scope="module")
def party_env():
    with operator_fixture() as value:
        fixture,service,ids=value
        state={"revision":"0"}
        yield fixture,service,ids,state

def _party(fixture,state):
    if state=="active_test":return None # fixture actor
    identity=uuid4()
    if state!="missing":
        env="PRODUCTION" if state=="active_production" else "TEST"
        active="false" if state=="inactive_test" else "true"
        fixture.cluster.psql(sql_text=f"""INSERT INTO propertyai.party(party_id,party_code,display_name,data_environment,active)
          VALUES('{identity}','P-{uuid4().hex[:12]}','Synthetic boundary','{env}',{active});""")
    return identity

@pytest.mark.parametrize('table,column,state,expected',[pytest.param(*x,id=f'{x[0]}.{x[1]}.{x[2]}') for x in MATRIX])
def test_all_new_party_fk_guards(table,column,state,expected,party_env,request):
    fixture,service,ids,shared=party_env
    party=ids["party_id"] if state=="active_test" else _party(fixture,state)
    if state=="null":party=None
    accepted=expected.startswith("ACCEPT")
    before=shared["revision"]
    try:
        if table=="rent_resident":
            _,after,_=create_resident(service,before,"Boundary",party=party)
        elif table=="finance_account":
            _,after,_=create_account(service,before,owner=party)
        elif table=="rent_contract_party":
            resident,rev,_=create_resident(service,before,"Contract party base")
            shared["revision"]=rev
            if party is None:
                parties=[{"party_id":None,"party_role":"TENANT"}]
            else:parties=[{"party_id":str(party),"party_role":"TENANT"}]
            contract=create_contract(service,ids,[resident],rev,parties=parties)
            after=contract["revision"]
        elif table=="finance_movement_revision":
            account,rev,_=create_account(service,before)
            shared["revision"]=rev
            body={"account_id":account,"occurred_on":"2026-09-28","amount":"1000","currency":"KRW",
                  "payer_raw":None,"counterparty_party_id":str(party) if party else None,
                  "attribution":{"status":"PROPERTY_CONFIRMED","property_id":str(ids["property_id"]),"contract_id":None},
                  "allocations":[],"expected_ledger_revision":rev}
            result=service.handle(RentCommand("recordReceipt",body,uuid4()))[0]
            after=result["ledger_revision"]
        else:
            if state=="null":
                login="scheduler_null_"+uuid4().hex[:10]
                fixture.cluster.psql(sql_text=f"""CREATE ROLE {login} LOGIN; GRANT propertyai_rent_scheduler TO {login};
                  INSERT INTO propertyai.rent_runtime_binding(login_name,organization_id,actor_party_id,principal_type,capability,data_environment,enabled)
                  VALUES('{login}','{ids["organization_id"]}',NULL,'SYSTEM','SCHEDULER','TEST',true);""")
                with psycopg.connect(fixture.cluster.login_dsn(login)) as conn:
                    assert conn.execute("SELECT propertyai.rent_visible_org()").fetchone()[0]==ids["organization_id"]
                return
            if state in {"missing","active_production","inactive_test"}:
                with pytest.raises(Exception):
                    fixture.cluster.psql(sql_text=f"""UPDATE propertyai.rent_runtime_binding SET actor_party_id='{party}' WHERE login_name='{ids["login"]}';""")
                return
            fixture.cluster.psql(sql_text=f"""UPDATE propertyai.rent_runtime_binding SET actor_party_id='{party}' WHERE login_name='{ids["login"]}';""")
            try:
                result=service.handle(RentCommand("createResident",{"display_name":"Actor test","linked_party_id":None,
                    "private_contact_ref":None,"review_status":"VERIFIED","expected_ledger_revision":before},uuid4()))[0]
                after=result["ledger_revision"]
            finally:
                fixture.cluster.psql(sql_text=f"""UPDATE propertyai.rent_runtime_binding SET actor_party_id='{ids["party_id"]}' WHERE login_name='{ids["login"]}';""")
        if not accepted:pytest.fail("wrong-environment/inactive/missing party reference accepted")
        shared["revision"]=after
    except RentError as exc:
        if accepted:raise
        assert exc.code in {"NOT_FOUND","NOT_AUTHORIZED","VALIDATION_ERROR"}
    except (TypeError,ValueError):
        if accepted:raise
        assert party is None

def test_binding_actor_cannot_use_production_party():
    with operator_fixture() as (fixture,service,ids):
        prod=uuid4()
        fixture.cluster.psql(sql_text=f"""INSERT INTO propertyai.party(party_id,party_code,display_name,data_environment)
          VALUES('{prod}','PROD-{uuid4().hex[:8]}','Production party','PRODUCTION');""")
        with pytest.raises(Exception):
            fixture.cluster.psql(sql_text=f"""UPDATE propertyai.rent_runtime_binding SET actor_party_id='{prod}' WHERE login_name='{ids["login"]}';""")
        create_resident(service)

def test_direct_dml_cannot_bypass_guard():
    with operator_fixture() as (fixture,service,ids):
        with psycopg.connect(fixture.cluster.login_dsn(ids["login"]),autocommit=True) as conn:
            with pytest.raises(psycopg.Error):
                conn.execute("""INSERT INTO propertyai.rent_resident(resident_id,organization_id,created_command_id,last_command_id,display_name,review_status)
                  VALUES(%s,%s,%s,%s,'Bypass','VERIFIED')""",(uuid4(),ids["organization_id"],uuid4(),uuid4()))

def test_wrong_environment_combined_command_rolls_back():
    with operator_fixture() as (fixture,service,ids):
        prod=uuid4();fixture.cluster.psql(sql_text=f"""INSERT INTO propertyai.party(party_id,party_code,display_name,data_environment)
          VALUES('{prod}','PROD-{uuid4().hex[:8]}','Production party','PRODUCTION');""")
        with pytest.raises(RentError):
            create_resident(service,party=prod)
        assert service.repository.read_rows("SELECT resident_id FROM propertyai.rent_resident")==[]

def test_revision_reversal_replacement_rechecks_party():
    # The final-state deferred check scans every current and historical movement revision.
    with operator_fixture() as (_,service,_):
        functions=service.repository.read_rows("""SELECT pg_get_functiondef('propertyai.rent_check_ledger(uuid)'::regprocedure) AS body""")
        body=functions[0]["body"]
        assert "finance_movement_revision" in body and "counterparty_party_id" in body
        assert "rent_assert_test_party" in body

def test_concurrent_shared_party_change_serializes():
    with operator_fixture() as (fixture,service,ids):
        barrier=Barrier(2);out=[]
        def command():
            barrier.wait()
            try:out.append(("command",create_resident(service,party=ids["party_id"])[2]))
            except RentError as e:out.append(("command_error",e.code))
        def deactivate():
            barrier.wait()
            fixture.cluster.psql(sql_text=f"UPDATE propertyai.party SET active=false WHERE party_id='{ids['party_id']}';")
            out.append(("party","inactive"))
        a=Thread(target=command);b=Thread(target=deactivate);a.start();b.start();a.join();b.join()
        assert len(out)==2
        residents=fixture.query('SELECT linked_party_id FROM propertyai.rent_resident')
        assert not residents or residents[0][0]==ids['party_id']
