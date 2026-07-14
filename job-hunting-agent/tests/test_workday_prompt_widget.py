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
from adapters.workday.widgets.prompt import WorkdayPromptWidget
from adapters.workday.widgets.utilities import collect_visible_option_candidates
from tests.workday_fixture_loader import field_by_key, load_workday_fixtures


class WorkdayPromptWidgetTests(unittest.TestCase):
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

    def test_select_one_is_not_complete(self):
        self.set_content('<button id="degree" aria-haspopup="listbox">Select One</button>')

        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)

    def test_zero_items_selected_is_not_complete(self):
        self.set_content('<button id="school" aria-haspopup="listbox">0 items selected</button>')

        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#school", "canonical_key": "education.school", "required": True},
        )

        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)

    def test_typing_school_text_without_selecting_option_is_not_complete(self):
        self.set_content(
            """
            <label>School <input id="school" role="combobox" aria-autocomplete="list"></label>
            <script>
              document.querySelector("#school").addEventListener("input", event => {
                window.typed = event.target.value;
              });
            </script>
            """
        )
        self.page.locator("#school").fill("University of Example")

        result = WorkdayPromptWidget().verify_committed_value(
            self.page,
            "University of Example",
            context={"selector": "#school", "canonical_key": "education.school", "required": True},
        )
        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#school", "canonical_key": "education.school", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)

    def test_typed_school_text_with_unrelated_hidden_input_is_not_complete(self):
        self.set_content(
            """
            <section id="school-field">
              <label>School <input id="school" role="combobox" aria-autocomplete="list"></label>
              <input type="hidden" name="csrf_token" value="unrelated">
            </section>
            """
        )
        self.page.locator("#school").fill("University of Example")

        result = WorkdayPromptWidget().verify_committed_value(
            self.page,
            "University of Example",
            context={"selector": "#school", "canonical_key": "education.school", "required": True},
        )
        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#school", "canonical_key": "education.school", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)

    def test_data_field_scoped_csrf_token_is_not_committed_value(self):
        self.set_content(
            """
            <section id="school-field" data-field>
              <label>School <input id="school" role="combobox" aria-autocomplete="list"></label>
              <input type="hidden" name="csrf_token" value="University of Example">
            </section>
            """
        )
        self.page.locator("#school").fill("University of Example")

        result = WorkdayPromptWidget().verify_committed_value(
            self.page,
            "University of Example",
            context={"selector": "#school", "canonical_key": "education.school", "required": True},
        )
        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#school", "canonical_key": "education.school", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)

    def test_prompt_search_data_automation_id_uses_field_scope_for_selected_token(self):
        self.set_content(
            """
            <section id="school-field" data-field>
              <input id="school" data-automation-id="promptSearch" role="combobox">
              <span data-automation-id="selectedItem">University of Example</span>
            </section>
            """
        )

        result = WorkdayPromptWidget().verify_committed_value(
            self.page,
            "University of Example",
            context={"selector": "#school", "canonical_key": "education.school", "required": True},
        )
        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#school", "canonical_key": "education.school", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(state.normalized_status(), FieldStatus.FILLED.value)

    def test_verified_prompt_does_not_report_placeholder_reason(self):
        self.set_content(
            """
            <section id="degree-field" data-field>
              <button id="degree" aria-haspopup="listbox">Select One</button>
              <span data-automation-id="selectedItem">Bachelor's Degree</span>
            </section>
            """
        )

        result = WorkdayPromptWidget().verify_committed_value(
            self.page,
            "Bachelor's Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(result.reason, "verified")

    def test_exact_option_selection_produces_committed_token(self):
        self.set_content(
            """
            <div id="field">
              <button id="degree" aria-haspopup="listbox">Select One</button>
              <span id="token" data-automation-id="selectedItem" hidden></span>
            </div>
            <div id="portal" role="listbox" hidden>
              <div role="option">Bachelor's Degree</div>
              <div role="option">Associate Degree</div>
            </div>
            <script>
              const button = document.querySelector("#degree");
              const portal = document.querySelector("#portal");
              button.addEventListener("click", () => portal.hidden = false);
              for (const option of document.querySelectorAll("[role=option]")) {
                option.addEventListener("click", () => {
                  button.textContent = option.textContent;
                  const token = document.querySelector("#token");
                  token.hidden = false;
                  token.textContent = option.textContent;
                  portal.hidden = true;
                });
              }
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Bachelor's Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertIn("Bachelor's Degree", WorkdayPromptWidget().read_committed_values(self.page, {"selector": "#degree"}))

    def test_option_matching_multiple_selectors_is_one_candidate_and_selects(self):
        self.set_content(
            """
            <div id="field">
              <button id="degree" aria-haspopup="listbox">Select One</button>
              <span id="token" data-automation-id="selectedItem" hidden></span>
            </div>
            <div id="portal" role="listbox" hidden>
              <div role="option" data-automation-id="promptOption">Bachelor's Degree</div>
            </div>
            <script>
              const button = document.querySelector("#degree");
              const portal = document.querySelector("#portal");
              button.addEventListener("click", () => portal.hidden = false);
              document.querySelector("[role=option]").addEventListener("click", event => {
                button.textContent = event.target.textContent;
                const token = document.querySelector("#token");
                token.hidden = false;
                token.textContent = event.target.textContent;
                portal.hidden = true;
              });
            </script>
            """
        )
        widget = WorkdayPromptWidget()

        widget.open(self.page, {"selector": "#degree", "canonical_key": "education.degree", "required": True})
        candidates = collect_visible_option_candidates(self.page)
        result = widget.select_exact_or_alias(
            self.page,
            "Bachelor's Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertEqual([candidate.text for candidate in candidates], ["Bachelor's Degree"])
        self.assertTrue(result.verified, result.to_dict())

    def test_preopened_prompt_is_not_opened_twice_before_selection(self):
        self.set_content(
            """
            <section id="field" data-field>
              <button id="source" aria-haspopup="listbox">0 items selected</button>
              <span id="token" data-automation-id="selectedItem" hidden></span>
            </section>
            <div id="portal" role="listbox" hidden>
              <div role="option" data-automation-id="promptOption">Employer Website</div>
            </div>
            <script>
              window.openCount = 0;
              const button = document.querySelector("#source");
              const portal = document.querySelector("#portal");
              button.addEventListener("click", () => {
                window.openCount += 1;
                portal.hidden = false;
              });
              document.querySelector("[role=option]").addEventListener("click", event => {
                button.textContent = event.target.textContent;
                const token = document.querySelector("#token");
                token.hidden = false;
                token.textContent = event.target.textContent;
                portal.hidden = true;
              });
            </script>
            """
        )
        widget = WorkdayPromptWidget()
        context = {"selector": "#source", "canonical_key": "how_heard", "required": True}

        widget.open(self.page, context)
        result = widget.select_exact_or_alias(
            self.page,
            "Employer Website",
            context=context,
            prompt_already_open=True,
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(self.page.evaluate("window.openCount"), 1)

    def test_options_after_first_eighty_are_available_for_exact_selection(self):
        options = "".join(f'<div role="option">Option {index}</div>' for index in range(121))
        self.set_content(
            """
            <section id="field" data-field>
              <button id="country" aria-haspopup="listbox">Select One</button>
              <span id="token" data-automation-id="selectedItem" hidden></span>
            </section>
            <div id="portal" role="listbox" hidden>
            """
            + options
            + """
            </div>
            <script>
              const button = document.querySelector("#country");
              const portal = document.querySelector("#portal");
              button.addEventListener("click", () => portal.hidden = false);
              for (const option of document.querySelectorAll("[role=option]")) {
                option.addEventListener("click", event => {
                  button.textContent = event.target.textContent;
                  const token = document.querySelector("#token");
                  token.hidden = false;
                  token.textContent = event.target.textContent;
                  portal.hidden = true;
                });
              }
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Option 120",
            context={"selector": "#country", "canonical_key": "country", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(self.page.locator("#country").inner_text(), "Option 120")

    def test_search_prompt_filters_when_initial_options_do_not_match(self):
        self.set_content(
            """
            <section id="field" data-field>
              <input id="phone-code" role="combobox" aria-autocomplete="list" value="">
              <span id="token" data-automation-id="selectedItem">Ireland (+353)</span>
            </section>
            <div id="portal" role="listbox"><div role="option">Ireland (+353)</div></div>
            <script>
              const input = document.querySelector("#phone-code");
              const portal = document.querySelector("#portal");
              input.addEventListener("input", () => {
                portal.innerHTML = '<div role="option">United States of America (+1)</div>';
                portal.querySelector("[role=option]").addEventListener("click", event => {
                  document.querySelector("#token").textContent = event.target.textContent;
                });
              });
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "United States of America (+1)",
            aliases=["United States (+1)"],
            context={"selector": "#phone-code", "canonical_key": "country_phone_code", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(self.page.locator("#token").inner_text(), "United States of America (+1)")

    def test_failed_prompt_selection_dismisses_open_popup(self):
        self.set_content(
            """
            <button id="degree" aria-haspopup="listbox">Select One</button>
            <div id="portal" role="listbox" hidden><div role="option">Other Degree</div></div>
            <script>
              const portal = document.querySelector("#portal");
              document.querySelector("#degree").addEventListener("click", () => portal.hidden = false);
              document.addEventListener("keydown", event => {
                if (event.key === "Escape") portal.hidden = true;
              });
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Target Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertTrue(self.page.locator("#portal").is_hidden())

    def test_active_popper_options_take_priority_over_unrelated_prompt_tokens(self):
        self.set_content(
            """
            <section id="field" data-field>
              <input id="source" role="combobox" value="">
              <span data-automation-id="promptOption">United States of America (+1)</span>
            </section>
            <div data-popper-placement="bottom">
              <div role="option"><div data-automation-id="promptOption">Employer Website</div></div>
            </div>
            """
        )

        options = WorkdayPromptWidget().read_visible_options(
            self.page,
            {"selector": "#source", "canonical_key": "how_heard", "required": True},
            "Company Website",
        )

        self.assertEqual(options, ["Employer Website"])

    def test_hierarchical_prompt_requires_same_exact_option_on_second_level(self):
        self.set_content(
            """
            <section id="field" data-field>
              <button id="source" aria-haspopup="listbox">0 items selected</button>
              <span id="token" data-automation-id="selectedItem" hidden></span>
            </section>
            <div id="level-one" data-popper-placement="bottom" hidden>
              <div id="category" role="option" data-automation-id="promptOption">Employer Website</div>
            </div>
            <div id="level-two" data-popper-placement="right" hidden>
              <div id="leaf" role="option" data-automation-id="promptOption">Employer Website</div>
            </div>
            <script>
              const levelOne = document.querySelector("#level-one");
              const levelTwo = document.querySelector("#level-two");
              document.querySelector("#source").addEventListener("click", () => levelOne.hidden = false);
              document.querySelector("#category").addEventListener("click", () => {
                levelOne.hidden = true;
                levelTwo.hidden = false;
              });
              document.querySelector("#leaf").addEventListener("click", event => {
                const token = document.querySelector("#token");
                token.hidden = false;
                token.textContent = event.target.textContent;
                levelTwo.hidden = true;
              });
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Employer Website",
            context={"selector": "#source", "canonical_key": "how_heard", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertTrue(result.metadata["drill_down"])

    def test_committed_verification_does_not_accept_unconfigured_containment(self):
        self.set_content(
            """
            <section id="phone-field">
              <button id="device" aria-haspopup="listbox">Business Mobile</button>
              <span data-automation-id="selectedItem">Business Mobile</span>
            </section>
            """
        )

        result = WorkdayPromptWidget().verify_committed_value(
            self.page,
            "Mobile",
            context={"selector": "#device", "canonical_key": "phone_device_type", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "committed_value_mismatch")

    def test_aria_controls_popup_is_scoped_before_page_wide_options(self):
        self.set_content(
            """
            <section id="field">
              <button id="degree" aria-haspopup="listbox" aria-controls="owned-list">Select One</button>
              <span id="token" data-automation-id="selectedItem" hidden></span>
            </section>
            <div id="owned-list" role="listbox" hidden>
              <div id="owned-option" role="option">Target Degree</div>
            </div>
            <div id="other-list" role="listbox">
              <div id="other-option" role="option">Target Degree</div>
            </div>
            <script>
              window.clickedOther = false;
              const button = document.querySelector("#degree");
              button.addEventListener("click", () => document.querySelector("#owned-list").hidden = false);
              document.querySelector("#owned-option").addEventListener("click", event => {
                button.textContent = event.target.textContent;
                const token = document.querySelector("#token");
                token.hidden = false;
                token.textContent = event.target.textContent;
              });
              document.querySelector("#other-option").addEventListener("click", () => window.clickedOther = true);
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Target Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertFalse(self.page.evaluate("window.clickedOther"))

    def test_field_scoped_popup_is_used_before_page_wide_options(self):
        self.set_content(
            """
            <section id="field" data-field>
              <button id="degree" aria-haspopup="listbox">Select One</button>
              <span id="token" data-automation-id="selectedItem" hidden></span>
              <div id="field-list" role="listbox" hidden>
                <div id="field-option" role="option">Target Degree</div>
              </div>
            </section>
            <div id="other-list" role="listbox">
              <div id="other-option" role="option">Target Degree</div>
            </div>
            <script>
              window.clickedOther = false;
              const button = document.querySelector("#degree");
              button.addEventListener("click", () => document.querySelector("#field-list").hidden = false);
              document.querySelector("#field-option").addEventListener("click", event => {
                button.textContent = event.target.textContent;
                const token = document.querySelector("#token");
                token.hidden = false;
                token.textContent = event.target.textContent;
              });
              document.querySelector("#other-option").addEventListener("click", () => window.clickedOther = true);
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Target Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertFalse(self.page.evaluate("window.clickedOther"))

    def test_page_wide_fallback_does_not_select_ambiguous_options(self):
        self.set_content(
            """
            <section id="field">
              <button id="degree" aria-haspopup="listbox">Select One</button>
            </section>
            <div role="listbox">
              <div role="option">Target Degree</div>
            </div>
            <div role="listbox">
              <div role="option">Target Degree</div>
            </div>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Target Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "ambiguous_exact")

    def test_option_portal_outside_immediate_field_container_is_supported(self):
        self.set_content(
            """
            <section id="field"><input id="school" role="combobox" aria-autocomplete="list"></section>
            <div id="prompt-root">
              <div role="option">University of Illinois Urbana-Champaign</div>
            </div>
            <script>
              document.querySelector("[role=option]").addEventListener("click", () => {
                const field = document.querySelector("#field");
                const token = document.createElement("span");
                token.dataset.automationId = "selectedToken";
                token.textContent = "University of Illinois Urbana-Champaign";
                field.appendChild(token);
                document.querySelector("#school").value = "University of Illinois Urbana-Champaign";
              });
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "University of Illinois Urbana-Champaign",
            context={"selector": "#school", "canonical_key": "education.school", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())

    def test_clicking_option_without_committed_value_change_fails_verification(self):
        self.set_content(
            """
            <button id="degree" aria-haspopup="listbox">Select One</button>
            <div role="listbox"><div role="option">Bachelor's Degree</div></div>
            <script>
              document.querySelector("[role=option]").addEventListener("click", () => {
                window.clickedOnly = true;
              });
            </script>
            """
        )

        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Bachelor's Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "committed_value_not_changed")

    def test_loading_options_return_loading_rather_than_missing(self):
        self.set_content('<button id="degree" aria-haspopup="listbox" aria-busy="true">Loading</button>')

        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )
        result = WorkdayPromptWidget().select_exact_or_alias(
            self.page,
            "Bachelor's Degree",
            context={"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertEqual(state.normalized_status(), FieldStatus.LOADING.value)
        self.assertFalse(result.verified)
        self.assertTrue(result.retryable)
        self.assertEqual(result.metadata["status"], FieldStatus.LOADING.value)

    def test_unrelated_visible_spinner_does_not_mark_prompt_loading(self):
        self.set_content(
            """
            <div class="spinner">Loading dashboard</div>
            <section id="field">
              <button id="degree" aria-haspopup="listbox">Select One</button>
            </section>
            """
        )

        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)

    def test_unrelated_visible_loading_listbox_does_not_mark_prompt_loading(self):
        self.set_content(
            """
            <section id="field" data-field>
              <button id="degree" aria-haspopup="listbox">Select One</button>
            </section>
            <section id="other-field" data-field>
              <div role="listbox"><div role="option">Loading</div></div>
            </section>
            """
        )

        state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#degree", "canonical_key": "education.degree", "required": True},
        )

        self.assertEqual(state.normalized_status(), FieldStatus.MISSING.value)

    def test_conversation_one_fixture_prompt_observations(self):
        fixtures = {fixture["fixture_name"]: fixture for fixture in load_workday_fixtures()}
        state_street_phone = field_by_key(fixtures["statestreet_phone_group"], "phone_device_type")
        northrop_school = field_by_key(fixtures["northrop_education_group"], "education.school")
        northrop_degree = field_by_key(fixtures["northrop_education_group"], "education.degree")
        self.set_content(
            f"""
            <section id="phone-group">
              <button id="phone-device" aria-haspopup="listbox">{state_street_phone["visible_value"]}</button>
            </section>
            <section id="education">
              <input id="school-text" value="Typed University Name">
              <button id="school-prompt" aria-haspopup="listbox">{northrop_school["visible_value"]}</button>
              <button id="degree-prompt" aria-haspopup="listbox">{northrop_degree["visible_value"]}</button>
            </section>
            """
        )

        phone_state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#phone-device", "canonical_key": "phone_device_type", "required": True},
        )
        school_state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#school-prompt", "canonical_key": "education.school", "required": True},
        )
        degree_state = WorkdayPromptWidget().observe(
            self.page,
            {"selector": "#degree-prompt", "canonical_key": "education.degree", "required": True},
        )

        self.assertEqual(phone_state.normalized_status(), FieldStatus.MISSING.value)
        self.assertEqual(school_state.normalized_status(), FieldStatus.MISSING.value)
        self.assertEqual(degree_state.normalized_status(), FieldStatus.MISSING.value)


if __name__ == "__main__":
    unittest.main()
