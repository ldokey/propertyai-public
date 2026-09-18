import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from health_monitor import health_monitor


class HealthMonitorTests(unittest.TestCase):
    def healthy_report(self):
        return {
            "schema_version": 1,
            "run_at": "2026-08-03T10:00:00+09:00",
            "overall_health": "HEALTHY",
            "critical_codes": [],
            "warning_codes": [],
            "checks_passed": 18,
            "checks_total": 18,
            "checks": [],
            "fingerprint": "healthy",
            "contains_secrets": False,
            "external_effects": {"telegram_messages": 0, "notion_updates": 0},
        }

    def test_first_healthy_send_mirrors_notion_without_telegram(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(health_monitor, "STATE_PATH", Path(tmp) / "state.json"), \
             patch.object(health_monitor, "LATEST_PATH", Path(tmp) / "latest.json"), \
             patch.object(health_monitor, "OUTPUT_DIR", Path(tmp)), \
             patch.object(health_monitor, "build_report", return_value=self.healthy_report()), \
             patch.object(health_monitor, "mirror_notion") as notion, \
             patch.object(health_monitor, "send_telegram") as telegram:
            result = health_monitor.execute(send=True, now=datetime.now(timezone.utc))
            notion.assert_called_once()
            telegram.assert_not_called()
            self.assertEqual(result["external_effects"], {"telegram_messages": 0, "notion_updates": 1, "service_restarts": 0, "mac_restarts": 0})

    def test_transition_to_down_sends_one_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"fingerprint": "healthy", "overall_health": "HEALTHY", "last_notion_at": "2026-08-03T09:00:00+09:00"}))
            down = self.healthy_report() | {
                "overall_health": "DOWN",
                "critical_codes": ["SERVICE_TELEGRAM_APPROVAL"],
                "checks_passed": 17,
                "fingerprint": "down",
            }
            with patch.object(health_monitor, "STATE_PATH", state), \
                 patch.object(health_monitor, "LATEST_PATH", Path(tmp) / "latest.json"), \
                 patch.object(health_monitor, "OUTPUT_DIR", Path(tmp)), \
                 patch.object(health_monitor, "build_report", return_value=down), \
                 patch.object(health_monitor, "mirror_notion") as notion, \
                 patch.object(health_monitor, "send_telegram") as telegram:
                result = health_monitor.execute(send=True)
            telegram.assert_called_once_with(down, recovery=False)
            notion.assert_called_once()
            self.assertEqual(result["external_effects"], {"telegram_messages": 1, "notion_updates": 1, "service_restarts": 0, "mac_restarts": 0})

    def test_unchanged_health_has_no_external_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "fingerprint": "healthy",
                "overall_health": "HEALTHY",
                "last_notion_at": "2026-08-03T09:00:00+09:00",
                "notion_fingerprint": "healthy",
                "telegram_fingerprint": "healthy",
            }))
            with patch.object(health_monitor, "STATE_PATH", state), \
                 patch.object(health_monitor, "LATEST_PATH", Path(tmp) / "latest.json"), \
                 patch.object(health_monitor, "OUTPUT_DIR", Path(tmp)), \
                 patch.object(health_monitor, "build_report", return_value=self.healthy_report()), \
                 patch.object(health_monitor, "mirror_notion") as notion, \
                 patch.object(health_monitor, "send_telegram") as telegram:
                result = health_monitor.execute(send=True)
            notion.assert_not_called()
            telegram.assert_not_called()
            self.assertEqual(result["external_effects"], {"telegram_messages": 0, "notion_updates": 0, "service_restarts": 0, "mac_restarts": 0})

    def test_failed_alert_is_marked_uncertain_without_repeating_notion(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "fingerprint": "down",
                "overall_health": "DOWN",
                "last_notion_at": "2026-08-03T09:00:00+09:00",
                "notion_fingerprint": "down",
                "telegram_fingerprint": "healthy",
            }))
            down = self.healthy_report() | {
                "overall_health": "DOWN",
                "critical_codes": ["SERVICE_TELEGRAM_APPROVAL"],
                "checks_passed": 17,
                "fingerprint": "down",
            }
            with patch.object(health_monitor, "STATE_PATH", state), \
                 patch.object(health_monitor, "LATEST_PATH", Path(tmp) / "latest.json"), \
                 patch.object(health_monitor, "OUTPUT_DIR", Path(tmp)), \
                 patch.object(health_monitor, "build_report", return_value=down), \
                 patch.object(health_monitor, "mirror_notion") as notion, \
                 patch.object(health_monitor, "send_telegram", side_effect=RuntimeError("offline")) as telegram:
                result = health_monitor.execute(send=True)
            telegram.assert_called_once()
            notion.assert_not_called()
            self.assertEqual(result["delivery_errors"], ["TELEGRAM_UNCERTAIN:RuntimeError"])

    def test_dry_run_never_sends_or_updates(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(health_monitor, "STATE_PATH", Path(tmp) / "state.json"), \
             patch.object(health_monitor, "LATEST_PATH", Path(tmp) / "latest.json"), \
             patch.object(health_monitor, "OUTPUT_DIR", Path(tmp)), \
             patch.object(health_monitor, "build_report", return_value=self.healthy_report()), \
             patch.object(health_monitor, "mirror_notion") as notion, \
             patch.object(health_monitor, "send_telegram") as telegram:
            result = health_monitor.execute(send=False)
            notion.assert_not_called()
            telegram.assert_not_called()
            self.assertEqual(result["external_effects"], {"telegram_messages": 0, "notion_updates": 0, "service_restarts": 0, "mac_restarts": 0})

    def test_W06_DRY_RUN_ZERO_WRITE(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(health_monitor, "STATE_PATH", root / "state.json"), \
                 patch.object(health_monitor, "LATEST_PATH", root / "latest.json"), \
                 patch.object(health_monitor, "OUTPUT_DIR", root), \
                 patch.object(health_monitor, "REBOOT_TEST_PATH", root / "reboot.json"), \
                 patch.object(health_monitor, "build_report", return_value=self.healthy_report()), \
                 patch.object(health_monitor, "atomic_private_json", wraps=health_monitor.atomic_private_json) as durable, \
                 patch.object(health_monitor, "mutation_scope") as lease:
                result = health_monitor.execute(send=False)
            self.assertEqual(durable.call_count, 0)
            lease.assert_not_called()
            self.assertFalse((root / "latest.json").exists())
            self.assertFalse((root / "state.json").exists())
            self.assertFalse((root / "reboot.json").exists())
            self.assertFalse((root / "history").exists())
            self.assertEqual(result["external_effects"], {"telegram_messages": 0, "notion_updates": 0, "service_restarts": 0, "mac_restarts": 0})

    def test_W06_PREPARE_REBOOT_LEASED_with_fresh_assert_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker_path = Path(tmp) / "reboot.json"
            events = []
            scopes = []
            real_write = health_monitor.atomic_private_json

            @contextmanager
            def tracking_scope(writer_code, **kwargs):
                events.append("enter")
                scopes.append((writer_code, kwargs))
                try:
                    yield object()
                finally:
                    events.append("exit")

            def current_assert():
                events.append("assert")
                return {"status": "MATCH"}

            def tracked_write(path, value):
                events.append("write")
                real_write(path, value)

            with patch.object(health_monitor, "REBOOT_TEST_PATH", marker_path), \
                 patch.object(health_monitor, "build_report", return_value=self.healthy_report()), \
                 patch.object(health_monitor, "current_boot_epoch", return_value=100), \
                 patch.object(health_monitor, "mutation_scope", side_effect=tracking_scope), \
                 patch.object(health_monitor, "assert_current_production_writer", side_effect=current_assert), \
                 patch.object(health_monitor, "atomic_private_json", side_effect=tracked_write):
                marker = health_monitor.prepare_reboot_test()

            self.assertEqual(marker["status"], "PENDING_REBOOT")
            self.assertEqual(len(scopes), 1)
            self.assertEqual(scopes[0][0], "W06")
            self.assertEqual(scopes[0][1]["operation_class"], "HEALTH_REBOOT_TEST_PREPARE")
            self.assertLess(events.index("assert"), events.index("write"))
            self.assertEqual(events[0], "enter")
            self.assertEqual(events[-1], "exit")
            self.assertTrue(marker_path.exists())

    def test_W06_PROCESS_REBOOT_MUTATION_LEASED_with_fresh_assert_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker_path = Path(tmp) / "reboot.json"
            marker_path.write_text(json.dumps({
                "status": "PENDING_REBOOT",
                "pre_boot_epoch": 100,
                "telegram_sent": False,
                "notion_recorded": False,
            }))
            events = []
            scopes = []
            real_write = health_monitor.atomic_private_json

            @contextmanager
            def tracking_scope(writer_code, **kwargs):
                events.append("enter")
                scopes.append((writer_code, kwargs))
                try:
                    yield object()
                finally:
                    events.append("exit")

            def current_assert():
                events.append("assert")
                return {"status": "MATCH"}

            def tracked_write(path, value):
                events.append("write")
                real_write(path, value)

            with patch.object(health_monitor, "REBOOT_TEST_PATH", marker_path), \
                 patch.object(health_monitor, "current_boot_epoch", return_value=200), \
                 patch.object(health_monitor, "mutation_scope", side_effect=tracking_scope), \
                 patch.object(health_monitor, "assert_current_production_writer", side_effect=current_assert), \
                 patch.object(health_monitor, "atomic_private_json", side_effect=tracked_write), \
                 patch.object(health_monitor, "send_telegram_text"), \
                 patch.object(health_monitor, "mirror_notion"):
                result = health_monitor.process_reboot_test(self.healthy_report(), send=True)

            self.assertTrue(result["completed"])
            self.assertEqual(len(scopes), 1)
            self.assertEqual(scopes[0][0], "W06")
            self.assertEqual(scopes[0][1]["operation_class"], "HEALTH_REBOOT_TEST_MARKER")
            self.assertLess(max(i for i, event in enumerate(events) if event == "assert"), events.index("write"))
            self.assertEqual(events[0], "enter")
            self.assertEqual(events[-1], "exit")
            saved = json.loads(marker_path.read_text())
            self.assertEqual(saved["status"], "COMPLETED")

    def test_second_consecutive_failure_restarts_service_without_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "fingerprint": "down",
                "overall_health": "DOWN",
                "failure_counts": {"ai.openclaw.gateway": 1},
                "recovery_attempts": {},
                "notion_fingerprint": "down",
                "telegram_fingerprint": "down",
                "last_notion_at": "2026-08-03T09:00:00+09:00",
            }))
            down = self.healthy_report() | {
                "overall_health": "DOWN",
                "critical_codes": ["OPENCLAW_LOCAL"],
                "checks_passed": 19,
                "checks_total": 20,
                "fingerprint": "down",
            }
            with patch.object(health_monitor, "STATE_PATH", state), \
                 patch.object(health_monitor, "LATEST_PATH", Path(tmp) / "latest.json"), \
                 patch.object(health_monitor, "OUTPUT_DIR", Path(tmp)), \
                 patch.object(health_monitor, "build_report", return_value=down), \
                 patch.object(health_monitor, "perform_recovery", return_value={"service": "ai.openclaw.gateway", "result": "STARTED"}) as restart, \
                 patch.object(health_monitor, "mirror_notion"), \
                 patch.object(health_monitor, "send_telegram_text"), \
                 patch.object(health_monitor, "send_telegram"):
                result = health_monitor.execute(send=True)
            restart.assert_called_once_with("ai.openclaw.gateway")
            self.assertEqual(result["external_effects"]["service_restarts"], 1)
            self.assertEqual(result["external_effects"]["mac_restarts"], 0)

    def test_LEASE_UNIT_CARDINALITY_ADVERSARIAL_TEST_multiple_due_recoveries_use_one_lease_per_service_then_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            state.write_text(json.dumps({
                "fingerprint": "multi-down",
                "overall_health": "DOWN",
                "failure_counts": {
                    "com.ollama.ollama": 1,
                    "ai.openclaw.gateway": 1,
                },
                "recovery_attempts": {},
                "notion_fingerprint": "multi-down",
                "telegram_fingerprint": "multi-down",
                "last_notion_at": "2026-08-03T09:00:00+09:00",
            }))
            down = self.healthy_report() | {
                "overall_health": "DOWN",
                "critical_codes": ["SERVICE_OLLAMA", "OPENCLAW_LOCAL"],
                "checks_passed": 16,
                "checks_total": 18,
                "fingerprint": "multi-down",
            }
            scopes = []
            active = 0

            @contextmanager
            def tracking_scope(writer_code, **kwargs):
                nonlocal active
                self.assertEqual(writer_code, "W06")
                self.assertEqual(active, 0, "W06 scopes must be sequential, never nested")
                active += 1
                scopes.append(kwargs)
                try:
                    yield object()
                finally:
                    active -= 1

            def recovered(service):
                return {"service": service, "result": "STARTED"}

            with patch.object(health_monitor, "STATE_PATH", state), \
                 patch.object(health_monitor, "LATEST_PATH", root / "latest.json"), \
                 patch.object(health_monitor, "OUTPUT_DIR", root), \
                 patch.object(health_monitor, "REBOOT_TEST_PATH", root / "reboot.json"), \
                 patch.object(health_monitor, "build_report", return_value=down), \
                 patch.object(health_monitor, "mutation_scope", side_effect=tracking_scope), \
                 patch.object(health_monitor, "perform_recovery", side_effect=recovered) as recovery, \
                 patch.object(health_monitor, "mirror_notion"), \
                 patch.object(health_monitor, "send_telegram_text"), \
                 patch.object(health_monitor, "send_telegram"):
                result = health_monitor.execute(send=True)

            self.assertEqual(recovery.call_count, 2)
            recovery_scopes = [item for item in scopes if item["operation_class"] == "HEALTH_SERVICE_RECOVERY"]
            self.assertEqual(len(recovery_scopes), 2)
            self.assertEqual(
                {item["target"] for item in recovery_scopes},
                {"com.ollama.ollama", "ai.openclaw.gateway"},
            )
            projection_scopes = [item for item in scopes if item["operation_class"] == "HEALTH_STATE_PROJECTION"]
            self.assertEqual(len(projection_scopes), 1)
            self.assertEqual(result["external_effects"]["service_restarts"], 2)

    def test_three_failed_recoveries_recommend_but_never_reboot(self):
        current = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
        previous = {
            "failure_counts": {"ai.openclaw.gateway": 4},
            "recovery_attempts": {"ai.openclaw.gateway": [
                "2026-08-03T06:00:00+09:00",
                "2026-08-03T07:00:00+09:00",
                "2026-08-03T08:00:00+09:00",
            ]},
        }
        down = self.healthy_report() | {"critical_codes": ["OPENCLAW_LOCAL"]}
        recovery, _state, _errors = health_monitor.recovery_decision(down, previous, current.astimezone(health_monitor.KST), send=True)
        self.assertTrue(recovery["reboot_recommended"])
        self.assertFalse(recovery["automatic_reboot_executed"])
        self.assertEqual(recovery["automatic_actions"], [])

    def test_notification_smoke_test_has_two_messages_and_no_restart(self):
        with patch.object(health_monitor, "send_telegram_text") as send:
            result = health_monitor.send_test_notifications()
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result, {"test_mode": True, "telegram_messages": 2, "service_restarts": 0, "mac_restarts": 0})

    def test_prepare_reboot_requires_healthy_and_stores_private_marker(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(health_monitor, "REBOOT_TEST_PATH", Path(tmp) / "reboot.json"), \
             patch.object(health_monitor, "build_report", return_value=self.healthy_report()), \
             patch.object(health_monitor, "current_boot_epoch", return_value=100):
            marker = health_monitor.prepare_reboot_test()
            self.assertEqual(marker["status"], "PENDING_REBOOT")
            self.assertEqual(marker["pre_boot_epoch"], 100)
            self.assertEqual((Path(tmp) / "reboot.json").stat().st_mode & 0o777, 0o600)

    def test_post_boot_healthy_records_telegram_and_notion_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker_path = Path(tmp) / "reboot.json"
            marker_path.write_text(json.dumps({
                "status": "PENDING_REBOOT",
                "pre_boot_epoch": 100,
                "telegram_sent": False,
                "notion_recorded": False,
            }))
            with patch.object(health_monitor, "REBOOT_TEST_PATH", marker_path), \
                 patch.object(health_monitor, "current_boot_epoch", return_value=200), \
                 patch.object(health_monitor, "send_telegram_text") as telegram, \
                 patch.object(health_monitor, "mirror_notion") as notion:
                result = health_monitor.process_reboot_test(self.healthy_report(), send=True)
            self.assertTrue(result["completed"])
            self.assertEqual(result["telegram_messages"], 1)
            self.assertEqual(result["notion_updates"], 1)
            telegram.assert_called_once()
            notion.assert_called_once()

    def test_recovery_fence_failure_blocks_launchctl_kickstart(self):
        service = "ai.openclaw.gateway"
        with patch.object(health_monitor, "_launch_service_readback", return_value={"loaded": True}) as readback, \
             patch.object(health_monitor, "assert_current_production_writer", side_effect=RuntimeError("stale-fence")), \
             patch.object(health_monitor.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "stale-fence"):
                health_monitor.perform_recovery(service)
        readback.assert_called_once_with(service)
        run.assert_not_called()

    def test_ambiguous_launchctl_result_always_gets_fresh_readback(self):
        service = "ai.openclaw.gateway"
        before = {"loaded": True, "pid": 100}
        after = {"loaded": True, "pid": 200}
        with patch.object(health_monitor, "_launch_service_readback", side_effect=[before, after]) as readback, \
             patch.object(health_monitor, "assert_current_production_writer"), \
             patch.object(health_monitor.subprocess, "run", side_effect=TimeoutError("ambiguous")) as run:
            result = health_monitor.perform_recovery(service)
        self.assertEqual(readback.call_count, 2)
        run.assert_called_once()
        self.assertEqual(result["result"], "STARTED_READBACK")
        self.assertEqual(result["readback_before"], before)
        self.assertEqual(result["readback_after"], after)
        self.assertEqual(result["kick_error_type"], "TimeoutError")

    def test_retired_mixed_telegram_runtime_cannot_be_recovered(self):
        retired = "com.propertyai.telegram-approval"
        self.assertNotIn(retired, health_monitor.RECOVERY_TARGETS)
        self.assertIn("com.propertyai.telegram-ops", health_monitor.RECOVERY_TARGETS)
        self.assertIn("com.propertyai.telegram-cleaner", health_monitor.RECOVERY_TARGETS)
        with patch.object(health_monitor.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "retired mixed Telegram runtime"):
                health_monitor.perform_recovery(retired)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
