import unittest
from unittest.mock import MagicMock, patch

from gmail_ingest import lifecycle_actions
from telegram_approval import cleaning_completion_evidence, cleaning_issue


class LifecycleActionTests(unittest.TestCase):
    def test_completion_submission_archives_drive_photos_and_waits_for_review(self):
        session = {
            "session_id": "completion-session", "status": "OPEN", "test_mode": False,
            "evidence_type": "completion", "photos": [
                {"sha256": str(index) * 64, "path": f"/tmp/photo-{index}.jpg"}
                for index in range(1, 5)
            ], "notes": [],
        }
        page = {
            "last_edited_time": "2026-08-02T05:00:00.000Z",
            "properties": {
                "배정 수락 상태": {"select": {"name": "수락"}},
                "데이터 환경": {"select": {"name": "PRODUCTION"}},
                "등록 상태": {"select": {"name": "APPROVED"}},
                "상태": {"select": {"name": "진행중"}},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
                "점검일": {"date": {"start": "2026-08-02"}},
                "청소비 Snapshot": {"number": 60000},
                "완료 사진": {"type": "files", "files": []},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        uploads = [{
            "drive_file_id": f"file-{index}",
            "drive_url": f"https://drive.google.com/file/d/file-{index}/view",
            "filename": f"completion-{index}.jpg", "verified": True,
        } for index in range(1, 5)]
        evidence = {
            "verified": True, "uploads": uploads,
            "cleaning_folder_id": "cleaning-folder",
            "cleaning_folder_url": "https://drive.google.com/drive/folders/cleaning-folder",
            "completion_folder_id": "completion-folder",
            "completion_folder_url": "https://drive.google.com/drive/folders/completion-folder",
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            return page if method == "GET" else {"last_edited_time": "2026-08-02T05:01:00.000Z"}

        record = {
            "action_type": "CLEANING_COMPLETION_SUBMISSION",
            "completion_session_id": "completion-session", "cleaning_page_id": "cleaning-page",
            "cleaning_date": "2026-08-02", "cleaning_fee_krw": 60000,
            "candidate_party_page_id": "party",
            "expected_last_edited_time": "2026-08-02T05:00:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion), \
             patch.object(cleaning_completion_evidence, "load_session", return_value=session), \
             patch.object(cleaning_completion_evidence, "save_session"), \
             patch("drive_archive.issue_evidence.archive_completion_photos", return_value=evidence):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["cleaning_completion"], "PHOTOS_SUBMITTED_REVIEW_REQUIRED")
        properties = calls[-1][2]["properties"]
        self.assertEqual(properties["상태"]["select"]["name"], "완료 보고")
        self.assertFalse(properties["관리자 확인"]["checkbox"])
        self.assertEqual(len(properties["완료 사진"]["files"]), 4)
        self.assertEqual(properties["사진 링크"]["url"], evidence["completion_folder_url"])
        self.assertEqual(properties["청소 대금 상태"]["select"]["name"], "미생성")
        self.assertTrue(session["drive_evidence"]["notion_recorded"])

    def test_operator_review_approves_completion_for_payment_confirmation(self):
        page = {
            "last_edited_time": "2026-08-02T05:01:00.000Z",
            "properties": {
                "상태": {"select": {"name": "완료 보고"}},
                "사진 분석 상태": {"select": {"name": "UPLOADED"}},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            return page if method == "GET" else {"last_edited_time": "2026-08-02T05:02:00.000Z"}

        record = {"action_type": "CLEANING_EVIDENCE_REVIEW", "cleaning_page_id": "cleaning-page",
                  "expected_last_edited_time": "2026-08-02T05:01:00.000Z"}
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["cleaning_review"], "APPROVED_FOR_PAYMENT_CONFIRMATION")
        properties = calls[-1][2]["properties"]
        self.assertEqual(properties["상태"]["select"]["name"], "관리자 확인 완료")
        self.assertTrue(properties["관리자 확인"]["checkbox"])

    def test_payment_confirmation_rejects_missing_operator_review(self):
        page = {"properties": {
            "상태": {"select": {"name": "완료 보고"}},
            "관리자 확인": {"checkbox": False},
        }}
        record = {"action_type": "CLEANING_PAYMENT_CONFIRMATION", "cleaning_page_id": "cleaning-page"}
        with patch.object(lifecycle_actions, "_notion", return_value=page):
            with self.assertRaisesRegex(ValueError, "operator-approved"):
                lifecycle_actions.execute_lifecycle_action(record, "approve")

    def test_issue_submission_archives_to_drive_and_links_notion(self):
        session = {
            "session_id": "issue-session", "status": "OPEN", "test_mode": False,
            "photos": [{"sha256": "a" * 64, "path": "/tmp/photo.jpg"}],
            "notes": [{"text": "누수 흔적"}],
        }
        page = {
            "last_edited_time": "2026-08-02T05:00:00.000Z",
            "properties": {
                "배정 수락 상태": {"select": {"name": "수락"}},
                "데이터 환경": {"select": {"name": "PRODUCTION"}},
                "등록 상태": {"select": {"name": "APPROVED"}},
                "상태": {"select": {"name": "진행중"}},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
                "점검일": {"date": {"start": "2026-09-06"}},
                "문제 사진": {"type": "files", "files": []},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        evidence = {
            "verified": True,
            "cleaning_folder_id": "drive-cleaning-folder",
            "cleaning_folder_url": "https://drive.google.com/drive/folders/drive-cleaning-folder",
            "issue_folder_id": "drive-issue-folder",
            "issue_folder_url": "https://drive.google.com/drive/folders/drive-issue-folder",
            "uploads": [{
                "drive_file_id": "drive-photo", "drive_url": "https://drive.google.com/file/d/drive-photo/view",
                "filename": "2026-09-06_JJ_현장_문제_01.jpg", "verified": True,
            }],
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            return page if method == "GET" else {"last_edited_time": "2026-08-02T05:01:00.000Z"}

        record = {
            "action_type": "CLEANING_ISSUE_SUBMISSION", "issue_session_id": "issue-session",
            "cleaning_page_id": "cleaning-page", "candidate_party_page_id": "party",
            "expected_last_edited_time": "2026-08-02T05:00:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion), \
             patch.object(cleaning_issue, "load_session", return_value=session), \
             patch.object(cleaning_issue, "save_session"), \
             patch("drive_archive.issue_evidence.archive_issue_photos", return_value=evidence):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["cleaning_issue"], "SUBMITTED_DRIVE_FIRST")
        properties = calls[-1][2]["properties"]
        self.assertEqual(properties["Drive File ID"]["rich_text"][0]["text"]["content"], "drive-photo")
        self.assertEqual(properties["Drive Folder ID"]["rich_text"][0]["text"]["content"], "drive-cleaning-folder")
        self.assertEqual(properties["사진 링크"]["url"], evidence["issue_folder_url"])
        self.assertEqual(properties["문제 사진"]["files"][0]["type"], "external")
        self.assertTrue(session["drive_evidence"]["notion_recorded"])

    def test_issue_submission_rejects_non_production_before_drive_write(self):
        session = {"status": "OPEN", "photos": [{"sha256": "a" * 64}], "test_mode": False}
        page = {"properties": {
            "배정 수락 상태": {"select": {"name": "수락"}},
            "데이터 환경": {"select": {"name": "TEST"}},
        }}
        record = {"action_type": "CLEANING_ISSUE_SUBMISSION", "issue_session_id": "issue-session",
                  "cleaning_page_id": "cleaning-page"}
        with patch.object(lifecycle_actions, "_notion", return_value=page), \
             patch.object(cleaning_issue, "load_session", return_value=session), \
             patch("drive_archive.issue_evidence.archive_issue_photos") as archive:
            with self.assertRaisesRegex(ValueError, "PRODUCTION"):
                lifecycle_actions.execute_lifecycle_action(record, "approve")
        archive.assert_not_called()

    def test_cleaning_start_moves_to_in_progress_with_exact_version(self):
        page = {
            "last_edited_time": "2026-08-02T05:00:00.000Z",
            "properties": {
                "상태": {"select": {"name": "담당자 배정"}},
                "배정 수락 상태": {"select": {"name": "수락"}},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            return page if method == "GET" else {"last_edited_time": "2026-08-02T05:01:00.000Z"}

        record = {
            "action_type": "CLEANING_START",
            "cleaning_page_id": "cleaning-page",
            "candidate_party_page_id": "party",
            "expected_last_edited_time": "2026-08-02T05:00:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion), \
             patch.object(lifecycle_actions, "_assert_effective_assignment_for_work_start", return_value=(object(), {})):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["cleaning_operation"], "IN_PROGRESS")
        self.assertEqual(calls[-1][2]["properties"]["상태"]["select"]["name"], "진행중")

    def test_cleaning_completion_reports_done_without_creating_payment(self):
        page = {
            "last_edited_time": "2026-08-02T05:00:00.000Z",
            "properties": {
                "상태": {"select": {"name": "담당자 배정"}},
                "배정 수락 상태": {"select": {"name": "수락"}},
                "점검일": {"date": {"start": "2026-08-02"}},
                "청소비 Snapshot": {"number": 60000},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return page
            return {"last_edited_time": "2026-08-02T05:01:00.000Z"}

        record = {
            "action_type": "CLEANING_COMPLETION",
            "cleaning_page_id": "cleaning-page",
            "cleaning_date": "2026-08-02",
            "cleaning_fee_krw": 60000,
            "expected_last_edited_time": "2026-08-02T05:00:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["cleaning_completion"], "REPORTED")
        self.assertEqual(result["payment_effects"], 0)
        properties = calls[-1][2]["properties"]
        self.assertEqual(properties["상태"]["select"]["name"], "완료 보고")
        self.assertEqual(properties["청소 대금 상태"]["select"]["name"], "미생성")
        self.assertFalse(properties["정산 반영 여부"]["checkbox"])

    def test_payment_confirmation_creates_finance_labor_and_marks_cleaning_paid(self):
        cleaning = {
            "last_edited_time": "2026-08-02T05:01:00.000Z",
            "properties": {
                "상태": {"select": {"name": "관리자 확인 완료"}},
                "관리자 확인": {"checkbox": True},
                "청소 대금 상태": {"select": {"name": "미생성"}},
                "청소비 Snapshot": {"number": 60000},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
                "관련 Reservation": {"relation": [{"id": "reservation"}]},
                "연결 집": {"relation": [{"id": "house"}]},
                "연결 운영상품": {"relation": [{"id": "unit"}]},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return cleaning
            if method == "POST" and path.endswith("/query"):
                return {"results": []}
            if method == "POST" and path == "/v1/pages":
                parent = body["parent"]["data_source_id"]
                return {"id": "finance-page" if parent == lifecycle_actions.FINANCE_SOURCE else "labor-page"}
            return {}

        record = {
            "action_id": "payment-action",
            "action_type": "CLEANING_PAYMENT_CONFIRMATION",
            "cleaning_page_id": "cleaning-page",
            "property_nickname": "JJ",
            "cleaning_date": "2026-08-02",
            "cleaning_fee_krw": 60000,
            "candidate_party_page_id": "party",
            "candidate_label": "운영자 겸 청소담당자(TEST)",
            "expected_last_edited_time": "2026-08-02T05:01:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["cleaning_payment"], "PAID_CONFIRMED")
        self.assertEqual(result["finance"]["outcome"], "CREATED")
        self.assertEqual(result["labor"]["outcome"], "CREATED")
        creates = [call for call in calls if call[0] == "POST" and call[1] == "/v1/pages"]
        self.assertEqual(len(creates), 2)
        finance_properties = creates[0][2]["properties"]
        labor_properties = creates[1][2]["properties"]
        self.assertEqual(finance_properties["상태"]["select"]["name"], "완료")
        self.assertEqual(finance_properties["금액"]["number"], 60000)
        self.assertEqual(labor_properties["지급상태"]["select"]["name"], "지급완료")
        cleaning_patch = [call for call in calls if call[0] == "PATCH" and call[1] == "/v1/pages/cleaning-page"][-1]
        self.assertEqual(cleaning_patch[2]["properties"]["청소 대금 상태"]["select"]["name"], "지급완료")
        self.assertTrue(cleaning_patch[2]["properties"]["정산 반영 여부"]["checkbox"])

    def test_urgent_payment_uses_frozen_accepted_total_not_base_or_current_policy(self):
        cleaning = {
            "last_edited_time": "2026-09-10T05:01:00.000Z",
            "properties": {
                "상태": {"select": {"name": "관리자 확인 완료"}},
                "관리자 확인": {"checkbox": True},
                "청소 대금 상태": {"select": {"name": "미생성"}},
                "청소비 Snapshot": {"number": 55000},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
                "관련 Reservation": {"relation": [{"id": "reservation"}]},
                "연결 집": {"relation": [{"id": "house"}]},
                "연결 운영상품": {"relation": [{"id": "unit"}]},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return cleaning
            if method == "POST" and path.endswith("/query"):
                return {"results": []}
            if method == "POST" and path == "/v1/pages":
                parent = body["parent"]["data_source_id"]
                return {"id": "finance-urgent" if parent == lifecycle_actions.FINANCE_SOURCE else "labor-urgent"}
            return {}

        record = {
            "action_id": "urgent-payment",
            "action_type": "CLEANING_PAYMENT_CONFIRMATION",
            "cleaning_page_id": "cleaning-urgent",
            "property_nickname": "JJ",
            "cleaning_date": "2026-09-10",
            "cleaning_fee_krw": 55000,
            "replacement_urgency": "URGENT",
            "urgent_premium_krw": 5000,
            "total_agreed_fee_krw": 60000,
            "urgent_premium_policy_version": "PREMIUM-V1",
            "candidate_party_page_id": "party",
            "candidate_label": "담당자",
            "expected_last_edited_time": "2026-09-10T05:01:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["payout_amount_krw"], 60000)
        creates = [call for call in calls if call[0] == "POST" and call[1] == "/v1/pages"]
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0][2]["properties"]["금액"]["number"], 60000)
        self.assertEqual(creates[1][2]["properties"]["금액"]["number"], 60000)
        self.assertEqual(record["cleaning_fee_krw"], 55000)
        self.assertEqual(record["urgent_premium_policy_version"], "PREMIUM-V1")

    def test_payment_confirmation_reuses_existing_finance_and_labor_rows(self):
        cleaning = {
            "last_edited_time": "2026-08-02T05:01:00.000Z",
            "properties": {
                "상태": {"select": {"name": "관리자 확인 완료"}},
                "관리자 확인": {"checkbox": True},
                "청소 대금 상태": {"select": {"name": "미생성"}},
                "청소비 Snapshot": {"number": 60000},
                "담당 참여자/업체": {"relation": [{"id": "party"}]},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return cleaning
            if method == "POST" and lifecycle_actions.FINANCE_SOURCE in path:
                return {"results": [{"id": "finance-existing"}]}
            if method == "POST" and lifecycle_actions.LABOR_SOURCE in path:
                return {"results": [{"id": "labor-existing"}]}
            return {}

        record = {
            "action_id": "payment-retry",
            "action_type": "CLEANING_PAYMENT_CONFIRMATION",
            "cleaning_page_id": "cleaning-page",
            "property_nickname": "JJ",
            "cleaning_date": "2026-08-02",
            "cleaning_fee_krw": 60000,
            "candidate_party_page_id": "party",
            "candidate_label": "담당자",
            "expected_last_edited_time": "2026-08-02T05:01:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["finance"]["outcome"], "EXISTING")
        self.assertEqual(result["labor"]["outcome"], "EXISTING")
        creates = [call for call in calls if call[0] == "POST" and call[1] == "/v1/pages"]
        self.assertEqual(creates, [])

    def test_assignment_accept_updates_cleaning_without_payment_effect(self):
        page = {
            "last_edited_time": "2026-08-02T05:00:00.000Z",
            "properties": {
                "배정 수락 상태": {"select": {"name": "수락대기"}},
                "배정 상태": {"select": {"name": "UNPLANNED"}},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []
        def notion(method, path, body=None):
            calls.append((method, path, body))
            return page if method == "GET" else {}
        record = {
            "action_type": "CLEANING_ASSIGNMENT",
            "cleaning_page_id": "cleaning-page",
            "expected_acceptance_status": "수락대기",
            "expected_last_edited_time": "2026-08-02T05:00:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "approve")
        self.assertEqual(result["cleaning_assignment"], "ACCEPTED")
        self.assertEqual(result["payment_effects"], 0)
        properties = calls[-1][2]["properties"]
        self.assertEqual(properties["배정 수락 상태"]["select"]["name"], "수락")
        self.assertEqual(properties["배정 상태"]["select"]["name"], "HARD_BOOKED")
        self.assertEqual(properties["청소 대금 상태"]["select"]["name"], "미생성")

    def test_assignment_accept_projection_is_immediately_valid_for_unavailable(self):
        cleaning = {
            "id": "cleaning-page",
            "last_edited_time": "edit-1",
            "properties": {
                "데이터 환경": {"select": {"name": "PRODUCTION"}},
                "등록 상태": {"select": {"name": "APPROVED"}},
                "상태": {"select": {"name": "예정"}},
                "배정 수락 상태": {"select": {"name": "수락대기"}},
                "배정 상태": {"select": {"name": "UNPLANNED"}},
                "담당 참여자/업체": {"relation": []},
                "관련 Reservation": {"relation": [{"id": "reservation-page"}]},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        reservation = self._active_reservation()

        def notion(method, path, body=None):
            if method == "GET" and "reservation-page" in path:
                return reservation
            if method == "GET":
                return cleaning
            for name, value in body["properties"].items():
                cleaning["properties"][name] = value
            cleaning["last_edited_time"] = "edit-2"
            return cleaning

        assignment = {
            "action_type": "CLEANING_ASSIGNMENT",
            "cleaning_page_id": "cleaning-page",
            "candidate_party_page_id": "party",
            "expected_acceptance_status": "수락대기",
            "expected_last_edited_time": "edit-1",
        }
        unavailable = {
            "cleaning_page_id": "cleaning-page",
            "cleaner_party_page_id": "party",
            "expected_cleaning_last_edited_time": "edit-2",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            accepted = lifecycle_actions.execute_cleaning_assignment(assignment, "approve")
            validated = lifecycle_actions.validate_cleaner_unavailable(unavailable)

        self.assertEqual(accepted["cleaning_assignment"], "ACCEPTED")
        self.assertEqual(cleaning["properties"]["배정 상태"]["select"]["name"], "HARD_BOOKED")
        self.assertEqual(validated["reservation_page_id"], "reservation-page")

    def test_assignment_reject_requires_replacement_without_payment_effect(self):
        page = {
            "last_edited_time": "2026-08-02T05:00:00.000Z",
            "properties": {
                "배정 수락 상태": {"select": {"name": "수락대기"}},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []
        def notion(method, path, body=None):
            calls.append((method, path, body))
            return page if method == "GET" else {}
        record = {
            "action_type": "CLEANING_ASSIGNMENT",
            "cleaning_page_id": "cleaning-page",
            "expected_acceptance_status": "수락대기",
            "expected_last_edited_time": "2026-08-02T05:00:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "reject")
        self.assertEqual(result["cleaning_assignment"], "REPLACEMENT_REQUIRED")
        self.assertEqual(result["payment_effects"], 0)
        properties = calls[-1][2]["properties"]
        self.assertEqual(properties["배정 수락 상태"]["select"]["name"], "대체필요")
        self.assertEqual(properties["청소 대금 상태"]["select"]["name"], "미생성")

    def test_assignment_reject_keeps_pending_when_next_candidate_exists(self):
        page = {
            "last_edited_time": "2026-08-02T05:00:00.000Z",
            "properties": {
                "배정 수락 상태": {"select": {"name": "수락대기"}},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return page
            return {"last_edited_time": "2026-08-02T05:01:00.000Z"}

        record = {
            "action_type": "CLEANING_ASSIGNMENT",
            "cleaning_page_id": "cleaning-page",
            "expected_acceptance_status": "수락대기",
            "expected_last_edited_time": "2026-08-02T05:00:00.000Z",
            "remaining_candidates": [{"telegram_user_id": 2, "telegram_chat_id": 2}],
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "reject")
        self.assertEqual(result["cleaning_assignment"], "NEXT_CANDIDATE_REQUIRED")
        self.assertEqual(result["next_expected_last_edited_time"], "2026-08-02T05:01:00.000Z")
        properties = calls[-1][2]["properties"]
        self.assertEqual(properties["배정 수락 상태"]["select"]["name"], "수락대기")

    def test_negative_hold_and_cancel_paths_never_write(self):
        no_write_cases = (
            ({"action_type": "CLEANING_COMPLETION"}, "cleaning_completion", "NOT_COMPLETED_NO_WRITES"),
            ({"action_type": "CLEANING_COMPLETION_SUBMISSION"}, "cleaning_completion", "PHOTO_SUBMISSION_CANCELLED_NO_WRITES"),
            ({"action_type": "CLEANING_PAYMENT_CONFIRMATION"}, "cleaning_payment", "HELD_NO_WRITES"),
            ({"action_type": "CLEANING_ISSUE_SUBMISSION"}, "cleaning_issue", "CANCELLED_NO_WRITES"),
        )
        for record, key, expected in no_write_cases:
            with self.subTest(action_type=record["action_type"]), \
                 patch.object(lifecycle_actions, "_notion") as notion:
                result = lifecycle_actions.execute_lifecycle_action(record, "reject")
                self.assertEqual(result[key], expected)
                notion.assert_not_called()

    def test_operator_review_rejects_to_revision_and_blocks_payment(self):
        page = {
            "last_edited_time": "2026-08-02T05:01:00.000Z",
            "properties": {
                "상태": {"select": {"name": "완료 보고"}},
                "사진 분석 상태": {"select": {"name": "UPLOADED"}},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            },
        }
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            return page if method == "GET" else {"last_edited_time": "2026-08-02T05:02:00.000Z"}

        record = {
            "action_type": "CLEANING_EVIDENCE_REVIEW",
            "cleaning_page_id": "cleaning-page",
            "expected_last_edited_time": "2026-08-02T05:01:00.000Z",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            result = lifecycle_actions.execute_lifecycle_action(record, "reject")
        self.assertEqual(result["cleaning_review"], "REVISION_REQUIRED")
        properties = calls[-1][2]["properties"]
        self.assertEqual(properties["상태"]["select"]["name"], "진행중")
        self.assertEqual(properties["현장 보고 상태"]["select"]["name"], "보완요청")
        self.assertFalse(properties["관리자 확인"]["checkbox"])

    def test_operation_negative_paths_stop_at_the_correct_state(self):
        cases = {
            "CLEANING_DAY_CONFIRM": ("REPLACEMENT_REQUIRED", "배정 수락 상태", "대체필요"),
            "CLEANING_ARRIVAL": ("ENTRY_PROBLEM_REPORTED", "현장 보고 상태", "예외있음"),
            "CLEANING_START": ("SITE_PROBLEM_REPORTED", "현장 보고 상태", "예외있음"),
        }
        for action_type, (expected_outcome, property_name, expected_value) in cases.items():
            with self.subTest(action_type=action_type):
                page = {
                    "last_edited_time": "2026-08-02T05:00:00.000Z",
                    "properties": {
                        "상태": {"select": {"name": "담당자 배정"}},
                        "배정 수락 상태": {"select": {"name": "수락"}},
                        "담당 참여자/업체": {"relation": [{"id": "party"}]},
                        "점검 메모": {"type": "rich_text", "rich_text": []},
                    },
                }
                calls = []

                def notion(method, path, body=None):
                    calls.append((method, path, body))
                    return page if method == "GET" else {"last_edited_time": "2026-08-02T05:01:00.000Z"}

                record = {
                    "action_type": action_type,
                    "cleaning_page_id": "cleaning-page",
                    "candidate_party_page_id": "party",
                    "expected_last_edited_time": "2026-08-02T05:00:00.000Z",
                }
                with patch.object(lifecycle_actions, "_notion", side_effect=notion):
                    result = lifecycle_actions.execute_lifecycle_action(record, "reject")
                self.assertEqual(result["cleaning_operation"], expected_outcome)
                properties = calls[-1][2]["properties"]
                self.assertEqual(properties[property_name]["select"]["name"], expected_value)

    def test_approved_cancellation_preserves_rows_and_marks_calendar(self):
        reservation = {"properties": {"Change History": {"type": "rich_text", "rich_text": []}}}
        cleaning = {
            "properties": {
                "상태": {"select": {"name": "담당자 배정"}},
                "점검 메모": {"type": "rich_text", "rich_text": []},
            }
        }
        notion_calls = []

        def notion(method, path, body=None):
            notion_calls.append((method, path, body))
            if method == "GET" and "reservation" in path:
                return reservation
            if method == "GET" and "cleaning" in path:
                return cleaning
            if method == "POST" and path == "/v1/pages":
                return {"id": "finance-page"}
            return {}

        calendar = MagicMock()
        calendar.events.return_value.get.return_value.execute.return_value = {
            "id": "calendar-event", "summary": "JJ · 퇴실청소", "description": "audit"
        }
        calendar.events.return_value.patch.return_value.execute.return_value = {"id": "calendar-event"}
        record = {
            "action_type": "CANCEL_RESERVATION_WORKFLOW",
            "reservation_code": "TESTCODE3",
            "reservation_page_id": "reservation-page",
            "cleaning_page_ids": ["cleaning-page"],
            "cleaning_calendar_id": "calendar-id",
            "calendar_event_ids": ["calendar-event"],
            "source_message_hash": "d" * 64,
            "cancellation_source": "PLATFORM_NOTICE",
            "cancellation_received_at": "2026-08-02T04:32:00+00:00",
            "refund_scope": "GUEST_FULL_REFUND",
            "summary": {"nickname": "JJ", "check_in": "2026-09-04", "check_out": "2026-09-06", "guests": "1명"},
            "cleaner_delivery_chat_ids": [123],
        }
        token_path = MagicMock()
        token_path.read_text.return_value = "synthetic-test-token"
        history_store = MagicMock()
        history_store.query_accepted_for_cleaning.return_value = []
        with patch.object(lifecycle_actions, "_notion", side_effect=notion), \
             patch.object(lifecycle_actions.Credentials, "from_authorized_user_file", return_value=MagicMock()), \
             patch.object(lifecycle_actions, "build", return_value=calendar), \
             patch.object(lifecycle_actions, "TELEGRAM_TOKEN_PATH", token_path), \
             patch.object(lifecycle_actions, "telegram_api", return_value={"message_id": 99}) as telegram:
            result = lifecycle_actions.execute_lifecycle_action(record, history_store=history_store)

        self.assertEqual(result["reservation"], "CANCELLED")
        self.assertEqual(result["cleaning"][0]["outcome"], "CANCELLED")
        self.assertEqual(result["calendar"][0]["outcome"], "MARKED_CANCELLED")
        self.assertEqual(result["finance"]["outcome"], "CREATED_REVIEW_REQUIRED")
        self.assertEqual(result["cleaner_notifications"][0]["message_id"], 99)
        reservation_patch = next(call for call in notion_calls if call[0] == "PATCH" and "reservation" in call[1])
        self.assertEqual(reservation_patch[2]["properties"]["상태"]["select"]["name"], "취소")
        calendar.events.return_value.patch.assert_called_once()
        self.assertFalse(calendar.events.return_value.delete.called)
        telegram.assert_called_once()
        self.assertNotIn("예약번호", telegram.call_args.kwargs["text"])
        finance_create = next(call for call in notion_calls if call[0] == "POST" and call[1] == "/v1/pages")
        self.assertNotIn("금액", finance_create[2]["properties"])


    def _unavailable_cleaning(self, *, state="담당자 배정", acceptance="수락", assignee="party", last_edit="edit-1"):
        return {
            "id": "cleaning-page",
            "last_edited_time": last_edit,
            "properties": {
                "데이터 환경": {"select": {"name": "PRODUCTION"}},
                "등록 상태": {"select": {"name": "APPROVED"}},
                "상태": {"select": {"name": state}},
                "배정 수락 상태": {"select": {"name": acceptance}},
                "배정 상태": {"select": {"name": "HARD_BOOKED"}},
                "담당 참여자/업체": {"relation": [] if assignee is None else [{"id": assignee}]},
                "관련 Reservation": {"relation": [{"id": "reservation-page"}]},
                "점검 메모": {"type": "rich_text", "rich_text": []},
                "점검일": {"date": {"start": "2026-09-08"}},
            },
        }

    def _active_reservation(self, *, state="확정"):
        return {"id": "reservation-page", "properties": {
            "데이터 환경": {"select": {"name": "PRODUCTION"}},
            "등록 상태": {"select": {"name": "APPROVED"}},
            "상태": {"select": {"name": state}},
        }}

    def _unavailable_record(self):
        return {
            "cleaning_page_id": "cleaning-page",
            "cleaner_party_page_id": "party",
            "expected_cleaning_last_edited_time": "edit-1",
            "transition_at": "2026-09-02T03:00:00+00:00",
        }

    def test_cleaner_unavailable_validation_allows_future_or_same_day_pre_start_without_time_cutoff(self):
        for service_day in ("2026-09-02", "2026-09-08"):
            with self.subTest(service_day=service_day):
                cleaning = self._unavailable_cleaning()
                cleaning["properties"]["점검일"] = {"date": {"start": service_day}}
                reservation = self._active_reservation()
                calls = []

                def notion(method, path, body=None):
                    calls.append((method, path, body))
                    self.assertEqual(method, "GET")
                    return reservation if "reservation-page" in path else cleaning

                with patch.object(lifecycle_actions, "_notion", side_effect=notion):
                    result = lifecycle_actions.validate_cleaner_unavailable(self._unavailable_record())
                self.assertEqual(result["reservation_page_id"], "reservation-page")
                self.assertEqual(len(calls), 2)

    def test_cleaner_unavailable_validation_fails_closed_when_reservation_cancelled_or_work_started(self):
        cases = [
            (self._unavailable_cleaning(), self._active_reservation(state="취소"), "reservation is no longer active"),
            (self._unavailable_cleaning(state="진행중"), self._active_reservation(), "started, completed, or been cancelled"),
        ]
        for cleaning, reservation, error in cases:
            with self.subTest(error=error):
                calls = []

                def notion(method, path, body=None):
                    calls.append((method, path, body))
                    return reservation if "reservation-page" in path else cleaning

                with patch.object(lifecycle_actions, "_notion", side_effect=notion):
                    with self.assertRaisesRegex(ValueError, error):
                        lifecycle_actions.validate_cleaner_unavailable(self._unavailable_record())
                self.assertFalse(any(method == "PATCH" for method, _path, _body in calls))

    def test_cleaner_unavailable_validation_fails_closed_on_stale_version_or_changed_assignee(self):
        stale = self._unavailable_cleaning(last_edit="edit-new")
        changed = self._unavailable_cleaning(assignee="party-other")
        for cleaning, error in ((stale, "stale unavailable action"), (changed, "assignee changed")):
            with self.subTest(error=error):
                with patch.object(lifecycle_actions, "_notion", return_value=cleaning):
                    with self.assertRaisesRegex(ValueError, error):
                        lifecycle_actions.validate_cleaner_unavailable(self._unavailable_record())

    def test_unavailable_projection_is_idempotent_after_patch_response_loss(self):
        cleaning = self._unavailable_cleaning()
        reservation = self._active_reservation()
        patch_calls = []
        response_loss = [True]

        def notion(method, path, body=None):
            if method == "GET" and "reservation-page" in path:
                return reservation
            if method == "GET":
                return cleaning
            patch_calls.append((method, path, body))
            properties = body["properties"]
            cleaning["properties"]["배정 수락 상태"] = properties["배정 수락 상태"]
            cleaning["properties"]["담당 참여자/업체"] = properties["담당 참여자/업체"]
            cleaning["last_edited_time"] = "edit-2"
            if response_loss.pop():
                raise RuntimeError("response lost after commit")
            return cleaning

        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                lifecycle_actions.execute_cleaner_unavailable_projection(self._unavailable_record())
            recovered = lifecycle_actions.execute_cleaner_unavailable_projection(self._unavailable_record())
        self.assertEqual(len(patch_calls), 1)
        self.assertEqual(recovered["projection_write"], "ALREADY_CONVERGED")
        self.assertEqual(recovered["next_expected_last_edited_time"], "edit-2")

    def test_stale_day_confirm_or_start_cannot_progress_after_unavailable_projection(self):
        cleaning = self._unavailable_cleaning(acceptance="대체필요", assignee=None, last_edit="edit-2")
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            return cleaning

        old_action = {
            "action_type": "CLEANING_START",
            "cleaning_page_id": "cleaning-page",
            "candidate_party_page_id": "party",
            "expected_last_edited_time": "edit-1",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            with self.assertRaisesRegex(ValueError, "accepted"):
                lifecycle_actions.execute_cleaning_operation(old_action, "approve")
        self.assertFalse(any(method == "PATCH" for method, _path, _body in calls))


    def test_f06_unavailable_end_wins_stale_work_start_fails_on_durable_history_before_patch(self):
        cleaning = self._unavailable_cleaning(acceptance="수락", assignee="party", last_edit="edit-1")
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            return cleaning if method == "GET" else {"last_edited_time": "unexpected"}

        old_action = {
            "action_type": "CLEANING_START",
            "cleaning_page_id": "cleaning-page",
            "candidate_party_page_id": "party",
            "expected_last_edited_time": "edit-1",
        }
        with patch.object(lifecycle_actions, "_notion", side_effect=notion), \
             patch.object(
                 lifecycle_actions,
                 "_assert_effective_assignment_for_work_start",
                 side_effect=ValueError("cleaning work-start assignment is no longer effective"),
             ):
            with self.assertRaisesRegex(ValueError, "no longer effective"):
                lifecycle_actions.execute_cleaning_operation(old_action, "approve")
        self.assertFalse(any(method == "PATCH" for method, _path, _body in calls))

    def test_f06_work_start_wins_unavailable_final_read_fails_closed(self):
        cleaning = self._unavailable_cleaning(state="진행중", acceptance="수락", assignee="party", last_edit="edit-2")
        reservation = self._active_reservation()
        calls = []

        def notion(method, path, body=None):
            calls.append((method, path, body))
            return reservation if "reservation-page" in path else cleaning

        with patch.object(lifecycle_actions, "_notion", side_effect=notion):
            with self.assertRaisesRegex(ValueError, "started, completed, or been cancelled"):
                lifecycle_actions.validate_cleaner_unavailable(self._unavailable_record())
        self.assertFalse(any(method == "PATCH" for method, _path, _body in calls))


if __name__ == "__main__":
    unittest.main()
