from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.browser import launch_replay_browser
from adapters.workday.contracts import OutcomeType
from adapters.workday.controllers import MyExperienceController


class WorkdayMyExperienceControllerTests(unittest.TestCase):
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
        temp = tempfile.NamedTemporaryFile(delete=False, suffix="-resume.pdf")
        temp.write(b"%PDF-1.4\n% my experience controller test\n")
        temp.close()
        self.resume_path = Path(temp.name)

    def tearDown(self) -> None:
        self.context.close()
        self.resume_path.unlink(missing_ok=True)

    def set_content(self, body: str) -> None:
        self.page.set_content(f"<!doctype html><html><body>{body}</body></html>", wait_until="domcontentloaded")

    def unresolved_keys(self, result) -> set[str]:
        return {item["canonical_key"] for item in result.to_dict()["unresolved_required_fields"]}

    def test_northrop_style_education_placeholders_block_structurally(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="education" role="group" aria-label="Education">
                <label for="school">School or University*</label>
                <input id="school" role="combobox" aria-autocomplete="list" required
                  value="University of Illinois Urbana-Champaign">
                <label for="degree">Degree*</label>
                <button id="degree" aria-haspopup="listbox" aria-required="true">Select One</button>
                <label for="endYear">Expected End Year*</label>
                <input id="endYear" required placeholder="YYYY" value="YYYY">
              </section>
            </main>
            """
        )

        result = MyExperienceController().run_pass(self.page, {})
        unresolved = self.unresolved_keys(result)

        self.assertEqual(result.normalized_outcome(), OutcomeType.MY_EXPERIENCE_BLOCKED.value)
        self.assertIn("education.school", unresolved)
        self.assertIn("education.degree", unresolved)
        self.assertIn("education.end_year", unresolved)
        self.assertNotIn(result.normalized_outcome(), {"stage_timeout", "timeout"})

    def test_uiuc_school_requires_committed_token_and_prefers_primary_name(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section id="education" data-section="education" role="group" aria-label="Education">
                <label for="school">School or University*</label>
                <input id="school" role="combobox" aria-autocomplete="list" required
                  onfocus="schoolOptions.hidden=false" oninput="schoolOptions.hidden=false"
                  value="University of Illinois Urbana-Champaign">
                <span id="schoolToken" data-automation-id="selectedItem" hidden></span>
                <div id="schoolOptions" role="listbox" hidden>
                  <button type="button" role="option" id="uiucPrimary">University of Illinois Urbana-Champaign</button>
                  <button type="button" role="option" id="uiucAlias">University of Illinois at Urbana-Champaign</button>
                </div>
              </section>
              <script>
                const school = document.querySelector("#school");
                const schoolOptions = document.querySelector("#schoolOptions");
                for (const option of document.querySelectorAll("[role=option]")) {
                  option.addEventListener("click", event => {
                    school.value = event.target.textContent;
                    schoolToken.textContent = event.target.textContent;
                    schoolToken.hidden = false;
                    schoolOptions.hidden = true;
                  });
                }
              </script>
            </main>
            """
        )
        controller = MyExperienceController()
        before = controller.observe(self.page, {})

        result = controller.run_pass(
            self.page,
            {"trusted_profile": {"education": {"school": "University of Illinois at Urbana-Champaign"}}},
        )

        self.assertIn("education.school", {item["canonical_key"] for item in before.unresolved_required_fields})
        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value)
        self.assertEqual(self.page.locator("#schoolToken").inner_text(), "University of Illinois Urbana-Champaign")

    def test_autofill_pollution_is_rejected_and_replaced_from_trusted_profile(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="education" role="group" aria-label="Education">
                <label id="school-label" for="school">School or University*</label>
                <button id="school" aria-haspopup="listbox" aria-labelledby="school-label"
                  onclick="schoolOptions.hidden=false">TO_BE_REVIEWED_BY_APPLICANT</button>
                <span id="schoolToken" data-automation-id="selectedItem">TO_BE_REVIEWED_BY_APPLICANT</span>
                <div id="schoolOptions" role="listbox" hidden>
                  <button type="button" role="option" id="schoolOption">University of Illinois Urbana-Champaign</button>
                </div>
                <label id="degree-label" for="degree">Degree*</label>
                <button id="degree" aria-haspopup="listbox" aria-labelledby="degree-label"
                  onclick="degreeOptions.hidden=false">To Be Reviewed By Applicant</button>
                <span id="degreeToken" data-automation-id="selectedItem">To Be Reviewed By Applicant</span>
                <div id="degreeOptions" role="listbox" hidden>
                  <button type="button" role="option" id="degreeOption">Bachelor's Degree</button>
                </div>
              </section>
              <section data-section="experience" role="group" aria-label="Work Experience">
                <label for="company">Company*</label>
                <input id="company" required value="TO_BE_REVIEWED_BY_APPLICANT">
              </section>
              <script>
                schoolOption.addEventListener("click", event => {
                  school.textContent = event.target.textContent;
                  schoolToken.textContent = event.target.textContent;
                  schoolOptions.hidden = true;
                });
                degreeOption.addEventListener("click", event => {
                  degree.textContent = event.target.textContent;
                  degreeToken.textContent = event.target.textContent;
                  degreeOptions.hidden = true;
                });
              </script>
            </main>
            """
        )

        result = MyExperienceController().run_pass(
            self.page,
            {
                "trusted_profile": {
                    "education": {
                        "school": "University of Illinois Urbana-Champaign",
                        "degree": "Bachelor of Science",
                    },
                    "experience": {"company": "Example Research Lab"},
                }
            },
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.locator("#schoolToken").inner_text(), "University of Illinois Urbana-Champaign")
        self.assertEqual(self.page.locator("#degreeToken").inner_text(), "Bachelor's Degree")
        self.assertEqual(self.page.locator("#company").input_value(), "Example Research Lab")

    def test_work_experience_required_group_completes_or_blocks(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="experience" role="group" aria-label="Work Experience">
                <label for="title">Job Title*</label><input id="title" required>
                <label for="company">Company*</label><input id="company" required>
                <label for="location">Location*</label><input id="location" required>
                <label for="description">Description*</label><textarea id="description" required></textarea>
              </section>
            </main>
            """
        )

        blocked = MyExperienceController().run_pass(self.page, {})
        complete = MyExperienceController().run_pass(
            self.page,
            {
                "trusted_profile": {
                    "experience": {
                        "title": "Software Engineer Intern",
                        "company": "Example Research Lab",
                        "location": "Champaign, IL",
                        "description": "Built internal software tools.",
                    }
                }
            },
        )

        self.assertEqual(blocked.normalized_outcome(), OutcomeType.MY_EXPERIENCE_BLOCKED.value)
        self.assertIn("experience.company", self.unresolved_keys(blocked))
        self.assertIn("work_experience", blocked.to_dict()["unresolved_required_groups"])
        self.assertEqual(complete.normalized_outcome(), OutcomeType.COMPLETE.value)
        self.assertEqual(self.page.locator("#company").input_value(), "Example Research Lab")

    def test_resume_upload_requires_exact_marker_and_does_not_click_submit(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="resume" role="group" aria-label="Resume">
                <label for="resume">Upload Resume*</label>
                <input id="resume" type="file" required>
                <div id="status" data-automation-id="uploadStatus"></div>
              </section>
              <button id="submit" type="submit">Submit</button>
              <script>
                window.submitClicks = 0;
                document.querySelector("#submit").addEventListener("click", () => window.submitClicks += 1);
                document.querySelector("#resume").addEventListener("change", event => {
                  document.querySelector("#status").textContent = "Successfully Uploaded " + event.target.files[0].name;
                });
              </script>
            </main>
            """
        )

        result = MyExperienceController().run_pass(
            self.page,
            {"resume_path": str(self.resume_path), "confirm_submit": False},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        self.assertEqual(self.page.evaluate("window.submitClicks"), 0)

    def test_resume_upload_stale_or_missing_marker_blocks(self):
        for marker in ("", "Successfully Uploaded old-resume.pdf"):
            with self.subTest(marker=marker):
                self.set_content(
                    f"""
                    <main>
                      <h1>My Experience</h1>
                      <section data-section="resume" role="group" aria-label="Resume">
                        <label for="resume">Upload Resume*</label>
                        <input id="resume" type="file" required>
                        <div id="status" data-automation-id="uploadStatus">{marker}</div>
                      </section>
                    </main>
                    """
                )

                result = MyExperienceController().run_pass(self.page, {"resume_path": str(self.resume_path)})

                self.assertEqual(result.normalized_outcome(), OutcomeType.MY_EXPERIENCE_BLOCKED.value)
                self.assertIn("resume_upload", self.unresolved_keys(result))

    def test_hp_plain_school_and_degree_display_alias_are_already_complete(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="education" role="group" aria-label="Education">
                <label for="school">School or University*</label>
                <input id="school" required value="University of Illinois at Urbana-Champaign">
                <label id="degree-label" for="degree">Degree*</label>
                <button id="degree" aria-haspopup="listbox" aria-required="true"
                  aria-labelledby="degree-label">Bachelors (±16 years of education)</button>
                <span data-automation-id="selectedItem">Bachelors (±16 years of education)</span>
              </section>
            </main>
            """
        )

        result = MyExperienceController().run_pass(
            self.page,
            {
                "application_profile": {
                    "education": {
                        "school": "University of Illinois Urbana-Champaign",
                        "degree": "Bachelor of Science",
                    }
                }
            },
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        actions = [item for item in result.to_dict()["actions"] if item.get("acted")]
        self.assertEqual(actions, [])

    def test_workday_searchbox_selectinput_is_observed_as_prompt(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="education" role="group" aria-label="Education">
                <label for="field">Field of Study*</label>
                <input id="field" required data-automation-id="searchBox"
                  data-uxi-widget-type="selectinput" value="Computer Science and Linguistics">
                <span data-automation-id="selectedItem">Computer Science and Linguistics</span>
              </section>
            </main>
            """
        )

        snapshot = MyExperienceController().observe(
            self.page,
            {"application_profile": {"education": {"field": "Computer Science and Linguistics"}}},
        )
        field = next(item for item in snapshot.fields if item.canonical_key == "education.field")

        self.assertEqual(field.metadata["kind"], "prompt")
        self.assertEqual(field.normalized_status(), "filled")

    def test_hp_language_controls_are_known_and_match_profile_values(self):
        self.set_content(
            """
            <main>
              <h1>My Experience</h1>
              <section data-section="languages" role="group" aria-label="Languages">
                <label id="language-label" for="language">Language*</label>
                <button id="language" aria-haspopup="listbox" aria-required="true"
                  aria-labelledby="language-label">English</button>
                <span data-automation-id="selectedItem">English</span>
                <label id="overall-label" for="overall">Overall*</label>
                <button id="overall" aria-haspopup="listbox" aria-required="true"
                  aria-labelledby="overall-label">4 - Fluent</button>
                <span data-automation-id="selectedItem">4 - Fluent</span>
              </section>
            </main>
            """
        )

        result = MyExperienceController().run_pass(
            self.page,
            {"application_profile": {"languages": [{"language": "English", "overall": "Fluent"}]}},
        )

        self.assertEqual(result.normalized_outcome(), OutcomeType.COMPLETE.value, result.to_dict())
        keys = {item["canonical_key"] for item in result.to_dict()["fields"]}
        self.assertEqual(keys, {"language.name", "language.overall"})


if __name__ == "__main__":
    unittest.main()
