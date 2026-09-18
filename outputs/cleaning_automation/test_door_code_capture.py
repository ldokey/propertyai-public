import unittest

from door_code_capture import (
    build_cleaner_message,
    input_prompt,
    new_request,
    preserve_airbnb_phone_last4,
    should_send_door_code,
)


class DoorCodeCaptureTests(unittest.TestCase):
    def test_preserves_airbnb_value_exactly(self):
        for value in ("0000", "1234", "0123", " 8624 ", "12-34"):
            with self.subTest(value=value):
                self.assertEqual(preserve_airbnb_phone_last4(value), value)

    def test_regular_lock_is_default_and_includes_exact_value(self):
        self.assertTrue(should_send_door_code())
        self.assertEqual(
            build_cleaner_message("청소 안내", "0123"),
            "청소 안내\n도어락 번호: 0123",
        )

    def test_smart_lock_also_includes_exact_code_line(self):
        self.assertTrue(should_send_door_code(smart_doorlock=True))
        self.assertEqual(
            build_cleaner_message("청소 안내", "0123", smart_doorlock=True),
            "청소 안내\n도어락 번호: 0123",
        )

    def test_smart_lock_still_requests_and_captures_code(self):
        request = new_request("req-test", "reservation-ref", "access-ref", 10, smart_doorlock=True)
        self.assertEqual(request.status, "PENDING_OPERATOR_INPUT")
        self.assertIn("뒤 4자리", input_prompt(smart_doorlock=True))

    def test_regular_lock_requests_airbnb_suffix(self):
        request = new_request("req-test", "reservation-ref", "access-ref", 10)
        self.assertEqual(request.status, "PENDING_OPERATOR_INPUT")
        self.assertIn("뒤 4자리", input_prompt())

    def test_prompt_contains_reservation_identity_and_amounts(self):
        prompt = input_prompt(
            True,
            property_nickname="JJ",
            reservation_ref="TEST123",
            check_in="2026-09-04 15:00",
            check_out="2026-09-06 11:00",
            guest_count="성인 1명",
            guest_total="USD 358.09",
            host_payout="USD 302.59",
        )
        for expected in ("숙소: JJ", "예약번호: TEST123", "체크인: 2026-09-04 15:00", "인원: 성인 1명", "게스트 결제: USD 358.09", "호스트 수령: USD 302.59"):
            self.assertIn(expected, prompt)


if __name__ == "__main__":
    unittest.main()
