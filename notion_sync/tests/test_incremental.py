import copy
import json
import tempfile
import unittest
from pathlib import Path

from notion_sync.incremental_ingest import ingest


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "initial_seed.json"
READ_PLAN = Path(__file__).resolve().parents[1] / "config" / "read_plan.json"


class IncrementalIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "runtime"
        self.snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def write_snapshot(self, value, name):
        target = Path(self.temp.name) / name
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return target

    def test_public_read_plan_matches_synthetic_fixture_authority(self):
        plan = json.loads(READ_PLAN.read_text(encoding="utf-8"))
        self.assertEqual(plan["provider"], "public_synthetic_fixture")
        self.assertFalse(plan["write_operations_allowed"])
        fixture_ids = {document["source_id"] for document in self.snapshot["documents"]}
        planned_ids = set(plan["page_fetches"])
        planned_ids.update(item["data_source"] for item in plan["metadata_queries"])
        planned_ids.update(plan["schema_only_fetches"])
        self.assertTrue(planned_ids <= fixture_ids)
        self.assertNotIn("app.notion.com", json.dumps(plan))

    def test_initial_then_unchanged(self):
        path = self.write_snapshot(self.snapshot, "initial.json")
        first = ingest(path, self.runtime)
        second = ingest(path, self.runtime)
        self.assertEqual(first["counts"], {"added": 10})
        self.assertEqual(second["counts"], {"unchanged": 10})

    def test_modified_document_creates_new_version(self):
        first_path = self.write_snapshot(self.snapshot, "first.json")
        ingest(first_path, self.runtime)
        changed = copy.deepcopy(self.snapshot)
        changed["fetched_at"] = "2026-08-01T05:00:00Z"
        changed["documents"][0]["content"] += " 변경된 규칙."
        report = ingest(self.write_snapshot(changed, "changed.json"), self.runtime)
        self.assertEqual(report["counts"].get("modified"), 1)
        versions = list((self.runtime / "staging" / "00000000-0000-4000-8000-000000000001").glob("*.json"))
        self.assertEqual(len(versions), 2)

    def test_partial_inventory_does_not_mark_missing(self):
        ingest(self.write_snapshot(self.snapshot, "first.json"), self.runtime)
        partial = copy.deepcopy(self.snapshot)
        partial["fetched_at"] = "2026-08-01T05:00:00Z"
        partial["documents"] = partial["documents"][:1]
        report = ingest(self.write_snapshot(partial, "partial.json"), self.runtime)
        self.assertNotIn("missing_observed", report["counts"])

    def test_three_complete_misses_only_create_candidate(self):
        ingest(self.write_snapshot(self.snapshot, "first.json"), self.runtime)
        empty = copy.deepcopy(self.snapshot)
        empty["documents"] = []
        empty["complete_inventory"] = True
        statuses = []
        for index in range(1, 4):
            empty["fetched_at"] = f"2026-08-0{index + 1}T05:00:00Z"
            report = ingest(self.write_snapshot(empty, f"empty-{index}.json"), self.runtime)
            statuses.append(report["events"][0]["status"])
        self.assertEqual(statuses, ["missing_observed", "missing_observed", "tombstone_candidate"])
        self.assertTrue(any((self.runtime / "staging").rglob("*.json")))

    def test_redacts_personal_data_before_storage(self):
        sample = copy.deepcopy(self.snapshot)
        sample["documents"] = sample["documents"][:1]
        sample["documents"][0]["content"] = "guest@example.com 010-1234-5678 900101-1234567 HMSZMQ89FB"
        report = ingest(self.write_snapshot(sample, "pii.json"), self.runtime)
        event = report["events"][0]
        stored = next((self.runtime / "staging").rglob("*.json")).read_text(encoding="utf-8")
        self.assertEqual(event["redactions"], {"email": 1, "phone": 1, "rrn": 1, "booking_code": 1})
        self.assertNotIn("guest@example.com", stored)
        self.assertNotIn("010-1234-5678", stored)
        self.assertNotIn("900101-1234567", stored)
        self.assertNotIn("HMSZMQ89FB", stored)


if __name__ == "__main__":
    unittest.main()
