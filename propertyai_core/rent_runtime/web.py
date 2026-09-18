"""Packaged loopback Web process composed from the accepted I2 interfaces."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import signal
import stat
from uuid import UUID, uuid4

import psycopg

from propertyai_core.adapters.postgres.rent_session_store import PostgresSessionStore
from propertyai_core.application.handlers.rent import BusinessDateProvider
from propertyai_core.application.rent_errors import RentError, rent_error
from propertyai_core.web.auth_context import AuthorizedPrincipal, ServerPrincipalDirectory
from propertyai_core.web.auth_session import RequestAuthenticator, SessionService, session_cookie
from propertyai_core.web.empty_shell import EmptyShell
from propertyai_core.web.rent_api import RentAPI
from propertyai_core.web.rent_integration import (AuthoritativeEmptyShellLoader, PrincipalRentServiceResolver,
                                               RentDatabaseBinding, StaticRentDatabaseBindings)
from propertyai_core.web.rent_longstay_admin import RentLongstayAdmin
from propertyai_core.web.rent_operations import RentOperationsUI
from propertyai_core.web.rent_server import handler_class
from .common import LocalTarget, OperationalError, canonical, checked_file, observe, require, write_new
from .local_auth import PersistentStagingLocalIssuer
from .staging import (ISOLATED_TEST, PERSISTENT_STAGING, PersistentPostgresTarget,
                      persistent_target, validate_persistent_common)

_LOCAL_LOGIN_PATH = "/auth/login/local"
_LOCAL_LOGIN_FORBIDDEN_HEADERS = (
    "Authorization", "Cookie", "X-Organization-ID", "X-Actor-Party-ID",
    "X-Subject", "X-Capabilities", "X-CSRF-Token",
)


@dataclass(frozen=True, repr=False)
class RuntimeConfig:
    target: LocalTarget | PersistentPostgresTarget
    web_login: str
    session_login: str
    principal: AuthorizedPrincipal
    port: int
    environment: str = ISOLATED_TEST
    state_dir: Path | None = None
    local_issuer: PersistentStagingLocalIssuer | None = None


def load_config(path: Path, sha256: str) -> RuntimeConfig:
    path = Path(path)
    require(path.is_absolute() and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "CONFIG_INVALID")
    raw = json.loads(checked_file(path, sha256))
    require(isinstance(raw, dict), "CONFIG_INVALID")
    if raw.get("environment") == ISOLATED_TEST:
        keys = {"environment", "target_root", "target_marker_sha256", "pg_port", "web_login", "session_login",
                "principal", "bind_host", "bind_port", "scheduler", "load_fixtures"}
        require(set(raw) == keys and raw["scheduler"] == "OFF"
                and raw["load_fixtures"] is False and raw["bind_host"] == "127.0.0.1", "CONFIG_INVALID")
        require(type(raw["bind_port"]) is int and (raw["bind_port"] == 0 or 1024 <= raw["bind_port"] <= 65535), "CONFIG_INVALID")
        target = LocalTarget(Path(raw["target_root"]), raw["target_marker_sha256"], raw["pg_port"])
        target.guard()
        for key in ("web_login", "session_login"):
            require(isinstance(raw[key], str) and re.fullmatch(r"rent_(test|session)_[a-z0-9_]{1,40}", raw[key]) is not None,
                    "CONFIG_INVALID")
        require(raw["web_login"] != raw["session_login"], "CONFIG_INVALID")
        p = raw["principal"]
        require(set(p) == {"organization_id", "actor_party_id", "subject", "capabilities"}
                and isinstance(p["subject"], str) and p["subject"].startswith("synthetic:"), "CONFIG_INVALID")
        principal = AuthorizedPrincipal(UUID(p["organization_id"]), UUID(p["actor_party_id"]), p["subject"], frozenset(p["capabilities"]))
        require(principal.capabilities <= {"READ", "WRITE"} and "READ" in principal.capabilities, "CONFIG_INVALID")
        return RuntimeConfig(target, raw["web_login"], raw["session_login"], principal, raw["bind_port"])

    keys = {"environment", "target_id", "database_connection_refs", "runtime_role", "auth_binding",
            "bind_host", "bind_port", "runtime_state_dir", "scheduler", "load_fixtures",
            "real_business_data_expected", "external_activation"}
    require(set(raw) == keys and raw.get("environment") == PERSISTENT_STAGING
            and raw["load_fixtures"] is False and raw["bind_host"] == "127.0.0.1", "CONFIG_INVALID")
    target_id = validate_persistent_common(raw, runtime_role="WEB")
    require(type(raw["bind_port"]) is int and (raw["bind_port"] == 0 or 1024 <= raw["bind_port"] <= 65535), "CONFIG_INVALID")
    target = persistent_target(target_id, raw["database_connection_refs"], required_roles={"WEB", "SESSION"})
    target.expected_database("WEB")
    auth = raw["auth_binding"]
    require(isinstance(auth, dict) and set(auth) == {"kind", "issuer", "principal"}
            and auth["kind"] == "PROVIDER_VERIFIED_SUBJECT_DIRECTORY" and isinstance(auth["principal"], dict),
            "CONFIG_INVALID")
    issuer_config = auth["issuer"]
    require(isinstance(issuer_config, dict) and set(issuer_config) == {"kind"}, "CONFIG_INVALID")
    p = auth["principal"]
    require(set(p) == {"organization_id", "actor_party_id", "subject", "capabilities"}
            and isinstance(p["subject"], str) and not p["subject"].startswith("synthetic:"), "CONFIG_INVALID")
    principal = AuthorizedPrincipal(UUID(p["organization_id"]), UUID(p["actor_party_id"]), p["subject"], frozenset(p["capabilities"]))
    require(principal.capabilities <= {"READ", "WRITE"} and "READ" in principal.capabilities, "CONFIG_INVALID")
    local_issuer = PersistentStagingLocalIssuer(
        environment=raw["environment"],
        auth_binding_kind=auth["kind"],
        issuer_kind=issuer_config["kind"],
        bind_host=raw["bind_host"],
        actor_party_id=principal.actor_party_id,
        allowlisted_subject=principal.subject,
    )
    state_dir = Path(raw["runtime_state_dir"])
    require(state_dir.is_absolute() and not state_dir.is_symlink() and state_dir.resolve(strict=True) == state_dir
            and state_dir.is_dir() and stat.S_IMODE(state_dir.stat().st_mode) & 0o077 == 0, "CONFIG_INVALID")
    return RuntimeConfig(target, "WEB", "SESSION", principal, raw["bind_port"], PERSISTENT_STAGING, state_dir, local_issuer)


def role_check(conn, group: str) -> None:
    row = conn.execute("SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls, "
                       "pg_has_role(session_user,%s,'USAGE'), "
                       "pg_has_role(session_user,'propertyai_owner','MEMBER'), "
                       "pg_has_role(session_user,'propertyai_migrator','MEMBER'), "
                       "pg_has_role(session_user,'propertyai_rent_scheduler','MEMBER') "
                       "FROM pg_roles WHERE rolname=session_user", (group,)).fetchone()
    require(row == (True, False, False, False, False, False, True, False, False, False), "CONFIG_INVALID")


def readiness(config: RuntimeConfig, manifest: dict) -> str:
    try:
        config.target.guard()
        expected_database = ("postgres" if isinstance(config.target, LocalTarget)
                             else config.target.expected_database(config.web_login))
        if (isinstance(config.target, LocalTarget)
                and ((config.target.root / "w3b-restore-pending.json").exists()
                     or (config.target.root / "w3b-restore-pending.json").is_symlink())):
            return "MIGRATION_NOT_READY"
        with config.target.connect(config.session_login) as conn:
            role_check(conn, "propertyai_app_runtime")
            rows = conn.execute("SELECT version,script,checksum,success FROM propertyai.flyway_schema_history "
                                "ORDER BY installed_rank").fetchall()
            expected = [(m["version"], m["script"], m["checksum"], True) for m in manifest["migrations"]]
            if rows != expected:
                return "MIGRATION_NOT_READY"
            conn.execute("SELECT * FROM propertyai.rent_auth_session_get(%s)", ("0" * 64,)).fetchall()
        with config.target.connect(config.web_login) as conn:
            role_check(conn, "propertyai_rent_runtime")
            row = conn.execute("SELECT propertyai.rent_visible_org(),inet_server_addr(),current_database()").fetchone()
            require(row == (config.principal.organization_id, None, expected_database), "CONFIG_INVALID")
            conn.execute("SELECT count(*) FROM propertyai.rent_contract").fetchone()
        return "APPLICATION_READY"
    except (psycopg.OperationalError, psycopg.InterfaceError):
        return "DATABASE_UNAVAILABLE"
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedFunction):
        return "MIGRATION_NOT_READY"
    except Exception:
        return "CONFIG_INVALID"


def compose(config: RuntimeConfig, manifest: dict) -> ThreadingHTTPServer:
    require(
        config.principal.capabilities <= {"READ", "WRITE"}
        and "READ" in config.principal.capabilities,
        "CONFIG_INVALID",
    )
    if config.environment == PERSISTENT_STAGING:
        require(
            isinstance(config.local_issuer, PersistentStagingLocalIssuer)
            and config.local_issuer.verified_subject() == config.principal.subject,
            "CONFIG_INVALID",
        )
    else:
        require(config.environment == ISOLATED_TEST and config.local_issuer is None, "CONFIG_INVALID")
    directory = ServerPrincipalDirectory([config.principal])
    sessions = SessionService(PostgresSessionStore(lambda: config.target.connect(config.session_login)), directory)
    binding = RentDatabaseBinding(config.principal.organization_id, config.principal.actor_party_id,
                                  lambda: config.target.connect(config.web_login))
    services = PrincipalRentServiceResolver(StaticRentDatabaseBindings({config.principal.subject: binding}), BusinessDateProvider(lambda name: datetime.now(ZoneInfo(name)).date()))
    auth = RequestAuthenticator(sessions)
    api = RentAPI(authenticator=auth, services=services)
    base = handler_class(api, shell=EmptyShell(auth, AuthoritativeEmptyShellLoader(services)),
                         longstay_admin=RentLongstayAdmin(), operations=RentOperationsUI(api))

    class Handler(base):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_error(self, *args):
            observe("WEB", "HTTP_SERVER_ERROR", error_class="INTERNAL_ERROR")

        def _send(self, status, content, mime, extra=None):
            event = "HTTP_OK" if status < 400 else "HTTP_CLIENT_ERROR" if status < 500 else "HTTP_SERVER_ERROR"
            observe("WEB", event, correlation_id=getattr(self, "operation_id", None))
            super()._send(status, content, mime, extra)

        def _handle_local_login(self, method: str) -> bool:
            if self.path != _LOCAL_LOGIN_PATH or method != "POST":
                return False
            try:
                if config.environment != PERSISTENT_STAGING or config.local_issuer is None:
                    raise rent_error("NOT_FOUND")
                if self.headers.get_all("Transfer-Encoding"):
                    raise rent_error("NOT_AUTHORIZED")
                lengths = self.headers.get_all("Content-Length") or []
                if len(lengths) > 1 or (lengths and lengths[0].strip() != "0"):
                    raise rent_error("NOT_AUTHORIZED")
                if any(self.headers.get_all(name) for name in _LOCAL_LOGIN_FORBIDDEN_HEADERS):
                    raise rent_error("NOT_AUTHORIZED")
                issued = sessions.issue(config.local_issuer.verified_subject())
                self._send(
                    303,
                    b"",
                    "text/plain; charset=utf-8",
                    {"Location": "/app", "Set-Cookie": session_cookie(issued)},
                )
                return True
            except RentError as exc:
                self._error(exc)
                return True
            except Exception:
                self._error(rent_error("INTERNAL_ERROR"))
                return True

        def _handle(self, method):
            self.operation_id = str(uuid4())
            if self.path == "/health/live" and method in {"GET", "HEAD"}:
                state = "PROCESS_ALIVE"
            else:
                state = readiness(config, manifest)
            if self.path in {"/health/live", "/health/ready"} and method in {"GET", "HEAD"}:
                okay = state in {"PROCESS_ALIVE", "APPLICATION_READY"}
                observe("WEB", state, error_class="NONE" if okay else state, correlation_id=self.operation_id)
                self._send(200 if okay else 503, canonical({"result_class": state, "scheduler": "OFF"}), "application/json")
            elif state != "APPLICATION_READY":
                observe("WEB", state, error_class=state, correlation_id=self.operation_id)
                self._send(503, canonical({"result_class": state}), "application/json")
            elif self._handle_local_login(method):
                return
            else:
                super()._handle(method)

    class Server(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True

        def handle_error(self, request, client_address):
            observe("WEB", "HTTP_SERVER_ERROR", error_class="INTERNAL_ERROR")

    server = Server(("127.0.0.1", config.port), Handler)
    server.timeout = 0.25
    return server


def serve(config: RuntimeConfig, manifest: dict, state_path: Path) -> int:
    state_path = Path(state_path)
    if config.environment == ISOLATED_TEST:
        require(isinstance(config.target, LocalTarget) and state_path.parent == config.target.root
                and state_path.name.startswith("w3b-web-") and state_path.suffix == ".json", "CONFIG_INVALID")
    else:
        require(config.environment == PERSISTENT_STAGING and config.state_dir is not None
                and state_path.is_absolute() and state_path.parent == config.state_dir
                and state_path.name.startswith("rent-web-") and state_path.suffix == ".json", "CONFIG_INVALID")
    config.target.guard()
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    server = compose(config, manifest)
    run_id = str(uuid4())
    state_bytes = canonical({"port": server.server_address[1], "artifact_id": manifest["artifact_id"], "scheduler": "OFF", "run_id": run_id})
    written = False
    old_signals = {}
    try:
        write_new(state_path, state_bytes)
        written = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_signals[sig] = signal.signal(sig, stop)
        observe("WEB", "STARTED", correlation_id=run_id)
        while not stopping:
            server.handle_request()
    finally:
        server.server_close()
        for sig, old in old_signals.items():
            signal.signal(sig, old)
        if written and checked_file(state_path) == state_bytes:
            state_path.unlink()
        observe("WEB", "STOPPED", correlation_id=run_id)
    return 0
