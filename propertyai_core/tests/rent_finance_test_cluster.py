"""Rent-only disposable PostgreSQL/Flyway adapter.

This does not call, modify or monkeypatch the accepted 101..108 harness.
It reuses its owned-fixture primitives and in-process loopback/Unix bridge.
Every database is newly initialized, Unix-socket-only, and positively cleaned up.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterator

import psycopg
import pytest
from propertyai_core.tests.fixture_lifecycle import FixtureLifecycle, FixtureLifecycleError, run_owned_command, safe_fixture_env
from propertyai_core.tests.postgres_stage_a_cluster import DisposablePostgres, _LoopbackUnixBridge, _free_port, _postgres_bin, _validated_flyway_version
from propertyai_core.tests.rent_finance_staging import checked_bytes, canonical, sha256, require, RentPrerequisiteError, validate_staging_manifest

STABLE_INSTALL = Path("/Users/kate/DKATE/adcp-runtime/artifacts/flyway/13.5.0")
APPROVED_ARCHIVE_SHA256 = "8de41cc837833d815cf14e5649806e4ab7bb9422fa22090ad8be308a57347cce"
DISTRIBUTION_MANIFEST_SHA256 = "6f0333dda5671e49cdb4ce7e9851d0cf3bf01323cf5b191c7f5c7fb0381a88f5"
STABLE_REUSE_APPROVAL = "CHAT.PROJ.HQ:APPROVAL:PROPERTYAI-LONGSTAY-RENT-BATCH-01:P1_FLYWAY_13_5_0_STABLE_REUSE:V1"
CRITICAL_HASHES = {
    "flyway": "7aa02a1013d3b80a0fccdd7d342fa9816e54d0221111db4b08432db7cc705722",
    "executable-ref.json": "e745fd55f0e765c11ee943ef4d9f935be2eaf81982029862f5872fcd60795320",
    "lib/flyway/flyway-commandline-13.5.0.jar": "8a44ce54d1ac60e808696588b901a79c28fa377e1d4bc9919b190a2e77e96ae3",
    "jre/bin/java": "2187a28d325113dd5a4af73cb35a3e4049876d474d601b5b1d60c5474cf2d28e",
    "jre/release": "01c7bc7a0c070b630c1e2d9abda6039dab5ab1d009e32b2fd1e54090b594f13b",
}
PROVENANCE_FIELDS = {"approval_ref", "version_exact_13.5.0", "distribution_source", "distribution_edition", "platform_arch", "archive_filename", "archive_bytes", "archive_sha256", "verification_source", "verification_method", "installation_root", "entrypoint_realpath", "entrypoint_sha256", "distribution_file_manifest_sha256", "version_output_sha256", "resolved_java_realpath_version_if_used", "materialization_receipt_ref"}


def stream_hash(path: Path) -> tuple[int, str]:
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and path.absolute() == path.resolve(), "ARTIFACT_FILE_ALIAS")
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
        after = os.fstat(stream.fileno())
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "ARTIFACT_CHANGED_DURING_READ")
    return size, digest.hexdigest()


def verify_flyway_provenance(path: Path, expected_sha256: str) -> dict:
    receipt = json.loads(checked_bytes(Path(path), expected_sha256))
    fields = receipt.get("required_fields", {})
    require(PROVENANCE_FIELDS <= fields.keys() and all(fields[k] is not None and fields[k] != "" for k in PROVENANCE_FIELDS), "FLYWAY_PROVENANCE_FIELD_MISSING")
    require(receipt.get("P1_FLYWAY_PROVENANCE") == "PASS", "FLYWAY_PROVENANCE_NOT_PASS")
    require(fields["approval_ref"] == STABLE_REUSE_APPROVAL and fields["version_exact_13.5.0"] is True, "FLYWAY_APPROVAL_OR_VERSION")
    require(fields["installation_root"] == str(STABLE_INSTALL) and fields["entrypoint_realpath"] == str(STABLE_INSTALL / "flyway"), "UNAPPROVED_FLYWAY_LOCATION")
    require(os.environ.get("PROPERTYAI_FLYWAY_BIN") == fields["entrypoint_realpath"], "PROCESS_SCOPED_FLYWAY_BIN_NOT_BOUND")
    require(fields["archive_filename"] == "flyway-commandline-13.5.0-macosx-arm64.tar.gz" and fields["archive_bytes"] == 549578755 and fields["archive_sha256"] == APPROVED_ARCHIVE_SHA256, "FLYWAY_ARCHIVE_IDENTITY")
    parity_ref = fields["materialization_receipt_ref"]
    parity = json.loads(checked_bytes(Path(parity_ref["path"]), parity_ref["sha256"]))
    require(parity["receipt_kind"] == "P1_FLYWAY_APPROVED_ARCHIVE_BYTE_EQUIVALENCE_RECEIPT_V1" and parity["result"] == "PASS_1448_OF_1448", "ARCHIVE_PARITY_RECEIPT_INVALID")
    require(parity["HISTORICAL_MATERIALIZATION_EVENT_RECONSTRUCTED"] == "NO" and parity["CURRENT_APPROVED_DISTRIBUTION_BYTE_EQUIVALENCE"] == "YES", "PROVENANCE_SEMANTICS")
    require(all(parity[k] == 0 for k in ("missing_count", "extra_count", "byte_mismatch_count", "sha_mismatch_count")), "ARCHIVE_PARITY_NOT_EXACT")
    # P1 archive/stable 1448-file parity was independently accepted. The required
    # profile consumes its exact frozen receipt without repeating that closed audit.
    require(fields['distribution_file_manifest_sha256'] == DISTRIBUTION_MANIFEST_SHA256,
            'DISTRIBUTION_MANIFEST_BINDING')
    for rel, expected in CRITICAL_HASHES.items():
        require(stream_hash(STABLE_INSTALL / rel)[1] == expected, "STABLE_CRITICAL_FILE_DRIFT")
    for item in fields["verification_source"]:
        checked_bytes(Path(item["path"]), item["sha256"])
    return receipt


@dataclass
class RentFixture:
    cluster: DisposablePostgres
    manifest_path: Path
    manifest_sha256: str
    manifest: dict
    evidence_root: Path
    durability: bool
    pg_ctl: str
    observations: list[dict] = field(default_factory=list)
    cleanup_report: dict | None = None

    def record(self, phase: str, **facts: Any) -> None:
        value = {"at": datetime.now(timezone.utc).isoformat(), "phase": phase, **facts}
        self.observations.append(value)
        with (self.evidence_root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def query(self, sql_text: str, parameters: tuple | None = None) -> list[tuple]:
        self.cluster.lifecycle.guard()
        with psycopg.connect(self.cluster.base_dsn, autocommit=True) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                return conn.execute(sql_text, parameters).fetchall()

    def history(self) -> list[dict]:
        exists = self.query("SELECT to_regclass('propertyai.flyway_schema_history') IS NOT NULL")[0][0]
        if not exists:
            return []
        columns = ("installed_rank", "version", "description", "type", "script", "checksum", "installed_by", "success")
        rows = self.query("SELECT installed_rank,version,description,type,script,checksum,installed_by,success FROM propertyai.flyway_schema_history ORDER BY installed_rank")
        return [dict(zip(columns, row)) for row in rows]

    def introspection(self) -> dict:
        return {"history": self.history(),
                "tables": self.query("SELECT c.relname,c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='propertyai' AND c.relkind='r' ORDER BY c.relname"),
                "functions": self.query("SELECT p.proname,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='propertyai' AND p.proname LIKE 'rent_%' ORDER BY p.proname"),
                "constraints": self.query("SELECT conname,contype,condeferrable,condeferred FROM pg_constraint WHERE connamespace='propertyai'::regnamespace ORDER BY conname"),
                "roles": self.query("SELECT rolname,rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname LIKE 'propertyai_%' ORDER BY rolname"),
                "settings": self.query("SELECT current_setting('server_version'),current_setting('listen_addresses'),current_setting('fsync'),current_setting('synchronous_commit'),current_setting('full_page_writes')")}

    def flyway(self, operation: str, target: str | None = None) -> None:
        require(operation in {"migrate", "validate", "info"}, "UNAUTHORIZED_FLYWAY_OPERATION")
        validate_staging_manifest(self.manifest_path, self.manifest_sha256)
        self.cluster.lifecycle.guard()
        env = safe_fixture_env()
        for key in ("JAVA_HOME", "FLYWAY_JAVA_CMD", "FLYWAY_LOCATIONS"):
            env.pop(key, None)
        home, tmp = self.cluster.root / "flyway-home", self.cluster.root / "flyway-tmp"
        home.mkdir(mode=0o700, exist_ok=True)
        tmp.mkdir(mode=0o700, exist_ok=True)
        env.update(HOME=str(home), TMPDIR=str(tmp), FLYWAY_USE_SYSTEM_PROXIES="false", LANG="C", LC_ALL="C",
                   JAVA_ARGS="-XX:-UsePerfData -Djava.io.tmpdir=" + str(tmp) + " -Duser.home=" + str(home))
        config = self.manifest_path.parent / "db/v2_2_1/flyway/flyway.conf"
        locations = self.manifest_path.parent / "db/v2_2_1/migration"
        bridge = _LoopbackUnixBridge(self.cluster.socket_dir / f".s.PGSQL.{self.cluster.port}")
        try:
            with bridge:
                require(bridge.bind_address is not None and bridge.bind_address[0] == "127.0.0.1", "NON_LOOPBACK_FLYWAY_BRIDGE")
                argv = [str(STABLE_INSTALL / "flyway"), "-configFiles=" + str(config),
                        "-locations=filesystem:" + str(locations),
                        f"-url=jdbc:postgresql://127.0.0.1:{bridge.bind_address[1]}/postgres?sslmode=disable",
                        "-user=propertyai_flyway", "-password="]
                if target is not None:
                    require(target in {"20260904.108", "20260904.111"}, "UNAPPROVED_MIGRATION_TARGET")
                    argv.append("-target=" + target)
                argv.append(operation)
                result = run_owned_command(argv, cwd=self.cluster.root, env=env, check=False, timeout=60)
                self.record("FLYWAY", argv=argv, exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr,
                            bridge_bind=bridge.bind_address, bridge_upstream=str(bridge.upstream),
                            config_sha256=self.manifest["flyway_config_path_bytes_sha256"]["sha256"],
                            staging_manifest_sha256=self.manifest_sha256)
                require(result.returncode == 0, "REAL_FLYWAY_" + operation.upper() + "_FAILED")
        finally:
            self.record("BRIDGE_CLOSED", closed=bridge.closed)
        validate_staging_manifest(self.manifest_path, self.manifest_sha256)

    def assert_baseline_history(self, history: list[dict]) -> None:
        wanted = self.manifest["baseline_history_checksums"]
        actual = {row["version"]: row for row in history if row["version"] in wanted}
        require(len(actual) == len(wanted) == 8, "BASELINE_HISTORY_COUNT")
        for version, checksum in wanted.items():
            require(actual[version]["checksum"] == checksum and actual[version]["success"] is True, "BASELINE_HISTORY_CHECKSUM_DRIFT")

    def restart(self) -> None:
        require(self.durability, "NONDURABLE_FIXTURE_CANNOT_PROVE_DURABILITY")
        lifecycle = self.cluster.lifecycle
        lifecycle.guard()
        pid = int((self.cluster.data_dir / "postmaster.pid").read_text().splitlines()[0])
        require(lifecycle._postmasters() == [pid], "RESTART_POSTMASTER_IDENTITY_UNKNOWN")
        history_before = self.history()
        result = run_owned_command([self.pg_ctl, "-D", str(self.cluster.data_dir), "-m", "fast", "-w", "-t", "15", "stop"], check=True, timeout=20)
        require(not lifecycle._postmasters() and not (self.cluster.data_dir / "postmaster.pid").exists(), "RESTART_STOP_NOT_PROVEN")
        self.record("DURABILITY_STOP", exit_code=result.returncode, stopped_pid=pid)
        _start_server(self)
        settings = self.introspection()["settings"][0]
        require(tuple(settings[2:]) == ("on", "on", "on"), "DURABILITY_SETTINGS_NOT_EFFECTIVE")
        require(self.history() == history_before, "HISTORY_CHANGED_ACROSS_RESTART")
        self.record("DURABILITY_RESTART", settings=settings)


def _start_server(fixture: RentFixture) -> None:
    c = fixture.cluster
    c.lifecycle.guard()
    options = f"-p {c.port} -k {c.socket_dir} -c listen_addresses='' -c fsync={'on' if fixture.durability else 'off'} -c synchronous_commit=on -c full_page_writes=on"
    result = run_owned_command([fixture.pg_ctl, "-D", str(c.data_dir), "-l", str(c.root / "postgres.log"),
                                "-o", options, "-w", "-t", "15", "start"], check=True, timeout=20, allow_daemon=True)
    fixture.record("START", argv=result.args, exit_code=result.returncode,
                   root=str(c.root), socket_dir=str(c.socket_dir), port=c.port, listen_addresses="")
    require(fixture.query("SELECT 1")[0][0] == 1, "POSTGRES_NOT_READY")


@contextmanager

def start_disposable_rent_postgres(resolved_staging_manifest_path: Path,
                                   expected_staging_manifest_sha256: str,
                                   approved_flyway_provenance_path: Path,
                                   expected_flyway_provenance_sha256: str,
                                   *, scenario: str = "FRESH", durability: bool = False) -> Iterator[RentFixture]:
    manifest_path = Path(resolved_staging_manifest_path)
    manifest = validate_staging_manifest(manifest_path, expected_staging_manifest_sha256)
    require(manifest["scenario"] == scenario and manifest["flyway_provenance_sha256"] == expected_flyway_provenance_sha256, "SCENARIO_OR_PROVENANCE_BINDING_MISMATCH")
    provenance = verify_flyway_provenance(Path(approved_flyway_provenance_path), expected_flyway_provenance_sha256)
    source_root = Path(__file__).resolve().parents[2]
    for key in ("baseline_harness_path_bytes_sha256", "fixture_lifecycle_path_bytes_sha256"):
        record = manifest[key]
        data = checked_bytes(source_root / record["path"], record["sha256"])
        require(len(data) == record["bytes"], "PROTECTED_BASELINE_HARNESS_DRIFT")
    try:
        initdb, pg_ctl = _postgres_bin("initdb"), _postgres_bin("pg_ctl")
    except pytest.skip.Exception as exc:
        raise RentPrerequisiteError("POSTGRES_CAPABILITY_UNAVAILABLE") from exc
    evidence_parent = Path(os.environ.get("PROPERTYAI_RENT_TEST_EVIDENCE_ROOT", str(manifest_path.parent.parent))).resolve()
    require(evidence_parent.is_dir(), "EVIDENCE_ROOT_MISSING")
    evidence_root = Path(tempfile.mkdtemp(prefix="rent-db-" + scenario.lower() + "-", dir=evidence_parent))
    lifecycle = FixtureLifecycle.create()
    c = DisposablePostgres(lifecycle.root, lifecycle.root / "data", lifecycle.root / "sock", _free_port(), getpass.getuser(), lifecycle=lifecycle)
    fixture = RentFixture(c, manifest_path, expected_staging_manifest_sha256, manifest, evidence_root, durability, pg_ctl)
    setup_complete = False
    primary_error: BaseException | None = None
    try:
        lifecycle.guard()
        c.socket_dir.mkdir(mode=0o700)
        lifecycle.record("INIT", "ATTEMPTED")
        result = run_owned_command([initdb, "-D", str(c.data_dir), "--auth=trust", "--no-locale", "--encoding=UTF8", "-U", c.superuser], check=True, timeout=25)
        fixture.record("INIT", argv=result.args, exit_code=result.returncode)
        lifecycle.record("INIT", "PASS")
        lifecycle.record("START", "ATTEMPTED")
        _start_server(fixture)
        lifecycle.record("READY", "PASS")
        settings = fixture.query("SELECT current_setting('server_version'),current_setting('listen_addresses'),current_setting('fsync'),current_setting('synchronous_commit'),current_setting('full_page_writes')")[0]
        require(settings[1] == "" and settings[3:] == ("on", "on"), "FIXTURE_ISOLATION_SETTINGS")
        if durability:
            require(settings[2] == "on", "DURABILITY_FSYNC_NOT_ENABLED")
        for version in ("001", "002"):
            record = next(x for x in manifest["ordered_inputs"] if x["version"] == version)
            bootstrap = manifest_path.parent / record["staged_relative_path"]
            result = c.psql(file=bootstrap)
            fixture.record("BOOTSTRAP_" + version, argv=result.args, exit_code=result.returncode, sha256=record["sha256"])
        lifecycle.record("RUN", "ATTEMPTED")
        baseline_history = None
        if scenario == "UPGRADE_108":
            fixture.flyway("migrate", "20260904.108")
            baseline_history = fixture.history()
            fixture.assert_baseline_history(baseline_history)
            require(len([r for r in baseline_history if r["version"] is not None]) == 8, "UPGRADE_BASELINE_EXTRA_MIGRATION")
            fixture.record("UPGRADE_BASELINE_108", history=baseline_history)
        fixture.flyway("migrate", "20260904.111")
        fixture.flyway("validate")
        history = fixture.history()
        fixture.assert_baseline_history(history)
        require([r["version"] for r in history if r["version"] is not None] == ["20260904." + str(i) for i in range(101, 112)], "FINAL_HISTORY_VERSIONS")
        require(all(r["success"] for r in history), "UNSUCCESSFUL_MIGRATION_HISTORY")
        if baseline_history is not None:
            require(history[:len(baseline_history)] == baseline_history, "UPGRADE_CHANGED_PRIOR_HISTORY_ROWS")
        fixture.record("POST_MIGRATION_INTROSPECTION", **fixture.introspection())
        lifecycle.record("RUN", "PASS")
        setup_complete = True
        yield fixture
    except BaseException as exc:
        primary_error = exc
        fixture.record("FAILURE", setup_complete=setup_complete, error_type=type(exc).__name__, reason=str(exc))
        try:
            fixture.record("FAILURE_DATABASE_STATE", **fixture.introspection())
        except Exception as observed:
            fixture.record("FAILURE_STATE_UNAVAILABLE", reason=type(observed).__name__)
        raise
    finally:
        try:
            validate_staging_manifest(manifest_path, expected_staging_manifest_sha256)
        except Exception as drift:
            lifecycle.fail("INPUT_RECHECK", str(drift))
        cleanup = lifecycle.finish(pg_ctl)
        fixture.cleanup_report = cleanup
        fixture.record("CLEANUP", report=cleanup)
        terminal = {"scenario": scenario, "setup_complete": setup_complete, "primary_error": None if primary_error is None else str(primary_error),
                    "staging_manifest_sha256": expected_staging_manifest_sha256, "provenance_sha256": expected_flyway_provenance_sha256,
                    "owned_fixture_root": str(c.root), "cleanup": cleanup, "production_effect": False, "observations": fixture.observations}
        data = canonical(terminal)
        with (evidence_root / "terminal.json").open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for artifact in evidence_root.iterdir():
            if artifact.is_file():
                artifact.chmod(0o444)
        if cleanup["classification"] != "PASS" and primary_error is None:
            raise FixtureLifecycleError(cleanup["classification"], "CLEANUP", "RENT_FIXTURE_CLEANUP_NOT_PASS", cleanup)
