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
from adapters.workday.controllers import MyInformationController


class WorkdayMyInformationControllerTests(unittest.TestCase):
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

    def test_phone_extension_optional_does_not_block_state_street_style_phone_group(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <section>
                <label for="device">Phone Device Type*</label>
                <select id="device" required>
                  <option value="">Select One</option>
                  <option selected>Mobile</option>
                </select>
                <label for="code">Country Phone Code*</label>
                <input id="code" required value="United States of America (+1)">
                <label for="phone">Phone Number*</label>
                <input id="phone" required value="2172500626">
                <label for="extension">Phone Extension</label>
                <input id="extension" value="">
              </section>
            </main>
            """
        )

        result = MyInformationController().run_pass(self.page, {"trusted_profile": {"phone": "2172500626"}})
        serialized = result.to_dict()

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)
        self.assertFalse(serialized["unresolved_required_fields"])
        extension = [field for field in serialized["fields"] if field["canonical_key"] == "phone_extension"][0]
        self.assertFalse(extension["required"])
        self.assertEqual(extension["status"], "optional")

    def test_how_did_you_hear_prompt_is_planned_and_filled(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <section>
                <label id="hear-label" for="hear">How Did You Hear About Us?*</label>
                <button id="hear" aria-haspopup="listbox" aria-labelledby="hear-label"
                  onclick="hearOptions.hidden=false">0 items selected</button>
                <ul id="hearOptions" role="listbox" hidden>
                  <li role="option" onclick="hear.textContent='Company Website'; hearOptions.hidden=true">Company Website</li>
                  <li role="option" onclick="hear.textContent='LinkedIn'; hearOptions.hidden=true">LinkedIn</li>
                </ul>
              </section>
              <script>
                const hear = document.getElementById("hear");
                const hearOptions = document.getElementById("hearOptions");
              </script>
            </main>
            """
        )
        controller = MyInformationController()
        snapshot = controller.observe(self.page, {})

        actions = controller.plan(snapshot, {})
        result = controller.run_pass(self.page, {})

        self.assertEqual(actions[0].action, "select_how_heard")
        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)
        self.assertEqual(self.page.locator("#hear").inner_text(), "LinkedIn")

    def test_previously_worked_radio_answers_no_from_trusted_profile(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <fieldset>
                <legend>Have you previously worked here?*</legend>
                <label><input id="prev-yes" name="previousWorker" type="radio" required value="Yes"> Yes</label>
                <label><input id="prev-no" name="previousWorker" type="radio" required value="No"> No</label>
              </fieldset>
            </main>
            """
        )
        context = {"trusted_profile": {"previous_worker": "No"}}
        controller = MyInformationController()

        actions = controller.plan(controller.observe(self.page, context), context)
        result = controller.run_pass(self.page, context)

        self.assertEqual(actions[0].action, "select_previous_worker_answer")
        self.assertEqual(actions[0].value, "No")
        self.assertTrue(self.page.locator("#prev-no").is_checked())
        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)

    def test_country_selects_only_united_states_alias_never_anguilla(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <section>
                <label id="country-label" for="country">Country/Region*</label>
                <button id="country" aria-haspopup="listbox" aria-labelledby="country-label"
                  onclick="countryOptions.hidden=false">Select One</button>
                <ul id="countryOptions" role="listbox" hidden>
                  <li role="option" onclick="country.textContent='Anguilla'; countryOptions.hidden=true">Anguilla</li>
                  <li role="option" onclick="country.textContent='United States of America'; countryOptions.hidden=true">United States of America</li>
                </ul>
              </section>
              <script>
                const country = document.getElementById("country");
                const countryOptions = document.getElementById("countryOptions");
              </script>
            </main>
            """
        )

        result = MyInformationController().run_pass(self.page, {})

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)
        self.assertEqual(self.page.locator("#country").inner_text(), "United States of America")
        self.assertNotEqual(self.page.locator("#country").inner_text(), "Anguilla")

    def test_alerts_found_capitalization_warning_does_not_block(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <label for="first">First Name/Given Name*</label>
              <input id="first" value="Yifan" required>
              <div role="alert">
                <h4>Alerts Found</h4>
                <p>First Name/Given Name uses capitalization that may need review.</p>
              </div>
            </main>
            """
        )

        result = MyInformationController().run_pass(self.page, {})
        serialized = result.to_dict()

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)
        self.assertTrue(serialized["alerts"])
        self.assertFalse(serialized["validation_errors"])

    def test_errors_found_required_field_becomes_unresolved_required_field(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <section>
                <label id="hear-label" for="hear">How Did You Hear About Us?*</label>
                <button id="hear" aria-haspopup="listbox" aria-labelledby="hear-label">0 items selected</button>
              </section>
              <div role="alert">
                <h4>Errors Found</h4>
                <p>The field How Did You Hear About Us? is required and must have a value.</p>
              </div>
            </main>
            """
        )

        result = MyInformationController().run_pass(self.page, {})
        unresolved = {item["canonical_key"] for item in result.to_dict()["unresolved_required_fields"]}

        self.assertEqual(result.normalized_outcome(), OutcomeType.MY_INFORMATION_BLOCKED.value)
        self.assertIn("how_heard", unresolved)
        self.assertTrue(result.validation_errors)

    def test_loading_skeleton_returns_retry_then_loading_stuck_not_field_loop(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <section>
                <label id="country-label" for="country">Country*</label>
                <button id="country" aria-haspopup="listbox" aria-labelledby="country-label" aria-busy="true">Loading</button>
              </section>
            </main>
            """
        )
        context: dict = {}
        controller = MyInformationController()

        first = controller.run_pass(self.page, context)
        second = controller.run_pass(self.page, context)

        self.assertEqual(first.normalized_outcome(), OutcomeType.RETRYABLE.value)
        self.assertEqual(second.normalized_outcome(), OutcomeType.LOADING_STUCK.value)

    def test_run_pass_uses_task_one_protocol_without_type_error(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <fieldset>
                <legend>Have you been previously employed with us?*</legend>
                <label><input id="employed-yes" name="previouslyEmployed" type="radio" required value="Yes"> Yes</label>
                <label><input id="employed-no" name="previouslyEmployed" type="radio" required value="No"> No</label>
              </fieldset>
            </main>
            """
        )

        result = MyInformationController().run_pass(
            self.page,
            {"trusted_profile": {"previously_employed": "No"}},
        )

        self.assertTrue(self.page.locator("#employed-no").is_checked())
        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)


if __name__ == "__main__":
    unittest.main()
