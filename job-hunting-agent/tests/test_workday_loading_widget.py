from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.browser import launch_replay_browser
from adapters.workday.contracts import FieldStatus
from adapters.workday.widgets.loading import LoadingStateDetector


class WorkdayLoadingWidgetTests(unittest.TestCase):
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

    def test_loading_resolves_within_timeout(self):
        self.set_content(
            """
            <div id="field" aria-busy="true">Loading</div>
            <script>
              setTimeout(() => {
                const field = document.querySelector("#field");
                field.removeAttribute("aria-busy");
                field.textContent = "Ready";
              }, 100);
            </script>
            """
        )

        result = LoadingStateDetector(self.page.locator("#field")).wait_until_resolved(1000)

        self.assertTrue(result.verified, result.to_dict())

    def test_persistent_loading_returns_typed_loading_result(self):
        self.set_content('<div id="field" aria-busy="true">Fetching options</div>')

        state = LoadingStateDetector(self.page.locator("#field")).detect()
        result = LoadingStateDetector(self.page.locator("#field")).wait_until_resolved(150)

        self.assertEqual(state.normalized_status(), FieldStatus.LOADING.value)
        self.assertFalse(result.verified)
        self.assertTrue(result.retryable)
        self.assertEqual(result.metadata["status"], FieldStatus.LOADING.value)

    def test_disabled_alone_is_not_loading(self):
        self.set_content('<button id="field" disabled aria-disabled="true">Continue</button>')

        state = LoadingStateDetector(self.page.locator("#field")).detect()

        self.assertEqual(state.normalized_status(), FieldStatus.UNKNOWN.value)
        self.assertTrue(state.metadata["disabled"])
        self.assertTrue(state.metadata["aria_disabled"])

    def test_disabled_with_loading_evidence_is_loading(self):
        cases = [
            '<button id="field" disabled aria-busy="true">Continue</button>',
            '<button id="field" disabled><span class="spinner">Loading</span></button>',
        ]
        for body in cases:
            with self.subTest(body=body):
                self.set_content(body)

                state = LoadingStateDetector(self.page.locator("#field")).detect()

                self.assertEqual(state.normalized_status(), FieldStatus.LOADING.value)
                self.assertTrue(state.metadata["disabled"])

    def test_no_infinite_waits(self):
        self.set_content('<div id="field" aria-busy="true">Please wait</div>')
        started = time.perf_counter()

        LoadingStateDetector(self.page.locator("#field")).wait_until_resolved(200)

        self.assertLess(time.perf_counter() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
