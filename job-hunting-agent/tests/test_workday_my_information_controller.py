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
              <section>
                <label id="country-label" for="country">Country*</label>
                <button id="country" aria-haspopup="listbox" aria-labelledby="country-label">United States of America</button>
              </section>
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

    def test_unknown_required_address_line_is_preserved_as_unresolved_blocker(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <section>
                <label for="address1">Address Line 1*</label>
                <input id="address1" required value="">
              </section>
            </main>
            """
        )

        result = MyInformationController().run_pass(self.page, {})
        serialized = result.to_dict()
        [unresolved] = serialized["unresolved_required_fields"]

        self.assertEqual(result.normalized_outcome(), OutcomeType.MY_INFORMATION_BLOCKED.value)
        self.assertTrue(unresolved["canonical_key"].startswith("unknown_required::Address Line 1"))
        self.assertIn(unresolved["canonical_key"], serialized["snapshot"]["required_fields"])
        self.assertEqual(unresolved["label"], "Address Line 1")
        self.assertTrue(unresolved["selector"])
        self.assertEqual(unresolved["kind"], "text")
        self.assertEqual(unresolved["options"], [])

    def test_country_phone_code_canada_plus_one_is_not_accepted_as_us(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <section>
                <label id="code-label" for="code">Country Phone Code*</label>
                <button id="code" aria-haspopup="listbox" aria-labelledby="code-label"
                  aria-controls="codeOptions" onclick="codeOptions.hidden=false">Canada (+1)</button>
                <ul id="codeOptions" role="listbox" hidden>
                  <li role="option" onclick="code.textContent='Canada (+1)'; codeOptions.hidden=true">Canada (+1)</li>
                  <li role="option" onclick="code.textContent='United States of America (+1)'; codeOptions.hidden=true">United States of America (+1)</li>
                  <li role="option" onclick="code.textContent='United States (+1)'; codeOptions.hidden=true">United States (+1)</li>
                </ul>
              </section>
              <script>
                const code = document.getElementById("code");
                const codeOptions = document.getElementById("codeOptions");
              </script>
            </main>
            """
        )
        controller = MyInformationController()

        actions = controller.plan(controller.observe(self.page, {}), {})
        result = controller.run_pass(self.page, {})

        self.assertEqual(actions[0].action, "select_country_phone_code")
        self.assertIn(
            self.page.locator("#code").inner_text(),
            {"United States of America (+1)", "United States (+1)"},
        )
        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)

    def test_unrelated_loading_spinner_does_not_block_complete_my_information(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <section>
                <label id="country-label" for="country">Country*</label>
                <button id="country" aria-haspopup="listbox" aria-labelledby="country-label">United States of America</button>
              </section>
              <section>
                <label for="phone">Phone Number*</label>
                <input id="phone" required value="2172500626">
              </section>
              <section aria-label="Recommendations">
                <div class="spinner">Loading recommendations</div>
                <ul role="listbox"><li role="option">Loading</li></ul>
              </section>
            </main>
            """
        )

        result = MyInformationController().run_pass(self.page, {"trusted_profile": {"phone": "2172500626"}})

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)
        self.assertFalse(result.snapshot.loading_indicators)

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

    def test_hp_names_previous_employment_and_selected_phone_code_are_known(self):
        self.set_content(
            """
            <main>
              <h1>My Information</h1>
              <fieldset>
                <legend>Have you previously been employed by HP? If you are currently employed by HP, please use the internal Job Searcher site to apply for this role.*</legend>
                <label><input name="previouslyEmployed" type="radio" required value="Yes"> Yes</label>
                <label><input id="employed-no" name="previouslyEmployed" type="radio" required value="No" checked> No</label>
              </fieldset>
              <section role="group" aria-label="Name">
                <label for="firstName">First Name*</label><input id="firstName" required value="Yifan">
                <label for="lastName">Last Name*</label><input id="lastName" required value="Li">
              </section>
              <section role="group" aria-label="Phone">
                <label for="phoneCode">Country/Region Phone Code*</label>
                <input id="phoneCode" required data-automation-id="searchBox" value="">
                <span data-automation-id="selectedItem">United States of America (+1)</span>
              </section>
            </main>
            """
        )

        result = MyInformationController().run_pass(
            self.page,
            {
                "user_data": {
                    "first_name": "Yifan",
                    "last_name": "Li",
                    "common_answers": {"current_or_previous_company_employee": "No"},
                }
            },
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        fields = {item["canonical_key"]: item for item in result.to_dict()["fields"]}
        self.assertEqual(fields["previously_employed"]["status"], "filled")
        self.assertEqual(fields["first_name"]["status"], "filled")
        self.assertEqual(fields["last_name"]["status"], "filled")
        self.assertEqual(fields["country_phone_code"]["status"], "filled")


if __name__ == "__main__":
    unittest.main()
