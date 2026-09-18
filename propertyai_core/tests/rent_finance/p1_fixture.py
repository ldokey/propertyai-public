"""Only synthetic disposable Rent product fixtures; Production connections are absent."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg

from propertyai_core.application.handlers.rent import BusinessDateProvider, RentService
from propertyai_core.adapters.postgres.rent_repository import RentPostgresRepository
from propertyai_core.tests.rent_finance_staging import checked_bytes, require
from propertyai_core.tests.rent_finance_test_cluster import start_disposable_rent_postgres


@contextmanager
def operator_fixture(*, today: date = date(2026, 9, 28), durability: bool = False, with_scheduler: bool = False):
    binding_path = os.environ.get("PROPERTYAI_RENT_TEST_BINDING_FILE")
    binding_sha = os.environ.get("PROPERTYAI_RENT_TEST_BINDING_SHA256")
    require(bool(binding_path and binding_sha), "RENT_TEST_BINDING_NOT_PROVIDED")
    binding = json.loads(checked_bytes(Path(binding_path), binding_sha))
    stage = next(x for x in binding["staging"] if x["scenario"] == "FRESH")
    provenance = binding["provenance"]
    with start_disposable_rent_postgres(
        Path(stage["path"]),stage["sha256"],Path(provenance["path"]),provenance["sha256"],
        scenario="FRESH",durability=durability,
    ) as fixture:
        suffix = uuid4().hex[:10]
        organization_id, property_id, unit_id, party_id = (uuid4() for _ in range(4))
        login = "rent_test_" + suffix
        fixture.cluster.psql(sql_text=f"""
            CREATE ROLE {login} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            GRANT propertyai_rent_runtime TO {login};
            INSERT INTO propertyai.organization(organization_id,organization_code,display_name,organization_status,data_environment)
            VALUES('{organization_id}','RT-ORG-{suffix}','Synthetic Rent Org','ACTIVE','TEST');
            INSERT INTO propertyai.property(property_id,organization_id,property_code,display_name,timezone_name)
            VALUES('{property_id}','{organization_id}','RT-PROP-{suffix}','Synthetic Property','Asia/Seoul');
            INSERT INTO propertyai.rental_unit(rental_unit_id,rental_unit_code,property_id,display_name)
            VALUES('{unit_id}','RT-UNIT-{suffix}','{property_id}','Synthetic Unit');
            INSERT INTO propertyai.party(party_id,party_code,display_name,data_environment)
            VALUES('{party_id}','RT-ACTOR-{suffix}','Synthetic Operator','TEST');
            INSERT INTO propertyai.organization_member(organization_member_id,organization_id,party_id,membership_role,membership_status,joined_at)
            VALUES('{uuid4()}','{organization_id}','{party_id}','OPERATOR','ACTIVE',transaction_timestamp());
            INSERT INTO propertyai.finance_ledger_scope(organization_id,revision) VALUES('{organization_id}',0);
            INSERT INTO propertyai.rent_runtime_binding(login_name,organization_id,actor_party_id,principal_type,capability,data_environment,enabled)
            VALUES('{login}','{organization_id}','{party_id}','PARTY','WRITE','TEST',true);
        """)
        repository = RentPostgresRepository(lambda: psycopg.connect(fixture.cluster.login_dsn(login)))
        service = RentService(repository,BusinessDateProvider(lambda timezone: today))
        ids={"organization_id":organization_id,"property_id":property_id,
             "unit_id":unit_id,"party_id":party_id,"login":login}
        if with_scheduler:
            scheduler_login='rent_scheduler_test_'+suffix
            fixture.cluster.psql(sql_text=f"""
                CREATE ROLE {scheduler_login} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
                GRANT propertyai_rent_scheduler TO {scheduler_login};
                INSERT INTO propertyai.rent_runtime_binding(login_name,organization_id,actor_party_id,principal_type,capability,data_environment,enabled)
                VALUES('{scheduler_login}','{organization_id}',NULL,'SYSTEM','SCHEDULER','TEST',true);
            """)
            scheduler_repo=RentPostgresRepository(lambda: psycopg.connect(fixture.cluster.login_dsn(scheduler_login)))
            ids['scheduler_service']=RentService(scheduler_repo,BusinessDateProvider(lambda timezone: today),scheduler_entry_enabled=True)
            ids['scheduler_login']=scheduler_login
        yield fixture,service,ids
    assert fixture.cleanup_report["classification"] == "PASS"
    assert fixture.cleanup_report["root_absent"]
