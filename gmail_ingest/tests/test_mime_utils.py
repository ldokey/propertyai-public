import base64
import unittest

from gmail_ingest.mime_utils import decoded_message_text


def encoded(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


class MimeUtilsTests(unittest.TestCase):
    def test_prefers_complete_html_and_preserves_link_targets(self):
        payload = {"parts": [
            {"mimeType": "text/plain", "body": {"data": encoded("Check-in Aug 10 Checkout Aug 12")}},
            {"mimeType": "text/html", "body": {"data": encoded(
                '<p>Check-in</p><p>Aug 10</p><p>Checkout</p><p>Aug 12</p>'
                '<p>Confirmation code</p><p>HMABCDEFG1</p><p>Total (USD)</p>'
                '<a href="https://www.airbnb.com/rooms/123">Listing</a>'
            )}},
        ]}
        text = decoded_message_text(payload)
        self.assertIn("Confirmation code", text)
        self.assertIn("airbnb.com/rooms/123", text)


if __name__ == "__main__":
    unittest.main()
