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
from adapters.workday.controllers import ApplicationQuestionsController


class WorkdayApplicationQuestionsControllerTests(unittest.TestCase):
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

    def unresolved(self, result) -> list[dict]:
        return result.to_dict()["unresolved_required_fields"]

    def unresolved_keys(self, result) -> set[str]:
        return {item["canonical_key"] for item in self.unresolved(result)}

    def test_hp_start_date_composite_uses_trusted_start_date_parts(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Application Questions">
                <p>When are you available to start?*</p>
                <div class="date-row">
                  <div><label for="available-dateSectionMonth-input">Month</label><input id="available-dateSectionMonth-input" required></div>
                  <div><label for="available-dateSectionDay-input">Day</label><input id="available-dateSectionDay-input" required></div>
                  <div><label for="available-dateSectionYear-input">Year</label><input id="available-dateSectionYear-input" required></div>
                </div>
                <div role="alert">The field When are you available to start? is required and must have a value.</div>
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(
            self.page,
            {"trusted_profile": {"earliest_start_date": "12/15/2026"}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.locator("#available-dateSectionMonth-input").input_value(), "12")
        self.assertEqual(self.page.locator("#available-dateSectionDay-input").input_value(), "15")
        self.assertEqual(self.page.locator("#available-dateSectionYear-input").input_value(), "2026")
        self.assertNotEqual(self.page.locator("#available-dateSectionMonth-input").input_value(), "12/15/2026")

    def test_select_label_comes_from_question_context_not_placeholder_button(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Certification">
                <p>Do you agree to the certification statement?*</p>
                <button id="certification" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                <span data-automation-id="selectedItem" hidden></span>
                <div id="certificationOptions" role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">I agree</button>
                  <button type="button" role="option" class="fixture-option">I do not agree</button>
                </div>
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(self.page, {})
        blocker = self.unresolved(result)[0]

        self.assertEqual(result.normalized_outcome(), OutcomeType.BLOCKED_ON_QUESTIONS.value)
        self.assertIn("certification statement", blocker["question_text"].lower())
        self.assertNotEqual(blocker["question_text"], "Select One Required")
        self.assertIn("I agree", blocker["options"])

    def test_work_authorization_uses_trusted_answer_not_first_option(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section id="authorizationField" role="group" aria-label="Work Authorization">
                <p>Are you legally authorized to work in the United States?*</p>
                <button id="authorization" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                <span id="authorizationToken" data-automation-id="selectedItem" hidden></span>
                <div id="authorizationOptions" role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">No</button>
                  <button type="button" role="option" class="fixture-option">Yes</button>
                </div>
              </section>
              <script>
                authorization.addEventListener("click", () => authorizationOptions.hidden = false);
                for (const option of authorizationOptions.querySelectorAll("[role=option]")) {
                  option.addEventListener("click", event => {
                    authorization.textContent = event.target.textContent;
                    authorizationToken.textContent = event.target.textContent;
                    authorizationToken.hidden = false;
                    authorizationOptions.hidden = true;
                  });
                }
              </script>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(
            self.page,
            {"trusted_profile": {"work_authorization": {"us_authorized": True}}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.locator("#authorizationToken").inner_text(), "Yes")

    def test_hp_government_employment_uses_nested_common_answer(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section id="governmentField" role="group" aria-label="Government Employment">
                <p>Within the past 5 years, have you been employed by the federal or any state or local government or public institution within the United States?*</p>
                <button id="government" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                <span id="governmentToken" data-automation-id="selectedItem" hidden></span>
                <div id="governmentOptions" role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
              <script>
                government.addEventListener("click", () => governmentOptions.hidden = false);
                for (const option of governmentOptions.querySelectorAll("[role=option]")) {
                  option.addEventListener("click", event => {
                    government.textContent = event.target.textContent;
                    governmentToken.textContent = event.target.textContent;
                    governmentToken.hidden = false;
                    governmentOptions.hidden = true;
                  });
                }
              </script>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(
            self.page,
            {"user_data": {"common_answers": {"government_employment": "No"}}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.locator("#governmentToken").inner_text(), "No")
        actions = [
            action
            for action in result.to_dict()["actions"]
            if action.get("target") == "compliance.government_employment"
        ]
        self.assertEqual(len(actions), 1)

    def test_hp_existing_employee_uses_nested_common_answer(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section id="employeeField" role="group" aria-label="Existing Employee">
                <p>Are you an existing HP employee?*</p>
                <button id="employee" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                <span id="employeeToken" data-automation-id="selectedItem" hidden></span>
                <div id="employeeOptions" role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
              <script>
                employee.addEventListener("click", () => employeeOptions.hidden = false);
                for (const option of employeeOptions.querySelectorAll("[role=option]")) {
                  option.addEventListener("click", event => {
                    employee.textContent = event.target.textContent;
                    employeeToken.textContent = event.target.textContent;
                    employeeToken.hidden = false;
                    employeeOptions.hidden = true;
                  });
                }
              </script>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(
            self.page,
            {"user_data": {"common_answers": {"current_or_previous_company_employee": "No"}}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.locator("#employeeToken").inner_text(), "No")

    def test_sponsorship_uses_trusted_false_answer(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section id="sponsorshipField" role="group" aria-label="Sponsorship">
                <p>Will you now or in the future require sponsorship for employment visa status?*</p>
                <button id="sponsorship" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                <span id="sponsorshipToken" data-automation-id="selectedItem" hidden></span>
                <div id="sponsorshipOptions" role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
              <script>
                sponsorship.addEventListener("click", () => sponsorshipOptions.hidden = false);
                for (const option of sponsorshipOptions.querySelectorAll("[role=option]")) {
                  option.addEventListener("click", event => {
                    sponsorship.textContent = event.target.textContent;
                    sponsorshipToken.textContent = event.target.textContent;
                    sponsorshipToken.hidden = false;
                    sponsorshipOptions.hidden = true;
                  });
                }
              </script>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(
            self.page,
            {"trusted_profile": {"work_authorization": {"needs_sponsorship": False}}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.locator("#sponsorshipToken").inner_text(), "No")

    def test_sensitive_compliance_without_trusted_answer_blocks_with_options(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Compliance">
                <p>Do you have a conflict of interest or export control restriction?*</p>
                <button id="compliance" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                <div id="complianceOptions" role="listbox">
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(self.page, {})
        blocker = self.unresolved(result)[0]

        self.assertEqual(result.normalized_outcome(), OutcomeType.BLOCKED_ON_QUESTIONS.value)
        self.assertIn(blocker["canonical_key"], {"compliance.conflict_of_interest", "compliance.export_control"})
        self.assertEqual(blocker["risk_type"], "sensitive_compliance")
        self.assertEqual(blocker["reason"], "trusted_answer_required_for_sensitive_compliance")
        self.assertEqual(blocker["options"], ["Yes", "No"])

    def test_sensitive_compliance_current_value_without_trusted_answer_still_blocks(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Compliance">
                <p>Do you have a conflict of interest?*</p>
                <button id="compliance" aria-haspopup="listbox" aria-required="true">Yes</button>
                <span id="complianceToken" data-automation-id="selectedItem">Yes</span>
                <div id="complianceOptions" role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(self.page, {})
        blocker = self.unresolved(result)[0]

        self.assertEqual(result.normalized_outcome(), OutcomeType.BLOCKED_ON_QUESTIONS.value)
        self.assertEqual(blocker["canonical_key"], "compliance.conflict_of_interest")
        self.assertEqual(blocker["reason"], "trusted_answer_required_for_sensitive_compliance")
        self.assertEqual(blocker["current_value"], "Yes")

    def test_sensitive_compliance_mismatched_current_value_is_replaced_from_trusted_answer(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section id="complianceField" role="group" aria-label="Compliance">
                <p>Do you have a conflict of interest?*</p>
                <button id="compliance" aria-haspopup="listbox" aria-required="true">Yes</button>
                <span id="complianceToken" data-automation-id="selectedItem">Yes</span>
                <div id="complianceOptions" role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
              <script>
                compliance.addEventListener("click", () => complianceOptions.hidden = false);
                for (const option of complianceOptions.querySelectorAll("[role=option]")) {
                  option.addEventListener("click", event => {
                    compliance.textContent = event.target.textContent;
                    complianceToken.textContent = event.target.textContent;
                    complianceOptions.hidden = true;
                  });
                }
              </script>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(
            self.page,
            {"trusted_profile": {"compliance": {"conflict_of_interest": False}}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.locator("#complianceToken").inner_text(), "No")

    def test_unknown_sensitive_required_question_blocks_structurally(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Legal">
                <p>Are you subject to a non-compete agreement?*</p>
                <button id="noncompete" aria-haspopup="listbox" aria-required="true">Select One Required</button>
                <div id="noncompeteOptions" role="listbox">
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(self.page, {})
        blocker = self.unresolved(result)[0]

        self.assertEqual(result.normalized_outcome(), OutcomeType.BLOCKED_ON_QUESTIONS.value)
        self.assertEqual(blocker["canonical_key"], "compliance.non_compete")
        self.assertEqual(blocker["risk_type"], "sensitive_compliance")
        self.assertNotIn(result.normalized_outcome(), {"stage_timeout", "timeout"})

    def test_unknown_required_current_value_without_trusted_answer_still_blocks(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Custom Question">
                <p>What is your custom required answer?*</p>
                <button id="custom" aria-haspopup="listbox" aria-required="true">Yes</button>
                <span data-automation-id="selectedItem">Yes</span>
                <div role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(self.page, {})
        blocker = self.unresolved(result)[0]

        self.assertEqual(result.normalized_outcome(), OutcomeType.BLOCKED_ON_QUESTIONS.value)
        self.assertTrue(blocker["canonical_key"].startswith("unknown_required::"))
        self.assertEqual(blocker["reason"], "unknown_required_question")
        self.assertEqual(blocker["current_value"], "Yes")
        self.assertNotEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)

    def test_unknown_required_current_value_matching_trusted_answer_can_complete(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Custom Question">
                <p>What is your custom required answer?*</p>
                <button id="custom" aria-haspopup="listbox" aria-required="true">Yes</button>
                <span data-automation-id="selectedItem">Yes</span>
                <div role="listbox" hidden>
                  <button type="button" role="option" class="fixture-option">Yes</button>
                  <button type="button" role="option" class="fixture-option">No</button>
                </div>
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(
            self.page,
            {"answer_library": {"What is your custom required answer?": "Yes"}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.unresolved(result), [])

    def test_placeholder_required_prompt_blocks_with_current_value(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Preference">
                <p>Which office location do you prefer?*</p>
                <button id="location" aria-haspopup="listbox" aria-required="true">0 items selected</button>
                <div id="locationOptions" role="listbox">
                  <button type="button" role="option" class="fixture-option">Austin</button>
                  <button type="button" role="option" class="fixture-option">Remote</button>
                </div>
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(self.page, {})
        blocker = self.unresolved(result)[0]

        self.assertEqual(result.normalized_outcome(), OutcomeType.BLOCKED_ON_QUESTIONS.value)
        self.assertEqual(blocker["current_value"], "0 items selected")
        self.assertIn(blocker["reason"], {"unknown_required_question", "trusted_answer_required", "placeholder"})

    def test_complete_application_questions_do_not_click_submit(self):
        self.set_content(
            """
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Application Questions">
                <p>When are you available to start?*</p>
                <label for="month">Month</label><input id="month" required value="12">
                <label for="day">Day</label><input id="day" required value="15">
                <label for="year">Year</label><input id="year" required value="2026">
              </section>
              <button id="submit" type="submit">Submit</button>
              <script>
                window.submitClicks = 0;
                submit.addEventListener("click", event => {
                  event.preventDefault();
                  window.submitClicks += 1;
                });
              </script>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(self.page, {})

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.evaluate("window.submitClicks"), 0)

    def test_application_questions_loading_requires_scoped_loading_and_can_stick(self):
        self.set_content(
            """
            <main aria-busy="true">
              <h1>Application Questions</h1>
              <p>Loading</p>
            </main>
            """
        )

        controller = ApplicationQuestionsController()
        context: dict = {}
        first = controller.run_pass(self.page, context)
        second = controller.run_pass(self.page, context)

        self.assertEqual(first.normalized_outcome(), OutcomeType.RETRYABLE.value)
        self.assertEqual(second.normalized_outcome(), OutcomeType.LOADING_STUCK.value)

    def test_unrelated_spinner_outside_application_questions_does_not_block_completion(self):
        self.set_content(
            """
            <div aria-busy="true" class="spinner">Loading recommendations</div>
            <main>
              <h1>Application Questions</h1>
              <section role="group" aria-label="Application Questions">
                <p>When are you available to start?*</p>
                <label for="month">Month</label><input id="month" required value="12">
                <label for="day">Day</label><input id="day" required value="15">
                <label for="year">Year</label><input id="year" required value="2026">
              </section>
            </main>
            """
        )

        result = ApplicationQuestionsController().run_pass(self.page, {})

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())


if __name__ == "__main__":
    unittest.main()
