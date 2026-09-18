import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notion_door_code_writer import update_reservation_door_code


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"object": "page", "id": "page-1"}).encode()


class NotionDoorCodeWriterTests(unittest.TestCase):
    def test_patch_preserves_value_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "token"
            token_path.write_text("test-token\n")
            captured = {}

            def fake_urlopen(request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeResponse()

            with patch("notion_door_code_writer.urllib.request.urlopen", side_effect=fake_urlopen):
                result = update_reservation_door_code("page-1", " 0123 ", token_path=token_path)

            body = json.loads(captured["request"].data)
            value = body["properties"]["출입 코드"]["rich_text"][0]["text"]["content"]
            self.assertEqual(value, " 0123 ")
            self.assertEqual(body["properties"]["출입정보 저장 상태"]["select"]["name"], "STORED")
            self.assertEqual(captured["request"].method, "PATCH")
            self.assertTrue(result["updated"])


if __name__ == "__main__":
    unittest.main()
