import json
import hashlib
import hmac
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gmail_ingest import lifecycle_actions
from telegram_approval import bot_service, cleaning_completion, cleaning_completion_evidence


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class CleaningCompletionTests(unittest.TestCase):
    def test_test_completion_requires_photos_then_review_before_transfer_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator_path = root / "operator.json"
            token_path = root / "token"
            action_secret_path = root / "action-secret"
            request_dir = root / "requests"
            request_dir.mkdir()
            operator_path.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
            token_path.write_text("token")
            action_secret_path.write_text("secret")
            completion_dir = root / "completion-uploads"
            sent = []

            def fake_api(_token, method, **values):
                sent.append((method, values))
                if method == "getFile":
                    return {"file_path": f"photos/{values['file_id']}.jpg"}
                return {"message_id": len(sent)}

            def callback_data(action_id, decision="approve"):
                supplied = hmac.new(
                    b"secret", f"{action_id}:{decision}".encode(), hashlib.sha256
                ).hexdigest()[:16]
                return f"a:{action_id}:{decision}:{supplied}"

            candidate = {
                "telegram_user_id": 101,
                "telegram_chat_id": 202,
                "party_page_id": "party",
                "label": "운영자 겸 청소담당자(TEST)",
            }
            with patch.object(cleaning_completion, "OPERATOR_PATH", operator_path), \
                 patch.object(cleaning_completion, "TOKEN_PATH", token_path), \
                 patch.object(cleaning_completion, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_completion, "api", side_effect=fake_api), \
                 patch.object(cleaning_completion, "secret", return_value=b"secret"), \
                 patch.object(cleaning_completion_evidence, "TOKEN_PATH", token_path), \
                 patch.object(cleaning_completion_evidence, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_completion_evidence, "COMPLETION_DIR", completion_dir), \
                 patch.object(cleaning_completion_evidence, "api", side_effect=fake_api), \
                 patch.object(cleaning_completion_evidence, "secret", return_value=b"secret"), \
                 patch.object(cleaning_completion_evidence.urllib.request, "urlopen",
                              side_effect=lambda *_args, **_kwargs: Response(b"jpeg-data")), \
                 patch.object(bot_service, "OPERATOR_PATH", operator_path), \
                 patch.object(bot_service, "REQUEST_DIR", request_dir), \
                 patch.object(bot_service, "ACTION_SECRET_PATH", action_secret_path), \
                 patch.object(bot_service, "api", side_effect=fake_api):
                completion = cleaning_completion.send_completion_request(
                    cleaning_page_id="test-cleaning",
                    property_nickname="JJ",
                    address="서울특별시 마포구 테스트로 1",
                    cleaning_date="2026-09-06",
                    cleaning_fee_krw=60000,
                    candidate=candidate,
                    test_mode=True,
                )
                result = bot_service.callback({"callback_query": {
                    "id": "callback-1",
                    "data": callback_data(completion["action_id"]),
                    "from": {"id": 101},
                    "message": {"chat": {"id": 202}},
                }}, "token")
                parent = json.loads((request_dir / f"{completion['action_id']}.json").read_text())
                submission_id = parent["next_action"]["action_id"]
                submission = json.loads((request_dir / f"{submission_id}.json").read_text())
                self.assertEqual(submission["action_type"], "CLEANING_COMPLETION_SUBMISSION")
                self.assertFalse(any("이체 금액" in call[1].get("text", "") for call in sent))

                for index in range(4):
                    captured = cleaning_completion_evidence.capture_completion_message({"message": {
                        "message_id": 100 + index,
                        "photo": [{"file_id": f"file-{index}", "file_unique_id": f"unique-{index}", "file_size": 9}],
                        "from": {"id": 101}, "chat": {"id": 202, "type": "private"},
                    }}, "token")
                    self.assertEqual(captured, "completion_photo_saved")

                result = bot_service.callback({"callback_query": {
                    "id": "callback-2", "data": callback_data(submission_id),
                    "from": {"id": 101}, "message": {"chat": {"id": 202}},
                }}, "token")
                submission = json.loads((request_dir / f"{submission_id}.json").read_text())
                review_id = submission["next_action"]["action_id"]
                review = json.loads((request_dir / f"{review_id}.json").read_text())
                self.assertEqual(review["action_type"], "CLEANING_EVIDENCE_REVIEW")
                self.assertFalse(any("이체 금액" in call[1].get("text", "") for call in sent))

                result = bot_service.callback({"callback_query": {
                    "id": "callback-3", "data": callback_data(review_id),
                    "from": {"id": 101}, "message": {"chat": {"id": 202}},
                }}, "token")

            parent = json.loads((request_dir / f"{completion['action_id']}.json").read_text())
            submission = json.loads((request_dir / f"{submission_id}.json").read_text())
            review = json.loads((request_dir / f"{review_id}.json").read_text())
            payment_id = review["next_action"]["action_id"]
            payment = json.loads((request_dir / f"{payment_id}.json").read_text())
            self.assertEqual(result, "approved")
            self.assertEqual(parent["status"], "PHOTO_SUBMISSION_REQUIRED_TEST")
            self.assertEqual(submission["external_writes_executed"], 0)
            self.assertEqual(review["external_writes_executed"], 0)
            self.assertEqual(payment["action_type"], "CLEANING_PAYMENT_CONFIRMATION")
            self.assertTrue(payment["test_mode"])
            self.assertTrue(any("이체 금액: ₩60,000" in call[1].get("text", "") for call in sent))

    def test_factual_completion_success_is_not_downgraded_by_followup_projection_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_dir = root / "requests"
            request_dir.mkdir()
            action_secret = root / "action-secret"
            action_secret.write_text("secret")
            action_id = "completion-factual-success"
            record = {
                "schema_version": 1,
                "action_id": action_id,
                "action_type": "CLEANING_COMPLETION_SUBMISSION",
                "cleaning_page_id": "cleaning-page",
                "candidate_user_id": 101,
                "candidate_chat_id": 202,
                "status": "PENDING",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "consumed": False,
                "test_mode": False,
                "execute_on_reject": False,
                "external_writes_on_approval": 1,
                "external_writes_on_reject": 0,
            }
            (request_dir / f"{action_id}.json").write_text(json.dumps(record))
            supplied = hmac.new(
                b"secret", f"{action_id}:approve".encode(), hashlib.sha256
            ).hexdigest()[:16]
            sent = []

            def fake_api(_token, method, **values):
                sent.append((method, values))
                return {"message_id": len(sent)}

            with patch.object(cleaning_completion_evidence, "session_ready", return_value=True), \
                 patch.object(cleaning_completion_evidence, "finish_session"), \
                 patch.object(
                     lifecycle_actions,
                     "execute_lifecycle_action",
                     return_value={"cleaning_completion": "PHOTOS_SUBMITTED_REVIEW_REQUIRED"},
                 ), \
                 patch.object(
                     cleaning_completion,
                     "send_operator_review",
                     side_effect=OSError("synthetic follow-up projection failure"),
                 ):
                result = bot_service.callback(
                    {"callback_query": {
                        "id": "callback-factual-success",
                        "data": f"a:{action_id}:approve:{supplied}",
                        "from": {"id": 101},
                        "message": {"chat": {"id": 202}},
                    }},
                    "token",
                    request_dir=request_dir,
                    action_secret_path=action_secret,
                    request_api=fake_api,
                )

            saved = json.loads((request_dir / f"{action_id}.json").read_text())
            self.assertEqual(result, "approved")
            self.assertEqual(saved["status"], "EXECUTED")
            self.assertEqual(
                saved["execution_result"]["cleaning_completion"],
                "PHOTOS_SUBMITTED_REVIEW_REQUIRED",
            )
            self.assertEqual(saved["external_writes_executed"], 1)
            self.assertEqual(
                saved["next_action"],
                {"sent": False, "error_type": "OSError"},
            )
            self.assertNotIn("execution_error_type", saved)

    def test_production_delivery_uncertain_blocks_duplicate_completion_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_dir = Path(tmp) / "requests"
            request_dir.mkdir()
            candidate = {
                "telegram_user_id": 101,
                "telegram_chat_id": 202,
                "party_page_id": "party",
                "label": "cleaner",
            }

            class AmbiguousRouter:
                def __init__(self):
                    self.calls = 0

                def send_message(self, *_args, **_kwargs):
                    self.calls += 1
                    raise TimeoutError("telegram acknowledgement unknown")

            router = AmbiguousRouter()
            with patch.object(cleaning_completion, "REQUEST_DIR", request_dir), \
                 patch.object(cleaning_completion, "TOKEN_PATH", Path(tmp) / "synthetic-token"), \
                 patch.object(cleaning_completion, "secret", return_value=b"synthetic-secret"), \
                 patch.object(cleaning_completion, "assert_current_production_writer"), \
                 patch.object(cleaning_completion, "cleaner_outbound_router", return_value=router):
                with self.assertRaisesRegex(TimeoutError, "acknowledgement unknown"):
                    cleaning_completion.send_completion_request(
                        cleaning_page_id="cleaning-page",
                        property_nickname="JJ",
                        address="synthetic",
                        cleaning_date="2026-08-29",
                        cleaning_fee_krw=60000,
                        candidate=candidate,
                        test_mode=False,
                        expected_last_edited_time="2026-08-29T00:00:00.000Z",
                    )
                records = [json.loads(path.read_text()) for path in request_dir.glob("*.json")]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "DELIVERY_UNCERTAIN")

                replay = cleaning_completion.send_completion_request(
                    cleaning_page_id="cleaning-page",
                    property_nickname="JJ",
                    address="synthetic",
                    cleaning_date="2026-08-29",
                    cleaning_fee_krw=60000,
                    candidate=candidate,
                    test_mode=False,
                    expected_last_edited_time="2026-08-29T00:00:00.000Z",
                )
            self.assertFalse(replay["sent"])
            self.assertEqual(replay["reason"], "PENDING_REQUEST_EXISTS")
            self.assertEqual(router.calls, 1)


if __name__ == "__main__":
    unittest.main()
