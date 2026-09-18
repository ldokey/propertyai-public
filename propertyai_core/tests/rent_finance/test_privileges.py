from __future__ import annotations
from uuid import uuid4
import psycopg,pytest
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture

def test_finance_helpers_not_public():
    with operator_fixture() as (fixture,service,_):
        rows=fixture.query("""SELECT p.proname,
          has_function_privilege('public',p.oid,'EXECUTE')
          FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='propertyai' AND p.proname IN
          ('rent_context','rent_visible_org','rent_lock_scope','rent_complete_command',
           'rent_command_result','rent_command_by_id') ORDER BY p.proname""")
        assert rows and all(not granted for _,granted in rows)
        role=next(x for x in fixture.introspection()["roles"] if x[0]=="propertyai_rent_runtime")
        assert role[1:]==(False,False,False,False,False,False)

def test_forged_context_and_cross_org_denied():
    with operator_fixture() as (fixture,service,ids):
        other=uuid4()
        fixture.cluster.psql(sql_text=f"""INSERT INTO propertyai.organization(
          organization_id,organization_code,display_name,organization_status,data_environment)
          VALUES('{other}','OTHER-{uuid4().hex[:8]}','Other TEST','ACTIVE','TEST');""")
        with psycopg.connect(fixture.cluster.login_dsn(ids["login"]),autocommit=True) as conn:
            assert conn.execute("SELECT propertyai.rent_visible_org()").fetchone()[0]==ids["organization_id"]
            conn.execute("SELECT set_config('propertyai.organization_id',%s,false)",(str(other),))
            assert conn.execute("SELECT propertyai.rent_visible_org()").fetchone()[0]==ids["organization_id"]
            with pytest.raises(psycopg.Error):
                conn.execute("BEGIN ISOLATION LEVEL SERIALIZABLE; SELECT propertyai.rent_lock_scope(%s)",(other,))
            conn.execute("ROLLBACK")
            with pytest.raises(psycopg.Error):
                conn.execute("SELECT * FROM propertyai.rent_runtime_binding")
        assert service.reference_data()["units"][0]["property_id"]==str(ids["property_id"])

def test_legacy_public_reader_denied():
    with operator_fixture() as (fixture,_,_):
        login="legacy_reader_"+uuid4().hex[:10]
        fixture.cluster.psql(sql_text=f"CREATE ROLE {login} LOGIN; GRANT propertyai_readonly TO {login};")
        with psycopg.connect(fixture.cluster.login_dsn(login),autocommit=True) as conn:
            for statement in ("SELECT * FROM propertyai.v_rent_receivables",
                              "SELECT propertyai.rent_visible_org()",
                              "SELECT * FROM propertyai.finance_receivable"):
                with pytest.raises(psycopg.Error):conn.execute(statement).fetchall()
