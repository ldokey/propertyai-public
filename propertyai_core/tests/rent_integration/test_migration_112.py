from __future__ import annotations

import json
import os
from pathlib import Path

from propertyai_core.tests.fixture_lifecycle import run_owned_command, safe_fixture_env
from propertyai_core.tests.postgres_stage_a_cluster import _LoopbackUnixBridge
from propertyai_core.tests.rent_finance_staging import checked_bytes, require
from propertyai_core.tests.rent_finance_test_cluster import STABLE_INSTALL, start_disposable_rent_postgres

REPO_ROOT = Path(__file__).resolve().parents[3]
CURRENT_MIGRATIONS = REPO_ROOT / "db/v2_2_1/migration"


def _flyway_current(fixture, operation: str) -> None:
    require(operation in {"migrate", "validate"}, "I1_FLYWAY_OPERATION_NOT_ALLOWED")
    env = safe_fixture_env()
    for key in ("JAVA_HOME", "FLYWAY_JAVA_CMD", "FLYWAY_LOCATIONS"):
        env.pop(key, None)
    home = fixture.cluster.root / "i1-flyway-home"
    tmp = fixture.cluster.root / "i1-flyway-tmp"
    home.mkdir(mode=0o700, exist_ok=True)
    tmp.mkdir(mode=0o700, exist_ok=True)
    env.update(
        HOME=str(home), TMPDIR=str(tmp), FLYWAY_USE_SYSTEM_PROXIES="false",
        LANG="C", LC_ALL="C",
        JAVA_ARGS="-XX:-UsePerfData -Djava.io.tmpdir=" + str(tmp) + " -Duser.home=" + str(home),
    )
    config = fixture.manifest_path.parent / "db/v2_2_1/flyway/flyway.conf"
    bridge = _LoopbackUnixBridge(fixture.cluster.socket_dir / f".s.PGSQL.{fixture.cluster.port}")
    try:
        with bridge:
            require(bridge.bind_address is not None and bridge.bind_address[0] == "127.0.0.1", "I1_NON_LOOPBACK_FLYWAY_BRIDGE")
            argv = [
                str(STABLE_INSTALL / "flyway"),
                "-configFiles=" + str(config),
                "-locations=filesystem:" + str(CURRENT_MIGRATIONS),
                f"-url=jdbc:postgresql://127.0.0.1:{bridge.bind_address[1]}/postgres?sslmode=disable",
                "-user=propertyai_flyway", "-password=", operation,
            ]
            result = run_owned_command(
                argv, cwd=fixture.cluster.root, env=env, check=False, timeout=60
            )
            fixture.record(
                "I1_FLYWAY_112_" + operation.upper(), argv=argv, exit_code=result.returncode,
                stdout=result.stdout, stderr=result.stderr, bridge_bind=bridge.bind_address,
                bridge_upstream=str(bridge.upstream), current_migrations=str(CURRENT_MIGRATIONS),
            )
            require(result.returncode == 0, "I1_FLYWAY_112_" + operation.upper() + "_FAILED")
    finally:
        fixture.record("I1_FLYWAY_112_BRIDGE_CLOSED", closed=bridge.closed)


def _assert_source_parity(binding: dict) -> None:
    staged = Path(next(item for item in binding["staging"] if item["scenario"] == "FRESH")["path"])
    manifest = json.loads(checked_bytes(staged))
    records = {item["version"]: item for item in manifest["ordered_inputs"] if item["phase"] == "FLYWAY"}
    for number in range(101, 112):
        version = f"20260904.{number}"
        record = records[version]
        checked_bytes(CURRENT_MIGRATIONS / Path(record["staged_relative_path"]).name, record["sha256"])
    migration_112 = CURRENT_MIGRATIONS / "V20260904.112__rent_auth_sessions.sql"
    assert migration_112.is_file()


def test_migration_112_fresh_and_upgrade_108_chain_role_grants_and_cleanup():
    binding_path = Path(os.environ["PROPERTYAI_RENT_TEST_BINDING_FILE"])
    binding = json.loads(checked_bytes(binding_path, os.environ["PROPERTYAI_RENT_TEST_BINDING_SHA256"]))
    _assert_source_parity(binding)
    provenance = binding["provenance"]
    observed_cleanup = []

    for scenario in ("FRESH", "UPGRADE_108"):
        stage = next(item for item in binding["staging"] if item["scenario"] == scenario)
        fixture = None
        with start_disposable_rent_postgres(
            Path(stage["path"]), stage["sha256"], Path(provenance["path"]), provenance["sha256"],
            scenario=scenario, durability=False,
        ) as fixture:
            before = fixture.history()
            prior = [row for row in before if row["version"] is not None]
            assert [row["version"] for row in prior] == [f"20260904.{n}" for n in range(101, 112)]

            _flyway_current(fixture, "migrate")
            _flyway_current(fixture, "validate")

            after = fixture.history()
            installed = [row for row in after if row["version"] is not None]
            assert [row["version"] for row in installed] == [f"20260904.{n}" for n in range(101, 113)]
            assert after[:len(before)] == before
            assert installed[-1]["version"] == "20260904.112" and installed[-1]["success"] is True
            assert fixture.query("SELECT count(*) FROM propertyai.rent_auth_session") == [(0,)]

            privilege = fixture.query("""
                SELECT
                  has_table_privilege('propertyai_app_runtime','propertyai.rent_auth_session','SELECT'),
                  has_table_privilege('propertyai_app_runtime','propertyai.rent_auth_session','INSERT'),
                  has_function_privilege('propertyai_app_runtime','propertyai.rent_auth_session_get(text)','EXECUTE'),
                  has_function_privilege('propertyai_app_runtime','propertyai.rent_auth_session_revoke(text,timestamp with time zone)','EXECUTE'),
                  has_function_privilege('propertyai_app_runtime','propertyai.rent_auth_session_create(text,text,uuid,uuid,text,text[],text,timestamp with time zone,timestamp with time zone)','EXECUTE')
            """)[0]
            assert privilege == (False, False, True, True, True)
            assert fixture.query("SELECT relowner::regrole::text FROM pg_class WHERE oid='propertyai.rent_auth_session'::regclass") == [("propertyai_owner",)]
        assert fixture is not None and fixture.cleanup_report is not None
        assert fixture.cleanup_report["classification"] == "PASS"
        assert fixture.cleanup_report["root_absent"] is True
        observed_cleanup.append(scenario)

    assert observed_cleanup == ["FRESH", "UPGRADE_108"]
