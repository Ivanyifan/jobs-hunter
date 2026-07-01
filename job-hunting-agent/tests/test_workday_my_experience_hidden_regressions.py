from __future__ import annotations

from pathlib import Path
import sys
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.browser import launch_replay_browser
from adapters.workday.contracts import OutcomeType
from adapters.workday.controllers import MyExperienceController
from adapters.workday.widgets.date import WorkdayDateGroupWidget


class WorkdayMyExperienceHiddenRegressionTests(unittest.TestCase):
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

    def unresolved_keys(self, result) -> set[str]:
        return {item["canonical_key"] for item in result.to_dict()["unresolved_required_fields"]}

    def test_duplicate_required_experience_controls_do_not_hide_missing_row(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="experience" role="group" aria-label="Work Experience">
                <label for="company1">Company*</label><input id="company1" required value="Filled Company">
                <label for="title1">Job Title*</label><input id="title1" required value="Engineer">
              </section>
              <section data-section="experience" role="group" aria-label="Work Experience">
                <label for="company2">Company*</label><input id="company2" required>
                <label for="title2">Job Title*</label><input id="title2" required value="Engineer">
              </section>
            </main>
            """
        )

        result = MyExperienceController().run_pass(self.page, {})

        self.assertEqual(result.normalized_outcome(), OutcomeType.MY_EXPERIENCE_BLOCKED.value)
        self.assertIn("experience.company", self.unresolved_keys(result))
        self.assertIn("work_experience", result.to_dict()["unresolved_required_groups"])

    def test_month_only_field_accepts_month_name_from_trusted_profile(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="education" role="group" aria-label="Education">
                <label for="month">End Month*</label><input id="month" required placeholder="Month">
              </section>
            </main>
            """
        )

        result = MyExperienceController().run_pass(
            self.page,
            {"trusted_profile": {"education": {"graduation_date": "May 2026"}}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.locator("#month").input_value(), "05")

    def test_date_widget_month_name_and_month_only_values_verify(self):
        self.set_content('<fieldset id="date"><input aria-label="Month" placeholder="Month"></fieldset>')

        result = WorkdayDateGroupWidget().fill_parts(
            self.page,
            "May",
            {"selector": "#date", "canonical_key": "education.end_month", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(self.page.locator("input").input_value(), "05")


if __name__ == "__main__":
    unittest.main()
