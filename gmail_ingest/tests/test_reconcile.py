import unittest

from gmail_ingest.reconcile import build_changeset, classify


EVENT = {
    "source_message_hash": "m" * 64,
    "external_booking_ref_hash": "b" * 64,
    "listing_id": "listing-1",
    "check_in": "2026-08-10",
    "check_out": "2026-08-12",
    "adults": 1,
    "currency": "USD",
    "guest_total": "264.89",
    "host_payout": "223.83",
}


class ReconcileTests(unittest.TestCase):
    def test_no_match_is_new_but_never_write_ready(self):
        result = build_changeset(EVENT, {"matches": []})
        self.assertEqual(result["classification"], "NEW")
        self.assertFalse(result["write_ready"])
        self.assertEqual(result["external_writes"], 0)

    def test_exact_reference_and_values_is_duplicate(self):
        match = {**EVENT, "external_booking_ref_hash": EVENT["external_booking_ref_hash"]}
        self.assertEqual(classify(EVENT, {"matches": [match]})[0], "DUPLICATE")

    def test_exact_reference_with_change_is_update_candidate(self):
        match = {**EVENT, "adults": 2}
        status, differences = classify(EVENT, {"matches": [match]})
        self.assertEqual(status, "UPDATE_CANDIDATE")
        self.assertIn("adults", differences)

    def test_same_dates_different_reference_requires_review(self):
        match = {**EVENT, "external_booking_ref_hash": "other"}
        self.assertEqual(classify(EVENT, {"matches": [match]})[0], "REVIEW_REQUIRED")

    def test_overlap_requires_review(self):
        match = {"listing_id": "listing-1", "check_in": "2026-08-11", "check_out": "2026-08-13"}
        self.assertEqual(classify(EVENT, {"matches": [match]})[0], "REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
