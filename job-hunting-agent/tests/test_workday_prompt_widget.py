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
