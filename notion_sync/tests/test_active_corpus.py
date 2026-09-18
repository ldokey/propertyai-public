import tempfile
import unittest

from notion_sync.active_corpus import load_active_corpus
from notion_sync.tests.support import build_canonical_runtime


class ActiveCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = build_canonical_runtime(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_exports_canonical_pages_without_wrapper(self):
        corpus = load_active_corpus(self.runtime)
        self.assertEqual(len(corpus["documents"]), 10)
        by_id = {item["id"]: item for item in corpus["documents"]}
        self.assertIn("OPERATIONS_MANUAL", by_id)
        self.assertIn("OPS_STATE_TRANSITIONS_DRAFT", by_id)
        self.assertIn("CONTRACT_BOOKING_MODEL", by_id)
        self.assertGreater(len(by_id["OPERATIONS_MANUAL"]["content"]), 20)
        self.assertNotIn("<page url=", by_id["OPERATIONS_MANUAL"]["content"][:200])


if __name__ == "__main__":
    unittest.main()
