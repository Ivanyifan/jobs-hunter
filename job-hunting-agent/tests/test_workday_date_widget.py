from __future__ import annotations

from pathlib import Path
import sys
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.browser import launch_replay_browser
from adapters.workday.contracts import FieldStatus
from adapters.workday.widgets.date import WorkdayDateGroupWidget
from tests.workday_fixture_loader import field_by_key, load_workday_fixtures


class WorkdayDateWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = launch_replay_browser(cls.playwright, headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.context = self.browser.new_context(viewport={"width": 900, "height": 700})
        self.page = self.context.new_page()

    def tearDown(self) -> None:
        self.context.close()

    def set_content(self, body: str) -> None:
        self.page.set_content(f"<!doctype html><html><body>{body}</body></html>", wait_until="domcontentloaded")

    def test_month_day_year_fills_and_verifies(self):
        self.set_content(
            """
            <fieldset id="date">
              <input aria-label="Month" placeholder="MM">
              <input aria-label="Day" placeholder="DD">
              <input aria-label="Year" placeholder="YYYY">
            </fieldset>
            """
        )

        result = WorkdayDateGroupWidget().fill_parts(
            self.page,
            "12/15/2026",
            {"selector": "#date", "canonical_key": "start_date", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertTrue(
            WorkdayDateGroupWidget().verify_date(
                self.page,
                "12/15/2026",
                {"selector": "#date", "canonical_key": "start_date", "required": True},
            ).verified
        )

    def test_date_section_month_id_is_not_treated_as_single_input(self):
        self.set_content(
            """
            <fieldset id="date">
              <input id="available-dateSectionMonth-input" aria-label="Month" placeholder="MM">
              <input id="available-dateSectionDay-input" aria-label="Day" placeholder="DD">
              <input id="available-dateSectionYear-input" aria-label="Year" placeholder="YYYY">
            </fieldset>
            """
        )

        result = WorkdayDateGroupWidget().fill_parts(
            self.page,
            "12/15/2026",
            {"selector": "#date", "canonical_key": "available_start_date", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(self.page.locator("#available-dateSectionMonth-input").input_value(), "12")
        self.assertEqual(self.page.locator("#available-dateSectionDay-input").input_value(), "15")
        self.assertEqual(self.page.locator("#available-dateSectionYear-input").input_value(), "2026")

    def test_single_month_year_input_fills_and_verifies(self):
        self.set_content('<input id="month-year" placeholder="MM/YYYY">')

        result = WorkdayDateGroupWidget().fill_parts(
            self.page,
            "12/2026",
            {"selector": "#month-year", "canonical_key": "education.start_month", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(self.page.locator("#month-year").input_value(), "12/2026")

    def test_full_date_requires_month_day_and_year_controls(self):
        self.set_content(
            """
            <fieldset id="date">
              <input aria-label="Month" placeholder="MM" value="12">
              <input aria-label="Year" placeholder="YYYY" value="2026">
            </fieldset>
            """
        )

        result = WorkdayDateGroupWidget().verify_date(
            self.page,
            "12/15/2026",
            {"selector": "#date", "canonical_key": "start_date", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "missing_date_component:day")

    def test_month_year_requires_month_and_year_controls(self):
        self.set_content(
            """
            <fieldset id="date">
              <input aria-label="Year" placeholder="YYYY" value="2026">
            </fieldset>
            """
        )

        result = WorkdayDateGroupWidget().verify_date(
            self.page,
            "12/2026",
            {"selector": "#date", "canonical_key": "education.start_month", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "missing_date_component:month")

    def test_year_requires_year_control(self):
        self.set_content(
            """
            <fieldset id="date">
              <input aria-label="Month" placeholder="MM" value="12">
            </fieldset>
            """
        )

        result = WorkdayDateGroupWidget().verify_date(
            self.page,
            "2026",
            {"selector": "#date", "canonical_key": "education.end_year", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "missing_date_component:year")

    def test_yyyy_placeholder_remains_incomplete(self):
        self.set_content('<fieldset id="date"><input aria-label="Year" placeholder="YYYY" value="YYYY"></fieldset>')

        state = WorkdayDateGroupWidget().observe(
            self.page,
            {"selector": "#date", "canonical_key": "education.end_year", "required": True},
        )

        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)

    def test_vague_notice_period_text_is_rejected_as_date(self):
        self.set_content('<fieldset id="date"><input aria-label="Month"><input aria-label="Year"></fieldset>')

        result = WorkdayDateGroupWidget().fill_parts(
            self.page,
            "two weeks after offer",
            {"selector": "#date", "canonical_key": "start_date", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertIn("explicit", result.reason)

    def test_conversation_one_fixture_year_observation(self):
        fixtures = {fixture["fixture_name"]: fixture for fixture in load_workday_fixtures()}
        end_year = field_by_key(fixtures["northrop_education_group"], "education.end_year")
        self.set_content(
            f"""
            <fieldset id="end-year">
              <input aria-label="Year" placeholder="YYYY" value="{end_year["visible_value"]}">
            </fieldset>
            """
        )

        state = WorkdayDateGroupWidget().observe(
            self.page,
            {"selector": "#end-year", "canonical_key": "education.end_year", "required": True},
        )

        self.assertEqual(state.visible_value["year"], "YYYY")
        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)


if __name__ == "__main__":
    unittest.main()
