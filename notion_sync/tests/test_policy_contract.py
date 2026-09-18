import tempfile
import unittest

from notion_sync.active_corpus import load_active_corpus
from notion_sync.policy_contract import derive_policy_contract, policy_context
from notion_sync.tests.support import build_canonical_runtime


class PolicyContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = build_canonical_runtime(self.temp.name)
        self.contract = derive_policy_contract(load_active_corpus(self.runtime))

    def tearDown(self):
        self.temp.cleanup()

    def test_contract_is_verified_against_active_sources(self):
        self.assertEqual(len(self.contract["rules"]), 2)
        self.assertTrue(all(rule["evidence_verified"] for rule in self.contract["rules"]))

    def test_synthetic_d45_rule_is_derived_from_fixture_evidence(self):
        context = policy_context(self.contract, "example renewal at 45 days")
        self.assertIn("SYNTHETIC_RENEWAL_REVIEW", context)
        self.assertIn("SYNTHETIC_AUTO_DATE", context)
        self.assertIn("fixture-only", context)


if __name__ == "__main__":
    unittest.main()
