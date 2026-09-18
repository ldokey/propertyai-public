import json
import tempfile
import unittest
from pathlib import Path

from notion_sync.active_corpus import load_active_corpus
from notion_sync.incremental_ingest import load_json
from notion_sync.policy_contract import derive_policy_contract
from notion_sync.quality_gate import evaluate
from notion_sync.tests.support import build_canonical_runtime


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp.name)
        self.runtime = build_canonical_runtime(self.temp_root)

    def tearDown(self):
        self.temp.cleanup()

    def test_current_canonical_report_passes_without_deployment(self):
        manifest = load_json(self.runtime / "active" / "manifest.json")
        contract = derive_policy_contract(load_active_corpus(self.runtime))
        report_path = self.temp_root / "canonical_report.json"
        report_path.write_text(
            json.dumps(
                {
                    "active_generation_id": manifest["generation_id"],
                    "active_fingerprint": manifest["fingerprint"],
                    "questions": 10,
                    "results": [{} for _ in range(10)],
                    "summary": {
                        "retrieval_recall_at_5": 1.0,
                        "answer_accuracy": 1.0,
                    },
                    "policy_contract_hash": contract["config_hash"],
                }
            ),
            encoding="utf-8",
        )
        gate = evaluate(report_path, self.runtime, write=False)
        self.assertEqual(gate["status"], "PASS")
        self.assertFalse(gate["deployment_changed"])


if __name__ == "__main__":
    unittest.main()
