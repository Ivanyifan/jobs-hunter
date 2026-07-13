from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
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
    DetectedQuestion,
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
    is_blocking_validation_message,
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

    def test_workday_select_placeholder_ignores_sibling_selected_summary(self):
        page = self.open_probe_page("""
            <section>
              <label id="phone-type-label" for="phoneNumber--phoneType">Phone Device Type*</label>
              <button
                id="phoneNumber--phoneType"
                name="phoneType"
                aria-haspopup="listbox"
                aria-labelledby="phone-type-label"
                aria-required="true">Select One</button>
              <label for="phoneNumber--countryPhoneCode">Country Phone Code*</label>
              <input id="phoneNumber--countryPhoneCode" value="">
              <div>1 item selected, United States of America (+1), press delete to clear value.</div>
            </section>
        """)

        fields = playwright_server.extract_form_schema(page, {})
        phone_type = next(field for field in fields if field.get("id") == "phoneNumber--phoneType")

        self.assertEqual(phone_type["value"], "Select One")
        self.assertFalse(phone_type["value_present"])

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

    def test_workday_language_field_is_not_high_risk_due_to_age_substring(self):
        page = self.open_probe_page("""
            <main>
              <section>
                <label id="language-label" for="language-1--language">Language*</label>
                <button id="language-1--language" name="language" aria-haspopup="listbox" aria-labelledby="language-label">Select One</button>
              </section>
            </main>
        """)

        fields = playwright_server.extract_form_schema(page, {})
        language = next(field for field in fields if field.get("id") == "language-1--language")

        self.assertEqual(language["risk"], "low")
        self.assertFalse(language["requires_confirmation"])

    def test_workday_overall_proficiency_maps_to_fluent_option(self):
        self.assertIn("4 fluent", playwright_server.discovery_option_terms("Overall*", "4 - Fluent"))
        self.assertIn(
            "4 fluent",
            playwright_server.discovery_option_terms("Overall Proficiency", "Professional Working Proficiency"),
        )

    def test_workday_my_experience_fills_language_and_overall_from_profile(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Experience</h3>
              <section>
                <h4>Languages 1</h4>
                <label id="language-label" for="language-6--language">Language*</label>
                <button id="language-6--language" name="language" aria-haspopup="listbox" aria-labelledby="language-label"
                  onclick="languageOptions.hidden=false">Select One</button>
                <ul id="languageOptions" role="listbox" hidden>
                  <li role="option" onclick="selectLanguage('English')">English</li>
                  <li role="option" onclick="language.textContent='Chinese (Mandarin)'; languageOptions.hidden=true">Chinese (Mandarin)</li>
                </ul>
                <label for="language-6--native">I am fluent in this language.</label>
                <input id="language-6--native" type="checkbox">
                <label id="overall-label" for="language-6--overall">Overall*</label>
                <button id="language-6--overall" aria-haspopup="listbox" aria-labelledby="overall-label"
                  onkeydown="if (event.key === 'Enter' && document.getElementById('language-6--native').checked) document.getElementById('overallOptions').hidden=false">Select One</button>
                <ul id="overallOptions" role="listbox" hidden>
                  <li role="option" onclick="selectOverall('3 - Advanced')">3 - Advanced</li>
                  <li role="option" onclick="selectOverall('4 - Fluent')">4 - Fluent</li>
                </ul>
              </section>
              <script>
                const language = document.getElementById('language-6--language');
                const languageOptions = document.getElementById('languageOptions');
                const overall = document.getElementById('language-6--overall');
                const overallLabel = document.getElementById('overall-label');
                const overallOptions = document.getElementById('overallOptions');
                const fluentCheckbox = document.getElementById('language-6--native');
                function selectLanguage(value) {
                  language.textContent = value;
                  languageOptions.hidden = true;
                  overall.id = 'language-7--overall';
                  overallLabel.htmlFor = 'language-7--overall';
                }
                function selectOverall(value) {
                  overall.textContent = value;
                  overallOptions.hidden = true;
                }
              </script>
            </main>
        """)

        filled = playwright_server.fill_workday_language_from_profile(
            page,
            {"language": "English", "language_overall": "Fluent"},
        )

        self.assertEqual(page.locator("#language-6--language").inner_text(), "English")
        self.assertTrue(page.locator("#language-6--native").is_checked())
        self.assertEqual(page.locator("#language-7--overall").inner_text(), "4 - Fluent")
        self.assertEqual(
            {item.get("source") for item in filled},
            {"profile_language", "profile_language_fluent", "profile_language_overall"},
        )

    def test_workday_my_experience_rechecks_fields_after_profile_override(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Experience</h3>
              <section>
                <h4>Work Experience 1</h4>
                <label for="workExperience-4--jobTitle">Job Title*</label>
                <input id="workExperience-4--jobTitle" name="jobTitle" required value="">
                <label for="workExperience-4--companyName">Company*</label>
                <input id="workExperience-4--companyName" name="companyName" required value="">
              </section>
              <section id="education-section"><h4>Education</h4></section>
            </main>
        """)
        initial_fields = playwright_server.extract_form_schema(page, {})

        def fill_profile_override(target_page, _user_data):
            target_page.locator("#workExperience-4--jobTitle").fill("Software Engineer Intern")
            target_page.locator("#workExperience-4--companyName").fill("Volcengine")
            target_page.locator("#education-section").evaluate("element => element.remove()")
            return [{"field": "Work Experience", "source": "test_profile_override", "risk": "low"}]

        with patch.object(playwright_server, "fill_workday_profile_overrides", side_effect=fill_profile_override):
            result = playwright_server.fill_discovery_page_fields(
                page,
                initial_fields,
                {"application_id": "app-experience-refresh"},
                self.probe_req(probe=False),
            )

        self.assertEqual(result["missing_required"], [])
        self.assertNotIn("question_blocker", result)
        self.assertEqual(page.locator("#workExperience-4--jobTitle").input_value(), "Software Engineer Intern")
        self.assertEqual(page.locator("#workExperience-4--companyName").input_value(), "Volcengine")

    def test_workday_segmented_date_parts_fill_month_and_year(self):
        page = self.open_probe_page("""
            <main>
              <input id="workExperience-1--startDate-dateSectionMonth-input" value="Month">
              <input id="workExperience-1--startDate-dateSectionYear-input" value="Year">
              <input id="workExperience-1--endDate-dateSectionMonth-input" value="Month">
              <input id="workExperience-1--endDate-dateSectionYear-input" value="Year">
            </main>
        """)

        filled = playwright_server.fill_workday_date_parts(
            page,
            "workExperience-",
            {"month_number": "05", "year": "2024"},
            {"month_number": "08", "year": "2024"},
        )

        self.assertIn("start_month", filled)
        self.assertIn("start_year", filled)
        self.assertIn("end_month", filled)
        self.assertIn("end_year", filled)
        self.assertEqual(page.locator('[id="workExperience-1--startDate-dateSectionMonth-input"]').input_value(), "05")
        self.assertEqual(page.locator('[id="workExperience-1--startDate-dateSectionYear-input"]').input_value(), "2024")
        self.assertEqual(page.locator('[id="workExperience-1--endDate-dateSectionMonth-input"]').input_value(), "08")
        self.assertEqual(page.locator('[id="workExperience-1--endDate-dateSectionYear-input"]').input_value(), "2024")

    def test_workday_current_value_date_descriptions_are_not_validation_errors(self):
        page = self.open_probe_page("""
            <main>
              <label for="from-month">Month*</label>
              <input id="from-month" aria-describedby="from-desc" value="5">
              <div id="from-desc">current value is 5/2024</div>
              <label for="grad-year">Year*</label>
              <input id="grad-year" aria-describedby="year-desc" value="2026">
              <div id="year-desc">current value is 2026</div>
              <label for="empty-year">Year*</label>
              <input id="empty-year" aria-describedby="empty-year-desc" value="">
              <div id="empty-year-desc">current value is YYYY</div>
            </main>
        """)

        questions = detect_visible_required_questions(page, approved_answers={}, user_data={})
        raw_texts = [question.raw_text for question in questions]

        self.assertNotIn("Month", raw_texts)
        self.assertFalse(is_blocking_validation_message("current value is 5/2024"))
        self.assertFalse(is_blocking_validation_message("current value is 2026"))
        self.assertTrue(is_blocking_validation_message("current value is MM/YYYY"))
        self.assertTrue(is_blocking_validation_message("current value is YYYY"))

    def test_workday_application_start_date_composite_maps_to_start_date(self):
        page = self.open_probe_page("""
            <main>
              <section role="group">
                <p>When are you available to start?*</p>
                <div id="start-date-error" role="alert">
                  The field When are you available to start? is required and must have a value.
                </div>
                <label for="available-dateSectionMonth-input">Month</label>
                <input id="available-dateSectionMonth-input" required aria-describedby="start-date-error" value="">
                <label for="available-dateSectionDay-input">Day</label>
                <input id="available-dateSectionDay-input" required aria-describedby="start-date-error" value="">
                <label for="available-dateSectionYear-input">Year</label>
                <input id="available-dateSectionYear-input" required aria-describedby="start-date-error" value="">
              </section>
            </main>
        """)

        questions = detect_visible_required_questions(page, approved_answers={}, user_data={})

        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].raw_text, "When are you available to start?")
        self.assertEqual(questions[0].canonical_key, "start_date")
        self.assertNotEqual(questions[0].raw_text, "Month")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-start-date", "start_date": "12/15/2026"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#available-dateSectionMonth-input").input_value(), "12")
        self.assertEqual(page.locator("#available-dateSectionDay-input").input_value(), "15")
        self.assertEqual(page.locator("#available-dateSectionYear-input").input_value(), "2026")
        self.assertEqual(result["missing_required"], [])
        self.assertTrue(result["question_blocker"]["all_questions_resolved_by_trusted_answers"])

    def test_legacy_workday_start_date_keyboard_helper_fills_component_parts(self):
        page = self.open_probe_page("""
            <main>
              <section role="group">
                <p>When are you available to start?*</p>
                <label for="available-dateSectionMonth-input">Month</label>
                <input id="available-dateSectionMonth-input" required value="">
                <label for="available-dateSectionDay-input">Day</label>
                <input id="available-dateSectionDay-input" required value="">
                <label for="available-dateSectionYear-input">Year</label>
                <input id="available-dateSectionYear-input" required value="">
              </section>
            </main>
        """)

        result = playwright_server.fill_workday_start_date_by_keyboard(
            page,
            {"start_date": "12/15/2026"},
        )

        self.assertTrue(result["filled"], result)
        self.assertEqual(page.locator("#available-dateSectionMonth-input").input_value(), "12")
        self.assertEqual(page.locator("#available-dateSectionDay-input").input_value(), "15")
        self.assertEqual(page.locator("#available-dateSectionYear-input").input_value(), "2026")
        self.assertNotEqual(page.locator("#available-dateSectionMonth-input").input_value(), "12/15/2026")

    def test_workday_select_fields_do_not_all_become_select_one_required(self):
        page = self.open_probe_page("""
            <main>
              <section role="group">
                <p>Are you legally authorized to work in the job posting country?*</p>
                <button id="auth" aria-haspopup="listbox" aria-required="true" aria-describedby="auth-error">Select One Required</button>
                <div id="auth-error" role="alert">The field Are you legally authorized to work in the job posting country? is required and must have a value.</div>
              </section>
              <section role="group">
                <p>Will you now, or in the future, require sponsorship for employment to work in the job posting country?*</p>
                <button id="sponsor" aria-haspopup="listbox" aria-required="true" aria-describedby="sponsor-error">Select One Required</button>
                <div id="sponsor-error" role="alert">The field Will you now, or in the future, require sponsorship for employment to work in the job posting country? is required and must have a value.</div>
              </section>
            </main>
        """)

        questions = detect_visible_required_questions(page, approved_answers={}, user_data={})
        raw_texts = [question.raw_text for question in questions]

        self.assertIn("Are you legally authorized to work in the job posting country?", raw_texts)
        self.assertIn("Will you now, or in the future, require sponsorship for employment to work in the job posting country?", raw_texts)
        self.assertNotIn("Select One Required", raw_texts)
        self.assertEqual(len(set(question.fingerprint for question in questions)), 2)

    def test_workday_section_select_required_from_container_text(self):
        page = self.open_probe_page("""
            <main>
              <section>
                <p>Are you legally authorized to work in the job posting country?*</p>
                <button id="auth" aria-haspopup="listbox">Select One Required</button>
              </section>
            </main>
        """)

        fields = playwright_server.extract_form_schema(page, {})
        questions = detect_visible_required_questions(page, approved_answers={}, user_data={})

        auth_field = next(field for field in fields if field.get("id") == "auth")
        self.assertTrue(auth_field["required"])
        self.assertFalse(auth_field["value_present"])
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].raw_text, "Are you legally authorized to work in the job posting country?")
        self.assertEqual(questions[0].canonical_key, "authorized_to_work_us")

    def test_workday_section_required_star_does_not_pollute_optional_children(self):
        page = self.open_probe_page("""
            <main>
              <section>
                <h3>Work Experience 1</h3>
                <label for="title">Job Title*</label>
                <input id="title" value="Software Engineer Intern">
                <label><input id="current" type="checkbox"> I currently work here</label>
                <label for="gpa">Overall Result (GPA)</label>
                <input id="gpa" value="">
              </section>
            </main>
        """)

        fields = playwright_server.extract_form_schema(page, {})
        current = next(field for field in fields if field.get("id") == "current")
        gpa = next(field for field in fields if field.get("id") == "gpa")

        self.assertFalse(current["required"])
        self.assertFalse(gpa["required"])

    def test_workday_accept_cookies_is_privacy_policy_stage(self):
        page = self.open_probe_page("""
            <main>
              <p>We use cookies to personalize content and ads.</p>
              <button>Accept Cookies</button>
              <button>Decline</button>
            </main>
        """)

        self.assertEqual(playwright_server.infer_apply_stage(page, []), "privacy_policy")

    def test_workday_email_verification_notice_uses_deterministic_handler_before_local_planner(self):
        page = self.open_probe_page("""
            <main>
              <h1>Sign In</h1>
              <div role="alert">An email has been sent to you. Please verify your account.</div>
              <label>Email Address<input type="email"></label>
              <label>Password<input type="password"></label>
              <button>Sign In</button>
              <a>Create Account</a>
            </main>
        """)

        stage = playwright_server.infer_apply_stage(page, [])
        self.assertEqual(stage, "email_verification")
        self.assertTrue(playwright_server.workday_email_verification_pending(page))
        self.assertFalse(playwright_server.should_run_workday_local_planner(page, stage, []))
        self.assertEqual(playwright_server._workday_auth_flow(page, page.locator("body").inner_text()), "sign_in")

    def test_workday_verify_before_sign_in_banner_is_email_verification_stage(self):
        page = self.open_probe_page("""
            <main>
              <h1>Sign In</h1>
              <div role="alert">
                Verify your account before you sign in or request a verification email.
              </div>
              <a>Resend Account Verification</a>
              <label>Email Address<input type="email"></label>
              <label>Password<input type="password"></label>
              <button>Sign In</button>
            </main>
        """)

        stage = playwright_server.infer_apply_stage(page, [])
        diagnostic = playwright_server.workday_auth_overlay_diagnostic(page)

        self.assertEqual(stage, "email_verification")
        self.assertTrue(playwright_server.workday_email_verification_pending(page))
        self.assertIn("Verify your account before you sign in", diagnostic["auth_error_text"])

    def test_workday_local_planner_repetition_resets_on_auth_page_navigation(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <h1>Create Account</h1>
              <label>Email Address<input type="email"></label>
              <label>Password<input type="password"></label>
              <label>Verify New Password<input type="password"></label>
              <button>Create Account</button>
            </main>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/login",
        )
        prior_apply_history = [
            {"stage": "create_account", "url": "https://unit.myworkdayjobs.com/en-US/test/job/R1/apply"},
            {"stage": "create_account", "url": "https://unit.myworkdayjobs.com/en-US/test/job/R1/apply"},
        ]

        self.assertFalse(
            playwright_server.should_run_workday_local_planner(
                page,
                "create_account",
                prior_apply_history,
            )
        )
        current_history = prior_apply_history + [
            {"stage": "create_account", "url": page.url},
            {"stage": "create_account", "url": page.url},
        ]
        self.assertTrue(
            playwright_server.should_run_workday_local_planner(
                page,
                "create_account",
                current_history,
            )
        )

    def test_workday_resend_account_verification_is_safe_send_action(self):
        page = self.open_probe_page("""
            <main>
              <div role="alert">Verify your account before you sign in.</div>
              <button>Resend Account Verification</button>
              <button>Sign In</button>
            </main>
        """)

        clicked, label = playwright_server.click_workday_email_verification_send(page)

        self.assertTrue(clicked)
        self.assertEqual(label, "Resend Account Verification")

    def test_workday_resend_verification_has_exact_dom_click_fallback(self):
        page = self.open_probe_page("""
            <main>
              <button onclick="document.body.dataset.resent='true'">Resend Account Verification</button>
              <button>Sign In</button>
            </main>
        """)

        with patch.object(playwright_server, "click_matching_control", return_value=(False, "")):
            clicked, label = playwright_server.click_workday_email_verification_send(page)

        self.assertTrue(clicked)
        self.assertEqual(label, "Resend Account Verification")
        self.assertEqual(page.locator("body").get_attribute("data-resent"), "true")

    def test_workday_loading_shell_is_not_ready_without_apply_action(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <nav><button>Sign In</button><a>Search for Jobs</a></nav>
              <section>
                <div>Loading</div>
                <div class="skeleton"></div>
              </section>
            </main>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
        )

        snapshot = playwright_server.apply_page_readiness_snapshot(page)
        self.assertGreaterEqual(snapshot["controls"], 2)
        self.assertFalse(snapshot["has_workday_job_action"])
        self.assertTrue(snapshot["has_workday_loading_shell"])
        self.assertFalse(playwright_server.wait_for_apply_page_ready(page, timeout=10)["ready"])

    def test_workday_apply_loading_shell_requests_another_state_machine_wait(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <nav><button>Sign In</button></nav>
              <div>Loading</div>
            </main>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually",
        )

        self.assertTrue(
            playwright_server.is_workday_apply_shell_loading(page, "job_detail", [])
        )

    def test_workday_apply_loading_shell_does_not_hide_extracted_fields(self):
        page = self.open_workday_autofill_page(
            "<main><div>Loading</div><label>Name<input></label></main>",
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually",
        )

        self.assertFalse(
            playwright_server.is_workday_apply_shell_loading(
                page,
                "job_detail",
                [{"label": "Name", "kind": "text"}],
            )
        )
        job_page = self.open_workday_autofill_page(
            "<main><div>Loading</div></main>",
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
        )
        self.assertFalse(
            playwright_server.is_workday_apply_shell_loading(
                job_page,
                "job_detail",
                [],
            )
        )

    def test_workday_plain_apply_button_marks_job_detail_ready(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <nav><button>Sign In</button><a>Search for Jobs</a></nav>
              <h1>Browser Software Engineer Intern</h1>
              <button>Apply</button>
            </main>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
        )

        snapshot = playwright_server.apply_page_readiness_snapshot(page)
        self.assertTrue(snapshot["has_workday_job_action"])
        self.assertTrue(playwright_server.wait_for_apply_page_ready(page, timeout=10)["ready"])

    def test_workday_schema_fallback_creates_question_blocker_from_group_text(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <h2>Application Questions</h2>
              <section>
                <p>Are you legally authorized to work in the job posting country?*</p>
                <button id="auth" aria-haspopup="listbox" aria-required="true">Select One Required</button>
              </section>
              <div id="portal" role="listbox" style="display:none">
                <div role="option">Yes</div>
                <div role="option">No</div>
              </div>
              <script>
                document.getElementById('auth').addEventListener('click', () => {
                  document.getElementById('portal').style.display = 'block';
                });
              </script>
            </main>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply",
        )
        fields = playwright_server.extract_form_schema(page, {})
        req = self.probe_req(probe=True)

        def persist(_application_id, payload):
            return {"created": True, "blocker": {**payload, "id": "blocker-1"}}

        with patch("mcp_servers.playwright_server.detect_visible_required_questions", return_value=[]), \
             patch("mcp_servers.playwright_server.persist_question_blocker_to_memory", side_effect=persist):
            outcome = playwright_server.detect_application_question_blockers(
                page,
                fields,
                {},
                req,
                "application_questions",
            )

        self.assertEqual(outcome["blocker_ids"], ["blocker-1"])
        self.assertEqual(outcome["questions"][0]["raw_text"], "Are you legally authorized to work in the job posting country?")
        self.assertEqual(outcome["questions"][0]["canonical_key"], "authorized_to_work_us")
        self.assertEqual(outcome["questions"][0]["options"], ["Yes", "No"])

    def test_workday_detected_placeholder_question_repairs_from_schema_group_text(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <h2>Application Questions</h2>
              <section>
                <p>Will you now, or in the future, require sponsorship for employment to work in the job posting country?*</p>
                <button id="sponsor" aria-haspopup="listbox" aria-required="true">Select One Required</button>
              </section>
              <div id="portal" role="listbox" style="display:none">
                <div role="option">Yes</div>
                <div role="option">No</div>
              </div>
              <script>
                document.getElementById('sponsor').addEventListener('click', () => {
                  document.getElementById('portal').style.display = 'block';
                });
              </script>
            </main>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply",
        )
        fields = playwright_server.extract_form_schema(page, {})
        question = DetectedQuestion(
            raw_text="Select One Required 003181317e521000b936f490327d0006 primaryQuestionnaire--003181317e521000b936f490327d0006",
            normalized_text="select one 003181317e521000b936f490327d0006 primaryquestionnaire--003181317e521000b936f490327d0006",
            fingerprint="forged",
            required=True,
            control_type="select",
            options=[],
            locator_hints={"id": "sponsor"},
        )

        repaired = playwright_server.repair_workday_detected_questions_from_schema(
            page,
            [question],
            fields,
            "application_questions",
        )

        self.assertEqual(
            repaired[0].raw_text,
            "Will you now, or in the future, require sponsorship for employment to work in the job posting country?",
        )
        self.assertEqual(repaired[0].canonical_key, "need_sponsorship")
        self.assertEqual(repaired[0].options, ["Yes", "No"])

    def test_access_state_machine_returns_blocker_status_from_discovery(self):
        page = SimpleNamespace(url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply")
        req = SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            max_steps=1,
            discover_all_steps=True,
            stop_at_form=False,
        )
        blocker = {
            "status": NEEDS_TECHNICAL_REVIEW,
            "blocker_ids": ["b1"],
            "checkpoint": {"stage": "application_questions"},
        }
        discovery = {
            "fields": [],
            "pages": [{"question_blocker": blocker}],
            "stop_reason": "application_question_blocker",
            "question_blocker": blocker,
        }

        with patch("mcp_servers.playwright_server.build_account_key", return_value=("account", {"ats": "workday"})), \
             patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.get_application_password", return_value=("", "")), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="application_questions"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.is_application_flow_stage", return_value=True), \
             patch("mcp_servers.playwright_server.remember_apply_account", return_value={"account_created": True}), \
             patch("mcp_servers.playwright_server.workday_stage_controller_pipeline_enabled", return_value=False), \
             patch("mcp_servers.playwright_server.discover_application_steps", return_value=discovery):
            result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(result["blocked_reason"], "application_question_blocker")
        self.assertEqual(result["question_blocker"], blocker)

    def test_access_state_machine_uses_last_discovery_stage_when_blocker_checkpoint_is_missing(self):
        page = SimpleNamespace(url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply")
        req = SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            max_steps=1,
            discover_all_steps=True,
            stop_at_form=False,
        )
        blocker = {"status": NEEDS_TECHNICAL_REVIEW, "blocker_ids": ["b1"]}
        discovery = {
            "fields": [],
            "pages": [
                {"stage": "my_information"},
                {"stage": "application_questions"},
            ],
            "stop_reason": "application_question_blocker",
            "question_blocker": blocker,
        }

        with patch("mcp_servers.playwright_server.build_account_key", return_value=("account", {"ats": "workday"})), \
             patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.get_application_password", return_value=("", "")), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="my_information"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.is_application_flow_stage", return_value=True), \
             patch("mcp_servers.playwright_server.remember_apply_account", return_value={"account_created": True}), \
             patch("mcp_servers.playwright_server.workday_stage_controller_pipeline_enabled", return_value=False), \
             patch("mcp_servers.playwright_server.discover_application_steps", return_value=discovery):
            result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

        self.assertEqual(result["stage"], "application_questions")
        self.assertEqual(result["question_blocker"], blocker)

    def test_access_state_machine_routes_workday_form_to_stage_controller_pipeline(self):
        page = SimpleNamespace(url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply")
        req = SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            max_steps=1,
            discover_all_steps=True,
            stop_at_form=False,
        )
        pipeline_result = {
            "success": False,
            "status": BLOCKED_ON_QUESTIONS,
            "outcome_type": "BLOCKED_ON_QUESTIONS",
            "stage": "APPLICATION_QUESTIONS",
            "blocked_reason": "application_questions_require_review",
            "controller_pipeline_used": True,
        }

        with patch("mcp_servers.playwright_server.build_account_key", return_value=("account", {"ats": "workday"})), \
             patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.get_application_password", return_value=("", "")), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="application_questions"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.is_application_flow_stage", return_value=True), \
             patch("mcp_servers.playwright_server.remember_apply_account", return_value={"account_created": True}), \
             patch("mcp_servers.playwright_server.run_workday_stage_controller_pipeline", return_value=pipeline_result) as pipeline, \
             patch("mcp_servers.playwright_server.discover_application_steps") as legacy_discovery:
            result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

        pipeline.assert_called_once_with(page, req, {"email": "test@example.com"})
        legacy_discovery.assert_not_called()
        self.assertTrue(result["controller_pipeline_used"])
        self.assertEqual(result["outcome_type"], "BLOCKED_ON_QUESTIONS")

    def test_access_state_machine_runs_local_candidate_plan_on_unknown_page(self):
        page = SimpleNamespace(url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply")
        req = SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            max_steps=1,
            allow_visual_fallback=True,
        )
        planner_result = {
            "acted": True,
            "suggested_transition": "click_apply",
            "observed_state": "unknown",
            "local_state_machine_trace": [{"attempt_number": 1, "acted": True}],
        }

        with patch("mcp_servers.playwright_server.build_account_key", return_value=("account", {"ats": "workday"})), \
             patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.get_application_password", return_value=("", "")), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.recover_workday_transient_error_page", return_value=0), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="unknown"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.should_run_workday_local_planner", return_value=True), \
             patch("mcp_servers.playwright_server.run_llm_local_state_machine", return_value=planner_result) as planner, \
             patch("mcp_servers.playwright_server.workday_auth_overlay_diagnostic", return_value={"visible": False}), \
             patch("mcp_servers.playwright_server.write_live_smoke_progress"):
            result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

        planner.assert_called_once()
        planner_args, planner_kwargs = planner.call_args
        self.assertEqual(planner_args[:2], (page, req))
        self.assertEqual(planner_args[2]["email"], "test@example.com")
        self.assertGreater(planner_args[2]["_workday_email_verification_not_before_epoch"], 0)
        self.assertTrue(planner_args[2]["_workday_email_verification_correlation_id"])
        self.assertEqual(planner_kwargs["reason"], "workday_unknown_recovery")
        self.assertEqual(result["history"][0]["action"], "llm_local_transition:click_apply")
        self.assertEqual(result["history"][0]["llm_local_state_machine"], planner_result)

    def test_access_state_machine_records_local_plan_on_job_detail_navigation(self):
        page = SimpleNamespace(url="https://unit.myworkdayjobs.com/en-US/test/job/R0001")
        req = SimpleNamespace(
            url=page.url,
            max_steps=1,
            allow_visual_fallback=True,
        )
        planner_result = {
            "acted": True,
            "suggested_transition": "click_apply",
            "observed_state": "job_detail",
            "local_state_machine_trace": [{"attempt_number": 1, "acted": True}],
        }

        with patch("mcp_servers.playwright_server.build_account_key", return_value=("account", {"ats": "workday"})), \
             patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.get_application_password", return_value=("", "")), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.recover_workday_transient_error_page", return_value=0), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="job_detail"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.workday_auth_overlay_diagnostic", return_value={"visible": False}), \
             patch("mcp_servers.playwright_server.is_blank_workday_autofill_branch", return_value=False), \
             patch("mcp_servers.playwright_server.click_workday_application_choice", return_value=(False, "")), \
             patch("mcp_servers.playwright_server.run_llm_local_state_machine", return_value=planner_result) as planner, \
             patch("mcp_servers.playwright_server.write_live_smoke_progress") as progress:
            result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

        planner.assert_called_once()
        planner_args, planner_kwargs = planner.call_args
        self.assertEqual(planner_args[:2], (page, req))
        self.assertEqual(planner_args[2]["email"], "test@example.com")
        self.assertGreater(planner_args[2]["_workday_email_verification_not_before_epoch"], 0)
        self.assertTrue(planner_args[2]["_workday_email_verification_correlation_id"])
        self.assertEqual(planner_kwargs["reason"], "job_detail_navigation")
        self.assertEqual(result["history"][0]["visual_fallback"], planner_result)
        self.assertEqual(result["history"][0]["llm_local_state_machine"], planner_result)
        self.assertTrue(any("llm_local_transition:click_apply" in str(call) for call in progress.call_args_list))

    def test_controller_pipeline_runs_local_candidate_plan_for_unsupported_stage(self):
        page = SimpleNamespace(url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply")
        req = SimpleNamespace(max_form_pages=1, allow_visual_fallback=True)
        planner_result = {
            "acted": True,
            "suggested_transition": "click_continue",
            "observed_state": "unknown",
            "local_state_machine_trace": [{"attempt_number": 1, "acted": True}],
        }

        with patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.recover_workday_transient_error_page", return_value=0), \
             patch("mcp_servers.playwright_server.page_shows_workday_transient_error", return_value=False), \
             patch("mcp_servers.playwright_server.wait_for_workday_step_interactive", return_value={}), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="unsupported_accessibility_step"), \
             patch("mcp_servers.playwright_server.workday_auth_overlay_diagnostic", return_value={"visible": False}), \
             patch("mcp_servers.playwright_server.page_has_final_submit", return_value=False), \
             patch("mcp_servers.playwright_server.run_llm_local_state_machine", return_value=planner_result) as planner, \
             patch("mcp_servers.playwright_server.capture_apply_screenshot", return_value="controller.png"), \
             patch("mcp_servers.playwright_server.write_live_smoke_progress"):
            result = playwright_server.run_workday_stage_controller_pipeline(page, req, {})

        planner.assert_called_once_with(page, req, {}, reason="unsupported_controller_stage")
        self.assertEqual(result["trace_events"][0]["action"], "llm_local_state_machine")
        self.assertEqual(result["trace_events"][0]["llm_local_state_machine"], planner_result)

    def test_stage_controller_pipeline_runs_all_migrated_stages_and_stops_before_submit(self):
        page = self.open_workday_autofill_page(
            """
            <main><div id="root"></div></main>
            <script>
              window.finalSubmitClicks = 0;
              const root = document.querySelector("#root");
              function renderInformation() {
                root.innerHTML = `
                  <h1>My Information</h1>
                  <section role="group" aria-label="Phone">
                    <label for="phone">Phone Number*</label>
                    <input id="phone" required value="2172500626">
                  </section>
                  <button id="continue">Save and Continue</button>`;
                document.querySelector("#continue").addEventListener("click", renderExperience);
              }
              function renderExperience() {
                root.innerHTML = `
                  <h1>My Experience</h1>
                  <section data-section="education" role="group" aria-label="Education">
                    <label for="school">School or University*</label>
                    <input id="school" required value="University of Illinois at Urbana-Champaign">
                    <label id="degree-label" for="degree">Degree*</label>
                    <button id="degree" aria-haspopup="listbox" aria-required="true"
                      aria-labelledby="degree-label">Bachelors (16 years of education)</button>
                    <span data-automation-id="selectedItem">Bachelors (16 years of education)</span>
                  </section>
                  <button id="continue">Save and Continue</button>`;
                document.querySelector("#continue").addEventListener("click", renderQuestions);
              }
              function renderQuestions() {
                root.innerHTML = `
                  <h1>Application Questions</h1>
                  <section role="group" aria-label="Availability">
                    <p>When are you available to start?*</p>
                    <label for="month">Month</label><input id="month" required value="07">
                    <label for="day">Day</label><input id="day" required value="27">
                    <label for="year">Year</label><input id="year" required value="2026">
                  </section>
                  <section role="group" aria-label="Candidate Location">
                    <p>Are you an existing HP employee?*</p>
                    <button id="employee" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                    <span id="employeeToken" data-automation-id="selectedItem" hidden></span>
                    <div id="employeeOptions" role="listbox" hidden>
                      <button type="button" role="option">Yes</button>
                      <button id="employeeNo" type="button" role="option">No</button>
                    </div>
                  </section>
                  <section id="locatedSection" role="group" aria-label="Candidate Location" hidden>
                    <p>Are you located in US?*</p>
                    <button id="located" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                    <span id="locatedToken" data-automation-id="selectedItem" hidden></span>
                    <div id="locatedOptions" role="listbox" hidden>
                      <button type="button" role="option">No</button>
                      <button id="locatedYes" type="button" role="option">Yes</button>
                    </div>
                  </section>
                  <button id="continue">Save and Continue</button>`;
                document.querySelector("#employee").addEventListener("click", () => employeeOptions.hidden = false);
                document.querySelector("#employeeNo").addEventListener("click", () => {
                  employee.textContent = "No";
                  employeeToken.textContent = "No";
                  employeeToken.hidden = false;
                  employeeOptions.hidden = true;
                  locatedSection.hidden = false;
                });
                document.querySelector("#located").addEventListener("click", () => locatedOptions.hidden = false);
                document.querySelector("#locatedYes").addEventListener("click", () => {
                  located.textContent = "Yes";
                  locatedToken.textContent = "Yes";
                  locatedToken.hidden = false;
                  locatedOptions.hidden = true;
                });
                document.querySelector("#continue").addEventListener("click", renderReview);
              }
              function renderReview() {
                root.innerHTML = `
                  <h1>Review Application</h1>
                  <p>Review your application before submitting.</p>
                  <button id="finalSubmit">Submit Application</button>`;
                document.querySelector("#finalSubmit").addEventListener("click", () => window.finalSubmitClicks += 1);
              }
              renderInformation();
            </script>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply",
        )
        req = self.probe_req(probe=False, confirm_submit=False)
        req.url = "https://unit.myworkdayjobs.com/en-US/test/job/R0001"
        user_data = {
            "phone": "2172500626",
            "common_answers": {
                "current_or_previous_company_employee": "No",
                "located_in_us": "Yes",
            },
            "application_profile_library": {
                "education": {
                    "school": "University of Illinois Urbana-Champaign",
                    "degree": "Bachelor of Science",
                },
                "availability": {"start_date": "07/27/2026"},
            },
        }

        with patch("mcp_servers.playwright_server.capture_apply_screenshot", return_value="controller-stage.png"), \
             patch("mcp_servers.playwright_server.fill_discovery_page_fields") as legacy_fill:
            result = playwright_server.run_workday_stage_controller_pipeline(page, req, user_data)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["outcome_type"], "READY_TO_SUBMIT")
        self.assertTrue(result["controller_pipeline_used"])
        self.assertEqual(
            [item["controller"] for item in result["pages"]],
            ["MyInformationController", "MyExperienceController", "ApplicationQuestionsController"],
        )
        self.assertEqual(page.evaluate("window.finalSubmitClicks"), 0)
        legacy_fill.assert_not_called()
        self.assertTrue(result["smoke_evidence"]["month_day_year_filled_correctly"]["verified"])

    def test_stage_controller_pipeline_recovers_workday_transient_error_before_dispatch(self):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        request_count = {"value": 0}
        error_html = """
            <main>
              <h1>My Information</h1>
              <h2>Something went wrong</h2>
              <p>Please refresh the page and then try again.</p>
            </main>
        """
        review_html = """
            <main>
              <h1>Review Application</h1>
              <p>Review your application before submitting.</p>
              <button id="submit">Submit Application</button>
              <script>
                window.finalSubmitClicks = 0;
                submit.addEventListener("click", () => window.finalSubmitClicks += 1);
              </script>
            </main>
        """

        def serve(route):
            request_count["value"] += 1
            route.fulfill(
                status=200,
                content_type="text/html",
                body=error_html if request_count["value"] == 1 else review_html,
            )

        page.route("https://unit.myworkdayjobs.com/**", serve)
        page.goto("https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply", wait_until="domcontentloaded")
        req = self.probe_req(probe=False, confirm_submit=False)
        req.url = "https://unit.myworkdayjobs.com/en-US/test/job/R0001"

        with patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.capture_apply_screenshot", return_value="transient-recovered.png"):
            result = playwright_server.run_workday_stage_controller_pipeline(page, req, {})

        self.assertEqual(result["outcome_type"], "READY_TO_SUBMIT", result)
        self.assertGreaterEqual(request_count["value"], 2)
        self.assertEqual(page.evaluate("window.finalSubmitClicks"), 0)

    def test_access_apply_form_hands_off_to_submit_steps_when_not_stop_at_form(self):
        class FakePage:
            url = "https://unit.myworkdayjobs.com/en-US/test/job/R0001"

            def goto(self, url, **_kwargs):
                self.url = url

            def screenshot(self, path, **_kwargs):
                Path(path).write_bytes(b"")

        class FakeContext:
            def __init__(self, page):
                self.page = page

            def add_cookies(self, _cookies):
                return None

            def new_page(self):
                return self.page

        class FakeBrowser:
            def __init__(self, page):
                self.page = page

            def new_context(self, **_kwargs):
                return FakeContext(self.page)

            def close(self):
                return None

        class FakePlaywrightContext:
            def __enter__(self):
                return SimpleNamespace()

            def __exit__(self, *_args):
                return None

        fake_page = FakePage()
        req = playwright_server.AccessApplyRequest(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            user_data={"email": "test@example.com"},
            stop_at_form=False,
            confirm_submit=True,
        )

        def fake_submit(_page, submit_req, _user_data):
            self.assertFalse(submit_req.confirm_submit)
            return {
                "success": False,
                "status": READY_TO_SUBMIT,
                "blocked_reason": "final_submit_confirmation_required",
                "stage": "review",
                "fields": [],
                "pages": [{"page_number": 1}],
                "page_state": {},
                "preflight": {},
                "screenshot_path": "safe.png",
            }

        with patch("mcp_servers.playwright_server.load_default_user_data", return_value={}), \
             patch("mcp_servers.playwright_server.ensure_resume_text", side_effect=lambda data, _path: data), \
             patch("mcp_servers.playwright_server.resolve_workday_auth_profile", return_value={}), \
             patch("mcp_servers.playwright_server.sync_playwright", return_value=FakePlaywrightContext()), \
             patch("mcp_servers.playwright_server.extract_timezone_and_locale", return_value=("UTC", "en-US")), \
             patch("mcp_servers.playwright_server.launch_browser", return_value=(FakeBrowser(fake_page), False)), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.run_apply_access_state_machine", return_value={
                 "success": True,
                 "status": "form_detected",
                 "stage": "my_information",
                 "history": [{"stage": "my_information"}],
             }), \
             patch("mcp_servers.playwright_server.submit_application_steps", side_effect=fake_submit) as submit_steps, \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="review"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.debug_workday_country_state_controls", return_value=[]):
            response = playwright_server.access_apply_form(req)

        self.assertTrue(submit_steps.called)
        self.assertTrue(req.confirm_submit)
        self.assertEqual(response["status"], READY_TO_SUBMIT)
        self.assertEqual(response["blocked_reason"], "final_submit_confirmation_required")
        self.assertEqual(response["submit_pages"], [{"page_number": 1}])
        self.assertEqual(response["access_result"]["status"], "form_detected")

    def test_access_apply_form_does_not_handoff_after_controller_pipeline(self):
        class FakePage:
            url = "https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply"

            def goto(self, url, **_kwargs):
                self.url = url

            def screenshot(self, path, **_kwargs):
                Path(path).write_bytes(b"")

        class FakeContext:
            def __init__(self, page):
                self.page = page

            def add_cookies(self, _cookies):
                return None

            def new_page(self):
                return self.page

        class FakeBrowser:
            def __init__(self, page):
                self.page = page

            def new_context(self, **_kwargs):
                return FakeContext(self.page)

            def close(self):
                return None

        class FakePlaywrightContext:
            def __enter__(self):
                return SimpleNamespace()

            def __exit__(self, *_args):
                return None

        fake_page = FakePage()
        req = playwright_server.AccessApplyRequest(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            user_data={"email": "test@example.com"},
            stop_at_form=False,
            confirm_submit=False,
        )
        pipeline_result = {
            "success": True,
            "status": READY_TO_SUBMIT,
            "outcome_type": "READY_TO_SUBMIT",
            "stage": "REVIEW",
            "blocked_reason": "final_submit_confirmation_required",
            "controller_pipeline_used": True,
            "method": "workday_stage_controller_pipeline",
            "fields": [],
            "pages": [{"controller": "ApplicationQuestionsController"}],
            "smoke_evidence": {"no_unsafe_first_option_fallback": {"verified": True}},
            "screenshot_path": "controller-safe.png",
        }

        with patch("mcp_servers.playwright_server.load_default_user_data", return_value={}), \
             patch("mcp_servers.playwright_server.ensure_resume_text", side_effect=lambda data, _path: data), \
             patch("mcp_servers.playwright_server.resolve_workday_auth_profile", return_value={}), \
             patch("mcp_servers.playwright_server.sync_playwright", return_value=FakePlaywrightContext()), \
             patch("mcp_servers.playwright_server.extract_timezone_and_locale", return_value=("UTC", "en-US")), \
             patch("mcp_servers.playwright_server.launch_browser", return_value=(FakeBrowser(fake_page), False)), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.run_apply_access_state_machine", return_value=pipeline_result), \
             patch("mcp_servers.playwright_server.submit_application_steps") as legacy_submit, \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="review"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.debug_workday_country_state_controls", return_value=[]):
            response = playwright_server.access_apply_form(req)

        legacy_submit.assert_not_called()
        self.assertTrue(response["controller_pipeline_used"])
        self.assertEqual(response["status"], READY_TO_SUBMIT)
        self.assertEqual(response["controller_method"], "workday_stage_controller_pipeline")
        self.assertEqual(response["submit_pages"], [{"controller": "ApplicationQuestionsController"}])

    def test_workday_existing_account_password_error_requires_technical_review(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <h1>Sign In</h1>
              <label>Email Address<input name="email" type="email"></label>
              <label>Password<input name="password" type="password"></label>
              <div role="alert">You may have entered the wrong email address or password or your account might be locked.</div>
              <button>Sign In</button>
            </main>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/login",
        )
        req = SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            max_steps=1,
            adjust_password_to_policy=False,
            allow_terms_acceptance=False,
            allow_email_verification=False,
            wait_for_email_seconds=0,
            allow_account_creation=True,
            allow_visual_fallback=False,
            discover_all_steps=False,
            stop_at_form=True,
        )

        with patch("mcp_servers.playwright_server.build_account_key", return_value=("account", {"ats": "workday"})), \
             patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.get_application_password", return_value=("default-password", "default")), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="sign_in"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.remember_apply_account", return_value={"account_exists": True}) as remember_account, \
             patch("mcp_servers.playwright_server.click_matching_control") as click_matching, \
             patch("mcp_servers.playwright_server.click_workday_sign_in_submit") as click_sign_in:
            result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(result["blocked_reason"], "ats_login_password_required")
        self.assertEqual(remember_account.call_args.kwargs["event"], "exists_warning")
        self.assertFalse(click_matching.called)
        self.assertFalse(click_sign_in.called)

    def test_known_workday_account_without_saved_password_does_not_create_or_default_login(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <h1>Sign In</h1>
              <label>Email Address<input name="email" type="email"></label>
              <label>Password<input name="password" type="password"></label>
              <button>Sign In</button>
              <button>Create Account</button>
            </main>
            """,
            url="https://unit.myworkdayjobs.com/en-US/test/login",
        )
        req = SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            max_steps=1,
            adjust_password_to_policy=False,
            allow_terms_acceptance=False,
            allow_email_verification=False,
            wait_for_email_seconds=0,
            allow_account_creation=True,
            allow_visual_fallback=False,
            discover_all_steps=False,
            stop_at_form=True,
        )

        with patch("mcp_servers.playwright_server.build_account_key", return_value=("account", {"ats": "workday"})), \
             patch("mcp_servers.playwright_server.get_registry_account", return_value={"account_exists": True}), \
             patch("mcp_servers.playwright_server.get_application_password", return_value=("default-password", "default")), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="sign_in"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.click_matching_control") as click_matching, \
             patch("mcp_servers.playwright_server.click_workday_sign_in_submit") as click_sign_in:
            result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(result["blocked_reason"], "ats_login_password_required")
        self.assertFalse(click_matching.called)
        self.assertFalse(click_sign_in.called)

    def workday_auth_req(self, max_steps=3):
        return SimpleNamespace(
            url="https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/job/R0001",
            max_steps=max_steps,
            adjust_password_to_policy=False,
            allow_terms_acceptance=False,
            allow_email_verification=False,
            wait_for_email_seconds=0,
            allow_account_creation=True,
            allow_visual_fallback=False,
            discover_all_steps=False,
            stop_at_form=True,
        )

    def test_access_apply_request_unsafe_live_flags_are_false_by_default(self):
        req = playwright_server.AccessApplyRequest(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            user_data={"email": "test@example.com"},
        )

        self.assertFalse(req.confirm_submit)
        self.assertFalse(req.probe_fill_unapproved_questions)
        self.assertFalse(req.allow_visual_fallback)
        self.assertFalse(req.allow_visual_field_fallback)
        self.assertFalse(req.allow_resume_upload)
        self.assertFalse(req.allow_email_verification)

    def test_boeing_configured_registered_sign_in_only_attempts_sign_in_with_registry_empty(self):
        page = SimpleNamespace(url="https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/login")
        req = self.workday_auth_req(max_steps=3)
        user_data = {
            "email": "fallback@example.com",
            "workday_credentials": {
                "boeing.wd1.myworkdayjobs.com": {
                    "email": "boeing@example.com",
                    "password": "TenantSecret123!",
                    "registered": True,
                    "auth_strategy": "sign_in_only",
                }
            },
        }
        click_patterns = []

        def fill_identity(_page, filled_user_data, password):
            self.assertEqual(filled_user_data["email"], "boeing@example.com")
            self.assertEqual(password, "TenantSecret123!")
            return {"email": True, "password_fields": 1}

        def click_control(_page, patterns, **_kwargs):
            text = " ".join(patterns).lower()
            click_patterns.append(text)
            self.assertNotIn("create account", text)
            self.assertNotIn("register", text)
            return True, "Sign In"

        with patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="sign_in"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.page_has_credential_failure", return_value=False), \
             patch("mcp_servers.playwright_server.fill_auth_identity", side_effect=fill_identity) as fill_auth, \
             patch("mcp_servers.playwright_server.click_matching_control", side_effect=click_control), \
             patch("mcp_servers.playwright_server.click_workday_sign_in_submit", return_value=(False, "")) as click_sign_in, \
             patch("mcp_servers.playwright_server.wait_for_workday_auth_submission", return_value={
                 "outcome": "no_transition",
                 "before": {},
                 "after": {},
                 "network_events": [],
             }):
            result = playwright_server.run_apply_access_state_machine(page, req, user_data)

        self.assertGreaterEqual(fill_auth.call_count, 1)
        self.assertTrue(click_patterns)
        self.assertEqual(result["outcome_type"], "AUTH_BLOCKED")
        self.assertEqual(result["blocked_reason"], playwright_server.WORKDAY_AUTH_NO_TRANSITION_REASON)
        self.assertEqual(result["needs_user_action"], "wait_then_retry_or_use_persistent_session")
        self.assertEqual(result["account"]["tenant"], "boeing")
        self.assertEqual(result["account"]["host"], "boeing.wd1.myworkdayjobs.com")
        self.assertEqual(result["account"]["email"], "boeing@example.com")
        self.assertTrue(result["account"]["account_exists"])
        self.assertEqual(result["account"]["auth_strategy"], "sign_in_only")
        self.assertEqual(result["account"]["password_source"], "workday_credentials")
        self.assertEqual(result["account"]["login_attempt_count"], 1)
        self.assertNotIn("TenantSecret123!", json.dumps(result["account"]))
        self.assertNotIn("password", result["account"])
        self.assertTrue(click_sign_in.called)
        submission = next(item["auth_submission"] for item in result["history"] if "auth_submission" in item)
        self.assertEqual(submission["outcome"], "no_transition")

    def test_workday_auth_submission_blockers_keep_unknown_and_blank_distinct_from_bad_password(self):
        cases = {
            "credential_rejected": ("stored_workday_password_rejected", "verify_or_reset_workday_password"),
            "no_transition": (
                playwright_server.WORKDAY_AUTH_NO_TRANSITION_REASON,
                "wait_then_retry_or_use_persistent_session",
            ),
            "blank_page": (
                playwright_server.WORKDAY_AUTH_BLANK_PAGE_REASON,
                "reload_or_resume_with_persistent_session",
            ),
            "rate_limited": (
                playwright_server.WORKDAY_AUTH_RATE_LIMITED_REASON,
                "wait_before_retrying_workday_sign_in",
            ),
        }

        for outcome, expected in cases.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    playwright_server.workday_auth_submission_blocker({"outcome": outcome}),
                    expected,
                )

    def test_workday_auth_submission_wait_classifies_blank_page_without_retrying_credentials(self):
        page = SimpleNamespace(remove_listener=lambda *_args, **_kwargs: None)
        before = {"auth_visible": True, "content_blank": False, "url": {"host": "hp.example", "path": "/login"}}
        blank = {"auth_visible": False, "content_blank": True, "url": before["url"], "auth_error_text": ""}

        with patch("mcp_servers.playwright_server.workday_auth_submission_snapshot", return_value=blank):
            result = playwright_server.wait_for_workday_auth_submission(
                page,
                before,
                network_trace={"events": [], "handler": None},
                timeout_ms=0,
            )

        self.assertEqual(result["outcome"], "blank_page")

    def test_workday_auth_submission_wait_routes_verification_banner_to_email_stage(self):
        page = SimpleNamespace(remove_listener=lambda *_args, **_kwargs: None)
        before = {"auth_visible": True, "content_blank": False, "url": "https://unit.wd5.myworkdayjobs.com/login"}
        verification = {
            "auth_visible": True,
            "content_blank": False,
            "url": before["url"],
            "auth_error_text": "Verify your account before you sign in or request a verification email.",
        }

        with patch("mcp_servers.playwright_server.workday_auth_submission_snapshot", return_value=verification):
            result = playwright_server.wait_for_workday_auth_submission(
                page,
                before,
                network_trace={"events": [], "handler": None},
                timeout_ms=0,
            )

        self.assertEqual(result["outcome"], "email_verification_required")
        self.assertEqual(
            playwright_server.workday_auth_submission_blocker(result),
            (None, None),
        )

    def test_browser_profile_scope_isolated_by_workday_tenant_and_email_hash(self):
        hp_scope = playwright_server.browser_profile_scope(
            "https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/R1",
            "maya@example.com",
        )
        hp_other_user_scope = playwright_server.browser_profile_scope(
            "https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/R1",
            "other@example.com",
        )
        boeing_scope = playwright_server.browser_profile_scope(
            "https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/job/R1",
            "maya@example.com",
        )

        self.assertTrue(hp_scope.startswith("workday-hp-"))
        self.assertNotIn("maya", hp_scope)
        self.assertNotEqual(hp_scope, hp_other_user_scope)
        self.assertNotEqual(hp_scope, boeing_scope)

    def test_persistent_browser_launch_uses_installed_chrome_channel(self):
        persistent_context = object()

        class FakeChromium:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("channel") == "chrome":
                    return persistent_context
                raise RuntimeError("unexpected browser candidate")

        chromium = FakeChromium()
        playwright = SimpleNamespace(chromium=chromium)
        with tempfile.TemporaryDirectory() as profile_root, patch.dict(os.environ, {
            "PLAYWRIGHT_USE_PERSISTENT_PROFILE": "true",
            "PLAYWRIGHT_PROFILE_ROOT": profile_root,
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": "",
            "CHROMIUM_EXECUTABLE_PATH": "",
        }):
            context, is_persistent = playwright_server.launch_browser(
                playwright,
                headless=True,
                profile_scope="workday-hp-test",
            )

        self.assertIs(context, persistent_context)
        self.assertTrue(is_persistent)
        self.assertEqual(chromium.calls[0]["channel"], "chrome")

    def test_auth_network_url_identity_redacts_opaque_path_ids_and_query(self):
        identity = playwright_server._safe_url_identity(
            "https://hp.wd5.myworkdayjobs.com/wday/person/2cdf011a2edb10001555934a4d600000/addresses?token=secret"
        )

        self.assertEqual(identity["host"], "hp.wd5.myworkdayjobs.com")
        self.assertEqual(identity["path"], "/wday/person/:id/addresses")
        self.assertNotIn("secret", json.dumps(identity))

    def test_successful_login_registry_event_marks_account_as_existing(self):
        registry = {"version": 1, "accounts": {}}
        meta = {
            "ats": "workday",
            "host": "hp.wd5.myworkdayjobs.com",
            "tenant": "hp",
            "email": "test@example.com",
            "login_attempt_count": 1,
            "last_auth_action": "clicked:Sign In",
            "password_source": "registry",
        }

        with patch("mcp_servers.playwright_server.load_account_registry", return_value=registry), \
             patch("mcp_servers.playwright_server.save_account_registry"):
            record = playwright_server.remember_apply_account("account", meta, event="login")

        self.assertTrue(record["account_created"])
        self.assertTrue(record["account_exists"])
        self.assertEqual(record["login_attempt_count"], 1)
        self.assertEqual(record["last_auth_action"], "clicked:Sign In")

    def test_sign_in_only_missing_tenant_password_returns_existing_account_without_password(self):
        page = SimpleNamespace(url="https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/login")
        req = self.workday_auth_req(max_steps=1)
        user_data = {
            "email": "fallback@example.com",
            "workday_credentials": {
                "boeing": {
                    "email": "boeing@example.com",
                    "registered": True,
                    "auth_strategy": "sign_in_only",
                }
            },
        }

        with patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="sign_in"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.page_has_credential_failure", return_value=False), \
             patch("mcp_servers.playwright_server.fill_auth_identity") as fill_auth, \
             patch("mcp_servers.playwright_server.click_matching_control") as click_matching, \
             patch("mcp_servers.playwright_server.click_workday_sign_in_submit") as click_sign_in:
            result = playwright_server.run_apply_access_state_machine(page, req, user_data)

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(result["outcome_type"], "AUTH_BLOCKED")
        self.assertEqual(result["blocked_reason"], "existing_account_without_stored_password")
        self.assertEqual(result["needs_user_action"], "provide_workday_password")
        self.assertFalse(fill_auth.called)
        self.assertFalse(click_matching.called)
        self.assertFalse(click_sign_in.called)

    def test_sign_in_only_create_account_stage_clicks_sign_in_not_create_or_guest(self):
        page = SimpleNamespace(url="https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/createAccount")
        req = self.workday_auth_req(max_steps=1)
        user_data = {
            "email": "fallback@example.com",
            "workday_credentials": {
                "boeing": {
                    "email": "boeing@example.com",
                    "password": "TenantSecret123!",
                    "registered": True,
                    "auth_strategy": "sign_in_only",
                }
            },
        }
        with patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="create_account"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.click_workday_switch_to_sign_in", return_value=(True, "Sign In")) as switch_to_sign_in, \
             patch("mcp_servers.playwright_server.click_matching_control") as click_matching, \
             patch("mcp_servers.playwright_server.fill_auth_identity") as fill_auth:
            result = playwright_server.run_apply_access_state_machine(page, req, user_data)

        switch_to_sign_in.assert_called_once_with(page)
        self.assertFalse(click_matching.called)
        self.assertFalse(fill_auth.called)
        self.assertEqual(result["blocked_reason"], "max_steps_reached")

    def test_sign_in_only_misclassified_sign_in_stage_switches_out_of_create_account_form(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <div data-automation-id="authView">
                <h1>Create Account</h1>
                <label>Email Address<input name="email" type="email"></label>
                <label>Password<input name="password" type="password"></label>
                <label>Verify New Password<input name="verifyPassword" type="password"></label>
                <button data-automation-id="createAccountSubmitButton"
                        onclick="document.body.dataset.created='true'">Create Account</button>
                <button data-automation-id="signInLink"
                        onclick="document.body.dataset.switched='true'">Sign In</button>
              </div>
            </main>
            """,
            url="https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/createAccount",
        )
        req = self.workday_auth_req(max_steps=1)
        user_data = {
            "email": "fallback@example.com",
            "workday_credentials": {
                "boeing": {
                    "email": "boeing@example.com",
                    "password": "TenantSecret123!",
                    "registered": True,
                    "auth_strategy": "sign_in_only",
                }
            },
        }

        with patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="sign_in"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}):
            result = playwright_server.run_apply_access_state_machine(page, req, user_data)

        self.assertEqual(page.locator("body").get_attribute("data-switched"), "true")
        self.assertIsNone(page.locator("body").get_attribute("data-created"))
        self.assertEqual(page.locator('input[name="email"]').input_value(), "")
        self.assertEqual(result["history"][0]["action"], "clicked:Sign In")

    def test_sign_in_submit_rejects_create_account_button_and_sign_in_switch(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <div data-automation-id="authView">
                <h1>Create Account</h1>
                <input type="password"><input type="password">
                <button data-automation-id="createAccountSubmitButton"
                        onclick="document.body.dataset.created='true'">Create Account</button>
                <button data-automation-id="signInLink"
                        onclick="document.body.dataset.switched='true'">Sign In</button>
              </div>
            </main>
            """
        )

        clicked, label = playwright_server.click_workday_sign_in_submit(page)

        self.assertFalse(clicked)
        self.assertEqual(label, "")
        self.assertIsNone(page.locator("body").get_attribute("data-created"))
        self.assertIsNone(page.locator("body").get_attribute("data-switched"))
        diagnostic = playwright_server.workday_auth_submit_control_diagnostic(page)
        self.assertEqual(diagnostic["label"], "Create Account")
        self.assertEqual(diagnostic["automation_id"], "createAccountSubmitButton")

    def test_sign_in_only_credential_error_returns_stored_password_rejected(self):
        page = SimpleNamespace(url="https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/login")
        req = self.workday_auth_req(max_steps=1)
        user_data = {
            "email": "fallback@example.com",
            "workday_credentials": {
                "boeing": {
                    "email": "boeing@example.com",
                    "password": "TenantSecret123!",
                    "registered": True,
                    "auth_strategy": "sign_in_only",
                }
            },
        }

        with patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
             patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
             patch("mcp_servers.playwright_server.dismiss_popups"), \
             patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
             patch("mcp_servers.playwright_server.infer_apply_stage", return_value="sign_in"), \
             patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
             patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
             patch("mcp_servers.playwright_server.page_has_credential_failure", return_value=True), \
             patch("mcp_servers.playwright_server.remember_apply_account", return_value={"account_exists": True}), \
             patch("mcp_servers.playwright_server.fill_auth_identity") as fill_auth, \
             patch("mcp_servers.playwright_server.click_matching_control") as click_matching:
            result = playwright_server.run_apply_access_state_machine(page, req, user_data)

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(result["outcome_type"], "AUTH_BLOCKED")
        self.assertEqual(result["blocked_reason"], "stored_workday_password_rejected")
        self.assertEqual(result["needs_user_action"], "verify_or_reset_workday_password")
        self.assertFalse(fill_auth.called)
        self.assertFalse(click_matching.called)
        self.assertNotIn("TenantSecret123!", json.dumps(result["account"]))

    def test_hp_style_sign_in_overlay_still_visible_returns_auth_blocked(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <h2>Application Questions</h2>
              <div role="dialog" data-automation-id="signInContent" aria-modal="true">
                <h2>Sign In</h2>
                <label>Email Address<input data-automation-id="email" type="email"></label>
                <label>Password<input data-automation-id="password" type="password"></label>
                <button data-automation-id="signInSubmitButton">Sign In</button>
              </div>
            </main>
            """,
            url="https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/R0001/apply",
        )
        req = self.workday_auth_req(max_steps=1)
        req.url = "https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/R0001"

        with tempfile.TemporaryDirectory() as artifact_dir:
            screenshot_path = Path(artifact_dir) / "auth.png"
            screenshot_path.write_bytes(b"auth screenshot")
            with patch.dict(os.environ, {"PLAYWRIGHT_AUTH_DIAGNOSTIC_DIR": artifact_dir}), \
                 patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
                 patch("mcp_servers.playwright_server.get_application_password", return_value=("default-password", "default")), \
                 patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
                 patch("mcp_servers.playwright_server.dismiss_popups"), \
                 patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
                 patch("mcp_servers.playwright_server.infer_apply_stage", return_value="application_questions"), \
                 patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
                 patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
                 patch("mcp_servers.playwright_server.capture_apply_screenshot", return_value=str(screenshot_path)):
                result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "AUTH")
            self.assertEqual(result["outcome_type"], "AUTH_BLOCKED")
            self.assertEqual(result["blocked_reason"], "workday_sign_in_overlay_still_visible")
            self.assertEqual(result["screenshot_path"], str(screenshot_path))
            self.assertTrue(result["smoke_evidence"]["auth_blocked_without_trusted_credential"]["verified"])
            self.assertTrue(result["smoke_evidence"]["blocker_screenshot_trace_on_failure"]["verified"])
            self.assertTrue(Path(result["result_json_path"]).exists())
            self.assertTrue(Path(result["progress_log_path"]).exists())
            self.assertTrue(Path(result["trace_events_path"]).exists())

    def test_workday_job_detail_sign_in_nav_button_is_not_auth_overlay(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <nav>
                <button data-automation-id="signInHeaderButton">Sign In</button>
                <button>Search for Jobs</button>
              </nav>
              <h1>Senior Software Engineer</h1>
              <a>Apply</a>
            </main>
            """,
            url="https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/R0001",
        )

        diagnostic = playwright_server.workday_auth_overlay_diagnostic(page)

        self.assertFalse(diagnostic["visible"])
        self.assertFalse(diagnostic["email_input_visible"])
        self.assertFalse(diagnostic["password_input_visible"])
        self.assertTrue(diagnostic["auth_action_visible"])

    def test_workday_something_went_wrong_overlay_returns_auth_error(self):
        visible_error = "Something went wrong Please refresh the page and then try again."
        page = self.open_workday_autofill_page(
            f"""
            <main>
              <div role="dialog" data-automation-id="signInContent" aria-modal="true">
                <h2>Sign In</h2>
                <div role="alert">{visible_error}</div>
                <label>Email Address<input data-automation-id="email" type="email"></label>
                <label>Password<input data-automation-id="password" type="password"></label>
                <button data-automation-id="signInSubmitButton">Sign In</button>
              </div>
            </main>
            """,
            url="https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/login",
        )
        req = self.workday_auth_req(max_steps=1)
        req.url = "https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/R0001"

        with tempfile.TemporaryDirectory() as artifact_dir:
            screenshot_path = Path(artifact_dir) / "auth-error.png"
            screenshot_path.write_bytes(b"auth screenshot")
            with patch.dict(os.environ, {"PLAYWRIGHT_AUTH_DIAGNOSTIC_DIR": artifact_dir}), \
                 patch("mcp_servers.playwright_server.get_registry_account", return_value={}), \
                 patch("mcp_servers.playwright_server.get_application_password", return_value=("default-password", "default")), \
                 patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
                 patch("mcp_servers.playwright_server.dismiss_popups"), \
                 patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
                 patch("mcp_servers.playwright_server.infer_apply_stage", return_value="sign_in"), \
                 patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
                 patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
                 patch("mcp_servers.playwright_server.capture_apply_screenshot", return_value=str(screenshot_path)):
                result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

            self.assertEqual(result["stage"], "AUTH")
            self.assertEqual(result["outcome_type"], "AUTH_BLOCKED")
            self.assertEqual(result["blocked_reason"], "workday_sign_in_overlay_error")
            self.assertEqual(result["auth_error_text"], visible_error)
            stored = json.loads(Path(result["result_json_path"]).read_text(encoding="utf-8"))
            self.assertEqual(stored["auth_error_text"], visible_error)
            self.assertEqual(stored["auth_flow"], "sign_in")
            self.assertFalse(stored["email_verification_attempted"])
            self.assertEqual(stored["email_verification_timeout_seconds"], 0)
            self.assertFalse(stored["password_policy_adjusted"])
            self.assertFalse(stored["stored_password_rejected"])
            self.assertEqual(stored["current_url"], page.url)
            self.assertEqual(stored["screenshot_path"], str(screenshot_path))

    def test_max_steps_reached_while_auth_overlay_visible_is_auth_not_questions(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <div role="dialog" data-automation-id="signInContent" aria-modal="true">
                <h2>Sign In</h2>
                <label>Email Address<input data-automation-id="email" type="email"></label>
                <label>Password<input data-automation-id="password" type="password"></label>
                <button data-automation-id="signInSubmitButton">Sign In</button>
              </div>
            </main>
            """,
            url="https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/login",
        )
        req = self.workday_auth_req(max_steps=1)
        req.url = "https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/R0001"

        with tempfile.TemporaryDirectory() as artifact_dir:
            screenshot_path = Path(artifact_dir) / "auth-max-steps.png"
            screenshot_path.write_bytes(b"auth screenshot")
            with patch.dict(os.environ, {"PLAYWRIGHT_AUTH_DIAGNOSTIC_DIR": artifact_dir}), \
                 patch("mcp_servers.playwright_server.get_registry_account", return_value={"account_exists": True}), \
                 patch("mcp_servers.playwright_server.get_application_password", return_value=("Stored123!", "workday_credentials")), \
                 patch("mcp_servers.playwright_server.wait_for_apply_page_ready", return_value={"ready": True}), \
                 patch("mcp_servers.playwright_server.dismiss_popups"), \
                 patch("mcp_servers.playwright_server.extract_form_schema", return_value=[]), \
                 patch("mcp_servers.playwright_server.infer_apply_stage", return_value="sign_in"), \
                 patch("mcp_servers.playwright_server.build_page_state", return_value={}), \
                 patch("mcp_servers.playwright_server.build_preflight", return_value={}), \
                 patch("mcp_servers.playwright_server.page_has_credential_failure", return_value=False), \
                 patch("mcp_servers.playwright_server.fill_auth_identity"), \
                 patch("mcp_servers.playwright_server.click_workday_sign_in_submit", return_value=(True, "Sign In")), \
                 patch("mcp_servers.playwright_server.click_matching_control", return_value=(True, "Sign In")), \
                 patch("mcp_servers.playwright_server.wait_for_workday_auth_submission", return_value={
                     "outcome": "no_transition",
                     "before": {},
                     "after": {},
                     "network_events": [],
                 }), \
                 patch("mcp_servers.playwright_server.capture_apply_screenshot", return_value=str(screenshot_path)):
                result = playwright_server.run_apply_access_state_machine(page, req, {"email": "test@example.com"})

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "AUTH")
            self.assertEqual(result["outcome_type"], "AUTH_BLOCKED")
            self.assertEqual(result["blocked_reason"], playwright_server.WORKDAY_AUTH_NO_TRANSITION_REASON)
            self.assertEqual(result["needs_user_action"], "wait_then_retry_or_use_persistent_session")
            self.assertNotEqual(result.get("stage"), "APPLICATION_QUESTIONS")
            self.assertNotEqual(result.get("blocked_reason"), "max_steps_reached")
            self.assertEqual(result["screenshot_path"], str(screenshot_path))
            self.assertTrue(result["smoke_evidence"]["auth_blocked_without_trusted_credential"]["verified"])
            self.assertTrue(result["smoke_evidence"]["blocker_screenshot_trace_on_failure"]["verified"])

    def test_legally_authorized_question_maps_to_authorized_to_work_us(self):
        page = self.open_probe_page("""
            <section role="group">
              <p>Are you legally authorized to work in the United States?*</p>
              <button id="auth" aria-haspopup="listbox" aria-required="true" aria-describedby="auth-error">Select One</button>
              <div id="auth-error" role="alert">The field Are you legally authorized to work in the United States? is required and must have a value.</div>
            </section>
        """)

        questions = detect_visible_required_questions(page, approved_answers={}, user_data={})

        self.assertEqual(questions[0].canonical_key, "authorized_to_work_us")

    def test_sponsorship_question_maps_to_need_sponsorship(self):
        page = self.open_probe_page("""
            <section role="group">
              <p>Will you now or in the future require sponsorship?</p>
              <button id="sponsor" aria-haspopup="listbox" aria-required="true" aria-describedby="sponsor-error">Select One</button>
              <div id="sponsor-error" role="alert">The field Will you now or in the future require sponsorship? is required and must have a value.</div>
            </section>
        """)

        questions = detect_visible_required_questions(page, approved_answers={}, user_data={})

        self.assertEqual(questions[0].canonical_key, "need_sponsorship")

    def test_ambiguous_placeholder_can_use_llm_canonicalizer_without_answer(self):
        page = self.open_probe_page("""
            <section>
              <button id="ambiguous" aria-label="Select One" aria-haspopup="listbox" aria-required="true">Select One</button>
            </section>
        """)

        def classifier(context):
            self.assertIn("options", context)
            self.assertIn("field_label", context)
            self.assertIn("current_visible_value", context)
            return {
                "question_text": "Are you legally authorized to work in the United States?",
                "canonical_key": "authorized_to_work_us",
                "risk_level": "sensitive_compliance",
                "confidence": 0.92,
                "evidence": "mocked classifier",
                "answer": "Yes",
            }

        questions = detect_visible_required_questions(
            page,
            approved_answers={},
            user_data={"llm_question_canonicalizer": classifier},
        )

        self.assertEqual(questions[0].raw_text, "Are you legally authorized to work in the United States?")
        self.assertEqual(questions[0].canonical_key, "authorized_to_work_us")
        self.assertEqual(questions[0].question_context["semantic_classifier"]["risk_level"], "sensitive_compliance")
        self.assertNotIn("answer", questions[0].question_context["semantic_classifier"])

    def test_low_confidence_semantic_classifier_marks_technical_review(self):
        page = self.open_probe_page("""
            <section>
              <button id="ambiguous" aria-label="Select One" aria-haspopup="listbox" aria-required="true">Select One</button>
            </section>
        """)

        def classifier(_context):
            return {
                "question_text": "How did you hear about us?",
                "canonical_key": "how_heard",
                "risk_level": "low",
                "confidence": 0.62,
                "evidence": "weak context",
            }

        questions = detect_visible_required_questions(
            page,
            approved_answers={},
            user_data={"llm_question_canonicalizer": classifier},
        )

        self.assertEqual(questions[0].canonical_key, "how_heard")
        self.assertEqual(questions[0].status, TECHNICAL_REVIEW)
        self.assertTrue(questions[0].question_context["semantic_classifier"]["requires_technical_review"])
        self.assertEqual(outcome_status_for_questions(questions), NEEDS_TECHNICAL_REVIEW)

    def test_execution_blocks_low_confidence_semantic_classifier_without_probe(self):
        page = self.open_probe_page("<main><p>Application Questions</p></main>")
        question = DetectedQuestion(
            raw_text="How did you hear about us?",
            normalized_text=normalize_question_text("How did you hear about us?"),
            fingerprint=fingerprint_question(normalize_question_text("How did you hear about us?"), "select", ["Corporate Website"]),
            required=True,
            control_type="select",
            options=["Corporate Website"],
            canonical_key="how_heard",
            question_context={
                "semantic_classifier": {
                    "used": True,
                    "canonical_key": "how_heard",
                    "risk_level": "low",
                    "confidence": 0.62,
                    "evidence": "weak context",
                    "requires_technical_review": True,
                }
            },
            status=TECHNICAL_REVIEW,
        )
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "detect_visible_required_questions", return_value=[question]), \
             patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                [],
                {"application_id": "app-low-confidence"},
                self.probe_req(),
            )

        self.assertEqual(result["question_blocker"]["status"], NEEDS_TECHNICAL_REVIEW)
        self.assertEqual(captured[0]["status"], TECHNICAL_REVIEW)
        self.assertTrue(captured[0]["metadata"]["semantic_classifier_low_confidence"])
        self.assertEqual(result["question_blocker"].get("probe_filled"), [])

    def test_sensitive_compliance_classifier_without_trusted_answer_does_not_probe(self):
        page = self.open_probe_page("<main><p>Application Questions</p></main>")
        question = DetectedQuestion(
            raw_text="Conflict of interest",
            normalized_text=normalize_question_text("Conflict of interest"),
            fingerprint=fingerprint_question(normalize_question_text("Conflict of interest"), "select", ["Yes", "No"]),
            required=True,
            control_type="select",
            options=["Yes", "No"],
            canonical_key="conflict_of_interest",
            question_context={
                "semantic_classifier": {
                    "used": True,
                    "canonical_key": "conflict_of_interest",
                    "risk_level": "sensitive_compliance",
                    "confidence": 0.96,
                    "evidence": "mocked classifier",
                }
            },
        )
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "detect_visible_required_questions", return_value=[question]), \
             patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                [],
                {"application_id": "app-sensitive-semantic"},
                self.probe_req(),
            )

        self.assertEqual(captured[0]["canonical_key"], "conflict_of_interest")
        self.assertEqual(captured[0]["metadata"]["probe_stop_reason"], "sensitive_question_requires_trusted_answer")
        self.assertEqual(result["question_blocker"].get("probe_filled"), [])

    def test_conflict_of_interest_only_fills_from_trusted_answer(self):
        page = self.open_probe_page("""
            <section role="group">
              <p>Conflict of Interest Question 1: HP Policy prohibits outside employment with a competitor.*</p>
              <button id="coi" aria-haspopup="listbox" aria-required="true" aria-describedby="coi-error"
                onclick="document.getElementById('coi-options').hidden=false">Select One</button>
              <ul id="coi-options" role="listbox" hidden>
                <li role="option" onclick="coi.textContent='Yes'; coi.dataset.value='Yes'; coiOptions.hidden=true">Yes</li>
                <li role="option" onclick="coi.textContent='No'; coi.dataset.value='No'; coiOptions.hidden=true">No</li>
              </ul>
              <div id="coi-error" role="alert">The field Conflict of Interest Question 1: HP Policy prohibits outside employment with a competitor. is required and must have a value.</div>
            </section>
            <script>
              const coi = document.getElementById('coi');
              const coiOptions = document.getElementById('coi-options');
            </script>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"application_id": "app-coi"},
                self.probe_req(),
            )

        self.assertEqual(page.locator("#coi").inner_text(), "Select One")
        self.assertFalse(any(item.get("source") == "first_valid_select_probe" for item in result["filled"]))
        self.assertEqual(captured[0]["canonical_key"], "conflict_of_interest")
        self.assertEqual(captured[0]["status"], UNANSWERED)

        trusted_page = self.open_probe_page("""
            <section role="group">
              <p>Conflict of Interest Question 1: HP Policy prohibits outside employment with a competitor.*</p>
              <button id="coi" aria-haspopup="listbox" aria-required="true" aria-describedby="coi-error"
                onclick="document.getElementById('coi-options').hidden=false">Select One</button>
              <ul id="coi-options" role="listbox" hidden>
                <li role="option" onclick="coi.textContent='Yes'; coi.dataset.value='Yes'; coiOptions.hidden=true">Yes</li>
                <li role="option" onclick="coi.textContent='No'; coi.dataset.value='No'; coiOptions.hidden=true">No</li>
              </ul>
              <div id="coi-error" role="alert">The field Conflict of Interest Question 1: HP Policy prohibits outside employment with a competitor. is required and must have a value.</div>
            </section>
            <script>
              const coi = document.getElementById('coi');
              const coiOptions = document.getElementById('coi-options');
            </script>
        """)
        captured_trusted, fake_persist_trusted = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist_trusted):
            trusted_result = playwright_server.fill_discovery_page_fields(
                trusted_page,
                playwright_server.extract_form_schema(trusted_page, {}),
                {"application_id": "app-coi-trusted", "common_answers": {"conflict_of_interest": "No"}},
                self.probe_req(),
            )

        self.assertEqual(trusted_page.locator("#coi").inner_text(), "No")
        self.assertEqual(captured_trusted, [])
        self.assertEqual(trusted_result["missing_required"], [])

    def test_workday_first_valid_probe_excludes_country_and_language(self):
        country = {
            "label": "Country/Region*",
            "name": "country",
            "id": "country--country",
            "input_type": "select",
            "required": True,
            "value_present": False,
            "risk": "low",
        }
        language = {
            "label": "Language*",
            "name": "language",
            "id": "language-1--language",
            "input_type": "select",
            "required": True,
            "value_present": False,
            "risk": "low",
        }

        self.assertFalse(playwright_server.can_choose_first_valid_required_select(country))
        self.assertFalse(playwright_server.can_choose_first_valid_required_select(language))

    def test_country_region_canonicalizes_to_country_before_state_region(self):
        self.assertEqual(
            playwright_server.canonicalize_field(
                "Country/Region*",
                input_type="select",
                name="country",
                field_id="country--country",
            ),
            "country",
        )

    def test_protected_workday_country_selects_united_states_not_first_option(self):
        page = self.open_probe_page("""
            <main>
              <section>
                <label id="country-label" for="country--country">Country/Region*</label>
                <button id="country--country" name="country" aria-haspopup="listbox" aria-labelledby="country-label"
                  onclick="countryOptions.hidden=false">Select One</button>
                <ul id="countryOptions" role="listbox" hidden>
                  <li role="option" onclick="country.textContent='Anguilla'; countryOptions.hidden=true">Anguilla</li>
                  <li role="option" onclick="country.textContent='United States of America'; countryOptions.hidden=true">United States of America</li>
                </ul>
              </section>
              <script>
                const country = document.getElementById('country--country');
                const countryOptions = document.getElementById('countryOptions');
              </script>
            </main>
        """)

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-country", "country": "US"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#country--country").inner_text(), "United States of America")
        self.assertFalse(any(item.get("source") == "first_valid_select_probe" for item in result["filled"]))
        self.assertEqual(result["missing_required"], [])

    def test_workday_phone_country_code_is_not_protected_country(self):
        page = self.open_probe_page("""
            <main>
              <section>
                <label id="phone-code-label" for="phoneNumber--countryPhoneCode">Country/Region Phone Code*</label>
                <button id="phoneNumber--countryPhoneCode" name="countryPhoneCode" aria-haspopup="listbox" aria-labelledby="phone-code-label"
                  onclick="phoneOptions.hidden=false">Select One</button>
                <ul id="phoneOptions" role="listbox" hidden>
                  <li role="option" onclick="phoneCode.textContent='Anguilla (+1)'; phoneOptions.hidden=true">Anguilla (+1)</li>
                  <li role="option" onclick="phoneCode.textContent='United States of America (+1)'; phoneOptions.hidden=true">United States of America (+1)</li>
                </ul>
              </section>
              <script>
                const phoneCode = document.getElementById('phoneNumber--countryPhoneCode');
                const phoneOptions = document.getElementById('phoneOptions');
              </script>
            </main>
        """)
        fields = playwright_server.extract_form_schema(page, {})
        phone_field = next(field for field in fields if field.get("id") == "phoneNumber--countryPhoneCode")

        self.assertFalse(playwright_server.is_protected_workday_country_field(phone_field))
        result = playwright_server.fill_discovery_page_fields(
            page,
            fields,
            {"application_id": "app-phone-code"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#phoneNumber--countryPhoneCode").inner_text(), "United States of America (+1)")
        self.assertEqual(result["missing_required"], [])

    def test_workday_address_region_is_not_protected_country(self):
        field = {
            "label": "Region",
            "raw_label": "Region",
            "name": "countryRegion",
            "id": "address--countryRegion",
            "selector": "button#address--countryRegion",
            "input_type": "select",
            "required": True,
            "canonical_field": "state",
        }

        self.assertFalse(playwright_server.is_protected_workday_country_field(field))

    def test_workday_my_information_corrects_stord_country_before_field_loop(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <p>* Indicates a required field</p>
              <p>How Did You Hear About Us?*</p>
              <section>
                <label id="country-label" for="country--country">Country*</label>
                <button id="country--country" name="country" aria-haspopup="listbox" aria-labelledby="country-label"
                  onclick="document.getElementById('countryOptions').hidden=false">Australia</button>
                <ul id="countryOptions" role="listbox" hidden>
                  <li role="option" onclick="country.textContent='Australia'; countryOptions.hidden=true">Australia</li>
                  <li role="option" onclick="country.textContent='United States of America'; countryOptions.hidden=true">United States of America</li>
                </ul>
              </section>
              <section>
                <label id="region-label" for="address--countryRegion">Region</label>
                <button id="address--countryRegion" name="countryRegion" aria-haspopup="listbox" aria-labelledby="region-label"
                  onclick="document.getElementById('regionOptions').hidden=false">Australian Capital Territory</button>
                <ul id="regionOptions" role="listbox" hidden>
                  <li role="option" onclick="region.textContent='Australian Capital Territory'; regionOptions.hidden=true">Australian Capital Territory</li>
                  <li role="option" onclick="region.textContent='Illinois'; regionOptions.hidden=true">Illinois</li>
                </ul>
              </section>
              <section>
                <label id="phone-code-label" for="phoneNumber--countryPhoneCode">Country Phone Code*</label>
                <button id="phoneNumber--countryPhoneCode" name="countryPhoneCode" aria-haspopup="listbox" aria-labelledby="phone-code-label"
                  onclick="document.getElementById('phoneOptions').hidden=false">Australia (+61)</button>
                <ul id="phoneOptions" role="listbox" hidden>
                  <li role="option" onclick="phoneCode.textContent='Australia (+61)'; phoneOptions.hidden=true">Australia (+61)</li>
                  <li role="option" onclick="phoneCode.textContent='United States of America (+1)'; phoneOptions.hidden=true">United States of America (+1)</li>
                </ul>
              </section>
              <script>
                const country = document.getElementById('country--country');
                const countryOptions = document.getElementById('countryOptions');
                const region = document.getElementById('address--countryRegion');
                const regionOptions = document.getElementById('regionOptions');
                const phoneCode = document.getElementById('phoneNumber--countryPhoneCode');
                const phoneOptions = document.getElementById('phoneOptions');
              </script>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/Stord_External_Career/job/HQ---Atlanta-GA/Software-Engineer---New-Grad_JR102585/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-stord-country", "country": "United States", "state": "Illinois"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#country--country").inner_text(), "United States of America")
        self.assertEqual(page.locator("#phoneNumber--countryPhoneCode").inner_text(), "United States of America (+1)")
        self.assertFalse(any(item.get("reason") == "protected_country_mismatch" for item in result["missing_required"]))

    def test_workday_my_information_phone_number_uses_local_digits_without_country_code(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <p>* Indicates a required field</p>
              <label for="phoneNumber">Phone Number*</label>
              <input id="phoneNumber" name="phoneNumber" value="+1 2172500626" required>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/Stord_External_Career/job/HQ---Atlanta-GA/Software-Engineer---New-Grad_JR102585/apply/autofillWithResume")

        filled = playwright_server.fill_workday_profile_overrides(
            page,
            {"application_id": "app-stord-phone", "phone": "+1 2172500626"},
        )

        self.assertEqual(page.locator("#phoneNumber").input_value(), "2172500626")
        self.assertTrue(any(item.get("field") == "Phone Number" for item in filled))

    def test_workday_my_information_how_did_you_hear_zero_items_selects_first_valid_option(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <p>* Indicates a required field</p>
              <section>
                <label id="hear-label" for="hear">How Did You Hear About Us?*</label>
                <button id="hear" name="source" aria-haspopup="listbox" aria-labelledby="hear-label"
                  onclick="hearOptions.hidden=false">0 items selected</button>
                <ul id="hearOptions" role="listbox" hidden>
                  <li role="option" onclick="hear.textContent='Company Website'; hearOptions.hidden=true">Company Website</li>
                  <li role="option" onclick="hear.textContent='LinkedIn'; hearOptions.hidden=true">LinkedIn</li>
                </ul>
              </section>
              <script>
                const hear = document.getElementById('hear');
                const hearOptions = document.getElementById('hearOptions');
              </script>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-hear"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#hear").inner_text(), "Company Website")
        self.assertEqual(result["missing_required"], [])
        self.assertTrue(any(item.get("source", "").startswith("workday_my_information_how_heard") for item in result["filled"]))
        self.assertEqual(result["exit_condition"], "SUCCESS")
        self.assertEqual(result["state_snapshot"]["how_heard"]["status"], "filled")
        self.assertEqual(result["state_snapshot"]["how_heard"]["source"], "deterministic")
        how_heard_group = result["field_groups"]["how_heard_group"]
        self.assertTrue(how_heard_group["required"])
        self.assertTrue(how_heard_group["completed"])
        self.assertEqual(how_heard_group["missing_subfields"], [])
        self.assertEqual(how_heard_group["subfields"], ["how_heard"])

    def test_workday_my_information_how_did_you_hear_uses_explicit_user_value_outside_safe_defaults(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <section>
                <label id="hear-label" for="hear">How Did You Hear About Us?*</label>
                <button id="hear" name="source" aria-haspopup="listbox" aria-labelledby="hear-label"
                  onclick="hearOptions.hidden=false">0 items selected</button>
                <ul id="hearOptions" role="listbox" hidden>
                  <li role="option" onclick="hear.textContent='Campus Recruiting'; hearOptions.hidden=true">Campus Recruiting</li>
                  <li role="option" onclick="hear.textContent='Employee Referral'; hearOptions.hidden=true">Employee Referral</li>
                </ul>
              </section>
              <script>
                const hear = document.getElementById('hear');
                const hearOptions = document.getElementById('hearOptions');
              </script>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-hear-user-value", "how_heard": "Campus Recruiting"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#hear").inner_text(), "Campus Recruiting")
        self.assertEqual(result["missing_required"], [])
        self.assertTrue(any(item.get("source", "").startswith("workday_my_information_how_heard") for item in result["filled"]))
        self.assertFalse(any("first_valid" in item.get("source", "") for item in result["filled"]))

    def test_workday_my_information_how_did_you_hear_without_safe_match_blocks_not_first_valid(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <section>
                <label id="hear-label" for="hear">How Did You Hear About Us?*</label>
                <button id="hear" name="source" aria-haspopup="listbox" aria-labelledby="hear-label"
                  onclick="hearOptions.hidden=false">0 items selected</button>
                <ul id="hearOptions" role="listbox" hidden>
                  <li role="option" onclick="hear.textContent='Campus Recruiting'; hearOptions.hidden=true">Campus Recruiting</li>
                  <li role="option" onclick="hear.textContent='Employee Referral'; hearOptions.hidden=true">Employee Referral</li>
                </ul>
              </section>
              <script>
                const hear = document.getElementById('hear');
                const hearOptions = document.getElementById('hearOptions');
              </script>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"application_id": "app-hear-no-safe-match"},
                self.probe_req(),
            )

        self.assertEqual(page.locator("#hear").inner_text(), "0 items selected")
        self.assertEqual(result["outcome_type"], "MY_INFORMATION_BLOCKED")
        self.assertEqual(result["unresolved_required_fields"][0]["group"], "how_heard_group")
        self.assertEqual(result["unresolved_required_fields"][0]["missing_subfields"], ["how_heard"])
        self.assertEqual(captured[0]["stage"], "my_information")

    def test_workday_my_information_previously_worked_for_organization_selects_no(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <fieldset>
                <legend>Have you previously worked for our Organization?*</legend>
                <label><input id="prev-yes" name="previousOrg" type="radio" required value="Yes"> Yes</label>
                <label><input id="prev-no" name="previousOrg" type="radio" required value="No"> No</label>
              </fieldset>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-prev-org"},
            self.probe_req(),
        )

        self.assertTrue(page.locator("#prev-no").is_checked())
        self.assertEqual(result["missing_required"], [])
        group = result["field_groups"]["employment_history_group"]
        self.assertTrue(group["required"])
        self.assertTrue(group["completed"])
        self.assertEqual(group["missing_subfields"], [])
        self.assertEqual(group["subfields"], ["previous_worker"])

    def test_workday_my_information_previous_worker_uses_yes_only_when_explicit(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <fieldset>
                <legend>Have you previously worked for our Organization?*</legend>
                <label><input id="prev-yes" name="previousOrg" type="radio" required value="Yes"> Yes</label>
                <label><input id="prev-no" name="previousOrg" type="radio" required value="No"> No</label>
              </fieldset>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-prev-org-yes", "previous_worker": "Yes"},
            self.probe_req(),
        )

        self.assertTrue(page.locator("#prev-yes").is_checked())
        self.assertFalse(page.locator("#prev-no").is_checked())
        self.assertEqual(result["missing_required"], [])

    def test_workday_my_information_previously_employed_with_us_selects_no(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <fieldset>
                <legend>Have you been previously employed with us?*</legend>
                <label><input id="employed-yes" name="previouslyEmployed" type="radio" required value="Yes"> Yes</label>
                <label><input id="employed-no" name="previouslyEmployed" type="radio" required value="No"> No</label>
              </fieldset>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-prev-employed"},
            self.probe_req(),
        )

        self.assertTrue(page.locator("#employed-no").is_checked())
        self.assertEqual(result["missing_required"], [])

    def test_workday_detector_ignores_stale_required_errors_after_value_selected(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <section>
                <label id="source-label" for="source--source">How Did You Hear About Us?*</label>
                <button id="source--source" name="source" aria-haspopup="listbox" aria-labelledby="source-label">
                  1 item selected, eFinancial Careers
                </button>
                <div role="alert">The field How Did You Hear About Us? is required and must have a value.</div>
              </section>
              <fieldset>
                <legend>Have you previously worked for our Organization?*</legend>
                <label><input id="prev-yes" name="candidateIsPreviousWorker" type="radio" required value="true"> Yes</label>
                <label><input id="prev-no" name="candidateIsPreviousWorker" type="radio" required value="false" checked> No</label>
                <div role="alert">The field Have you previously worked for our Organization? is required and must have a value.</div>
              </fieldset>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        questions = playwright_server.detect_visible_required_questions(page, approved_answers={}, user_data={})

        self.assertEqual(questions, [])

    def test_workday_my_information_formerly_employed_at_stord_selects_no(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <fieldset>
                <legend>If you were formerly employed at Stord, do you have a former worker record?*</legend>
                <label><input id="stord-yes" name="formerStord" type="radio" required value="Yes"> Yes</label>
                <label><input id="stord-no" name="formerStord" type="radio" required value="No"> No</label>
              </fieldset>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-former-stord"},
            self.probe_req(),
        )

        self.assertTrue(page.locator("#stord-no").is_checked())
        self.assertEqual(result["missing_required"], [])

    def test_workday_my_information_required_phone_device_type_selects_mobile(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <section>
                <h4>Phone</h4>
                <label id="device-label" for="phoneNumber--phoneType">Phone Device Type*</label>
                <button id="phoneNumber--phoneType" name="phoneType" aria-haspopup="listbox" aria-labelledby="device-label"
                  aria-controls="deviceOptions" onclick="deviceOptions.hidden=false; this.setAttribute('aria-expanded', 'true')">Select One</button>
                <div>Error: The field Phone Device Type is required and must have a value.</div>
                <label for="phoneNumber--countryPhoneCode">Country Phone Code*</label>
                <input id="phoneNumber--countryPhoneCode" value="United States of America (+1)" />
                <label for="phoneNumber--phoneNumber">Phone Number*</label>
                <input id="phoneNumber--phoneNumber" value="2172500626" />
                <label for="phoneNumber--extension">Phone Extension</label>
                <input id="phoneNumber--extension" value="" />
                <ul id="deviceOptions" role="listbox" hidden>
                  <li role="option" onclick="device.textContent='Business Mobile'; deviceOptions.hidden=true">Business Mobile</li>
                  <li role="option" onclick="device.textContent='Home'; deviceOptions.hidden=true">Home</li>
                </ul>
              </section>
              <script>
                const device = document.getElementById('phoneNumber--phoneType');
                const deviceOptions = document.getElementById('deviceOptions');
              </script>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-device"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#phoneNumber--phoneType").inner_text(), "Business Mobile")
        self.assertEqual(result["missing_required"], [])

    def test_workday_my_information_phone_device_type_uses_own_listbox(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <section>
                <label id="source-label" for="source--source">How Did You Hear About Us?*</label>
                <button id="source--source" name="source" aria-haspopup="listbox" aria-labelledby="source-label"
                  aria-controls="sourceOptions" aria-expanded="true">1 item selected, Careers in Poland</button>
                <ul id="sourceOptions" role="listbox">
                  <li role="option">Careers in Poland</li>
                  <li role="option">LinkedIn</li>
                </ul>
              </section>
              <section>
                <label id="country-label" for="country--country">Country*</label>
                <button id="country--country" name="country" aria-haspopup="listbox" aria-labelledby="country-label">
                  United States of America
                </button>
              </section>
              <section>
                <h4>Phone</h4>
                <label id="device-label" for="phoneNumber--phoneType">Phone Device Type*</label>
                <button id="phoneNumber--phoneType" name="phoneType" aria-haspopup="listbox" aria-labelledby="device-label"
                  aria-controls="deviceOptions" onclick="deviceOptions.hidden=false; this.setAttribute('aria-expanded', 'true')">Select One</button>
                <label for="phoneNumber--countryPhoneCode">Country Phone Code*</label>
                <input id="phoneNumber--countryPhoneCode" value="United States of America (+1)" />
                <label for="phoneNumber--phoneNumber">Phone Number*</label>
                <input id="phoneNumber--phoneNumber" value="2172500626" />
                <ul id="deviceOptions" role="listbox" hidden>
                  <li role="option" data-value="android" onclick="device.textContent='Mobile/Android'; device.setAttribute('aria-label', 'Phone Device Type Mobile/Android Required'); deviceOptions.hidden=true">Mobile/Android</li>
                  <li role="option" data-value="ios" onclick="device.textContent='Mobile/Apple iOS'; device.setAttribute('aria-label', 'Phone Device Type Mobile/Apple iOS Required'); deviceOptions.hidden=true">Mobile/Apple iOS</li>
                </ul>
              </section>
              <script>
                const device = document.getElementById('phoneNumber--phoneType');
                const deviceOptions = document.getElementById('deviceOptions');
              </script>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-device-scoped"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#phoneNumber--phoneType").inner_text(), "Mobile/Android")
        self.assertEqual(result["missing_required"], [])

    def test_workday_my_information_phone_device_type_without_priority_match_blocks(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <section>
                <label id="device-label" for="phoneNumber--phoneType">Phone Device Type*</label>
                <button id="phoneNumber--phoneType" name="phoneType" aria-haspopup="listbox" aria-labelledby="device-label"
                  aria-controls="deviceOptions" onclick="deviceOptions.hidden=false; this.setAttribute('aria-expanded', 'true')">Select One</button>
                <label for="phoneNumber--countryPhoneCode">Country Phone Code*</label>
                <input id="phoneNumber--countryPhoneCode" value="United States of America (+1)" />
                <label for="phoneNumber--phoneNumber">Phone Number*</label>
                <input id="phoneNumber--phoneNumber" value="2172500626" />
                <ul id="deviceOptions" role="listbox" hidden>
                  <li role="option" onclick="device.textContent='Home'; deviceOptions.hidden=true">Home</li>
                  <li role="option" onclick="device.textContent='Work'; deviceOptions.hidden=true">Work</li>
                </ul>
              </section>
              <script>
                const device = document.getElementById('phoneNumber--phoneType');
                const deviceOptions = document.getElementById('deviceOptions');
              </script>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"application_id": "app-device-no-match"},
                self.probe_req(),
            )

        self.assertEqual(page.locator("#phoneNumber--phoneType").inner_text(), "Select One")
        self.assertEqual(result["outcome_type"], "MY_INFORMATION_BLOCKED")
        self.assertEqual(result["unresolved_required_fields"][0]["group"], "phone_group")
        self.assertIn("phone_device_type", result["unresolved_required_fields"][0]["missing_subfields"])
        self.assertEqual(captured[0]["stage"], "my_information")

    def test_workday_my_information_phone_extension_is_skipped_even_if_marked_required(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <section data-field-container="phone">
                <label for="device">Phone Device Type*</label>
                <select id="device" required>
                  <option value="Mobile" selected>Mobile</option>
                </select>
                <label for="phone">Phone Number*</label>
                <input id="phone" name="phoneNumber" required value="2172500626">
                <label for="extension">Phone Extension*</label>
                <input id="extension" name="phoneNumber--extension" required value="">
              </section>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")
        fields = playwright_server.extract_form_schema(page, {})

        result = playwright_server.fill_discovery_page_fields(
            page,
            fields,
            {"application_id": "app-extension"},
            self.probe_req(),
        )
        preflight = playwright_server.build_preflight(page, fields, {"application_id": "app-extension"}, self.probe_req(), stage="my_information")

        self.assertEqual(result["missing_required"], [])
        self.assertEqual(result["execution_policy"]["mode"], "deterministic_workday_my_information_convergence")
        self.assertFalse(any("Extension" in item.get("field", "") for item in preflight["blocking_issues"]))
        self.assertEqual(result["field_groups"]["phone_group"]["missing_subfields"], [])
        self.assertNotIn("phone_extension", result["field_groups"]["phone_group"]["subfields"])

    def test_workday_my_information_alerts_found_capitalization_warning_does_not_block(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <label for="first">First Name/Given Name*</label>
              <input id="first" value="Yifan" required>
              <div role="alert">
                <h4>Alerts Found</h4>
                <p>First Name/Given Name uses capitalization that may need review.</p>
              </div>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-alert"},
            self.probe_req(),
        )

        self.assertEqual(result["missing_required"], [])
        self.assertTrue(result["alerts"])
        self.assertFalse(result["validation_errors"])
        self.assertFalse(playwright_server.page_has_validation_errors(page))

    def test_workday_my_information_only_hard_validation_text_blocks(self):
        warning_page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <label for="first">First Name/Given Name*</label>
              <input id="first" value="Yifan" required>
              <div role="alert">Please select a value if applicable.</div>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        warning_result = playwright_server.fill_discovery_page_fields(
            warning_page,
            playwright_server.extract_form_schema(warning_page, {}),
            {"application_id": "app-soft-warning"},
            self.probe_req(),
        )

        self.assertEqual(warning_result["missing_required"], [])
        self.assertFalse(warning_result["validation_errors"])
        self.assertFalse(playwright_server.page_has_validation_errors(warning_page))

        error_page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <label for="first">First Name/Given Name*</label>
              <input id="first" value="Yifan" required>
              <div role="alert">Validation error: required profile data is missing.</div>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")

        error_result = playwright_server.fill_discovery_page_fields(
            error_page,
            playwright_server.extract_form_schema(error_page, {}),
            {"application_id": "app-hard-error"},
            self.probe_req(),
        )

        self.assertTrue(error_result["validation_errors"])
        self.assertTrue(playwright_server.page_has_validation_errors(error_page))

    def test_workday_my_information_loading_country_state_returns_loading_stuck(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <label id="country-label" for="country">Country*</label>
              <button id="country" aria-haspopup="listbox" aria-labelledby="country-label">Loading</button>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")
        fields = playwright_server.extract_form_schema(page, {})
        loading = [{"field": "Country country", "current_value": "Loading", "reason": "workday_loading_stuck"}]

        with patch.object(playwright_server, "wait_for_workday_profile_loading_to_settle", return_value=(fields, loading)):
            result = playwright_server.fill_discovery_page_fields(
                page,
                fields,
                {"application_id": "app-loading"},
                self.probe_req(),
            )

        self.assertEqual(result["outcome_type"], "WORKDAY_LOADING_STUCK")
        self.assertEqual(result["blocked_reason"], "workday_loading_stuck")
        self.assertEqual(result["missing_required"][0]["reason"], "workday_loading_stuck")
        self.assertEqual(result["exit_condition"], "NEEDS_RETRY")
        self.assertEqual(result["state_snapshot"]["country"]["status"], "blocked")
        self.assertIn("address_group", result["field_groups"])

    def test_workday_my_information_unresolved_required_field_produces_blocker_not_timeout(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h3>My Information</h3>
              <label id="hear-label" for="hear">How Did You Hear About Us?*</label>
              <button id="hear" name="source" aria-haspopup="listbox" aria-labelledby="hear-label">0 items selected</button>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually")
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"application_id": "app-unresolved"},
                self.probe_req(),
            )

        self.assertEqual(result["outcome_type"], "MY_INFORMATION_BLOCKED")
        self.assertEqual(result["blocked_reason"], "my_information_required_fields_unresolved")
        self.assertEqual(result["question_blocker"]["status"], BLOCKED_ON_QUESTIONS)
        self.assertEqual(captured[0]["stage"], "my_information")
        self.assertEqual(result["exit_condition"], BLOCKED_ON_QUESTIONS)
        self.assertEqual(result["state_snapshot"]["how_heard"]["status"], "blocked")
        self.assertTrue(result["unresolved_required_fields"])
        self.assertEqual(result["unresolved_required_fields"][0]["group"], "how_heard_group")
        self.assertEqual(result["unresolved_required_fields"][0]["missing_subfields"], ["how_heard"])

    def test_protected_workday_country_does_not_create_first_valid_probe_metadata(self):
        page = self.open_probe_page("""
            <main>
              <section>
                <label id="country-label" for="country--country">Country/Region*</label>
                <button id="country--country" name="country" aria-haspopup="listbox" aria-labelledby="country-label"
                  onclick="countryOptions.hidden=false">Select One</button>
                <ul id="countryOptions" role="listbox" hidden>
                  <li role="option" onclick="country.textContent='Anguilla'; countryOptions.hidden=true">Anguilla</li>
                  <li role="option" onclick="country.textContent='United States of America'; countryOptions.hidden=true">United States of America</li>
                </ul>
              </section>
              <script>
                const country = document.getElementById('country--country');
                const countryOptions = document.getElementById('countryOptions');
              </script>
            </main>
        """)
        captured, fake_persist = self.capture_blockers()

        with patch.object(playwright_server, "persist_question_blocker_to_memory", side_effect=fake_persist):
            result = playwright_server.fill_discovery_page_fields(
                page,
                playwright_server.extract_form_schema(page, {}),
                {"application_id": "app-country-default"},
                self.probe_req(),
            )

        self.assertEqual(page.locator("#country--country").inner_text(), "United States of America")
        self.assertEqual(captured, [])
        self.assertNotIn("question_blocker", result)
        self.assertFalse(any(item.get("source") == "first_valid_select_probe" for item in result["filled"]))

    def test_protected_workday_country_already_us_does_not_modify(self):
        page = self.open_probe_page("""
            <main>
              <section>
                <label id="country-label" for="country--country">Country/Region*</label>
                <button id="country--country" name="country" aria-haspopup="listbox" aria-labelledby="country-label"
                  onclick="window.countryClicks=(window.countryClicks||0)+1; countryOptions.hidden=false">United States of America</button>
                <ul id="countryOptions" role="listbox" hidden>
                  <li role="option" onclick="country.textContent='Anguilla'; countryOptions.hidden=true">Anguilla</li>
                  <li role="option" onclick="country.textContent='United States of America'; countryOptions.hidden=true">United States of America</li>
                </ul>
              </section>
              <script>
                window.countryClicks = 0;
                const country = document.getElementById('country--country');
                const countryOptions = document.getElementById('countryOptions');
              </script>
            </main>
        """)

        result = playwright_server.fill_discovery_page_fields(
            page,
            playwright_server.extract_form_schema(page, {}),
            {"application_id": "app-country-already-us"},
            self.probe_req(),
        )

        self.assertEqual(page.locator("#country--country").inner_text(), "United States of America")
        self.assertEqual(page.evaluate("window.countryClicks"), 0)
        self.assertEqual(result["missing_required"], [])

    def test_first_valid_workday_option_ignores_progress_items(self):
        page = self.open_probe_page("""
            <main>
              <ol aria-label="Progress">
                <li role="option">completed step 1 of 5 My Information</li>
                <li role="option">current step 2 of 5 My Experience</li>
              </ol>
              <label id="source-label" for="source">How Did You Hear About Us?*</label>
              <button id="source" aria-haspopup="listbox" aria-labelledby="source-label"
                onclick="document.getElementById('source-options').hidden=false">Select One</button>
              <ul id="source-options" role="listbox" hidden>
                <li role="option" onclick="source.textContent='Company Website'; source.dataset.value='company'">Company Website</li>
                <li role="option" onclick="source.textContent='LinkedIn'; source.dataset.value='linkedin'">LinkedIn</li>
              </ul>
            </main>
        """)
        field = {
            "selector": "button#source",
            "scope_index": 0,
            "tag": "button",
            "input_type": "select",
        }

        selected = playwright_server.select_first_valid_option_for_field(page, field)

        self.assertEqual(selected["selected"], "Company Website")
        self.assertEqual(page.locator("#source").inner_text(), "Company Website")

    def test_education_section_text_does_not_match_footer_year(self):
        page = self.open_probe_page("""
            <main>
              <h2>Education</h2>
              <label>School or University</label>
              <input value="University of Illinois at Urbana-Champaign">
              <label>To (Actual or Expected)</label>
              <input id="education-1--lastYearAttended-dateSectionYear-input" value="Year">
              <h2>Languages</h2>
              <footer>© 2026 Workday, Inc. All rights reserved.</footer>
            </main>
        """)

        self.assertFalse(playwright_server.workday_section_contains_text(
            page,
            "Education",
            playwright_server.WORKDAY_EDUCATION_SECTION_STOPS,
            "2026",
        ))

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
        self.assertEqual(entries[0]["start"]["month_number"], "05")
        self.assertEqual(entries[0]["start"]["date"], "05/2024")
        self.assertEqual(entries[0]["end"]["month_number"], "08")
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

    def test_workday_provisional_select_question_uses_group_text(self):
        field = {
            "label": " Select One Required",
            "raw_label": " Select One Required",
            "group_text": "How did you hear about us?* Select One",
            "input_type": "select",
            "required": True,
            "risk": "low",
            "id": "primaryQuestionnaire--abc123",
            "name": "abc123",
        }

        question, metadata = playwright_server.provisional_select_question_from_field(
            field,
            {"selected": "Company Website", "options": ["Company Website", "LinkedIn"]},
        )

        self.assertEqual(question.raw_text, "How did you hear about us?")
        self.assertNotIn("primaryQuestionnaire", question.raw_text)
        self.assertEqual(question.options, ["Company Website", "LinkedIn"])
        self.assertTrue(metadata["conditional_branch_probe"])

    def test_first_valid_select_probe_blocks_workday_compliance_questions(self):
        non_compete = {
            "label": " Select One Required",
            "raw_label": " Select One Required",
            "group_text": "Non-compete Question: Have you signed any agreement with restrictions on competition?* Select One",
            "input_type": "select",
            "required": True,
            "risk": "low",
        }
        existing_employee = {
            "label": " Select One Required",
            "raw_label": " Select One Required",
            "group_text": "Are you an existing HP employee?* Select One",
            "input_type": "select",
            "required": True,
            "risk": "low",
        }

        self.assertFalse(playwright_server.can_choose_first_valid_required_select(non_compete))
        self.assertFalse(playwright_server.can_choose_first_valid_required_select(existing_employee))

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
        page.route("https://*.myworkdayjobs.com/**", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
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
              <h1 id="state">Application Form</h1>
              <button id="continue" onclick="document.body.dataset.clicked='continue'; state.textContent='Next Step'">Continue</button>
            </main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        classification_payload = {
            "observed_state": "application_form",
            "confidence": 0.94,
            "evidence": ["Continue"],
            "candidate_transitions": [{
                "from_state": "application_form",
                "transition": "click_continue",
                "expected_states": ["unknown"],
                "postconditions": ["page_content_changed"],
            }],
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

    def test_visual_local_plan_rejects_low_confidence_transition(self):
        page = self.open_probe_page("""
            <main><button onclick="document.body.dataset.clicked='1'">Continue</button></main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        payload = {
            "observed_state": "unknown",
            "confidence": 0.40,
            "evidence": ["Continue"],
            "candidate_transitions": [{
                "from_state": "unknown",
                "transition": "click_continue",
                "expected_states": ["unknown"],
                "postconditions": ["page_content_changed"],
            }],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(payload)):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertFalse(result["acted"])
        self.assertEqual(result["reason"], "candidate_plan_rejected")
        self.assertIn("confidence_below_threshold", " ".join(result["candidate_plan_validation"]["issues"]))
        self.assertIsNone(page.evaluate("document.body.dataset.clicked || null"))

    def test_visual_local_plan_rejects_ungrounded_evidence(self):
        page = self.open_probe_page("""
            <main><button onclick="document.body.dataset.clicked='1'">Continue</button></main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        payload = {
            "observed_state": "unknown",
            "confidence": 0.98,
            "evidence": ["MFA verification code is required"],
            "candidate_transitions": [{
                "from_state": "unknown",
                "transition": "click_continue",
                "expected_states": ["unknown"],
                "postconditions": ["page_content_changed"],
            }],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(payload)):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertFalse(result["acted"])
        self.assertIn("model_evidence_not_grounded_in_page", result["candidate_plan_validation"]["issues"])
        self.assertIsNone(page.evaluate("document.body.dataset.clicked || null"))

    def test_visual_local_plan_cannot_downgrade_auth_error_to_sign_in(self):
        page = self.open_probe_page("""
            <main>
              <h1>Sign In</h1>
              <div role="alert">Invalid credentials</div>
              <label>Email Address<input type="email"></label>
              <label>Password<input type="password"></label>
              <button onclick="document.body.dataset.clicked='1'">Sign In</button>
            </main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        payload = {
            "observed_state": "sign_in",
            "confidence": 0.99,
            "evidence": ["Invalid credentials", "Sign In"],
            "candidate_transitions": [{
                "from_state": "sign_in",
                "transition": "click_create_account",
                "expected_states": ["create_account"],
                "postconditions": ["state_or_url_changed"],
            }],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(payload)):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertFalse(result["acted"])
        self.assertIn(
            "observed_state_conflicts_with_runtime:sign_in!=auth_error",
            result["candidate_plan_validation"]["issues"],
        )
        self.assertIsNone(page.evaluate("document.body.dataset.clicked || null"))

    def test_visual_local_plan_cannot_bypass_disabled_resume_autofill_choice(self):
        page = self.open_probe_page("""
            <main>
              <h1>Start Your Application</h1>
              <button onclick="document.body.dataset.choice='autofill'">Autofill with Resume</button>
              <button>Apply Manually</button>
            </main>
        """)
        req = SimpleNamespace(
            resume_path="",
            allow_resume_upload=False,
            allow_terms_acceptance=False,
            allow_email_verification=False,
            disable_resume_autofill_choice=True,
            prefer_manual_apply=True,
        )
        payload = {
            "observed_state": "application_choice",
            "confidence": 0.99,
            "evidence": ["Start Your Application", "Autofill with Resume"],
            "candidate_transitions": [{
                "from_state": "application_choice",
                "transition": "click_autofill_with_resume",
                "expected_states": ["resume_upload"],
                "postconditions": ["state_or_url_changed"],
            }],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(payload)):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        rejected_issues = [
            issue
            for candidate in result["candidate_plan_validation"]["rejected_candidates"]
            for issue in candidate["issues"]
        ]
        self.assertFalse(result["acted"])
        self.assertIn(
            "transition_disallowed_by_request:click_autofill_with_resume:resume_autofill_choice_disabled",
            rejected_issues,
        )
        self.assertIsNone(page.evaluate("document.body.dataset.choice || null"))

    def test_visual_local_plan_checks_candidate_acknowledgment_and_verifies_postcondition(self):
        page = self.open_probe_page("""
            <main>
              <h1>Create Account</h1>
              <p>Password Requirements</p>
              <label>Email Address<input type="email"></label>
              <label>Password<input type="password"></label>
              <label>Verify New Password<input type="password"></label>
              <label for="ack">Candidate acknowledgment</label>
              <input id="ack" type="checkbox" aria-invalid="true">
              <div role="alert">Please check the box to continue</div>
              <button>Create Account</button>
            </main>
        """)
        req = SimpleNamespace(
            resume_path="",
            allow_resume_upload=False,
            allow_terms_acceptance=True,
            allow_email_verification=False,
        )
        payload = {
            "observed_state": "create_account_validation",
            "confidence": 0.99,
            "evidence": ["Candidate acknowledgment", "Please check the box to continue"],
            "candidate_transitions": [{
                "from_state": "create_account_validation",
                "transition": "check_terms_acknowledgment",
                "expected_states": ["create_account_validation"],
                "postconditions": ["terms_acknowledgment_checked"],
            }],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(payload)):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertTrue(result["acted"])
        self.assertTrue(page.locator("#ack").is_checked())
        self.assertTrue(result["postcondition_result"]["verified"])

    def test_visual_local_plan_resolves_email_verification_before_sign_in(self):
        page = self.open_probe_page("""
            <main>
              <h1>Sign In</h1>
              <div id="verification" role="alert">An email has been sent to you. Please verify your account.</div>
              <label>Email Address<input type="email"></label>
              <label>Password<input type="password"></label>
              <button>Sign In</button>
              <a>Create Account</a>
            </main>
        """)
        req = SimpleNamespace(
            resume_path="",
            allow_resume_upload=False,
            allow_terms_acceptance=False,
            allow_email_verification=True,
            wait_for_email_seconds=5,
        )
        payload = {
            "observed_state": "email_verification",
            "confidence": 0.99,
            "evidence": ["An email has been sent to you", "Please verify your account"],
            "candidate_transitions": [{
                "from_state": "email_verification",
                "transition": "resolve_email_verification",
                "expected_states": ["sign_in"],
                "postconditions": ["email_verification_resolved"],
            }],
        }

        def resolve_email(page_arg, _email, _timeout, **_kwargs):
            page_arg.locator("#verification").evaluate("el => el.remove()")
            return True, "link", {"verified": True, "method": "link"}

        with (
            patch.object(playwright_server, "client", self.fake_vision_client(payload)),
            patch.object(playwright_server, "resolve_email_challenge", side_effect=resolve_email) as resolver,
        ):
            result = playwright_server.visual_access_step(
                page,
                req,
                {
                    "email": "candidate@example.com",
                    "_workday_email_verification_not_before_epoch": time.time(),
                    "_workday_email_verification_correlation_id": "unit-correlation",
                },
                reason="unit",
            )

        self.assertTrue(result["acted"])
        self.assertEqual(result["after_snapshot"]["planner_state"], "sign_in")
        self.assertTrue(result["postcondition_result"]["verified"])
        self.assertTrue(result["email_verification"]["verified"])
        resolver.assert_called_once()

    def test_email_verification_timeout_is_auth_blocked_with_gate_evidence(self):
        page = self.open_workday_autofill_page(
            """
            <main>
              <h1>Verify Your Email</h1>
              <div role="alert">An email has been sent to you. Please verify your account.</div>
            </main>
            """,
            url="https://unit.wd5.myworkdayjobs.com/en-US/test/login",
        )
        req = SimpleNamespace(
            url="https://unit.wd5.myworkdayjobs.com/en-US/test/job/R0001",
            application_id="email-timeout-unit",
            batch_id="email-timeout-batch",
            max_steps=1,
            wait_for_email_seconds=5,
            allow_email_verification=True,
            allow_visual_fallback=False,
            allow_terms_acceptance=False,
            allow_account_creation=True,
            discover_all_steps=False,
            stop_at_form=True,
        )
        verification_evidence = {
            "verified": False,
            "poll_count": 1,
            "service_statuses": [404],
        }

        with (
            patch.object(playwright_server, "wait_for_apply_page_ready", return_value={"ready": True}),
            patch.object(playwright_server, "dismiss_popups"),
            patch.object(playwright_server, "recover_workday_transient_error_page", return_value=0),
            patch.object(playwright_server, "extract_form_schema", return_value=[]),
            patch.object(playwright_server, "infer_apply_stage", return_value="email_verification"),
            patch.object(playwright_server, "build_page_state", return_value={}),
            patch.object(playwright_server, "build_preflight", return_value={}),
            patch.object(playwright_server, "get_registry_account", return_value={}),
            patch.object(
                playwright_server,
                "get_application_password",
                return_value=("unused", "default"),
            ),
            patch.object(playwright_server, "click_matching_control", return_value=(False, None)),
            patch.object(
                playwright_server,
                "resolve_email_challenge",
                return_value=(False, "email_verification_timeout", verification_evidence),
            ) as resolver,
            patch.object(playwright_server, "capture_apply_screenshot", return_value="auth.png"),
            patch.object(
                playwright_server,
                "_write_workday_auth_diagnostic_artifacts",
                side_effect=lambda value: value,
            ),
        ):
            result = playwright_server.run_apply_access_state_machine(
                page,
                req,
                {"email": "candidate@example.com"},
            )

        self.assertEqual(result["stage"], "AUTH")
        self.assertEqual(result["outcome_type"], "AUTH_BLOCKED")
        self.assertEqual(result["blocked_reason"], "email_verification_timeout")
        self.assertEqual(result["email_verification"], verification_evidence)
        self.assertEqual(result["screenshot_path"], "auth.png")
        self.assertTrue(result["smoke_evidence"]["blocker_screenshot_trace_on_failure"]["verified"])
        call = resolver.call_args
        self.assertGreater(call.kwargs["not_before_epoch"], 0)
        self.assertTrue(call.kwargs["correlation_id"])

    def test_visual_local_plan_requires_postcondition_before_marking_action_successful(self):
        page = self.open_probe_page("""
            <main><button onclick="document.body.dataset.clicks=String(Number(document.body.dataset.clicks || 0)+1)">Continue</button></main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        payload = {
            "observed_state": "unknown",
            "confidence": 0.99,
            "evidence": ["Continue"],
            "candidate_transitions": [{
                "from_state": "unknown",
                "transition": "click_continue",
                "expected_states": ["unknown"],
                "postconditions": ["page_content_changed"],
            }],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(payload)):
            result = playwright_server.visual_access_step(page, req, {}, reason="unit")

        self.assertTrue(result["attempted"])
        self.assertFalse(result["acted"])
        self.assertEqual(result["reason"], "candidate_transition_postcondition_failed")
        self.assertEqual(page.evaluate("document.body.dataset.clicks"), "1")

    def test_visual_local_state_machine_stops_repeated_no_progress_plan_before_second_click(self):
        page = self.open_probe_page("""
            <main><button onclick="document.body.dataset.clicks=String(Number(document.body.dataset.clicks || 0)+1)">Continue</button></main>
        """)
        req = SimpleNamespace(resume_path="", allow_resume_upload=False)
        payload = {
            "observed_state": "unknown",
            "confidence": 0.99,
            "evidence": ["Continue"],
            "candidate_transitions": [{
                "from_state": "unknown",
                "transition": "click_continue",
                "expected_states": ["unknown"],
                "postconditions": ["page_content_changed"],
            }],
        }

        with patch.object(playwright_server, "client", self.fake_vision_client(payload)):
            result = playwright_server.run_llm_local_state_machine(page, req, {}, reason="unit", max_attempts=2)

        self.assertFalse(result["acted"])
        self.assertEqual(result["stop_reason"], "no_safe_action")
        self.assertEqual(result["reason"], "local_plan_no_progress")
        self.assertEqual(page.evaluate("document.body.dataset.clicks"), "1")
        json.dumps(result)

    def test_visual_provider_failure_does_not_become_no_safe_action(self):
        page = self.open_probe_page("<main><button>Apply</button></main>")
        req = SimpleNamespace()
        failure = {
            "ok": False,
            "reason": "vision_query_failed:invalid provider credential",
        }

        with patch.object(playwright_server, "classify_visual_access_state", return_value=failure):
            result = playwright_server.run_llm_local_state_machine(
                page,
                req,
                {},
                reason="unit",
            )

        self.assertFalse(result["acted"])
        self.assertTrue(result["provider_unavailable"])
        self.assertEqual(result["reason"], "vision_provider_unavailable")
        self.assertNotIn("stop_reason", result)

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

    def test_batch_worker_payload_disables_workday_resume_autofill_choice(self):
        from scripts import live_hp_smoke_worker

        args = SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            resume_path="resume.pdf",
            application_id="app-batch",
            batch_id="batch-live",
        )

        with patch.dict(os.environ, {
            "WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FALLBACK": "",
            "WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FIELD_FALLBACK": "",
            "WORKDAY_LIVE_SMOKE_ALLOW_RESUME_UPLOAD": "",
            "WORKDAY_LIVE_SMOKE_ALLOW_EMAIL_VERIFICATION": "",
        }):
            payload = live_hp_smoke_worker.build_access_apply_payload(args, {"email": "user@example.com"})

        self.assertTrue(payload["prefer_manual_apply"])
        self.assertTrue(payload["disable_resume_autofill_choice"])
        self.assertFalse(payload["allow_visual_fallback"])
        self.assertFalse(payload["allow_visual_field_fallback"])
        self.assertFalse(payload["allow_resume_upload"])
        self.assertFalse(payload["allow_email_verification"])
        self.assertFalse(payload["confirm_submit"])

        with patch.dict(os.environ, {
            "WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FALLBACK": "1",
            "WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FIELD_FALLBACK": "1",
            "WORKDAY_LIVE_SMOKE_ALLOW_RESUME_UPLOAD": "1",
            "WORKDAY_LIVE_SMOKE_ALLOW_EMAIL_VERIFICATION": "1",
            "WORKDAY_LIVE_SMOKE_EMAIL_VERIFICATION_TIMEOUT_SECONDS": "180",
        }):
            opted_in = live_hp_smoke_worker.build_access_apply_payload(args, {"email": "user@example.com"})

        self.assertTrue(opted_in["allow_visual_fallback"])
        self.assertTrue(opted_in["allow_visual_field_fallback"])
        self.assertTrue(opted_in["allow_resume_upload"])
        self.assertTrue(opted_in["allow_email_verification"])
        self.assertEqual(opted_in["wait_for_email_seconds"], 180)
        self.assertFalse(opted_in["confirm_submit"])

    def test_workday_choice_prefers_apply_manually_over_autofill_when_disabled(self):
        page = self.open_workday_autofill_page("""
            <main>
              <h1>Start Your Application</h1>
              <button onclick="document.body.dataset.choice='autofill'">Autofill with Resume</button>
              <button onclick="document.body.dataset.choice='manual'">Apply Manually</button>
            </main>
        """, url="https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply")

        clicked, label = playwright_server.click_workday_application_choice(
            page,
            prefer_resume=True,
            prefer_manual=True,
            disable_resume_autofill_choice=True,
        )

        self.assertTrue(clicked)
        self.assertEqual(label, "Apply Manually")
        self.assertEqual(page.evaluate("document.body.dataset.choice"), "manual")

    def test_manual_apply_flags_still_allow_normal_resume_upload(self):
        page = self.open_probe_page("""
            <main>
              <h1>My Information</h1>
              <label for="resume">Upload Resume</label>
              <input id="resume" type="file">
            </main>
        """)
        resume_path = self.temp_resume_pdf()
        req = SimpleNamespace(
            resume_path=resume_path,
            allow_resume_upload=True,
            prefer_manual_apply=True,
            disable_resume_autofill_choice=True,
        )

        result = playwright_server.handle_resume_upload_prompt(page, req)

        self.assertTrue(result["attempted"])
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["method"], "input[type=file]")
        self.assertIn(Path(resume_path).name, page.locator("#resume").input_value())

    def test_workday_autofill_stuck_returns_structured_outcome_after_failed_recovery(self):
        page = self.open_workday_autofill_page("""
            <main>
              <nav>My Information My Experience Application Questions Review</nav>
              <p>Follow Us</p>
            </main>
        """)
        req = SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            prefer_manual_apply=True,
            disable_resume_autofill_choice=True,
        )
        resume_upload = {"attempted": False, "reason": "resume_autofill_disabled_by_request"}

        with (
            patch.object(page, "goto", return_value=None),
            patch.object(playwright_server, "wait_for_apply_page_ready", return_value={"ready": False}),
            patch.object(playwright_server, "capture_apply_screenshot", return_value="mock-stuck.png"),
        ):
            recovery = playwright_server.recover_workday_autofill_resume_stuck(page, req, resume_upload=resume_upload)
            result = playwright_server.autofill_resume_stuck_result(
                page,
                req,
                resume_upload=resume_upload,
                blank_recovery=recovery,
            )

        self.assertFalse(recovery["recovered"])
        self.assertEqual(recovery["reason"], "workday_autofill_resume_stuck")
        self.assertEqual(result["outcome_type"], "AUTOFILL_RESUME_STUCK")
        self.assertEqual(result["blocked_reason"], "workday_autofill_resume_stuck")
        self.assertIn("autofillWithResume", result["current_url"])
        self.assertEqual(result["resume_upload"], resume_upload)
        self.assertEqual(result["blank_step_recovery"], recovery)
        self.assertTrue(result["dom_excerpt"])

    def test_batch_summary_includes_resume_autofill_stuck_diagnostics(self):
        from scripts import run_live_workday_batch

        data = {
            "success": False,
            "status": "timeout",
            "current_url": "https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/autofillWithResume",
            "original_job_url": "https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            "resume_upload": {"attempted": False, "reason": "resume_autofill_disabled_by_request"},
            "autofill_resume_wait": {"ready": False, "field_count": 0},
            "blank_step_recovery": {"attempted": True, "recovered": False},
        }

        summary = run_live_workday_batch.summarize_result(data)

        self.assertEqual(summary["outcome_type"], "AUTOFILL_RESUME_STUCK")
        self.assertEqual(summary["blocked_reason"], "workday_autofill_resume_stuck")
        self.assertEqual(summary["question_count"], 0)
        self.assertEqual(summary["resume_autofill"]["resume_upload"], data["resume_upload"])
        self.assertEqual(summary["resume_autofill"]["autofill_resume_wait"], data["autofill_resume_wait"])
        self.assertEqual(summary["resume_autofill"]["blank_step_recovery"], data["blank_step_recovery"])

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

    def test_submit_stops_after_three_identical_unresolved_required_iterations(self):
        class FakePage:
            url = "https://unit.myworkdayjobs.com/en-US/test/job/R0001/apply/applyManually"

            def wait_for_timeout(self, *_args, **_kwargs):
                return None

        unresolved = [{
            "field": "How Did You Hear About Us?",
            "reason": "my_information_required_prompt_unresolved",
            "canonical_key": "how_heard",
            "group": "how_heard_group",
            "missing_subfields": ["how_heard"],
            "validation_message": "The field How Did You Hear About Us? is required.",
        }]
        fill_result = {
            "filled": [],
            "missing_required": [],
            "skipped": [],
            "unresolved_required_fields": unresolved,
            "validation_errors": ["The field How Did You Hear About Us? is required."],
            "alerts": [],
            "last_attempted_field": "How Did You Hear About Us?",
            "last_attempted_action": "fill_how_heard",
            "outcome_type": "MY_INFORMATION_BLOCKED",
            "blocked_reason": "my_information_required_fields_unresolved",
            "question_blocker": {"status": BLOCKED_ON_QUESTIONS, "blocker_ids": ["b1"]},
        }
        req = self.probe_req(confirm_submit=True)
        req.max_form_pages = 5

        with patch.object(playwright_server, "wait_for_apply_page_ready", return_value={"ready": True}), \
             patch.object(playwright_server, "dismiss_popups"), \
             patch.object(playwright_server, "wait_for_workday_step_interactive", return_value={"interactive": True}), \
             patch.object(playwright_server, "handle_workday_autofill_with_resume_branch", return_value={"attempted": False}), \
             patch.object(playwright_server, "handle_resume_upload_prompt", return_value={"attempted": False}), \
             patch.object(playwright_server, "recover_workday_blank_apply_step", return_value={"attempted": False}), \
             patch.object(playwright_server, "extract_form_schema", return_value=[{"label": "How Did You Hear About Us?", "input_type": "select", "required": True}]), \
             patch.object(playwright_server, "workday_start_date_validation_errors", return_value=[]), \
             patch.object(playwright_server, "apply_page_signature", return_value="same-my-info-page"), \
             patch.object(playwright_server, "infer_apply_stage", return_value="my_information"), \
             patch.object(playwright_server, "build_preflight", return_value={}), \
             patch.object(playwright_server, "build_page_state", return_value={}), \
             patch.object(playwright_server, "fill_discovery_page_fields", return_value=fill_result) as fill_fields, \
             patch.object(playwright_server, "non_final_blocking_issues", return_value=[]), \
             patch.object(playwright_server, "page_has_final_submit", return_value=False), \
             patch.object(playwright_server, "click_next_form_step", return_value=(True, "Continue")), \
             patch.object(playwright_server, "capture_apply_screenshot", return_value="same-unresolved.png"):
            result = playwright_server.submit_application_steps(FakePage(), req, {"application_id": "app-loop"})

        self.assertEqual(fill_fields.call_count, 3)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], BLOCKED_ON_QUESTIONS)
        self.assertEqual(result["blocked_reason"], "application_question_blocker")
        self.assertEqual(result["unresolved_required_fields"], unresolved)
        self.assertEqual(result["validation_errors"], fill_result["validation_errors"])
        self.assertEqual(result["last_attempted_field"], "How Did You Hear About Us?")
        self.assertNotEqual(result["blocked_reason"], "repeated_form_page")

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

    def test_hp_compliance_questions_match_explicit_trusted_profile_answers(self):
        user_data = {
            "common_answers": {
                "government_employment": "No",
                "conflict_of_interest": "No",
                "noncompete": "No",
                "export_control": "No",
                "authorized_to_work_us": "Yes",
                "need_sponsorship": "Yes",
                "current_or_previous_company_employee": "No",
            }
        }
        cases = [
            (
                "Within the past 5 years, have you been employed by the federal or any state or local government or public institution?",
                "state",
                "government_employment",
                "No",
            ),
            ("Conflict of Interest Question 1: Would you engage in any listed conflict during HP employment?", "conflict_of_interest", "conflict_of_interest", "No"),
            ("Conflict of Interest Question 2: Have you served in a government body that regulates or purchases from HP?", "conflict_of_interest", "conflict_of_interest", "No"),
            ("Conflict of Interest Question 3: Is a family member a government official who can influence HP business?", "conflict_of_interest", "conflict_of_interest", "No"),
            ("Non-compete Question: Are you subject to restrictions on competition or solicitation that affect this role?", "", "noncompete", "No"),
            ("US Export Control screening: are you a citizen or permanent resident of a listed country?", "export_control", "export_control", "No"),
            ("Are you legally authorized to work in the job posting country?", "authorized_to_work_us", "authorized_to_work_us", "Yes"),
            ("Will you now or in the future require sponsorship for employment?", "need_sponsorship", "need_sponsorship", "Yes"),
            ("Are you an existing HP employee?", "current_or_previous_company_employee", "current_or_previous_company_employee", "No"),
        ]

        for index, (text, canonical_key, source_key, expected) in enumerate(cases):
            with self.subTest(text=text):
                question = SimpleNamespace(
                    raw_text=text,
                    normalized_text=normalize_question_text(text),
                    fingerprint=f"hp-compliance-{index}",
                    canonical_key=canonical_key,
                    question_context={},
                    control_type="select",
                    options=["Yes", "No"],
                )
                match = playwright_server.match_question_to_trusted_answer(question, user_data)

                self.assertTrue(match["matched"])
                self.assertFalse(match["requires_review"])
                self.assertEqual(match["source_key"], source_key)
                self.assertEqual(match["answer"], expected)
                self.assertEqual(playwright_server.map_trusted_answer_to_question_option(question, match["answer"]), expected)

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
