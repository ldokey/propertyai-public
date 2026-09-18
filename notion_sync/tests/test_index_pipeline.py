import json
import tempfile
import unittest
from pathlib import Path

from notion_sync.incremental_ingest import ingest, safe_id
from notion_sync.index_pipeline import promote, validate_index


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "initial_seed.json"


class IndexPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "runtime"
        ingest(FIXTURE, self.runtime)

    def tearDown(self):
        self.temp.cleanup()

    def current_record_path(self, source_id):
        state = json.loads((self.runtime / "state" / "state.json").read_text())
        digest = state["documents"][source_id]["content_hash"]
        return self.runtime / "staging" / safe_id(source_id) / f"{digest}.json"

    def test_validates_and_promotes_atomic_manifest(self):
        result = promote(self.runtime)
        self.assertEqual(result["status"], "promoted")
        self.assertEqual(result["documents"], 10)
        self.assertFalse(result["production_rag_eligible"])
        manifest = json.loads((self.runtime / "active" / "manifest.json").read_text())
        self.assertEqual(manifest["stage"], "ACTIVE")
        self.assertEqual(len(manifest["documents"]), 10)

    def test_second_promotion_is_unchanged(self):
        first = promote(self.runtime)
        second = promote(self.runtime)
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["generation_id"], first["generation_id"])

    def test_hash_tamper_blocks_promotion(self):
        source_id = "00000000-0000-4000-8000-000000000001"
        path = self.current_record_path(source_id)
        record = json.loads(path.read_text())
        record["content"] += " tampered"
        path.write_text(json.dumps(record), encoding="utf-8")
        report = validate_index(self.runtime, write=False)
        failed = next(item for item in report["results"] if item["source_id"] == source_id)
        self.assertIn("content hash integrity failure", failed["errors"])
        self.assertEqual(promote(self.runtime)["status"], "blocked")
        self.assertFalse((self.runtime / "active" / "manifest.json").exists())

    def test_unredacted_pii_blocks_promotion(self):
        source_id = "00000000-0000-4000-8000-000000000001"
        path = self.current_record_path(source_id)
        record = json.loads(path.read_text())
        record["content"] = "guest@example.com"
        path.write_text(json.dumps(record), encoding="utf-8")
        report = validate_index(self.runtime, write=False)
        failed = next(item for item in report["results"] if item["source_id"] == source_id)
        self.assertTrue(any(error.startswith("unredacted personal data") for error in failed["errors"]))

    def test_missing_required_source_preserves_existing_active(self):
        first = promote(self.runtime)
        manifest_before = (self.runtime / "active" / "manifest.json").read_text()
        state_path = self.runtime / "state" / "state.json"
        state = json.loads(state_path.read_text())
        state["documents"].pop("00000000-0000-4000-8000-000000000004")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        blocked = promote(self.runtime)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["active_generation_unchanged"], first["generation_id"])
        self.assertEqual((self.runtime / "active" / "manifest.json").read_text(), manifest_before)


if __name__ == "__main__":
    unittest.main()
