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
from adapters.workday.widgets.text import WorkdayTextInputWidget


class WorkdayTextWidgetTests(unittest.TestCase):
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

    def test_native_react_value_is_re_read_after_fill(self):
        self.set_content(
            """
            <input id="name">
            <div id="committed"></div>
            <script>
              const input = document.querySelector("#name");
              input.addEventListener("input", event => {
                document.querySelector("#committed").textContent = event.target.value;
              });
            </script>
            """
        )

        result = WorkdayTextInputWidget().act(
            self.page,
            "Ada Lovelace",
            {"selector": "#name", "canonical_key": "name", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(self.page.locator("#name").input_value(), "Ada Lovelace")
        self.assertEqual(self.page.locator("#committed").inner_text(), "Ada Lovelace")

    def test_optional_empty_input_can_remain_empty_without_being_required_complete(self):
        self.set_content('<input id="extension" value="">')

        widget = WorkdayTextInputWidget(mode=WorkdayTextInputWidget.MODE_OPTIONAL_EMPTY)
        result = widget.act(
            self.page,
            "",
            {"selector": "#extension", "canonical_key": "phone_extension", "required": False},
        )
        state = widget.observe(
            self.page,
            {"selector": "#extension", "canonical_key": "phone_extension", "required": False},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(state.normalized_status(), FieldStatus.OPTIONAL.value)


if __name__ == "__main__":
    unittest.main()
