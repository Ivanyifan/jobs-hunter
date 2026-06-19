from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

os.environ.pop("MONGO_URI", None)

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

warnings.filterwarnings(
    "ignore",
    message=".*_UnionGenericAlias.*",
    category=DeprecationWarning,
)

from application_questions import (
    BLOCKED_ON_QUESTIONS,
    NEEDS_TECHNICAL_REVIEW,
    READY_TO_RESUME,
    TECHNICAL_REVIEW,
    UNANSWERED,
    fingerprint_question,
    normalize_question_text,
)
from application_questions.detector import (
    detect_visible_required_questions,
    is_sensitive_question,
    outcome_status_for_questions,
)
from adapters.workday.browser import launch_replay_browser


FIXTURE_DIR = ROOT / "adapters" / "workday" / "fixtures"


class ApplicationQuestionDetectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = launch_replay_browser(cls.playwright, headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def open_fixture(self):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_content((FIXTURE_DIR / "application_questions.html").read_text(encoding="utf-8"), wait_until="domcontentloaded")
        self.addCleanup(context.close)
        return page

    def questions_by_text(self, page, approved_answers=None):
        questions = detect_visible_required_questions(page, approved_answers=approved_answers or {}, user_data={})
        return {question.normalized_text: question for question in questions}

    def test_unknown_required_radio_generates_one_blocker(self):
        questions = self.questions_by_text(self.open_fixture())
        key = normalize_question_text("Will you now or in the future require sponsorship?")

        self.assertIn(key, questions)
        question = questions[key]
        self.assertEqual(question.control_type, "radio")
        self.assertEqual(question.status, UNANSWERED)
        self.assertEqual(sorted(question.options), ["No", "Yes"])

    def test_unknown_required_select_extracts_options(self):
        questions = self.questions_by_text(self.open_fixture())
        key = normalize_question_text("Are you willing to relocate?")

        self.assertIn(key, questions)
        question = questions[key]
        self.assertEqual(question.control_type, "select")
        self.assertEqual(question.options, ["Yes", "No", "Depends on location"])

    def test_unknown_text_question_generates_blocker(self):
        questions = self.questions_by_text(self.open_fixture())
        key = normalize_question_text("Please describe why you are interested in this role.")

        self.assertIn(key, questions)
        self.assertEqual(questions[key].control_type, "textarea")

    def test_hidden_required_input_is_not_reported(self):
        questions = detect_visible_required_questions(self.open_fixture(), approved_answers={}, user_data={})

        self.assertFalse(any("hidden template" in question.normalized_text for question in questions))

    def test_same_application_questions_have_stable_fingerprint(self):
        first = self.questions_by_text(self.open_fixture())
        second = self.questions_by_text(self.open_fixture())
        key = normalize_question_text("Are you willing to relocate?")

        self.assertEqual(first[key].fingerprint, second[key].fingerprint)

    def test_same_fingerprint_can_aggregate_across_applications(self):
        left = fingerprint_question("Are you willing to relocate?", "select", ["No", "Yes"])
        right = fingerprint_question("are you willing to relocate required", "select", ["Yes", "No"])

        self.assertEqual(left, right)

    def test_approved_answer_with_validation_error_is_technical_review(self):
        page = self.open_fixture()
        fingerprint = fingerprint_question("Portfolio URL", "text", [])

        questions = detect_visible_required_questions(
            page,
            approved_answers={fingerprint: "https://portfolio.example.invalid"},
            user_data={},
        )
        technical = [question for question in questions if question.normalized_text == normalize_question_text("Portfolio URL")]

        self.assertEqual(len(technical), 1)
        self.assertEqual(technical[0].status, TECHNICAL_REVIEW)
        self.assertEqual(outcome_status_for_questions(technical), NEEDS_TECHNICAL_REVIEW)

    def test_sensitive_question_is_never_auto_answered(self):
        self.assertTrue(is_sensitive_question("Will you now or in the future require sponsorship?"))
        question = self.questions_by_text(self.open_fixture())[normalize_question_text("Will you now or in the future require sponsorship?")]

        self.assertEqual(question.status, UNANSWERED)

    def test_bridge_blocks_without_clicking_submit(self):
        server = importlib.import_module("mcp_servers.playwright_server")
        page = self.open_fixture()
        req = SimpleNamespace(
            allow_low_risk_autofill=True,
            allow_placeholder_autofill=False,
            allow_visual_field_fallback=False,
            application_id=None,
            batch_id="batch-local",
            url="file://application_questions.html",
        )
        with patch.object(server, "visual_field_fill_step", return_value={"acted": False}):
            result = server.fill_discovery_page_fields(page, [], {}, req)

        self.assertEqual(result["question_blocker"]["status"], BLOCKED_ON_QUESTIONS)
        self.assertEqual(page.evaluate("window.fixtureMetrics.nextClicks"), 0)
        self.assertEqual(page.evaluate("window.fixtureMetrics.submitClicks"), 0)


class ApplicationQuestionMemoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mongo_server = importlib.import_module("mcp_servers.mongo_server")
        self.mongo_server.mongo_client = None
        self.mongo_server.MONGO_CONFIGURED = False
        self.mongo_server.question_blocker_indexes_ready = False
        self.mongo_server.sqlite_db_path = str(Path(self.tmp.name) / "applications.db")
        self.mongo_server.init_sqlite()
        self.client = TestClient(self.mongo_server.app)

    def upsert_app(self, app_id: str, batch: str = "batch-a") -> None:
        response = self.client.post("/applications/upsert", json={
            "id": app_id,
            "company": f"Company {app_id}",
            "role": "Software Engineer",
            "resume_v0": "synthetic resume",
            "status": "Queued",
            "metadata": {"batch_id": batch},
        })
        self.assertEqual(response.status_code, 200, response.text)

    def create_blocker(self, app_id: str, question: str = "Are you willing to relocate?", batch: str = "batch-a", options=None, **extra):
        payload = {
            "batch_id": batch,
            "ats": "workday",
            "tenant": "fixture",
            "company": f"Company {app_id}",
            "role": "Software Engineer",
            "job_url": "https://example.invalid/job",
            "page_name": "Application Questions",
            "stage": "application_questions",
            "raw_text": question,
            "required": True,
            "control_type": "select",
            "options": options if options is not None else ["Yes", "No"],
            "status": UNANSWERED,
        }
        payload.update(extra)
        response = self.client.post(f"/applications/{app_id}/question-blockers", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def app_status(self, app_id: str) -> str:
        response = self.client.get("/applications", params={"limit": 50})
        self.assertEqual(response.status_code, 200, response.text)
        for app in response.json()["applications"]:
            if app["id"] == app_id:
                return app["status"]
        raise AssertionError(f"missing app {app_id}")

    def test_same_application_rerun_does_not_create_duplicate_blocker(self):
        self.upsert_app("app-1")

        first = self.create_blocker("app-1")
        second = self.create_blocker("app-1")
        listed = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"]

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(len(listed), 1)
        self.assertEqual(self.app_status("app-1"), BLOCKED_ON_QUESTIONS)

    def test_batch_scope_approval_only_affects_same_batch(self):
        for app_id, batch in [("app-1", "batch-a"), ("app-2", "batch-a"), ("app-3", "batch-b")]:
            self.upsert_app(app_id, batch=batch)
            self.create_blocker(app_id, batch=batch)
        first_id = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"][0]["id"]

        response = self.client.post(f"/question-blockers/{first_id}/approve", json={
            "answer": "Yes",
            "scope": "batch",
            "approved_by": "unit-test",
            "batch_id": "batch-a",
        })

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["approved_count"], 2)
        self.assertEqual(set(data["ready_to_resume_applications"]), {"app-1", "app-2"})
        self.assertEqual(self.app_status("app-1"), READY_TO_RESUME)
        self.assertEqual(self.app_status("app-2"), READY_TO_RESUME)
        self.assertEqual(self.app_status("app-3"), BLOCKED_ON_QUESTIONS)

    def test_options_incompatible_rejects_batch_approval(self):
        shared_fingerprint = "forced-same-fingerprint"
        self.upsert_app("app-1")
        self.upsert_app("app-2")
        first = self.create_blocker("app-1", options=["Yes", "No"], fingerprint=shared_fingerprint)
        self.create_blocker("app-2", options=["Agree", "Decline"], fingerprint=shared_fingerprint)

        response = self.client.post(f"/question-blockers/{first['blocker']['id']}/approve", json={
            "answer": "Yes",
            "scope": "batch",
            "approved_by": "unit-test",
            "batch_id": "batch-a",
        })

        self.assertEqual(response.status_code, 409)

    def test_illegal_status_fails_closed_to_unanswered(self):
        self.upsert_app("app-1")

        result = self.create_blocker("app-1", status="MADE_UP_STATUS")

        self.assertEqual(result["blocker"]["status"], UNANSWERED)
        self.assertEqual(result["application_status"], BLOCKED_ON_QUESTIONS)

    def test_sqlite_contract_uses_backend_independent_fingerprint(self):
        self.upsert_app("app-1")
        expected = fingerprint_question("Are you willing to relocate?", "select", ["No", "Yes"])

        result = self.create_blocker("app-1", options=["Yes", "No"])

        self.assertEqual(result["blocker"]["fingerprint"], expected)

    def test_grouped_endpoint_returns_status_counts(self):
        self.upsert_app("app-1")
        self.upsert_app("app-2")
        self.create_blocker("app-1")
        self.create_blocker("app-2")

        response = self.client.get("/question-blockers/groups")

        self.assertEqual(response.status_code, 200, response.text)
        groups = response.json()["groups"]
        self.assertEqual(groups[0]["occurrence_count"], 2)
        self.assertEqual(groups[0]["status_counts"][UNANSWERED], 2)


if __name__ == "__main__":
    unittest.main()
