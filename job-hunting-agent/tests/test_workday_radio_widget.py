from __future__ import annotations

from pathlib import Path
import sys
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.browser import launch_replay_browser
from adapters.workday.widgets.radio import WorkdayRadioGroupWidget


class WorkdayRadioWidgetTests(unittest.TestCase):
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

    def test_select_no_in_nested_fieldset_and_verify_checked_state(self):
        self.set_content(
            """
            <fieldset id="authorized">
              <legend>Are you legally authorized?</legend>
              <div><label><span>Yes</span><input type="radio" name="auth" value="Yes"></label></div>
              <div><label><span>No</span><input type="radio" name="auth" value="No"></label></div>
            </fieldset>
            """
        )

        result = WorkdayRadioGroupWidget().select_value(
            self.page,
            "No",
            context={"selector": "#authorized", "canonical_key": "authorized", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(
            WorkdayRadioGroupWidget().read_selected_value(self.page, {"selector": "#authorized"}),
            "No",
        )

    def test_css_hidden_native_radio_uses_visible_label_and_checked_state(self):
        self.set_content(
            """
            <fieldset id="authorized">
              <legend>Are you legally authorized?</legend>
              <label class="choice"><input type="radio" name="auth" value="Yes" style="display:none"> Yes</label>
              <label class="choice"><input type="radio" name="auth" value="No" style="display:none"> No</label>
            </fieldset>
            """
        )

        result = WorkdayRadioGroupWidget().select_value(
            self.page,
            "No",
            context={"selector": "#authorized", "canonical_key": "authorized", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertTrue(self.page.locator("input[value='No']").is_checked())

    def test_click_that_does_not_change_checked_state_is_failure(self):
        self.set_content(
            """
            <fieldset id="group">
              <legend>Question</legend>
              <div role="radio" aria-checked="false">Yes</div>
              <div role="radio" aria-checked="false">No</div>
            </fieldset>
            """
        )

        result = WorkdayRadioGroupWidget().select_value(
            self.page,
            "No",
            context={"selector": "#group", "canonical_key": "question", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "checked_state_not_changed")

    def test_multiple_nearby_radio_groups_do_not_cross_select(self):
        self.set_content(
            """
            <fieldset id="first">
              <legend>First</legend>
              <label><input type="radio" name="first" value="Yes">Yes</label>
              <label><input type="radio" name="first" value="No">No</label>
            </fieldset>
            <fieldset id="second">
              <legend>Second</legend>
              <label><input type="radio" name="second" value="Yes">Yes</label>
              <label><input type="radio" name="second" value="No">No</label>
            </fieldset>
            """
        )

        result = WorkdayRadioGroupWidget().select_value(
            self.page,
            "No",
            context={"selector": "#second", "canonical_key": "second", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertFalse(self.page.locator("#first input[value='No']").is_checked())
        self.assertTrue(self.page.locator("#second input[value='No']").is_checked())


if __name__ == "__main__":
    unittest.main()
