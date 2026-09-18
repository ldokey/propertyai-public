#!/usr/bin/env python3
"""Redacted health monitor for PropertyAI's local automation services."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from propertyai_core.runtime.cleaner_topology import topology_contract_from_env
from propertyai_core.global_writer import (
    ProductionWriterError,
    assert_current_production_writer,
    mutation_scope,
    publish_startup_runtime_identity,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "outputs" / "system_health"
STATE_PATH = OUTPUT_DIR / "state.json"
LATEST_PATH = OUTPUT_DIR / "latest.json"
REBOOT_TEST_PATH = OUTPUT_DIR / "reboot-test.json"
NOTION_PAGE_ID = "3b03f2c8-cf47-8180-8ee7-c08eef1848fa"
NOTION_TOKEN_PATH = ROOT / "secrets" / "notion" / "token"
TELEGRAM_TOKEN_PATH = ROOT / "secrets" / "telegram" / "bot-token"
OPERATOR_PATH = ROOT / "secrets" / "telegram" / "operator.json"
KST = ZoneInfo("Asia/Seoul")

CLEANER_TOPOLOGY = topology_contract_from_env()
RETIRED_RECOVERY_LABELS = CLEANER_TOPOLOGY.retired_labels
TELEGRAM_RUNTIME_SERVICES = {
    "telegram_ops": ("com.propertyai.telegram-ops", True),
}
_BASE_SERVICES = {
    "ollama": ("com.ollama.ollama", True),
    "openclaw_gateway": ("ai.openclaw.gateway", True),
    **TELEGRAM_RUNTIME_SERVICES,
}
SERVICES = {**_BASE_SERVICES, **CLEANER_TOPOLOGY.services}
RECOVERY_TARGETS = {
    "com.ollama.ollama": {"SERVICE_OLLAMA", "OLLAMA_API"},
    "ai.openclaw.gateway": {"SERVICE_OPENCLAW_GATEWAY", "OPENCLAW_LOCAL"},
    "com.propertyai.telegram-ops": {"SERVICE_TELEGRAM_OPS"},
    **{label: set(codes) for label, codes in CLEANER_TOPOLOGY.recovery_targets.items()},
}
RECOVERY_MIN_FAILURES = 2
RECOVERY_COOLDOWN = timedelta(minutes=30)
RECOVERY_WINDOW = timedelta(hours=6)
RECOVERY_MAX_ATTEMPTS = 3
REQUIRED_MODELS = {"qwen3:4b", "qwen3:30b", "qwen3-embedding:0.6b"}
REQUIRED_GOOGLE_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
}
SECRET_PATHS = [
    ROOT / "secrets" / "google" / "credentials.json",
    ROOT / "secrets" / "google" / "token.json",
    ROOT / "secrets" / "google" / "drive-token.json",
    NOTION_TOKEN_PATH,
    TELEGRAM_TOKEN_PATH,
    ROOT / "secrets" / "telegram" / "action-secret",
    ROOT / "secrets" / "telegram" / "cleaners.json",
    OPERATOR_PATH,
]
_ALL_SCHEDULER_LOGS = {
    "gmail_poller": ROOT / "gmail_ingest" / "runtime" / "poller.log",
    "cleaning_operations": ROOT / "telegram_approval" / "runtime" / "cleaning-operations.out.log",
    "cleaning_completion": ROOT / "telegram_approval" / "runtime" / "completion-scheduler.log",
    "cleaner_pg_outbox": ROOT / "propertyai_core" / "runtime" / "cleaner-pg-outbox.out.log",
}
_ALL_ERROR_LOGS = {
    "gmail_poller": ROOT / "gmail_ingest" / "runtime" / "poller-error.log",
    "telegram_ops": ROOT / "telegram_approval" / "runtime" / "ops" / "bot-error.log",
    "telegram_cleaner": ROOT / "telegram_approval" / "runtime" / "cleaner" / "bot-error.log",
    "cleaning_operations": ROOT / "telegram_approval" / "runtime" / "cleaning-operations.err.log",
    "cleaning_completion": ROOT / "telegram_approval" / "runtime" / "completion-scheduler-error.log",
    "cleaner_pg_outbox": ROOT / "propertyai_core" / "runtime" / "cleaner-pg-outbox.err.log",
}
SCHEDULER_LOGS = {name: path for name, path in _ALL_SCHEDULER_LOGS.items() if name in CLEANER_TOPOLOGY.scheduler_logs}
ERROR_LOGS = {
    name: path
    for name, path in _ALL_ERROR_LOGS.items()
    if name == "telegram_ops" or name in CLEANER_TOPOLOGY.error_logs
}



def atomic_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _check(code: str, ok: bool, severity: str, detail: str) -> dict:
    return {"code": code, "ok": ok, "severity": severity if not ok else "INFO", "detail": detail}


def _http_json(url: str, *, method: str = "GET", body: dict | None = None, headers: dict | None = None, timeout: int = 8) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        method=method,
        headers=headers or {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def check_launch_services() -> list[dict]:
    checks = []
    for code, (label, requires_pid) in SERVICES.items():
        result = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=8)
        output = result.stdout + result.stderr
        pid = re.search(r'"PID"\s*=\s*(\d+)', output)
        exit_status = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', output)
        loaded = result.returncode == 0
        running = bool(pid) if requires_pid else loaded and (not exit_status or exit_status.group(1) == "0")
        ok = loaded and running
        detail = "running" if pid else ("loaded, last exit 0" if ok else "not running")
        checks.append(_check(f"SERVICE_{code.upper()}", ok, "CRITICAL", detail))
    return checks


def check_local_endpoints() -> list[dict]:
    checks = []
    try:
        tags = _http_json("http://127.0.0.1:11434/api/tags")
        checks.append(_check("OLLAMA_API", True, "CRITICAL", "local API reachable"))
        models = {item.get("name") for item in tags.get("models", [])}
        missing = sorted(REQUIRED_MODELS - models)
        checks.append(_check("OLLAMA_MODELS", not missing, "CRITICAL", "required models present" if not missing else f"missing: {', '.join(missing)}"))
    except Exception as exc:
        checks.append(_check("OLLAMA_API", False, "CRITICAL", f"local API unavailable: {type(exc).__name__}"))
        checks.append(_check("OLLAMA_MODELS", False, "CRITICAL", "model inventory unavailable"))
    try:
        with urllib.request.urlopen("http://127.0.0.1:18789/", timeout=5) as response:
            ok = 200 <= response.status < 500
        checks.append(_check("OPENCLAW_LOCAL", ok, "CRITICAL", "local endpoint reachable" if ok else "local endpoint unavailable"))
    except Exception as exc:
        checks.append(_check("OPENCLAW_LOCAL", False, "CRITICAL", f"local endpoint unavailable: {type(exc).__name__}"))
    return checks


def check_tokens_and_permissions() -> list[dict]:
    checks = []
    token_path = ROOT / "secrets" / "google" / "token.json"
    try:
        token = json.loads(token_path.read_text())
        scopes = set(token.get("scopes") or [])
        ok = REQUIRED_GOOGLE_SCOPES.issubset(scopes) and bool(token.get("refresh_token"))
        checks.append(_check("GOOGLE_OAUTH", ok, "CRITICAL", "required scopes and refresh capability present" if ok else "scope or refresh capability missing"))
    except Exception as exc:
        checks.append(_check("GOOGLE_OAUTH", False, "CRITICAL", f"token unreadable: {type(exc).__name__}"))
    bad = []
    missing = []
    for path in SECRET_PATHS:
        if not path.exists():
            missing.append(path.name)
        elif path.stat().st_mode & 0o777 != 0o600:
            bad.append(path.name)
    ok = not bad and not missing
    detail = "all secret files mode 600" if ok else f"missing={missing}; wrong_mode={bad}"
    checks.append(_check("SECRET_FILE_PERMISSIONS", ok, "CRITICAL", detail))
    return checks


def check_scheduler_logs(now: datetime) -> list[dict]:
    checks = []
    for code, path in SCHEDULER_LOGS.items():
        if not path.exists():
            checks.append(_check(f"LOG_FRESH_{code.upper()}", False, "CRITICAL", "log missing"))
            continue
        age = now.timestamp() - path.stat().st_mtime
        ok = age <= 15 * 60
        checks.append(_check(f"LOG_FRESH_{code.upper()}", ok, "CRITICAL", f"age_seconds={max(0, int(age))}"))
    for code, path in ERROR_LOGS.items():
        if not path.exists():
            checks.append(_check(f"ERROR_LOG_{code.upper()}", False, "WARNING", "error log missing"))
            continue
        recent_nonempty = path.stat().st_size > 0 and now.timestamp() - path.stat().st_mtime <= 15 * 60
        checks.append(_check(f"ERROR_LOG_{code.upper()}", not recent_nonempty, "WARNING", "no recent errors" if not recent_nonempty else "recent error output present"))
    return checks


def check_disk() -> list[dict]:
    free_gib = shutil.disk_usage(ROOT).free / (1024 ** 3)
    if free_gib < 20:
        return [_check("DISK_FREE", False, "CRITICAL", f"free_gib={free_gib:.1f}")]
    if free_gib < 40:
        return [_check("DISK_FREE", False, "WARNING", f"free_gib={free_gib:.1f}")]
    return [_check("DISK_FREE", True, "INFO", f"free_gib={free_gib:.1f}")]


def build_report(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    checks = []
    checks.extend(check_launch_services())
    checks.extend(check_local_endpoints())
    checks.extend(check_tokens_and_permissions())
    checks.extend(check_scheduler_logs(now))
    checks.extend(check_disk())
    critical = [item["code"] for item in checks if not item["ok"] and item["severity"] == "CRITICAL"]
    warnings = [item["code"] for item in checks if not item["ok"] and item["severity"] == "WARNING"]
    overall = "DOWN" if critical else ("DEGRADED" if warnings else "HEALTHY")
    fingerprint_source = json.dumps({
        "overall": overall,
        "critical": critical,
        "warnings": warnings,
        "check_contract": sorted(item["code"] for item in checks),
    }, sort_keys=True)
    return {
        "schema_version": 1,
        "run_at": now.astimezone(KST).isoformat(),
        "overall_health": overall,
        "critical_codes": critical,
        "warning_codes": warnings,
        "checks_passed": sum(1 for item in checks if item["ok"]),
        "checks_total": len(checks),
        "checks": checks,
        "fingerprint": hashlib.sha256(fingerprint_source.encode()).hexdigest()[:16],
        "contains_secrets": False,
        "external_effects": {"telegram_messages": 0, "notion_updates": 0},
    }


def _telegram_message(report: dict, *, recovery: bool) -> str:
    if recovery:
        return (
            "✅ PropertyAI 로컬 자동화 복구\n\n"
            f"현재 상태: {report['overall_health']}\n"
            f"점검: {report['checks_passed']}/{report['checks_total']}\n"
            f"확인 시각: {report['run_at']}"
        )
    codes = report["critical_codes"] + report["warning_codes"]
    return (
        "🚨 PropertyAI 로컬 자동화 상태 변경\n\n"
        f"현재 상태: {report['overall_health']}\n"
        f"이상 항목: {', '.join(codes) if codes else '없음'}\n"
        f"점검: {report['checks_passed']}/{report['checks_total']}\n"
        "예약 취소·지급·삭제는 자동 실행하지 않았습니다.\n"
        f"확인 시각: {report['run_at']}"
    )


def send_telegram(report: dict, *, recovery: bool) -> None:
    send_telegram_text(_telegram_message(report, recovery=recovery))


def send_telegram_text(text: str) -> None:
    token = TELEGRAM_TOKEN_PATH.read_text().strip()
    operator = json.loads(OPERATOR_PATH.read_text())
    data = urllib.parse.urlencode({"chat_id": operator["telegram_chat_id"], "text": text}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    assert_current_production_writer()
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError("Telegram health alert failed")


def _recovery_message(report: dict, actions: list[dict], *, reboot_recommended: bool) -> str:
    lines = [
        "🛠 PropertyAI 자동 복구 실행",
        "",
        f"점검 상태: {report['overall_health']}",
    ]
    for action in actions:
        lines.append(f"- {action['service']}: {action['result']}")
    if reboot_recommended:
        lines.extend([
            "",
            "동일 서비스 복구가 반복 실패했습니다. Mac 재부팅을 권장하지만 자동 재부팅은 실행하지 않았습니다.",
            "FileVault가 켜져 있어 재부팅 후 최초 사용자 로그인이 필요합니다.",
        ])
    lines.extend(["", f"확인 시각: {report['run_at']}"])
    return "\n".join(lines)


def _launch_service_readback(service: str) -> dict:
    result = subprocess.run(
        ["launchctl", "list", service],
        capture_output=True,
        text=True,
        timeout=8,
    )
    output = result.stdout + result.stderr
    pid_match = re.search(r'"PID"\s*=\s*(\d+)', output)
    exit_match = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', output)
    return {
        "loaded": result.returncode == 0,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "last_exit_status": int(exit_match.group(1)) if exit_match else None,
    }


def perform_recovery(service: str) -> dict:
    if service in RETIRED_RECOVERY_LABELS:
        raise RuntimeError("retired mixed Telegram runtime cannot be recovered")
    if service not in RECOVERY_TARGETS:
        raise RuntimeError("service is not an explicit recovery target")
    before = _launch_service_readback(service)
    assert_current_production_writer()
    kick_error: Exception | None = None
    returncode: int | None = None
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{service}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        returncode = result.returncode
    except Exception as error:
        kick_error = error
    # A fresh readback is mandatory even when kickstart returns ambiguously.
    after = _launch_service_readback(service)
    if kick_error is not None:
        result_name = "STARTED_READBACK" if after["loaded"] else "FAILED_READBACK"
    else:
        result_name = "STARTED" if returncode == 0 else "FAILED"
    return {
        "service": service,
        "result": result_name,
        "launchctl_returncode": returncode,
        "readback_before": before,
        "readback_after": after,
        "kick_error_type": type(kick_error).__name__ if kick_error is not None else None,
    }


def recovery_decision(report: dict, previous: dict | None, current_at: datetime, *, send: bool) -> tuple[dict, dict, list[str]]:
    previous = previous or {}
    previous_counts = previous.get("failure_counts") or {}
    previous_attempts = previous.get("recovery_attempts") or {}
    active_codes = set(report["critical_codes"])
    failure_counts = {}
    attempts_state = {}
    actions = []
    errors = []
    reboot_services = []
    for service, codes in RECOVERY_TARGETS.items():
        active = bool(active_codes & codes)
        failure_counts[service] = int(previous_counts.get(service, 0)) + 1 if active else 0
        timestamps = []
        for value in previous_attempts.get(service, []):
            try:
                parsed = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue
            if current_at - parsed <= RECOVERY_WINDOW:
                timestamps.append(parsed)
        if active and failure_counts[service] >= RECOVERY_MIN_FAILURES:
            cooldown_ok = not timestamps or current_at - timestamps[-1] >= RECOVERY_COOLDOWN
            if len(timestamps) >= RECOVERY_MAX_ATTEMPTS:
                reboot_services.append(service)
            elif cooldown_ok:
                if send:
                    try:
                        # One W06 lease == one concrete recovery operation.
                        # Read-only health discovery and recovery eligibility are
                        # deliberately complete before this bounded scope opens.
                        with mutation_scope(
                            "W06",
                            unit_id=f"recovery:{service}:{current_at.isoformat()}",
                            operation_class="HEALTH_SERVICE_RECOVERY",
                            target=service,
                        ):
                            action = perform_recovery(service)
                    except ProductionWriterError:
                        raise
                    except Exception as exc:
                        action = {"service": service, "result": "FAILED"}
                        errors.append(f"RECOVERY_{service}:{type(exc).__name__}")
                    actions.append(action)
                    timestamps.append(current_at)
                else:
                    actions.append({"service": service, "result": "PLANNED"})
        attempts_state[service] = [item.isoformat() for item in timestamps]
    recovery = {
        "automatic_actions": actions,
        "reboot_recommended": bool(reboot_services),
        "reboot_services": reboot_services,
        "automatic_reboot_executed": False,
        "policy": {
            "consecutive_failures_before_restart": RECOVERY_MIN_FAILURES,
            "cooldown_minutes": int(RECOVERY_COOLDOWN.total_seconds() // 60),
            "max_attempts_per_6h": RECOVERY_MAX_ATTEMPTS,
        },
    }
    state = {"failure_counts": failure_counts, "recovery_attempts": attempts_state}
    return recovery, state, errors


def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN_PATH.read_text().strip()}",
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json",
    }


def mirror_notion(report: dict, *, transition: bool) -> None:
    children = _http_json(
        f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children?page_size=100",
        headers=_notion_headers(),
        timeout=20,
    ).get("results", [])
    latest = next((item for item in children if item.get("type") == "callout"), None)
    if not latest:
        raise RuntimeError("Notion health callout missing")
    emoji = {"HEALTHY": "🟢", "DEGRADED": "🟡", "DOWN": "🔴"}[report["overall_health"]]
    color = {"HEALTHY": "green_background", "DEGRADED": "yellow_background", "DOWN": "red_background"}[report["overall_health"]]
    codes = report["critical_codes"] + report["warning_codes"]
    recovery = report.get("recovery") or {}
    action_text = ", ".join(f"{item['service']}={item['result']}" for item in recovery.get("automatic_actions", []))
    recovery_text = f" · 자동복구: {action_text}" if action_text else ""
    reboot_text = " · 재부팅 권장(자동 실행 안 함)" if recovery.get("reboot_recommended") else ""
    content = (
        f"[LATEST_HEALTH] {report['overall_health']} · {report['checks_passed']}/{report['checks_total']} · "
        f"{report['run_at']} · 이상 항목: {', '.join(codes) if codes else '없음'}{recovery_text}{reboot_text}"
    )
    assert_current_production_writer()
    _http_json(
        f"https://api.notion.com/v1/blocks/{latest['id']}",
        method="PATCH",
        headers=_notion_headers(),
        body={"callout": {"rich_text": [{"type": "text", "text": {"content": content}}], "icon": {"type": "emoji", "emoji": emoji}, "color": color}},
        timeout=20,
    )
    if transition:
        assert_current_production_writer()
        _http_json(
            f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children",
            method="PATCH",
            headers=_notion_headers(),
            body={"children": [{"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": f"{report['run_at']}: {report['overall_health']} · {', '.join(codes) if codes else '정상'}"}}]}}]},
            timeout=20,
        )


def load_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return None


def current_boot_epoch() -> int:
    result = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=8, check=True)
    match = re.search(r"sec\s*=\s*(\d+)", result.stdout)
    if not match:
        raise RuntimeError("boot time unavailable")
    return int(match.group(1))


def prepare_reboot_test() -> dict:
    report = build_report()
    if report["overall_health"] != "HEALTHY":
        raise RuntimeError("reboot test requires a healthy baseline")
    marker = {
        "schema_version": 1,
        "test_mode": True,
        "status": "PENDING_REBOOT",
        "prepared_at": report["run_at"],
        "pre_boot_epoch": current_boot_epoch(),
        "baseline_checks_passed": report["checks_passed"],
        "baseline_checks_total": report["checks_total"],
        "telegram_sent": False,
        "notion_recorded": False,
        "contains_secrets": False,
    }
    with mutation_scope(
        "W06",
        unit_id=f"reboot-test-prepare:{report['run_at']}",
        operation_class="HEALTH_REBOOT_TEST_PREPARE",
        target="propertyai-health-reboot-marker",
    ):
        assert_current_production_writer()
        atomic_private_json(REBOOT_TEST_PATH, marker)
    return marker


def process_reboot_test(report: dict, *, send: bool) -> dict:
    try:
        marker = json.loads(REBOOT_TEST_PATH.read_text())
    except FileNotFoundError:
        return {"detected": False, "completed": False, "telegram_messages": 0, "notion_updates": 0}
    if marker.get("status") == "COMPLETED":
        return {"detected": True, "completed": True, "telegram_messages": 0, "notion_updates": 0}
    boot_epoch = current_boot_epoch()
    if boot_epoch <= int(marker.get("pre_boot_epoch", boot_epoch)):
        return {"detected": False, "completed": False, "telegram_messages": 0, "notion_updates": 0}
    if not send:
        return {"detected": True, "completed": False, "telegram_messages": 0, "notion_updates": 0, "errors": []}
    with mutation_scope(
        "W06",
        unit_id=f"reboot-test-process:{marker.get('pre_boot_epoch')}:{boot_epoch}",
        operation_class="HEALTH_REBOOT_TEST_MARKER",
        target="propertyai-health-reboot-marker",
    ):
        marker["status"] = "POST_BOOT_DETECTED"
        marker["post_boot_epoch"] = boot_epoch
        marker["post_boot_first_seen_at"] = marker.get("post_boot_first_seen_at") or report["run_at"]
        marker["post_boot_health"] = report["overall_health"]
        marker["post_boot_checks_passed"] = report["checks_passed"]
        marker["post_boot_checks_total"] = report["checks_total"]
        telegram_messages = 0
        notion_updates = 0
        errors = []
        if send and report["overall_health"] == "HEALTHY":
            if not marker.get("telegram_sent") and not marker.get("telegram_uncertain"):
                try:
                    send_telegram_text(
                        "✅ PropertyAI Mac 재부팅 검증 완료\n\n"
                        f"로그인 후 자동화 상태: HEALTHY {report['checks_passed']}/{report['checks_total']}\n"
                        "Ollama·OpenClaw·Telegram·Gmail·청소 스케줄러·건강상태 관제 자동 시작 확인\n"
                        "예약 변경·지급·삭제 효과: 0건\n"
                        f"확인 시각: {report['run_at']}"
                    )
                    marker["telegram_sent"] = True
                    telegram_messages = 1
                except ProductionWriterError:
                    raise
                except Exception as exc:
                    assert_current_production_writer()
                    marker["telegram_uncertain"] = True
                    errors.append(f"REBOOT_TELEGRAM_UNCERTAIN:{type(exc).__name__}")
            if not marker.get("notion_recorded") and not marker.get("notion_uncertain"):
                try:
                    mirror_notion(report, transition=True)
                    marker["notion_recorded"] = True
                    notion_updates = 1
                except ProductionWriterError:
                    raise
                except Exception as exc:
                    assert_current_production_writer()
                    marker["notion_uncertain"] = True
                    errors.append(f"REBOOT_NOTION_UNCERTAIN:{type(exc).__name__}")
            if marker.get("telegram_sent") and marker.get("notion_recorded"):
                marker["status"] = "COMPLETED"
                marker["completed_at"] = report["run_at"]
        if send:
            assert_current_production_writer()
        atomic_private_json(REBOOT_TEST_PATH, marker)
        return {
            "detected": True,
            "completed": marker.get("status") == "COMPLETED",
            "telegram_messages": telegram_messages,
            "notion_updates": notion_updates,
            "errors": errors,
        }


def _send_test_notifications_under_global_writer() -> dict:
    sent = 0
    messages = [
        (
            "🧪 PropertyAI 장애 관제 TEST\n\n"
            "상태: DOWN 시뮬레이션\n"
            "자동 복구 대상 판별과 운영자 알림 경로를 시험합니다.\n"
            "실제 서비스 중단·재시작·예약 변경·지급·삭제: 0건"
        ),
        (
            "✅ PropertyAI 장애 관제 TEST 복구\n\n"
            "상태: HEALTHY 시뮬레이션\n"
            "복구 알림 경로가 정상입니다.\n"
            "실제 서비스 중단·재시작·예약 변경·지급·삭제: 0건"
        ),
    ]
    for message in messages:
        send_telegram_text(message)
        sent += 1
    return {"test_mode": True, "telegram_messages": sent, "service_restarts": 0, "mac_restarts": 0}


def send_test_notifications() -> dict:
    with mutation_scope(
        "W06",
        unit_id="health-notification-smoke-test",
        operation_class="HEALTH_NOTIFICATION_TEST",
        target="propertyai-health-telegram-test",
    ):
        return _send_test_notifications_under_global_writer()


def _execute_after_probe(*, report: dict, previous: dict | None, send: bool) -> dict:
    transition = previous is None or previous.get("fingerprint") != report["fingerprint"]
    previous_health = previous.get("overall_health") if previous else None
    previous_telegram_fingerprint = previous.get("telegram_fingerprint") if previous else None
    previous_notion_fingerprint = previous.get("notion_fingerprint") if previous else None
    uncertain_effects = set((previous or {}).get("uncertain_external_effects") or [])
    state_telegram_key = f"telegram:state:{report['fingerprint']}"
    recovery_telegram_key = f"telegram:recovery:{report['fingerprint']}"
    notion_key = f"notion:{report['fingerprint']}"
    last_notion_at = datetime.fromisoformat(previous["last_notion_at"]) if previous and previous.get("last_notion_at") else None
    current_at = datetime.fromisoformat(report["run_at"])
    recovery, recovery_state, recovery_errors = recovery_decision(report, previous, current_at, send=send)
    report["recovery"] = recovery
    reboot_test = process_reboot_test(report, send=send)
    def finish_under_current_authority() -> dict:
        notion_due = (
            notion_key not in uncertain_effects
            and (
            previous_notion_fingerprint != report["fingerprint"]
            or last_notion_at is None
            or current_at - last_notion_at >= timedelta(hours=24)
            or bool(recovery["automatic_actions"])
            or recovery["reboot_recommended"]
            )
        )
        telegram_due = (
            state_telegram_key not in uncertain_effects
            and (
            (report["overall_health"] != "HEALTHY" and previous_telegram_fingerprint != report["fingerprint"])
            or (
                report["overall_health"] == "HEALTHY"
                and previous is not None
                and previous_health != "HEALTHY"
                and previous_telegram_fingerprint != report["fingerprint"]
            )
            )
        )
        delivery_errors = list(recovery_errors)
        telegram_sent = 0
        notion_updates = 0
        if send:
            if telegram_due and report["overall_health"] != "HEALTHY":
                try:
                    send_telegram(report, recovery=False)
                    telegram_sent = 1
                except ProductionWriterError:
                    raise
                except Exception as exc:
                    assert_current_production_writer()
                    uncertain_effects.add(state_telegram_key)
                    delivery_errors.append(f"TELEGRAM_UNCERTAIN:{type(exc).__name__}")
            elif telegram_due and report["overall_health"] == "HEALTHY":
                try:
                    send_telegram(report, recovery=True)
                    telegram_sent = 1
                except ProductionWriterError:
                    raise
                except Exception as exc:
                    assert_current_production_writer()
                    uncertain_effects.add(state_telegram_key)
                    delivery_errors.append(f"TELEGRAM_UNCERTAIN:{type(exc).__name__}")
            recommendation_fingerprint = report["fingerprint"] if recovery["reboot_recommended"] else None
            recommendation_already_sent = (previous or {}).get("reboot_recommendation_fingerprint") == recommendation_fingerprint
            if recovery_telegram_key not in uncertain_effects and (
                recovery["automatic_actions"]
                or (recovery["reboot_recommended"] and not recommendation_already_sent)
            ):
                try:
                    send_telegram_text(_recovery_message(report, recovery["automatic_actions"], reboot_recommended=recovery["reboot_recommended"]))
                    telegram_sent += 1
                except ProductionWriterError:
                    raise
                except Exception as exc:
                    assert_current_production_writer()
                    uncertain_effects.add(recovery_telegram_key)
                    delivery_errors.append(f"TELEGRAM_RECOVERY_UNCERTAIN:{type(exc).__name__}")
            if notion_due:
                try:
                    mirror_notion(report, transition=transition)
                    notion_updates = 1
                except ProductionWriterError:
                    raise
                except Exception as exc:
                    assert_current_production_writer()
                    uncertain_effects.add(notion_key)
                    delivery_errors.append(f"NOTION_UNCERTAIN:{type(exc).__name__}")
        telegram_sent += reboot_test.get("telegram_messages", 0)
        notion_updates += reboot_test.get("notion_updates", 0)
        delivery_errors.extend(reboot_test.get("errors", []))
        report["reboot_test"] = reboot_test
        report["transition_detected"] = transition
        report["delivery_errors"] = delivery_errors
        report["external_effects"] = {
            "telegram_messages": telegram_sent,
            "notion_updates": notion_updates,
            "service_restarts": sum(1 for item in recovery["automatic_actions"] if item["result"] in {"STARTED", "FAILED"}),
            "mac_restarts": 0,
        }
        if send:
            assert_current_production_writer()
            atomic_private_json(LATEST_PATH, report)
            state = {
                "fingerprint": report["fingerprint"],
                "overall_health": report["overall_health"],
                "last_run_at": report["run_at"],
                "last_notion_at": report["run_at"] if notion_updates else (previous or {}).get("last_notion_at"),
                "notion_fingerprint": report["fingerprint"] if notion_updates else previous_notion_fingerprint,
                "telegram_fingerprint": (
                    report["fingerprint"]
                    if telegram_sent or (previous is None and report["overall_health"] == "HEALTHY")
                    else previous_telegram_fingerprint
                ),
                "failure_counts": recovery_state["failure_counts"],
                "recovery_attempts": recovery_state["recovery_attempts"],
                "uncertain_external_effects": sorted(uncertain_effects),
                "reboot_recommendation_fingerprint": (
                    report["fingerprint"]
                    if recovery["reboot_recommended"] and telegram_sent
                    else (previous or {}).get("reboot_recommendation_fingerprint")
                ),
            }
            assert_current_production_writer()
            atomic_private_json(STATE_PATH, state)
            if transition:
                stamp = current_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                assert_current_production_writer()
                atomic_private_json(OUTPUT_DIR / "history" / f"{stamp}.json", report)
        return report

    if not send:
        return finish_under_current_authority()
    # Recovery kickstarts above each had their own lease. Remaining durable
    # health projection/notification/state effects are a separate bounded unit,
    # so multiple service recoveries never share one recovery lease.
    with mutation_scope(
        "W06",
        unit_id=f"health-projection:{report['run_at']}:{report['fingerprint']}",
        operation_class="HEALTH_STATE_PROJECTION",
        target="propertyai-health-projection",
    ):
        return finish_under_current_authority()


def execute(*, send: bool, now: datetime | None = None) -> dict:
    # All potentially slow health probes are read-only and intentionally occur
    # before the bounded GLOBAL_PRODUCTION mutation window.
    report = build_report(now)
    previous = load_state()
    return _execute_after_probe(report=report, previous=previous, send=send)


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true", help="Enable state-change Telegram and Notion effects")
    mode.add_argument("--dry-run", action="store_true", help="Explicit no-write mode")
    mode.add_argument("--test-notifications", action="store_true", help="Send two explicit TEST-only Telegram notifications")
    mode.add_argument("--prepare-reboot-test", action="store_true", help="Store a healthy pre-reboot baseline")
    args = parser.parse_args()
    if args.send or args.test_notifications or args.prepare_reboot_test:
        publish_startup_runtime_identity("W06")
    if args.test_notifications:
        print(json.dumps(send_test_notifications(), ensure_ascii=False))
        return
    if args.prepare_reboot_test:
        print(json.dumps(prepare_reboot_test(), ensure_ascii=False))
        return
    print(json.dumps(execute(send=args.send), ensure_ascii=False))


if __name__ == "__main__":
    main()
