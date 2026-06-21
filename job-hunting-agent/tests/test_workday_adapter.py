from __future__ import annotations

import atexit
import importlib
import json
import os
from pathlib import Path
import random
import socket
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

warnings.filterwarnings(
    "ignore",
    message=".*_UnionGenericAlias.*",
    category=DeprecationWarning,
)

from adapters.workday.browser import launch_replay_browser
from adapters.workday.handlers import (
    CountrySelectorHandler,
    EducationRepeatableSectionHandler,
    ExecutionState,
    ExperienceRepeatableSectionHandler,
    NativeSelectHandler,
    SearchPromptHandler,
    STATUS_FAILED,
    STATUS_HUMAN_REQUIRED,
    STATUS_OK,
)
from adapters.workday.network import install_network_guard, is_allowed_replay_url, is_denied_domain


FIXTURE_DIR = ROOT / "adapters" / "workday" / "fixtures"
STAGE_DATA = json.loads((FIXTURE_DIR / "stage_data.json").read_text(encoding="utf-8"))
RUN_METRICS = {
    "education_add_clicks": 0,
    "experience_add_clicks": 0,
    "external_requests": 0,
    "blocked_urls": [],
}


def write_run_metrics() -> None:
    path = os.getenv("WORKDAY_ADAPTER_TEST_METRICS")
    if not path:
        return
    record = dict(RUN_METRICS)
    record["pid"] = os.getpid()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


atexit.register(write_run_metrics)


class WorkdayAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = launch_replay_browser(cls.playwright, headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def collect_fixture_metrics(self, page, network_metrics: dict) -> None:
        try:
            metrics = page.evaluate("window.fixtureMetrics || {}")
        except Exception:
            metrics = {}
        RUN_METRICS["education_add_clicks"] += int(metrics.get("educationAddClicks") or 0)
        RUN_METRICS["experience_add_clicks"] += int(metrics.get("experienceAddClicks") or 0)
        RUN_METRICS["external_requests"] += int(network_metrics.get("external_requests") or 0)
        RUN_METRICS["blocked_urls"].extend(network_metrics.get("blocked_urls") or [])

    def open_fixture(self, name: str):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        network_metrics: dict = {}
        install_network_guard(context, network_metrics)
        page = context.new_page()
        page.set_content((FIXTURE_DIR / f"{name}.html").read_text(encoding="utf-8"), wait_until="domcontentloaded")
        def cleanup() -> None:
            self.collect_fixture_metrics(page, network_metrics)
            context.close()
        self.addCleanup(cleanup)
        return page

    def randomize_dynamic_ids(self, page) -> None:
        page.evaluate(
            """() => {
                let index = 0;
                for (const el of document.querySelectorAll('[id]')) {
                    if (el.id === 'prompt-root' || el.id.endsWith('-rows')) continue;
                    const oldId = el.id;
                    const newId = `rnd_${++index}_${Math.random().toString(16).slice(2)}`;
                    for (const label of document.querySelectorAll(`label[for="${CSS.escape(oldId)}"]`)) {
                        label.setAttribute('for', newId);
                    }
                    el.id = newId;
                }
            }"""
        )

    def test_country_already_correct_does_not_modify(self):
        page = self.open_fixture("my_information")
        page.evaluate("document.getElementById('country--country').textContent = 'United States of America'")
        state = ExecutionState()

        result = CountrySelectorHandler().ensure_united_states(page, state)

        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "already_valid")
        self.assertEqual(page.evaluate("window.fixtureMetrics.countryOpens"), 0)
        self.assertEqual(page.evaluate("window.fixtureMetrics.countrySelections"), 0)

    def test_country_empty_selects_united_states_and_verifies(self):
        page = self.open_fixture("my_information")
        state = ExecutionState()

        result = CountrySelectorHandler().ensure_united_states(page, state)

        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(state.field_locks["country"], "valid")
        self.assertIn("United States", page.locator("#country--country").inner_text())
        self.assertEqual(page.evaluate("window.fixtureMetrics.countrySelections"), 1)

    def test_education_add_only_once(self):
        page = self.open_fixture("education")
        state = ExecutionState()
        handler = EducationRepeatableSectionHandler()

        first = handler.fill(page, STAGE_DATA["education"], state)
        second = handler.fill(page, STAGE_DATA["education"], state)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(page.evaluate("window.fixtureMetrics.educationAddClicks"), 1)
        self.assertEqual(state.section_add_attempts["education"], 1)

    def test_school_and_field_inputs_select_search_candidates(self):
        page = self.open_fixture("education")
        state = ExecutionState()

        result = EducationRepeatableSectionHandler().fill(page, STAGE_DATA["education"], state)

        fixture_state = page.evaluate("window.fixtureState.education")
        self.assertTrue(result.ok)
        self.assertEqual(fixture_state["school"], "University of Illinois at Urbana-Champaign")
        self.assertEqual(fixture_state["field"], "Computer Science and Linguistics")
        selections = page.evaluate("window.fixtureMetrics.promptSelections")
        self.assertEqual([item["kind"] for item in selections], ["school", "field"])

    def test_degree_matches_bachelor_display_variants(self):
        variants = [
            ([["partial_bachelor", "Partial Bachelor"], ["bachelors", "Bachelors Degree"]], "bachelors"),
            ([["bachelor_science", "Bachelor of Science"]], "bachelor_science"),
            ([["bachelor_apostrophe", "Bachelor's Degree"]], "bachelor_apostrophe"),
        ]
        for options, expected_value in variants:
            with self.subTest(expected_value=expected_value):
                page = self.open_fixture("education")
                page.evaluate("options => { window.fixtureConfig.degreeOptions = options; }", options)
                state = ExecutionState()

                result = EducationRepeatableSectionHandler().fill(page, STAGE_DATA["education"], state)

                self.assertTrue(result.ok)
                self.assertEqual(page.locator("select[data-field='education-degree']").input_value(), expected_value)
                self.assertNotEqual(page.locator("select[data-field='education-degree']").input_value(), "partial_bachelor")

    def test_one_field_failure_does_not_rebuild_whole_section(self):
        page = self.open_fixture("education")
        page.evaluate("window.fixtureConfig.schoolOptions = []")
        state = ExecutionState()
        handler = EducationRepeatableSectionHandler()

        first = handler.fill(page, STAGE_DATA["education"], state)
        second = handler.fill(page, STAGE_DATA["education"], state)

        self.assertFalse(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(first.reason, "option_not_found")
        self.assertEqual(page.evaluate("window.fixtureMetrics.educationAddClicks"), 1)
        self.assertEqual(state.section_add_attempts["education"], 1)
        self.assertEqual(state.field_attempts["education.school"], 2)

    def test_react_rerender_relocates_elements_after_school_selection(self):
        page = self.open_fixture("education")
        state = ExecutionState()

        result = EducationRepeatableSectionHandler().fill(page, STAGE_DATA["education"], state)

        self.assertTrue(result.ok)
        self.assertGreaterEqual(page.evaluate("window.fixtureMetrics.educationRenderCount"), 2)
        self.assertEqual(page.locator("select[data-field='education-degree']").input_value(), "bachelors")
        self.assertEqual(page.evaluate("window.fixtureState.education.field"), "Computer Science and Linguistics")

    def test_native_select_handler_uses_bachelor_variant_without_partial(self):
        page = self.open_fixture("education")
        page.click("#add-education")
        state = ExecutionState()

        result = NativeSelectHandler().select(
            page,
            "select[data-field='education-degree']",
            ["Bachelor's Degree", "Bachelors Degree", "Bachelor of Science"],
            state=state,
            key="education.degree",
        )

        self.assertTrue(result.ok)
        self.assertEqual(page.locator("select[data-field='education-degree']").input_value(), "bachelors")

    def test_experience_repeatable_section_fills_once(self):
        page = self.open_fixture("experience")
        state = ExecutionState()
        handler = ExperienceRepeatableSectionHandler()

        first = handler.fill(page, STAGE_DATA["experience"], state)
        second = handler.fill(page, STAGE_DATA["experience"], state)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(page.evaluate("window.fixtureMetrics.experienceAddClicks"), 1)
        self.assertEqual(page.locator("input[data-field='experience-title']").input_value(), STAGE_DATA["experience"]["title"])

    def test_single_education_experience_fill_add_clicks_max_one(self):
        education_page = self.open_fixture("education")
        education = EducationRepeatableSectionHandler().fill(education_page, STAGE_DATA["education"], ExecutionState())
        experience_page = self.open_fixture("experience")
        experience = ExperienceRepeatableSectionHandler().fill(experience_page, STAGE_DATA["experience"], ExecutionState())

        self.assertTrue(education.ok)
        self.assertTrue(experience.ok)
        self.assertLessEqual(education_page.evaluate("window.fixtureMetrics.educationAddClicks"), 1)
        self.assertLessEqual(experience_page.evaluate("window.fixtureMetrics.experienceAddClicks"), 1)

    def test_randomized_dynamic_dom_ids_still_pass(self):
        country_page = self.open_fixture("my_information")
        self.randomize_dynamic_ids(country_page)
        country = CountrySelectorHandler().ensure_united_states(country_page, ExecutionState())
        self.assertTrue(country.ok)

        education_page = self.open_fixture("education")
        education_page.click("#add-education")
        self.randomize_dynamic_ids(education_page)
        education = EducationRepeatableSectionHandler().fill(education_page, STAGE_DATA["education"], ExecutionState())
        self.assertTrue(education.ok)

        experience_page = self.open_fixture("experience")
        experience_page.click("#add-experience")
        self.randomize_dynamic_ids(experience_page)
        experience = ExperienceRepeatableSectionHandler().fill(experience_page, STAGE_DATA["experience"], ExecutionState())
        self.assertTrue(experience.ok)

    def test_two_add_buttons_clicks_only_education_section_add(self):
        page = self.open_fixture("education")
        page.evaluate(
            """() => {
                window.fixtureMetrics.otherAddClicks = 0;
                const top = document.createElement('button');
                top.type = 'button';
                top.textContent = 'Add';
                top.addEventListener('click', () => window.fixtureMetrics.otherAddClicks += 1);
                document.body.insertBefore(top, document.body.firstChild);
                const skills = document.querySelector('[aria-label="Skills"]');
                const skillsAdd = document.createElement('button');
                skillsAdd.type = 'button';
                skillsAdd.textContent = 'Add';
                skillsAdd.addEventListener('click', () => window.fixtureMetrics.otherAddClicks += 1);
                skills.appendChild(skillsAdd);
            }"""
        )

        result = EducationRepeatableSectionHandler().fill(page, STAGE_DATA["education"], ExecutionState())

        self.assertTrue(result.ok)
        self.assertEqual(page.evaluate("window.fixtureMetrics.educationAddClicks"), 1)
        self.assertEqual(page.evaluate("window.fixtureMetrics.otherAddClicks"), 0)

    def test_option_portal_outside_section_still_selects(self):
        page = self.open_fixture("education")
        page.click("#add-education")
        outside = page.evaluate(
            """() => {
                const root = document.getElementById('prompt-root');
                const section = document.querySelector('[data-section="education"]');
                return !section.contains(root);
            }"""
        )

        result = EducationRepeatableSectionHandler().fill(page, STAGE_DATA["education"], ExecutionState())

        self.assertTrue(outside)
        self.assertTrue(result.ok)
        self.assertEqual(page.evaluate("window.fixtureState.education.schoolId"), "univ-uiuc")

    def test_school_text_without_candidate_click_fails_validation(self):
        page = self.open_fixture("education")
        page.click("#add-education")
        page.locator("input[data-field='education-school']").fill(STAGE_DATA["education"]["school"])

        valid = SearchPromptHandler().selection_is_valid(
            page,
            "input[data-field='education-school']",
            STAGE_DATA["education"]["school"],
            hidden_id_selector="input[data-field='education-school-id']",
        )

        self.assertFalse(valid)
        self.assertEqual(page.locator("input[data-field='education-school-id']").input_value(), "")

    def test_candidate_click_requires_hidden_entity_id(self):
        page = self.open_fixture("education")
        page.evaluate("window.fixtureConfig.skipHiddenIds = true")

        result = EducationRepeatableSectionHandler().fill(page, STAGE_DATA["education"], ExecutionState())

        self.assertFalse(result.ok)
        self.assertEqual(result.field, "education.school")
        self.assertEqual(result.reason, "hidden_entity_id_missing")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(page.locator("input[data-field='education-school-id']").input_value(), "")

    def test_delayed_candidates_zero_to_one_second_are_stable(self):
        random.seed(20260619)
        delays = [random.randint(0, 1000) for _ in range(6)]
        for delay in delays:
            with self.subTest(delay_ms=delay):
                page = self.open_fixture("education")
                page.evaluate("delay => { window.fixtureConfig.promptDelayMs = delay; }", delay)
                started = time.perf_counter()

                result = EducationRepeatableSectionHandler().fill(page, STAGE_DATA["education"], ExecutionState())

                self.assertTrue(result.ok)
                self.assertLess((time.perf_counter() - started), 10)
                self.assertEqual(page.evaluate("window.fixtureState.education.schoolId"), "univ-uiuc")
                self.assertEqual(page.evaluate("window.fixtureState.education.fieldId"), "field-cs-ling")

    def test_school_success_degree_failure_retries_only_degree_then_human_required(self):
        page = self.open_fixture("education")
        page.evaluate("window.fixtureConfig.degreeOptions = []")
        state = ExecutionState()
        handler = EducationRepeatableSectionHandler()

        first = handler.fill(page, STAGE_DATA["education"], state)
        second = handler.fill(page, STAGE_DATA["education"], state)

        self.assertFalse(first.ok)
        self.assertEqual(first.field, "education.degree")
        self.assertEqual(first.status, STATUS_FAILED)
        self.assertFalse(second.ok)
        self.assertEqual(second.field, "education.degree")
        self.assertEqual(second.status, STATUS_HUMAN_REQUIRED)
        self.assertEqual(page.evaluate("window.fixtureMetrics.educationAddClicks"), 1)
        self.assertEqual(len(page.evaluate("window.fixtureMetrics.promptSelections")), 1)
        self.assertEqual(state.field_attempts["education.degree"], 2)
        self.assertEqual(state.field_attempts["education.school"], 1)

    def test_adapter_failure_keeps_existing_education_row(self):
        page = self.open_fixture("education")
        page.evaluate("window.fixtureConfig.schoolOptions = []")

        result = EducationRepeatableSectionHandler().fill(page, STAGE_DATA["education"], ExecutionState())

        self.assertFalse(result.ok)
        self.assertEqual(page.locator("[data-row='education']").count(), 1)
        self.assertEqual(page.evaluate("window.fixtureMetrics.educationAddClicks"), 1)

    def test_repeating_same_stage_twice_does_not_change_valid_fields(self):
        page = self.open_fixture("education")
        handler = EducationRepeatableSectionHandler()
        first = handler.fill(page, STAGE_DATA["education"], ExecutionState())
        metrics_after_first = page.evaluate("JSON.parse(JSON.stringify(window.fixtureMetrics))")
        state_after_first = page.evaluate("JSON.parse(JSON.stringify(window.fixtureState.education))")

        second = handler.fill(page, STAGE_DATA["education"], ExecutionState())

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(second.changed)
        self.assertEqual(page.evaluate("window.fixtureMetrics.educationAddClicks"), metrics_after_first["educationAddClicks"])
        self.assertEqual(page.evaluate("window.fixtureMetrics.promptSelections.length"), len(metrics_after_first["promptSelections"]))
        self.assertEqual(page.evaluate("window.fixtureState.education"), state_after_first)

    def test_network_policy_denies_external_and_workday_domains(self):
        self.assertTrue(is_denied_domain("boeing.com"))
        self.assertTrue(is_denied_domain("wd5.myworkdayjobs.com"))
        self.assertTrue(is_denied_domain("www.workday.com"))
        self.assertFalse(is_allowed_replay_url("https://boeing.com/jobs"))
        self.assertFalse(is_allowed_replay_url("https://wd5.myworkdayjobs.com/example"))
        self.assertFalse(is_allowed_replay_url("https://example.com/asset.js"))
        self.assertTrue(is_allowed_replay_url("http://127.0.0.1:9999/asset.js"))
        self.assertTrue(is_allowed_replay_url("data:text/plain,ok"))

    def test_port_8004_has_no_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.25)
        try:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", 8004)), 0)
        finally:
            sock.close()


