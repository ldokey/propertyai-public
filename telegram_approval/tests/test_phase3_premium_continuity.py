import json
from pathlib import Path
from unittest.mock import patch

from telegram_approval import cleaning_completion


class Router:
    def __init__(self, messages):
        self.messages = messages

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return {"message_id": len(self.messages)}


def test_urgent_frozen_total_survives_completion_review_and_transfer_records(tmp_path: Path):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    operator = tmp_path / "operator.json"
    operator.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 202}))
    messages = []
    candidate = {
        "telegram_user_id": 303,
        "telegram_chat_id": 404,
        "party_page_id": "party-urgent",
        "label": "Urgent Cleaner",
    }
    router = Router(messages)
    with patch.object(cleaning_completion, "REQUEST_DIR", request_dir), \
         patch.object(cleaning_completion, "OPERATOR_PATH", operator), \
         patch.object(cleaning_completion, "TOKEN_PATH", None), \
         patch.object(cleaning_completion, "cleaner_outbound_router", return_value=router), \
         patch.object(cleaning_completion, "ops_outbound_router", return_value=router), \
         patch.object(cleaning_completion, "secret", return_value="secret"), \
         patch.object(cleaning_completion, "signature", return_value="sig"):
        completion_result = cleaning_completion.send_completion_request(
            cleaning_page_id="cleaning-urgent",
            property_nickname="JJ",
            address="safe",
            cleaning_date="2026-09-10",
            cleaning_fee_krw=55000,
            candidate=candidate,
            test_mode=True,
            replacement_urgency="URGENT",
            urgent_premium_krw=5000,
            total_agreed_fee_krw=60000,
            urgent_premium_policy_version="PREMIUM-V1",
            assignment_page_id="history-urgent",
            assignment_version="assignment-v1",
            accepted_assignment_action_id="offer-v1",
        )
        completion_path = request_dir / f"{completion_result['action_id']}.json"
        completion = json.loads(completion_path.read_text())
        assert completion["cleaning_fee_krw"] == 55000
        assert completion["total_agreed_fee_krw"] == 60000
        assert "정산 예정 총액: ₩60,000" in messages[-1][1]
        completion["execution_result"] = {
            "completion_folder_url": "https://example.invalid/folder",
            "photo_count": 4,
            "next_expected_last_edited_time": "T2",
        }

        review_result = cleaning_completion.send_operator_review(completion)
        review = json.loads((request_dir / f"{review_result['action_id']}.json").read_text())
        assert review["cleaning_fee_krw"] == 55000
        assert review["urgent_premium_krw"] == 5000
        assert review["total_agreed_fee_krw"] == 60000
        assert review["assignment_page_id"] == "history-urgent"
        review["execution_result"] = {"next_expected_last_edited_time": "T3"}

        transfer_result = cleaning_completion.send_transfer_confirmation(review)
        transfer = json.loads((request_dir / f"{transfer_result['action_id']}.json").read_text())
        assert transfer["cleaning_fee_krw"] == 55000
        assert transfer["urgent_premium_krw"] == 5000
        assert transfer["total_agreed_fee_krw"] == 60000
        assert transfer["urgent_premium_policy_version"] == "PREMIUM-V1"
        assert "이체 예정 총액: ₩60,000" in messages[-1][1]
