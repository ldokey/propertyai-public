import unittest

from gmail_ingest.airbnb_parser import parse_airbnb_confirmation, parse_airbnb_event


BODY = """New booking confirmed! Sample arrives Aug 10.
[Sample Guest](https://www.airbnb.com/hosting/reservations/details/HMABCDEFG1)
[Sample listing](https://www.airbnb.com/rooms/1660608918051136616)
Check-in
Mon, Aug 10
3:00 PM
Checkout
Wed, Aug 12
11:00 AM
Guests
1 adult
Confirmation code
HMABCDEFG1
Guest paid
$120.00 x 2 nights
$240.00
Total (USD)
$264.89
Host payout
You earn
$223.83
"""


class AirbnbParserTests(unittest.TestCase):
    def test_parses_and_excludes_personal_data(self):
        result = parse_airbnb_confirmation({
            "id": "synthetic-message-id",
            "subject": "Reservation confirmed - Sample arrives Aug 10",
            "email_ts": "2026-08-01T06:12:49+00:00",
            "body": BODY,
        })
        self.assertEqual(result["status"], "PARSED")
        self.assertEqual(result["check_in"], "2026-08-10")
        self.assertEqual(result["check_out"], "2026-08-12")
        self.assertEqual(result["nights"], 2)
        self.assertEqual(result["guest_total"], "264.89")
        self.assertEqual(result["host_payout"], "223.83")
        self.assertNotIn("guest_name", result)
        self.assertNotIn("body", result)

    def test_parses_krw_confirmation_without_decimal_amounts(self):
        body = """New booking confirmed! Sample arrives Aug 31.
[Sample Guest](https://www.airbnb.com/hosting/reservations/details/HMBPYWEQ5D)
[Sample listing](https://www.airbnb.com/rooms/1660608918051136616)
Check-in
Mon, Aug 31
3:00 PM
Checkout
Fri, Sep 4
11:00 AM
Guests
4 adults
Confirmation code
HMBPYWEQ5D
Guest paid
₩172,650 x 4 nights
₩690,600
Total (KRW)
₩732,184
Host payout
You earn
₩618,695
"""
        result = parse_airbnb_confirmation({
            "id": "synthetic-krw-message-id",
            "subject": "Reservation confirmed - Sample arrives Aug 31",
            "email_ts": "2026-08-22T03:43:39+00:00",
            "body": body,
        })
        self.assertEqual(result["status"], "PARSED")
        self.assertEqual(result["nights"], 4)
        self.assertEqual(result["currency"], "KRW")
        self.assertEqual(result["guest_total"], "732184")
        self.assertEqual(result["host_payout"], "618695")
        self.assertEqual(result["missing_fields"], [])

    def test_missing_required_money_is_review_required_not_parsed(self):
        body = BODY.replace("You earn\n$223.83\n", "")
        result = parse_airbnb_confirmation({
            "id": "synthetic-missing-payout-message-id",
            "subject": "Reservation confirmed - Sample arrives Aug 10",
            "email_ts": "2026-08-01T06:12:49+00:00",
            "body": body,
        })
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("host_payout", result["missing_fields"])

    def _parse_financial_variant(self, body, message_id):
        return parse_airbnb_confirmation({
            "id": message_id,
            "subject": "Reservation confirmed - Sample arrives Aug 10",
            "email_ts": "2026-08-01T06:12:49+00:00",
            "body": body,
        })

    def test_rejects_krw_declared_with_usd_guest_total_symbol(self):
        body = BODY.replace("Total (USD)\n$264.89", "Total (KRW)\n$732,184").replace(
            "You earn\n$223.83", "You earn\n₩618,695"
        )
        result = self._parse_financial_variant(body, "synthetic-krw-usd-total-message-id")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("guest_total", result["missing_fields"])

    def test_rejects_usd_declared_with_krw_guest_total_symbol(self):
        body = BODY.replace("$264.89", "₩732,184")
        result = self._parse_financial_variant(body, "synthetic-usd-krw-total-message-id")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("guest_total", result["missing_fields"])

    def test_rejects_host_payout_symbol_mismatching_declared_currency(self):
        body = BODY.replace("Total (USD)\n$264.89", "Total (KRW)\n₩732,184").replace(
            "You earn\n$223.83", "You earn\n$618,695"
        )
        result = self._parse_financial_variant(body, "synthetic-krw-usd-payout-message-id")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("host_payout", result["missing_fields"])

    def test_rejects_malformed_comma_grouping(self):
        body = BODY.replace("Total (USD)\n$264.89", "Total (KRW)\n₩7,32,184").replace(
            "You earn\n$223.83", "You earn\n₩618,695"
        )
        result = self._parse_financial_variant(body, "synthetic-malformed-krw-message-id")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("guest_total", result["missing_fields"])

    def test_rejects_malformed_host_payout_grouping(self):
        body = BODY.replace("Total (USD)\n$264.89", "Total (KRW)\n₩732,184").replace(
            "You earn\n$223.83", "You earn\n₩6,18,695"
        )
        result = self._parse_financial_variant(body, "synthetic-malformed-payout-message-id")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("host_payout", result["missing_fields"])

    def test_parses_grouped_usd_decimal(self):
        body = BODY.replace("$264.89", "$1,234.56")
        result = self._parse_financial_variant(body, "synthetic-grouped-usd-message-id")
        self.assertEqual(result["status"], "PARSED")
        self.assertEqual(result["guest_total"], "1234.56")

    def test_missing_currency_is_review_required_not_parsed(self):
        body = BODY.replace("Total (USD)\n$264.89\n", "")
        result = self._parse_financial_variant(body, "synthetic-missing-currency-message-id")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("currency", result["missing_fields"])
        self.assertIn("guest_total", result["missing_fields"])
        self.assertIn("host_payout", result["missing_fields"])

    def test_missing_guest_total_is_review_required_not_parsed(self):
        body = BODY.replace("$264.89\n", "")
        result = self._parse_financial_variant(body, "synthetic-missing-total-message-id")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("guest_total", result["missing_fields"])

    def test_parses_update_confirmation_without_inventing_changed_values(self):
        result = parse_airbnb_event({
            "id": "update-message",
            "subject": "Reservation updated",
            "template": "ALTERATION_ACCEPTED",
            "email_ts": "2026-08-02T04:31:00+00:00",
            "body": (
                "YOUR RESERVATION WITH Sample HAS BEEN UPDATED\n\nSample\n\nSouth Korea\n\n"
                "Synthetic Listing\n\nWe’ve already updated the reservation itinerary.\n"
                "https://www.airbnb.com/hosting/reservations/details/HMABCDEFG1"
            ),
        })
        self.assertEqual(result["event_type"], "BOOKING_UPDATED")
        self.assertEqual(result["status"], "PARSED")
        self.assertFalse(result["exact_changes_in_email"])
        accepted = result

    def test_real_cancellation_shape_beats_generic_update_wording(self):
        result = parse_airbnb_event({
            "id": "synthetic-hmm2-cancel",
            "subject": "Canceled: Reservation for Synthetic Listing",
            "template": "CANCELLATIONS_RESERVATION_CANCELED_BY_GUEST_TO_HOST",
            "email_ts": "2026-08-27T14:27:27+00:00",
            "body": (
                "Your reservation has been updated.\n"
                "https://www.airbnb.com/hosting/reservations/details/HMTESTC001\n"
                "Reservation canceled by guest.\n"
            ),
        })
        self.assertEqual(result["event_type"], "BOOKING_CANCELLED")
        self.assertEqual(result["status"], "PARSED")

    def test_cancellation_template_beats_ambiguous_update_body(self):
        result = parse_airbnb_event({
            "id": "synthetic-template-cancel",
            "subject": "Reservation notice",
            "template": "CANCELLATIONS_RESERVATION_CANCELED_BY_GUEST_TO_HOST",
            "email_ts": "2026-08-27T14:27:27+00:00",
            "body": (
                "Your reservation has been updated.\n"
                "https://www.airbnb.com/hosting/reservations/details/HMTESTC002\n"
            ),
        })
        self.assertEqual(result["event_type"], "BOOKING_CANCELLED")

    def test_cancellation_subject_beats_generic_update_body_without_cancel_template(self):
        result = parse_airbnb_event({
            "id": "synthetic-subject-cancel",
            "subject": "Canceled: Reservation for Synthetic Listing",
            "template": "GENERIC_RESERVATION_NOTICE",
            "email_ts": "2026-08-27T14:27:27+00:00",
            "body": (
                "Your reservation has been updated.\n"
                "https://www.airbnb.com/hosting/reservations/details/HMTESTC003\n"
            ),
        })
        self.assertEqual(result["event_type"], "BOOKING_CANCELLED")

    def test_parses_platform_cancellation_with_exact_booking_reference(self):
        result = parse_airbnb_event({
            "id": "cancel-message",
            "subject": "Reservation canceled",
            "template": "STAY_RESERVATION_CANCELLED_HOST",
            "email_ts": "2026-08-02T04:32:00+00:00",
            "body": (
                "Reservation canceled\n"
                "https://www.airbnb.com/hosting/reservations/details/HMABCDEFG1\n"
                "Listing #1660608918051136616\nJul 9 – 14, 4 guests\n"
                "Unfortunately, your guest had to cancel reservation HMABCDEFG1 for Jul 9 – 14.\n"
                "According to your cancellation policy, a complete refund was given to the guest."
            ),
        })
        self.assertEqual(result["event_type"], "BOOKING_CANCELLED")
        self.assertEqual(result["status"], "PARSED")
        self.assertEqual(result["cancellation_evidence"], "PLATFORM_NOTICE")
        self.assertEqual(result["listing_id"], "1660608918051136616")
        self.assertEqual(result["check_in"], "2026-07-09")
        self.assertEqual(result["check_out"], "2026-07-14")
        self.assertEqual(result["guests"], 4)
        self.assertEqual(result["refund_scope"], "GUEST_FULL_REFUND")

    def test_change_request_is_observed_but_never_applied(self):
        result = parse_airbnb_event({
            "id": "request-message",
            "subject": "Sample wants to change their reservation",
            "template": "STAY_RESERVATION_ALTERATION_REQUESTED",
            "email_ts": "2026-08-02T04:33:00+00:00",
            "body": (
                "Sample\n\nSeoul\n\nSynthetic Listing\n\nORIGINAL GUESTS\n1 guest\n"
                "REQUESTED GUESTS\n4 guests\nhttps://www.airbnb.com/reservation/alteration/123456"
            ),
        })
        self.assertEqual(result["event_type"], "BOOKING_CHANGE_REQUESTED")
        self.assertEqual(result["status"], "OBSERVED_NO_WRITE")
        self.assertEqual(result["requested_adults"], 4)
        self.assertIsNotNone(result["actor_ref_hash"])
        self.assertIsNotNone(result["listing_ref_hash"])


if __name__ == "__main__":
    unittest.main()
