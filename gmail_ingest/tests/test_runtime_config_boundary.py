from __future__ import annotations

import inspect
import json
import plistlib
from pathlib import Path

import pytest

from gmail_ingest import booking_bridge, poll_once, preflight_dry_run
from gmail_ingest.runtime_config import (
    GMAIL_INGEST_CONFIG_PATH_ENV,
    GmailIngestRuntimeConfigError,
    load_gmail_ingest_config,
    resolve_gmail_ingest_config_path,
)


ROOT = Path(__file__).resolve().parents[2]


def _write_config(path: Path, *, automation_bridge_enabled: bool = True) -> dict:
    payload = {
        "schema_version": 1,
        "mode": "shadow",
        "provider": "gmail_readonly",
        "query": "label:propertyai-test",
        "max_results_per_poll": 1,
        "automation_bridge_enabled": automation_bridge_enabled,
        "external_writes_enabled": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return payload


def test_runtime_config_requires_explicit_external_authority() -> None:
    with pytest.raises(
        GmailIngestRuntimeConfigError,
        match=f"GMAIL_INGEST_CONFIG_PATH_MISSING:{GMAIL_INGEST_CONFIG_PATH_ENV}",
    ):
        resolve_gmail_ingest_config_path({})


def test_runtime_config_rejects_relative_missing_directory_and_symlink_paths(tmp_path: Path) -> None:
    for raw in ("~/config.json", "./config.json", "config.json", "gmail_ingest/config.json"):
        with pytest.raises(
            GmailIngestRuntimeConfigError, match="GMAIL_INGEST_CONFIG_PATH_NOT_ABSOLUTE"
        ):
            resolve_gmail_ingest_config_path({GMAIL_INGEST_CONFIG_PATH_ENV: raw})

    missing = tmp_path / "missing.json"
    with pytest.raises(GmailIngestRuntimeConfigError, match="GMAIL_INGEST_CONFIG_PATH_NOT_FOUND"):
        resolve_gmail_ingest_config_path({GMAIL_INGEST_CONFIG_PATH_ENV: str(missing)})

    with pytest.raises(GmailIngestRuntimeConfigError, match="GMAIL_INGEST_CONFIG_PATH_NOT_FILE"):
        resolve_gmail_ingest_config_path({GMAIL_INGEST_CONFIG_PATH_ENV: str(tmp_path)})

    target = tmp_path / "external" / "config.json"
    _write_config(target)
    link = tmp_path / "config-link.json"
    link.symlink_to(target)
    with pytest.raises(GmailIngestRuntimeConfigError, match="GMAIL_INGEST_CONFIG_PATH_SYMLINK_FORBIDDEN"):
        resolve_gmail_ingest_config_path({GMAIL_INGEST_CONFIG_PATH_ENV: str(link)})


def test_runtime_config_loads_same_external_authority_independent_of_cwd(tmp_path: Path, monkeypatch) -> None:
    external = tmp_path / "runtime-authority" / "gmail-config.json"
    expected = _write_config(external)
    environment = {GMAIL_INGEST_CONFIG_PATH_ENV: str(external)}

    first_cwd = tmp_path / "working-tree"
    second_cwd = tmp_path / "materialized-release"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = load_gmail_ingest_config(environment)
    first_path = resolve_gmail_ingest_config_path(environment)
    monkeypatch.chdir(second_cwd)
    second = load_gmail_ingest_config(environment)
    second_path = resolve_gmail_ingest_config_path(environment)

    assert first == expected == second
    assert first_path == external.resolve() == second_path


def test_runtime_config_json_is_fail_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not-json}\n", encoding="utf-8")
    with pytest.raises(GmailIngestRuntimeConfigError, match="GMAIL_INGEST_CONFIG_JSON_INVALID"):
        load_gmail_ingest_config({GMAIL_INGEST_CONFIG_PATH_ENV: str(invalid)})

    wrong_shape = tmp_path / "wrong-shape.json"
    wrong_shape.write_text("[]\n", encoding="utf-8")
    with pytest.raises(GmailIngestRuntimeConfigError, match="GMAIL_INGEST_CONFIG_OBJECT_REQUIRED"):
        load_gmail_ingest_config({GMAIL_INGEST_CONFIG_PATH_ENV: str(wrong_shape)})


def test_w01_source_plist_declares_external_config_path_cutover_contract() -> None:
    plist = plistlib.loads((ROOT / "gmail_ingest" / "com.propertyai.gmail-readonly.plist").read_bytes())
    configured = plist["EnvironmentVariables"][GMAIL_INGEST_CONFIG_PATH_ENV]

    assert configured == "/REPLACE_AT_CUTOVER/propertyai/gmail-ingest-config"
    assert "/Users/kate/" not in configured
    assert plist["ProgramArguments"][1:] == ["-m", "gmail_ingest.poll_once"]


def test_w01_main_resolves_config_then_publishes_identity_before_business(
    tmp_path: Path, monkeypatch
) -> None:
    external = tmp_path / "external-runtime" / "config.json"
    _write_config(external, automation_bridge_enabled=True)
    monkeypatch.setenv(GMAIL_INGEST_CONFIG_PATH_ENV, str(external))
    calls: list[object] = []

    monkeypatch.setattr(
        poll_once,
        "publish_startup_runtime_identity",
        lambda writer: calls.append(("runtime_identity", writer)),
    )
    monkeypatch.setattr(poll_once, "poll", lambda: calls.append("business_poll"))

    poll_once.main()

    assert calls == [("runtime_identity", "W01"), "business_poll"]


def test_w01_missing_config_fails_before_identity_or_business(monkeypatch) -> None:
    monkeypatch.delenv(GMAIL_INGEST_CONFIG_PATH_ENV, raising=False)
    calls: list[object] = []
    monkeypatch.setattr(
        poll_once,
        "publish_startup_runtime_identity",
        lambda writer: calls.append(("runtime_identity", writer)),
    )
    monkeypatch.setattr(poll_once, "poll", lambda: calls.append("business_poll"))

    with pytest.raises(GmailIngestRuntimeConfigError, match="GMAIL_INGEST_CONFIG_PATH_MISSING"):
        poll_once.main()

    assert calls == []


def test_all_w01_config_consumers_use_the_shared_external_resolver() -> None:
    for module in (poll_once, booking_bridge, preflight_dry_run):
        source = inspect.getsource(module)
        assert "load_gmail_ingest_config" in source
        assert '"config.json"' not in source
