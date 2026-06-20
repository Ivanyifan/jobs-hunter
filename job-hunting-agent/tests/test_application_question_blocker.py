from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch
import warnings

os.environ.pop("MONGO_URI", None)

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

warnings.filterwarnings(
    "ignore",
    message=".*_UnionGenericAlias.*",
    category=DeprecationWarning,
)

from application_questions import (
    APPROVED,
    BLOCKED_ON_QUESTIONS,
    NEEDS_TECHNICAL_REVIEW,
    READY_TO_RESUME,
    READY_TO_SUBMIT,
    SUBMITTING,
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
from frontend.apply_flow import (
    READY_TO_SUBMIT as FRONTEND_READY_TO_SUBMIT,
    approved_question_answers_for_application,
    apply_application_profile_library,
    build_playwright_apply_payload,
    can_confirm_submit,
    can_start_apply,
    enable_application_question_matcher,
    enrich_user_data_with_approved_question_answers,
    record_playwright_apply_failure,
)
from mcp_servers import playwright_server


FIXTURE_DIR = ROOT / "adapters" / "workday" / "fixtures"


class FakeResult:
    def __init__(self, matched_count=0, upserted_id=None, modified_count=0):
        self.matched_count = matched_count
        self.upserted_id = upserted_id
        self.modified_count = modified_count


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, key, direction):
        reverse = direction < 0
        self.rows.sort(key=lambda item: item.get(key) or "", reverse=reverse)
        return self

    def skip(self, count):
        self.rows = self.rows[count:]
        return self

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class FakeCollection:
    def __init__(self):
        self.docs = []

    def create_index(self, *_args, **_kwargs):
        return "fake-index"

    def _matches(self, doc, query):
        for key, expected in (query or {}).items():
            actual = doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif isinstance(expected, dict) and "$nin" in expected:
                if actual in expected["$nin"]:
                    return False
            elif actual != expected:
                return False
        return True

    def _apply_update(self, doc, update, inserting=False):
        if inserting:
            doc.update(update.get("$setOnInsert", {}))
        doc.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + value

    def insert_one(self, doc, **_kwargs):
        self.docs.append(dict(doc))
        return FakeResult(upserted_id=doc.get("_id") or doc.get("id"))

    def update_one(self, query, update, upsert=False, **_kwargs):
        for doc in self.docs:
            if self._matches(doc, query):
                self._apply_update(doc, update)
                return FakeResult(matched_count=1, modified_count=1)
        if not upsert:
            return FakeResult(matched_count=0)
        doc = {key: value for key, value in query.items() if not isinstance(value, dict)}
        self._apply_update(doc, update, inserting=True)
        if "_id" not in doc and "id" in doc:
            doc["_id"] = doc["id"]
        self.docs.append(doc)
        return FakeResult(matched_count=0, upserted_id=doc.get("_id") or doc.get("id"))

    def update_many(self, query, update, **_kwargs):
        modified = 0
        for doc in self.docs:
            if self._matches(doc, query):
                self._apply_update(doc, update)
                modified += 1
        return FakeResult(matched_count=modified, modified_count=modified)

    def find_one(self, query, projection=None, **_kwargs):
        for doc in self.docs:
            if self._matches(doc, query):
                return {key: value for key, value in doc.items() if not (projection or {}).get(key) == 0}
        return None

    def find(self, query=None, projection=None, **_kwargs):
        rows = []
        for doc in self.docs:
            if self._matches(doc, query or {}):
                rows.append({key: value for key, value in doc.items() if not (projection or {}).get(key) == 0})
        return FakeCursor(rows)

    def count_documents(self, query, limit=0, **_kwargs):
        count = sum(1 for doc in self.docs if self._matches(doc, query or {}))
        return min(count, limit) if limit else count

    def distinct(self, key, query=None, **_kwargs):
        return list({
            doc.get(key)
            for doc in self.docs
            if doc.get(key) is not None and self._matches(doc, query or {})
        })

    def aggregate(self, _pipeline):
        groups = {}
        for doc in sorted(self.docs, key=lambda item: item.get("created_at") or "", reverse=True):
            fingerprint = doc.get("fingerprint")
            group = groups.setdefault(fingerprint, {
                "_id": fingerprint,
                "question": doc.get("raw_text"),
                "normalized_text": doc.get("normalized_text"),
                "control_type": doc.get("control_type"),
                "options": doc.get("options"),
                "occurrence_count": 0,
                "application_ids": set(),
                "companies": set(),
                "roles": set(),
                "batch_ids": set(),
                "blocker_ids": set(),
                "validation_messages": set(),
                "occurrences": [],
                "statuses": [],
            })
            group["occurrence_count"] += 1
            group["application_ids"].add(doc.get("application_id"))
            group["companies"].add(doc.get("company"))
            group["roles"].add(doc.get("role"))
            group["batch_ids"].add(doc.get("batch_id"))
            group["blocker_ids"].add(doc.get("id"))
            group["validation_messages"].add(doc.get("validation_message"))
            group["statuses"].append(doc.get("status"))
            group["occurrences"].append({
                "id": doc.get("id"),
                "application_id": doc.get("application_id"),
                "company": doc.get("company"),
                "role": doc.get("role"),
                "batch_id": doc.get("batch_id"),
                "status": doc.get("status"),
                "artifacts": doc.get("artifacts"),
            })
        rows = []
        for group in groups.values():
            rows.append({
                **group,
                "application_ids": list(group["application_ids"]),
                "companies": list(group["companies"]),
                "roles": list(group["roles"]),
                "batch_ids": list(group["batch_ids"]),
                "blocker_ids": list(group["blocker_ids"]),
                "validation_messages": list(group["validation_messages"]),
            })
        return sorted(rows, key=lambda item: item["occurrence_count"], reverse=True)


class FakeMongoDB:
    def __init__(self):
        self.collections = {}

    def __getattr__(self, name):
        return self.collections.setdefault(name, FakeCollection())


class FakeMongoClient:
    def __init__(self):
        self.database = FakeMongoDB()

    def __getitem__(self, _name):
        return self.database

    def start_session(self):
        return FakeMongoSession()


class FakeMongoSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def with_transaction(self, callback):
        return callback(self)


class FrontendApplyFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "applications.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE mcp_applications (id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO mcp_applications (id, status) VALUES (?, ?)", ("app-1", "Applying"))
        conn.commit()
        conn.close()
        self.sync_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def db_connection(self):
        return sqlite3.connect(self.db_path)

    def sync_mongo_status(self, config, app_id, status, reason=None, metadata=None):
        self.sync_calls.append({
            "config": config,
            "app_id": app_id,
            "status": status,
            "reason": reason,
            "metadata": metadata or {},
        })

    def app_status(self):
        conn = sqlite3.connect(self.db_path)
        status = conn.execute("SELECT status FROM mcp_applications WHERE id = ?", ("app-1",)).fetchone()[0]
        conn.close()
        return status

    def record_failure(self, res_data):
        return record_playwright_apply_failure(
            {"active_batch_id": "batch-a"},
            self.db_connection,
            self.sync_mongo_status,
            "app-1",
            "Acme",
            "Software Engineer",
            "https://example.test/apply",
            res_data,
        )

    def test_frontend_preserves_blocked_on_questions_apply_response(self):
        result = self.record_failure({
            "success": False,
            "status": BLOCKED_ON_QUESTIONS,
            "blocked_reason": "application_question_blocker",
            "question_blocker": {"status": BLOCKED_ON_QUESTIONS, "blocker_ids": ["b1"]},
        })

        self.assertTrue(result["is_question_blocker"])
        self.assertEqual(result["status"], BLOCKED_ON_QUESTIONS)
        self.assertEqual(self.app_status(), BLOCKED_ON_QUESTIONS)
        self.assertEqual(self.sync_calls[-1]["status"], BLOCKED_ON_QUESTIONS)
        self.assertEqual(self.sync_calls[-1]["reason"], "playwright_application_question_blocker")
        self.assertNotEqual(self.sync_calls[-1]["status"], "Queued")

    def test_frontend_preserves_needs_technical_review_apply_response(self):
        result = self.record_failure({
            "success": False,
            "status": NEEDS_TECHNICAL_REVIEW,
            "blocked_reason": "application_question_blocker",
            "question_blocker": {"status": NEEDS_TECHNICAL_REVIEW},
        })

        self.assertTrue(result["is_question_blocker"])
        self.assertEqual(result["status"], NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(self.app_status(), NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(self.sync_calls[-1]["status"], NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(self.sync_calls[-1]["reason"], "playwright_application_question_blocker")
        self.assertNotEqual(self.sync_calls[-1]["status"], "Queued")

    def test_frontend_ordinary_apply_failure_returns_to_queued(self):
        result = self.record_failure({
            "success": False,
            "error": "browser timeout",
        })

        self.assertFalse(result["is_question_blocker"])
        self.assertEqual(result["status"], "Queued")
        self.assertEqual(self.app_status(), "Queued")
        self.assertEqual(self.sync_calls[-1]["status"], "Queued")
        self.assertEqual(self.sync_calls[-1]["reason"], "playwright_apply_failed")

    def test_frontend_ready_to_submit_response_is_not_requeued(self):
        result = self.record_failure({
            "success": False,
            "status": FRONTEND_READY_TO_SUBMIT,
            "blocked_reason": "final_submit_confirmation_required",
        })

        self.assertTrue(result["is_ready_to_submit"])
        self.assertEqual(result["status"], FRONTEND_READY_TO_SUBMIT)
        self.assertEqual(self.app_status(), FRONTEND_READY_TO_SUBMIT)
        self.assertEqual(self.sync_calls[-1]["status"], FRONTEND_READY_TO_SUBMIT)
        self.assertEqual(self.sync_calls[-1]["reason"], "playwright_ready_to_submit")

    def test_ready_to_resume_is_normal_runnable_state(self):
        self.assertTrue(can_start_apply(READY_TO_RESUME))
        self.assertTrue(can_start_apply("Queued"))
        self.assertTrue(can_start_apply("Applying"))
        self.assertFalse(can_confirm_submit(READY_TO_RESUME))

    def test_ready_to_submit_requires_final_confirmation_state(self):
        self.assertFalse(can_start_apply(FRONTEND_READY_TO_SUBMIT))
        self.assertTrue(can_confirm_submit(FRONTEND_READY_TO_SUBMIT))
        self.assertFalse(can_confirm_submit("Queued"))

    def test_normal_apply_payload_does_not_confirm_submit(self):
        payload = build_playwright_apply_payload(
            "https://example.test/apply",
            "http://localhost:8004/mock-form",
            "resume.pdf",
            {"application_id": "app-1"},
            "app-1",
            "batch-a",
        )

        self.assertNotIn("confirm_submit", payload)
        self.assertEqual(payload["url"], "https://example.test/apply")

    def test_frontend_enables_llm_library_matcher_for_playwright_payload(self):
        original = {"application_id": "app-1", "application_question_config": {"max_llm_match_calls_per_application": 3}}
        enriched = enable_application_question_matcher(original)

        self.assertIsNone(original.get("application_question_config", {}).get("enable_llm_library_matcher"))
        self.assertTrue(enriched["application_question_config"]["enable_llm_library_matcher"])
        self.assertTrue(enriched["application_question_config"]["enable_llm_library_matcher_for_sensitive_questions"])
        self.assertEqual(enriched["application_question_config"]["max_llm_match_calls_per_application"], 3)

    def test_application_profile_library_expands_to_playwright_user_data(self):
        enriched = apply_application_profile_library({
            "application_profile_library": {
                "education": {
                    "school": "University of Illinois at Urbana-Champaign",
                    "degree": "Bachelor's Degree",
                    "field": "Computer Science and Linguistics",
                    "end_month": "December",
                    "end_year": "2026",
                },
                "experiences": [{
                    "title": "Software Engineer Intern",
                    "company": "Volcengine",
                    "location": "Beijing, China",
                    "start_month": "May",
                    "start_year": "2024",
                    "end_month": "August",
                    "end_year": "2024",
                    "description": "Built B2B platform services.",
                }],
                "languages": [{"language": "English", "overall": "Professional Working Proficiency"}],
                "availability": {
                    "start_date": "December 2026",
                    "notice_period": "Two weeks after offer",
                },
            }
        })

        self.assertEqual(enriched["education_degree"], "Bachelor's Degree")
        self.assertEqual(enriched["education_end_month"], "December")
        self.assertEqual(enriched["work_experience_entries"][0]["company"], "Volcengine")
        self.assertEqual(enriched["language"], "English")
        self.assertEqual(enriched["language_overall"], "Professional Working Proficiency")
        self.assertEqual(enriched["start_date"], "December 2026")
        self.assertEqual(enriched["notice_period"], "Two weeks after offer")

    def test_final_confirmation_payload_sets_confirm_submit_only_when_explicit(self):
        unchecked_payload = build_playwright_apply_payload(
            "https://example.test/apply",
            "http://localhost:8004/mock-form",
            "resume.pdf",
            {"application_id": "app-1"},
            "app-1",
            "batch-a",
            confirm_submit=False,
        )
        checked_payload = build_playwright_apply_payload(
            "https://example.test/apply",
            "http://localhost:8004/mock-form",
            "resume.pdf",
            {"application_id": "app-1"},
            "app-1",
            "batch-a",
            confirm_submit=True,
        )

        self.assertNotIn("confirm_submit", unchecked_payload)
        self.assertIs(checked_payload["confirm_submit"], True)

    def test_enrich_user_data_with_approved_blocker_answers(self):
        calls = []

        def fake_memory(config, method, path, payload=None, timeout=6):
            calls.append((method, path))
            return {
                "blockers": [{
                    "fingerprint": "fp-1",
                    "normalized_text": "are you willing to relocate",
                    "raw_text": "Are you willing to relocate?",
                    "canonical_key": "relocation",
                    "approved_answer": "No",
                }],
                "pagination": {"has_more": False, "limit": 100, "offset": 0, "total": 1},
            }

        answers = approved_question_answers_for_application(
            {"mongo_url": "http://memory.test"},
            "app-1",
            call_memory_func=fake_memory,
        )
        enriched = enrich_user_data_with_approved_question_answers(
            {"mongo_url": "http://memory.test"},
            "app-1",
            {"profile": {"name": "Test User"}},
            call_memory_func=fake_memory,
        )

        self.assertEqual(answers["fp-1"], "No")
        self.assertEqual(answers["are you willing to relocate"], "No")
        self.assertEqual(answers["Are you willing to relocate?"], "No")
        self.assertEqual(answers["relocation"], "No")
        self.assertEqual(enriched["approved_question_answers"]["fp-1"], "No")
        self.assertEqual(enriched["question_blocker_answers"]["relocation"], "No")
        self.assertEqual(calls[0][0], "GET")


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

    def test_workday_selected_combobox_text_is_not_validation_error(self):
        page = self.open_probe_page("""
            <section>
              <label for="phoneCode">Country/Region Phone Code*</label>
              <input id="phoneCode" required aria-describedby="phoneSelected" value="">
              <div id="phoneSelected">1 item selected, United States of America (+1), press delete to clear value.</div>
            </section>
        """)

        questions = detect_visible_required_questions(page, approved_answers={}, user_data={})

        self.assertEqual(questions, [])

    def test_workday_capitalization_warning_with_value_is_not_blocking(self):
        page = self.open_probe_page("""
            <section>
              <label for="first">First Name*</label>
              <input id="first" required aria-describedby="firstWarning" value="YIFAN">
              <div id="firstWarning" role="alert">
                Alert: Verify that the field Given Name(s) is correctly capitalized because it contains more than 2 capital letters.
              </div>
            </section>
        """)

        questions = detect_visible_required_questions(page, approved_answers={}, user_data={"first_name": "Yifan"})

        self.assertEqual(questions, [])

    def test_workday_required_prompt_button_is_detected_from_star_label(self):
        page = self.open_probe_page("""
            <section>
              <label id="hear-label" for="hear">How Did You Hear About Us? *</label>
              <button id="hear" aria-haspopup="listbox" aria-labelledby="hear-label">Select One</button>
            </section>
        """)

        fields = playwright_server.extract_form_schema(page, {})
        questions = detect_visible_required_questions(page, approved_answers={}, user_data={})

        prompt_fields = [field for field in fields if "How Did You Hear" in field["label"]]
        self.assertEqual(len(prompt_fields), 1)
        self.assertTrue(prompt_fields[0]["required"])
        self.assertEqual(prompt_fields[0]["input_type"], "select")
        self.assertFalse(prompt_fields[0]["value_present"])
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].control_type, "select")

    def test_workday_language_selector_button_is_not_form_field(self):
        page = self.open_probe_page("""
            <header>
              <button id="languageSelectorButton" aria-haspopup="listbox">English</button>
              <button>Sign In</button>
            </header>
            <main>
              <h2>Browser Software Engineer Intern</h2>
              <a href="/apply">Apply</a>
            </main>
        """)

        fields = playwright_server.extract_form_schema(page, {})

        self.assertFalse(any(field.get("id") == "languageSelectorButton" for field in fields))

    def test_profile_work_experience_entries_override_resume_parser(self):
        entries = playwright_server.parse_resume_work_experiences(
            "No parseable work experience here.",
            max_items=1,
            user_data={
                "work_experience_entries": [{
                    "title": "Software Engineer Intern",
                    "company": "Volcengine",
                    "location": "Beijing, China",
                    "start_month": "May",
                    "start_year": "2024",
                    "end_month": "August",
                    "end_year": "2024",
                    "description": "Built platform services.",
                }]
            },
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["company"], "Volcengine")
        self.assertEqual(entries[0]["start"]["date"], "05/2024")
        self.assertEqual(entries[0]["end"]["date"], "08/2024")

    def test_low_risk_required_workday_prompt_selects_first_valid_option(self):
        page = self.open_probe_page("""
            <section>
              <label id="hear-label" for="hear">How Did You Hear About Us? *</label>
              <button
                id="hear"
                aria-haspopup="listbox"
                aria-labelledby="hear-label"
                onclick="document.getElementById('hear-options').hidden=false"
              >Select One</button>
              <ul id="hear-options" role="listbox" hidden>
                <li role="option" onclick="hear.textContent='Company Website'; hear.dataset.value='company'; hearOptions.hidden=true">Company Website</li>
                <li role="option" onclick="hear.textContent='LinkedIn'; hear.dataset.value='linkedin'; hearOptions.hidden=true">LinkedIn</li>
              </ul>
            </section>
            <script>
              const hear = document.getElementById('hear');
              const hearOptions = document.getElementById('hear-options');
            </script>
        """)
        req = self.probe_req()
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"application_id": "app-probe"},
                req,
            )

        self.assertEqual(page.locator("#hear").inner_text(), "Company Website")
        self.assertEqual(result["missing_required"], [])
        self.assertTrue(any(item.get("source") == "first_valid_select_probe" for item in result["filled"]))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["status"], UNANSWERED)
        self.assertEqual(captured[0]["options"], ["Company Website", "LinkedIn"])
        self.assertTrue(captured[0]["metadata"]["probe_answer_applied"])
        self.assertEqual(captured[0]["metadata"]["probe_answer"], "Company Website")
        self.assertTrue(result["question_blocker"]["requires_final_review"])
        self.assertEqual(result["question_blocker"]["probe_filled_count"], 1)

    def test_previous_worker_radio_defaults_to_no(self):
        page = self.open_probe_page("""
            <section>
              <fieldset>
                <legend>Have you previously been employed by HP? *</legend>
                <label><input id="prev-yes" name="candidateIsPreviousWorker" type="radio" required value="Yes"> Yes</label>
                <label><input id="prev-no" name="candidateIsPreviousWorker" type="radio" required value="No"> No</label>
              </fieldset>
            </section>
        """)
        req = self.probe_req()

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-previous-worker"},
            req,
        )

        self.assertFalse(page.locator("#prev-yes").is_checked())
        self.assertTrue(page.locator("#prev-no").is_checked())
        self.assertEqual(result["missing_required"], [])
        self.assertTrue(any(item.get("source") == "profile" for item in result["filled"]))
        self.assertIsNone(result.get("question_blocker"))

    def test_hidden_required_input_is_not_reported(self):
        questions = detect_visible_required_questions(self.open_fixture(), approved_answers={}, user_data={})

        self.assertFalse(any("hidden template" in question.normalized_text for question in questions))

    def test_optional_required_wording_is_not_reported(self):
        questions = detect_visible_required_questions(self.open_fixture(), approved_answers={}, user_data={})

        self.assertFalse(any("preferred nickname" in question.normalized_text for question in questions))
        self.assertFalse(any("required only for some applicants" in question.normalized_text for question in questions))

    def test_required_file_input_is_classified_explicitly(self):
        questions = self.questions_by_text(self.open_fixture())
        key = normalize_question_text("Upload supporting document")

        self.assertIn(key, questions)
        self.assertEqual(questions[key].control_type, "file")

    def test_disabled_required_repeated_control_is_not_reported(self):
        questions = detect_visible_required_questions(self.open_fixture(), approved_answers={}, user_data={})

        self.assertFalse(any("disabled repeated required field" in question.normalized_text for question in questions))

    def test_post_next_validation_error_is_detected(self):
        page = self.open_fixture()
        before = self.questions_by_text(page)
        self.assertNotIn(normalize_question_text("Post-next validation field"), before)

        page.click("#next-button")
        after = self.questions_by_text(page)

        key = normalize_question_text("Post-next validation field")
        self.assertIn(key, after)
        self.assertEqual(after[key].validation_message, "This answer is required after attempting to continue.")

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

    def test_playwright_to_memory_api_integration_uses_local_mock(self):
        server = importlib.import_module("mcp_servers.playwright_server")
        page = self.open_fixture()
        req = SimpleNamespace(
            allow_low_risk_autofill=True,
            allow_placeholder_autofill=False,
            allow_visual_field_fallback=False,
            application_id="app-local",
            batch_id="batch-local",
            url="file://application_questions.html",
        )

        class Response:
            status_code = 200
            text = "{}"

            def json(self):
                return {"created": True, "blocker": {"id": "mock-blocker"}}

        with patch("requests.post", return_value=Response()) as post:
            result = server.fill_discovery_page_fields(page, [], {"application_id": "app-local"}, req)

        self.assertEqual(result["question_blocker"]["status"], BLOCKED_ON_QUESTIONS)
        self.assertTrue(post.called)
        self.assertIn("/applications/app-local/question-blockers", post.call_args.args[0])
        self.assertEqual(page.evaluate("window.fixtureMetrics.submitClicks"), 0)

    def open_probe_page(self, html):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_content(html, wait_until="domcontentloaded")
        self.addCleanup(context.close)
        return page

    def probe_req(self, probe=True, confirm_submit=False):
        return SimpleNamespace(
            url="https://local.test/apply",
            application_id="app-probe",
            batch_id="batch-probe",
            resume_path="",
            max_form_pages=6,
            allow_low_risk_autofill=True,
            allow_placeholder_autofill=False,
            allow_visual_fallback=False,
            allow_visual_field_fallback=False,
            allow_resume_upload=False,
            probe_fill_unapproved_questions=probe,
            confirm_submit=confirm_submit,
        )

    def temp_resume_pdf(self):
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        handle.write(b"%PDF-1.4\n% test resume\n")
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.remove(handle.name))
        return handle.name

    def open_workday_autofill_page(self, html, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/autofillWithResume"):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.route("https://unit.myworkdayjobs.com/**", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(url, wait_until="domcontentloaded")
        self.addCleanup(context.close)
        return page

    def workday_upload_req(self, resume_path):
        return SimpleNamespace(
            resume_path=resume_path,
            allow_resume_upload=True,
        )

    def fake_vision_client(self, payload):
        class Response:
            text = json.dumps(payload)

        class Models:
            def generate_content(self, **_kwargs):
                return Response()

        return SimpleNamespace(models=Models())

    def test_visual_fallback_rejects_arbitrary_vision_click_coordinates(self):
        page = self.open_probe_page("""
            <main>
              <button id="safe" onclick="document.body.dataset.clicked='safe'">Continue</button>
            </main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        legacy_click_payload = {
            "action": "click",
            "target": "Continue",
            "x": 20,
            "y": 20,
            "reason": "old coordinate schema",
        }

        with (
            patch.object(playwright_server, "client", self.fake_vision_client(legacy_click_payload)),
            patch.object(page.mouse, "click", side_effect=AssertionError("vision coordinates must not click")),
        ):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertFalse(result["acted"])
        self.assertEqual(result["reason"], "vision_transition_not_allowed")
        self.assertIsNone(page.evaluate("document.body.dataset.clicked || null"))

    def test_visual_fallback_executes_only_allowed_deterministic_transition(self):
        page = self.open_probe_page("""
            <main>
              <button id="continue" onclick="document.body.dataset.clicked='continue'">Continue</button>
            </main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        classification_payload = {
            "observed_state": "application_form",
            "confidence": 0.94,
            "evidence": ["Continue button is visible"],
            "suggested_transition": "click_continue",
            "visible_controls": ["Continue"],
        }

        with (
            patch.object(playwright_server, "client", self.fake_vision_client(classification_payload)),
            patch.object(page.mouse, "click", side_effect=AssertionError("deterministic transition should not use mouse coordinates")),
        ):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertTrue(result["acted"])
        self.assertEqual(result["suggested_transition"], "click_continue")
        self.assertEqual(page.evaluate("document.body.dataset.clicked"), "continue")

    def test_visual_fallback_blocks_unallowed_transition(self):
        page = self.open_probe_page("""
            <main>
              <button id="danger" onclick="document.body.dataset.clicked='danger'">Delete Application</button>
            </main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        classification_payload = {
            "observed_state": "application_form",
            "confidence": 0.99,
            "evidence": ["A button is visible"],
            "suggested_transition": "click_delete_application",
            "visible_controls": ["Delete Application"],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(classification_payload)):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertFalse(result["acted"])
        self.assertEqual(result["reason"], "vision_transition_not_allowed")
        self.assertIsNone(page.evaluate("document.body.dataset.clicked || null"))

    def test_visual_fallback_final_submit_guard_overrides_allowed_transition(self):
        page = self.open_probe_page("""
            <main>
              <h1>Review Application</h1>
              <p>Ready to submit</p>
              <button id="submit" onclick="document.body.dataset.submitted='1'">Submit Application</button>
            </main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        classification_payload = {
            "observed_state": "final_review",
            "confidence": 0.99,
            "evidence": ["Review page is visible"],
            "suggested_transition": "click_continue",
            "visible_controls": ["Submit Application"],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(classification_payload)):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertFalse(result["acted"])
        self.assertEqual(result["stop_reason"], "final_submit_guard")
        self.assertIsNone(page.evaluate("document.body.dataset.submitted || null"))

    def test_workday_autofill_branch_uploads_visible_file_input(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h1>Autofill with Resume</h1>
              <label for="resume">Upload Resume</label>
              <input id="resume" type="file" onchange="ready.hidden=false">
              <section id="ready" hidden>
                <label for="given">Given Name</label>
                <input id="given" name="given">
              </section>
            </main>
        """)
        resume_path = self.temp_resume_pdf()

        result = playwright_server.handle_workday_autofill_with_resume_branch(page, self.workday_upload_req(resume_path))

        self.assertTrue(result["attempted"])
        self.assertTrue(result["uploaded"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["method"], "input[type=file]")
        self.assertIn(Path(resume_path).name, page.locator("#resume").input_value())

    def test_workday_autofill_branch_upload_button_uses_file_chooser(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h1>Autofill with Resume</h1>
              <button id="choose" onclick="
                const input = document.createElement('input');
                input.type = 'file';
                input.onchange = () => { document.body.dataset.uploaded = '1'; ready.hidden = false; };
                document.body.appendChild(input);
                input.click();
              ">Choose File</button>
              <section id="ready" hidden>
                <label for="given">Given Name</label>
                <input id="given" name="given">
              </section>
            </main>
        """)
        resume_path = self.temp_resume_pdf()

        result = playwright_server.handle_workday_autofill_with_resume_branch(page, self.workday_upload_req(resume_path))

        self.assertTrue(result["attempted"])
        self.assertTrue(result["uploaded"])
        self.assertTrue(result["ready"])
        self.assertTrue(result["method"].startswith("file_chooser:"))
        self.assertEqual(page.evaluate("document.body.dataset.uploaded"), "1")

    def test_workday_autofill_branch_blank_without_upload_ui_returns_timeout(self):
        page = self.open_workday_autofill_page("""
            <main>
              <nav>My Information My Experience Application Questions Review</nav>
              <p>Follow Us</p>
            </main>
        """)
        resume_path = self.temp_resume_pdf()

        with patch.object(page, "wait_for_timeout", return_value=None), patch.object(playwright_server.time, "time", side_effect=[0, 1, 46, 46, 46]):
            result = playwright_server.handle_workday_autofill_with_resume_branch(page, self.workday_upload_req(resume_path))

        self.assertTrue(result["attempted"])
        self.assertFalse(result["uploaded"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "workday_autofill_upload_ui_not_found")
        self.assertTrue(result["autofill_blank_timeout"])
        self.assertTrue(result["screenshot_path"])

    def test_workday_autofill_uploaded_success_clicks_continue_to_fields(self):
        page = self.open_workday_autofill_page("""
            <main>
              <section id="uploaded">
                <h1>Autofill with Resume</h1>
                <p>Successfully Uploaded!</p>
                <p>test resume.pdf</p>
                <button id="continue" onclick="
                  document.getElementById('uploaded').hidden = true;
                  document.getElementById('ready').hidden = false;
                  history.pushState({}, '', '/en-US/test/job/R0001/apply/myInformation');
                ">Continue</button>
              </section>
              <section id="ready" hidden>
                <h1>My Information</h1>
                <label for="given">Given Name</label>
                <input id="given" name="given">
                <label for="family">Family Name</label>
                <input id="family" name="family">
              </section>
            </main>
        """)
        resume_path = self.temp_resume_pdf()

        result = playwright_server.handle_workday_autofill_with_resume_branch(page, self.workday_upload_req(resume_path))

        self.assertTrue(result["uploaded"])
        self.assertTrue(result["success_marker"])
        self.assertTrue(result["continued_after_upload"])
        self.assertTrue(result["ready"])
        self.assertGreater(result["field_count"], 0)
        self.assertEqual(result["reason"], "workday_autofill_continue_reached_form")
        self.assertFalse(page.locator("#ready").is_hidden())

    def test_workday_autofill_uploaded_success_without_continue_returns_not_found(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h1>Autofill with Resume</h1>
              <p>Upload Complete</p>
              <p>test resume.pdf</p>
              <button id="back" onclick="document.body.dataset.clicked='back'">Back</button>
            </main>
        """)
        resume_path = self.temp_resume_pdf()

        with patch.object(playwright_server, "capture_apply_screenshot", return_value="mock.png"):
            result = playwright_server.handle_workday_autofill_with_resume_branch(page, self.workday_upload_req(resume_path))

        self.assertTrue(result["uploaded"])
        self.assertFalse(result["continued_after_upload"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "workday_autofill_continue_not_found")
        self.assertTrue(result["autofill_blank_timeout"])
        self.assertIsNone(page.evaluate("document.body.dataset.clicked || null"))

    def test_workday_autofill_uploaded_continue_parse_timeout(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h1>Autofill with Resume</h1>
              <p>Resume Uploaded</p>
              <button id="continue" onclick="document.body.dataset.continued='1'">Continue</button>
            </main>
        """)
        resume_path = self.temp_resume_pdf()

        with (
            patch.object(page, "wait_for_timeout", return_value=None),
            patch.object(playwright_server, "capture_apply_screenshot", return_value="mock.png"),
            patch.object(playwright_server.time, "time", side_effect=[0, 1, 2, 3, 49, 50]),
        ):
            result = playwright_server.handle_workday_autofill_with_resume_branch(page, self.workday_upload_req(resume_path))

        self.assertTrue(result["uploaded"])
        self.assertTrue(result["continued_after_upload"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "workday_autofill_continue_parse_timeout")
        self.assertTrue(result["autofill_blank_timeout"])
        self.assertEqual(page.evaluate("document.body.dataset.continued"), "1")

    def test_workday_autofill_uploaded_success_does_not_click_final_submit(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h1>Autofill with Resume</h1>
              <p>Successfully Uploaded!</p>
              <button id="submit" onclick="document.body.dataset.submitted='1'">Submit Application</button>
            </main>
        """)
        resume_path = self.temp_resume_pdf()

        with patch.object(playwright_server, "capture_apply_screenshot", return_value="mock.png"):
            result = playwright_server.handle_workday_autofill_with_resume_branch(page, self.workday_upload_req(resume_path))

        self.assertEqual(result["reason"], "workday_autofill_continue_not_found")
        self.assertFalse(result["continued_after_upload"])
        self.assertIsNone(page.evaluate("document.body.dataset.submitted || null"))

    def test_workday_autofill_timeout_prefers_apply_manually_next(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h1>Start Your Application</h1>
              <button onclick="document.body.dataset.choice='autofill'">Autofill with Resume</button>
              <button onclick="document.body.dataset.choice='manual'">Apply Manually</button>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply")
        resume_path = self.temp_resume_pdf()
        req = self.workday_upload_req(resume_path)

        prefer_resume = playwright_server.workday_resume_autofill_allowed(req, [{"autofill_blank_timeout": True}])
        clicked, label = playwright_server.click_workday_application_choice(page, prefer_resume=prefer_resume)

        self.assertFalse(prefer_resume)
        self.assertTrue(clicked)
        self.assertEqual(label, "Apply Manually")
        self.assertEqual(page.evaluate("document.body.dataset.choice"), "manual")

    def capture_blockers(self):
        captured = []

        def fake_persist(_application_id, payload):
            blocker = dict(payload)
            blocker["id"] = f"blocker-{len(captured) + 1}"
            captured.append(blocker)
            return {"created": True, "blocker": blocker, "memory_persisted": True}

        return captured, fake_persist

    def test_probe_fills_required_select_and_continues_to_final_review_guard(self):
        page = self.open_probe_page("""
            <section id="page1">
              <label for="relocate">Are you willing to relocate?</label>
              <select id="relocate" required>
                <option value="">Choose an answer</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
              <button id="next" onclick="page1.hidden=true; final.hidden=false">Continue</button>
            </section>
            <section id="final" hidden>
              <button id="submit" onclick="window.submitted=true">Submit Application</button>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.discover_application_steps(page, self.probe_req(), {"application_id": "app-probe"})

        self.assertEqual(result["stop_reason"], "application_question_blocker")
        self.assertEqual(page.locator("#relocate").input_value(), "Yes")
        self.assertGreaterEqual(len(result["pages"]), 2)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["status"], UNANSWERED)
        self.assertTrue(captured[0]["metadata"]["probe_answer_applied"])
        self.assertEqual(captured[0]["metadata"]["probe_answer"], "Yes")
        self.assertTrue(captured[0]["metadata"]["conditional_branch_probe"])
        self.assertEqual(captured[0]["metadata"]["branch_probe_answer"], "Yes")
        self.assertIn("Later questions", captured[0]["metadata"]["branch_probe_warning"])
        self.assertEqual(result["question_blocker"]["status"], BLOCKED_ON_QUESTIONS)

    def test_multiple_probe_pages_return_one_consolidated_bundle_before_submit(self):
        page = self.open_probe_page("""
            <section id="page1">
              <label for="relocate">Are you willing to relocate?</label>
              <select id="relocate" required>
                <option value="">Choose an answer</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
              <button onclick="page1.hidden=true; page2.hidden=false">Continue</button>
            </section>
            <section id="page2" hidden>
              <fieldset>
                <legend>Can you work weekends?</legend>
                <label><input type="radio" name="weekends" required value="Yes">Yes</label>
                <label><input type="radio" name="weekends" required value="No">No</label>
              </fieldset>
              <button onclick="page2.hidden=true; final.hidden=false">Continue</button>
            </section>
            <section id="final" hidden>
              <button onclick="window.submitted=true">Submit Application</button>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.discover_application_steps(page, self.probe_req(), {"application_id": "app-probe"})

        self.assertEqual(result["stop_reason"], "application_question_blocker")
        self.assertEqual(len(captured), 2)
        self.assertEqual(len(result["question_blocker"]["blocker_ids"]), 2)
        self.assertEqual(result["question_blocker"]["probe_filled_count"], 2)
        self.assertFalse(page.evaluate("Boolean(window.submitted)"))

    def test_final_submit_guard_blocks_unresolved_probe_blockers_even_when_confirmed(self):
        page = self.open_probe_page("""
            <section id="page1">
              <label for="relocate">Are you willing to relocate?</label>
              <select id="relocate" required>
                <option value="">Choose an answer</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
              <button onclick="page1.hidden=true; final.hidden=false">Continue</button>
            </section>
            <section id="final" hidden>
              <button onclick="window.submitted=true">Submit Application</button>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.submit_application_steps(page, self.probe_req(confirm_submit=True), {"application_id": "app-probe"})

        self.assertFalse(result["success"])
        self.assertEqual(result["blocked_reason"], "application_question_blocker")
        self.assertEqual(result["question_blocker"]["status"], BLOCKED_ON_QUESTIONS)
        self.assertTrue(captured[0]["metadata"]["probe_answer_applied"])
        self.assertFalse(page.evaluate("Boolean(window.submitted)"))

    def test_trusted_answer_fills_directly_without_unresolved_blocker(self):
        page = self.open_probe_page("""
            <section id="page1">
              <label for="relocate">Are you willing to relocate?</label>
              <select id="relocate" required>
                <option value="">Choose an answer</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
              <button onclick="page1.hidden=true; final.hidden=false">Continue</button>
            </section>
            <section id="final" hidden>
              <button>Submit Application</button>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.discover_application_steps(
                page,
                self.probe_req(),
                {"application_id": "app-probe", "approved_answers": {"Are you willing to relocate?": "No"}},
            )

        self.assertEqual(page.locator("#relocate").input_value(), "No")
        self.assertEqual(captured, [])
        self.assertEqual(result["stop_reason"], "final_submit_guard")
        self.assertIsNone(result.get("question_blocker"))

    def test_playwright_replays_approved_question_answer_without_duplicate_blocker(self):
        page = self.open_probe_page("""
            <section>
              <label for="relocate">Are you willing to relocate?</label>
              <select id="relocate" required>
                <option value="">Choose an answer</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"approved_question_answers": {"Are you willing to relocate?": "No"}},
                self.probe_req(),
            )

        self.assertEqual(page.locator("#relocate").input_value(), "No")
        self.assertEqual(captured, [])
        self.assertEqual(result["missing_required"], [])
        self.assertTrue(result["question_blocker"]["all_questions_resolved_by_trusted_answers"])

    def test_ready_to_resume_replay_reaches_ready_to_submit_without_clicking_final_submit(self):
        page = self.open_probe_page("""
            <section id="page1">
              <label for="relocate">Are you willing to relocate?</label>
              <select id="relocate" required>
                <option value="">Choose an answer</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
              <button onclick="page1.hidden=true; final.hidden=false">Continue</button>
            </section>
            <section id="final" hidden>
              <button onclick="window.submitted=true">Submit Application</button>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.submit_application_steps(
                page,
                self.probe_req(confirm_submit=False),
                {
                    "application_id": "app-probe",
                    "approved_question_answers": {"Are you willing to relocate?": "No"},
                },
            )

        self.assertEqual(captured, [])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], READY_TO_SUBMIT)
        self.assertEqual(result["blocked_reason"], "final_submit_confirmation_required")
        self.assertFalse(page.evaluate("Boolean(window.submitted)"))

    def test_certification_question_is_not_probe_filled_and_stops_immediately(self):
        page = self.open_probe_page("""
            <section>
              <label><input type="checkbox" id="certify" required>I certify this information is true and complete</label>
              <button onclick="window.nextClicked=true">Continue</button>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.discover_application_steps(page, self.probe_req(), {"application_id": "app-probe"})

        self.assertEqual(result["stop_reason"], "application_question_blocker")
        self.assertFalse(page.locator("#certify").is_checked())
        self.assertEqual(captured[0]["status"], TECHNICAL_REVIEW)
        self.assertFalse(captured[0]["metadata"]["probe_answer_applied"])
        self.assertIn("probe_stop_reason", captured[0]["metadata"])
        self.assertFalse(page.evaluate("Boolean(window.nextClicked)"))

    def test_probe_filled_answer_is_never_marked_approved(self):
        page = self.open_probe_page("""
            <section>
              <label for="interest">Why are you interested?</label>
              <textarea id="interest" required></textarea>
              <button onclick="window.nextClicked=true">Continue</button>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {"application_id": "app-probe"}),
                {"application_id": "app-probe"},
                self.probe_req(),
            )

        self.assertEqual(result["missing_required"], [])
        self.assertEqual(page.locator("#interest").input_value(), "TO_BE_REVIEWED_BY_APPLICANT")
        self.assertEqual(captured[0]["status"], UNANSWERED)
        self.assertIsNone(captured[0]["approved_answer"])
        self.assertTrue(captured[0]["metadata"]["probe_answer_applied"])

    def test_strict_mode_still_stops_on_unapproved_required_question(self):
        page = self.open_probe_page("""
            <section>
              <label for="relocate">Are you willing to relocate?</label>
              <select id="relocate" required>
                <option value="">Choose an answer</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
              <button onclick="window.nextClicked=true">Continue</button>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.discover_application_steps(page, self.probe_req(probe=False), {"application_id": "app-probe"})

        self.assertEqual(result["stop_reason"], "application_question_blocker")
        self.assertEqual(page.locator("#relocate").input_value(), "")
        self.assertEqual(captured[0]["status"], UNANSWERED)
        self.assertFalse(page.evaluate("Boolean(window.nextClicked)"))

    def test_trusted_alias_answers_fill_sensitive_sponsorship_and_work_authorization(self):
        page = self.open_probe_page("""
            <section>
              <fieldset data-question="sponsorship">
                <legend>Will you now or in the future require sponsorship?</legend>
                <label><input type="radio" name="sponsorship" required value="Yes">Yes</label>
                <label><input type="radio" name="sponsorship" required value="No">No</label>
              </fieldset>
              <fieldset data-question="work-auth">
                <legend>Are you legally authorized to work in the United States?</legend>
                <label><input type="radio" name="work_authorization" required value="Yes">Yes</label>
                <label><input type="radio" name="work_authorization" required value="No">No</label>
              </fieldset>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"profile": {"sponsorship": "No", "work_authorization": "Yes"}},
                self.probe_req(),
            )

        self.assertEqual(captured, [])
        self.assertEqual(result["missing_required"], [])
        self.assertTrue(page.locator('input[name="sponsorship"][value="No"]').is_checked())
        self.assertTrue(page.locator('input[name="work_authorization"][value="Yes"]').is_checked())
        match_methods = {
            item.get("answer_source_key"): item.get("answer_match_method")
            for item in result["question_blocker"]["trusted_filled"]
        }
        self.assertEqual(match_methods["sponsorship"], "alias")
        self.assertEqual(match_methods["work_authorization"], "alias")

    def test_llm_matcher_cannot_return_answer_not_in_library(self):
        question = SimpleNamespace(
            raw_text="Do you prefer remote work?",
            normalized_text=normalize_question_text("Do you prefer remote work?"),
            fingerprint=fingerprint_question("Do you prefer remote work?", "select", ["Yes", "No"]),
            canonical_key="",
            control_type="select",
            options=["Yes", "No"],
            locator_hints={},
        )

        with patch.object(playwright_server, "llm_match_trusted_answer", return_value={
            "matched_library_key": "remote_preference",
            "answer": "Maybe",
            "confidence": 0.99,
            "reason": "bad mock answer",
            "safe_to_autofill": True,
        }):
            match = playwright_server.match_question_to_trusted_answer(
                question,
                {
                    "common_answers": {"remote_preference": "No"},
                    "enable_llm_library_matcher": True,
                },
            )

        self.assertFalse(match["matched"])
        self.assertIn("existing trusted library answer", match["reason"])

    def test_llm_matcher_is_disabled_by_default(self):
        question = SimpleNamespace(
            raw_text="Do you prefer remote work?",
            normalized_text=normalize_question_text("Do you prefer remote work?"),
            fingerprint=fingerprint_question("Do you prefer remote work?", "select", ["Yes", "No"]),
            canonical_key="",
            control_type="select",
            options=["Yes", "No"],
            locator_hints={},
        )

        with patch.object(playwright_server, "llm_match_trusted_answer") as llm_mock:
            match = playwright_server.match_question_to_trusted_answer(
                question,
                {"common_answers": {"remote_preference": "No"}},
            )

        self.assertFalse(match["matched"])
        llm_mock.assert_not_called()

    def test_low_confidence_llm_match_creates_suggestion_without_autofill(self):
        page = self.open_probe_page("""
            <section>
              <label for="remote">Do you prefer remote work?</label>
              <select id="remote" required>
                <option value="">Choose an answer</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist), \
             patch.object(playwright_server, "llm_match_trusted_answer", return_value={
                 "matched_library_key": "remote_preference",
                 "answer": "Yes",
                 "confidence": 0.80,
                 "reason": "semantic match but not certain",
                 "safe_to_autofill": True,
             }):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {
                    "common_answers": {"remote_preference": "Yes"},
                    "enable_llm_library_matcher": True,
                },
                self.probe_req(),
            )

        self.assertEqual(page.locator("#remote").input_value(), "Yes")
        self.assertEqual(result["missing_required"], [])
        self.assertEqual(result["question_blocker"]["probe_filled_count"], 1)
        self.assertTrue(result["question_blocker"]["requires_final_review"])
        self.assertEqual(captured[0]["status"], UNANSWERED)
        self.assertEqual(captured[0]["metadata"]["suggested_answer"], "Yes")
        self.assertTrue(captured[0]["metadata"]["suggested_answer_requires_review"])
        self.assertTrue(captured[0]["metadata"]["probe_answer_applied"])
        self.assertEqual(captured[0]["metadata"]["probe_answer"], "Yes")
        self.assertEqual(captured[0]["metadata"]["answer_match_method"], "llm")
        self.assertEqual(captured[0]["metadata"]["answer_match_confidence"], 0.80)

    def test_sensitive_low_confidence_llm_match_requires_review_without_probe(self):
        page = self.open_probe_page("""
            <section>
              <fieldset data-question="sponsorship">
                <legend>Will you now or in the future require sponsorship?</legend>
                <label><input type="radio" name="sponsorship" required value="Yes">Yes</label>
                <label><input type="radio" name="sponsorship" required value="No">No</label>
              </fieldset>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist), \
             patch.object(playwright_server, "llm_match_trusted_answer", return_value={
                 "matched_library_key": "future_sponsorship",
                 "answer": "No",
                 "confidence": 0.80,
                 "reason": "sensitive match needs review",
                 "safe_to_autofill": True,
             }):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {
                    "common_answers": {"future_sponsorship": "No"},
                    "enable_llm_library_matcher": True,
                    "enable_llm_library_matcher_for_sensitive_questions": True,
                },
                self.probe_req(),
            )

        self.assertEqual(result["missing_required"][0]["field"], "Application question blocker")
        self.assertFalse(page.locator('input[name="sponsorship"][value="No"]').is_checked())
        self.assertEqual(captured[0]["status"], UNANSWERED)
        self.assertEqual(captured[0]["metadata"]["suggested_answer"], "No")
        self.assertTrue(captured[0]["metadata"]["suggested_answer_requires_review"])
        self.assertNotIn("probe_answer_applied", captured[0]["metadata"])

    def test_radio_group_matching_does_not_cross_yes_no_groups_for_trusted_answers(self):
        page = self.open_probe_page("""
            <section>
              <fieldset data-question="sponsorship">
                <legend>Will you now or in the future require sponsorship?</legend>
                <label><input type="radio" name="sponsorship" required value="Yes">Yes</label>
                <label><input type="radio" name="sponsorship" required value="No">No</label>
              </fieldset>
              <fieldset data-question="relocation">
                <legend>Are you willing to relocate?</legend>
                <label><input type="radio" name="relocation" required value="Yes">Yes</label>
                <label><input type="radio" name="relocation" required value="No">No</label>
              </fieldset>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"common_answers": {"sponsorship": "No"}},
                self.probe_req(probe=False),
            )

        self.assertTrue(page.locator('input[name="sponsorship"][value="No"]').is_checked())
        self.assertFalse(page.locator('input[name="relocation"][value="No"]').is_checked())
        self.assertFalse(page.locator('input[name="relocation"][value="Yes"]').is_checked())
        self.assertEqual(len(captured), 1)
        self.assertIn("relocate", captured[0]["normalized_text"])

    def test_radio_group_matching_does_not_cross_yes_no_groups_for_probe_answers(self):
        page = self.open_probe_page("""
            <section>
              <fieldset data-question="sponsorship">
                <legend>Will you now or in the future require sponsorship?</legend>
                <label><input type="radio" name="sponsorship" required value="Yes">Yes</label>
                <label><input type="radio" name="sponsorship" required value="No">No</label>
              </fieldset>
              <fieldset data-question="relocation">
                <legend>Are you willing to relocate?</legend>
                <label><input type="radio" name="relocation" required value="Yes">Yes</label>
                <label><input type="radio" name="relocation" required value="No">No</label>
              </fieldset>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"application_id": "app-probe"},
                self.probe_req(),
            )

        self.assertFalse(page.locator('input[name="sponsorship"][value="Yes"]').is_checked())
        self.assertFalse(page.locator('input[name="sponsorship"][value="No"]').is_checked())
        self.assertTrue(page.locator('input[name="relocation"][value="Yes"]').is_checked())
        sponsorship = next(item for item in captured if "sponsorship" in item["normalized_text"])
        self.assertEqual(sponsorship["metadata"]["probe_stop_reason"], "sensitive_question_requires_trusted_answer")
        relocation = next(item for item in captured if "relocate" in item["normalized_text"])
        self.assertTrue(relocation["metadata"]["conditional_branch_probe"])
        self.assertEqual(relocation["metadata"]["branch_probe_answer"], "Yes")

    def test_unrelated_validation_error_does_not_mark_probe_as_technical_review(self):
        page = self.open_probe_page("""
            <section>
              <div class="validation-error" role="alert">Error - unrelated previous section is required.</div>
              <div class="form-group" data-question="relocation">
                <label for="relocate">Are you willing to relocate?</label>
                <select id="relocate" required>
                  <option value="">Choose an answer</option>
                  <option value="Yes">Yes</option>
                  <option value="No">No</option>
                </select>
              </div>
            </section>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"application_id": "app-probe"},
                self.probe_req(),
            )

        self.assertEqual(result["missing_required"], [])
        self.assertEqual(captured[0]["status"], UNANSWERED)
        self.assertTrue(captured[0]["metadata"]["probe_answer_applied"])
        self.assertNotIn("probe_fill_failed", captured[0]["metadata"])


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

    def upsert_app(self, app_id: str, batch: str = "batch-a", status: str = "Queued") -> None:
        response = self.client.post("/applications/upsert", json={
            "id": app_id,
            "company": f"Company {app_id}",
            "role": "Software Engineer",
            "resume_v0": "synthetic resume",
            "status": status,
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

    def post_blocker(self, app_id: str, payload: dict):
        return self.client.post(f"/applications/{app_id}/question-blockers", json=payload)

    def blocker_request(self, question: str = "Are you willing to relocate?", batch: str = "batch-a", options=None, **extra):
        payload = {
            "batch_id": batch,
            "ats": "workday",
            "tenant": "fixture",
            "company": "Company app-1",
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
        return self.mongo_server.QuestionBlockerCreateRequest(**payload)

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

    def test_approval_then_duplicate_create_does_not_regress_status(self):
        self.upsert_app("app-1")
        created = self.create_blocker("app-1")
        blocker_id = created["blocker"]["id"]
        approved = self.client.post(f"/question-blockers/{blocker_id}/approve", json={
            "answer": "Yes",
            "scope": "application",
            "approved_by": "unit-test",
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.app_status("app-1"), READY_TO_RESUME)

        duplicate = self.create_blocker("app-1")

        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["blocker"]["status"], APPROVED)
        self.assertEqual(self.app_status("app-1"), READY_TO_RESUME)

    def test_forged_fingerprint_cannot_group_different_questions(self):
        self.upsert_app("app-1")
        self.upsert_app("app-2")
        first = self.create_blocker("app-1", question="Are you willing to relocate?")
        forged = {
            "batch_id": "batch-a",
            "raw_text": "Please describe why you want this role.",
            "control_type": "textarea",
            "options": [],
            "fingerprint": first["blocker"]["fingerprint"],
        }

        response = self.post_blocker("app-2", forged)

        self.assertEqual(response.status_code, 422)

    def test_batch_scope_approval_only_affects_same_batch(self):
        first = None
        for app_id, batch in [("app-1", "batch-a"), ("app-2", "batch-a"), ("app-3", "batch-b")]:
            self.upsert_app(app_id, batch=batch)
            created = self.create_blocker(app_id, batch=batch)
            if app_id == "app-1":
                first = created

        response = self.client.post(f"/question-blocker-groups/{first['blocker']['fingerprint']}/approve", json={
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

    def test_group_batch_approval_does_not_use_first_stored_occurrence(self):
        self.upsert_app("app-a", batch="batch-a")
        first = self.create_blocker("app-a", batch="batch-a")
        for app_id in ["app-b1", "app-b2"]:
            self.upsert_app(app_id, batch="batch-b")
            self.create_blocker(app_id, batch="batch-b")

        response = self.client.post(f"/question-blocker-groups/{first['blocker']['fingerprint']}/approve", json={
            "answer": "Yes",
            "scope": "batch",
            "approved_by": "unit-test",
            "batch_id": "batch-b",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["affected_blocker_count"], 2)
        self.assertEqual(set(response.json()["ready_to_resume_applications"]), {"app-b1", "app-b2"})
        self.assertEqual(self.app_status("app-a"), BLOCKED_ON_QUESTIONS)
        self.assertEqual(self.app_status("app-b1"), READY_TO_RESUME)
        self.assertEqual(self.app_status("app-b2"), READY_TO_RESUME)

    def test_application_scope_approves_exact_selected_application(self):
        self.upsert_app("app-1")
        self.upsert_app("app-2")
        self.create_blocker("app-1")
        selected = self.create_blocker("app-2")

        response = self.client.post(f"/question-blockers/{selected['blocker']['id']}/approve", json={
            "answer": "yes",
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["approved_count"], 1)
        self.assertEqual(self.app_status("app-2"), READY_TO_RESUME)
        self.assertEqual(self.app_status("app-1"), BLOCKED_ON_QUESTIONS)

    def test_batch_mismatch_rejects_approval(self):
        self.upsert_app("app-1", batch="batch-a")
        blocker = self.create_blocker("app-1", batch="batch-a")

        response = self.client.post(f"/question-blocker-groups/{blocker['blocker']['fingerprint']}/approve", json={
            "answer": "Yes",
            "scope": "batch",
            "approved_by": "unit-test",
            "batch_id": "batch-b",
        })

        self.assertIn(response.status_code, {404, 409})
        self.assertEqual(self.app_status("app-1"), BLOCKED_ON_QUESTIONS)

    def test_batch_approval_preserves_technical_review_blockers(self):
        self.upsert_app("app-1", batch="batch-a")
        self.upsert_app("app-2", batch="batch-a")
        first = self.create_blocker("app-1", batch="batch-a")
        technical = self.create_blocker("app-2", batch="batch-a", status=TECHNICAL_REVIEW)

        response = self.client.post(f"/question-blocker-groups/{first['blocker']['fingerprint']}/approve", json={
            "answer": "Yes",
            "scope": "batch",
            "approved_by": "unit-test",
            "batch_id": "batch-a",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["approved_count"], 1)
        second = self.client.get("/question-blockers", params={"application_id": "app-2"}).json()["blockers"][0]
        self.assertEqual(second["id"], technical["blocker"]["id"])
        self.assertEqual(second["status"], TECHNICAL_REVIEW)
        self.assertEqual(self.app_status("app-2"), NEEDS_TECHNICAL_REVIEW)

    def test_technical_review_then_unanswered_preserves_technical_status(self):
        self.upsert_app("app-1")
        self.create_blocker("app-1", question="Needs technical review?", status=TECHNICAL_REVIEW)
        second = self.create_blocker("app-1", question="Are you willing to relocate?")

        self.assertTrue(second["created"])
        self.assertEqual(self.app_status("app-1"), NEEDS_TECHNICAL_REVIEW)

    def test_unanswered_then_technical_review_escalates_status(self):
        self.upsert_app("app-1")
        self.create_blocker("app-1", question="Are you willing to relocate?")
        second = self.create_blocker("app-1", question="Needs technical review?", status=TECHNICAL_REVIEW)

        self.assertTrue(second["created"])
        self.assertEqual(self.app_status("app-1"), NEEDS_TECHNICAL_REVIEW)

    def test_approving_unanswered_preserves_technical_review_status(self):
        self.upsert_app("app-1")
        unanswered = self.create_blocker("app-1", question="Are you willing to relocate?")
        self.create_blocker("app-1", question="Needs technical review?", status=TECHNICAL_REVIEW)

        response = self.client.post(f"/question-blockers/{unanswered['blocker']['id']}/approve", json={
            "answer": "Yes",
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ready_to_resume_count"], 0)
        self.assertEqual(self.app_status("app-1"), NEEDS_TECHNICAL_REVIEW)

    def test_duplicate_create_preserves_severity_order(self):
        self.upsert_app("app-1")
        self.create_blocker("app-1", question="Needs technical review?", status=TECHNICAL_REVIEW)
        first = self.create_blocker("app-1", question="Are you willing to relocate?")
        duplicate = self.create_blocker("app-1", question="Are you willing to relocate?")
        listed = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"]

        self.assertTrue(first["created"])
        self.assertFalse(duplicate["created"])
        self.assertEqual(len(listed), 2)
        self.assertEqual(self.app_status("app-1"), NEEDS_TECHNICAL_REVIEW)

    def test_options_incompatible_rejects_batch_approval(self):
        self.upsert_app("app-1")
        self.upsert_app("app-2")
        first = self.create_blocker("app-1", options=["Yes", "No"])
        second = self.create_blocker("app-2", options=["Yes", "No"])
        conn = sqlite3.connect(self.mongo_server.sqlite_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE application_question_blockers SET options_json = ? WHERE id = ?",
            ('["Agree", "Decline"]', second["blocker"]["id"]),
        )
        conn.commit()
        conn.close()

        response = self.client.post(f"/question-blocker-groups/{first['blocker']['fingerprint']}/approve", json={
            "answer": "Yes",
            "scope": "batch",
            "approved_by": "unit-test",
            "batch_id": "batch-a",
        })

        self.assertEqual(response.status_code, 409)

    def test_invalid_select_value_rejected_without_updates(self):
        self.upsert_app("app-1")
        blocker = self.create_blocker("app-1")
        before = self.client.get("/applications/app-1/memory").json()["events"]

        response = self.client.post(f"/question-blockers/{blocker['blocker']['id']}/approve", json={
            "answer": "Maybe",
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 422)
        after = self.client.get("/applications/app-1/memory").json()
        self.assertEqual(len(after["events"]), len(before))
        listed = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"]
        self.assertEqual(listed[0]["status"], UNANSWERED)
        self.assertEqual(self.app_status("app-1"), BLOCKED_ON_QUESTIONS)

    def test_empty_text_answer_rejected(self):
        self.upsert_app("app-1")
        blocker = self.create_blocker("app-1", question="Why this role?", control_type="textarea", options=[])

        response = self.client.post(f"/question-blockers/{blocker['blocker']['id']}/approve", json={
            "answer": "   ",
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 422)

    def test_wrong_checkbox_answer_type_rejected(self):
        self.upsert_app("app-1")
        blocker = self.create_blocker("app-1", question="I certify this is accurate", control_type="checkbox", options=[])

        response = self.client.post(f"/question-blockers/{blocker['blocker']['id']}/approve", json={
            "answer": {"checked": True},
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 422)

    def test_valid_normalized_option_is_accepted(self):
        self.upsert_app("app-1")
        blocker = self.create_blocker("app-1", options=["Yes", "No"])

        response = self.client.post(f"/question-blockers/{blocker['blocker']['id']}/approve", json={
            "answer": " yes ",
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 200, response.text)
        approved = response.json()["blockers"][0]
        self.assertEqual(approved["status"], APPROVED)
        self.assertEqual(approved["approved_answer"], "Yes")

    def test_terminal_status_is_preserved_when_blocker_is_created(self):
        self.upsert_app("app-1", status=SUBMITTING)

        result = self.create_blocker("app-1")

        self.assertTrue(result["created"])
        self.assertEqual(result["application_status"], SUBMITTING)
        self.assertEqual(self.app_status("app-1"), SUBMITTING)

    def test_stale_approval_cannot_overwrite_ready_to_submit(self):
        self.upsert_app("app-1")
        blocker = self.create_blocker("app-1")
        status_response = self.client.patch("/applications/app-1/status", json={
            "status": READY_TO_SUBMIT,
            "reason": "concurrent submit preparation",
        })
        self.assertEqual(status_response.status_code, 200, status_response.text)

        response = self.client.post(f"/question-blockers/{blocker['blocker']['id']}/approve", json={
            "answer": "Yes",
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ready_to_resume_count"], 0)
        self.assertEqual(self.app_status("app-1"), READY_TO_SUBMIT)

    def test_more_than_500_records_still_approves_by_id(self):
        target_id = None
        for index in range(505):
            app_id = f"bulk-{index:03d}"
            self.upsert_app(app_id)
            created = self.create_blocker(app_id, question=f"Question {index}?", control_type="select", options=["Yes", "No"])
            if index == 0:
                target_id = created["blocker"]["id"]

        response = self.client.post(f"/question-blockers/{target_id}/approve", json={
            "answer": "Yes",
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.app_status("bulk-000"), READY_TO_RESUME)
        first_page = self.client.get("/question-blockers", params={"limit": 100}).json()
        self.assertEqual(first_page["pagination"]["total"], 505)
        self.assertTrue(first_page["pagination"]["has_more"])

    def test_group_occurrences_endpoint_paginates_more_than_200_records(self):
        fingerprint = None
        for index in range(201):
            app_id = f"occurrence-{index:03d}"
            self.upsert_app(app_id)
            created = self.create_blocker(app_id)
            fingerprint = created["blocker"]["fingerprint"]

        first_page = self.client.get(
            f"/question-blocker-groups/{fingerprint}/occurrences",
            params={"status": UNANSWERED, "limit": 100, "offset": 0},
        )
        last_page = self.client.get(
            f"/question-blocker-groups/{fingerprint}/occurrences",
            params={"status": UNANSWERED, "limit": 100, "offset": 200},
        )

        self.assertEqual(first_page.status_code, 200, first_page.text)
        self.assertEqual(first_page.json()["pagination"]["total"], 201)
        self.assertEqual(len(first_page.json()["occurrences"]), 100)
        self.assertTrue(first_page.json()["pagination"]["has_more"])
        self.assertEqual(last_page.status_code, 200, last_page.text)
        self.assertEqual(len(last_page.json()["occurrences"]), 1)
        self.assertFalse(last_page.json()["pagination"]["has_more"])

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

    def test_application_question_review_bundle_returns_all_review_items(self):
        self.upsert_app("app-1")
        self.create_blocker(
            "app-1",
            question="Are you willing to relocate?",
            metadata={"probe_answer_applied": True, "probe_answer": "Yes"},
        )
        self.create_blocker(
            "app-1",
            question="Upload supporting document",
            control_type="file",
            options=[],
            status=TECHNICAL_REVIEW,
        )
        self.create_blocker(
            "app-1",
            question="Do you prefer remote work?",
            metadata={"suggested_answer": "No", "suggested_answer_requires_review": True},
        )
        approved = self.create_blocker("app-1", question="Are you legally authorized to work?", options=["Yes", "No"])
        response = self.client.post(f"/question-blockers/{approved['blocker']['id']}/approve", json={
            "answer": "Yes",
            "scope": "application",
            "approved_by": "unit-test",
        })
        self.assertEqual(response.status_code, 200, response.text)

        bundle_response = self.client.get("/applications/app-1/question-review-bundle")

        self.assertEqual(bundle_response.status_code, 200, bundle_response.text)
        bundle = bundle_response.json()
        self.assertEqual(bundle["application_id"], "app-1")
        self.assertEqual(bundle["summary"]["unanswered_count"], 2)
        self.assertEqual(bundle["summary"]["technical_review_count"], 1)
        self.assertEqual(bundle["summary"]["approved_count"], 1)
        self.assertEqual(bundle["summary"]["probe_answer_count"], 1)
        self.assertEqual(bundle["summary"]["suggested_answer_count"], 1)
        questions = {item["raw_text"]: item for item in bundle["questions"]}
        self.assertEqual(questions["Are you willing to relocate?"]["probe_answer"], "Yes")
        self.assertEqual(questions["Do you prefer remote work?"]["suggested_answer"], "No")
        self.assertEqual(questions["Are you legally authorized to work?"]["approved_answer"], "Yes")

    def test_application_question_review_bundles_are_application_first(self):
        self.upsert_app("app-1", batch="batch-a")
        self.upsert_app("app-2", batch="batch-a")
        self.create_blocker("app-1")
        self.create_blocker("app-2")

        response = self.client.get("/applications/question-review-bundles", params={
            "batch_id": "batch-a",
            "status": BLOCKED_ON_QUESTIONS,
            "limit": 1,
        })

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["pagination"]["total"], 2)
        self.assertEqual(len(data["bundles"]), 1)
        self.assertEqual(data["bundles"][0]["summary"]["unanswered_count"], 1)
        self.assertTrue(data["pagination"]["has_more"])

    def test_application_question_bundle_approval_approves_all_unanswered(self):
        self.upsert_app("app-1")
        first = self.create_blocker("app-1", question="Are you willing to relocate?", options=["Yes", "No"])
        second = self.create_blocker("app-1", question="Do you prefer remote work?", options=["Yes", "No"])

        response = self.client.post("/applications/app-1/question-review-bundle/approve", json={
            "answers": {
                first["blocker"]["id"]: "No",
                second["blocker"]["id"]: "Yes",
            },
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["approved_count"], 2)
        self.assertEqual(data["ready_to_resume_count"], 1)
        self.assertEqual(self.app_status("app-1"), READY_TO_RESUME)
        blockers = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"]
        self.assertEqual({item["status"] for item in blockers}, {APPROVED})

    def test_application_question_bundle_approval_rejects_missing_answer_without_partial_update(self):
        self.upsert_app("app-1")
        first = self.create_blocker("app-1", question="Are you willing to relocate?", options=["Yes", "No"])
        second = self.create_blocker("app-1", question="Do you prefer remote work?", options=["Yes", "No"])

        response = self.client.post("/applications/app-1/question-review-bundle/approve", json={
            "answers": {
                first["blocker"]["id"]: "No",
            },
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 422)
        blockers = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"]
        self.assertEqual({item["id"]: item["status"] for item in blockers}, {
            first["blocker"]["id"]: UNANSWERED,
            second["blocker"]["id"]: UNANSWERED,
        })
        self.assertEqual(self.app_status("app-1"), BLOCKED_ON_QUESTIONS)

    def test_sqlite_create_rolls_back_when_application_transition_fails(self):
        self.upsert_app("app-1")

        with patch.object(self.mongo_server, "transition_application_status", side_effect=RuntimeError("transition failed")):
            with self.assertRaises(RuntimeError):
                self.mongo_server.create_question_blocker("app-1", self.blocker_request())

        listed = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"]
        self.assertEqual(listed, [])
        self.assertEqual(self.app_status("app-1"), "Queued")

    def test_sqlite_create_rolls_back_when_event_insert_fails_after_status_update(self):
        self.upsert_app("app-1")

        with patch.object(self.mongo_server, "write_event", side_effect=RuntimeError("event failed")):
            with self.assertRaises(RuntimeError):
                self.mongo_server.create_question_blocker("app-1", self.blocker_request())

        listed = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"]
        self.assertEqual(listed, [])
        self.assertEqual(self.app_status("app-1"), "Queued")

    def test_sqlite_approval_rolls_back_when_transaction_aborts_after_blocker_update(self):
        self.upsert_app("app-1")
        blocker = self.create_blocker("app-1")["blocker"]

        with patch.object(self.mongo_server, "write_event", side_effect=RuntimeError("event failed")):
            with self.assertRaises(RuntimeError):
                self.mongo_server.approve_question_blocker(
                    blocker["id"],
                    self.mongo_server.QuestionBlockerApproveRequest(
                        answer="Yes",
                        scope="application",
                        approved_by="unit-test",
                    ),
                )

        listed = self.client.get("/question-blockers", params={"application_id": "app-1"}).json()["blockers"]
        self.assertEqual(listed[0]["status"], UNANSWERED)
        self.assertEqual(self.app_status("app-1"), BLOCKED_ON_QUESTIONS)


class ApplicationQuestionMongoContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mongo_server = importlib.import_module("mcp_servers.mongo_server")
        self.mongo_server.mongo_client = FakeMongoClient()
        self.mongo_server.MONGO_CONFIGURED = False
        self.mongo_server.question_blocker_indexes_ready = False
        self.client = TestClient(self.mongo_server.app)

    def upsert_app(self, app_id: str, batch: str = "batch-a", status: str = "Queued") -> None:
        response = self.client.post("/applications/upsert", json={
            "id": app_id,
            "company": f"Company {app_id}",
            "role": "Software Engineer",
            "resume_v0": "synthetic resume",
            "status": status,
            "metadata": {"batch_id": batch},
        })
        self.assertEqual(response.status_code, 200, response.text)

    def create_blocker(self, app_id: str, batch: str = "batch-a", question: str = "Are you willing to relocate?", status: str = UNANSWERED):
        response = self.client.post(f"/applications/{app_id}/question-blockers", json={
            "batch_id": batch,
            "ats": "workday",
            "company": f"Company {app_id}",
            "role": "Software Engineer",
            "stage": "application_questions",
            "raw_text": question,
            "control_type": "select",
            "options": ["Yes", "No"],
            "status": status,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def app_status(self, app_id: str) -> str:
        response = self.client.get("/applications", params={"limit": 50})
        self.assertEqual(response.status_code, 200, response.text)
        for app in response.json()["applications"]:
            if app["id"] == app_id:
                return app["status"]
        raise AssertionError(f"missing app {app_id}")

    def app_doc(self, app_id: str) -> dict:
        response = self.client.get("/applications", params={"limit": 50})
        self.assertEqual(response.status_code, 200, response.text)
        for app in response.json()["applications"]:
            if app["id"] == app_id:
                return app
        raise AssertionError(f"missing app {app_id}")

    def test_fake_mongo_contract_matches_sqlite_approval_flow(self):
        self.upsert_app("app-1")
        self.upsert_app("app-2")
        first = self.create_blocker("app-1")
        self.create_blocker("app-2")

        response = self.client.post(f"/question-blocker-groups/{first['blocker']['fingerprint']}/approve", json={
            "answer": "Yes",
            "scope": "batch",
            "approved_by": "unit-test",
            "batch_id": "batch-a",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["approved_count"], 2)
        self.assertEqual(self.app_status("app-1"), READY_TO_RESUME)
        self.assertEqual(self.app_status("app-2"), READY_TO_RESUME)

    def test_fake_mongo_forged_fingerprint_is_rejected(self):
        self.upsert_app("app-1")
        forged = self.client.post("/applications/app-1/question-blockers", json={
            "batch_id": "batch-a",
            "raw_text": "Question one?",
            "control_type": "select",
            "options": ["Yes", "No"],
            "fingerprint": "forged",
        })

        self.assertEqual(forged.status_code, 422)

    def test_fake_mongo_technical_review_then_unanswered_preserves_technical_status(self):
        self.upsert_app("app-1")
        self.create_blocker("app-1", question="Needs technical review?", status=TECHNICAL_REVIEW)
        self.create_blocker("app-1", question="Are you willing to relocate?")

        self.assertEqual(self.app_status("app-1"), NEEDS_TECHNICAL_REVIEW)

    def test_fake_mongo_unanswered_then_technical_review_escalates_status(self):
        self.upsert_app("app-1")
        self.create_blocker("app-1", question="Are you willing to relocate?")
        self.create_blocker("app-1", question="Needs technical review?", status=TECHNICAL_REVIEW)

        self.assertEqual(self.app_status("app-1"), NEEDS_TECHNICAL_REVIEW)

    def test_fake_mongo_approving_unanswered_preserves_technical_review_status(self):
        self.upsert_app("app-1")
        unanswered = self.create_blocker("app-1", question="Are you willing to relocate?")
        self.create_blocker("app-1", question="Needs technical review?", status=TECHNICAL_REVIEW)

        response = self.client.post(f"/question-blockers/{unanswered['blocker']['id']}/approve", json={
            "answer": "Yes",
            "scope": "application",
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ready_to_resume_count"], 0)
        self.assertEqual(self.app_status("app-1"), NEEDS_TECHNICAL_REVIEW)

    def test_fake_mongo_duplicate_create_preserves_status_and_revision(self):
        self.upsert_app("app-1")
        self.create_blocker("app-1", question="Needs technical review?", status=TECHNICAL_REVIEW)
        before = self.app_doc("app-1").get("question_blocker_revision")
        first = self.create_blocker("app-1", question="Are you willing to relocate?")
        duplicate = self.create_blocker("app-1", question="Are you willing to relocate?")
        after = self.app_doc("app-1").get("question_blocker_revision")

        self.assertTrue(first["created"])
        self.assertFalse(duplicate["created"])
        self.assertEqual(self.app_status("app-1"), NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(after, before + 1)

    def test_fake_mongo_application_bundle_approval_matches_sqlite(self):
        self.upsert_app("app-1")
        first = self.create_blocker("app-1", question="Are you willing to relocate?")
        second = self.create_blocker("app-1", question="Do you prefer remote work?")

        response = self.client.post("/applications/app-1/question-review-bundle/approve", json={
            "answers": {
                first["blocker"]["id"]: "No",
                second["blocker"]["id"]: "Yes",
            },
            "approved_by": "unit-test",
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["approved_count"], 2)
        self.assertEqual(self.app_status("app-1"), READY_TO_RESUME)


class DeprecatedOrchestratorInstructionTests(unittest.TestCase):
    def test_deprecated_app_orchestrator_is_not_loaded_by_production_paths(self):
        orchestrator_path = ROOT / "app_orchestrator.py"
        text = orchestrator_path.read_text(encoding="utf-8")

        self.assertIn("DEPRECATED_ORCHESTRATOR = True", text)
        self.assertIn("ENABLE_DEPRECATED_VERTEX_ORCHESTRATOR", text)
        for path in ROOT.rglob("*.py"):
            if path == orchestrator_path or "tests" in path.parts:
                continue
            source = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn("import app_orchestrator", source, str(path))
            self.assertNotIn("from app_orchestrator", source, str(path))

    def test_active_orchestrator_instructions_do_not_mix_auto_submit_and_stop_guard(self):
        text = (ROOT / "app_orchestrator.py").read_text(encoding="utf-8")
        is_deprecated = "DEPRECATED_ORCHESTRATOR = True" in text
        automatic_submit = any(
            phrase.lower() in text.lower()
            for phrase in [
                "click final submit",
                "clicks submit",
                "complete the real application submission",
                "完成真实的表单填写和简历投递",
            ]
        )
        stop_before_submit = any(
            phrase.lower() in text.lower()
            for phrase in [
                "stop at ready_to_submit",
                "do not click final submit",
                "禁止自动点击真实最终 submit",
            ]
        )

        if not is_deprecated:
            self.assertFalse(automatic_submit and stop_before_submit)


@unittest.skipUnless(os.getenv("TEST_MONGO_URI"), "TEST_MONGO_URI is required for real MongoDB transaction integration tests")
class ApplicationQuestionRealMongoTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mongo_server = importlib.import_module("mcp_servers.mongo_server")
        self.previous_client = self.mongo_server.mongo_client
        self.previous_configured = self.mongo_server.MONGO_CONFIGURED
        self.previous_db_name = self.mongo_server.DB_NAME
        self.client_handle = MongoClient(os.environ["TEST_MONGO_URI"], serverSelectionTimeoutMS=5000)
        self.client_handle.admin.command("ping")
        self.db_name = f"jobs_hunter_txn_{uuid.uuid4().hex}"
        self.mongo_server.mongo_client = self.client_handle
        self.mongo_server.MONGO_CONFIGURED = True
        self.mongo_server.DB_NAME = self.db_name
        self.mongo_server.question_blocker_indexes_ready = False
        self.mongo_server.APPROVAL_BEFORE_READY_UPDATE_HOOK = None
        self.client = TestClient(self.mongo_server.app)

    def tearDown(self) -> None:
        self.mongo_server.APPROVAL_BEFORE_READY_UPDATE_HOOK = None
        self.client_handle.drop_database(self.db_name)
        self.client_handle.close()
        self.mongo_server.mongo_client = self.previous_client
        self.mongo_server.MONGO_CONFIGURED = self.previous_configured
        self.mongo_server.DB_NAME = self.previous_db_name
        self.mongo_server.question_blocker_indexes_ready = False

    def upsert_app(self, app_id: str, status: str = "Queued") -> None:
        response = self.client.post("/applications/upsert", json={
            "id": app_id,
            "company": f"Company {app_id}",
            "role": "Software Engineer",
            "resume_v0": "synthetic resume",
            "status": status,
            "metadata": {"batch_id": "batch-a"},
        })
        self.assertEqual(response.status_code, 200, response.text)

    def blocker_request(self, question: str = "Are you willing to relocate?"):
        return self.mongo_server.QuestionBlockerCreateRequest(
            batch_id="batch-a",
            ats="workday",
            company="Company app-1",
            role="Software Engineer",
            stage="application_questions",
            raw_text=question,
            control_type="select",
            options=["Yes", "No"],
            status=UNANSWERED,
        )

    def create_blocker(self, app_id: str, question: str = "Are you willing to relocate?", status: str = UNANSWERED):
        response = self.client.post(f"/applications/{app_id}/question-blockers", json={
            "batch_id": "batch-a",
            "ats": "workday",
            "company": f"Company {app_id}",
            "role": "Software Engineer",
            "stage": "application_questions",
            "raw_text": question,
            "control_type": "select",
            "options": ["Yes", "No"],
            "status": status,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["blocker"]

    def app_status(self, app_id: str) -> str:
        response = self.client.get("/applications", params={"limit": 50})
        self.assertEqual(response.status_code, 200, response.text)
        for app in response.json()["applications"]:
            if app["id"] == app_id:
                return app["status"]
        raise AssertionError(f"missing app {app_id}")

    def blocker_docs(self, app_id: str):
        response = self.client.get("/question-blockers", params={"application_id": app_id, "limit": 20})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["blockers"]

    def event_types(self, app_id: str):
        response = self.client.get(f"/applications/{app_id}/memory")
        self.assertEqual(response.status_code, 200, response.text)
        return [item["event_type"] for item in response.json()["events"]]

    def test_real_mongo_concurrent_blocker_create_prevents_ready_to_resume(self):
        self.upsert_app("race-app")
        first = self.create_blocker("race-app", "Are you willing to relocate?")
        created_second = {"done": False}

        def create_second_blocker(app_id, _session):
            if created_second["done"]:
                return
            created_second["done"] = True
            self.mongo_server.create_question_blocker(app_id, self.blocker_request("Do you require sponsorship?"))

        with patch.object(self.mongo_server, "APPROVAL_BEFORE_READY_UPDATE_HOOK", create_second_blocker):
            response = self.client.post(f"/question-blockers/{first['id']}/approve", json={
                "answer": "Yes",
                "scope": "application",
                "approved_by": "unit-test",
            })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(self.app_status("race-app"), {BLOCKED_ON_QUESTIONS, NEEDS_TECHNICAL_REVIEW})
        blockers = self.blocker_docs("race-app")
        self.assertEqual(len(blockers), 2)
        self.assertIn(UNANSWERED, {item["status"] for item in blockers})
        self.assertNotIn("application_ready_to_resume", self.event_types("race-app"))

    def test_real_mongo_create_rolls_back_when_application_transition_fails(self):
        self.upsert_app("app-1")

        with patch.object(self.mongo_server, "transition_application_status", side_effect=RuntimeError("transition failed")):
            with self.assertRaises(RuntimeError):
                self.mongo_server.create_question_blocker("app-1", self.blocker_request())

        self.assertEqual(self.blocker_docs("app-1"), [])
        self.assertEqual(self.app_status("app-1"), "Queued")

    def test_real_mongo_create_rolls_back_when_event_insert_fails_after_status_update(self):
        self.upsert_app("app-1")

        with patch.object(self.mongo_server, "write_event", side_effect=RuntimeError("event failed")):
            with self.assertRaises(RuntimeError):
                self.mongo_server.create_question_blocker("app-1", self.blocker_request())

        self.assertEqual(self.blocker_docs("app-1"), [])
        self.assertEqual(self.app_status("app-1"), "Queued")

    def test_real_mongo_approval_rolls_back_when_transaction_aborts_after_blocker_update(self):
        self.upsert_app("app-1")
        blocker = self.create_blocker("app-1")

        with patch.object(self.mongo_server, "write_event", side_effect=RuntimeError("event failed")):
            with self.assertRaises(RuntimeError):
                self.mongo_server.approve_question_blocker(
                    blocker["id"],
                    self.mongo_server.QuestionBlockerApproveRequest(
                        answer="Yes",
                        scope="application",
                        approved_by="unit-test",
                    ),
                )

        blockers = self.blocker_docs("app-1")
        self.assertEqual(blockers[0]["status"], UNANSWERED)
        self.assertEqual(self.app_status("app-1"), BLOCKED_ON_QUESTIONS)

    def test_real_mongo_mixed_blocker_order_preserves_technical_review_severity(self):
        self.upsert_app("tech-first")
        self.create_blocker("tech-first", "Needs technical review?", status=TECHNICAL_REVIEW)
        self.create_blocker("tech-first", "Are you willing to relocate?")
        self.assertEqual(self.app_status("tech-first"), NEEDS_TECHNICAL_REVIEW)

        self.upsert_app("unanswered-first")
        unanswered = self.create_blocker("unanswered-first", "Are you willing to relocate?")
        self.create_blocker("unanswered-first", "Needs technical review?", status=TECHNICAL_REVIEW)
        self.assertEqual(self.app_status("unanswered-first"), NEEDS_TECHNICAL_REVIEW)

        response = self.client.post(f"/question-blockers/{unanswered['id']}/approve", json={
            "answer": "Yes",
            "scope": "application",
            "approved_by": "unit-test",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ready_to_resume_count"], 0)
        self.assertEqual(self.app_status("unanswered-first"), NEEDS_TECHNICAL_REVIEW)


if __name__ == "__main__":
    unittest.main()