class StageReplayTests(unittest.TestCase):
    def test_each_local_stage_replay_finishes_under_10_seconds(self):
        from adapters.workday.replay import run_stage

        for stage in ["my_information", "education", "experience"]:
            with self.subTest(stage=stage):
                result = run_stage(stage)
                self.assertTrue(result["ok"], result)
                self.assertLess(result["elapsed_ms"], 10000, result)
                self.assertEqual(result["network"]["external_requests"], 0, result)


class WorkdayServerSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = importlib.import_module("mcp_servers.playwright_server")

    def neutral_profile_patches(self):
        names = [
            "fill_workday_saved_question_answers",
            "fill_workday_age_over_18",
            "fill_workday_resume_question_answers",
            "fill_workday_business_disclosure_no_fields",
            "fill_workday_voluntary_disclosure_declines",
            "fill_workday_self_identification_signature_fields",
            "fill_workday_provisional_select_answers",
        ]
        patchers = [patch.object(self.server, name, return_value=[]) for name in names]
        patchers.extend([
            patch.object(self.server, "clear_labeled_text_control", return_value=False),
            patch.object(self.server, "fill_labeled_text_control", return_value=False),
            patch.object(self.server, "choose_workday_labeled_option", return_value=False),
            patch.object(self.server, "choose_workday_control_by_selector", return_value=False),
            patch.object(self.server, "force_workday_phone_country_code_us", return_value=False),
            patch.object(self.server, "workday_country_is_united_states", return_value=True),
        ])
        return patchers

    def start_patches(self, patchers):
        started = []
        for patcher in patchers:
            started.append(patcher.start())
            self.addCleanup(patcher.stop)
        return started

    def fake_workday_page(self):
        return SimpleNamespace(
            url="https://unit.myworkdayjobs.com/en-US/test/job/R0001",
            wait_for_timeout=lambda _ms: None,
        )

    def test_feature_flag_off_preserves_legacy_path(self):
        page = self.fake_workday_page()
        calls = {"education": 0, "experience": 0}

        def legacy_education(_page, _user_data):
            calls["education"] += 1
            return [{"field": "legacy education", "source": "legacy"}]

        def legacy_experience(_page, _user_data):
            calls["experience"] += 1
            return [{"field": "legacy experience", "source": "legacy"}]

        patchers = self.neutral_profile_patches() + [
            patch.dict(os.environ, {"WORKDAY_ADAPTER_V2": "0"}),
            patch.object(self.server, "fill_workday_adapter_sections", side_effect=AssertionError("adapter must be disabled")),
            patch.object(self.server, "fill_workday_education_from_resume", side_effect=legacy_education),
            patch.object(self.server, "fill_workday_experience_from_resume", side_effect=legacy_experience),
            patch.object(self.server, "force_workday_country_united_states", return_value=False),
            patch.object(self.server, "is_workday_my_experience_page", return_value=True),
        ]
        self.start_patches(patchers)

        result = self.server.fill_workday_profile_overrides(page, {"resume_text": "redacted"})

        self.assertEqual(calls, {"education": 1, "experience": 1})
        self.assertEqual([item["field"] for item in result], ["legacy experience", "legacy education"])

    def test_feature_flag_on_adapter_failure_blocks_legacy_fallback(self):
        page = self.fake_workday_page()
        adapter_result = [
            {
                "field": "Education",
                "source": "workday_adapter.education",
                "adapter_section": "education",
                "status": STATUS_HUMAN_REQUIRED,
                "risk": "medium",
                "reason": "max_attempts",
            },
            {
                "field": "Work Experience",
                "source": "workday_adapter.experience",
                "adapter_section": "experience",
                "status": STATUS_FAILED,
                "risk": "medium",
                "reason": "field_not_found",
            },
        ]
        patchers = self.neutral_profile_patches() + [
            patch.dict(os.environ, {"WORKDAY_ADAPTER_V2": "1"}),
            patch.object(self.server, "fill_workday_adapter_sections", return_value=(adapter_result, {"education", "experience"})),
            patch.object(self.server, "fill_workday_education_from_resume", side_effect=AssertionError("legacy education fallback forbidden")),
            patch.object(self.server, "fill_workday_experience_from_resume", side_effect=AssertionError("legacy experience fallback forbidden")),
            patch.object(self.server, "force_workday_country_united_states", return_value=False),
        ]
        self.start_patches(patchers)

        result = self.server.fill_workday_profile_overrides(page, {"resume_text": "redacted"})

        self.assertEqual(result, adapter_result)
        self.assertTrue(all(item["status"] != STATUS_OK for item in result))

    def test_adapter_non_ok_status_enters_missing_required_to_stop_continue(self):
        page = self.fake_workday_page()
        req = SimpleNamespace(allow_placeholder_autofill=False, allow_low_risk_autofill=True)
        for status in ["unsupported", STATUS_FAILED, STATUS_HUMAN_REQUIRED, None]:
            with self.subTest(status=status):
                override = {
                    "field": "Education",
                    "source": "workday_adapter.education",
                    "status": status,
                    "risk": "medium",
                    "reason": "max_attempts",
                }
                patchers = [
                    patch.object(self.server, "fill_workday_profile_overrides", return_value=[override]),
                    patch.object(self.server, "is_workday_my_information_page", return_value=False),
                    patch.object(self.server, "is_workday_my_experience_page", return_value=False),
                    patch.object(self.server, "debug_workday_country_state_controls", return_value=[]),
                ]
                self.start_patches(patchers)

                result = self.server.fill_discovery_page_fields(page, [], {}, req)

                self.assertEqual(result["filled"], [override])
                self.assertEqual(len(result["missing_required"]), 1)
                self.assertEqual(result["missing_required"][0]["reason"], f"workday_adapter_{status}")
                self.assertEqual(result["missing_required"][0]["source"], "workday_adapter.education")

    def test_v2_country_missing_control_does_not_call_legacy_country(self):
        page = self.fake_workday_page()

        class MissingCountryHandler:
            def ensure_united_states(self, _page, _state):
                return SimpleNamespace(ok=False, reason="country_control_not_found", status="unsupported", attempts=0)

        patchers = self.neutral_profile_patches() + [
            patch.dict(os.environ, {"WORKDAY_ADAPTER_V2": "1"}),
            patch.object(self.server, "is_workday_my_information_page", return_value=True),
            patch.object(self.server, "is_workday_my_experience_page", return_value=False),
            patch.object(self.server, "workday_country_is_united_states", return_value=False),
            patch.object(self.server, "CountrySelectorHandler", return_value=MissingCountryHandler()),
            patch.object(self.server, "force_workday_country_united_states", side_effect=AssertionError("legacy country fallback forbidden")),
        ]
        self.start_patches(patchers)

        result = self.server.fill_workday_profile_overrides(page, {"resume_text": "redacted"})

        country = [item for item in result if item.get("source") == "workday_adapter.country"]
        self.assertEqual(len(country), 1)
        self.assertEqual(country[0]["status"], STATUS_HUMAN_REQUIRED)
        self.assertEqual(country[0]["reason"], "country_control_not_found")

    def test_same_page_two_applications_get_fresh_execution_state(self):
        page = self.fake_workday_page()
        state_ids = []
        inherited_locks = []

        class RecordingEducationHandler:
            def fill(self, _page, _data, state):
                state_ids.append(id(state))
                inherited_locks.append(dict(state.field_locks))
                state.mark_valid("education.school")
                return SimpleNamespace(ok=True, status=STATUS_OK, reason="", attempts=0)

        patchers = [
            patch.dict(os.environ, {"WORKDAY_ADAPTER_V2": "1"}),
            patch.object(self.server, "is_workday_my_information_page", return_value=False),
            patch.object(self.server, "is_workday_my_experience_page", return_value=True),
            patch.object(self.server, "parse_resume_education", return_value={"school": "Unit School"}),
            patch.object(self.server, "parse_resume_work_experiences", return_value=[]),
            patch.object(self.server, "EducationRepeatableSectionHandler", return_value=RecordingEducationHandler()),
        ]
        self.start_patches(patchers)

        self.server.fill_workday_adapter_sections(page, {"resume_text": "first"})
        self.server.fill_workday_adapter_sections(page, {"resume_text": "second"})

        self.assertEqual(len(state_ids), 2)
        self.assertNotEqual(state_ids[0], state_ids[1])
        self.assertEqual(inherited_locks, [{}, {}])

    def test_unknown_adapter_status_blocks_continue(self):
        page = self.fake_workday_page()
        req = SimpleNamespace(allow_placeholder_autofill=False, allow_low_risk_autofill=True)
        override = {
            "field": "Education",
            "source": "workday_adapter.education",
            "status": "unexpected_status",
            "risk": "medium",
            "reason": "unit",
        }
        patchers = [
            patch.object(self.server, "fill_workday_profile_overrides", return_value=[override]),
            patch.object(self.server, "is_workday_my_information_page", return_value=False),
            patch.object(self.server, "is_workday_my_experience_page", return_value=False),
            patch.object(self.server, "debug_workday_country_state_controls", return_value=[]),
        ]
        self.start_patches(patchers)

        result = self.server.fill_discovery_page_fields(page, [], {}, req)

        self.assertEqual(len(result["missing_required"]), 1)
        self.assertEqual(result["missing_required"][0]["reason"], "workday_adapter_unexpected_status")


if __name__ == "__main__":
    unittest.main()
